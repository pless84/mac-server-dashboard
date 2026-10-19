#!/usr/bin/env python3
"""Read-only status dashboard for a Mac home server: local LLMs, scheduled jobs and GitHub Actions runners.

Discovers runners in /Users/*/actions-runner*/ (including ones running under a
dedicated account as a LaunchDaemon), reads their launchd service state,
processes and logs, and serves an auto-refreshing page plus /api/status JSON.
Stdlib only (works with the system python3 3.9).
"""
import glob
import json
import os
import re
import plistlib
import shutil
import subprocess
import threading
import time
import urllib.request
from collections import deque
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

HOME = os.path.expanduser("~")
PORT = int(os.environ.get("MAC_DASH_PORT", "8089"))
BIND = os.environ.get("MAC_DASH_BIND", "0.0.0.0")
HISTORY_LIMIT = 25

LOG_LINE = re.compile(r"^(\d{4}-\d\d-\d\d \d\d:\d\d:\d\dZ): (.*)$")
RUNNING = re.compile(r"^Running job: (.+)$")
COMPLETED = re.compile(r"^Job (.+) completed with result: (\w+)$")
VERSION = re.compile(r"Current runner version: '([^']+)'")


def sh(*args, timeout=5):
    try:
        return subprocess.run(args, capture_output=True, text=True, timeout=timeout).stdout
    except Exception:
        return ""


def read_json(path):
    try:
        with open(path, encoding="utf-8-sig") as f:
            return json.load(f)
    except Exception:
        return {}


def read_text(path):
    try:
        with open(path, encoding="utf-8-sig", errors="replace") as f:
            return f.read().strip()
    except Exception:
        return ""


def tail_lines(path, max_bytes=256 * 1024):
    try:
        with open(path, "rb") as f:
            f.seek(0, os.SEEK_END)
            size = f.tell()
            f.seek(max(0, size - max_bytes))
            return f.read().decode("utf-8", "replace").splitlines()
    except Exception:
        return []


def parse_ts(s):
    return datetime.strptime(s, "%Y-%m-%d %H:%M:%SZ").replace(tzinfo=timezone.utc).timestamp()


def processes():
    """Return list of (pid, elapsed_seconds, command, user)."""
    out = []
    for line in sh("ps", "-axo", "pid=,etime=,user=,command=").splitlines():
        parts = line.split(None, 3)
        if len(parts) < 4:
            continue
        out.append((int(parts[0]), etime_seconds(parts[1]), parts[3], parts[2]))
    return out


def etime_seconds(et):
    days = 0
    if "-" in et:
        d, et = et.split("-", 1)
        days = int(d)
    nums = [int(x) for x in et.split(":")]
    while len(nums) < 3:
        nums.insert(0, 0)
    h, m, s = nums
    return days * 86400 + h * 3600 + m * 60 + s


def launchd_state(service):
    if not service:
        return {}
    # Runners installed with svc.sh are LaunchAgents; dedicated-account ones are LaunchDaemons.
    for domain in ("gui/%d" % os.getuid(), "system"):
        out = sh("launchctl", "print", "%s/%s" % (domain, service))
        if out:
            break
    else:
        return {"loaded": False}
    info = {"loaded": True, "domain": domain.split("/")[0]}
    for key in ("state", "pid", "last exit code", "runs", "stdout path"):
        m = re.search(r"^\t%s = (.+)$" % re.escape(key), out, re.M)
        if m:
            info[key.replace(" ", "_")] = m.group(1).strip()
    return info


def parse_stdout(path):
    """Extract version, last listening time and job history from the service stdout log."""
    version, listening, history, current = None, None, [], None
    for line in tail_lines(path):
        m = VERSION.search(line)
        if m:
            version = m.group(1)
        m = LOG_LINE.match(line)
        if not m:
            continue
        ts, msg = parse_ts(m.group(1)), m.group(2)
        if msg == "Listening for Jobs":
            listening = ts
        elif RUNNING.match(msg):
            current = {"name": RUNNING.match(msg).group(1), "started": ts}
        elif COMPLETED.match(msg):
            name, result = COMPLETED.match(msg).groups()
            job = {"name": name, "result": result, "finished": ts, "started": None}
            if current and current["name"] == name:
                job["started"] = current["started"]
            history.append(job)
            current = None
    return version, listening, history[-HISTORY_LIMIT:][::-1], current


def worker_job_details(diag_dir):
    """Pull workflow/repo/run info out of the newest Worker log."""
    logs = sorted(glob.glob(os.path.join(diag_dir, "Worker_*.log")))
    if not logs:
        return {}
    try:
        with open(logs[-1], "rb") as f:
            text = f.read(1024 * 1024).decode("utf-8", "replace")
    except Exception:
        return {}
    details = {}
    for key in ("workflow", "repository", "run_id", "run_number", "ref", "event_name", "actor"):
        m = re.search(r'"k": "%s",\s*"v": "([^"]*)"' % key, text)
        if m:
            details[key] = m.group(1)
    m = re.search(r'"jobDisplayName": "([^"]*)"', text)
    if m:
        details["job"] = m.group(1)
    return details


def recent_problems(diag_dir, limit=5):
    logs = sorted(glob.glob(os.path.join(diag_dir, "Runner_*.log")))
    if not logs:
        return []
    found = []
    for line in tail_lines(logs[-1], max_bytes=128 * 1024):
        m = re.match(r"^\[(\S+ \S+) (WARN|ERR) +(\w+)\] (.*)$", line)
        if m and not m.group(4).startswith("#"):
            found.append({"time": parse_ts(m.group(1)), "level": m.group(2), "msg": m.group(4)[:240]})
    return found[-limit:][::-1]


_du_cache = {}


