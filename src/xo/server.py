"""`xo serve`: the server half of xo, holding the Clerk credentials so the CLI never needs them.

    xo serve [--host 127.0.0.1] [--port 8787]

The same package runs as the command line (on a user's machine) and as this server (on one
deployment that holds the Clerk Secret Key). The CLI sends the user's XO API key; the server
verifies it with Clerk and answers:

    GET   /                         the status page (HTML): health, counts and recent requests
    GET   /status                   the same as JSON: no key, no Clerk call, no ids
    GET   /healthz                  no key, no Clerk call
    GET   /me                       any user key: the key's own account, with its primary email and
                                    its public and private metadata (secret-looking values masked)
    GET   /me/projects              any user key: the `projects` list in its own private metadata
    POST  /me/projects              any user key: add {"name": ...} to that list; the server builds
                                    the change and writes only `projects`, nothing else
    GET   /me/spaces                any user key: the `spaces` list in its own private metadata
    POST  /me/spaces                any user key: {"kind", "name", "body"}: create the space through
                                    XO with the caller's key, then record it in `spaces`
    POST  /me/spaces/connect        any user key: {"name", "id"?}: a new space entry; with an id, only
                                    for an XO space the key can read
    POST  /me/spaces/{name}/projects  any user key: {"project", "ref"}: set one project's ref in a
                                    space; the server writes only `spaces`, nothing else
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

Calls to Clerk are capped for the whole process ($XO_CLERK_PER_SECOND, default 20), and so are
requests passed on to XO ($XO_CORE_PER_SECOND, default 10). A bearer token that does not look like
a Clerk API key never reaches Clerk or XO, and each token is rate limited. Open
connections are capped ($XO_SERVE_MAX_CONNECTIONS, default 64), and each is closed after
REQUEST_DEADLINE seconds however slowly its client sends.

Configuration (environment only, so the secret never lands in shell history or the process list):
    CLERK_SECRET_KEY          required; never printed or logged
    XO_ADMIN_KEY_IDS          comma-separated Clerk API key ids (ak_...) treated as admin
    XO_EDITABLE_PUBLIC_KEYS   comma-separated top-level keys PATCH may change in public metadata
    XO_EDITABLE_PRIVATE_KEYS  the same for private metadata
    XO_ME_PRIVATE_KEYS        comma-separated top-level private metadata keys /me shows a user
                              (unset: all of them)
    XO_CLERK_PER_SECOND       calls to Clerk per second for the whole process (default 20)
    XO_CORE_PER_SECOND        requests passed on to XO per second for the whole process (default 10)
    XO_SERVE_MAX_CONNECTIONS  connections served at once (default 64)
    CLERK_API_URL             default https://api.clerk.com (tests point it at a fake)
    XO_CORE_API_URL           where /api/core/v1 goes; default the xo address in the
                              services list (https://dev.xo.builders) plus /api/core/v1

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

from xo import __version__, names, services, status_page
from xo.client import LOCAL_HOSTS, XoError, redact

SECRET_ENV = "CLERK_SECRET_KEY"
CLERK_URL_ENV = "CLERK_API_URL"
DEFAULT_CLERK_URL = "https://api.clerk.com"
CLERK_TIMEOUT = 10.0
# The Backend API version xo was written against (@clerk/backend sends the same header).
CLERK_API_VERSION = "2026-05-12"
USER_ID = re.compile(r"user_[A-Za-z0-9]{1,64}")
CORE_ENV = "XO_CORE_API_URL"
CORE_PREFIX = "/api/core/v1"  # the client's CORE_PATH
# XO's platform: the address in the services list (services.json), unless XO_CORE_API_URL says.
DEFAULT_CORE_URL = services.builtin()["xo"].url + CORE_PREFIX
# XO's time for a whole forward, under the 30 seconds the CLI waits by default and the server's
# REQUEST_DEADLINE, so the caller hears why rather than timing out itself.
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
LIMITS = {"me": 60, "projects": 30, "spaces": 30, "user-metadata": 20, "core": 30}
# /me/projects: the one write a normal key can make. A name is letters, digits, spaces and _ . -,
# starting with a letter or digit; the list stays well under Clerk's 8KB private metadata limit.
MAX_PROJECTS = 30
MAX_SPACES = 10
MAX_SPACE_PROJECTS = 10
# The most a write may leave in a user's private metadata. Clerk keeps 8KB, shared with XO's own
# keys, so xo's lists stop here and leave XO at least 2KB to grow into.
PRIVATE_BUDGET = 6 * 1024
DEFAULT_REF = "main"
PROJECT_TRIES = 3  # writes tried when the list read back is not the one written
PROJECT_SETTLE = 0.3  # seconds before the read that confirms a write
MAX_TRACKED = 10_000  # tokens remembered for rate limiting; the least recently seen go first
ME_CACHE_SECONDS = 60
ME_CACHE_PRUNE_AT = 1000  # cached /me answers kept before the expired ones are dropped
ACTIVITY_KEPT = 100  # recent requests the status page lists, per process
# The status is "degraded" when a request to a route that calls Clerk (/me, /user-metadata) failed
# with a 5xx within the last DEGRADED_SECONDS. Failures are kept apart from other requests, so no
# amount of other traffic hides one, and each heals by itself once it is older than that.
DEGRADED_SECONDS = 300
DEGRADED_KEPT = 1000  # times remembered of each kind; enough to count five minutes of real traffic
_CLERK_ROUTES = ("/me", "/me/projects", "/me/spaces", "/me/spaces/connect", "/me/spaces/{name}/projects",
                 "/user-metadata/{userId}")
# The creates `xo create space KIND` may record: the core routes that make a space.
SPACE_KINDS = ("claude-code", "codex", "antigravity", "hermes", "openclaw", "xo-cowork", "nebius-openclaw")
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


def _upstream(name: str, url: str) -> str:
    """An https:// address (or http:// to this machine) for a server setting, checked once at startup
    so a malformed one stops the server with a clear message instead of failing on every request."""
    url = url.strip().rstrip("/")  # a pasted value often ends in a newline; only whitespace inside is refused
    try:
        if any(c.isspace() or ord(c) < 32 or ord(c) == 127 for c in url):
            raise ValueError
        parts = urllib.parse.urlsplit(url)
        host = parts.hostname  # raises on a malformed bracketed host
    except ValueError:
        raise XoError(f"{name} is not a valid address (check its host and characters)") from None
    try:
        port = parts.port
    except ValueError:
        raise XoError(f"{name} has a port that is not a number from 1 to 65535") from None
    if not (parts.scheme == "https" or (parts.scheme == "http" and host in LOCAL_HOSTS)):
        raise XoError(f"{name} must be https:// (or http:// to localhost)")
    if not host or port == 0:
        raise XoError(f"{name} needs a host name and, if it has a port, one from 1 to 65535")
    return url


class Config:
    def __init__(self) -> None:
        secret = os.environ.get(SECRET_ENV, "").strip()
        if not secret:
            raise XoError(f"xo serve needs {SECRET_ENV} in its environment")
        self.secret = secret
        self.clerk_url = _upstream(CLERK_URL_ENV, os.environ.get(CLERK_URL_ENV) or DEFAULT_CLERK_URL)
        self.core_url = _upstream(CORE_ENV, os.environ.get(CORE_ENV) or DEFAULT_CORE_URL)
        self.admin_ids = _list_env("XO_ADMIN_KEY_IDS")
        self.editable = {"publicMetadata": _list_env("XO_EDITABLE_PUBLIC_KEYS"),
                         "privateMetadata": _list_env("XO_EDITABLE_PRIVATE_KEYS")}
        # Top-level private metadata keys /me shows its user; unset or blank shows them all.
        self.me_private = _list_env("XO_ME_PRIVATE_KEYS") or None
        self.clerk_per_second = _int_env("XO_CLERK_PER_SECOND", 20)
        self.core_per_second = _int_env("XO_CORE_PER_SECOND", 10)
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
                raw = resp.read()
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
        try:
            out = json.loads(raw or b"null")
        except (ValueError, RecursionError):  # Clerk answered, but not with JSON: not "unreachable"
            raise ClerkError(502, "not JSON", seen="not JSON") from None
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


# Metadata names that read like a credential: their values are masked in /me, which hands a user's
# metadata back over a long-lived API key. /user-metadata (admin keys) shows values as stored.
_SECRET_WORDS = {"token", "tokens", "secret", "secrets", "credential", "credentials", "creds", "cookie",
                 "cookies", "key", "keys", "apikey", "auth", "authorization", "bearer", "jwt", "session",
                 "sid", "sk", "pat", "mnemonic", "seed", "dsn", "hmac", "webhook"}
_SECRET_PARTS = ("token", "secret", "credential", "apikey", "privatekey", "accesskey")
# Names whose values show nothing at all, however long: a password's last 4 characters are too many.
_HIDDEN_WORDS = {"password", "passwd", "pwd", "passphrase", "pass", "passcode", "pin", "otp", "totp"}
# Values that look like a credential whatever they are stored under.
_SECRET_VALUE = re.compile(r"\A\s*(?:(?:sk|rk|pk)[-_]|ak_|gh[pousr]_|github_pat_|glpat-|xox[abprs]-|hf_|AKIA|ASIA"
                           r"|AIza|ya29\.|npm_|pypi-|Bearer\s|eyJ[\w-]*\.[\w-]+\.)"
                           r"|-----BEGIN [A-Z ]*PRIVATE KEY-----|://[^/\s:@]+:[^/\s@]+@")


def _secret_name(name: str) -> str | None:
    """"hidden" for a password-like name, "secret" for another credential-like one, else None."""
    words = {w.lower() for w in re.findall(r"[A-Z]?[a-z]+|[A-Z]+(?![a-z])|[0-9]+", name)}
    flat = re.sub(r"[^a-z0-9]", "", name.lower())
    if words & _HIDDEN_WORDS or "passw" in flat:
        return "hidden"
    if words & _SECRET_WORDS or any(part in flat for part in _SECRET_PARTS):
        return "secret"
    return None


def _mask(value: Any) -> str:
    """The last 4 characters of a long string; nothing of anything shorter or not a string."""
    if isinstance(value, str) and len(value) >= 12:
        return "\u2026" + value[-4:]
    return "(hidden)"


def _mask_secrets(value: Any, depth: int = 0) -> Any:
    """A copy with every value under a secret-looking name, and every string that looks like a
    credential, masked, at any depth. Null and empty strings stay as they are: they hold nothing."""
    if depth > 32:
        return "(hidden)"
    if isinstance(value, dict):
        out = {}
        for k, v in value.items():
            kind = _secret_name(k) if isinstance(k, str) else None
            if kind is None or v in (None, ""):
                out[k] = _mask_secrets(v, depth + 1)
            else:
                out[k] = "(hidden)" if kind == "hidden" else _mask(v)
        return out
    if isinstance(value, list):
        return [_mask_secrets(v, depth + 1) for v in value]
    if isinstance(value, str) and _SECRET_VALUE.search(value):
        return _mask(value)
    return value


# Token prefixes a branch can also start with (npm_bump, sk-rewrite, hf_integration). Behind one
# of these a ref is a credential only with a token's body: a long run of letters and digits, or,
# separators removed, 20 or more that mix upper case, lower case and digits (a token split by / or
# -). Every other credential prefix (glpat-, AIza, ya29., eyJ, Bearer) is refused on its own.
_BRANCH_PREFIX = re.compile(r"\A\s*(?:(?:sk|rk|pk)[-_]|ak_|gh[pousr]_|github_pat_|hf_|npm_|pypi-|xox[abprs]-|AKIA|ASIA)")
_TOKEN_BODY = re.compile(r"[A-Za-z0-9]{16,}")


def _secret_ref(ref: str) -> bool:
    """A ref that looks like a credential. A commit sha has a token's body but never a prefix."""
    if not _SECRET_VALUE.search(ref):
        return False
    if not _BRANCH_PREFIX.match(ref) or _TOKEN_BODY.search(ref):
        return True
    body = re.sub(r"[^A-Za-z0-9]", "", ref)
    return len(body) >= 20 and all(re.search(p, body) for p in ("[0-9]", "[a-z]", "[A-Z]"))


