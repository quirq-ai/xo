"""The XO OpenAPI spec: where xo learns which operations exist and what each one takes.

The spec is read from, in order: --spec FILE, $XO_SPEC, then the copy bundled with xo
(src/xo/openapi.json). Nothing here talks to the network: the API's own docs sit behind a browser
sign-in, so there is nothing to fetch with a key. A spec is data from elsewhere: every node is
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

from xo.client import XoError

SPEC_ENV = "XO_SPEC"
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
    name: str  # the field's path, dotted: name, or providers.anthropic.apiKey
    required: bool  # only top-level fields can be required; see Operation.required_fields
    description: str = ""
    type: str = ""  # the schema's type; "string" fields keep their text as given
    path: tuple[str, ...] = ()
    # Defaults the spec gives this field's siblings, set when this field is (enabled: true beside an apiKey).
    defaults: dict[str, Any] = field(default_factory=dict)
    secret: bool = False  # writeOnly or format password: a key or token, never printed

    def __post_init__(self) -> None:
        self.path = self.path or (self.name,)


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
    # The leaf fields of a JSON object body: top-level fields, and the fields of objects nested up to
    # three deep. Empty when the body is not a plain object, so it can only be given whole with --data.
    body_fields: list[BodyField] = field(default_factory=list)
    required_fields: list[str] = field(default_factory=list)  # top-level names the body must hold
    top_fields: list[str] = field(default_factory=list)  # every top-level name the body schema lists
    secret_paths: list[tuple[str, ...]] = field(default_factory=list)  # where the body holds keys or tokens
    copies_key: bool = False  # x-xo-copies-api-key: the server stores the caller's API key in what it creates
    server: bool = False  # x-xo-server: answered by the xo server itself, not passed on to the core API

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


def load(path: str | None = None) -> dict:
    local = load_local(path)
    if local is None:
        raise XoError("the spec bundled with xo is missing; reinstall xo, or pass --spec FILE (or set XO_SPEC)")
    return local


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
                required_fields=_required_fields(spec, body),
                top_fields=list(_properties(spec, _body_schema(spec, body))),
                secret_paths=_secret_paths(spec, _body_schema(spec, body)),
                copies_key=raw.get("x-xo-copies-api-key") is True,
                server=raw.get("x-xo-server") is True,
            ))
    return sorted(ops, key=lambda o: (o.path, o.method))


def _body_schema(spec: dict, body: dict | None) -> dict:
    if not body:
        return {}
    content = _dict(body.get("content"))
    media = _dict(content.get("application/json") or (next(iter(content.values()), {}) if content else {}))
    return _dict(_resolve(spec, media.get("schema")))


def _object_parts(spec: dict, schema: dict) -> list[dict]:
    """The object parts of a schema: itself and its allOf branches."""
    parts = [schema, *(_dict(_resolve(spec, s)) for s in _list(schema.get("allOf")))]
    return [p for p in parts if p.get("type", "object") == "object"]


def _properties(spec: dict, schema: dict) -> dict[str, dict]:
    props: dict[str, dict] = {}
    for part in _object_parts(spec, schema):
        for name, prop in _dict(part.get("properties")).items():
            if isinstance(name, str) and name.strip():
                props[name] = _dict(_resolve(spec, prop))
    return props


def _required_fields(spec: dict, body: dict | None) -> list[str]:
    names: list[str] = []
    for part in _object_parts(spec, _body_schema(spec, body)):
        names += [r for r in _list(part.get("required")) if isinstance(r, str) and r not in names]
    return names


def _body_fields(spec: dict, body: dict | None) -> list[BodyField]:
    schema = _body_schema(spec, body)
    if not schema or not _object_parts(spec, schema):
        return []
    required = set(_required_fields(spec, body))
    fields: list[BodyField] = []

    def walk(props: dict[str, dict], path: tuple[str, ...]) -> None:
        defaults = {n: p["default"] for n, p in props.items() if "default" in p}
        for name, prop in props.items():
            inner = _properties(spec, prop) if prop.get("type") == "object" else {}
            if inner and len(path) < 2:  # an object with fields: a flag per field, not one for the object
                walk(inner, (*path, name))
                continue
            fields.append(BodyField(".".join((*path, name)), not path and name in required,
                                    _first_line(prop.get("description") or prop.get("title")), _kind(spec, prop),
                                    (*path, name), {k: v for k, v in defaults.items() if k != name} if path else {},
                                    _secret(prop)))
    walk(_properties(spec, schema), ())
    return fields


def _secret(prop: dict) -> bool:
    return prop.get("writeOnly") is True or prop.get("format") == "password"


def _secret_paths(spec: dict, schema: dict, path: tuple[str, ...] = ()) -> list[tuple[str, ...]]:
    """Every path in a body schema whose value is a key or token, however deep."""
    out: list[tuple[str, ...]] = []
    if len(path) > 8:
        return out
    for name, prop in _properties(spec, schema).items():
        if _secret(prop):
            out.append((*path, name))
        elif prop.get("type") == "object":
            out += _secret_paths(spec, prop, (*path, name))
    return out


def _kind(spec: dict, prop: dict) -> str:
    """"string" when every value the field takes is text (or null): type string, a type list such as
    ["string", "null"], or an anyOf/oneOf of such branches, as pydantic writes optional fields."""
    def texty(node: Any, depth: int = 0) -> bool:
        node = _dict(_resolve(spec, node))
        kind = node.get("type")
        if isinstance(kind, str):
            return kind in ("string", "null")
        if isinstance(kind, list):
            return bool(kind) and all(k in ("string", "null") for k in kind)
        branches = _list(node.get("anyOf")) or _list(node.get("oneOf"))
        return depth < 5 and bool(branches) and all(texty(b, depth + 1) for b in branches)
    kind = prop.get("type")
    if texty(prop) and kind != "null":
        return "string"
    return kind if isinstance(kind, str) else ""


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


def match(ops: list[Operation], method: str, path: str) -> Operation | None:
    """The operation a raw METHOD PATH would run, so `xo api` hides the same secrets a command does."""
    path = "/" + path.split("?", 1)[0].strip("/")
    for op in ops:
        pattern = "".join("[^/]+" if part.startswith("{") else re.escape(part)
                          for part in re.split(r"({[^}]+})", "/" + op.path.strip("/")))
        if op.method == method.upper() and re.fullmatch(pattern, path):
            return op
    return None


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
        # Only a name that would go out as a header is refused: a query parameter `host` is fine.
        if own_header(name) and (name not in known or known[name].where == "header"):
            raise XoError(f"{name.strip()} cannot be set: xo sends it itself or it steers routing")
        given[name if name in known else headers_by_lower.get(name.lower(), name)] = value
    unknown = sorted(set(given) - set(known))
    if unknown:
        raise XoError(f"{label} takes no parameter {clean(', '.join(unknown))}; "
                      f"it takes: {clean(', '.join(sorted(known))) or 'none'}")
    missing = [n for n, p in known.items() if p.required and n not in given]
    if missing:
        raise XoError(f"{label} needs {clean(', '.join(flag(n) for n in missing))}")
    empty = [n for n in op.path_params if n in given and not given[n]]
    if empty:
        raise XoError(f"{clean(flag(empty[0]))} can't be empty")
    path = op.path
    for name in op.path_params:
        path = path.replace("{" + name + "}", quote(given[name], safe=""))
    query = {n: v for n, v in given.items() if known[n].where == "query"}
    headers = {n: v for n, v in given.items() if known[n].where == "header"}
    return path, query, headers
