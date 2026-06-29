#!/usr/bin/env python3
"""HEATSEEKER Vol Signal Dashboard — professional options flow + GEX platform."""
import json, os, threading, time, logging, pathlib, math
from datetime import datetime, date
from zoneinfo import ZoneInfo
from flask import Flask, render_template, jsonify

app = Flask(__name__)
ET  = ZoneInfo("America/New_York")
log = logging.getLogger("heatseeker.web")
logging.basicConfig(level=logging.INFO, format="%(asctime)s  %(levelname)s  %(message)s")

DATA_DIR     = os.path.join(os.path.dirname(__file__), "data")
GEX_STATE    = os.path.join(DATA_DIR, "gex_state.json")
JOURNAL_FILE = os.path.join(DATA_DIR, "trades.json")
GOAL         = 10_000.0
VIX_UUID     = "3b912aa2-88f9-4682-8ae3-e39520bdf4db"
ACCOUNT_NUMBER = "634079917"

_state = {
    "spot": None, "vix": None, "vix_prev": None, "vix3m": None,
    "ts_slope": None, "ts_label": None,
    "vanna_label": None, "vanna_mult": None,
    "gamma_flip": None, "call_wall": None, "put_wall": None,
    "gex_total_m": None, "max_pain": None,
    "strike_ladder": [],   # [{strike, call_gex, put_gex, net_gex, call_oi, put_oi}]
    "flow_feed": [],       # [{time, strike, type, expiry, premium_k, sentiment, contracts}]
    "net_flow_call_pct": 50,
    "balance": None, "last_updated": None,
    "last_action": "Starting up...",
}
_lock = threading.Lock()
_rh_ok = False


# ── Robinhood auth ────────────────────────────────────────────────────────────

def _rh_login():
    global _rh_ok
    u = os.getenv("RH_USERNAME", "")
    p = os.getenv("RH_PASSWORD", "")
    if not u or not p:
        log.warning("RH_USERNAME/RH_PASSWORD not set")
        return False
    try:
        import robin_stocks.robinhood as rh
        session_dir = pathlib.Path(os.getenv("RH_SESSION_DIR", "/data/rh_session"))
        session_dir.mkdir(parents=True, exist_ok=True)
        tokens = pathlib.Path.home() / ".tokens"
        if not tokens.exists():
            try: tokens.symlink_to(session_dir)
            except Exception: pass
        rh.login(username=u, password=p,
                 mfa_code=os.getenv("RH_MFA_CODE") or None,
                 store_session=True, expiresIn=86400, pickle_name="heatseeker")
        _rh_ok = True
        log.info("Robinhood login OK")
        return True
    except Exception as e:
        log.warning(f"Robinhood login failed: {e}")
        _rh_ok = False
        return False


# ── GEX + flow computation ────────────────────────────────────────────────────

def _norm_delta(delta_str):
    try: return abs(float(delta_str or 0))
    except: return 0.0

def _norm_gamma(gamma_str):
    try: return abs(float(gamma_str or 0))
    except: return 0.0

def _compute_gex(chain_by_strike, spot):
    """Build strike ladder, gamma flip, call/put walls, max pain from options chain."""
    ladder = []
    for strike, data in sorted(chain_by_strike.items()):
        c = data.get("call", {})
        p = data.get("put", {})
        c_oi  = int(c.get("open_interest", 0) or 0)
        p_oi  = int(p.get("open_interest", 0) or 0)
        c_g   = _norm_gamma(c.get("gamma"))
        p_g   = _norm_gamma(p.get("gamma"))
        c_gex = c_oi * c_g * 100
        p_gex = p_oi * p_g * 100
        net   = c_gex - p_gex
        ladder.append({
            "strike": strike,
            "call_gex": round(c_gex / 1e6, 3),
            "put_gex":  round(p_gex / 1e6, 3),
            "net_gex":  round(net / 1e6, 3),
            "call_oi":  c_oi,
            "put_oi":   p_oi,
            "total_oi": c_oi + p_oi,
        })

    if not ladder:
        return ladder, None, None, None, None, None

    # Gamma flip: strike where cumulative net GEX crosses zero (nearest to spot)
    cum = 0.0
    gamma_flip = None
    for row in ladder:
        prev = cum
        cum += row["net_gex"]
        if prev * cum < 0:  # sign change
            gamma_flip = row["strike"]
            break
    if gamma_flip is None:
        gamma_flip = min(ladder, key=lambda r: abs(r["strike"] - spot))["strike"]

    # Call wall = strike > spot with highest call OI
    above = [r for r in ladder if r["strike"] > spot]
    call_wall = max(above, key=lambda r: r["call_oi"])["strike"] if above else None

    # Put wall = strike < spot with highest put OI
    below = [r for r in ladder if r["strike"] < spot]
    put_wall = max(below, key=lambda r: r["put_oi"])["strike"] if below else None

    # Max pain = strike that minimizes total option dollar value at expiry
    max_pain = None
    min_pain_val = float("inf")
    for test in ladder:
        ts = test["strike"]
        val = sum(
            r["call_oi"] * max(ts - r["strike"], 0) * 100 +
            r["put_oi"]  * max(r["strike"] - ts, 0) * 100
            for r in ladder
        )
        if val < min_pain_val:
            min_pain_val = val
            max_pain = ts

    total_gex = round(sum(r["net_gex"] for r in ladder), 2)
    return ladder, gamma_flip, call_wall, put_wall, max_pain, total_gex


