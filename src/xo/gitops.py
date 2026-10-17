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
        error = XoError(f"git {args[0]} failed: {lines[-1] if lines else f'exit {done.returncode}'}")
        error.detail = done.stderr  # all of git's message, for a caller that needs to tell failures apart
        raise error
    return done.stdout


def clone(name: str, dest: Path) -> None:
    """Clone GitHub's owner/repo into dest, without its submodules."""
    if not (names.project_ok(name) and names.is_github(name)):
        raise XoError(f"{name} is not a GitHub owner/repo")
    run("clone", "--no-recurse-submodules", "--end-of-options", f"{GITHUB}/{name}", str(dest))


def _is_repo(url: str, name: str) -> bool:
    """Whether a remote URL is GitHub's owner/repo, in the https, git@ or ssh:// form."""
    url = url.strip().rstrip("/")
    url = url[:-4] if url.endswith(".git") else url
    forms = (f"{GITHUB}/{name}", f"https://github.com/{name}", f"git@github.com:{name}",
             f"ssh://git@github.com/{name}")
    return any(url.casefold() == f.casefold() for f in forms)


def set_origin(dest: Path, name: str) -> str:
    """Point a checkout with no origin at GitHub's owner/repo, and say what happened. One whose
    origin, fetch and push URLs alike, is already that repo is left as it is; any other origin is
    refused, so nothing later pushes the project to the wrong place."""
    if not (names.project_ok(name) and names.is_github(name)):
        raise XoError(f"{name} is not a GitHub owner/repo")
    own_checkout(dest)
    url = f"{GITHUB}/{name}"
    if run("remote", "get-url", "origin", cwd=dest, check=False) is None:
        run("remote", "add", "origin", url, cwd=dest)
        return f"its origin is now {url}"
    if not origin_is(dest, name):
        # Never echo a URL: it can carry a token. One with a login in it is refused too, since xo
        # cannot tell where it points.
        raise XoError(f"its origin is not one xo can confirm is {name}; point it at {url} yourself")
    return f"its origin is already {name}"


def origin_is(dest: Path, name: str) -> bool:
    """Whether a checkout's origin, every fetch and push URL, is GitHub's owner/repo."""
    urls = [line for kind in (["--all"], ["--push", "--all"])
            for line in (run("remote", "get-url", *kind, "origin", cwd=dest, check=False) or "").splitlines()
            if line.strip()]
    return bool(urls) and all(_is_repo(u, name) for u in urls)


def init(dest: Path) -> None:
    """An empty repository at dest, for a project with no GitHub remote yet."""
    run("init", "--quiet", "--end-of-options", str(dest))


def own_checkout(dest: Path) -> None:
    """Refuse unless dest is the top of a git checkout. Without this, git run in a plain folder walks
    up to whatever repository holds it, and would fetch or push that one."""
    top = run("rev-parse", "--show-toplevel", cwd=dest, check=False) if dest.is_dir() else None
    if top is None or Path(top.strip()).resolve() != dest.resolve():
        raise XoError(f"./{dest} is not a git checkout of its own; xo install makes one")


def fetch(dest: Path) -> None:
    own_checkout(dest)
    # Not --quiet: when a fetch fails, git's last line says why (a tag moved on the remote, say).
    run("fetch", "--tags", "--no-recurse-submodules", "origin", cwd=dest)


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
            raise XoError(f"./{dest}: your branch {ref} and its remote have both moved on; rebase yours on it and "
                          "push (git pull --rebase, then git push, in that folder), then try again")
    else:
        run("switch", "--quiet", "--create", ref, "--track", f"refs/remotes/origin/{ref}", cwd=dest)


def upstream(dest: Path) -> tuple[str, str, str]:
    """(branch, remote, remote branch) of a checkout that can be pushed: on a branch, clean, with an
    upstream. Anything else is refused, since a push from it could not be what the user meant."""
    own_checkout(dest)
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


def tracking(dest: Path) -> bool:
    """Whether the checkout is on a branch with an upstream, so it can be pulled and pushed."""
    name = branch(dest)
    return name is not None and bool(run("config", "--get", f"branch.{name}.merge", cwd=dest, check=False))


def push(dest: Path) -> None:
    """Push the current branch to its upstream: never forced, so git refuses if the remote is ahead."""
    name, remote, theirs = upstream(dest)
    try:
        run("push", "--quiet", "--end-of-options", remote, f"HEAD:refs/heads/{theirs}", cwd=dest)
    except XoError as e:
        ahead = any(w in getattr(e, "detail", "") for w in ("[rejected]", "non-fast-forward", "fetch first"))
        raise XoError(f"./{dest}: {e}" + (" (the remote is ahead: run xo sync)" if ahead else "")) from None


def pull_rebase(dest: Path) -> None:
    """Rebase the current branch on its upstream. On a conflict the rebase is undone, so the checkout
    is as it was, and the error says so."""
    name, _, _ = upstream(dest)
    try:
        run("pull", "--rebase", "--quiet", "--no-recurse-submodules", cwd=dest)
    except XoError as e:
        if run("rebase", "--abort", cwd=dest, check=False) is not None:  # it stopped mid-rebase
            raise XoError(f"./{dest}: your branch {name} and its upstream change the same lines, so xo stopped and "
                          "left it as it was; rebase it by hand (git pull --rebase in that folder)") from None
        raise XoError(f"./{dest}: {e}") from None


def fast_forward(dest: Path) -> str:
    """After a fetch, move the checked-out branch up to its upstream when it is only behind; what
    happened, in words. A branch that has moved on from its upstream is refused, never merged."""
    name = branch(dest)
    if name is None:
        return "fetched; HEAD is detached, so it was left where it is"
    if not tracking(dest):
        return f"fetched; {name} has no upstream, so it was left where it is"
    if run("merge", "--ff-only", "--quiet", "@{upstream}", cwd=dest, check=False) is None:
        raise XoError(f"./{dest}: your branch {name} and its upstream have both moved on; run xo sync project:"
                      f"{dest} to rebase yours on it")
    return f"{name} is up to date with its upstream"
