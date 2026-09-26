"""Routes for sign-in, sign-out and first-run setup."""
import asyncio
import logging
import urllib.parse

from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from qobuz_librarian.web import auth as web_auth
from qobuz_librarian.web import rendering

router = APIRouter()
_log = logging.getLogger("qobuz_librarian")


@router.get("/login", response_class=HTMLResponse)
async def login_page(request: Request):
    if web_auth.auth_disabled():
        return RedirectResponse(url="/", status_code=303)
    if not web_auth.credentials_configured():
        return RedirectResponse(url="/setup", status_code=303)
    cookie = request.cookies.get(web_auth.SESSION_COOKIE)
    if cookie and web_auth.verify_session(cookie):
        return RedirectResponse(url="/", status_code=303)
    return rendering.templates.TemplateResponse(
        request=request, name="login.html",
        context={"error": (rendering._notice_text(request.query_params.get("error"))
                           or rendering._lockout_notice(web_auth.client_ip(request))),
                 "username": rendering._notice_text(request.query_params.get("u")),
                 "changed": request.query_params.get("changed") == "1",
                 "next_path": web_auth.safe_next_path(
                     request.query_params.get("next"))})


def _login_again(error, username, next_path):
    """Send a refused sign-in back to the form by redirect, so Back from
    the page that follows never lands on a form post."""
    params = {"error": rendering._notice_key(error)}
    if username:
        params["u"] = rendering._notice_key(username)
    if next_path:
        params["next"] = next_path
    return RedirectResponse(url="/login?" + urllib.parse.urlencode(params),
                            status_code=303)


@router.post("/login", response_class=HTMLResponse)
async def login_submit(request: Request, username: str = Form(""),
                       password: str = Form(""), next: str = Form("")):
    if web_auth.auth_disabled():
        return RedirectResponse(url="/", status_code=303)
    if not web_auth.credentials_configured():
        return RedirectResponse(url="/setup", status_code=303)
    # Where to land after signing in: the deep link that bounced here, kept
    # through failed attempts and re-validated so the form can't smuggle in an
    # off-site redirect.
    next_path = web_auth.safe_next_path(next)
    ip = web_auth.client_ip(request)
    # A request already carrying a valid session is provably the logged-in user,
    # not the brute-forcer the throttle exists to stop, so exempt it so a remote
    # flood of failed logins for the admin username can't lock the real admin out.
    cookie = request.cookies.get(web_auth.SESSION_COOKIE)
    has_session = bool(cookie) and web_auth.verify_session(cookie)
    # A submission that could never succeed shouldn't cost a strike: an empty
    # field is a slip, not an attempt, and five of them locked the owner out.
    if not username.strip() or not password:
        return _login_again("Enter your username and password.",
                            username.strip(), next_path)
    # Checked before the password is verified, so a correct one can't clear the
    # wait and a guess costs an attacker the wait rather than a KDF they can
    # keep spending. The counters live in memory, so a restart is the way back
    # in for whoever owns the box.
    reserved_attempt = False
    if not has_session:
        reserved_attempt = web_auth.begin_login_attempt(ip, username)
        if not reserved_attempt:
            refusal = rendering._lockout_notice(ip, username) or (
                "Sign-in checks are busy. Try again shortly."
            )
            return _login_again(refusal, username.strip(), next_path)
    # Offload the 600k-round PBKDF2 to a thread so one login attempt can't stall
    # the single-worker event loop (health, API and SSE all freeze during a KDF
    # that runs on the loop thread).
    loop = asyncio.get_running_loop()
    try:
        ok = await loop.run_in_executor(
            None, web_auth.verify_login, username.strip(), password)
    except BaseException:
        if reserved_attempt:
            web_auth.cancel_login_attempt(ip, username)
        raise
    if reserved_attempt:
        web_auth.finish_login_attempt(ip, username, success=ok)
    if not ok:
        wait = rendering._lockout_notice(ip, username, after_failure=True)
        # Keep what they typed, as the setup screen already does.
        return _login_again(
            "Incorrect username or password." + (f" {wait}" if wait else ""),
            username.strip(), next_path)
    web_auth.clear_login_failures(ip, username)
    resp = RedirectResponse(url=next_path or "/", status_code=303)
    try:
        web_auth.set_session_cookie(resp, request)
    except web_auth.SessionPersistenceError:
        _log.warning(
            "Couldn't persist a new web session; login refused.")
        return rendering.templates.TemplateResponse(
            request=request, name="login.html",
            context={"error": "Couldn't save your session. Check that the "
                              "data volume is writable, then try again.",
                     "username": username.strip(),
                     "next_path": next_path},
            status_code=503)
    return resp