def _vix_regime(vix):
    if vix < 13:  return "ultra_low",  "Ultra-Low"
    if vix < 18:  return "normal",     "Normal / Bull"
    if vix < 25:  return "elevated",   "Elevated"
    if vix < 35:  return "high_vol",   "High Vol / Fear"
    return           "crisis",     "CRISIS"

def _vanna_flow(vix_now, vix_prev):
    if not vix_prev or vix_prev <= 0:
        return "neutral", 1.0
    pct = (vix_now - vix_prev) / vix_prev
    if pct < -0.07: return "strong_vanna_bid",  1.25
    if pct < -0.03: return "vanna_bid",         1.12
    if pct >  0.07: return "vanna_sell",        0.65
    if pct >  0.03: return "vanna_headwind",    0.80
    return "neutral", 1.0


# ── Live fetch ────────────────────────────────────────────────────────────────

def _fetch_once():
    import robin_stocks.robinhood as rh
    patch = {"last_updated": datetime.now(ET).isoformat()}

    # ── SPY price
    try:
        q = (rh.stocks.get_quotes(["SPY"], info=None) or [{}])
        raw = q[0].get("last_trade_price") or q[0].get("adjusted_previous_close")
        if raw: patch["spot"] = round(float(raw), 2)
    except Exception as e:
        log.debug(f"SPY: {e}")

    # ── VIX via index-instruments endpoint
    try:
        vd = rh.helper.request_get(
            f"https://api.robinhood.com/marketdata/index-instruments/{VIX_UUID}/quotes/"
        )
        if vd and vd.get("value"):
            patch["vix"] = round(float(vd["value"]), 2)
    except Exception as e:
        log.debug(f"VIX: {e}")

    # ── VIX3M estimate + term structure
    vix_now = patch.get("vix") or _state.get("vix")
    if vix_now:
        patch["vix3m"]    = round(vix_now * 1.07, 2)
        slope             = round((patch["vix3m"] - vix_now) / vix_now, 4)
        patch["ts_slope"] = slope
        patch["ts_label"] = ("deep_contango" if slope > 0.10
                              else "contango" if slope > 0
                              else "backwardation")
        # Regime
        rk, rl = _vix_regime(vix_now)
        patch["vix_regime"] = rk
        patch["vix_regime_label"] = rl
        patch["vix_regime_ok"] = vix_now < 30
        # Vanna flow
        vix_prev = _state.get("vix_prev")
        vl, vm = _vanna_flow(vix_now, vix_prev)
        patch["vanna_label"] = vl
        patch["vanna_mult"]  = vm
        patch["vix_prev"]    = vix_now   # roll for next tick

    # ── Options chain → GEX ladder + levels
    spot = patch.get("spot") or _state.get("spot")
    if spot:
        try:
            today  = date.today().isoformat()
            chains = rh.options.get_options_instrument_data_in_strategy(
                "SPY", today, "both", info=None
            ) or []

            chain_by_strike = {}
            flow_prints = []
            total_call_prem = 0.0
            total_put_prem  = 0.0

            for opt in chains:
                try:
                    strike = round(float(opt.get("strike_price", 0)), 0)
                    otype  = opt.get("type", "").lower()
                    if not strike or otype not in ("call", "put"): continue
                    if strike not in chain_by_strike:
                        chain_by_strike[strike] = {}
                    chain_by_strike[strike][otype] = opt
                    # Flow feed — collect big premium prints
                    vol  = int(opt.get("volume", 0) or 0)
                    mk   = float(opt.get("mark_price", 0) or opt.get("ask_price", 0) or 0)
                    prem = vol * mk * 100
                    if prem > 50_000:   # $50k+ prints only
                        flow_prints.append({
                            "strike":    int(strike),
                            "type":      otype,
                            "expiry":    opt.get("expiration_date", today),
                            "premium_k": round(prem / 1000, 1),
                            "contracts": vol,
                            "sentiment": "bullish" if otype == "call" else "bearish",
                            "time":      datetime.now(ET).strftime("%H:%M"),
                        })
                    if otype == "call": total_call_prem += prem
                    else:               total_put_prem  += prem
                except Exception:
                    continue

            # Only process strikes within ±30 of spot (keep ladder readable)
            filtered = {k: v for k, v in chain_by_strike.items()
                        if abs(k - spot) <= 30}
            ladder, gflip, cwall, pwall, mpain, total_gex = _compute_gex(filtered, spot)

            if ladder:
                patch["strike_ladder"] = ladder
            if gflip:  patch["gamma_flip"]  = gflip
            if cwall:  patch["call_wall"]   = cwall
            if pwall:  patch["put_wall"]    = pwall
            if mpain:  patch["max_pain"]    = mpain
            if total_gex is not None:
                patch["gex_total_m"] = total_gex

            # Net flow %
            total_prem = total_call_prem + total_put_prem
            if total_prem > 0:
                patch["net_flow_call_pct"] = round(total_call_prem / total_prem * 100, 1)

            # Flow feed — top 10 by premium
            flow_prints.sort(key=lambda x: x["premium_k"], reverse=True)
            patch["flow_feed"] = flow_prints[:10]

            patch["last_action"] = (
                f"SPY ${spot:.2f}  VIX {vix_now:.2f}  "
                f"GEX {total_gex:+.1f}M  Flip ${gflip}  "
                f"Walls ${pwall}/${cwall}"
            ) if gflip and cwall and pwall else f"SPY ${spot:.2f}  VIX {vix_now:.2f}  Chain loaded"

        except Exception as e:
            log.debug(f"Options chain: {e}")
            patch["last_action"] = f"SPY ${spot:.2f}  VIX {vix_now:.2f}  (chain unavailable)"

    # ── Account balance
    try:
        ph  = rh.account.load_phoenix_account()
        bal = None
        if ph:
            abp = ph.get("account_buying_power") or {}
            bal = float(abp.get("amount", 0) or 0) or None
        if not bal:
            port = rh.account.load_portfolio_profile()
            if port:
                bal = float(port.get("withdrawable_amount") or port.get("excess_margin") or 0) or None
        if bal: patch["balance"] = round(bal, 2)
    except Exception as e:
        log.debug(f"Balance: {e}")

    with _lock:
        _state.update(patch)

    # Persist
    os.makedirs(DATA_DIR, exist_ok=True)
    try:
        try:
            with open(GEX_STATE) as f: disk = json.load(f)
        except Exception: disk = {}
        disk.update({k: v for k, v in patch.items() if k not in ("strike_ladder", "flow_feed")})
        with open(GEX_STATE, "w") as f: json.dump(disk, f, indent=2)
    except Exception as e:
        log.debug(f"State write: {e}")


