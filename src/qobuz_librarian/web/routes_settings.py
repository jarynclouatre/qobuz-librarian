"""Routes for the Settings page."""
import asyncio
import logging
import os
import shutil

from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response

from qobuz_librarian import config as cfg
from qobuz_librarian import run_lock, state_file
from qobuz_librarian.api import client as api_client
from qobuz_librarian.api import lastfm
from qobuz_librarian.api.auth import AuthEvidence, AuthOutcome, credentials_from_values
from qobuz_librarian.integrations import downsample_engine
from qobuz_librarian.library import generation_state
from qobuz_librarian.quality import upgrade_state
from qobuz_librarian.ui_cli.colors import format_size
from qobuz_librarian.web import auth as web_auth
from qobuz_librarian.web import jobs as job_mgr
from qobuz_librarian.web import runtime, settings_store

router = APIRouter()
_log = logging.getLogger("qobuz_librarian")


@router.head("/settings")
async def settings_head():
    return Response(status_code=200)


def _web_login_env_managed() -> bool:
    """Whether Compose is still carrying a web password."""
    return bool(os.environ.get("WEB_AUTH_PASSWORD", "").strip()
                or os.environ.get("WEB_AUTH_PASSWORD_FILE", "").strip())


def _settings_response(request, *, saved=False, queued=False, connected=False,
                       unverified=False, envchecked=False,
                       rerendered=False,
                       error="", mode="", user_id=None,
                       auth_token_prefill="", diagnostics=None, warnings=None,
                       quality_note=False, password_error="",
                       password_locked=False, lastfm_check="",
                       status_code=200):
    creds = runtime._read_creds()
    values = settings_store.current()
    # If credentials come from environment or a secret-file declaration,
    # anything saved via the form lacks authority, so let the user know.
    creds_from_env = _qobuz_token_is_env_owned()
    cli_only_env = os.environ.get("QL_CLI_ONLY", "").strip().lower() in (
        "1", "true", "yes", "on")
    # Two separate facts. disk_usage() measures the FILESYSTEM the music folder
    # sits on, never the folder. It was labelled "Music folder: 3.31 TB used"
    # while the folder held 1.6 MB. The library's own size comes from the census
    # the Library page already shows, and the volume figure is labelled by what
    # it actually covers: its own mount (the usual Docker bind, or a dataset
    # with a quota) or a disk shared with everything else on the machine.
    music_storage = None
    try:
        du = shutil.disk_usage(cfg.MUSIC_ROOT)

        music_storage = {
            "free": format_size(du.free), "total": format_size(du.total),
            "pct": round(du.used / du.total * 100, 1) if du.total else 0,
            "own_volume": runtime._is_mount_point(cfg.MUSIC_ROOT),
        }
    except OSError:
        pass
    census = runtime._census_view()
    library_size = census.get("total") if census else ""
    return runtime._tr(request, "settings.html", {
        "music_storage": music_storage,
        "library_size": library_size,
        # True once Qobuz has accepted the saved token, False once it has
        # rejected it, None when it has never been asked.
        "token_verified": runtime._token_valid_for(),
        "user_id": (
            cfg.QOBUZ_USER_ID or creds.get("user_id", "")
            if user_id is None else user_id
        ),
        "auth_token_set": bool(creds.get("auth_token")),
        "downloader_ready": bool(
            creds.get("auth_token") and creds.get("user_id")
        ),
        "auth_token_prefill": auth_token_prefill,
        "credential_generation": creds.get("_generation", ""),
        "creds_from_env": creds_from_env,
        "env_user_id_set": bool(
            cfg.QOBUZ_USER_ID or os.environ.get("QOBUZ_USER_ID", "").strip()
        ),
        "cli_only_env": cli_only_env,
        "mode_changed": (mode or "").strip().lower(),
        "saved": saved,
        "queued": queued,
        "quality_note": quality_note,
        "connected": connected,
        "unverified": unverified,
        "envchecked": envchecked,
        "rerendered": rerendered,
        "error": error,
        "warnings": warnings or [],
        # Every warning a save can raise is about a field in the collapsed
        # defaults section, and it re-rendered closed: the notice named a
        # value the reader could not see or correct without hunting for it.
        "defaults_open": bool(warnings) or error in {
            "invalidsettings", "settingschanged"
        },
        "page": "settings",
        "library_paths": [
            {"label": label, "container": cp,
             "host": host, "resolved": resolved}
            for label, cp in (
                ("Music library", cfg.MUSIC_ROOT),
                ("Staging area", cfg.STAGING_DIR),
                ("Beets database", cfg.BEETS_DB_PATH),
                ("Streamrip config", cfg.STREAMRIP_CONFIG),
            )
            for host, resolved in [runtime._resolve_host_path(cp)]
        ],
        "behavior_fields": settings_store.BEHAVIOR_FIELDS,
        "inert_notes": settings_store.inert_behaviour_notes(
            values, have_downsample=downsample_engine.HAVE_DOWNSAMPLE),
        "text_fields": settings_store.TEXT_FIELDS,
        "behavior_generation": settings_store.form_generation(
            "behaviour", values),
        "lastfm_generation": settings_store.form_generation(
            "discover", values),
        "collection_backup_generation": settings_store.form_generation(
            "collection-backup", values),
        "collection_backup": runtime._collection_backup_status(),
        "lastfm_check": (lastfm_check
                         if lastfm_check in ("ok", "rejected", "down") else ""),
        "option_labels": settings_store.ENUM_OPTION_LABELS,
        # Which dropdowns keep their unset entry after something is picked, so
        # a choice the app asks for once can still be handed back to it.
        "unset_enum_keys": settings_store.UNSET_ENUM_KEYS,
        "behavior": values,
        # The worst store to lose without being told: quality tier and
        # downsample policy revert to the env defaults and this page then shows
        # them as if they were chosen.
        "corrupt_stores": state_file.corrupt_store_details(),
        "settings_file_reset": any(
            store["original"] == settings_store.SETTINGS_FILE.name
            for store in state_file.corrupt_store_details()),
        "backup_dir_cleared": settings_store.cleared_backup_dir_notice(),
        "diagnostics_html": runtime._diagnostics_fragment(request, diagnostics),
        "web_login_available": (not web_auth.auth_disabled()
                                and web_auth.credentials_configured()),
        "web_login_username": web_auth.current_username(),
        "web_login_env_managed": _web_login_env_managed(),
        "password_error": password_error,
        "password_locked": password_locked,
    }, status_code=status_code)


