# llmmon — operations runbook

Day-2 reference for the dashboard installed by [../install.sh](../install.sh). Everything here assumes
the container is named `llmmon` and listens on `8899`.

## Controlling the service

```bash
cd ~/Providence-LMS/llmmon

docker compose up -d --build          # create/recreate (build only if Dockerfile changed)
docker restart llmmon                 # after editing server.py or .env  (bind-mounted, no rebuild)
docker logs -f llmmon                 # live dashboard log (also written to mon.log)
cat autostart.log                     # boot-time daemon/model bootstrap output
curl -s http://127.0.0.1:8899/healthz # -> ok

# update the code, then apply
cd ~/Providence-LMS && git pull && docker restart llmmon
```

The container's restart policy is `unless-stopped`: it survives host reboots and crashes, but a manual
`docker stop` stays stopped until something starts it again.

## HTTP API

Base `http://127.0.0.1:8899`. **No authentication** — the trust boundary is your network. Everything is
`POST` with an empty body except `/api/reboot`, which requires a confirmation payload.

| Method | Path | Effect |
|---|---|---|
| GET | `/` | dashboard HTML (rendered from `server.py`; button labels come from the configured identifiers) |
| GET | `/api/stats` | current telemetry + 50 min history (1500 samples @ 2 s). The UI renders only `current` since the graph cards were removed; `history` is still returned for external collectors |
| POST | `/api/lms/load` | load **button A** (`LLMON_MODEL_A_KEY` → `LLMON_MODEL_A_ID`). No-ops if that identifier is already loaded, otherwise unloads other instances first |
| POST | `/api/lms/load-fn` | load **button B** (`LLMON_MODEL_B_*`) after an **unconditional** `lms unload --all` — for models that need essentially all VRAM |
| POST | `/api/lms/unload` | `lms unload --all`, then SIGTERM→SIGKILL any orphaned `llama-server` backends so VRAM is actually released. Daemon stays up |
| POST | `/api/lms/restart` | graceful `lms daemon down` → signal escalation if needed (exact `/proc/<pid>/comm == llmster` match) → `daemon up` → re-check models |
| POST | `/api/reboot` | body **must** be `{"confirm":"REBOOT"}`; SSHes to `LLMON_REBOOT_SSH` with `LLMON_REBOOT_KEY` and runs `sudo /usr/local/bin/reboot_server.sh` |
| POST | `/api/self/restart` | re-exec the dashboard process (use instead of `docker restart` when you only touched code) |
| POST | `/api/gpu/fan` | *orphan endpoint*: accepts `{"gpu":0,"mode":"auto","pct":97}` but always errors — this driver's `nvidia-smi` has no `-fan`/`--gpu-fan-speed` options. Kept as a hook for a future NVML implementation; there is no UI for it |
| GET | `/healthz` | liveness probe (`ok`) |

Long operations report progress through the `ctl` object inside `/api/stats`:

```json
{"op": "load-fn", "state": "running", "step": "unloading all existing models",
 "started": 1792712575.4, "finished": null, "message": null}
```

`state` goes `running → done | error`; the UI shows it as a staged overlay and keeps the final message on
screen for 10 s. Concurrency is guarded: a second control request while one is running gets HTTP 409
`{"error": "operation already in progress"}`.

### What `/api/stats` returns

`current` carries `ts`, `time`, `hostname`, then grouped objects:

| Group | Fields |
|---|---|
| `cpu` | `temp_c`, `usage_pct`, `cores`, and live frequency: `ghz` (fastest active core), `avg_ghz`, `min_ghz`, `boost_ghz`, `freq_source` |
| `mem` / `load` / `disk` | GB + percentages; load average and uptime string; disk is the filesystem at `LLMON_DISK_PATH` |
| `nvme_temps`, `sys_power_w` | hwmon drive temperatures; RAPL package power in watts |
| `gpus[]` | one entry per GPU: `index`, `name`, `pci_bus_id`, `temp_c`, `power_w`, `power_limit_w`, `util_pct`, `mem_util_pct`, `mem_used_mib`, `mem_total_mib`, `fan_pct`, `clock_mhz` |
| `lmstudio` | `available`, `instances[]` (identifier, model, quant, size, status, context, parallel), `gen_tps`, `prompt_tps`, `draft_acceptance`, `active_slots`, `recent_requests`, plus the `ctl` and `heal` objects above |

