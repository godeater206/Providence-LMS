#!/usr/bin/env python3
"""Providence LLM — system monitor dashboard.
Stdlib only. Serves a dark-themed dashboard with live GPU/CPU/power/memory stats.
"""
import json
import os
import re
import shutil
import signal
import socket
import subprocess
import sys
import threading
import time
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

HOST = os.environ.get("LLMON_HOST", "0.0.0.0")
PORT = int(os.environ.get("LLMON_PORT", "8899"))
SAMPLE_INTERVAL = 2.0
MAX_SAMPLES = 1500  # 50 min @ 2s

# Docker-aware configuration. Inside the container these resolve to the
# mounted host dirs; on a bare host they are the original locations.
HOSTNAME = os.environ.get("LLMON_HOSTNAME") or os.uname().nodename
LMS_HOME = os.environ.get("LLMON_LMS_HOME", os.path.expanduser("~/.lmstudio"))
LMS_BIN = os.path.join(LMS_HOME, "bin", "lms")
LMS_LOGS = os.path.join(LMS_HOME, "server-logs")
DISK_PATH = os.environ.get("LLMON_DISK_PATH", "/home")
RAPL_FILE = os.environ.get("LLMON_RAPL_FILE",
                           "/sys/class/powercap/intel-rapl:0/energy_uj")
DOCKER_SOCK = os.environ.get("LLMON_DOCKER_SOCK", "/var/run/docker.sock")
# Reboot mechanism: on the bare host the sudo helper runs directly; in docker
# the same host helper is invoked over SSH with a dedicated key.
REBOOT_SSH = os.environ.get("LLMON_REBOOT_SSH", "")
REBOOT_KEY = os.environ.get("LLMON_REBOOT_KEY", "")


def _env(name, default):
    """Env override that treats blank/whitespace values as unset.

    A bare `LLMON_MODEL_A_KEY=` line in .env would otherwise hand an empty
    string to os.environ.get() and break the load command."""
    v = os.environ.get(name)
    return v.strip() if v is not None and v.strip() else default

# ---------------------------------------------------------------- collectors

def find_hwmon(name):
    base = "/sys/class/hwmon"
    for d in os.listdir(base):
        try:
            with open(os.path.join(base, d, "name")) as f:
                if f.read().strip() == name:
                    return os.path.join(base, d)
        except OSError:
            pass
    return None

def read_temp(path, scale=1.0):
    try:
        with open(path) as f:
            return round(int(f.read().strip()) * scale / 1000.0, 1)
    except (OSError, ValueError):
        return None

K10 = find_hwmon("k10temp")

def cpu_temp():
    if not K10:
        return None
    return read_temp(os.path.join(K10, "temp1_input"))

NVME_DIRS = []
_base = "/sys/class/hwmon"
try:
    for d in sorted(os.listdir(_base)):
        try:
            with open(os.path.join(_base, d, "name")) as f:
                if f.read().strip() == "nvme":
                    NVME_DIRS.append(os.path.join(_base, d))
        except OSError:
            pass
except OSError:
    pass

def nvme_temps():
    out = []
    for d in NVME_DIRS:
        t = read_temp(os.path.join(d, "temp1_input"))
        if t is not None:
            out.append(t)
    return out

_prev_cpu = None

def _cpu_jiffies():
    with open("/proc/stat") as f:
        parts = f.readline().split()[1:]
    vals = list(map(int, parts))
    idle = vals[3] + (vals[4] if len(vals) > 4 else 0)
    return idle, sum(vals)

def cpu_usage():
    global _prev_cpu
    try:
        idle, total = _cpu_jiffies()
    except OSError:
        return None
    if _prev_cpu is None:
        _prev_cpu = (idle, total)
        return 0.0
    p_idle, p_total = _prev_cpu
    _prev_cpu = (idle, total)
    dt = total - p_total
    if dt <= 0:
        return None
    return round(100.0 * (dt - (idle - p_idle)) / dt, 1)

# ---- live CPU frequency -------------------------------------------------------
# Cores on this EPYC board boost independently under the schedutil governor, so the
# header shows the fastest active core and keeps avg/min/ceiling for the tooltip.
# scaling_cur_freq is preferred over /proc/cpuinfo's "cpu MHz": that field is a per-task
# TSC estimate and disagrees with what the governor actually runs (observed 2.49 GHz next
# to 3.89 GHz in one sample). The container sees this sysfs tree without an extra bind
# mount; where cpufreq is absent, fall back to /proc/cpuinfo so a fresh install still
# reports something rather than "—".
CPUFREQ_DIR = "/sys/devices/system/cpu"

def _cpu_dirs():
    try:
        return sorted(n for n in os.listdir(CPUFREQ_DIR) if re.fullmatch(r"cpu\d+", n))
    except OSError:
        return []

def _read_khz(path):
    try:
        with open(path) as f:
            khz = int(f.read().strip())
        return khz if khz > 0 else None
    except (OSError, ValueError):
        return None

def _cpuinfo_mhz():
    vals = []
    try:
        with open("/proc/cpuinfo") as f:
            for line in f:
                if line.lower().startswith("cpu mhz"):
                    khz = int(float(line.split(":", 1)[1].strip()) * 1000)
                    if khz > 0:
                        vals.append(khz)
    except (OSError, ValueError):
        return []
    return vals

def cpu_ghz():
    """Current CPU frequency in GHz: fastest / average / slowest core + boost ceiling."""
    dirs = _cpu_dirs()
    cur, ceiling = [], None
    for d in dirs:
        khz = _read_khz(os.path.join(CPUFREQ_DIR, d, "cpufreq", "scaling_cur_freq"))
        if khz:
            cur.append(khz)
        if ceiling is None:
            ceiling = _read_khz(os.path.join(CPUFREQ_DIR, d, "cpufreq", "cpuinfo_max_freq"))
    source = "cpufreq"
    if not cur:
        cur, source = _cpuinfo_mhz(), "cpuinfo"
    if not cur:
        return {"ghz": None, "avg_ghz": None, "min_ghz": None,
                "boost_ghz": None, "freq_source": None}
    top = ceiling or max(cur)
    return {
        "ghz": round(max(cur) / 1e6, 2),
        "avg_ghz": round(sum(cur) / len(cur) / 1e6, 2),
        "min_ghz": round(min(cur) / 1e6, 2),
        "boost_ghz": round(top / 1e6, 2),
        "freq_source": source,
    }

def meminfo():
    info = {}
    with open("/proc/meminfo") as f:
        for line in f:
            k, v = line.split(":", 1)
            info[k] = int(v.strip().split()[0])  # kB
    total = info.get("MemTotal", 0)
    avail = info.get("MemAvailable", 0)
    s_total = info.get("SwapTotal", 0)
    s_free = info.get("SwapFree", 0)
    return {
        "total_gb": round(total / 1048576.0, 1),
        "used_gb": round((total - avail) / 1048576.0, 1),
        "pct": round(100.0 * (total - avail) / total, 1) if total else None,
        "swap_total_gb": round(s_total / 1048576.0, 1),
        "swap_used_gb": round((s_total - s_free) / 1048576.0, 1),
    }

def load_uptime():
    load = os.getloadavg()
    with open("/proc/uptime") as f:
        up = float(f.read().split()[0])
    d, r = divmod(int(up), 86400)
    h, r = divmod(r, 3600)
    m, _ = divmod(r, 60)
    return {"load": [round(x, 2) for x in load], "uptime": f"{d}d {h}h {m}m"}

def disk():
    try:
        u = shutil.disk_usage(DISK_PATH)
        return {"total_tb": round(u.total / 1099511627776, 1),
                "used_tb": round(u.used / 1099511627776, 1),
                "pct": round(100.0 * u.used / u.total, 1)}
    except OSError:
        return None

GPU_QUERY = ("index,name,pci.bus_id,temperature.gpu,power.draw,power.limit,utilization.gpu,"
             "utilization.memory,memory.used,memory.total,fan.speed,"
             "clocks.current.graphics")

def gpus():
    try:
        out = subprocess.run(
            ["nvidia-smi", f"--query-gpu={GPU_QUERY}", "--format=csv,noheader,nounits"],
            capture_output=True, text=True, timeout=10)
        result = []
        for line in out.stdout.strip().splitlines():
            p = [x.strip() for x in line.split(",")]
            if len(p) < 12:
                continue
            def f(v):
                try:
                    return float(v)
                except ValueError:
                    return None
            result.append({
                "index": int(p[0]),
                "name": p[1],
                "pci_bus_id": p[2],
                "temp_c": f(p[3]),
                "power_w": f(p[4]),
                "power_limit_w": f(p[5]),
                "util_pct": f(p[6]),
                "mem_util_pct": f(p[7]),
                "mem_used_mib": f(p[8]),
                "mem_total_mib": f(p[9]),
                "fan_pct": f(p[10]),
                "clock_mhz": f(p[11]),
            })
        return result
    except (OSError, subprocess.SubprocessError):
        return []

# ---- system power via RAPL (root-only energy counter)
_rapl_lock = threading.Lock()
_rapl_last = None            # (energy_uj, timestamp) of the last *usable* sample
_rapl_good_w = None          # last plausible wattage, reused when a sample can't be computed
_rapl_wrap_uj = -1           # max_energy_range_uj + 1; -1 = unresolved, 0 = unavailable
_rapl_ever_ok = False        # seen a plausible reading this process lifetime?
_rapl_frozen_count = 0       # consecutive reads where the counter did not advance
_rapl_low_count = 0          # consecutive advancing-but-below-10 W reads (real load)
_rapl_warned_low = False     # one-shot log flag per low-power episode
_RAPL_RESTART_AFTER = 45     # ~90 s of a frozen counter after being healthy => self-restart
_RAPL_WARN_LOW_AFTER = 300   # ~10 min below 10 W while still counting => warn, keep running

def _read_rapl_uj():
    """Read the RAPL energy counter. Direct file read first (works in the
    docker container, which runs as root with /sys/class/powercap mounted
    read-only); then the on-host sudo helper (bare-host deployment)."""
    try:
        with open(RAPL_FILE) as f:
            return int(f.read().strip())
    except (OSError, ValueError):
        pass
    try:
        out = subprocess.run(["sudo", "-n", "/usr/local/bin/rapl_power.sh"],
                             capture_output=True, text=True, timeout=5)
        return int(out.stdout.strip())
    except (OSError, subprocess.SubprocessError, ValueError):
        return None

def _rapl_wrap_period():
    """Counter modulus for wraparound correction: max_energy_range_uj + 1.

    The package counter is a free-running microjoule accumulator that rolls over at
    max_energy_range_uj (65 532 610 987 uJ on this board, i.e. roughly every 20 min
    at idle), so one delta per rollover comes back negative. Read the sibling sysfs
    attribute once and cache it; 0 means the file is absent (older kernels, or a
    bare-host deploy without the mount) and no correction applies."""
    global _rapl_wrap_uj
    if _rapl_wrap_uj < 0:
        try:
            p = os.path.join(os.path.dirname(RAPL_FILE), "max_energy_range_uj")
            with open(p) as f:
                _rapl_wrap_uj = int(f.read().strip()) + 1
        except (OSError, ValueError):
            _rapl_wrap_uj = 0
    return _rapl_wrap_uj or None

def sys_power_w():
    """Package power from the RAPL energy counter: delta between consecutive reads.

    Two bugs this replaces:

      * The baseline only advanced when a sample was skipped, so every reading after
        the first was an average since process start — a 57 W spike visibly decayed
        towards ~46 W over ten minutes and could not follow load at all.
      * The counter rolls over at max_energy_range_uj (~65.5 kJ here: about every
        20 min at idle). Each rollover arrived as one hugely negative delta, which
        blanked that sample and — with the stale baseline above — dragged the running
        average under the wedge threshold, restarting the dashboard roughly hourly.

    So: add the modulus back after a rollover, advance the baseline only when a sample
    is actually usable, and self-restart only for the real fault signature (the counter
    stops advancing at all). A genuinely idle package still accumulates energy, so sub-10 W
    readings are reported as data and logged once instead of killing the dashboard.

    Note that this runs from two threads — the sampler loop and every `/api/stats` request
    (`do_GET` → `snapshot()`) — hence the lock, and why a too-short interval returns the
    previous value rather than a hole: two callers landing 0.05 s apart is normal traffic,
    not missing data."""
    global _rapl_last, _rapl_ever_ok, _rapl_frozen_count, _rapl_low_count
    global _rapl_warned_low, _rapl_good_w
    e = _read_rapl_uj()
    if e is None:
        return _rapl_good_w
    now = time.time()
    with _rapl_lock:
        return _rapl_eval(e, now)

