#!/usr/bin/env python3
"""
refresh.py
Everything related to checking JVM status (Refresh).
Refresh runs as the normal SSH user (no su).
"""
import time
import shlex
from concurrent.futures import ThreadPoolExecutor, as_completed

import config
from config import (
    LOCK, INSTANCES, STATUS,
    HTTP_OK_CODES, CURL_TIMEOUT_SECONDS,
    REFRESH_DELAY_SECONDS, SSH_MAX_PARALLEL,
    inst_key, push_output, log_event,
)
from ssh_utils import run_ssh_raw, run_ssh_bash


def check_jvm_status(user, inst):
    """Check a single JVM via remote curl. Used by single-row Refresh."""
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


def refresh_server_batch(user: str, server_name: str, inst_list: list):
    """
    One SSH per server; runs multiple curls remotely and returns results
    for all JVMs on that server.
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
        curl = (
            "code=$(curl -s -o /dev/null -w '%{http_code}' "
            "-X POST -H 'Content-Type: application/x-www-form-urlencoded' "
            "--max-time \"$ct\" "
            f"{shlex.quote(url)} || echo 0); "
            f"printf '%s|%s\\n' {shlex.quote(name)} \"$code\""
        )
        lines.append(curl)

    remote_script = "\n".join(lines)

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
            status_line = str(code)
            push_output(key, "REFRESH", f"[batch] {server_name}\n[ssh] {ssh_cmd}", rc, status_line, err)
        else:
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
        if REFRESH_DELAY_SECONDS > 0:
            time.sleep(REFRESH_DELAY_SECONDS)

    max_workers = max(1, min(len(by_server), max(1, SSH_MAX_PARALLEL)))
    with ThreadPoolExecutor(max_workers=max_workers) as ex:
        futures = [ex.submit(worker, srv, lst) for srv, lst in by_server.items()]
        for _ in as_completed(futures):
            pass
