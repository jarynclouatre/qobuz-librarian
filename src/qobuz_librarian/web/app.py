"""FastAPI web application for Qobuz Librarian."""
import logging

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from starlette.exceptions import HTTPException as StarletteHTTPException

from qobuz_librarian import config as cfg
from qobuz_librarian.web import auth as web_auth
from qobuz_librarian.web import (
    lifespan,
    rendering,
    routes_api,
    routes_auth,
    routes_backup,
    routes_discover,
    routes_downsample,
    routes_jobs,
    routes_library,
    routes_lyrics,
    routes_migrate,
    routes_queue,
    routes_repair,
    routes_search,
    routes_settings,
    routes_upgrade,
    write_gate,
)
from qobuz_librarian.web.csrf import (
    CSRFMiddleware,
    RequestBodyLimitMiddleware,
    SecurityHeadersMiddleware,
    StripServerHeaderMiddleware,
)

_log = logging.getLogger("qobuz_librarian")


app = FastAPI(title="Qobuz Librarian", docs_url=None, redoc_url=None,
              openapi_url=None, lifespan=lifespan._lifespan)

# AuthMiddleware is added first so it ends up innermost, so it runs after the
# CSRF middleware, which keeps CSRF validation on the login/setup POSTs and
# lets the redirects it returns pick up the security headers.
app.add_middleware(web_auth.AuthMiddleware)
app.add_middleware(CSRFMiddleware)
app.add_middleware(RequestBodyLimitMiddleware)
app.add_middleware(SecurityHeadersMiddleware)
app.add_middleware(StripServerHeaderMiddleware)

app.include_router(routes_auth.router)
app.include_router(routes_discover.router)
app.include_router(routes_upgrade.router)
app.include_router(routes_downsample.router)
app.include_router(routes_repair.router)
app.include_router(routes_lyrics.router)
app.include_router(routes_migrate.router)
app.include_router(routes_library.router)
app.include_router(routes_queue.router)
app.include_router(routes_api.router)
app.include_router(routes_backup.router)
app.include_router(routes_settings.router)
app.include_router(routes_search.router)
app.include_router(routes_jobs.router)

app.mount("/static", StaticFiles(directory=str(rendering.static_dir)), name="static")


@app.get("/favicon.ico", include_in_schema=False)
async def favicon():
    """Serve the app icon for the browser's automatic /favicon.ico probe."""
    return FileResponse(rendering.static_dir / "icon-192.png", media_type="image/png")


# Bake the asset version into the worker so its cache name changes whenever
# the served assets change.
_SW_JS = (rendering.static_dir / "sw.js").read_text(encoding="utf-8").replace(
    "__APP_VERSION__", rendering._ASSET_VERSION)


@app.get("/sw.js")
async def service_worker():
    return Response(
        _SW_JS,
        media_type="application/javascript",
        headers={"Service-Worker-Allowed": "/", "Cache-Control": "no-cache"},
    )


@app.get("/healthz")
async def healthz():
    """Cheap process liveness probe for uptime monitors."""
    return JSONResponse({"ok": True})


@app.head("/healthz")
async def healthz_head():
    """A body-less 200 for uptime monitors that send HEAD."""
    return Response(status_code=200)


@app.get("/readyz")
async def readyz():
    status_code, report = write_gate._readiness_report()
    return JSONResponse(report, status_code=status_code)


@app.head("/readyz")
async def readyz_head():
    status_code, _report = write_gate._readiness_report()
    return Response(status_code=status_code)


@app.exception_handler(StarletteHTTPException)
async def _http_exception_handler(request: Request, exc: StarletteHTTPException):
    """Render a styled page for a mistyped/stale URL instead of a bare
    ``{"detail": "Not Found"}``. API routes and every non-404 status keep the
    JSON shape callers expect."""
    if exc.status_code == 404 and not request.scope["path"].startswith("/api/"):
        return rendering.render_error_page(
            request, 404, "Page not found",
            "That page doesn't exist. The link may have moved or been "
            "mistyped.")
    return JSONResponse({"detail": exc.detail}, status_code=exc.status_code,
                        headers=getattr(exc, "headers", None))


@app.exception_handler(RequestValidationError)
async def _validation_exception_handler(request: Request,
                                        exc: RequestValidationError):
    """A mangled query param (``/library?page=abc``) renders the styled error
    page instead of dumping framework validation JSON into the browser. API
    routes keep the JSON detail machine callers want."""
    if not request.scope["path"].startswith("/api/"):
        return rendering.render_error_page(
            request, 400, "Bad request",
            "That address has an invalid value in it. Check the link and try "
            "again.")
    return JSONResponse({"detail": exc.errors()}, status_code=422)


def _file_error(exc):
    """The OSError naming a file behind ``exc``, if there is one."""
    for _ in range(3):
        if isinstance(exc, OSError) and exc.filename:
            return exc
        exc = exc.__cause__ or exc.__context__
        if exc is None:
            return None
    return None


@app.exception_handler(Exception)
async def _unhandled_exception_handler(request: Request, exc: Exception):
    """An uncaught route error renders the styled page for browser paths instead
    of FastAPI's bare JSON 500. API routes keep JSON. The detail is logged, never
    shown, since it can carry internals."""
    _log.exception(
        "Unhandled error on %s", request.scope.get("path", "?"))
    file_error = _file_error(exc)
    if file_error is not None and not request.scope["path"].startswith("/api/"):
        advice = ("Check that it belongs to the user Qobuz Librarian runs as "
                  "(PUID and PGID in Docker) and can be read and written."
                  if isinstance(file_error, PermissionError) else
                  "Check the file and the folder it is in, then try again.")
        return rendering.render_error_page(
            request, 500, "A file can't be used",
            f"Qobuz Librarian can't use {file_error.filename}: "
            f"{file_error.strerror or file_error}. {advice}")
    if not request.scope["path"].startswith("/api/"):
        return rendering.render_error_page(
            request, 500, "Something went wrong",
            "An unexpected error happened on the server. Try again, or check "
            "the container logs if it keeps happening.")
    return JSONResponse({"detail": "internal server error"}, status_code=500)


def start():
    import uvicorn
    # server_header=False mirrors the --no-server-header the Docker entrypoint
    # passes, so the installed qobuz-librarian-web entrypoint doesn't advertise
    # "Server: uvicorn" (a free hint to anyone scanning for framework CVEs).
    uvicorn.run("qobuz_librarian.web.app:app", host=cfg.WEB_HOST,
                port=cfg.WEB_PORT, workers=1, server_header=False)
