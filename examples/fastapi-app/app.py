"""Example host app: mounts the goto-sms setup page behind a demo cookie login and sends texts.

Run from the repo root (needs `pip install -e .[sms] fastapi uvicorn`):

    ADMIN_PASSWORD=change-me uvicorn app:app --app-dir examples/fastapi-app --port 8765
    # or: ADMIN_PASSWORD=change-me python examples/fastapi-app/app.py   (reads PORT, default 8765)

Then open http://localhost:8765/login?password=change-me once, and go to /api/goto/.
The cookie login is a demo: put your own authentication in `is_admin`.
"""

import dataclasses
import hmac
import logging
import os
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse

from aiogotoconnect.sms import (
    DEFAULT_API_BASE,
    DEFAULT_AUTH_BASE,
    AuthorizeRequest,
    InboundMessage,
    create_goto_sms,
)

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("example")

ADMIN_PASSWORD = os.environ.get("ADMIN_PASSWORD")
if not ADMIN_PASSWORD:
    raise SystemExit("Set ADMIN_PASSWORD (the demo login password for /login?password=...).")
COOKIE = "goto_sms_admin"
MOUNT = "/api/goto"


def same_string(a: str, b: str) -> bool:
    return hmac.compare_digest(a.encode(), b.encode())


def is_admin(req: AuthorizeRequest) -> bool:
    """Demo auth: the cookie that /login sets must hold ADMIN_PASSWORD."""
    return same_string(req.cookies.get(COOKIE, ""), ADMIN_PASSWORD or "")


sms = create_goto_sms(
    authorize=is_admin,
    mount_path=MOUNT,
    # The setup page shows "← Back to the app" linking here; a signed-out visitor goes to /login.
    back_url="/",
    login_url="/login",
    public_url=os.environ.get("GOTO_SMS_PUBLIC_URL"),
    data_dir=os.environ.get("GOTO_SMS_DATA_DIR") or str(Path(__file__).parent / "data"),
    # Overridden only by the e2e test, which points both at a fake GoTo (unset in real use).
    auth_base_url=os.environ.get("GOTO_SMS_AUTH_BASE_URL") or DEFAULT_AUTH_BASE,
    api_base_url=os.environ.get("GOTO_SMS_API_BASE_URL") or DEFAULT_API_BASE,
)


def on_message(message: InboundMessage) -> None:
    """Logs that a text arrived: the masked sender and its length, never the text itself."""
    tail = message.from_[-4:] if len(message.from_) > 4 else ""
    log.info("inbound text from ***%s (%d chars)", tail, len(message.text))


sms.on("message", on_message)


@asynccontextmanager
async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
    # start() ... close(); a startup failure is logged and the setup page shows why.
    async with sms:
        yield


app = FastAPI(lifespan=lifespan)
# Unauthorized visitors: the page redirects to /login, the API answers 403. The OAuth callback
# and the GoTo webhook are exempt from authorize inside the package.
app.mount(MOUNT, sms.asgi_app())


@app.get("/", response_class=HTMLResponse)
async def index() -> str:
    return f'<p>goto-sms example app. <a href="{MOUNT}/">Open the SMS setup page</a>.</p>'


@app.get("/healthz")
async def healthz() -> dict[str, bool]:
    return {"ok": True}


@app.get("/login")
async def login(password: str = "") -> RedirectResponse:
    """Demo login: sets the cookie when the password matches, then goes to the app."""
    response = RedirectResponse("/", status_code=303)
    if same_string(password, ADMIN_PASSWORD or ""):
        response.set_cookie(COOKIE, password, httponly=True, samesite="lax")
    return response


@app.post("/send")
async def send(request: Request) -> JSONResponse:
    if not is_admin(
        AuthorizeRequest("POST", "/send", {}, dict(request.headers), dict(request.cookies))
    ):
        return JSONResponse({"error": "forbidden"}, status_code=403)
    try:
        body = await request.json()
    except ValueError:  # malformed JSON (or bad UTF-8)
        body = None
    if not isinstance(body, dict):
        return JSONResponse(
            {"error": "invalid_body", "message": "Expected a JSON object"}, status_code=400
        )
    result = await sms.send(str(body.get("to", "")), str(body.get("text", "")))
    return JSONResponse(dataclasses.asdict(result))


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="127.0.0.1", port=int(os.environ.get("PORT", "8765")))
