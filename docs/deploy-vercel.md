# Deploy the xo server on Vercel and test it

The xo server (`xo serve`, see [server.md](server.md)) runs on Vercel at `https://xo.quirq.dev`.
Vercel runs the repo's root `app.py`, which serves the same routes as a Python function, on
Python 3.12 (pinned by `.python-version`). Vercel's GitHub integration deploys it: every commit
on `main` goes to production by itself, so every merge to `main` ships `xo.quirq.dev`. Nothing in
this repo deploys it, and the steps below are yours, followed by the test script.

Use **development** credentials only (`sk_test_...`) until the test passes and the production gate
at the end is passed. The Secret Key and the API key you test with must come from the same
Clerk instance: a key from another instance is refused as "Invalid API key". XO's platform
(`XO_CORE_API_URL`, dev.xo.builders by default) checks keys with that same instance (section 6),
so such a key fails the creates and `xo spaces --space-id ID` too, and `xo login` saves nothing.

## What you need

- A Vercel account on the team that owns `quirq.dev`, and the Vercel CLI (`npm i -g vercel`, then
  `vercel login`).
- From XO's Clerk **development** instance: its Secret Key (`sk_test_...`), and an API key
  (`ak_...`) that belongs to a test user of that instance.
- Python 3.11 or newer and `curl`, for the test script.

## 1. Create the Vercel project

1. **Add New → Project**, import `quirq-ai/xo`, name it `xo`.
2. **Framework Preset:** **Python** (Vercel should pick it on its own from `pyproject.toml`; choose
   it if not). **Root Directory:** the repo root. Leave the preset's build, output and install
   settings as they are. The Python preset is what runs `app.py`: it routes every path to its
   top-level `app`. With "Other", no function runs and `/healthz` fails.
3. **Environment Variables:** add exactly one, `CLERK_SECRET_KEY` = the `sk_test_...` key, for the
   **Production** environment only, marked **Sensitive**. Add no admin key ids: with none, the
   metadata routes answer 403 to everyone and only `/me` is in use. Without `XO_CORE_API_URL`,
   creates go to XO's platform at `https://dev.xo.builders` (it was `https://app.xo.builders` before
   this version); add `XO_CORE_API_URL` only to send them somewhere else.
4. Leave the **Production Branch** at `main` (**Settings → Git**) and the GitHub integration on.
   The first deploy builds `main`.

## 2. Add xo.quirq.dev

1. **Settings → Domains**, add `xo.quirq.dev` to Production.
2. If `quirq.dev`'s DNS is on Vercel, the record is added for you. Otherwise add the record Vercel
   shows there (a `CNAME` named `xo` pointing to the target Vercel gives) where `quirq.dev`'s DNS
   is hosted.
3. Wait for **Valid Configuration** and the certificate.

## 3. Add the rate limit rule (recommended, where your plan has it)

The server limits calls to Clerk (`XO_CLERK_PER_SECOND`, default 20 a second), requests passed on
to XO (`XO_CORE_PER_SECOND`, default 10 a second) and requests per key, but on Vercel each function
instance keeps its own counts, and Vercel can run many instances at once. So these limits slow a
flood down; they do not stop it. A per-address limit can only be Vercel's. Add it if your plan
offers rate limit rules under **Firewall**; without it the server's own limits are all there is,
and the test script's last check fails:

1. **Firewall → Rules → New rule**, named `xo per-IP limit`:
   - **If** Request Path **starts with** `/me`, **or** Request Path **starts with** `/user-metadata`,
     **or** Request Path **starts with** `/api/core`;
   - **Then** **Rate Limit**: fixed window, **60 requests per 60 seconds**, keyed by **IP**;
   - past the limit: the default action (`rate_limit`, "too many requests"). Deny also works.
2. **Save**, then **Review Changes → Publish**. A rule is a draft and does nothing until it is
   published. The test script's last check sends up to 130 requests and fails if the rule is not
   active.

The same rule from the Vercel CLI, run inside a checkout linked with `vercel link` (taken from the CLI's
source and not run by Claude; the dashboard steps above are enough):

```bash
vercel firewall rules add "xo per-IP limit" \
  --condition '{"type":"path","op":"pre","value":"/me"}' --or \
  --condition '{"type":"path","op":"pre","value":"/user-metadata"}' --or \
  --condition '{"type":"path","op":"pre","value":"/api/core"}' \
  --action rate_limit --rate-limit-window 60 --rate-limit-requests 60 \
  --rate-limit-keys ip --rate-limit-action rate_limit --yes
vercel firewall publish
```

