"""The quirq hub: where a space's projects meet while people and their agents work on them.

GitHub stays where a project's work ends up. The hub is a relay next to it: each checkout of a
space's GitHub project gets a second remote, always named `quirq` and always pointing at a quirq
URL, `quirq://hub.quirq.dev/AGENT/OWNER--REPO`, and `xo update` and `xo sync` of a space push and
pull there instead of to GitHub. `origin`, the branch upstreams and everything else GitHub sees
are left as they are, so `git push` and pull requests work as before.

`xo login` asks the xo server for the user's hub agent (POST /me/hub). When the server keeps a hub,
the agent's token goes straight to the hub's own client (`spacehub auth login --with-token`), which
keeps it; xo saves only the hub's address and the agent's name, in `hub` next to the key file.
Without that file, a space works with GitHub alone, as before. git reaches the hub through
`git-remote-quirq`, which comes with `spacehub` (quirq-ai/space-hub).

The hub can be read by anyone in this version, so only a project anyone can read on GitHub is
pushed there; a private one is refused before anything is sent.
"""
from __future__ import annotations

import dataclasses
import json
import os
import re
import secrets
import shutil
import stat
import subprocess
from pathlib import Path

from xo import names
from xo.client import XoError, credentials_path

REMOTE = "quirq"
SCHEME = "quirq"
HELPER = "git-remote-quirq"
CLIENT = "spacehub"
INSTALL = ("install the hub's client: git clone https://github.com/quirq-ai/space-hub && "
           "space-hub/scripts/install-cli.sh (needs Node 22 and git), then run xo login again")
TIMEOUT = 120.0
AGENT = re.compile(r"[a-z0-9](?:[a-z0-9-]{0,37}[a-z0-9])?")
HOST = re.compile(r"[A-Za-z0-9](?:[A-Za-z0-9.-]{0,251}[A-Za-z0-9])?(?::\d{1,5})?")
MAX_NAME = 100  # the hub's longest space name
# Never handed to the hub's client: xo's key and the tokens of other services.
_DROPPED = {"XO_API_KEY", "CODER_SESSION_TOKEN", "GH_TOKEN", "GITHUB_TOKEN", "GH_ENTERPRISE_TOKEN",
            "GITHUB_ENTERPRISE_TOKEN", "SPACEHUB_TOKEN"}


@dataclasses.dataclass(frozen=True)
class Hub:
    """Where xo login found the user's hub: its https address and the user's agent there."""
    server: str
    agent: str

    @property
    def host(self) -> str:
        return self.server.split("://", 1)[1]

    @property
    def websocket(self) -> str:
        """The address the hub's client takes (wss://, as it writes it)."""
        return "wss://" + self.host

    def url(self, project: str) -> str:
        """The quirq URL of project's copy on this hub: the project OWNER--REPO in the agent's own space."""
        return self.space_url(name(project))

    def space_url(self, space: str) -> str:
        """The quirq URL of the hub project AGENT/space (a folder space of xo create space)."""
        return f"{SCHEME}://{self.host}/{self.agent}/{space}"


def name(project: str) -> str:
    """The hub project, in the agent's space, a GitHub project's copy lives in: OWNER--REPO (a GitHub
    owner never holds two hyphens in a row, so no two projects share one)."""
    if not (names.project_ok(project) and names.is_github(project)):
        raise XoError(f"{project} has no GitHub repo, so it has no copy on the hub")
    owner, repo = project.split("/")
    found = f"{owner}--{repo}"
    if len(found) > MAX_NAME:
        raise XoError(f"{project} is too long a name for the hub ({MAX_NAME} characters at most for OWNER--REPO)")
    return found


def hub_path() -> Path:
    return credentials_path().with_name("hub")


def parse(server: object, agent: object) -> Hub:
    """A hub as the xo server describes it, checked: an https address with nothing after its host,
    and an agent name the hub could have made."""
    if not (isinstance(server, str) and server.startswith("https://") and HOST.fullmatch(server[len("https://"):])):
        raise XoError("the xo server's hub address is not https://HOST")
    if not (isinstance(agent, str) and AGENT.fullmatch(agent)):
        raise XoError("the xo server's hub agent name is not one the hub uses")
    return Hub(server, agent)


