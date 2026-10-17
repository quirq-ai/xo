"""The network door to XO.

XO API requests go through `Client.request`: this module is the only place that knows the
base URL, how the API key is sent, the timeout, and how a failure is shaped. It never prints or
returns the key (nor the provider keys and bot tokens it is told about), never follows a redirect (a redirect could carry the key to another host), and
refuses plain http except to this machine.
"""
from __future__ import annotations

import dataclasses
import errno
import functools
import getpass
import http.client
import json
import os
import re
import secrets
import stat
import sys
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from xo import __version__

# One address for XO API calls: the server answers `xo me` (and /user-metadata) itself and
# passes the core API on to XO's platform (dev.xo.builders/api/core/v1) under CORE_PATH.
DEFAULT_URL = "https://xo.quirq.dev"
CORE_PATH = "/api/core/v1"
DEFAULT_TIMEOUT = 30.0
KEY_ENV = "XO_API_KEY"
URL_ENV = "XO_URL"
OLD_URL_ENVS = ("XO_BASE_URL", "XO_SERVER_URL")  # before one address; refused, not ignored
CONFIG_ENV = "XDG_CONFIG_HOME"
SAVED = "the key saved by xo login"
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


def xo_url(flag: str | None = None, saved: str | None = None) -> str:
    """The address XO API commands use: --url, else $XO_URL, else the one xo login saved, else
    https://xo.quirq.dev. Only https://, or http:// to this machine."""
    if flag:
        source, url = "--url", flag
    elif os.environ.get(URL_ENV, "").strip():
        source, url = URL_ENV, os.environ[URL_ENV].strip()
    elif old := [name for name in OLD_URL_ENVS if os.environ.get(name, "").strip()]:
        # Set for an xo that had two addresses: going on would send this to another one unasked.
        raise XoError(f"{' and '.join(old)} {'is' if len(old) == 1 else 'are'} no longer read; set {URL_ENV} "
                      f"to the xo server's address (one address for everything), or unset {' and '.join(old)}")
    elif saved:
        source, url = "the URL saved by xo login", saved
    else:
        source, url = "the default URL", DEFAULT_URL
    url = url.rstrip("/")
    try:
        parts = urllib.parse.urlsplit(url)
        parts.hostname  # raises on a malformed bracketed host, such as https://[::1]@evil.example
    except ValueError:
        shown = "it is not shown" if "@" in url else f"not {url!r}"
        raise XoError(f"{source} is not a valid address; {shown}") from None
    if "@" in parts.netloc:
        raise XoError(f"{source} must not hold a user or password; it is not shown")
    try:
        port = parts.port
    except ValueError:
        port = 0
    ok = parts.scheme == "https" or (parts.scheme == "http" and parts.hostname in LOCAL_HOSTS)
    if not ok or not parts.hostname or parts.query or parts.fragment or port == 0:
        # An "@" anywhere may be a password typed with a "/", "?" or "#" in it: never repeat it then.
        shown = "it is not shown" if "@" in url else f"not {url!r}"
        raise XoError(f"{source} must be an https:// address (or http:// to localhost) with no query "
                      f"and a port from 1 to 65535 if any; {shown}")
    return url


def api_key(flag: str | None = None, url: str | None = None, saved: "Login | None" = None) -> str:
    """The key from --api-key (`-` reads one line from stdin), $XO_API_KEY, or the one `xo login`
    saved, in that order. Never echoed. The saved key goes only to the URL it was saved with: a
    different `url` (from --url or $XO_URL) is refused rather than sent the key."""
    if flag == "-":
        source, key = "the key read from stdin", read_key()
    elif flag:
        source, key = "--api-key", flag.strip()
    elif (os.environ.get(KEY_ENV) or "").strip():
        source, key = KEY_ENV, os.environ[KEY_ENV].strip()
    elif saved is not None:
        if url is not None and url != saved.url:
            raise XoError(f"the key xo login saved is only sent to {saved.url}, not {url}; "
                          f"run xo login --url {url} to use this address")
        source, key = SAVED, saved.key
    else:
        source, key = SAVED, ""
    if not key:
        raise XoError(f"no API key: run xo login, set {KEY_ENV}, or pass --api-key - to read it from stdin "
                      "(get a key from the XO team)")
    return check_key(key, source)


def read_key() -> str:
    """One key from stdin: asked for without echo on a terminal, so it never lands in the scrollback."""
    typed = getpass.getpass("XO API key (hidden: paste it, then press Enter): ") if sys.stdin.isatty() else sys.stdin.readline()
    return typed.strip()


def check_key(key: str, source: str) -> str:
    """`key` if it holds only characters an API key may have; the error never shows it."""
    # Only characters that JSON and repr never escape, so redaction finds the key as sent; redact()
    # also looks for the forms a URL, an escaping JSON writer or an HTML writer would give them.
    if not KEY_CHARS.fullmatch(key):
        raise XoError(f"{source} holds characters an API key cannot have "
                      "(only letters, digits and _ . - + / = are allowed); the key is not shown")
    return key