If the rule already exists without `/api/core`, edit it to add that condition and publish again:
the server passes anything under `/api/core` that looks like an API key on to XO, so without it one
address can send XO as many requests as the per-key limit allows for each made-up key.

If your plan does not offer rate limit rules, tell Claude before you go on: without the rule,
anyone can make the server spend Clerk requests (1000 Backend API calls per 10 seconds for the whole instance)
and, past 100,000 a month, paid key checks. Through `/me` that is the server's own Clerk instance,
the development one on development credentials. Through `/api/core` it is the same instance
(section 6), reached another way: a request there goes on to XO's platform (`XO_CORE_API_URL`,
dev.xo.builders by default), which checks the key itself, so the server's own Clerk budget
(`XO_CLERK_PER_SECOND`) does not count it, and both paths use up one instance's limit. So the
`/api/core` condition matters from the day the forwarding is deployed, not only once the server
holds production credentials.

## 4. How it deploys

- **Production:** every commit on `main` (each merged pull request) deploys to production and
  `xo.quirq.dev` by itself. A merge is a release: merge only what was reviewed.
- **Previews:** every other branch and pull request gets a preview deployment. Previews have no
  `CLERK_SECRET_KEY` (it is set for Production only), so they answer 503 "xo server
  misconfigured". That is expected, and it keeps the Secret Key out of any branch's build.
- **Rolling back:** in **Deployments**, pick the last good production deployment and choose
  **Instant Rollback**, then fix `main`. Vercel's documentation says a rollback turns off the
  automatic assignment of production domains: later merges to `main` still build, but they do not
  reach `xo.quirq.dev`. Once the fix is merged and its deployment is **Ready**, **Promote** it to
  production to turn automatic deploys back on, then run the test script. Not **Undo Rollback**:
  that puts back the deployment you rolled back from, the broken one. Also from the
  documentation: on the Hobby plan, Instant Rollback can go back only to the previous production
  deployment.
- `.vercelignore` keeps `.env` files and the tests out of every upload.

## 5. Test

From a checkout at the commit that is deployed: its sha is on the production deployment in
**Deployments**. Paste it into the first line:

```bash
SHA=paste-the-deployed-commit-sha-here
git clone https://github.com/quirq-ai/xo.git && cd xo && git checkout --detach "$SHA" &&
  bash docs/test-xo-server.sh
```

The script runs only if the checkout worked, so a sha left as the placeholder stops it.

It runs `xo` straight from the checkout (nothing is installed or downloaded, and it calls only
`xo.quirq.dev`, which passes one request with a made-up key on to XO), checks `/healthz`, asks for the test user's `ak_...` key without echoing it, and
checks:

| Check | Expected |
|---|---|
| `/healthz` | `ok` |
| your key's format | `ak_`, then letters, digits and `_ . - + / =` only (checked without showing it; anything else stops the script before the key is sent) |
| `xo me` with your key | your profile under `xo`: `id`, `username`, `firstName`, `lastName`, `imageUrl`, `createdAt`, `email`, `public_data`, `private_data`, nothing else |
| `xo spaces --space-id ID` with a made-up key | XO's 401 (or its 404 "Space not found"), passed back by the server; your key is not used. A bare "Not found" means the deployment is older than the forwarding |
| a wrong `ak_` key | 401 |
| a string that is not a Clerk key | 401, without a call to Clerk |
| `/user-metadata/user_test` with your key | 403 (no admin keys yet) |
| up to 130 requests to `/me` with no key | refused (429, or 403 for a Deny rule) after about 60, or up to 120 if the rule's minute ends mid-burst; any other answer is a FAIL. This holds your address back for up to a minute |
| then one request to `/api/core/v1/spaces/0000…` with no key | 429 or 403: the same rule covers `/api/core`. A 401 means the `/api/core` condition is missing or not published. This assumes Vercel counts one rule's paths together for an address, which has not been checked against Vercel |

It prints PASS or FAIL per line and ends with "All checks passed." The key is never printed, saved,
put on a command line or exported; only the `xo` calls that need it get it. If the format check
fails, tell Claude which kinds of characters the key has (never the key): xo and the server would
need to accept them. A `/healthz` FAIL that says "misconfigured" means `CLERK_SECRET_KEY` is
missing or refused; the project's **Logs** say which.

## 6. Browser login

Until both settings below are set, `xo login` asks for a key as it always has, and `GET /login`
answers `{"browser": null}`. To let users sign in in a browser instead (see docs/server.md,
"Browser login"), do the steps below.

