#!/usr/bin/env bash
# Providence-LMS bootstrap — one-shot installer for the llmmon dashboard + LM Studio stack.
#
#   curl -fsSL https://raw.githubusercontent.com/godeater206/Providence-LMS/main/install.sh | sudo bash
#
# Run as root (sudo). Safe to re-run: every step is idempotent, so this doubles as the
# "apply my .env changes / update the code" command.
#
# Flags:
#   --user=NAME        Linux user that will own ~/.lmstudio and run lms  (default: $SUDO_USER)
#   --port=N           Dashboard port                                   (default: 8899)
#   --dir=PATH         Where to clone this repo                         (default: ~USER/Providence-LMS)
#   --repo=URL         Git remote to clone                              (default: github.com/godeater206/Providence-LMS)
#   --ref=BRANCH       Branch/tag to check out                          (default: main)
#   --gpu-watt=N       Install nvidia-powerlimit.service capping each GPU at N watts
#   --tailscale        Install Tailscale + `tailscale serve` HTTPS for the dashboard
#   --skip-driver      Do not touch NVIDIA kernel drivers
#   --skip-docker      Do not install Docker / container toolkit
#   -h | --help        Show this help
set -euo pipefail

REPO_URL="https://github.com/godeater206/Providence-LMS.git"
GIT_REF="main"
TARGET_USER="${SUDO_USER:-}"
LLMON_PORT="8899"
CLONE_DIR=""
GPU_WATT=""
WITH_TAILSCALE=0
SKIP_DRIVER=0
SKIP_DOCKER=0

log()  { printf '\n\033[1;36m==> %s\033[0m\n' "$*"; }
ok()   { printf '    \033[1;32mok\033[0m %s\n' "$*"; }
warn() { printf '    \033[1;33mwarn\033[0m %s\n' "$*"; }
die()  { printf '\n\033[1;31mERROR: %s\033[0m\n' "$*" >&2; exit 1; }

usage() { sed -n '2,25p' "$0" | sed 's/^# \{0,1\}//'; exit 0; }

for arg in "$@"; do
  case "$arg" in
    --user=*)        TARGET_USER="${arg#*=}" ;;
    --port=*)        LLMON_PORT="${arg#*=}" ;;
    --dir=*)         CLONE_DIR="${arg#*=}" ;;
    --repo=*)        REPO_URL="${arg#*=}" ;;
    --ref=*)         GIT_REF="${arg#*=}" ;;
    --gpu-watt=*)    GPU_WATT="${arg#*=}" ;;
    --tailscale)     WITH_TAILSCALE=1 ;;
    --skip-driver)   SKIP_DRIVER=1 ;;
    --skip-docker)   SKIP_DOCKER=1 ;;
    -h|--help)       usage ;;
    *)               die "unknown flag: $arg (try --help)" ;;
  esac
done

[ "$(id -u)" = 0 ] || die "run as root: sudo bash install.sh   (or pipe into | sudo bash)"
[ -n "$TARGET_USER" ] || die "could not determine the target user; pass --user=NAME"
command -v curl >/dev/null 2>&1 || { apt-get update -qq && apt-get install -y -qq curl ca-certificates; }

USER_HOME="$(getent passwd "$TARGET_USER" | cut -d: -f6)"
[ -n "$USER_HOME" ] && [ -d "$USER_HOME" ] || die "home for user '$TARGET_USER' not found"
LLMON_DIR="${CLONE_DIR:-$USER_HOME/Providence-LMS}"
LMS_HOME="$USER_HOME/.lmstudio"

log "target: user=$TARGET_USER home=$USER_HOME port=$LLMON_PORT dir=$LLMON_DIR"

# ---------------------------------------------------------------- 1. prerequisites
log "[1/9] base packages"
export DEBIAN_FRONTEND=noninteractive
apt-get update -qq
apt-get install -y -qq apt-transport-https ca-certificates curl git gnupg kmod lsb-release \
                       pciutils openssh-client sudo util-linux >/dev/null
ok "packages present"

# ---------------------------------------------------------------- 2. NVIDIA GPU + driver
log "[2/9] NVIDIA driver"
GPU_COUNT="$(lspci -nn 2>/dev/null | grep -ic 'nvidia\|geforce' || true)"
if [ "$GPU_COUNT" = "0" ]; then
  warn "no NVIDIA device found via lspci — continuing, but GPU telemetry/model loading will not work"
