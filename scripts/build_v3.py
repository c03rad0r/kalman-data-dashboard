#!/usr/bin/env python3
"""build_v3.py — Enhanced Kalman dashboard v3 (SATs edition).

v3.1 improvements:
- SATs burned per hour instead of raw tokens on chart c3
- PPQ.ai tokens included in hourly timeline
- Log-scale y-axis toggle on chart c3
- Price-per-token computed from flat-rate (z.ai: 144/mo) / top-up (PPQ: BTC->USD)
- BTC price feed from CoinGecko
"""

import sqlite3, json, os, re, urllib.request
from datetime import datetime, timezone
from pathlib import Path

DB = Path.home() / ".hermes" / "bot" / "zai_usage.db"
BURN_DB = Path.home() / ".hermes" / "bot" / "api_burn.db"
OUT_HTML = Path.home() / "nsites" / "kalman-data" / "index.html"
OUT_JSON = Path.home() / "nsites" / "kalman-data" / "data.json"
NSEC_PUBKEY = "29296138c53d33b2ff055198db8fcd883214ac141b2a0a4473fc87510b0eec1d"

# Price config
ZAI_MONTHLY_EUR = 144.0
BTCOINGECKO_URL = "https://api.coingecko.com/api/v3/simple/price?ids=bitcoin&vs_currencies=eur,usd"


def humanize_reason(reason):
    if not reason:
        return "unknown"
    if "only_available_ours_locked" in reason:
        m = re.search(r'ours_locked_(\w+?)_(\d+)pct', reason)
        window = m.group(1) if m else "?"
        pct = m.group(2) if m else "?"
        return f"Switched to friend's key - ours hit {pct}% on {window} window"
    if "only_available_friend_locked" in reason:
        m = re.search(r'friend_locked_(\w+?)_(\d+)pct', reason)
        window = m.group(1) if m else "?"
        pct = m.group(2) if m else "?"
        return f"Switched to our key - friend's hit {pct}% on {window} window"
    if "fallback_both_locked" in reason:
        m = re.search(r'ours_(\w+?)_(\d+)pct_friend_(\w+?)_(\d+)pct', reason)
        if m:
            our_w, our_p, fri_w, fri_p = m.groups()
            if "error" in our_w or "999" in our_p:
                return f"Both keys locked/error - ours: error, friend: {fri_p}% {fri_w}"
            return f"Both keys over quota - ours {our_p}% {our_w}, friend {fri_p}% {fri_w}"
        return "Both keys locked - emergency fallback"
    if "prefer_ours_both_unlocked" in reason:
        m = re.search(r'ours_(\d+)_friend_(\d+)', reason)
        if m:
            our_p, fri_p = m.groups()
            return f"Both available - prefer ours ({our_p}% used, friend at {fri_p}%)"
        return "Both keys available - prefer ours"
    if "ours_unlocked_higher_quota" in reason:
        return "Our key has more remaining quota"
    if "friend_unlocked_higher_quota" in reason:
        return "Friend's key has more remaining quota"
    if "lowest_quota" in reason:
        return "Selected key with lowest usage"
    if "default_preferred" in reason:
        return "Default preference (ours)"
    if "friend_blocked" in reason:
        return "Friend's key is blocked"
    if "ours_blocked" in reason:
        return "Our key is blocked"
    return reason.replace("_", " ")


def fetch_btc_price():
    """Fetch current BTC price from CoinGecko. Returns {eur, usd}."""
    try:
        req = urllib.request.Request(BTCOINGECKO_URL, headers={"User-Agent": "Hermes/1.0"})
        with urllib.request.urlopen(req, timeout=10) as resp:
            data = json.loads(resp.read())
        return {
            "eur": data.get("bitcoin", {}).get("eur", 0),
            "usd": data.get("bitcoin", {}).get("usd", 0),
        }
    except Exception as e:
        print(f"WARN: BTC price fetch failed: {e}")
        return {"eur": 0, "usd": 0}


