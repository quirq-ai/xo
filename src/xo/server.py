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
    POST  /me/projects/connect      any user key: {"name", "repo"}: give a project with no repo its
                                    GitHub OWNER/REPO, which becomes its name (`projects` only)
    POST  /me/spaces                any user key: {"kind", "name", "body"}: create the space through
                                    XO with the caller's key, then record it in `spaces`
    POST  /me/spaces/connect        any user key: {"name", "id"?}: a new space entry; with an id, only
                                    for an XO space the key can read
    POST  /me/spaces/{name}/projects  any user key: {"project", "ref"}: set one project's ref in a
                                    space; the server writes only `spaces`, nothing else
    PUT   /me/spaces/{name}         any user key: {"rev", "projects", "base"}: replace a space's projects
                                    and refs, only if it is still at that rev holding base (else 409)
    POST  /me/share                 any user key: {"item": "space:S"|"project:OWNER/REPO", "to": USERNAME}:
                                    an invite in that user's `shared` (the only write to another
                                    user's metadata: their `shared` key, the caller's own invite)
    GET   /me/shared                any user key: the invites in its own `shared`, each with an id
    POST  /me/shared/accept         any user key: {"id", "as"?} for a space, {"id", "space", "ref"?}
                                    for a project: in one write, a shared space into its spaces, a
                                    shared project into the space it names, their projects into
                                    its projects, and the invite off
    POST  /me/shared/reject         any user key: {"id"}: the invite off; the sender can share again
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
CORE_ROUTES = [("POST", re.compile("/(" + "|".join(names.SPACE_KINDS) + ")"), None),
               ("GET", re.compile(r"/spaces/[A-Za-z0-9_-]{1,64}"), "/spaces/{spaceId}")]
# What a Clerk API key looks like; anything else is refused without asking Clerk (the SDK checks
# the ak_ prefix the same way). The characters are the CLI's (client.KEY_CHARS): letters, digits and
# _ . - plus base64's + / = (Clerk's own test key has / and =).
API_KEY = re.compile(r"ak_[A-Za-z0-9_.+/=-]{1,200}")
BUCKETS = {"publicMetadata": "public_metadata", "privateMetadata": "private_metadata"}
MAX_BODY = 32 * 1024  # two 8KB buckets with room for JSON overhead
# Requests per minute per key: /me, then /user-metadata. Clerk allows 1000 Backend API calls per
# 10 seconds for the whole instance and 10 metadata writes per 10 seconds per user.
LIMITS = {"me": 60, "projects": 30, "spaces": 30, "share": 10, "shared": 30, "user-metadata": 20, "core": 30}
# /me/projects: a write a normal key can make to its own metadata. A name is letters, digits, spaces and _ . -,
# starting with a letter or digit; the list stays well under Clerk's 8KB private metadata limit.
MAX_PROJECTS = 30
MAX_SPACES = 10
MAX_SPACE_PROJECTS = 10
# The most a write may leave in a user's private metadata. Clerk keeps 8KB, shared with XO's own
# keys, so xo's lists stop here and leave XO at least 2KB to grow into.
PRIVATE_BUDGET = 6 * 1024
DEFAULT_REF = "main"
# Invites in a user's `shared`: the most waiting at once, and the most from any one sender.
MAX_INVITES = 20
MAX_INVITES_FROM = 5
# Invites count toward the recipient's PRIVATE_BUDGET like everything else, so the whole stays
# within it; all of `shared` is at most SHARED_BUDGET bytes and each invite at most INVITE_BUDGET,
# so nobody else can take more than that of anyone's room. Accept and reject give it back.
SHARED_BUDGET = 1536
INVITE_BUDGET = 1024
INVITE_ID_LEN = 16
RESHARE_WAIT = 60  # seconds before a sender may replace their invite with a changed one
# Seconds every POST /me/share takes at least, success or not, so its timing does not say whether
# the username exists or what stopped it.
SHARE_FLOOR = 1.5
USERNAME = re.compile(r"[A-Za-z0-9_-]{1,64}")
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
_CLERK_ROUTES = ("/me", "/me/projects", "/me/projects/connect", "/me/spaces", "/me/spaces/connect", "/me/spaces/{name}",
                 "/me/spaces/{name}/projects", "/me/share", "/me/shared", "/me/shared/accept", "/me/shared/reject",
                 "/user-metadata/{userId}")
# The creates `xo create space NAME KIND` may record: the core routes that make a space.
SPACE_KINDS = names.SPACE_KINDS
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

    def _call(self, method: str, path: str, body: Any = None, shape: type = dict) -> Any:
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
        if not isinstance(out, shape):
            what = "not an object" if shape is dict else "not a list"
            raise ClerkError(502, what, seen=what)
        return out

    def verify_key(self, secret: str) -> dict:
        return self._call("POST", "/api_keys/verify", {"secret": secret})

    def user(self, user_id: str) -> dict:
        return self._call("GET", "/users/" + urllib.parse.quote(user_id, safe=""))

    def users_named(self, username: str) -> list:
        """The users whose username is exactly this one (Clerk's list, filtered by username)."""
        return self._call("GET", "/users?" + urllib.parse.urlencode({"username": username, "limit": 2}), shape=list)

    def users_by_id(self, user_ids: list[str]) -> list:
        """The users with these ids, in one call (those deleted are missing)."""
        query = urllib.parse.urlencode([("user_id", i) for i in user_ids] + [("limit", len(user_ids))])
        return self._call("GET", "/users?" + query, shape=list)

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


