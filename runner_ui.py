#!/usr/bin/env python3
import os, sys, time, json, threading, subprocess, webbrowser, getpass, shlex, uuid
from datetime import datetime
from http.server import HTTPServer, BaseHTTPRequestHandler
from urllib.parse import urlparse, parse_qs
from concurrent.futures import ThreadPoolExecutor, as_completed
from collections import deque

try:
    import yaml
except ImportError:
    print("ERROR: PyYAML not found. Install with: pip install pyyaml")
    sys.exit(1)

APP_DIR = os.path.dirname(os.path.abspath(__file__))
CONFIG_FILE = os.path.join(APP_DIR, "targets.yaml")
if not os.path.exists(CONFIG_FILE):
    print(f"ERROR: targets.yaml not found in {APP_DIR}")
    sys.exit(1)

with open(CONFIG_FILE, "r", encoding="utf-8") as f:
    CONFIG = yaml.safe_load(f) or {}

SETTINGS = CONFIG.get("settings", {}) or {}
SERVERS  = CONFIG.get("servers", {}) or {}
APPS     = CONFIG.get("apps", []) or []

HTTP_OK_CODES = set(SETTINGS.get("http_ok_codes", [200, 302, 401, 403]) or [200, 302, 401, 403])
CURL_TIMEOUT_SECONDS = int(SETTINGS.get("curl_timeout_seconds", 1) or 1)
SSH_CONNECT_TIMEOUT_SECONDS = int(SETTINGS.get("ssh_connect_timeout_seconds", 8) or 8)

HISTORY_FILE = SETTINGS.get("history_file", "history.jsonl") or "history.jsonl"
HISTORY_PATH = os.path.join(APP_DIR, HISTORY_FILE)

CONSOLE_HOST = SETTINGS.get("console_host", "127.0.0.1")
CONSOLE_PORT = int(SETTINGS.get("console_port", 5000))

SSH_MAX_PARALLEL = int(SETTINGS.get("ssh_max_parallel", 1) or 1)
REFRESH_DELAY_SECONDS = float(SETTINGS.get("refresh_delay_seconds", 1) or 1)
ACTION_MAX_PARALLEL = int(SETTINGS.get("action_max_parallel", 1) or 1)

RUN_AS = (SETTINGS.get("run_as", "su") or "su").strip().lower()
ACTION_FORCE_TTY = bool(SETTINGS.get("action_force_tty", True))
ACTION_TIMEOUT_SECONDS = int(SETTINGS.get("action_timeout_seconds", 90) or 90)

INSTANCE_OUTPUT_MAX = int(SETTINGS.get("instance_output_max_entries", 60) or 60)
LOG_MAX_ENTRIES = int(SETTINGS.get("log_max_entries", 1000) or 1000)

LOCK = threading.Lock()

GROUPS = set()
INSTANCES = []
STATUS = {}
LAST_MESSAGE = {}
OUTPUT_HISTORY = {}   # key -> deque of entries
JOBS = {}             # job_id -> state
LOG = deque(maxlen=LOG_MAX_ENTRIES)

def now_ts():
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")

def inst_key(group, server, name):
    return f"{group}|{server}|{name}"

def html_escape(s: str) -> str:
    s = s or ""
    return (s.replace("&", "&amp;")
             .replace("<", "&lt;")
             .replace(">", "&gt;")
             .replace('"', "&quot;")
             .replace("'", "&#39;"))

def log_event(level, msg, **fields):
    entry = {"ts": now_ts(), "level": level, "msg": msg}
    if fields: entry.update(fields)
    with LOCK:
        LOG.append(entry)

def append_history(entry: dict):
    try:
        entry = dict(entry)
        entry.setdefault("ts", now_ts())
        with open(HISTORY_PATH, "a", encoding="utf-8") as f:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")
    except Exception:
        pass

def set_last_message(key, level, text):
    with LOCK:
        LAST_MESSAGE[key] = {"ts": now_ts(), "level": level, "text": text}

def push_output(key, out_type, cmd_display, rc=None, stdout="", stderr=""):
    entry = {
        "ts": now_ts(),
        "type": out_type,
        "cmd": cmd_display or "",
        "rc": rc,
        "stdout": stdout or "",
        "stderr": stderr or ""
    }
    with LOCK:
        OUTPUT_HISTORY.setdefault(key, deque(maxlen=INSTANCE_OUTPUT_MAX)).appendleft(entry)

def detect_su_permission_error(stderr: str, stdout: str):
    s = (stderr or "") + "\n" + (stdout or "")
    low = s.lower()
    checks = [
        ("authentication failure", "Authentication failure switching to ihtomcat (su failed)."),
        ("permission denied", "Permission denied switching to ihtomcat (su failed)."),
        ("must be run from a terminal", "su requires a TTY (policy)."),
        ("sorry, try again", "Authentication/permission issue switching to ihtomcat."),
        ("su:", "su failed. Check policy/permissions to switch to ihtomcat."),
    ]
    for needle, msg in checks:
        if needle in low:
            return msg
    return None

def get_user():
    try:
        return getpass.getuser()
    except Exception:
        return ""

CURRENT_USER = get_user()

# ---- Build instances ----
for app in APPS:
    group = app.get("group", "Unknown")
    GROUPS.add(group)
    for inst in (app.get("instances", []) or []):
        server_name = inst.get("server")
        server_info = SERVERS.get(server_name, {}) or {}

        k = inst_key(group, server_name, inst.get("name"))
        obj = {
            "group": group,
            "server": server_name,
            "name": inst.get("name"),
            "path": inst.get("path", "/"),
            "http_port": int(inst.get("http_port")) if inst.get("http_port") is not None else None,
            "server_ip": server_info.get("server_ip", "127.0.0.1"),
            "sdm_host": server_info.get("sdm_host", "127.0.0.5"),
            "sdm_port": int(server_info.get("sdm_port", 22087)),
            "start_cmd": inst.get("start_cmd", ""),
            "stop_cmd": inst.get("stop_cmd", ""),
            "restart_cmd": inst.get("restart_cmd", ""),
            "status": {"state": "CHECKING", "color": "unknown", "icon": "…", "code": 0},
        }
        INSTANCES.append(obj)
        STATUS[k] = obj["status"]
        LAST_MESSAGE[k] = {"ts": "", "level": "info", "text": ""}
        OUTPUT_HISTORY[k] = deque(maxlen=INSTANCE_OUTPUT_MAX)