# The outcomes Settings words itself; any other error in its URL is a notice key.
_SETTINGS_ERROR_CODES = frozenset({
    "creds", "credsbusy", "credschanged", "credsmode", "empty", "envcreds",
    "envrejected", "envunreachable", "invalidsettings", "needuser", "persist",
    "rejected", "settingschanged", "unreachable",
})


@router.get("/settings", response_class=HTMLResponse)
async def settings_page(request: Request, saved: bool = False,
                        queued: bool = False, connected: bool = False,
                        unverified: bool = False, envchecked: bool = False,
                        error: str = "",
                        mode: str = "", quality_note: bool = False,
                        lastfm: str = ""):
    loop = asyncio.get_running_loop()
    diags = await loop.run_in_executor(None, runtime._diagnostics)
    if error not in _SETTINGS_ERROR_CODES:
        error = runtime._notice_text(error)
    return _settings_response(request, saved=saved, queued=queued,
                              connected=connected, unverified=unverified,
                              envchecked=envchecked,
                              error=error, mode=mode, diagnostics=diags,
                              quality_note=quality_note, lastfm_check=lastfm)


def _qobuz_token_is_env_owned() -> bool:
    """Whether environment configuration owns the Qobuz token slot."""
    return bool(
        cfg.QOBUZ_USER_AUTH_TOKEN
        or os.environ.get("QOBUZ_USER_AUTH_TOKEN", "").strip()
        or os.environ.get("QOBUZ_USER_AUTH_TOKEN_FILE", "").strip()
    )


