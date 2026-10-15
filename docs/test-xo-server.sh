#!/usr/bin/env bash
# Test a deployed xo server (docs/deploy-vercel.md, step 5). Run it from the checkout you deployed:
#
#     bash docs/test-xo-server.sh
#
# It runs xo straight from this checkout's src/ (nothing is installed or downloaded), checks
# /healthz, asks for a test user's API key without echoing it, checks `xo me`, that the server
# passes the core API on to XO (with a made-up key, never yours), and three refusals, and last
# checks that the Vercel Firewall rule is active. The key is never printed, written to a
# file, put on a command line or exported: only the xo calls that need it get it. Its only network
# calls go to the server. Works with macOS bash 3.2.
# XO_URL overrides the server (default https://xo.quirq.dev).
set -u

server="${XO_URL:-https://xo.quirq.dev}"
server="${server%/}"
repo="$(cd "$(dirname "$0")/.." && pwd)"
failed=0

pass() { printf 'PASS  %s\n' "$1"; }
fail() { printf 'FAIL  %s\n' "$1"; failed=1; }

python=""
for candidate in python3.14 python3.13 python3.12 python3.11 python3; do
  if command -v "$candidate" >/dev/null 2>&1 &&
     "$candidate" -c 'import sys; sys.exit(sys.version_info < (3, 11))' 2>/dev/null; then
    python="$candidate"
    break
  fi
done
if [ -z "$python" ]; then
  echo "Python 3.11 or newer is needed (see docs/quickstart.md)."
  exit 1
fi

work="$(mktemp -d "${TMPDIR:-/tmp}/xo-test.XXXXXX")" || exit 1
trap 'rm -rf "$work"' EXIT

# xo from this checkout, isolated from any installed copy (-I) and any key xo login saved,
# talking only to $server.
xo() {
  XDG_CONFIG_HOME="$work/config" XO_URL="$server" "$python" -I -c \
    'import sys; sys.path.insert(0, sys.argv[1]); from xo.cli import main; sys.exit(main(sys.argv[2:]))' \
    "$repo/src" "$@"
}

echo "Testing $server with xo from $repo"

# 1. Health: no key, no Clerk call.
health="$(curl -sS --max-time 20 "$server/healthz" 2>&1)"
case "$health" in
  *'"status": "ok"'*) pass "/healthz answers ok" ;;
  *misconfigured*) fail "/healthz: the server is misconfigured; check CLERK_SECRET_KEY in Vercel and its logs" ;;
  *) fail "/healthz: unexpected answer: $(printf '%s' "$health" | head -c 200)" ;;
esac

# 2. Your test user's key, read without echo, kept in an unexported variable.
printf 'Paste the test user'"'"'s API key (ak_..., nothing shows), then press Enter: '
IFS= read -rs key
echo
key="${key%$'\r'}"  # a paste from some terminals ends in a carriage return; xo strips it too
if [ -z "$key" ]; then
  echo "No key given; stopping."
  exit 1
fi

# 3. The key's format, without showing it: xo and the server accept ak_ followed by letters,
#    digits and _ . - + / = only. Anything else stops here, before the key is sent anywhere (a
#    Secret Key pasted by mistake must not reach even your own server).
case "$key" in
  ak_*[!A-Za-z0-9_.+/=-]*) bad="your key has characters xo does not accept yet (tell Claude which kinds; do not paste the key)" ;;
  ak_?*) bad="" ;;
  ak_) bad="your key is only ak_, with nothing after it (was the paste cut short?)" ;;
  *) bad="your key does not start with ak_ (tell Claude; do not paste the key)" ;;
esac
if [ -n "$bad" ]; then
  key=""
  fail "$bad"
  echo "Stopping: the key was not sent. Run the script again with the test user's ak_ key."
  exit 1
fi
pass "your key's format is one xo accepts"

# 4. xo me shows the key's own account, and no email or metadata.
if XO_API_KEY="$key" xo me >"$work/me.json" 2>"$work/me.err"; then
  if "$python" -I - "$work/me.json" <<'PY'
import json, sys
data = json.load(open(sys.argv[1]))["data"]
assert str(data.get("id", "")).startswith("user_"), "no user id"
assert set(data) == {"id", "username", "firstName", "lastName", "imageUrl", "createdAt"}, sorted(data)
print("      id %s, username %s" % (data["id"], data.get("username")))
PY
  then pass "xo me shows your account, and only the six fields"
  else fail "xo me answered, but not with the six fields"
  fi
else
  fail "xo me: $(head -c 300 "$work/me.err")"
fi

# 5. The core API goes through the server to XO. The key is made up, so XO answers 401 (or 404 for
#    the space); a server without the forwarding answers its own bare "Not found".
probe="00000000-0000-0000-0000-000000000000"
if XO_API_KEY=ak_made_up_for_the_xo_test xo spaces --space-id "$probe" >/dev/null 2>"$work/core.err"; then
  fail "the core API check: XO accepted a made-up key"
elif grep -q -e "HTTP 401" -e "Space not found" "$work/core.err"; then
  pass "the server passes the core API on to XO"
else
  fail "the core API check: $(head -c 300 "$work/core.err") (is the latest main deployed?)"
fi

# 6. What must be refused.
expect() {  # expect LABEL TEXT KEY -- xo arguments...
  label="$1"; text="$2"; with="$3"; shift 4
  if XO_API_KEY="$with" xo "$@" >/dev/null 2>"$work/err"; then
    fail "$label: it was not refused"
  elif grep -q "$text" "$work/err"; then
    pass "$label"
  else
    fail "$label: $(head -c 300 "$work/err")"
  fi
}
expect "a wrong key gets 401" "HTTP 401" ak_wrong_key_for_testing -- me
expect "a key that is not a Clerk key gets 401" "HTTP 401" not-a-key -- me
expect "your key cannot read metadata (no admin keys yet)" "HTTP 403" "$key" -- \
  user-metadata get --user-id user_test
key=""; with=""

# 7. The Firewall rule: requests to /me with no key (each a 401 from the server, with no Clerk call)
#    must run into Vercel's limit of 60 a minute. Its window is fixed, so one that ends mid-burst
#    lets up to 60 more through: up to 130 are sent, and the first 429 (or 403, if the rule denies)
#    ends it. Any other answer stops the check, so a server error is never taken for the rule. Last,
#    because it holds your address back for up to a minute.
echo "Sending up to 130 requests without a key to check the Firewall rule..."
limited=""
other=""
i=0
while [ "$i" -lt 130 ]; do
  code="$(curl -s -o /dev/null -w '%{http_code}' --max-time 10 "$server/me")"
  i=$((i + 1))
  case "$code" in
    401) ;;
    429|403) limited="$code"; break ;;
    *) other="$code"; break ;;
  esac
done
if [ -n "$other" ]; then
  fail "request $i got $other (not 401, 429 or 403), so the rule could not be checked; see the server's Logs, wait a minute and run the script again"
elif [ -z "$limited" ]; then
  fail "130 requests in a row got 401: the Firewall rule is not active (is it published?)"
elif [ "$i" -le 50 ]; then
  fail "request $i got $limited, well before the limit of 60; check the rule (if you ran the script within the last minute, or a status page tab shares the rule, wait a minute and run it again)"
else
  pass "the Firewall rule is active (request $i got $limited)"
fi

echo
if [ "$failed" -eq 0 ]; then
  echo "All checks passed."
else
  echo "Some checks failed; see above. The server's side is in Vercel: the project's Logs."
fi
exit "$failed"
