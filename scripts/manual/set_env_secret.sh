#!/usr/bin/env bash
# Set a single secret in .env WITHOUT echoing it to the terminal or any transcript.
#
# Usage (run on the host that holds .env):
#   bash scripts/manual/set_env_secret.sh VAR_NAME
#
# Prompts once (input hidden), then replaces the VAR_NAME= line in .env
# (or appends it if absent). The value is passed to Python via an environment
# variable — never on the command line — so it does not appear in `ps`, and a
# regex-safe replacement avoids backslash/backref surprises. Prints only the
# variable name, never the value.
set -euo pipefail
cd "$(dirname "$0")/../.."  # repo root (scripts/manual -> ../../)

VAR="${1:?usage: set_env_secret.sh VAR_NAME}"

if [ ! -f .env ]; then
  echo "ERROR: .env not found in $(pwd)" >&2
  exit 1
fi

read -rsp "Paste value for ${VAR} (input hidden): " VAL
printf '\n'
if [ -z "${VAL}" ]; then
  echo "empty value — aborting, .env unchanged"
  exit 1
fi

VAR="$VAR" VAL="$VAL" python3 - <<'PY'
import os, re
var = os.environ["VAR"]
val = os.environ["VAL"]
p = ".env"
env = open(p).read()
pat = re.compile(rf"^{re.escape(var)}=.*$", re.M)
# Use a function replacement so backslashes / \1 in the secret are literal.
if pat.search(env):
    env = pat.sub(lambda _m: f"{var}={val}", env, count=1)
else:
    env = env.rstrip("\n") + f"\n{var}={val}\n"
open(p, "w").write(env)
print(f"{var} updated in .env (value hidden, length={len(val)}).")
PY
unset VAL