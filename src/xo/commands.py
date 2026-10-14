"""One command per API endpoint, generated from the spec.

    xo <endpoint> [ACTION] [--flag VALUE ...]

The command is the endpoint's path, without a leading /api or /vN and without its {placeholders},
joined with dashes: POST /claude-code is `xo claude-code`, GET /api/v1/templates is
`xo templates`. When several operations share a command (GET, POST and DELETE on /templates and
/templates/{id}), each is an action named for its method: list, get, create, update, replace,
delete. Path, query and header parameters and the top-level fields of a JSON body become flags;
a body that is not a plain object is given with --data.
"""
from __future__ import annotations

import argparse
import re
from typing import Any, Callable

from xo.spec import Operation, _is_auth

RESERVED = {"ops", "call", "api"}


def kebab(text: str) -> str:
    text = re.sub(r"([a-z0-9])([A-Z])", r"\1-\2", text)
    text = re.sub(r"[^A-Za-z0-9]+", "-", text).strip("-").lower()
    return text or "x"


def command_of(op: Operation) -> str:
    segments = [seg for seg in op.path.strip("/").split("/") if seg and not seg.startswith("{")]
    while segments and (segments[0].lower() == "api" or re.fullmatch(r"v\d+", segments[0].lower())):
        segments = segments[1:]
    name = kebab("-".join(segments)) if segments else "root"
    return f"{name}-api" if name in RESERVED else name


def verb_of(op: Operation) -> str:
    by_id = op.path.rstrip("/").endswith("}")
    return {"GET": "get" if by_id else "list", "POST": "create", "PATCH": "update",
            "PUT": "replace", "DELETE": "delete"}.get(op.method, op.method.lower())


def plan(ops: list[Operation]) -> dict[str, dict[str, Operation]]:
    """command -> action -> operation. A command with one operation has the action ""."""
    by_command: dict[str, list[Operation]] = {}
    for op in ops:
        by_command.setdefault(command_of(op), []).append(op)
    out: dict[str, dict[str, Operation]] = {}
    for command, group in by_command.items():
        if len(group) == 1:
            out[command] = {"": group[0]}
            continue
        actions: dict[str, Operation] = {}
        for op in group:
            name = verb_of(op)
            if name in actions or name in ("help",):
                name = kebab(op.op_id)
            while name in actions:
                name += "-" + op.method.lower()
            actions[name] = op
        out[command] = actions
    return out


def register(sub: argparse._SubParsersAction, ops: list[Operation], parents: list[argparse.ArgumentParser],
             add_send: Callable[[argparse.ArgumentParser], None], run: Callable[[argparse.Namespace], int]) -> None:
    for command, actions in sorted(plan(ops).items()):
        if list(actions) == [""]:
            op = actions[""]
            _operation(sub.add_parser(command, parents=parents, help=_line(op), description=_about(op)),
                       op, add_send, run)
            continue
        g = sub.add_parser(command, help=", ".join(sorted(actions)))
        gsub = g.add_subparsers(dest="action", required=True, metavar="ACTION")
        for name, op in sorted(actions.items()):
            _operation(gsub.add_parser(name, parents=parents, help=_line(op), description=_about(op)),
                       op, add_send, run)


def _line(op: Operation) -> str:
    return f"{op.method} {op.path}  {op.summary}".rstrip()


def _about(op: Operation) -> str:
    return f"{op.summary}\n\n{op.method} {op.path}  (operation {op.op_id})".strip()


def _operation(p: argparse.ArgumentParser, op: Operation, add_send: Callable[[argparse.ArgumentParser], None],
               run: Callable[[argparse.Namespace], int]) -> None:
    flags: list[tuple[str, str, str]] = []  # (dest, where, wire name)
    taken = {"data", "field", "dry-run", "yes", "api-key", "base-url", "spec", "timeout", "help"}
    for prm in op.params:
        if _is_auth(prm):
            continue
        flag = _unique(kebab(prm.name), taken)
        dest = f"p_{len(flags)}"
        p.add_argument(f"--{flag}", dest=dest, metavar="VALUE",
                       help=f"{prm.where} parameter{' (required)' if prm.required else ''}. {prm.description}".strip())
        flags.append((dest, prm.where, prm.name))
    for fname, required, desc in op.body_fields:
        flag = _unique(kebab(fname), taken)
        dest = f"p_{len(flags)}"
        p.add_argument(f"--{flag}", dest=dest, metavar="VALUE",
                       help=f"body field{' (required)' if required else ''}; JSON when it parses. {desc}".strip())
        flags.append((dest, "body", fname))
    if op.has_body:
        add_send(p)
    else:
        p.add_argument("--dry-run", action="store_true", help="print the request and send nothing")
        p.add_argument("--yes", action="store_true", help="needed to send a DELETE")
        p.set_defaults(data=None, field=None)
    p.set_defaults(run=run, op=op, flags=flags)


def _unique(flag: str, taken: set[str]) -> str:
    name = flag
    while name in taken:
        name += "-param"
    taken.add(name)
    return name


def values(args: argparse.Namespace) -> tuple[dict[str, str], dict[str, str]]:
    """(params, body fields) the user gave, by their wire names."""
    params: dict[str, str] = {}
    fields: dict[str, str] = {}
    for dest, where, wire in args.flags:
        value: Any = getattr(args, dest, None)
        if value is None:
            continue
        (fields if where == "body" else params)[wire] = value
    return params, fields