fi
if command -v nvidia-smi >/dev/null 2>&1 && nvidia-smi -L >/dev/null 2>&1; then
  ok "driver already working: $(nvidia-smi --query-gpu=driver_version --format=csv,noheader | head -1) ($GPU_COUNT device(s))"
elif [ "$SKIP_DRIVER" = "1" ]; then
  warn "--skip-driver set, leaving drivers alone"
else
  apt-get install -y -qq "linux-headers-$(uname -r)" >/dev/null 2>&1 || \
    warn "could not install linux-headers for $(uname -r) (DKMS may fail)"
  if command -v ubuntu-drivers >/dev/null 2>&1; then
    ubuntu-drivers autoinstall >/dev/null 2>&1 || apt-get install -y -qq nvidia-driver-580-open >/dev/null
  else
    apt-get install -y -qq nvidia-driver-580-open >/dev/null
  fi
  if nvidia-smi -L >/dev/null 2>&1; then
    ok "driver installed"
  else
    warn "driver installed but the kernel module is not loaded yet"
    echo    "    reboot, then re-run this same command — it will pick up where it left off:"
    echo    "      curl -fsSL https://raw.githubusercontent.com/godeater206/Providence-LMS/main/install.sh | sudo bash"
    exit 3
  fi
fi

# Optional per-GPU power cap is applied later, once the repo checkout exists (--gpu-watt=N).

# ---------------------------------------------------------------- 3. Docker + compose plugin
log "[3/9] Docker"
if command -v docker >/dev/null 2>&1 && docker compose version >/dev/null 2>&1; then
  ok "docker $(docker --version | awk '{print $3}' | tr -d ',') + compose $(docker compose version --short 2>/dev/null || echo v2)"
elif [ "$SKIP_DOCKER" = "1" ]; then
  warn "--skip-docker set; expecting docker + compose to already exist"
  command -v docker >/dev/null 2>&1 || die "docker not found and --skip-docker was given"
else
  curl -fsSL https://get.docker.com | sh >/dev/null 2>&1 || die "Docker install failed"
  ok "docker installed"
fi
systemctl enable docker >/dev/null 2>&1 || true
id -nG "$TARGET_USER" | grep -qw docker || usermod -aG docker "$TARGET_USER"
ok "$TARGET_USER is in the docker group (re-login or new shell needed for group membership)"

# ---------------------------------------------------------------- 4. NVIDIA Container Toolkit
log "[4/9] NVIDIA Container Toolkit (lets containers see the GPUs)"
if dpkg -s nvidia-container-toolkit >/dev/null 2>&1; then
  ok "already installed"
elif [ "$SKIP_DOCKER" = "1" ]; then
  warn "--skip-docker set, skipping toolkit install"
else
  apt-get install -y -qq nvidia-container-toolkit >/dev/null 2>&1 || {
    install -m 755 -d /etc/apt/keyrings
    curl -fsSL https://nvidia.github.io/libnvidia-container/gpgkey \
      | gpg --dearmor -o /usr/share/keyrings/nvidia-container-toolkit-keyring.gpg --yes
    curl -fsSL https://nvidia.github.io/libnvidia-container/stable/deb/nvidia-container-toolkit.list \
      | sed 's#deb https://#deb [signed-by=/usr/share/keyrings/nvidia-container-toolkit-keyring.gpg] https://#g' \
      > /etc/apt/sources.list.d/nvidia-container-toolkit.list
    apt-get update -qq && apt-get install -y -qq nvidia-container-toolkit >/dev/null
  }
  ok "toolkit installed"
fi
if command -v nvidia-ctk >/dev/null 2>&1; then
  nvidia-ctk runtime configure --runtime=docker >/dev/null 2>&1 || true
  systemctl restart docker
  ok "docker configured for the nvidia runtime"
fi

# ---------------------------------------------------------------- 5. LM Studio (llmster + lms CLI)
log "[5/9] LM Studio headless daemon (llmster)"
if [ -x "$LMS_HOME/bin/lms" ]; then
  ok "lms already installed: $LMS_HOME/bin/lms"
else
  su - "$TARGET_USER" -c 'curl -fsSL https://lmstudio.ai/install.sh | bash' >/dev/null 2>&1 \
    || die "LM Studio install failed (see https://lmstudio.ai/docs/developer/core/headless)"
  ok "llmster + lms installed for $TARGET_USER"
fi
su - "$TARGET_USER" -c "$LMS_HOME/bin/lms daemon up" >/dev/null 2>&1 || true
ok "daemon start requested (the dashboard container also owns this lifecycle)"

