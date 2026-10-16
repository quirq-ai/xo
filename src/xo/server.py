"""`xo serve`: the server half of xo, holding the Clerk credentials so the CLI never needs them.

    xo serve [--host 127.0.0.1] [--port 8787]

The same package runs as the command line (on a user's machine) and as this server (on one
deployment that holds the Clerk Secret Key). The CLI sends the user's XO API key; the server
verifies it with Clerk and answers:

    GET   /                         the status page (HTML): health, counts and recent requests
    GET   /status                   the same as JSON: no key, no Clerk call, no ids
    GET   /healthz                  no key, no Clerk call
    GET   /me                       any user key: the key's own account, a fixed list of fields
    GET   /user-metadata/{userId}   admin keys only: public and private metadata
    PATCH /user-metadata/{userId}   admin keys only: deep-merge changes into allow-listed keys
    POST  /api/core/v1/{create}     passed on to XO's core API as sent, key included, and its
    GET   /api/core/v1/spaces/{id}  answer passed back: the creates and the space read only, so
                                    the CLI needs one URL for everything

An admin key is a Clerk API key whose claims carry `xo_admin: true` AND whose id is in
$XO_ADMIN_KEY_IDS. Only top-level metadata keys named in $XO_EDITABLE_PUBLIC_KEYS and
$XO_EDITABLE_PRIVATE_KEYS can be changed; both start empty. Every request to /user-metadata, refused
and failed ones included, is one JSON audit line on stderr with ids and key names, never values or
keys.

Calls to Clerk are capped for the whole process ($XO_CLERK_PER_SECOND, default 20), a bearer token
that does not look like a Clerk API key never reaches Clerk, and each token is rate limited. Open
connections are capped ($XO_SERVE_MAX_CONNECTIONS, default 64), and each is closed after
REQUEST_DEADLINE seconds however slowly its client sends.

Configuration (environment only, so the secret never lands in shell history or the process list):
    CLERK_SECRET_KEY          required; never printed or logged
    XO_ADMIN_KEY_IDS          comma-separated Clerk API key ids (ak_...) treated as admin
    XO_EDITABLE_PUBLIC_KEYS   comma-separated top-level keys PATCH may change in public metadata
    XO_EDITABLE_PRIVATE_KEYS  the same for private metadata
    XO_CLERK_PER_SECOND       calls to Clerk per second for the whole process (default 20)
    XO_SERVE_MAX_CONNECTIONS  connections served at once (default 64)
    CLERK_API_URL             default https://api.clerk.com (tests point it at a fake)
    XO_CORE_API_URL           where /api/core/v1 goes; default https://app.xo.builders/api/core/v1

It listens on 127.0.0.1 by default: put it behind an HTTPS proxy before giving it any other host.
On Vercel, the repo's root app.py serves the same routes through `wsgi_app()` (docs/deploy-vercel.md).
"""
from __future__ import annotations

import collections
import hashlib
import http
import http.client
import json
import os
import re
import socket
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

from xo import __version__, status_page
from xo.client import LOCAL_HOSTS, XoError, redact

SECRET_ENV = "CLERK_SECRET_KEY"
CLERK_URL_ENV = "CLERK_API_URL"
DEFAULT_CLERK_URL = "https://api.clerk.com"
CLERK_TIMEOUT = 10.0
# The Backend API version xo was written against (@clerk/backend sends the same header).
CLERK_API_VERSION = "2026-05-12"
USER_ID = re.compile(r"user_[A-Za-z0-9]{1,64}")
CORE_ENV = "XO_CORE_API_URL"
DEFAULT_CORE_URL = "https://app.xo.builders/api/core/v1"
CORE_PREFIX = "/api/core/v1"  # the client's CORE_PATH
# Under the 30 seconds the CLI waits by default and the server's REQUEST_DEADLINE, so the caller
# hears why rather than timing out itself.
CORE_TIMEOUT = 25.0
MAX_CORE_ANSWER = 1024 * 1024
# What is passed on to the core API: the bundled spec's routes that the server does not answer
# itself (a test keeps the two in step). Each is (method, path pattern, route name); nothing else
# under /api/core/v1 leaves the server, and a path with any escape in it matches none.
CORE_ROUTES = [("POST", re.compile(r"/(claude-code|codex|antigravity|hermes|openclaw|xo-cowork|nebius-openclaw)"),
                None),
               ("GET", re.compile(r"/spaces/[A-Za-z0-9_-]{1,64}"), "/spaces/{spaceId}")]
