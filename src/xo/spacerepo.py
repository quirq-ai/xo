"""`xo create space NAME` with no KIND: the folder it runs in becomes a space, a git repository on
GitHub with one branch per git repository under it.

Every git repository under the current folder (at any depth, but not one inside another) becomes a
branch called after its path here (`api`, or `work/api`), holding that repository's files as they
are now, not its history (its .gitignore and this folder's followed, secrets and workflows left
out). Folders that are not git repositories and hold none, and files outside the repositories, are
not part of the space. `main` is the index: a README table of the branches, xo-space.json with each
branch's commit and one hash over all of them, and an update script. The space's repository is .git
here, made bare so that nothing in this folder is ever checked out, written over or shown as
changed; it is built with git's plumbing only (a scratch index and commit-tree), and the
repositories' own .git, remotes and branches are never touched.

Its remote is github.com/OWNER/NAME (OWNER is your gh login unless --owner says otherwise). When
GitHub does not have it, xo gives the link to make it and waits, then pushes main and every branch
with your own git login. xo never reads, stores or prints a token: whether the repository exists is
asked anonymously (no credential helper, askpass, .netrc or extra header), so a private one looks
missing until the push itself, and a repository GitHub redirects to (renamed or moved) is refused.

Running it again makes a new commit only for a repository that changed and pushes again, never forced.
A commit is always made on top of what GitHub already has, never on one made here that was not
pushed, so files left out before the first push never reach GitHub in the history; what GitHub has
is asked each run, so a repository deleted and made again starts over. A repository that is gone keeps
the branch GitHub has of it (xo deletes nothing); one never pushed is dropped. Only main and the
branches main lists are pushed.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import threading
from fnmatch import fnmatchcase
from pathlib import Path
from typing import Callable, Iterator

from xo import gitops
from xo.client import XoError

FILE = "xo-space.json"
SCHEMA = "xo-space/1"
MAIN = "main"
MARK = "xo.space"  # set in .git/config when xo makes the repository, so a run that stopped early can run again
FILE_CAP = 100 * 1024 * 1024  # GitHub refuses any file larger
# Never copied, whatever any .gitignore says (a negation there cannot bring them back), at any depth
# and in any case: environment files, keys and credential stores. A pattern ending in / is a folder.
NEVER = [".env", ".env.*", ".env-*", ".env_*", "*.env", ".envrc", "*.pem", "*.key", "*.p12", "*.pfx", "*.jks",
         "*.keystore", "*.ppk", "id_rsa*", "id_dsa*", "id_ecdsa*", "id_ed25519*", ".npmrc", ".pypirc", ".netrc",
         "_netrc", ".git-credentials", "git/credentials", "gh/hosts.yml", ".pgpass", ".my.cnf", ".s3cfg", ".boto",
         ".vault-token", "*.kdbx", ".aws/", ".ssh/", ".gnupg/", ".azure/", ".kube/", ".docker/", ".password-store/",
         ".config/gcloud/", "credentials.json", "service-account*.json", "application_default_credentials.json",
         "*.tfstate", "*.tfstate.*", ".terraform/", "node_modules/", ".DS_Store"]
# What a committed secret looks like, in a file's bytes or its name; one stops everything before
# anything is pushed. (?P<pw>) is a password in a URL, skipped when it is a placeholder.
SECRET = re.compile(
    rb"-----BEGIN (?:[A-Z0-9]+ )*PRIVATE KEY(?: BLOCK)?-----|PuTTY-User-Key-File-\d"
    rb"|\bgh[pousr]_[A-Za-z0-9]{36,}|\bgithub_pat_[A-Za-z0-9_]{60,}"
    rb"|\b(?:AKIA|ASIA)[0-9A-Z]{16}\b|(?i:aws_secret_access_key)\W{1,4}[A-Za-z0-9/+]{40}"
    rb"|\bxox[abprs]-[A-Za-z0-9-]{10,}|\bsk-(?:ant-|proj-)?[A-Za-z0-9_-]{32,}"
    rb"|\b[rs]k_(?:live|test)_[A-Za-z0-9]{24,}|\bsk_[a-f0-9]{48}\b"
    rb"|\bAIza[0-9A-Za-z_-]{35}|\bglpat-[A-Za-z0-9_-]{20,}|\bak_[A-Za-z0-9]{24,}"
    rb"|\bnpm_[A-Za-z0-9]{36}\b|\bhf_[A-Za-z0-9]{30,}|\bpypi-AgE[A-Za-z0-9_-]{50,}"
    rb"|\b[a-z][a-z0-9+.-]{1,20}://[^\s/:@'\"<>]{1,64}:(?P<pw>[^\s/@'\"<>]{3,128})@[A-Za-z0-9.-]+")
_PLACEHOLDER = re.compile(rb"(?i)[$<{%*]|^(?:pass(?:word|wd)?|pwd|secret|changeme|x+|\.+)$|example")
# Folders that hold keys or a whole home: never made a space, and never a branch.
_KEYS = (".ssh", ".aws", ".gnupg", ".kube", ".docker", ".azure", ".password-store", ".config/gcloud")
# Folders in a home folder where programs keep settings and logins (besides any hidden one).
_SETTINGS = ("Library", "AppData")
_BRANCH = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,99}")  # one part of a branch name (a folder's name)
_DEPTH = 6  # how deep xo looks for git repositories
_SKIP = ("node_modules", "venv", "__pycache__")  # folders never looked into for git repositories
_IDENTITY = ["-c", "user.name=xo", "-c", "user.email=xo@localhost", "-c", "commit.gpgsign=false",
             "-c", f"core.hooksPath={os.devnull}"]


def _say(text: str) -> None:
    print(f"xo: {text}", file=sys.stderr)


def _z(paths: list[str]) -> bytes:
    """Paths for git's -z --stdin, as the bytes git gave them (surrogateescape keeps any name)."""
    return b"".join(p.encode("utf-8", "surrogateescape") + b"\0" for p in paths)