# ---------------------------------------------------------------- 6. repo checkout
log "[6/9] fetching the llmmon dashboard source"
if [ -d "$LLMON_DIR/.git" ]; then
  git -C "$LLMON_DIR" fetch --quiet origin "$GIT_REF" && git -C "$LLMON_DIR" reset --quiet --hard "origin/$GIT_REF"
  ok "updated existing checkout at $LLMON_DIR"
else
  if [ -d "$LLMON_DIR" ] && [ -n "$(ls -A "$LLMON_DIR" 2>/dev/null)" ]; then
    die "$LLMON_DIR exists and is not a git checkout — pass --dir=/some/other/path"
  fi
  su - "$TARGET_USER" -c "git clone --quiet --branch '$GIT_REF' '$REPO_URL' '$LLMON_DIR'" \
    || die "git clone failed: $REPO_URL ($GIT_REF)"
  ok "cloned to $LLMON_DIR"
fi

# ---------------------------------------------------------------- 7. reboot plumbing
log "[7/9] reboot button plumbing (SSH key + sudoers helper)"
install -m 755 "$LLMON_DIR/system/reboot_server.sh" /usr/local/bin/reboot_server.sh
printf '%s ALL=(ALL) NOPASSWD: /usr/local/bin/reboot_server.sh\n' "$TARGET_USER" \
  > /etc/sudoers.d/llmmon-reboot
chmod 440 /etc/sudoers.d/llmmon-reboot
visudo -cf /etc/sudoers.d/llmmon-reboot >/dev/null && ok "sudo rule installed (/usr/local/bin/reboot_server.sh)"

SSH_DIR="$LLMON_DIR/llmmon/ssh"
install -m 700 -d "$SSH_DIR"
if [ ! -f "$SSH_DIR/id_ed25519" ]; then
  su - "$TARGET_USER" -c "ssh-keygen -q -t ed25519 -N '' -C llmmon-reboot -f '$SSH_DIR/id_ed25519'"
fi
chown -R "$TARGET_USER":"$TARGET_USER" "$SSH_DIR" 2>/dev/null || true
chmod 600 "$SSH_DIR/id_ed25519"
install -m 700 -d "$USER_HOME/.ssh"; touch "$USER_HOME/.ssh/authorized_keys"
chown "$TARGET_USER":"$TARGET_USER" "$USER_HOME/.ssh" "$USER_HOME/.ssh/authorized_keys"
PUB="$(cat "$SSH_DIR/id_ed25519.pub")"
grep -qxF "$PUB" "$USER_HOME/.ssh/authorized_keys" || echo "$PUB" >> "$USER_HOME/.ssh/authorized_keys"
ok "reboot SSH key ready ($SSH_DIR/id_ed25519)"

