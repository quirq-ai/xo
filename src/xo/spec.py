"""The XO OpenAPI spec: where xo learns which operations exist and what each one takes.

The spec is read from, in order: --spec FILE, $XO_SPEC, the copy bundled with xo
(src/xo/openapi.json), and last the API itself (<base>/api/openapi). Nothing here talks to the
network except `load` through the client it is given.
"""
from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass, field
from importlib import resources
from pathlib import Path
from typing import Any

from xo.client import Client, XoError

SPEC_ENV = "XO_SPEC"
REMOTE_PATH = "/api/openapi"
METHODS = ("get", "put", "post", "delete", "patch")


@dataclass
class Param:
    name: str
    where: str  # path, query or header
    required: bool
    description: str = ""


@dataclass
class Operation:
    op_id: str
    method: str
    path: str
    summary: str
    tags: list[str]
    params: list[Param] = field(default_factory=list)
    body_required: bool = False
    has_body: bool = False

    @property
    def path_params(self) -> list[str]:
        return re.findall(r"{([^}]+)}", self.path)


def _bundled() -> dict | None:
    try:
        text = resources.files("xo").joinpath("openapi.json").read_text()
    except (FileNotFoundError, OSError):
        return None
    return json.loads(text)


def load(client: Client, path: str | None = None) -> dict:
    path = path or os.environ.get(SPEC_ENV)
    if path:
        try:
            return json.loads(Path(path).read_text())
        except (OSError, json.JSONDecodeError) as e:
            raise XoError(f"cannot read spec {path}: {e}") from None
    bundled = _bundled()
    if bundled is not None:
        return bundled
    resp = client.request("GET", REMOTE_PATH)
    if not isinstance(resp.data, dict) or "paths" not in resp.data:
        raise XoError(f"{client.base}{REMOTE_PATH} did not return an OpenAPI document; pass --spec FILE")
    return resp.data


def _resolve(spec: dict, obj: Any) -> Any:
    """Follow one local $ref (#/components/...), the only kind an OpenAPI export uses."""
    if isinstance(obj, dict) and isinstance(obj.get("$ref"), str) and obj["$ref"].startswith("#/"):
        node: Any = spec
        for part in obj["$ref"][2:].split("/"):
            node = node.get(part, {}) if isinstance(node, dict) else {}
        return node
    return obj


def _op_id(method: str, path: str) -> str:
    words = re.sub(r"[{}]", "", path).strip("/").replace("/", "_").replace("-", "_")
    return f"{method}_{words or 'root'}"


def operations(spec: dict) -> list[Operation]:
    ops: list[Operation] = []
    for path, item in (spec.get("paths") or {}).items():
        if not isinstance(item, dict):
            continue
        shared = item.get("parameters") or []
        for method in METHODS:
            raw = item.get(method)
            if not isinstance(raw, dict):
                continue
            params = []
            for p in [*shared, *(raw.get("parameters") or [])]:
                p = _resolve(spec, p)
                if p.get("in") in ("path", "query", "header") and p.get("name"):
                    params.append(Param(p["name"], p["in"], bool(p.get("required") or p["in"] == "path"),
                                        p.get("description", "")))
            body = _resolve(spec, raw.get("requestBody")) if raw.get("requestBody") else None
            ops.append(Operation(
                op_id=raw.get("operationId") or _op_id(method, path),
                method=method.upper(), path=path,
                summary=(raw.get("summary") or raw.get("description") or "").strip().splitlines()[0]
                if (raw.get("summary") or raw.get("description")) else "",
                tags=list(raw.get("tags") or []),
                params=params,
                has_body=body is not None,
                body_required=bool(body and body.get("required")),
            ))
    return sorted(ops, key=lambda o: (o.path, o.method))


def find(spec: dict, name: str) -> Operation:
    ops = operations(spec)
    for op in ops:
        if op.op_id == name:
            return op
    lowered = [op for op in ops if op.op_id.lower() == name.lower()]
    if len(lowered) == 1:
        return lowered[0]
    near = [op.op_id for op in ops if name.lower() in op.op_id.lower()][:5]
    hint = f"; did you mean {', '.join(near)}?" if near else "; run `xo ops` to list them"
    raise XoError(f"no operation {name!r} in the spec{hint}")


def build(op: Operation, values: dict[str, str]) -> tuple[str, dict[str, str]]:
    """The request path and query for `op` from NAME=VALUE pairs; every required one must be given."""
    known = {p.name: p for p in op.params}
    for name in op.path_params:
        known.setdefault(name, Param(name, "path", True))
    unknown = sorted(set(values) - set(known))
    if unknown:
        raise XoError(f"{op.op_id} takes no parameter {', '.join(unknown)}; "
                      f"it takes: {', '.join(sorted(known)) or 'none'}")
    missing = [n for n, p in known.items() if p.required and p.where != "header" and n not in values]
    if missing:
        raise XoError(f"{op.op_id} needs {', '.join(f'--param {n}=...' for n in missing)}")
    from urllib.parse import quote
    path = op.path
    for name in op.path_params:
        path = path.replace("{" + name + "}", quote(values[name], safe=""))
    query = {n: v for n, v in values.items() if known[n].where == "query"}
    return path, query
