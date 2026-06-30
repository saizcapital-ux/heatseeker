#!/usr/bin/env python3
import os, sys, json, logging
from datetime import date
import robin_stocks.robinhood as rh
from scripts.journal import log_exit, load as load_journal

logging.basicConfig(level=logging.INFO, format="%(asctime)s  %(levelname)s  %(message)s", handlers=[logging.StreamHandler(sys.stdout)])
log = logging.getLogger("heatseeker.exit")

DRY_RUN        = os.getenv("DRY_RUN", "false").lower() == "true"
FORCE_CLOSE    = os.getenv("FORCE_CLOSE", "false").lower() == "true"
ACCOUNT_NUMBER = "634079917"
STOP_LOSS      = -0.50
TRAIL_ACTIVATE = 1.00   # trailing stop kicks in at +100% (or +30% if regime bullish)
TRAIL_DROP     = 0.25   # sell if price falls 25% from peak

# Regime-aware thresholds
REGIME_TRAIL_ACTIVATE = 0.30  # activate trailing stop early at +30% if regime still bullish
VIX_CRISIS       = 30.0       # above this → exit calls immediately
SLOPE_BEARISH    = -0.05      # term structure below this → exit calls immediately

DATA_DIR   = os.path.join(os.path.dirname(os.path.dirname(__file__)), "data")
GEX_STATE  = os.path.join(DATA_DIR, "gex_state.json")
STATE_FILE = os.path.join(os.getenv("RH_SESSION_DIR", "/data/rh_session"), "trail_state.json")

def get_regime():
    """Read current regime from gex_state.json. Returns dict with vix, ts_slope, direction, ts_label."""
    try:
        with open(GEX_STATE) as f:
            s = json.load(f)
        return {
            "vix":       float(s.get("vix") or 0),
            "ts_slope":  float(s.get("ts_slope") or 0),
            "ts_label":  s.get("ts_label", ""),
            "direction": s.get("direction", ""),
        }
    except Exception as e:
        log.warning(f"Could not read regime state: {e}")
        return {"vix": 0, "ts_slope": 0, "ts_label": "", "direction": ""}

def regime_is_bullish(regime):
    """True if regime still supports holding calls."""
    if regime["vix"] and regime["vix"] >= VIX_CRISIS:
        return False
    if regime["ts_slope"] and regime["ts_slope"] < SLOPE_BEARISH:
        return False
    if regime["direction"] and regime["direction"] == "bearish":
        return False
    return True

def regime_flipped_bearish(regime, opt_type):
    """True if regime has turned against the current position."""
    if opt_type == "call":
        if regime["vix"] >= VIX_CRISIS:
            return True, f"VIX crisis ({regime['vix']:.1f} >= {VIX_CRISIS})"
        if regime["ts_slope"] < SLOPE_BEARISH:
            return True, f"Backwardation (slope={regime['ts_slope']:.3f})"
        if regime["direction"] == "bearish":
            return True, "Regime flipped bearish"
    return False, ""

def load_state():
    try:
        with open(STATE_FILE) as f:
            return json.load(f)
    except Exception:
        return {}

def save_state(state):
    try:
        with open(STATE_FILE, "w") as f:
            json.dump(state, f)
    except Exception as e:
        log.warning(f"Could not save trail state: {e}")

def login():
    user = os.getenv("RH_USERNAME", "")
    pwd  = os.getenv("RH_PASSWORD", "")
    mfa  = os.getenv("RH_MFA_CODE", "")
    if not user or not pwd:
        log.error("RH_USERNAME and RH_PASSWORD must be set.")
        sys.exit(1)
    import pathlib
    session_dir = pathlib.Path(os.getenv("RH_SESSION_DIR", "/data/rh_session"))
    session_dir.mkdir(parents=True, exist_ok=True)
    tokens_dir = pathlib.Path.home() / ".tokens"
    if not tokens_dir.exists():
        tokens_dir.symlink_to(session_dir)
    elif tokens_dir.is_symlink() and tokens_dir.resolve() != session_dir.resolve():
        tokens_dir.unlink()
        tokens_dir.symlink_to(session_dir)
    log.info("Logging in to Robinhood...")
    try:
        rh.login(username=user, password=pwd, mfa_code=mfa or None,
                 store_session=True, expiresIn=86400, pickle_name="heatseeker")
    except Exception as e:
        log.error(f"Login exception: {e}")
        sys.exit(1)
    try:
        if not rh.profiles.load_portfolio_profile():
            raise Exception("empty")
        log.info("Login verified OK")
    except Exception as e:
        log.error(f"Login failed: {e}")
        sys.exit(1)

def get_spy_0dte_positions():
    today = date.today().strftime("%Y-%m-%d")
    positions = rh.options.get_open_option_positions() or []
    result = []
    for p in positions:
        if float(p.get("quantity", 0)) <= 0: continue
        url = p.get("option")
        if not url: continue
        try:
            inst = rh.options.get_option_instrument_data_by_url(url)
        except Exception:
            continue
        if not inst: continue
        if inst.get("chain_symbol") == "SPY" and inst.get("expiration_date") == today:
            p["instrument"] = inst
            p["expiration"] = today
            p["opt_type"]   = inst.get("type", "call")
            p["strike"]     = float(inst.get("strike_price", 0))
            p["qty"]        = int(float(p.get("quantity", 0)))
            result.append(p)
    return result