GROUPS = sorted(GROUPS)

log_event("info", "Console started",
          instances=len(INSTANCES), groups=len(GROUPS),
          run_as=RUN_AS, action_force_tty=ACTION_FORCE_TTY,
          refresh_parallel=SSH_MAX_PARALLEL, refresh_delay=REFRESH_DELAY_SECONDS,
          action_timeout=ACTION_TIMEOUT_SECONDS)

# ---- SSH ----
def run_ssh_raw(user, sdm_host, sdm_port, remote_cmd, timeout=25):
    ssh_cmd = (
        f"ssh -p {sdm_port} "
        f"-o BatchMode=yes "
        f"-o ConnectTimeout={SSH_CONNECT_TIMEOUT_SECONDS} "
        f"-o StrictHostKeyChecking=accept-new "
        f"{shlex.quote(user)}@{shlex.quote(sdm_host)} "
        f"{remote_cmd}"
    )
    try:
        res = subprocess.run(ssh_cmd, shell=True, capture_output=True, text=True, timeout=timeout)
        return (res.returncode == 0), res.returncode, (res.stdout or "").strip(), (res.stderr or "").strip(), ssh_cmd
    except subprocess.TimeoutExpired as e:
        return False, 124, "", f"TimeoutExpired: {e}", ssh_cmd
    except Exception as e:
        return False, 1, "", f"Exception: {e}", ssh_cmd

def run_ssh_bash(user, sdm_host, sdm_port, remote_cmd, timeout=15, force_tty=False, use_stdin=False):
    """
    Execute a remote script over SSH.

    - use_stdin=False (default): calls `ssh ... bash -lc <script>` (single-arg bash -lc)
    - use_stdin=True: calls `ssh ... bash -s` and sends the script on stdin (safer for multi-line/heredoc)
    """
    import subprocess
    tty_flag = "-tt" if force_tty else "-T"

    ssh_connect_timeout = int(globals().get("SSH_CONNECT_TIMEOUT_SECONDS", 8) or 8)

    base = [
        "ssh", tty_flag,
        "-p", str(int(sdm_port)),
        "-o", "BatchMode=yes",
        "-o", f"ConnectTimeout={ssh_connect_timeout}",
        "-o", "StrictHostKeyChecking=accept-new",
        f"{user}@{sdm_host}"
    ]

    # Normalize line endings for remote shell
    script = (remote_cmd or "").replace("\r\n", "\n").replace("\r", "\n")

    if use_stdin:
        cmd = base + ["bash", "-s"]
        ssh_disp = " ".join(cmd) + " <remote_script"
        print(f"[DEBUG] ssh={ssh_disp}", flush=True)
        print(f"[DEBUG] remote_script={script!r}", flush=True)
        p = subprocess.run(
            cmd,
            input=script,
            capture_output=True,
            text=True,
            timeout=timeout,
            shell=False
        )
    else:
        # quote script as single arg to bash -lc
        cmd = base + ["bash", "-lc", script]
        ssh_disp = " ".join(base) + " bash -lc <remote_script>"
        print(f"[DEBUG] ssh={ssh_disp}", flush=True)
        p = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=timeout,
            shell=False
        )

    return (p.returncode == 0), p.returncode, (p.stdout or ""), (p.stderr or ""), ssh_disp

# ---- Refresh (normal user) ----
def check_jvm_status(user, inst):
    group, server, name = inst["group"], inst["server"], inst["name"]
    key = inst_key(group, server, name)

    http_port = inst.get("http_port")
    path = inst.get("path", "/")
    server_ip = inst.get("server_ip")
    sdm_host = inst.get("sdm_host")
    sdm_port = inst.get("sdm_port")

    if http_port is None:
        status = {"state": "UNKNOWN", "color": "unknown", "icon": "❓", "code": 0}
        with LOCK:
            STATUS[key] = status
            inst["status"] = status
        return status

    if not path.startswith("/"):
        path = "/" + path

    def mk_curl(host):
        url = f"http://{host}:{int(http_port)}{path}"
        return (
            f"PATH=/usr/bin:/bin:/usr/local/bin:$PATH; "
            f"curl -s -o /dev/null -w '%{{http_code}}' "
            f"-X POST -H 'Content-Type: application/x-www-form-urlencoded' "
            f"--max-time {CURL_TIMEOUT_SECONDS} {shlex.quote(url)}"
        )

    remote_cmd = mk_curl(server_ip)
    ok, rc, out, err, _ = run_ssh_raw(user, sdm_host, sdm_port, remote_cmd, timeout=25)

    if (not ok) or (not out.strip().isdigit()):
        remote_cmd2 = mk_curl("127.0.0.1")
        ok2, rc2, out2, err2, _ = run_ssh_raw(user, sdm_host, sdm_port, remote_cmd2, timeout=25)
        if ok2 and out2.strip().isdigit():
            ok, rc, out, err = ok2, rc2, out2, err2
            remote_cmd = remote_cmd2

    push_output(key, "REFRESH", f"[exec] {remote_cmd}", rc, out, err)

    if not ok:
        status = {"state": "DOWN", "color": "down", "icon": "❌", "code": 0}
    else:
        try:
            code = int(out.strip())
        except Exception:
            status = {"state": "UNKNOWN", "color": "unknown", "icon": "❓", "code": 0}
        else:
            if code in HTTP_OK_CODES:
                status = {"state": "UP", "color": "up", "icon": "✅", "code": code}
            elif 500 <= code <= 599:
                status = {"state": "UNHEALTHY", "color": "warn", "icon": "⚠️", "code": code}
            elif 400 <= code <= 499:
                status = {"state": "WARN", "color": "warn", "icon": "⚠️", "code": code}
            else:
                status = {"state": "UNKNOWN", "color": "unknown", "icon": "❓", "code": code}

    with LOCK:
        STATUS[key] = status
        inst["status"] = status

    log_event("info", "Refresh executed", group=group, server=server, name=name, ok=ok, rc=rc, out=out)
    return status