def _rapl_eval(e, now):
    """Body of sys_power_w(); called with _rapl_lock held."""
    global _rapl_last, _rapl_ever_ok, _rapl_frozen_count, _rapl_low_count
    global _rapl_warned_low, _rapl_good_w
    if _rapl_last is None:
        _rapl_last = (e, now)
        return None                              # first sample: baseline only
    e0, t0 = _rapl_last
    dt = now - t0
    if dt <= 0.5:                                # two callers raced; keep the baseline
        return _rapl_good_w
    de = e - e0
    wrap = _rapl_wrap_period()
    if de < 0 and wrap:
        de += wrap                               # counter rolled over between reads
    w = de * 1e-6 / dt
    if not (0 <= w < 2000):                      # implausible jump: glitch, keep last value
        return _rapl_good_w
    if de <= 0:                                  # wedged counter: no energy accumulated
        _rapl_frozen_count += 1
        if _rapl_ever_ok and _rapl_frozen_count >= _RAPL_RESTART_AFTER:
            print("RAPL counter frozen (no advance) for ~%ds after healthy readings "
                  "(e0=%d e=%d dt=%.1fs wrap=%s); exiting for a clean restart "
                  "(docker/systemd respawns)"
                  % (int(_RAPL_RESTART_AFTER * SAMPLE_INTERVAL), e0, e, dt, wrap), flush=True)
            os._exit(1)
    else:
        _rapl_frozen_count = 0
        if w >= 10:
            _rapl_ever_ok = True
            _rapl_low_count = 0
            _rapl_warned_low = False
        elif _rapl_ever_ok:                      # low but counting: real load, not a wedge
            _rapl_low_count += 1
            if _rapl_low_count >= _RAPL_WARN_LOW_AFTER and not _rapl_warned_low:
                _rapl_warned_low = True
                print("RAPL has reported only %.1fW for ~%ds (counter still advancing; "
                      "treating as real load, not a wedge)"
                      % (w, int(_RAPL_WARN_LOW_AFTER * SAMPLE_INTERVAL)), flush=True)
    _rapl_last = (e, now)                        # advance only on a usable sample
    _rapl_good_w = round(w, 1)
    return _rapl_good_w

# ---------------------------------------------------------------- LM Studio

_LMS_REFRESH = 20.0          # how often `lms ps --json` is re-run (each call
                             # writes ~15 DEBUG lines to the LM Studio log)
_lms_cache = None
_lms_cache_ts = 0.0


def _bust_lms_cache():
    global _lms_cache, _lms_cache_ts
    _lms_cache = None
    _lms_cache_ts = 0.0


def lms_ps():
    """Run `lms ps --json`; result cached for _LMS_REFRESH seconds."""
    global _lms_cache, _lms_cache_ts
    now = time.time()
    if _lms_cache is not None and now - _lms_cache_ts < _LMS_REFRESH:
        return _lms_cache
    try:
        out = subprocess.run([LMS_BIN, "ps", "--json"],
                             capture_output=True, text=True, timeout=10)
        if out.returncode == 0:
            _lms_cache = json.loads(out.stdout or "[]")
        else:
            # CLI ran but the daemon API didn't answer — likely down. Any lms
            # command can *wake* a dead daemon; when that doesn't happen, heal.
            _lms_cache = []
            maybe_heal_lms()
    except (OSError, subprocess.SubprocessError, ValueError):
        _lms_cache = []
        maybe_heal_lms()
    _lms_cache_ts = now
    return _lms_cache


def lmstudio_instances():
    out = []
    for inst in lms_ps():
        q = inst.get("quantization") or {}
        size = inst.get("sizeBytes") or 0
        out.append({
            "identifier": inst.get("identifier"),
            "model": inst.get("path") or inst.get("modelKey"),
            "display_name": inst.get("displayName"),
            "quantization": q.get("name"),
            "size_gb": round(size / 1073741824.0, 1) if size else None,
            "status": inst.get("status"),
            "context": inst.get("contextLength"),
            "max_context": inst.get("maxContextLength"),
            "parallel": inst.get("parallel"),
        })
    return out


_RE_PROMPT = re.compile(
    r"prompt eval time =\s+[\d.]+ ms /\s+(\d+) tokens \([^)]*?([\d.]+) tokens per second\)")
_RE_GEN = re.compile(
    r"(?<!prompt )eval time =\s+[\d.]+ ms /\s+(\d+) tokens \([^)]*?([\d.]+) tokens per second\)")
_RE_SLOT_START = re.compile(r"launch_slot_:\s+id\s+(\d+)")
_RE_SLOT_STOP = re.compile(r"release:\s+id\s+(\d+)")
_RE_DRAFT = re.compile(r"draft acceptance = ([\d.]+)")
_LOGFILE = re.compile(r"^\d{4}-\d{2}-\d{2}\.\d+\.log$")


class LmsLogTailer:
    """Incrementally tail the current llama-server log under
    ~/.lmstudio/server-logs/YYYY-MM/. Tracks per-request token speeds,
    which parallel slots are active, and spec-decoding draft acceptance.

    Note: if several llama-server instances ever share one log file, the
    "latest wins" values reflect whichever instance logged last."""

    MAX_CHUNK = 1024 * 1024   # never read more than 1 MB per sample
    START_TAIL = 65536        # on (re)start, only scan the final 64 KB

    def __init__(self):
        self.base = LMS_LOGS
        self.lock = threading.Lock()
        self.path = None
        self.inode = None
        self.offset = 0
        self.buf = b""
        self.gen_tps = None
        self.prompt_tps = None
        self.draft_acceptance = None
        self.active_slots = set()
        self.slot_launched_at = {}   # slot -> epoch when launch line was seen
        self.last_activity = None
        self.seen_any = False
        self.recent_requests = []   # [[HH:MM:SS, gen_tps, prompt_tps], ...] newest first

    def _months(self):
        now = datetime.now()
        py, pm = (now.year - 1, 12) if now.month == 1 else (now.year, now.month - 1)
        return ["%04d-%02d" % (now.year, now.month), "%04d-%02d" % (py, pm)]

    def _current_file(self):
        best = None
        for d in self._months():
            p = os.path.join(self.base, d)
            try:
                names = os.listdir(p)
            except OSError:
                continue
            for n in names:
                if not _LOGFILE.match(n):
                    continue
                fp = os.path.join(p, n)
                try:
                    mt = os.path.getmtime(fp)
                except OSError:
                    continue
                if best is None or mt > best[0]:
                    best = (mt, fp)
        return best[1] if best else None

    def read_new(self):
        with self.lock:
            try:
                self._read_new_unlocked()
            except OSError:
                pass

    def _read_new_unlocked(self):
        path = self._current_file()
        if path is None:
            return
        st = os.stat(path)
        if path != self.path or st.st_ino != self.inode:
            # new or rotated file: jump to a small tail to avoid rescanning
            self.path, self.inode = path, st.st_ino
            self.offset = max(0, st.st_size - self.START_TAIL)
            self.buf = b""
        if st.st_size < self.offset:
            self.offset = 0
        if st.st_size <= self.offset:
            return
        with open(path, "rb") as f:
            f.seek(self.offset)
            data = f.read(self.MAX_CHUNK)
            if not data:
                return
            self.offset += len(data)
        data = self.buf + data
        i = data.rfind(b"\n")
        if i < 0:
            self.buf = data[-8192:]
            return
        self.buf = data[i + 1:]
        for line in data[:i].decode("utf-8", "replace").splitlines():
            self._consume(line)

    def _consume(self, line):
        self.seen_any = True
        self.last_activity = time.time()
        m = _RE_PROMPT.search(line)
        if m:
            self.prompt_tps = float(m.group(2))
            return
        m = _RE_GEN.search(line)
        if m:
            self.gen_tps = float(m.group(2))
            self.recent_requests.insert(
                0, [datetime.now().strftime("%H:%M:%S"), self.gen_tps, self.prompt_tps])
            del self.recent_requests[50:]
            return
        m = _RE_SLOT_START.search(line)
        if m:
            s = int(m.group(1))
            self.active_slots.add(s)
            self.slot_launched_at[s] = time.time()
            return
        m = _RE_SLOT_STOP.search(line)
        if m:
            s = int(m.group(1))
            self.active_slots.discard(s)
            self.slot_launched_at.pop(s, None)
            return
        m = _RE_DRAFT.search(line)
        if m:
            self.draft_acceptance = float(m.group(1))


_lms_tailer = LmsLogTailer()


# ---- live per-slot state from llama-server /slots (silent: no log noise)

_llama_conn_cache = None    # (port, api_key, scanned_at)


def _llama_conn():
    """Find a running llama-server process and its --port / --api-key.
    Scanned at most every 30 s; re-scans immediately after a miss."""
    global _llama_conn_cache
    now = time.time()
    if _llama_conn_cache and now - _llama_conn_cache[2] < 30:
        return _llama_conn_cache[0], _llama_conn_cache[1]
    port, key = None, None
    try:
        pids = [p for p in os.listdir("/proc") if p.isdigit()]
    except OSError:
        pids = []
    for pid in pids:
        try:
            with open("/proc/%s/cmdline" % pid, "rb") as f:
                argv = [a.decode("utf-8", "replace") for a in f.read().split(b"\0") if a]
        except OSError:
            continue
        if not argv or "llama-server" not in os.path.basename(argv[0]):
            continue
        for i, a in enumerate(argv):
            if a == "--port" and i + 1 < len(argv):
                port = argv[i + 1]
            elif a == "--api-key" and i + 1 < len(argv):
                key = argv[i + 1]
        break   # first llama-server wins (single-instance setup)
    _llama_conn_cache = (port, key, now)
    return port, key


def llama_slots():
    """Live slot state from llama-server. Returns [{id, is_processing}, ...]
    or None when unavailable (caller falls back to log-derived slots)."""
    port, key = _llama_conn()
    if not port:
        return None
    req = urllib.request.Request("http://127.0.0.1:%s/slots" % port)
    if key:
        req.add_header("Authorization", "Bearer " + key)
    try:
        with urllib.request.urlopen(req, timeout=3) as r:
            data = json.loads(r.read().decode("utf-8", "replace"))
    except (OSError, ValueError):
        return None
    if isinstance(data, dict):        # some versions wrap as {"slots": [...]}
        data = data.get("slots")
    if not isinstance(data, list):
        return None
    out = []
    for s in data:
        if isinstance(s, dict) and "id" in s:
            out.append({"id": int(s["id"]),
                        "is_processing": bool(s.get("is_processing"))})
    return out or None


def lmstudio_status():
    _lms_tailer.read_new()
    instances = lmstudio_instances()
    slots_live = llama_slots()
    if slots_live is not None:
        # authoritative: llama-server's own view of its slots
        active_slots = sorted(s["id"] for s in slots_live if s["is_processing"])
    else:
        # fallback: log-derived slots, with stale cleanup
        if instances and all(i["status"] == "idle" for i in instances):
            for s in list(_lms_tailer.active_slots):
                if _lms_tailer.slot_launched_at.get(s, 0) < _lms_cache_ts:
                    _lms_tailer.active_slots.discard(s)
                    _lms_tailer.slot_launched_at.pop(s, None)
        active_slots = sorted(_lms_tailer.active_slots)
    t = _lms_tailer
    return {
        "available": bool(instances) or t.seen_any,
        "instances": instances,
        "gen_tps": t.gen_tps,
        "prompt_tps": t.prompt_tps,
        "draft_acceptance": t.draft_acceptance,
        "active_slots": sorted(t.active_slots),
        "last_activity_ago": (round(time.time() - t.last_activity)
                              if t.last_activity is not None else None),
        "recent_requests": t.recent_requests[:10],
        "ctl": lms_ctl_snapshot(),
        "heal": dict(_lms_heal),   # daemon self-heal state (null when idle)
    }


# ---- LM Studio load/unload control (the two dashboard buttons)
#
# Both buttons are configurable so the same image runs on any box: set the
# LLMON_MODEL_A_* / LLMON_MODEL_B_* environment variables (see .env.example in
# the Providence-LMS repo). The defaults are the models this dashboard was
# originally built for, so an unconfigured install behaves exactly as before.
LMS_LOAD_MODEL_KEY = _env(
    "LLMON_MODEL_A_KEY",
    "qwen3.8-27b-turbo-fable-cold-fusion-735-882-heretic-uncensored-neo-coder-max-mtp")
