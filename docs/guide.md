# xo user guide

`xo` is one command line for two XO services and GitHub. One `xo login` logs you in to all of them.

| Service | Address | What it does | You log in with |
|---|---|---|---|
| me | `https://xo.quirq.dev` | the xo server: answers `xo me`, holds the list of services, passes creates on to the platform | your XO API key (`ak_...`) |
| xo | `https://dev.xo.builders` | XO Cloud: creates spaces (Claude Code, Codex, OpenClaw, ...) | the same key, through me |
| github | `https://github.com` | your GitHub account, through the GitHub CLI (`gh`) | gh's own login, in your browser |

xo saves your keys only on your machine; the xo server stores none. Your XO key is still sent to
the xo server and the platform on every call, and a create copies it into the new space.

**Changed:** XO's platform was `https://app.xo.builders` before this version. Creates now go to
`https://dev.xo.builders`.

## Install

Follow the [quickstart](quickstart.md) step 1 (Python 3.11 or newer, one paste). For `github`,
also install the [GitHub CLI](https://cli.github.com) (`brew install gh` on a Mac).

## Log in

```sh
xo login          # all services
xo login xo       # only the XO key (me and xo)
xo login github   # only GitHub (gh auth login, unless gh is already connected)
xo status         # each service, its address, and whether you are logged in
xo logout         # deletes your XO key; logs gh out (asks first)
```

`xo login` prints the addresses first. Then:
- **me / xo:** if a key you saved before still works, it says "already logged in" and doesn't ask.
  Otherwise, when the xo server has browser login set up, xo prints a link and opens it: sign in
  to XO in your browser, and the browser comes back to xo on this machine (`127.0.0.1`). The xo
  server then sends xo your account's key: the one browser login made you before, or a new one
  that works for 90 days. Over SSH the browser cannot come back to the remote machine: connect
  with `ssh -L 8976:127.0.0.1:8976` (the port xo prints), or use `xo login --key`. Without browser
  login, with `xo login --key`, or with a key piped in, paste your XO API key at the prompt
  (nothing shows while you type).
- **github:** xo asks GitHub who gh is logged in as. If that works, it says "already connected".
  Otherwise it runs `gh auth login` for github.com, which opens your browser; then it checks again.
  xo never sees or saves the GitHub token: gh keeps it. If `GH_TOKEN` or `GITHUB_TOKEN` is set in
  your terminal, gh uses that instead, and xo says so.
  gh may then ask "Authenticate Git with your GitHub credentials?" (not when it already is git's
  login), and xo prints a line saying so first. Yes (gh's default) makes gh git's login for
  github.com, which changes `~/.gitconfig` or the github.com login your system keeps for git (the
  Keychain on a Mac). Answer `n` to leave git as it is.

The key and github each end with one line (`xo: me: ...`, `xo: github: ...`): logged in, already logged in, skipped (CLI not installed), or failed and why. One
failure does not stop the others.

## Create spaces (xo)

```sh
xo --help                                        # every command
xo create space --help                           # the kinds of space
xo create space NAME claude-code --help          # the flags of one kind

xo create space my-cc claude-code --volume-size 20 --providers-anthropic-api-key -
                                                 # my-cc on XO, recorded in your spaces as my-cc
xo create space my-codex codex --volume-size 20
xo create space my-ag antigravity --volume-size 20
xo create space dev                              # no KIND: a space here only, ./dev, nothing on XO
xo create space NAME openclaw --help             # needs a provider key and a chat-channel token
xo create space NAME hermes --help
xo create space NAME xo-cowork --help
xo spaces --space-id THE_ID                      # a space you created, with its URLs
xo me                                            # your profile: name, image, email, public and private metadata
xo projects                                      # the projects list in your private metadata
xo projects add my-app                           # add one; the server writes only that list
xo create project https://github.com/juanfont/headscale
                                                 # called headscale, cloned into ./headscale; your
                                                 # projects list shows it with its owner beside it
xo create project headscale https://github.com/juanfont/headscale   # the same: a name and a URL must match
xo create project notes                          # a project with no GitHub remote: ./notes, git init
xo connect notes --repo https://github.com/OWNER/REPO
                                                 # give it its repo later: called REPO from then on;
                                                 # ./notes gets that origin if it has none and moves
                                                 # to ./REPO (not moved if its origin is another repo)
xo connect headscale my-cc --ref main            # put a project in a space, at a branch, tag or sha
xo connect https://github.com/OWNER/REPO my-cc   # not in your projects yet: xo adds it, then puts
                                                 # it in my-cc (xo install my-cc clones it)
xo connect dev                                   # a new space entry in your metadata, no XO space
xo connect old --id THE_ID                       # an entry for an XO space you already have, or
                                                 # the id for an entry made without one
xo spaces                                        # your spaces, with their projects and refs (also xo list spaces)
xo list                                          # your projects and your spaces together
xo install space:dev                             # ./dev/REPO for each project, at its ref
xo update space:dev                              # push each branch under ./dev, record the refs
xo sync space:dev                                # rebase and push each branch, merge the refs, record
xo share space:dev bob                           # invite bob to a copy of dev (its name and projects)
xo list shared                                   # the invites waiting on you, each with an id
xo accept 0123abcd0123abcd                       # a shared space: show it, ask, then add it as your own
xo accept 0123abcd0123abcd --as dev2             # the same, when you already have a space called dev
xo accept 4567cdef4567cdef --space dev           # a shared project: into your space dev, at main
xo reject 0123abcd0123abcd                       # take it off (its sender can share it again)
xo install project:headscale                     # clone it here, or fetch and fast-forward it
xo update project:headscale                      # git push its branch (never forced)
xo sync project:headscale                        # git pull --rebase, then push
```

- `xo connect` takes plain names and works out which is which from your projects: `xo connect
  headscale test2` puts the project headscale in the space test2 (`xo connect test2 headscale`
  too), and `xo connect dev` makes a space entry. When both names are your projects, the one that
  is also a space is the space (refused if both or neither are). For a space named like one of your
  projects, say `xo connect space:NAME`; `project:` and `space:` in front still work everywhere.
- A project you name in `xo connect PROJECT SPACE` that is not in your projects yet is added first,
  as `xo create project` does but without making its folder (`xo install` does that), then put in
  the space; xo says so on stderr. Give it as `OWNER/REPO` or a GitHub URL for a repo; a plain
  name becomes a project with no repo, which `xo connect NAME --repo URL` can give one later.
  When neither name is your project, the one that is your space is the space (refused if both
  are). Nothing is added when the space is missing.
- `xo install`, `update` and `sync` use your own git and its login, over https only and without
  submodules. A space's projects go in `./SPACE/REPO`; `./SPACE/.xo/state.json` remembers the
  space's rev and refs as installed. A project with no GitHub remote is left as it is.
  - A GitHub project is recorded as `OWNER/REPO` and called by its `REPO` in every command, in
    `./REPO`. When another of your projects (or of the space's) is called the same, say a fork and
    its upstream, or a plain project, call it by its `OWNER/REPO`; its folder is `./OWNER/REPO`,
    as before short names. A plain project keeps its own name. A repo called `.xo` always goes in
    `./OWNER/.xo`, since `.xo` holds a space's own state.
  - xo looks for a checkout in `./REPO` and `./OWNER/REPO` (where an older xo put it) and uses it
    only when its origin, every fetch and push URL, is that repository in an https, `git@` or
    `ssh://` form. A folder whose origin is another repository, or carries a login
    (`https://USER@github.com/...`), is refused: move it, or point its origin at the repository.
  - `xo install`, `update` and `sync project:P` look P up in your projects first, so they need the
    xo server.
  - `xo create space NAME` with no KIND makes the space entry and `./NAME` here, already
    installed, so `xo connect P NAME` and `xo update space:NAME` work on it. NAME is
    lowercased, as on XO. If the folder could not be made, `xo install space:NAME` makes it.
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
  - Accepting a space copies its name and projects into your own metadata as a new space at rev 1.
    Accepting a project puts it in the space of yours you name with `--space`, at `--ref` (default
    `main`). Either way the projects are added to your projects too, which xo needs to install and
    update them. Later changes on either side do not reach the other.
  - Invites count toward your 6KB, but all of them together take at most 1.5KB; accepting or
    rejecting one gives its room back.
  - A share to a username that does not exist, or to someone whose list is full, gets the same
    "could not share" answer. After a reject the sender can share again. A share that works
    does tell you the username exists.
  - Only GitHub projects can be shared, since nobody else could install a local one.
  - `xo list shared` shows each sender's username now. An invite sent again gets a new id, so
    `xo accept` never takes something other than what you saw.
- `xo claude-code`, `xo codex` and the other old create commands are gone; each says to use
  `xo create space NAME KIND`.
- A project is named after its repo. `xo connect P --repo URL` is refused while P is in a
  space, since the space would still name it P. It gives `./P` that origin only when it has none,
  and leaves the folder where it is unless every fetch and push URL of its origin is that
  repository, so `xo update` never pushes to the wrong place.
- XO keeps space names in lowercase, so `xo create space MyDev codex` creates and records `mydev`,
  and says so.
- Add `--dry-run` to `xo create space` to see the request without sending it.
  `xo create project` and `xo connect` have no `--dry-run`.
- `-` makes xo ask for a key or token without showing it. Only one `-` works per command; put any
  other secret in a JSON file and pass it with `--data @FILE`. The file holds part of the request
  body, and the flags you give are added to it. For example, `body.json` holding
  `{"providers": {"anthropic": {"enabled": true, "apiKey": "sk-ant-..."}}}` with
  `xo create space my-cc claude-code --volume-size 20 --data @body.json` (leave `name` out of the
  file: NAME sets it). Make the file readable only by
  you (`chmod 600 body.json`) and delete it afterwards.
- Every create except `nebius-openclaw` copies your XO key into the new space, and xo says so as it
  sends.
- XO has no delete or list: `xo list spaces` shows the spaces xo recorded, with their XO ids.

## Other addresses

| To change | Flag | Variable |
|---|---|---|
| the xo server (and so the list) | `--url URL` | `XO_URL` |
| the XO key for one command | `--api-key -` (the same hidden prompt) | |

A saved key is only ever sent to the address it was saved with: for a one-off `--url`, give the key
with `--api-key -`.

## How to test it

Each step says what you should see. Nothing here creates anything until step 5.

1. `xo --version` prints `xo 0.1.0`.
2. `xo login` prints the three addresses, then `xo: me: logged in as ...` and
   `xo: github: connected to github.com as ...` (or `skipped` without gh).
3. `xo status` shows three lines: me `key saved by xo login`, xo, and github `logged in as ...`.
4. `xo spaces --space-id 00000000-0000-0000-0000-000000000000` ends with `HTTP 404: Space not found`:
   your key reaches the platform at dev.xo.builders, and its `/api/core/v1` is there (step 2 checks
   the same way). A 401 means the platform does not know the key. `HTTP 404: XO answered 404 without
   a JSON object` means the platform has no core API at that address; then `xo login` in step 2 fails the same way and saves nothing.
5. `xo create space test-1 codex --volume-size 20 --dry-run`, then without `--dry-run` to really
   create; `xo list spaces` then shows test-1.
6. `xo logout`, then `xo status`: `not logged in` for me, and for github if you answered yes to
   logging gh out. Answer N and it stays logged in.

If a step fails, the line starting `xo:` says why.
