"""GitHub through the gh CLI: log in, check the login works, and log out, on github.com only.

xo never reads, prints or stores a GitHub token: gh keeps its own login, and xo only asks gh who it
is logged in as. Works with gh 2.27 and later (no --json on gh auth status there).
"""
from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys

from xo.client import DEFAULT_TIMEOUT, XoError

HOST = "github.com"
# gh reads these before its stored login; with one set, gh auth login refuses to run.
TOKEN_ENVS = ("GH_TOKEN", "GITHUB_TOKEN")
# Never handed to gh: it needs none of xo's secrets. GH_HOST is dropped too, so the host is always ours.
_NOT_FOR_GH = re.compile(r"(XO_API_KEY|XO_CODER_URL|CODER_.*|CLERK_.*|ELEVENLABS_.*|GH_HOST|GH_ENTERPRISE_TOKEN"
                         r"|GITHUB_ENTERPRISE_TOKEN)\Z")
_LOGIN = re.compile(r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,38})")
INSTALL = "https://cli.github.com"


def installed() -> bool:
    return shutil.which("gh") is not None


def _terminal() -> bool:
    """gh auth login asks questions and opens a browser: it needs a person at a terminal."""
    return sys.stdin.isatty() and sys.stdout.isatty()


class GhUnsure(XoError):
    """gh could not say whether it is logged in, for a reason other than having no login."""


def token_env() -> str | None:
    """The token variable gh will use instead of its stored login, if one is set."""
    return next((name for name in TOKEN_ENVS if os.environ.get(name, "").strip()), None)


class GhClient:
    """Runs gh with the host pinned to github.com and its output kept to itself."""

    def __init__(self, timeout: float = DEFAULT_TIMEOUT):
        self.timeout = timeout

    def env(self) -> dict[str, str]:
        return {k: v for k, v in os.environ.items() if not _NOT_FOR_GH.match(k)}

    def _proc(self, args: list[str]) -> subprocess.CompletedProcess:
        try:
            return subprocess.run(["gh", *args], stdin=subprocess.DEVNULL, capture_output=True, text=True,
                                  env=self.env(), timeout=self.timeout)
        except FileNotFoundError:
            raise XoError(f"GitHub CLI not found: install gh ({INSTALL}), then run xo login github") from None
        except subprocess.TimeoutExpired:
            raise GhUnsure(f"gh {args[0]} timed out") from None
        except OSError:
            raise XoError("could not run the GitHub CLI; check that gh is installed and executable") from None

    def logged_in_as(self) -> str | None:
        """The GitHub user gh is connected as, proven by asking GitHub (gh api user); None only when
        gh says it is not logged in. Any other failure (a revoked token, no network) raises GhUnsure.
        gh's own output is never printed: it can name the token's source and scopes."""
        proc = self._proc(["api", "--hostname", HOST, "user", "--jq", ".login"])
        if proc.returncode == 0:
            login = proc.stdout.strip()
            if not _LOGIN.fullmatch(login):
                raise XoError("gh api user returned an unexpected answer; check gh auth status")
            return login
        status = self._proc(["auth", "status", "--hostname", HOST])
        said = f"{status.stdout}\n{status.stderr}".lower()
        if status.returncode and ("not logged in" in said or "gh auth login" in said) and "failed to log in" not in said:
            return None
        raise GhUnsure(f"gh is set up for {HOST} but GitHub did not accept it (gh api user failed, exit "
                       f"{proc.returncode}); check gh auth status")

    def status(self) -> str:
        """One line for xo status."""
        env = token_env()
        try:
            who = self.logged_in_as()
        except XoError as e:
            return str(e)
        if who is None:
            return "not logged in; run xo login github"
        return f"logged in as {who}" + (f" (with {env} from this terminal)" if env else "")

    def login(self, ask) -> str:
        """Log gh in to github.com in this terminal (browser sign-in), unless it already works.
        `ask(question)` confirms replacing a login GitHub no longer accepts. Returns a one-line result."""
        env = token_env()
        try:
            who = self.logged_in_as()
            unsure = None
        except GhUnsure as e:
            who, unsure = None, str(e)
        if who:
            return (f"already connected to {HOST} as {who}"
                    + (f", with {env} from this terminal (unset it to use gh's own login)" if env else ""))
        if env:
            raise XoError(f"{env} is set in this terminal and GitHub does not accept it; gh auth login "
                          f"refuses to run while it is set. Unset {env}, then run xo login github")
        if not _terminal():
            raise XoError(f"gh auth login needs a terminal; run xo login github in one (it logs in to {HOST})")
        if unsure and not ask(f"{unsure.split(';')[0]}; log gh in to {HOST} again, replacing that login?"):
            raise XoError(f"kept gh's login as it is; nothing changed ({unsure})")
        try:
            code = subprocess.run(["gh", "auth", "login", "--hostname", HOST, "--git-protocol", "https", "--web"],
                                  env=self.env()).returncode
        except FileNotFoundError:
            raise XoError(f"GitHub CLI not found: install gh ({INSTALL}), then run xo login github") from None
        if code:
            raise XoError(f"gh auth login failed (exit {code})")
        who = self.logged_in_as()
        if not who:
            raise XoError("gh auth login finished, but gh is still not logged in; check gh auth status")
        return f"connected to {HOST} as {who}"

    def logout(self, ask) -> str:
        """Log gh out of github.com, only when someone at the terminal says yes: git over https on
        this machine may use the same login."""
        env = token_env()
        if env:
            return f"left alone: {env} is set in this terminal; unset it to stop using it"
        try:
            who = self.logged_in_as()
        except GhUnsure:
            who = "an account GitHub no longer accepts"
        if who is None:
            return "not logged in"
        if not ask(f"Log gh out of {HOST} ({who})? git over https on this machine may use the same login"):
            return "left logged in"
        try:
            code = subprocess.run(["gh", "auth", "logout", "--hostname", HOST], env=self.env()).returncode
        except FileNotFoundError:
            raise XoError(f"GitHub CLI not found: install gh ({INSTALL})") from None
        if code:
            raise XoError(f"gh auth logout failed (exit {code})")
        return f"logged out of {HOST}"
