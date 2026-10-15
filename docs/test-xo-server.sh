#!/usr/bin/env bash
# Test a deployed xo server (docs/deploy-vercel.md, step 4). Run it from a checkout of this repo:
#
#     bash docs/test-xo-server.sh
#
# It installs xo from this checkout into a throwaway virtualenv, checks /healthz, asks for a test
# user's API key without echoing it, and checks `xo me` and three refusals. The key is never
# printed, written to a file or put on a command line. Works with macOS bash 3.2.
# XO_SERVER_URL overrides the server (default https://xo.quirq.dev).
set -u

server="${XO_SERVER_URL:-https://xo.quirq.dev}"
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

echo "Testing $server"
echo "Installing xo from $repo into a throwaway virtualenv..."
if ! "$python" -m venv "$work/venv" ||
   ! "$work/venv/bin/python" -m pip install --quiet --disable-pip-version-check "$repo"; then
  echo "Could not install xo from $repo."
  exit 1
fi
xo="$work/venv/bin/xo"
export XO_SERVER_URL="$server"

# 1. Health: no key, no Clerk call.
health="$(curl -sS --max-time 20 "$server/healthz" 2>&1)"
case "$health" in
  *'"status": "ok"'*) pass "/healthz answers ok" ;;
  *misconfigured*) fail "/healthz: the server is misconfigured; check CLERK_SECRET_KEY in Vercel and its logs" ;;
  *) fail "/healthz: unexpected answer: $(printf '%s' "$health" | head -c 200)" ;;
esac

# 2. Your test user's key, read without echo.
printf 'Paste the test user'"'"'s API key (ak_..., nothing shows), then press Enter: '
IFS= read -rs XO_API_KEY
echo
if [ -z "$XO_API_KEY" ]; then
  echo "No key given; stopping."
  exit 1
fi
export XO_API_KEY

# 3. xo me shows the key's own account, and no email or metadata.
if "$xo" me >"$work/me.json" 2>"$work/me.err"; then
  if "$work/venv/bin/python" - "$work/me.json" <<'PY'
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

# 4. What must be refused.
expect() {  # expect LABEL TEXT -- command...
  label="$1"; text="$2"; shift 3
  if "$@" >/dev/null 2>"$work/err"; then
    fail "$label: it was not refused"
  elif grep -q "$text" "$work/err"; then
    pass "$label"
  else
    fail "$label: $(head -c 300 "$work/err")"
  fi
}
expect "a wrong key gets 401" "HTTP 401" -- env XO_API_KEY=ak_wrong_key_for_testing "$xo" me
expect "a key that is not a Clerk key gets 401" "HTTP 401" -- env XO_API_KEY=not-a-key "$xo" me
expect "your key cannot read metadata (no admin keys yet)" "HTTP 403" -- \
  "$xo" user-metadata get --user-id user_test

unset XO_API_KEY
echo
if [ "$failed" -eq 0 ]; then
  echo "All checks passed."
else
  echo "Some checks failed; see above. The server's side is in Vercel: the project's Logs."
fi
exit "$failed"
