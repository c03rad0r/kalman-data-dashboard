#!/usr/bin/env python3
"""Build the Kalman filter data dashboard from real zai_usage.db data.

Reads kalman_samples (2945 rows), system_readings, anomaly_events, and the
current pool Kalman state, then emits a self-contained Plotly dashboard with
inline JSON data (Plotly.js loaded from CDN).
"""
import sqlite3, json, datetime, os, statistics

DB = os.path.expanduser("~/.hermes/bot/zai_usage.db")
POOL = os.path.expanduser("~/.hermes/state/pool_kalman.json")
OUT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "index.html")

conn = sqlite3.connect(DB)
conn.row_factory = sqlite3.Row
c = conn.cursor()

# ---- all kalman samples ----
rows = [dict(r) for r in c.execute(
    "SELECT ts,key,window,used_pct_observed,projected_additional_pct,"
    "projected_total_pct,burn_rate_tph,velocity_tph2,uncertainty,"
    "exhausts_in_hours,will_exhaust,note FROM kalman_samples ORDER BY ts,id")]

def iso(ts):
    return datetime.datetime.utcfromtimestamp(ts).strftime("%Y-%m-%d %H:%M:%S")

# dedupe burn-rate by (ts,key) since it's constant across windows for a given sample
seen = {}
for r in rows:
    k = (r["ts"], r["key"])
    if k not in seen:
        seen[k] = r
burn_rows = sorted(seen.values(), key=lambda r: (r["key"], r["ts"]))

ts_min = min(r["ts"] for r in rows)
ts_max = max(r["ts"] for r in rows)

# per-key burn arrays
def by_key(rws):
    out = {}
    for r in rws:
        out.setdefault(r["key"], []).append(r)
    return out

burn_by_key = by_key(burn_rows)

# ---- status / summary ----
n_total = len(rows)
n_exhaust = sum(1 for r in rows if r["will_exhaust"])
burn_vals = [r["burn_rate_tph"] for r in burn_rows]
burn_mean = statistics.mean(burn_vals) if burn_vals else 0
burn_max = max(burn_vals) if burn_vals else 0

# pool kalman state
try:
    with open(POOL) as f:
        pool = json.load(f)
except Exception:
    pool = {"x": [None, None], "P": [[None, None], [None, None]], "ts": None}

pool_ts_iso = iso(pool["ts"]) if pool.get("ts") else "n/a"

# ---- system_readings ----
sys_rows = [dict(r) for r in c.execute(
    "SELECT * FROM system_readings ORDER BY ts")]

# ---- anomaly_events ----
anom_rows = [dict(r) for r in c.execute(
    "SELECT id,ts,severity,category,title,detail,alerted,resolved "
    "FROM anomaly_events ORDER BY ts DESC")]

conn.close()

# windows present
windows = sorted({r["window"] for r in rows})
keys = sorted({r["key"] for r in rows})

# Build JSON datasets for each chart
data = {
    "generated": datetime.datetime.utcnow().isoformat() + "+00:00",
    "ts_range": [iso(ts_min), iso(ts_max)],
    "n_total": n_total,
    "n_exhaust": n_exhaust,
    "burn_mean": burn_mean,
    "burn_max": burn_max,
    "windows": windows,
    "keys": keys,
    "pool": {
        "x": pool["x"],
        "P": pool["P"],
        "ts": pool_ts_iso,
        "ts_raw": pool.get("ts"),
    },
    # Chart 1: burn rate over time, deduped by (ts,key)
    "burn": {k: {
        "ts": [iso(r["ts"]) for r in burn_by_key[k]],
        "burn": [r["burn_rate_tph"] for r in burn_by_key[k]],
        "exhaust": [r["will_exhaust"] for r in burn_by_key[k]],
    } for k in burn_by_key},
    # Chart 3: uncertainty bands
    "uncert": {k: {
        "ts": [iso(r["ts"]) for r in burn_by_key[k]],
        "burn": [r["burn_rate_tph"] for r in burn_by_key[k]],
        "unc": [r["uncertainty"] for r in burn_by_key[k]],
    } for k in burn_by_key},
    # Chart 5: velocity
    "vel": {k: {
        "ts": [iso(r["ts"]) for r in burn_by_key[k]],
        "burn": [r["burn_rate_tph"] for r in burn_by_key[k]],
        "vel": [r["velocity_tph2"] for r in burn_by_key[k]],
    } for k in burn_by_key},
    # Chart 6: histogram of burn rates (deduped)
    "burn_hist": burn_vals,
}

