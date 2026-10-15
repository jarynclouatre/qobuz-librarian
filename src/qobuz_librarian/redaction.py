"""Keep account secrets out of logs and stored jobs."""
import os
import re
import threading

MASK = "[redacted]"

# Names whose value is an account secret, in a query string, a form body, a
# JSON body, a dumped header, or a Python tuple/list repr such as
# ('X-User-Auth-Token', 'abc123'). The separator tolerates the quotes a dict,
# a JSON blob or a tuple repr puts around the name, so one rule covers all
# five shapes; the comma alternative is what a printed header tuple uses in
# place of = or :.
_SEP = r'["\']?\s*(?:=|%3D|:|,)\s*["\']?'
_VALUE = r'[^\s&;,"\'<>\\]+'
# No word boundary before the name: a long URL reaches the log already wrapped,
# so the parameter can arrive with its first characters on the line above.
_NAMED = re.compile(
    r"(?i)(user_auth_token|auth_token|user_id|password|app_secret|"
    r"request_sig|x-user-auth-token|authorization|api_key)(" + _SEP + r")"
    r"(?:(Bearer|Basic)\s+)?(" + _VALUE + r")")

_lock = threading.Lock()
_values: set = set()
# Below this a "secret" is an ordinary word, and masking every occurrence of it
# would mangle the text around it.
_MIN_VALUE = 8


def register_secret(value) -> None:
    """Remember a live credential so it is masked wherever it turns up, even
    printed on its own."""
    text = str(value or "").strip()
    if len(text) < _MIN_VALUE:
        return
    with _lock:
        _values.add(text)


def _mask_named(match) -> str:
    scheme = match.group(3)
    return match.group(1) + match.group(2) + (scheme + " " if scheme else "") + MASK


def redact(text):
    """Mask credentials in one piece of text. Runs on every log line, so it
    does the cheap containment test before any substitution."""
    if not text:
        return text
    s = text if isinstance(text, str) else str(text)
    with _lock:
        known = tuple(sorted(_values, key=len, reverse=True))
    for value in known:
        if value in s:
            s = s.replace(value, MASK)
    return _NAMED.sub(_mask_named, s)


def scrub_file(path) -> bool | None:
    """Mask credentials in a file, returning None when it could not be read.

    The file is rewritten in place, so a logger that already has it open goes
    on writing into the same file."""
    try:
        f = open(path, "r+b")
    except FileNotFoundError:
        return False
    except OSError:
        return None
    with f:
        try:
            original = f.read().decode("utf-8", errors="replace")
            cleaned = redact(original)
            if cleaned == original:
                return False
            f.seek(0)
            f.write(cleaned.encode("utf-8"))
            f.truncate()
            f.flush()
            os.fsync(f.fileno())
        except OSError:
            return None
    return True
