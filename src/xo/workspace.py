"""`xo install`, `xo update` and `xo sync`: a space's projects on this machine, kept in step with
the space recorded in your private metadata.

    xo install space:S    ./S/<folder> for each project, at its ref (a GitHub project's folder is
                          its repo name: juanfont/headscale is ./S/headscale, or ./S/juanfont/headscale
                          when another project there has that name); ./S/.xo/state.json remembers
                          the space's rev and refs as installed
    xo update space:S     push each checkout's branch to its upstream (refused if the remote is
                          ahead), then record each checkout's branch (or commit) in the space, if the
                          space is still as installed; refused otherwise: run xo sync
    xo sync space:S       rebase each checkout's branch on its upstream and push it, take the
                          space's changes, keep yours, stop on a project both changed, then record
                          the result
    xo install project:P  ./headscale cloned from GitHub, or fetched and fast-forwarded (P is
                          juanfont/headscale or, when only one of your projects is called that,
                          headscale)
    xo update project:P   git push of P's checkout here to its upstream, never forced
    xo sync project:P     git pull --rebase, then that push

A GitHub project's checkout is looked for where this xo puts it and where an older one did
(./OWNER/REPO), and is only used when its origin is that repo.

The metadata is only ever changed by the xo server; this module reads it and sends refs.
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from typing import Any
from urllib.parse import quote

from xo import gitops, names
from xo.client import Client, XoError

STATE = Path(".xo") / "state.json"


def target(text: str) -> tuple[str, str]:
    kind, _, name = text.partition(":")
    if kind == "project" and names.project_ok(name):
        return kind, name
    if kind == "space" and names.space_ok(name):
        return kind, name
    raise XoError("name it project:NAME or space:NAME (" + (names.SPACE_RULE if kind == "space" else names.PROJECT_RULE)
                  + ")")


def _say(text: str) -> None:
    print(f"xo: {text}", file=sys.stderr)


def _data(answer: Any, field: str) -> Any:
    found = answer.get("data", {}).get(field) if isinstance(answer, dict) and isinstance(answer.get("data"), dict) \
        else None
    if found is None:
        raise XoError(f"the xo server did not answer with {field} (is it older than this command?)")
    return found


def _pairs(entries: Any, where: str) -> list[tuple[str, str]]:
    """A space's [{project: ref}] as checked pairs: every name and ref is checked again here, since
    git and the filesystem are about to see them."""
    if not isinstance(entries, list):
        raise XoError(f"{where}: projects is not a list")
    out = []
    for e in entries:
        if not (isinstance(e, dict) and len(e) == 1):
            raise XoError(f"{where}: a project entry is not {{PROJECT: REF}}")
        (project, ref), = e.items()
        if not names.project_ok(project):
            raise XoError(f"{where}: {project!r} is not a project name xo uses")
        if not names.ref_ok(ref):
            raise XoError(f"{where}: {ref!r} for {project} is not a ref xo uses")
        out.append((project, ref))
    return out


def dest(root: Path, project: str, projects: list) -> Path:
    """Where a project's checkout is under root, among projects (the others there): a checkout of
    that repo already in root/REPO or root/OWNER/REPO (where an older xo, or a name taken later, put
    it), else the folder names.folder gives it."""
    want = root / names.folder(project, projects)
    if not names.is_github(project):
        return want
    owners = names.owner_folders([*projects, project])
    for there in dict.fromkeys((want, root / names.folder(project), root / project)):
        if there != want and there.relative_to(root).parts[0].casefold() in owners:
            continue  # another project's OWNER/REPO goes in there, so it is no checkout's home now
        if (there / ".git").exists() and gitops.origin_is(there, project):
            return there
    return want


def _mine(there: Path, project: str, projects: list = ()) -> None:
    """Refuse a folder that is not project's own checkout: a GitHub project's origin must be its repo,
    and a plain project's must not be one of projects' GitHub repos, so another project with the same
    name is never fetched, moved or pushed."""
    gitops.own_checkout(there)
    if names.is_github(project):
        if not gitops.origin_is(there, project):
            raise XoError(f"./{there} is not a checkout of {project} (its origin is not one xo can confirm is "
                          "that repo); move it and try again")
        return
    for other in projects:
        if isinstance(other, str) and names.is_github(other) and gitops.origin_is(there, other):
            raise XoError(f"./{there} is a checkout of {other}, not of {project}; move it and try again")


def resolve(projects: Any, typed: str) -> str:
    """The one project of yours a typed name means (see names.named)."""
    if not isinstance(projects, list):
        raise XoError("the xo server's projects list is not a list")
    found = names.named(projects, typed)
    if len(found) > 1:
        raise XoError(f"{typed} could be " + " or ".join(found) + "; say which with its OWNER/REPO")
    if not found:
        raise XoError(f"{typed} is not in your projects (xo list projects shows them)")
    return found[0]


def made(name: str, space: dict) -> Path:
    """A new space with no projects, made with no kind: its folder ./NAME here, recorded as installed."""
    root = Path(name)
    root.mkdir()
    _save(root, name, _rev(space), [])
    return root


def _project_here(client: Client, typed: str, checkout: bool = False) -> tuple[str, Path]:
    """The project of yours a typed name means, and its folder here. With checkout, that folder must
    already hold the project's own checkout."""
    projects = _data(client.request("GET", "/me/projects").data, "projects")
    name = resolve(projects, typed)
    here = dest(Path("."), name, projects)
    if checkout:
        if not (here / ".git").exists():
            raise XoError(f"{name} has no checkout here (looked in ./{here}" + (
                f" and ./{name}" if names.is_github(name) and Path(name) != here else "") +
                f"); xo install project:{typed} makes one")
        _mine(here, name, projects)
    return name, here


