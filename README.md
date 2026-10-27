# Providence-LMS

Self-hosted **LM Studio inference server + monitoring/control dashboard** for NVIDIA GPU rigs on
Ubuntu Server. One Docker container (**llmmon**) watches your GPUs and serves a web dashboard that can
also start the LM Studio daemon, load/unload models, restart the runtime and reboot the box.

Built for headless boxes — no GUI, no vLLM, no Kubernetes. Everything runs as `systemd` + one container.

---

## Install in one line

On a fresh **Ubuntu Server** machine with at least one NVIDIA GPU:

```bash
curl -fsSL https://raw.githubusercontent.com/godeater206/Providence-LMS/main/install.sh | sudo bash
```

That installs the NVIDIA driver, Docker + Compose, the NVIDIA Container Toolkit, LM Studio's headless
daemon (`llmster` + `lms`), and then builds and starts the dashboard on **port 8899**. Re-run the same
line any time you want to update or re-apply configuration — every step is idempotent.

Useful flags (pass them after `bash -s --`):

```bash
curl -fsSL https://raw.githubusercontent.com/godeater206/Providence-LMS/main/install.sh \
  | sudo bash -s -- --port=8899 --gpu-watt=225 --tailscale
```

| Flag | Meaning |
|---|---|
| `--user=NAME` | Linux user that owns `~/.lmstudio` and runs `lms` (default: `$SUDO_USER`) |
| `--port=N` | Dashboard port (default `8899`) |
| `--dir=PATH` | Where to clone this repo (default `~NAME/Providence-LMS`) |
| `--gpu-watt=N` | Install a systemd unit capping every GPU at N watts (the reference rig uses 225 W) |
| `--tailscale` | Install Tailscale and expose the dashboard as HTTPS inside your tailnet |
| `--skip-driver` / `--skip-docker` | Skip those stages if you already manage them yourself |

> If the driver stage installs a new kernel module it may need a reboot. The installer tells you, then
> exits with code 3 — reboot and re-run the same one-liner to finish.

---

## What you get

**Dashboard** (`http://<server-ip>:8899`, dark theme, auto-refresh every 2 s)

- Per-GPU cards: temperature, power draw vs limit, utilisation, VRAM used/total, fan %, clock — with bars
- System cards: CPU package power (RAPL, instantaneous and rollover-corrected), CPU + NVMe temperatures,
  load average, RAM/swap, disk usage
- Header chip with **live CPU GHz** — fastest active core, hover for avg / min / boost ceiling — read from
  `cpufreq scaling_cur_freq` so it tracks what the governor is actually doing
- LM Studio card: which model is loaded, context length, parallel slots (live per-slot pips),
  generate/prompt **tok/s**, speculative draft acceptance, recent requests
- Two **load buttons** whose labels are the identifiers they load, plus **Unload All** and **Restart LMS**
- Reboot-server button (SSH + sudoers helper) and Restart-dashboard button