def credentials_path() -> Path:
    """Where `xo login` keeps the key: $XDG_CONFIG_HOME/xo/credentials, else ~/.config/xo/credentials."""
    base = os.environ.get(CONFIG_ENV) or ""
    root = Path(base) if os.path.isabs(base) else Path.home() / ".config"
    return root / "xo" / "credentials"


def _private(st: os.stat_result) -> bool:
    """Only its owner, the current user, can read or write it (Windows has no such mode bits)."""
    if os.name == "nt":
        return True
    return st.st_mode & 0o077 == 0 and st.st_uid == os.getuid()


@dataclass(frozen=True)
class Login:
    """What `xo login` saved: the address and the key that goes only to it."""
    url: str
    key: str = dataclasses.field(repr=False)


def saved_login() -> Login | None:
    """What `xo login` saved, or None when nothing is. A file other users could read is refused
    rather than used: the key in it should be treated as leaked."""
    path = credentials_path()
    try:
        # Non-blocking, so a FIFO put there cannot hang every command.
        fd = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0))
    except FileNotFoundError:
        return None
    except OSError as e:
        raise XoError(f"cannot read {path} ({e.strerror}); run xo logout, then xo login") from None
    st = os.fstat(fd)
    if not stat.S_ISREG(st.st_mode):
        os.close(fd)
        raise XoError(f"{path} is not a file xo login wrote; run xo logout, then xo login")
    with os.fdopen(fd) as f:
        if not _private(st):
            raise XoError(f"{path} can be read by other users, so xo will not use it; run xo logout, "
                          "ask the XO team for a new key, then xo login")
        text = f.read(8192)
    try:
        data = json.loads(text)
    except ValueError:
        data = None
    if not (isinstance(data, dict) and isinstance(data.get("url"), str) and isinstance(data.get("key"), str)
            and data["url"] and data["key"]):
        raise XoError(f"{path} is not a file xo login wrote; run xo logout, then xo login")
    return Login(data["url"], data["key"])


def save_login(login: Login) -> Path:
    """Write `login` where saved_login() reads it: the folder 0700, the file 0600 from the start, and
    swapped in whole (rename), so no other user and no half-written file ever sees it."""
    path = credentials_path()
    tmp = path.with_name(f".credentials.{secrets.token_hex(8)}.tmp")
    try:
        path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        if os.name != "nt":
            os.chmod(path.parent, 0o700)
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0), 0o600)
        if os.name != "nt":
            os.fchmod(fd, 0o600)  # whatever the umask took away, the owner can read it back
        with os.fdopen(fd, "w") as f:
            f.write(json.dumps({"url": login.url, "key": login.key}) + "\n")
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    except OSError as e:
        try:
            tmp.unlink()
        except OSError:
            pass
        raise XoError(f"cannot save the key to {path} ({e.strerror})") from None
    return path


def forget_login() -> Path | None:
    """Delete what xo login saved (a folder put there is left for the user); the path it was at, or None when there was none."""
    path = credentials_path()
    try:
        path.unlink()
    except FileNotFoundError:
        return None
    except OSError as e:
        # Linux reports EISDIR for unlink(directory); macOS reports EPERM. Only classify an
        # actual directory this way: a regular file or symlink with EPERM is a permission error.
        try:
            folder = e.errno in (errno.EISDIR, errno.EPERM) and stat.S_ISDIR(path.lstat().st_mode)
        except OSError:
            folder = False
        if folder:
            raise XoError(f"{path} is a folder, not a file xo login wrote; remove it yourself") from None
        raise XoError(f"cannot delete {path} ({e.strerror})") from None
    return path


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


def _hex(value: int, width: int = 0) -> str:
    """A regex for `value` in hex, either case, at least `width` digits."""
    return "".join(f"[{d}{d.lower()}]" if d.isalpha() else d for d in f"{value:0{width}X}")


# Named HTML references for the characters an API key may hold besides letters, digits and _ . -
_NAMED = {"/": "&sol;", "+": "&plus;", "=": "&equals;"}


@functools.lru_cache(maxsize=32)
def _pattern(key: str) -> re.Pattern[str]:
    """`key` as sent, or as a URL, a JSON writer or an HTML writer might show it, in any mix.

    Each character also matches its %XX form in either case (or double-encoded as %25XX), JSON's
    \\uXXXX, HTML's &#xHH; or &#DD; (zero-padded or not) and its named reference; / also matches
    JSON's \\/, + also a space (a form decoder's) and a space also + (a form encoder's).
    """
    parts = []
    for c in key:
        forms = [re.escape(c),
                 "".join(f"%(?:25)?{_hex(b, 2)}" for b in c.encode()),
                 f"&#[xX]0*{_hex(ord(c))};", f"&#0*{ord(c)};"]
        if ord(c) < 0x10000:
            forms.append(f"\\\\[uU]{_hex(ord(c), 4)}")
        if c in _NAMED:
            forms.append(re.escape(_NAMED[c]))
        if c == "/":
            forms.append(re.escape("\\/"))
        if c == "+":
            forms.append(" ")
        if c == " ":
            forms.append(re.escape("+"))  # a form encoder's space
        parts.append("(?:" + "|".join(forms) + ")")
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
            raise XoError(f"no API key: run xo login, set {KEY_ENV}, or pass --api-key -")
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
