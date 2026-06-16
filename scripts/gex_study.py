#!/usr/bin/env python3
"""
GEX Pattern Study Engine
Tracks dealer hedging patterns, scores current GEX setup against
historical outcomes, and outputs a confidence multiplier for sizing.
"""
import math, logging
from scripts.journal import load as load_journal

log = logging.getLogger("heatseeker.gex")


# ── GEX Feature Extraction ────────────────────────────────────────────────────

def extract_gex_features(calls, puts, spot, nodes, king_strike, king_gex):
    """
    Compute dealer positioning features from the live options chain.
    These get stored in the journal and used for pattern learning.
    """
    total_call_gex = sum(abs(v) for s, v in nodes if v > 0)
    total_put_gex  = sum(abs(v) for s, v in nodes if v < 0)
    total_abs_gex  = total_call_gex + total_put_gex

    # GEX concentration: how much of total is in the king node?
    concentration = abs(king_gex) / total_abs_gex if total_abs_gex > 0 else 0

    # GEX skew: what fraction is call-side vs put-side?
    gex_skew = total_call_gex / total_abs_gex if total_abs_gex > 0 else 0.5

    # Top-5 node consensus: do the biggest nodes agree on direction?
    top5 = sorted(nodes, key=lambda x: abs(x[1]), reverse=True)[:5]
    king_sign = 1 if king_gex >= 0 else -1
    consensus = sum(1 for s, v in top5 if (1 if v >= 0 else -1) == king_sign) / len(top5) if top5 else 0

    # Distance from king node to spot (as %)
    king_dist_pct = abs(king_strike - spot) / spot if spot > 0 else 0

    # Call wall: highest call OI above spot
    call_oi_above = [(float(o["strike_price"]), o.get("open_interest", 0))
                     for o in calls if float(o["strike_price"]) > spot and o.get("open_interest", 0) > 0]
    call_wall = max(call_oi_above, key=lambda x: x[1])[0] if call_oi_above else spot + 5
    call_wall_dist_pct = (call_wall - spot) / spot

    # Put wall: highest put OI below spot
    put_oi_below = [(float(o["strike_price"]), o.get("open_interest", 0))
                    for o in puts if float(o["strike_price"]) < spot and o.get("open_interest", 0) > 0]
    put_wall = max(put_oi_below, key=lambda x: x[1])[0] if put_oi_below else spot - 5
    put_wall_dist_pct = (spot - put_wall) / spot

    # Gamma flip: strike where cumulative GEX crosses zero
    cum = 0
    gamma_flip = None
    for s, v in sorted(nodes):
        prev = cum
        cum += v
        if prev * cum < 0 and prev != 0:
            gamma_flip = s
            break
    gamma_flip = gamma_flip or king_strike
    gamma_flip_dist_pct = (spot - gamma_flip) / spot  # + = flip below price

    # Net dealer delta direction (simplified from OI imbalance)
    net_call_oi = sum(o.get("open_interest", 0) for o in calls)
    net_put_oi  = sum(o.get("open_interest", 0) for o in puts)
    dealer_delta = "long" if net_call_oi > net_put_oi else "short"

    return {
        "gex_total_m":         round(total_abs_gex / 1e6, 2),
        "gex_sign":            king_sign,
        "gex_concentration":   round(concentration, 3),
        "gex_skew":            round(gex_skew, 3),
        "top5_consensus":      round(consensus, 2),
        "king_dist_pct":       round(king_dist_pct * 100, 3),
        "call_wall":           call_wall,
        "call_wall_dist_pct":  round(call_wall_dist_pct * 100, 3),
        "put_wall":            put_wall,
        "put_wall_dist_pct":   round(put_wall_dist_pct * 100, 3),
        "gamma_flip":          gamma_flip,
        "gamma_flip_dist_pct": round(gamma_flip_dist_pct * 100, 3),
        "dealer_delta":        dealer_delta,
        "net_call_oi":         net_call_oi,
        "net_put_oi":          net_put_oi,
    }


# ── Pattern Learning ──────────────────────────────────────────────────────────

def _bucket(value, thresholds):
    """Map a value to a named bucket for pattern grouping."""
    for label, lo, hi in thresholds:
        if lo <= value < hi:
            return label
    return thresholds[-1][0]