**Control API** (no auth — see [Security](#security))

```bash
curl -s http://127.0.0.1:8899/api/stats | python3 -m json.tool   # telemetry + 50 min history
curl -s -X POST http://127.0.0.1:8899/api/lms/load-fn            # load model B (unloads first)
curl -s -X POST http://127.0.0.1:8899/api/reboot \
     -H 'Content-Type: application/json' -d '{"confirm":"REBOOT"}'
```

Full endpoint list and the `ctl` progress object: [docs/OPERATIONS.md](docs/OPERATIONS.md).

## Architecture

```
Browser ── HTTP :8899 (LAN / tailnet) ──┐
                                        ▼
                       ┌────────────── llmmon (Docker) ──────────────┐
                       │ server.py  stdlib http.server + sampler     │
                       │   nvidia-smi ─▶ GPU telemetry               │
                       │   /proc      ─▶ CPU, llama-server slots     │
                       │   /rapl      ─▶ CPU package power           │
                       │   hwmon      ─▶ CPU + NVMe temps            │
                       │   lms CLI    ─▶ daemon / load / unload      │
                       └───────┬───────────────────────┬─────────────┘
                        bind mount                host network + PID ns
                               ▼                       ▼
                    ~/Providence-LMS/llmmon     llmster (LM Studio daemon)
                    .env, ssh key, logs         └─ llama-server :127.0.0.1:<ephemeral>
                                                        ▲
                                          LM Link / your other devices
```

Design decisions worth knowing before you change anything:

- `network_mode: host` + `pid: host` — the dashboard must see host PIDs (to signal orphaned backends)
  and reach the backend's localhost port.
- Runs **as root**: RAPL energy counters are root-only, and it owns the LM Studio lifecycle. Files it
  creates under `~/.lmstudio` become root-owned; that is expected.
- `security_opt: apparmor=unconfined` — required. The default AppArmor profile blocks container→host
  `kill(2)` (EPERM), which breaks *Restart LMS* and the orphan sweep.
- **No graphs**: telemetry cards use plain CSS bars, so there is no Chart.js/CDN dependency to fail.

## Requirements

| | Minimum | Reference rig this was built on |
|---|---|---|
| OS | Ubuntu Server 22.04+ (x86_64), systemd | Ubuntu 26.04 LTS, HUANANZHI H12D-8D |
| CPU | 4 cores; `k10temp`/`coretemp` for CPU temp | AMD EPYC 7F32, 8c/16t |
| GPU | 1× NVIDIA, ≥12 GB VRAM, driver ≥ 550 | 4× RTX 3090 (96 GB), driver 580.178.04 |
| RAM | ≈ model size + 16 GB | 123 GiB |
| Disk | 20 GB free for models to start with | 15 TB `/home` mounted at `~/.lmstudio/models` |
| Docker | Engine with Compose v2 + NVIDIA Container Toolkit | Docker 29.x, Compose 5.x |

VRAM is the constraint that matters: a 3-bit 84 GB MoE needs all four 24 GB cards, so *Load* on this
dashboard deliberately unloads everything else first.

## What the installer does

1. Installs base packages (`git`, `curl`, `pciutils`, `kmod`, …).
2. Detects NVIDIA GPUs; installs the driver via `ubuntu-drivers autoinstall` (falls back to
   `nvidia-driver-580-open`) plus matching kernel headers — only if `nvidia-smi` is not already working.
3. Installs Docker Engine + Compose (`get.docker.com`) and adds your user to the `docker` group.
4. Installs **NVIDIA Container Toolkit** and runs `nvidia-ctk runtime configure --runtime=docker`.
5. Installs LM Studio headless for your user: `curl -fsSL https://lmstudio.ai/install.sh | bash`, then
   `lms daemon up`.
6. Clones this repo to `~/Providence-LMS` (later runs do `git fetch` + hard reset, so it self-updates).
7. Installs the reboot helper `/usr/local/bin/reboot_server.sh`, a `NOPASSWD` sudoers rule for your user,
   and generates `llmmon/ssh/id_ed25519` wired into `~/.ssh/authorized_keys`.
8. Writes `llmmon/.env`: hostname, port, SSH target, **autodetected RAPL counter path** (Intel or AMD),
   and blank model-button slots so nothing is guessed for you.
9. `docker compose up -d --build`, then polls `/healthz` until the dashboard answers.
10. Optionally (`--gpu-watt`) installs `nvidia-powerlimit.service`; optionally (`--tailscale`) sets up
    `tailscale serve` HTTPS.

## After installing: load your first model

```bash
export PATH="$HOME/.lmstudio/bin:$PATH"
lms get <publisher>/<model>              # download (or copy a GGUF into ~/.lmstudio/models)
lms ls                                   # note the exact key it prints
nano ~/Providence-LMS/llmmon/.env        # LLMON_MODEL_A_KEY / _ID (+ CTX)
cd ~/Providence-LMS/llmmon && docker compose up -d --build
```

Then open `http://<server-ip>:8899` and press the button — it runs
`lms load <key> --gpu max --context-length <ctx> --identifier <id>` for you. Button labels follow the
identifiers you configured, so there is no drift between what the UI says and what gets loaded.

## Manual install (if you'd rather run the steps yourself)

```bash
# 1-2. driver
sudo apt update && sudo apt install -y linux-headers-$(uname -r)
sudo ubuntu-drivers autoinstall && sudo reboot      # only if nvidia-smi is missing

# 3. docker + compose plugin
curl -fsSL https://get.docker.com | sh && sudo usermod -aG docker $USER   # re-login after

# 4. container toolkit
sudo apt install -y nvidia-container-toolkit
sudo nvidia-ctk runtime configure --runtime=docker && sudo systemctl restart docker

# 5. LM Studio headless
curl -fsSL https://lmstudio.ai/install.sh | bash
~/.lmstudio/bin/lms daemon up

# 6. dashboard
git clone https://github.com/godeater206/Providence-LMS ~/Providence-LMS
cd ~/Providence-LMS/llmmon && cp .env.example .env && nano .env      # set LLMON_DIR + LMS_HOME absolute paths
docker compose up -d --build
curl -s localhost:8899/healthz                                        # -> ok

# 7. reboot button (optional)
sudo install -m 755 ~/Providence-LMS/system/reboot_server.sh /usr/local/bin/reboot_server.sh
echo "$USER ALL=(ALL) NOPASSWD: /usr/local/bin/reboot_server.sh" | sudo tee /etc/sudoers.d/llmmon-reboot
ssh-keygen -t ed25519 -N '' -C llmmon-reboot -f ~/Providence-LMS/llmmon/ssh/id_ed25519
cat ~/Providence-LMS/llmmon/ssh/id_ed25519.pub >> ~/.ssh/authorized_keys
```

## Updating and day-2 operations

```bash
cd ~/Providence-LMS && git pull && docker restart llmmon      # code is bind-mounted: no rebuild needed
docker logs -f llmmon                                          # dashboard log
cat ~/Providence-LMS/llmmon/autostart.log                      # boot-time daemon/model bootstrap
curl -s -X POST localhost:8899/api/self/restart                # re-exec without touching the model
```

Because `server.py` is bind-mounted, editing it (or `.env`) and restarting the container is enough — no
image rebuild. Full runbook, API reference and troubleshooting recipes: **[docs/OPERATIONS.md](docs/OPERATIONS.md)**.

## Security

- **The dashboard has no authentication.** Anyone who can reach port 8899 can load/unload models and
  reboot the server. Keep it on the LAN or your tailnet; do not expose it to the internet.
- If you want a firewall: `sudo ufw allow in on <lan-iface> to any port 8899` (the reference box relies
  on network boundaries instead and leaves `ufw` inactive).
- Recommended remote access is Tailscale (`--tailscale`), which gives you HTTPS with tailnet ACLs:
  `sudo tailscale serve --bg https+insecure://http://<lan-ip>:8899`.
- The reboot button SSHes to the machine as your user using a dedicated key and a single `NOPASSWD`
  sudoers rule for `/usr/local/bin/reboot_server.sh` — no broad sudo grant.

## Troubleshooting quick hits

| Symptom | Cause / fix |
|---|---|
| Dashboard shows *LM Studio: offline* but the model is loaded | Normal if you don't use `lms server start`: inference runs on an ephemeral localhost port and via **LM Link**. `Server: OFF` in `lms status` only means the optional :1234 API is off. |
| CPU power reads 0 W | No/odd RAPL counter for your CPU. The process self-restarts after ~90 s to clear a wedged counter; if it stays 0, point `LLMON_RAPL_FILE` at the right `/rapl/...energy_uj`. |
| *Restart LMS* fails with EPERM | AppArmor: keep `apparmor=unconfined` in the compose file. |
| VRAM not freed after Unload | Orphaned `llama-server` from a SIGKILLed daemon — **Unload All** sweeps them; otherwise `pkill -f llama-server`. |
| Container won't start, `$'\r': command not found` | A shell script got Windows line endings: `sed -i 's/\r$//' llmmon/*.sh`. |
| Dashboard loads but nothing updates | You edited JS inside the Python string in `server.py`; it is served verbatim, so a syntax error kills all updates. Check with any JS checker before restarting. |

## Repo layout

```
install.sh                  ← the one-liner bootstrap (idempotent)
uninstall.sh                ← remove the dashboard, keep drivers/LM Studio
llmmon/
  server.py                 ← the whole dashboard: sampler + stdlib HTTP server + UI (~2050 lines)
  docker-compose.yml        ← host net/pid, gpus: all, apparmor=unconfined, bind mounts
  Dockerfile                ← python:3.12-slim + procps + openssh-client
  entrypoint.sh             ← XDG runtime dir, lms daemon up, then serve
  load_model.sh             ← boot-time wait/report (does not auto-load a model)
  .env.example              ← all configuration knobs, documented
system/
  reboot_server.sh          ← /usr/local/bin helper for the reboot button
  llmmon-reboot.sudoers     ← NOPASSWD rule template
  nvidia-powerlimit.service ← per-GPU power cap unit (--gpu-watt)
docs/OPERATIONS.md          ← API reference, env vars, runbook, troubleshooting
```

## Provenance

This is the production stack of a home-LAN box: 4× RTX 3090 at a 225 W cap, AMD EPYC,
123 GiB RAM, Ubuntu Server, LM Studio serving an 84 GB Qwen MoE at 262 K context over LM Link. The
dashboard has run that rig since 2026-09; the installer and `.env` configuration layer were added to make
the same setup reproducible on a new machine.

## License

MIT — see [LICENSE](LICENSE).
