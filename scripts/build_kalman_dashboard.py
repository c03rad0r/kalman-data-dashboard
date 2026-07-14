#!/usr/bin/env python3
"""
Build the REAL-TIME Kalman dashboard.
The dashboard subscribes to Nostr kind 31998 events via WebSocket
and updates charts live in the browser.

No server needed — pure client-side JavaScript connecting to relay.
"""

import sqlite3, json, os, math
from datetime import datetime, timezone

DB = os.path.expanduser("~/.hermes/bot/zai_usage.db")
POOL = os.path.expanduser("~/.hermes/state/pool_kalman.json")
OUT = os.path.expanduser("~/nsites/kalman-data/index.html")
NSEC_PUBKEY = "d5c85ab43517b35d374a6360fa58fdc5f807b5331b3df7b155f34290acbb922b"

# Extract historical data for initial render
db = sqlite3.connect(DB)
rows = db.execute("""
    SELECT ts, burn_rate_tph, projected_total_pct, used_pct_observed,
           uncertainty, will_exhaust, velocity_tph2, exhausts_in_hours
    FROM kalman_samples ORDER BY ts DESC LIMIT 200
""").fetchall()
rows.reverse()

anom_rows = db.execute("SELECT ts, severity, title FROM anomaly_events ORDER BY ts DESC LIMIT 10").fetchall()
db.close()

with open(POOL) as f:
    pool = json.load(f)

# Build data arrays
ts_str = [datetime.fromtimestamp(r[0], tz=timezone.utc).strftime("%H:%M") for r in rows]
burn = [min(r[1], 50000) if r[1] else 0 for r in rows]
proj = [r[2] or 0 for r in rows]
used = [r[3] or 0 for r in rows]
uncer = [min(r[4], 50000) if r[4] else 0 for r in rows]
exhaust = [r[5] for r in rows]

anomaly_html = ""
for ts, sev, title in anom_rows:
    c = "#f85149" if sev=="critical" else "#d29922" if sev=="warning" else "#58a6ff"
    anomaly_html += f'<tr><td style="color:{c}">{sev}</td><td>{title}</td><td>{datetime.fromtimestamp(ts,tz=timezone.utc).strftime("%m-%d %H:%M")}</td></tr>'

