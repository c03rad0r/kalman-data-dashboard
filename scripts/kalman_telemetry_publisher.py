#!/usr/bin/env python3
"""
Kalman Telemetry Publisher — reads fresh data from zai_usage.db and publishes
it as Nostr kind 31998 telemetry events to relay.ngit.dev.

This makes Kalman filter data available in real-time to any subscriber,
including the nsite dashboard which connects via WebSocket.

Runs as a cron job every 5 minutes (same cadence as kalman-collect.sh).

Uses the Kalman Data nsite identity (npub19y5kzwx...) so events are
attributed to the same entity that owns the dashboard.

Kind 31998 format (addressable, replaceable per d-tag):
  d-tag: "kalman-telemetry"
  content: JSON with latest Kalman state, burn rate, system health
  tags: t=kalman, t=telemetry, t=zai-proxy
"""

import sqlite3
import json
import os
import sys
import time
import hashlib
import asyncio
import websockets
from pathlib import Path
from datetime import datetime, timezone

# ── Config ────────────────────────────────────────────────────────────────

NSEC = os.environ.get("KALMAN_NSEC", "")
if not NSEC:
    print("WARNING: KALMAN_NSEC env var not set — telemetry publishing disabled")
    sys.exit(0)
RELAYS = ["wss://relay.ngit.dev", "wss://nos.lol"]
DB_PATH = os.path.expanduser("~/.hermes/bot/zai_usage.db")
POOL_PATH = os.path.expanduser("~/.hermes/state/pool_kalman.json")
PUBLISH_INTERVAL = 300  # 5 minutes

# ── Nostr signing (same crypto as contextvm-anker-solix) ──────────────────

try:
    from coincurve import PrivateKey
except ImportError:
    # Fallback: use subprocess with nak
    pass

def bech32_decode_nsec(nsec_str):
    """Decode nsec (bech32) to raw 32-byte private key."""
    CHARSET = "qpzry9x8gf2tvdw0s3jn54khce6mua7l"
    hrp, data = nsec_str.split("1", 1)
    values = [CHARSET.index(c) for c in data]
    # Drop last 6 values (bech32 checksum)
    data_values = values[:-6]
    # Convert 5-bit groups to 8-bit bytes
    acc = 0
    bits = 0
    ret = []
    for v in data_values:
        acc = (acc << 5) | v
        bits += 5
        while bits >= 8:
            bits -= 8
            ret.append((acc >> bits) & 0xFF)
    return bytes(ret[:32])

def get_pubkey(nsec_str):
    """Get hex pubkey from nsec."""
    sk_bytes = bech32_decode_nsec(nsec_str)
    sk = PrivateKey(sk_bytes)
    return sk.public_key.format(compressed=False)[1:33].hex()

def sign_and_publish(event, nsec_str, relay_urls):
    """Sign event and publish to relays via WebSocket."""
    sk_bytes = bech32_decode_nsec(nsec_str)
    sk = PrivateKey(sk_bytes)
    pubkey = sk.public_key.format(compressed=False)[1:33].hex()
    event["pubkey"] = pubkey

    # Serialize for hashing
    serialized = json.dumps(
        [0, event["pubkey"], event["created_at"], event["kind"], event["tags"], event["content"]],
        separators=(",", ":"),
    )
    event["id"] = hashlib.sha256(serialized.encode()).hexdigest()
    sig = sk.sign_schnorr(bytes.fromhex(event["id"]))
    event["sig"] = sig.hex()

    async def _publish():
        for url in relay_urls:
            try:
                async with websockets.connect(url, max_size=2**20) as ws:
                    await ws.send(json.dumps(["EVENT", event]))
                    # Wait briefly for OK
                    try:
                        resp = await asyncio.wait_for(ws.recv(), timeout=3.0)
                        result = json.loads(resp)
                        ok = result[0] == "OK" and result[2]
                    except asyncio.TimeoutError:
                        ok = True  # Assume OK if no response
                    print(f"  {'✅' if ok else '⚠️'} {url}: {'OK' if ok else 'rejected'}")
            except Exception as e:
                print(f"  ❌ {url}: {e}")

    asyncio.run(_publish())

# ── Data extraction ───────────────────────────────────────────────────────

