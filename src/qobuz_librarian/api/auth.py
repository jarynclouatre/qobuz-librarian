"""Auth helpers and shared exceptions.

Exceptions live here because everything imports them; having them in a
dedicated module avoids circular imports (client.py imports AuthLost from
here without needing to import anything back).

detect_auth_lost() lives here because it parses rip subprocess output for
auth signals - no API calls, no session needed.
"""
import contextlib
import fcntl
import hashlib
import json
import os
import re
import threading
import tomllib
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Callable

from qobuz_librarian import config, redaction, safefile, state_file
from qobuz_librarian.ui_cli.colors import C, fmt
from qobuz_librarian.ui_cli.logging import log


class AuthOutcome(StrEnum):
    ACCEPTED = "accepted"
    REJECTED = "rejected"
    TEMPORARY = "temporary"
    INCONCLUSIVE = "inconclusive"
    ENTITLEMENT = "entitlement"


@dataclass(frozen=True, slots=True)
class AuthEvidence:
    generation: str
    outcome: AuthOutcome


class AuthLost(Exception):
    def __init__(self, message="Qobuz rejected the saved token", *,
                 credential_generation: str = ""):
        super().__init__(message)
        self.credential_generation = credential_generation


class DownloadsRefused(AuthLost):
    """Qobuz takes the token for the catalogue but refuses every download,
    for the account or for the app id and secrets. Its text says which."""


class CatalogMiss(Exception): pass
class Aborted(Exception):     pass


class QobuzError(Exception):
    def __init__(self, message, *, status_code: int | None = None,
                 auth_outcome: AuthOutcome = AuthOutcome.INCONCLUSIVE):
        super().__init__(message)
        self.status_code = status_code
        self.auth_outcome = auth_outcome


# Qobuz reached its retry ceiling without a usable answer - the network is
# down, the request timed out, or the API is rate-limiting / 5xx-ing.
class QobuzUnavailable(Exception): pass


# A hook the web layer registers in its lifespan so the dashboard's "saved
# token isn't authenticating" banner reflects the most recent API call, not
# just the startup probe.
_auth_state_listeners: list[Callable[[AuthEvidence], None]] = []


def register_auth_state_listener(cb) -> None:
    """Register a callback for generation-bound authentication evidence."""
    if cb not in _auth_state_listeners:
        _auth_state_listeners.append(cb)


def notify_auth_state(outcome: AuthOutcome, *, token="",
                      generation: str = "") -> None:
    if outcome not in {
        AuthOutcome.ACCEPTED,
        AuthOutcome.REJECTED,
        AuthOutcome.ENTITLEMENT,
    }:
        return
    generation = generation or token_credential_generation(token)
    if not generation:
        return
    evidence = AuthEvidence(generation, outcome)
    for cb in list(_auth_state_listeners):
        try:
            cb(evidence)
        except Exception:
            pass


def classify_qobuz_status(status_code: int) -> AuthOutcome:
    """Classify HTTP evidence without guessing that every 4xx is bad auth."""
    if 200 <= status_code < 300:
        return AuthOutcome.ACCEPTED
    if status_code == 401:
        return AuthOutcome.REJECTED
    if status_code in {402, 403}:
        return AuthOutcome.ENTITLEMENT
    if status_code in {408, 425, 429} or status_code >= 500:
        return AuthOutcome.TEMPORARY
    return AuthOutcome.INCONCLUSIVE


_RAW_API_BODY_RE = re.compile(r"^((?:HTTP \d+|bad JSON) from [^:]+):\s+.+$", re.DOTALL)


def friendly_qobuz_error(e):
    """Strip the raw API response body from a QobuzError's message."""
    msg = str(e)
    m = _RAW_API_BODY_RE.match(msg)
    if m:
        return m.group(1)
    return msg


class NoCredsError(Exception):
    """No usable Qobuz credentials - env var or streamrip config."""


class DownloaderNotReady(Exception):
    """The API token works, but streamrip still lacks its user identity."""


class CredentialChanged(Exception):
    """The active credential changed while an action was being admitted."""


class QobuzEntitlementError(Exception):
    """Qobuz accepted the token but refused the requested account action."""


class CredentialToken(str):
    """A token string carrying the non-secret generation that loaded it."""

    def __new__(cls, value: str, generation: str):
        obj = super().__new__(cls, value)
        obj.credential_generation = generation
        return obj