def dir_size_mb(path, ttl=300):
    hit = _du_cache.get(path)
    if hit and time.time() - hit[0] < ttl:
        return hit[1]
    out = sh("du", "-sk", path, timeout=20)
    size = round(int(out.split()[0]) / 1024, 1) if out else None
    _du_cache[path] = (time.time(), size)
    return size


def runner_status(root, procs):
    cfg = read_json(os.path.join(root, ".runner"))
    # .service holds the LaunchAgent plist path; the launchd label is its basename.
    service = os.path.basename(read_text(os.path.join(root, ".service")))
    if service.endswith(".plist"):
        service = service[:-len(".plist")]
    if not service and cfg.get("gitHubUrl"):
        # No svc.sh install (hand-made LaunchDaemon): use svc.sh's label scheme.
        owner_repo = cfg["gitHubUrl"].replace("https://github.com/", "").replace("/", "-")
        service = "actions.runner.%s.%s" % (owner_repo, cfg.get("agentName", ""))
    svc = launchd_state(service)
    listener = next((p for p in procs if p[2].startswith(os.path.join(root, "bin", "Runner.Listener"))), None)
    worker = next((p for p in procs if p[2].startswith(os.path.join(root, "bin", "Runner.Worker"))), None)

    home = os.path.dirname(root)
    stdout = svc.get("stdout_path") or os.path.join(home, "Library", "Logs", service, "stdout.log")
    version, listening, history, current = parse_stdout(stdout)
    diag = os.path.join(root, "_diag")

    if not listener:
        status = "offline"
    elif worker:
        status = "busy"
    else:
        status = "idle"

    job = None
    if worker:
        job = worker_job_details(diag)
        job["started"] = current["started"] if current else time.time() - worker[1]

    repo_url = cfg.get("gitHubUrl", "")
    if not repo_url.startswith("https://"):  # only ever used as a link target
        repo_url = ""
    return {
        "dir": root,
        "name": cfg.get("agentName", os.path.basename(root)),
        "repo_url": repo_url,
        "repo": repo_url.replace("https://github.com/", ""),
        "pool": cfg.get("poolName"),
        "service": service,
        "user": listener[3] if listener else None,
        "launchd": svc,
        "status": status,
        "listener_pid": listener[0] if listener else None,
        "uptime": listener[1] if listener else None,
        "version": version,
        "listening_since": listening,
        "job": job,
        "history": history,
        "problems": recent_problems(diag),
        "work_mb": dir_size_mb(os.path.join(root, "_work")),
    }


def host_status():
    disk = shutil.disk_usage(HOME)
    boot = re.search(r"sec = (\d+)", sh("sysctl", "-n", "kern.boottime"))
    return {
        "hostname": os.uname().nodename,
        "os": sh("sw_vers", "-productVersion").strip(),
        "load": [round(x, 2) for x in os.getloadavg()],
        "cpus": os.cpu_count(),
        "uptime": time.time() - int(boot.group(1)) if boot else None,
        "disk_free_gb": round(disk.free / 1e9, 1),
        "disk_total_gb": round(disk.total / 1e9, 1),
    }


# ---------------------------------------------------------------- local LLMs

OLLAMA_URL = os.environ.get("OLLAMA_URL", "http://127.0.0.1:11434")
OLLAMA_LOG = os.path.join(HOME, ".ollama", "logs", "server.log")
SAMPLE_SECS = 2
SAMPLES = deque(maxlen=180)  # ~6 minutes of GPU/memory history
REQUEST_LIMIT = 8
# Polling noise (incl. this dashboard's own calls) that shouldn't show as requests.
QUIET_PATHS = ("/api/ps", "/api/version", "/api/tags", "/v1/models")

GIN_LINE = re.compile(r'^\[GIN\] (\d{4}/\d\d/\d\d - \d\d:\d\d:\d\d) \| (\d+) \| +(\S+) \| +(\S+) \| (\w+) +"([^"]+)"')
MLX_REQ = re.compile(r'^(\S+) - - \[(\d\d/\w{3}/\d{4} \d\d:\d\d:\d\d)\] "(\w+) (\S+) [^"]*" (\d+)')
MLX_TS = re.compile(r"^(\d{4}-\d\d-\d\d \d\d:\d\d:\d\d),\d+ - \w+ - (.*)$")
GO_DUR = re.compile(r"([\d.]+)(h|ms|µs|us|ns|m|s)")
GO_UNITS = {"h": 3600, "m": 60, "s": 1, "ms": 1e-3, "µs": 1e-6, "us": 1e-6, "ns": 1e-9}
SIZE_UNITS = {"B": 1, "KB": 1e3, "MB": 1e6, "GB": 1e9, "TB": 1e12}


def http_json(url, timeout=1.5):
    try:
        with urllib.request.urlopen(url, timeout=timeout) as r:
            return json.loads(r.read().decode())
    except Exception:
        return None


def go_duration(s):
    total = sum(float(n) * GO_UNITS[u] for n, u in GO_DUR.findall(s))
    return round(total, 3)


def gpu_stats():
    """GPU utilisation and memory from IOKit; no sudo needed."""
    out = sh("ioreg", "-r", "-d", "1", "-c", "IOAccelerator")

    def num(key):
        m = re.search(r'"%s"=(\d+)' % re.escape(key), out)
        return int(m.group(1)) if m else None

    model = re.search(r'"model" = "([^"]+)"', out)
    cores = re.search(r'"gpu-core-count" = (\d+)', out)
    alloc = num("Alloc system memory")
    return {
        "model": model.group(1) if model else None,
        "cores": int(cores.group(1)) if cores else None,
        "util": num("Device Utilization %"),
        "alloc_gb": round(alloc / 1e9, 1) if alloc is not None else None,
    }


