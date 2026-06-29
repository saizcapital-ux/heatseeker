#!/usr/bin/env python3
"""
HEATSEEKER Live Dashboard
Serves a real-time trading dashboard showing bot analysis, GEX levels,
trade journal, and pattern learning.
"""
import json, os, threading, time, logging
from datetime import datetime
from zoneinfo import ZoneInfo
from flask import Flask, render_template, redirect, jsonify

app = Flask(__name__)
ET  = ZoneInfo("America/New_York")
log = logging.getLogger("heatseeker.web")

DATA_DIR     = os.path.join(os.path.dirname(__file__), "data")
# Check volume path first, fall back to local data/ dir
_vol_gex  = os.path.join(os.getenv("RH_SESSION_DIR", "/data/rh_session"), "gex_state.json")
_local_gex = os.path.join(DATA_DIR, "gex_state.json")
GEX_STATE    = _vol_gex if os.path.exists(os.path.dirname(_vol_gex)) else _local_gex
JOURNAL_FILE = os.path.join(DATA_DIR, "trades.json")
GOAL         = 10_000.0

# ── Live market data cache ────────────────────────────────────────────────────
_live = {}
_live_lock = threading.Lock()

_rh_logged_in = False
_rh_lock = threading.Lock()

def _rh_login():
    global _rh_logged_in
    if _rh_logged_in:
        return True
    u = os.getenv("RH_USERNAME", "")
    p = os.getenv("RH_PASSWORD", "")
    if not u or not p:
        return False
    try:
        import robin_stocks.robinhood as rh
        rh.login(u, p, store_session=True)
        _rh_logged_in = True
        log.info("Web: Robinhood login OK")
        return True
    except Exception as e:
        log.warning(f"Web: Robinhood login failed: {e}")
        return False

def _fetch_rh_prices():
    """Fetch SPY + VIX quotes from Robinhood. Returns (spot, vix) or Nones."""
    try:
        import robin_stocks.robinhood as rh
        quotes = rh.stocks.get_quotes(["SPY"], info=None)
        spy_q  = (quotes or [{}])[0] or {}
        raw = spy_q.get("last_trade_price") or spy_q.get("adjusted_previous_close")
        spot = round(float(raw), 2) if raw else None

        vq = rh.stocks.get_quotes(["VIX"], info=None)
        vix_q = (vq or [{}])[0] or {}
        vraw = vix_q.get("last_trade_price") or vix_q.get("adjusted_previous_close")
        vix_val = round(float(vraw), 2) if vraw else None

        return spot, vix_val
    except Exception as e:
        log.debug(f"RH price fetch error: {e}")
        return None, None

def _patch_gex_state(patch: dict):
    """Merge patch into gex_state.json on disk and in-memory cache."""
    with _live_lock:
        _live.update(patch)
    try:
        try:
            with open(GEX_STATE) as f:
                state = json.load(f)
        except Exception:
            state = {}
        state.update(patch)
        os.makedirs(os.path.dirname(GEX_STATE), exist_ok=True)
        with open(GEX_STATE, "w") as f:
            json.dump(state, f, indent=2)
    except Exception as e:
        log.warning(f"gex_state write error: {e}")

def _fetch_live_market():
    """Fetch SPY + VIX every 60 s — try Robinhood first, yfinance as fallback."""
    while True:
        try:
            now_str = datetime.now(ET).strftime("%H:%M:%S ET")
            spot = vix_val = vix3m = None

            # Primary: Robinhood (works on Railway, no Yahoo block)
            if _rh_login():
                spot, vix_val = _fetch_rh_prices()

            # Fallback: yfinance (works locally, may be blocked on cloud)
            if not spot:
                try:
                    import yfinance as yf
                    fi_spy = yf.Ticker("SPY").fast_info
                    fi_vix = yf.Ticker("^VIX").fast_info
                    fi_v3m = yf.Ticker("^VIX3M").fast_info
                    for attr in ("last_price", "regularMarketPrice", "previousClose"):
                        if not spot:
                            v = getattr(fi_spy, attr, None)
                            if v: spot = round(float(v), 2)
                        if not vix_val:
                            v = getattr(fi_vix, attr, None)
                            if v: vix_val = round(float(v), 2)
                        if not vix3m:
                            v = getattr(fi_v3m, attr, None)
                            if v: vix3m = round(float(v), 2)
                except Exception:
                    pass

            patch = {"market_updated": now_str}
            if spot:    patch["spot"]    = spot
            if vix_val: patch["vix"]     = vix_val
            if vix3m:
                patch["vix3m"] = vix3m
                if vix_val:
                    slope = round((vix3m - vix_val) / vix_val, 4)
                    patch["ts_slope"] = slope
                    patch["ts_label"] = ("deep_contango" if slope > 0.10
                                         else "contango" if slope > 0
                                         else "backwardation")

            _patch_gex_state(patch)
            log.info(f"Live tick: SPY={spot} VIX={vix_val} VIX3M={vix3m} @ {now_str}")
        except Exception as e:
            log.warning(f"Live fetch error: {e}")
        time.sleep(60)

def start_live_thread():
    t = threading.Thread(target=_fetch_live_market, daemon=True)
    t.start()

# ── Data loaders ──────────────────────────────────────────────────────────────
def load_gex_state():
    try:
        with open(GEX_STATE) as f:
            state = json.load(f)
    except Exception:
        state = {}
    # Overlay with in-memory live cache (most recent tick)
    with _live_lock:
        state.update({k: v for k, v in _live.items() if v is not None})
    return state

