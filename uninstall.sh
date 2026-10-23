#!/usr/bin/env bash
# Remove the llmmon dashboard. Drivers, Docker and LM Studio are left alone by default —
# this only undoes what install.sh put in place for the dashboard itself.
#
#   sudo bash uninstall.sh                # stop + remove the container, image and sudo rule
#   sudo bash uninstall.sh --keep-repo    # ...but leave ~/Providence-LMS on disk
#   sudo bash uninstall.sh --all          # also remove the repo checkout and system helpers
set -euo pipefail

KEEP_REPO=1
REMOVE_ALL=0
for arg in "$@"; do
  case "$arg" in
    --keep-repo) KEEP_REPO=1; REMOVE_ALL=0 ;;
    --all)       KEEP_REPO=0; REMOVE_ALL=1 ;;
    -h|--help)   sed -n '2,8p' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
    *)           echo "unknown flag: $arg" >&2; exit 1 ;;
  esac
done

[ "$(id -u)" = 0 ] || { echo "run as root (sudo)" >&2; exit 1; }

echo "==> stopping and removing the llmmon container"
docker stop llmmon >/dev/null 2>&1 || true
docker rm   llmmon >/dev/null 2>&1 || true
docker rmi  llmmon:latest >/dev/null 2>&1 || true
echo "    ok (nvidia-smi / lms keep running; they are not part of this dashboard)"

echo "==> removing the reboot sudo rule"
rm -f /etc/sudoers.d/llmmon-reboot
if [ "$REMOVE_ALL" = "1" ]; then
  rm -f /usr/local/bin/reboot_server.sh /usr/local/bin/nvidia-powerlimit.sh
  rm -f /etc/systemd/system/nvidia-powerlimit.service
  systemctl daemon-reload >/dev/null 2>&1 || true
fi

if [ "$KEEP_REPO" = "0" ]; then
  for d in "$HOME/Providence-LMS" "$(getent passwd "${SUDO_USER:-root}" | cut -d: -f6)/Providence-LMS"; do
    if [ -d "$d/.git" ]; then
      echo "==> removing checkout $d (your .env and ssh key go with it)"
      rm -rf "$d"
    fi
  done
fi

echo
echo "done. LM Studio models (~/.lmstudio/models) and the NVIDIA driver were left untouched."
echo "To remove LM Studio itself:   ~/.lmstudio/bin/lms daemon down && rm -rf ~/.lmstudio"