class Space:
    """The space's repository: ./.git here, bare, with origin at GitHub."""

    def __init__(self, here: Path) -> None:
        self.here = here
        self.repo = here / ".git"

    def git(self, *args: str, tree: Path | None = None, check: bool = True, config: tuple = (),
            input: bytes | None = None, raw: bool = False) -> str | None:
        where = ["--git-dir", str(self.repo)]
        if tree is not None:  # the folder a branch is made from: read only, never written
            where += ["--work-tree", str(tree), "-c", "core.bare=false"]
        return gitops.run(*config, *where, *args, cwd=self.here, check=check, input=input, raw=raw)

    def ours(self) -> bool:
        """Whether ./.git is a space repository xo made (bare, and marked so or its main holds xo-space.json)."""
        if not self.repo.is_dir():
            return False
        bare = self.git("config", "--get", "core.bare", check=False)
        return (bare or "").strip() == "true" and (
            self.git("config", "--get", MARK, check=False) is not None
            or self.git("cat-file", "-e", f"refs/heads/{MAIN}:{FILE}", check=False) is not None)


def _homes() -> set[Path]:
    """Your home folder, as $HOME says and (where there is one) as the system's user list says."""
    homes = {Path.home().resolve()}
    try:
        import pwd
        homes.add(Path(pwd.getpwuid(os.getuid()).pw_dir).resolve())
    except (ImportError, KeyError, OSError, AttributeError):
        pass
    return homes


def refuse_here(here: Path) -> None:
    """A space is never made in a home folder, the root, a folder that holds keys or is in one (or in
    any hidden or settings folder of your home), or inside another git repository."""
    real = here.resolve()
    for home in _homes():
        if real == home or real == Path(real.anchor) or real in home.parents:
            raise XoError(f"{real} is your home folder or above it; run xo create space inside the folder that "
                          "holds the git repositories for the space")
        if home in real.parents:
            top = real.relative_to(home).parts[0]
            if top.startswith(".") or top in _SETTINGS:
                raise XoError(f"{real} is {'' if real == home / top else 'in '}{home / top}, where programs keep "
                              "settings, logins and keys, which must never be pushed; run xo create space inside "
                              "the folder that holds the git repositories for the space")
    held = [k for k in _KEYS if (here / k).exists()]
    if held:
        raise XoError(f"{real} holds {', '.join(held)}, which must never be pushed; run xo create space inside "
                      "the folder that holds the git repositories for the space")
    if (gitops.run("rev-parse", "--is-inside-git-dir", cwd=here, check=False) or "").strip() == "true":
        raise XoError(f"{real} is inside a git repository's own .git folder (or a bare repository), whose files "
                      "hold all of its history; run xo create space in a folder outside it")
    top = gitops.run("rev-parse", "--show-toplevel", cwd=here, check=False)
    if top is not None:
        if Path(top.strip()).resolve() == real:
            raise XoError(f"{real} is already a git repository (./.git); xo makes a space's repository only where "
                          "there is none, so run it in another folder")
        raise XoError(f"{real} is inside the git repository {Path(top.strip())}; a .git here would hide that "
                      "repository from git in these folders, so run xo create space in a folder outside it")


