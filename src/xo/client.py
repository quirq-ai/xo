"""The one HTTP door to the XO API.

Everything xo sends goes through `Client.request`: this module is the only place that knows the
base URL, how the API key is sent, the timeout, and how a failure is shaped. It never prints or
returns the key (nor the provider keys and bot tokens it is told about), never follows a redirect (a redirect could carry the key to another host), and
refuses plain http except to this machine.
"""
from __future__ import annotations

import functools
import getpass
import http.client
import json
import os
import re
import sys
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from typing import Any

from xo import __version__

DEFAULT_BASE_URL = "https://app.xo.builders/api/core/v1"
DEFAULT_SERVER_URL = "https://xo.quirq.dev"
DEFAULT_TIMEOUT = 30.0
KEY_ENV = "XO_API_KEY"
BASE_URL_ENV = "XO_BASE_URL"
SERVER_URL_ENV = "XO_SERVER_URL"
LOCAL_HOSTS = {"localhost", "127.0.0.1", "::1"}
REDACTED = "<redacted>"
# A secret shorter than this is not looked for in text: replacing it would garble the message.
MIN_SECRET = 4
MAYBE_RECEIVED = "the request may have been received, so check before you retry"
# What an API key may hold: letters, digits, _ . - and base64's + / = (Clerk's own test key has / and
# =). The server's check (server.API_KEY) uses the same characters.
KEY_CHARS = re.compile(r"[A-Za-z0-9_.+/=-]+")


class XoError(Exception):
    """A failure with one actionable line. Its text never contains the API key."""


@dataclass
class Response:
    status: int
    data: Any
    text: str


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: D102
        raise urllib.error.HTTPError(req.full_url, code, msg, headers, fp)


def server_url(flag: str | None = None) -> str:
    """The base of an xo server (`xo serve`), for the routes that need Clerk."""
    url = (flag or os.environ.get(SERVER_URL_ENV) or DEFAULT_SERVER_URL).rstrip("/")
    try:
        return base_url(url)
    except XoError:
        source = "--server-url" if flag else SERVER_URL_ENV
        raise XoError(f"{source} must be https:// (or http:// to localhost)") from None


def base_url(flag: str | None = None) -> str:
    url = (flag or os.environ.get(BASE_URL_ENV) or DEFAULT_BASE_URL).rstrip("/")
    parts = urllib.parse.urlsplit(url)
    if parts.scheme == "https" and parts.hostname:
        return url
    if parts.scheme == "http" and parts.hostname in LOCAL_HOSTS:
        return url
    raise XoError(f"base URL must be https:// (or http:// to localhost), not {url!r}")


def api_key(flag: str | None = None) -> str:
    """The key from --api-key (`-` reads one line from stdin) or $XO_API_KEY. Never echoed."""
    if flag == "-":
        # On a terminal, read without echo so the key never lands in the scrollback.
        typed = getpass.getpass("XO API key: ") if sys.stdin.isatty() else sys.stdin.readline()
        source, key = "the key read from stdin", typed.strip()
    elif flag:
        source, key = "--api-key", flag.strip()
    else:
        source, key = KEY_ENV, (os.environ.get(KEY_ENV) or "").strip()
    if not key:
        raise XoError(f"no API key: set {KEY_ENV} or pass --api-key - to read it from stdin "
                      "(get a key from the XO team)")
    # Only characters that JSON and repr never escape, so redaction finds the key as sent; redact()
    # also looks for the forms a URL, an escaping JSON writer or an HTML writer would give + / and =.
    if not KEY_CHARS.fullmatch(key):
        raise XoError(f"{source} holds characters an API key cannot have "
                      "(only letters, digits and _ . - + / = are allowed); the key is not shown")
    return key


def redact(value: Any, key: str | None) -> Any:
    """`value` with every occurrence of `key` replaced, in strings, list items and dict keys."""
    if not key:
        return value
    if isinstance(value, str):
        return _pattern(key).sub(lambda _: REDACTED, value)
    if isinstance(value, list):
        return [redact(v, key) for v in value]
    if isinstance(value, dict):
        return {redact(k, key): redact(v, key) for k, v in value.items()}
    return value


@functools.lru_cache(maxsize=32)
def _pattern(key: str) -> re.Pattern[str]:
    """`key` as sent, or as a URL, a JSON writer or an HTML writer might show it, in any mix.

    Each character a URL may escape matches itself or its %XX form in either case (a space also as
    +), and / also matches JSON's \\/ and HTML's &#x2F; and &#47;.
    """
    parts = []
    for c in key:
        if c.isascii() and (c.isalnum() or c in "_.-~"):
            parts.append(re.escape(c))
            continue
        hexed = "".join(f"%{b:02X}" for b in c.encode())
        forms = {c, hexed, hexed.lower(), *(("+",) if c == " " else ()),
                 *(("\\/", "&#x2F;", "&#x2f;", "&#47;") if c == "/" else ())}
        parts.append("(?:" + "|".join(re.escape(f) for f in sorted(forms, key=len, reverse=True)) + ")")
    return re.compile("".join(parts))


