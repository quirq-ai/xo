# xo user guide

`xo` is one command line for three XO services. One `xo login` logs you in to all of them.

| Service | Address | What it does | You log in with |
|---|---|---|---|
| me | `https://xo.quirq.dev` | the xo server: answers `xo me`, holds the list of services, passes creates on to the platform | your XO API key (`ak_...`) |
| xo | `https://dev.xo.builders` | XO Cloud: creates spaces (Claude Code, Codex, OpenClaw, ...) | the same key, through me |
| coder | `https://dev-coder.xo.builders` | Coder workspaces: GitHub previews, ssh | Coder's own login, in your browser |

xo saves your keys only on your machine; the xo server stores none. Your XO key is still sent to
the xo server and the platform on every call, and a create copies it into the new space.

**Changed:** XO's platform was `https://app.xo.builders` before this version. Creates now go to
`https://dev.xo.builders`.

## Install

Follow the [quickstart](quickstart.md) step 1 (Python 3.11 or newer, one paste). For the `coder`
service, also install the [Coder CLI](https://coder.com/docs/install/cli).

## Log in

```sh
xo login          # all services
xo login xo       # only the XO key (me and xo)
xo login coder    # only Coder
xo status         # each service, its address, and whether you are logged in
xo logout         # deletes your XO key; logs Coder out of xo's deployment (asks first)
```

`xo login` prints the three addresses first. Then:
- **me / xo:** paste your XO API key at the prompt (nothing shows while you type).
  If a key you saved before still works, it says "already logged in" and doesn't ask.
- **coder:** Coder opens its sign-in page in your browser; paste back the token it shows. If Coder is
  already logged in to xo's deployment, it says "already logged in". If Coder is logged in somewhere
  else, xo asks before replacing that login.

The key and coder each end with one line (`xo: me: ...` and `xo: coder: ...`): logged in, already
logged in, skipped (no Coder CLI), or failed and why. One failure does not stop the other.

## Create spaces (xo)

```sh
xo --help                                        # every command
xo claude-code --help                            # the flags of one command

xo claude-code --name my-cc --volume-size 20 --providers-anthropic-api-key -
xo codex --name my-codex --volume-size 20
xo antigravity --name my-ag --volume-size 20
xo openclaw --help                               # needs a provider key and a chat-channel token
xo hermes --help
xo xo-cowork --help
xo spaces --space-id THE_ID                      # a space you created, with its URLs
xo me                                            # whose key this is
```

- Add `--dry-run` to any create to see the request without sending it.
- `-` makes xo ask for a key or token without showing it. Only one `-` works per command; put any
  other secret in a JSON file and pass it with `--data @FILE`.
- Every create except `nebius-openclaw` copies your XO key into the new space, and xo says so as it
  sends.
- There is no delete or list: keep the `data.id` a create prints.

## Coder workspaces (coder)

```sh
xo coder github https://github.com/OWNER/REPO --dry-run   # the plan, sends nothing
xo coder github https://github.com/OWNER/REPO             # create a preview; prints its URLs
xo coder github https://github.com/OWNER/REPO --name my-preview --ref main

xo coder list                     # your workspaces
xo coder show my-preview
xo coder start my-preview
xo coder stop my-preview
xo coder ssh my-preview
```

Before every Coder command, xo checks that Coder is logged in to xo's deployment and stops, sending
nothing, if it isn't. Delete workspaces in the Coder dashboard; xo has no delete.

## Other addresses

| To change | Flag | Variable |
|---|---|---|
| the xo server (and so the list) | `--url URL` | `XO_URL` |
| the Coder deployment | `--coder-url URL` | `XO_CODER_URL` |
| the XO key for one command | `--api-key -` (the same hidden prompt) | |

A saved key is only ever sent to the address it was saved with: for a one-off `--url`, give the key
with `--api-key -`.

## How to test it

Each step says what you should see. Nothing here creates anything until step 5.

1. `xo --version` prints `xo 0.1.0`.
2. `xo login` prints the three addresses, then `xo: me: logged in as ...` and
   `xo: coder: logged in to https://dev-coder.xo.builders as ...` (or `skipped` without the Coder CLI).
3. `xo status` shows three lines: me `key saved by xo login`, xo, coder
   `logged in as ...`.
4. `xo spaces --space-id 00000000-0000-0000-0000-000000000000` ends with `HTTP 404: Space not found`:
   your key reaches the platform at dev.xo.builders, and its `/api/core/v1` is there (step 2 checks
   the same way). A 401 means the platform does not know the key. `HTTP 404: XO answered 404 without
   a JSON object` means the platform has no core API at that address; then `xo login` in step 2
   fails the same way and saves nothing.
5. `xo codex --name test-1 --volume-size 20 --dry-run`, then without `--dry-run` to really create.
6. `xo coder github https://github.com/OWNER/REPO --dry-run`, then without `--dry-run`; open the
   `appUrl` it prints.
7. `xo coder list` shows the workspace from step 6.
8. `xo logout`, then `xo status`: `not logged in`.

If a step fails, the line starting `xo:` says why.