class _HereIgnore:
    """This folder's own .gitignore, read by git as if this folder were one repository (so the folders'
    .gitignore files count too, as git would count them)."""

    def __init__(self, here: Path) -> None:
        self.here = here
        self.on = (here / ".gitignore").is_file()
        self.tmp: str | None = None

    def ignored(self, paths: list[str]) -> set[str]:
        if not (self.on and paths):
            return set()
        if self.tmp is None:
            self.tmp = tempfile.mkdtemp(prefix="xo-space-")
            gitops.run("init", "--quiet", "--bare", "--end-of-options", self.tmp)
        try:
            out = gitops.run("--git-dir", self.tmp, "--work-tree", str(self.here), "-c", "core.bare=false",
                             "check-ignore", "--no-index", "-z", "--stdin", cwd=self.here, input=_z(paths), raw=True)
        except XoError as e:
            if getattr(e, "code", None) == 1:  # none of them is ignored
                return set()
            raise
        return {p for p in (out or "").split("\0") if p}

    def close(self) -> None:
        if self.tmp is not None:
            shutil.rmtree(self.tmp, ignore_errors=True)


def _is_repo(folder: Path) -> bool:
    """A git repository's work tree: .git is a folder (a clone) or a file (a worktree or submodule)."""
    return (folder / ".git").is_dir() or (folder / ".git").is_file()


def _branch_ok(branch: str) -> bool:
    """A branch name xo makes: each part a folder name git and GitHub both take."""
    return all(_BRANCH.fullmatch(part) and ".." not in part and part.upper() != "HEAD"
               and not part.endswith(("_HEAD", ".lock", ".")) for part in branch.split("/")) and \
        branch.split("/")[0].casefold() != MAIN and len(branch) <= 200


def _inside(a: str, b: str) -> bool:
    """Whether branch b sits inside branch a's name (a/...), in any case: git cannot hold both."""
    return b.casefold().startswith(a.casefold() + "/")


def folders(here: Path, probe: _HereIgnore | None = None) -> tuple[dict[str, Path], list[str]]:
    """The git repositories that become branches, {branch: folder}: every repository under here (not
    one inside another, nor in hidden folders, links, node_modules or what this folder's .gitignore
    leaves out); and the folders left out by that .gitignore."""
    found: dict[str, Path] = {}
    skipped: list[str] = []

    def look(at: Path, rel: str, depth: int) -> None:
        kids = sorted((p for p in at.iterdir() if p.is_dir() and not p.is_symlink() and not p.name.startswith(".")
                       and p.name not in _SKIP), key=lambda p: p.name.casefold())
        names = [f"{rel}{p.name}/" for p in kids]
        ignored = probe.ignored(names) if probe else set()
        for p, name in zip(kids, names):
            if name in ignored:
                skipped.append(name.rstrip("/"))
            elif _is_repo(p):
                found[name.rstrip("/")] = p
            elif depth < _DEPTH:
                look(p, name, depth + 1)

    look(here, "", 1)
    bad = [b for b in found if not _branch_ok(b)]
    if bad:
        raise XoError("these git repositories cannot be branch names (each folder name letters, digits, . _ -, "
                      f"starting with a letter or digit, no .., not HEAD, and not {MAIN} or in a folder {MAIN}): "
                      + ", ".join(bad) +
                      "; rename them or run elsewhere")
    seen: dict[str, str] = {}
    for b in found:
        if b.casefold() in seen:
            raise XoError(f"{seen[b.casefold()]} and {b} differ only in case, which GitHub cannot keep "
                          "apart as branches; rename one")
        seen[b.casefold()] = b
    inside = sorted(f"{a} and {b}" for a in found for b in found if _inside(a, b))
    if inside:
        raise XoError("one git repository's branch would sit inside another's name, which git cannot hold (the "
                      "folders differ only in case): " + ", ".join(inside) + "; rename one")
    return found, skipped


def size(folder: Path) -> int:
    """Bytes of the folder's files, not counting .git folders (a quick count, before .gitignore)."""
    total = 0
    for root, dirs, files in os.walk(folder):
        dirs[:] = [d for d in dirs if d != ".git" and not os.path.islink(os.path.join(root, d))]
        for f in files:
            try:
                total += os.lstat(os.path.join(root, f)).st_size
            except OSError:
                pass
    return total


def shown(n: int) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024 or unit == "GB":
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024
    return f"{n} B"