html = f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>Kalman Filter Monitor — Live</title>
<script src="https://cdn.plot.ly/plotly-2.35.2.min.js"></script>
<style>
body{{font-family:-apple-system,system-ui,sans-serif;margin:0;padding:20px;background:#0d1117;color:#c9d1d9;}}
h1{{color:#58a6ff;font-size:1.4em;}}
h2{{color:#8b949e;font-size:1em;margin:30px 0 8px;}}
.grid{{display:grid;grid-template-columns:repeat(auto-fit,minmax(160px,1fr));gap:12px;margin:16px 0;}}
.card{{background:#161b22;border:1px solid #30363d;border-radius:8px;padding:14px;}}
.card h3{{margin:0 0 4px;color:#58a6ff;font-size:0.75em;text-transform:uppercase;}}
.card .v{{font-size:1.6em;font-weight:bold;}}
.chart{{width:100%;height:380px;margin:8px 0;border:1px solid #30363d;border-radius:8px;}}
.live{{display:inline-block;width:8px;height:8px;border-radius:50%;background:#238636;animation:pulse 2s infinite;margin-right:6px;}}
@keyframes pulse{{0%,100%{{opacity:1;}}50%{{opacity:0.3;}}}}
.note{{color:#8b949e;font-size:0.85em;}}
.badge{{display:inline-block;padding:2px 8px;border-radius:4px;font-size:0.75em;font-weight:bold;}}
.ok{{background:#238636;color:#fff;}}.warn{{background:#d29922;color:#000;}}.crit{{background:#da3633;color:#fff;}}
table{{width:100%;border-collapse:collapse;font-size:0.85em;}}
td,th{{padding:6px 8px;border-bottom:1px solid #30363d;text-align:left;}}
</style>
</head>
<body>

<h1><span class="live"></span>Kalman Filter Monitor — Live</h1>
<p class="note">Real-time burn prediction data via Nostr. Updates every 5 min from <code>relay.ngit.dev</code>.</p>

<div class="grid">
  <div class="card"><h3>Burn Rate</h3><div class="v" id="burnRate" style="color:#7ee787">...</div><div class="note">tokens/hour</div></div>
  <div class="card"><h3>Predicted Total</h3><div class="v" id="projTotal" style="color:#58a6ff">...</div><div class="note">% of quota</div></div>
  <div class="card"><h3>Pool State</h3><div class="v" id="poolX" style="color:#d29922">...</div><div class="note">burn estimate</div></div>
  <div class="card"><h3>Exhaustion</h3><div class="v" id="exhaust" style="color:#8b949e">...</div><div class="note">hours left</div></div>
  <div class="card"><h3>Workers</h3><div class="v" id="workers" style="color:#c9d1d9">...</div><div class="note">active</div></div>
  <div class="card"><h3>Last Update</h3><div class="v" id="lastUpdate" style="color:#8b949e;font-size:1.1em">...</div><div class="note">UTC</div></div>
</div>

<h2>Burn Rate (200 samples + live)</h2>
<div class="chart" id="c1"></div>

<h2>Predicted vs Actual Usage</h2>
<div class="chart" id="c2"></div>

<h2>Recent Anomalies</h2>
<table><tr><th>Severity</th><th>Title</th><th>Time</th></tr>
{anomaly_html}
</table>

<p style="margin-top:40px;padding-top:16px;border-top:1px solid #30363d;color:#8b949e;font-size:0.8em;">
<span class="live"></span>Live data via Nostr kind 31998 | Publisher: kalman_telemetry_publisher.py (cron 5min) | <span id="connStatus">connecting...</span>
</p>

<script>
const PUBKEY = "{NSEC_PUBKEY}";
const RELAY = "wss://relay.ngit.dev";
const initialTs = {json.dumps(ts_str)};
const initialBurn = {json.dumps(burn)};
const initialProj = {json.dumps(proj)};
const initialUsed = {json.dumps(used)};

const dark = {{paper_bgcolor:'#161b22',plot_bgcolor:'#0d1117',font:{{color:'#c9d1d9',size:10}},margin:{{t:30,b:35,l:50,r:15}}}};
const ax = {{gridcolor:'#30363d'}};

// Initial charts with historical data
Plotly.newPlot('c1',[{{x:initialTs,y:initialBurn,mode:'markers',marker:{{size:3,color:'#58a6ff'}},name:'Historical'}}],
  {{...dark,title:'Burn Rate (tokens/hr)',xaxis:ax,yaxis:{{...ax,title:'tokens/hr'}}}},{{responsive:true}});

Plotly.newPlot('c2',[
  {{x:initialTs,y:initialProj,mode:'lines',name:'Predicted %',line:{{color:'#58a6ff'}}}},
  {{x:initialTs,y:initialUsed,mode:'markers',marker:{{size:2,color:'#d29922'}},name:'Observed %'}}
],{{...dark,title:'Predicted vs Actual',xaxis:ax,yaxis:{{...ax,title:'%'}}}},{{responsive:true}});

// Live update cards
function updateCards(data) {{
  document.getElementById('burnRate').textContent = data.kalman.burn_rate ? (data.kalman.burn_rate/1000).toFixed(1)+'k' : '—';
  document.getElementById('projTotal').textContent = data.kalman.projected_total_pct ? data.kalman.projected_total_pct.toFixed(1)+'%' : '—';
  document.getElementById('poolX').textContent = data.pool.x[0] ? data.pool.x[0].toFixed(2) : '—';
  document.getElementById('exhaust').textContent = data.kalman.exhausts_in_hours ? data.kalman.exhausts_in_hours.toFixed(1)+'h' : 'safe';
  document.getElementById('exhaust').style.color = data.kalman.will_exhaust ? '#f85149' : '#7ee787';
  document.getElementById('workers').textContent = data.system.workers || '—';
  document.getElementById('lastUpdate').textContent = new Date(data.timestamp).toLocaleTimeString('en-GB') + ' UTC';
  
  // Append to charts
  const now = new Date().toLocaleTimeString('en-GB',{{hour:'2-digit',minute:'2-digit'}});
  Plotly.extendTraces('c1',{{x:[[now]],y:[[data.kalman.burn_rate||0]]}},[0]);
  Plotly.extendTraces('c2',{{x:[[now],[now]],y:[[data.kalman.projected_total_pct||0],[data.kalman.used_pct||0]]}},[0,1]);
}}

// WebSocket to Nostr relay
let ws;
function connect() {{
  document.getElementById('connStatus').textContent = 'connecting to ' + RELAY + '...';
  ws = new WebSocket(RELAY);
  
  ws.onopen = () => {{
    document.getElementById('connStatus').innerHTML = '<span style="color:#7ee787">connected</span>';
    // Subscribe to kind 31998 from our pubkey
    ws.send(JSON.stringify(["REQ","kalman-live",{{"kinds":[31998],"authors":[PUBKEY],"limit":1}}]));
  }};
  
  ws.onmessage = (event) => {{
    const msg = JSON.parse(event.data);
    if (msg[0] === "EVENT" && msg[1] === "kalman-live") {{
      const evt = msg[2];
      try {{
        const data = JSON.parse(evt.content);
        updateCards(data);
      }} catch(e) {{ console.error('Parse error:', e); }}
    }} else if (msg[0] === "EOSE") {{
      // Switch to live subscription (new events only)
      ws.send(JSON.stringify(["REQ","kalman-live2",{{"kinds":[31998],"authors":[PUBKEY],"since":Math.floor(Date.now()/1000)}}]));
    }}
  }};
  
  ws.onerror = () => {{
    document.getElementById('connStatus').innerHTML = '<span style="color:#f85149">error — retrying</span>';
  }};
  
  ws.onclose = () => {{
    document.getElementById('connStatus').innerHTML = '<span style="color:#d29922">disconnected — reconnecting in 10s</span>';
    setTimeout(connect, 10000);
  }};
}}

connect();
</script>
</body></html>"""

with open(OUT, "w") as f:
    f.write(html)
print(f"Dashboard: {OUT} ({len(html)} bytes)")