async def _classify_token_async(loop, token):
    """Ask Qobuz whether a token still works, within the page's time budget.
    A timeout is TEMPORARY, the same as an unreachable API."""
    try:
        return await asyncio.wait_for(
            loop.run_in_executor(
                None,
                lambda: api_client.call_within(
                    cfg.WEB_TEST_AUTH_TIMEOUT, runtime._classify_token, token),
            ),
            timeout=cfg.WEB_TEST_AUTH_TIMEOUT,
        )
    except asyncio.TimeoutError:
        return AuthOutcome.TEMPORARY


@router.post("/settings", response_class=HTMLResponse)
async def save_settings(
    request: Request,
    user_id: str = Form(""),
    auth_token: str = Form(""),
    credential_generation: str = Form(""),
):
    loop = asyncio.get_running_loop()
    diags = await loop.run_in_executor(None, runtime._diagnostics)
    existing = runtime._read_creds()
    # Environment and secret-file credentials are authoritative for the live
    # process. Refuse a form value that claims to replace one: writing it only
    # to streamrip would report success while the app kept using the env value,
    # and startup sync would overwrite the shadow value again. A token-only env
    # may still use this page to supply streamrip's required user id.
    env_owned = _qobuz_token_is_env_owned()
    env_token = cfg.QOBUZ_USER_AUTH_TOKEN
    env_user_id = cfg.QOBUZ_USER_ID
    if ((env_owned and (
            not env_token
            or (auth_token.strip() and auth_token.strip() != env_token)))
            or (env_user_id and user_id.strip()
                and user_id.strip() != env_user_id)):
        return _settings_response(
            request,
            error="envcreds",
            user_id=existing.get("user_id", ""),
            auth_token_prefill="",
            diagnostics=diags,
        )
    submitted_generation = str(credential_generation or "").strip()
    existing_generation = str(existing.get("_generation") or "")
    if submitted_generation != existing_generation:
        return _settings_response(
            request,
            error="credschanged",
            diagnostics=diags,
        )
    # First-run with empty inputs: nothing to save and no creds to keep,
    # bounce back with a banner rather than writing blanks and flashing green.
    if not auth_token.strip() and not user_id.strip() \
            and not existing.get("auth_token") \
            and not cfg.QOBUZ_USER_AUTH_TOKEN:
        return RedirectResponse(url="/settings?error=empty", status_code=303)
    # Blank means "keep the existing value": the fields are not pre-filled,
    # so an empty submission must not wipe a previously-saved credential.
    if not auth_token.strip() and not user_id.strip() and cfg.QOBUZ_USER_AUTH_TOKEN:
        # Blank submit with the credential coming from the environment: there
        # is nothing this page can write, so check the token actually in use
        # and report that. Reporting a save here told someone with a dead
        # environment token that everything was fine.
        verdict = await _classify_token_async(loop, cfg.QOBUZ_USER_AUTH_TOKEN)
        if verdict == AuthOutcome.REJECTED:
            return RedirectResponse(url="/settings?error=envrejected",
                                    status_code=303)
        if verdict in {AuthOutcome.ACCEPTED, AuthOutcome.ENTITLEMENT}:
            runtime._on_auth_state(
                AuthEvidence(runtime._credentials_snapshot().generation, verdict))
            return RedirectResponse(url="/settings?envchecked=1",
                                    status_code=303)
        return RedirectResponse(url="/settings?error=envunreachable",
                                status_code=303)
    new_token = auth_token.strip() or existing.get("auth_token", "")
    new_uid = env_user_id.strip() or user_id.strip() or existing.get("user_id", "")
    if new_uid and not new_token:
        return _settings_response(request, error="empty",
                                  user_id=user_id.strip(),
                                  auth_token_prefill=auth_token.strip(),
                                  diagnostics=diags)
    verdict = AuthOutcome.TEMPORARY
    if new_token:
        probe = credentials_from_values(
            new_uid,
            new_token,
            source="env" if env_owned else "streamrip",
        )
        verdict = await _classify_token_async(loop, probe.token)
    if verdict == AuthOutcome.REJECTED:
        # Re-render with the real token still in the (password-type, so
        # visually masked) field so the user can fix a paste slip without
        # re-typing it, same as the needuser/empty/creds branches.
        return _settings_response(request, error="rejected",
                                  user_id=user_id.strip(),
                                  auth_token_prefill=auth_token.strip(),
                                  diagnostics=diags)
    if (verdict in {AuthOutcome.TEMPORARY, AuthOutcome.INCONCLUSIVE}
            and new_token and runtime._token_valid_for() is True
            and new_token != existing.get("auth_token", "")):
        # Couldn't check it, and the token already saved is one that has
        # authenticated. Overwriting a known-good credential with an unproven
        # one, and then reporting "Connected", is how a working install
        # became a broken one during a network blip. A save that keeps the
        # same token (blank field, or a user-id-only edit) overwrites
        # nothing and passes.
        return _settings_response(request, error="unreachable",
                                  user_id=user_id.strip(),
                                  auth_token_prefill=auth_token.strip(),
                                  diagnostics=diags)
    with runtime._auto_check_lock, runtime._CREDENTIAL_LOCK:
        active_credentials = runtime._credentials_snapshot()
        candidate_credentials = credentials_from_values(
            new_uid,
            new_token,
            source="env" if env_owned else "streamrip",
        )
        if active_credentials.generation != submitted_generation:
            return _settings_response(
                request,
                error="credschanged",
                diagnostics=diags,
            )
        credential_changed = (
            candidate_credentials.generation
            != active_credentials.generation
        )
        if credential_changed and (
            runtime.shutting_down()
            or runtime.cli_mode()
            or runtime.lock_busy_pid() is not None
            or runtime.lock_unenforceable()
            or not runtime._run_lock_intact()
        ):
            return _settings_response(
                request,
                error="credsmode",
                user_id=user_id.strip(),
                auth_token_prefill=auth_token.strip(),
                diagnostics=diags,
            )
        credential_work_running = any(
            (getattr(job, "execute_kind", "") or "download")
            not in {"downsample", "lyrics", "migration", "collection_snapshot"}
            and job.status != job_mgr.JobStatus.AWAITING_REVIEW
            for job in job_mgr.registry.pending_and_running()
        )
        if (
            credential_changed
            and credential_work_running
        ):
            return _settings_response(
                request,
                error="credsbusy",
                user_id=user_id.strip(),
                auth_token_prefill=auth_token.strip(),
                diagnostics=diags,
            )
        ok = runtime._write_creds(new_uid, new_token)
        if not ok:
            return _settings_response(request, error="creds",
                                      user_id=user_id.strip(),
                                      auth_token_prefill=auth_token.strip(),
                                      diagnostics=diags)
        saved_credentials = runtime._credentials_snapshot()
        if verdict not in {AuthOutcome.ACCEPTED, AuthOutcome.ENTITLEMENT}:
            runtime.set_token_state(None, saved_credentials.generation)
    if verdict in {AuthOutcome.ACCEPTED, AuthOutcome.ENTITLEMENT}:
        runtime._on_auth_state(AuthEvidence(saved_credentials.generation, verdict))
    suffix = (
        "&unverified=1"
        if verdict in {AuthOutcome.TEMPORARY, AuthOutcome.INCONCLUSIVE}
        else ""
    )
    return RedirectResponse(url=f"/settings?connected=1{suffix}", status_code=303)


