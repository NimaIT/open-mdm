#!/usr/bin/env bash
# Capture a headless screenshot of a UI route with an authenticated session.
#   ./scripts/shoot.sh <route> <out.png> <jwt> [width] [height]
set -uo pipefail
ROUTE="${1:?route}"; OUT="${2:?out}"; JWT="${3:?jwt}"
W="${4:-1600}"; H="${5:-1100}"
PORT="${PORT:-8099}"
PROFILE=$(mktemp -d)

# Seed the session cookie directly into the profile's cookie store so the SPA
# boots already authenticated.
python3 - "$PROFILE" "$JWT" <<'PY'
import sqlite3, sys, time
profile, jwt = sys.argv[1], sys.argv[2]
db = sqlite3.connect(f"{profile}/cookies.sqlite")
db.execute("""CREATE TABLE moz_cookies (
  id INTEGER PRIMARY KEY, originAttributes TEXT NOT NULL DEFAULT '', name TEXT,
  value TEXT, host TEXT, path TEXT, expiry INTEGER, lastAccessed INTEGER,
  creationTime INTEGER, isSecure INTEGER, isHttpOnly INTEGER, inBrowserElement INTEGER DEFAULT 0,
  sameSite INTEGER DEFAULT 0, rawSameSite INTEGER DEFAULT 0, schemeMap INTEGER DEFAULT 0,
  CONSTRAINT moz_uniqueid UNIQUE (name, host, path, originAttributes))""")
now = int(time.time())
db.execute(
  "INSERT INTO moz_cookies (name,value,host,path,expiry,lastAccessed,creationTime,"
  "isSecure,isHttpOnly,sameSite,rawSameSite,schemeMap) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
  ("mdm_session", jwt, "127.0.0.1", "/", now + 86400, now * 1000000, now * 1000000,
   0, 1, 1, 1, 1),
)
db.commit()
PY

timeout 150 firefox --headless --profile "$PROFILE" --window-size="$W,$H" \
  --screenshot "$OUT" "http://127.0.0.1:$PORT$ROUTE" >/dev/null 2>&1
rm -rf "$PROFILE"
[[ -s "$OUT" ]] && echo "captured $OUT ($(stat -c%s "$OUT") bytes)" || { echo "FAILED $OUT"; exit 1; }
