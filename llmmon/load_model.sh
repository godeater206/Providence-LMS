#!/usr/bin/env bash
# Boot-time LM Studio check: wait for the daemon API, then report status.
# Does NOT auto-load any model — models are loaded manually via the dashboard.
LMS="$HOME/.lmstudio/bin/lms"

# Wait up to 10 minutes for the LM Studio app API
for _ in $(seq 1 120); do
  if "$LMS" ps --json >/dev/null 2>&1; then break; fi
  sleep 5
done
if ! "$LMS" ps --json >/dev/null 2>&1; then
  echo "ERROR: LM Studio API (lms) never came up" >&2
  exit 1
fi

echo "LM Studio daemon is running. Current loaded models:"
"$LMS" ps --json 2>/dev/null || true
