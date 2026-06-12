#!/usr/bin/env python3
"""
actions.py
----------
START / STOP / RESTART for JVM instances, plus bulk-job management.
Only actions use `su - ihtomcat` on the remote host.
"""

import time
import uuid
import shlex
from concurrent.futures import ThreadPoolExecutor, as_completed

from config import (
    LOCK, INSTANCES, JOBS,
    IHTOMCAT_HOME, ACTION_TIMEOUT_SECONDS, ACTION_MAX_PARALLEL,
    INSTANCE_OUTPUT_MAX,
    inst_key, push_output, set_last_message, append_history, now_ts,
)
from ssh_utils import run_ssh_bash
from refresh import check_jvm_status


def run_action(user, inst, action):
    """
    Run START/STOP/RESTART for a JVM instance.
    Returns: (ok, rc, stdout, stderr)
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

    if action == "START":
        payload_as_ihtomcat = (
            prelude +
            f"nohup bash -lc {shlex.quote(run_in_home)} >> {shlex.quote(log_file)} 2>&1 &\n"
            f"echo 'Detached. Activity log: {log_file}'\n"
        )
    else:  # STOP
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

    # Prefer showing activity log tail if available.
    # IMPORTANT: the log file is owned by ihtomcat, so the tail must also run
    # as ihtomcat (same sudo su wrapper as the action), otherwise the normal
    # SSH user gets "Permission denied" / empty output.
    tail_inner = f"tail -n {INSTANCE_OUTPUT_MAX} {shlex.quote(log_file)} 2>/dev/null || true"
    tail_cmd = (
        "if [ \"$(id -un)\" = \"ihtomcat\" ]; then\n"
        f"{tail_inner}\n"
        "else\n"
        "sudo -n /usr/bin/su - ihtomcat <<'JWS_EOF'\n"
        f"{tail_inner}\n"
        "JWS_EOF\n"
        "fi\n"
    )
    _, _rc2, tail_out, tail_err, _ = run_ssh_bash(
        user=user,
        sdm_host=inst["sdm_host"],
        sdm_port=inst["sdm_port"],
        remote_cmd=tail_cmd,
        timeout=15,
        force_tty=True,
        use_stdin=True
    )

    # Show BOTH: what the SSH session printed AND the activity log tail,
    # so the Output panel is never empty.
    parts_out = []
    if (out or "").strip():
        parts_out.append("--- ACTION OUTPUT ---\n" + out.strip())
    if (tail_out or "").strip():
        parts_out.append(f"--- ACTIVITY LOG (last {INSTANCE_OUTPUT_MAX} lines: {log_file}) ---\n" + tail_out.strip())
    display_out = "\n\n".join(parts_out)

    parts_err = []
    if (err or "").strip():
        parts_err.append(err.strip())
    if (tail_err or "").strip():
        parts_err.append(tail_err.strip())
    display_err = "\n\n".join(parts_err)

    # clean noisy lines (also strip \r added by the forced TTY)
    clean_out = "\n".join(ln.rstrip("\r") for ln in (display_out or "").splitlines()
                          if not ln.startswith("Last login:")).strip()
    clean_err = "\n".join(ln.rstrip("\r") for ln in (display_err or "").splitlines()
                          if "Connection to" not in ln or "closed." not in ln).strip()

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


# ---------------------------------------------------------------------------
# Bulk jobs
# ---------------------------------------------------------------------------
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

        # NOTE: QUEUED entry is added only once in the POST handler (no duplicates)
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