def settings() -> Hub | None:
    """The hub xo login saved, or None: no file, or one xo cannot trust (then the space works with
    GitHub alone, as before xo login found a hub)."""
    try:
        fd = os.open(hub_path(), os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0))
    except OSError:
        return None
    try:
        st = os.fstat(fd)
        if not stat.S_ISREG(st.st_mode) or (os.name != "nt" and (st.st_mode & 0o022 or st.st_uid != os.getuid())):
            return None  # a file others could have written could point pushes elsewhere
        with os.fdopen(fd, "rb", closefd=False) as f:
            data = json.loads(f.read(4096).decode("utf-8"))
        return parse(data.get("server"), data.get("agent")) if isinstance(data, dict) else None
    except (OSError, ValueError, XoError):
        return None
    finally:
        os.close(fd)


def save(hub: Hub) -> Path:
    path = hub_path()
    tmp = path.with_name(f".hub.{secrets.token_hex(8)}.tmp")
    try:
        path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0), 0o600)
        with os.fdopen(fd, "w") as f:
            f.write(json.dumps({"server": hub.server, "agent": hub.agent}) + "\n")
        os.replace(tmp, path)
    except OSError as e:
        try:
            tmp.unlink()
        except OSError:
            pass
        raise XoError(f"cannot save the hub's address to {path} ({e.strerror})") from None
    return path


def forget() -> Path | None:
    """Delete what xo login saved about the hub; the path deleted, or None when there was none."""
    try:
        hub_path().unlink()
    except FileNotFoundError:
        return None
    except OSError as e:
        raise XoError(f"cannot delete {hub_path()} ({e.strerror})") from None
    return hub_path()


def installed() -> bool:
    """Whether git can reach the hub here: the hub's client and its git helper are on PATH."""
    return bool(shutil.which(CLIENT) and shutil.which(HELPER))


def ready(hub: Hub | None) -> Hub | None:
    """The hub to use, or None when xo login found none. A hub without its client here is an error,
    never a quiet fall back to GitHub: the space's work would go somewhere else than the user expects."""
    if hub is not None and not installed():
        raise XoError(f"your space works through the quirq hub at {hub.server}, but {HELPER} is not installed; "
                      + INSTALL)
    return hub


def _client(hub: Hub, *args: str, input: str | None = None) -> str:
    """Run the hub's client against this hub, with none of xo's keys in its environment."""
    env = {k: v for k, v in os.environ.items() if k not in _DROPPED}
    env["SPACEHUB_SERVER"] = hub.websocket
    try:
        done = subprocess.run([CLIENT, *args], env=env, input=input, capture_output=True, text=True,
                              timeout=TIMEOUT, stdin=None if input is not None else subprocess.DEVNULL)
    except FileNotFoundError:
        raise XoError(f"{CLIENT} is not installed; " + INSTALL) from None
    except subprocess.TimeoutExpired:
        raise XoError(f"{CLIENT} {args[0]} did not finish in {TIMEOUT:.0f} seconds") from None
    if done.returncode != 0:
        lines = [line for line in (done.stderr or "").splitlines() if line.strip()]
        error = XoError(f"{CLIENT} {' '.join(args[:2])} failed: {lines[-1] if lines else f'exit {done.returncode}'}")
        error.detail = done.stderr or ""
        raise error
    return done.stdout


def link(hub: Hub, token: str) -> None:
    """Hand the agent's token to the hub's client, which keeps it for this hub's address only. The
    token goes on stdin, never on a command line or into xo's own files."""
    _client(hub, "auth", "login", "--server", hub.websocket, "--with-token", input=token + "\n")


def create(hub: Hub, project: str) -> None:
    """Make the hub copy of project, AGENT/OWNER--REPO; one that is already there is left as it is."""
    make(hub, name(project), f"xo: a copy of github.com/{project}")


def make(hub: Hub, space: str, description: str) -> None:
    """Make the project AGENT/space on the hub, in the agent's own space, where space_url points;
    one that is already there is left as it is. Always `project create AGENT/NAME`: since space-hub's
    builders, spaces and projects (#5), `space create NAME` makes an org called NAME instead."""
    try:
        _client(hub, "project", "create", f"{hub.agent}/{space}", "-d", description)
    except XoError as e:
        detail = getattr(e, "detail", "")
        if 'unknown command "project"' in detail:
            raise XoError(f"{CLIENT} here is older than the hub (it has no project command); update it: "
                          "git pull in your space-hub folder, then scripts/install-cli.sh") from None
        if "already exists" not in detail:
            raise