def memory_stats():
    total = sh("sysctl", "-n", "hw.memsize").strip()
    free = re.search(r"free percentage: (\d+)%", sh("memory_pressure"))
    swap = re.search(r"total = ([\d.]+)M\s+used = ([\d.]+)M", sh("sysctl", "-n", "vm.swapusage"))
    return {
        "total_gb": round(int(total) / 2**30) if total.isdigit() else None,
        "free_pct": int(free.group(1)) if free else None,
        "swap_total_gb": round(float(swap.group(1)) / 1024, 1) if swap else None,
        "swap_used_gb": round(float(swap.group(2)) / 1024, 1) if swap else None,
    }


def sampler():
    """Background thread: keep a short GPU/memory history for the sparkline."""
    while True:
        try:
            g, m = gpu_stats(), memory_stats()
            SAMPLES.append([round(time.time()), g["util"], g["alloc_gb"], m["free_pct"]])
        except Exception:
            pass
        time.sleep(SAMPLE_SECS)


def footprint_gb(pid):
    """Physical footprint incl. GPU (IOAccelerator) memory, which ps RSS leaves out."""
    m = re.search(r"Footprint: ([\d.]+) (\w+)", sh("footprint", "-p", str(pid)))
    if not m or m.group(2) not in SIZE_UNITS:
        return None
    return round(float(m.group(1)) * SIZE_UNITS[m.group(2)] / 1e9, 1)


def ollama_requests(limit=REQUEST_LIMIT):
    found = []
    for line in tail_lines(OLLAMA_LOG, max_bytes=192 * 1024):
        m = GIN_LINE.match(line)
        if not m or m.group(5) == "OPTIONS" or m.group(6).split("?")[0] in QUIET_PATHS:
            continue
        ts = time.mktime(time.strptime(m.group(1), "%Y/%m/%d - %H:%M:%S"))
        found.append({"time": ts, "status": int(m.group(2)), "secs": go_duration(m.group(3)),
                      "client": m.group(4), "method": m.group(5), "path": m.group(6)})
    return found[-limit:][::-1]


_ollama_version = [0, None]


def ollama_status(procs):
    if time.time() - _ollama_version[0] > 300 or not _ollama_version[1]:
        _ollama_version[:] = [time.time(), (http_json(OLLAMA_URL + "/api/version") or {}).get("version")]
    ps = http_json(OLLAMA_URL + "/api/ps")
    if ps is None:
        return {"up": False, "url": OLLAMA_URL, "requests": ollama_requests()}
    models = []
    for m in ps.get("models", []):
        details = m.get("details") or {}
        size, vram = m.get("size") or 0, m.get("size_vram") or 0
        models.append({
            "name": m.get("name"),
            "size_gb": round(size / 1e9, 1),
            "gpu_pct": round(100 * vram / size) if size else None,
            "params": details.get("parameter_size"),
            "quant": details.get("quantization_level"),
            "context": m.get("context_length"),
            "expires": m.get("expires_at"),
        })
    pids = [p[0] for p in procs if "ollama" in p[2].lower() and " runner" in p[2]]
    return {
        "up": True,
        "url": OLLAMA_URL,
        "version": _ollama_version[1],
        "models": models,
        "runner_gb": round(sum(footprint_gb(pid) or 0 for pid in pids), 1) if pids else None,
        "requests": ollama_requests(),
    }


def arg_value(args, flag, default=None):
    return args[args.index(flag) + 1] if flag in args and args.index(flag) + 1 < len(args) else default


def mlx_agents():
    """LaunchAgents whose program is mlx_lm.server."""
    agents = []
    for path in sorted(glob.glob(os.path.join(HOME, "Library", "LaunchAgents", "*.plist"))):
        try:
            with open(path, "rb") as f:
                pl = plistlib.load(f)
        except Exception:
            continue
        args = pl.get("ProgramArguments") or []
        if any(a.endswith("mlx_lm.server") for a in args):
            agents.append({"label": pl.get("Label"), "args": args,
                           "log": pl.get("StandardErrorPath") or pl.get("StandardOutPath")})
    return agents


def mlx_log(path, limit=REQUEST_LIMIT):
    requests, cache, last_prompt = [], None, None
    for line in tail_lines(path, max_bytes=128 * 1024):
        m = MLX_REQ.match(line)
        if m:
            if m.group(3) == "OPTIONS" or m.group(4).split("?")[0] in QUIET_PATHS:
                continue
            ts = time.mktime(time.strptime(m.group(2), "%d/%b/%Y %H:%M:%S"))
            requests.append({"time": ts, "client": m.group(1), "method": m.group(3),
                             "path": m.group(4), "status": int(m.group(5))})
            continue
        m = MLX_TS.match(line)
        if not m:
            continue
        msg = m.group(2)
        c = re.match(r"Prompt Cache: (\d+) sequences, ([\d.]+) GB", msg)
        if c:
            cache = {"sequences": int(c.group(1)), "gb": float(c.group(2))}
        p = re.match(r"Prompt processing progress: (\d+)/(\d+)", msg)
        if p and p.group(1) == p.group(2):
            ts = time.mktime(time.strptime(m.group(1), "%Y-%m-%d %H:%M:%S"))
            last_prompt = {"time": ts, "tokens": int(p.group(2))}
    return requests[-limit:][::-1], cache, last_prompt


