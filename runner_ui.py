import os
import sys
import time
import json
import shlex
import queue
import shutil
import threading
import subprocess
from dataclasses import dataclass
from datetime import datetime
from typing import Dict, List, Any, Optional, Tuple

import tkinter as tk
from tkinter import ttk, messagebox

import yaml

CONFIG_FILE = "targets.yaml"


# ----------------------------
# Helpers
# ----------------------------
def app_dir() -> str:
    if getattr(sys, "frozen", False):
        return os.path.dirname(sys.executable)
    return os.path.dirname(os.path.abspath(__file__))


def now_ts() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def ensure_ssh_available():
    if not shutil.which("ssh"):
        raise RuntimeError(
            "ssh.exe not found in PATH.\n\n"
            "Fix:\n"
            "1) Windows Settings → Apps → Optional features → Add 'OpenSSH Client'\n"
            "2) Or ensure ssh.exe is available in PATH."
        )


def load_yaml_config() -> Dict[str, Any]:
    path = os.path.join(app_dir(), CONFIG_FILE)
    if not os.path.exists(path):
        raise FileNotFoundError(
            f"Could not find {CONFIG_FILE}\nExpected at: {path}\n"
            f"Fix: put {CONFIG_FILE} in same folder as runner_ui.py"
        )
    with open(path, "r", encoding="utf-8") as f:
        data = yaml.safe_load(f)

    # Must be dict root: settings/servers/apps
    if not isinstance(data, dict):
        raise ValueError("Invalid YAML: root must be a dictionary (must start with settings:/servers:/apps:).")
    if "servers" not in data or "apps" not in data:
        raise ValueError("Invalid YAML: must contain top-level 'servers' and 'apps'.")
    if "settings" not in data:
        data["settings"] = {}
    return data


def ssh_run(user: str, sdm_host: str, sdm_port: int, remote_cmd: str,
            connect_timeout: int, overall_timeout: int) -> Tuple[int, str, float]:
    """
    Runs one SSH command (via StrongDM local port).
    Returns (rc, combined_output, elapsed_seconds)
    """
    cmd = [
        "ssh",
        "-p", str(sdm_port),
        "-o", "BatchMode=yes",
        "-o", f"ConnectTimeout={connect_timeout}",
        "-o", "StrictHostKeyChecking=accept-new",
        f"{user}@{sdm_host}",
        remote_cmd
    ]

    start = time.time()
    try:
        cp = subprocess.run(cmd, capture_output=True, text=True, timeout=overall_timeout)
        elapsed = time.time() - start
        out = (cp.stdout or "").strip()
        err = (cp.stderr or "").strip()
        combined = out if out else err
        return cp.returncode, combined, elapsed
    except subprocess.TimeoutExpired:
        elapsed = time.time() - start
        return 124, f"TIMEOUT after {overall_timeout}s", elapsed
    except Exception as e:
        elapsed = time.time() - start
        return 255, str(e), elapsed


def classify_http(code: int, ok_codes: set) -> Tuple[str, str, str, str]:
    """
    Returns: (state, fg_color, icon, bg_color)
    """
    if code in ok_codes:
        return "UP", "#0a7a0a", "✔", "#eaffea"
    if code == 0:
        return "DOWN", "#c20f0f", "✖", "#ffecec"
    if 500 <= code <= 599:
        return "UNHEALTHY", "#b36b00", "⚠", "#fff3e1"
    if 400 <= code <= 499:
        return "WARN", "#b36b00", "⚠", "#fff3e1"
    return "UNKNOWN", "#666666", "?", "#f2f2f2"


@dataclass(frozen=True)
class InstanceRef:
    group: str
    server: str
    name: str
    http_port: int
    path: str
    start_cmd: str
    stop_cmd: str
    restart_cmd: str


class HistoryStore:
    def __init__(self, file_path: str):
        self.file_path = file_path
        if not os.path.exists(self.file_path):
            with open(self.file_path, "w", encoding="utf-8") as f:
                pass

    def append(self, record: Dict[str, Any]):
        with open(self.file_path, "a", encoding="utf-8") as f:
            f.write(json.dumps(record) + "\n")