@router.post("/settings/behavior", response_class=HTMLResponse)
async def save_behavior(request: Request):
    form = await request.form()
    # The single-field sections (Discover, Collection backup) name themselves
    # so a save returns the reader to the section it came from.
    anchor = str(form.get("section") or "")
    if anchor not in ("discover", "collection-backup"):
        anchor = "behaviour"
    submitted_generation = str(form.get("settings_generation") or "")
    allowed_keys = set(settings_store.FORM_KEYS[anchor])
    def _posted_bool(key):
        return form.get(key, "").strip().lower() not in (
            "0", "false", "off", "no", ""
        )
    is_complete = "form_complete" in form
    if is_complete and anchor == "behaviour":
        values = {k: (_posted_bool(k) if k in form else False)
                  for k in settings_store.BEHAVIOR_KEYS}
    else:
        values = {k: _posted_bool(k)
                  for k in settings_store.BEHAVIOR_KEYS
                  if k in allowed_keys and k in form}
    # Text/enum/list fields: take whatever the form posted; absent =
    # leave unchanged (don't wipe a previously-set value).
    for k in settings_store.TEXT_KEYS:
        if k in allowed_keys and k in form:
            values[k] = form.get(k, "")
    # The saved Last.fm key is never sent to the page, so its box always
    # arrives blank: blank keeps the key, and only Remove key clears it.
    if anchor == "discover":
        if form.get("lastfm_remove") == "1":
            values["LASTFM_API_KEY"] = ""
        elif not str(values.get("LASTFM_API_KEY") or "").strip():
            values.pop("LASTFM_API_KEY", None)
    effective_before = settings_store.current()
    quality_before = (
        str(effective_before.get("STREAMRIP_QUALITY", "")),
        bool(effective_before.get("PREFER_HIRES", False)),
        bool(effective_before.get("SUPPRESS_SINGLE_TRACK_GAPS", False)),
    )
    try:
        ok, warnings = settings_store.save_from_form(
            values,
            anchor,
            submitted_generation,
        )
    except settings_store.SettingsChanged:
        loop = asyncio.get_running_loop()
        diags = await loop.run_in_executor(None, runtime._diagnostics)
        return _settings_response(
            request,
            error="settingschanged",
            diagnostics=diags,
            rerendered=True,
        )
    if ok is None:
        # Re-render rather than redirect so the reason can name the field and
        # say what to change; a redirect can only carry the generic code.
        loop = asyncio.get_running_loop()
        diags = await loop.run_in_executor(None, runtime._diagnostics)
        return _settings_response(request, error="invalidsettings",
                                  warnings=warnings, diagnostics=diags,
                                  rerendered=True)
    # A quality or singles change leaves a saved Upgrade review promising
    # targets the settings no longer produce, and a saved Library scan whose
    # lists were built under the old policy (library_scan_state signature).
    quality_note = False
    effective_after = settings_store.current()
    if quality_before != (
        str(effective_after.get("STREAMRIP_QUALITY", "")),
        bool(effective_after.get("PREFER_HIRES", False)),
        bool(effective_after.get("SUPPRESS_SINGLE_TRACK_GAPS", False)),
    ):
        loop = asyncio.get_running_loop()
        state = await loop.run_in_executor(None, upgrade_state.load)
        quality_note = (
            bool((state or {}).get("candidates"))
            or await loop.run_in_executor(
                None, generation_state.baseline_complete))
    # Durable publication is the settings store's admission point; failure
    # leaves both the live config and any deferred overlay unchanged.
    if not ok:
        return RedirectResponse(url=f"/settings?error=persist#{anchor}", status_code=303)
    if warnings:
        # Re-render in place so we can name exactly which entries were dropped
        # (a misspelt provider, an uninstalled beets plugin) without smuggling
        # user-typed values through the redirect URL.
        loop = asyncio.get_running_loop()
        diags = await loop.run_in_executor(None, runtime._diagnostics)
        # Re-rendering lands the reader at the top of the document, so the
        # outcome is drawn there rather than down beside the controls.
        return _settings_response(request, saved=True,
                                  queued=settings_store._any_active_job(),
                                  warnings=warnings, diagnostics=diags,
                                  quality_note=quality_note,
                                  rerendered=True)
    queued = settings_store._any_active_job()
    suffix = "&queued=1" if queued else ""
    if quality_note:
        suffix += "&quality_note=1"
    # A freshly saved Last.fm key gets checked right away, so the section can
    # say whether Discover will work instead of leaving that for the tab to
    # discover later. One cheap chart call, bounded by the web timeout.
    posted_lastfm_key = str(form.get("LASTFM_API_KEY") or "").strip()
    if posted_lastfm_key:
        loop = asyncio.get_running_loop()
        try:
            await asyncio.wait_for(
                loop.run_in_executor(
                    None, lambda: lastfm.probe_key(posted_lastfm_key)),
                timeout=cfg.WEB_FETCH_TIMEOUT)
            suffix += "&lastfm=ok"
        except lastfm.LastfmKeyRejected:
            suffix += "&lastfm=rejected"
        except (lastfm.LastfmError, asyncio.TimeoutError, OSError):
            suffix += "&lastfm=down"
    return RedirectResponse(url=f"/settings?saved=1{suffix}#{anchor}", status_code=303)


