# xo

`xo` is the command line for the [XO Cloud](https://app.xo.builders) API. It reads the API's
OpenAPI spec, so every operation the API documents is a command, and it sends your XO API key on
each request without ever printing it.

It follows the shape of quirq's own tools (`qq fetch`, `qqsync`): one Python package, one
argparse command, standard library only, Apache-2.0. Unlike them the command is plain `xo`.

## Install

```sh
pip install "quirq-xo @ git+https://github.com/quirq-ai/xo"
```

Python 3.11 or newer. The package is `quirq-xo`, not `xo`: PyPI's `xo` is an unrelated game, and
`pip install -U xo` would replace this tool with it. The command it installs is `xo`.

## The spec

xo does not bundle XO's spec yet. `https://app.xo.builders/api/openapi` opens only in a signed-in
browser, so until a copy is bundled: download the OpenAPI JSON there while signed in, and pass
`--spec FILE` or set `XO_SPEC=FILE`. Without one, `xo ops` and `xo call` stop and say so; `xo api`
needs no spec.

## One command per endpoint

With a spec, every endpoint is a command, named after its path:

```sh
export XO_API_KEY=ak_...        # from your XO account's API keys; never printed
export XO_SPEC=~/xo-openapi.json

xo --help                                   # every endpoint command
xo claude-code --prompt "hello"             # POST /claude-code {"prompt": "hello"}
xo templates list --limit 5                 # GET  /api/v1/templates?limit=5
xo templates create --name demo --public false
xo templates get --template-id t1           # GET  /api/v1/templates/t1
xo templates delete --template-id t1 --yes
xo templates create --help                  # the flags an endpoint takes
```

The command is the path without a leading `/api` or `/vN` and without its `{placeholders}`, joined
with dashes. When several methods share a path, each is an action: `list` and `get` (GET on the
collection or one item), `create` (POST), `update` (PATCH), `replace` (PUT), `delete` (DELETE).
Path, query and header parameters and the top-level fields of a JSON body are the flags. A body
field the spec types as a string keeps the text you type; other values are JSON when they parse.
Required ones are checked before anything is sent. A spec that cannot be read leaves `ops`, `call`
and `api` working and makes any other command stop with one line saying why.

## Lower level

```sh

xo ops                          # every operation: id, method, path, summary
xo ops template                 # only those whose id, path or tag mentions "template"
xo ops template --json          # the same with their parameters

xo call OPERATION_ID --param NAME=VALUE --data '{"name": "demo"}'
xo call OPERATION_ID --field name=demo --field public=false   # build the body field by field
xo call OPERATION_ID --data @body.json                         # or from a file, or - for stdin
xo call OPERATION_ID --field name=demo --dry-run               # print the request, send nothing

xo api GET /some/path --query limit=5                          # any request, spec or not
xo api DELETE /some/path/1 --yes                               # a DELETE needs --yes
```

`--param` fills path, query and header parameters, never `Authorization`, `Host`, `Cookie`,
`Proxy-*` or the other headers xo and HTTP own.
`--field` values are JSON when they parse (`false`, `5`, `["a"]`) and plain strings otherwise.
A request that is not a GET changes things on XO, so it prints one line on stderr before it goes.
`--dry-run` needs no key and sends nothing, so it never fetches the spec from the API either.
Responses print as JSON on stdout. Any problem exits 1 with one line on stderr.

| Setting | Variable | Flag | Default |
|---|---|---|---|
| API key | `XO_API_KEY` | `--api-key -` (asks without echo on a terminal, else reads one line from stdin) | none; required for every call but a dry run |
| API base | `XO_BASE_URL` | `--base-url` | `https://app.xo.builders` |
| Spec | `XO_SPEC` | `--spec` | the copy bundled with xo, else `<base>/api/openapi` |

`--api-key KEY` also works, but the key then lands in your shell history and in the process list
other users of the machine can read. Prefer the variable, or `--api-key -`. The options go before
or after the command.

## Safety

- The key goes only in the `Authorization: Bearer` header. It is redacted from any response or
  error that echoes it, and tests check it never reaches stdout or stderr, including on argument
  errors and unexpected failures. An argument error never repeats a value you typed, so a
  mistyped key flag (`--api_key KEY`, `--token=KEY`, `--api-keyKEY`) stays off the screen. A key
  with anything but letters, digits, `_`, `.` and `-` is refused without being shown, so no URL or
  JSON escaping can hide the key from redaction.
- xo never follows a redirect, so a redirect cannot carry the key to another host. A redirect to
  XO's sign-in page is reported as "this route does not accept an API key".
- Plain `http://` is refused except to this machine.

## Develop

```sh
python -m pip install -e ".[test]"
python -m pytest -q
```

Tests run against a fake API on 127.0.0.1 (`tests/conftest.py`) and a synthetic spec
(`tests/fixtures/openapi.json`); they never reach XO.

## License

Apache-2.0.