# Chart 2: predicted vs actual — per window, key 'ours' (primary)
pred_by_win = {}
for w in windows:
    wr = [r for r in rows if r["window"] == w and r["key"] == "ours"]
    pred_by_win[w] = {
        "ts": [iso(r["ts"]) for r in wr],
        "projected": [r["projected_total_pct"] for r in wr],
        "observed": [r["used_pct_observed"] for r in wr],
    }
data["pred"] = pred_by_win

# Chart 4: exhaustion timeline — will_exhaust=1, exhausts_in_hours over time
exh_by_win = {}
for w in windows:
    wr = [r for r in rows if r["window"] == w and r["will_exhaust"] == 1]
    exh_by_win[w] = {
        "ts": [iso(r["ts"]) for r in wr],
        "hours": [r["exhausts_in_hours"] for r in wr],
    }
data["exhaust"] = exh_by_win

# system readings compact
data["sys"] = {
    "ts": [iso(r["ts"]) for r in sys_rows],
    "load": [r["load_per_core"] for r in sys_rows],
    "mem": [r["mem_pct"] for r in sys_rows],
    "quota": [r["api_quota_pct"] for r in sys_rows],
    "workers": [r["running_workers"] for r in sys_rows],
    "cpu_sm": [r["cpu_smoothed"] for r in sys_rows],
}
data["sys_n"] = len(sys_rows)

# anomalies compact for table
data["anomalies"] = [{
    "ts": iso(r["ts"]),
    "severity": r["severity"],
    "category": r["category"],
    "title": r["title"],
    "resolved": r["resolved"],
} for r in anom_rows[:12]]
data["anomaly_n"] = len(anom_rows)

DATA_JSON = json.dumps(data)

