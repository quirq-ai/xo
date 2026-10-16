"""The git xo runs for projects: the user's own git, with their own login, never a token of xo's.

Every call is `git` with GIT_ALLOW_PROTOCOL=https in its environment, which overrides any protocol
setting or insteadOf in the user's config (no ssh, file or ext transports), no submodules, and
`--end-of-options` before anything that came from a name or a ref, so a value can never be read as
an option. Keys and tokens xo or its services use, and any variable that points git at another
repository, are left out of git's environment. xo never reads, stores or prints a git credential.
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
# Never inherited: these point git at another repository than the one xo names (git rev-parse
# --local-env-vars, less the user's own config settings).
_ELSEWHERE = {"GIT_DIR", "GIT_WORK_TREE", "GIT_IMPLICIT_WORK_TREE", "GIT_INDEX_FILE", "GIT_OBJECT_DIRECTORY",
              "GIT_ALTERNATE_OBJECT_DIRECTORIES", "GIT_COMMON_DIR", "GIT_GRAFT_FILE", "GIT_NO_REPLACE_OBJECTS",
              "GIT_REPLACE_REF_BASE", "GIT_PREFIX", "GIT_SHALLOW_FILE", "GIT_NAMESPACE", "GIT_CONFIG"}
# The only transports git may use; it beats every protocol.* setting and insteadOf.
ALLOWED_PROTOCOLS = "https"


def environment() -> dict[str, str]:
    env = {k: v for k, v in os.environ.items()
           if k not in _DROPPED and k not in _ELSEWHERE and not k.startswith("CLERK_")}
    env["GIT_ALLOW_PROTOCOL"] = ALLOWED_PROTOCOLS
    return env


def run(*args: str, cwd: Path | None = None, check: bool = True) -> str | None:
    """git's output; with check=False, None when git fails instead of an error."""
    try:
        done = subprocess.run(["git", *SAFE, *args], cwd=cwd, env=environment(), capture_output=True, text=True,
                              timeout=GIT_TIMEOUT, stdin=subprocess.DEVNULL)
    except FileNotFoundError:
        raise XoError("git is not installed; install it and try again") from None
    except subprocess.TimeoutExpired:
        raise XoError(f"git {args[0]} did not finish in {GIT_TIMEOUT:.0f} seconds") from None
    if done.returncode != 0:
        if not check:
            return None
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


def fetch(dest: Path) -> None:
    run("fetch", "--quiet", "--tags", "--no-recurse-submodules", "origin", cwd=dest)


def dirty(dest: Path) -> bool:
    """Whether tracked files hold changes not committed (untracked files do not count)."""
    return bool((run("status", "--porcelain", "--untracked-files=no", cwd=dest) or "").strip())


def branch(dest: Path) -> str | None:
    """The branch checked out, or None when HEAD is detached."""
    name = run("symbolic-ref", "--quiet", "--short", "HEAD", cwd=dest, check=False)
    return name.strip() if name else None


def current(dest: Path) -> str:
    """What a space would record for this checkout: its branch, or its commit when detached."""
    return branch(dest) or (run("rev-parse", "--verify", "HEAD", cwd=dest) or "").strip()


def _commit(dest: Path, ref: str) -> str | None:
    out = run("rev-parse", "--verify", "--quiet", "--end-of-options", f"{ref}^{{commit}}", cwd=dest, check=False)
    return out.strip() if out else None


def resolve(dest: Path, ref: str) -> tuple[str, str]:
    """("branch" | "tag" | "commit", sha) for a ref, after a fetch: a branch on origin first, then a
    tag, then a commit sha that a branch or tag on origin holds. Anything else is an error."""
    if not names.ref_ok(ref):
        raise XoError(f"{ref!r} is not a ref xo uses: {names.REF_RULE}")
    sha = _commit(dest, f"refs/remotes/origin/{ref}")
    if sha:
        return "branch", sha
    sha = _commit(dest, f"refs/tags/{ref}")
    if sha:
        return "tag", sha
    if len(ref) == 40 and all(c in "0123456789abcdef" for c in ref.lower()):
        sha = _commit(dest, ref)
        if sha and _published(dest, sha):
            return "commit", sha
    raise XoError(f"{ref} is not a branch, tag or commit on {dest}'s remote")


def _published(dest: Path, sha: str) -> bool:
    for kind in (["branch", "--remotes"], ["tag"]):
        out = run(*kind, "--contains", sha, cwd=dest, check=False)
        if out and out.strip():
            return True
    return False


def checkout(dest: Path, ref: str) -> None:
    """Put the checkout at ref: a branch is switched to and fast-forwarded to origin's (refused if
    the local branch has commits origin lacks), a tag or commit is checked out detached."""
    kind, sha = resolve(dest, ref)
    if kind != "branch":
        run("switch", "--quiet", "--detach", sha, cwd=dest)
        return
    if _commit(dest, f"refs/heads/{ref}"):
        run("switch", "--quiet", "--no-guess", ref, cwd=dest)
        if run("merge", "--ff-only", "--quiet", f"refs/remotes/origin/{ref}", cwd=dest, check=False) is None:
            raise XoError(f"{dest}: your branch {ref} has commits its remote lacks; push them or run xo sync")
    else:
        run("switch", "--quiet", "--create", ref, "--track", f"refs/remotes/origin/{ref}", cwd=dest)


def upstream(dest: Path) -> tuple[str, str, str]:
    """(branch, remote, remote branch) of a checkout that can be pushed: on a branch, clean, with an
    upstream. Anything else is refused, since a push from it could not be what the user meant."""
    name = branch(dest)
    if name is None:
        raise XoError(f"{dest} is not on a branch (detached HEAD); switch to one first")
    if dirty(dest):
        raise XoError(f"{dest} has changes that are not committed; commit or stash them first")
    remote = (run("config", "--get", f"branch.{name}.remote", cwd=dest, check=False) or "").strip()
    merge = (run("config", "--get", f"branch.{name}.merge", cwd=dest, check=False) or "").strip()
    if not remote or not merge.startswith("refs/heads/"):
        raise XoError(f"{dest}: branch {name} has no upstream; set one with git branch --set-upstream-to")
    return name, remote, merge[len("refs/heads/"):]


def push(dest: Path) -> None:
    """Push the current branch to its upstream: never forced, so git refuses if the remote is ahead."""
    name, remote, theirs = upstream(dest)
    try:
        run("push", "--quiet", "--end-of-options", remote, f"HEAD:refs/heads/{theirs}", cwd=dest)
    except XoError as e:
        raise XoError(f"{dest}: {e} (if the remote is ahead, run xo sync)") from None


def pull_rebase(dest: Path) -> None:
    upstream(dest)
    run("pull", "--rebase", "--quiet", "--no-recurse-submodules", cwd=dest)