def compute_sats_per_token():
    """Compute SATs-per-token for z.ai and PPQ.

    z.ai: 144/mo flat rate  30-day token burn = /token  SATs via BTC/EUR
    PPQ:  balance_snapshots (USD spent / tokens used)  SATs via BTC/USD
    """
    btc = fetch_btc_price()
    result = {"ours": 0, "friend": 0, "ppq": 0}

    db = sqlite3.connect(str(DB))
    cutoff_30d = datetime.now(timezone.utc).timestamp() - 86400 * 30

    # z.ai price
    row = db.execute("""
        SELECT SUM(total_tokens)
        FROM api_calls
        WHERE ts > ? AND ppq_hit = 0 AND status_code = 200
          AND key_name IN ('ours','friend')
    """, (cutoff_30d,)).fetchone()
    monthly_tokens = row[0] or 1
    eur_per_token = ZAI_MONTHLY_EUR / monthly_tokens
    if btc["eur"] > 0:
        result["ours"] = (eur_per_token / btc["eur"]) * 100_000_000
        result["friend"] = result["ours"]
    else:
        result["ours"] = 0
        result["friend"] = 0

    # PPQ price - from balance_snapshots
    # Try balance_snapshots for real balance data
    usd_per_token = 0.50 / 1_000_000  # default fallback: 50/M
    try:
        bdb = sqlite3.connect(str(BURN_DB))
        ppq_rows = bdb.execute("""
            SELECT ts, balance_usd FROM balance_snapshots
            WHERE provider='ppq' AND balance_usd IS NOT NULL AND error IS NULL
            ORDER BY ts DESC LIMIT 2
        """).fetchall()
        bdb.close()
        if len(ppq_rows) >= 2:
            latest = ppq_rows[0]
            previous = ppq_rows[1]
            ppq_tokens = db.execute("""
                SELECT SUM(total_tokens) FROM api_calls
                WHERE ppq_hit=1 AND ts BETWEEN ? AND ?
            """, (previous[0], latest[0])).fetchone()[0] or 1
            usd_spent = previous[1] - latest[1]
            if usd_spent > 0 and ppq_tokens > 1:
                usd_per_token = usd_spent / ppq_tokens
    except Exception:
        pass

    if btc["usd"] > 0 and usd_per_token > 0:
        result["ppq"] = (usd_per_token / btc["usd"]) * 100_000_000

    db.close()
    return result, btc


def format_sats(n):
    if n >= 1_000_000:
        return f"{n/1_000_000:.1f}M"
    elif n >= 1_000:
        return f"{n/1_000:.1f}K"
    elif n >= 1:
        return f"{n:.1f}"
    else:
        return f"{n:.4f}"