**Browser login gives each user an API key from the Clerk instance this deployment's
`CLERK_SECRET_KEY` belongs to.** XO's platform (`XO_CORE_API_URL`, dev.xo.builders by default)
checks keys with that same instance (suraj confirmed this on 2026-10-20), so such a key works with
XO. If `CLERK_SECRET_KEY` ever moves to another instance (the production gate at the end does),
delete `XO_OAUTH_CLIENT_ID` in that same redeploy, and set browser login up again (steps 1 to 4
in the new instance, then 6 and 7; the Firewall condition in step 5 stays) only once XO's
platform checks keys with that instance too.

In the Clerk Dashboard (dashboard.clerk.com), with that instance chosen at the top:

1. **API keys must be on.** Open <https://dashboard.clerk.com/~/platform/api-keys>. If it offers
   **Enable API keys**, select it, choose **Enable User API keys**, then **Enable**. If it already
   lists keys, they are on. Clerk bills each new key past 1,000 a month ($0.001 each), and the
   Hobby plan stops at 1,000; browser login makes an account at most 5 a day.
2. **Add the OAuth application.** Open <https://dashboard.clerk.com/~/oauth-applications> and
   select **Add application** → **Create OAuth application**. **Name:** `xo CLI`. **Scopes:** all
   three of `openid`, `profile` and `email`. Select **Add**. If a **Client Secret** is shown, xo
   does not use it: close it without copying it anywhere. Without `openid`, every sign-in comes
   back to 127.0.0.1 with `invalid_scope` (the address bar says `not allowed to request scope
   'openid'`, and `xo login` fails with `the sign-in was not finished (invalid_scope)`); then select
   it on the application's page and **Save**.
3. **On the application's page that opens:**
   - turn on **Public** (xo has no client secret; it proves itself with PKCE instead), and turn on
     **Require PKCE**, both on this application's page. Leave the instance-wide **Require PKCE**
     under **Settings** alone: it covers every OAuth application of the instance, XO's included.
     Without **Public**, every browser login fails, and the project's **Logs** say
     `Clerk refused the OAuth application`;
   - leave the consent screen on (it is on by default): without it, someone signed in to XO who
     opens a sign-in link someone else made is not asked first;
   - in **Redirect URIs**, add these three, one at a time, exactly as written (Clerk compares them
     exactly: `http`, `127.0.0.1` and not `localhost`, the port, and no `/` at the end):
     `http://127.0.0.1:8976/callback`, `http://127.0.0.1:8977/callback`,
     `http://127.0.0.1:8978/callback`;
   - select **Save** if the page has one, then under **Application credentials**, copy the
     **Client ID**.
4. **Copy the Frontend API URL** from <https://dashboard.clerk.com/~/api-keys> (the page with the
   Publishable Key, not the one in step 1). It is an address only, such as
   `https://clerk.example.com` for a production instance (a development one ends in
   `.clerk.accounts.dev`): copy nothing after the host. Not the OAuth application's **Discovery
   URL** (`.../.well-known/openid-configuration`): with a path the server turns browser login off
   and its log line says why. Its start, `https://` and the host, is the same address.

Then in Vercel:

5. **Cap it per address first.** `/login/browser` needs no key and each request makes a call to
   Clerk, and the server's own cap (2 a second) is per instance. In **Firewall → Rules**, open
   `xo per-IP limit` from section 3 and add a condition with **Or**, not **And** (an **And** would
   stop the rule matching `/api/core`): Request Path **starts with** `/login`. The rule then covers
   `/me`, `/user-metadata`, `/api/core` and `/login`. **Save**, then **Review Changes → Publish**.
   A login sends four requests that this rule counts. Doing this while browser login is still off
   changes nothing for anyone.
6. **Settings → Environment Variables**, for **Production**: add `XO_OAUTH_CLIENT_ID` (the Client
   ID) and `XO_CLERK_FRONTEND_URL` (the Frontend API URL, starting `https://`). Neither is a
   secret. Vercel uses new values only in a new deployment, so then **Deployments**, the latest
   production deployment, **⋯ → Redeploy**, and **Redeploy** again in the box that opens (leave
   **Use existing Build Cache** as it is). Wait until it says **Ready**.