def _one_line(value: Any) -> str:
    return " ".join(str(value).split())


class Client:
    def __init__(self, base: str, key: str | None, timeout: float = DEFAULT_TIMEOUT):
        self.base = base
        self._key = key
        self.timeout = timeout
        self._opener = urllib.request.build_opener(_NoRedirect)
        self._secrets: list[str] = []  # provider keys and bot tokens in the body being sent
        self.in_flight: str | None = None  # "POST /codex" while a request that changes things is out

    def hide(self, secrets: list[str]) -> None:
        """Also redact these (a body's provider keys and bot tokens) from everything printed."""
        found = {s for s in secrets if len(s) >= MIN_SECRET and s != self._key}
        self._secrets = sorted(found | set(self._secrets), key=len, reverse=True)

    def scrub(self, value: Any) -> Any:
        for secret in (self._key, *self._secrets):
            value = redact(value, secret)
        return value

    def url(self, path: str, query: dict[str, Any] | None = None) -> str:
        if not path.startswith("/"):
            path = "/" + path
        url = self.base + path
        pairs = [(k, v) for k, v in (query or {}).items() if v is not None]
        if pairs:
            url += "?" + urllib.parse.urlencode(pairs, doseq=True)
        return url

    @staticmethod
    def encode(body: Any) -> bytes:
        try:
            return json.dumps(body, allow_nan=False).encode()
        except ValueError as e:
            raise XoError(f"the body is not valid JSON: {e}") from None

    def request(self, method: str, path: str, *, query: dict[str, Any] | None = None,
                body: Any = None, headers: dict[str, str] | None = None, auth: bool = True) -> Response:
        """Send one request and return the parsed response, or raise XoError. 2xx only."""
        if auth and not self._key:
            raise XoError(f"no API key: set {KEY_ENV} or pass --api-key -")
        sent = {"Accept": "application/json", "User-Agent": f"xo/{__version__}", **(headers or {})}
        data = None
        if body is not None:
            data = self.encode(body)
            sent["Content-Type"] = "application/json"
        label = f"{method.upper()} {path}"
        try:
            req = urllib.request.Request(self.url(path, query), data=data, headers=sent, method=method.upper())
            if auth:
                # Unredirected: kept off any follow-up request, on top of never following redirects.
                req.add_unredirected_header("Authorization", f"Bearer {self._key}")
            self.in_flight = label if method.upper() != "GET" else None
            with self._opener.open(req, timeout=self.timeout) as resp:
                raw = resp.read()
            self.in_flight = None
            return self._parse(resp.status, raw)
        except urllib.error.HTTPError as e:
            self.in_flight = None
            raise XoError(self.scrub(self._http_error(label, e))) from None
        except (urllib.error.URLError, TimeoutError, OSError) as e:
            reason = getattr(e, "reason", e)
            # A timeout may come after the server took the request: a create may have happened.
            maybe = self.in_flight and isinstance(reason, TimeoutError)
            self.in_flight = None
            raise XoError(self.scrub(f"{label}: could not reach {self.base} ({_one_line(reason)})"
                                     + (f"; {MAYBE_RECEIVED}" if maybe else ""))) from None
        except (http.client.HTTPException, ValueError) as e:
            self.in_flight = None
            reason = f"{type(e).__name__}: {_one_line(e)}"
            raise XoError(self.scrub(f"{label}: bad request or response ({reason})")) from None

    def _parse(self, status: int, raw: bytes) -> Response:
        text = self.scrub(raw.decode("utf-8", errors="replace"))
        try:
            data = self.scrub(json.loads(raw)) if raw.strip() else None
        except (json.JSONDecodeError, UnicodeDecodeError):
            data = None
        return Response(status, data, text)

    def _http_error(self, label: str, e: urllib.error.HTTPError) -> str:
        if 300 <= e.code < 400:
            where = e.headers.get("Location", "") if e.headers else ""
            if "sign-in" in where:
                return (f"{label}: XO answered with its sign-in page ({e.code}), so this route does "
                        "not accept an API key")
            return f"{label}: redirected ({e.code}) to {where or 'another URL'}; xo never follows redirects"
        try:
            raw = self.scrub(e.read().decode("utf-8", errors="replace"))
        except Exception:  # noqa: BLE001 - the status line is still worth reporting
            raw = ""
        detail = ""
        try:
            body = self.scrub(json.loads(raw))
            if isinstance(body, dict):
                d = body.get("detail") or body.get("message") or body.get("error")
                detail = d if isinstance(d, str) else json.dumps(d, ensure_ascii=False) if d else ""
        except json.JSONDecodeError:
            first = raw.strip().splitlines()[:1]
            detail = first[0][:200] if first else ""
        hint = {401: " (check the API key)", 403: " (the key lacks access to this)"}.get(e.code, "")
        return f"{label}: HTTP {e.code}{hint}" + (f": {detail}" if detail else "")
