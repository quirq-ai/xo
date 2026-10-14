"""The XO OpenAPI spec: where xo learns which operations exist and what each one takes.

The spec is read from, in order: --spec FILE, $XO_SPEC, the copy bundled with xo
(src/xo/openapi.json), and last the API itself (<base>/api/openapi). Nothing here talks to the
network except `load` through the client it is given. A spec is data from elsewhere: every node is
type-checked before use, and text from it is cleaned of control characters before it is printed.
"""
from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass, field
from importlib import resources
from pathlib import Path
from typing import Any, Callable
from urllib.parse import quote

from xo.client import Client, XoError

SPEC_ENV = "XO_SPEC"
REMOTE_PATH = "/api/openapi"
METHODS = ("get", "put", "post", "delete", "patch")
# Headers that xo or the HTTP layer owns. A spec parameter with one of these names gets no flag and
# --param cannot set it: Authorization carries the key, the rest steer routing or framing.
OWN_HEADERS = {"authorization", "host", "content-length", "content-type", "transfer-encoding", "connection",
               "cookie", "te", "upgrade", "expect"}
_CONTROL = re.compile(r"[\x00-\x1f\x7f-\x9f]")


@dataclass
class Param:
    name: str
    where: str  # path, query or header
    required: bool
    description: str = ""


@dataclass
class BodyField:
    name: str
    required: bool
    description: str = ""
    type: str = ""  # the schema's type; "string" fields keep their text as given


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
    # Top-level fields of a JSON object body. Empty when the body is not a plain object, so it can
    # only be given whole with --data.
    body_fields: list[BodyField] = field(default_factory=list)

    @property
    def path_params(self) -> list[str]:
        return re.findall(r"{([^}]+)}", self.path)


def clean(text: Any) -> str:
    """Text from a spec, safe to print: no control characters."""
    return _CONTROL.sub("", str(text))


def own_header(name: str) -> bool:
    name = name.strip().lower()
    return name in OWN_HEADERS or name.startswith("proxy-")


def _parse(text: str, where: str) -> dict:
    try:
        data = json.loads(text)
    except (json.JSONDecodeError, RecursionError) as e:
        raise XoError(f"cannot use spec {where}: not JSON ({type(e).__name__})") from None
    if not isinstance(data, dict):
        raise XoError(f"cannot use spec {where}: not an OpenAPI object")
    return data


def _read(path: str) -> dict:
    try:
        text = Path(path).read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as e:
        raise XoError(f"cannot use spec {path}: {getattr(e, 'strerror', None) or type(e).__name__}") from None
    return _parse(text, path)


def _bundled() -> dict | None:
    try:
        text = resources.files("xo").joinpath("openapi.json").read_text(encoding="utf-8")
    except (FileNotFoundError, OSError):
        return None
    return _parse(text, "bundled with xo")


def load_local(path: str | None = None) -> dict | None:
    """The spec from --spec, $XO_SPEC or the bundled copy, without touching the network.

    None when there is none; XoError when one is named but cannot be read.
    """
    path = path or os.environ.get(SPEC_ENV)
    return _read(path) if path else _bundled()


def load(client: Client, path: str | None = None, *, remote: bool = True) -> dict:
    local = load_local(path)
    if local is not None:
        return local
    hint = (f"xo has no bundled spec yet, so download it from {client.base}{REMOTE_PATH} while signed in "
            "and pass --spec FILE (or set XO_SPEC)")
    if not remote:
        raise XoError(f"a dry run never fetches the spec; {hint}")
    try:
        resp = client.request("GET", REMOTE_PATH)
    except XoError as e:
        raise XoError(f"{e}. {hint}") from None
    if not isinstance(resp.data, dict) or "paths" not in resp.data:
        raise XoError(f"{client.base}{REMOTE_PATH} did not return an OpenAPI document. {hint}")
    return resp.data


def _resolve(spec: dict, obj: Any) -> Any:
    """Follow one local $ref (#/components/...), the only kind an OpenAPI export uses."""
    if isinstance(obj, dict) and isinstance(obj.get("$ref"), str) and obj["$ref"].startswith("#/"):
        node: Any = spec
        for part in obj["$ref"][2:].split("/"):
            node = node.get(part, {}) if isinstance(node, dict) else {}
        return node
    return obj


def _dict(value: Any) -> dict:
    return value if isinstance(value, dict) else {}


def _list(value: Any) -> list:
    return value if isinstance(value, list) else []


def _first_line(text: Any) -> str:
    lines = str(text or "").strip().splitlines()[:1]
    return clean(lines[0]).strip() if lines else ""


