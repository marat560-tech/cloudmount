"""Вход OAuth2 через браузер с приёмом кода на локальном порту."""
from __future__ import annotations

import base64
import hashlib
import http.server
import secrets
import threading
import time
import urllib.parse
import webbrowser

import requests

PORT = 53682
REDIRECT_URI = f"http://127.0.0.1:{PORT}/"

_PAGE = """<!doctype html><html><head><meta charset="utf-8"><title>CloudMount</title></head>
<body style="font-family:sans-serif;text-align:center;margin-top:15%">
<h2>{msg}</h2><p>Окно можно закрыть и вернуться в CloudMount.</p></body></html>"""


def token_from_response(js: dict, old: dict | None = None) -> dict:
    tok = dict(old or {})
    tok["access_token"] = js["access_token"]
    if js.get("refresh_token"):
        tok["refresh_token"] = js["refresh_token"]
    if js.get("expires_in"):
        tok["expires_at"] = time.time() + int(js["expires_in"])
    else:
        tok.pop("expires_at", None)
    return tok


class OAuthCancelled(Exception):
    pass


def browser_login(authorize_url: str, token_url: str, client_id: str,
                  client_secret: str | None = None, params: dict | None = None,
                  pkce: bool = False, timeout: int = 300,
                  cancel_event: threading.Event | None = None) -> dict:
    state = secrets.token_urlsafe(16)
    q = {"response_type": "code", "client_id": client_id,
         "redirect_uri": REDIRECT_URI, "state": state}
    verifier = None
    if pkce:
        verifier = secrets.token_urlsafe(64)
        challenge = base64.urlsafe_b64encode(
            hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
        q.update(code_challenge=challenge, code_challenge_method="S256")
    q.update(params or {})

    result: dict = {}

    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            qs = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
            if "code" in qs or "error" in qs:
                if qs.get("state", [""])[0] != state:
                    result["error"] = "Неверный параметр state"
                elif "error" in qs:
                    result["error"] = qs.get("error_description", qs["error"])[0]
                else:
                    result["code"] = qs["code"][0]
                msg = "Вход выполнен" if "code" in result else "Ошибка: " + result.get("error", "")
                body = _PAGE.format(msg=msg).encode()
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
            else:
                self.send_response(404)
                self.end_headers()

        def log_message(self, *a):
            pass

    srv = http.server.HTTPServer(("127.0.0.1", PORT), Handler)
    srv.timeout = 1
    try:
        webbrowser.open(authorize_url + "?" + urllib.parse.urlencode(q))
        deadline = time.time() + timeout
        while not result and time.time() < deadline:
            if cancel_event is not None and cancel_event.is_set():
                raise OAuthCancelled()
            srv.handle_request()
    finally:
        srv.server_close()

    if "error" in result:
        raise RuntimeError(result["error"])
    if "code" not in result:
        raise TimeoutError("Время ожидания входа истекло")

    form = {"grant_type": "authorization_code", "code": result["code"],
            "redirect_uri": REDIRECT_URI, "client_id": client_id}
    if client_secret:
        form["client_secret"] = client_secret
    if verifier:
        form["code_verifier"] = verifier
    r = requests.post(token_url, data=form, timeout=30)
    if r.status_code != 200:
        raise RuntimeError(f"Обмен кода на токен не удался: {r.status_code} {r.text[:300]}")
    return token_from_response(r.json())