@router.post("/settings/password", response_class=HTMLResponse)
async def change_web_password(request: Request):
    """Change the password this app is signed in with. Every browser is signed
    out by the change, this one included, so it ends at the sign-in page."""
    if web_auth.auth_disabled() or not web_auth.credentials_configured():
        return RedirectResponse(url="/settings", status_code=303)
    form = await request.form()
    current = str(form.get("current_password") or "")
    fresh = str(form.get("new_password") or "")
    confirm = str(form.get("confirm_password") or "")
    username, credential_generation = (
        web_auth.current_username_and_generation()
    )
    loop = asyncio.get_running_loop()
    # The sign-in throttle covers this form too: a session left open is
    # otherwise an unlimited guesser of the password it was opened with.
    ip = web_auth.client_ip(request)
    if not web_auth.begin_login_attempt(ip, username):
        diags = await loop.run_in_executor(None, runtime._diagnostics)
        return _settings_response(
            request,
            password_error=(runtime._lockout_notice(ip, username)
                            or "Sign-in checks are busy. Try again shortly."),
            password_locked=True, diagnostics=diags, rerendered=True,
            status_code=429)
    # The KDF is deliberately slow, so it runs off the event loop; on the loop
    # it would stall every other request for the length of the hash.
    try:
        holder = await loop.run_in_executor(
            None, lambda: web_auth.verify_login(username, current))
    except BaseException:
        web_auth.cancel_login_attempt(ip, username)
        raise
    web_auth.finish_login_attempt(ip, username, success=holder)
    password_locked = False
    if not holder:
        error = "That is not your current password."
        wait = runtime._lockout_notice(ip, username, after_failure=True)
        if wait:
            error += f" {wait}"
            password_locked = True
    elif fresh != confirm:
        error = "The two new passwords don't match."
    else:
        error = web_auth.new_password_error(username, fresh)
    if not error:
        try:
            stored = await loop.run_in_executor(
                None,
                lambda: web_auth.set_credentials(
                    username, fresh,
                    env_password_hash=web_auth.env_override_hash(),
                    expected_generation=credential_generation),
            )
        except web_auth.PasswordRejected as exc:
            stored, error = False, str(exc)
        except web_auth.CredentialsChanged:
            return RedirectResponse(url="/login?changed=1", status_code=303)
        if not stored and not error:
            error = ("The new password couldn't be saved. Check that the data "
                     "folder is writable, then try again.")
    if error:
        diags = await loop.run_in_executor(None, runtime._diagnostics)
        return _settings_response(request, password_error=error,
                                  password_locked=password_locked,
                                  diagnostics=diags, rerendered=True)
    return RedirectResponse(url="/login?changed=1", status_code=303)


