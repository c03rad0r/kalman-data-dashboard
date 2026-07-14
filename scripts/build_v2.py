#!/usr/bin/env python3
"""
build_v2.py — Generate Kalman dashboard that fetches data.json on load.
Solves the "datapoints lost on reload" problem by serving ALL historical
data from a same-origin JSON file instead of a frozen baked-in snapshot.

Deploy alongside data.json to nsite. Frontend auto-polls data.json every
2 minutes for fresh data without requiring a page reload.
"""

import sqlite3, json, os
from datetime import datetime, timezone
from pathlib import Path

DB = Path.home() / ".hermes" / "bot" / "zai_usage.db"
OUT_HTML = Path.home() / "nsites" / "kalman-data" / "index.html"
OUT_JSON = Path.home() / "nsites" / "kalman-data" / "data.json"
NSEC_PUBKEY = "d5c85ab43517b35d374a6360fa58fdc5f807b5331b3df7b155f34290acbb922b"


def generate_data_json():
    """Export ALL kalman samples to compact JSON."""
    db = sqlite3.connect(str(DB))
    rows = db.execute("""
        SELECT ts, burn_rate_tph, projected_total_pct, used_pct_observed,
               uncertainty, will_exhaust, velocity_tph2, exhausts_in_hours
        FROM kalman_samples ORDER BY ts ASC
    """).fetchall()
    anom_rows = db.execute("""
        SELECT ts, severity, title FROM anomaly_events ORDER BY ts DESC LIMIT 20
    """).fetchall()
    db.close()

    data = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "sample_count": len(rows),
        "times": [r[0] * 1000 for r in rows],
        "burn_rate": [min(r[1] or 0, 50000) for r in rows],
        "projected_pct": [r[2] or 0 for r in rows],
        "used_pct": [r[3] or 0 for r in rows],
        "uncertainty": [min(r[4] or 0, 50000) for r in rows],
        "will_exhaust": [bool(r[5]) for r in rows],
        "exhausts_in_hours": [r[7] for r in rows],
        "anomalies": [{"ts": r[0], "severity": r[1], "title": r[2]} for r in anom_rows],
    }
    with open(OUT_JSON, "w") as f:
        json.dump(data, f, separators=(",", ":"))
    return len(rows)


def generate_html():
    """Generate dashboard HTML that fetches data.json dynamically."""
    html = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>Kalman Filter Monitor</title>
