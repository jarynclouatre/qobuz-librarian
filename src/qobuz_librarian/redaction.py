"""Keep account secrets out of anything the app writes down.

A failed call is reported as the call that failed, and a Qobuz call carries the
account email and the auth token in its query string, so an ordinary timeout
puts the whole credential into the activity log, the stored job record and the
app log file. Masking belongs where the text is written: by the time a line is
rendered it has already been persisted, and a record written yesterday is still
readable today.

Two rules run together. Names are matched so a value never has to be known in
advance, which covers an error nobody has seen yet; live credential values are
registered as they are read, which covers a token printed on its own with no
name beside it.
"""
import os
import re
import tempfile
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
    r"request_sig|x-user-auth-token|authorization)(" + _SEP + r")"
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
        known = tuple(_values)
    for value in known:
        if value in s:
            s = s.replace(value, MASK)
    return _NAMED.sub(_mask_named, s)


def scrub_file(path) -> bool:
    """Mask credentials in a file already on disk, in place. Returns True when
    something was masked. For records written before this module existed: an
    upgrade must not leave yesterday's token sitting in the log."""
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            original = f.read()
    except (OSError, ValueError):
        return False
    cleaned = redact(original)
    if cleaned == original:
        return False
    directory = os.path.dirname(os.path.abspath(path)) or "."
    fd, tmp = tempfile.mkstemp(dir=directory)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(cleaned)
        os.replace(tmp, path)
    except OSError:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        return False
    return True
