"""
The console's request guard. The console binds 127.0.0.1, but a web page
open in the same browser can still send it requests: a cross-site form POST
(CSRF), or, through DNS rebinding, any request under a hostname the attacker
controls. So every request must name the console's own host, and every
request that is not a GET or HEAD must also come from the console's own
origin (when the browser says) and carry the per-process token that only the
console's own pages contain.
"""

import secrets
from urllib.parse import parse_qs

TOKEN_HEADER = "x-console-token"
TOKEN_FIELD = "console_token"
SAFE_METHODS = ("GET", "HEAD")
FORM_TYPE = "application/x-www-form-urlencoded"


def new_token():
    return secrets.token_urlsafe(32)


class Guard:
    """ASGI middleware enforcing the Host, Origin and token rules above.

    The token comes from the X-Console-Token header (htmx sends it on every
    request) or, for a plain form post, the console_token form field. The
    body is read here to find that field and then replayed to the app.
    """

    def __init__(self, app, port, token):
        self.app = app
        self.token = token
        self.hosts = {f"127.0.0.1:{port}", f"localhost:{port}"}
        self.origins = {f"http://{host}" for host in self.hosts}

    async def __call__(self, scope, receive, send):
        if scope["type"] not in ("http", "websocket"):
            return await self.app(scope, receive, send)
        headers = {k.decode("latin-1").lower(): v.decode("latin-1")
                   for k, v in scope["headers"]}
        if headers.get("host", "").lower() not in self.hosts:
            return await refuse(scope, send, 400, "Unknown Host header.")
        if scope["type"] == "websocket" or scope["method"] in SAFE_METHODS:
            return await self.app(scope, receive, send)
        origin = headers.get("origin")
        if origin is not None and origin.lower() not in self.origins:
            return await refuse(scope, send, 403, "Cross-origin request refused.")
        body = await read_body(receive)
        token = headers.get(TOKEN_HEADER) or form_token(headers, body)
        if not token or not secrets.compare_digest(token.encode(), self.token.encode()):
            return await refuse(scope, send, 403, "Missing or wrong console token.")
        await self.app(scope, replay(body, receive), send)


def form_fields(body):
    """{name: value} of an urlencoded form body, the last value of a repeated name."""
    pairs = parse_qs(body.decode("utf-8", "replace"), keep_blank_values=True)
    return {name: values[-1] for name, values in pairs.items()}


def form_token(headers, body):
    if not headers.get("content-type", "").startswith(FORM_TYPE):
        return None
    return form_fields(body).get(TOKEN_FIELD)


async def read_body(receive):
    chunks = []
    while True:
        message = await receive()
        if message["type"] == "http.disconnect":
            break
        chunks.append(message.get("body", b""))
        if not message.get("more_body"):
            break
    return b"".join(chunks)


def replay(body, receive):
    """A receive that yields the already-read body once, then defers to the client."""
    sent = False

    async def replayed():
        nonlocal sent
        if sent:
            return await receive()
        sent = True
        return {"type": "http.request", "body": body, "more_body": False}
    return replayed


async def refuse(scope, send, status, text):
    if scope["type"] == "websocket":
        return await send({"type": "websocket.close", "code": 1008})
    await send({"type": "http.response.start", "status": status,
                "headers": [(b"content-type", b"text/plain; charset=utf-8")]})
    await send({"type": "http.response.body", "body": text.encode()})