<script src="https://cdn.plot.ly/plotly-2.35.2.min.js"></script>
<style>
body{font-family:-apple-system,system-ui,sans-serif;margin:0;padding:20px;background:#0d1117;color:#c9d1d9;}
h1{color:#58a6ff;font-size:1.4em;}
h2{color:#8b949e;font-size:1em;margin:30px 0 8px;}
.grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(160px,1fr));gap:12px;margin:16px 0;}
.card{background:#161b22;border:1px solid #30363d;border-radius:8px;padding:14px;}
.card h3{margin:0 0 4px;color:#58a6ff;font-size:0.75em;text-transform:uppercase;}
.card .v{font-size:1.6em;font-weight:bold;}
.chart{width:100%;height:380px;margin:8px 0;border:1px solid #30363d;border-radius:8px;}
.live{display:inline-block;width:8px;height:8px;border-radius:50%;background:#238636;animation:pulse 2s infinite;margin-right:6px;}
@keyframes pulse{0%,100%{opacity:1;}50%{opacity:0.3;}}
.note{color:#8b949e;font-size:0.85em;}
table{width:100%;border-collapse:collapse;font-size:0.85em;}
td,th{padding:6px 8px;border-bottom:1px solid #30363d;text-align:left;}
#loading{display:flex;justify-content:center;align-items:center;height:200px;color:#8b949e;font-size:1.2em;}
.err{color:#f85149;}
</style>
</head>
<body>

<h1><span class="live"></span>Kalman Filter Monitor</h1>
<p class="note">Data served from <code>data.json</code> (same-origin, all historical points). Auto-refreshes every 2 min.</p>

<div id="loading">Loading data.json...</div>
<div id="content" style="display:none">

<div class="grid">
  <div class="card"><h3>Burn Rate</h3><div class="v" id="burnRate" style="color:#7ee787">...</div><div class="note">tokens/hour</div></div>
  <div class="card"><h3>Predicted Total</h3><div class="v" id="projTotal" style="color:#58a6ff">...</div><div class="note">% of quota</div></div>
  <div class="card"><h3>Exhaustion</h3><div class="v" id="exhaust" style="color:#8b949e">...</div><div class="note">hours left</div></div>
  <div class="card"><h3>Data Points</h3><div class="v" id="pointCount" style="color:#d29922">...</div><div class="note">samples</div></div>
  <div class="card"><h3>Last Sample</h3><div class="v" id="lastUpdate" style="color:#8b949e;font-size:1.1em">...</div><div class="note">UTC</div></div>
  <div class="card"><h3>Generated</h3><div class="v" id="genTime" style="color:#8b949e;font-size:1.1em">...</div><div class="note">data.json age</div></div>
</div>

<h2>Burn Rate (all samples)</h2>
<div class="chart" id="c1"></div>

<h2>Predicted vs Actual Usage</h2>
<div class="chart" id="c2"></div>

<h2>Recent Anomalies</h2>
<table id="anomTable"><tr><th>Severity</th><th>Title</th><th>Time</th></tr></table>

</div>

<p style="margin-top:40px;padding-top:16px;border-top:1px solid #30363d;color:#8b949e;font-size:0.8em;">
<span class="live"></span>Auto-refresh every 2 min | Data source: <code>data.json</code> (rebuilt every 5 min by cron) | <span id="refreshStatus">idle</span>
</p>

<script>
const dark = {paper_bgcolor:'#161b22',plot_bgcolor:'#0d1117',font:{color:'#c9d1d9',size:10},margin:{t:30,b:35,l:50,r:15}};
const ax = {gridcolor:'#30363d'};
var chartsReady = false;

function fmtDate(ms) {
    return new Date(ms).toLocaleString('en-GB',{month:'2-digit',day:'2-digit',hour:'2-digit',minute:'2-digit'});
}

function render(data) {
    const ts = data.times.map(t => new Date(t));
    const burn = data.burn_rate;
    const proj = data.projected_pct;
    const used = data.used_pct;

    // Update cards
    const lastIdx = burn.length - 1;
    document.getElementById('burnRate').textContent = burn[lastIdx] ? (burn[lastIdx]/1000).toFixed(1)+'k' : '—';
    document.getElementById('projTotal').textContent = proj[lastIdx] ? proj[lastIdx].toFixed(1)+'%' : '—';
    const exhaustVal = data.exhausts_in_hours[lastIdx];
    document.getElementById('exhaust').textContent = exhaustVal ? exhaustVal.toFixed(1)+'h' : 'safe';
    document.getElementById('exhaust').style.color = data.will_exhaust[lastIdx] ? '#f85149' : '#7ee787';
    document.getElementById('pointCount').textContent = data.sample_count.toLocaleString();
    document.getElementById('lastUpdate').textContent = fmtDate(data.times[lastIdx]);
    const genDate = new Date(data.generated_at);
    const ageMin = Math.round((Date.now() - genDate) / 60000);
    document.getElementById('genTime').textContent = ageMin + ' min ago';

    // Anomaly table
    var anomHtml = '<tr><th>Severity</th><th>Title</th><th>Time</th></tr>';
    for (var a of data.anomalies) {
        var c = a.severity==='critical'?'#f85149':a.severity==='warning'?'#d29922':'#58a6ff';
        anomHtml += '<tr><td style="color:'+c+'">'+a.severity+'</td><td>'+a.title+'</td><td>'+fmtDate(a.ts*1000)+'</td></tr>';
    }
    document.getElementById('anomTable').innerHTML = anomHtml;

    // Charts
    var burnTrace = {x:ts, y:burn, mode:'markers', marker:{size:3,color:'#58a6ff'}, name:'Burn Rate'};
    var projTrace = {x:ts, y:proj, mode:'lines', name:'Predicted %', line:{color:'#58a6ff'}};
    var usedTrace = {x:ts, y:used, mode:'markers', marker:{size:2,color:'#d29922'}, name:'Observed %'};

    if (!chartsReady) {
        Plotly.newPlot('c1',[burnTrace],
            {...dark,title:'Burn Rate (tokens/hr)',xaxis:ax,yaxis:{...ax,title:'tokens/hr'}},{responsive:true});
        Plotly.newPlot('c2',[projTrace,usedTrace],
            {...dark,title:'Predicted vs Actual',xaxis:ax,yaxis:{...ax,title:'%'}},{responsive:true});
        chartsReady = true;
    } else {
        Plotly.react('c1',[burnTrace],
            {...dark,title:'Burn Rate (tokens/hr)',xaxis:ax,yaxis:{...ax,title:'tokens/hr'}},{responsive:true});
        Plotly.react('c2',[projTrace,usedTrace],
            {...dark,title:'Predicted vs Actual',xaxis:ax,yaxis:{...ax,title:'%'}},{responsive:true});
    }
}

async function loadData() {
    try {
        document.getElementById('refreshStatus').textContent = 'fetching...';
        // Cache-bust to always get fresh data
        const resp = await fetch('data.json?_t=' + Date.now());
        if (!resp.ok) throw new Error('HTTP ' + resp.status);
        const data = await resp.json();
        document.getElementById('loading').style.display = 'none';
        document.getElementById('content').style.display = 'block';
        render(data);
        document.getElementById('refreshStatus').innerHTML = '<span style="color:#7ee787">updated ' + new Date().toLocaleTimeString('en-GB') + '</span>';
    } catch(e) {
        document.getElementById('loading').innerHTML = '<p class="err">Failed to load data.json: ' + e.message + '</p><p class="note">The data file may not be deployed yet.</p>';
        document.getElementById('refreshStatus').innerHTML = '<span style="color:#f85149">error: ' + e.message + '</span>';
    }
}

// Initial load
loadData();

// Auto-refresh every 2 minutes
setInterval(loadData, 120000);
</script>
</body></html>"""

    with open(OUT_HTML, "w") as f:
        f.write(html)
    return len(html)


if __name__ == "__main__":
    n = generate_data_json()
    size = generate_html()
    json_size = os.path.getsize(OUT_JSON) / 1024
    print(f"data.json: {json_size:.0f} KB ({n} samples)")
    print(f"index.html: {size/1024:.0f} KB")
    print(f"Output: {OUT_HTML.parent}")