def _invite_ok(entry: Any) -> bool:
    """An invite as xo writes it: who sent it, what it is, and for a space its GitHub projects."""
    if not (isinstance(entry, dict) and isinstance(entry.get("from"), str) and isinstance(entry.get("fromName"), str)
            and isinstance(entry.get("item"), str) and isinstance(entry.get("at"), str)):
        return False
    kind, _, name = entry["item"].partition(":")
    if kind == "project":
        return names.project_ok(name) and names.is_github(name) and "projects" not in entry
    return kind == "space" and names.space_ok(name) and isinstance(entry.get("projects"), list)


def _old_mark(entry: Any) -> bool:
    """A reject mark an earlier build of this branch wrote to `shared`. Readers pass over it and
    the next write of `shared` drops it."""
    return isinstance(entry, dict) and set(entry) == {"block", "until"}


def _handle(entry: dict) -> str:
    """The id `xo list shared` shows for an invite: its sender's user id and exact content, `at`
    included, so a re-share or another sender's look-alike is never taken for the one listed."""
    return hashlib.sha256(json.dumps(entry, sort_keys=True, separators=(",", ":")).encode()).hexdigest()[:INVITE_ID_LEN]


def _shown_invite(e: dict, current: str | None = None, gone: bool = False, unchecked: bool = False) -> dict:
    """An invite as its user sees it: never the sender's user id. `from` is the sender's username
    now, when it was looked up; `sharedAs` is the one they had when they shared, if different;
    `senderUnchecked` says the lookup failed, so `from` is only the name they shared as."""
    name = current or e["fromName"]
    return {"id": _handle(e), "from": name,
            **({"sharedAs": e["fromName"]} if name != e["fromName"] else {}),
            **({"senderGone": True} if gone else {}),
            **({"senderUnchecked": True} if unchecked else {}),
            "item": e["item"], "at": e["at"],
            **({"projects": _mask_values(e["projects"])} if "projects" in e else {})}


def _shown_invites(invites: Any) -> Any:
    """`shared` as /me shows it: the invites by the usernames they were sent under."""
    if not isinstance(invites, list):
        return _mask_secrets(invites)
    return [_shown_invite(e) if _invite_ok(e) else "(an entry xo cannot read)" for e in invites if not _old_mark(e)]


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


