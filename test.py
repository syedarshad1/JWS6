#!/usr/bin/env python3
import os, sys, time, json, shlex, shutil, threading, subprocess, urllib.parse, webbrowser, getpass
from dataclasses import dataclass, asdict
from datetime import datetime
from typing import Dict, List, Any, Tuple
from pathlib import Path
from http.server import HTTPServer, BaseHTTPRequestHandler

try:
    import yaml
except ImportError:
    print("ERROR: PyYAML not found. Install with: pip install PyYAML")
    sys.exit(1)

CONFIG_FILE = "targets.yaml"

def app_dir(): 
    return os.path.dirname(sys.executable) if getattr(sys, "frozen", False) else os.path.dirname(os.path.abspath(__file__))

def now_ts(): 
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")

def get_current_user():
    try: 
        return getpass.getuser()
    except: 
        return ""

def ensure_ssh_available():
    if not shutil.which("ssh"):
        raise RuntimeError("ssh not found in PATH")

def load_yaml_config():
    path = os.path.join(app_dir(), CONFIG_FILE)
    if not os.path.exists(path): 
        raise FileNotFoundError(f"Missing {CONFIG_FILE} in {app_dir()}")
    with open(path, "r", encoding="utf-8") as f: 
        data = yaml.safe_load(f)
    if not isinstance(data, dict) or "servers" not in data or "apps" not in data:
        raise ValueError("Invalid YAML format - must have 'servers' and 'apps' sections")
    if "settings" not in data: 
        data["settings"] = {}
    return data

def ssh_run(user, sdm_host, sdm_port, remote_cmd, connect_timeout, overall_timeout):
    cmd = ["ssh", "-p", str(sdm_port), "-o", "BatchMode=yes", "-o", f"ConnectTimeout={connect_timeout}", "-o", "StrictHostKeyChecking=accept-new", f"{user}@{sdm_host}", remote_cmd]
    start = time.time()
    try:
        cp = subprocess.run(cmd, capture_output=True, text=True, timeout=overall_timeout)
        elapsed = time.time() - start
        out = (cp.stdout or "").strip()
        err = (cp.stderr or "").strip()
        return cp.returncode, out if out else err, elapsed
    except subprocess.TimeoutExpired:
        return 124, f"TIMEOUT", time.time() - start
    except Exception as e:
        return 255, str(e), time.time() - start

def classify_http(code, ok_codes):
    if code in ok_codes: 
        return {"state": "UP", "color": "up", "icon": "✅"}
    if code == 0: 
        return {"state": "DOWN", "color": "down", "icon": "❌"}
    if 500 <= code <= 599: 
        return {"state": "UNHEALTHY", "color": "warn", "icon": "⚠️"}
    if 400 <= code <= 499: 
        return {"state": "WARN", "color": "warn", "icon": "⚠️"}
    return {"state": "UNKNOWN", "color": "unknown", "icon": "❓"}

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
    def to_dict(self): 
        return asdict(self)
    def key(self): 
        return f"{self.group}|{self.server}|{self.name}"