def _space(client: Client, name: str) -> dict:
    spaces = _data(client.request("GET", "/me/spaces").data, "spaces")
    found = [s for s in spaces if isinstance(s, dict) and str(s.get("name", "")).casefold() == name.casefold()]
    if not found:
        raise XoError(f"you have no space named {name} (xo list spaces shows them)")
    return found[0]


def _rev(space: dict) -> int:
    rev = space.get("rev")
    if not isinstance(rev, int) or isinstance(rev, bool):
        raise XoError(f"space {space.get('name')} has no rev xo can use")
    return rev


def _state(root: Path) -> dict:
    path = root / STATE
    try:
        state = json.loads(path.read_text())
    except FileNotFoundError:
        raise XoError(f"./{root} was not installed by xo; run xo install space:{root.name} first") from None
    except (OSError, ValueError) as e:
        raise XoError(f"cannot read {path}: {e}") from None
    if not isinstance(state, dict) or not isinstance(state.get("rev"), int):
        raise XoError(f"{path} is not one xo wrote; run xo install space:{root.name} again")
    return state


def _save(root: Path, name: str, rev: int, pairs: list[tuple[str, str]]) -> None:
    path = root / STATE
    if path.parent.is_symlink() or (path.parent / ".git").exists():
        raise XoError(f"./{path.parent} is not xo's own folder (a link or a checkout); move it and try again")
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.unlink(missing_ok=True)  # never write through a link someone left there
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0), 0o644)
    with os.fdopen(fd, "w") as f:
        f.write(json.dumps({"space": name, "rev": rev, "projects": [{p: r} for p, r in pairs]}, indent=2) + "\n")
    os.replace(tmp, path)