@dataclass(frozen=True, slots=True)
class QobuzCredentials:
    user_id: str
    token: CredentialToken
    source: str
    generation: str

    @property
    def configured(self) -> bool:
        return bool(self.token)

    @property
    def downloader_ready(self) -> bool:
        return bool(self.token and self.user_id)


class QobuzAccess(StrEnum):
    SAVED_READ = "saved_read"
    LOCAL_ACTION = "local_action"
    CATALOGUE_ACTION = "catalogue_action"
    DOWNLOAD_ACTION = "download_action"


class AccessBlock(StrEnum):
    CONNECT_QOBUZ = "connect_qobuz"
    RECONNECT_QOBUZ = "reconnect_qobuz"
    ADD_USER_ID = "add_user_id"


@dataclass(frozen=True, slots=True)
class CapabilityDecision:
    allowed: bool
    block: AccessBlock | None = None
    live_check_required: bool = False
    credential_generation: str = ""


def credential_generation(user_id: str, token: str, *, source: str) -> str:
    """Return a stable, non-secret identity for one effective credential."""
    if not token:
        return ""
    value = "\0".join((source, user_id, token)).encode("utf-8")
    return hashlib.blake2s(
        value,
        digest_size=16,
        person=b"ql-qbz",
    ).hexdigest()


def credentials_from_values(user_id: str = "", token: str = "", *,
                            source: str = "settings") -> QobuzCredentials:
    user_id = str(user_id or "").strip()
    token = str(token or "").strip()
    # Registered as they are read, so a token printed with no parameter name
    # beside it is still masked out of the logs and the job records.
    redaction.register_secret(token)
    redaction.register_secret(user_id)
    generation = credential_generation(user_id, token, source=source)
    return QobuzCredentials(
        user_id=user_id,
        token=CredentialToken(token, generation),
        source=source,
        generation=generation,
    )


def token_credential_generation(token) -> str:
    generation = getattr(token, "credential_generation", "")
    if generation:
        return str(generation)
    return credential_generation("", str(token or ""), source="token")


def _read_streamrip_qobuz(*, strict: bool = False) -> dict:
    if not config.STREAMRIP_CONFIG.exists():
        return {}
    try:
        with open(config.STREAMRIP_CONFIG, "rb") as f:
            cfg = tomllib.load(f)
    except Exception as exc:
        if strict:
            raise NoCredsError(
                f"Couldn't parse streamrip config: {exc}"
            ) from exc
        return {}
    qobuz = cfg.get("qobuz") or {}
    if not qobuz.get("use_auth_token"):
        return {}
    return {
        "user_id": str(qobuz.get("email_or_userid", "") or "").strip(),
        "token": str(qobuz.get("password_or_token", "") or "").strip(),
    }


def read_qobuz_credentials(*, strict: bool = False) -> QobuzCredentials:
    """Read the effective API token and matching downloader identity."""
    env_token = str(config.QOBUZ_USER_AUTH_TOKEN or "").strip()
    if env_token:
        # Environment credentials are authoritative. A malformed fallback
        # file must not disable them; read it only as an optional source for a
        # matching user id.
        streamrip = _read_streamrip_qobuz(strict=False)
        user_id = str(config.QOBUZ_USER_ID or "").strip()
        if (not user_id and streamrip.get("token") == env_token):
            user_id = streamrip.get("user_id", "")
        return credentials_from_values(user_id, env_token, source="env")
    streamrip = _read_streamrip_qobuz(strict=strict)
    user_id = str(config.QOBUZ_USER_ID or "").strip()
    return credentials_from_values(
        user_id or streamrip.get("user_id", ""),
        streamrip.get("token", ""),
        source="streamrip",
    )


def qobuz_capability(access: QobuzAccess, credentials: QobuzCredentials, *,
                     auth_valid: bool | None) -> CapabilityDecision:
    """Describe offer-time access. Remote actions still need a live check."""
    if access in {QobuzAccess.SAVED_READ, QobuzAccess.LOCAL_ACTION}:
        return CapabilityDecision(True)
    if not credentials.configured:
        return CapabilityDecision(False, AccessBlock.CONNECT_QOBUZ)
    if auth_valid is False:
        return CapabilityDecision(
            False,
            AccessBlock.RECONNECT_QOBUZ,
            credential_generation=credentials.generation,
        )
    if access is QobuzAccess.DOWNLOAD_ACTION and not credentials.downloader_ready:
        return CapabilityDecision(
            False,
            AccessBlock.ADD_USER_ID,
            credential_generation=credentials.generation,
        )
    return CapabilityDecision(
        True,
        live_check_required=True,
        credential_generation=credentials.generation,
    )