def refresh_one(user, group, server, name):
    inst = next((i for i in INSTANCES if i["group"] == group and i["server"] == server and i["name"] == name), None)
    if inst:
        check_jvm_status(user, inst)

def _compute_status_from_http_code(code: int):
    if code == 200:
        return {"state": "UP", "color": "up", "icon": "✅", "code": code}
    return {"state": "DOWN", "color": "down", "icon": "❌", "code": code}

def refresh_server_batch(user: str, server_name: str, inst_list: list[dict]):
    """
    One SSH per server; runs multiple curls remotely and returns results for all JVMs on that server.
    """
    if not inst_list:
        return

    # IMPORTANT: no `set -e` here; we want the batch to continue even if one JVM check fails
    lines = ["set -uo pipefail", "export PATH=/usr/bin:/bin:/usr/local/bin:$PATH"]
    lines.append("ct=" + shlex.quote(str(CURL_TIMEOUT_SECONDS)))

    for inst in inst_list:
        name = inst["name"]
        http_port = inst.get("http_port")
        path = inst.get("path", "/") or "/"
        if http_port is None:
            continue
        if not path.startswith("/"):
            path = "/" + path

        url = f"http://{inst.get('server_ip')}:{int(http_port)}{path}"
        # Use POST + content-type (matches your working manual check)
        curl = (
            "code=$(curl -s -o /dev/null -w '%{http_code}' "
            "-X POST -H 'Content-Type: application/x-www-form-urlencoded' "
            "--max-time \"$ct\" "
            f"{shlex.quote(url)} || echo 0); "
            f"printf '%s|%s\\n' {shlex.quote(name)} \"$code\""
        )
        lines.append(curl)

    remote_script = "\n".join(lines)

    # Use run_ssh_bash so the remote script runs reliably as a shell program
    sdm_host = inst_list[0]["sdm_host"]
    sdm_port = inst_list[0]["sdm_port"]

    ok, rc, out, err, ssh_cmd = run_ssh_bash(
        user=user,
        sdm_host=sdm_host,
        sdm_port=sdm_port,
        remote_cmd=remote_script,
        timeout=25,
        force_tty=False
    )

    # Update output history for each instance and parse results
    name_to_inst = {i["name"]: i for i in inst_list}
    results = {}

    if ok and out:
        for raw in out.splitlines():
            if "|" not in raw:
                continue
            n, c = raw.split("|", 1)
            n = n.strip()
            c = c.strip()
            if not c.isdigit():
                continue
            results[n] = int(c)

    for inst in inst_list:
        group, server, name = inst["group"], inst["server"], inst["name"]
        key = inst_key(group, server, name)

        if name in results:
            code = results[name]
            status = _compute_status_from_http_code(code)
            status_line = str(code)  # you said "just 200 is good" and code display is enough
            push_output(key, "REFRESH", f"[batch] {server_name}\n[ssh] {ssh_cmd}", rc, status_line, err)
        else:
            # batch ran but didn't return a line for this JVM (or curl failed hard)
            status = {"state": "DOWN", "color": "down", "icon": "❌", "code": 0}
            push_output(key, "REFRESH", f"[batch] {server_name}\n[ssh] {ssh_cmd}", rc, "", err or "No result from batch refresh")

        with LOCK:
            STATUS[key] = status
            inst["status"] = status

def refresh_groups(user, groups):
    targets = [i for i in INSTANCES if i["group"] in groups]
    if not targets:
        return

    # Group by server -> one SSH per server
    by_server = {}
    for inst in targets:
        by_server.setdefault(inst["server"], []).append(inst)

    def worker(server_name, inst_list):
        refresh_server_batch(user, server_name, inst_list)
        # optional small pacing between server batches
        if REFRESH_DELAY_SECONDS > 0:
            time.sleep(REFRESH_DELAY_SECONDS)

    max_workers = max(1, min(len(by_server), max(1, SSH_MAX_PARALLEL)))
    with ThreadPoolExecutor(max_workers=max_workers) as ex:
        futures = [ex.submit(worker, srv, lst) for srv, lst in by_server.items()]
        for _ in as_completed(futures):
            pass

# ---- Actions (ONLY actions use su ihtomcat) ----
IHTOMCAT_HOME = (SETTINGS.get("ihtomcat_home", "/home/ihtomcat") or "/home/ihtomcat").strip()
ACTION_TIMEOUT_SECONDS = int(SETTINGS.get("action_timeout_seconds", 90) or 90)


