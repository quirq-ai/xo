# xo quickstart

Use this to set up `xo`, the command line for XO Cloud, on your own Mac or Linux machine. It works the same for every user. Once it is set up, the [user guide](guide.md) lists the commands.

You need:
- Python 3.11 or newer. Check with `python3 --version`. Stock macOS ships Python 3.9, which is too old: install 3.11 or newer from python.org, or run `brew install python@3.13` and use `python3.13` in place of `python3` below.
- On Linux with Python 3.10 or older, install 3.11 or newer from your distribution or python.org and use that `python3.x` on the first line.
- git. On a Mac, `xcode-select --install` provides it.
- On Debian or Ubuntu, the venv module: `sudo apt install python3-venv`.
- An XO API key from the XO team. Each user needs their own key.

## 1. Install (once)

Using bash? Before you paste, change `~/.zshrc` (twice) in the block to `~/.bash_profile` on a Mac or `~/.bashrc` on Linux.

```sh
python3 -m venv ~/.xo \
  && ~/.xo/bin/pip install "quirq-xo @ git+https://github.com/quirq-ai/xo@38361fb016d1d589ee0aa659ef383d166fa536a4" \
  && { grep -qxF 'export PATH="$HOME/.xo/bin:$PATH"' ~/.zshrc 2>/dev/null || echo 'export PATH="$HOME/.xo/bin:$PATH"' >> ~/.zshrc; } \
  && export PATH="$HOME/.xo/bin:$PATH" \
  && xo --version
```

The last line should print `xo 0.1.0`. If any step fails, the ones after it don't run. If you installed Python with Homebrew, use `python3.13 -m venv ~/.xo` on the first line.

The install is pinned to the reviewed commit of xo. The pin covers xo's own code only: pip still fetches its build tool (setuptools) unpinned while it installs.

## 2. Log in with your key (once)

```sh
xo login
```

xo first prints the services it logs in to, on rows `me`, `xo` and `github`: the xo server (`https://xo.quirq.dev`), XO's platform (`https://dev.xo.builders`, reached through the xo server with the same key) and GitHub (`https://github.com`, through the GitHub CLI). Run `xo login` on its own, not pasted together with other commands: the prompt drops anything typed before it. If the xo server has browser login set up, xo prints a link and opens it instead of asking: sign in to XO in your browser, and xo gets your key from the xo server (`xo login --key` asks for a key either way). Otherwise, paste your key at the `XO API key (hidden: paste it, then press Enter):` prompt and press Enter. Nothing shows while you type, and the key stays out of your shell history. xo refuses anything that isn't an XO API key (those start with `ak_`) without sending it anywhere. Otherwise it prints `checking the key with XO` and checks the key with XO, through that address; the check can take up to a minute when XO is slow. If the check passes, xo saves the key and the address in `~/.config/xo/credentials`, which only you can read; if it fails, nothing is saved. It also prints the account the key belongs to, when the xo server can tell. If the [GitHub CLI](https://cli.github.com) (`gh`) is installed, it then checks that gh is connected to github.com, and runs gh's own sign-in in your browser if it isn't; gh asks whether to use that login for git too (see the [user guide](guide.md#log-in)). Without gh, its step says `skipped`. `xo status` shows where you are logged in.

Every later XO API command, in any terminal, uses the saved key and sends it only to the saved address. To use a different key, run `xo login` again. `xo logout` deletes the saved key. A key in `XO_API_KEY` wins over the saved one, so a terminal where you set that variable uses it instead.

### One address for everything

XO API commands go to one address: your creates, `xo spaces` and `xo me` alike (bare `xo spaces` lists the spaces linked to your account; `--space-id ID` asks XO for one). The xo server answers `xo me` and bare `xo spaces` itself and passes the rest on to XO's platform (`dev.xo.builders`). To use another address, log in to it with `xo login --url https://...`. `XO_URL` or `--url` changes it for one terminal or one command, and the saved key is never sent to a different address than the one you logged in to.

## 3. Check it works (creates nothing)

```sh
xo --help
xo create space NAME codex --help
xo spaces --space-id 00000000-0000-0000-0000-000000000000
```

The last command should print `xo: GET /spaces/00000000-0000-0000-0000-000000000000: HTTP 404: Space not found` and exit with status 1. That 404 means your key works. A 401 means the key is wrong.

## 4. Preview a create (sends nothing)

```sh
xo create space my-first-space codex --volume-size 20 --dry-run
```

This prints the request xo would send, a notice that a real create copies your key into the space, then `xo: dry run, nothing sent`.

## 5. Create a space for real

Before you create anything, know these three things:
- The API can't delete, list, start or stop spaces, and xo has no command for it (`xo list spaces` lists only the spaces xo recorded). A space you create stays, so create only one you mean to keep.
- Every create except `nebius-openclaw` stores your XO API key inside the new space. xo prints a notice saying so as the request goes out; it does not pause or ask first. If the XO team can give you a second key, use that one for creates.
- `xo spaces --space-id ID` prints URLs that carry a session token. Don't share them.

```sh
xo create space my-first-space codex --volume-size 20
```

This creates a real space on your account and records it in your spaces. `xo list spaces` shows it
again later, with its XO id; to see XO's own view of it, with its connection URLs:

```sh
xo spaces --space-id THE_ID
```

## The commands

| Command | Creates |
|---|---|
| `xo create space N codex --volume-size 20` | a Codex space |
| `xo create space N antigravity --volume-size 20` | an Antigravity space |
| `xo create space N claude-code --volume-size 20 --providers-anthropic-api-key -` | a Claude Code space (`-` asks for the key) |
| `xo create space N hermes …`, `… openclaw …`, `… xo-cowork …` | spaces with providers and chat channels; see `xo create space N openclaw --help` |
| `xo create space N nebius-openclaw --token-factory-api-key -` | a Nebius OpenClaw space |
| `xo create project N` or `xo create project https://github.com/OWNER/REPO` | a project: an empty repository at `./N`, or a clone at `./REPO`, called `REPO` |
| `xo create space` | this folder as a space named after it: one branch per git repository in it, in its `.git` (made, or the one already there), pushed to GitHub after you say yes, recorded in your spaces, nothing on XO |
| `xo create space N` | the same for the folder `./N`, made if it is not there |
| `xo spaces --space-id ID` | nothing; it shows a space you own |

With a kind, a space's name N is 1 to 32 letters, digits or hyphens, starts with a letter or digit, and can't be `new` or `create`; it is its name on XO and in your spaces. A name you already used fails. The volume size is 20 to 100 GB. Add `--dry-run` to any create on XO to see it first; `xo create space N` with no kind sends no create to XO and takes no `--dry-run`.

`nebius-openclaw` is behind a feature flag on XO Cloud; when the flag is off for your account it answers HTTP 404.

When you give a provider key or bot token as `-`, xo asks for it without showing it. Anything you type directly on the command line lands in your shell history.

## Update or remove xo

To update xo, replace `NEW_SHA` with the commit you were given:

```sh
~/.xo/bin/pip install --force-reinstall --no-deps "quirq-xo @ git+https://github.com/quirq-ai/xo@NEW_SHA"
```

To see which commit you have: `~/.xo/bin/pip freeze | grep quirq-xo`.

To remove xo, delete the saved key, then xo's folder, and remove the PATH line from `~/.zshrc` (or the bash file you used):

```sh
xo logout
rm -rf ~/.xo
unset XO_API_KEY
```