def load_journal():
    try:
        with open(JOURNAL_FILE) as f:
            return json.load(f).get("trades", [])
    except Exception:
        return []

def compute_stats(trades, gex_state=None):
    closed   = [t for t in trades if t.get("pnl_pct") is not None]
    recent   = closed[-20:]
    wins     = [t for t in recent if t["pnl_pct"] > 0]
    losses   = [t for t in recent if t["pnl_pct"] <= 0]
    win_rate = len(wins) / len(recent) if recent else 0.5
    avg_win  = sum(t["pnl_pct"] for t in wins) / len(wins) if wins else 100.0
    avg_loss = abs(sum(t["pnl_pct"] for t in losses) / len(losses)) if losses else 50.0
    R        = avg_win / avg_loss if avg_loss else 2.0
    kelly    = max(0.0, min(0.5, win_rate - (1 - win_rate) / R))
    total_pnl = sum(t.get("pnl_usd", 0) or 0 for t in closed)

    streak, streak_type = 0, None
    for t in reversed(recent):
        w = t["pnl_pct"] > 0
        if streak_type is None:
            streak_type = "win" if w else "loss"; streak = 1
        elif (streak_type == "win") == w:
            streak += 1
        else:
            break

    # Use live Robinhood balance from gex_state if available, else fall back to journal
    balance = None
    if gex_state:
        balance = gex_state.get("account_balance") or gex_state.get("buying_power")
    if not balance and closed:
        balance = closed[-1].get("balance_after")
    if not balance:
        balance = 21.64  # actual starting balance from Robinhood

    goal_pct = balance / GOAL * 100

    return {
        "total_trades":     len(closed),
        "win_rate":         round(win_rate, 3),
        "avg_win_pct":      round(avg_win, 1),
        "avg_loss_pct":     round(avg_loss, 1),
        "kelly":            round(kelly, 3),
        "total_pnl_usd":    round(total_pnl, 2),
        "streak":           streak,
        "streak_type":      streak_type,
        "balance":          round(balance, 2),
        "goal":             GOAL,
        "goal_progress_pct": round(goal_pct, 2),
    }

def compute_patterns(trades):
    closed = [t for t in trades if t.get("pnl_pct") is not None]
    pattern_stats = {}
    for t in closed[-30:]:
        gf = t.get("gex_features") or {}
        if not gf: continue
        win = t["pnl_pct"] > 0
        for key, val in {
            "gex_size": "large" if gf.get("gex_total_m", 0) > 150 else "medium" if gf.get("gex_total_m", 0) > 60 else "small",
            "consensus": "strong" if gf.get("top5_consensus", 0) > 0.8 else "aligned" if gf.get("top5_consensus", 0) > 0.6 else "mixed",
        }.items():
            k = f"{key}:{val}"
            s = pattern_stats.setdefault(k, {"wins": 0, "total": 0})
            s["wins"] += int(win); s["total"] += 1

    results = []
    for k, s in pattern_stats.items():
        if s["total"] < 2: continue
        results.append({"pattern": k, "win_rate": round(s["wins"] / s["total"], 2), "n": s["total"]})
    return sorted(results, key=lambda x: x["win_rate"], reverse=True)

def get_next_action():
    now = datetime.now(ET)
    h, m = now.hour, now.minute
    t = h * 60 + m
    weekday = now.weekday()
    if weekday >= 5:
        return "Market closed (weekend)"
    if t < 585:   return f"Entry in {585 - t} min (9:45 AM ET)"
    if t <= 750:  return "Entry window open (9:45 AM–12:30 PM ET)"
    if t < 810:   return "Monitoring position"
    if t < 870:   return "Approaching force-close window"
    if t < 930:   return "Force close at 3:30 PM ET"
    return "Market closed — next entry tomorrow 9:45 AM ET"

def get_market_session():
    now = datetime.now(ET)
    h, m = now.hour, now.minute
    t = h * 60 + m
    weekday = now.weekday()
    if weekday >= 5:           return "closed"
    if t < 570:                return "pre"
    if 570 <= t < 960:         return "open"
    return "after"

# ── Routes ────────────────────────────────────────────────────────────────────
@app.route("/")
def dashboard():
    gex      = load_gex_state()
    trades   = load_journal()
    stats    = compute_stats(trades, gex)
    patterns = compute_patterns(trades)
    return render_template(
        "dashboard.html",
        gex=type("G", (), gex)(),
        trades=trades,
        stats=type("S", (), stats)(),
        patterns=patterns,
        next_action=get_next_action(),
    )

@app.route("/refresh")
def refresh():
    return redirect("/")

@app.route("/api/state")
def api_state():
    trades = load_journal()
    gex = load_gex_state()
    return jsonify({
        "gex":          gex,
        "stats":        compute_stats(trades, gex),
        "journal":      trades[-10:],
        "next_action":  get_next_action(),
        "session":      get_market_session(),
        "ts":           datetime.now(ET).strftime("%H:%M:%S ET"),
    })

if __name__ == "__main__":
    start_live_thread()
    port = int(os.getenv("PORT", 5000))
    app.run(host="0.0.0.0", port=port, debug=False)
else:
    # Gunicorn / production entry point
    start_live_thread()