HTML = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Kalman Filter Data — Burn Prediction Dashboard</title>
<script src="https://cdn.plot.ly/plotly-2.35.2.min.js"></script>
<style>
  body { font-family: -apple-system, system-ui, sans-serif; margin:0; padding:20px;
         background:#0d1117; color:#c9d1d9; }
  h1 { color:#58a6ff; font-size:1.5em; margin-bottom:4px; }
  h2 { color:#8b949e; font-size:1.05em; margin-top:30px; }
  p.sub { color:#8b949e; font-size:0.85em; margin-top:0; }
  .status { display:grid; grid-template-columns:repeat(auto-fit,minmax(190px,1fr));
            gap:12px; margin:18px 0; }
  .card { background:#161b22; border:1px solid #30363d; border-radius:8px; padding:14px; }
  .card h3 { margin:0 0 6px 0; color:#58a6ff; font-size:0.75em; text-transform:uppercase; letter-spacing:0.5px;}
  .card .value { font-size:1.7em; font-weight:bold; color:#7ee787; }
  .card .note { font-size:0.78em; color:#8b949e; margin-top:4px; }
  .chart { width:100%; height:420px; margin:8px 0; border:1px solid #30363d;
           border-radius:8px; overflow:hidden; }
  .grid2 { display:grid; grid-template-columns:1fr 1fr; gap:16px; }
  @media (max-width:900px){ .grid2{ grid-template-columns:1fr; } }
  table { border-collapse:collapse; width:100%; font-size:0.82em; }
  th,td { text-align:left; padding:6px 8px; border-bottom:1px solid #30363d; }
  th { color:#58a6ff; text-transform:uppercase; font-size:0.8em; }
  tr:hover td { background:#21262d; }
  .badge { display:inline-block; padding:2px 8px; border-radius:4px; font-size:0.75em; font-weight:bold;}
  .live { background:#238636; color:white; }
  .sev-critical { color:#f85149; font-weight:bold; }
  .sev-warning { color:#d29922; font-weight:bold; }
  .footer { margin-top:40px; padding-top:18px; border-top:1px solid #30363d;
            color:#8b949e; font-size:0.82em; }
  .matrix { font-family:ui-monospace,monospace; font-size:0.8em; color:#7ee787; }
  .mono { font-family:ui-monospace,monospace; }
</style>
</head>
<body>
<h1>Kalman Filter Data — Burn Prediction Dashboard</h1>
<p class="sub"><span class="badge live">REAL DATA</span> &nbsp; __N_TOTAL__ samples from zai_usage.db · __TS_RANGE__ · keys: __KEYS__ · windows: __WINS__</p>

<div class="status">
  <div class="card"><h3>Total Samples</h3><div class="value">__N_TOTAL__</div>
     <div class="note">kalman_samples rows (deduped __N_BURN__ by ts+key)</div></div>
  <div class="card"><h3>Exhaust Predicted</h3><div class="value" style="color:#f85149;">__N_EXHAUST__</div>
     <div class="note">samples flagged will_exhaust=1</div></div>
  <div class="card"><h3>Mean Burn Rate</h3><div class="value">__BURN_MEAN__</div>
     <div class="note">avg tokens/hour (deduped)</div></div>
  <div class="card"><h3>Peak Burn Rate</h3><div class="value">__BURN_MAX__</div>
     <div class="note">max tokens/hour observed</div></div>
  <div class="card"><h3>Pool State x</h3><div class="value" style="font-size:1.1em;" class="mono">__POOL_X__</div>
     <div class="note">updated __POOL_TS__</div></div>
</div>

<h2>1. Burn Rate Over Time — API quota consumption speed</h2>
<p class="sub">burn_rate_tph (tokens/hour) vs time, deduped by ts+key. Red = sample flagged will_exhaust=1, green = safe. Axis uses SI notation (M=million).</p>
<div class="chart" id="c1"></div>

<h2>2. Predicted vs Actual — the core prediction-vs-reality chart</h2>
<p class="sub">Kalman projected_total_pct (predicted usage) vs used_pct_observed (actual), per window horizon. Shows how close the filter's projection lands to reality.</p>
<div class="chart" id="c2"></div>

<div class="grid2">
<div>
<h2>3. Uncertainty Bands — filter confidence</h2>
<p class="sub">burn_rate_tph ± uncertainty as a shaded band. Narrow band = high confidence.</p>
<div class="chart" id="c3"></div>
</div>
<div>
<h2>4. Exhaustion Timeline — critical alert metric</h2>
<p class="sub">When will_exhaust=1: hours-until-exhaust over time, per window. Lower = sooner to run out.</p>
<div class="chart" id="c4"></div>
</div>
</div>

<div class="grid2">
<div>
<h2>5. Velocity — acceleration of burn</h2>
<p class="sub">burn_rate_tph (left) vs velocity_tph2 (right). Positive velocity = burn accelerating.</p>
<div class="chart" id="c5"></div>
</div>
<div>
<h2>6. Burn Rate Distribution</h2>
<p class="sub">Histogram of burn_rate_tph values the Kalman filter operates on.</p>
<div class="chart" id="c6"></div>
</div>
</div>

<h2>System Readings (__SYS_N__ samples) — host load while Kalman ran</h2>
<p class="sub">load-per-core, memory %, API quota %, running workers, smoothed CPU from system_readings table.</p>
<div class="chart" id="c7"></div>

<h2>Pool Kalman Covariance Matrix (P)</h2>
<div class="card"><pre class="matrix" id="pmtx"></pre></div>

<h2>Recent Anomaly Events (__ANOM_N__ total)</h2>
<table id="anomtbl">
<tr><th>Time</th><th>Severity</th><th>Category</th><th>Title</th><th>Resolved</th></tr>
__ANOM_ROWS__
</table>

<div class="footer">
  <p><b>Source:</b> ~/.hermes/bot/zai_usage.db (kalman_samples, system_readings, anomaly_events) + ~/.hermes/state/pool_kalman.json</p>
  <p><b>Kalman state vector x:</b> [burn_rate_tph, velocity_tph2] &nbsp;·&nbsp; <b>P:</b> 2×2 covariance. Diagonal = variance of each state estimate.</p>
  <p><b>Generated:</b> __GEN__</p>
</div>

<script>
const D = __DATA__;
const C = {paper:'#161b22',plot:'#0d1117',font:'#c9d1d9',grid:'#30363d'};
const axis = (t)=>({gridcolor:C.grid,title:t,zerolinecolor:'#30363d'});
const layout = (title)=>({title,paper_bgcolor:C.paper,plot_bgcolor:C.plot,
  font:{color:C.font,size:11},margin:{t:40,b:40,l:60,r:30}});
const keyColor = {'ours':'#58a6ff','friend':'#bc8cff'};
const winColor = {'5-hour':'#d29922','weekly':'#58a6ff','monthly':'#7ee787','error':'#f85149'};

// Chart 1: burn rate over time
{let trs=[];
for(const k of Object.keys(D.burn)){
  const b=D.burn[k];
  trs.push({x:b.ts,y:b.burn,mode:'markers',name:`${k} (burn)`,
    text:b.exhaust.map(e=>e?'EXHAUST':'safe'),
    marker:{size:4,color:b.exhaust.map(e=>e?'#f85149':'#7ee787'),
            line:{color:'#30363d',width:0.5}},
    transforms:[]});
}
Plotly.newPlot('c1',trs,Object.assign(layout('Burn Rate Over Time'),{
  xaxis:axis('Time (UTC)'),yaxis:axis('Burn rate (tokens/hr)')}),
  {responsive:true});}

// Chart 2: predicted vs actual per window
{let trs=[];
for(const w of Object.keys(D.pred)){
  const p=D.pred[w];
  trs.push({x:p.ts,y:p.projected,mode:'lines',name:`${w} projected_total %`,
    line:{color:winColor[w]||'#888',width:2}});
  trs.push({x:p.ts,y:p.observed,mode:'markers',name:`${w} used_observed %`,
    marker:{size:3,color:winColor[w]||'#888',symbol:'x'}});
}
Plotly.newPlot('c2',trs,Object.assign(layout('Predicted vs Actual Usage %'),{
  xaxis:axis('Time (UTC)'),yaxis:axis('Usage %'),
  legend:{x:0,y:1,bgcolor:'rgba(22,27,34,0.85)'}}),
  {responsive:true});}

// Chart 3: uncertainty bands
{let trs=[];
for(const k of Object.keys(D.uncert)){
  const u=D.uncert[k];
  const lo=u.burn.map((v,i)=>Math.max(0,v-u.unc[i]));
  const hi=u.burn.map((v,i)=>v+u.unc[i]);
  trs.push({x:u.ts.concat([].concat(u.ts).reverse()),
    y:hi.concat([].concat(lo).reverse()),fill:'toself',fillcolor:'rgba(88,166,255,0.15)',
    line:{color:'transparent'},name:`${k} ±uncertainty`,hoverinfo:'skip'});
  trs.push({x:u.ts,y:u.burn,mode:'lines',name:`${k} burn_rate`,
    line:{color:keyColor[k]||'#888',width:2}});
}
Plotly.newPlot('c3',trs,Object.assign(layout('Burn Rate ± Uncertainty'),{
  xaxis:axis('Time (UTC)'),yaxis:axis('Burn rate (tokens/hr)')}),
  {responsive:true});}

// Chart 4: exhaustion timeline
{let trs=[];
for(const w of Object.keys(D.exhaust)){
  const e=D.exhaust[w];
  if(!e.ts.length) continue;
  trs.push({x:e.ts,y:e.hours,mode:'markers+lines',name:`${w} exhausts_in_hours`,
    marker:{size:5,color:winColor[w]||'#888'},
    line:{color:winColor[w]||'#888',width:1,dash:'dot'}});
}
Plotly.newPlot('c4',trs,Object.assign(layout('Exhaustion Timeline (will_exhaust=1 only)'),{
  xaxis:axis('Time (UTC)'),yaxis:axis('Hours until quota exhausts')}),
  {responsive:true});}

// Chart 5: velocity
{let trs=[];
for(const k of Object.keys(D.vel)){
  const v=D.vel[k];
  trs.push({x:v.ts,y:v.burn,mode:'lines',name:`${k} burn_rate`,
    line:{color:keyColor[k]||'#888',width:2}});
  trs.push({x:v.ts,y:v.vel,mode:'lines',name:`${k} velocity`,
    line:{color:keyColor[k]||'#888',width:1,dash:'dot'},yaxis:'y2'});
}
Plotly.newPlot('c5',trs,Object.assign(layout('Burn Rate vs Velocity'),{
  xaxis:axis('Time (UTC)'),yaxis:axis('burn_rate (tokens/hr)'),
  yaxis2:{overlaying:'y',side:'right',title:'velocity (tokens/hr²)',gridcolor:'#30363d'},
  legend:{x:0,y:1,bgcolor:'rgba(22,27,34,0.85)'}}),
  {responsive:true});}

// Chart 6: histogram
{Plotly.newPlot('c6',[{x:D.burn_hist,type:'histogram',marker:{color:'#58a6ff'},
  name:'burn_rate_tph',opacity:0.85}],Object.assign(layout('Burn Rate Distribution'),{
  xaxis:axis('burn_rate (tokens/hr)'),yaxis:axis('count'),bargap:0.05}),
  {responsive:true});}

// Chart 7: system readings
{const s=D.sys;
let trs=[
  {x:s.ts,y:s.load,mode:'lines',name:'load/core',line:{color:'#f85149',width:2}},
  {x:s.ts,y:s.mem,mode:'lines',name:'mem %',line:{color:'#d29922',width:1}},
  {x:s.ts,y:s.cpu_sm,mode:'lines',name:'cpu smoothed',line:{color:'#bc8cff',width:1}},
  {x:s.ts,y:s.workers,mode:'markers',name:'workers',marker:{size:4,color:'#7ee787'},yaxis:'y2'},
];
Plotly.newPlot('c7',trs,Object.assign(layout('System Readings Over Time'),{
  xaxis:axis('Time (UTC)'),yaxis:axis('load / % / quota'),
  yaxis2:{overlaying:'y',side:'right',title:'workers',gridcolor:'#30363d'},
  legend:{x:0,y:1,bgcolor:'rgba(22,27,34,0.85)'}}),
  {responsive:true});}

// pool matrix
document.getElementById('pmtx').textContent = 'x = '+JSON.stringify(D.pool.x)+'\\nP = '+JSON.stringify(D.pool.P)+'\\nts = '+D.pool.ts;
</script>
</body>
</html>
"""

def fmt_big(v):
    if v is None: return "n/a"
    if abs(v) >= 1e6: return f"{v/1e6:.2f}M"
    if abs(v) >= 1e3: return f"{v/1e3:.1f}K"
    return f"{v:.2f}"

anom_rows_html = ""
for a in data["anomalies"]:
    sev_cls = "sev-critical" if a["severity"]=="critical" else "sev-warning"
    anom_rows_html += (f"<tr><td class='mono'>{a['ts']}</td>"
                       f"<td class='{sev_cls}'>{a['severity']}</td>"
                       f"<td>{a['category']}</td><td>{a['title']}</td>"
                       f"<td>{'✅' if a['resolved'] else '⏳'}</td></tr>\n")

html = (HTML
    .replace("__DATA__", DATA_JSON)
    .replace("__N_TOTAL__", str(n_total))
    .replace("__N_BURN__", str(len(burn_rows)))
    .replace("__N_EXHAUST__", str(n_exhaust))
    .replace("__BURN_MEAN__", fmt_big(burn_mean))
    .replace("__BURN_MAX__", fmt_big(burn_max))
    .replace("__TS_RANGE__", f"{iso(ts_min)} → {iso(ts_max)}")
    .replace("__KEYS__", ", ".join(keys))
    .replace("__WINS__", ", ".join(windows))
    .replace("__POOL_X__", str(pool["x"]))
    .replace("__POOL_TS__", pool_ts_iso)
    .replace("__SYS_N__", str(data["sys_n"]))
    .replace("__ANOM_N__", str(data["anomaly_n"]))
    .replace("__ANOM_ROWS__", anom_rows_html)
    .replace("__GEN__", data["generated"])
)

with open(OUT, "w") as f:
    f.write(html)
print(f"Wrote {OUT} ({len(html)} bytes), {n_total} samples, {n_exhaust} exhaust flags")