# What a Clerk API key looks like; anything else is refused without asking Clerk (the SDK checks
# the ak_ prefix the same way). The characters are the CLI's (client.KEY_CHARS): letters, digits and
# _ . - plus base64's + / = (Clerk's own test key has / and =).
API_KEY = re.compile(r"ak_[A-Za-z0-9_.+/=-]{1,200}")
BUCKETS = {"publicMetadata": "public_metadata", "privateMetadata": "private_metadata"}
MAX_BODY = 32 * 1024  # two 8KB buckets with room for JSON overhead
# Requests per minute per key: /me, then /user-metadata. Clerk allows 1000 Backend API calls per
# 10 seconds for the whole instance and 10 metadata writes per 10 seconds per user.
LIMITS = {"me": 60, "user-metadata": 20, "core": 30}
MAX_TRACKED = 10_000  # tokens remembered for rate limiting; the least recently seen go first
ME_CACHE_SECONDS = 60
ACTIVITY_KEPT = 100  # recent requests the status page lists, per process
# The status is "degraded" when a request to a route that calls Clerk (/me, /user-metadata) failed
# with a 5xx within the last DEGRADED_SECONDS. Failures are kept apart from other requests, so no
# amount of other traffic hides one, and each heals by itself once it is older than that.
DEGRADED_SECONDS = 300
DEGRADED_KEPT = 1000  # times remembered of each kind; enough to count five minutes of real traffic
_CLERK_ROUTES = ("/me", "/user-metadata/{userId}")
_PROCESS_STARTED = time.time()  # when this process (on Vercel, this instance) loaded the server
REQUEST_TIMEOUT = 10  # seconds a client may wait between bytes of its request
# Seconds a connection to `xo serve` may stay open in all: a client that trickles one byte at a time
# cannot hold a slot past it. Room for two Clerk calls of CLERK_TIMEOUT each.
REQUEST_DEADLINE = 30
# Control characters in a request line are logged as \xNN, as the standard library does, and a
# backslash as \\, so a logged \xNN always means an escaped character.
_LOG_ESCAPES = {c: f"\\x{c:02x}" for c in (*range(0x20), *range(0x7F, 0xA0))} | {0x5C: "\\\\"}


def _list_env(name: str) -> frozenset[str]:
    return frozenset(v.strip() for v in os.environ.get(name, "").split(",") if v.strip())


def _int_env(name: str, default: int) -> int:
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    if not (raw.isascii() and raw.isdigit()) or int(raw) < 1:
        raise XoError(f"{name} must be a whole number of 1 or more")
    return int(raw)


class Config:
    def __init__(self) -> None:
        secret = os.environ.get(SECRET_ENV, "").strip()
        if not secret:
            raise XoError(f"xo serve needs {SECRET_ENV} in its environment")
        self.secret = secret
        url = (os.environ.get(CLERK_URL_ENV) or DEFAULT_CLERK_URL).rstrip("/")
        parts = urllib.parse.urlsplit(url)
        if not (parts.scheme == "https" or (parts.scheme == "http" and parts.hostname in LOCAL_HOSTS)):
            raise XoError(f"{CLERK_URL_ENV} must be https:// (or http:// to localhost)")
        self.clerk_url = url
        core = (os.environ.get(CORE_ENV) or DEFAULT_CORE_URL).rstrip("/")
        parts = urllib.parse.urlsplit(core)
        if not (parts.scheme == "https" or (parts.scheme == "http" and parts.hostname in LOCAL_HOSTS)):
            raise XoError(f"{CORE_ENV} must be https:// (or http:// to localhost)")
        self.core_url = core
        self.admin_ids = _list_env("XO_ADMIN_KEY_IDS")
        self.editable = {"publicMetadata": _list_env("XO_EDITABLE_PUBLIC_KEYS"),
                         "privateMetadata": _list_env("XO_EDITABLE_PRIVATE_KEYS")}
        self.clerk_per_second = _int_env("XO_CLERK_PER_SECOND", 20)
        self.max_connections = _int_env("XO_SERVE_MAX_CONNECTIONS", 64)