def _place(dest: Path, project: str, ref: str | None) -> bool:
    """Clone or fetch a GitHub project at dest and put it at ref. False for a project with no
    GitHub remote, which xo cannot fetch: it is left as it is."""
    if not names.is_github(project):
        _say(f"{project} has no GitHub remote; left as it is" + ("" if dest.exists() else " (not here)"))
        return False
    if dest.exists():
        if not (dest / ".git").exists():
            raise XoError(f"./{dest} exists and is not a git checkout; move it and try again")
        _mine(dest, project)
        if gitops.dirty(dest):
            raise XoError(f"./{dest} has changes that are not committed; commit or stash them first")
        gitops.fetch(dest)
    else:
        holder = next((p for p in dest.parents if p != Path(".") and (p / ".git").exists()), None)
        if holder is not None:
            raise XoError(f"./{holder} is a git checkout, and {project} goes in ./{dest} inside it; move ./{holder} "
                          "and try again")
        gitops.clone(project, dest)
    if ref is not None:
        gitops.checkout(dest, ref)
    return True


def install(client: Client, kind: str, name: str) -> Any:
    if kind == "project":
        name, here = _project_here(client, name)
        existed = here.exists()
        if not _place(here, name, None):
            raise XoError(f"{name} has no GitHub remote, so there is nothing to install")
        _say(f"./{here}: " + (gitops.fast_forward(here) if existed else f"cloned from {gitops.GITHUB}/{name}"))
        return {"project": name}
    space = _space(client, name)
    pairs = _pairs(space.get("projects"), f"space {name}")
    root = Path(name)
    for project, ref in pairs:
        there = dest(root, project, [p for p, _ in pairs])
        if _place(there, project, ref):
            _say(f"./{there}: {ref}")
    rev = _rev(space)
    _save(root, name, rev, pairs)
    return {"space": name, "rev": rev, "projects": [{p: r} for p, r in pairs]}


def _local(root: Path, base: list[tuple[str, str]]) -> list[tuple[str, str]]:
    """Each project's ref as checked out under root; the installed ref when it is not here."""
    out = []
    for project, ref in base:
        there = dest(root, project, [p for p, _ in base])
        if not (there / ".git").exists():
            out.append((project, ref))
            continue
        gitops.own_checkout(there)
        here = gitops.current(there)
        if gitops.branch(there) is None:  # detached: still the installed tag or commit, if HEAD is there
            try:
                here = ref if gitops.resolve(there, ref)[1] == here else here
            except XoError:
                pass
        if not names.ref_ok(here):
            raise XoError(f"./{there} is on {here!r}, which is not a ref xo can record ({names.REF_RULE})")
        out.append((project, here))
    return out


def _published(root: Path, pairs: list[tuple[str, str]], base: dict[str, str]) -> None:
    """Refuse to record a ref others could not install: it must be on the project's remote."""
    for project, ref in pairs:
        there = dest(root, project, [p for p, _ in pairs])
        if base.get(project) == ref or not (there / ".git").exists():
            continue
        gitops.fetch(there)
        try:
            gitops.resolve(there, ref)
        except XoError:
            raise XoError(f"./{there} is at {ref}, which its remote does not have; push it first "
                          "(xo update project:" + project + ")") from None


def _record(client: Client, name: str, rev: int, pairs: list[tuple[str, str]],
            base: list[tuple[str, str]]) -> dict:
    """PUT the space's new refs. The server refuses (409) unless the space is still at rev and still
    holds base, the projects these refs were worked out from."""
    path = f"/me/spaces/{quote(name, safe='')}"
    print(f"xo: PUT {client.scrub(client.url(path))}", file=sys.stderr)
    body = {"rev": rev, "projects": [{p: r} for p, r in pairs], "base": [{p: r} for p, r in base]}
    return _data(client.request("PUT", path, body=body).data, "space")


def _checkouts(root: Path, pairs: list[tuple[str, str]]) -> list[Path]:
    """The space's projects that have a checkout here, each refused if it is not a checkout of its
    own or holds changes not committed, before anything is changed."""
    found = [(dest(root, p, [q for q, _ in pairs]), p) for p, _ in pairs]
    found = [(there, p) for there, p in found if there.exists()]
    for there, project in found:
        _mine(there, project, [p for p, _ in pairs])
        if gitops.dirty(there):
            raise XoError(f"./{there} has changes that are not committed; commit or stash them first. Nothing "
                          "was changed")
    return [there for there, _ in found]


