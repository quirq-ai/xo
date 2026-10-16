# xo quickstart

Use this to set up `xo`, the command line for XO Cloud, on your own Mac or Linux machine. It works the same for every user.

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
  && ~/.xo/bin/pip install "quirq-xo @ git+https://github.com/quirq-ai/xo@ebc2ae89f462018dd1f606b392bde9ad6e41489b" \
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

xo first prints the address it logs in to (`https://xo.quirq.dev`). Paste your key at the `XO API key:` prompt and press Enter. Nothing shows while you type, and the key stays out of your shell history. xo refuses anything that isn't an XO API key (those start with `ak_`) without sending it anywhere. Then it checks the key with XO, through that address, and saves the key and the address in `~/.config/xo/credentials`, which only you can read. If the check fails, nothing is saved. It also prints the account the key belongs to, when the xo server can tell.

Every later XO API command, in any terminal, uses the saved key and sends it only to the saved address. To use a different key, run `xo login` again. `xo logout` deletes the saved key. A key in `XO_API_KEY` wins over the saved one, so a terminal where you set that variable uses it instead.

### One address for everything

XO API commands go to one address: your creates, `xo spaces`, `xo me` and `xo user-metadata` alike. The xo server answers `xo me` and `xo user-metadata` itself and passes the rest on to XO's platform (`dev.xo.builders`). To use another address, log in to it with `xo login --url https://...`. `XO_URL` or `--url` changes it for one terminal or one command, and the saved key is never sent to a different address than the one you logged in to. Coder commands use their own login and deployment setting; see [Deploy a GitHub repository](coder.md).

## 3. Check it works (creates nothing)

```sh
xo --help
xo codex --help
xo spaces --space-id 00000000-0000-0000-0000-000000000000
```

The last command should print `xo: GET /spaces/00000000-0000-0000-0000-000000000000: HTTP 404: Space not found` and exit with status 1. That 404 means your key works. A 401 means the key is wrong.

## 4. Preview a create (sends nothing)

```sh
xo codex --name my-first-space --volume-size 20 --dry-run
```

This prints the request xo would send, a notice that a real create copies your key into the space, then `xo: dry run, nothing sent`.

## 5. Create a space for real

Before you create anything, know these three things:
- The API can't delete, list, start or stop spaces, and xo has no command for it. A space you create stays, so create only one you mean to keep.
- Every create except `nebius-openclaw` stores your XO API key inside the new space. xo prints a notice saying so as the request goes out; it does not pause or ask first. If the XO team can give you a second key, use that one for creates.
- `xo spaces` prints URLs that carry a session token. Don't share them.

```sh
xo codex --name my-first-space --volume-size 20
```

This creates a real space on your account. Keep the `data.id` it prints. To look at the space again later:

```sh
xo spaces --space-id THE_ID
```

## The commands

To deploy a GitHub repository through Coder instead of the XO core API, see
[Deploy a GitHub repository](coder.md). `xo coder github URL` uses a separate Coder login and
needs no XO API key. It requires a revision of xo that includes the command; the install pin
above predates it.

| Command | Creates |
|---|---|
| `xo codex --name N --volume-size 20` | a Codex space |
| `xo antigravity --name N --volume-size 20` | an Antigravity space |
| `xo claude-code --name N --volume-size 20 --providers-anthropic-api-key -` | a Claude Code space (`-` asks for the key) |
| `xo hermes …`, `xo openclaw …`, `xo xo-cowork …` | spaces with providers and chat channels; see `xo openclaw --help` |
| `xo nebius-openclaw --name N --token-factory-api-key -` | a Nebius OpenClaw space |
| `xo spaces --space-id ID` | nothing; it shows a space you own |

A name is 1 to 32 letters, digits or hyphens, starts with a letter or digit, and can't be `new` or `create`. A name you already used fails with HTTP 500. The volume size is 20 to 100 GB. Add `--dry-run` to any create to see it first.

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