class ClerkError(Exception):
    def __init__(self, status: int, code: str = "", seen: int | str | None = None) -> None:
        super().__init__(f"Clerk answered {status}" + (f" ({code})" if code else ""))
        self.status = status
        self.code = code
        # For the audit line: the status Clerk answered, or what happened when it did not answer.
        self.seen = status if seen is None else seen


BUSY = ClerkError(429, "xo_budget", seen="xo budget")  # our own cap, not Clerk's


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: D102
        raise urllib.error.HTTPError(req.full_url, code, msg, headers, fp)


class _Bucket:
    """A token bucket: `rate` calls a second, up to `rate` at once."""

    def __init__(self, rate: int, clock=time.monotonic) -> None:
        self.rate = rate
        self.clock = clock
        self.tokens = float(rate)
        self.at = clock()
        self.lock = threading.Lock()

    def take(self) -> bool:
        with self.lock:
            now = self.clock()
            self.tokens = min(self.rate, self.tokens + (now - self.at) * self.rate)
            self.at = now
            if self.tokens < 1:
                return False
            self.tokens -= 1
            return True


class Clerk:
    """The only code in the server that talks to the network: Clerk's Backend API."""

    def __init__(self, cfg: Config, clock=time.monotonic) -> None:
        self._cfg = cfg
        self._opener = urllib.request.build_opener(_NoRedirect)
        self._budget = _Bucket(cfg.clerk_per_second, clock)

    def _call(self, method: str, path: str, body: Any = None) -> dict:
        if not self._budget.take():
            raise BUSY
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(self._cfg.clerk_url + "/v1" + path, data=data, method=method, headers={
            "Accept": "application/json", "User-Agent": f"xo-server/{__version__}",
            "Clerk-API-Version": CLERK_API_VERSION,
            **({"Content-Type": "application/json"} if data is not None else {})})
        req.add_unredirected_header("Authorization", f"Bearer {self._cfg.secret}")
        try:
            with self._opener.open(req, timeout=CLERK_TIMEOUT) as resp:
                out = json.loads(resp.read() or b"null")
        except urllib.error.HTTPError as e:
            code = ""
            try:
                errors = json.loads(e.read()).get("errors") or []
                code = str(errors[0].get("code", ""))[:60] if errors and isinstance(errors[0], dict) else ""
            except Exception:  # noqa: BLE001 - the status is enough
                pass
            # A redirect is never followed, so the secret never goes anywhere else; treat it as down.
            if 300 <= e.code < 400:
                raise ClerkError(502, code, seen="redirect") from None
            raise ClerkError(e.code, code) from None
        except (urllib.error.URLError, TimeoutError, OSError, http.client.HTTPException, ValueError):
            raise ClerkError(502, "unreachable", seen="unreachable") from None
        if not isinstance(out, dict):
            raise ClerkError(502, "not an object", seen="not an object")
        return out

    def verify_key(self, secret: str) -> dict:
        return self._call("POST", "/api_keys/verify", {"secret": secret})

    def user(self, user_id: str) -> dict:
        return self._call("GET", "/users/" + urllib.parse.quote(user_id, safe=""))

    def update_metadata(self, user_id: str, changes: dict) -> dict:
        body = {BUCKETS[name]: value for name, value in changes.items()}
        return self._call("PATCH", "/users/" + urllib.parse.quote(user_id, safe="") + "/metadata", body)