def _git_step(dests: list[Path], rebase: bool) -> list[Path]:
    """Each checkout on a branch with an upstream: rebased on it first when rebase, then pushed
    (never forced, so git refuses when the remote is ahead). Tags and commits are left as they are.
    The checkouts pushed."""
    pushed = []
    for there in dests:
        if gitops.tracking(there):
            if rebase:
                gitops.pull_rebase(there)
            gitops.push(there)
            pushed.append(there)
    return pushed


def update(client: Client, kind: str, name: str) -> Any:
    if kind == "project":
        name, here = _project_here(client, name, checkout=True)
        gitops.push(here)
        _say(f"./{here}: pushed")
        return {"project": name}
    root = Path(name)
    state = _state(root)
    base = _pairs(state.get("projects"), str(root / STATE))
    _git_step(_checkouts(root, base), rebase=False)
    pairs = _local(root, base)
    if pairs == base:
        _say(f"space {name}: your checkouts are where the space has them; nothing to record")
        return {"space": name, "rev": state["rev"], "projects": [{p: r} for p, r in base]}
    _published(root, pairs, dict(base))
    space = _record(client, name, state["rev"], pairs, base)
    _save(root, name, _rev(space), _pairs(space.get("projects"), f"space {name}"))
    return space


def sync(client: Client, kind: str, name: str) -> Any:
    if kind == "project":
        name, here = _project_here(client, name, checkout=True)
        gitops.pull_rebase(here)
        gitops.push(here)
        _say(f"./{here}: rebased on its upstream and pushed")
        return {"project": name}
    root = Path(name)
    state = _state(root)
    installed = _pairs(state.get("projects"), str(root / STATE))
    pushed = _git_step(_checkouts(root, installed), rebase=True)
    base = dict(installed)
    local = dict(_local(root, installed))
    space = _space(client, name)
    remote = _pairs(space.get("projects"), f"space {name}")
    done = (" The branches in " + ", ".join(f"./{d}" for d in pushed) + " were rebased and pushed;") if pushed else ""
    left = (f"{done} No checkout was moved and nothing was recorded. To take the space's refs, run xo install "
            f"space:{name}; to keep yours, run that, check yours out again and run xo update space:{name}")
    if _rev(space) < state["rev"]:
        # An older copy of the space was put back (a write racing on another server instance), and
        # someone may have recorded a change on top of it since: xo cannot tell whose refs are newer.
        differ = sorted(p for p in {*local, *dict(remote)} if local.get(p) != dict(remote).get(p))
        if differ:
            raise XoError(f"space {name} went back to rev {_rev(space)} from your rev {state['rev']}, so xo cannot "
                          f"tell whether yours or the space's are newer for " + ", ".join(differ) + "." + left)
        base = dict(remote)
    merged, clashes = [], []
    for project, was in base.items():  # dropped from the space while you changed it
        if project not in dict(remote) and local.get(project, was) != was:
            clashes.append(f"{project} (yours {local[project]}, removed from the space)")
    for project, theirs in remote:
        was, mine = base.get(project), local.get(project, base.get(project))
        if mine is None or mine == theirs or mine == was:
            merged.append((project, theirs))
        elif theirs == was:
            merged.append((project, mine))
        else:
            clashes.append(f"{project} (yours {mine}, the space's {theirs}, installed {was})")
    if clashes:
        raise XoError("both you and the space changed " + "; ".join(clashes) + "." + left)
    for project, ref in merged:
        if local.get(project) != ref:
            _place(dest(root, project, [p for p, _ in merged]), project, ref)
    # The checkouts now hold the space's refs and yours: save the space as read, so that if recording
    # fails below, the next sync still counts your refs as yours and the space's as its own.
    _save(root, name, _rev(space), remote)
    rev = _rev(space)
    if merged != remote:
        _published(root, merged, dict(remote))
        rev = _rev(_record(client, name, rev, merged, remote))
    _save(root, name, rev, merged)
    return {"space": name, "rev": rev, "projects": [{p: r} for p, r in merged]}