LMS_LOAD_IDENTIFIER = _env("LLMON_MODEL_A_ID", "Uncensored-262K")
LMS_LOAD_CTX = _env("LLMON_MODEL_A_CTX", "262144")             # str: goes straight into argv
LMS_LOAD_ARGS = ["load", LMS_LOAD_MODEL_KEY, "--gpu", "max",
                 "--context-length", LMS_LOAD_CTX,
                 "--identifier", LMS_LOAD_IDENTIFIER, "--yes"]

# Second button. It unloads everything first (unconditionally) so a model that
# needs all available VRAM never loads alongside another instance.
LMS_LOAD_FN_MODEL_KEY = _env("LLMON_MODEL_B_KEY", "qwen3.8-flash-next-gsq-rco-abliterated")
LMS_LOAD_FN_IDENTIFIER = _env("LLMON_MODEL_B_ID", "Qwen-Abliterated")
LMS_LOAD_FN_CTX = _env("LLMON_MODEL_B_CTX", "262144")
LMS_LOAD_FN_ARGS = ["load", LMS_LOAD_FN_MODEL_KEY, "--gpu", "max",
                    "--context-length", LMS_LOAD_FN_CTX,
                    "--identifier", LMS_LOAD_FN_IDENTIFIER, "--yes"]

# Unload All = MODELS ONLY on this box's LM Studio daemon (2026-09-13):
# `lms unload --all` + an orphaned-backend sweep. The daemon itself is NOT
# restarted anymore — the old daemon down/up cycle respawned the whole LM
# Studio service for every other user of it, and left follow-up Load/Restart
# ops in a confused state (stale instances, orphaned backends holding VRAM).
UNLOAD_STEPS = [("lms unload --all", ["unload", "--all"], 300)]

_ctl_lock = threading.Lock()
_ctl = {"op": None, "state": None, "step": None,
        "started": None, "finished": None, "message": None}


def lms_ctl_snapshot():
    with _ctl_lock:
        return dict(_ctl)


def lms_ctl_set(**kw):
    with _ctl_lock:
        _ctl.update(kw)


def _lms_daemon_running():
    """True when the lms daemon (llmster) reports 'is running'."""
    try:
        r = subprocess.run([LMS_BIN, "daemon", "status"], capture_output=True,
                           text=True, timeout=8)
        return r.returncode == 0 and "is running" in (r.stdout or "")
    except (OSError, subprocess.SubprocessError):
        return False


def _llmster_pids():
    """PIDs of the LM Studio daemon matched by EXACT process identity:
    /proc/<pid>/comm == 'llmster' (the daemon binary itself). A loose
    `pgrep -f llmster` also matches the node helper and any command line that
    merely CONTAINS "llmster"; procps additionally treats a bare numeric arg
    as a NAME, not a PID — both observed on this box. Returns [] when no
    daemon is found, None when /proc itself was unreadable."""
    try:
        names = os.listdir("/proc")
    except OSError:
        return None
    out = []
    for n in names:
        if not n.isdigit():
            continue
        pid = int(n)
        try:
            with open("/proc/%d/comm" % pid) as f:
                if f.read().strip() != "llmster":
                    continue
            os.kill(pid, 0)   # also verifies we can signal it (EPERM -> skip)
        except OSError:
            continue
        out.append(pid)
    return sorted(out)


def _lmstudio_backend_pids():
    """PIDs of LM Studio's model backend(s): comm 'llama-server' whose cmdline
    points into the .lmstudio tree. A SIGKILLed daemon orphans these and they
    keep holding VRAM until killed."""
    out = []
    try:
        names = os.listdir("/proc")
    except OSError:
        return []
    for n in names:
        if not n.isdigit():
            continue
        pid = int(n)
        try:
            with open("/proc/%s/comm" % n) as f:
                if f.read().strip() != "llama-server":
                    continue
            with open("/proc/%s/cmdline" % n, "rb") as f:
                cmd = f.read().decode("utf-8", "replace").lower()
            if ".lmstudio" not in cmd and "/lmstudio/" not in cmd:
                continue
            os.kill(pid, 0)
        except OSError:
            continue
        out.append(pid)
    return sorted(out)


def _kill_backend_pids(grace_s=30):
    """SIGTERM LM Studio's llama-server backends, wait up to grace_s seconds,
    SIGKILL the survivors. Frees VRAM properly — used by Unload All (orphan
    sweep) and Restart LMS (after a hard-killed daemon). Returns [] when all
    backends are gone."""
    for _ in range(2):
        pids = _lmstudio_backend_pids()
        if not pids:
            return []
        for pid in pids:
            try:
                os.kill(pid, signal.SIGTERM)
            except OSError:
                pass
        deadline = time.time() + grace_s
        while time.time() < deadline:
            pids = _lmstudio_backend_pids()
            if not pids:
                return []
            time.sleep(2)
        for pid in pids:   # second round escalates to KILL
            try:
                os.kill(pid, signal.SIGKILL)
            except OSError:
                pass
    time.sleep(3)   # let a just-SIGKILLed process actually vanish before reporting
    return _lmstudio_backend_pids()