class JWSConsoleApp:
    def __init__(self):
        self.cfg = load_yaml_config()
        settings = self.cfg.get("settings", {})
        self.ok_codes = set(settings.get("http_ok_codes", [200, 302, 401, 403]))
        self.curl_timeout = int(settings.get("curl_timeout_seconds", 1))
        self.ssh_connect_timeout = int(settings.get("ssh_connect_timeout_seconds", 8))
        self.history_file = os.path.join(app_dir(), settings.get("history_file", "history.jsonl"))
        self.servers = self.cfg["servers"]
        self.instances = self._build_instances(self.cfg["apps"])
        self.groups = sorted({i.group for i in self.instances})
        self.status = {}
        self.status_lock = threading.Lock()
        Path(self.history_file).touch(exist_ok=True)
        print(f"✓ Loaded {len(self.groups)} groups, {len(self.instances)} instances")

    def _build_instances(self, apps_cfg):
        out = []
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

    def get_instances_by_group(self):
        result = {}
        for inst in self.instances:
            if inst.group not in result: 
                result[inst.group] = {}
            if inst.server not in result[inst.group]: 
                result[inst.group][inst.server] = []
            status = self.status.get(inst.key(), {"state": "UNKNOWN", "code": 0, "color": "unknown", "icon": "❓"})
            inst_data = inst.to_dict()
            inst_data["status"] = status
            result[inst.group][inst.server].append(inst_data)
        return result

    def append_history(self, record):
        with open(self.history_file, "a", encoding="utf-8") as f: 
            f.write(json.dumps(record) + "\n")

    def read_history(self, limit=100):
        if not os.path.exists(self.history_file): 
            return []
        records = []
        with open(self.history_file, "r", encoding="utf-8") as f:
            for line in f.readlines()[-limit:]:
                try: 
                    records.append(json.loads(line))
                except: 
                    pass
        return records

console = None

