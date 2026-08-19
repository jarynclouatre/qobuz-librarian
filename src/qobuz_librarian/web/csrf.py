"""CSRF protection - double-submit cookie with SameSite=Strict."""
import secrets

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.responses import PlainTextResponse

CSRF_COOKIE_NAME = "ql_csrf"
CSRF_FORM_FIELD = "_csrf_token"
CSRF_HEADER = "X-CSRF-Token"

_SAFE_METHODS = {"GET", "HEAD", "OPTIONS"}
_MAX_FORM_BYTES = 1 * 1024 * 1024  # 1 MB - no form on this app gets close
# One route takes a file instead of a form. A collection backup is a few MB for
# a large library, and the route enforces this same bound as it reads.
_UPLOAD_LIMITS = {"/collection/restore": 64 * 1024 * 1024}


def body_limit(path: str) -> int:
    return _UPLOAD_LIMITS.get(path, _MAX_FORM_BYTES)


def _new_token() -> str:
    return secrets.token_urlsafe(32)


_STALE_MESSAGE = ("This page had been open too long to be trusted with that "
                  "action, so nothing was changed. Reload and try again.")


def _stale_page_response(request, token):
    """A refusal the user can act on, carrying a fresh token so the retry works."""
    from starlette.responses import HTMLResponse
    if request.headers.get("HX-Request"):
        # htmx swallows a non-2xx body, and the app's toast host reads this
        # verbatim; HX-Refresh reloads the page with the new cookie in place.
        resp = HTMLResponse("", status_code=200,
                            headers={"HX-Refresh": "true"})
    else:
        from qobuz_librarian.web.app import render_error_page
        resp = render_error_page(request, 403, "That page went stale",
                                 _STALE_MESSAGE)
    _set_csrf_cookie(request, resp, token)
    return resp


def _set_csrf_cookie(request, response, token):
    secure = (request.url.scheme == "https"
              or request.headers.get("x-forwarded-proto") == "https")
    response.set_cookie(
        CSRF_COOKIE_NAME,
        token,
        max_age=60 * 60 * 24 * 30,
        samesite="strict",
        httponly=True,
        secure=secure,
        path="/",
    )


class CSRFMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request, call_next):
        cookie_token = request.cookies.get(CSRF_COOKIE_NAME)
        token = cookie_token or _new_token()
        request.state.csrf_token = token

        if request.method not in _SAFE_METHODS:
            try:
                content_length = int(request.headers.get("content-length") or 0)
            except ValueError:
                content_length = 0
            limit = body_limit(request.url.path)
            if content_length > limit:
                return PlainTextResponse("Request body too large",
                                         status_code=413)
            submitted = request.headers.get(CSRF_HEADER)
            if not submitted and 0 < content_length <= _MAX_FORM_BYTES:
                # Read the body only when its length is declared and bounded -
                # a chunked request with no Content-Length is never read here,
                # so a tokenless POST can't make us buffer an unbounded body.
                try:
                    body = await request.body()
                    ct = request.headers.get("content-type", "")
                    if "application/x-www-form-urlencoded" in ct:
                        from urllib.parse import parse_qs
                        parsed = parse_qs(body.decode("latin-1"))
                        submitted = (parsed.get(CSRF_FORM_FIELD) or [None])[0]
                    # multipart/form-data isn't parsed here (doing so would
                    # consume the stream before downstream Form() handlers): a
                    # multipart POST must carry the token in the CSRF header.
                except Exception:
                    submitted = None
            if not cookie_token or not submitted or not secrets.compare_digest(
                str(cookie_token).encode("utf-8"), str(submitted).encode("utf-8")
            ):
                # Ordinary causes: a tab left open past the cookie's 30 days, a
                # browser that clears cookies, a privacy extension. The old
                # reply was a bare white page reading "CSRF token missing or
                # invalid" with no nav and no way back - and because it was
                # text/plain, the minting below skipped it too, so retrying
                # failed exactly the same way. Say what happened in English and
                # always hand back a usable token.
                return _stale_page_response(request, token)

        response = await call_next(request)
        # Mint the cookie only when the client has none AND we're returning an
        # HTML page - the page is what reads the token (from its <meta> tag)
        # and then submits it, so static assets, /healthz, /sw.js and JSON
        # /api responses don't need it.
        is_html = response.headers.get("content-type", "").startswith("text/html")
        if not cookie_token and is_html:
            secure = (request.url.scheme == "https"
                      or request.headers.get("x-forwarded-proto") == "https")
            response.set_cookie(
                CSRF_COOKIE_NAME,
                token,
                max_age=60 * 60 * 24 * 30,
                samesite="strict",
                # The page reads the token from its <meta> tag, never from
                # this cookie, so HttpOnly costs nothing and keeps it out of
                # reach of any injected script.
                httponly=True,
                secure=secure,
            )
        return response


# script-src carries a per-request nonce (set on request.state.csp_nonce
# below) rather than 'unsafe-inline', so a reflected/injected <script> can't
# run - only the few inline blocks we mint with this request's nonce do.
def _csp(nonce: str) -> str:
    return (
        "default-src 'self'; "
        f"script-src 'self' 'nonce-{nonce}'; "
        "style-src 'self' 'unsafe-inline'; "
        "img-src 'self' data: https://static.qobuz.com; "
        "connect-src 'self'; "
        "object-src 'none'; "
        "frame-ancestors 'none'; "
        "base-uri 'self'; "
        "form-action 'self'"
    )


class SecurityHeadersMiddleware(BaseHTTPMiddleware):
    """Adds X-Content-Type-Options, X-Frame-Options, Referrer-Policy,
    Permissions-Policy, a nonce-based Content-Security-Policy, and HSTS on
    HTTPS requests only. Private non-static responses are non-storable."""
    async def dispatch(self, request, call_next):
        # Minted before the route renders so templates can stamp it on their
        # inline <script>s via request.state.csp_nonce (mirrors csrf_token).
        nonce = _new_token()
        request.state.csp_nonce = nonce
        response = await call_next(request)
        cacheable_asset = (
            request.url.path.startswith("/static/")
            or request.url.path in {"/favicon.ico", "/sw.js"}
        )
        if not cacheable_asset:
            # Job JSON and event streams contain the same private library data
            # as the HTML pages.
            response.headers["Cache-Control"] = "no-store"
        response.headers.setdefault("X-Content-Type-Options", "nosniff")
        response.headers.setdefault("X-Frame-Options", "DENY")
        response.headers.setdefault("Referrer-Policy", "same-origin")
        response.headers.setdefault("Content-Security-Policy", _csp(nonce))
        # The UI uses no device sensors; deny them so an injected script can't
        # reach for the camera/mic/location either.
        response.headers.setdefault(
            "Permissions-Policy", "geolocation=(), microphone=(), camera=()")
        # HSTS only on HTTPS - emitting it over plain HTTP is pointless and
        # would brick a user who later reaches the host via HTTP.
        is_https = (request.url.scheme == "https"
                    or request.headers.get("x-forwarded-proto") == "https")
        if is_https:
            response.headers.setdefault(
                "Strict-Transport-Security", "max-age=31536000")
        return response


class StripServerHeaderMiddleware:
    """ASGI middleware that drops the Server header before it leaves the
    process - uvicorn advertises itself by default, which is a free hint
    to anyone scanning for known framework CVEs."""

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        async def _send(msg):
            if msg["type"] == "http.response.start":
                msg["headers"] = [
                    (n, v) for (n, v) in msg.get("headers", [])
                    if n.lower() != b"server"
                ]
            await send(msg)
        await self.app(scope, receive, _send)