@router.post("/logout")
async def logout(request: Request):
    # Revoke the session server-side, not just the browser cookie; otherwise a
    # captured cookie value stays valid for its full 30-day lifetime.
    if not web_auth.revoke_session(
        request.cookies.get(web_auth.SESSION_COOKIE)
    ):
        return rendering.render_error_page(
            request,
            503,
            "Couldn't log out",
            "The session store couldn't be saved, so nothing changed. Check "
            "that the data volume is writable, then try logging out again.",
        )
    resp = RedirectResponse(url="/login", status_code=303)
    resp.headers["Cache-Control"] = "no-store"
    web_auth.clear_session_cookie(resp)
    return resp


@router.get("/setup", response_class=HTMLResponse)
async def setup_page(request: Request):
    if web_auth.auth_disabled():
        return RedirectResponse(url="/", status_code=303)
    if web_auth.credentials_configured():
        return RedirectResponse(url="/", status_code=303)
    return rendering.templates.TemplateResponse(request=request, name="setup.html",
                                      context={"error": "", "username": ""})


@router.post("/setup", response_class=HTMLResponse)
async def setup_submit(request: Request, username: str = Form(""),
                       password: str = Form(""), confirm: str = Form("")):
    if web_auth.auth_disabled():
        return RedirectResponse(url="/", status_code=303)
    if web_auth.credentials_configured():
        return rendering.templates.TemplateResponse(
            request=request, name="setup.html",
            context={"setup_conflict": True}, status_code=409)
    user = username.strip()
    if not user:
        err = "Pick a username."
    elif password_error := web_auth.new_password_error(user, password):
        err = password_error
    elif password != confirm:
        err = "The two passwords don't match."
    else:
        err = ""
    if err:
        return rendering.templates.TemplateResponse(
            request=request, name="setup.html",
            context={"error": err, "username": user}, status_code=400)
    # First-run setup is unauthenticated by necessity (no creds exist yet), so
    # whoever reaches the open port first claims admin.
    ip = web_auth.client_ip(request)
    _log.warning(
        "First-run /setup creating admin account from %s (username=%r).",
        ip, user)
    # The KDF is deliberately slow, so it runs off the event loop; on the loop
    # it would stall every other request for the length of the hash, and a
    # second tap while it ran would land here again before the first request
    # finished, reading a false "created in another browser" conflict.
    loop = asyncio.get_running_loop()
    try:
        stored = await loop.run_in_executor(
            None,
            lambda: web_auth.set_credentials(
                user, password, require_unconfigured=True),
        )
    except web_auth.CredentialsAlreadyConfigured:
        return rendering.templates.TemplateResponse(
            request=request, name="setup.html",
            context={"setup_conflict": True}, status_code=409)
    if not stored:
        return rendering.templates.TemplateResponse(
            request=request, name="setup.html",
            context={"error": "Couldn't save the login: the data volume "
                              "isn't writable. Check PUID/PGID and volume "
                              "permissions.", "username": user},
            status_code=500)
    resp = RedirectResponse(url="/", status_code=303)
    try:
        web_auth.set_session_cookie(resp, request)
    except web_auth.SessionPersistenceError:
        _log.warning(
            "Couldn't persist the first web session; setup login was saved.")
        return rendering.templates.TemplateResponse(
            request=request, name="login.html",
            context={"error": "Your login was created, but its session couldn't "
                              "be saved. Check that the data volume is writable, "
                              "then sign in.",
                     "username": user,
                     "next_path": ""},
            status_code=503)
    return resp