HTML_TEMPLATE = """<!DOCTYPE html><html><head><meta charset="UTF-8"><meta name="viewport" content="width=device-width, initial-scale=1.0"><title>JWS6 Console</title><style>*{margin:0;padding:0;box-sizing:border-box}body{font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,"Helvetica Neue",Arial,sans-serif;background:#f5f5f5;color:#333;font-size:13px}.container{max-width:1600px;margin:0 auto;padding:20px}header{background:linear-gradient(135deg,#1e3a8a 0%,#2563eb 100%);color:white;padding:20px;border-radius:8px;margin-bottom:30px;box-shadow:0 2px 8px rgba(0,0,0,0.1)}h1{font-size:24px;margin-bottom:10px;display:flex;align-items:center;gap:10px}.header-info{font-size:12px;opacity:0.9}.controls{display:flex;gap:12px;margin:15px 0 0 0;flex-wrap:wrap;align-items:center}.username-input{padding:6px 10px;border:1px solid rgba(255,255,255,0.3);border-radius:4px;background:rgba(255,255,255,0.1);color:white;font-size:12px;min-width:200px}.username-input::placeholder{color:rgba(255,255,255,0.6)}button{padding:6px 12px;border:none;border-radius:4px;cursor:pointer;font-size:12px;font-weight:500}.btn-secondary{background:rgba(255,255,255,0.2);color:white;border:1px solid rgba(255,255,255,0.3)}.btn-secondary:hover{background:rgba(255,255,255,0.3)}.btn-danger{background:#ef4444;color:white}.btn-danger:hover{background:#dc2626}.status-message{padding:6px 10px;border-radius:4px;font-size:11px;font-weight:500;min-width:100px;text-align:center}.status-message.ready{background:rgba(16,185,129,0.2);color:#059669}.status-message.updating{background:rgba(59,130,246,0.2);color:#1e40af}.status-message.error{background:rgba(239,68,68,0.2);color:#b91c1c}.group-toggles{background:white;border-radius:8px;border:1px solid #e5e7eb;padding:12px;margin-bottom:20px;box-shadow:0 1px 3px rgba(0,0,0,0.05)}.group-toggles h3{font-size:12px;font-weight:600;color:#1f2937;margin-bottom:10px}.group-toggle-item{display:inline-flex;align-items:center;gap:6px;margin-right:12px;margin-bottom:6px;padding:4px 8px;background:#f3f4f6;border-radius:4px;border:1px solid #e5e7eb}.group-toggle-item input{cursor:pointer;width:16px;height:16px}.group-toggle-item label{cursor:pointer;font-size:12px;color:#6b7280;font-weight:500;margin:0}.dashboard{display:grid;grid-template-columns:1fr;gap:20px}.group-section{background:white;border-radius:8px;border:1px solid #e5e7eb;overflow:hidden;box-shadow:0 1px 3px rgba(0,0,0,0.05)}.group-header{background:linear-gradient(135deg,#f3f4f6 0%,#e5e7eb 100%);padding:12px 16px;border-bottom:2px solid #d1d5db;font-size:13px;font-weight:600;color:#1f2937;display:flex;align-items:center;gap:10px}.group-header::before{content:'';display:inline-block;width:3px;height:16px;background:#2563eb;border-radius:2px}.server-block{border-top:1px solid #e5e7eb;padding:12px 16px}.server-block:first-child{border-top:none}.server-name{font-size:11px;font-weight:600;color:#6b7280;text-transform:uppercase;letter-spacing:0.5px;margin-bottom:8px}.server-meta{font-size:10px;color:#9ca3af;margin-left:20px;margin-bottom:8px}.instance-row{display:grid;grid-template-columns:auto 1fr auto auto auto auto auto;gap:10px;align-items:center;padding:10px;background:#fafafa;border-radius:6px;margin-bottom:6px;border-left:2px solid #e5e7eb;font-size:12px}.instance-icon{font-size:16px;width:20px;text-align:center}.instance-name{font-size:12px;font-weight:500;color:#111827}.status-badge{display:inline-flex;padding:2px 8px;border-radius:12px;font-size:11px;font-weight:600;min-width:70px;justify-content:center}.status-up{background:#d1fae5;color:#047857}.status-down{background:#fee2e2;color:#991b1b}.status-warn{background:#fef3c7;color:#b45309}.status-unknown{background:#f3f4f6;color:#6b7280}.instance-buttons{display:flex;gap:4px}.btn-small{padding:4px 8px;font-size:11px;border-radius:3px;border:1px solid #d1d5db;background:white;color:#374151;cursor:pointer;white-space:nowrap}.btn-small:hover{background:#f3f4f6;border-color:#9ca3af}.btn-small:disabled{opacity:0.5;cursor:not-allowed}.btn-refresh{padding:3px 6px;font-size:10px;background:#e0f2fe;color:#0369a1;border:1px solid #bae6fd}.btn-refresh:hover{background:#cffafe}.tabs{display:flex;gap:0;border-bottom:1px solid #e5e7eb;margin-bottom:20px}.tab{padding:10px 16px;border:none;background:none;cursor:pointer;font-size:12px;font-weight:500;color:#6b7280;border-bottom:3px solid transparent;transition:color 0.2s}.tab.active{color:#2563eb;border-bottom-color:#2563eb}.tab:hover{color:#1f2937}.tab-content{display:none}.tab-content.active{display:block}.coming-soon{background:white;border-radius:8px;border:1px solid #e5e7eb;padding:40px 30px;text-align:center;color:#9ca3af;font-size:16px;font-weight:500}.history-view{background:white;border-radius:8px;border:1px solid #e5e7eb;padding:16px}.history-item{padding:10px;border-left:2px solid #e5e7eb;margin-bottom:10px;background:#fafafa;border-radius:4px;font-size:12px}.history-item.success{border-left-color:#10b981;background:#f0fdf4}.history-item.failed{border-left-color:#ef4444;background:#fef2f2}.history-time{font-size:11px;color:#9ca3af;margin-bottom:4px}.history-action{font-weight:600;margin-bottom:3px;font-size:12px}.history-meta{font-size:11px;color:#6b7280}.modal{display:none;position:fixed;top:0;left:0;right:0;bottom:0;background:rgba(0,0,0,0.5);z-index:1000;align-items:center;justify-content:center}.modal.active{display:flex}.modal-content{background:white;border-radius:8px;padding:20px;max-width:350px;box-shadow:0 20px 25px rgba(0,0,0,0.15)}.modal-title{font-size:15px;font-weight:600;margin-bottom:10px;color:#1f2937}.modal-text{font-size:13px;color:#6b7280;margin-bottom:16px}.modal-buttons{display:flex;gap:10px;justify-content:flex-end}</style></head><body><div class="container"><header><h1><span>🖥️</span> JWS6 Console</h1><div class="header-info"><span id="instance-count">Loading...</span> | <span id="group-count">Loading...</span></div><div class="controls"><input type="text" id="username" class="username-input" placeholder="SSH Username" value="USERNAME_PLACEHOLDER" autocomplete="off"><button class="btn-secondary" onclick="app.refresh()">🔄 Refresh All</button><div class="status-message ready" id="status-msg">Ready</div></div></header><div class="group-toggles"><h3>📊 App Groups:</h3><div id="group-checkboxes"></div></div><div class="tabs"><button class="tab active" onclick="switchTab('readiness')">🔍 Readiness</button><button class="tab" onclick="switchTab('prod')">🚀 Production</button><button class="tab" onclick="switchTab('history')">📜 History</button></div><div id="readiness" class="tab-content active"><div class="dashboard" id="dashboard-content"></div></div><div id="prod" class="tab-content"><div class="coming-soon">🚀 Production Environment<br><br>Coming Soon...</div></div><div id="history" class="tab-content"><div class="history-view"><div id="history-content"></div></div></div></div><div class="modal" id="confirm-modal"><div class="modal-content"><div class="modal-title" id="modal-title">Confirm</div><div class="modal-text" id="modal-text">OK?</div><div class="modal-buttons"><button class="btn-secondary" onclick="app.cancelAction()">Cancel</button><button class="btn-danger" id="confirm-btn" onclick="app.confirmAction()">Confirm</button></div></div></div><script>const app={data:null,pendingAction:null,enabledGroups:new Set(),async init(){await this.loadConfig();this.initGroupToggles();await this.loadInstances();this.render();setInterval(()=>{if(document.getElementById('username').value){}},30000)},initGroupToggles(){const container=document.getElementById('group-checkboxes');container.innerHTML='';for(const group of this.config.groups){const item=document.createElement('div');item.className='group-toggle-item';const checkbox=document.createElement('input');checkbox.type='checkbox';checkbox.id='group-'+group;checkbox.checked=true;checkbox.onchange=()=>this.onGroupToggle(group,checkbox.checked);const label=document.createElement('label');label.htmlFor='group-'+group;label.textContent=group;item.appendChild(checkbox);item.appendChild(label);container.appendChild(item);this.enabledGroups.add(group)}},onGroupToggle(group,enabled){if(enabled){this.enabledGroups.add(group)}else{this.enabledGroups.delete(group)}this.render()},async loadConfig(){try{const res=await fetch('/api/config');if(!res.ok)throw new Error('Failed to load config');this.config=await res.json();document.getElementById('group-count').textContent=this.config.groups.length+' groups'}catch(e){console.error('Config error:',e);this.setStatus('error','Failed to load config')}},async loadInstances(){try{const res=await fetch('/api/instances');if(!res.ok)throw new Error('Failed to load instances');this.data=await res.json();const count=Object.values(this.data).reduce((sum,groups)=>sum+Object.values(groups).reduce((s,instances)=>s+instances.length,0),0);document.getElementById('instance-count').textContent=count+' instances'}catch(e){console.error('Instances error:',e);this.setStatus('error','Failed to load instances')}},render(){const dashboard=document.getElementById('dashboard-content');dashboard.innerHTML='';if(!this.data){dashboard.innerHTML='<p style="text-align:center;color:#9ca3af;">No data loaded</p>';return}for(const group in this.data){if(!this.enabledGroups.has(group))continue;const servers=this.data[group];const groupEl=document.createElement('div');groupEl.className='group-section';const header=document.createElement('div');header.className='group-header';header.textContent=group;groupEl.appendChild(header);for(const server in servers){const serverEl=document.createElement('div');serverEl.className='server-block';const serverName=document.createElement('div');serverName.className='server-name';serverName.innerHTML='📡 '+server;serverEl.appendChild(serverName);const serverMeta=document.createElement('div');serverMeta.className='server-meta';const srvInfo=this.config.servers[server]||{};serverMeta.textContent=srvInfo.sdm_host+':'+srvInfo.sdm_port;serverEl.appendChild(serverMeta);for(const inst of servers[server]){const row=this.createInstanceRow(inst);serverEl.appendChild(row)}groupEl.appendChild(serverEl)}dashboard.appendChild(groupEl)}},createInstanceRow(inst){const row=document.createElement('div');row.className='instance-row';const status=inst.status||{state:'UNKNOWN',color:'unknown'};const iconMap={UP:'✅',DOWN:'❌',UNHEALTHY:'⚠️',UNKNOWN:'❓'};row.innerHTML='<div class="instance-icon">'+iconMap[status.state]+'</div><div class="instance-name">'+inst.name+'</div><div class="status-badge status-'+status.color+'">'+status.state+'</div><div class="instance-buttons"><button class="btn-small start" onclick="app.action(\''+inst.group+'\',\''+inst.server+'\',\''+inst.name+'\',\'START\')"'+(inst.start_cmd?'':' disabled')+'>START</button><button class="btn-small stop" onclick="app.action(\''+inst.group+'\',\''+inst.server+'\',\''+inst.name+'\',\'STOP\')"'+(inst.stop_cmd?'':' disabled')+'>STOP</button><button class="btn-small restart" onclick="app.action(\''+inst.group+'\',\''+inst.server+'\',\''+inst.name+'\',\'RESTART\')"'+(inst.restart_cmd?'':' disabled')+'>RESTART</button><button class="btn-small btn-refresh" onclick="app.refreshInstance(\''+inst.group+'\',\''+inst.server+'\',\''+inst.name+'\')">🔄</button></div>';return row},refreshInstance(group,server,name){const username=document.getElementById('username').value.trim();if(!username){alert('Enter username first');return}this.setStatus('updating','Refreshing '+name+'...');for(const i of this.data[group][server]){if(i.name===name){this.checkInstanceStatus(username,i);break}}},checkInstanceStatus(username,inst){const path=inst.path||'/';const url='http://127.0.0.1:'+inst.http_port+path;const timeout=2000;setTimeout(()=>{fetch(url,{signal:AbortSignal.timeout(timeout)}).then(res=>{this.render();this.setStatus('ready','Updated '+inst.name)}).catch(err=>{this.render();this.setStatus('ready','Updated '+inst.name)})},100)},async refresh(){const username=document.getElementById('username').value.trim();if(!username){alert('Enter username first');return}const groups=Array.from(this.enabledGroups);this.setStatus('updating','Refreshing all...');try{const res=await fetch('/api/refresh',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({user:username,groups:groups})});if(!res.ok){const err=await res.json();this.setStatus('error','Error: '+err.error);return}this.pollStatus()}catch(e){this.setStatus('error','Network error')}},async pollStatus(){await new Promise(r=>setTimeout(r,2000));await this.loadInstances();this.render();this.setStatus('ready','Updated all')},action(group,server,name,actionType){const username=document.getElementById('username').value.trim();if(!username){alert('Enter username first');return}const modal=document.getElementById('confirm-modal');document.getElementById('modal-title').textContent='Confirm '+actionType;document.getElementById('modal-text').textContent=actionType+' '+name+' on '+server+'?';this.pendingAction={group,server,name,actionType,username};modal.classList.add('active')},confirmAction(){const action=this.pendingAction;this.executeAction(action);document.getElementById('confirm-modal').classList.remove('active')},cancelAction(){this.pendingAction=null;document.getElementById('confirm-modal').classList.remove('active')},async executeAction(action){this.setStatus('updating','Running '+action.action+'...');try{const res=await fetch('/api/action',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({user:action.username,action:action.actionType,group:action.group,server:action.server,name:action.name})});if(!res.ok){const err=await res.json();this.setStatus('error','Error: '+err.error);return}this.setStatus('ready','Executed');setTimeout(()=>this.refreshInstance(action.group,action.server,action.name),2000)}catch(e){this.setStatus('error','Network error')}},async loadHistory(){try{const res=await fetch('/api/history?limit=50');const history=await res.json();const historyContent=document.getElementById('history-content');historyContent.innerHTML='';if(history.length===0){historyContent.innerHTML='<p style="color:#9ca3af;text-align:center;">No history yet</p>';return}history.reverse().forEach(item=>{const el=document.createElement('div');el.className='history-item '+(item.result==='SUCCESS'?'success':'failed');el.innerHTML='<div class="history-time">'+item.time+'</div><div class="history-action">'+item.action+' '+item.instance+'</div><div class="history-meta">Server: '+item.server+' | User: '+item.user+' | '+item.result+'</div>';historyContent.appendChild(el)})}catch(e){console.error('Failed to load history')}},setStatus(type,text){const msg=document.getElementById('status-msg');msg.className='status-message '+type;msg.textContent=text}};function switchTab(tab){document.querySelectorAll('.tab').forEach(t=>t.classList.remove('active'));document.querySelectorAll('.tab-content').forEach(c=>c.classList.remove('active'));event.target.classList.add('active');document.getElementById(tab).classList.add('active');if(tab==='history')app.loadHistory()}app.init();</script></body></html>"""

class RequestHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path in ("/", "/index.html"): 
            self.serve_html()
        elif self.path == "/api/config": 
            self.json_response({"groups": console.groups, "servers": console.servers})
        elif self.path == "/api/instances": 
            self.json_response(console.get_instances_by_group())
        elif self.path == "/api/history": 
            limit = int(urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query).get("limit", [100])[0])
            self.json_response(console.read_history(limit))
        else: 
            self.send_error(404)

    def do_POST(self):
        content_length = int(self.headers.get("Content-Length", 0))
        body = self.rfile.read(content_length).decode("utf-8")
        try: 
            data = json.loads(body)
        except: 
            self.json_error("Invalid JSON", 400)
            return
        if self.path == "/api/refresh": 
            self.handle_refresh(data)
        elif self.path == "/api/action": 
            self.handle_action(data)
        else: 
            self.send_error(404)

    def handle_refresh(self, data):
        user = data.get("user", "").strip()
        if not user: 
            self.json_error("Username required", 400)
            return
        groups_to_refresh = data.get("groups", console.groups)
        targets = [inst for inst in console.instances if inst.group in groups_to_refresh]
        by_server = {}
        for inst in targets: 
            by_server.setdefault(inst.server, []).append(inst)
        for server_name, inst_list in by_server.items():
            threading.Thread(target=refresh_server_worker, args=(user, server_name, inst_list), daemon=True).start()
        self.json_response({"status": "refreshing"})

    def handle_action(self, data):
        user = data.get("user", "").strip()
        action = data.get("action", "").upper()
        group, server, name = data.get("group"), data.get("server"), data.get("name")
        if not user: 
            self.json_error("Username required", 400)
            return
        inst = None
        for i in console.instances:
            if i.group == group and i.server == server and i.name == name: 
                inst = i
                break
        if not inst: 
            self.json_error("Instance not found", 404)
            return
        cmd = inst.start_cmd if action == "START" else (inst.stop_cmd if action == "STOP" else inst.restart_cmd)
        if not cmd: 
            self.json_error(f"{action} not configured", 400)
            return
        threading.Thread(target=action_worker, args=(user, inst, action, cmd), daemon=True).start()
        self.json_response({"status": "running"})

    def serve_html(self):
        current_user = get_current_user()
        html = HTML_TEMPLATE.replace("USERNAME_PLACEHOLDER", current_user)
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", len(html.encode("utf-8")))
        self.end_headers()
        self.wfile.write(html.encode("utf-8"))

    def json_response(self, data):
        response = json.dumps(data).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Content-Length", len(response))
        self.end_headers()
        self.wfile.write(response)

    def json_error(self, message, code=500):
        response = json.dumps({"error": message}).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Content-Length", len(response))
        self.end_headers()
        self.wfile.write(response)

    def log_message(self, format, *args): 
        pass