def generate_data_json():
    """Export ALL data to compact JSON with SATs pricing."""
    db = sqlite3.connect(str(DB))

    # Kalman samples
    rows = db.execute("""
        SELECT ts, burn_rate_tph, projected_total_pct, used_pct_observed,
               uncertainty, will_exhaust, velocity_tph2, exhausts_in_hours
        FROM kalman_samples ORDER BY ts ASC
    """).fetchall()

    # Anomalies
    anom_rows = db.execute("""
        SELECT ts, severity, category, title, detail
        FROM anomaly_events ORDER BY ts DESC LIMIT 20
    """).fetchall()

    # Key transitions
    kd_rows = db.execute("""
        SELECT ts, chosen_key, reason, ours_pct, friend_pct,
               ours_available, friend_available
        FROM key_decisions ORDER BY ts ASC
    """).fetchall()

    transitions = []
    prev_key = None
    for ts, key, reason, ours_p, friend_p, ours_avail, friend_avail in kd_rows:
        if key != prev_key:
            transitions.append({
                "ts": ts,
                "from": prev_key,
                "to": key,
                "reason_raw": reason,
                "reason_human": humanize_reason(reason),
                "ours_pct": ours_p,
                "friend_pct": friend_p,
            })
            prev_key = key

    # API hourly stats
    api_stats = db.execute("""
        SELECT
            CAST(ts / 3600 AS INTEGER) * 3600 as hour_ts,
            key_name,
            COUNT(*) as calls,
            SUM(total_tokens) as tokens,
            SUM(CASE WHEN cache_hit = 1 THEN 1 ELSE 0 END) as cache_hits,
            SUM(CASE WHEN status_code = 200 THEN 1 ELSE 0 END) as success,
            AVG(duration_ms) as avg_duration
        FROM api_calls
        WHERE ts > (SELECT MAX(ts) - 86400*7 FROM api_calls)
        GROUP BY hour_ts, key_name
        ORDER BY hour_ts ASC
    """).fetchall()

    # Key summary
    key_summary = db.execute("""
        SELECT key_name,
            COUNT(*) as calls,
            SUM(total_tokens) as tokens,
            SUM(CASE WHEN cache_hit = 1 THEN 1 ELSE 0 END) as cache_hits,
            SUM(CASE WHEN status_code = 200 THEN 1 ELSE 0 END) as success
        FROM api_calls
        GROUP BY key_name
    """).fetchall()

    db.close()

    # Price computation
    sats_per_token, btc_price = compute_sats_per_token()

    # Anomalies with detail
    anomalies = []
    for ts, sev, cat, title, detail_json in anom_rows:
        detail = {}
        if detail_json:
            try:
                detail = json.loads(detail_json)
            except Exception:
                detail = {"raw": detail_json}
        explanation = title
        if cat == "task_duration" and detail:
            task_id = detail.get("task_id", "?")
            profile = detail.get("profile", "?")
            ratio = detail.get("ratio", 0)
            expected = detail.get("expected_sec", 0)
            elapsed = detail.get("elapsed_sec", 0)
            baseline = detail.get("baseline", "?")
            explanation = (
                f"Worker '{profile}' (task {task_id[:12]}) took {elapsed:.0f}s "
                f"vs expected {expected:.0f}s ({ratio:.1f}x slower). "
                f"Baseline: {baseline}."
            )
        anomalies.append({
            "ts": ts,
            "severity": sev,
            "category": cat or "general",
            "title": title,
            "explanation": explanation,
            "detail": detail,
        })

    # Build hourly data - tokens AND SATs
    keys_present = sorted(set(r[1] for r in api_stats if r[1]))
    api_hourly = {k: {"times": [], "tokens": [], "calls": []} for k in keys_present}
    sats_hourly = {k: {"times": [], "sats": [], "tokens": []} for k in keys_present}

    for hour_ts, key_name, calls, tokens, cache, success, avg_dur in api_stats:
        if key_name and key_name in api_hourly:
            t = tokens or 0
            api_hourly[key_name]["times"].append(hour_ts * 1000)
            api_hourly[key_name]["tokens"].append(t)
            api_hourly[key_name]["calls"].append(calls or 0)
            sats_hourly[key_name]["times"].append(hour_ts * 1000)
            sats_hourly[key_name]["sats"].append(t * sats_per_token.get(key_name, 0))
            sats_hourly[key_name]["tokens"].append(t)

    # Key summary with SATs
    key_summaries = []
    for key_name, calls, tokens, cache, success in key_summary:
        if not key_name:
            continue
        t = tokens or 0
        cost_sats = t * sats_per_token.get(key_name, 0)
        key_summaries.append({
            "key": key_name,
            "calls": calls,
            "tokens_millions": round(t / 1_000_000, 1),
            "cache_hits": cache or 0,
            "success_rate": round((success or 0) / calls * 100, 1) if calls else 0,
            "cost_sats": round(cost_sats, 0),
            "cost_sats_fmt": format_sats(cost_sats),
        })

    price_info = {
        "btc_eur": btc_price.get("eur", 0),
        "btc_usd": btc_price.get("usd", 0),
        "zai_sats_per_token": sats_per_token.get("ours", 0),
        "zai_sats_per_Mtokens": round(sats_per_token.get("ours", 0) * 1_000_000, 4),
        "ppq_sats_per_token": sats_per_token.get("ppq", 0),
        "ppq_sats_per_Mtokens": round(sats_per_token.get("ppq", 0) * 1_000_000, 4),
    }

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
        "anomalies": anomalies,
        "key_transitions": transitions,
        "api_hourly": api_hourly,
        "api_hourly_sats": sats_hourly,
        "key_summaries": key_summaries,
        "price_info": price_info,
    }

    with open(OUT_JSON, "w") as f:
        json.dump(data, f, separators=(",", ":"))
    return len(rows)