def load_qobuz_credentials() -> QobuzCredentials:
    credentials = read_qobuz_credentials(strict=True)
    if not credentials.configured:
        raise NoCredsError(
            "No Qobuz credentials found. Set QOBUZ_USER_AUTH_TOKEN, or "
            "open the Settings page in the web UI. "
            f"(streamrip config expected at {config.STREAMRIP_CONFIG})"
        )
    return credentials


def load_qobuz_token():
    """Return (user_id, token)."""
    credentials = load_qobuz_credentials()
    return credentials.user_id, credentials.token


# Every change the app makes to STREAMRIP_CONFIG reads, edits and replaces the
# whole file; one at a time, so none undoes another.
_STREAMRIP_CONFIG_LOCK = threading.Lock()


def write_streamrip_creds(user_id, auth_token) -> bool:
    """Write Qobuz creds into the streamrip config at STREAMRIP_CONFIG.

    Returns False if the config dir/file isn't writable (NAS perms) so
    callers can surface a clear message instead of crashing. Parses with
    tomlkit so the seeded default's inline docs/ordering survive the
    round-trip. Atomic (tmp + os.replace) so a kill mid-write can't leave
    a half-written config that breaks auth on next start. Only a
    credentials save on Settings writes it; downloads use a copy.
    """
    with _STREAMRIP_CONFIG_LOCK:
        return _write_streamrip_creds(user_id, auth_token)


def _default_streamrip_doc():
    """The bundled streamrip config, or the least of one when this install
    ships none (e.g. a pipx install)."""
    import tomlkit
    _pkg_root = Path(__file__).resolve().parents[3]
    for cand in (Path("/app/docker/streamrip-default.toml"),
                 _pkg_root / "docker" / "streamrip-default.toml"):
        if cand.exists():
            try:
                return tomlkit.parse(cand.read_text(encoding="utf-8"))
            except Exception:
                pass
    doc = tomlkit.document()
    import importlib.metadata as _im
    try:
        _srv = _im.version("streamrip")
    except _im.PackageNotFoundError:
        _srv = None
    doc["misc"] = tomlkit.table()
    if _srv:
        doc["misc"]["version"] = _srv
    doc["database"] = tomlkit.table()
    doc["database"]["failed_downloads_enabled"] = True
    return doc


def _write_streamrip_creds(user_id, auth_token):
    import tomlkit
    try:
        config.STREAMRIP_CONFIG.parent.mkdir(parents=True, exist_ok=True)
    except OSError:
        return False
    doc = None
    if config.STREAMRIP_CONFIG.exists():
        try:
            doc = tomlkit.parse(config.STREAMRIP_CONFIG.read_text(encoding="utf-8"))
        except Exception:
            doc = None
    if doc is None:
        doc = _default_streamrip_doc()
    if "qobuz" not in doc:
        doc["qobuz"] = tomlkit.table()
    doc["qobuz"]["email_or_userid"]   = user_id
    doc["qobuz"]["password_or_token"] = auth_token
    doc["qobuz"]["use_auth_token"]    = True
    # streamrip 2.2.0 REQUIRES the `secrets` key to exist (it's a required
    # field on QobuzConfig - deleting it makes the whole config fail to load).
    if "secrets" not in doc["qobuz"]:
        doc["qobuz"]["secrets"] = tomlkit.array()
    return _save_streamrip_config(doc)


def _save_streamrip_config(doc, *, mode=0o600) -> bool:
    """Replace the file the config path names, through any symlink. It holds
    the account token, so by default only its owner can read it."""
    import tomlkit
    try:
        safefile.write_atomic(
            os.path.realpath(config.STREAMRIP_CONFIG), tomlkit.dumps(doc),
            mode=mode, prefix=".streamrip.")
    except OSError:
        return False
    return True


def _table(doc, name):
    import tomlkit
    if name not in doc:
        doc[name] = tomlkit.table()
    return doc[name]