def _never(path: str) -> bool:
    """Whether a path (with /) is one NEVER leaves out: a file name anywhere, a run of folders and a
    file name at its end, or a folder (or run of folders) anywhere above it; case is ignored."""
    parts = path.casefold().split("/")
    for pattern in NEVER:
        want = pattern.casefold().rstrip("/").split("/")
        if pattern.endswith("/"):
            hay = parts[:-1]
            starts = range(len(hay) - len(want) + 1)
        else:
            hay = parts
            starts = range(len(hay) - len(want), len(hay) - len(want) + 1) if len(hay) >= len(want) else range(0)
        if any(all(fnmatchcase(hay[i + j], w) for j, w in enumerate(want)) for i in starts):
            return True
    return False


def _exclude_lines() -> list[str]:
    """NEVER as git exclude patterns, matching at any depth (so those files are not even read)."""
    return [("**/" + p) if "/" in p.rstrip("/") else p for p in NEVER]


def _start(space: Space, name: str) -> None:
    if space.repo.exists() and not space.ours():
        raise XoError(f"{space.here.resolve()} is already a git repository (./.git); xo makes a space's "
                      "repository only where there is none, so run it in another folder")
    if not space.repo.exists():
        gitops.run("init", "--quiet", "--bare", "--object-format=sha1", f"--initial-branch={MAIN}",
                   "--end-of-options", str(space.repo))
        space.git("config", MARK, name)
    (space.repo / "info").mkdir(exist_ok=True)


def _tip(space: Space, ref: str) -> str | None:
    out = space.git("rev-parse", "--verify", "--quiet", f"{ref}^{{commit}}", check=False)
    return out.strip() if out else None


def _commit(space: Space, branch: str, tree: str, message: str) -> str:
    """A commit of tree on branch, on top of what GitHub has of it (origin's branch as last pushed), never
    on a commit made here that was not pushed: that one could hold files left out since. None is made
    when nothing changed."""
    tip = _tip(space, f"refs/heads/{branch}")
    base = _tip(space, f"refs/remotes/origin/{branch}")

    def tree_of(commit: str) -> str:
        return space.git("rev-parse", f"{commit}^{{tree}}").strip()

    if tip is not None and tree_of(tip) == tree:
        if tip == base or space.git("rev-list", "--parents", "-n", "1", tip).split()[1:] == ([base] if base else []):
            return tip
    if base is not None and tree_of(base) == tree:
        sha = base
    else:
        sha = space.git("commit-tree", tree, *(["-p", base] if base else []), "-m", message,
                        config=tuple(_IDENTITY)).strip()
    if sha != tip:
        space.git("update-ref", f"refs/heads/{branch}", sha, tip or "")
    return sha


def _staged(space: Space, folder: Path) -> list[tuple[str, str]]:
    out = space.git("ls-files", "-z", "--stage", tree=folder, raw=True) or ""
    return [(meta, path) for meta, _, path in (rec.partition("\t") for rec in out.split("\0") if rec)]


def _snapshot(space: Space, branch: str, folder: Path, probe: _HereIgnore | None = None) -> tuple[str, list[str]]:
    """The tree of folder's files, built in a scratch index: its .gitignore (and this folder's, and a
    clone's own .git/info/exclude) followed, NEVER left out, workflows and nested repositories dropped.
    The tree and what was left out."""
    index = space.repo / "index"
    index.unlink(missing_ok=True)
    (space.repo / "index.lock").unlink(missing_ok=True)  # left by a run that was stopped mid-way
    exclude = _exclude_lines()
    own = None
    if (folder / ".git").exists():  # a clone: its own exclude, also where .git is a file (worktree, submodule)
        at = gitops.run("rev-parse", "--git-path", "info/exclude", cwd=folder, check=False)
        own = folder / at.strip() if at and at.strip() else None
    if own is not None and own.is_file():
        exclude += ["# the folder's own .git/info/exclude", *own.read_text(errors="replace").splitlines()]
    (space.repo / "info" / "exclude").write_text("\n".join(exclude) + "\n")
    # No filters (git-lfs and the like): the files go in as they are on disk.
    plain = ("-c", "core.autocrlf=false", "-c", "core.safecrlf=false", "-c", "filter.lfs.required=false",
             "-c", "filter.lfs.clean=", "-c", "filter.lfs.process=")
    try:
        try:
            space.git("add", "--all", "--", ".", tree=folder, config=plain)
        except XoError as e:
            if "does not have a commit checked out" in (getattr(e, "detail", "") or ""):
                raise XoError(f"{branch}: it holds a git repository with no commit yet; commit in it or move "
                              "it, then run again. Nothing was pushed") from None
            raise
        dropped, drop, flows, never = [], [], False, 0
        staged = _staged(space, folder)
        for meta, path in staged:
            if meta.startswith("160000"):  # a repository inside the folder: its files are not here
                drop.append(path)
                dropped.append(f"{path} (a git repository of its own)")
            elif path == ".github/workflows" or path.startswith(".github/workflows/"):
                drop.append(path)
                flows = True
            elif _never(path):
                drop.append(path)
                never += 1
        if flows:
            dropped.append(".github/workflows (GitHub would run them on the space's repository)")
        if never:
            dropped.append(f"{never} file(s) xo never pushes (.env files, keys, credential stores)")
        gone = set(drop)
        keep = [path for _, path in staged if path not in gone]
        here_ignored = probe.ignored([f"{branch}/{p}" for p in keep]) if probe else set()
        if here_ignored:
            out = [p for p in keep if f"{branch}/{p}" in here_ignored]
            drop += out
            dropped.append(f"{len(out)} file(s) this folder's .gitignore leaves out")
            keep = [p for p in keep if f"{branch}/{p}" not in here_ignored]
        if drop:
            space.git("update-index", "--force-remove", "-z", "--stdin", tree=folder, input=_z(drop))
        _refuse_encrypted(space, branch, folder, keep)
        _refuse_bare(branch, folder, keep)
        tree = space.git("write-tree", tree=folder).strip()
    finally:
        index.unlink(missing_ok=True)
    return tree, dropped