def get_current_price(position):
    try:
        md = rh.options.get_option_market_data_by_url(position.get("option"))
        if md:
            return float(md.get("bid_price", 0) or 0), float(md.get("ask_price", 0) or 0), float(md.get("adjusted_mark_price", 0) or 0)
    except Exception:
        pass
    return 0.0, 0.0, 0.0

def close_position(position, limit_price):
    strike = position["strike"]
    exp    = position["expiration"]
    otype  = position["opt_type"]
    qty    = position["qty"]
    symbol = position["instrument"].get("chain_symbol", "SPY")
    log.info(f"{'[DRY RUN] ' if DRY_RUN else ''}CLOSE: SELL {qty}x {symbol} {strike}{otype[0].upper()} exp={exp} limit=${limit_price:.2f}")
    if DRY_RUN:
        return {"dry_run": True}
    order = rh.orders.order_sell_option_limit(
        positionEffect="close", creditOrDebit="credit", price=limit_price,
        symbol=symbol, quantity=qty, expirationDate=exp,
        strike=str(int(strike)), optionType=otype, timeInForce="gfd",
        account_number=ACCOUNT_NUMBER,
    )
    log.info(f"Close order: {json.dumps(order, indent=2)}")
    return order

def position_key(p):
    return f"{p['expiration']}_{p['strike']}_{p['opt_type']}"

def main():
    login()
    today = date.today().strftime("%Y-%m-%d")
    log.info(f"Exit check: {today}  FORCE_CLOSE={FORCE_CLOSE}")

    state = load_state()
    # Clear stale state from prior trading days
    state = {k: v for k, v in state.items() if k.startswith(today)}

    regime = get_regime()
    bullish = regime_is_bullish(regime)
    log.info(f"Regime: VIX={regime['vix']} slope={regime['ts_slope']:.3f} dir={regime['direction']} bullish={bullish}")

    positions = get_spy_0dte_positions()
    print("\n" + "="*60)
    print(f"HEATSEEKER EXIT CHECK - {today}")
    print("="*60)
    print(f"  Account: {ACCOUNT_NUMBER} (agentic)")

    if not positions:
        log.info("No open SPY 0DTE positions.")
        print("  No open positions.")
        print("="*60)
        save_state(state)
        return

    for p in positions:
        avg  = float(p.get("average_price", 0) or 0)
        bid, ask, mark = get_current_price(p)
        pnl_pct = (mark - avg) / avg if avg > 0 else 0
        pnl_usd = (mark - avg) * p["qty"] * 100
        key     = position_key(p)

        log.info(f"POSITION: SPY {p['strike']}{p['opt_type'][0].upper()} qty={p['qty']} avg=${avg:.2f} mark=${mark:.2f} P&L={pnl_pct*100:+.1f}% (${pnl_usd:+.2f})")
        print(f"  SPY {p['strike']}{p['opt_type'][0].upper()}: avg=${avg:.2f} mark=${mark:.2f} P&L={pnl_pct*100:+.1f}% (${pnl_usd:+.2f})")

        reason = ""
        opt_type = p.get("opt_type", "call")
        # Activate trailing stop early (+30%) if regime bullish, otherwise wait for +100%
        activate_threshold = REGIME_TRAIL_ACTIVATE if bullish else TRAIL_ACTIVATE

        if FORCE_CLOSE:
            reason = "FORCE CLOSE (3:30 PM ET)"

        else:
            # Regime flip check — exit immediately if market turned against position
            flipped, flip_reason = regime_flipped_bearish(regime, opt_type)
            if flipped and pnl_pct > 0:
                reason = f"REGIME FLIP EXIT: {flip_reason}"

            if not reason and (pnl_pct >= activate_threshold or state.get(key, {}).get("activated")):
                prev_high = state.get(key, {}).get("high", 0)
                new_high  = max(mark, prev_high)
                state[key] = {"high": new_high, "activated": True}
                trail_stop = new_high * (1 - TRAIL_DROP)

                log.info(f"TRAILING STOP active: peak=${new_high:.4f}  trail_stop=${trail_stop:.4f}  mark=${mark:.4f}  regime_bullish={bullish}")

                if mark <= trail_stop:
                    reason = f"TRAILING STOP HIT (peak=${new_high:.4f}, dropped {TRAIL_DROP*100:.0f}% to ${mark:.4f})"
                else:
                    pct_from_peak = (new_high - mark) / new_high * 100
                    regime_note = "regime BULLISH — riding" if bullish else "regime NEUTRAL — cautious"
                    print(f"  --> TRAILING STOP ACTIVE ({regime_note}): peak=${new_high:.4f}  stop=${trail_stop:.4f}  now={pct_from_peak:.1f}% below peak — HOLD")

            elif not reason and pnl_pct <= STOP_LOSS:
                reason = f"STOP LOSS ({pnl_pct*100:.0f}%)"

        if reason:
            log.info(f"ACTION: {reason} - CLOSING")
            print(f"  --> {reason} - CLOSING")
            close_position(p, max(round(bid, 2), 0.01))
            if key in state:
                del state[key]
            # Log exit to journal
            if not DRY_RUN:
                try:
                    trade_id = f"{today}_{p['strike']}_{p['opt_type']}"
                    balance_after = avg + (mark - avg)  # rough; actual will be fetched next run
                    log_exit(trade_id, mark, reason, balance_after)
                except Exception as je:
                    log.warning(f"Journal exit log failed (non-fatal): {je}")
        elif not reason and not state.get(key, {}).get("activated") and pnl_pct < activate_threshold:
            log.info("ACTION: HOLD")
            print(f"  --> HOLD")

    print("="*60)
    save_state(state)

if __name__ == "__main__":
    main()
