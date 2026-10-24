#!/bin/bash
# llmmon container entrypoint:
#  0) set up an XDG runtime dir: LM Studio (llmster is Electron-based) wants
#     one; root has none in the container and /run/user/1000 belongs to
#     the host user (UID 1000). Without it `lms` can TALK to a running daemon
#     but fails to
#     START a new one ("Failed to start or connect to local LM Studio API").
#  1) in the background: ensure the LM Studio daemon is up and report which
#     models are loaded. Idempotent — no-ops when the daemon/model already
#     exist (e.g. first deploy while an old host daemon is still running).
#     This makes the container the single owner of the LMS lifecycle,
#     replacing the old lmstudio/llmload systemd user units.
#  2) run the dashboard server as PID 1 (the "Restart Service" button
#     re-execs it in place; docker restart-policy covers crashes).
LMS_BIN=/root/.lmstudio/bin/lms
export XDG_RUNTIME_DIR=/tmp/llmmon-xdg-runtime-dir
mkdir -p "$XDG_RUNTIME_DIR" && chmod 700 "$XDG_RUNTIME_DIR"

nohup bash -c "
  echo \"[autostart \$(date -u +%FT%TZ)] ensuring lms daemon is up...\"
  $LMS_BIN daemon up 2>&1 | tail -3
  echo \"[autostart] model auto-load (idempotent)...\"
  bash /root/llmmon/load_model.sh
  echo \"[autostart \$(date -u +%FT%TZ)] done\"
" >/root/llmmon/autostart.log 2>&1 &

exec python3 /root/llmmon/server.py