def _refuse_encrypted(space: Space, branch: str, folder: Path, paths: list[str]) -> None:
    """Stop on files a git filter keeps encrypted in their repository (git-crypt, transcrypt): here
    they are decrypted, and xo would push them so."""
    if not paths:
        return
    out = space.git("check-attr", "-z", "--stdin", "filter", tree=folder, input=_z(paths), raw=True) or ""
    fields = out.split("\0")
    bad = [fields[i] for i in range(0, len(fields) - 2, 3)
           if fields[i + 2] not in ("unspecified", "unset", "set", "lfs")]
    if bad:
        raise XoError(f"{branch}: " + ", ".join(bad[:10]) + " go through a git filter (such as git-crypt) that "
                      "keeps them encrypted in their repository; here they are decrypted, and xo would push them "
                      "so. Add them to its .gitignore. Nothing was pushed")


def _refuse_bare(branch: str, folder: Path, paths: list[str]) -> None:
    """Stop on a bare git repository among the files: pushing it would publish its whole history."""
    for p in paths:
        if p == "HEAD" or p.endswith("/HEAD"):
            where = folder / p[:-4] if p != "HEAD" else folder
            if (where / "objects").is_dir() and (where / "config").is_file():
                shown_at = p[:-5] or "the folder itself"
                raise XoError(f"{branch}: {shown_at} is a git repository (a bare one), and pushing it would "
                              "publish its whole history; move it out or add it to its .gitignore. Nothing was "
                              "pushed")


def _blobs(space: Space, shas: list[str]) -> Iterator[tuple[str, bytes]]:
    """(sha, bytes) of each blob, read with one git cat-file --batch."""
    if not shas:
        return
    proc = subprocess.Popen(["git", *gitops.SAFE, "--git-dir", str(space.repo), "cat-file", "--batch"],
                            cwd=space.here, env=gitops.environment(), stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                            stderr=subprocess.DEVNULL)

    def feed() -> None:
        try:
            proc.stdin.write("".join(s + "\n" for s in shas).encode())
            proc.stdin.close()
        except OSError:
            pass
    writer = threading.Thread(target=feed, daemon=True)
    writer.start()
    try:
        for _ in shas:
            head = proc.stdout.readline().split()
            if len(head) != 3 or head[1] != b"blob":
                raise XoError("git cat-file gave an answer xo cannot read; nothing was pushed")
            size_ = int(head[2])
            data = proc.stdout.read(size_)
            proc.stdout.read(1)
            if len(data) != size_:
                raise XoError("git cat-file stopped early; nothing was pushed")
            yield head[0].decode(), data
    finally:
        proc.stdout.close()
        writer.join()
        proc.wait()


def _secret(data: bytes) -> bool:
    """Whether data holds a secret; text in UTF-16 (as Windows PowerShell writes it) is read with its
    NUL bytes taken out."""
    for text in (data, data.replace(b"\0", b"")) if b"\0" in data else (data,):
        for m in SECRET.finditer(text):
            if m.group("pw") is None or not _PLACEHOLDER.search(m.group("pw")):
                return True
    return False


