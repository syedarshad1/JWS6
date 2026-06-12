#!/usr/bin/env python3
"""
ssh_utils.py
SSH execution helpers used by both refresh.py and actions.py.
"""
import shlex
import subprocess

from config import SSH_CONNECT_TIMEOUT_SECONDS


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
    tty_flag = "-tt" if force_tty else "-T"

    base = [
        "ssh", tty_flag,
        "-p", str(int(sdm_port)),
        "-o", "BatchMode=yes",
        "-o", f"ConnectTimeout={SSH_CONNECT_TIMEOUT_SECONDS}",
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