def _classify_trade(t):
    """Classify a trade into GEX pattern buckets."""
    gf = t.get("gex_features") or {}
    if not gf:
        return None
    return {
        "gex_size":      _bucket(gf.get("gex_total_m", 0),
                                 [("tiny", 0, 20), ("small", 20, 60),
                                  ("medium", 60, 150), ("large", 150, 9999)]),
        "concentration": _bucket(gf.get("gex_concentration", 0),
                                 [("spread", 0, 0.30), ("moderate", 0.30, 0.60),
                                  ("concentrated", 0.60, 1.01)]),
        "consensus":     _bucket(gf.get("top5_consensus", 0),
                                 [("mixed", 0, 0.60), ("aligned", 0.60, 0.81),
                                  ("strong", 0.81, 1.01)]),
        "king_dist":     _bucket(gf.get("king_dist_pct", 0),
                                 [("atm", 0, 0.30), ("near", 0.30, 0.80),
                                  ("far", 0.80, 99)]),
        "gex_sign":      "bullish" if gf.get("gex_sign", 1) > 0 else "bearish",
    }

def analyze_patterns(n_recent=30):
    """
    Study the last n closed trades. Return win rates by GEX pattern bucket
    and a ranked list of which patterns perform best.
    """
    data   = load_journal()
    closed = [t for t in data["trades"] if t.get("pnl_pct") is not None]
    recent = closed[-n_recent:]

    pattern_stats = {}
    for t in recent:
        cls = _classify_trade(t)
        if not cls:
            continue
        win = t["pnl_pct"] > 0
        for feature, bucket in cls.items():
            key = f"{feature}:{bucket}"
            s   = pattern_stats.setdefault(key, {"wins": 0, "total": 0, "pnl": []})
            s["wins"]  += int(win)
            s["total"] += 1
            s["pnl"].append(t["pnl_pct"])

    results = []
    for key, s in pattern_stats.items():
        if s["total"] < 2:
            continue
        wr  = s["wins"] / s["total"]
        avg = sum(s["pnl"]) / len(s["pnl"])
        results.append({"pattern": key, "win_rate": round(wr, 2),
                        "avg_pnl": round(avg, 1), "n": s["total"]})

    return sorted(results, key=lambda x: x["win_rate"], reverse=True)


# ── GEX Confidence Score ──────────────────────────────────────────────────────

def gex_confidence_score(gex_features, direction):
    """
    Score 0–1 based on current GEX setup quality.
    Combines structural strength, consensus, and historical pattern win rates.
    """
    if not gex_features:
        return 0.5, []

    score    = 0.5
    reasons  = []
    patterns = analyze_patterns()
    pat_map  = {p["pattern"]: p for p in patterns}

    f = gex_features

    # 1. GEX total size — bigger = more pinning force
    if f["gex_total_m"] >= 150:
        score += 0.12; reasons.append(f"Strong GEX ${f['gex_total_m']:.0f}M (+12%)")
    elif f["gex_total_m"] >= 60:
        score += 0.06; reasons.append(f"Moderate GEX ${f['gex_total_m']:.0f}M (+6%)")
    elif f["gex_total_m"] < 20:
        score -= 0.10; reasons.append(f"Weak GEX ${f['gex_total_m']:.0f}M (-10%)")

    # 2. Top-5 node consensus
    if f["top5_consensus"] >= 0.80:
        score += 0.10; reasons.append(f"Strong node consensus {f['top5_consensus']*100:.0f}% (+10%)")
    elif f["top5_consensus"] < 0.60:
        score -= 0.08; reasons.append(f"Mixed nodes {f['top5_consensus']*100:.0f}% (-8%)")

    # 3. GEX concentration — tight king = stronger magnet
    if f["gex_concentration"] >= 0.60:
        score += 0.07; reasons.append(f"Concentrated king node {f['gex_concentration']*100:.0f}% (+7%)")

    # 4. King node distance from price
    if f["king_dist_pct"] < 0.30:
        score += 0.08; reasons.append("King ATM — strong pin magnet (+8%)")
    elif f["king_dist_pct"] > 0.80:
        score -= 0.06; reasons.append(f"King far from price {f['king_dist_pct']:.2f}% (-6%)")

    # 5. Gamma flip location
    gfd = f["gamma_flip_dist_pct"]
    if direction == "call" and gfd > 0:
        score += 0.06; reasons.append(f"Gamma flip below price — dealer tailwind (+6%)")
    elif direction == "put" and gfd < 0:
        score += 0.06; reasons.append(f"Gamma flip above price — dealer tailwind (+6%)")
    else:
        score -= 0.05; reasons.append("Trading against gamma flip (-5%)")

    # 6. Historical pattern boost
    cls = _classify_trade({"gex_features": f})
    if cls:
        hist_scores = []
        for feature, bucket in cls.items():
            key = f"{feature}:{bucket}"
            if key in pat_map and pat_map[key]["n"] >= 3:
                hist_scores.append(pat_map[key]["win_rate"])
        if hist_scores:
            avg_hist = sum(hist_scores) / len(hist_scores)
            adj = (avg_hist - 0.5) * 0.20   # up to ±10%
            score += adj
            reasons.append(f"Historical pattern match: {avg_hist*100:.0f}% WR ({adj:+.0%})")

    score = max(0.10, min(0.95, score))
    return round(score, 3), reasons