def _check(space: Space, name: str, tree: str) -> None:
    """Stop before anything is pushed: a file GitHub refuses, a git-lfs pointer, or a secret in any
    file (text or not) or in a file's name. If the check itself fails, nothing is pushed."""
    entries = []
    for rec in (space.git("ls-tree", "-r", "-l", "-z", "--full-tree", tree, raw=True) or "").split("\0"):
        meta, _, path = rec.partition("\t")
        parts = meta.split()
        if len(parts) == 4 and parts[1] == "blob" and parts[3].isdigit():
            entries.append((path, parts[2], int(parts[3])))
    big = [f"{p} ({shown(n)})" for p, _, n in entries if n > FILE_CAP]
    if big:
        raise XoError(f"{name}: GitHub takes no file over {shown(FILE_CAP)}: " + ", ".join(big) +
                      "; add them to its .gitignore. Nothing was pushed")
    paths: dict[str, list[str]] = {}
    for p, sha, _ in entries:
        paths.setdefault(sha, []).append(p)
    lfs, found = [], [p for p, _, _ in entries if _secret(p.encode("utf-8", "surrogateescape"))]
    for sha, data in _blobs(space, list(paths)):
        if len(data) < 200 and data.startswith(b"version https://git-lfs.github.com/spec/"):
            lfs += paths[sha]
        if _secret(data):
            found += paths[sha]
    if lfs:
        raise XoError(f"{name}: git-lfs pointers, whose data would stay behind: " + ", ".join(lfs[:10]) +
                      "; nothing was pushed")
    if found:
        raise XoError(f"{name}: these files look like they hold a secret (a key, token or password), in them or in "
                      "their name: " + ", ".join(sorted(set(found))[:10]) + "; take it out or add them to its "
                      ".gitignore. Nothing was pushed")


def digest(branches: dict[str, dict]) -> str:
    """The space's hash: sha256 over each branch's tree id, written one way. A tree id is git's hash of
    every file under it, so this one hash covers all of the space's data."""
    text = json.dumps({b: e["tree"] for b, e in branches.items()}, sort_keys=True, separators=(",", ":"))
    return "sha256:" + hashlib.sha256(text.encode()).hexdigest()


def _index(space: Space, name: str, owner: str, branches: dict[str, dict]) -> str:
    """main's tree: README.md, xo-space.json and update, written as blobs (nothing on disk). Bytes in and
    -z for mktree, so no line ending is ever changed on the way (Windows turns \\n into \\r\\n in text)."""
    hashed = digest(branches)
    record = {"schema": SCHEMA, "space": name, "github": f"{owner}/{name}", "hash": hashed, "branches": branches}
    rows = "\n".join(f"| [{b}](../../tree/{b}) | {e['files']} | {shown(e['bytes'])} | `{e['commit'][:7]}` |"
                     for b, e in branches.items())
    readme = (f"# {name}\n\nAn xo space: each branch is one git repository's files, as they were when `xo create "
              f"space {name}` last ran.\n\n| Branch | Files | Size | Commit |\n|---|---|---|---|\n{rows}\n\nHash of all of it: "
              f"`{hashed}` (sha256 over each branch's git tree id; see {FILE}).\n\nTo update it, run `xo create "
              f"space {name}` again in the folder that holds these repositories (or `sh update` there, with this "
              "branch's `update` copied in).\n")
    script = ("#!/bin/sh\n# Snapshot every git repository here again and push the branches that changed (xo create "
              f"space).\nexec xo create space {name} --owner {owner} \"$@\"\n")

    def blob(text: str) -> str:
        return space.git("hash-object", "-w", "--stdin", input=text.encode()).strip()

    entries = (f"100644 blob {blob(readme)}\tREADME.md\0"
               f"100755 blob {blob(script)}\tupdate\0"
               f"100644 blob {blob(json.dumps(record, indent=2, sort_keys=True) + chr(10))}\t{FILE}\0")
    return space.git("mktree", "-z", input=entries.encode()).strip()


def _entry(space: Space, commit: str) -> dict:
    tree = space.git("rev-parse", f"{commit}^{{tree}}").strip()
    listing = space.git("ls-tree", "-r", "-l", "--full-tree", tree) or ""
    sizes = [int(p[3]) for p in (line.partition("\t")[0].split() for line in listing.splitlines())
             if len(p) == 4 and p[3].isdigit()]
    return {"commit": commit, "tree": tree, "files": len(sizes), "bytes": sum(sizes)}