class Core:
    """The other network door: XO's core API, for the routes the server passes on."""

    def __init__(self, cfg: Config) -> None:
        self._cfg = cfg
        self._opener = urllib.request.build_opener(_NoRedirect)

    def forward(self, method: str, path: str, token: str, body: bytes | None) -> tuple[int, dict]:
        """XO's answer to the caller's request, sent with the caller's own key and body."""
        headers = {"Accept": "application/json", "User-Agent": f"xo-server/{__version__}"}
        if body is not None:
            headers["Content-Type"] = "application/json"
        req = urllib.request.Request(self._cfg.core_url + path, data=body, method=method, headers=headers)
        req.add_unredirected_header("Authorization", f"Bearer {token}")
        try:
            with self._opener.open(req, timeout=CORE_TIMEOUT) as resp:
                status, raw = resp.status, resp.read(MAX_CORE_ANSWER)
        except urllib.error.HTTPError as e:
            if 300 <= e.code < 400:  # never followed, so the key goes nowhere else
                return 502, {"success": False, "error": f"XO redirected the request ({e.code}); not followed"}
            try:
                status, raw = e.code, e.read(MAX_CORE_ANSWER)
            except (OSError, http.client.HTTPException, ValueError):
                status, raw = e.code, b""  # the status still says what happened
        except (urllib.error.URLError, TimeoutError, OSError, http.client.HTTPException, ValueError) as e:
            late = isinstance(getattr(e, "reason", e), TimeoutError)
            maybe = "; it may have been received, so check before you retry" if method != "GET" else ""
            return (504, {"success": False, "error": "XO did not answer in time" + maybe}) if late else \
                   (502, {"success": False, "error": "XO did not answer" + maybe})
        try:
            payload = json.loads(raw)
        except (ValueError, RecursionError):
            payload = None
        if status not in _KNOWN_STATUSES:
            return 502, {"success": False, "error": f"XO answered with status {status}"}
        if not isinstance(payload, dict):
            return status, {"success": 200 <= status < 300, "error": f"XO answered {status} without a JSON object"}
        return status, payload


class _Forwarded(Exception):
    """XO's answer, passed back to the caller as it came."""

    def __init__(self, status: int, payload: dict) -> None:
        super().__init__(status)
        self.status = status
        self.payload = payload


class _Reply(Exception):
    def __init__(self, status: int, error: str, clerk: int | str | None = None) -> None:
        super().__init__(error)
        self.status = status
        self.error = error
        self.clerk = clerk  # what Clerk said, for the audit line, when Clerk is why the request failed


MISCONFIGURED = "xo server misconfigured; tell whoever runs it"


def _clerk_failure(e: ClerkError) -> _Reply:
    """A Clerk failure that is not about the caller's request: our secret, our cap, or Clerk down."""
    if e.status in (401, 403):
        # One loud line for whoever runs the server; never the secret.
        print(f"xo serve: Clerk refused this server's {SECRET_ENV} ({e.status}); check or rotate it",
              file=sys.stderr, flush=True)
        return _Reply(503, MISCONFIGURED, e.seen)
    if e.status == 429:
        return _Reply(503, "The server is busy; try again shortly", e.seen)
    return _Reply(502, "Clerk did not answer", e.seen)