def _wait_api_up(timeout_s):
    """Poll until the daemon API answers (lms daemon status reports running)."""
    for _ in range(max(1, int(timeout_s // 2))):
        if _lms_daemon_running():
            return True
        time.sleep(2)
    return _lms_daemon_running()


# ---- daemon self-heal: a dead daemon revives without user action ---------
#
# The sampler's `lms ps` refresh calls maybe_heal_lms() when the CLI can't
# reach the daemon API. This is what replaces "user runs lms ps by hand to
# wake it up": an explicit, cooldown-gated `lms daemon up` in a bg thread.

_lms_heal = {"state": None, "msg": None, "ts": None}   # exposed as lmstudio.heal
_HEAL_COOLDOWN = 120.0    # seconds between attempts
_heal_lock = threading.Lock()
_heal_last_ts = time.time()   # grace period after (re)start: entrypoint's own
                              # autostart may still be bringing the daemon up


def maybe_heal_lms():
    """Background `lms daemon up` with a 120 s cooldown; skipped while an
    explicit control op owns the lifecycle. Never raises into the sampler."""
    global _heal_last_ts
    now = time.time()
    with _heal_lock:
        if _lms_heal["state"] == "running" or now - _heal_last_ts < _HEAL_COOLDOWN:
            return
        ctl = lms_ctl_snapshot()
        if ctl.get("op") is not None and ctl.get("state") == "running":
            return  # an explicit op (load/unload/restart) owns the daemon now
        _heal_last_ts = now
        _lms_heal.update(state="running", msg=None, ts=round(now))

    def work():
        try:
            # Re-check ownership: an explicit control op (unload/load/restart)
            # may have started while this thread was being scheduled — observed
            # 2026-09-13, where a heal fired adjacent to the Unload All cycle.
            ctl = lms_ctl_snapshot()
            if ctl.get("op") is not None and ctl.get("state") == "running":
                _lms_heal.update(state=None, msg="skipped — control op owns daemon",
                                 ts=round(time.time()))
                return
            r = subprocess.run([LMS_BIN, "daemon", "up"], capture_output=True,
                               text=True, timeout=300)
            if not _wait_api_up(90):
                err = (r.stderr or r.stdout or "").strip()
                raise RuntimeError(err[-200:] or "daemon did not answer within 90 s")
            _lms_heal.update(state="ok", ts=round(time.time()),
                             msg="daemon revived at %s"
                                 % datetime.now().strftime("%H:%M:%S"))
        except Exception as e:   # report, never crash the sampler chain
            _lms_heal.update(state="failed", ts=round(time.time()), msg=str(e)[:200])

    threading.Thread(target=work, daemon=True).start()


def lms_ctl_start(kind):
    """kind: 'load' | 'unload' | 'restart'. Runs the command sequence in a
    background thread (model loads can take minutes). Returns (started, reason)."""
    with _ctl_lock:
        if _ctl["op"] is not None and _ctl["state"] == "running":
            return False, "another operation is already running"
        _ctl.update(op=kind, state="running", step="starting",
                    started=round(time.time()), finished=None, message=None)

    def work():
        def run(args, timeout):
            r = subprocess.run([LMS_BIN] + args, capture_output=True,
                               text=True, timeout=timeout)
            if r.returncode != 0:
                err = (r.stderr or r.stdout or "command failed").strip()
                raise RuntimeError(err[-200:])
        try:
            if kind == "unload":
                for step, args, timeout in UNLOAD_STEPS:
                    lms_ctl_set(step=step)
                    run(args, timeout)
                # The daemon no longer knows about the models, but a
                # llama-server that was ORPHANED (daemon SIGKILLed — e.g. by a
                # manual `pkill -f llmster`) keeps holding ~12 GB/GPU until it
                # is killed itself. Sweep so the next Load has clean VRAM.
                lms_ctl_set(step="sweeping orphaned backends")
                leftover = _kill_backend_pids(30)
                msg = "all models unloaded · daemon left running"
                if leftover:
                    # AppArmor's docker profile blocks container->host kill(2) with
                    # EPERM (verified 2026-09-14): a host-owned orphan can't be
                    # signalled from in here, so tell the user what to do.
                    msg += " · ⚠ %d backend(s) survived — pkill -f llama-server via SSH" % len(leftover)
                lms_ctl_set(state="done", message=msg)
            elif kind == "load":
                # Idempotent load: unload everything else first, then load target.
                lms_ctl_set(step="checking existing instances")
                _bust_lms_cache()
                insts = lmstudio_instances()
                if any((i.get("identifier") or "") == LMS_LOAD_IDENTIFIER for i in insts):
                    lms_ctl_set(state="done",
                                 message=f"{LMS_LOAD_IDENTIFIER} already loaded · nothing to do")
                    return
                # Unload all other models to free VRAM before loading this one.
                if insts:
                    lms_ctl_set(step="unloading all existing models")
                    run(["unload", "--all"], 300)
                    time.sleep(5)  # let freed VRAM settle
                lms_ctl_set(step="loading model")
                run(LMS_LOAD_ARGS, 1800)
                # Verify the identifier actually took — this LM Studio build has
                # been observed reporting instances under the model key instead.
                _bust_lms_cache()
                ids = [i.get("identifier") or "?" for i in lmstudio_instances()]
                if LMS_LOAD_IDENTIFIER not in ids:
                    raise RuntimeError(
                        "model loaded but as %s, expected identifier '%s'"
                        % (", ".join(ids) if len(ids) <= 4 else "%d instances" % len(ids),
                           LMS_LOAD_IDENTIFIER))
                lms_ctl_set(state="done", message=f"{LMS_LOAD_IDENTIFIER} loaded · ctx {LMS_LOAD_CTX}")
            elif kind == "load-fn":
                # Second button: unconditional `lms unload --all` first, then load
                # LLMON_MODEL_B_KEY. It exists for models that need essentially all
                # available VRAM and must never load alongside another instance.
                lms_ctl_set(step="checking existing instances")
                _bust_lms_cache()
                insts = lmstudio_instances()
                if any((i.get("identifier") or "") == LMS_LOAD_FN_IDENTIFIER for i in insts):
                    lms_ctl_set(state="done",
                                 message=f"{LMS_LOAD_FN_IDENTIFIER} already loaded · nothing to do")
                    return
                # Unload ALL models (unconditional, even if `lms ps` reports none —
                # a stale/orphaned backend can hold VRAM the daemon has forgotten).
                lms_ctl_set(step="unloading all existing models")
                run(["unload", "--all"], 300)
                time.sleep(5)  # let freed VRAM settle
                lms_ctl_set(step="loading model")
                run(LMS_LOAD_FN_ARGS, 1800)
                _bust_lms_cache()
                ids = [i.get("identifier") or "?" for i in lmstudio_instances()]
                if LMS_LOAD_FN_IDENTIFIER not in ids:
                    raise RuntimeError(
                        "model loaded but as %s, expected identifier '%s'"
                        % (", ".join(ids) if len(ids) <= 4 else "%d instances" % len(ids),
                           LMS_LOAD_FN_IDENTIFIER))
                lms_ctl_set(state="done", message=f"{LMS_LOAD_FN_IDENTIFIER} loaded · ctx {LMS_LOAD_FN_CTX}")
            else:  # restart: full reset of the LM Studio app + daemon (v4).
                # GRACEFUL FIRST (`lms daemon down` via the CLI): it talks to the
                # daemon over its API socket, so it works no matter WHO owns the
                # process — host user vs container root. Signals can't: AppArmor's
                # docker profile blocks container->host kill(2) with EPERM (verified
                # 2026-09-14), which is why v3's TERM/KILL bounced off a host-owned
                # daemon while `lms unload`/`daemon status` (TCP) worked fine. Exact-pid
                # signal escalation (/proc comm == 'llmster', never the loose
                # `pkill -f llmster` of v2 — that also signaled node helpers, SSH
                # sessions and tailscaled) stays as fallback for an UNRESPONSIVE
                # daemon, plus the orphaned-backend sweep so VRAM is released.
                def wait_dead(timeout_s):
                    for _ in range(timeout_s):
                        if not (_llmster_pids() or []):
                            return True
                        time.sleep(1)
                    return not (_llmster_pids() or [])

                lms_ctl_set(step="finding daemon process")
                pids = _llmster_pids() or []
                leftover_backends = []
                if not pids and not _lms_daemon_running():
                    lms_ctl_set(step="daemon already down")
                else:
                    # 1) graceful in-app shutdown. Blocks until done; teardown of a
                    #    large model happens inside the GPU driver (30-60 s+).
                    lms_ctl_set(step="lms daemon down (graceful)")
                    try:
                        run(["daemon", "down"], 120)
                    except RuntimeError:
                        if _lms_daemon_running():
                            raise   # API alive but `daemon down` failed — genuine error
                        # else: the daemon's API is already gone; a lingering /proc
                        # entry (zombie / D-state corpse) is NOT "alive" — let the
                        # signal-escalation path below deal with what remains.
                    # 2) signal escalation ONLY for whatever is still alive.
                    if not wait_dead(30):
                        live = _llmster_pids() or []
                        lms_ctl_set(step="SIGTERM pid " + ", ".join(map(str, live)))
                        for pid in live:
                            try:
                                os.kill(pid, signal.SIGTERM)
                            except OSError:
                                pass
                        if not wait_dead(30):
                            lms_ctl_set(step="SIGKILL daemon + orphaned backends")
                            for pid in (_llmster_pids() or []):
                                try:
                                    os.kill(pid, signal.SIGKILL)
                                except OSError:
                                    pass
                            # a SIGKILLed daemon orphans its model backend; kill it
                            # so VRAM is actually released (TERM first, KILL after)
                            leftover_backends = _kill_backend_pids(20)
                            if not wait_dead(15):
                                # Outlived everything — observed 2026-09-14: a D-state
                                # corpse keeps its /proc entry for minutes. The API
                                # socket frees when it finally dies, which is enough to
                                # bring a fresh daemon up; wait state-aware instead of
                                # failing the whole op.
                                lms_ctl_set(step="waiting for driver release (D-state)")
                                for _ in range(60):
                                    if not (_llmster_pids() or []) \
                                            or not _lms_daemon_running():
                                        break
                                    time.sleep(2)
                lms_ctl_set(step="lms daemon up")
                run(["daemon", "up"], 300)   # explicit bring-up; raises with stderr tail on failure
                lms_ctl_set(step="waiting for daemon API")
                if not _wait_api_up(90):     # a fresh start can take a while
                    raise RuntimeError("daemon did not answer within 90 s — check LM Studio logs")
                # Model-aware result: LM Studio sometimes keeps/restores the
                # loaded model across a daemon restart — report the real state.
                time.sleep(3)
                _bust_lms_cache()
                insts = lmstudio_instances()
                if insts:
                    msg = "lms restarted · " + ", ".join((i["identifier"] or "model") for i in insts) + " still loaded"
                else:
                    msg = "lms restarted · model unloaded — click Load Uncensored"
                # No-op detection (2026-09-14): if the daemon we tried to kill is
                # somehow still around, `daemon up` was a no-op against it — say so.
                survivors = sorted(set(_llmster_pids() or []) & set(pids))
                if pids and survivors:
                    msg += (" · ⚠ original pid %s survived (AppArmor/D-state?) — check dmesg"
                            % ",".join(map(str, survivors)))
                if leftover_backends:
                    msg += " · ⚠ %d backend(s) held VRAM — pkill -f llama-server via SSH" % len(leftover_backends)
                lms_ctl_set(state="done", message=msg)
        except (OSError, subprocess.SubprocessError, RuntimeError) as e:
            # NOTE: RuntimeError MUST be caught here — a failed lms command
            # raises it; missing it wedges ctl in 'running' forever and the
            # frontend buttons stay disabled (the 2026-09 stuck-controls bug).
            lms_ctl_set(state="error", message=str(e)[:200])
        finally:
            lms_ctl_set(finished=round(time.time()), step=None)

    threading.Thread(target=work, daemon=True).start()
    return True, "started"



def snapshot():
    s = {
        "ts": time.time(),
        "time": datetime.now().strftime("%H:%M:%S"),
        "hostname": HOSTNAME,
        "cpu": {"temp_c": cpu_temp(), "usage_pct": cpu_usage(),
                "cores": os.cpu_count(), **cpu_ghz()},
        "mem": meminfo(),
        "load": load_uptime(),
        "disk": disk(),
        "nvme_temps": nvme_temps(),
        "sys_power_w": sys_power_w(),
        "gpus": gpus(),
        "lmstudio": lmstudio_status(),
    }
    return s

# ---------------------------------------------------------------- history

_history = []
_hist_lock = threading.Lock()

def sampler():
    while True:
        try:
            s = snapshot()
        except Exception:
            s = None
        if s:
            with _hist_lock:
                _history.append(s)
                if len(_history) > MAX_SAMPLES:
                    del _history[:len(_history) - MAX_SAMPLES]
        time.sleep(SAMPLE_INTERVAL)

def history_json():
    with _hist_lock:
        items = list(_history)
    # downsample to ~300 points
    step = max(1, len(items) // 300)
    items = items[::step]
    return {
        "t": [s["time"] for s in items],
        "sys_power": [s["sys_power_w"] for s in items],
        "cpu_temp": [s["cpu"]["temp_c"] for s in items],
        "cpu_usage": [s["cpu"]["usage_pct"] for s in items],
        "cpu_ghz": [s["cpu"]["ghz"] for s in items],
        "mem_used": [s["mem"]["used_gb"] for s in items],
        "swap_used": [s["mem"]["swap_used_gb"] for s in items],
        "gpu_temp": [[g["temp_c"] for g in s["gpus"]] for s in items],
        "gpu_power": [[g["power_w"] for g in s["gpus"]] for s in items],
        "gpu_util": [[g["util_pct"] for g in s["gpus"]] for s in items],
        "gpu_mem": [[g["mem_used_mib"] for g in s["gpus"]] for s in items],
        "gen_tps": [s["lmstudio"]["gen_tps"] for s in items],
        "prompt_tps": [s["lmstudio"]["prompt_tps"] for s in items],
        "lm_slots": [len(s["lmstudio"]["active_slots"]) for s in items],
    }

# ---------------------------------------------------------------- control ops (host-side)

def do_reboot():
    """Reboot the host. Bare host: the sudo helper directly. Docker: the same
    host helper over SSH (dedicated key, BatchMode) so no sudo inside the box."""
    if REBOOT_SSH:
        cmd = ["ssh"]
        if REBOOT_KEY:
            cmd += ["-i", REBOOT_KEY]
        cmd += ["-o", "BatchMode=yes", "-o", "ConnectTimeout=8",
                "-o", "StrictHostKeyChecking=accept-new", REBOOT_SSH,
                "sudo -n /usr/local/bin/reboot_server.sh"]
        return subprocess.run(cmd, capture_output=True, text=True, timeout=30)
    return subprocess.run(["sudo", "-n", "/usr/local/bin/reboot_server.sh"],
                          capture_output=True, text=True, timeout=25)

def _self_reexec():
    """Restart the web service in place: re-exec this very script. The
    container (or systemd) keeps running; in-memory history is reset."""
    print("self-restart requested; re-execing python", flush=True)
    os.execv(sys.executable, [sys.executable, os.path.abspath(__file__)])

# ---------------------------------------------------------------- http

class Handler(BaseHTTPRequestHandler):
    server_version = "llmmon/1.0"

    def log_message(self, *a):
        pass

    def _send(self, code, body, ctype):
        data = body.encode() if isinstance(body, str) else body
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        if self.path == "/api/stats":
            try:
                cur = snapshot()
                payload = {"current": cur, "history": history_json()}
                self._send(200, json.dumps(payload), "application/json")
            except Exception as e:
                self._send(500, json.dumps({"error": str(e)}), "application/json")
        elif self.path in ("/", "/index.html"):
            self._send(200, PAGE, "text/html; charset=utf-8")
        elif self.path == "/healthz":
            self._send(200, "ok", "text/plain")
        else:
            self._send(404, "not found", "text/plain")

    def do_POST(self):
        if self.path == "/api/reboot":
            try:
                n = int(self.headers.get("Content-Length", 0))
                body = json.loads(self.rfile.read(n) or b"{}")
            except (ValueError, TypeError):
                body = {}
            if body.get("confirm") != "REBOOT":
                self._send(400, json.dumps({"error": "confirm must be 'REBOOT'"}), "application/json")
                return
            try:
                r = do_reboot()
            except (OSError, subprocess.TimeoutExpired) as e:
                self._send(500, json.dumps({"error": str(e)}), "application/json")
                return
            if r.returncode != 0:
                self._send(500, json.dumps({"error": (r.stderr or "reboot helper failed").strip()[:200]}),
                           "application/json")
                return
            self._send(200, json.dumps({"ok": True, "message": "reboot scheduled"}), "application/json")
            return
        elif self.path in ("/api/lms/load", "/api/lms/load-fn", "/api/lms/unload", "/api/lms/restart"):
            kind = self.path.rsplit("/", 1)[1]
            ok, reason = lms_ctl_start(kind)
            if ok:
                self._send(200, json.dumps({"ok": True, "op": kind}), "application/json")
            else:
                self._send(409, json.dumps({"error": reason}), "application/json")
            return
        elif self.path == "/api/self/restart":
            self._send(200, json.dumps({"ok": True, "message": "restarting in ~1 s"}),
                       "application/json")
            threading.Timer(0.5, _self_reexec).start()
            return
        elif self.path == "/api/gpu/fan":
            try:
                body = json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0))))
                gpu_idx = int(body.get("gpu", 0))
                mode = body.get("mode", "auto")
                pct = int(body.get("pct", 97))
                
                # Set fan mode and speed via nvidia-smi
                if mode == "manual":
                    r1 = subprocess.run(["nvidia-smi", "-i", str(gpu_idx), "-fan", "manual"],
                                       capture_output=True, text=True, timeout=10)
                    if r1.returncode != 0:
                        raise RuntimeError(r1.stderr.strip()[-200:])
                    r2 = subprocess.run(["nvidia-smi", "-i", str(gpu_idx), "--gpu-fan-speed", str(pct)],
                                       capture_output=True, text=True, timeout=10)
                    if r2.returncode != 0:
                        raise RuntimeError(r2.stderr.strip()[-200:])
                else:  # auto
                    r = subprocess.run(["nvidia-smi", "-i", str(gpu_idx), "-fan", "auto"],
                                      capture_output=True, text=True, timeout=10)
                    if r.returncode != 0:
                        raise RuntimeError(r.stderr.strip()[-200:])
                
                self._send(200, json.dumps({"ok": True, "message": f"GPU {gpu_idx} fan set to {mode}"}),
                           "application/json")
            except Exception as e:
                self._send(500, json.dumps({"error": str(e)}), "application/json")
            return
        self._send(404, "not found", "text/plain")

# ---------------------------------------------------------------- frontend