def run_action(user, inst, action):
    """
    Run START/STOP/RESTART for a JVM instance (uses IHTOMCAT_HOME, run_ssh_bash, push_output, etc.)
    This is a module-level function (no self).
    """
    key = inst_key(inst["group"], inst["server"], inst["name"])

    # RESTART is a STOP then START sequence
    if action == "RESTART":
        push_output(key, "RESTART_PHASE", "Stopping JVM...", None, "", "")
        ok1, rc1, out1, err1 = run_action(user, inst, "STOP")
        if not ok1:
            push_output(key, "RESTART_DONE", "STOP phase failed", rc1, out1, err1)
            try:
                append_history({
                    "ts": now_ts(), "user": user, "group": inst["group"],
                    "server": inst["server"], "name": inst["name"],
                    "action": "RESTART", "phase": "STOP", "rc": rc1,
                    "stdout": out1, "stderr": err1
                })
            except Exception:
                pass
            return False, rc1, out1, err1

        push_output(key, "RESTART_PHASE", "Stop completed. Starting JVM...", 0, "", "")
        ok2, rc2, out2, err2 = run_action(user, inst, "START")
        try:
            check_jvm_status(user, inst)
        except Exception:
            pass
        push_output(key, "RESTART_DONE", "RESTART completed", rc2, out2, err2)
        try:
            append_history({
                "ts": now_ts(), "user": user, "group": inst["group"],
                "server": inst["server"], "name": inst["name"],
                "action": "RESTART", "phase": "DONE", "rc": rc2,
                "stdout": out2, "stderr": err2
            })
        except Exception:
            pass
        return ok2, rc2, out2, err2

    # map action -> yaml command
    if action == "START":
        yaml_cmd = inst.get("start_cmd", "")
    elif action == "STOP":
        yaml_cmd = inst.get("stop_cmd", "")
    elif action == "RESTART":
        yaml_cmd = inst.get("restart_cmd", "")
    else:
        push_output(key, f"{action}_DONE", "[internal] invalid action", 2, "", "Invalid action")
        set_last_message(key, "error", f"❌ Invalid action {action}")
        return False, 2, "", "Invalid action"

    if not yaml_cmd:
        push_output(key, f"{action}_DONE", "[yaml] (missing)", 2, "", f"{action} not configured")
        set_last_message(key, "error", f"❌ {action} not configured")
        return False, 2, "", f"{action} not configured"

    run_in_home = f"cd {shlex.quote(IHTOMCAT_HOME)} && {yaml_cmd}"
    log_file = f"{IHTOMCAT_HOME}/logs/{inst['name']}_Activitylogs.logs"

    prelude = (
        f"mkdir -p {shlex.quote(IHTOMCAT_HOME)}/logs\n"
        "find " + shlex.quote(f"{IHTOMCAT_HOME}/logs") +
        " -maxdepth 1 -type f -name '*_Activitylogs.logs' -mtime +90 -delete >/dev/null 2>&1 || true\n"
        f"echo \"===== $(date '+%Y-%m-%d %H:%M:%S %Z') | user={user} | action={action} | jvm={inst['name']} =====\" >> {shlex.quote(log_file)}\n"
    )

    if action in ("START", "RESTART"):
        payload_as_ihtomcat = (
            prelude +
            f"nohup bash -lc {shlex.quote(run_in_home)} >> {shlex.quote(log_file)} 2>&1 &\n"
            f"echo 'Detached. Activity log: {log_file}'\n"
        )
    else:
        payload_as_ihtomcat = (
            prelude +
            f"bash -lc {shlex.quote(run_in_home)} >> {shlex.quote(log_file)} 2>&1\n"
            "rc=$?\n"
            f"echo \"RC=$rc\" >> {shlex.quote(log_file)}\n"
            "exit $rc\n"
        )

    full_cmd = (
        "if [ \"$(id -un)\" = \"ihtomcat\" ]; then\n"
        f"{payload_as_ihtomcat}\n"
        "else\n"
        "sudo -n /usr/bin/su - ihtomcat <<'JWS_EOF'\n"
        f"{payload_as_ihtomcat}\n"
        "JWS_EOF\n"
        "fi\n"
    )

    # debug context
    if action in ("START", "STOP", "RESTART"):
        print(f"[DEBUG] action={action} user={user} server={inst.get('server')} jvm={inst.get('name')}", flush=True)
        print(f"[DEBUG] run_in_home={run_in_home}", flush=True)
        print(f"[DEBUG] log_file={log_file}", flush=True)
        print(f"[DEBUG] full_cmd_preview={full_cmd.splitlines()[0]!r} ...", flush=True)

    ok, rc, out, err, _ssh_cmd = run_ssh_bash(
        user=user,
        sdm_host=inst["sdm_host"],
        sdm_port=inst["sdm_port"],
        remote_cmd=full_cmd,
        timeout=ACTION_TIMEOUT_SECONDS,
        force_tty=True,
        use_stdin=True
    )

    # Prefer showing activity log tail if available
    tail_cmd = f"tail -n {INSTANCE_OUTPUT_MAX} {shlex.quote(log_file)} || true"
    _, _rc2, tail_out, tail_err, _ = run_ssh_bash(
        user=user,
        sdm_host=inst["sdm_host"],
        sdm_port=inst["sdm_port"],
        remote_cmd=tail_cmd,
        timeout=15,
        force_tty=False,
        use_stdin=False
    )

    display_out = tail_out if tail_out else (out or "")
    display_err = tail_err if tail_err else (err or "")

    # clean noisy lines
    clean_out = "\n".join(ln for ln in (display_out or "").splitlines() if not ln.startswith("Last login:")).strip()
    clean_err = "\n".join(ln for ln in (display_err or "").splitlines() if "Connection to" not in ln or "closed." not in ln).strip()

    push_output(key, f"{action}_DONE", f"{action} wrapper for {inst['name']}", rc, clean_out, clean_err)
    # record history
    try:
        append_history({
            "ts": now_ts(), "user": user, "group": inst["group"],
            "server": inst["server"], "name": inst["name"],
            "action": action, "cmd": yaml_cmd, "exec": full_cmd, "rc": rc,
            "stdout": clean_out, "stderr": clean_err
        })
    except Exception:
        pass

    # set last message
    if ok:
        set_last_message(key, "success", f"✅ {action} completed (rc={rc})")
    else:
        set_last_message(key, "error", f"❌ {action} failed (rc={rc})")

    return ok, rc, clean_out, clean_err