def _invites_note(private: dict) -> str:
    """For a 6KB refusal: what the invites waiting take, which xo reject always frees."""
    shared = private.get("shared")
    if not isinstance(shared, list) or not shared:
        return ""
    return f". Invites waiting take {_size(shared)} bytes; xo reject ID frees them (a reject always works)"


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
        if path == "/me/projects/connect":
            if method != "POST":
                raise _Reply(405, "Method not allowed")
            return self._connect_repo(headers, body)
        if path == "/me/spaces":
            if method == "GET":
                return self._spaces(self._key(headers, "spaces"))
            if method == "POST":
                return self._create_space(headers, body)
            raise _Reply(405, "Method not allowed")
        if path == "/me/spaces/connect":
            if method == "PUT":  # a space named connect
                return self._update_space("connect", headers, body)
            if method != "POST":
                raise _Reply(405, "Method not allowed")
            return self._connect_space(headers, body)
        if path.startswith("/me/spaces/") and path.count("/") == 3 and path != "/me/spaces/connect":
            if method != "PUT":
                raise _Reply(405, "Method not allowed")
            return self._update_space(urllib.parse.unquote(path.split("/")[3]), headers, body)
        if path.startswith("/me/spaces/") and path.endswith("/projects") and path.count("/") == 4:
            if method != "POST":
                raise _Reply(405, "Method not allowed")
            return self._connect_project(urllib.parse.unquote(path.split("/")[3]), headers, body)
        if path == "/me/share":
            if method != "POST":
                raise _Reply(405, "Method not allowed")
            return self._share(headers, body)
        if path == "/me/shared":
            if method != "GET":
                raise _Reply(405, "Method not allowed")
            return self._shared(self._key(headers, "shared"))
        if path in ("/me/shared/accept", "/me/shared/reject"):
            if method != "POST":
                raise _Reply(405, "Method not allowed")
            return self._answer_invite(path.rsplit("/", 1)[1], headers, body)
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

    def _caller(self, headers: dict[str, str], group: str, route: str, method: str = "POST") -> tuple[dict, str, dict]:
        """The verified key, its token and the audit fields of a request that changes metadata;
        a refused key is audited too."""
        try:
            key, token = self._verified(headers, group)
        except _Reply as r:
            self.audit(method=method, route=route, keyId=None, caller=None, outcome=r.status,
                       **({"clerk": r.clerk} if r.clerk is not None else {}))
            raise
        return key, token, {"method": method, "route": route, "keyId": key.get("id"), "caller": key["subject"]}

    def _refuse(self, who: dict, status: int, error: str) -> _Reply:
        self.audit(**who, outcome=status)
        return _Reply(status, error)

    def _body(self, body: bytes | None, who: dict, fields: dict[str, bool]) -> dict:
        """A JSON object holding the string fields named (True: required) and nothing else."""
        try:
            sent = json.loads(body or b"")
        except (ValueError, RecursionError):  # ValueError: bad JSON, bad UTF-8, an int past 4300 digits
            raise self._refuse(who, 422, "Body is not JSON") from None
        shape = "{" + ", ".join(f'"{k}": "..."' for k in fields) + "}"
        if (not isinstance(sent, dict) or set(sent) - set(fields)
                or any(need and k not in sent for k, need in fields.items())
                or any(not isinstance(sent[k], str) for k in sent if k != "body")):
            raise self._refuse(who, 422, f"Body must be {shape} and nothing else")
        return {k: v.strip() if isinstance(v, str) else v for k, v in sent.items()}

    def _change(self, subject: str, who: dict, field: str, step, what: str, busy, settled=None) -> list:
        """Change one list in the user's own private metadata, the way /me/projects does: read it,
        let `step` say what it should be, and send Clerk a merge that names only `field`, so every
        other key is left as it was. Clerk has no compare-and-swap, so after each write the list is
        read again (after PROJECT_SETTLE) and `step` decides whether the change is there, whether a
        racing write must be undone, or whether to give up.

        step(current list, private metadata, wrote) answers ("done", list) or ("write", list), or
        raises a refusal. The answer is the list, after the user's cached /me is dropped."""
        def keyed(private: dict, wrote: bool) -> tuple[str, dict]:
            current = private.get(field, [])
            if not isinstance(current, list):
                raise self._refuse(who, 409, f"Your private metadata `{field}` is not a list; nothing was changed")
            outcome, value = step(current, private, wrote)
            return outcome, {field: value}

        return self._change_keys(subject, who, keyed, what, busy, settled)[field]

    def _change_keys(self, subject: str, who: dict, step, what: str, busy, settled=None) -> dict:
        """_change for one or more top-level keys at once: step(private metadata, wrote) answers
        ("done" | "write", {key: value}), and the merge sent to Clerk names only those keys, in one
        write, held to PRIVATE_BUDGET as a whole. A write of `shared` alone is not: a share checks
        its own room, and a reject only makes `shared` smaller.

        busy is the 503 message, or a function giving it. settled(), when given, runs once the
        change is done and before its 200 audit line, and may refuse instead."""
        with self._lock:
            lock = self._project_locks.setdefault(subject, threading.Lock())
        with lock:
            wrote, first, outcome, value = False, None, None, {}
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
                first = private if first is None else first
                outcome, value = step(private, wrote)
                if outcome == "done":
                    break
                # No retry write once a slow Clerk has used CLERK_TIMEOUT: the request would outlive the
                # client's and the platform's deadlines. The caller gets the 503 below, never a false 200.
                if attempt == PROJECT_TRIES or (wrote and time.monotonic() - started > CLERK_TIMEOUT):
                    break
                if set(value) != {"shared"} and _size({**private, **value}) > PRIVATE_BUDGET:
                    raise self._refuse(who, 422, f"That would take your private metadata past {PRIVATE_BUDGET // 1024}KB "
                                                 "(Clerk keeps 8KB, and XO needs the rest); nothing was changed"
                                                 + _invites_note(private))
                try:
                    # A merge naming only these keys: Clerk leaves every other key as it is.
                    self.clerk.update_metadata(subject, {"privateMetadata": value})
                except ClerkError as e:
                    reply = _project_failure(e, what) if e.status == 429 else self._clerk_reply(e)
                    self.audit(**who, outcome=reply.status, clerk=e.seen)
                    raise reply from None
                wrote = True
            if outcome != "done":
                raise self._refuse(who, 503, busy() if callable(busy) else busy)
        with self._lock:
            self._me_cache.pop(subject, None)
        if settled:
            settled()
        # Anything else that differs was changed by someone else meanwhile, not by this merge.
        others = {k: v for k, v in first.items() if k not in value} != \
            {k: v for k, v in private.items() if k not in value}
        extra = {"othersChanged": True} if others else {}
        counts = [len(v) for v in value.values() if isinstance(v, list)]
        self.audit(**who, outcome=200, count=counts[0] if len(counts) == 1 else counts, **extra)  # never names or values
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
            # Anything read earlier and gone now was dropped by a racing write that started from an
            # older list: put it back. The one route that removes a name, `xo connect --repo`
            # renaming it, can race this too and see the old name come back (docs/server.md, Races).
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

    def _connect_repo(self, headers: dict[str, str], body: bytes | None) -> dict:
        """Give a project that has no repo its GitHub OWNER/REPO. A project's name is its repo, so
        the name in `projects` becomes OWNER/REPO, in place; the server writes `projects` and nothing
        else. A project already in a space is refused, since its spaces would still name it the old
        way."""
        key, _, who = self._caller(headers, "projects", "/me/projects/connect")
        sent = self._body(body, who, {"name": True, "repo": True})
        name, repo = sent["name"], sent["repo"]
        self._project_name(name, who)
        self._project_name(repo, who)
        if names.is_github(name):
            raise self._refuse(who, 422, f"{name} already has a repo: a project's name is its OWNER/REPO")
        if not names.is_github(repo):
            raise self._refuse(who, 422, "repo must be a GitHub OWNER/REPO")
        seen: list = []
        undone: list = []  # the space a racing write put the project in, after this rename landed

        def same(p: Any, q: str) -> bool:
            return isinstance(p, str) and p.casefold() == q.casefold()

        def naming(spaces: list, project: str) -> list[str]:
            return sorted(e["name"] for e in spaces
                          if any(isinstance(x, dict) and any(same(k, project) for k in x) for x in _space_projects(e)))

        def back(projects: list, keep_repo: bool, missing: list) -> list:
            """The rename undone: NAME listed again, in REPO's place unless a space names REPO too
            (then both stay, so no space names a project missing from `projects`)."""
            out: list = []
            for p in projects:
                q = name if same(p, repo) and not keep_repo else p
                if not ((same(q, name) or same(q, repo)) and any(same(w, q) for w in out)):
                    out.append(q)
            out += [] if any(same(w, name) for w in out) else [name]
            out += [repo] if keep_repo and not any(same(w, repo) for w in out) else []
            return out + missing

        def step(projects: list, private: dict, wrote: bool) -> tuple[str, list]:
            has_name = any(same(p, name) for p in projects)
            has_repo = any(same(p, repo) for p in projects)
            spaces = private.get("spaces", [])
            # Only the first read can say nothing was changed: by a re-read, the rename may be there.
            self._readable_spaces(spaces, who, tail=None if not wrote else
                                  f"run xo projects to see whether {name} is now {repo}")
            # Checked on every read: a racing `xo connect project:NAME space:S` may land at any time.
            held = naming(spaces, name)
            # Only this route removes a name from `projects` (renaming it), so anything else read
            # earlier and gone now was dropped by a racing write that started from an older list:
            # put it back.
            missing = [p for p in seen if p not in projects and not same(p, name) and not same(p, repo)]
            if held and wrote and not undone:
                undone[:] = held
            if undone:
                # A space came to name the project after the rename landed: the rename is put back
                # so that space still names a project in `projects`.
                keep_repo = bool(naming(spaces, repo))
                if has_name and has_repo == keep_repo and not missing:
                    return "done", projects
                return "write", back(projects, keep_repo, missing)
            if held:
                # TODO(suraj): rename the project inside its spaces too (one merge naming `projects`
                # and `spaces`), so a project already in a space can get its repo.
                raise self._refuse(who, 409, f"{name} is in space {held[0]}, which would still name it {name}; "
                                             "nothing was changed")
            if wrote and has_repo and not has_name and not missing and \
                    sum(same(p, repo) for p in projects) == 1:
                return "done", projects
            if not wrote:
                if not has_name:
                    raise self._refuse(who, 404, f"{name} is not in your projects; nothing was changed")
                if has_repo:
                    raise self._refuse(who, 409, f"{repo} is already in your projects; nothing was changed")
            # NAME becomes REPO in place; REPO is listed once, whatever a racing write added.
            wanted: list = []
            for p in projects:
                q = repo if same(p, name) else p
                if not (same(q, repo) and any(same(w, repo) for w in wanted)):
                    wanted.append(q)
            if not any(same(p, repo) for p in wanted):
                wanted.append(repo)
            wanted += missing
            seen.extend(p for p in projects if p not in seen and not same(p, name))
            return "write", wanted

        def busy() -> str:
            if undone:
                return (f"{name} was put in space {undone[0]} while this ran, and putting its name back did not "
                        f"settle; run xo projects to see whether {name} or {repo} is listed")
            return (f"Your projects changed while this ran; run xo projects to see whether {repo} is there before "
                    "trying again")

        def settled() -> None:
            # One audit line per request: the put-back is a 409, never a 200 followed by a 409.
            if undone:
                raise self._refuse(who, 409, f"{name} was put in space {undone[0]} while this ran, so it keeps "
                                             f"its name and was not renamed {repo}")

        projects = self._change(key["subject"], who, "projects", step, "projects", busy, settled)
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

    def _readable_spaces(self, spaces: Any, who: dict, nothing: str = "changed", tail: str | None = None) -> None:
        """Refuse a `spaces` xo did not write and could not write back whole: not a list, an entry
        that is not an object with a name, or two names that differ only in case. The answer ends
        "nothing was {nothing}", or `tail` when one is given."""
        tail = tail or f"nothing was {nothing}"
        if not isinstance(spaces, list):
            raise self._refuse(who, 409, f"Your private metadata `spaces` is not a list; {tail}")
        if any(not isinstance(e, dict) or not isinstance(e.get("name"), str) for e in spaces) or \
                len({e["name"].casefold() for e in spaces}) != len(spaces):
            raise self._refuse(who, 409, f"Your private metadata `spaces` holds an entry xo cannot read; {tail}")

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
                                         "nothing was created" + _invites_note(private))
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

    def _update_space(self, name: str, headers: dict[str, str], body: bytes | None) -> dict:
        """Replace a space's projects and refs with the caller's, if nobody changed the space since
        the caller last read it: `rev` must be the space's rev now, and `base`, the projects the
        caller's change starts from, must be its projects now (two writes can both get 200 at one rev,
        so the rev alone cannot tell). Each project must be in the caller's `projects`; the answer is
        the space with its rev one higher."""
        key, _, who = self._caller(headers, "spaces", "/me/spaces/{name}", method="PUT")
        try:
            sent = json.loads(body or b"")
        except (ValueError, RecursionError):  # ValueError: bad JSON, bad UTF-8, an int past 4300 digits
            raise self._refuse(who, 422, "Body is not JSON") from None
        rev, entries, base = ((sent.get("rev"), sent.get("projects"), sent.get("base")) if isinstance(sent, dict)
                              else (None, None, None))
        if (not isinstance(sent, dict) or set(sent) != {"rev", "projects", "base"} or not isinstance(rev, int)
                or isinstance(rev, bool) or not isinstance(entries, list) or not isinstance(base, list)):
            raise self._refuse(who, 422, 'Body must be {"rev": N, "projects": [{"PROJECT": "REF"}, ...], "base": '
                                         '[the projects as you read them]} and nothing else')
        self._space_name(name, who)
        wanted, seen = [], set()
        for e in entries:
            if not (isinstance(e, dict) and len(e) == 1):
                raise self._refuse(who, 422, 'Each project is {"PROJECT": "REF"}')
            (project, ref), = e.items()
            if not names.project_ok(project) or _SECRET_VALUE.search(project):
                raise self._refuse(who, 422, names.PROJECT_RULE[0].upper() + names.PROJECT_RULE[1:])
            if not names.ref_ok(ref):
                raise self._refuse(who, 422, names.REF_RULE[0].upper() + names.REF_RULE[1:])
            if _secret_ref(ref):
                raise self._refuse(who, 422, "That ref looks like a credential (sk-, ghp_, ak_ and the like); "
                                             "nothing was changed")
            if project in seen:
                raise self._refuse(who, 422, f"{project} is listed twice; a space holds each project once")
            seen.add(project)
            wanted.append({project: ref})
        if len(wanted) > MAX_SPACE_PROJECTS:
            raise self._refuse(who, 422, f"A space holds at most {MAX_SPACE_PROJECTS} projects; nothing was changed")

        def make(current: dict | None, private: dict) -> dict:
            if current is None:
                raise self._refuse(who, 404, f"You have no space named {name}")
            if _rev(current) != rev:
                raise self._refuse(who, 409, f"Space {name} is at rev {_rev(current)}, not your rev {rev}: it changed "
                                             f"since you read it; run xo sync space:{name} first")
            if _space_projects(current) != base:
                raise self._refuse(who, 409, f"Space {name} changed since you read it; run xo sync space:{name} first")
            projects = private.get("projects", [])
            missing = [p for p in seen if not isinstance(projects, list) or p not in projects]
            if missing:
                raise self._refuse(who, 404, f"{missing[0]} is not in your projects; add it with xo create project "
                                             "first")
            if wanted == base:  # nothing changed: no new rev, which would send everyone else to xo sync
                return current
            return {**current, "projects": wanted, "rev": rev + 1}

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

    def _share(self, headers: dict[str, str], body: bytes | None) -> dict:
        """Put an invite in another user's `shared`: a copy of one of the caller's spaces (its name
        and GitHub projects, nothing else) or one of the caller's GitHub projects. The server writes
        only the recipient's `shared`, and only adds or replaces the caller's own invite there.

        What concerns the caller (a bad item, one they do not have, one too big) gets its own answer.
        Everything about the recipient (no such username, too many invites, no room, a list xo
        cannot read) gets the same 404, and once the key is verified every answer
        takes SHARE_FLOOR seconds at least, so neither says more than whether the share was made."""
        key, _, who = self._caller(headers, "share", "/me/share")
        started = time.monotonic()
        try:
            return self._share_now(key, who, body)
        finally:
            time.sleep(max(0.0, started + SHARE_FLOOR - time.monotonic()))

    def _share_now(self, key: dict, who: dict, body: bytes | None) -> dict:
        sent = self._body(body, who, {"item": True, "to": True})
        item, to = sent["item"], sent["to"]
        kind, _, name = item.partition(":")
        if kind == "space":
            self._space_name(name, who)
        elif kind == "project":
            self._project_name(name, who)
            if not names.is_github(name):
                raise self._refuse(who, 422, f"{name} has no GitHub remote, so nobody else could install it; only "
                                             "an OWNER/REPO project can be shared")
        else:
            raise self._refuse(who, 422, "item must be space:NAME or project:OWNER/REPO")
        me = self._user(key["subject"])
        sender = me.get("username")
        if not isinstance(sender, str) or not USERNAME.fullmatch(sender):
            raise self._refuse(who, 409, "Your account has no username, so nobody could tell who the invite is "
                                         "from; set one first")
        private = me.get("private_metadata") if isinstance(me.get("private_metadata"), dict) else {}
        invite: dict = {"from": key["subject"], "fromName": sender, "item": item}
        left_out = 0
        if kind == "space":
            spaces = private.get("spaces", [])
            self._readable_spaces(spaces, who, "shared")
            found = next((e for e in spaces if e["name"].casefold() == name.casefold()), None)
            if found is None:
                raise self._refuse(who, 404, f"You have no space named {name}")
            projects = []
            for p in _space_projects(found):
                (project, ref), = p.items() if isinstance(p, dict) and len(p) == 1 else ((None, None),)
                if not (names.project_ok(project) and names.ref_ok(ref)) or _secret_ref(ref) or \
                        _SECRET_VALUE.search(project):
                    raise self._refuse(who, 409, f"Space {name} holds a project entry xo cannot share; nothing was "
                                                 "shared")
                if names.is_github(project):
                    projects.append({project: ref})
                else:  # a local project: nobody else could install it, and its name stays yours
                    left_out += 1
            invite.update(item=f"space:{found['name']}", projects=projects)
        else:
            projects = private.get("projects", [])
            if not isinstance(projects, list) or name not in projects:
                raise self._refuse(who, 404, f"{name} is not in your projects")
        now = time.time()
        invite["at"] = _iso(now)
        if _size(invite) > INVITE_BUDGET:
            raise self._refuse(who, 422, f"{invite['item']} is too big to share (an invite is at most "
                                         f"{INVITE_BUDGET} bytes); nothing was shared")
        nope = _Reply(404, f"Could not share {invite['item']} with {to}")
        if not USERNAME.fullmatch(to) or to.casefold() == sender.casefold():
            raise self._refuse(who, 404, nope.error)
        try:
            found_users = [u for u in self.clerk.users_named(to)
                           if isinstance(u, dict) and str(u.get("username", "")).casefold() == to.casefold()
                           and USER_ID.fullmatch(str(u.get("id", "")))]
        except ClerkError as e:
            reply = _clerk_failure(e)
            self.audit(**who, outcome=reply.status, clerk=e.seen)
            raise reply from None
        if len(found_users) != 1 or found_users[0]["id"] == key["subject"]:
            raise self._refuse(who, 404, nope.error)
        recipient = found_users[0]["id"]
        who = {**who, "target": hashlib.sha256(recipient.encode()).hexdigest()[:16]}
        mine = (key["subject"], invite["item"].casefold())
        stands: list = []

        def ours(e: Any) -> bool:
            return isinstance(e, dict) and (e.get("from"), str(e.get("item", "")).casefold()) == mine

        def same(a: dict, b: dict) -> bool:
            return {k: v for k, v in a.items() if k != "at"} == {k: v for k, v in b.items() if k != "at"}

        def step(recipient_private: dict, wrote: bool) -> tuple[str, dict]:
            current = recipient_private.get("shared", [])
            if not isinstance(current, list) or any(not (_invite_ok(e) or _old_mark(e)) for e in current):
                raise _Reply(404, nope.error)
            if wrote and invite in current:
                stands[:] = [invite]
                return "done", {"shared": current}
            before = next((e for e in current if ours(e)), None)
            if not wrote and before is not None and same(before, invite):
                stands[:] = [before]  # sent again unchanged: nothing to write
                return "done", {"shared": current}
            if not wrote and before is not None and _iso(now - RESHARE_WAIT) < before["at"]:
                raise _Reply(404, nope.error)
            others = [e for e in current if _invite_ok(e) and not ours(e)]
            if len(others) >= MAX_INVITES or sum(e["from"] == key["subject"] for e in others) >= MAX_INVITES_FROM:
                raise _Reply(404, nope.error)
            out = [*others, invite]
            if _size(out) > SHARED_BUDGET or _size({**recipient_private, "shared": out}) > PRIVATE_BUDGET:
                raise _Reply(404, nope.error)
            return "write", {"shared": out}

        try:
            self._change_keys(recipient, who, step, "invites", "busy")
        except _Reply as r:
            # Nothing about the recipient reaches the caller: their limits, their metadata, their
            # Clerk write limit. Only a server or Clerk failure says to try again.
            status, error = (503, f"Could not share {invite['item']} with {to} right now; try again shortly") \
                if r.status >= 500 else (404, nope.error)
            self.audit(**who, outcome=status)
            raise _Reply(status, error) from None
        stood = stands[0] if stands else invite
        return {"shared": {"to": to, "item": stood["item"],
                           **({"projects": _mask_values(stood["projects"])} if "projects" in stood else {}),
                           **({"localProjectsLeftOut": left_out} if left_out else {})}}

    def _invites_of(self, subject: str, who: dict | None = None) -> tuple[dict, list]:
        """The user's private metadata and their invites; refused if `shared` is not a list of
        entries xo wrote."""
        private = self._user(subject).get("private_metadata")
        private = private if isinstance(private, dict) else {}
        invites = private.get("shared", [])
        if not isinstance(invites, list) or any(not (_invite_ok(e) or _old_mark(e)) for e in invites):
            error = "Your private metadata `shared` holds an entry xo cannot read"
            raise self._refuse(who, 409, error) if who else _Reply(409, error)
        return private, [e for e in invites if _invite_ok(e)]

    def _shared(self, key: dict) -> dict:
        """The invites waiting on the key's own user, each with its id and its sender's username now,
        looked up in one Clerk call (a sender whose account is gone shows the name they shared as). If
        that lookup fails, each shows the name it was shared as, marked senderUnchecked."""
        _, invites = self._invites_of(key["subject"])
        ids = sorted({e["from"] for e in invites if USER_ID.fullmatch(e["from"])})
        now: dict[str, str] = {}
        checked = True
        if ids:
            try:
                for u in self.clerk.users_by_id(ids):
                    if isinstance(u, dict) and u.get("id") in ids and isinstance(u.get("username"), str):
                        now[u["id"]] = u["username"]
            except ClerkError:
                checked = False
        return {"shared": [_shown_invite(e, now.get(e["from"]), gone=checked and e["from"] not in now,
                                         unchecked=not checked) for e in invites]}

    def _answer_invite(self, answer: str, headers: dict[str, str], body: bytes | None) -> dict:
        """accept: in one write to the caller's own metadata, take the invite off `shared` and put
        what it holds where the caller chose. A shared space becomes a new space at rev 1 (named
        `as`, or its own name); a shared project goes into the caller's space named `space`, at
        `ref` (default main), added there or its ref replaced. Its projects go into `projects` too,
        since xo update and sync need every project of a space there. Every refusal is checked
        before that write, so a refused accept leaves nothing behind.
        reject: take it off `shared`, and nothing else: the sender can share it again.
        Both pick the invite by the id `xo list shared` shows."""
        key, _, who = self._caller(headers, "spaces", f"/me/shared/{answer}")
        fields = {"id": True, **({"as": False, "space": False, "ref": False} if answer == "accept" else {})}
        sent = self._body(body, who, fields)
        handle, rename, into, ref = sent["id"], sent.get("as"), sent.get("space"), sent.get("ref")
        if not re.fullmatch(r"[0-9a-f]{%d}" % INVITE_ID_LEN, handle):
            raise self._refuse(who, 422, f"id is the {INVITE_ID_LEN}-character id xo list shared shows")
        subject = key["subject"]
        _, invites = self._invites_of(subject, who)
        match = [e for e in invites if _handle(e) == handle]
        if not match:
            raise self._refuse(who, 404, f"You have no invite {handle} (xo list shared shows them; one sent again has "
                                         "a new id)")
        if len(match) > 1 and answer == "accept":
            # Ids are 64 bits, so only a forged look-alike gets here; a reject takes them all off.
            raise self._refuse(who, 409, f"More than one invite has id {handle}; nothing was changed (xo reject "
                                         f"{handle} takes them all off)")
        invite = match[0]
        kind, _, name = invite["item"].partition(":")
        entries: list[dict] = []
        new: dict | None = None  # a shared space, as it will be recorded
        wanted: list[str] = []
        if answer == "accept" and kind == "space":
            if into is not None or ref is not None:
                raise self._refuse(who, 422, "space and ref are for a shared project; a shared space becomes a space "
                                             "of its own (name it with as)")
            if rename is not None:
                self._space_name(rename, who)
                name = rename
            for p in invite["projects"]:
                (project, pref), = p.items() if isinstance(p, dict) and len(p) == 1 else ((None, None),)
                if not (names.project_ok(project) and names.is_github(project) and names.ref_ok(pref)) or \
                        _secret_ref(pref) or _SECRET_VALUE.search(project) or any(project in e for e in entries):
                    raise self._refuse(who, 409, f"Invite {handle} holds a project entry xo cannot use; reject it "
                                                 "instead")
                entries.append({project: pref})
            if len(entries) > MAX_SPACE_PROJECTS:
                raise self._refuse(who, 422, f"Invite {handle} holds more than {MAX_SPACE_PROJECTS} projects; reject "
                                             "it instead")
            wanted = [next(iter(e)) for e in entries]
            new = {"name": name, "rev": 1, "projects": entries}
        elif answer == "accept":
            if rename is not None:
                raise self._refuse(who, 422, "as names a shared space; put a shared project in one of your spaces "
                                             "with space")
            if into is None:
                raise self._refuse(who, 422, f"Name the space to put {name} in (xo accept ID --space NAME)")
            self._space_name(into, who)
            ref = ref or DEFAULT_REF
            if not names.ref_ok(ref):
                raise self._refuse(who, 422, names.REF_RULE[0].upper() + names.REF_RULE[1:])
            if _secret_ref(ref):
                raise self._refuse(who, 422, "That ref looks like a credential (sk-, ghp_, ak_ and the like); "
                                             "nothing was changed")
            if _SECRET_VALUE.search(name):
                raise self._refuse(who, 409, f"Invite {handle} holds a project name xo cannot use; reject it instead")
            wanted = [name]
        added: list = []
        placed: list = []  # the space a shared project went into, as written

        def into_space(spaces: list, wrote: bool) -> tuple[list | None, dict]:
            """The caller's spaces with the shared project at ref in the space named `into` (None
            when it is already there), and that space."""
            at = next((i for i, e in enumerate(spaces) if e["name"].casefold() == into.casefold()), None)
            if at is None:
                raise self._refuse(who, 409 if wrote else 404, f"You have no space named {into}; nothing was "
                                                               "changed")
            target = spaces[at]
            if any(not isinstance(p, dict) or len(p) != 1 for p in _space_projects(target)):
                raise self._refuse(who, 409, f"Space {into} holds a project entry xo cannot read; nothing was changed")
            if {name: ref} in _space_projects(target):
                return None, target
            rest = [p for p in _space_projects(target) if name not in p]
            if len(rest) >= MAX_SPACE_PROJECTS:
                raise self._refuse(who, 422, f"Space {into} has {MAX_SPACE_PROJECTS} projects, the most there can "
                                             "be; nothing was changed")
            # Already in the space at another ref: replaced in place, as xo connect does.
            pos = next((i for i, p in enumerate(_space_projects(target)) if name in p), len(rest))
            rest.insert(pos, {name: ref})
            changed = {**target, "projects": rest, "rev": _rev(target) + 1}
            return [*spaces[:at], changed, *spaces[at + 1:]], changed

        def step(private: dict, wrote: bool) -> tuple[str, dict]:
            current = private.get("shared", [])
            if not isinstance(current, list):
                raise self._refuse(who, 409, "Your private metadata `shared` is not a list; nothing was changed")
            left = [e for e in current if e not in match and not _old_mark(e)]
            if answer == "reject":
                if not any(e in current for e in match):
                    if wrote:
                        return "done", {"shared": current}
                    raise self._refuse(who, 404, f"You have no invite {handle}")
                return "write", {"shared": left}
            projects = private.get("projects", [])
            if not isinstance(projects, list):
                raise self._refuse(who, 409, "Your private metadata `projects` is not a list; nothing was changed")
            spaces = private.get("spaces", [])
            self._readable_spaces(spaces, who)
            if new is not None:
                there = next((e for e in spaces if e["name"].casefold() == name.casefold()), None)
                in_place = there == new
                out_spaces = None if there is not None else [*spaces, new]
            else:
                out_spaces, target = into_space(spaces, wrote)
                in_place = out_spaces is None
                placed[:] = [target]
            if wrote and invite not in current and all(p in projects for p in wanted):
                if in_place:
                    return "done", {"shared": current, "projects": projects, "spaces": spaces}
                # Our write landed and a write of `spaces` from another instance then replaced the
                # list: the invite is spent, so put our space part back rather than lose it.
                if new is not None and there is not None:
                    raise self._refuse(who, 409, f"A space named {name} was made while this ran; the invite's "
                                                 "projects are in your projects, so add them to a space with xo "
                                                 "connect")
                if new is not None and len(spaces) >= MAX_SPACES:
                    raise self._refuse(who, 409, f"You reached {MAX_SPACES} spaces while this ran; the invite's "
                                                 "projects are in your projects")
                return "write", {"spaces": out_spaces}
            if invite not in current:
                raise self._refuse(who, 409 if wrote else 404, f"Invite {handle} changed while this ran; run xo list "
                                                               "shared" if wrote else f"You have no invite {handle}")
            if new is not None and there is not None and there != new:
                raise self._refuse(who, 409, f"You already have a space named {name}; accept it with --as NEW_NAME. "
                                             "Nothing was changed")
            missing = [p for p in wanted if p not in projects]
            if len(projects) + len(missing) > MAX_PROJECTS:
                raise self._refuse(who, 422, f"Accepting it would take you past {MAX_PROJECTS} projects; nothing was "
                                             "changed")
            if new is not None and there is None and len(spaces) >= MAX_SPACES:
                raise self._refuse(who, 422, f"You have {MAX_SPACES} spaces, the most there can be; nothing was "
                                             "changed")
            added[:] = missing if not wrote else added
            out = {"shared": left, "projects": [*projects, *missing]}
            if out_spaces is not None:
                out["spaces"] = out_spaces
            return "write", out

        self._change_keys(subject, who, step, "invites",
                          "Your invites changed while this ran; run xo list shared to see whether it is still there")
        if answer == "reject":
            return {"rejected": {"id": handle, "item": invite["item"]}}
        space = new if new is not None else placed[0]
        return {"accepted": {"id": handle, "item": invite["item"], "space": _shown_space(space),
                             "projectsAdded": added}}

    def _masked_private(self, private: Any) -> Any:
        shown = self._shown_private(private)
        masked = _mask_secrets(shown)
        if isinstance(shown, dict) and "spaces" in shown:
            masked["spaces"] = _shown_spaces(shown["spaces"])
        if isinstance(shown, dict) and "shared" in shown:
            masked["shared"] = _shown_invites(shown["shared"])
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
        except (ValueError, RecursionError):  # ValueError: bad JSON, bad UTF-8, an int past 4300 digits
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
    if path in ("/healthz", "/me", "/me/projects", "/me/projects/connect", "/me/spaces", "/me/spaces/connect",
                "/config", "/me/share", "/me/shared", "/me/shared/accept", "/me/shared/reject"):
        return path
    if path.startswith("/me/spaces/") and path.endswith("/projects") and path.count("/") == 4:
        return "/me/spaces/{name}/projects"
    if path.startswith("/me/spaces/") and path.count("/") == 3:
        return "/me/spaces/{name}"
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
            data["xo"]["url"] = None  # not an address the list can name: say so
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