HTML = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>__HOSTNAME__ · monitor</title>
<script src="https://cdn.jsdelivr.net/npm/gsap@3.12.5/dist/gsap.min.js"></script>
<script src="https://cdn.jsdelivr.net/npm/gsap@3.12.5/dist/Physics2DPlugin.min.js"></script>
<link rel="stylesheet" href="https://fonts.googleapis.com/css2?family=Poppins:wght@600&display=swap">
<style>
  :root{
    --bg0:#070b14; --bg1:#0b1220; --card:rgba(255,255,255,.045);
    --stroke:rgba(255,255,255,.09); --txt:#e8eefb; --dim:#8b98b8;
    --g0:#22d3ee; --g1:#4ade80; --g2:#fbbf24; --g3:#f472b6; --cpu:#a78bfa; --pwr:#f87171;
  }
  *{box-sizing:border-box;margin:0;padding:0}
  html,body{height:100%}
  body{
    font-family:'Segoe UI',system-ui,-apple-system,sans-serif; color:var(--txt);
    background:
      radial-gradient(1200px 700px at 85% -10%, rgba(124,58,237,.22), transparent 60%),
      radial-gradient(1000px 600px at -10% 110%, rgba(14,165,233,.18), transparent 60%),
      linear-gradient(160deg, var(--bg0), var(--bg1));
    min-height:100vh; padding:22px 26px 40px;
  }
  header{display:flex;align-items:baseline;gap:16px;flex-wrap:wrap;margin-bottom:20px}
  header h1{font-size:26px;font-weight:700;letter-spacing:.4px;
    background:linear-gradient(90deg,#67e8f9,#a78bfa,#f472b6);
    -webkit-background-clip:text;background-clip:text;color:transparent}
  header .meta{color:var(--dim);font-size:13px;font-family:ui-monospace,monospace}
  .live{display:inline-flex;align-items:center;gap:7px;font-size:12px;color:var(--dim);
    border:1px solid var(--stroke);padding:4px 10px;border-radius:99px}
  .live .dot{width:8px;height:8px;border-radius:50%;background:#4ade80;
    box-shadow:0 0 8px #4ade80;animation:pulse 1.6s infinite}
  .chip{display:inline-flex;align-items:center;justify-content:center;gap:6px;
    border:1px solid var(--stroke);padding:4px 10px;border-radius:99px;
    font-family:ui-monospace,monospace;font-size:12px;color:var(--dim)}
  /* min-width:4ch equals the width of a real reading ("3.89") in this monospace font, so
     the pill never resizes as digits change yet has no dead space around the number. */
  .chip b{color:var(--cpu);font-weight:700;font-size:13px;min-width:4ch;text-align:center}
  .chip.boost b{color:#f0abfc;text-shadow:0 0 8px rgba(240,171,252,.45)}
  @keyframes pulse{50%{opacity:.35}}
  .grid{display:grid;gap:16px}
  .top{grid-template-columns:repeat(auto-fit,minmax(215px,1fr))}
  .gpus{grid-template-columns:repeat(auto-fit,minmax(250px,1fr));margin-top:16px}
  .card{background:var(--card);border:1px solid var(--stroke);border-radius:16px;
    padding:18px 20px;backdrop-filter:blur(8px);
    box-shadow:0 8px 28px rgba(0,0,0,.35);transition:transform .15s ease}
  .card:hover{transform:translateY(-2px)}
  .card .label{font-size:11px;text-transform:uppercase;letter-spacing:1.6px;color:var(--dim)}
  .big{font-size:34px;font-weight:700;font-family:ui-monospace,'Cascadia Mono',monospace;margin:6px 0 2px}
  .big small{font-size:15px;color:var(--dim);font-weight:400}
  .sub{font-size:12px;color:var(--dim);font-family:ui-monospace,monospace}
  .bar{height:8px;border-radius:99px;background:rgba(255,255,255,.08);overflow:hidden;margin-top:10px}
  .bar i{display:block;height:100%;border-radius:99px;width:0%;transition:width .6s ease}
  .gcard .head{display:flex;justify-content:space-between;align-items:center;margin-bottom:8px}
  .gcard .tag{font-family:ui-monospace,monospace;font-size:12px;font-weight:700;
    padding:3px 9px;border-radius:8px;border:1px solid}
  .gcard .row{display:flex;justify-content:space-between;font-size:13px;padding:3px 0;
    font-family:ui-monospace,monospace}
  .gcard .row b{font-weight:600}
  .gcard .row.pci{font-size:11px;color:var(--dim);padding:2px 0 4px}
  .c0{color:var(--g0)} .c1{color:var(--g1)} .c2{color:var(--g2)} .c3{color:var(--g3)} .ccpu{color:var(--cpu)}

    /* Iridescent Progress Button */
  @import url(https://fonts.bunny.net/css?family=amaranth:400,700);
  
  .iridescent-btn {
    --cubic-out: cubic-bezier(0.33, 1, 0.68, 1);
    font-size: 14px;
    font-family: "Amaranth", sans-serif;
    text-transform: none;
    color: #818e9e;
    min-width: 140px;
    height: 48px;
    line-height: 1;
    display: inline-grid;
    justify-content: center;
    align-items: center;
    position: relative;
    padding: 0.5em 1.5em;
    outline: max(2px, calc(var(--brdr) * 0.75)) solid transparent;
    outline-offset: max(2px, var(--brdr));
    border-radius: 99vw;
    overflow: hidden;
    transition: all 0.33s var(--cubic-out);
    --brdr: 0.15em;
    border: max(2px, var(--brdr)) solid transparent;
    background: 
      linear-gradient(to bottom, oklch(0.95 0.01 257), oklch(0.92 0.0175 257 / 80%) 33%, oklch(0.99 0.01 257 / 80%)) padding-box,
      linear-gradient(165deg, oklch(0.94 0.025 257 / 80%) 25%, oklch(0.99 0.01 257 / 80%)) border-box;
    --inset-shadow: inset 0 0 0 0 oklch(1 0.02 257 / 0), inset -0.35em -0.35em 0.25em -0.25em oklch(0.99 0.02 257), inset -0.33em -1em 0.75em -0.75em oklch(0.99 0.01 257);
    --outer-shadow: oklch(0.35 0.1 257 / 0.12) 0px max(4px, 0.3em) 0.3em 0px, oklch(0.35 0.1 257 / 0.12) 0px max(2px, 0.18em) 0.18em 0px, oklch(0.35 0.1 257 / 0.1) 0px max(1px, 0.05em) max(2px, 0.05em) 0px;
    box-shadow: var(--inset-shadow), var(--outer-shadow);
    margin: 6px;
    cursor: pointer;
  }
  
  .iridescent-btn:hover, .iridescent-btn.shine {
    opacity: 1;
    color: #3c5a80;
    &::before, &::after { transition-duration: 0.3s; transition-timing-function: var(--cubic-out); }
  }
  
  .iridescent-btn:hover::before, .iridescent-btn.shine::before {
    opacity: 0.6;
    translate: 0em;
    scale: 1;
  }
  
  .iridescent-btn::before, .iridescent-btn::after {
    content: "";
    position: absolute;
    inset: min(-2px, calc(var(--brdr) * -1));
    border-radius: inherit;
    pointer-events: none;
    transition: all 0.6s cubic-bezier(0.32, 0, 0.67, 0);
  }
  
  .iridescent-btn::after {
    opacity: 0.3;
    background: transparent;
    box-shadow: inset 0 -0.3em 2px 1px oklch(0.99 0.01 257), inset 0 -0.3em 0.25em oklch(0.99 0.01 257), inset 0 -0.3em 0.5em oklch(0.99 0.01 257);
    mix-blend-mode: lighten;
    z-index: 2;
  }
  
  .iridescent-btn::before {
    opacity: 0;
    translate: 1.1em 0em;
    scale: 0.8;
    background: linear-gradient(98deg in oklab, oklch(0.8 0.1348 355.91) -5%, oklch(0.8 0.2 280), oklch(0.98 0.22 200) 160%);
    mask: linear-gradient(166deg, transparent 60%, black);
    filter: blur(5px) brightness(1) contrast(1.3);
    z-index: 3;
  }
  
  .iridescent-btn:active {
    transition: all 0.1s var(--cubic-out);
    translate: 0 max(1px, 0.05em);
    border-color: oklch(0.99 0.01 257 / 70%);
  }
  
  .iridescent-btn:active::after {
    transition: all 0.1s var(--cubic-out);
    opacity: 1;
  }
  
  /* Progress fill overlay */
  .progress-fill {
    position: absolute;
    top: 0;
    left: 0;
    height: 100%;
    width: 0%;
    background: linear-gradient(90deg in oklab, oklch(0.75 0.15 355.91) -5%, oklch(0.72 0.22 280), oklch(0.85 0.24 200) 160%);
    border-radius: inherit;
    transition: width 0.05s linear;
    z-index: 1;
  }
  
  /* Text wrapper for contrast */
  .btn-text {
    position: relative;
    z-index: 2;
    text-shadow: 0 1px 3px rgba(255, 255, 255, 0.8), 0 0 8px rgba(255, 255, 255, 0.4);
  }
  
  /* Greyed out state for cards during operations */
  .card.greyed { opacity: 0.35; pointer-events: none; filter: grayscale(0.8); transition: all 0.3s ease; }


  .foot{margin-top:18px;color:var(--dim);font-size:11px;text-align:center;font-family:ui-monospace,monospace}
  .warn{color:#fca5a5 !important}
  .mid{font-size:19px;font-weight:700;margin:6px 0 4px;word-break:break-word}
  .badge{font-size:10px;font-weight:700;letter-spacing:1.2px;padding:2px 9px;border-radius:99px;border:1px solid}
  .badge.idle{color:#4ade80;border-color:rgba(74,222,128,.4);background:rgba(74,222,128,.08)}
  .badge.busy{color:#fbbf24;border-color:rgba(251,191,36,.45);background:rgba(251,191,36,.08);animation:pulse 1.6s infinite}
  .badge.off{color:#5b6883;border-color:var(--stroke)}
  .pips{display:flex;align-items:center;gap:6px;margin-top:12px}
  .pips .plabel{font-size:11px;color:var(--dim);font-family:ui-monospace,monospace;margin-right:2px}
  .pip{width:12px;height:12px;border-radius:50%;background:rgba(255,255,255,.09);border:1px solid var(--stroke);transition:all .3s}
  .pip.on{background:#fbbf24;border-color:#fbbf24;box-shadow:0 0 10px rgba(251,191,36,.55)}
  .sysctl{display:flex;flex-direction:column}
  .sysctl .btnwrap{flex:1;display:flex;flex-direction:column;align-items:center;justify-content:center;gap:10px;padding:12px 0}
  /* ---- full-screen wait overlay (reboot / service restart): grays the dashboard ---- */
  #waitoverlay{position:fixed;inset:0;z-index:9999;display:none;align-items:center;justify-content:center;
    background:rgba(4,7,14,.82);backdrop-filter:blur(3px) grayscale(.5)}
  .waitdialog{background:#0d1526;border:1px solid rgba(255,255,255,.12);border-radius:18px;
    padding:34px 46px;text-align:center;max-width:560px;box-shadow:0 20px 60px rgba(0,0,0,.6)}
  .waitspin{width:34px;height:34px;border-radius:50%;border:3px solid rgba(255,255,255,.15);
    border-top-color:#a78bfa;margin:0 auto 18px;animation:waitspin .9s linear infinite}
  @keyframes waitspin{to{transform:rotate(360deg)}}
  .waith1{font-size:18px;font-weight:700;margin-bottom:8px}
  .waitsub{font-size:13px;color:#8b98b8;font-family:ui-monospace,monospace;line-height:1.6}
  .waittimer{margin-top:14px;font-family:ui-monospace,monospace;font-size:12px;color:#5b6883}
  .waitbtns{display:flex;gap:10px;justify-content:center;margin-top:20px}
  .waitsmall{font-size:11px;font-family:ui-monospace,monospace;background:rgba(255,255,255,.06);
    border:1px solid rgba(255,255,255,.15);color:#e8eefb;padding:7px 16px;border-radius:99px;cursor:pointer}
  .waitsmall:hover{background:rgba(255,255,255,.12)}

  .card{cursor:grab;position:relative}
  .card::after{content:'⠿';position:absolute;top:5px;right:9px;font-size:11px;color:#8b98b8;
    opacity:0;transition:opacity .2s;pointer-events:none}
  .card:hover::after{opacity:.45}
  .card.dragging{opacity:.4;transform:none;box-shadow:none;cursor:grabbing;
    border-style:dashed;border-color:rgba(255,255,255,.4)}
  /* ---- "generate-button" style (sparkle pill, purple hover; ported from CodePen, 2/3 size) ---- */
  .generate-button{
    --generate-button-star-1-opacity:.25;
    --generate-button-star-1-scale:1;
    --generate-button-star-2-opacity:1;
    --generate-button-star-2-scale:1;
    --generate-button-star-3-opacity:.5;
    --generate-button-star-3-scale:1;
    --generate-button-dots-opacity:0;
    appearance:none;outline:none;border:none;
    padding:11px 16px 11px 13px;border-radius:19px;margin:0;
    background-color:#1D1D1D;color:#616161;
    width:100%;min-width:0;display:flex;justify-content:center;position:relative;cursor:pointer;
    -webkit-tap-highlight-color:transparent;z-index:1;
    transform:scale(var(--generate-button-scale,1)) translateZ(0);
    box-shadow:0 0 120px var(--generate-button-shadow-wide,transparent),
      0 4px 12px rgba(0,0,0,.05),0 1px 2px rgba(0,0,0,.1),
      inset 0 1px 1px var(--generate-button-shadow-inset,rgba(255,255,255,.04)),
      0 0 0 var(--generate-button-shadow-outline,0px) rgba(109,68,244,.4);
    transition:transform .3s,background-color .3s,box-shadow .3s,color .3s;
  }
  .generate-button:before{content:'';display:block;position:absolute;right:20%;height:13px;left:20%;bottom:-7px;
    background:#D3B3FF;filter:blur(8px);z-index:2;
    clip-path:inset(-200% -30% 7px -30% round 19px);opacity:0;transition:opacity .4s;transform:translateZ(0)}
  .generate-button span{position:relative;z-index:1;font-family:'Poppins','Segoe UI',Arial;font-weight:600;
    font-size:10.7px;line-height:17px;letter-spacing:.005em;display:block;white-space:nowrap}
  .generate-button span:before{content:'';background-image:linear-gradient(to right,transparent,#1D1D1D);
    position:absolute;inset:0;z-index:1;pointer-events:none;opacity:.4;transition:opacity .4s}
  .generate-button .stroke{mix-blend-mode:hard-light}
  .generate-button .stroke svg{position:absolute;inset:0;width:100%;height:100%;fill:none;stroke-width:.75px;
    stroke:#E2D9FF;stroke-dasharray:1.5 14;stroke-dashoffset:22;opacity:0}
  .generate-button .stroke svg:nth-child(2){stroke-width:1px;stroke-opacity:.5;filter:blur(3px)}
  .generate-button svg{display:block;overflow:visible;pointer-events:none}
  .generate-button svg.dots{position:absolute;inset:0;width:100%;height:100%;z-index:10;fill:#D3B2FF;
    opacity:var(--generate-button-dots-opacity)}
  .generate-button svg.icon{width:16px;height:17px;margin-right:8px;fill:currentColor;flex:0 0 auto}
  .generate-button svg.icon path:nth-child(1){opacity:var(--generate-button-star-1-opacity);
    transform:scale(var(--generate-button-star-1-scale)) translateZ(0);transform-origin:25% 14.58%}
  .generate-button svg.icon path:nth-child(2){opacity:var(--generate-button-star-2-opacity);
    transform:scale(var(--generate-button-star-2-scale)) translateZ(0);transform-origin:60.42% 50%}
  .generate-button svg.icon path:nth-child(3){opacity:var(--generate-button-star-3-opacity);
    transform:scale(var(--generate-button-star-3-scale)) translateZ(0);transform-origin:25% 85.42%}
  .generate-button:hover{
    --generate-button-scale:1.1;
    --generate-button-shadow-wide:rgba(208,173,255,.4);
    --generate-button-shadow-inset:rgba(255,255,255,.35);
    --generate-button-shadow-outline:3px;
    color:#fff;background-color:#6D44F4;
  }
  .generate-button:hover .stroke svg{animation:genbtn-stroke 2s linear infinite}
  .generate-button:hover:before{opacity:1}
  .generate-button:hover span:before{opacity:0}
  .generate-button:active{--generate-button-scale:1.05}
  .generate-button:disabled{opacity:.55;cursor:wait;animation:none}
  .generate-button.armed,.generate-button.armed:hover{background-color:#ef4444;color:#1a0505;
    --generate-button-shadow-wide:rgba(248,113,113,.45);
    --generate-button-shadow-outline:3px;
    animation:pulse 1s infinite}
  @keyframes genbtn-stroke{0%{opacity:0}25%,75%{opacity:1}95%,100%{stroke-dashoffset:6;opacity:0}}
  /* LM Studio control card (two stacked generate-buttons, 2/3 size) */
  .lmsctl{display:flex;flex-direction:column}
  .lmsctl .btns{flex:1;display:flex;flex-direction:column;align-items:center;justify-content:center;
    gap:10px;padding:10px 0 8px}

</style>
</head>
<body>
<header>
  <h1>⚡ __HOSTNAME__</h1>
  <span class="live"><span class="dot" id="livedot"></span><span id="livetxt">connecting…</span></span>
  <span class="chip" id="ghzchip" title="CPU frequency"><b id="cpughz">—</b>GHz</span>
  <span class="meta" id="meta"></span>
</header>

<div class="grid top">
  <div class="card" data-oid="power">
    <div class="label">System Power (CPU pkg)</div>
    <div class="big" id="syspower">—<small> W</small></div>
    <div class="sub" id="nvmetemps"></div>
    <hr style="border:none;border-top:1px solid rgba(255,255,255,.06);margin:10px 0">
    <div class="label">GPU Power Total</div>
    <div class="big" id="gtotpower">—<small> W</small></div>
    <div class="sub" id="gtotutil"></div>
    <div class="bar"><i id="gtotbar" style="background:linear-gradient(90deg,#f87171,#fbbf24)"></i></div>
  </div>
  <div class="card" data-oid="cpu">
    <div class="label">CPU · <span id="cpunames">AMD EPYC</span></div>
    <div class="big ccpu" id="cputemp">—<small> °C</small></div>
    <div class="sub" id="cpusub">—</div>
    <div class="bar"><i id="cpubar" style="background:var(--cpu)"></i></div>
    <hr style="border:none;border-top:1px solid rgba(255,255,255,.06);margin:10px 0">
    <div class="label">Token Speed · generate</div>
    <div class="big" id="genps" style="color:#4ade80">—<small> tok/s</small></div>
    <div class="sub" id="genpsub">—</div>
  </div>
  <div class="card" data-oid="mem">
    <div class="label">Memory</div>
    <div class="big" id="memused">—<small> / — GB</small></div>
    <div class="sub" id="memsub"></div>
    <div class="bar"><i id="membar" style="background:linear-gradient(90deg,#22d3ee,#4ade80)"></i></div>
    <hr style="border:none;border-top:1px solid rgba(255,255,255,.06);margin:10px 0">
    <div class="label">Disk · /home</div>
    <div class="big" id="diskused">—<small> / — TB</small></div>
    <div class="sub" id="disksub"></div>
    <div class="bar"><i id="diskbar" style="background:linear-gradient(90deg,#a78bfa,#f472b6)"></i></div>
  </div>

  <div class="card" data-oid="lms">
    <div class="label">LM Studio · Model</div>
    <div class="mid" id="lmsmodel">—</div>
    <div class="sub"><span class="badge off" id="lmsbadge">—</span></div>
    <div class="sub" id="lmsmeta">—</div>
    <div class="sub" id="lmsctx">—</div>
    <div class="pips" id="lmspips"></div>
  </div>

  <div class="card sysctl" data-oid="sysctl">
    <div class="label">System Control</div>
    <div class="btnwrap">
      <button id="rebootbtn" class="iridescent-btn" type="button">
        <div class="progress-fill"></div>
        <span class="btn-text">Reboot Server</span>
      </button>
      <button id="restartsvcbtn" class="iridescent-btn" type="button">
        <div class="progress-fill"></div>
        <span class="btn-text">Restart Dashboard</span>
      </button>
    </div>
    <div class="sub" id="rebootsub">click to confirm reboot · no undo</div>
  </div>
  <div class="card lmsctl" data-oid="lmsctl">
    <div class="label">LM Studio Control</div>
    <div class="btns">
      <button id="lmsloadbtn" class="iridescent-btn" type="button">
        <div class="progress-fill"></div>
        <span class="btn-text">__MODEL_A_ID__</span>
      </button>
      <button id="lmsloadfnbtn" class="iridescent-btn" type="button">
        <div class="progress-fill"></div>
        <span class="btn-text">__MODEL_B_ID__</span>
      </button>
    </div>
    <div class="sub" id="lmsctlsub">__MODEL_A_HINT__</div>
  </div>
</div>

<div class="grid gpus" id="gpugrid"></div>

<div id="waitoverlay">
  <div class="waitdialog">
    <div class="waitspin"></div>
    <div class="waith1" id="waith1">…</div>
    <div class="waitsub" id="waitsub">…</div>
    <div class="waittimer" id="waittimer"></div>
    <div class="waitbtns">
      <button class="waitsmall" id="waitcheck" type="button">Check now</button>
      <button class="waitsmall" id="waitdismiss" type="button">Dismiss</button>
    </div>
  </div>
</div>

<div class="foot">__HOSTNAME__ monitor · docker · stdlib http server · refresh 2s · drag cards to reorder (saved per browser)</div>

<script>
const GC = ['#22d3ee','#4ade80','#fbbf24','#f472b6'];
let gpuCount = 0;
function buildGpuCards(count){
  if (gpuCount === count) return; // already built
  gpuCount = count;
  const grid = document.getElementById('gpugrid');
  grid.innerHTML = '';
  for(let i=0; i<count; i++){
    const card = document.createElement('div');
    card.className = 'card gcard';
    card.setAttribute('data-oid', 'gpu'+i);
    card.innerHTML = `
      <div class="head"><span class="label">GPU ${i}</span><span class="tag c${i%4}" id="g${i}name">…</span></div>
      <div class="row pci" id="g${i}pci"></div>
      <div class="row"><span>Temp</span><b class="c${i%4}" id="g${i}temp">—</b></div>
      <div class="row"><span>Power</span><b id="g${i}power">—</b></div>
      <div class="bar"><i id="g${i}pbar" style="background:${GC[i%4]}"></i></div>
      <div class="row" style="margin-top:8px"><span>Util</span><b id="g${i}util">—</b></div>
      <div class="row"><span>VRAM</span><b class="c${i%4}" id="g${i}vram">—</b></div>
      <div class="bar"><i id="g${i}vbar" style="background:linear-gradient(90deg,${GC[i%4]},#fff6)"></i></div>
      <div class="row" style="margin-top:8px"><span>Fan / Clock</span><b id="g${i}fan">—</b></div>`;

    grid.appendChild(card);
  }
}
buildGpuCards(0); // start empty, will populate on first data

const $ = id => document.getElementById(id);


// ---- full-screen wait overlay: gray the dashboard + staged status while we
//      wait for (a) the server to come back after a reboot and
//      (b) LM Studio + the uncensored model to finish loading, then refresh.
const waitOverlay = (function(){
  const ov = $('waitoverlay'), h1 = $('waith1'), sub = $('waitsub'), timerEl = $('waittimer');
  let kind = null, t0 = 0, tickTimer = null, pollTimer = null, alive = false, curPoll = null;
  function fmtEl(ms){ const s = Math.floor(ms / 1000);
    return Math.floor(s / 60) + ':' + String(s % 60).padStart(2, '0'); }
  async function healthz(){ try { const r = await fetch('/healthz', {cache:'no-store'}); return r.ok; } catch(e){ return false; } }
  async function modelReady(){
    try{
      const r = await fetch('/api/stats', {cache:'no-store'}); const j = await r.json();
      const i = j.current && j.current.lmstudio && j.current.lmstudio.instances
               && j.current.lmstudio.instances[0];
      return !!(i && i.status && i.status !== 'loading');
    }catch(e){ return false; }
  }
  function setPhase(title, line){ h1.textContent = title; sub.textContent = line; }
  function stop(){
    alive = false; kind = null; curPoll = null;
    ov.style.display = 'none';
    clearInterval(tickTimer); clearTimeout(pollTimer);
  }
  function start(k){
    if (kind === k && alive && curPoll){ curPoll(); return; }   // "Check now"
    stop();
    kind = k; alive = true; t0 = Date.now();
    ov.style.display = 'flex';
    timerEl.textContent = 'waiting 0:00';
    tickTimer = setInterval(()=>{ timerEl.textContent = 'waiting ' + fmtEl(Date.now() - t0); }, 1000);
    if (k === 'reboot'){
      setPhase('Rebooting server', 'waiting for the server to come back online…');
      const p2 = async ()=>{
        if (!alive) return;
        if (await modelReady()){
          setPhase('✓ Everything is ready', 'all systems up · refreshing in a moment…');
          pollTimer = setTimeout(()=>location.reload(), 1500);
          return;
        }
        pollTimer = setTimeout(p2, 3000);
      };
      const p1 = async ()=>{
        if (!alive) return;
        if (await healthz()){
          setPhase('Server is back online', 'waiting for LM Studio + the uncensored model to finish loading…');
          curPoll = p2; p2();
        } else {
          pollTimer = setTimeout(p1, 2500);
        }
      };
      curPoll = p1; p1();
    } else {  // 'service': dashboard web service restart (no model wait)
      setPhase('Restarting dashboard service', 'waiting for the web service to come back…');
      const p1 = async ()=>{
        if (!alive) return;
        if (await healthz()){
          setPhase('✓ Service is back', 'closing in a moment…');
          pollTimer = setTimeout(stop, 800);
          return;
        }
        pollTimer = setTimeout(p1, 1000);
      };
      curPoll = p1; p1();
    }
  }
  $('waitcheck').addEventListener('click', ()=>{ if (kind) start(kind); });
  $('waitdismiss').addEventListener('click', stop);
  return { start, close: stop };
})();
// ---- Reboot Server: iridescent progress button with confirmation ----
(function(){
  const b = $('rebootbtn'), s = $('rebootsub');
  let armed = false, timer = null;
  
  function greyOutCards(exceptOid, grey){
    document.querySelectorAll('.card').forEach(card => {
      if (card.dataset.oid === exceptOid) return;
      if (grey) card.classList.add('greyed');
      else card.classList.remove('greyed');
    });
  }
  
  b.addEventListener('click', async () => {
    if (b.disabled) return;
    
    const textEl = b.querySelector('.btn-text');
    const fillEl = b.querySelector('.progress-fill');
    
    if (!armed){
      // First click - ask for confirmation
      armed = true;
      textEl.textContent = 'Click again to confirm';
      s.textContent = 'server goes down in a few seconds · no undo';
      
      timer = setTimeout(() => {
        armed = false;
        textEl.textContent = 'Reboot Server';
        s.textContent = 'click to confirm reboot · no undo';
      }, 6000);
      return;
    }
    
    // Second click - confirmed, proceed with reboot
    clearTimeout(timer);
    b.disabled = true;
    b.classList.add('shine');
    
    // Grey out other cards
    greyOutCards('sysctl', true);
    
    // Start progress animation
    let progress = 0;
    const durationMs = 110000; // ~90s boot + 20s buffer
    
    try{
      await fetch('/api/reboot', {method:'POST',
        headers:{'Content-Type':'application/json'}, body: JSON.stringify({confirm:'REBOOT'})});
    }catch(e){
      // Connection cut is expected during reboot
    }
    
    const startTime = Date.now();
    function updateProgress(){
      const elapsed = Date.now() - startTime;
      progress = Math.min(elapsed / durationMs, 1.0);
      
      if (progress < 1) {
        fillEl.style.width = `${progress * 100}%`;
        textEl.textContent = Math.round(progress * 100) + '%';
        requestAnimationFrame(updateProgress);
      } else {
        fillEl.style.width = '100%';
        textEl.innerHTML = '&#10003; Done!';
        
        setTimeout(() => {
          b.classList.remove('shine');
          b.disabled = false;
          fillEl.style.width = '0%';
          textEl.textContent = 'Reboot Server';
          s.textContent = 'click to confirm reboot · no undo';
          greyOutCards('sysctl', false);
        }, 2000);
      }
    }
    
    requestAnimationFrame(updateProgress);
  });
})();
// ---- Restart Dashboard: iridescent progress button ----
(function(){
  const b = $('restartsvcbtn'), s = $('rebootsub');
  
  function greyOutCards(exceptOid, grey){
    document.querySelectorAll('.card').forEach(card => {
      if (card.dataset.oid === exceptOid) return;
      if (grey) card.classList.add('greyed');
      else card.classList.remove('greyed');
    });
  }
  
  b.addEventListener('click', async () => {
    if (b.disabled) return;
    
    const textEl = b.querySelector('.btn-text');
    const fillEl = b.querySelector('.progress-fill');
    
    b.disabled = true;
    b.classList.add('shine');
    greyOutCards('sysctl', true);
    
    // Start progress animation
    let progress = 0;
    const durationMs = 3000; // ~2-3s for dashboard restart
    
    try{
      await fetch('/api/self/restart', {method:'POST'});
    }catch(e){
      console.error('Restart failed:', e);
    }
    
    const startTime = Date.now();
    function updateProgress(){
      const elapsed = Date.now() - startTime;
      progress = Math.min(elapsed / durationMs, 1.0);
      
      if (progress < 1) {
        fillEl.style.width = `${progress * 100}%`;
        textEl.textContent = Math.round(progress * 100) + '%';
        requestAnimationFrame(updateProgress);
      } else {
        fillEl.style.width = '100%';
        textEl.innerHTML = '&#10003; Done!';
        
        setTimeout(() => {
          b.classList.remove('shine');
          b.disabled = false;
          fillEl.style.width = '0%';
          textEl.textContent = 'Restart Dashboard';
          greyOutCards('sysctl', false);
        }, 2000);
      }
    }
    
    requestAnimationFrame(updateProgress);
  });
})();
// ---- LM Studio control buttons (load models) with progress tracking ----
(function(){
  const lb = $('lmsloadbtn'), lfnb = $('lmsloadfnbtn'), s = $('lmsctlsub');
  const IDLE = '__MODEL_A_HINT__';
  let shownOp = null, resetTimer = null, resultUntil = 0, healShownTs = null;
  let loadStartTime = null, lastBtn = null;
  
  function greyOutCards(exceptOid, grey){
    document.querySelectorAll('.card').forEach(card => {
      if (card.dataset.oid === exceptOid) return;
      if (grey) card.classList.add('greyed');
      else card.classList.remove('greyed');
    });
  }
  
  function reset(){ s.textContent = IDLE; }
  
  async function fire(url, label, btn){
    if (!btn) btn = lb;
    loadStartTime = Date.now();
    btn.classList.add('shine');
    greyOutCards('lmsctl', true);
    
    const textEl = btn.querySelector('.btn-text');
    const fillEl = btn.querySelector('.progress-fill');
    
    s.textContent = label + '… started';
    
    // Start progress animation (estimated 180s for 27B model with 262K context)
    let progress = 0;
    const durationMs = 180000;
    
    function updateProgress(){
      if (!loadStartTime) return;
      const elapsed = Date.now() - loadStartTime;
      progress = Math.min(elapsed / durationMs, 1.0);
      
      fillEl.style.width = `${progress * 100}%`;
      textEl.textContent = Math.round(progress * 100) + '%';
      
      if (progress < 1) {
        requestAnimationFrame(updateProgress);
      }
    }
    
    requestAnimationFrame(updateProgress);
    
    try{
      const r = await fetch(url, {method:'POST'});
      const j = await r.json().catch(()=>({}));
      if (!r.ok) throw new Error(j.error || ('HTTP ' + r.status));
    }catch(e){
      s.textContent = '✗ ' + String(e.message || e).slice(0, 70);
      clearTimeout(resetTimer); resetTimer = setTimeout(reset, 6000);
    }
  }
  
  lb.addEventListener('click', ()=>{ 
    if (lb.disabled) return; 
    lastBtn = lb;
    lb.disabled = true; 
    fire('/api/lms/load', 'loading __MODEL_A_ID__', lb); 
  });

  lfnb.addEventListener('click', ()=>{ 
    if (lfnb.disabled) return; 
    lastBtn = lfnb;
    lfnb.disabled = true; 
    fire('/api/lms/load-fn', 'loading __MODEL_B_ID__', lfnb); 
  });
  
  window.lmsctlPoll = function(lm){
    const ctl = lm ? (lm.ctl || null) : null;
    const heal = lm ? (lm.heal || null) : null;
    const running = !!(ctl && ctl.state === 'running');
    
    if (!running && loadStartTime) {
      // Loading completed - reset progress animation
      loadStartTime = null;
      const btn = lastBtn || lb;
      btn.classList.remove('shine');
      
      const textEl = btn.querySelector('.btn-text');
      const fillEl = btn.querySelector('.progress-fill');
      
      fillEl.style.width = '100%';
      textEl.innerHTML = '&#10003; Done!';
      
      setTimeout(() => {
        fillEl.style.width = '0%';
        textEl.textContent = (btn === lfnb) ? '__MODEL_B_ID__' : '__MODEL_A_ID__';
        btn.disabled = false;
        lastBtn = null;
        greyOutCards('lmsctl', false);
      }, 2000);
    }
    
    if (running){
      shownOp = null;
      clearTimeout(resetTimer);
      const modelLabel = (lastBtn === lfnb) ? '__MODEL_B_ID__' : '__MODEL_A_ID__';
      s.textContent = 'loading ' + modelLabel + '…' + (ctl.step ? ' · ' + ctl.step : '');
      return;
    }
    if (ctl && ctl.state && ctl.started != null && ctl.started !== shownOp){
      shownOp = ctl.started;
      resultUntil = Date.now() + 10000;   // keep the op result on screen for 10 s
      s.textContent = (ctl.state === 'done' ? '✓ ' : '✗ ') + (ctl.message || ctl.state);
      clearTimeout(resetTimer); resetTimer = setTimeout(reset, 10000);
      return;
    }
    if (Date.now() < resultUntil) return; // op result still on screen — it wins
    if (heal && heal.ts != null && heal.ts !== healShownTs){
      if (heal.state === 'running'){
        s.textContent = '⚡ daemon down — reviving in background…';
        return;
      }
      healShownTs = heal.ts;
      const txt = heal.state === 'ok' ? ('✓ ' + (heal.msg || 'daemon revived'))
                                      : ('✗ self-heal failed: ' + (heal.msg || 'no details'));
      s.textContent = txt;
      clearTimeout(resetTimer); resetTimer = setTimeout(reset, 20000);
    }
  };
})();

function fmt(v, d=1){ return v==null ? '—' : (Math.round(v*10**d)/10**d).toString(); }

async function tick(){
  try{
    const r = await fetch('/api/stats'); const j = await r.json();
    const c = j.current;
    $('livedot').style.background = '#4ade80';
    $('livetxt').textContent = 'live · ' + c.time;
    const ghz = c.cpu.ghz, ceil = c.cpu.boost_ghz;
    $('cpughz').textContent = fmt(ghz, 2);
    $('ghzchip').title = ghz == null
      ? 'no cpufreq source on this host'
      : `fastest core ${fmt(ghz,2)} GHz · avg ${fmt(c.cpu.avg_ghz,2)} · min ${fmt(c.cpu.min_ghz,2)} · ceiling ${fmt(ceil,2)} (${c.cpu.freq_source})`;
    $('ghzchip').classList.toggle('boost', ghz != null && ceil != null && ghz >= ceil - 0.05);
    $('meta').textContent = `${c.hostname} · up ${c.load.uptime} · load ${c.load.load.join(' / ')} · ${c.cpu.cores} cores`;

    $('syspower').innerHTML = fmt(c.sys_power_w) + '<small> W</small>';
    $('nvmetemps').textContent = c.nvme_temps.length ? 'NVMe ' + c.nvme_temps.map(t=>t+'°C').join(' · ') : '';

    const ct = c.cpu.temp_c;
    $('cputemp').innerHTML = fmt(ct) + '<small> °C</small>';
    $('cputemp').className = 'big ccpu' + (ct>80?' warn':'');
    $('cpusub').textContent = `${fmt(c.cpu.usage_pct)}% of ${c.cpu.cores} cores`;
    $('cpubar').style.width = (c.cpu.usage_pct||0) + '%';

    $('memused').innerHTML = fmt(c.mem.used_gb) + `<small> / ${fmt(c.mem.total_gb)} GB</small>`;
    $('memsub').textContent = `${fmt(c.mem.pct)}% used · swap ${fmt(c.mem.swap_used_gb)} / ${fmt(c.mem.swap_total_gb)} GB`;
    $('membar').style.width = (c.mem.pct||0) + '%';

    if (c.disk){
      $('diskused').innerHTML = fmt(c.disk.used_tb) + `<small> / ${fmt(c.disk.total_tb)} TB</small>`;
      $('disksub').textContent = `${fmt(c.disk.pct)}% used`;
      $('diskbar').style.width = (c.disk.pct||0) + '%';
    }

    let gtot = 0, gmax = 0, gutilSum = 0, n = 0;
    buildGpuCards(c.gpus.length);
    c.gpus.forEach((g, i)=>{
      $('g'+i+'name').textContent = g.name.replace('NVIDIA GeForce ','');
      if (g.pci_bus_id) {
        const pciEl = $('g'+i+'pci');
        pciEl.textContent = 'PCI: ' + g.pci_bus_id;
        pciEl.style.display = '';
      }
      $('g'+i+'temp').textContent = fmt(g.temp_c)+' °C';
      $('g'+i+'temp').className = 'c'+(i%4) + (g.temp_c>83?' warn':'');
      $('g'+i+'power').textContent = `${fmt(g.power_w)} W`;
      $('g'+i+'pbar').style.width = (g.power_limit_w? 100*g.power_w/g.power_limit_w:0) + '%';
      $('g'+i+'util').textContent = `${fmt(g.util_pct,0)}%`;
      $('g'+i+'vram').textContent = `${fmt(g.mem_used_mib/1024)} / ${fmt(g.mem_total_mib/1024)} GB`;
      $('g'+i+'vbar').style.width = (100*g.mem_used_mib/g.mem_total_mib) + '%';
      const fanPct = parseFloat(g.fan_pct) || 0;
      $('g'+i+'fan').textContent = `${fmt(fanPct,0)}% / ${fmt(g.clock_mhz,0)} MHz`;
      gtot += g.power_w||0; gutilSum += g.util_pct||0; if(g.power_w>gmax) gmax=g.power_w; n++;
    });
    $('gtotpower').innerHTML = fmt(gtot) + '<small> W</small>';
    $('gtotutil').textContent = n? `avg util ${fmt(gutilSum/n,0)}% · peak ${fmt(gmax)} W` : '';
    $('gtotbar').style.width = (n? 100*gmax/ (4*350) : 0) + '%';

    const lm = c.lmstudio;
    const inst = (lm && lm.instances && lm.instances.length) ? lm.instances[0] : null;
    if (inst){
      $('lmsmodel').textContent = inst.display_name || inst.model || '—';
      const busy = inst.status === 'busy' || inst.status === 'generating' || (lm.active_slots && lm.active_slots.length > 0);
      const b = $('lmsbadge');
      b.textContent = (inst.status || '—').toUpperCase();
      b.className = 'badge ' + (busy ? 'busy' : (inst.status === 'idle' ? 'idle' : 'off'));
      const meta = [];
      if (inst.quantization) meta.push(inst.quantization);
      if (inst.size_gb) meta.push(fmt(inst.size_gb) + ' GB');
      if (inst.identifier) meta.push(inst.identifier);
      if (lm.instances.length > 1) meta.push('+' + (lm.instances.length - 1) + ' more');
      $('lmsmeta').textContent = meta.join(' · ') || '—';
      $('lmsctx').textContent = inst.context
        ? 'ctx ' + inst.context.toLocaleString() + (inst.max_context && inst.max_context !== inst.context ? ' · max ' + inst.max_context.toLocaleString() : '')
        : '—';
      const on = new Set(lm.active_slots || []);
      const npar = Math.max(inst.parallel || 0, on.size);
      $('lmspips').innerHTML = '<span class="plabel">slots ' + on.size + '/' + npar + '</span>' +
        Array.from({length: npar}, (_, i) => '<span class="pip' + (on.has(i) ? ' on' : '') + '"></span>').join('');
    } else if (lm){
      $('lmsmodel').textContent = lm.available ? 'starting…' : 'no instance';
      $('lmsbadge').textContent = '—';
      $('lmsbadge').className = 'badge off';
      $('lmsmeta').textContent = '—';
      $('lmsctx').textContent = '—';
      $('lmspips').innerHTML = '';
    }
    $('genps').innerHTML = fmt(lm ? lm.gen_tps : null) + '<small> tok/s</small>';
    const tsub = [];
    if (lm){
      if (lm.prompt_tps != null) tsub.push('prompt ' + fmt(lm.prompt_tps) + ' tok/s');
      if (lm.draft_acceptance != null) tsub.push('draft ' + fmt(lm.draft_acceptance * 100, 1) + '%');
      if (lm.last_activity_ago != null) tsub.push(lm.last_activity_ago + 's ago');
      const rr = lm.recent_requests && lm.recent_requests[0];
      if (rr) tsub.push('last ' + rr[0]);
    }
    $('genpsub').textContent = tsub.join('  ·  ') || 'waiting for first request…';
    if (window.lmsctlPoll) window.lmsctlPoll(lm);

  }catch(e){
    $('livedot').style.background = '#f87171';
    $('livetxt').textContent = 'reconnecting…';
  }
}
// ---- drag cards to reorder (per grid, persisted in localStorage) ----
(function(){
  const LS_KEY = 'llmmon.cardOrder.v1';
  let saved = {};
  try { saved = JSON.parse(localStorage.getItem(LS_KEY) || '{}') || {}; } catch(e) { saved = {}; }
  const keyOf = g => g.classList.contains('top') ? 'top' : (g.classList.contains('charts') ? 'charts' : 'gpus');
  document.querySelectorAll('.grid').forEach(grid => {
    const key = keyOf(grid);
    const order = saved[key];
    if (Array.isArray(order)){
      const els = Array.from(grid.children).filter(el => el.dataset && el.dataset.oid);
      order.forEach(oid => { const el = els.find(e => e.dataset.oid === oid); if (el) grid.appendChild(el); });
      els.forEach(el => { if (!order.includes(el.dataset.oid)) grid.appendChild(el); }); // newer cards go last
    }
    grid.querySelectorAll(':scope > .card').forEach(card => card.setAttribute('draggable', 'true'));
    let dragEl = null;
    grid.addEventListener('dragstart', e => {
      const card = e.target.closest ? e.target.closest('.card') : null;
      if (!card || card.parentNode !== grid) return;
      dragEl = card;
      e.dataTransfer.effectAllowed = 'move';
      try { e.dataTransfer.setData('text/plain', card.dataset.oid || ''); } catch(_e) {}
      requestAnimationFrame(() => card.classList.add('dragging'));
    });
    grid.addEventListener('dragover', e => {
      if (!dragEl) return;
      e.preventDefault();
      e.dataTransfer.dropEffect = 'move';
      let best = null, bestD = Infinity;
      grid.querySelectorAll(':scope > .card:not(.dragging)').forEach(el => {
        const r = el.getBoundingClientRect();
        const dx = e.clientX - (r.left + r.width/2);
        const dy = e.clientY - (r.top + r.height/2);
        const d = dx*dx + dy*dy;
        if (d < bestD) { bestD = d; best = { el, dx }; }
      });
      if (!best) return;
      if (best.dx < 0) grid.insertBefore(dragEl, best.el);           // pointer left of neighbor → before it
      else if (best.el.nextElementSibling) grid.insertBefore(dragEl, best.el.nextElementSibling); // else after it
      else grid.appendChild(dragEl);
    });
    grid.addEventListener('drop', e => e.preventDefault());
    grid.addEventListener('dragend', () => {
      if (!dragEl) return;
      dragEl.classList.remove('dragging');
      dragEl = null;
      saved[key] = Array.from(grid.querySelectorAll(':scope > .card')).map(el => el.dataset.oid).filter(Boolean);
      try { localStorage.setItem(LS_KEY, JSON.stringify(saved)); } catch(e) {}
    });
  });
})();

// ---- generate-button hover fx (GSAP particles/stars; CSS hover still works if CDN blocked) ----
(function(){
  if (typeof gsap === 'undefined' || !gsap.plugins || !gsap.plugins.physics2d) return;
  const DOT_AMOUNT = 40;
  const createSVG = (width, height, className, childType, childAttributes) => {
    const svg = document.createElementNS("http://www.w3.org/2000/svg", "svg");
    svg.classList.add(className);
    const child = document.createElementNS("http://www.w3.org/2000/svg", childType);
    svg.setAttributeNS("http://www.w3.org/2000/svg", "viewBox", `0 0 ${width} ${height}`);
    for (const attr in childAttributes) { child.setAttribute(attr, childAttributes[attr]); }
    svg.appendChild(child);
    return { svg, child };
  };
  document.querySelectorAll(".generate-button").forEach((button) => {
    const width = button.offsetWidth;
    const height = button.offsetHeight;
    const style = getComputedStyle(button);
    const { svg, child: circle } = createSVG(width, height, "dots", "circle", { cx: "0", cy: "0", r: "0" });
    const strokeGroup = document.createElement("div");
    strokeGroup.classList.add("stroke");
    const { svg: stroke } = createSVG(width, height, "stroke-line", "rect", {
      x: "0", y: "0", width: "100%", height: "100%",
      rx: parseInt(style.borderRadius, 10), ry: parseInt(style.borderRadius, 10), pathLength: "10",
    });
    button.appendChild(svg);
    strokeGroup.appendChild(stroke);
    strokeGroup.appendChild(stroke.cloneNode(true));
    button.appendChild(strokeGroup);
    const timeline = gsap.timeline({ paused: true });
    for (var i = 0; i < DOT_AMOUNT; i++) {
      var p = circle.cloneNode(true);
      svg.appendChild(p);
      gsap.set(p, { attr: { cx: gsap.utils.random(width * 0.25, width * 0.75),
                            cy: gsap.utils.random(height * 0.5, height * 0.5), r: 0 } });
      var durationRandom = gsap.utils.random(10, 12);
      var tl = gsap.timeline();
      tl.to(p, {
        duration: durationRandom,
        rotation: i % 2 === 0 ? 200 : -200,
        attr: { r: gsap.utils.random(0.75, 1.5), cy: -width * gsap.utils.random(1.25, 1.75) },
        physics2D: { angle: -90, gravity: gsap.utils.random(-4, -8), velocity: gsap.utils.random(10, 25) },
      }, "-=" + durationRandom / 2)
      .to(p, { duration: durationRandom / 3, attr: { r: 0 } }, "-=" + durationRandom / 4);
      timeline.add(tl, i / 3);
    }
    svg.removeChild(circle);
    const finalTimeline = gsap.to(timeline, { duration: 10, repeat: -1, time: timeline.duration(), paused: true });
    const stars = gsap.to(button, {
      repeat: -1, repeatDelay: 0.75, paused: true,
      keyframes: [
        { "--generate-button-star-2-scale": ".5", "--generate-button-star-2-opacity": ".25",
          "--generate-button-star-3-scale": "1.25", "--generate-button-star-3-opacity": "1", duration: 0.3 },
        { "--generate-button-star-1-scale": "1.5", "--generate-button-star-1-opacity": ".5",
          "--generate-button-star-2-scale": ".5", "--generate-button-star-3-scale": "1",
          "--generate-button-star-3-opacity": ".5", duration: 0.3 },
        { "--generate-button-star-1-scale": "1", "--generate-button-star-1-opacity": ".25",
          "--generate-button-star-2-scale": "1.15", "--generate-button-star-2-opacity": "1", duration: 0.3 },
        { "--generate-button-star-2-scale": "1", duration: 0.35 },
      ],
    });
    button.addEventListener("pointerenter", () => {
      gsap.to(button, { "--generate-button-dots-opacity": ".5", duration: 0.25,
        onStart: () => { finalTimeline.restart().play(); setTimeout(() => stars.restart().play(), 500); } });
    });
    button.addEventListener("pointerleave", () => {
      gsap.to(button, { "--generate-button-dots-opacity": "0",
        "--generate-button-star-1-opacity": ".25", "--generate-button-star-1-scale": "1",
        "--generate-button-star-2-opacity": "1", "--generate-button-star-2-scale": "1",
        "--generate-button-star-3-opacity": ".5", "--generate-button-star-3-scale": "1",
        duration: 0.15,
        onComplete: () => { finalTimeline.pause(); stars.pause(); } });
    });
  });
})();

tick(); setInterval(tick, 2000);
</script>
</body>
</html>
"""

# Title/header/footer text, button labels and the hint line are all filled from
# configuration, so editing .env and restarting the container is all it takes to
# retarget a new machine — nothing box-specific is baked into this file.
PAGE = (HTML.replace("__HOSTNAME__", HOSTNAME)
            .replace("__MODEL_A_ID__", LMS_LOAD_IDENTIFIER)
            .replace("__MODEL_B_ID__", LMS_LOAD_FN_IDENTIFIER)
            .replace("__MODEL_A_HINT__", "load %s · ctx %s" % (LMS_LOAD_MODEL_KEY, LMS_LOAD_CTX)))


# ---------------------------------------------------------------- main

if __name__ == "__main__":
    t = threading.Thread(target=sampler, daemon=True)
    t.start()
    srv = ThreadingHTTPServer((HOST, PORT), Handler)
    print(f"monitor listening on http://{HOST}:{PORT}", flush=True)
    srv.serve_forever()