# ---- Jobs (bulk) ----
def create_job(action, user, keys):
    job_id = str(uuid.uuid4())
    with LOCK:
        JOBS[job_id] = {
            "job_id": job_id,
            "created": now_ts(),
            "action": action,
            "user": user,
            "done": False,
            "summary": {"total": len(keys), "success": 0, "failed": 0},
            "items": {k: {"state": "PENDING", "ok": None, "rc": None, "ts": ""} for k in keys}
        }
    return job_id

def finalize_job(job_id):
    with LOCK:
        job = JOBS.get(job_id)
        if not job:
            return
        s = f = 0
        for it in job["items"].values():
            if it["state"] == "DONE" and it["ok"] is True:
                s += 1
            if it["state"] == "DONE" and it["ok"] is False:
                f += 1
        job["summary"] = {"total": len(job["items"]), "success": s, "failed": f}
        job["done"] = all(it["state"] == "DONE" for it in job["items"].values())

def run_bulk_job(job_id, action):
    with LOCK:
        job = JOBS.get(job_id)
        if not job:
            return
        user = job["user"]
        keys = list(job["items"].keys())

    inst_by_key = {inst_key(i["group"], i["server"], i["name"]): i for i in INSTANCES}

    def do_one(k):
        inst = inst_by_key.get(k)
        if not inst:
            return k, False, 1

        with LOCK:
            JOBS[job_id]["items"][k]["state"] = "RUNNING"
            JOBS[job_id]["items"][k]["ts"] = now_ts()

        # NOTE: QUEUED entry is added only once in the POST handler now (no duplicates)
        ok, rc, out, err = run_action(user, inst, action)

        # refresh after action so state updates
        time.sleep(1)
        check_jvm_status(user, inst)
        return k, ok, rc

    max_workers = max(1, min(ACTION_MAX_PARALLEL, 5))
    with ThreadPoolExecutor(max_workers=max_workers) as ex:
        futures = [ex.submit(do_one, k) for k in keys]
        for fut in as_completed(futures):
            k, ok, rc = fut.result()
            with LOCK:
                JOBS[job_id]["items"][k]["state"] = "DONE"
                JOBS[job_id]["items"][k]["ok"] = bool(ok)
                JOBS[job_id]["items"][k]["rc"] = rc
                JOBS[job_id]["items"][k]["ts"] = now_ts()
            finalize_job(job_id)

    finalize_job(job_id)

