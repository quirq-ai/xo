"""The list of services one `xo login` covers, and the addresses xo last logged in to.

The xo server serves the list at /config, without a key. It never holds keys, tokens or passwords:
only an address and a line about each service. The xo server itself is not in the list: it is
always the server the list was fetched from. xo ships the same file (services.json), checks a
served list exactly as it checks its own copy, and uses its own copy when the server cannot give
a good one.
"""
from __future__ import annotations

import http.client
import json
import os
import re
import secrets
import stat
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from importlib import resources
from pathlib import Path

from xo import __version__
from xo.client import Client, XoError, _private, credentials_path, xo_url

SCHEMA = "xo-services/1"
KNOWN = ("xo-platform", "coder")  # in the order xo logs in to them, after the xo server
MAX_LIST = 64 * 1024
_HOST = re.compile(r"[a-z0-9](?:[a-z0-9-]*[a-z0-9])?(?:\.[a-z0-9](?:[a-z0-9-]*[a-z0-9])?)*")


@dataclass(frozen=True)
class Service:
    name: str
    url: str
    about: str


def listed_url(name: str, url: object) -> str:
    """A listed address: https only, a plain host name, no user, password, query or fragment."""
    problem = f"the address for {name} in the list is not a plain https:// address"
    if not isinstance(url, str) or "@" in url or any(c.isspace() or ord(c) < 32 for c in url):
        raise XoError(problem)
    parts = urllib.parse.urlsplit(url.rstrip("/"))
    try:
        port = parts.port
    except ValueError:
        raise XoError(problem) from None
    if (parts.scheme != "https" or not _HOST.fullmatch(parts.hostname or "") or port == 0 or parts.query
            or parts.fragment or parts.path not in ("", "/") or len(url) > 200):
        raise XoError(problem)
    return url.rstrip("/")


def _clean(text: object) -> str:
    if not isinstance(text, str):
        return ""
    return "".join(c for c in text if c.isprintable())[:200]


def parse(data: object) -> tuple[dict[str, Service], list[str]]:
    """The known services in a list, and a line for each one skipped. Raises XoError when the list
    as a whole is not one xo can use."""
    if not (isinstance(data, dict) and data.get("schema") == SCHEMA and isinstance(data.get("services"), dict)):
        raise XoError(f"it is not an {SCHEMA} list")
    found: dict[str, Service] = {}
    notes: list[str] = []
    for name, entry in data["services"].items():
        if name not in KNOWN:
            shown = _clean(name)[:40] if re.fullmatch(r"[a-z0-9-]{1,40}", str(name)) else "one"
            notes.append(f"skipped the {shown} service: this xo does not know it (an update may)")
            continue
        if not isinstance(entry, dict):
            raise XoError(f"its entry for {name} is not an object")
        found[name] = Service(name, listed_url(name, entry.get("url")), _clean(entry.get("about")))
    return found, notes


SHIPPED = json.loads(resources.files("xo").joinpath("services.json").read_text())


def builtin() -> dict[str, Service]:
    """The copy shipped with xo, checked like a served one."""
    return parse(SHIPPED)[0]


def _get_list(server: str, timeout: float) -> object:
    """GET {server}/config with no key, never following a redirect, reading at most MAX_LIST bytes."""
    client = Client(server, None, timeout)
    req = urllib.request.Request(client.url("/config"), headers={"Accept": "application/json",
                                                                 "User-Agent": f"xo/{__version__}"})
    try:
        with client._opener.open(req, timeout=timeout) as resp:
            raw = resp.read(MAX_LIST + 1)
    except urllib.error.HTTPError as e:
        raise XoError(f"it answered HTTP {e.code}") from None
    except (urllib.error.URLError, TimeoutError, OSError, http.client.HTTPException, ValueError):
        raise XoError("it could not be reached") from None
    if len(raw) > MAX_LIST:
        raise XoError(f"its answer is over {MAX_LIST} bytes")
    try:
        data = json.loads(raw)
    except ValueError:
        raise XoError("its answer is not JSON") from None
    return data.get("data") if isinstance(data, dict) else None


def fetch(server: str, timeout: float) -> tuple[dict[str, Service], list[str]]:
    """The server's list, or xo's own copy with a line saying why."""
    own = builtin()
    try:
        found, notes = parse(_get_list(server, timeout))
    except XoError as e:
        return own, [f"using xo's own list of services: the server's list could not be used ({_clean(str(e))})"]
    for name in KNOWN:
        if name not in found:
            found[name] = own[name]
            notes.append(f"the server's list has no {name}; using xo's own address for it")
    return {name: found[name] for name in KNOWN}, notes


# Where each service was last logged in to: not secret, but private like the key file.
def addresses_path() -> Path:
    return credentials_path().with_name("services")


def saved_addresses() -> dict[str, str]:
    """The address xo last logged in to, per service; {} when there is none or the file is unusable."""
    # Read like the credentials file: no symlink, a regular file the user alone owns and can read,
    # and non-blocking, so a FIFO put there cannot hang xo status or a Coder command.
    try:
        fd = os.open(addresses_path(), os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0))
    except OSError:
        return {}
    try:
        st = os.fstat(fd)
        if not (stat.S_ISREG(st.st_mode) and _private(st)):
            return {}
        with os.fdopen(fd, "rb", closefd=False) as f:
            data = json.loads(f.read(MAX_LIST).decode("utf-8"))
    except (OSError, ValueError):
        return {}
    finally:
        os.close(fd)
    if not isinstance(data, dict):
        return {}
    out = {}
    for name in KNOWN:
        try:
            out[name] = listed_url(name, data.get(name))
        except XoError:
            pass
    if isinstance(data.get("xo-server"), str):
        try:
            out["xo-server"] = xo_url(data["xo-server"])
        except XoError:
            pass
    return out


def save_addresses(addresses: dict[str, str]) -> None:
    path = addresses_path()
    tmp = path.with_name(f".services.{secrets.token_hex(8)}.tmp")
    try:
        path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        if os.name != "nt":
            os.chmod(path.parent, 0o700)
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0), 0o600)
        if os.name != "nt":
            os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w") as f:
            f.write(json.dumps(addresses, indent=1, sort_keys=True) + "\n")
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    except OSError as e:
        try:
            tmp.unlink()
        except OSError:
            pass
        raise XoError(f"cannot save the addresses to {path} ({e.strerror})") from None


def forget_addresses() -> None:
    try:
        addresses_path().unlink()
    except FileNotFoundError:
        pass
    except OSError as e:
        raise XoError(f"cannot delete {addresses_path()} ({e.strerror})") from None


def coder_default() -> str:
    """The Coder deployment `xo coder` uses when no flag or $XO_CODER_URL says: the one xo login
    last logged in to, else xo's own list."""
    return saved_addresses().get("coder") or builtin()["coder"].url
