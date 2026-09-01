from __future__ import annotations

import hmac
import secrets

from starlette.requests import Request
from starlette.responses import PlainTextResponse
from starlette.types import ASGIApp, Message, Receive, Scope, Send

# Double-submit-cookie CSRF protection for the entire web app (wizard,
# dashboard, settings, generate). The web app binds 0.0.0.0 by design
# (CLAUDE.md Section 15) and has no login/session of its own, so without
# this, any third-party page a LAN-adjacent browser opens can silently
# drive every state-changing route here via forged requests — including,
# per the pre-publication security audit, exfiltrating a live Plex token
# through the wizard's own connect flow. A pure ASGI implementation (not
# Starlette's BaseHTTPMiddleware) is used deliberately so the SSE stream at
# /dashboard/sync-stream keeps working — BaseHTTPMiddleware buffers/wraps
# StreamingResponse in ways that don't mix well with long-lived responses.

CSRF_COOKIE_NAME = "csrf_token"
CSRF_FIELD_NAME = "csrf_token"
CSRF_HEADER_NAME = "x-csrf-token"
_SAFE_METHODS = frozenset({"GET", "HEAD", "OPTIONS", "TRACE"})


def _new_token() -> str:
    return secrets.token_urlsafe(32)


def _expected_origin(request: Request) -> str:
    return f"{request.url.scheme}://{request.url.netloc}"


async def _submitted_token(request: Request) -> str | None:
    header_token = request.headers.get(CSRF_HEADER_NAME)
    if header_token:
        return header_token
    content_type = request.headers.get("content-type", "")
    if "form" in content_type:
        form = await request.form()
        value = form.get(CSRF_FIELD_NAME)
        return value if isinstance(value, str) else None
    return None


class CSRFMiddleware:
    """Requires a matching csrf_token cookie + header/form-field on every
    state-changing request, checked BEFORE the route handler runs (so a
    failed check never lets a sensitive operation start). GET/HEAD/OPTIONS
    are left untouched (no body consumed, no validation) so they stay safe
    and side-effect-free, and streaming responses aren't buffered.
    """

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        request = Request(scope, receive=receive)
        cookie_token = request.cookies.get(CSRF_COOKIE_NAME)
        is_new_cookie = cookie_token is None
        token = cookie_token or _new_token()

        if request.method not in _SAFE_METHODS:
            origin = request.headers.get("origin")
            origin_ok = origin is None or origin == _expected_origin(request)

            # A cookie that didn't exist before this request cannot possibly
            # have been echoed back by a legitimate prior page load, so
            # nothing needs the body read to know this request must fail.
            if is_new_cookie or not origin_ok:
                await _reject(scope, receive, send)
                return

            body = await request.body()

            async def replay_receive() -> Message:
                return {"type": "http.request", "body": body, "more_body": False}

            submitted = await _submitted_token(Request(scope, receive=replay_receive))
            if not submitted or not hmac.compare_digest(cookie_token, submitted):
                await _reject(scope, receive, send)
                return

            receive = replay_receive

        state = scope.setdefault("state", {})
        state["csrf_token"] = token

        async def send_wrapper(message: Message) -> None:
            if is_new_cookie and message["type"] == "http.response.start":
                headers = list(message.get("headers", []))
                headers.append(
                    (
                        b"set-cookie",
                        # HttpOnly: nothing here ever reads the cookie from
                        # JS — the token reaches the page via server-side
                        # interpolation (hx-headers on <body>, hidden form
                        # fields), never document.cookie — so this closes
                        # off one passive cookie-theft channel (a leaky
                        # subresource, a stray logged document.cookie
                        # snapshot) for free.
                        f"{CSRF_COOKIE_NAME}={token}; Path=/; SameSite=Lax; HttpOnly".encode(),
                    )
                )
                message = {**message, "headers": headers}
            await send(message)

        await self.app(scope, receive, send_wrapper)


async def _reject(scope: Scope, receive: Receive, send: Send) -> None:
    response = PlainTextResponse("Rejected: missing or invalid CSRF token.", status_code=403)
    await response(scope, receive, send)
