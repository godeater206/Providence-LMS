#!/bin/sh
# Reboot the whole server. Called by the llmmon dashboard (POST /api/reboot) over SSH as:
#   sudo /usr/local/bin/reboot_server.sh
# with a matching NOPASSWD rule in /etc/sudoers.d/llmmon-reboot (see install.sh).
#
# --test: verify the sudo + SSH path without actually rebooting.
if [ "${1:-}" = "--test" ]; then
  echo "reboot helper OK (would reboot now)"
  exit 0
fi
exec shutdown -r now "reboot requested from llmmon dashboard"
