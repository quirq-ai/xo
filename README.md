# xo

`xo` is the command line for the [XO Cloud](https://app.xo.builders) API. It reads the API's
OpenAPI spec, so every operation the API documents is a command, and it sends your XO API key on
each request without ever printing it.

It follows the shape of quirq's own tools (`qq fetch`, `qqsync`): one Python package, one
argparse command, standard library only, Apache-2.0. Unlike them it is called plain `xo`.

## Install

```sh
pip install "xo @ git+https://github.com/quirq-ai/xo"
```

Python 3.11 or newer.

## Use

```sh
export XO_API_KEY=ak_...        # from your XO account's API keys; never printed

xo ops                          # every operation: id, method, path, summary
xo ops template                 # only those whose id, path or tag mentions "template"
xo ops template --json          # the same with their parameters

xo call OPERATION_ID --param NAME=VALUE --data '{"name": "demo"}'
xo call OPERATION_ID --field name=demo --field public=false   # build the body field by field
xo call OPERATION_ID --data @body.json                         # or from a file, or - for stdin

xo api GET /some/path --query limit=5                          # any request, spec or not
```

`--field` values are JSON when they parse (`false`, `5`, `["a"]`) and plain strings otherwise.
Responses print as JSON on stdout. Any problem exits 1 with one line on stderr.

| Setting | Variable | Flag | Default |
|---|---|---|---|
| API key | `XO_API_KEY` | `--api-key` | none; required for every call |
| API base | `XO_BASE_URL` | `--base-url` | `https://app.xo.builders` |
| Spec | `XO_SPEC` | `--spec` | the copy bundled with xo, else `<base>/api/openapi` |

Prefer the variable to `--api-key`: a flag shows in the process list.

## Safety

- The key goes only in the `Authorization: Bearer` header. It is redacted from any response or
  error that echoes it, and tests check it never reaches stdout or stderr.
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