7. **Check**, on a computer with a browser. First:

   ```sh
   curl -s https://xo.quirq.dev/login
   ```

   It should show the sign-in address and client id under `browser`. If it says `null`, stop here:
   the project's **Logs** have a line starting `xo serve: browser login is off:` that says why.

   An `xo` installed with the quickstart before its pin moved to #56 has no browser login, so
   update it:

   ```sh
   ~/.xo/bin/pip install --force-reinstall --no-deps "quirq-xo @ git+https://github.com/quirq-ai/xo@10937c629fae9bb823dc1ff77f3ca59b4dc1aa29"
   ```

   Have your XO key handy. With a saved key that works, `xo login` only says you are already
   logged in, so log out first (when it asks about GitHub, press Enter to keep gh logged in):

   ```sh
   xo logout
   ```

   Then:

   ```sh
   xo login
   ```

   Sign in and allow `xo CLI` on the consent screen. The `xo: me:` line should say `logged in as
   ...; a new key that works for 90 days and URL saved to ...`. If it fails, log in with your key
   as before:

   ```sh
   xo login --key
   ```

To turn it off, delete both variables and redeploy as in step 6 (deleting only one also turns it
off, with a log line on every cold start); `xo login` then asks for a key again, and the Firewall
condition and the Clerk application can stay. Keys made by browser login are named
`xo CLI on HOST`, described `made by xo login in a browser`, and expire after 90 days. A later
browser login for the same account hands back the newest of those while it has a week or more
left (when Clerk will read its secret back), and never a key made any other way; otherwise it makes
a new one. Revoke a key in the Clerk Dashboard.

## 7. The hub (off until you turn it on)

With `XO_HUB_URL` and `XO_HUB_KEY` set, the server keeps xo's data (`projects`, `spaces`, `shared`)
on the quirq hub instead of in Clerk ([server.md](server.md), "The hub"). Both are unset today, and
nothing changes until you set them.

1. Deploy the hub (quirq-ai/space-hub) at `https://hub.quirq.dev` with `SPACEHUB_XO_KEY` set to a
   random value of 32 characters or more, and check that `https://hub.quirq.dev` answers.
2. In **Settings → Environment Variables**, add **both** for Production in one save:
   `XO_HUB_URL` = `https://hub.quirq.dev` and `XO_HUB_KEY` = the same value as the hub's
   `SPACEHUB_XO_KEY`. One without the other makes every request a 503 "xo server misconfigured";
   Vercel does not check this when it deploys, only the first request does.