def _mask_values(value: Any, depth: int = 0) -> Any:
    """Like _mask_secrets, but by value only, for a space's {project: ref} list: its keys are
    project names, not settings, and its values are refs."""
    if depth > 32:
        return "(hidden)"
    if isinstance(value, dict):
        return {k: _mask_values(v, depth + 1) for k, v in value.items()}
    if isinstance(value, list):
        return [_mask_values(v, depth + 1) for v in value]
    # Only a valid ref gets the ref rule; anything else someone stored is masked as /me masks it.
    if isinstance(value, str) and (_secret_ref(value) if names.ref_ok(value) else _SECRET_VALUE.search(value)):
        return _mask(value)
    return value


def _shown_space(entry: Any) -> Any:
    """A space as answered: masked like other metadata, except its {project: ref} list, whose keys
    are project names (a repository called auth-service or tokenizers is not a secret)."""
    if not isinstance(entry, dict):
        return _mask_secrets(entry)
    return {k: _mask_values(v) if k == "projects" else _mask_secrets({k: v})[k] for k, v in entry.items()}


def _shown_spaces(spaces: Any) -> Any:
    return [_shown_space(e) for e in spaces] if isinstance(spaces, list) else _mask_secrets(spaces)


def _primary_email(u: dict) -> str | None:
    primary = u.get("primary_email_address_id")
    addresses = u.get("email_addresses")
    for e in addresses if isinstance(addresses, list) else []:
        if isinstance(e, dict) and primary is not None and e.get("id") == primary:
            return e.get("email_address")
    return None