class App:
    """Routing, auth, limits and audit, apart from the HTTP plumbing so tests can drive it."""

    def __init__(self, cfg: Config, clerk: Clerk | None = None, audit=None, clock=time.monotonic,
                 core: Core | None = None) -> None:
        self.cfg = cfg
        self.clerk = clerk or Clerk(cfg, clock)
        self.core = core or Core(cfg)
        self.audit_out = audit or (lambda line: print(line, file=sys.stderr, flush=True))
        self.clock = clock
        self._lock = threading.Lock()
        self._hits: collections.OrderedDict[tuple[str, str], list[float]] = collections.OrderedDict()
        self._me_cache: dict[str, tuple[float, dict]] = {}
        # What the status page shows: counts and the last ACTIVITY_KEPT requests, each only its time,
        # method, route template, status and duration (never a key, user id, path or address).
        self.started = _PROCESS_STARTED
        self._activity: collections.deque[dict] = collections.deque(maxlen=ACTIVITY_KEPT)
        self._seen: collections.deque[float] = collections.deque(maxlen=DEGRADED_KEPT)  # Clerk routes
        self._failed: collections.deque[float] = collections.deque(maxlen=DEGRADED_KEPT)  # and their 5xx
        self._counts = {"requests": 0, "pings": 0, "2xx": 0, "3xx": 0, "4xx": 0, "5xx": 0}

    def audit(self, **fields: Any) -> None:
        fields = {"time": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), **fields}
        self.audit_out(json.dumps(fields, sort_keys=True))

    def _limit(self, token: str, group: str) -> None:
        # By a hash of the bearer token, before verifying it, so a flood never reaches Clerk. The
        # table is an LRU of at most MAX_TRACKED tokens, so made-up tokens cannot grow it.
        who = (hashlib.sha256(token.encode()).hexdigest()[:16], group)
        now = self.clock()
        with self._lock:
            hits = [t for t in self._hits.pop(who, []) if now - t < 60]
            over = len(hits) >= LIMITS[group]
            if not over:
                hits.append(now)
            self._hits[who] = hits  # now the most recently seen
            while len(self._hits) > MAX_TRACKED:
                self._hits.popitem(last=False)
        if over:
            raise _Reply(429, "Too many requests; try again in a minute")

    def _token(self, headers: dict[str, str], group: str) -> str:
        """The caller's bearer token, if it looks like an API key and is within its rate limit."""
        auth = headers.get("authorization", "")
        token = auth[7:].strip() if auth[:7].lower() == "bearer " else ""
        if not token:
            raise _Reply(401, "Missing API key")
        if not API_KEY.fullmatch(token):
            raise _Reply(401, "Invalid API key")  # never sent to Clerk or XO
        self._limit(token, group)
        return token

    def _key(self, headers: dict[str, str], group: str) -> dict:
        token = self._token(headers, group)
        try:
            key = self.clerk.verify_key(token)  # never cached: a revoked key stops at once
        except ClerkError as e:
            if e.status in (400, 404):  # Clerk: malformed, or no such key
                raise _Reply(401, "Invalid API key") from None
            raise _clerk_failure(e) from None
        if key.get("revoked") is not False or key.get("expired") is not False:
            raise _Reply(401, "Invalid API key")
        if not str(key.get("subject", "")).startswith("user_"):
            raise _Reply(401, "API key is not tied to a user")
        return key

    def _admin(self, key: dict) -> bool:
        claims = key.get("claims") if isinstance(key.get("claims"), dict) else {}
        return claims.get("xo_admin") is True and key.get("id") in self.cfg.admin_ids

    def handle(self, method: str, path: str, headers: dict[str, str], body: bytes | None) -> tuple[int, dict]:
        start = time.monotonic()
        try:
            status, payload = 200, {"success": True, "data": self._route(method, path, headers, body)}
        except _Forwarded as f:
            status, payload = f.status, f.payload
        except _Reply as r:
            status, payload = r.status, {"success": False, "error": r.error}
        self.record(method, path, status, start)
        return status, payload

    def record(self, method: str, path: str, status: int, start: float) -> None:
        """Count a request for the status page; the page and its own polling are left out."""
        route = _route_name(path)
        if route is None:
            return
        entry = {"time": _iso(time.time()), "method": method if method in _METHODS else "OTHER", "route": route,
                 "status": status, "ms": round((time.monotonic() - start) * 1000)}
        with self._lock:
            self._activity.appendleft(entry)
            if route in _CLERK_ROUTES:
                self._seen.append(self.clock())
                if status >= 500:
                    self._failed.append(self.clock())
            self._counts["requests"] += 1
            self._counts["pings"] += route == "/healthz" and status == 200
            self._counts[f"{min(max(status // 100, 2), 5)}xx"] += 1

    def status(self) -> dict:
        with self._lock:
            counts, activity = dict(self._counts), list(self._activity)
            since = self.clock() - DEGRADED_SECONDS
            seen = sum(t > since for t in self._seen)
            failed = sum(t > since for t in self._failed)
        return {"status": "degraded" if failed else "ok", "version": __version__,
                "recent": {"entries": seen, "failed": failed},
                "instance": {"startedAt": _iso(self.started), "requests": counts["requests"],
                             "pings": counts["pings"],
                             "byStatus": {k: counts[k] for k in ("2xx", "3xx", "4xx", "5xx")}},
                "activity": activity}

    def refused(self, method: str, path: str, status: int) -> None:
        """Audit a /user-metadata request refused before it reached the app (a body too big)."""
        self.record(method, path, status, time.monotonic())
        if urllib.parse.urlsplit(path).path.startswith("/user-metadata/"):
            self.audit(method=method, route="/user-metadata", keyId=None, caller=None, target=None, outcome=status)

    def _route(self, method: str, path: str, headers: dict[str, str], body: bytes | None) -> Any:
        path = urllib.parse.urlsplit(path).path.rstrip("/") or "/"
        if path == "/healthz" and method in ("GET", "HEAD"):
            return {"status": "ok"}
        if path == "/status" and method in ("GET", "HEAD"):
            return self.status()
        if path == "/me":
            if method != "GET":
                raise _Reply(405, "Method not allowed")
            return self._me(self._key(headers, "me"))
        if path.startswith(CORE_PREFIX + "/"):
            return self._forward(method, path[len(CORE_PREFIX):], headers, body)
        if path.startswith("/user-metadata/"):
            if method not in ("GET", "PATCH"):
                self.audit(method=method[:10], route="/user-metadata", keyId=None, caller=None, target=None,
                           outcome=405)
                raise _Reply(405, "Method not allowed")
            return self._metadata(method, urllib.parse.unquote(path[len("/user-metadata/"):]), headers, body)
        raise _Reply(404, "Not found")

    def _forward(self, method: str, path: str, headers: dict[str, str], body: bytes | None) -> Any:
        """Pass a core API request on to XO: only the routes in CORE_ROUTES, only with something
        that looks like an API key, within its rate limit. XO checks the key itself."""
        if not _passes_on(method, path):
            raise _Reply(404, "Not found")
        token = self._token(headers, "core")
        raise _Forwarded(*self.core.forward(method, path, token, body if method != "GET" else None))

    def _me(self, key: dict) -> dict:
        subject = key["subject"]
        now = self.clock()
        with self._lock:
            hit = self._me_cache.get(subject)
        if hit and now - hit[0] < ME_CACHE_SECONDS:
            return hit[1]
        try:
            u = self.clerk.user(subject)
        except ClerkError as e:
            if e.status == 404:
                raise _Reply(404, "Account not found") from None
            raise _clerk_failure(e) from None
        data = {"id": u.get("id"), "username": u.get("username"), "firstName": u.get("first_name"),
                "lastName": u.get("last_name"), "imageUrl": u.get("image_url"), "createdAt": u.get("created_at")}
        with self._lock:
            self._me_cache[subject] = (now, data)
        return data

    def _metadata(self, method: str, user_id: str, headers: dict[str, str], body: bytes | None) -> dict:
        target = user_id if USER_ID.fullmatch(user_id) else "<invalid>"
        try:
            key = self._key(headers, "user-metadata")  # auth before anything else is looked at
        except _Reply as r:
            # 401s here are someone trying keys; 429, 502 and 503 are worth seeing too.
            self.audit(method=method, route="/user-metadata", keyId=None, caller=None, target=target,
                       outcome=r.status, **({"clerk": r.clerk} if r.clerk is not None else {}))
            raise
        who = {"method": method, "route": "/user-metadata", "keyId": key.get("id"), "caller": key.get("subject")}
        if not self._admin(key):
            claims = key.get("claims") if isinstance(key.get("claims"), dict) else {}
            # A key claiming xo_admin but not on the list is the forgery signal: always logged.
            self.audit(**who, outcome="denied", claimedAdmin=claims.get("xo_admin") is True, target=target)
            raise _Reply(403, "Admin key required")
        if target == "<invalid>":
            self.audit(**who, outcome="bad user id", target=target)
            raise _Reply(422, "userId is not a Clerk user id (user_ followed by letters and digits)")
        who["target"] = user_id
        changes = self._changes(body, who) if method == "PATCH" else None
        changed = {name: sorted(value) for name, value in changes.items()} if changes else None
        extra = {"changed": changed} if changed else {}
        try:
            u = self.clerk.update_metadata(user_id, changes) if changes else self.clerk.user(user_id)
        except ClerkError as e:
            reply = self._clerk_reply(e)
            # The outcome is what the caller got; `clerk` is what Clerk (or our own budget) said.
            self.audit(**who, outcome=reply.status, clerk=e.seen, **extra)
            raise reply from None
        data = {"publicMetadata": u.get("public_metadata") or {}, "privateMetadata": u.get("private_metadata") or {}}
        self.audit(**who, outcome=200, **extra)  # after the reply is built
        return data

    def _changes(self, body: bytes | None, who: dict) -> dict:
        def bad(error: str, **extra: Any) -> _Reply:
            self.audit(**who, outcome="refused", **extra)
            return _Reply(422, error)
        try:
            changes = json.loads(body or b"")
        except (json.JSONDecodeError, UnicodeDecodeError, RecursionError):
            raise bad("Body is not JSON") from None
        if not isinstance(changes, dict) or not changes:
            raise bad("Body must be an object with publicMetadata, privateMetadata or both")
        if set(changes) - set(BUCKETS):
            raise bad("Body may only hold publicMetadata and privateMetadata")
        for name, value in changes.items():
            if not isinstance(value, dict):
                raise bad(f"{name} must be a JSON object")
            off = sorted(k for k in value if k not in self.cfg.editable[name])
            if off:
                raise bad(f"{name}.{off[0][:60]} is not editable", bucket=name, keys=[k[:60] for k in off[:10]])
        return changes

    @staticmethod
    def _clerk_reply(e: ClerkError) -> _Reply:
        if e.status == 404:
            return _Reply(404, "User not found")
        if e.status in (400, 422):
            return _Reply(422, "Clerk refused the change (each bucket holds at most 8KB)")
        return _clerk_failure(e)