def _live_thread():
    # Restore persisted state
    try:
        with open(GEX_STATE) as f:
            saved = json.load(f)
        with _lock:
            _state.update({k: v for k, v in saved.items() if v is not None})
    except Exception:
        pass

    # Login with retry
    while not _rh_login():
        with _lock: _state["last_action"] = "Waiting for Robinhood auth — retrying in 5 min"
        for _ in range(300): time.sleep(1)

    # Fetch loop
    while True:
        try:
            _fetch_once()
            with _lock:
                log.info(f"Tick OK: SPY={_state.get('spot')} VIX={_state.get('vix')} gex={_state.get('gex_total_m')}M")
        except Exception as e:
            log.warning(f"Fetch error: {e}")
        for _ in range(60): time.sleep(1)


def start_live_thread():
    threading.Thread(target=_live_thread, daemon=True).start()


# ── Journal helpers ───────────────────────────────────────────────────────────

def _load_journal():
    try:
        with open(JOURNAL_FILE) as f: return json.load(f).get("trades", [])
    except Exception: return []

def _journal_stats(trades):
    closed = [t for t in trades if t.get("pnl_pct") is not None]
    recent = closed[-20:]
    wins   = [t for t in recent if t["pnl_pct"] > 0]
    losses = [t for t in recent if t["pnl_pct"] <= 0]
    wr     = len(wins) / len(recent) if recent else 0.5
    total  = sum(t.get("pnl_usd", 0) or 0 for t in closed)
    streak, stype = 0, None
    for t in reversed(recent):
        w = t["pnl_pct"] > 0
        if stype is None: stype = "win" if w else "loss"; streak = 1
        elif (stype == "win") == w: streak += 1
        else: break
    with _lock: bal = _state.get("balance")
    if not bal and closed: bal = closed[-1].get("balance_after")
    bal = bal or 21.64
    return {
        "total_trades": len(closed), "win_rate": round(wr, 3),
        "total_pnl_usd": round(total, 2), "streak": streak, "streak_type": stype,
        "balance": round(bal, 2), "goal_pct": round(bal / GOAL * 100, 2),
    }

def _session():
    now = datetime.now(ET); t = now.hour * 60 + now.minute
    if now.weekday() >= 5: return "closed"
    if t < 570: return "pre"
    if t < 960: return "open"
    return "after"


# ── Routes ────────────────────────────────────────────────────────────────────

@app.route("/")
def dashboard():
    return render_template("dashboard.html")

@app.route("/api/state")
def api_state():
    with _lock: g = dict(_state)
    trades = _load_journal()
    stats  = _journal_stats(trades)
    return jsonify({
        "gex": g, "stats": stats,
        "journal": trades[-10:],
        "session": _session(),
        "ts": datetime.now(ET).strftime("%H:%M:%S ET"),
    })

@app.route("/api/health")
def health():
    return jsonify({"ok": True})


if __name__ == "__main__":
    start_live_thread()
    port = int(os.getenv("PORT", 5000))
    app.run(host="0.0.0.0", port=port, debug=False)

start_live_thread()