class Core:
    """The other network door: XO's core API, for the routes the server passes on."""

    def __init__(self, cfg: Config, clock=time.monotonic) -> None:
        self._cfg = cfg
        self._budget = _Bucket(cfg.core_per_second, clock)

    def forward(self, method: str, path: str, token: str, body: bytes | None) -> tuple[int, dict]:
        """XO's answer to the caller's request, sent with the caller's own key and body.

        XO gets CORE_TIMEOUT seconds for the whole exchange, however slowly it sends: a timer cuts
        the connection then. http.client never follows a redirect, so the key goes nowhere else."""
        maybe = "; it may have been received, so check before you retry" if method != "GET" else ""
        if not self._budget.take():
            return 503, {"success": False, "error": "The server is busy; try again shortly"}
        headers = {"Accept": "application/json", "User-Agent": f"xo-server/{__version__}",
                   "Authorization": f"Bearer {token}"}
        if body is not None:
            headers["Content-Type"] = "application/json"
        parts = urllib.parse.urlsplit(self._cfg.core_url)
        xo = f"XO ({parts.hostname})"  # names the upstream, so a timeout says which one did not answer
        kind = http.client.HTTPSConnection if parts.scheme == "https" else http.client.HTTPConnection
        # One second past the timer, so a reply cut at the deadline is always the timer's 504, not a 502.
        conn = kind(parts.hostname, parts.port, timeout=CORE_TIMEOUT + 1)
        late = threading.Event()
        opened: list[socket.socket] = []  # kept here: http.client drops conn.sock once it will close

        def cut() -> None:
            late.set()
            for sock in opened:
                try:
                    sock.shutdown(socket.SHUT_RDWR)  # wakes a read blocked in the other thread
                except OSError:
                    pass

        timer = threading.Timer(CORE_TIMEOUT, cut)
        timer.daemon = True
        timer.start()
        resp = None
        try:
            conn.connect()
            opened.append(conn.sock)
            if late.is_set():  # the timer fired while connecting, before there was a socket to cut
                raise TimeoutError
            conn.request(method, parts.path + path, body=body, headers=headers)
            resp = conn.getresponse()
            status, raw = resp.status, resp.read(MAX_CORE_ANSWER + 1)
        except (TimeoutError, OSError, http.client.HTTPException, ValueError):
            if late.is_set():
                return 504, {"success": False, "error": f"{xo} did not answer in time" + maybe}
            return 502, {"success": False, "error": f"{xo} did not answer" + maybe}
        finally:
            timer.cancel()
            if resp is not None:
                resp.close()
            conn.close()
        if late.is_set():  # cut mid-answer: what was read is not the whole answer
            return 504, {"success": False, "error": f"{xo} did not answer in time" + maybe}
        if 300 <= status < 400:
            return 502, {"success": False, "error": f"XO redirected the request ({status}); not followed"}
        if status not in _KNOWN_STATUSES:
            return 502, {"success": False, "error": f"XO answered with status {status}"}
        if len(raw) > MAX_CORE_ANSWER:
            return status, {"success": 200 <= status < 300,
                            "error": f"XO answered {status} with more than {MAX_CORE_ANSWER} bytes, too large "
                                     "to pass on"}
        try:
            payload = json.loads(raw)
        except (ValueError, RecursionError):
            payload = None
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


