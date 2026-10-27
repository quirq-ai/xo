"""Other CLIs, run through xo while xo is connected to them: `xo gh ...` runs GitHub's gh, and
`xo sh ...` runs the quirq hub's client `spacehub` (sh for short) against the hub xo login found.

The arguments go to the tool as they are, and its input, output and exit code are its own; xo only
picks the program, checks that it is connected, and hands it an environment without xo's keys or
other services' tokens. `sh` is a word inside xo only: the hub's client is installed as
`spacehub`, never as `sh`, which is the system shell. Attaching another CLI is one entry in TOOLS.
"""
from __future__ import annotations

import dataclasses
import os
import shutil
import subprocess
from collections.abc import Callable

from xo import github, quirq
from xo.client import XoError


@dataclasses.dataclass(frozen=True)
class Tool:
    program: str  # what is run
    about: str  # one line for xo --help
    install: str  # how to get the program
    env: Callable[[], dict[str, str]]  # its environment, or XoError when xo is not connected to it


def _gh_env() -> dict[str, str]:
    return github.GhClient().env()


def _sh_env() -> dict[str, str]:
    hub = quirq.settings()
    if hub is None:
        raise XoError("xo is not connected to a quirq hub; run xo login (it finds your hub), then xo sh again")
    env = {k: v for k, v in os.environ.items() if k not in quirq._DROPPED}
    env["SPACEHUB_SERVER"] = hub.websocket
    return env


TOOLS: dict[str, Tool] = {
    "gh": Tool("gh", "run GitHub's gh with these arguments (xo gh pr list)",
               f"install it from {github.INSTALL}", _gh_env),
    "sh": Tool(quirq.CLIENT, "run the quirq hub's client spacehub with these arguments, on the hub xo login "
                             "found (xo sh project list)", quirq.INSTALL, _sh_env),
}


def run(name: str, args: list[str]) -> int:
    """Run the tool called name with args, attached to this terminal; its exit code."""
    tool = TOOLS[name]
    env = tool.env()
    program = shutil.which(tool.program, path=env.get("PATH"))
    if program is None:
        raise XoError(f"{tool.program} is not installed; {tool.install}")
    try:
        return subprocess.run([program, *args], env=env).returncode
    except OSError as e:
        raise XoError(f"could not run {tool.program} ({e.strerror})") from None
    except KeyboardInterrupt:
        return 130
