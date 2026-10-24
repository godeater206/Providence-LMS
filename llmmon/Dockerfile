# llmmon — LM Studio + multi-GPU box dashboard (docker)
#
# python:3.12-slim + the two small host tools the container needs:
#   procps         -> pgrep/pkill for ops + diagnostics. The "Restart LMS"
#                        button itself signals via python os.kill() on exact
#                        /proc comm match (the container runs with `pid: host`
#                        so host PIDs are visible and signalable).
#   openssh-client -> reboot  (the Reboot button SSHes to the host and runs the
#                        existing /usr/local/bin/reboot_server.sh via its sudoers rule)
#
# server.py + load_model.sh are NOT baked in — they come from the host directory
# bind-mounted at /root/llmmon (compose sets it from LLMON_DIR in .env), so a code
# update is: git pull + docker restart llmmon. No rebuild needed.
FROM python:3.12-slim

RUN apt-get update \
 && apt-get install -y --no-install-recommends procps openssh-client ca-certificates \
 && rm -rf /var/lib/apt/lists/*

COPY entrypoint.sh /entrypoint.sh
RUN chmod +x /entrypoint.sh

ENTRYPOINT ["/entrypoint.sh"]
