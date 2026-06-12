#!/usr/bin/env python3
"""
ui.py
-----
The web UI: HTTP server, all API routes, and the HTML page.
"""

import json
import threading
from http.server import HTTPServer, BaseHTTPRequestHandler
from urllib.parse import urlparse, parse_qs

from config import (
    LOCK, GROUPS, SERVERS, INSTANCES, STATUS, LAST_MESSAGE, OUTPUT_HISTORY, JOBS,
    CURRENT_USER, inst_key, html_escape, push_output, set_last_message,
)
from refresh import refresh_one, refresh_groups
from actions import create_job, run_bulk_job


class Handler(BaseHTTPRequestHandler):

    # ------------------------------------------------------------------ GET
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
                    "message": LAST_MESSAGE.get(inst_key(i["group"], i["server"], i["name"]),
                                                {"ts": "", "level": "info", "text": ""})
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

    # ----------------------------------------------------------------- POST
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

            # QUEUED entry written ONCE here (no duplicates)
            for k in keys:
                push_output(k, f"{action}_QUEUED", f"{action} queued by {user}", None, "", "")
                set_last_message(k, "info", f"⏳ {action} queued")

            job_id = create_job(action, user, keys)
            threading.Thread(target=run_bulk_job, args=(job_id, action), daemon=True).start()
            return self.json_response({"status": "started", "job_id": job_id})

        self.send_error(404)

    # -------------------------------------------------------------- helpers
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

    # ----------------------------------------------------------------- HTML
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

  // UI polling helper so Refresh All updates the page
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


def start_server(host, port):
    """Create and run the HTTP server (blocks until Ctrl+C)."""
    httpd = HTTPServer((host, port), Handler)
    httpd.serve_forever()