def mlx_status(procs):
    servers = []
    for a in mlx_agents():
        args, svc = a["args"], launchd_state(a["label"])
        pid = int(svc["pid"]) if svc.get("pid", "").isdigit() else None
        proc = next((p for p in procs if p[0] == pid), None)
        port = arg_value(args, "--port", "8080")
        up = http_json("http://127.0.0.1:%s/v1/models" % port) is not None
        requests, cache, last_prompt = mlx_log(a["log"]) if a["log"] else ([], None, None)
        servers.append({
            "label": a["label"],
            "model": arg_value(args, "--model"),
            "port": port,
            "launchd": svc,
            "pid": pid,
            "up": up,
            "uptime": proc[1] if proc else None,
            "footprint_gb": footprint_gb(pid) if pid else None,
            "prompt_cache": cache,
            "last_prompt": last_prompt,
            "requests": requests,
        })
    return servers


def llm_status(procs):
    out = {"gpu": gpu_stats(), "memory": memory_stats(), "samples": list(SAMPLES)}
    for key, fn in (("ollama", ollama_status), ("mlx", mlx_status)):
        try:
            out[key] = fn(procs)
        except Exception as e:  # never let LLM probing break the runner view
            out[key] = {"error": str(e)}
    return out


# ---------------------------------------------------------------- scheduled jobs

# A job counts as having missed its run when nothing has been logged this long after it was due.
MISSED_GRACE_SECS = 30 * 60
JOB_LOG_LINES = 5
# The page is unauthenticated on the LAN, so job details are trimmed: the command is shown by name only
# (its arguments could hold secrets) and anything in a log line that looks like a secret is masked.
SECRET_ASSIGN = re.compile(
    r"""(?i)\b([\w-]{0,40}(?:password|passwd|pass|secret|token|api[_-]?key|auth)[\w-]{0,40}["']?\s{0,4}[=:]\s{0,4}["']?)[^\s"']+""")
SECRET_SCHEME = re.compile(r"(?i)\b(bearer|basic|token)\s+[^\s\"']+")
SECRET_URL = re.compile(r"(\w{1,20}://[^\s:/@]{0,100}:)[^\s@]{1,200}@")
LONG_TOKEN = re.compile(r"[A-Za-z0-9_\-+/]{32,}={0,2}")
SCRIPT_PATH = re.compile(r"^[\w./~-]+\.(sh|bash|zsh|py|rb|pl|js|mjs|php)$")
MAX_LOG_LINE = 240
LOG_TS = re.compile(r"^(\d{4}-\d\d-\d\d[ T]\d\d:\d\d:\d\d)\s+(.*)$")


def calendar_entries(spec):
    """StartCalendarInterval as a list of dicts (launchd accepts one dict or an array)."""
    if isinstance(spec, dict):
        return [spec]
    return [e for e in spec or [] if isinstance(e, dict)]


def calendar_match_day(entry, day):
    """Whether a calendar entry can fire on this date (launchd: Weekday 0 and 7 are Sunday)."""
    if "Month" in entry and entry["Month"] != day.month:
        return False
    if "Day" in entry and entry["Day"] != day.day:
        return False
    if "Weekday" in entry and entry["Weekday"] % 7 != (day.weekday() + 1) % 7:
        return False
    return True


def calendar_time(entries, now, forward=True, max_days=4 * 366):
    """The next (or previous) time any entry fires, as a timestamp in local time, or None."""
    base = datetime.fromtimestamp(now).replace(second=0, microsecond=0)
    for offset in range(max_days + 1):
        day = datetime.fromordinal(base.toordinal() + (offset if forward else -offset))
        times = []
        for e in entries:
            if not calendar_match_day(e, day):
                continue
            hours = [e["Hour"]] if "Hour" in e else range(24)
            minutes = [e["Minute"]] if "Minute" in e else range(60)
            times += [day.replace(hour=h, minute=m).timestamp() for h in hours for m in minutes]
        times = [t for t in times if (t > now if forward else t <= now)]
        if times:
            return min(times) if forward else max(times)
    return None


def describe_calendar(entries):
    """A short human description, e.g. "daily 03:30" or "Mon 09:00"."""
    days = ["Sun", "Mon", "Tue", "Wed", "Thu", "Fri", "Sat"]
    parts = []
    for e in entries:
        clock = "%02d:%02d" % (e.get("Hour", 0), e.get("Minute", 0)) if "Hour" in e else ":%02d" % e.get("Minute", 0)
        if "Hour" not in e:
            what = "hourly at " + clock if "Minute" in e else "every minute"
        elif "Weekday" in e:
            what = "%s %s" % (days[e["Weekday"] % 7], clock)
        elif "Day" in e:
            what = "day %d %s" % (e["Day"], clock)
        else:
            what = "daily " + clock
        if "Month" in e:
            what += " (month %d)" % e["Month"]
        parts.append(what)
    return ", ".join(parts)


def redact(text):
    # Bounded work per line, before any pattern runs. Masking shortens text, so drop the fragment
    # cut at the limit: half a secret is too short to be recognised and could slide into view.
    if len(text) > MAX_LOG_LINE * 2:
        parts = text[:MAX_LOG_LINE * 2].rsplit(None, 1)
        text = (parts[0] if len(parts) > 1 else "") + " …"  # one long word: nothing safe to keep
    text = SECRET_SCHEME.sub(r"\1 ***", text)
    text = SECRET_ASSIGN.sub(r"\1***", text)
    text = SECRET_URL.sub(r"\1***@", text)
    return LONG_TOKEN.sub("***", text)


def program_name(pl):
    """The job's program by name, e.g. "bash backup.sh", without the rest of its arguments."""
    args = pl.get("ProgramArguments") or [pl.get("Program", "")]
    names = [os.path.basename(str(args[0]))]
    # The second argument only when it's plainly a script path, never an option or a value.
    if len(args) > 1 and SCRIPT_PATH.match(str(args[1])):
        names.append(os.path.basename(str(args[1])))
    return redact(" ".join(names))


