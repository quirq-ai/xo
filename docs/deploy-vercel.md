# Deploy the xo server on Vercel and test it

The xo server (`xo serve`, see [server.md](server.md)) runs on Vercel at `https://xo.quirq.dev`.
Vercel runs the repo's root `app.py`, which serves the same routes as a Python function. Nothing
in this repo deploys it: you do, with the steps below.

Test with **development** credentials first. The Secret Key and the API key you test with must
come from the same Clerk instance: a production key checked against a development Secret Key is
"Invalid API key".

## What you need

- A Vercel account on the team that owns `quirq.dev`, and the [Vercel CLI](https://vercel.com/docs/cli)
  (`npm i -g vercel`, then `vercel login`).
- From XO's Clerk **development** instance: its Secret Key (`sk_test_...`), and an API key
  (`ak_...`) that belongs to a test user of that instance.
- Python 3.11 or newer, for the `xo` command.

## 1. Create the project

1. In Vercel, **Add New → Project**, import `quirq-ai/xo`, name it `xo`.
2. Framework preset **Other**. Leave the build and output settings empty; Vercel finds `app.py`.
3. Under **Environment Variables**, add `CLERK_SECRET_KEY` = your `sk_test_...` key, for
   **Production** only, marked **Sensitive**. Add nothing else: with no admin key ids, the
   metadata routes stay closed (403) and only `/me` is in use.
4. Deploy. While this branch is not merged, the first deploy from `main` has no `app.py` and
   fails or serves nothing; that is expected. Step 3 deploys the branch.

## 2. Add the domain

1. In the project, **Settings → Domains**, add `xo.quirq.dev` to Production.
2. If `quirq.dev`'s DNS is on Vercel, the record is made for you. Otherwise add the record Vercel
   shows (a `CNAME` for `xo` to Vercel's target) where `quirq.dev`'s DNS lives.
3. Wait until the domain shows **Valid Configuration** and a certificate.

## 3. Deploy this branch to production (before it is merged)

Preview deployments sit behind Vercel's login, so the `xo` command cannot reach them. Deploy the
branch to production from a checkout instead:

```bash
git clone --branch claude/xo-cli-5ynop4 https://github.com/quirq-ai/xo.git xo-deploy
cd xo-deploy
vercel link        # pick the team and the xo project
vercel --prod
```

After this PR is merged, pushes to `main` deploy production by themselves.

## 4. Test

**Health**, which needs no key and makes no Clerk call:

```bash
curl -sS https://xo.quirq.dev/healthz
```

You should see `{"success": true, "data": {"status": "ok"}}`. A 503 with "xo server misconfigured"
means `CLERK_SECRET_KEY` is missing or Clerk refused it; the function logs say which.

**The command line**, installed from this branch:

```bash
python3 -m venv ~/.xo-test && . ~/.xo-test/bin/activate
pip install "quirq-xo @ git+https://github.com/quirq-ai/xo@claude/xo-cli-5ynop4"
read -rs XO_API_KEY && export XO_API_KEY    # paste the test user's ak_... key; nothing shows
xo me
```

`xo me` prints the test user's `id`, `username`, `firstName`, `lastName`, `imageUrl` and
`createdAt`, and never an email or metadata.

**What should be refused:**

| Try | Expect |
|---|---|
| `XO_API_KEY=ak_wrong xo me` | `HTTP 401 (check the API key): Invalid API key` |
| `XO_API_KEY=nonsense xo me` | the same 401, without a call to Clerk |
| `xo user-metadata get --user-id user_x` with your key | `HTTP 403 (the key lacks access to this): Admin key required` |

When you are done: `unset XO_API_KEY; deactivate`.

## 5. Read the logs

In the project, **Logs** (runtime) show one line per request, and every `/user-metadata` request
adds one JSON audit line: the key's id, the caller, the target user and the outcome, never a key or
a metadata value. Vercel keeps runtime logs only for a short time on most plans; if you need them
longer, add a log drain.

## 6. Limit requests per address

The server caps its own calls to Clerk (`XO_CLERK_PER_SECOND`, default 20) and limits each key, but
on Vercel those limits apply per function instance. If your plan has it, add a **Firewall** rate
limit rule for `xo.quirq.dev` on paths `/me` and `/user-metadata`, keyed by IP, for example 60
requests a minute.

## Going to production credentials

Repeat step 1's variable with the production Secret Key (`sk_live_...`), redeploy, and test `xo me`
with a real XO key. Then a user only needs the `xo` command: `https://xo.quirq.dev` is its default
server.

If the Secret Key leaks or is rotated: create a new one in the Clerk Dashboard, replace
`CLERK_SECRET_KEY` in Vercel, redeploy, check `/healthz` and `xo me`, then delete the old key.