def extract_telemetry():
    """Read latest Kalman data from SQLite + pool state."""
    db = sqlite3.connect(DB_PATH)
    
    # Latest Kalman sample
    latest = db.execute("""
        SELECT ts, key, window, used_pct_observed, projected_total_pct,
               burn_rate_tph, uncertainty, exhausts_in_hours, will_exhaust, note
        FROM kalman_samples ORDER BY ts DESC LIMIT 1
    """).fetchone()
    
    # Last 12 samples (1 hour of data at 5-min intervals)
    recent = db.execute("""
        SELECT ts, burn_rate_tph, projected_total_pct, will_exhaust
        FROM kalman_samples ORDER BY ts DESC LIMIT 12
    """).fetchall()
    recent.reverse()  # Chronological order
    
    # Anomaly count in last 24h
    anom_count = db.execute("""
        SELECT COUNT(*) FROM anomaly_events 
        WHERE ts > ? AND alerted = 0
    """, (time.time() - 86400,)).fetchone()[0]
    
    # System health
    sys_health = db.execute("""
        SELECT load_per_core, mem_pct, swap_kb, running_workers, pending_tasks
        FROM system_readings ORDER BY ts DESC LIMIT 1
    """).fetchone()
    
    db.close()
    
    # Pool Kalman state
    pool = {}
    if os.path.exists(POOL_PATH):
        with open(POOL_PATH) as f:
            pool = json.load(f)
    
    # Build telemetry payload
    telemetry = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "kalman": {
            "burn_rate": latest[5] if latest else None,
            "projected_total_pct": latest[4] if latest else None,
            "used_pct": latest[3] if latest else None,
            "uncertainty": latest[6] if latest else None,
            "exhausts_in_hours": latest[7] if latest else None,
            "will_exhaust": bool(latest[8]) if latest else None,
            "note": latest[9] if latest else None,
        },
        "pool": {
            "x": pool.get("x", []),
            "P00": pool.get("P", [[0]])[0][0],
            "age_seconds": time.time() - pool.get("ts", 0),
        },
        "system": {
            "load_per_core": sys_health[0] if sys_health else None,
            "mem_pct": sys_health[1] if sys_health else None,
            "swap_kb": sys_health[2] if sys_health else None,
            "workers": sys_health[3] if sys_health else None,
            "pending_tasks": sys_health[4] if sys_health else None,
        },
        "anomalies_24h": anom_count,
        "recent_burn_rates": [r[1] for r in recent],
        "recent_projected": [r[2] for r in recent],
        "sample_count": db.execute("SELECT COUNT(*) FROM kalman_samples").fetchone()[0] if False else None,  # Closed already
    }
    
    # Reopen for count (quick)
    db2 = sqlite3.connect(DB_PATH)
    telemetry["sample_count"] = db2.execute("SELECT COUNT(*) FROM kalman_samples").fetchone()[0]
    db2.close()
    
    return telemetry

# ── Main ──────────────────────────────────────────────────────────────────

def publish_once():
    """Extract data and publish one telemetry event."""
    print(f"[{datetime.now(timezone.utc).isoformat()}] Publishing Kalman telemetry...")
    
    telemetry = extract_telemetry()
    
    event = {
        "kind": 31998,
        "content": json.dumps(telemetry),
        "tags": [
            ["d", "kalman-telemetry"],
            ["t", "kalman"],
            ["t", "telemetry"],
            ["t", "zai-proxy"],
            ["t", "burn-prediction"],
        ],
        "created_at": int(time.time()),
    }
    
    try:
        sign_and_publish(event, NSEC, RELAYS)
        print(f"  Published: burn_rate={telemetry['kalman']['burn_rate']:.0f}, exhaust={telemetry['kalman']['will_exhaust']}")
    except Exception as e:
        print(f"  ❌ Failed: {e}")
        return False
    
    return True

if __name__ == "__main__":
    if "--once" in sys.argv:
        publish_once()
    else:
        # Continuous mode
        print(f"Kalman Telemetry Publisher starting (interval={PUBLISH_INTERVAL}s)")
        while True:
            try:
                publish_once()
            except Exception as e:
                print(f"Error: {e}")
            time.sleep(PUBLISH_INTERVAL)