def job_log(path, limit=JOB_LOG_LINES):
    """The last lines of a job's log, newest first, with their time when the line starts with one.

    "key" orders lines across a job's logs: a line without a time (e.g. an error on stderr) gets
    the time of the last timed line before it in the same file, or else the file's mtime, so a
    fresh traceback doesn't sort below old timestamped lines."""
    lines = []
    last_ts = None
    file_time = mtime(path) or 0
    for line in tail_lines(path, max_bytes=32 * 1024):
        if not line.strip():
            continue
        m = LOG_TS.match(line)
        ts = None
        if m:
            try:
                ts = time.mktime(time.strptime(m.group(1).replace("T", " "), "%Y-%m-%d %H:%M:%S"))
            except ValueError:
                ts = None
        if ts:
            last_ts = ts
        lines.append({"time": ts, "key": ts or last_ts or file_time, "text": redact(m.group(2) if ts else line)[:MAX_LOG_LINE]})
    return lines[-limit:][::-1]


def mtime(path):
    try:
        return os.path.getmtime(path)
    except (OSError, TypeError):
        return None


def scheduled_agents():
    """This user's LaunchAgents that run on a schedule (StartCalendarInterval or StartInterval)."""
    agents = []
    for path in sorted(glob.glob(os.path.join(HOME, "Library", "LaunchAgents", "*.plist"))):
        try:
            with open(path, "rb") as f:
                pl = plistlib.load(f)
        except Exception:
            continue
        if not isinstance(pl, dict) or not pl.get("Label") or not (pl.get("StartCalendarInterval") or pl.get("StartInterval")):
            continue
        agents.append((path, pl))
    return agents


