#!/usr/bin/env python3
"""price_calc.py — Price per token → sats conversion for nsite burn dashboard.

Computes sats/token for each provider:
  - z.ai (ours + friend): €144/month flat rate ÷ rolling 30d token burn
  - PPQ.ai: USD balance change ÷ tokens burned (per topup cycle)

Both converted to SATs via live BTC price from CoinGecko.
"""

import sqlite3
import time
import json
import os
import urllib.request
from pathlib import Path

# ── Config ────────────────────────────────────────────────────────────────────
ZAI_MONTHLY_COST_EUR = 144.0
BTC_PRICE_CACHE_TTL = 300  # 5 minutes
BTC_PRICE_CACHE = Path.home() / ".hermes" / "state" / "btc_price_cache.json"

# ── BTC Price ──────────────────────────────────────────────────────────────────


def get_btc_price(max_age=BTC_PRICE_CACHE_TTL):
    """Fetch BTC/EUR and BTC/USD from CoinGecko, cached locally."""
    now = time.time()
    
    # Check cache
    if BTC_PRICE_CACHE.exists():
        try:
            cached = json.loads(BTC_PRICE_CACHE.read_text())
            if now - cached.get("ts", 0) < max_age:
                return cached["eur"], cached["usd"]
        except (json.JSONDecodeError, KeyError):
            pass
    
    # Fetch live
    url = "https://api.coingecko.com/api/v3/simple/price?ids=bitcoin&vs_currencies=eur,usd"
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "Hermes/1.0"})
        with urllib.request.urlopen(req, timeout=10) as resp:
            data = json.loads(resp.read().decode())
            eur = data["bitcoin"]["eur"]
            usd = data["bitcoin"]["usd"]
    except Exception:
        # Fallback: use last known value
        if BTC_PRICE_CACHE.exists():
            cached = json.loads(BTC_PRICE_CACHE.read_text())
            return cached["eur"], cached["usd"]
        eur, usd = 55258, 63146  # last known
    
    # Write cache
    BTC_PRICE_CACHE.parent.mkdir(parents=True, exist_ok=True)
    BTC_PRICE_CACHE.write_text(json.dumps({"ts": now, "eur": eur, "usd": usd}))
    return eur, usd


def sats_per_eur(btc_price_eur):
    """How many SATs is 1 EUR worth at given BTC price?"""
    return 100_000_000 / btc_price_eur


def sats_per_usd(btc_price_usd):
    """How many SATs is 1 USD worth at given BTC price?"""
    return 100_000_000 / btc_price_usd


# ── z.ai Price ─────────────────────────────────────────────────────────────────


def get_zai_monthly_tokens(db_path, lookback_days=30):
    """Sum all tokens burned across all z.ai keys in the last N days.
    
    Uses zai_usage.db's api_calls table. Returns total tokens.
    """
    cutoff = time.time() - lookback_days * 86400
    conn = sqlite3.connect(str(db_path))
    row = conn.execute(
        "SELECT COALESCE(SUM(total_tokens), 0) FROM api_calls "
        "WHERE ts > ? AND key_name IN ('ours', 'friend')",
        (cutoff,)
    ).fetchone()
    conn.close()
    return row[0]


def get_zai_hourly_tokens_by_key(db_path, lookback_hours=168):
    """Get hourly token buckets per z.ai key.
    
    Returns: {key: {times: [ms_timestamps], tokens: [counts], sats: [sats]}}
    """
    cutoff = time.time() - lookback_hours * 3600
    conn = sqlite3.connect(str(db_path))
    rows = conn.execute(
        """
        SELECT
            CAST(ts / 3600 AS INTEGER) * 3600 as hour_ts,
            key_name,
            COALESCE(SUM(total_tokens), 0) as tokens
        FROM api_calls
        WHERE ts > ? AND key_name IN ('ours', 'friend')
        GROUP BY hour_ts, key_name
        ORDER BY hour_ts ASC
        """,
        (cutoff,)
    ).fetchall()
    conn.close()
    
    # Build per-key dict
    keys = sorted(set(r[1] for r in rows if r[1]))
    result = {k: {"times": [], "tokens": [], "sats": []} for k in keys}
    
    # Get price per token in sats
    btc_eur, _ = get_btc_price()
    monthly_tokens = get_zai_monthly_tokens(db_path)
    sats_per_token = compute_zai_sats_per_token(btc_eur, monthly_tokens)
    
    for hour_ts, key_name, tokens in rows:
        if key_name and key_name in result:
            result[key_name]["times"].append(hour_ts * 1000)
            result[key_name]["tokens"].append(tokens)
            result[key_name]["sats"].append(tokens * sats_per_token)
    
    return result, sats_per_token


def compute_zai_sats_per_token(btc_price_eur, monthly_tokens):
    """sats/token for z.ai = (€144 / monthly_tokens) * sats_per_eur.
    
    If monthly_tokens is 0, returns 0 (no data yet).
    """
    if monthly_tokens <= 0:
        return 0.0
    sats_per_eur_val = sats_per_eur(btc_price_eur)
    cost_per_token_eur = ZAI_MONTHLY_COST_EUR / monthly_tokens
    return cost_per_token_eur * sats_per_eur_val


# ── PPQ Price ──────────────────────────────────────────────────────────────────


def get_ppq_balance_history(db_path, lookback_hours=168):
    """Get PPQ balance changes from api_burn.db.
    
    Returns: (hourly_snapshots: [{ts, balance_usd, total_credits, total_usage}])
    """
    cutoff = time.time() - lookback_hours * 3600
    conn = sqlite3.connect(str(db_path))
    rows = conn.execute(
        """
        SELECT ts, balance_usd, total_credits, total_usage
        FROM balance_snapshots
        WHERE provider = 'ppq' AND ts > ?
        ORDER BY ts ASC
        """,
        (cutoff,)
    ).fetchall()
    conn.close()
    
    return [{"ts": r[0], "balance_usd": r[1], "credits": r[2], "usage": r[3]}
            for r in rows]