def generate_html():
    """Generate dashboard HTML with SATs pricing and log-scale chart c3."""
    html = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>Kalman Filter Monitor v3</title>
<script src="https://cdn.plot.ly/plotly-2.35.2.min.js"></script>
<style>
body{font-family:-apple-system,system-ui,sans-serif;margin:0;padding:20px;background:#0d1117;color:#c9d1d9;max-width:1200px;margin:0 auto;}
h1{color:#58a6ff;font-size:1.4em;}
h2{color:#8b949e;font-size:1em;margin:30px 0 8px;display:flex;align-items:center;gap:8px;}
.grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(160px,1fr));gap:12px;margin:16px 0;}
.card{background:#161b22;border:1px solid #30363d;border-radius:8px;padding:14px;}
.card h3{margin:0 0 4px;color:#58a6ff;font-size:0.75em;text-transform:uppercase;}
.card .v{font-size:1.6em;font-weight:bold;}
.chart{width:100%;height:380px;margin:8px 0;border:1px solid #30363d;border-radius:8px;}
.live{display:inline-block;width:8px;height:8px;border-radius:50%;background:#238636;animation:pulse 2s infinite;margin-right:6px;}
@keyframes pulse{0%,100%{opacity:1;}50%{opacity:0.3;}}
.note{color:#8b949e;font-size:0.85em;}
table{width:100%;border-collapse:collapse;font-size:0.85em;margin:8px 0;}
td,th{padding:6px 8px;border-bottom:1px solid #30363d;text-align:left;vertical-align:top;}
#loading{display:flex;justify-content:center;align-items:center;height:200px;color:#8b949e;font-size:1.2em;}
.err{color:#f85149;}
.toggle{background:#21262d;border:1px solid #30363d;border-radius:6px;padding:4px 12px;color:#8b949e;cursor:pointer;font-size:0.8em;}
.toggle:hover{background:#30363d;color:#c9d1d9;}
.toggle.active{background:#238636;color:#fff;border-color:#238636;}
.anom-detail{background:#0d1117;border-left:3px solid #30363d;padding:6px 10px;margin:4px 0;font-size:0.8em;color:#8b949e;border-radius:0 4px 4px 0;}
.anom-detail.warn{border-left-color:#d29922;}
.anom-detail.crit{border-left-color:#f85149;}
.kpilog{background:#161b22;border:1px solid #30363d;border-radius:6px;padding:8px 12px;margin:4px 0;font-size:0.82em;}
.kpilog .arrow{font-weight:bold;font-size:1.1em;}
.kpilog .from{color:#f85149;}
.kpilog .to{color:#7ee787;}
.kpi-badge{display:inline-block;padding:1px 6px;border-radius:3px;font-size:0.75em;font-weight:bold;}
.kpi-ours{background:#1a3a5c;color:#58a6ff;}
.kpi-friend{background:#3d2a1a;color:#f0a050;}
.kpi-ppq{background:#2a1a3d;color:#bb86fc;}
.kpi-none{background:#333;color:#888;}
</style>
</head>
<body>

<h1><span class="live"></span>Kalman Filter Monitor v3</h1>
<p class="note">Enhanced: SATs burned per hour, log-scale plots, API key switching, descriptive anomalies. Auto-refreshes every 2 min.</p>

<div id="loading">Loading data.json...</div>
<div id="content" style="display:none">

<div class="grid">
  <div class="card"><h3>Burn Rate</h3><div class="v" id="burnRate" style="color:#7ee787">...</div><div class="note">tokens/hour</div></div>
  <div class="card"><h3>Predicted Total</h3><div class="v" id="projTotal" style="color:#58a6ff">...</div><div class="note">% of quota</div></div>
  <div class="card"><h3>Exhaustion</h3><div class="v" id="exhaust" style="color:#8b949e">...</div><div class="note">hours left</div></div>
  <div class="card"><h3>Active Key</h3><div class="v" id="activeKey" style="font-size:1.2em">...</div><div class="note">current</div></div>
  <div class="card"><h3>Data Points</h3><div class="v" id="pointCount" style="color:#d29922">...</div><div class="note">samples</div></div>
  <div class="card"><h3>Key Switches</h3><div class="v" id="switchCount" style="color:#f0a050">...</div><div class="note">total transitions</div></div>
</div>

<h2>Burn Rate (all samples) <span class="note"> tokens per hour over time</span></h2>
<div class="chart" id="c1"></div>

<h2>Predicted vs Actual Usage <button class="toggle" id="logToggle" onclick="toggleLog()">Linear</button></h2>
<div class="chart" id="c2"></div>

<h2>SATs Burned by Key <button class="toggle" id="c3LogToggle" onclick="toggleC3Log()">Log Scale</button> <span class="note"> SATs per hour (last 7 days)</span></h2>
<div class="chart" id="c3"></div>

<h2>Key Switching Log <span class="note"> when and why we switched API keys</span></h2>
<div id="switchLog"></div>

<h2>Token Cost Summary by Key</h2>
<div id="keySummary"></div>

<h2>Recent Anomalies <span class="note"> with detailed explanations</span></h2>
<div id="anomContainer"></div>

</div>

<p style="margin-top:40px;padding-top:16px;border-top:1px solid #30363d;color:#8b949e;font-size:0.8em;">
<span class="live"></span>Auto-refresh every 2 min | Data: <code>data.json</code> (rebuilt every 5 min by cron) | <span id="refreshStatus">idle</span>
</p>

<script>
const dark = {paper_bgcolor:'#161b22',plot_bgcolor:'#0d1117',font:{color:'#c9d1d9',size:10},margin:{t:30,b:35,l:50,r:15}};
const ax = {gridcolor:'#30363d'};
var logScale = false;
var c3LogScale = false;
var chartsReady = false;
var currentData = null;

function fmtDate(ms) {
    return new Date(ms).toLocaleString('en-GB',{month:'2-digit',day:'2-digit',hour:'2-digit',minute:'2-digit'});
}

function keyBadge(key) {
    var cls = 'kpi-badge kpi-' + (key || 'none');
    return '<span class="' + cls + '">' + (key || 'none') + '</span>';
}

function toggleLog() {
    logScale = !logScale;
    var btn = document.getElementById('logToggle');
    btn.textContent = logScale ? 'Logarithmic' : 'Linear';
    btn.classList.toggle('active', logScale);
    if (currentData) renderCharts(currentData);
}

function toggleC3Log() {
    c3LogScale = !c3LogScale;
    var btn = document.getElementById('c3LogToggle');
    btn.textContent = c3LogScale ? 'Linear' : 'Log Scale';
    btn.classList.toggle('active', c3LogScale);
    if (currentData) renderCharts(currentData);
}

function formatSats(n) {
    if (n >= 1000000) return (n/1000000).toFixed(1) + 'M';
    if (n >= 1000) return (n/1000).toFixed(1) + 'K';
    return n.toFixed(1);
}

function renderCharts(data) {
    const ts = data.times.map(t => new Date(t));
    const burn = data.burn_rate;
    const proj = data.projected_pct;
    const used = data.used_pct;

    // Chart 1: Burn rate
    var burnTrace = {x:ts, y:burn, mode:'markers', marker:{size:3,color:'#58a6ff'}, name:'Burn Rate'};
    Plotly.react('c1',[burnTrace],
        {...dark,title:'Burn Rate (tokens/hr)',xaxis:ax,yaxis:{...ax,title:'tokens/hr'}},{responsive:true});

    // Chart 2: Predicted vs Actual (with log toggle)
    var yConfig = {...ax, title:'%'};
    if (logScale) {
        yConfig.type = 'log';
        yConfig.autorange = true;
    }
    var projTrace = {x:ts, y:proj, mode:'lines', name:'Predicted %', line:{color:'#58a6ff'}};
    var usedTrace = {x:ts, y:used, mode:'markers', marker:{size:2,color:'#d29922'}, name:'Observed %'};

    var shapes = [];
    if (data.key_transitions) {
        for (var t of data.key_transitions) {
            shapes.push({
                type:'line',
                xref:'x',
                yref:'paper',
                x0:new Date(t.ts*1000),
                x1:new Date(t.ts*1000),
                y0:0,
                y1:1,
                line:{color: t.to==='friend'?'#f0a050':'#58a6ff', width:1, dash:'dot'},
                opacity:0.3
            });
        }
    }

    Plotly.react('c2',[projTrace,usedTrace],
        {...dark,title:'Predicted vs Actual' + (logScale?' (log scale)':''),xaxis:ax,yaxis:yConfig,shapes:shapes},{responsive:true});

    // Chart 3: API Cost Timeline  SATs burned per hour by key
    var satsData = data.api_hourly_sats;
    if (satsData) {
        var traces = [];
        var keyColors = {ours:'#58a6ff', friend:'#f0a050', ppq:'#bb86fc', None:'#888'};

        var hasPpq = false;
        for (var key in satsData) {
            if (key === 'ppq' && satsData[key].sats.some(s => s > 0)) hasPpq = true;
        }

        for (var key in satsData) {
            var kd = satsData[key];
            if (kd.times.length === 0) continue;

            var xVals = [];
            var yVals = [];
            for (var i = 0; i < kd.times.length; i++) {
                xVals.push(new Date(kd.times[i]));
                yVals.push(kd.sats[i] > 0 ? kd.sats[i] : null);
            }

            traces.push({
                x: xVals,
                y: yVals,
                mode: 'lines+markers',
                name: key,
                line: {color: keyColors[key] || '#888', width: 1.5},
                marker: {size: 3, color: keyColors[key] || '#888'},
            });
        }

        if (traces.length === 0) {
            traces.push({x:[], y:[], type:'scatter', name:'No data'});
        }

        var c3yConfig = {...ax, title:'SATs/hr', type: c3LogScale ? 'log' : 'linear', autorange: true};

        Plotly.react('c3', traces,
            {...dark, title:'API Cost by Key (SATs/hr)'
                + (hasPpq ? '  PPQ included' : ''),
             xaxis:ax, yaxis:c3yConfig},
            {responsive:true});
    }
}

function renderSwitchLog(data) {
    if (!data.key_transitions || data.key_transitions.length === 0) {
        document.getElementById('switchLog').innerHTML = '<p class="note">No key switches recorded.</p>';
        return;
    }
    var html = '';
    var transitions = data.key_transitions.slice(-30).reverse();
    for (var t of transitions) {
        var arrow = t.from ? '<span class="from">' + t.from + '</span>  <span class="to">' + t.to + '</span>' : ' <span class="to">' + t.to + '</span> (initial)';
        html += '<div class="kpilog">';
        html += '<div style="display:flex;justify-content:space-between;align-items:center;">';
        html += '<span><span class="arrow">' + arrow + '</span></span>';
        html += '<span class="note">' + fmtDate(t.ts*1000) + '</span>';
        html += '</div>';
        html += '<div class="note" style="margin-top:4px;">' + t.reason_human + '</div>';
        if (t.ours_pct !== null && t.ours_pct !== undefined) {
            html += '<div class="note" style="font-size:0.75em;">ours: ' + t.ours_pct + '% | friend: ' + t.friend_pct + '%</div>';
        }
        html += '</div>';
    }
    document.getElementById('switchLog').innerHTML = html;
    document.getElementById('switchCount').textContent = data.key_transitions.length;
}

function renderKeySummary(data) {
    if (!data.key_summaries || data.key_summaries.length === 0) {
        document.getElementById('keySummary').innerHTML = '<p class="note">No API call data.</p>';
        return;
    }
    var priceNote = '';
    if (data.price_info) {
        var p = data.price_info;
        priceNote = '<div class="note" style="margin-bottom:8px;">'
            + 'z.ai: ' + p.zai_sats_per_Mtokens + ' sats/Mtok | '
            + 'PPQ: ' + p.ppq_sats_per_Mtokens + ' sats/Mtok | '
            + 'BTC: ' + p.btc_eur + ' / $' + p.btc_usd
            + '</div>';
    }
    var html = priceNote
        + '<table><tr><th>Key</th><th>Calls</th><th>Tokens (M)</th><th>Cost (SATs)</th><th>Success Rate</th></tr>';
    for (var k of data.key_summaries) {
        html += '<tr><td>' + keyBadge(k.key) + '</td>';
        html += '<td>' + k.calls.toLocaleString() + '</td>';
        html += '<td>' + k.tokens_millions + 'M</td>';
        html += '<td>' + (k.cost_sats_fmt || k.cost_sats || '') + '</td>';
        html += '<td>' + k.success_rate + '%</td></tr>';
    }
    html += '</table>';
    document.getElementById('keySummary').innerHTML = html;
}

function renderAnomalies(data) {
    if (!data.anomalies || data.anomalies.length === 0) {
        document.getElementById('anomContainer').innerHTML = '<p class="note">No anomalies detected. System healthy.</p>';
        return;
    }
    var html = '';
    for (var a of data.anomalies) {
        var sevColor = a.severity==='critical'?'#f85149':a.severity==='warning'?'#d29922':'#58a6ff';
        var sevClass = a.severity==='critical'?'crit':a.severity==='warning'?'warn':'';
        html += '<div style="margin:8px 0;">';
        html += '<div style="display:flex;justify-content:space-between;align-items:flex-start;">';
        html += '<span><span style="color:' + sevColor + ';font-weight:bold;text-transform:uppercase;font-size:0.8em;">' + a.severity + '</span> ';
        html += '<span class="kpi-badge kpi-none" style="margin-left:6px;">' + a.category + '</span></span>';
        html += '<span class="note">' + fmtDate(a.ts*1000) + '</span>';
        html += '</div>';
        html += '<div class="anom-detail ' + sevClass + '" style="margin-top:4px;">' + a.explanation + '</div>';
        html += '</div>';
    }
    document.getElementById('anomContainer').innerHTML = html;
}

function render(data) {
    currentData = data;
    const lastIdx = data.burn_rate.length - 1;
    document.getElementById('burnRate').textContent = data.burn_rate[lastIdx] ? (data.burn_rate[lastIdx]/1000).toFixed(1)+'k' : '';
    document.getElementById('projTotal').textContent = data.projected_pct[lastIdx] ? data.projected_pct[lastIdx].toFixed(1)+'%' : '';
    const exhaustVal = data.exhausts_in_hours[lastIdx];
    document.getElementById('exhaust').textContent = exhaustVal ? exhaustVal.toFixed(1)+'h' : 'safe';
    document.getElementById('exhaust').style.color = data.will_exhaust[lastIdx] ? '#f85149' : '#7ee787';
    document.getElementById('pointCount').textContent = data.sample_count.toLocaleString();
    if (data.key_transitions && data.key_transitions.length > 0) {
        var lastT = data.key_transitions[data.key_transitions.length - 1];
        document.getElementById('activeKey').innerHTML = keyBadge(lastT.to);
    }
    renderCharts(data);
    renderSwitchLog(data);
    renderKeySummary(data);
    renderAnomalies(data);
    chartsReady = true;
}

async function loadData() {
    try {
        document.getElementById('refreshStatus').textContent = 'fetching...';
        const resp = await fetch('data.json?_t=' + Date.now());
        if (!resp.ok) throw new Error('HTTP ' + resp.status);
        const data = await resp.json();
        document.getElementById('loading').style.display = 'none';
        document.getElementById('content').style.display = 'block';
        render(data);
        document.getElementById('refreshStatus').innerHTML = '<span style="color:#7ee787">updated ' + new Date().toLocaleTimeString('en-GB') + '</span>';
    } catch(e) {
        document.getElementById('loading').innerHTML = '<p class="err">Failed to load data.json: ' + e.message + '</p>';
        document.getElementById('refreshStatus').innerHTML = '<span style="color:#f85149">error</span>';
    }
}

loadData();
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