def _sync_origin(space: Space, remote: dict[str, str]) -> None:
    """Make origin's branches here exactly what GitHub has now (remote), so nothing is built on, or
    kept from, a repository that was deleted, made again or is another one."""
    have = space.git("for-each-ref", "--format=%(objectname) %(refname)", "refs/remotes/origin/") or ""
    for line in have.splitlines():
        sha, _, ref = line.partition(" ")
        if remote.get(ref.removeprefix("refs/remotes/origin/")) != sha:
            space.git("update-ref", "-d", ref, sha)
    for branch, sha in remote.items():
        if _branch_ok(branch) and space.git("cat-file", "-e", f"{sha}^{{commit}}", check=False) is not None:
            space.git("update-ref", f"refs/remotes/origin/{branch}", sha)


def build(here: Path, name: str, owner: str, chosen: dict[str, Path], probe: _HereIgnore | None = None,
          remote: dict[str, str] | None = None) -> dict:
    """Make or update the space's repository from the repositories ({branch: folder}), all checked before
    main moves. remote:
    the branches GitHub has now (none when it has no such public repository)."""
    space = Space(here)
    _start(space, name)
    if remote is not None:
        _sync_origin(space, remote)
    trees = {}
    for branch, folder in chosen.items():
        tree, dropped = _snapshot(space, branch, folder, probe)
        _check(space, branch, tree)
        for d in dropped:
            _say(f"{branch}: left out {d}")
        trees[branch] = tree
    old = _old_branches(space)
    clash = {b.casefold(): b for b in old if b not in trees and b.casefold() in {t.casefold() for t in trees}}
    if clash:
        raise XoError("these repositories differ only in case from a branch the space already has: " +
                      ", ".join(clash.values()) + "; GitHub cannot keep them apart, so rename the folder back")
    kept = {}
    for gone in sorted(set(old) - set(trees), key=str.casefold):
        base = _tip(space, f"refs/remotes/origin/{gone}") if _branch_ok(gone) else None
        if base is None:  # never pushed, or not on GitHub now: it goes nowhere
            space.git("update-ref", "-d", f"refs/heads/{gone}", check=False)
            _say(f"{gone}: no such git repository here now, and GitHub does not have its branch; left out")
            continue
        kept[gone] = base
    nested = sorted(f"{o} and {n}" for o in kept for n in trees if _inside(o, n) or _inside(n, o))
    if nested:
        raise XoError("a branch cannot sit inside another branch's name, as these would: " + ", ".join(nested) +
                      "; GitHub has the first and xo deletes nothing, so delete that branch on GitHub (or move "
                      "the repository), then run again. Nothing was pushed")
    branches = {}
    for branch in chosen:
        sha = _commit(space, branch, trees[branch], f"{branch}: files as of xo create space {name}")
        branches[branch] = _entry(space, sha)
    for gone, base in kept.items():
        space.git("update-ref", f"refs/heads/{gone}", base)
        _say(f"{gone}: no such git repository here now; its branch is kept as GitHub has it")
        branches[gone] = _entry(space, base)
    branches = dict(sorted(branches.items(), key=lambda kv: kv[0].casefold()))
    tree = _index(space, name, owner, branches)
    commit = _commit(space, MAIN, tree, f"xo create space {name}: {digest(branches)}")
    return {"space": name, "github": f"{owner}/{name}", "hash": digest(branches), "main": commit,
            "branches": {b: e["commit"] for b, e in branches.items()}}


def _old_branches(space: Space) -> dict[str, dict]:
    out = space.git("cat-file", "blob", f"refs/heads/{MAIN}:{FILE}", check=False)
    try:
        record = json.loads(out) if out else {}
    except ValueError:
        return {}
    found = record.get("branches") if isinstance(record, dict) else None
    return found if isinstance(found, dict) else {}


def on_github(owner: str, name: str) -> bool:
    """Whether github.com/OWNER/NAME answers someone with no login at all (see heads)."""
    return heads(owner, name) is not None


def heads(owner: str, name: str) -> dict[str, str] | None:
    """The branches github.com/OWNER/NAME has ({name: sha}), asked with no login at all: no credential
    helper, askpass, .netrc, extra header or user config (so a private one looks missing: None). A
    repository GitHub redirects to (renamed or moved, maybe to another owner) is refused: xo pushes
    only to the one it names."""
    empty = tempfile.mkdtemp(prefix="xo-anon-")
    env = {"GIT_TERMINAL_PROMPT": "0", "GIT_ASKPASS": "", "SSH_ASKPASS": "", "HOME": empty,
           "XDG_CONFIG_HOME": empty, "GIT_CONFIG_GLOBAL": os.devnull}
    try:
        out = gitops.run("-c", "credential.helper=", "-c", "core.askPass=", "-c", "http.extraHeader=",
                         "-c", "http.followRedirects=false", "ls-remote", "--heads", "--end-of-options",
                         f"{gitops.GITHUB}/{owner}/{name}", cwd=Path(empty), env=env)
    except XoError as e:
        if re.search(r"returned error: 30[1278]", getattr(e, "detail", "") or ""):
            raise XoError(f"GitHub sends github.com/{owner}/{name} on to another repository (it was renamed or moved); "
                          "xo pushes only to the repository it names, so pick another NAME") from None
        return None
    finally:
        shutil.rmtree(empty, ignore_errors=True)
    found = {}
    for line in (out or "").splitlines():
        sha, _, ref = line.partition("\t")
        if ref.startswith("refs/heads/"):
            found[ref.removeprefix("refs/heads/")] = sha.strip()
    return found