def _project_failure(e: ClerkError, what: str = "projects") -> _Reply:
    """/me/projects and /me/spaces: Clerk allows a user 10 metadata writes per 10 seconds. Past that
    the caller is told to slow down (429), which is about them, not a 503 that marks the whole server
    degraded."""
    if e.status == 429 and e.seen == 429:  # Clerk's own limit, not this server's budget
        return _Reply(429, f"Too many changes to your {what}; try again in a few seconds", e.seen)
    return _clerk_failure(e)


def _size(private: dict) -> int:
    return len(json.dumps(private, separators=(",", ":"), ensure_ascii=False).encode())


def _rev(entry: dict) -> int:
    rev = entry.get("rev")
    return rev if isinstance(rev, int) and not isinstance(rev, bool) else 0


def _space_projects(entry: dict) -> list:
    projects = entry.get("projects")
    return projects if isinstance(projects, list) else []


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
        self._project_locks: dict[str, threading.Lock] = {}  # one change per user at a time, per instance
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
        return self._verified(headers, group)[0]

    def _verified(self, headers: dict[str, str], group: str) -> tuple[dict, str]:
        """The caller's Clerk API key, and the token itself for a request passed on to XO."""
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
        return key, token

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
        if path == "/config" and method in ("GET", "HEAD"):
            return services_list()
        if path == "/me":
            if method != "GET":
                raise _Reply(405, "Method not allowed")
            return self._me(self._key(headers, "me"))
        if path == "/me/projects":
            if method == "GET":
                return self._projects(self._key(headers, "projects"))
            if method == "POST":
                return self._add_project(headers, body)
            raise _Reply(405, "Method not allowed")
        if path == "/me/spaces":
            if method == "GET":
                return self._spaces(self._key(headers, "spaces"))
            if method == "POST":
                return self._create_space(headers, body)
            raise _Reply(405, "Method not allowed")
        if path == "/me/spaces/connect":
            if method != "POST":
                raise _Reply(405, "Method not allowed")
            return self._connect_space(headers, body)
        if path.startswith("/me/spaces/") and path.endswith("/projects") and path.count("/") == 4:
            if method != "POST":
                raise _Reply(405, "Method not allowed")
            return self._connect_project(urllib.parse.unquote(path.split("/")[3]), headers, body)
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
                "lastName": u.get("last_name"), "imageUrl": u.get("image_url"), "createdAt": u.get("created_at"),
                "email": _primary_email(u),
                # The key's own user only (the subject Clerk verified, never an id from the request).
                # Admin rights come from the key's claim and $XO_ADMIN_KEY_IDS, not from metadata.
                "publicMetadata": _mask_secrets(u.get("public_metadata") or {}),
                "privateMetadata": self._masked_private(u.get("private_metadata"))}
        with self._lock:
            if len(self._me_cache) >= ME_CACHE_PRUNE_AT:  # entries hold metadata: drop the expired ones
                self._me_cache = {s: e for s, e in self._me_cache.items() if now - e[0] < ME_CACHE_SECONDS}
            self._me_cache[subject] = (now, data)
        return data

    def _projects(self, key: dict) -> dict:
        """The `projects` list in the key's own private metadata ([] when there is none)."""
        u = self._user(key["subject"])
        private = u.get("private_metadata")
        projects = private.get("projects") if isinstance(private, dict) else None
        return {"projects": _mask_secrets([] if projects is None else projects)}

    def _user(self, subject: str) -> dict:
        try:
            return self.clerk.user(subject)
        except ClerkError as e:
            if e.status == 404:
                raise _Reply(404, "Account not found") from None
            raise _clerk_failure(e) from None

    def _caller(self, headers: dict[str, str], group: str, route: str) -> tuple[dict, str, dict]:
        """The verified key, its token and the audit fields of a request that changes metadata;
        a refused key is audited too."""
        try:
            key, token = self._verified(headers, group)
        except _Reply as r:
            self.audit(method="POST", route=route, keyId=None, caller=None, outcome=r.status,
                       **({"clerk": r.clerk} if r.clerk is not None else {}))
            raise
        return key, token, {"method": "POST", "route": route, "keyId": key.get("id"), "caller": key["subject"]}

    def _refuse(self, who: dict, status: int, error: str) -> _Reply:
        self.audit(**who, outcome=status)
        return _Reply(status, error)

    def _body(self, body: bytes | None, who: dict, fields: dict[str, bool]) -> dict:
        """A JSON object holding the string fields named (True: required) and nothing else."""
        try:
            sent = json.loads(body or b"")
        except (json.JSONDecodeError, UnicodeDecodeError, RecursionError):
            raise self._refuse(who, 422, "Body is not JSON") from None
        shape = "{" + ", ".join(f'"{k}": "..."' for k in fields) + "}"
        if (not isinstance(sent, dict) or set(sent) - set(fields)
                or any(need and k not in sent for k, need in fields.items())
                or any(not isinstance(sent[k], str) for k in sent if k != "body")):
            raise self._refuse(who, 422, f"Body must be {shape} and nothing else")
        return {k: v.strip() if isinstance(v, str) else v for k, v in sent.items()}

    def _change(self, subject: str, who: dict, field: str, step, what: str, busy: str) -> list:
        """Change one list in the user's own private metadata, the way /me/projects does: read it,
        let `step` say what it should be, and send Clerk a merge that names only `field`, so every
        other key is left as it was. Clerk has no compare-and-swap, so after each write the list is
        read again (after PROJECT_SETTLE) and `step` decides whether the change is there, whether a
        racing write must be undone, or whether to give up.

        step(current list, private metadata, wrote) answers ("done", list) or ("write", list), or
        raises a refusal. The answer is the list, after the user's cached /me is dropped."""
        with self._lock:
            lock = self._project_locks.setdefault(subject, threading.Lock())
        with lock:
            wrote, first, outcome, value = False, None, None, []
            started = time.monotonic()
            for attempt in range(PROJECT_TRIES + 1):
                if wrote:
                    time.sleep(PROJECT_SETTLE)  # let a write racing this one land before reading again
                try:
                    u = self.clerk.user(subject)
                except ClerkError as e:
                    reply = _Reply(404, "Account not found") if e.status == 404 else _project_failure(e, what)
                    self.audit(**who, outcome=reply.status, clerk=e.seen)
                    raise reply from None
                private = u.get("private_metadata")
                private = {} if private is None else private
                if not isinstance(private, dict):
                    raise self._refuse(who, 409, "Your private metadata is not an object; nothing was changed")
                current = private.get(field, [])
                if not isinstance(current, list):
                    raise self._refuse(who, 409, f"Your private metadata `{field}` is not a list; nothing was changed")
                first = private if first is None else first
                outcome, value = step(current, private, wrote)
                if outcome == "done":
                    break
                # No retry write once a slow Clerk has used CLERK_TIMEOUT: the request would outlive the
                # client's and the platform's deadlines. The caller gets the 503 below, never a false 200.
                if attempt == PROJECT_TRIES or (wrote and time.monotonic() - started > CLERK_TIMEOUT):
                    break
                if _size({**private, field: value}) > PRIVATE_BUDGET:
                    raise self._refuse(who, 422, f"That would take your private metadata past {PRIVATE_BUDGET // 1024}KB "
                                                 "(Clerk keeps 8KB, and XO needs the rest); nothing was changed")
                try:
                    # A merge naming only `field`: Clerk leaves every other key as it is.
                    self.clerk.update_metadata(subject, {"privateMetadata": {field: value}})
                except ClerkError as e:
                    reply = _project_failure(e, what) if e.status == 429 else self._clerk_reply(e)
                    self.audit(**who, outcome=reply.status, clerk=e.seen)
                    raise reply from None
                wrote = True
            if outcome != "done":
                raise self._refuse(who, 503, busy)
        with self._lock:
            self._me_cache.pop(subject, None)
        # Anything else that differs was changed by someone else meanwhile, not by this merge.
        others = {k: v for k, v in first.items() if k != field} != {k: v for k, v in private.items() if k != field}
        extra = {"othersChanged": True} if others else {}
        self.audit(**who, outcome=200, count=len(value), **extra)  # never the names or values
        return value

    def _add_project(self, headers: dict[str, str], body: bytes | None) -> dict:
        """Add one name to the key's own private metadata `projects`. The client sends only the
        name; the server reads the list, appends, and writes `projects` and nothing else."""
        key, _, who = self._caller(headers, "projects", "/me/projects")
        name = self._body(body, who, {"name": True})["name"]
        self._project_name(name, who)
        seen: list = []

        def step(projects: list, private: dict, wrote: bool) -> tuple[str, list]:
            present = any(isinstance(p, str) and p.casefold() == name.casefold() for p in projects)
            # No user route removes a project, so anything read earlier and gone now was dropped
            # by a racing write that started from an older list: put it back.
            missing = [p for p in seen if p not in projects and p != name]
            if wrote and present and not missing:
                return "done", projects
            if present and not wrote:
                raise self._refuse(who, 409, f"{name} is already in your projects; nothing was changed")
            wanted = [*projects, *missing, *([] if present else [name])]
            if len(wanted) > MAX_PROJECTS:
                raise self._refuse(who, 422, f"You have {MAX_PROJECTS} or more projects, the most there can be; "
                                             "nothing was changed")
            seen.extend(p for p in [*projects, name] if p not in seen)
            return "write", wanted

        projects = self._change(key["subject"], who, "projects", step, "projects",
                                "Your projects changed while this add ran; run xo projects to see whether it is "
                                "there before trying again")
        return {"projects": _mask_secrets(projects)}

    def _project_name(self, name: str, who: dict) -> None:
        if not names.project_ok(name):
            raise self._refuse(who, 422, names.PROJECT_RULE[0].upper() + names.PROJECT_RULE[1:])
        if _SECRET_VALUE.search(name):  # it would show masked in xo projects and xo me
            raise self._refuse(who, 422, "That name looks like a credential (sk-, ghp_, ak_ and the like); "
                                         "choose another")

    def _space_name(self, name: str, who: dict) -> None:
        if not names.space_ok(name):
            raise self._refuse(who, 422, names.SPACE_RULE[0].upper() + names.SPACE_RULE[1:])
        if _SECRET_VALUE.search(name):
            raise self._refuse(who, 422, "That name looks like a credential (sk-, ghp_, ak_ and the like); "
                                         "choose another")

    def _spaces(self, key: dict) -> dict:
        """The `spaces` list in the key's own private metadata ([] when there is none)."""
        u = self._user(key["subject"])
        private = u.get("private_metadata")
        spaces = private.get("spaces") if isinstance(private, dict) else None
        return {"spaces": _shown_spaces([] if spaces is None else spaces)}

    def _change_space(self, subject: str, who: dict, name: str, make) -> dict:
        """Write one space, by name (ignoring case), and leave every other space as it was.

        make(current entry or None, private metadata) answers the entry to write, or refuses; it is
        called once, on the first read. Writing `spaces` replaces the whole list, so a write racing
        this one can drop or revert a space: each other space is put back at the highest rev read,
        and this one is written again if it is missing or back as first read. Anything else means
        someone changed it meanwhile: 409. The answer is the entry written."""
        target = name.casefold()
        best: dict[str, dict] = {}
        written: list[dict] = []
        original: list = []  # the space as first read (None when it was not there)

        def step(spaces: list, private: dict, wrote: bool) -> tuple[str, list]:
            self._readable_spaces(spaces, who)
            by = {e["name"].casefold(): e for e in spaces}
            for k, e in by.items():
                if k != target and (k not in best or _rev(e) >= _rev(best[k])):
                    best[k] = e
            if not written:
                original.append(by.get(target))
                written.append(make(by.get(target), private))
            elif by.get(target) not in (None, original[0], written[0]):  # gone or as it was: ours was dropped
                raise self._refuse(who, 409, f"Space {name} changed while this ran; run xo list spaces and try again")
            if wrote and by.get(target) == written[0] and all(by.get(k) == e for k, e in best.items()):
                return "done", spaces
            out, placed = [], set()
            for e in spaces:
                k = e["name"].casefold()
                if k not in placed:
                    out.append(written[0] if k == target else best[k])
                    placed.add(k)
            out += [e for k, e in best.items() if k not in placed]
            if target not in placed:
                out.append(written[0])
            if len(out) > MAX_SPACES:
                raise self._refuse(who, 422, f"You have {MAX_SPACES} spaces, the most there can be; nothing was changed")
            return "write", out

        self._change(subject, who, "spaces", step, "spaces",
                     f"Your spaces changed while this ran; run xo list spaces to see whether {name} is as you "
                     "wanted before trying again")
        return written[0]

    def _readable_spaces(self, spaces: Any, who: dict, nothing: str = "changed") -> None:
        """Refuse a `spaces` xo did not write and could not write back whole: not a list, an entry
        that is not an object with a name, or two names that differ only in case."""
        if not isinstance(spaces, list):
            raise self._refuse(who, 409, f"Your private metadata `spaces` is not a list; nothing was {nothing}")
        if any(not isinstance(e, dict) or not isinstance(e.get("name"), str) for e in spaces) or \
                len({e["name"].casefold() for e in spaces}) != len(spaces):
            raise self._refuse(who, 409, "Your private metadata `spaces` holds an entry xo cannot read; "
                                         f"nothing was {nothing}")

    def _create_space(self, headers: dict[str, str], body: bytes | None) -> dict:
        """Create a space through XO with the caller's own key, then record it in `spaces`."""
        key, token, who = self._caller(headers, "spaces", "/me/spaces")
        sent = self._body(body, who, {"kind": True, "name": True, "body": False})
        kind, name, create = sent["kind"], sent["name"], sent.get("body", {})
        if kind not in SPACE_KINDS:
            raise self._refuse(who, 422, "kind must be one of " + ", ".join(SPACE_KINDS))
        self._space_name(name, who)
        if not isinstance(create, dict):
            raise self._refuse(who, 422, "body must be a JSON object: the create's own body")
        # Every refusal recording could make is checked before XO creates anything (and again when
        # recording), so a refusal never leaves a space on XO that xo has no record of.
        private = self._user(key["subject"]).get("private_metadata")
        private = {} if private is None else private
        if not isinstance(private, dict):
            raise self._refuse(who, 409, "Your private metadata is not an object; nothing was created")
        spaces = private.get("spaces", [])
        self._readable_spaces(spaces, who, "created")
        if any(e["name"].casefold() == name.casefold() for e in spaces):
            raise self._refuse(who, 409, f"You already have a space named {name}; nothing was created")
        if len(spaces) >= MAX_SPACES:
            raise self._refuse(who, 422, f"You have {MAX_SPACES} spaces, the most there can be; nothing was created")
        widest = {"name": name, "kind": kind, "id": "x" * names.SPACE_ID_MAX, "rev": 1, "projects": []}
        if _size({**private, "spaces": [*spaces, widest]}) > PRIVATE_BUDGET:
            raise self._refuse(who, 422, f"Recording it would take your private metadata past "
                                         f"{PRIVATE_BUDGET // 1024}KB (Clerk keeps 8KB, and XO needs the rest); "
                                         "nothing was created")
        status, answer = self.core.forward("POST", f"/{kind}", token, json.dumps(create).encode())
        if not 200 <= status < 300:
            self.audit(**who, outcome=status, xo=status)
            raise _Forwarded(status, answer)
        made = answer.get("data") if isinstance(answer.get("data"), dict) else {}
        space_id = made.get("id")
        if not isinstance(space_id, str) or not names.SPACE_ID.fullmatch(space_id) or _SECRET_VALUE.search(space_id):
            raise self._refuse(who, 502, "XO created the space but its answer had no id xo can use; nothing was "
                                         "recorded")

        def make(current: dict | None, private: dict) -> dict:
            if current is not None:
                raise self._refuse(who, 409, f"A space named {name} was recorded meanwhile")
            return {"name": name, "kind": kind, "id": space_id, "rev": 1, "projects": []}

        try:
            entry = self._change_space(key["subject"], who, name, make)
        except _Reply as r:
            # A race, or a Clerk that is busy, failing or unreachable, can pass; the refusals checked
            # above cannot, so no hint for them.
            hint = (f". If xo list spaces does not show it, run xo connect space:{name} --id {space_id} to record it"
                    if r.status in (409, 429, 502, 503) else "")
            raise _Reply(r.status, f"XO created the space (id {space_id}) but xo could not record it: {r.error}{hint}",
                         r.clerk) from None
        return {"space": _shown_space(entry), "xo": _mask_secrets(made)}

    def _connect_space(self, headers: dict[str, str], body: bytes | None) -> dict:
        """A new space entry in the caller's metadata, with no projects yet. With an id it stands for
        an XO space the caller already has, once XO confirms the caller's key can read it; without
        one it is an entry only, nothing on XO."""
        key, token, who = self._caller(headers, "spaces", "/me/spaces/connect")
        sent = self._body(body, who, {"name": True, "id": False})
        space_id, name = sent.get("id"), sent["name"]
        self._space_name(name, who)
        if space_id is not None:
            if not names.SPACE_ID.fullmatch(space_id) or _SECRET_VALUE.search(space_id):
                raise self._refuse(who, 422, f"id must be an XO space id: 1 to {names.SPACE_ID_MAX} letters, digits, _ or -")
            status, answer = self.core.forward("GET", f"/spaces/{space_id}", token, None)
            if status in (403, 404):
                raise self._refuse(who, 404, "XO has no space with that id that your key can read; nothing was "
                                             "recorded")
            if not 200 <= status < 300:
                self.audit(**who, outcome=status, xo=status)
                raise _Forwarded(status, answer)

        def make(current: dict | None, private: dict) -> dict:
            spaces = private.get("spaces", [])
            if space_id is not None and any(e is not current and e.get("id") == space_id for e in spaces):
                raise self._refuse(who, 409, "That space is already recorded under another name; nothing was changed")
            if current is None:
                return {"name": name, **({"id": space_id} if space_id is not None else {}), "rev": 1, "projects": []}
            # An entry made without an id can be given one, once; nothing else about it changes.
            if space_id is None or current.get("id") is not None:
                raise self._refuse(who, 409, f"You already have a space named {name}; nothing was changed")
            return {**current, "id": space_id, "rev": _rev(current) + 1}

        return {"space": _shown_space(self._change_space(key["subject"], who, name, make))}

    def _connect_project(self, name: str, headers: dict[str, str], body: bytes | None) -> dict:
        """Set one project's ref in one of the caller's spaces: added, or replaced if it is there."""
        key, _, who = self._caller(headers, "spaces", "/me/spaces/{name}/projects")
        sent = self._body(body, who, {"project": True, "ref": False})
        project, ref = sent["project"], sent.get("ref") or DEFAULT_REF
        self._space_name(name, who)
        self._project_name(project, who)
        if not names.ref_ok(ref):
            raise self._refuse(who, 422, names.REF_RULE[0].upper() + names.REF_RULE[1:])
        if _secret_ref(ref):
            raise self._refuse(who, 422, "That ref looks like a credential (sk-, ghp_, ak_ and the like); "
                                         "nothing was changed")

        def make(current: dict | None, private: dict) -> dict:
            if current is None:
                raise self._refuse(who, 404, f"You have no space named {name}")
            if any(not isinstance(p, dict) or len(p) != 1 for p in _space_projects(current)):
                raise self._refuse(who, 409, f"Space {name} holds a project entry xo cannot read; nothing was changed")
            projects = private.get("projects", [])
            if not isinstance(projects, list) or project not in projects:
                raise self._refuse(who, 404, f"{project} is not in your projects; add it with xo create project first")
            entries = [p for p in _space_projects(current) if not (isinstance(p, dict) and project in p)]
            if len(entries) >= MAX_SPACE_PROJECTS:
                raise self._refuse(who, 422, f"Space {name} has {MAX_SPACE_PROJECTS} projects, the most there can be; "
                                             "nothing was changed")
            # One entry per project: connecting it again replaces its ref, in place.
            at = next((i for i, p in enumerate(_space_projects(current)) if isinstance(p, dict) and project in p),
                      len(entries))
            entries.insert(at, {project: ref})
            return {**current, "projects": entries, "rev": _rev(current) + 1}

        return {"space": _shown_space(self._change_space(key["subject"], who, name, make))}

    def _masked_private(self, private: Any) -> Any:
        shown = self._shown_private(private)
        masked = _mask_secrets(shown)
        if isinstance(shown, dict) and "spaces" in shown:
            masked["spaces"] = _shown_spaces(shown["spaces"])
        return masked

    def _shown_private(self, private: Any) -> Any:
        """The private metadata /me shows its user: all of it, or only $XO_ME_PRIVATE_KEYS."""
        private = private or {}
        if self.cfg.me_private is None:
            return private
        if not isinstance(private, dict):
            return {}
        return {k: v for k, v in private.items() if k in self.cfg.me_private}

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
    if path in ("/healthz", "/me", "/me/projects", "/me/spaces", "/me/spaces/connect", "/config"):
        return path
    if path.startswith("/me/spaces/") and path.endswith("/projects") and path.count("/") == 4:
        return "/me/spaces/{name}/projects"
    if path.startswith("/user-metadata/"):
        return "/user-metadata/{userId}"
    if path.startswith(CORE_PREFIX + "/"):
        sub = path[len(CORE_PREFIX):]
        return next((CORE_PREFIX + (name or sub) for _, pattern, name in CORE_ROUTES if pattern.fullmatch(sub)),
                    "other")
    return "other"