def refresh_server_worker(user, server_name, inst_list):
    srv = console.servers.get(server_name, {})
    sdm_host, sdm_port = srv.get("sdm_host"), int(srv.get("sdm_port"))
    remote_cmd = build_remote_multi_check_cmd(inst_list)
    overall_timeout = console.ssh_connect_timeout + (len(inst_list) * console.curl_timeout) + 20
    rc, out, elapsed = ssh_run(user, sdm_host, sdm_port, remote_cmd, console.ssh_connect_timeout, overall_timeout)
    if rc != 0:
        for inst in inst_list:
            with console.status_lock: 
                console.status[inst.key()] = {"state": "DOWN", "code": 0, "color": "down", "icon": "❌"}
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
        try: 
            code_int = int(code.strip())
        except: 
            code_int = 0
        status_info = classify_http(code_int, console.ok_codes)
        status_info["code"] = code_int
        with console.status_lock: 
            console.status[inst.key()] = status_info

def build_remote_multi_check_cmd(inst_list):
    curl_timeout = console.curl_timeout
    lines = ["set -euo pipefail", f"check_one(){{ name=\"$1\"; port=\"$2\"; path=\"$3\"; code=$(curl -s -o /dev/null -w '%{{http_code}}' --max-time {curl_timeout} \"http://127.0.0.1:${{port}}${{path}}\" 2>/dev/null || echo 000); echo \"${{name}}|${{code}}\"; }}"]
    for inst in inst_list:
        path = inst.path or "/"
        if not path.startswith("/"): 
            path = "/" + path
        lines.append(f"check_one {shlex.quote(inst.name)} {shlex.quote(str(inst.http_port))} {shlex.quote(path)}")
    return "bash -lc " + shlex.quote("\n".join(lines))