# ---- HTTP ----
class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path in ("/", "/index.html"):
            html = self.generate_html()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.end_headers()
            self.wfile.write(html.encode("utf-8"))
            return

        if self.path.startswith("/api/refresh_one"):
            parsed = urlparse(self.path)
            p = parse_qs(parsed.query)
            user = (p.get("user", [""])[0] or "").strip()
            group = (p.get("group", [""])[0] or "").strip()
            server = (p.get("server", [""])[0] or "").strip()
            name = (p.get("name", [""])[0] or "").strip()
            if not user or not group or not server or not name:
                return self.json_error("Missing user/group/server/name", 400)
            threading.Thread(target=refresh_one, args=(user, group, server, name), daemon=True).start()
            return self.json_response({"status": "refreshing_one"})

        if self.path.startswith("/api/refresh"):
            parsed = urlparse(self.path)
            p = parse_qs(parsed.query)
            user = (p.get("user", [""])[0] or "").strip()
            groups_param = (p.get("groups", [""])[0] or "").strip()
            groups = [g.strip() for g in groups_param.split(",") if g.strip()]
            if not user or not groups:
                return self.json_error("Missing user or groups", 400)
            threading.Thread(target=refresh_groups, args=(user, groups), daemon=True).start()
            return self.json_response({"status": "refreshing", "groups": groups})

        if self.path == "/api/instances":
            with LOCK:
                data = [{
                    "group": i["group"],
                    "server": i["server"],
                    "name": i["name"],
                    "status": STATUS.get(inst_key(i["group"], i["server"], i["name"]), i["status"]),
                    "message": LAST_MESSAGE.get(inst_key(i["group"], i["server"], i["name"]), {"ts": "", "level": "info", "text": ""})
                } for i in INSTANCES]
            return self.json_response(data)

        if self.path.startswith("/api/output"):
            parsed = urlparse(self.path)
            p = parse_qs(parsed.query)
            group = (p.get("group", [""])[0] or "").strip()
            server = (p.get("server", [""])[0] or "").strip()
            name = (p.get("name", [""])[0] or "").strip()
            k = inst_key(group, server, name)
            with LOCK:
                entries = list(OUTPUT_HISTORY.get(k, []))
            return self.json_response(entries)

        if self.path.startswith("/api/job"):
            parsed = urlparse(self.path)
            p = parse_qs(parsed.query)
            job_id = (p.get("id", [""])[0] or "").strip()
            if not job_id:
                return self.json_error("Missing job id", 400)
            with LOCK:
                job = JOBS.get(job_id)
            if not job:
                return self.json_error("Job not found", 404)
            return self.json_response(job)

        self.send_error(404)

    def do_POST(self):
        length = int(self.headers.get("Content-Length", 0))
        body = (self.rfile.read(length) or b"").decode("utf-8", errors="replace")
        try:
            data = json.loads(body)
        except Exception:
            return self.json_error("Invalid JSON", 400)

        if self.path == "/api/bulk_action":
            user = (data.get("user", "") or "").strip()
            action = (data.get("action", "") or "").upper().strip()
            targets = data.get("targets", []) or []

            if not user:
                return self.json_error("Missing user", 400)
            if action not in ("START", "STOP", "RESTART"):
                return self.json_error("Invalid action", 400)
            if not isinstance(targets, list) or not targets:
                return self.json_error("No targets selected", 400)

            keys = []
            for t in targets:
                g = (t.get("group", "") or "").strip()
                s = (t.get("server", "") or "").strip()
                n = (t.get("name", "") or "").strip()
                if g and s and n:
                    keys.append(inst_key(g, s, n))
            keys = list(dict.fromkeys(keys))
            if not keys:
                return self.json_error("No valid targets", 400)

            # ✅ QUEUED entry written ONCE here (no duplicates)
            for k in keys:
                push_output(k, f"{action}_QUEUED", f"{action} queued by {user}", None, "", "")
                set_last_message(k, "info", f"⏳ {action} queued")

            job_id = create_job(action, user, keys)
            threading.Thread(target=run_bulk_job, args=(job_id, action), daemon=True).start()
            return self.json_response({"status": "started", "job_id": job_id})

        self.send_error(404)

    def json_response(self, data):
        payload = json.dumps(data).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(payload)

    def json_error(self, msg, code):
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(json.dumps({"error": msg}).encode("utf-8"))

    def log_message(self, fmt, *args):
        pass

    def generate_html(self):
        groups_json = json.dumps(sorted(GROUPS))
        servers_json = json.dumps(SERVERS)
        user_val = html_escape(CURRENT_USER)

        HTML = r"""<!DOCTYPE html>
<html>
<head>
<meta charset="UTF-8" />
<meta name="viewport" content="width=device-width, initial-scale=1.0" />
<title>JWS Console For Infinity Connect</title>
<style>
  * { box-sizing:border-box; }
  body { margin:0; font-family:"Segoe UI", Roboto, Arial, sans-serif; background:#f6f7fb; color:#111827; font-size:13px; }
  .topbar { background:#111827; color:#fff; padding:14px 18px; position:sticky; top:0; z-index:50; }
  .wrap { max-width:1700px; margin:0 auto; padding:14px 18px 26px; }
  .toolbar { display:flex; gap:10px; align-items:center; flex-wrap:wrap; background:#fff; border:1px solid #e5e7eb; border-radius:12px; padding:10px; margin-top:14px; }
  .toolbar .spacer { flex:1; }
  .input, .select { padding:8px 10px; border:1px solid #d1d5db; border-radius:10px; outline:none; background:#fff; font-size:12px; }
  .input { min-width:220px; }
  .btn { padding:8px 12px; border-radius:10px; border:1px solid #d1d5db; background:#fff; cursor:pointer; font-weight:800; font-size:12px; }
  .btn-primary { background:#111827; color:#fff; border-color:#111827; }
  .btn-danger { background:#fef2f2; border-color:#fecaca; color:#991b1b; }
  .btn-warn { background:#fff7ed; border-color:#fed7aa; color:#9a3412; }
  .btn:disabled { opacity:0.5; cursor:not-allowed; }
  .banner { margin-top:12px; display:none; padding:10px 12px; border-radius:12px; border:1px solid #e5e7eb; background:#fff; }
  .banner.show { display:block; }
  .banner.success { border-color:#bbf7d0; background:#f0fdf4; color:#166534; }
  .banner.error { border-color:#fecaca; background:#fef2f2; color:#991b1b; }
  .banner.info { border-color:#bfdbfe; background:#eff6ff; color:#1d4ed8; }

  .tablewrap { margin-top:12px; background:#fff; border:1px solid #e5e7eb; border-radius:12px; overflow:hidden; }
  table { width:100%; border-collapse:separate; border-spacing:0; }
  thead th { text-align:left; font-size:12px; font-weight:900; color:#374151; background:#f9fafb; border-bottom:1px solid #e5e7eb; padding:10px 12px; }
  tbody td { padding:10px 12px; border-bottom:1px solid #eef2f7; vertical-align:middle; }
  tbody tr:hover { background:#fafafa; }
  .chk { width:18px; height:18px; cursor:pointer; }
  .mono { font-family: ui-monospace, SFMono-Regular, Menlo, Monaco, Consolas, "Liberation Mono", "Courier New", monospace; }
  .badge { display:inline-flex; align-items:center; justify-content:center; padding:4px 10px; border-radius:999px; font-weight:900; font-size:11px; min-width:86px; }
  .up { background:#d1fae5; color:#065f46; }
  .down { background:#fee2e2; color:#991b1b; }
  .warn { background:#fef3c7; color:#92400e; }
  .unknown { background:#f3f4f6; color:#6b7280; }
  .msg { display:flex; gap:8px; align-items:center; font-size:12px; }
  .msg small { color:#6b7280; }
  .msg .ok { color:#166534; font-weight:800; }
  .msg .bad { color:#991b1b; font-weight:800; }
  .msg .info { color:#1d4ed8; font-weight:800; }
  .row-actions { display:flex; gap:8px; justify-content:flex-end; }
  .linkbtn { padding:6px 10px; border-radius:10px; border:1px solid #d1d5db; background:#fff; cursor:pointer; font-weight:900; font-size:11px; }
</style>
</head>
<body>
<div class="topbar">
  <div style="font-weight:900;">JWS Console For Infinity Connect</div>
</div>

<div class="wrap">
  <div class="toolbar">
    <input id="username" class="input" placeholder="SSH Username" value="%%USER_VAL%%" />
    <select id="groupFilter" class="select"></select>
    <select id="serverFilter" class="select"></select>
    <input id="search" class="input" placeholder="Search JVM name..." style="min-width:260px;" />
    <button class="btn btn-primary" onclick="refreshView()">Refresh</button>
    <div class="spacer"></div>
    <button id="btnStart" class="btn" onclick="bulkAction('START')">Start</button>
    <button id="btnStop" class="btn btn-danger" onclick="bulkAction('STOP')">Stop</button>
    <button id="btnRestart" class="btn btn-warn" onclick="bulkAction('RESTART')">Restart</button>
  </div>

  <div id="banner" class="banner"></div>

  <div class="tablewrap">
    <table>
      <thead>
        <tr>
          <th style="width:42px;"><input id="chkAll" class="chk" type="checkbox" onchange="toggleAll(this.checked)" /></th>
          <th>JVM Name</th>
          <th style="width:160px;">Group</th>
          <th style="width:160px;">Server</th>
          <th style="width:120px;">Status</th>
          <th>Message</th>
          <th style="width:280px; text-align:right;">Actions</th>
        </tr>
      </thead>
      <tbody id="rows"></tbody>
    </table>
  </div>
</div>

<div id="outputModal" style="display:none; position:fixed; inset:0; background:rgba(0,0,0,.45); z-index:220; align-items:center; justify-content:center;">
  <div style="width:min(980px,96vw); background:#fff; border-radius:14px; border:1px solid #e5e7eb; padding:14px;">
    <div style="font-weight:900; font-size:14px;">Output History (latest first)</div>
    <div style="color:#6b7280; font-size:12px; margin-top:6px;" id="outSub">-</div>
    <div style="margin-top:10px; padding:12px; border:1px solid #e5e7eb; border-radius:12px; background:#0b1220; color:#e5e7eb;
                font-family: ui-monospace, SFMono-Regular, Menlo, Monaco, Consolas, 'Liberation Mono', 'Courier New', monospace;
                font-size:12px; white-space:pre-wrap; max-height:520px; overflow:auto;" id="outBox">No output</div>
    <div style="display:flex; gap:10px; justify-content:flex-end; margin-top:12px;">
      <button class="btn" onclick="closeOutput()">Close</button>
    </div>
  </div>
</div>

<div id="confirmModal" style="display:none; position:fixed; inset:0; background:rgba(0,0,0,.45); z-index:200; align-items:center; justify-content:center;">
  <div style="width:min(700px,96vw); background:#fff; border-radius:14px; border:1px solid #e5e7eb; padding:14px;">
    <div style="font-weight:900; font-size:14px;" id="confirmTitle">Confirm</div>
    <div style="color:#6b7280; font-size:12px; margin-top:6px;" id="confirmSub">-</div>
    <div style="display:flex; gap:10px; justify-content:flex-end; margin-top:12px;">
      <button class="btn" onclick="closeConfirm()">Cancel</button>
      <button class="btn btn-primary" onclick="confirmBulk()">Confirm</button>
    </div>
  </div>
</div>

<script>
  const GROUPS = %%GROUPS_JSON%%;
  const SERVERS = %%SERVERS_JSON%%;

  let DATA = [];
  let selected = new Set();
  let pending = null;
  let pollTimer = null;

  function showBanner(type, text) {
    const b = document.getElementById('banner');
    b.className = 'banner show ' + type;
    b.textContent = text;
    setTimeout(() => { if (type !== 'error') b.className = 'banner'; }, type === 'error' ? 7000 : 3500);
  }

  function buildFilters() {
    const gf = document.getElementById('groupFilter');
    const sf = document.getElementById('serverFilter');
    gf.innerHTML = ''; sf.innerHTML = '';

    let o = document.createElement('option');
    o.value = ''; o.textContent = 'All Groups';
    gf.appendChild(o);
    GROUPS.forEach(g => { const opt = document.createElement('option'); opt.value=g; opt.textContent=g; gf.appendChild(opt); });

    o = document.createElement('option');
    o.value = ''; o.textContent = 'All Servers';
    sf.appendChild(o);
    Object.keys(SERVERS).forEach(s => { const opt = document.createElement('option'); opt.value=s; opt.textContent=s; sf.appendChild(opt); });

    gf.onchange = render;
    sf.onchange = render;
    document.getElementById('search').oninput = render;
  }

  function applyFilters(item) {
    const g = document.getElementById('groupFilter').value;
    const s = document.getElementById('serverFilter').value;
    const q = document.getElementById('search').value.trim().toLowerCase();
    if (g && item.group !== g) return false;
    if (s && item.server !== s) return false;
    if (q && !item.name.toLowerCase().includes(q)) return false;
    return true;
  }

  function statusBadge(st) {
    const color = (st && st.color) ? st.color : 'unknown';
    const state = (st && st.state) ? st.state : 'UNKNOWN';
    let cls = 'unknown';
    if (color === 'up') cls = 'up';
    else if (color === 'down') cls = 'down';
    else if (color === 'warn') cls = 'warn';
    return '<span class="badge ' + cls + '">' + state + '</span>';
  }

  function msgCell(m) {
    const text = (m && m.text) ? m.text : '';
    const ts = (m && m.ts) ? m.ts : '';
    const lvl = (m && m.level) ? m.level : 'info';
    let c = 'info';
    if (lvl === 'success') c = 'ok';
    if (lvl === 'error') c = 'bad';
    const tsPart = ts ? ('(' + ts + ')') : '';
    return '<div class="msg"><span class="' + c + '">' + (text || '') + '</span> <small>' + tsPart + '</small></div>';
  }

  function keyOf(i) { return i.group + '|' + i.server + '|' + i.name; }

  function toggleAll(checked) {
    selected.clear();
    const filtered = DATA.filter(applyFilters);
    if (checked) filtered.forEach(i => selected.add(keyOf(i)));
    render();
  }

  function toggleOne(k, checked) {
    if (checked) selected.add(k); else selected.delete(k);
    updateActionButtons();
  }

  function updateActionButtons() {
    const any = selected.size > 0;
    document.getElementById('btnStart').disabled = !any;
    document.getElementById('btnStop').disabled = !any;
    document.getElementById('btnRestart').disabled = !any;
  }

  function render() {
    const tbody = document.getElementById('rows');
    tbody.innerHTML = '';
    const filtered = DATA.filter(applyFilters);

    filtered.forEach(item => {
      const k = keyOf(item);
      const tr = document.createElement('tr');
      tr.innerHTML = `
        <td><input class="chk" type="checkbox" ${selected.has(k) ? 'checked' : ''} onchange="toggleOne('${k}', this.checked)" /></td>
        <td class="mono">${item.name}</td>
        <td>${item.group}</td>
        <td>${item.server}</td>
        <td>${statusBadge(item.status)}</td>
        <td>${msgCell(item.message)}</td>
        <td>
          <div class="row-actions">
            <button class="linkbtn" onclick="refreshOne('${item.group}','${item.server}','${item.name}')">Refresh</button>
            <button class="linkbtn" onclick="showOutput('${item.group}','${item.server}','${item.name}')">Output</button>
          </div>
        </td>
      `;
      tbody.appendChild(tr);
    });
    updateActionButtons();
  }

  function loadData() {
    return fetch('/api/instances')
      .then(r => r.json())
      .then(items => { DATA = items || []; render(); });
  }

  // ✅ UI polling helper so Refresh All updates the page
  function pollInstances(seconds=12) {
    let left = seconds;
    const t = setInterval(() => {
      loadData().catch(()=>{});
      left -= 1;
      if (left <= 0) clearInterval(t);
    }, 1000);
  }

  function refreshView() {
    const user = document.getElementById('username').value.trim();
    if (!user) return showBanner('error', 'Enter SSH username first');

    const gf = document.getElementById('groupFilter').value;
    const groups = gf ? [gf] : GROUPS;

    showBanner('info', 'Refreshing...');
    fetch('/api/refresh?user=' + encodeURIComponent(user) + '&groups=' + encodeURIComponent(groups.join(',')))
      .then(r => r.ok ? r.json() : r.json().then(d => Promise.reject(new Error(d.error || 'Refresh failed'))))
      .then(() => { pollInstances(12); showBanner('success','Refresh running (updating list)'); })
      .catch(e => showBanner('error', e.message));
  }

  function refreshOne(group, server, name) {
    const user = document.getElementById('username').value.trim();
    if (!user) return showBanner('error', 'Enter SSH username first');

    showBanner('info', 'Refreshing ' + name + ' ...');
    fetch('/api/refresh_one?user=' + encodeURIComponent(user) +
          '&group=' + encodeURIComponent(group) +
          '&server=' + encodeURIComponent(server) +
          '&name=' + encodeURIComponent(name))
      .then(r => r.ok ? r.json() : r.json().then(d => Promise.reject(new Error(d.error || 'Refresh failed'))))
      .then(() => { pollInstances(6); showBanner('success', name + ' refresh running'); })
      .catch(e => showBanner('error', e.message));
  }

  function bulkAction(action) {
    if (selected.size === 0) return showBanner('error', 'Select at least one JVM');

    const targets = [];
    DATA.forEach(i => { if (selected.has(keyOf(i))) targets.push({group:i.group, server:i.server, name:i.name}); });

    pending = {action, targets};
    document.getElementById('confirmTitle').textContent = action + ' selected JVM(s)';
    document.getElementById('confirmSub').textContent = 'Targets: ' + targets.length + ' (Output will show QUEUED/RUNNING/DONE)';
    document.getElementById('confirmModal').style.display = 'flex';
  }

  function closeConfirm() { document.getElementById('confirmModal').style.display = 'none'; }

  function confirmBulk() {
    if (!pending) return;
    closeConfirm();

    const user = document.getElementById('username').value.trim();
    if (!user) return showBanner('error', 'Enter SSH username first');

    showBanner('info', pending.action + ' queued...');

    fetch('/api/bulk_action', {
      method: 'POST',
      headers: {'Content-Type':'application/json'},
      body: JSON.stringify({user, action: pending.action, targets: pending.targets})
    })
    .then(r => r.ok ? r.json() : r.json().then(d => Promise.reject(new Error(d.error || 'Bulk action failed'))))
    .then(() => { pollInstances(15); showBanner('success', 'Action queued. Click Output to see details.'); })
    .catch(e => showBanner('error', e.message));
  }

  function showOutput(group, server, name) {
    fetch('/api/output?group=' + encodeURIComponent(group) + '&server=' + encodeURIComponent(server) + '&name=' + encodeURIComponent(name))
      .then(r => r.json())
      .then(entries => {
        document.getElementById('outSub').textContent = name + ' (' + group + '/' + server + ')';
        if (!entries || entries.length === 0) {
          document.getElementById('outBox').textContent = 'No output yet.';
        } else {
          const text = entries.map(e => (
            '=== ' + (e.type || '-') + ' @ ' + (e.ts || '-') + ' ===\n' +
            'RC: ' + ((e.rc === null || e.rc === undefined) ? '-' : e.rc) + '\n' +
            'CMD:\n' + (e.cmd || '-') + '\n\n' +
            (e.stdout ? ('--- STDOUT ---\n' + e.stdout + '\n\n') : '') +
            (e.stderr ? ('--- STDERR ---\n' + e.stderr + '\n') : '') +
            '\n'
          )).join('\n');
          document.getElementById('outBox').textContent = text;
        }
        document.getElementById('outputModal').style.display = 'flex';
      });
  }

  function closeOutput() { document.getElementById('outputModal').style.display = 'none'; }

  buildFilters();
  loadData().catch(()=>{});
  updateActionButtons();
</script>
</body>
</html>
"""
        return (HTML
                .replace("%%GROUPS_JSON%%", groups_json)
                .replace("%%SERVERS_JSON%%", servers_json)
                .replace("%%USER_VAL%%", user_val))

# ---- Main ----
if __name__ == "__main__":
    try:
        print(f"✓ Loaded {len(GROUPS)} groups, {len(INSTANCES)} instances")
        print("=" * 70)
        print("JWS Console")
        print("=" * 70)
        print(f"✓ Starting on http://{CONSOLE_HOST}:{CONSOLE_PORT}")
        print(f"✓ Action timeout: {ACTION_TIMEOUT_SECONDS}s")
        print("Press Ctrl+C to stop")
        print("=" * 70)

        httpd = HTTPServer((CONSOLE_HOST, CONSOLE_PORT), Handler)

        def open_browser():
            time.sleep(1)
            try:
                webbrowser.open(f"http://{CONSOLE_HOST}:{CONSOLE_PORT}")
            except Exception:
                pass

        threading.Thread(target=open_browser, daemon=True).start()
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\nStopped")
