#!/usr/bin/env bash
# Puts Autobench's credentials into Secret Manager (`autobench-credentials`, one JSON object), for Cloud Run.
#   tool/set_autobench_credentials.sh
# Asks for the IBKR username and password (hidden input). The market-data keys, the SEC user agent and the
# IBKR Flex settings are copied from the repo's .env when present. Nothing is printed or written to disk:
# the JSON goes from memory straight to `gcloud secrets versions add` on stdin.
# Then redeploy (deploy/deploy.sh) or restart the service so it reads the new version.
set -euo pipefail
REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PROJECT=vral-altos

# --market-data-only: no IBKR login (the service runs; IB Gateway reports that the login is missing)
if [[ "${1:-}" == "--market-data-only" ]]; then
  IBKR_USERNAME="" IBKR_PASSWORD=""
else
  read -r -p "IBKR username: " IBKR_USERNAME
  read -r -s -p "IBKR password (hidden): " IBKR_PASSWORD; echo
  [[ -n "$IBKR_USERNAME" && -n "$IBKR_PASSWORD" ]] || { echo "Both are required." >&2; exit 1; }
fi
export IBKR_USERNAME IBKR_PASSWORD

python3 - "$REPO/.env" <<'PY' | gcloud secrets versions add autobench-credentials --project="$PROJECT" --data-file=- >/dev/null
import json, os, sys
keys = ["ALPACA_API_KEY_ID", "ALPACA_API_SECRET_KEY", "SEC_USER_AGENT", "AUTOBENCH_IBKR_FLEX_TOKEN", "AUTOBENCH_IBKR_FLEX_QUERY"]
env = {}
try:
    for line in open(sys.argv[1]):
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, v = line.split("=", 1)
            env[k.strip()] = v.strip().strip('"').strip("'")
except OSError:
    pass
if not env.get("ALPACA_API_KEY_ID") and env.get("ALPACA_API_KEY"):
    env["ALPACA_API_KEY_ID"] = env["ALPACA_API_KEY"]
out = {k: os.environ[k] for k in ("IBKR_USERNAME", "IBKR_PASSWORD") if os.environ.get(k)}
out.update({k: env[k] for k in keys if env.get(k)})
sys.stdout.write(json.dumps(out))
print("stored: " + ", ".join(sorted(out)), file=sys.stderr)
PY
echo "autobench-credentials updated (new version). Restart or redeploy Autobench to use it."
