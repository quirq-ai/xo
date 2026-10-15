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
  && ~/.xo/bin/pip install "quirq-xo @ git+https://github.com/quirq-ai/xo@e4dee3e636fe67a090fb4b2079a7c58dc15a34c1" \
  && { grep -qxF 'export PATH="$HOME/.xo/bin:$PATH"' ~/.zshrc 2>/dev/null || echo 'export PATH="$HOME/.xo/bin:$PATH"' >> ~/.zshrc; } \
  && export PATH="$HOME/.xo/bin:$PATH" \
  && xo --version
```

The last line should print `xo 0.1.0`. If any step fails, the ones after it don't run. If you installed Python with Homebrew, use `python3.13 -m venv ~/.xo` on the first line.

The install is pinned to the reviewed commit of xo. The pin covers xo's own code only: pip still fetches its build tool (setuptools) unpinned while it installs.

## 2. Give xo your key (each new terminal)

```sh
printf 'XO API key: '; read -rs XO_API_KEY && export XO_API_KEY; echo
```

Paste your key and press Enter. Nothing shows while you type, and the key stays out of your shell history. The same line works in zsh and bash.

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

To remove xo, delete its folder, remove the PATH line from `~/.zshrc` (or the bash file you used), and clear the key from the current terminal:

```sh
rm -rf ~/.xo
unset XO_API_KEY
```
