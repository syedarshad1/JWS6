#!/usr/bin/env python3
import os, sys, time, json, threading, subprocess, webbrowser, getpass, shlex
from datetime import datetime
from http.server import HTTPServer, BaseHTTPRequestHandler

try:
    import yaml
except ImportError:
    print("ERROR: PyYAML not found. Install with: pip install PyYAML")
    sys.exit(1)

APP_DIR = os.path.dirname(os.path.abspath(__file__))
CONFIG_FILE = os.path.join(APP_DIR, "targets.yaml")

if not os.path.exists(CONFIG_FILE):
    print(f"ERROR: targets.yaml not found in {APP_DIR}")
    sys.exit(1)

with open(CONFIG_FILE, 'r') as f:
    CONFIG = yaml.safe_load(f)

SERVERS = CONFIG.get('servers', {})
APPS = CONFIG.get('apps', [])
SETTINGS = CONFIG.get('settings', {})

GROUPS = set()
INSTANCES = []
STATUS = {}

for app in APPS:
    group = app.get('group', 'Unknown')
    GROUPS.add(group)
    for inst in app.get('instances', []):
        inst_key = f"{group}|{inst.get('server')}|{inst.get('name')}"
        server_info = SERVERS.get(inst.get('server'), {})
        
        INSTANCES.append({
            'group': group,
            'server': inst.get('server'),
            'name': inst.get('name'),
            'http_port': inst.get('http_port'),
            'path': inst.get('path', '/'),
            'server_ip': server_info.get('server_ip', '127.0.0.1'),
            'sdm_host': server_info.get('sdm_host', '127.0.0.5'),
            'sdm_port': server_info.get('sdm_port', 22087),
            'start_cmd': inst.get('start_cmd', ''),
            'stop_cmd': inst.get('stop_cmd', ''),
            'restart_cmd': inst.get('restart_cmd', ''),
            'status': {'state': 'CHECKING', 'color': 'unknown', 'icon': '...', 'code': 0}
        })
        STATUS[inst_key] = {'state': 'CHECKING', 'color': 'unknown', 'icon': '...', 'code': 0}

GROUPS = sorted(list(GROUPS))

def get_user():
    try:
        return getpass.getuser()
    except:
        return ""

CURRENT_USER = get_user()

def check_jvm_status_ssh(user, sdm_host, sdm_port, server_ip, http_port, path):
    """Check JVM status via SSH tunnel through StrongDM"""
    try:
        # Build curl command to run on remote server
        curl_cmd = f"curl -s -o /dev/null -w '%{{http_code}}' --max-time 2 http://{server_ip}:{http_port}{path}"
        
        # SSH command to execute curl on remote
        ssh_cmd = f"ssh -p {sdm_port} -o BatchMode=yes -o ConnectTimeout=5 -o StrictHostKeyChecking=accept-new {user}@{sdm_host} {shlex.quote(curl_cmd)}"
        
        result = subprocess.run(ssh_cmd, shell=True, capture_output=True, text=True, timeout=15)
        
        if result.returncode != 0:
            return {'state': 'DOWN', 'color': 'down', 'icon': '❌', 'code': 0}
        
        try:
            code = int(result.stdout.strip())
        except:
            return {'state': 'UNKNOWN', 'color': 'unknown', 'icon': '❓', 'code': 0}
        
        if code in [200, 302, 401, 403]:
            return {'state': 'UP', 'color': 'up', 'icon': '✅', 'code': code}
        elif 500 <= code <= 599:
            return {'state': 'UNHEALTHY', 'color': 'warn', 'icon': '⚠️', 'code': code}
        elif 400 <= code <= 499:
            return {'state': 'WARN', 'color': 'warn', 'icon': '⚠️', 'code': code}
        else:
            return {'state': 'UNKNOWN', 'color': 'unknown', 'icon': '❓', 'code': code}
    except Exception as e:
        return {'state': 'DOWN', 'color': 'down', 'icon': '❌', 'code': 0}