def _op_id(method: str, path: str) -> str:
    words = re.sub(r"[{}]", "", path).strip("/").replace("/", "_").replace("-", "_")
    return f"{method}_{words or 'root'}"


def operations(spec: dict) -> list[Operation]:
    ops: list[Operation] = []
    for path, item in _dict(spec.get("paths")).items():
        item = _dict(item)
        if not isinstance(path, str) or not item:
            continue
        shared = _list(item.get("parameters"))
        for method in METHODS:
            raw = item.get(method)
            if not isinstance(raw, dict):  # an empty {} is still an operation
                continue
            params = []
            for p in [*shared, *_list(raw.get("parameters"))]:
                p = _dict(_resolve(spec, p))
                name, where = p.get("name"), p.get("in")
                if where in ("path", "query", "header") and isinstance(name, str) and name.strip():
                    params.append(Param(name, where, bool(p.get("required") or where == "path"),
                                        _first_line(p.get("description"))))
            body = _dict(_resolve(spec, raw.get("requestBody"))) if raw.get("requestBody") else None
            op_id = raw.get("operationId")
            ops.append(Operation(
                op_id=clean(op_id) if isinstance(op_id, str) and clean(op_id).strip() else _op_id(method, path),
                method=method.upper(), path=path,
                summary=_first_line(raw.get("summary") or raw.get("description")),
                tags=[clean(t) for t in _list(raw.get("tags")) if isinstance(t, str)],
                params=params,
                has_body=body is not None,
                body_required=bool(body and body.get("required")),
                body_fields=_body_fields(spec, body),
            ))
    return sorted(ops, key=lambda o: (o.path, o.method))


def _body_fields(spec: dict, body: dict | None) -> list[BodyField]:
    if not body:
        return []
    content = _dict(body.get("content"))
    media = _dict(content.get("application/json") or (next(iter(content.values()), {}) if content else {}))
    schema = _dict(_resolve(spec, media.get("schema")))
    parts = [schema, *(_dict(_resolve(spec, s)) for s in _list(schema.get("allOf")))]
    fields: dict[str, BodyField] = {}
    for part in parts:
        if part.get("type", "object") != "object":
            continue
        required = {r for r in _list(part.get("required")) if isinstance(r, str)}
        for name, prop in _dict(part.get("properties")).items():
            if not isinstance(name, str) or not name.strip():
                continue
            prop = _dict(_resolve(spec, prop))
            kind = prop.get("type") if isinstance(prop.get("type"), str) else ""
            fields[name] = BodyField(name, name in required, _first_line(prop.get("description") or prop.get("title")),
                                     kind)
    return list(fields.values())


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


def known_params(op: Operation) -> dict[str, Param]:
    """The parameters a caller may give, by name: the spec's, minus xo's own headers, plus any
    {placeholder} in the path that the spec forgot to declare."""
    known = {p.name: p for p in op.params if not (p.where == "header" and own_header(p.name))}
    for name in op.path_params:
        known.setdefault(name, Param(name, "path", True))
    return known


def build(op: Operation, values: dict[str, str], flag: Callable[[str], str] = lambda n: f"--param {n}=...",
          label: str | None = None) -> tuple[str, dict[str, str], dict[str, str]]:
    """The request path, query and headers for `op` from NAME=VALUE pairs.

    Every required parameter must be given. Header names match without regard to case. xo's own
    headers (Authorization, Host, Cookie, Proxy-*, ...) cannot be set.
    """
    label = label or op.op_id
    known = known_params(op)
    headers_by_lower = {n.lower(): n for n, p in known.items() if p.where == "header"}
    given: dict[str, str] = {}
    for name, value in values.items():
        if own_header(name):
            raise XoError(f"{name.strip()} cannot be set: xo sends it itself or it steers routing")
        given[name if name in known else headers_by_lower.get(name.lower(), name)] = value
    unknown = sorted(set(given) - set(known))
    if unknown:
        raise XoError(f"{label} takes no parameter {', '.join(unknown)}; "
                      f"it takes: {', '.join(sorted(known)) or 'none'}")
    missing = [n for n, p in known.items() if p.required and n not in given]
    if missing:
        raise XoError(f"{label} needs {', '.join(flag(n) for n in missing)}")
    path = op.path
    for name in op.path_params:
        path = path.replace("{" + name + "}", quote(given[name], safe=""))
    query = {n: v for n, v in given.items() if known[n].where == "query"}
    headers = {n: v for n, v in given.items() if known[n].where == "header"}
    return path, query, headers