def _apply_run_settings(doc):
    """Set in one run's copy what the app's downloads depend on, whatever the
    config itself says: the credentials in use, the staging folder, Disc N
    folders (without them, tracks that share a number and title on different
    discs overwrite each other), each single in its own folder, no booklets,
    no downloads database (it blocks downloading again a track removed by
    hand), no update check, and [artwork] from ARTWORK. Tracks carry art only
    when ARTWORK embeds it; the cover file is always saved, since the import
    files it beside the tracks or embeds it."""
    credentials = read_qobuz_credentials()
    qobuz = _table(doc, "qobuz")
    if credentials.configured:
        qobuz["email_or_userid"] = credentials.user_id
        qobuz["password_or_token"] = str(credentials.token)
        qobuz["use_auth_token"] = True
    if not (qobuz.get("app_id") and qobuz.get("secrets")):
        fetched = _fetched_app_id()
        if fetched is not None:
            qobuz["app_id"], qobuz["secrets"] = fetched
    for key, value in (("app_id", ""), ("secrets", [])):
        if key not in qobuz:
            qobuz[key] = value
    qobuz["download_booklets"] = False
    downloads = _table(doc, "downloads")
    downloads["folder"] = str(config.STAGING_DIR)
    downloads["disc_subdirectories"] = True
    _table(doc, "filepaths")["add_singles_to_folder"] = True
    database = _table(doc, "database")
    database["downloads_enabled"] = False
    for key, name in (("downloads_path", "downloads.db"),
                      ("failed_downloads_path", "failed_downloads.db")):
        path = str(database.get(key) or "")
        if not path or not Path(path).expanduser().parent.is_dir():
            database[key] = str(_streamrip_run_dir() / name)
    _table(doc, "misc")["check_for_updates"] = False
    artwork = _table(doc, "artwork")
    artwork["embed"] = config.ARTWORK in ("embed", "both")
    artwork["save_artwork"] = True


_outdated_noted = False


@contextlib.contextmanager
def streamrip_run_config():
    """Yield the config path for one `rip` run: a private copy of
    STREAMRIP_CONFIG, or of the bundled default when there is none, with what
    the app's downloads depend on set in it. The config itself is never
    changed; the app id and secrets streamrip fetches on a run are kept in
    the app's data folder for the next. Yields None when the copy can't be
    written."""
    global _outdated_noted
    import tempfile

    import tomlkit
    try:
        if config.STREAMRIP_CONFIG.exists():
            doc = tomlkit.parse(config.STREAMRIP_CONFIG.read_text(encoding="utf-8"))
        else:
            doc = _default_streamrip_doc()
        version = (doc.get("misc") or {}).get("version")
        _apply_run_settings(doc)
        text = tomlkit.dumps(doc)
        sent = tomllib.loads(text)["qobuz"]
    except OSError:
        yield None
        return
    except Exception:
        # streamrip says what is wrong with a config it can't read.
        yield config.STREAMRIP_CONFIG
        return
    copy = fd = None
    try:
        directory = _streamrip_run_dir()
        fd, copy = tempfile.mkstemp(
            dir=directory, prefix=_RUN_COPY_PREFIX, suffix=".toml")
        fcntl.flock(fd, fcntl.LOCK_EX)
        data = text.encode("utf-8")
        while data:
            data = data[os.write(fd, data):]
    except OSError:
        if copy is not None:
            Path(copy).unlink(missing_ok=True)
        if fd is not None:
            os.close(fd)
        yield None
        return
    try:
        yield Path(copy)
    finally:
        try:
            with open(copy, "rb") as f:
                ran = tomllib.load(f)
        except (OSError, ValueError):
            ran = {}
        finally:
            Path(copy).unlink(missing_ok=True)
            os.close(fd)
        qobuz = ran.get("qobuz") or {}
        fetched = (qobuz.get("app_id"), qobuz.get("secrets"))
        if all(fetched) and fetched != (sent["app_id"], sent["secrets"]):
            _keep_fetched_app_id(*fetched)
        updated = (ran.get("misc") or {}).get("version")
        if updated and version and updated != version and not _outdated_noted:
            _outdated_noted = True
            log.info(fmt(
                C.GRAY,
                f"  {config.STREAMRIP_CONFIG} is from streamrip {version}; "
                f"downloads use a copy updated to {updated}, and the file "
                "itself is left as it is."))


_RUN_COPY_PREFIX = ".streamrip-run-"


def _streamrip_run_dir():
    """The app's own folder for the per-run copies, which hold the
    credentials: under DATA_DIR, never beside the user's config."""
    directory = Path(config.DATA_DIR) / "streamrip-runs"
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    os.chmod(directory, 0o700)
    return directory


def sweep_streamrip_run_copies():
    """Delete the per-run config copies a killed run left behind.

    A run holds a lock on its copy until it deletes it, so any copy that
    can be locked and has been written belongs to no live run."""
    directory = Path(config.DATA_DIR) / "streamrip-runs"
    try:
        leftovers = sorted(directory.glob(f"{_RUN_COPY_PREFIX}*.toml"))
    except OSError:
        return
    for path in leftovers:
        try:
            fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
        except OSError:
            continue
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            if os.fstat(fd).st_size:
                path.unlink(missing_ok=True)
        except OSError:
            pass
        finally:
            os.close(fd)


