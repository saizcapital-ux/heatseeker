#!/usr/bin/env python3
"""
SPX "real-money" GEX signal.

SPX / SPXW options carry the deep, institutional dealer-gamma positioning that
actually drives the tape; SPY mechanically tracks SPX ~10:1. This module reads
the free CBOE delayed-quote feed for SPX, computes the same GEX picture the bot
uses for SPY (per-strike net gamma, king node, gamma flip, walls, max pain), and
exposes:

  * a directional bias (same king-node convention the SPY path uses),
  * a regime read (positive vs negative gamma, from spot vs flip),
  * every key level translated to SPY price scale via the live SPX/SPY ratio,
  * a confirmation verdict + confidence multiplier for a given SPY direction.

Used by both web.py (dashboard SPX overlay) and spy_trader.py (entry confidence).
Everything is best-effort: any failure returns None so callers degrade cleanly.
"""
import json
from datetime import datetime
from zoneinfo import ZoneInfo

ET = ZoneInfo("America/New_York")

CBOE_SPX_URL = "https://cdn.cboe.com/api/global/delayed_quotes/options/_SPX.json"
SPX_STRIKE_WINDOW = 250          # ±SPX points (~±25 SPY points) around spot
ALIGN_BOOST   = 1.15             # confidence ×boost when SPX confirms SPY
DIVERGE_PENALTY = 0.75           # confidence ×penalty when SPX diverges


def fetch_json(url, timeout=12):
    import urllib.request
    req = urllib.request.Request(url, headers={
        "User-Agent": "Mozilla/5.0 (HeatseekerTerminal/1.0)",
        "Accept": "application/json",
    })
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode())


def parse_occ(sym):
    """'SPXW  260701C06450000' -> (expiry 'YYYY-MM-DD', 'C'/'P', strike float)."""
    s = (sym or "").replace(" ", "")
    if len(s) < 15:
        return None
    try:
        strike = int(s[-8:]) / 1000.0
        typ    = s[-9].upper()
        yymmdd = s[-15:-9]
        expiry = f"20{yymmdd[0:2]}-{yymmdd[2:4]}-{yymmdd[4:6]}"
        return expiry, typ, strike
    except Exception:
        return None


def analyze(payload, window=SPX_STRIKE_WINDOW):
    """Pure: CBOE SPX JSON -> raw GEX picture on SPX's native price scale."""
    data = (payload or {}).get("data", {}) or {}
    spot = data.get("current_price") or data.get("close")
    opts = data.get("options", []) or []
    if not spot or not opts:
        return None
    spot = float(spot)
    today = datetime.now(ET).strftime("%Y-%m-%d")

    by_exp = {}
    for o in opts:
        p = parse_occ(o.get("option"))
        if not p:
            continue
        exp, typ, strike = p
        by_exp.setdefault(exp, []).append((typ, strike, o))
    future = sorted(e for e in by_exp if e >= today)
    if not future:
        return None
    exp = future[0]

    calls, puts = {}, {}
    for typ, strike, o in by_exp[exp]:
        if abs(strike - spot) > window:
            continue
        oi = float(o.get("open_interest") or 0)
        gm = float(o.get("gamma") or 0)
        bucket = calls if typ == "C" else puts
        b = bucket.setdefault(strike, {"oi": 0.0, "gamma": 0.0})
        b["oi"] += oi
        b["gamma"] = gm or b["gamma"]

    strikes = sorted(set(calls) | set(puts))
    if not strikes:
        return None

    def gex(oi, gamma, is_call):
        return (1 if is_call else -1) * oi * gamma * spot * 100

    net_by_strike, net_total, any_gamma = {}, 0.0, False
    for s in strikes:
        c = calls.get(s, {"oi": 0, "gamma": 0})
        p = puts.get(s, {"oi": 0, "gamma": 0})
        if c["gamma"] or p["gamma"]:
            any_gamma = True
        n = (gex(c["oi"], c["gamma"], True) + gex(p["oi"], p["gamma"], False)) / 1e6
        net_by_strike[s] = n
        net_total += n
    if not any_gamma:
        return None

    king_strike = max(net_by_strike, key=lambda s: abs(net_by_strike[s]))
    king_gex = net_by_strike[king_strike]

    cum, prev, gamma_flip = 0.0, None, king_strike
    for s in strikes:
        cum += net_by_strike[s]
        if prev is not None and ((prev < 0 <= cum) or (prev > 0 >= cum)):
            gamma_flip = s
        prev = cum

    calls_above = [(s, calls[s]["oi"]) for s in calls if s > spot]
    puts_below  = [(s, puts[s]["oi"])  for s in puts  if s < spot]
    call_wall = max(calls_above, key=lambda x: x[1])[0] if calls_above else None
    put_wall  = max(puts_below,  key=lambda x: x[1])[0] if puts_below  else None

    max_pain, best = None, None
    for k in strikes:
        pay = sum(calls.get(s, {}).get("oi", 0) * max(0.0, k - s) for s in strikes) \
            + sum(puts.get(s, {}).get("oi", 0)  * max(0.0, s - k) for s in strikes)
        if best is None or pay < best:
            best, max_pain = pay, k

    return {
        "spx_spot":    round(spot, 2),
        "expiry":      exp,
        "direction":   "call" if king_gex >= 0 else "put",
        "regime":      "positive" if spot >= gamma_flip else "negative",
        "net_gex":     round(net_total, 1),
        "king":        king_strike,
        "king_gex":    round(king_gex, 1),
        "gamma_flip":  gamma_flip,
        "call_wall":   call_wall,
        "put_wall":    put_wall,
        "max_pain":    max_pain,
    }


def compute(spy_spot=None, payload=None):
    """Fetch (or accept) SPX data and return the signal with SPY-scaled levels."""
    try:
        raw = analyze(payload if payload is not None else fetch_json(CBOE_SPX_URL))
    except Exception:
        return None
    if not raw:
        return None
    ratio = round(raw["spx_spot"] / spy_spot, 4) if spy_spot else 10.0

    def to_spy(v):
        return round(v / ratio, 2) if v else None

    raw["ratio"] = ratio
    raw["levels_spy"] = {
        "king":       to_spy(raw["king"]),
        "gamma_flip": to_spy(raw["gamma_flip"]),
        "call_wall":  to_spy(raw["call_wall"]),
        "put_wall":   to_spy(raw["put_wall"]),
        "max_pain":   to_spy(raw["max_pain"]),
    }
    return raw


def confirmation(spy_direction, spy_spot=None, payload=None):
    """Verdict + confidence multiplier for a SPY direction, judged against SPX.

    Returns dict with label/mult/reason and the full SPX signal, or None."""
    s = compute(spy_spot=spy_spot, payload=payload)
    if not s:
        return None
    aligned = (s["direction"] == spy_direction)
    s["aligned"] = aligned
    s["label"]   = "ALIGNED" if aligned else "DIVERGENT"
    s["mult"]    = ALIGN_BOOST if aligned else DIVERGE_PENALTY
    pct = "+15%" if aligned else "-25%"
    verb = "confirms" if aligned else "diverges from"
    s["reason"] = (f"SPX real-money {s['direction'].upper()} bias {verb} SPY "
                   f"{spy_direction.upper()} ({pct} conf) · king ${s['king']:.0f}")
    return s


if __name__ == "__main__":
    import sys
    spy = float(sys.argv[1]) if len(sys.argv) > 1 else None
    print(json.dumps(compute(spy_spot=spy), indent=2))
