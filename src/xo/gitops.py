"""The git xo runs for projects: the user's own git, with their own login, never a token of xo's.

Every call is `git` with options that keep it to plain https (no ssh, file or ext transports, no
submodules) and `--end-of-options` before anything that came from a name or a ref, so a value can
never be read as an option. Keys and tokens xo or its services use are left out of git's
environment. xo never reads, stores or prints a git credential.
"""
from __future__ import annotations

import os
import subprocess
from pathlib import Path

from xo import names
from xo.client import XoError

GITHUB = "https://github.com"
SAFE = ["-c", "protocol.allow=never", "-c", "protocol.https.allow=always", "-c", "submodule.recurse=false"]
GIT_TIMEOUT = 600.0
# Never handed to git: the XO key, the server's Clerk secret, Coder's and gh's tokens.
_DROPPED = {"XO_API_KEY", "CODER_SESSION_TOKEN", "GH_TOKEN", "GITHUB_TOKEN", "GH_ENTERPRISE_TOKEN",
            "GITHUB_ENTERPRISE_TOKEN"}


def environment() -> dict[str, str]:
    return {k: v for k, v in os.environ.items() if k not in _DROPPED and not k.startswith("CLERK_")}


def run(*args: str, cwd: Path | None = None) -> str:
    try:
        done = subprocess.run(["git", *SAFE, *args], cwd=cwd, env=environment(), capture_output=True, text=True,
                              timeout=GIT_TIMEOUT, stdin=subprocess.DEVNULL)
    except FileNotFoundError:
        raise XoError("git is not installed; install it and try again") from None
    except subprocess.TimeoutExpired:
        raise XoError(f"git {args[0]} did not finish in {GIT_TIMEOUT:.0f} seconds") from None
    if done.returncode != 0:
        lines = [line for line in done.stderr.splitlines() if line.strip()]
        raise XoError(f"git {args[0]} failed: {lines[-1] if lines else f'exit {done.returncode}'}")
    return done.stdout


def clone(name: str, dest: Path) -> None:
    """Clone GitHub's owner/repo into dest, without its submodules."""
    if not (names.project_ok(name) and names.is_github(name)):
        raise XoError(f"{name} is not a GitHub owner/repo")
    run("clone", "--no-recurse-submodules", "--end-of-options", f"{GITHUB}/{name}", str(dest))


def init(dest: Path) -> None:
    """An empty repository at dest, for a project with no GitHub remote yet."""
    run("init", "--quiet", "--end-of-options", str(dest))