`history` holds one array per metric — `t` (HH:MM:SS labels, not epoch), `sys_power`, `cpu_temp`,
`cpu_usage`, `cpu_ghz`, `mem_used`, `swap_used`, `gpu_temp`/`gpu_power`/`gpu_util`/`gpu_mem`, `gen_tps`,
`prompt_tps`, `lm_slots`. Every `gpu_*` sample carries one value **per GPU**, so index it as
`history.gpu_temp[i][gpu_index]`. Two things to expect: `sys_power[0]` is always `null` (that first sample
only records the RAPL baseline), and the buffer lives in RAM — it refills from zero after every container
restart.

The GHz figures come from `/sys/devices/system/cpu/cpu*/cpufreq/scaling_cur_freq`, i.e. what the governor
actually runs; boards without cpufreq fall back to `/proc/cpuinfo` (hence `freq_source`). Prefer the cpufreq
source: the cpuinfo "cpu MHz" field is a per-task TSC estimate that can disagree with the governor by over
1 GHz within a single sample.

```bash
curl -s http://127.0.0.1:8899/api/stats | python3 -m json.tool | head -60
curl -s -X POST http://127.0.0.1:8899/api/lms/load-fn
curl -s -X POST http://127.0.0.1:8899/api/reboot \
     -H 'Content-Type: application/json' -d '{"confirm":"REBOOT"}'
```

## Configuration reference

Set these in `llmmon/.env` (compose interpolates them) or as container environment variables. Blank
values are treated as unset, so an empty line never wipes a default.

| Variable | Default | Purpose |
|---|---|---|
| `LLMON_HOST` | `0.0.0.0` | bind address (host networking — leave alone) |
| `LLMON_PORT` | `8899` | dashboard port |
| `LLMON_HOSTNAME` | kernel hostname | header label |
| `LLMON_LMS_HOME` | `~/.lmstudio` | where the `lms` CLI + daemon state live (in-container path `/root/.lmstudio`) |
| `LLMON_DISK_PATH` | `/home` | path whose free space is shown as "Disk" |
| `LLMON_RAPL_FILE` | `/rapl/intel-rapl/intel-rapl:0/energy_uj` | CPU package power source; AMD boards often need a different `amd-rapl*` path (the installer autodetects) |
| `LLMON_DOCKER_SOCK` | `/var/run/docker.sock` | used for the optional Home Assistant card |
| `LLMON_REBOOT_SSH` | *(empty)* | SSH target for the reboot button; empty disables it |
| `LLMON_REBOOT_KEY` | *(empty)* | private key used for that SSH call |
| `LLMON_MODEL_A_KEY` / `_ID` / `_CTX` | qwen3.8-27b-turbo-fable… / `Uncensored-262K` / `262144` | button A: model key, identifier, context length |
| `LLMON_MODEL_B_KEY` / `_ID` / `_CTX` | qwen3.8-flash-next-gsq-rco-abliterated / `Qwen-Abliterated` / `262144` | button B (unload-all first) |

Model keys must match what `lms ls` prints. The load command behind a button is exactly:

```bash
lms load "$KEY" --gpu max --context-length "$CTX" --identifier "$ID" --yes
```

After changing `.env`: `docker restart llmmon`, then reload the page — the labels and hint line update with it.

## Self-healing behaviour (don't mistake this for a crash)

- **RAPL counter wedge** → package power is the delta between *consecutive* reads of the energy counter,
  corrected for its rollover at `max_energy_range_uj` (on many boards that wraps roughly every 20 min at
  idle draw). If the counter stops advancing **at all** for ~90 s after healthy readings, the process exits
  and Docker respawns it with a clean baseline; the exit line prints the raw `e0`/`e`/`dt`/`wrap` values.
  A reading that is low but still counting is treated as data — ~10 min below 10 W logs one warning and keeps
  running, because an idle CPU package genuinely draws that little.
