"""`xo login` in a browser: Clerk's sign-in (OAuth with PKCE), caught on this machine's loopback.

The xo server says where Clerk's sign-in is (GET /login). xo opens it with a one-time verifier's
hash; once you sign in, Clerk sends the browser back to 127.0.0.1 here with a code, and xo hands the
code and the verifier to the xo server (POST /login/browser), which swaps them with Clerk and hands
back the key xo made for your account before, or a new one. Nothing is sent anywhere but the xo
server; the browser talks to Clerk.
"""
from __future__ import annotations

import base64
import hashlib
import html
import os
import re
import secrets
import socket
import sys
import time
import urllib.parse
import webbrowser
from http.server import BaseHTTPRequestHandler, HTTPServer
from typing import Any

from xo.client import LOCAL_HOSTS, Client, XoError

WAIT = 300  # seconds xo waits for the browser to come back
# Seconds one connection to the loopback address may take to send its request. Browsers open
# spare connections they may never use; without this, one such connection (or any local
# program's) would hold the one-request-at-a-time listener, and WAIT, forever.
READ_TIMEOUT = 10
REDIRECT = re.compile(r"http://127\.0\.0\.1:([0-9]{4,5})/callback")
CLIENT_ID = re.compile(r"[A-Za-z0-9_-]{1,100}")
PAGE = ("<!doctype html><meta charset=utf-8><title>xo login</title>"
        "<body style=\"font:16px system-ui,sans-serif;margin:3rem\"><p>{}</p>")


def settings(url: str, timeout: float) -> dict | None:
    """The server's browser login settings, or None when it has none: an older server answers 404,
    and one that does not answer at all is left to the key login, which says what went wrong."""
    try:
        data = Client(url, None, timeout=timeout).request("GET", "/login", auth=False).data
    except XoError:
        return None
    found = data.get("data", {}).get("browser") if isinstance(data, dict) and isinstance(data.get("data"), dict) \
        else None
    if found is None:
        return None
    authorize = found.get("authorize") if isinstance(found, dict) else None
    parts = urllib.parse.urlsplit(authorize) if isinstance(authorize, str) else None
    redirects = found.get("redirects") if isinstance(found, dict) else None
    if (parts is None or not (parts.scheme == "https" or (parts.scheme == "http" and parts.hostname in LOCAL_HOSTS))
            or not parts.hostname or parts.query or parts.fragment
            or not isinstance(found.get("clientId"), str) or not CLIENT_ID.fullmatch(found["clientId"])
            or not isinstance(found.get("scope"), str) or not isinstance(redirects, list) or not redirects
            or not all(isinstance(r, str) and REDIRECT.fullmatch(r) for r in redirects)):
        raise XoError(f"the xo server at {url} sent browser login settings xo cannot use; "
                      "log in with an API key instead (xo login --key)")
    return found


def _pkce() -> tuple[str, str]:
    verifier = secrets.token_urlsafe(64)  # 86 characters, within RFC 7636's 43 to 128
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
    return verifier, challenge


def _listen(redirects: list[str]) -> tuple[HTTPServer, str, dict]:
    """A one-request server on the first free loopback port the server listed, and what it caught."""
    caught: dict[str, Any] = {}

    class Handler(BaseHTTPRequestHandler):
        timeout = READ_TIMEOUT

        def do_GET(self) -> None:  # noqa: N802 - http.server's name
            path, _, query = self.path.partition("?")
            if path != "/callback":
                self._page(404, "Not the xo login address.")
                return
            got = urllib.parse.parse_qs(query)
            state = got.get("state", [""])[0].encode()
            if not secrets.compare_digest(state, caught["state"].encode()):
                # Not this login's answer (an old tab, or someone else's link): ignored, still waiting.
                self._page(400, "This sign-in is not the one xo is waiting for. Go back to the terminal.")
                return
            if "error" in got:
                caught["error"] = got["error"][0][:60]
                self._page(400, "The sign-in was not finished. Go back to the terminal.")
            else:
                caught["code"] = got.get("code", [""])[0]
                self._page(200, "Signed in. You can close this tab and go back to the terminal.")

        def _page(self, status: int, text: str) -> None:
            data = PAGE.format(html.escape(text)).encode()
            self.send_response(status)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Referrer-Policy", "no-referrer")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def log_message(self, *args) -> None:  # the request line holds the code: never printed
            pass

    for redirect in redirects:
        try:
            httpd = HTTPServer(("127.0.0.1", int(REDIRECT.fullmatch(redirect).group(1))), Handler)
        except OSError:
            continue
        httpd.timeout = 1
        return httpd, redirect, caught
    ports = ", ".join(REDIRECT.fullmatch(r).group(1) for r in redirects)
    raise XoError(f"ports {ports} on 127.0.0.1 are all in use, and browser login needs one; "
                  "log in with an API key instead (xo login --key)")


