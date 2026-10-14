"""The one HTTP door to the XO API.

Everything xo sends goes through `Client.request`: this module is the only place that knows the
base URL, how the API key is sent, the timeout, and how a failure is shaped. It never prints or
returns the key, never follows a redirect (a redirect could carry the key to another host), and
refuses plain http except to this machine.
"""
from __future__ import annotations

import http.client
import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from typing import Any

from xo import __version__

DEFAULT_BASE_URL = "https://app.xo.builders"
DEFAULT_TIMEOUT = 30.0
KEY_ENV = "XO_API_KEY"
BASE_URL_ENV = "XO_BASE_URL"
LOCAL_HOSTS = {"localhost", "127.0.0.1", "::1"}
REDACTED = "<redacted>"


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
        source, key = "the key read from stdin", sys.stdin.readline().strip()
    elif flag:
        source, key = "--api-key", flag.strip()
    else:
        source, key = KEY_ENV, (os.environ.get(KEY_ENV) or "").strip()
    if not key:
        raise XoError(f"no API key: set {KEY_ENV} or pass --api-key - to read it from stdin "
                      "(create one in XO Cloud under your account's API keys)")
    if not (key.isascii() and key.isprintable()) or any(c.isspace() for c in key):
        raise XoError(f"{source} holds characters an API key cannot have "
                      "(spaces, line breaks or non-ASCII); the key is not shown")
    return key


def redact(value: Any, key: str | None) -> Any:
    """`value` with every occurrence of `key` replaced, in strings, list items and dict keys."""
    if not key:
        return value
    if isinstance(value, str):
        return value.replace(key, REDACTED)
    if isinstance(value, list):
        return [redact(v, key) for v in value]
    if isinstance(value, dict):
        return {redact(k, key): redact(v, key) for k, v in value.items()}
    return value


class Client:
    def __init__(self, base: str, key: str | None, timeout: float = DEFAULT_TIMEOUT):
        self.base = base
        self._key = key
        self.timeout = timeout
        self._opener = urllib.request.build_opener(_NoRedirect)

    def scrub(self, value: Any) -> Any:
        return redact(value, self._key)

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
            with self._opener.open(req, timeout=self.timeout) as resp:
                return self._parse(resp.status, resp.read())
        except urllib.error.HTTPError as e:
            raise XoError(self.scrub(self._http_error(label, e))) from None
        except (urllib.error.URLError, TimeoutError, OSError) as e:
            reason = getattr(e, "reason", e)
            raise XoError(self.scrub(f"{label}: could not reach {self.base} ({reason})")) from None
        except (http.client.HTTPException, ValueError) as e:
            raise XoError(self.scrub(f"{label}: bad request or response ({type(e).__name__}: {e})")) from None

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