def action_worker(user, inst, action, cmd):
    srv = console.servers.get(inst.server, {})
    sdm_host, sdm_port = srv.get("sdm_host"), int(srv.get("sdm_port"))
    remote_cmd = "bash -lc " + shlex.quote(cmd)
    overall_timeout = max(60, console.ssh_connect_timeout + 240)
    rc, out, elapsed = ssh_run(user, sdm_host, sdm_port, remote_cmd, console.ssh_connect_timeout, overall_timeout)
    result = "SUCCESS" if rc == 0 else "FAILED"
    record = {"time": now_ts(), "user": user, "server": inst.server, "instance": inst.name, "action": action, "result": result, "rc": rc, "elapsed": f"{elapsed:.2f}s", "details": (out or "")[:800]}
    console.append_history(record)

def main():
    global console
    try:
        ensure_ssh_available()
        console = JWSConsoleApp()
        settings = console.cfg.get("settings", {})
        host = settings.get("console_host", "127.0.0.1")
        port = int(settings.get("console_port", 5000))
        print("=" * 70)
        print("JWS6 Console - Global + Individual Refresh")
        print("=" * 70)
        print(f"✓ Configuration loaded - {len(console.groups)} groups, {len(console.instances)} instances")
        print(f"Starting on http://{host}:{port}")
        print("Opening browser...")
        print("Press Ctrl+C to stop")
        print("=" * 70)
        server = HTTPServer((host, port), RequestHandler)
        def open_browser():
            time.sleep(1)
            try: 
                webbrowser.open(f"http://{host}:{port}")
            except: 
                print(f"Open http://{host}:{port} manually")
        threading.Thread(target=open_browser, daemon=True).start()
        server.serve_forever()
    except KeyboardInterrupt: 
        print("\nStopped")
    except Exception as e: 
        print(f"ERROR: {e}")
        import traceback
        traceback.print_exc()
        sys.exit(1)

if __name__ == "__main__": 
    main()
