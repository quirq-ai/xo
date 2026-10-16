# xo user guide

`xo` is one command line for three XO services and GitHub. One `xo login` logs you in to all of them.

| Service | Address | What it does | You log in with |
|---|---|---|---|
| me | `https://xo.quirq.dev` | the xo server: answers `xo me`, holds the list of services, passes creates on to the platform | your XO API key (`ak_...`) |
| xo | `https://dev.xo.builders` | XO Cloud: creates spaces (Claude Code, Codex, OpenClaw, ...) | the same key, through me |
| coder | `https://dev-coder.xo.builders` | Coder workspaces: GitHub previews, ssh | Coder's own login, in your browser |
| github | `https://github.com` | your GitHub account, through the GitHub CLI (`gh`) | gh's own login, in your browser |

xo saves your keys only on your machine; the xo server stores none. Your XO key is still sent to
the xo server and the platform on every call, and a create copies it into the new space.

**Changed:** XO's platform was `https://app.xo.builders` before this version. Creates now go to
`https://dev.xo.builders`.

## Install

Follow the [quickstart](quickstart.md) step 1 (Python 3.11 or newer, one paste). For the `coder`
service, also install the [Coder CLI](https://coder.com/docs/install/cli); for `github`, the
[GitHub CLI](https://cli.github.com) (`brew install gh` on a Mac).

## Log in

```sh
xo login          # all services
xo login xo       # only the XO key (me and xo)
xo login coder    # only Coder
xo login github   # only GitHub (gh auth login, unless gh is already connected)
xo status         # each service, its address, and whether you are logged in
xo logout         # deletes your XO key; logs Coder and gh out (asks first for each)
```

`xo login` prints the addresses first. Then:
- **me / xo:** paste your XO API key at the prompt (nothing shows while you type).
  If a key you saved before still works, it says "already logged in" and doesn't ask.
- **coder:** Coder opens its sign-in page in your browser; paste back the token it shows. If Coder is
  already logged in to xo's deployment, it says "already logged in". If Coder is logged in somewhere
  else, xo asks before replacing that login.
- **github:** xo asks GitHub who gh is logged in as. If that works, it says "already connected".
  Otherwise it runs `gh auth login` for github.com, which opens your browser; then it checks again.
  xo never sees or saves the GitHub token: gh keeps it. If `GH_TOKEN` or `GITHUB_TOKEN` is set in
  your terminal, gh uses that instead, and xo says so.
  gh may then ask "Authenticate Git with your GitHub credentials?" (not when it already is git's
  login), and xo prints a line saying so first. Yes (gh's default) makes gh git's login for
  github.com, which changes `~/.gitconfig` or the github.com login your system keeps for git (the
  Keychain on a Mac). Answer `n` to leave git as it is.

The key, coder and github each end with one line (`xo: me: ...`, `xo: coder: ...`,
`xo: github: ...`): logged in, already logged in, skipped (CLI not installed), or failed and why. One
failure does not stop the others.

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
xo me                                            # your profile: name, image, email, public and private metadata
xo projects                                      # the projects list in your private metadata
xo projects add my-app                           # add one; the server writes only that list
xo create project https://github.com/OWNER/REPO  # add it to your projects and clone it here
xo create project notes                          # a project with no GitHub remote: ./notes, git init
xo create space claude-code dev --name my-cc --volume-size 20 --providers-anthropic-api-key -
                                                 # create it as `xo claude-code` does, record it as dev
xo connect project:OWNER/REPO space:dev --ref main   # put a project in a space, at a branch, tag or sha
xo connect space:dev                             # a new space entry in your metadata, no XO space
xo connect space:old --id THE_ID                 # an entry for an XO space you already have, or
                                                 # the id for an entry made without one
xo list spaces                                   # your spaces, with their projects and refs
xo install space:dev                             # ./dev/OWNER/REPO for each project, at its ref
xo update space:dev                              # push each branch under ./dev, record the refs
xo sync space:dev                                # rebase and push each branch, merge the refs, record
xo share space:dev bob                           # invite bob to a copy of dev (its name and projects)
xo list shared                                   # the invites waiting on you, each with an id
xo accept 0123abcd                               # show what it adds, ask, then add it to your metadata
xo accept 0123abcd --as dev2                     # the same, when you already have a space called dev
xo reject 0123abcd                               # take it off; its sender cannot re-invite you for 7 days
xo install project:OWNER/REPO                    # clone it here, or fetch and fast-forward it
xo update project:OWNER/REPO                     # git push its branch (never forced)
xo sync project:OWNER/REPO                       # git pull --rebase, then push
```

- `xo install`, `update` and `sync` use your own git and its login, over https only and without
  submodules. A space's projects go in `./SPACE/OWNER/REPO`; `./SPACE/.xo/state.json` remembers the
  space's rev and refs as installed. A project with no GitHub remote is left as it is.
  - `update` first pushes each checkout's branch to its upstream (refused if the remote is ahead:
    run `xo sync`). Then it sends the branch (or, when detached, the commit) each checkout is on,
    and is refused when the space changed since: run `xo sync`. A ref its remote does not have is
    refused too, so nobody else's install breaks. With nothing changed it records nothing.
  - `sync` first rebases each checkout's branch on its upstream and pushes it, then takes the
    space's changes and keeps yours; if both changed the same project (or the space dropped one you
    changed) it stops and changes nothing. If git cannot rebase a branch cleanly, xo undoes the
    rebase and says so.
  - A checkout with uncommitted changes is never touched, and a folder that is not a checkout of
    its own is refused. Pushes are never forced, go to the branch's upstream remote (not a
    `pushRemote`), and need a branch (not a detached HEAD). Tags and commits are left where they
    are.
- `xo share` puts an invite in the other user's `shared` list; nothing else of theirs is changed
  or shown to you. A space's local projects are left out of it.
  - Accepting copies the space's name and projects into your own metadata as a new space at rev 1,
    and adds its projects to your projects. Later changes on either side do not reach the other.
  - Invites have their own small room in your metadata, so they never stop your own changes.
  - A share to a username that does not exist, or to someone whose list is full or who rejected it
    this week, gets the same "could not share" answer. A share that works does tell you the
    username exists.
  - Only GitHub (OWNER/REPO) projects can be shared, since nobody else could install a local one.
  - `xo list shared` shows each sender's username now. An invite sent again gets a new id, so
    `xo accept` never takes something other than what you saw.
- Add `--dry-run` to `xo KIND` or `xo create space` to see the request without sending it.
  `xo create project` and `xo connect` have no `--dry-run`.
- `-` makes xo ask for a key or token without showing it. Only one `-` works per command; put any
  other secret in a JSON file and pass it with `--data @FILE`. The file holds part of the request
  body, and the flags you give are added to it. For example, `body.json` holding
  `{"providers": {"anthropic": {"enabled": true, "apiKey": "sk-ant-..."}}}` with
  `xo claude-code --name my-cc --volume-size 20 --data @body.json`. Make the file readable only by
  you (`chmod 600 body.json`) and delete it afterwards.
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
2. `xo login` prints the four addresses, then `xo: me: logged in as ...`,
   `xo: coder: logged in to https://dev-coder.xo.builders as ...` and
   `xo: github: connected to github.com as ...` (or `skipped` without the Coder CLI or gh).
3. `xo status` shows four lines: me `key saved by xo login`, xo, coder `logged in as ...`, github
   `logged in as ...`.
4. `xo spaces --space-id 00000000-0000-0000-0000-000000000000` ends with `HTTP 404: Space not found`:
   your key reaches the platform at dev.xo.builders, and its `/api/core/v1` is there (step 2 checks
   the same way). A 401 means the platform does not know the key. `HTTP 404: XO answered 404 without
   a JSON object` means the platform has no core API at that address; then `xo login` in step 2 fails the same way and saves nothing.
5. `xo codex --name test-1 --volume-size 20 --dry-run`, then without `--dry-run` to really create.
6. `xo coder github https://github.com/OWNER/REPO --dry-run`, then without `--dry-run`; open the
   `appUrl` it prints.
7. `xo coder list` shows the workspace from step 6.
8. `xo logout`, then `xo status`: `not logged in` for me, and for coder and github if you answered
   yes to logging them out. Answer N and they stay logged in.

If a step fails, the line starting `xo:` says why.