def compute_ppq_sats_per_token(db_path, btc_price_usd):
    """sats/token for PPQ = (USD spent / tokens burned) * sats_per_usd.
    
    From balance history: find periods where balance decreased (spent).
    If no spending history, returns None (price unknown).
    """
    snapshots = get_ppq_balance_history(db_path, lookback_hours=720)  # 30 days
    if not snapshots:
        return None
    
    # Find periods of spending: look at balance changes
    total_spent_usd = 0
    total_tokens = 0
    prev_balance = None
    
    # Also check zai_usage.db for PPQ tokens
    zai_db = Path.home() / ".hermes" / "bot" / "zai_usage.db"
    if zai_db.exists():
        conn = sqlite3.connect(str(zai_db))
        row = conn.execute(
            "SELECT COALESCE(SUM(total_tokens), 0) FROM api_calls "
            "WHERE key_name = 'ppq'"
        ).fetchone()
        total_tokens = row[0]
        conn.close()
    
    # From balance history: each time balance drops, that's spending
    for snap in snapshots:
        b = snap["balance_usd"]
        if prev_balance is not None and b is not None and prev_balance is not None:
            if b < prev_balance:
                total_spent_usd += prev_balance - b
        prev_balance = b
    
    if total_tokens <= 0:
        return None
    
    sats_per_usd_val = sats_per_usd(btc_price_usd)
    cost_per_token_usd = total_spent_usd / total_tokens
    return cost_per_token_usd * sats_per_usd_val


def get_ppq_hourly_data(db_path, zai_db_path, lookback_hours=168):
    """Get PPQ hourly token buckets with sats conversion.
    
    Returns: {"times": [ms], "tokens": [counts], "sats": [counts]}
    """
    btc_eur, btc_usd = get_btc_price()
    sats_per_token = compute_ppq_sats_per_token(db_path, btc_usd)
    
    if sats_per_token is None or sats_per_token <= 0:
        return {"times": [], "tokens": [], "sats": []}
    
    # PPQ tokens from zai_usage.db (same api_calls table)
    cutoff = time.time() - lookback_hours * 3600
    conn = sqlite3.connect(str(zai_db_path))
    rows = conn.execute(
        """
        SELECT
            CAST(ts / 3600 AS INTEGER) * 3600 as hour_ts,
            COALESCE(SUM(total_tokens), 0) as tokens
        FROM api_calls
        WHERE ts > ? AND key_name = 'ppq'
        GROUP BY hour_ts
        ORDER BY hour_ts ASC
        """,
        (cutoff,)
    ).fetchall()
    conn.close()
    
    result = {"times": [], "tokens": [], "sats": []}
    for hour_ts, tokens in rows:
        result["times"].append(hour_ts * 1000)
        result["tokens"].append(tokens)
        result["sats"].append(tokens * sats_per_token)
    
    return result


# ── Summary ────────────────────────────────────────────────────────────────────


def get_price_summary():
    """Generate price summary for all providers.
    
    Returns: list of {key, sats_per_token, tokens_per_eur_usd, notes}
    """
    btc_eur, btc_usd = get_btc_price()
    zai_db = Path.home() / ".hermes" / "bot" / "zai_usage.db"
    burn_db = Path.home() / ".hermes" / "bot" / "api_burn.db"
    
    summaries = []
    
    # z.ai
    monthly_tokens = get_zai_monthly_tokens(zai_db)
    zai_sats = compute_zai_sats_per_token(btc_eur, monthly_tokens)
    
    # Monthly cost per key (split by usage ratio)
    conn = sqlite3.connect(str(zai_db))
    key_tokens = conn.execute(
        "SELECT key_name, COALESCE(SUM(total_tokens), 0) as tokens "
        "FROM api_calls WHERE key_name IN ('ours', 'friend') "
        "AND ts > ? GROUP BY key_name",
        (time.time() - 30 * 86400,)
    ).fetchall()
    conn.close()
    
    total = sum(r[1] for r in key_tokens)
    for key_name, tokens in key_tokens:
        if total > 0 and tokens > 0:
            share = tokens / total
            sats = zai_sats  # same rate since they share the €144 flat fee
            summaries.append({
                "key": key_name,
                "sats_per_token": round(sats, 12),
                "tokens_30d": tokens,
                "cost_share_eur": round(ZAI_MONTHLY_COST_EUR * share, 2),
                "source": f"€{ZAI_MONTHLY_COST_EUR}/mo flat ÷ {total:,} tokens/mo",
            })
    
    # PPQ
    ppq_sats = compute_ppq_sats_per_token(burn_db, btc_usd)
    zai_conn = sqlite3.connect(str(zai_db))
    ppq_tokens = zai_conn.execute(
        "SELECT COALESCE(SUM(total_tokens), 0) FROM api_calls WHERE key_name = 'ppq'"
    ).fetchone()[0]
    zai_conn.close()
    
    summaries.append({
        "key": "ppq",
        "sats_per_token": round(ppq_sats, 12) if ppq_sats else None,
        "tokens_30d": ppq_tokens,
        "cost_share_eur": None,
        "source": "pay-per-request (BTC topup → USD balance)",
    })
    
    return {
        "btc_eur": btc_eur,
        "btc_usd": btc_usd,
        "sats_per_eur": round(sats_per_eur(btc_eur), 2),
        "sats_per_usd": round(sats_per_usd(btc_usd), 2),
        "per_key": summaries,
    }


if __name__ == "__main__":
    import pprint
    summary = get_price_summary()
    pprint.pprint(summary)
