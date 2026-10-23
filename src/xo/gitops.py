"""The git xo runs for projects: the user's own git, with their own login, never a token of xo's.

Every call is `git` with GIT_ALLOW_PROTOCOL=https in its environment, which overrides any protocol
setting or insteadOf in the user's config (no ssh, file or ext transports; calls to the quirq hub's
remote also allow quirq, its git helper, and nothing else), no submodules, and
`--end-of-options` before anything that came from a name or a ref, so a value can never be read as
an option. Keys and tokens xo or its services use, and any variable that points git at another
repository, are left out of git's environment. xo never reads, stores or prints a git credential.
"""
from __future__ import annotations

import os
import re
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
QUIRQ = "quirq"  # the hub's remote, and its URL scheme (xo.quirq)


def environment() -> dict[str, str]:
    env = {k: v for k, v in os.environ.items()
           if k not in _DROPPED and k not in _ELSEWHERE and not k.startswith("CLERK_")}
    env["GIT_ALLOW_PROTOCOL"] = ALLOWED_PROTOCOLS
    return env


def run(*args: str, cwd: Path | None = None, check: bool = True, env: dict[str, str] | None = None,
        input: bytes | None = None, raw: bool = False, hub: bool = False) -> str | None:
    """git's output; with check=False, None when git fails instead of an error. env: variables added
    to git's environment; input: the bytes git reads on stdin (nothing when None; bytes, so no line
    ending is changed on the way, as text mode does on Windows); raw: git's output as it is (paths with
    -z, any bytes), decoded with surrogateescape rather than the locale; hub: the call may use the
    quirq hub's transport too."""
    binary = raw or input is not None
    stdin = {"input": input} if input is not None else {"stdin": subprocess.DEVNULL}
    allowed = {"GIT_ALLOW_PROTOCOL": f"{ALLOWED_PROTOCOLS}:{QUIRQ}"} if hub else {}
    extra = ["-c", f"protocol.{QUIRQ}.allow=always"] if hub else []
    try:
        done = subprocess.run(["git", *SAFE, *extra, *args], cwd=cwd, env={**environment(), **(env or {}), **allowed},
                              capture_output=True, text=not binary, timeout=GIT_TIMEOUT, **stdin)
    except FileNotFoundError:
        raise XoError("git is not installed; install it and try again") from None
    except subprocess.TimeoutExpired:
        raise XoError(f"git {args[0]} did not finish in {GIT_TIMEOUT:.0f} seconds") from None
    out, err = done.stdout, done.stderr
    if binary:
        out, err = out.decode("utf-8", "surrogateescape"), err.decode("utf-8", "replace")
    if done.returncode != 0:
        if not check:
            return None
        lines = [line for line in err.splitlines() if line.strip()]
        error = XoError(f"git {args[0]} failed: {lines[-1] if lines else f'exit {done.returncode}'}")
        error.detail = err  # all of git's message, for a caller that needs to tell failures apart
        error.code = done.returncode
        raise error
    return out


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


def _remote_branch(dest: Path, ref: str, remotes: tuple[str, ...]) -> tuple[str, str] | None:
    """(remote, sha) of the first of remotes that has branch ref, after a fetch."""
    for remote in remotes:
        sha = _commit(dest, f"refs/remotes/{remote}/{ref}")
        if sha:
            return remote, sha
    return None


def resolve(dest: Path, ref: str, remotes: tuple[str, ...] = ("origin",)) -> tuple[str, str]:
    """("branch" | "tag" | "commit", sha) for a ref, after a fetch: a branch on one of remotes first
    (the first that has it), then a tag, then a commit sha that a branch on a remote or a tag holds.
    Anything else is an error."""
    if not names.ref_ok(ref):
        raise XoError(f"{ref!r} is not a ref xo uses: {names.REF_RULE}")
    found = _remote_branch(dest, ref, remotes)
    if found:
        return "branch", found[1]
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