@router.post("/settings/corrupt-stores/clear")
async def clear_corrupt_stores(request: Request):
    """Delete the preserved `….corrupt` copies a corrupt-store notice names.

    The notice's own text already tells the operator to delete the file; this
    is that action, for the app running with no shell access to do it by hand.
    """
    loop = asyncio.get_running_loop()
    ok = await loop.run_in_executor(None, state_file.clear_corrupt_stores)
    if ok:
        return RedirectResponse(url="/settings", status_code=303)
    return RedirectResponse(
        url="/settings?error=" + runtime._notice_key(
            "One of the unreadable copies couldn't be deleted. Check the "
            "data folder's permissions, then try again."),
        status_code=303)


@router.post("/settings/corrupt-stores/keep")
async def keep_corrupt_stores(request: Request):
    """Clear the notice but keep the copies, in a folder it doesn't list."""
    loop = asyncio.get_running_loop()
    ok = await loop.run_in_executor(None, state_file.keep_corrupt_stores)
    if ok:
        return RedirectResponse(url="/settings", status_code=303)
    return RedirectResponse(
        url="/settings?error=" + runtime._notice_key(
            "One of the unreadable copies couldn't be moved. Check the data "
            "folder's permissions, then try again."),
        status_code=303)


@router.post("/settings/mode")
async def set_mode(request: Request, target: str = Form("")):
    """Hand the run-lock to the terminal (CLI), or take it back for the web.

    Switching to CLI is refused while a download/scan is active: releasing the
    lock under a running job would let the CLI race the worker over /staging.
    """
    global _creds_cache
    want = (target or "").strip().lower()
    if want == "cli":
        # Flip to CLI mode first so a /download or scan POST landing during
        # the handoff is refused (503) instead of slipping past the check and
        # racing the CLI over /staging once we release the lock below.
        with runtime._auto_check_lock:
            runtime.set_cli_mode(True)

        def _handoff():
            with runtime._auto_check_lock:
                # Only work in flight blocks the handoff; the race this
                # guards against is the CLI and a running worker sharing
                # /staging.
                jobs_active = any(
                    j.status != job_mgr.JobStatus.AWAITING_REVIEW
                    for j in job_mgr.registry.pending_and_running()
                )
                if jobs_active or job_mgr.active_library_operations():
                    runtime.set_cli_mode(False)
                    return False
                if runtime.run_lock_handle() is not None:
                    try:
                        runtime.run_lock_handle().close()  # closing releases the flock
                    except OSError:
                        pass
                    runtime.set_run_lock_handle(None)
                runtime.set_lock_busy_pid(None)
                return True

        loop = asyncio.get_running_loop()
        if not await loop.run_in_executor(None, _handoff):
            return RedirectResponse(url="/settings?error=" + runtime._notice_key(
                "Finish or cancel the running library work before handing off to the "
                "terminal."), status_code=303)
        return RedirectResponse(url="/settings?mode=cli", status_code=303)
    if want == "nolock":
        # Durable recovery cannot inspect or reconcile saved work without
        # exact single-writer authority.
        return RedirectResponse(
            url="/settings?error=" + runtime._notice_key(
                "The run lock is required. Fix the data-folder filesystem or "
                "permissions, then restart Qobuz Librarian."),
            status_code=303,
        )
    if want == "web":
        with runtime._auto_check_lock:
            prior_cli_mode = runtime.cli_mode()
        try:
            lease = run_lock.acquire("web")
            if lease is None:
                # Can't enforce the lock, same stance as startup: pause
                # destructive routes until the filesystem can enforce it.
                with runtime._auto_check_lock:
                    runtime.set_run_lock_handle(None)
                    runtime.set_lock_busy_pid(None)
                    runtime.set_lock_unenforceable(True)
                    runtime.set_cli_mode(False)
            else:
                try:
                    with runtime._auto_check_lock:
                        runtime._recover_under_web_run_lock(lease)
                        runtime.set_lock_busy_pid(None)
                        runtime.set_lock_unenforceable(False)
                        runtime.set_cli_mode(False)
                except Exception:
                    with runtime._auto_check_lock:
                        runtime.set_run_lock_handle(None)
                        runtime.set_lock_busy_pid(None)
                        runtime.set_lock_unenforceable(False)
                        runtime.set_cli_mode(prior_cli_mode)
                    _log.exception(
                        "couldn't resume Web mode because durable recovery "
                        "could not be read"
                    )
                    return RedirectResponse(
                        url="/settings?error=" + runtime._notice_key(
                            "Saved recovery state could not be checked. Web "
                            "mode stayed paused and its run lock was "
                            "released; check the data-folder permissions, "
                            "then try again."
                        ),
                        status_code=303,
                    )
            # The CLI may have changed the saved token while it held the lock;
            # drop the cached creds so the banner reflects what's on disk now.
            _creds_cache = None
            return RedirectResponse(url="/settings?mode=web", status_code=303)
        except run_lock.LockBusy:
            # A CLI session still holds the lock, so we can't take it back yet.
            return RedirectResponse(url="/settings?error=" + runtime._notice_key(
                "The terminal is still using it. Finish your CLI command, then "
                "resume."), status_code=303)
    return RedirectResponse(url="/settings", status_code=303)