- **Daemon revival** → when `lms ps` starts failing, llmmon tries `lms daemon up` on its own (`heal` object in `/api/stats`).
- **Orphan sweep** → after SIGKILLing a daemon, leftover `llama-server` processes are killed explicitly so VRAM is released. If an orphan is owned by the host and AppArmor blocks the signal, the unload message warns you to `pkill -f llama-server` over SSH.
- **Boot bootstrap does not auto-load models** — it waits for the API and reports what is loaded. Models come up when you press a button (by design: an 84 GB load should be a decision, not a boot side-effect).

## Troubleshooting recipes

```bash
# dashboard down?
docker ps | grep llmmon; docker logs --tail 50 llmmon; ss -ltnp | grep 8899

# model won't load / VRAM not freed?
cat ~/Providence-LMS/llmmon/autostart.log
nvidia-smi --query-gpu=index,memory.used,utilization.gpu,power.draw --format=csv
docker exec llmmon /root/.lmstudio/bin/lms ps
pgrep -af llama-server                      # orphans -> press "Unload All", or pkill -f llama-server

# Restart-LMS failing with EPERM?
grep -i eperm ~/Providence-LMS/llmmon/mon.log   # then confirm apparmor=unconfined is still in the compose file

# CPU power stuck at ~0 W?
docker exec llmmon cat /rapl/intel-rapl/intel-rapl:0/energy_uj    # adjust LLMON_RAPL_FILE if this path is wrong

# container fails to start with "$'\r': command not found"
sed -i 's/\r$//' ~/Providence-LMS/llmmon/*.sh && docker compose up -d --force-recreate
```

### "LM Studio Server: OFF" is normal here

`lms status` reports `Server: OFF` because nothing listens on :1234/:1978 — this stack does not use LM
Studio's unified REST server. Inference runs through the per-instance `llama-server` backend on an
ephemeral localhost port (`pgrep -af llama-server`, health at `http://127.0.0.1:<port>/health`) and clients
reach it over **LM Link** (`lms link status`). Only run `lms server start` if you specifically want the
:1234 OpenAI-compatible endpoint for external clients; nothing in llmmon needs it.

### Fan speed is read-only

Only some cards expose a tachometer through NVML (on the reference rig only GPU 1 does; the others report
0 %). `nvidia-smi` on Linux/GeForce has no fan-control flags, so there is no control button — if you want
manual curves, implement them with NVML (`nvmlDeviceSetFanSpeedPolicy`) or a vendor tool and wire it to the
existing `/api/gpu/fan` hook.

### Editing `server.py` safely

The UI is one ~950-line raw string inside a Python file: `py_compile` proves nothing about the JavaScript,
and the page is served verbatim (no template engine). After editing:

1. check brace/paren balance of the `<script>` block with any JS-aware tool (`node --check` on the extracted script), and
2. grep for identifiers you removed so no caller is left dangling, then
3. `docker restart llmmon` and reload the page (hard refresh — the HTML is not cached by a service worker but browsers do cache it).

Keep files **LF**: Windows line endings break `entrypoint.sh`/`load_model.sh` inside the container.

## Remote access

Recommended: Tailscale, which gives HTTPS + ACLs without exposing anything publicly.

```bash
sudo tailscale up
sudo tailscale serve --bg https+insecure://http://<lan-ip>:8899
tailscale serve status          # https://<machine>.<tailnet>.ts.net -> http://<lan-ip>:8899
```

Plain LAN access is fine too (`http://<lan-ip>:8899`) as long as the network is trusted. If you firewall,
restrict 8899 to your LAN range — remember there is no login in front of a reboot button.

## Home Assistant card (optional)

`/var/run/docker.sock` is mounted read-write into the container and `/api/stats` is plain JSON, so a HA
RESTful sensor/card can poll it directly:

```yaml
sensor:
  - platform: rest
    name: llmmon_gpu_power
    resource_url: http://<server>:8899/api/stats
    value_template: "{{ value_json.current.gpus | map(attribute='power_w') | sum | round(0) }}"
```

## Teardown

```bash
cd ~/Providence-LMS && sudo bash uninstall.sh            # container + image + sudo rule
sudo bash uninstall.sh --all                             # also the checkout and system helpers
~/.lmstudio/bin/lms daemon down && rm -rf ~/.lmstudio    # LM Studio itself (models included)
```