_FETCHED_APP_ID = "qobuz-app-id.json"


def _fetched_app_id():
    try:
        with open(_streamrip_run_dir() / _FETCHED_APP_ID, encoding="utf-8") as f:
            saved = json.load(f)
        app_id, secrets = str(saved["app_id"]), [str(s) for s in saved["secrets"]]
    except (OSError, ValueError, KeyError, TypeError):
        return None
    return (app_id, secrets) if app_id and secrets else None


def _keep_fetched_app_id(app_id, secrets):
    """Save the app id and secrets streamrip fetched on a run, which it would
    have saved in its config, in the app's data folder instead."""
    try:
        state_file.write_json(
            _streamrip_run_dir() / _FETCHED_APP_ID,
            {"app_id": str(app_id), "secrets": [str(s) for s in secrets]},
            indent=None)
    except OSError:
        pass


def check_env_credentials():
    """Warn when the environment sets a token but no user id goes with it:
    `rip` cannot authenticate from the token alone."""
    credentials = read_qobuz_credentials()
    if credentials.source == "env" and not credentials.user_id:
        log.warning(fmt(
            C.YELLOW,
            "  ⚠  QOBUZ_USER_AUTH_TOKEN is set but QOBUZ_USER_ID is not - "
            "downloads need both. Set QOBUZ_USER_ID (or save credentials on "
            "the Settings page); `rip` cannot authenticate from the token "
            "alone."))


def detect_auth_lost(rip_output):
    """Heuristic check on rip's combined stdout/stderr for auth failures.

    'http 401' (with the protocol prefix) avoids matching plain track numbers.
    'user authentication failed' avoids matching unrelated debug noise.
    """
    o = rip_output.lower()
    # These markers are specific enough to be safe anywhere in the output.
    if any(s in o for s in (
            "http 401",
            "user authentication failed",
            "authenticationerror",
            "invalid credentials")):
        return True
    # "unauthorized" is also a real word in album/track titles (e.g. "The
    # Unauthorized Biography of Reinhold Messner"), and streamrip echoes
    # titles in its progress output.
    for line in o.splitlines():
        if "unauthor" in line and any(k in line for k in (
                "error", "exception", "traceback", "401", "403",
                "denied", "fail")):
            return True
    return False


def _config_sets_app_id():
    try:
        qobuz = tomllib.loads(
            config.STREAMRIP_CONFIG.read_text(encoding="utf-8")).get("qobuz") or {}
    except (OSError, ValueError):
        return False
    return bool(qobuz.get("app_id") and qobuz.get("secrets"))


def downloads_refused(rip_output):
    """The DownloadsRefused a rip's output shows, or None. Streamrip's
    login names the account (a free or lapsed subscription) or the app id and
    secrets; the app's own saved ones are dropped so the next run fetches
    new ones."""
    o = rip_output.lower()
    if "ineligibleerror" in o:
        return DownloadsRefused(
            "Qobuz accepted the token, but this account can't download "
            "music: free and lapsed subscriptions can't. Check the Qobuz "
            "subscription, then try again.")
    if "invalidappiderror" not in o and "invalidappsecreterror" not in o:
        return None
    if _config_sets_app_id():
        return DownloadsRefused(
            "Qobuz refused the app id and secrets set in "
            f"{config.STREAMRIP_CONFIG}. Empty app_id and secrets there so "
            "the next download fetches new ones.")
    try:
        (_streamrip_run_dir() / _FETCHED_APP_ID).unlink(missing_ok=True)
    except OSError:
        pass
    return DownloadsRefused(
        "Qobuz refused the app id and secrets used for downloads. They were "
        "cleared, so the next download fetches new ones; try again.")


def detect_rate_limited(rip_output):
    """Heuristic: did Qobuz throttle this rip? Only fires on explicit
    rate-limit signals. streamrip giving up on a track after its retries is
    usually a truncated stream, and names a 429 when that was the cause."""
    o = rip_output.lower()
    return any(s in o for s in (
        "http 429",
        "too many requests",
        "rate limit",
        "ratelimit",
    ))


def detect_disk_full(rip_output):
    """Heuristic: did the rip/import run out of disk space?"""
    o = rip_output.lower()
    return any(s in o for s in (
        "no space left on device",
        "errno 28",
        "oserror: [errno 28]",
        "disk quota exceeded",
        "errno 122",
    ))