def _can_open() -> bool:
    """Whether to open the link here: not on a Linux machine with no display (over SSH, say), where
    Python would pick a text browser that takes over this terminal; the link is printed instead."""
    return not sys.platform.startswith("linux") or bool(os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY"))


def _name() -> str:
    """The new key's name in Clerk: says which machine it is for."""
    host = re.sub(r"[^A-Za-z0-9._-]", "", socket.gethostname().split(".")[0])[:25]
    return f"xo CLI on {host}" if host else "xo CLI"


def login(url: str, found: dict, timeout: float, wait: float | None = None) -> tuple[str, str]:
    """Sign in in the browser; your API key (one you already had, else a new one) and which it is."""
    wait = WAIT if wait is None else wait
    verifier, challenge = _pkce()
    httpd, redirect, caught = _listen(found["redirects"])
    caught["state"] = secrets.token_urlsafe(24)
    link = found["authorize"] + "?" + urllib.parse.urlencode({
        "response_type": "code", "client_id": found["clientId"], "redirect_uri": redirect, "scope": found["scope"],
        "state": caught["state"], "code_challenge": challenge, "code_challenge_method": "S256"})
    try:
        print(f"xo: sign in to XO in your browser (it opens by itself if it can):\n{link}", file=sys.stderr, flush=True)
        if _can_open():
            try:
                webbrowser.open(link)
            except Exception:  # noqa: BLE001 - the link is printed; opening it is a convenience
                pass
        else:
            # The browser comes back to 127.0.0.1 on the machine it runs on, which over SSH is not this one.
            port = REDIRECT.fullmatch(redirect).group(1)
            print(f"xo: no browser here. Open the link on this machine, or, over SSH, reconnect with "
                  f"ssh -L {port}:127.0.0.1:{port} first; or log in with an API key (xo login --key)",
                  file=sys.stderr, flush=True)
        print(f"xo: waiting for the sign-in (up to {round(wait / 60)} minutes; Ctrl-C stops)", file=sys.stderr,
              flush=True)
        deadline = time.monotonic() + wait
        while "code" not in caught and "error" not in caught:
            if time.monotonic() > deadline:
                raise XoError("the sign-in did not come back in time; nothing saved (run xo login again)")
            httpd.handle_request()
    finally:
        httpd.server_close()
    if "error" in caught:
        raise XoError(f"the sign-in was not finished ({caught['error']}); nothing saved")
    data = Client(url, None, timeout=timeout).request("POST", "/login/browser", auth=False, body={
        "code": caught["code"], "verifier": verifier, "redirect": redirect, "name": _name()}).data
    made = data.get("data") if isinstance(data, dict) and isinstance(data.get("data"), dict) else {}
    key, days = made.get("key"), made.get("days")
    if not isinstance(key, str) or not key.startswith("ak_"):
        raise XoError(f"the xo server at {url} did not answer with an API key; nothing saved")
    if made.get("reused") is True:
        return key, "the key your account already has" + (
            f" (it works {days} more days)" if isinstance(days, int) and days > 0 else "")
    return key, "a new key" + (f" that works for {days} days" if isinstance(days, int) and days > 0 else "")