def services_list() -> dict:
    """What /config serves: the services list shipped with xo, with xo's address set to where this
    server really passes /api/core/v1 on to. No key, no Clerk call; never a key or token in it."""
    data = json.loads(json.dumps(services.SHIPPED))
    core = (os.environ.get(CORE_ENV) or "").strip().rstrip("/")
    if core:
        platform = core[:-len(CORE_PREFIX)] if core.endswith(CORE_PREFIX) else core
        try:
            data["xo"]["url"] = services.listed_url("xo", platform)
        except (XoError, ValueError):  # ValueError: a malformed bracketed host
            data["xo"]["url"] = None  # not an address the list can name: say so, keep coder
    return data


def is_config(method: str, path: str) -> bool:
    return method in ("GET", "HEAD") and (urllib.parse.urlsplit(path).path.rstrip("/") or "/") == "/config"


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
        if is_config(environ.get("REQUEST_METHOD", "GET"), environ.get("PATH_INFO") or "/"):
            # Served before the app is built, so a server missing its Clerk secret still lists them.
            reply = _wsgi_reply(start_response, 200, {"success": True, "data": services_list()})
            return [] if environ.get("REQUEST_METHOD") == "HEAD" else reply
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
          f"/api/core/v1 passed on to {app.cfg.core_url}; "
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
