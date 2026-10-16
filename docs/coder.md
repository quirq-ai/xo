# Deploy a GitHub repository

`xo coder github <url>` creates and starts a workspace from the `xo-github` Coder template,
then prints its dashboard and public app URLs. This is separate from XO Cloud's core API.

## Setup

Install xo using the [quickstart](quickstart.md), using a revision that includes this command,
and install the [Coder CLI](https://coder.com/docs/install/cli). Coder 2.36.4 is tested. Your chosen
deployment must have the `xo-github` template and its `xo-cowork` app slug. The default deployment
is Dev Coder; the command does not install or publish templates.

```sh
xo coder login
xo coder github https://github.com/OWNER/REPO --dry-run
xo coder github https://github.com/OWNER/REPO --name my-preview
```

No XO API key is required. `xo coder login` runs `coder login` for the deployment in your terminal;
Coder keeps the login. In CI, set `CODER_SESSION_TOKEN` and `CODER_URL` to the same deployment;
with the token set and `CODER_URL` missing or naming another one, xo stops with nothing sent. Before any other Coder command, xo runs
`coder whoami` without naming a deployment and stops, with nothing sent, unless Coder is logged in
to this one: on Linux Coder keeps one token and would send it to whatever deployment it is told.

For another deployment, use `--coder-url` or set `XO_CODER_URL`. The resolution order is:
`--coder-url`, `XO_CODER_URL`, `https://dev-coder.xo.builders`; Coder's own `CODER_URL` does not
pick it, and xo removes it (unless it goes with `CODER_SESSION_TOKEN`), `XO_API_KEY` and any `CLERK_*` from the coder process's environment. These
settings affect only Coder commands. XO API commands use `--url`, `XO_URL`, or the address saved by
`xo login`; that address and the saved XO key are not used by Coder commands. The positional GitHub
URL is independent of both deployment settings.
HTTPS is required except for HTTP to localhost, and credentials/query strings/fragments in the
deployment URL are refused. There is no additional xo configuration file.

## Output and lifecycle

Progress goes to stderr. Success prints JSON on stdout, for example:

```json
{
  "name": "my-preview",
  "repoUrl": "https://github.com/OWNER/REPO",
  "workspaceUrl": "https://coder.example.com/@alice/my-preview",
  "appUrl": "https://generated-preview.example.com/"
}
```

Coder resolves `appUrl` from the workspace app and region configuration. It carries no session
token. The template exposes port 3000 publicly and serves a status page while no app is listening.
The initial clone/install/build may continue after Coder reports that the workspace has started.
The command does not open a browser automatically.

Use `xo coder github --help` for options. Shared capacity, a small workspace and a 5 GB disk are
the defaults. `--start-command auto` detects Node apps; `--start-command manual` only clones.
Pass `--ref BRANCH_OR_TAG` to select a Git ref. Custom startup commands must listen on port 3000.
Use a new `--name` for a second deployment of the same repository; an existing workspace is never
changed. Manage existing workspaces with the Coder dashboard or `xo coder list`, `start`, `stop` and `ssh`,
which run the coder command on the checked deployment. Their arguments may not carry Coder's
`--url`, `--token`, `--global-config`, `--use-keyring`, `--header`, `--header-command` or
`--client-tls-*` options (before a `--`), since those would send the token elsewhere or use another
login; xo stops with nothing sent. xo has no delete command: delete a workspace
in the Coder dashboard.

`--timeout` sets the limit for each Coder account/app URL lookup (default 30 seconds).
`--wait-timeout` sets the create/start limit (default 600 seconds). If a create fails, times out or
is interrupted, inspect the dashboard before retrying. If only app URL resolution fails, xo says
the workspace was created and gives its dashboard URL; do not create it again.

Provider keys belong in Coder secrets or private workspace env files. Template parameters,
including startup commands, are visible in Coder. Native provisioner output is deliberately
omitted from xo's output; use the dashboard to inspect build details.