def job_status(path, pl, now):
    label = pl["Label"]
    svc = launchd_state(label)
    logs = [p for p in dict.fromkeys([pl.get("StandardOutPath"), pl.get("StandardErrorPath")]) if p]
    last_run = max([t for t in (mtime(p) for p in logs) if t] or [None]) if logs else None
    installed = mtime(path) or 0

    entries = calendar_entries(pl.get("StartCalendarInterval"))
    interval = pl.get("StartInterval")
    if entries:
        schedule = describe_calendar(entries)
        next_run, due = calendar_time(entries, now), calendar_time(entries, now, forward=False)
    else:
        if interval % 3600 == 0:
            schedule = "every %dh" % (interval // 3600)
        elif interval % 60 == 0:
            schedule = "every %dm" % (interval // 60)
        else:
            schedule = "every %ds" % interval
        base = last_run or installed
        next_run = base + interval if base else None
        # launchd doesn't catch up missed intervals, and a quiet log says nothing about whether
        # it ran, so interval jobs are never "missed".
        due = None

    code = re.match(r"^-?\d+", svc.get("last_exit_code", ""))
    exit_code = int(code.group(0)) if code else None
    if not svc.get("loaded"):
        status = "not loaded"
    elif svc.get("state") == "running":
        status = "running"
    elif exit_code not in (None, 0):
        status = "failed"
    elif logs and due and due > installed and now - due > MISSED_GRACE_SECS and (last_run or 0) < due:
        status = "missed"
    elif exit_code == 0 or last_run:
        status = "ok"
    else:
        status = "waiting"

    lines = []
    for p in logs:
        lines += job_log(p)
    lines.sort(key=lambda l: l.pop("key"), reverse=True)  # stable: same-key lines stay newest first
    return {
        "label": label,
        "name": label.split(".")[-1],
        "plist": path,
        "program": program_name(pl),
        "schedule": schedule,
        "launchd": svc,
        "status": status,
        "exit_code": exit_code,
        "last_run": last_run,
        "next_run": next_run,
        "due": due,
        "log_path": logs[0] if logs else None,
        "log": lines[:JOB_LOG_LINES],
    }


_jobs_cache = [0, None]
JOBS_TTL = 10  # seconds; each job costs a launchctl call


def jobs_status():
    if _jobs_cache[1] is not None and time.time() - _jobs_cache[0] < JOBS_TTL:
        return _jobs_cache[1]
    now = time.time()
    out = []
    for path, pl in scheduled_agents():
        try:
            out.append(job_status(path, pl, now))
        except Exception as e:  # one odd plist shouldn't hide the others
            out.append({"label": pl.get("Label"), "name": pl.get("Label"), "status": "error", "error": redact(str(e)), "log": []})
    _jobs_cache[:] = [time.time(), out]
    return out


def collect():
    procs = processes()
    dirs = set(glob.glob(os.path.join(HOME, "actions-runner*")) + glob.glob("/Users/*/actions-runner*"))
    roots = sorted(d for d in dirs if os.path.isfile(os.path.join(d, ".runner")))
    try:
        jobs = jobs_status()
    except Exception as e:
        jobs = [{"label": "jobs", "name": "jobs", "status": "error", "error": str(e), "log": []}]
    return {"now": time.time(), "host": host_status(), "runners": [runner_status(r, procs) for r in roots],
            "llm": llm_status(procs), "jobs": jobs}


PAGE = r"""<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Mac Dashboard</title>
<style>
:root{--bg:#f6f7f9;--card:#fff;--fg:#1b1f24;--muted:#656d76;--line:#d8dee4;
--ok:#1a7f37;--busy:#bf8700;--bad:#cf222e;--chip:#eef1f4;--link:#0969da}
@media (prefers-color-scheme:dark){:root{--bg:#0d1117;--card:#161b22;--fg:#e6edf3;--muted:#8d96a0;
--line:#30363d;--ok:#3fb950;--busy:#d29922;--bad:#f85149;--chip:#21262d;--link:#4493f8}}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--fg);font:14px/1.45 -apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif}
main{max-width:1100px;margin:0 auto;padding:20px 16px 40px}
header{display:flex;flex-wrap:wrap;gap:8px 20px;align-items:baseline;margin-bottom:16px}
h1{font-size:20px;margin:0}
.meta{color:var(--muted);font-size:13px;display:flex;flex-wrap:wrap;gap:4px 16px}
.grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(min(100%,460px),1fr));gap:16px}
.card{background:var(--card);border:1px solid var(--line);border-radius:10px;padding:16px;min-width:0}
.top{display:flex;justify-content:space-between;gap:12px;align-items:flex-start}
.title{font-weight:600;font-size:16px;word-break:break-word}
a{color:var(--link);text-decoration:none}a:hover{text-decoration:underline}
.badge{display:inline-flex;align-items:center;gap:6px;padding:3px 10px;border-radius:999px;font-weight:600;
font-size:12px;text-transform:uppercase;letter-spacing:.04em;background:var(--chip);white-space:nowrap}
.dot{width:8px;height:8px;border-radius:50%;background:currentColor}
.idle{color:var(--ok)}.busy{color:var(--busy)}.offline{color:var(--bad)}
.busy .dot{animation:pulse 1.2s infinite}@keyframes pulse{50%{opacity:.25}}
dl{display:grid;grid-template-columns:max-content 1fr;gap:4px 14px;margin:12px 0 0;font-size:13px}
dt{color:var(--muted)}dd{margin:0;word-break:break-all}
.job{margin-top:12px;padding:10px 12px;border-radius:8px;background:var(--chip);border-left:3px solid var(--busy)}
h3{font-size:12px;text-transform:uppercase;letter-spacing:.05em;color:var(--muted);margin:16px 0 6px}
table{width:100%;border-collapse:collapse;font-size:13px}
td{padding:5px 0;border-top:1px solid var(--line)}td:last-child{text-align:right;color:var(--muted);white-space:nowrap}
.res{font-weight:600}.Succeeded{color:var(--ok)}.Failed{color:var(--bad)}.Canceled,.Cancelled{color:var(--muted)}
.problems{font-size:12px;color:var(--muted);max-height:120px;overflow:auto}
.problems div{padding:3px 0;border-top:1px solid var(--line);word-break:break-word}
.ERR{color:var(--bad);font-weight:600}.WARN{color:var(--busy);font-weight:600}
.empty{color:var(--muted);font-size:13px}
#err{color:var(--bad);margin-bottom:12px}
h2{font-size:13px;text-transform:uppercase;letter-spacing:.06em;color:var(--muted);margin:24px 0 10px}
.big{font-size:28px;font-weight:600;font-variant-numeric:tabular-nums}
.stats{display:flex;flex-wrap:wrap;gap:8px 24px;margin-top:8px}
.stats div{min-width:90px}.stats small{display:block;color:var(--muted);font-size:12px}
.spark{width:100%;height:56px;margin-top:10px;display:block;background:var(--chip);border-radius:6px}
.spark polyline{fill:none;stroke-width:1.5;vector-effect:non-scaling-stroke}
.spark .u{stroke:var(--busy)}.spark .m{stroke:var(--link);stroke-dasharray:3 2}
.legend{font-size:12px;color:var(--muted);margin-top:4px}
.legend b{display:inline-block;width:10px;height:2px;vertical-align:middle;margin-right:4px}
.warn{color:var(--busy);font-weight:600}.bad{color:var(--bad);font-weight:600}
.up{color:var(--ok)}.down{color:var(--bad)}
code{font:12px ui-monospace,Menlo,monospace;word-break:break-all}
</style></head><body><main>
<header><h1>Mac Dashboard</h1><div class="meta" id="host"></div></header>
<div id="err"></div>
<h2>Local LLMs</h2><div class="grid" id="llm"></div>
<h2 id="jobs-h" hidden>Scheduled jobs</h2><div class="grid" id="jobs"></div>
<h2>Runners</h2><div class="grid" id="runners"></div>
</main><script>
const $=s=>document.querySelector(s);
const esc=s=>String(s??"").replace(/[&<>"]/g,c=>({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;"}[c]));
function dur(s){if(s==null)return"–";s=Math.max(0,Math.round(s));const d=Math.floor(s/86400),h=Math.floor(s%86400/3600),m=Math.floor(s%3600/60);
return d?`${d}d ${h}h`:h?`${h}h ${m}m`:m?`${m}m ${s%60}s`:`${s}s`}
const ago=(t,now)=>t?dur(now-t)+" ago":"–";
const when=t=>t?new Date(t*1000).toLocaleString():"–";
function card(r,now){
  const j=r.job,runUrl=j&&j.run_id?`${r.repo_url}/actions/runs/${j.run_id}`:null;
  const hist=r.history.length?`<table>${r.history.map(h=>`<tr><td>${esc(h.name)} <span class="res ${esc(h.result)}">${esc(h.result)}</span>
    ${h.started?`<span class="empty">· ${dur(h.finished-h.started)}</span>`:""}</td><td title="${esc(when(h.finished))}">${ago(h.finished,now)}</td></tr>`).join("")}</table>`
    :`<div class="empty">No jobs in the log yet.</div>`;
  const probs=r.problems.length?`<h3>Recent warnings</h3><div class="problems">${r.problems.map(p=>`<div><span class="${p.level}">${p.level}</span> ${esc(ago(p.time,now))} · ${esc(p.msg)}</div>`).join("")}</div>`:"";
  return `<section class="card"><div class="top"><div><div class="title">${esc(r.repo||r.name)}</div>
    <div class="empty">${esc(r.name)} · ${esc(r.dir)}</div></div>
    <span class="badge ${r.status}"><span class="dot"></span>${r.status}</span></div>
    ${j?`<div class="job"><b>${esc(j.job||"job")}</b> in ${esc(j.workflow||"workflow")} — running ${dur(now-j.started)}<br>
      <span class="empty">${esc(j.ref||"")} ${j.event_name?"· "+esc(j.event_name):""} ${j.actor?"· "+esc(j.actor):""}</span>
      ${runUrl?` · <a href="${esc(runUrl)}" target="_blank" rel="noopener">run #${esc(j.run_number||j.run_id)}</a>`:""}</div>`:""}
    <dl><dt>Repo</dt><dd><a href="${esc(r.repo_url)}/actions" target="_blank" rel="noopener">${esc(r.repo_url)}</a></dd>
    <dt>User</dt><dd>${esc(r.user||"–")}</dd>
    <dt>Service</dt><dd>${esc(r.launchd.state||(r.launchd.loaded===false?"not loaded":"–"))}${r.listener_pid?` · pid ${r.listener_pid}`:""}${r.launchd.last_exit_code?` · last exit ${esc(r.launchd.last_exit_code)}`:""}</dd>
    <dt>Uptime</dt><dd>${dur(r.uptime)}</dd>
    <dt>Listening</dt><dd>${ago(r.listening_since,now)}</dd>
    <dt>Version</dt><dd>${esc(r.version||"–")}</dd>
    <dt>_work size</dt><dd>${r.work_mb==null?"–":r.work_mb+" MB"}</dd></dl>
    <h3>Recent jobs</h3>${hist}${probs}</section>`;
}
function spark(samples){
  if(samples.length<2)return `<div class="empty" style="margin-top:10px">Collecting history…</div>`;
  const t0=samples[0][0],span=Math.max(1,samples[samples.length-1][0]-t0);
  const line=(i,max)=>samples.filter(s=>s[i]!=null).map(s=>`${((s[0]-t0)/span*100).toFixed(2)},${(40-Math.min(1,s[i]/max)*38).toFixed(2)}`).join(" ");
  return `<svg class="spark" viewBox="0 0 100 40" preserveAspectRatio="none">
    <polyline class="u" points="${line(1,100)}"/><polyline class="m" points="${line(4,100)}"/></svg>
    <div class="legend"><b style="background:var(--busy)"></b>GPU busy % &nbsp; <b style="background:var(--link)"></b>RAM used % · last ${dur(span)}</div>`;
}
function reqTable(reqs,now,withSecs){
  if(!reqs||!reqs.length)return `<div class="empty">No requests in the log yet.</div>`;
  return `<table>${reqs.map(r=>`<tr><td><span class="res ${r.status<400?"Succeeded":"Failed"}">${r.status}</span>
    <code>${esc(r.method)} ${esc(r.path)}</code> <span class="empty">· ${esc(r.client)}${withSecs&&r.secs!=null?" · "+dur(r.secs):""}</span></td>
    <td title="${esc(when(r.time))}">${ago(r.time,now)}</td></tr>`).join("")}</table>`;
}
function hwCard(l){
  const g=l.gpu,m=l.memory,used=m.free_pct==null?null:100-m.free_pct;
  const swapPct=m.swap_total_gb?m.swap_used_gb/m.swap_total_gb*100:0;
  const samples=l.samples.map(s=>[s[0],s[1],s[2],s[3],s[3]==null?null:100-s[3]]);
  return `<section class="card"><div class="top"><div><div class="title">GPU &amp; memory</div>
    <div class="empty">${esc(g.model||"GPU")}${g.cores?` · ${g.cores} GPU cores`:""}${m.total_gb?` · ${m.total_gb} GB unified`:""}</div></div></div>
    <div class="stats"><div><span class="big">${g.util??"–"}%</span><small>GPU busy now</small></div>
    <div><span class="big">${g.alloc_gb??"–"}</span><small>GB allocated to GPU</small></div>
    <div><span class="big ${used>90?"bad":used>75?"warn":""}">${used??"–"}%</span><small>RAM used</small></div>
    <div><span class="big ${swapPct>80?"warn":""}">${m.swap_used_gb??"–"}</span><small>GB swap of ${m.swap_total_gb??"–"}</small></div></div>
    ${spark(samples)}</section>`;
}
function ollamaCard(o,now){
  if(o.error)return `<section class="card"><div class="title">Ollama</div><div class="bad">${esc(o.error)}</div></section>`;
  const st=o.up?"up":"down";
  const models=o.up&&o.models.length?`<table>${o.models.map(x=>`<tr><td><b>${esc(x.name)}</b>
      <span class="empty">· ${x.size_gb} GB${x.params?" · "+esc(x.params):""}${x.quant?" · "+esc(x.quant):""}${x.context?" · ctx "+x.context:""}</span>
      ${x.gpu_pct!=null&&x.gpu_pct<100?` <span class="warn">${x.gpu_pct}% GPU</span>`:""}</td>
      <td>${x.expires?"unloads in "+dur(Date.parse(x.expires)/1000-now):""}</td></tr>`).join("")}</table>`
    :`<div class="empty">${o.up?"No model loaded (idle).":"Ollama is not responding at "+esc(o.url)+"."}</div>`;
  return `<section class="card"><div class="top"><div><div class="title">Ollama</div>
    <div class="empty">${esc(o.url)}${o.version?" · v"+esc(o.version):""}${o.runner_gb?` · runner ${o.runner_gb} GB`:""}</div></div>
    <span class="badge ${o.up?"idle":"offline"}"><span class="dot"></span>${st}</span></div>
    <h3>Loaded models</h3>${models}<h3>Recent requests</h3>${reqTable(o.requests,now,true)}</section>`;
}
function mlxCard(s,now){
  const lp=s.last_prompt,pc=s.prompt_cache;
  return `<section class="card"><div class="top"><div><div class="title">MLX · ${esc((s.model||"").split("/").pop())}</div>
    <div class="empty">${esc(s.label)} · :${esc(s.port)}</div></div>
    <span class="badge ${s.up?"idle":"offline"}"><span class="dot"></span>${s.up?"up":"down"}</span></div>
    <dl><dt>Model</dt><dd>${esc(s.model||"–")}</dd>
    <dt>Service</dt><dd>${esc(s.launchd.state||(s.launchd.loaded===false?"not loaded":"–"))}${s.pid?` · pid ${s.pid}`:""}${s.launchd.last_exit_code&&s.launchd.last_exit_code!=="0"?` · last exit ${esc(s.launchd.last_exit_code)}`:""}</dd>
    <dt>Uptime</dt><dd>${dur(s.uptime)}</dd>
    <dt>Memory</dt><dd>${s.footprint_gb!=null?s.footprint_gb+" GB":"–"}</dd>
    <dt>Prompt cache</dt><dd>${pc?`${pc.gb} GB · ${pc.sequences} sequences`:"–"}</dd>
    <dt>Last prompt</dt><dd>${lp?`${lp.tokens} tokens · ${ago(lp.time,now)}`:"–"}</dd></dl>
    <h3>Recent requests</h3>${reqTable(s.requests,now,false)}</section>`;
}
const JOB_CLASS={ok:"idle",running:"busy",waiting:"",failed:"offline",missed:"offline","not loaded":"offline",error:"offline"};
function jobCard(j,now){
  const until=t=>t?(t>now?"in "+dur(t-now):ago(t,now)):"–";
  const log=j.log&&j.log.length?`<table>${j.log.map(l=>`<tr><td><code>${esc(l.text)}</code></td>
    <td ${l.time?`title="${esc(when(l.time))}"`:""}>${l.time?ago(l.time,now):""}</td></tr>`).join("")}</table>`
    :`<div class="empty">Nothing logged yet.</div>`;
  const svc=j.launchd||{};
  return `<section class="card"><div class="top"><div><div class="title">${esc(j.name)}</div>
    <div class="empty">${esc(j.label)}${j.schedule?" · "+esc(j.schedule):""}</div></div>
    <span class="badge ${JOB_CLASS[j.status]??""}"><span class="dot"></span>${esc(j.status)}</span></div>
    ${j.error?`<div class="bad">${esc(j.error)}</div>`:""}
    <dl><dt>Last run</dt><dd title="${esc(when(j.last_run))}">${ago(j.last_run,now)}</dd>
    <dt>Next run</dt><dd title="${esc(when(j.next_run))}">${until(j.next_run)}</dd>
    <dt>Last exit</dt><dd class="${j.exit_code?"bad":""}">${j.exit_code==null?"–":j.exit_code}${svc.runs?` · ${esc(svc.runs)} runs since load`:""}</dd>
    <dt>Program</dt><dd><code>${esc(j.program||"–")}</code></dd>
    <dt>Log</dt><dd><code>${esc(j.log_path||"–")}</code></dd></dl>
    <h3>Recent log</h3>${log}</section>`;
}
function llmCards(l,now){
  if(!l)return "";
  const mlx=Array.isArray(l.mlx)?l.mlx.map(s=>mlxCard(s,now)).join("")
    :l.mlx&&l.mlx.error?`<section class="card"><div class="title">MLX</div><div class="bad">${esc(l.mlx.error)}</div></section>`:"";
  return hwCard(l)+ollamaCard(l.ollama||{},now)+mlx;
}
async function refresh(){
  try{
    const d=await (await fetch("api/status",{cache:"no-store"})).json(),h=d.host,now=d.now;
    $("#host").innerHTML=`<span>${esc(h.hostname)}</span><span>macOS ${esc(h.os)}</span><span>load ${h.load.join(" / ")} (${h.cpus} cpu)</span>
      <span>up ${dur(h.uptime)}</span><span>disk ${h.disk_free_gb} / ${h.disk_total_gb} GB free</span><span>updated ${new Date().toLocaleTimeString()}</span>`;
    $("#llm").innerHTML=llmCards(d.llm,now);
    const jobs=d.jobs||[];
    $("#jobs-h").hidden=!jobs.length;
    $("#jobs").innerHTML=jobs.map(j=>jobCard(j,now)).join("");
    $("#runners").innerHTML=d.runners.length?d.runners.map(r=>card(r,now)).join(""):`<div class="empty">No runners found in /Users/*/actions-runner*</div>`;
    const busy=d.runners.filter(r=>r.status==="busy").length,off=d.runners.filter(r=>r.status==="offline").length;
    const badJobs=jobs.filter(j=>JOB_CLASS[j.status]==="offline").length;
    document.title=(off?`⚠ ${off} offline · `:badJobs?`⚠ ${badJobs} job${badJobs>1?"s":""} failing · `:busy?`${busy} busy · `:"")+"Mac Dashboard";
    $("#err").textContent="";
  }catch(e){$("#err").textContent="Can't reach the dashboard server: "+e}
}
refresh();setInterval(refresh,5000);
</script></body></html>"""


class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        path = self.path.split("?")[0]
        if path == "/api/status":
            body, ctype = json.dumps(collect()).encode(), "application/json"
        elif path in ("/", "/index.html"):
            body, ctype = PAGE.encode(), "text/html; charset=utf-8"
        else:
            self.send_error(404)
            return
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):
        pass


if __name__ == "__main__":
    threading.Thread(target=sampler, daemon=True).start()
    print("Mac server dashboard on http://%s:%d" % (BIND, PORT), flush=True)
    ThreadingHTTPServer((BIND, PORT), Handler).serve_forever()