3. Redeploy (**Deployments**, the latest production deployment, **Redeploy**), and once it is
   **Ready** check it at once:

   ```sh
   curl -s https://xo.quirq.dev/healthz && xo me && xo projects
   ```

   `/healthz` must say `ok` (a 503 "misconfigured" means one variable is missing or `XO_HUB_URL`
   is not https; **Logs** say which), and `xo me` and `xo projects` must show your projects as
   before (a 503 "misconfigured" from them means the hub refused `XO_HUB_KEY`; a 502 "The hub did
   not answer" means it is down or the URL is wrong). If any of these fails, delete both variables
   and redeploy: that puts everything back on Clerk as it was.

**Turning it off later is not a clean undo.** Whatever users changed while the hub was on lives
only in the hub; with both variables deleted, the server reads Clerk's copy again, as it was when
each user was first copied. Read the details under "Cutover and rollback" in server.md before you
turn the hub off once people have used it.

## 8. Sign in to the hub's website with xo (off until you turn it on)

With the hub (section 7) and browser login on, the hub's website can sign people in with the
same Clerk sign-in ([server.md](server.md), "Signing in to the hub's website").

1. Open <https://dashboard.clerk.com/~/oauth-applications>, select the application from section 6
   (`xo CLI`, its Client ID is `XO_OAUTH_CLIENT_ID`), and in **Redirect URIs** add
   `https://hub.quirq.dev/-/login/xo/callback` exactly as written, next to the three loopback ones;
   then **Save** if the page has one. Leave the loopback ones: `xo login` needs them. The consent
   screen names the application, so visitors see "xo CLI"; renaming it (to `xo`, say) changes
   nothing else.
2. In **Settings → Environment Variables**, for Production, add
   `XO_HUB_LOGIN_REDIRECT` = `https://hub.quirq.dev/-/login/xo/callback`, then redeploy.
3. `curl -s https://xo.quirq.dev/login` now shows `"hub": {"redirect": ...}`, and the hub's sign-in
   page shows "Sign in with xo" within five minutes. If `hub` is missing, **Logs** have a line
   starting `xo serve: hub sign-in is off:` that says why.
4. Signing in needs a GitHub account connected to the XO account: the hub's builder is that
   GitHub account's. GitHub must be on in Clerk (**Configure → SSO connections → GitHub**, with
   the instance's own GitHub credentials in production). Someone with none connected is sent to
   Clerk's account page (`https://accounts.<domain>/user`, from `XO_CLERK_FRONTEND_URL`) to connect
   one; to send them elsewhere, add `XO_GITHUB_CONNECT_URL` (an https address) and redeploy.

To turn it off, delete the variable and redeploy: the button goes away and the route answers 404.

## What is and is not protected on Vercel

| Protected | How |
|---|---|
| The Secret Key | only in Vercel's environment; never printed, logged or returned |
| Who can read or edit metadata | nobody until an admin key exists (403) |
| Tokens that are not `ak_` keys | refused without a Clerk call, and never passed on to XO. A made-up `ak_` token is passed on: XO refuses it, but only the limits below bound how many reach XO |
| What reaches XO | only the creates and `GET /spaces/{id}` under `/api/core/v1`, with an `ak_` key; anything else there is a 404 from the server |
| Big bodies | over 32KB refused (413) |
| Requests per address | the Firewall rule in step 3, once published |
| Requests to XO per instance | `XO_CORE_PER_SECOND` (default 10 a second), then 503 |
| **Not protected** | a flood from many addresses spread over many instances; only Clerk's and XO's own limits stop it |
| **Changed for XO** | every xo user now reaches XO's platform from Vercel's addresses, not their own, so a per-address limit on XO's side would count all of them together |
| **Not rate limited** | `/login` and `/login/browser` until step 5 of section 6 adds `/login` to the rule in step 3 (the server caps swaps at 2 a second per instance), `/config` (the services list `xo login` reads; static, never a Clerk call; do not add it to the rule in step 3, for the same reason as `/status`), `/` (the status page), `/status` and `/healthz`: public, one function invocation each, never a Clerk call. An open status page tab polls `/status` every 5 seconds while it is visible (about 17,000 invocations a day per tab left open) and stops while hidden. To cap them too, add a second rule of its own (Request Path **starts with** `/status`, 60 requests per 60 seconds per IP, so `/status/` is capped too); the page stays well under that. Do not add `/status` to the rule in step 3: it would share one per-address budget with `/me`, and an open status tab could then make the test script's Firewall check fail. |

## Logs and audit lines

Every request is one line in the project's **Logs**, and every `/user-metadata` request adds one
JSON audit line (key id, caller, target user, outcome; never a key or a metadata value). Vercel
keeps runtime logs for a short time that depends on the plan; check **Observability** for yours.
To keep audit lines longer, a log drain is needed, which is a paid feature on some plans, so it is
not part of this setup.

## Limits

- Each request makes at most two Clerk calls, each with a 10-second timeout, except these: a share
  makes up to ten (the key, the sharer, the lookup, and up to four reads and three writes of the
  recipient), an accept or reject up to nine, `GET /me/shared` three, a `/me/projects` or
  `/me/spaces` write up to eight (the key, then up to four reads and three writes), and a browser
  login five (the code swap and who signed in, at Clerk's Frontend API, then the user's keys, one
  key's secret and a new key); or one call to XO
  that gets 25 seconds in all, however slowly XO sends (a create that takes longer answers 504 and
  may still have happened). Vercel's
  function time and request size limits are not checked here; 25 seconds and 32KB (the server's
  body cap) are small, but confirm them for your plan.

## Before production credentials (a gate)

Do not switch to production credentials until all of these hold:

1. The test script passed on development credentials, including the Firewall check where your plan
   has the rule.
2. **A global limit, or the risk accepted in writing.** The per-IP rule does not stop a flood
   spread over many addresses, and the server's own budgets (Clerk's, and `XO_CORE_PER_SECOND` for
   requests passed on to XO) are per instance. This is not only about this switch: requests to
   `/api/core` already reach Clerk through XO's platform, which checks keys with the server's own
   instance (section 6). Either add a
   limit that counts all requests together (one option is a second rate limit rule on the same
   paths keyed by the `host` header, since every request has the same host; whether Vercel counts
   it globally is not verified), or write down that XO accepts the risk: such a flood could use up
   the Backend API limit of that Clerk instance, which this server and XO's platform share, and pay
   for key checks past 100,000 a month. TODO(suraj): choose one.

Then replace `CLERK_SECRET_KEY` with the production key (`sk_live_...`), redeploy, and run the test
script with a real XO key. That moves the server to another Clerk instance, so first make sure
XO's platform at `XO_CORE_API_URL` checks keys with the production instance (set
`XO_CORE_API_URL` to one that does if dev.xo.builders does not), and with browser login on, follow
the note at the start of section 6. A user then needs only the `xo` command:
`https://xo.quirq.dev` is its default server.

## Rotating the Secret Key

If the Secret Key leaks or is rotated: create a new one in the Clerk Dashboard, replace
`CLERK_SECRET_KEY` in Vercel, redeploy, run the test script, then delete the old key.