_METHODS = frozenset({"GET", "HEAD", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"})
_KNOWN_STATUSES = frozenset(s.value for s in http.HTTPStatus if s.value >= 200)


def _passes_on(method: str, path: str) -> bool:
    """Whether the server passes this core API request (path below CORE_PREFIX) on to XO."""
    return any(method == m and pattern.fullmatch(path) for m, pattern, _ in CORE_ROUTES)


def _iso(t: float) -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(t))


def _route_name(path: str) -> str | None:
    """The route a path belongs to, for the status page: never the path itself, which can hold a
    user id. None for the page and its polling, which are not counted."""
    path = urllib.parse.urlsplit(path).path.rstrip("/") or "/"
    if path in ("/", "/status"):
        return None
    if path in ("/healthz", "/me"):
        return path
    if path.startswith("/user-metadata/"):
        return "/user-metadata/{userId}"
    if path.startswith(CORE_PREFIX + "/"):
        sub = path[len(CORE_PREFIX):]
        return next((CORE_PREFIX + (name or sub) for _, pattern, name in CORE_ROUTES if pattern.fullmatch(sub)),
                    "other")
    return "other"


def is_page(method: str, path: str) -> bool:
    """Whether this request is for the status page (HTML), not the JSON API."""
    return method in ("GET", "HEAD") and urllib.parse.urlsplit(path).path == "/"


PAGE_HEADERS = [("Content-Type", "text/html; charset=utf-8"), ("Cache-Control", "no-store"),
                ("Content-Security-Policy", status_page.CSP), ("X-Content-Type-Options", "nosniff"),
                ("Referrer-Policy", "no-referrer")]


class _Server(ThreadingHTTPServer):
    """At most `limit` connections are served at once; the next one gets a 503 and is closed."""

    daemon_threads = True

    def __init__(self, address, handler, limit: int) -> None:
        self._slots = threading.BoundedSemaphore(limit)
        super().__init__(address, handler)

    def process_request(self, request, client_address):  # noqa: D102
        if not self._slots.acquire(blocking=False):
            try:
                request.sendall(b"HTTP/1.1 503 Service Unavailable\r\nContent-Type: application/json\r\n"
                                b"Content-Length: 56\r\nConnection: close\r\n\r\n"
                                b'{"success": false, "error": "Too many open connections"}')
            except OSError:
                pass
            self.shutdown_request(request)
            return
        try:
            super().process_request(request, client_address)
        except Exception:
            self._slots.release()
            raise

    def process_request_thread(self, request, client_address):  # noqa: D102
        try:
            super().process_request_thread(request, client_address)
        finally:
            self._slots.release()


def wsgi_app(make_app=None):
    """The same server as a WSGI app, for hosts that run Python functions (Vercel's root app.py).

    PATH_INFO must be the raw, still percent-encoded path, as Vercel's runtime passes it; a server
    that decodes it first would let `%3F` in a user id read as a query.

    The App is built on the first request, so a missing or bad setting answers 503 and says why on
    stderr, instead of failing the import.
    """
    state: dict[str, Any] = {}
    lock = threading.Lock()

    def application(environ, start_response):
        if is_page(environ.get("REQUEST_METHOD", "GET"), environ.get("PATH_INFO") or "/"):
            start_response("200 OK", [*PAGE_HEADERS, ("Content-Length", str(len(status_page.PAGE)))])
            return [] if environ.get("REQUEST_METHOD") == "HEAD" else [status_page.PAGE]
        with lock:
            if "app" not in state and "error" not in state:
                try:
                    state["app"] = (make_app or (lambda: App(Config())))()
                except XoError as e:
                    state["error"] = str(e)
        if "error" in state:
            print(f"xo serve: {state['error']}", file=sys.stderr, flush=True)
            return _wsgi_reply(start_response, 503, {"success": False, "error": MISCONFIGURED})
        app: App = state["app"]
        method = environ.get("REQUEST_METHOD", "GET")
        path = environ.get("PATH_INFO") or "/"
        if environ.get("QUERY_STRING"):
            path += "?" + environ["QUERY_STRING"]
        headers = {k[5:].replace("_", "-").lower(): v for k, v in environ.items() if k.startswith("HTTP_")}
        body = None
        length = environ.get("CONTENT_LENGTH") or ""
        if length:
            size = int(length) if length.isascii() and length.isdigit() else -1
            if size < 0 or size > MAX_BODY:
                app.refused(method, path, 413)
                reply = _wsgi_reply(start_response, 413, {"success": False, "error": f"Body over {MAX_BODY} bytes"})
                return [] if method == "HEAD" else reply
            body = environ["wsgi.input"].read(size)
        status, payload = app.handle(method, path, headers, body)
        reply = _wsgi_reply(start_response, status, payload)
        return [] if method == "HEAD" else reply

    return application


def _wsgi_reply(start_response, status: int, payload: dict) -> list[bytes]:
    data = json.dumps(payload).encode()
    start_response(f"{status} {http.HTTPStatus(status).phrase}", [
        ("Content-Type", "application/json"), ("Content-Length", str(len(data))), ("Cache-Control", "no-store")])
    return [data]


def make_server(app: App, host: str, port: int) -> ThreadingHTTPServer:
    class Handler(BaseHTTPRequestHandler):
        server_version = f"xo-server/{__version__}"
        sys_version = ""
        timeout = REQUEST_TIMEOUT

        def setup(self) -> None:
            super().setup()
            # Closes the connection at REQUEST_DEADLINE however slowly the client sends.
            self._deadline = threading.Timer(REQUEST_DEADLINE, self._expire)
            self._deadline.daemon = True
            self._deadline.start()

        def _expire(self) -> None:
            try:
                self.connection.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass

        def finish(self) -> None:
            self._deadline.cancel()
            super().finish()

        def _serve(self) -> None:
            if is_page(self.command, self.path):
                self.send_response(200)
                for name, value in [*PAGE_HEADERS, ("Content-Length", str(len(status_page.PAGE)))]:
                    self.send_header(name, value)
                self.end_headers()
                if self.command != "HEAD":
                    self.wfile.write(status_page.PAGE)
                return
            length = self.headers.get("Content-Length")
            body = None
            if length:
                try:
                    size = int(length)
                except ValueError:
                    size = -1
                if size < 0 or size > MAX_BODY:
                    app.refused(self.command, self.path, 413)
                    self._send(413, {"success": False, "error": f"Body over {MAX_BODY} bytes"})
                    return
                body = self.rfile.read(size)
            headers = {k.lower(): v for k, v in self.headers.items()}
            status, payload = app.handle(self.command, self.path, headers, body)
            self._send(status, payload)

        def _send(self, status: int, payload: dict) -> None:
            data = json.dumps(payload).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            if self.command != "HEAD":
                self.wfile.write(data)

        do_GET = do_HEAD = do_PATCH = do_POST = do_PUT = do_DELETE = _serve

        def log_message(self, fmt: str, *args: Any) -> None:
            # The request line only (no headers, so never the key), with the secret scrubbed anyway.
            # Behind a proxy the address is the proxy's: per-address limits belong in the proxy.
            line = redact(fmt % args, app.cfg.secret).translate(_LOG_ESCAPES)
            print(f"xo-server: {self.address_string()} {line}", file=sys.stderr, flush=True)

    return _Server((host, port), Handler, app.cfg.max_connections)


def serve(host: str, port: int) -> int:
    app = App(Config())
    server = make_server(app, host, port)
    where = f"http://{host}:{server.server_address[1]}"
    print(f"xo serve: listening on {where} (Clerk at {app.cfg.clerk_url}; "
          f"{len(app.cfg.admin_ids)} admin key id(s))", file=sys.stderr, flush=True)
    if host not in LOCAL_HOSTS:
        print("xo serve: not on localhost; serve it only behind HTTPS", file=sys.stderr, flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0