# ---------------------------------------------------------------- 8. .env for compose
log "[8/9] writing llmmon configuration"
RAPL_FILE=""
for d in /sys/devices/virtual/powercap/intel-rapl /sys/devices/virtual/powercap/*; do
  [ -d "$d" ] || continue
  if   [ -f "$d/intel-rapl:0/energy_uj" ]; then RAPL_FILE="/rapl/intel-rapl/intel-rapl:0/energy_uj"; break
  elif [ -f "$d/energy_uj" ];              then RAPL_FILE="/rapl/$(basename "$d")/energy_uj";        break; fi
done
[ -n "$RAPL_FILE" ] || warn "no powercap/RAPL energy counter found (CPU package power will read 0 W — common on VMs and ARM)"

HOST_IP="$(hostname -I 2>/dev/null | awk '{print $1}')"
ENV_FILE="$LLMON_DIR/llmmon/.env"
if [ -f "$ENV_FILE" ]; then
  cp -n "$ENV_FILE" "$ENV_FILE.pre-installer.bak" || true
  ok "keeping your existing .env (backup: $ENV_FILE.pre-installer.bak)"
else
  cat > "$ENV_FILE" <<EOF
# Generated by Providence-LMS install.sh on $(date -u '+%Y-%m-%dT%H:%MZ')
LLMON_DIR=$LLMON_DIR/llmmon
LMS_HOME=$LMS_HOME
LLMON_HOSTNAME=$(hostname)
LLMON_PORT=$LLMON_PORT
LLMON_REBOOT_SSH=${TARGET_USER}@${HOST_IP:-127.0.0.1}
LLMON_RAPL_FILE=${RAPL_FILE:-/rapl/intel-rapl/intel-rapl:0/energy_uj}

# The two dashboard load buttons. Leave a value blank to keep the built-in default.
# Keys must match what 'lms ls' prints; download models with 'lms get <publisher/model>'.
LLMON_MODEL_A_KEY=
LLMON_MODEL_A_ID=
LLMON_MODEL_A_CTX=262144
LLMON_MODEL_B_KEY=
LLMON_MODEL_B_ID=
LLMON_MODEL_B_CTX=262144
EOF
  ok "wrote $ENV_FILE"
fi
chown -R "$TARGET_USER":"$TARGET_USER" "$ENV_FILE" 2>/dev/null || true

# ---------------------------------------------------------------- 9. build + start
log "[9/9] building the image and starting the container"
( cd "$LLMON_DIR/llmmon" && docker compose up -d --build ) >/tmp/llmmon-build.log 2>&1 \
  || { tail -30 /tmp/llmmon-build.log; die "docker compose build/up failed (full log: /tmp/llmmon-build.log)"; }

HEALTH=0
for _ in $(seq 1 45); do
  if curl -sf --max-time 3 "http://127.0.0.1:$LLMON_PORT/healthz" >/dev/null; then HEALTH=1; break; fi
  sleep 2
done
[ "$HEALTH" = 1 ] || { docker logs --tail 30 llmmon 2>&1 || true; die "dashboard never answered /healthz on port $LLMON_PORT"; }
ok "healthz ok on :$LLMON_PORT"

# ------------------------------------------------- optional per-GPU power cap (reference box: 225 W x 4)
if [ -n "$GPU_WATT" ]; then
  log "+ GPU power cap ${GPU_WATT} W per GPU"
  cat > /usr/local/bin/nvidia-powerlimit.sh <<EOF
#!/bin/sh
# Persistence mode + per-GPU power cap, applied at boot by nvidia-powerlimit.service.
nvidia-smi -pm 1 >/dev/null 2>&1 || true
for gpu in \$(nvidia-smi --query-gpu=index --format=csv,noheader); do
  nvidia-smi -i "\$gpu" -pl $GPU_WATT >/dev/null 2>&1 || true
done
EOF
  chmod 755 /usr/local/bin/nvidia-powerlimit.sh
  install -m 644 "$LLMON_DIR/system/nvidia-powerlimit.service" /etc/systemd/system/nvidia-powerlimit.service
  systemctl daemon-reload && systemctl enable --now nvidia-powerlimit.service >/dev/null 2>&1
  ok "capping at ${GPU_WATT} W/GPU (nvidia-powerlimit.service)"
fi

# ---------------------------------------------------------------- optional tailscale
if [ "$WITH_TAILSCALE" = "1" ]; then
  log "+ Tailscale (HTTPS access from anywhere in your tailnet)"
  if ! command -v tailscale >/dev/null 2>&1; then
    curl -fsSL https://tailscale.com/install.sh | sh >/dev/null 2>&1 || warn "tailscale install failed"
  fi
  if command -v tailscale >/dev/null 2>&1; then
    TS_IP="$(tailscale ip -4 2>/dev/null | head -1 || true)"
    [ -n "$TS_IP" ] || { echo "    run: sudo tailscale up   (browser auth), then re-run this installer"; } \
                   || { tailscale serve --bg "https+insecure://http://${HOST_IP}:${LLMON_PORT}" >/dev/null 2>&1 \
                        && ok "tailscale serve -> https://$(tailscale fqdn 2>/dev/null | sed 's/\.$//')"; }
  fi
fi

# ---------------------------------------------------------------- summary
log "done"
cat <<EOF
  Dashboard   http://${HOST_IP:-<this-host-ip>}:$LLMON_PORT        (LAN — no auth, keep it private)
              $(command -v tailscale >/dev/null 2>&1 && tailscale serve status 2>/dev/null | head -1 || echo 'tailscale: not configured (re-run with --tailscale)')

  Load a model as $TARGET_USER:
      export PATH="$LMS_HOME/bin:\$PATH"
      lms get <publisher>/<model>                     # download
      lms ls                                          # copy the printed key into .env
      nano $ENV_FILE                                  # set LLMON_MODEL_A_KEY / _ID, then:
      cd $LLMON_DIR/llmmon && docker compose up -d --build

  Day-2 operations (logs, restarts, API, troubleshooting): $LLMON_DIR/docs/OPERATIONS.md
EOF