# ----------------------------
# UI App
# ----------------------------
class JwsConsole(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("JWS6 Console (StrongDM) — Status + Start/Stop/Restart")
        self.geometry("1400x780")

        ensure_ssh_available()
        self.cfg = load_yaml_config()

        settings = self.cfg.get("settings", {})
        self.ok_codes = set(settings.get("http_ok_codes", [200, 302, 401, 403]))
        self.curl_timeout = int(settings.get("curl_timeout_seconds", 1))
        self.ssh_connect_timeout = int(settings.get("ssh_connect_timeout_seconds", 8))
        self.history_file = os.path.join(app_dir(), settings.get("history_file", "history.jsonl"))
        self.history = HistoryStore(self.history_file)

        self.servers: Dict[str, Dict[str, Any]] = self.cfg["servers"]
        self.instances: List[InstanceRef] = self._build_instances(self.cfg["apps"])
        self.groups: List[str] = sorted({i.group for i in self.instances})

        # group enable/disable (entire group)
        self.group_enabled: Dict[str, bool] = {g: True for g in self.groups}

        # status cache by key
        self.status: Dict[str, Dict[str, Any]] = {}

        # widget references to update status color quickly
        self.row_widgets: Dict[str, Dict[str, Any]] = {}

        # background worker queue
        self.q = queue.Queue()

        # UI vars
        self.user_var = tk.StringVar(value="")

        self._build_ui()
        self._build_home()

        self.after(150, self._process_queue)

    def _build_instances(self, apps_cfg: List[Dict[str, Any]]) -> List[InstanceRef]:
        out: List[InstanceRef] = []
        for app in apps_cfg:
            group = app.get("group", "UNKNOWN_GROUP")
            for inst in app.get("instances", []):
                out.append(InstanceRef(
                    group=group,
                    server=inst["server"],
                    name=inst["name"],
                    http_port=int(inst.get("http_port", 0)),
                    path=inst.get("path", "/") or "/",
                    start_cmd=inst.get("start_cmd", ""),
                    stop_cmd=inst.get("stop_cmd", ""),
                    restart_cmd=inst.get("restart_cmd", "")
                ))
        return out

    def _key(self, inst: InstanceRef) -> str:
        return f"{inst.group}|{inst.server}|{inst.name}"

    def _require_user(self) -> Optional[str]:
        user = self.user_var.get().strip()
        if not user:
            messagebox.showwarning("Username required", "Enter the SSH username you use with StrongDM.")
            return None
        return user

    # ----------------------------
    # UI Layout
    # ----------------------------
    def _build_ui(self):
        top = ttk.Frame(self)
        top.pack(fill="x", padx=12, pady=10)

        ttk.Label(top, text="SSH Username:").pack(side="left")
        ttk.Entry(top, textvariable=self.user_var, width=24).pack(side="left", padx=8)

        ttk.Button(top, text="Refresh Enabled (Fast)", command=self.refresh_enabled_fast).pack(side="left", padx=8)

        self.status_lbl = ttk.Label(top, text="Ready", foreground="green")
        self.status_lbl.pack(side="right")

        # Tabs
        self.tabs = ttk.Notebook(self)
        self.tabs.pack(fill="both", expand=True, padx=12, pady=(0, 12))

        self.home = ttk.Frame(self.tabs)
        self.history_tab = ttk.Frame(self.tabs)
        self.tabs.add(self.home, text="Home")
        self.tabs.add(self.history_tab, text="History")

        # History viewer
        self.hist_text = tk.Text(self.history_tab, wrap="none")
        self.hist_text.pack(fill="both", expand=True, padx=8, pady=8)
        self.hist_text.insert("end", f"History file: {self.history_file}\n\n")
        self.hist_text.configure(state="disabled")

    def _build_home(self):
        # Home layout: left panel for group enable/disable, right panel for the dashboard
        outer = ttk.Frame(self.home)
        outer.pack(fill="both", expand=True)
        outer.columnconfigure(0, weight=0)
        outer.columnconfigure(1, weight=1)
        outer.rowconfigure(0, weight=1)

        # Left panel: Groups
        left = ttk.LabelFrame(outer, text="App Groups (Enable/Disable Entire Group)")
        left.grid(row=0, column=0, sticky="ns", padx=(0, 10), pady=0)

        self.group_vars: Dict[str, tk.BooleanVar] = {}
        for g in self.groups:
            var = tk.BooleanVar(value=True)
            self.group_vars[g] = var
            ttk.Checkbutton(left, text=g, variable=var, command=self._on_group_toggle).pack(anchor="w", padx=10, pady=2)

        btns = ttk.Frame(left)
        btns.pack(fill="x", padx=10, pady=(10, 10))
        ttk.Button(btns, text="Enable All", command=self._enable_all_groups).pack(fill="x", pady=2)
        ttk.Button(btns, text="Disable All", command=self._disable_all_groups).pack(fill="x", pady=2)

        # Right panel: scrollable dashboard
        right = ttk.Frame(outer)
        right.grid(row=0, column=1, sticky="nsew")
        right.rowconfigure(0, weight=1)
        right.columnconfigure(0, weight=1)

        container = ttk.Frame(right)
        container.grid(row=0, column=0, sticky="nsew")

        self.canvas = tk.Canvas(container, highlightthickness=0)
        self.scroll_y = ttk.Scrollbar(container, orient="vertical", command=self.canvas.yview)
        self.canvas.configure(yscrollcommand=self.scroll_y.set)

        self.scroll_y.pack(side="right", fill="y")
        self.canvas.pack(side="left", fill="both", expand=True)

        self.inner = ttk.Frame(self.canvas)
        self.canvas.create_window((0, 0), window=self.inner, anchor="nw")
        self.inner.bind("<Configure>", lambda e: self.canvas.configure(scrollregion=self.canvas.bbox("all")))

        # Build group/server/instance rows
        self._render_dashboard()

    def _on_group_toggle(self):
        for g, var in self.group_vars.items():
            self.group_enabled[g] = bool(var.get())
        self._render_dashboard()

    def _enable_all_groups(self):
        for v in self.group_vars.values():
            v.set(True)
        self._on_group_toggle()

    def _disable_all_groups(self):
        for v in self.group_vars.values():
            v.set(False)
        self._on_group_toggle()

    def _render_dashboard(self):
        # Clear existing
        for child in self.inner.winfo_children():
            child.destroy()
        self.row_widgets.clear()

        enabled = {g for g, on in self.group_enabled.items() if on}

        # group -> server -> instances
        by_group: Dict[str, Dict[str, List[InstanceRef]]] = {}
        for inst in self.instances:
            if inst.group not in enabled:
                continue
            by_group.setdefault(inst.group, {}).setdefault(inst.server, []).append(inst)

        for group in sorted(by_group.keys()):
            group_frame = ttk.LabelFrame(self.inner, text=group)
            group_frame.pack(fill="x", padx=8, pady=8)

            for server in sorted(by_group[group].keys()):
                srv_info = self.servers.get(server, {})
                sdm = f"{srv_info.get('sdm_host','?')}:{srv_info.get('sdm_port','?')}"
                header = ttk.Label(group_frame, text=f"{server}  (StrongDM: {sdm})", font=("Segoe UI", 10, "bold"))
                header.pack(anchor="w", padx=10, pady=(8, 4))

                for inst in sorted(by_group[group][server], key=lambda x: x.name):
                    self._add_instance_row(group_frame, inst)

    def _add_instance_row(self, parent, inst: InstanceRef):
        """
        IMPORTANT: UI does NOT show ports.
        Only shows: status, instance name, buttons.
        """
        key = self._key(inst)
        row_bg = "#f7f7f7"

        row = tk.Frame(parent, bd=1, relief="solid", padx=8, pady=6, bg=row_bg)
        row.pack(fill="x", padx=18, pady=4)

        icon_lbl = tk.Label(row, text="•", font=("Segoe UI", 13, "bold"),
                            fg="#666", bg=row_bg, width=2)
        icon_lbl.pack(side="left")

        name_lbl = tk.Label(row, text=inst.name, font=("Segoe UI", 10), bg=row_bg)
        name_lbl.pack(side="left", padx=(6, 12))

        # Spacer pushes buttons to right
        spacer = tk.Label(row, text="", bg=row_bg)
        spacer.pack(side="left", expand=True, fill="x")

        # Buttons
        start_btn = ttk.Button(row, text="START", command=lambda: self.run_action(inst, "START"))
        stop_btn = ttk.Button(row, text="STOP", command=lambda: self.run_action(inst, "STOP"))
        restart_btn = ttk.Button(row, text="RESTART", command=lambda: self.run_action(inst, "RESTART"))

        start_btn.pack(side="right", padx=(6, 0))
        restart_btn.pack(side="right", padx=(6, 0))
        stop_btn.pack(side="right")

        # Disable buttons if not configured
        if not inst.start_cmd:
            start_btn.config(state="disabled")
        if not inst.stop_cmd:
            stop_btn.config(state="disabled")
        if not inst.restart_cmd:
            restart_btn.config(state="disabled")

        # Apply existing status if known
        st = self.status.get(key)
        if st:
            self._apply_status_to_row(key, st["code_int"])

        self.row_widgets[key] = {"row": row, "icon": icon_lbl, "name": name_lbl}

    # ----------------------------
    # Status / Refresh (FAST: 1 SSH per server)
    # ----------------------------
    def refresh_enabled_fast(self):
        user = self._require_user()
        if not user:
            return

        enabled_groups = {g for g, on in self.group_enabled.items() if on}
        targets = [inst for inst in self.instances if inst.group in enabled_groups]

        if not targets:
            self.status_lbl.config(text="No enabled groups", foreground="#b36b00")
            return

        # group by server → 1 SSH per server
        by_server: Dict[str, List[InstanceRef]] = {}
        for inst in targets:
            by_server.setdefault(inst.server, []).append(inst)

        self.status_lbl.config(text="Refreshing...", foreground="blue")

        for server_name, inst_list in by_server.items():
            threading.Thread(
                target=self._refresh_server_worker,
                args=(user, server_name, inst_list),
                daemon=True
            ).start()

    def _build_remote_multi_check_cmd(self, inst_list: List[InstanceRef]) -> str:
        """
        Remote bash prints:
          name|code
        (Ports are internal; not shown in UI.)
        """
        curl_timeout = self.curl_timeout

        lines = []
        lines.append("set -euo pipefail")
        lines.append(
            "check_one(){ name=\"$1\"; port=\"$2\"; path=\"$3\"; "
            f"code=$(curl -s -o /dev/null -w '%{{http_code}}' --max-time {curl_timeout} "
            "\"http://127.0.0.1:${port}${path}\" 2>/dev/null || echo 000); "
            "echo \"${name}|${code}\"; }"
        )

        for inst in inst_list:
            path = inst.path or "/"
            if not path.startswith("/"):
                path = "/" + path
            # Note: port/path used internally only
            lines.append(f"check_one {shlex.quote(inst.name)} {shlex.quote(str(inst.http_port))} {shlex.quote(path)}")

        script = "\n".join(lines)
        return "bash -lc " + shlex.quote(script)

    def _refresh_server_worker(self, user: str, server_name: str, inst_list: List[InstanceRef]):
        srv = self.servers.get(server_name, {})
        sdm_host = srv.get("sdm_host")
        sdm_port = int(srv.get("sdm_port"))

        remote_cmd = self._build_remote_multi_check_cmd(inst_list)
        overall_timeout = self.ssh_connect_timeout + (len(inst_list) * self.curl_timeout) + 20

        rc, out, elapsed = ssh_run(user, sdm_host, sdm_port, remote_cmd,
                                   self.ssh_connect_timeout, overall_timeout)

        if rc != 0:
            # mark all down on this server
            for inst in inst_list:
                self.q.put(("status", inst, "000", out, elapsed))
            return

        inst_by_name = {i.name: i for i in inst_list}
        for line in out.splitlines():
            line = line.strip()
            if not line or "|" not in line:
                continue
            name, code = line.split("|", 1)
            inst = inst_by_name.get(name)
            if not inst:
                continue
            self.q.put(("status", inst, code.strip(), "", 0.0))

    def _apply_status_to_row(self, key: str, code_int: int):
        w = self.row_widgets.get(key)
        if not w:
            return
        state, fg, icon, bg = classify_http(code_int, self.ok_codes)
        w["row"].config(bg=bg)
        w["icon"].config(text=icon, fg=fg, bg=bg)
        w["name"].config(bg=bg)

    # ----------------------------
    # Actions (Start/Stop/Restart)
    # ----------------------------
    def run_action(self, inst: InstanceRef, action: str):
        user = self._require_user()
        if not user:
            return

        cmd = ""
        if action == "START":
            cmd = inst.start_cmd
        elif action == "STOP":
            cmd = inst.stop_cmd
        elif action == "RESTART":
            cmd = inst.restart_cmd

        if not cmd:
            messagebox.showwarning("Not configured", f"{action} command not configured for this instance.")
            return

        if not messagebox.askyesno("Confirm", f"{action} {inst.name} on {inst.server}?"):
            return

        self.status_lbl.config(text=f"{action} running...", foreground="blue")

        threading.Thread(
            target=self._action_worker,
            args=(user, inst, action, cmd),
            daemon=True
        ).start()

    def _action_worker(self, user: str, inst: InstanceRef, action: str, cmd: str):
        srv = self.servers.get(inst.server, {})
        sdm_host = srv.get("sdm_host")
        sdm_port = int(srv.get("sdm_port"))

        # Run under bash -lc
        remote_cmd = "bash -lc " + shlex.quote(cmd)

        # Wrapper scripts may sleep 60s (your DSSOA wrapper), so allow a longer timeout
        overall_timeout = max(60, self.ssh_connect_timeout + 240)

        rc, out, elapsed = ssh_run(user, sdm_host, sdm_port, remote_cmd,
                                   self.ssh_connect_timeout, overall_timeout)

        result = "SUCCESS" if rc == 0 else "FAILED"
        record = {
            "time": now_ts(),
            "user": user,
            "server": inst.server,
            "instance": inst.name,
            "action": action,
            "result": result,
            "rc": rc,
            "elapsed": f"{elapsed:.2f}s",
            "details": (out or "")[:800]
        }
        self.history.append(record)
        self.q.put(("history", record))

        # After action, refresh status for this instance only
        code = self._check_one(user, inst)
        self.q.put(("status", inst, code, "", 0.0))

    def _check_one(self, user: str, inst: InstanceRef) -> str:
        srv = self.servers.get(inst.server, {})
        sdm_host = srv.get("sdm_host")
        sdm_port = int(srv.get("sdm_port"))

        path = inst.path or "/"
        if not path.startswith("/"):
            path = "/" + path

        inner = (
            f"curl -s -o /dev/null -w '%{{http_code}}' --max-time {self.curl_timeout} "
            f"http://127.0.0.1:{inst.http_port}{path} 2>/dev/null || echo 000"
        )
        remote_cmd = "bash -lc " + shlex.quote(inner)
        overall_timeout = self.ssh_connect_timeout + self.curl_timeout + 12
        rc, out, _ = ssh_run(user, sdm_host, sdm_port, remote_cmd,
                             self.ssh_connect_timeout, overall_timeout)
        if rc != 0:
            return "000"
        return out.strip() if out.strip() else "000"

    # ----------------------------
    # Queue processing
    # ----------------------------
    def _process_queue(self):
        try:
            while True:
                msg = self.q.get_nowait()

                if msg[0] == "status":
                    _, inst, code_str, err, elapsed = msg
                    key = self._key(inst)

                    code_str = str(code_str).strip()
                    code_int = int(code_str) if code_str.isdigit() else 0
                    if code_int == 0:
                        code_int = 0

                    self.status[key] = {"code_str": code_str, "code_int": code_int, "ts": now_ts()}
                    self._apply_status_to_row(key, code_int)

                    self.status_lbl.config(text="Updated", foreground="green")

                elif msg[0] == "history":
                    _, record = msg
                    self.hist_text.configure(state="normal")
                    self.hist_text.insert("end", json.dumps(record) + "\n")
                    self.hist_text.see("end")
                    self.hist_text.configure(state="disabled")
                    self.status_lbl.config(text="Ready", foreground="green")

        except queue.Empty:
            pass

        self.after(150, self._process_queue)


if __name__ == "__main__":
    try:
        JwsConsole().mainloop()
    except Exception as e:
        messagebox.showerror("Startup error", str(e))
        raise