# ── Dealer Flow Display ───────────────────────────────────────────────────────

def print_dealer_flow(gex_features, spot, vix, direction, confidence, reasons):
    f = gex_features
    gex_sign_label = "POSITIVE (dealers LONG gamma → PIN / mean-revert)" \
                     if f["gex_sign"] > 0 else \
                     "NEGATIVE (dealers SHORT gamma → TREND / amplify)"

    dealer_bias = "NET LONG delta → mechanical BID under SPY" \
                  if f["dealer_delta"] == "long" else \
                  "NET SHORT delta → mechanical SELL pressure on SPY"

    flip_side = "BELOW" if f["gamma_flip_dist_pct"] > 0 else "ABOVE"
    tailwind  = (direction == "call" and f["gamma_flip_dist_pct"] > 0) or \
                (direction == "put"  and f["gamma_flip_dist_pct"] < 0)

    print("\n" + "═"*62)
    print("  DEALER / HEDGER FLOW ANALYSIS")
    print("═"*62)
    print(f"  GEX Regime:     {gex_sign_label}")
    print(f"  Total GEX:      ${f['gex_total_m']:.1f}M  (concentration {f['gex_concentration']*100:.0f}%)")
    print(f"  Node Consensus: {f['top5_consensus']*100:.0f}% of top-5 agree with direction")
    print(f"  Dealer Delta:   {dealer_bias}")
    print(f"  Call Wall:      ${f['call_wall']:.0f}  ({f['call_wall_dist_pct']:+.2f}% from spot)")
    print(f"  Put Wall:       ${f['put_wall']:.0f}  ({-f['put_wall_dist_pct']:+.2f}% from spot)")
    print(f"  Gamma Flip:     ${f['gamma_flip']:.0f}  ({flip_side} price — {'✅ TAILWIND' if tailwind else '⚠️ HEADWIND'})")
    print(f"  GEX Skew:       {f['gex_skew']*100:.0f}% call-side / {(1-f['gex_skew'])*100:.0f}% put-side")
    print("─"*62)
    print(f"  Signal Dir:     {direction.upper()}")
    print(f"  GEX Confidence: {confidence*100:.0f}%")
    for r in reasons:
        print(f"    {'✅' if '+' in r else '⚠️'} {r}")
    print("═"*62)


def print_pattern_report():
    """Print top/bottom GEX patterns from historical trades."""
    patterns = analyze_patterns()
    if not patterns:
        print("  No pattern history yet — accumulating trades...")
        return
    print("\n" + "─"*62)
    print("  GEX PATTERN LEARNING (historical win rates)")
    print("─"*62)
    top = [p for p in patterns if p["n"] >= 3][:5]
    bot = [p for p in reversed(patterns) if p["n"] >= 3][:3]
    if top:
        print("  Best patterns:")
        for p in top:
            print(f"    {p['pattern']:35s}  WR={p['win_rate']*100:.0f}%  avg={p['avg_pnl']:+.0f}%  n={p['n']}")
    if bot:
        print("  Worst patterns:")
        for p in bot:
            print(f"    {p['pattern']:35s}  WR={p['win_rate']*100:.0f}%  avg={p['avg_pnl']:+.0f}%  n={p['n']}")
    print("─"*62)