def push(here: Path, owner: str, name: str, branches: list[str]) -> None:
    """main and the space's branches to github.com/OWNER/NAME with your own git login, never forced, and
    never to a repository GitHub redirects to. No other branch here is pushed."""
    space = Space(here)
    url = f"{gitops.GITHUB}/{owner}/{name}"
    if space.git("remote", "get-url", "origin", check=False) is None:
        space.git("remote", "add", "origin", url)
    else:
        space.git("remote", "set-url", "origin", url)
    try:
        space.git("push", "--atomic", "--porcelain", "origin",
                  *(f"refs/heads/{b}:refs/heads/{b}" for b in [MAIN, *branches]),
                  config=("-c", "http.followRedirects=false"))
    except XoError as e:
        why = getattr(e, "detail", "") or ""
        if "Updates were rejected" in why or "non-fast-forward" in why:
            raise XoError(f"github.com/{owner}/{name} has commits xo did not make (a README or license made with it, "
                          "or an edit on GitHub) or is private (xo builds only on what anyone can see there), so "
                          "nothing was pushed; make the repository empty and public, or pick another NAME") from None
        raise


def create(here: Path, name: str, owner: str, ask: Callable[[str], bool], wait: Callable[[str], bool]) -> dict:
    """The whole command after the name checks: refuse, show, ask, build, then push."""
    refuse_here(here)
    probe = _HereIgnore(here)
    try:
        chosen, skipped = folders(here, probe)
        if not chosen:
            raise XoError(f"{here.resolve()} has no git repositories in it to make branches of; run xo create space "
                          "inside the folder that holds them")
        now = heads(owner, name)
        exists = now is not None
        lines = [f"  {b:<30} {shown(size(f)):>10}" for b, f in chosen.items()]
        _say(f"xo create space {name} makes {here.resolve()} a space: each git repository here becomes its own branch "
             f"of github.com/{owner}/{name} (its files as they are now, not its history), and main lists them.\n"
             + "\n".join(lines) + "\n"
             + (f"  Left out by ./.gitignore: {', '.join(skipped)}.\n" if skipped else "")
             + "  Folders that are not git repositories (and hold none), files outside the repositories, hidden "
             f"folders, node_modules and venv, and repositories more than {_DEPTH} folders down are not part of the "
             "space. Each repository's .gitignore (and this folder's) is followed; .env "
             "files, keys, credential stores, repositories inside a repository and .github/workflows are left out, "
             "and a file that looks like it holds a key, token or password stops it before anything is pushed. xo "
             "cannot find every secret (one in an archive, say), so look through these repositories first. "
             "Everything else is pushed to "
             + (f"the repository github.com/{owner}/{name}, which is already there and public."
                if exists else f"a new public repository github.com/{owner}/{name}, which xo asks you to make.")
             + " Anyone can read it, and what is pushed stays public. Nothing in these folders is changed.")
        if not ask("Make this folder the space?"):
            raise XoError("nothing was made. To make a space of other git repositories, run xo create space inside "
                          "the folder that holds them")
        made = build(here, name, owner, chosen, probe, now or {})
    finally:
        probe.close()
    _say(f"./.git: {len(made['branches'])} branches and main, {made['hash']}")
    if not on_github(owner, name):
        link = f"https://github.com/new?name={name}&owner={owner}&visibility=public"
        _say(f"github.com/{owner}/{name} does not exist yet (or is private). Make it here, empty (no README, license "
             f"or .gitignore):\n  {link}")
        if not wait("Press Enter once it exists, or n to stop here"):
            _say(f"not pushed; run xo create space {name} here again once it exists")
            made["pushed"] = False
            return made
    push(here, owner, name, list(made["branches"]))
    _say(f"pushed main and {len(made['branches'])} branches to https://github.com/{owner}/{name}")
    made["pushed"] = True
    return made