def checkout(dest: Path, ref: str, hub: bool = False) -> None:
    """Put the checkout at ref: a branch is switched to and fast-forwarded to the remote's (refused if
    the local branch has commits the remote lacks), a tag or commit is checked out detached. With
    hub, a branch the quirq remote has is taken from there (the space's latest work), and a new
    local branch still tracks origin's when GitHub has it, so git push and pull stay on GitHub."""
    remotes = (QUIRQ, "origin") if hub else ("origin",)
    kind, sha = resolve(dest, ref, remotes)
    if kind != "branch":
        run("switch", "--quiet", "--detach", sha, cwd=dest)
        return
    remote, _ = _remote_branch(dest, ref, remotes)
    if _commit(dest, f"refs/heads/{ref}"):
        run("switch", "--quiet", "--no-guess", ref, cwd=dest)
        if run("merge", "--ff-only", "--quiet", f"refs/remotes/{remote}/{ref}", cwd=dest, check=False) is None:
            how = (f"git rebase {QUIRQ}/{ref}, then xo update space:SPACE" if remote == QUIRQ
                   else "git pull --rebase, then git push")
            raise XoError(f"./{dest}: your branch {ref} and its remote have both moved on; rebase yours on it ({how}, "
                          "in that folder), then try again")
        return
    if remote == "origin":
        run("switch", "--quiet", "--create", ref, "--track", f"refs/remotes/origin/{ref}", cwd=dest)
        return
    upstream = "origin" if _commit(dest, f"refs/remotes/origin/{ref}") else QUIRQ
    run("switch", "--quiet", "--create", ref, "--no-track", f"refs/remotes/{QUIRQ}/{ref}", cwd=dest)
    run("branch", "--quiet", f"--set-upstream-to=refs/remotes/{upstream}/{ref}", ref, cwd=dest)


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


def ahead_of_upstream(dest: Path) -> bool:
    """Whether the checked-out branch has commits its upstream lacks (after a fetch), so a push
    would send something."""
    count = run("rev-list", "--count", "@{upstream}..HEAD", cwd=dest, check=False)
    return count is None or count.strip() != "0"


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


def set_quirq(dest: Path, url: str) -> None:
    """Give a checkout the quirq hub's remote at url. One already there must point at url, fetch and
    push alike: a quirq remote that points anywhere else is refused, never changed."""
    own_checkout(dest)
    have = [line for kind in (["--all"], ["--push", "--all"])
            for line in (run("remote", "get-url", *kind, QUIRQ, cwd=dest, check=False) or "").splitlines()
            if line.strip()]
    if not have:
        run("remote", "add", "--no-tags", QUIRQ, url, cwd=dest)
    elif any(u.strip() != url for u in have):
        raise XoError(f"./{dest} has a {QUIRQ} remote that is not {url}; remove it (git remote remove {QUIRQ} in "
                      "that folder) and try again")


def fetch_quirq(dest: Path) -> bool:
    """Fetch the quirq remote's branches (never its tags); False when the hub has no copy yet."""
    try:
        run("fetch", "--quiet", "--no-tags", "--no-recurse-submodules", QUIRQ, cwd=dest, hub=True)
    except XoError as e:
        if re.search(r"does not exist|does not appear to be a git repository", getattr(e, "detail", "")):
            return False
        raise XoError(f"./{dest}: {e}") from None
    return True


def has(dest: Path, ref: str) -> bool:
    """Whether ref names a commit in the checkout."""
    return _commit(dest, ref) is not None


def holds(dest: Path, ref: str) -> bool:
    """Whether HEAD already holds every commit of ref (it is ref, or ref is behind it)."""
    return run("merge-base", "--is-ancestor", ref, "HEAD", cwd=dest, check=False) is not None


def on_remote(dest: Path, remote: str) -> bool:
    """Whether a branch of remote (as last fetched) already holds HEAD."""
    out = run("for-each-ref", "--count=1", "--contains", "HEAD", "--format=%(refname)",
              f"refs/remotes/{remote}/", cwd=dest, check=False)
    return bool(out and out.strip())


def ahead(dest: Path, theirs: str) -> bool:
    """Whether HEAD has commits ref theirs lacks (or theirs is not there at all)."""
    tip = _commit(dest, theirs)
    return tip is None or run("merge-base", "--is-ancestor", "HEAD", tip, cwd=dest, check=False) is None


def push_quirq(dest: Path, name: str) -> None:
    """Push HEAD to branch name on the quirq remote: never forced, so the hub refuses if it is ahead."""
    try:
        run("push", "--quiet", "--end-of-options", QUIRQ, f"HEAD:refs/heads/{name}", cwd=dest, hub=True)
    except XoError as e:
        ahead_ = any(w in getattr(e, "detail", "") for w in ("[rejected]", "non-fast-forward", "fetch first"))
        raise XoError(f"./{dest}: {e}" + (" (the hub is ahead: run xo sync)" if ahead_ else "")) from None


def rebase_on(dest: Path, theirs: str) -> None:
    """Rebase the current branch on ref theirs. On a conflict the rebase is undone, so the checkout
    is as it was, and the error says so."""
    name = branch(dest)
    try:
        run("rebase", "--quiet", "--end-of-options", theirs, cwd=dest)
    except XoError as e:
        if run("rebase", "--abort", cwd=dest, check=False) is not None:
            raise XoError(f"./{dest}: your branch {name} and {theirs.removeprefix('refs/remotes/')} change the same "
                          "lines, so xo stopped and left it as it was; rebase it by hand (git rebase "
                          f"{theirs.removeprefix('refs/remotes/')} in that folder)") from None
        raise XoError(f"./{dest}: {e}") from None