def refresh_all_status(user):
    """Refresh status for all instances via SSH"""
    if not user:
        return
    
    for inst in INSTANCES:
        inst_key = f"{inst['group']}|{inst['server']}|{inst['name']}"
        status = check_jvm_status_ssh(user, inst['sdm_host'], inst['sdm_port'], 
                                      inst['server_ip'], inst['http_port'], inst['path'])
        STATUS[inst_key] = status
        inst['status'] = status

def ssh_run(user, sdm_host, sdm_port, cmd):
    """Run command via SSH"""
    try:
        ssh_cmd = f"ssh -p {sdm_port} -o BatchMode=yes -o StrictHostKeyChecking=accept-new {user}@{sdm_host} {shlex.quote(cmd)}"
        result = subprocess.run(ssh_cmd, shell=True, capture_output=True, text=True, timeout=300)
        return result.returncode == 0, result.stdout + result.stderr
    except Exception as e:
        return False, str(e)

class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path == "/" or self.path == "/index.html":
            html = self.generate_html()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.end_headers()
            self.wfile.write(html.encode('utf-8'))
        elif self.path == "/api/config":
            self.json_response({"groups": GROUPS, "servers": SERVERS})
        elif self.path == "/api/instances":
            result = []
            for inst in INSTANCES:
                inst_copy = inst.copy()
                inst_key = f"{inst['group']}|{inst['server']}|{inst['name']}"
                inst_copy['status'] = STATUS.get(inst_key, inst['status'])
                result.append(inst_copy)
            self.json_response(result)
        elif self.path == "/api/history":
            self.json_response([])
        elif self.path == "/api/refresh":
            import urllib.parse
            parsed = urllib.parse.urlparse(self.path)
            params = urllib.parse.parse_qs(parsed.query)
            user = params.get('user', [''])[0]
            if user:
                threading.Thread(target=refresh_all_status, args=(user,), daemon=True).start()
            self.json_response({"status": "refreshing"})
        else:
            self.send_error(404)

    def do_POST(self):
        content_length = int(self.headers.get('Content-Length', 0))
        body = self.rfile.read(content_length).decode('utf-8')
        try:
            data = json.loads(body)
        except:
            self.json_error("Invalid JSON", 400)
            return

        if self.path == "/api/action":
            user = data.get('user', '')
            action = data.get('action', '').upper()
            group = data.get('group', '')
            server = data.get('server', '')
            name = data.get('name', '')
            
            inst = None
            for i in INSTANCES:
                if i['group'] == group and i['server'] == server and i['name'] == name:
                    inst = i
                    break
            
            if not inst:
                self.json_error("Instance not found", 404)
                return
            
            if action == 'START':
                cmd = inst['start_cmd']
            elif action == 'STOP':
                cmd = inst['stop_cmd']
            elif action == 'RESTART':
                cmd = inst['restart_cmd']
            else:
                self.json_error("Invalid action", 400)
                return
            
            if not cmd:
                self.json_error(f"{action} not configured", 400)
                return
            
            def execute():
                success, output = ssh_run(user, inst['sdm_host'], inst['sdm_port'], cmd)
                time.sleep(2)
                status = check_jvm_status_ssh(user, inst['sdm_host'], inst['sdm_port'],
                                             inst['server_ip'], inst['http_port'], inst['path'])
                inst_key = f"{group}|{server}|{name}"
                STATUS[inst_key] = status
                inst['status'] = status
            
            threading.Thread(target=execute, daemon=True).start()
            self.json_response({"status": "executing"})
        else:
            self.send_error(404)

    def json_response(self, data):
        response = json.dumps(data).encode('utf-8')
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(response)

    def json_error(self, msg, code):
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(json.dumps({"error": msg}).encode('utf-8'))

    def log_message(self, format, *args):
        pass

    def generate_html(self):
        groups_json = json.dumps(GROUPS)
        instances_json = json.dumps([{
            'group': i['group'],
            'server': i['server'],
            'name': i['name'],
            'http_port': i['http_port'],
            'path': i['path'],
            'server_ip': i['server_ip'],
            'start_cmd': i['start_cmd'],
            'stop_cmd': i['stop_cmd'],
            'restart_cmd': i['restart_cmd'],
            'status': STATUS.get(f"{i['group']}|{i['server']}|{i['name']}", i['status'])
        } for i in INSTANCES])
        servers_json = json.dumps(SERVERS)
        user_val = CURRENT_USER

        html = f'''<!DOCTYPE html>
<html>
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>JWS6 Console</title>
    <style>
        * {{ margin: 0; padding: 0; box-sizing: border-box; }}
        body {{ font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif; background: #f5f5f5; color: #333; font-size: 13px; }}
        .container {{ max-width: 1600px; margin: 0 auto; padding: 20px; }}
        header {{ background: linear-gradient(135deg, #1e3a8a 0%, #2563eb 100%); color: white; padding: 20px; border-radius: 8px; margin-bottom: 30px; }}
        h1 {{ font-size: 24px; margin-bottom: 10px; }}
        .header-info {{ font-size: 12px; opacity: 0.9; margin-bottom: 10px; }}
        .controls {{ display: flex; gap: 12px; margin-top: 15px; flex-wrap: wrap; align-items: center; }}
        .username-input {{ padding: 6px 10px; border: 1px solid rgba(255,255,255,0.3); border-radius: 4px; background: rgba(255,255,255,0.1); color: white; font-size: 12px; min-width: 200px; }}
        button {{ padding: 6px 12px; border: none; border-radius: 4px; cursor: pointer; font-size: 12px; font-weight: 500; }}
        .btn-secondary {{ background: rgba(255,255,255,0.2); color: white; border: 1px solid rgba(255,255,255,0.3); }}
        .btn-secondary:hover {{ background: rgba(255,255,255,0.3); }}
        .status-message {{ padding: 6px 10px; border-radius: 4px; font-size: 11px; background: rgba(16,185,129,0.2); color: #059669; }}
        .group-toggles {{ background: white; border-radius: 8px; border: 1px solid #e5e7eb; padding: 12px; margin-bottom: 20px; }}
        .group-toggles h3 {{ font-size: 12px; font-weight: 600; margin-bottom: 10px; }}
        .group-toggle-item {{ display: inline-flex; align-items: center; gap: 6px; margin-right: 12px; margin-bottom: 6px; padding: 4px 8px; background: #f3f4f6; border-radius: 4px; }}
        .group-toggle-item input {{ cursor: pointer; width: 16px; height: 16px; }}
        .group-toggle-item label {{ cursor: pointer; font-size: 12px; margin: 0; }}
        .tabs {{ display: flex; border-bottom: 1px solid #e5e7eb; margin-bottom: 20px; }}
        .tab {{ padding: 10px 16px; border: none; background: none; cursor: pointer; font-size: 12px; font-weight: 500; color: #6b7280; border-bottom: 3px solid transparent; }}
        .tab.active {{ color: #2563eb; border-bottom-color: #2563eb; }}
        .tab-content {{ display: none; }}
        .tab-content.active {{ display: block; }}
        .dashboard {{ display: grid; grid-template-columns: 1fr; gap: 20px; }}
        .group-section {{ background: white; border-radius: 8px; border: 1px solid #e5e7eb; overflow: hidden; }}
        .group-header {{ background: #f3f4f6; padding: 12px 16px; border-bottom: 2px solid #d1d5db; font-size: 13px; font-weight: 600; }}
        .server-block {{ border-top: 1px solid #e5e7eb; padding: 12px 16px; }}
        .server-block:first-child {{ border-top: none; }}
        .server-name {{ font-size: 11px; font-weight: 600; color: #6b7280; text-transform: uppercase; margin-bottom: 8px; }}
        .server-meta {{ font-size: 10px; color: #9ca3af; margin-left: 20px; margin-bottom: 8px; }}
        .instance-row {{ display: grid; grid-template-columns: auto 1fr auto auto auto auto auto; gap: 10px; align-items: center; padding: 10px; background: #fafafa; border-radius: 6px; margin-bottom: 6px; border-left: 2px solid #e5e7eb; }}
        .instance-icon {{ font-size: 16px; width: 20px; text-align: center; }}
        .instance-name {{ font-size: 12px; font-weight: 500; }}
        .status-badge {{ display: inline-flex; padding: 2px 8px; border-radius: 12px; font-size: 11px; font-weight: 600; min-width: 70px; justify-content: center; }}
        .status-up {{ background: #d1fae5; color: #047857; }}
        .status-down {{ background: #fee2e2; color: #991b1b; }}
        .status-warn {{ background: #fef3c7; color: #b45309; }}
        .status-unknown {{ background: #f3f4f6; color: #6b7280; }}
        .instance-buttons {{ display: flex; gap: 4px; }}
        .btn-small {{ padding: 4px 8px; font-size: 11px; border-radius: 3px; border: 1px solid #d1d5db; background: white; color: #374151; cursor: pointer; }}
        .btn-small:hover {{ background: #f3f4f6; }}
        .btn-small:disabled {{ opacity: 0.5; cursor: not-allowed; }}
        .btn-refresh {{ padding: 3px 6px; font-size: 10px; background: #e0f2fe; color: #0369a1; border: 1px solid #bae6fd; }}
        .coming-soon {{ background: white; border-radius: 8px; border: 1px solid #e5e7eb; padding: 40px; text-align: center; color: #9ca3af; }}
        .history-view {{ background: white; border-radius: 8px; border: 1px solid #e5e7eb; padding: 16px; }}
        .history-item {{ padding: 10px; border-left: 2px solid #e5e7eb; margin-bottom: 10px; background: #fafafa; border-radius: 4px; font-size: 12px; }}
        .modal {{ display: none; position: fixed; top: 0; left: 0; right: 0; bottom: 0; background: rgba(0,0,0,0.5); z-index: 1000; align-items: center; justify-content: center; }}
        .modal.active {{ display: flex; }}
        .modal-content {{ background: white; border-radius: 8px; padding: 20px; max-width: 350px; }}
        .modal-title {{ font-size: 15px; font-weight: 600; margin-bottom: 10px; }}
        .modal-text {{ font-size: 13px; color: #6b7280; margin-bottom: 16px; }}
        .modal-buttons {{ display: flex; gap: 10px; justify-content: flex-end; }}
        .btn-danger {{ background: #ef4444; color: white; }}
    </style>
</head>
<body>
    <div class="container">
        <header>
            <h1>🖥️ JWS6 Console</h1>
            <div class="header-info">{len(INSTANCES)} instances | {len(GROUPS)} groups</div>
            <div class="controls">
                <input type="text" id="username" class="username-input" placeholder="SSH Username" value="{user_val}" autocomplete="off">
                <button class="btn-secondary" onclick="refreshAll()">🔄 Refresh All</button>
                <div class="status-message" id="status">Ready</div>
            </div>
        </header>

        <div class="group-toggles">
            <h3>📊 App Groups:</h3>
            <div id="group-checkboxes"></div>
        </div>

        <div class="tabs">
            <button class="tab active" onclick="switchTab('readiness')">🔍 Readiness</button>
            <button class="tab" onclick="switchTab('prod')">🚀 Production</button>
            <button class="tab" onclick="switchTab('history')">📜 History</button>
        </div>

        <div id="readiness" class="tab-content active">
            <div class="dashboard" id="dashboard"></div>
        </div>

        <div id="prod" class="tab-content">
            <div class="coming-soon">🚀 Coming Soon...</div>
        </div>

        <div id="history" class="tab-content">
            <div class="history-view"><div id="history-content">No history</div></div>
        </div>
    </div>

    <div class="modal" id="confirm-modal">
        <div class="modal-content">
            <div class="modal-title" id="modal-title">Confirm</div>
            <div class="modal-text" id="modal-text">OK?</div>
            <div class="modal-buttons">
                <button class="btn-secondary" onclick="cancelAction()">Cancel</button>
                <button class="btn-danger" onclick="confirmAction()">Confirm</button>
            </div>
        </div>
    </div>

    <script>
        const GROUPS = {groups_json};
        const INSTANCES = {instances_json};
        const SERVERS = {servers_json};

        let pendingAction = null;
        let enabledGroups = new Set(GROUPS);

        function setStatus(msg) {{
            document.getElementById('status').textContent = msg;
        }}

        function initGroups() {{
            const container = document.getElementById('group-checkboxes');
            GROUPS.forEach(group => {{
                const item = document.createElement('div');
                item.className = 'group-toggle-item';
                
                const cb = document.createElement('input');
                cb.type = 'checkbox';
                cb.id = 'g-' + group;
                cb.checked = true;
                cb.onchange = () => {{
                    if (cb.checked) enabledGroups.add(group);
                    else enabledGroups.delete(group);
                    render();
                }};
                
                const label = document.createElement('label');
                label.htmlFor = 'g-' + group;
                label.textContent = group;
                
                item.appendChild(cb);
                item.appendChild(label);
                container.appendChild(item);
            }});
        }}

        function render() {{
            const dashboard = document.getElementById('dashboard');
            dashboard.innerHTML = '';
            
            const grouped = {{}};
            INSTANCES.forEach(inst => {{
                if (!enabledGroups.has(inst.group)) return;
                if (!grouped[inst.group]) grouped[inst.group] = {{}};
                if (!grouped[inst.group][inst.server]) grouped[inst.group][inst.server] = [];
                grouped[inst.group][inst.server].push(inst);
            }});
            
            Object.keys(grouped).forEach(group => {{
                const groupEl = document.createElement('div');
                groupEl.className = 'group-section';
                
                const header = document.createElement('div');
                header.className = 'group-header';
                header.textContent = group;
                groupEl.appendChild(header);
                
                Object.keys(grouped[group]).forEach(server => {{
                    const serverEl = document.createElement('div');
                    serverEl.className = 'server-block';
                    
                    const srvName = document.createElement('div');
                    srvName.className = 'server-name';
                    srvName.textContent = '📡 ' + server;
                    serverEl.appendChild(srvName);
                    
                    const srvMeta = document.createElement('div');
                    srvMeta.className = 'server-meta';
                    const srv = SERVERS[server] || {{}};
                    srvMeta.textContent = srv.server_ip + ':' + srv.sdm_port;
                    serverEl.appendChild(srvMeta);
                    
                    grouped[group][server].forEach(inst => {{
                        const row = document.createElement('div');
                        row.className = 'instance-row';
                        const status = inst.status || {{}};
                        const icon = status.icon || '❓';
                        const color = status.color || 'unknown';
                        const state = status.state || 'UNKNOWN';
                        
                        row.innerHTML = `
                            <div class="instance-icon">${{icon}}</div>
                            <div class="instance-name">${{inst.name}}</div>
                            <div class="status-badge status-${{color}}">${{state}}</div>
                            <button class="btn-small" onclick="doAction('${{group}}', '${{server}}', '${{inst.name}}', 'START')" ${{inst.start_cmd ? '' : 'disabled'}}>START</button>
                            <button class="btn-small" onclick="doAction('${{group}}', '${{server}}', '${{inst.name}}', 'STOP')" ${{inst.stop_cmd ? '' : 'disabled'}}>STOP</button>
                            <button class="btn-small" onclick="doAction('${{group}}', '${{server}}', '${{inst.name}}', 'RESTART')" ${{inst.restart_cmd ? '' : 'disabled'}}>RESTART</button>
                            <button class="btn-small btn-refresh" onclick="refreshOne('${{group}}', '${{server}}', '${{inst.name}}')">🔄</button>
                        `;
                        serverEl.appendChild(row);
                    }});
                    
                    groupEl.appendChild(serverEl);
                }});
                
                dashboard.appendChild(groupEl);
            }});
        }}

        function doAction(group, server, name, action) {{
            const user = document.getElementById('username').value.trim();
            if (!user) {{ alert('Enter username'); return; }}
            
            document.getElementById('modal-title').textContent = action;
            document.getElementById('modal-text').textContent = action + ' ' + name + '?';
            pendingAction = {{group, server, name, action, user}};
            document.getElementById('confirm-modal').classList.add('active');
        }}

        function confirmAction() {{
            if (!pendingAction) return;
            document.getElementById('confirm-modal').classList.remove('active');
            setStatus('Executing...');
            
            fetch('/api/action', {{
                method: 'POST',
                headers: {{'Content-Type': 'application/json'}},
                body: JSON.stringify(pendingAction)
            }}).then(r => r.json()).then(data => {{
                setStatus('Done');
                setTimeout(() => refreshOne(pendingAction.group, pendingAction.server, pendingAction.name), 2000);
            }}).catch(e => {{
                setStatus('Error');
            }});
        }}

        function cancelAction() {{
            pendingAction = null;
            document.getElementById('confirm-modal').classList.remove('active');
        }}

        function refreshOne(group, server, name) {{
            const user = document.getElementById('username').value.trim();
            if (!user) return;
            
            setStatus('Refreshing ' + name + '...');
            fetch('/api/instances').then(r => r.json()).then(data => {{
                INSTANCES.length = 0;
                data.forEach(i => INSTANCES.push(i));
                render();
                setStatus('Ready');
            }}).catch(e => {{
                setStatus('Error');
            }});
        }}

        function refreshAll() {{
            const user = document.getElementById('username').value.trim();
            if (!user) {{ alert('Enter username'); return; }}
            
            setStatus('Refreshing all...');
            fetch('/api/refresh?user=' + encodeURIComponent(user)).then(r => r.json()).then(data => {{
                setTimeout(() => {{
                    fetch('/api/instances').then(r => r.json()).then(data => {{
                        INSTANCES.length = 0;
                        data.forEach(i => INSTANCES.push(i));
                        render();
                        setStatus('Ready');
                    }});
                }}, 2000);
            }}).catch(e => {{
                setStatus('Error');
            }});
        }}

        function switchTab(tab) {{
            document.querySelectorAll('.tab').forEach(t => t.classList.remove('active'));
            document.querySelectorAll('.tab-content').forEach(c => c.classList.remove('active'));
            event.target.classList.add('active');
            document.getElementById(tab).classList.add('active');
        }}

        initGroups();
        render();
    </script>
</body>
</html>'''
        return html

if __name__ == "__main__":
    try:
        print(f"✓ Loaded {len(GROUPS)} groups, {len(INSTANCES)} instances")
        host = SETTINGS.get("console_host", "127.0.0.1")
        port = int(SETTINGS.get("console_port", 5000))
        
        print("=" * 70)
        print("JWS6 Console")
        print("=" * 70)
        print(f"✓ Starting on http://{host}:{port}")
        print("✓ Status checks via SSH: user@sdm_host curl http://server_ip:http_port")
        print("Press Ctrl+C to stop")
        print("=" * 70)
        
        server = HTTPServer((host, port), Handler)
        
        def open_browser():
            time.sleep(1)
            try:
                webbrowser.open(f"http://{host}:{port}")
            except:
                pass
        
        threading.Thread(target=open_browser, daemon=True).start()
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nStopped")
    except Exception as e:
        print(f"ERROR: {e}")
        import traceback
        traceback.print_exc()
        sys.exit(1)
