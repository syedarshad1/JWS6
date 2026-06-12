#!/usr/bin/env python3
"""
ssh_utils.py
------------
All SSH execution helpers. Nothing here knows about JVMs or the UI —
it just runs commands on remote hosts and returns the result.
"""

import shlex
import subprocess

from config import SSH_CONNECT_TIMEOUT_SECONDS


def run_ssh_raw(user, sdm_host, sdm_port, remote_cmd, timeout=25):
    """
    Run a single remote command through ssh (shell=True, one-liner style).
    Returns: (ok, returncode, stdout, stderr, ssh_cmd_display)
    """
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
    - use_stdin=True: calls `ssh ... bash -s` and sends the script on stdin
      (safer for multi-line scripts / heredocs)

    Returns: (ok, returncode, stdout, stderr, ssh_cmd_display)

    NOTE on use_stdin + force_tty:
    With a forced TTY (-tt), the remote `bash -s` does NOT see end-of-input
    when the local stdin pipe closes, so without an explicit `exit` it hangs
    forever waiting for more commands (until the timeout kills it). We always
    append an `exit` line to the streamed script to terminate cleanly while
    preserving the exit code of the last command.
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

    # Normalize line endings for the remote shell
    script = (remote_cmd or "").replace("\r\n", "\n").replace("\r", "\n")

    try:
        if use_stdin:
            # CRITICAL: explicit exit so remote bash -s terminates (see docstring).
            # `exit` with no argument keeps the exit status of the last command.
            if not script.endswith("\n"):
                script += "\n"
            script += "exit\n"

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
            # quote the script as a single argument to bash -lc
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

    except subprocess.TimeoutExpired as e:
        # IMPORTANT: never let this escape — it would crash the worker thread
        # and leave the Output panel empty.
        partial_out = ""
        partial_err = ""
        try:
            if e.stdout:
                partial_out = e.stdout.decode() if isinstance(e.stdout, bytes) else e.stdout
            if e.stderr:
                partial_err = e.stderr.decode() if isinstance(e.stderr, bytes) else e.stderr
        except Exception:
            pass
        msg = f"TimeoutExpired: command did not finish within {timeout}s"
        if partial_err:
            msg += "\n" + partial_err
        return False, 124, partial_out, msg, " ".join(base)
    except Exception as e:
        return False, 1, "", f"Exception: {e}", " ".join(base)
