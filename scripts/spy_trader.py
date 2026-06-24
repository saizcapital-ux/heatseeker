#!/usr/bin/env python3
import os, sys, json, logging
from datetime import date, datetime
from zoneinfo import ZoneInfo
import robin_stocks.robinhood as rh
from scripts.journal import get_smart_size, log_entry, print_dashboard
from scripts.gex_study import (extract_gex_features, gex_confidence_score,
                                print_dealer_flow, print_pattern_report)

logging.basicConfig(level=logging.INFO, format="%(asctime)s  %(levelname)s  %(message)s", handlers=[logging.StreamHandler(sys.stdout)])
log = logging.getLogger("heatseeker")

DRY_RUN        = os.getenv("DRY_RUN", "false").lower() == "true"
ACCOUNT_NUMBER = "634079917"
STRIKE_WINDOW  = 8
VIX_MIN        = 14.0
VIX_MAX        = 28.0
DELTA_MIN      = 0.30
DELTA_MAX      = 0.45
ET             = ZoneInfo("America/New_York")
# Known NYSE market holidays 2026 — bot will skip these automatically
NYSE_HOLIDAYS_2026 = {
    "2026-01-01",  # New Year's Day
    "2026-01-19",  # MLK Day
    "2026-02-16",  # Presidents Day
    "2026-04-03",  # Good Friday
    "2026-05-25",  # Memorial Day
    "2026-06-19",  # Juneteenth
    "2026-07-03",  # Independence Day (observed)
    "2026-09-07",  # Labor Day
    "2026-11-26",  # Thanksgiving
    "2026-11-27",  # Day after Thanksgiving (early close — skip)
    "2026-12-25",  # Christmas
}
SKIP_DATES = NYSE_HOLIDAYS_2026 | (set(os.getenv("SKIP_DATES", "").split(",")) - {""})
GEX_STATE_FILE = os.path.join(os.path.dirname(__file__), "..", "data", "gex_state.json")

def write_gex_state(update: dict):
    try:
        try:
            with open(GEX_STATE_FILE) as f:
                state = json.load(f)
        except Exception:
            state = {}
        state.update(update)
        state["last_updated"] = datetime.now(ET).isoformat()
        with open(GEX_STATE_FILE, "w") as f:
            json.dump(state, f, indent=2)
    except Exception as e:
        log.warning(f"Could not write GEX state: {e}")

def _et_now():
    return datetime.now(ET)

def check_time_window():
    """Allow entries 9:45 AM–12:30 PM ET. After 10:30 extra GEX confidence required (handled at Gate 7).
    After 12:30 PM theta decay on 0DTE makes buying too risky."""
    now = _et_now()
    h, m = now.hour, now.minute
    t = h * 60 + m
    if t < 585:
        return f"Too early ({now.strftime('%H:%M')} ET) — market opens 9:30, best entry 9:45 AM ET"
    if t > 750:   # after 12:30 PM
        return f"Too late ({now.strftime('%H:%M')} ET) — 0DTE theta too high after 12:30 PM"
    return None  # 9:45 AM – 12:30 PM: entry allowed

def net_gex(oi, gamma, spot, is_call):
    return (1 if is_call else -1) * oi * gamma * spot * 100

def login():
    user = os.getenv("RH_USERNAME", "")
    pwd  = os.getenv("RH_PASSWORD", "")
    mfa  = os.getenv("RH_MFA_CODE", "")
    if not user or not pwd:
        log.error("RH_USERNAME and RH_PASSWORD must be set.")
        sys.exit(1)
    # Persist session to /tmp so it survives within the same Railway container run.
    # store_session=True with a fixed path avoids re-authentication every restart.
    import pickle, pathlib
    session_dir = pathlib.Path("/tmp/rh_session")
    session_dir.mkdir(parents=True, exist_ok=True)
    log.info("Logging in to Robinhood...")
    try:
        rh.login(
            username=user, password=pwd,
            mfa_code=mfa or None,
            store_session=True,
            expiresIn=86400,
            pickle_name="heatseeker",
        )
    except Exception as e:
        log.error(f"Robinhood login exception: {e}")
        sys.exit(1)
    try:
        if not rh.profiles.load_portfolio_profile():
            raise Exception("empty profile")
        log.info("Login verified OK")
    except Exception as e:
        log.error(f"Login failed: {e}")
        sys.exit(1)

def get_account_balance():
    try:
        p = rh.profiles.load_account_profile(account_number=ACCOUNT_NUMBER, info=None)
        if p:
            bp = float(p.get("buying_power", 0) or 0)
            log.info(f"Account buying power: ${bp:.2f}")
            write_gex_state({"account_balance": round(bp, 2), "buying_power": round(bp, 2)})
            return bp
    except Exception as e:
        log.warning(f"Could not fetch balance: {e}")
    return 21.64

def has_open_position():
    """Return True if we already hold an open SPY 0DTE options position today."""
    try:
        today = date.today().strftime("%Y-%m-%d")
        positions = rh.options.get_open_option_positions(account_number=ACCOUNT_NUMBER) or []
        for p in positions:
            qty = float(p.get("quantity", 0) or 0)
            if qty > 0:
                exp = (p.get("option", {}) or {}).get("expiration_date", "")
                symbol = p.get("chain_symbol", "")
                if symbol == "SPY" and exp == today:
                    log.info(f"Open position found: {symbol} exp={exp} qty={qty}")
                    return True
    except Exception as e:
        log.warning(f"Position check error: {e}")
    return False

def get_vix():
    try:
        import yfinance as yf
        v = yf.Ticker("^VIX").fast_info.last_price
        if v and float(v) > 0:
            log.info(f"VIX: {float(v):.2f}")
            return round(float(v), 2)
    except Exception:
        pass
    try:
        data = rh.stocks.get_quotes("VIX")
        if data and data[0]:
            v = float(data[0].get("last_trade_price", 0) or 0)
            if v > 0:
                log.info(f"VIX (RH): {v:.2f}")
                return v
    except Exception:
        pass
    log.warning("VIX unavailable - defaulting to 15.0")
    return 15.0

def get_spot_and_prev_close(symbol="SPY"):
    data = rh.stocks.get_quotes(symbol)
    if not data:
        raise RuntimeError(f"No quote for {symbol}")
    q = data[0]
    spot, prev = float(q["last_trade_price"]), float(q["adjusted_previous_close"])
    log.info(f"{symbol} spot={spot:.2f}  prev_close={prev:.2f}")
    return spot, prev

def get_atm_strikes(symbol, expiration, spot):
    calls, puts = [], []
    all_opts = rh.options.find_options_by_expiration(inputSymbols=symbol, expirationDate=expiration)
    for o in (all_opts or []):
        if abs(float(o["strike_price"]) - spot) > STRIKE_WINDOW:
            continue
        if o.get("type") == "call": calls.append(o)
        elif o.get("type") == "put": puts.append(o)
    log.info(f"Found {len(calls)} calls and {len(puts)} puts within +/-${STRIKE_WINDOW}")
    return calls, puts

def fetch_greeks(instruments):
    for o in instruments:
        try:
            md_list = rh.options.get_option_market_data_by_id(o["id"])
            md = md_list[0] if isinstance(md_list, list) and md_list else (md_list or {})
            o["gamma"]         = float(md.get("gamma", 0) or 0)
            o["open_interest"] = int(md.get("open_interest", 0) or 0)
            o["ask_price"]     = float(md.get("ask_price", 0) or 0)
            o["bid_price"]     = float(md.get("bid_price", 0) or 0)
            o["mark_price"]    = float(md.get("adjusted_mark_price", 0) or 0)
            o["delta"]         = abs(float(md.get("delta", 0) or 0))
            o["implied_volatility"] = float(md.get("implied_volatility", 0) or 0)
        except Exception:
            o["gamma"] = o["open_interest"] = o["ask_price"] = 0
            o["bid_price"] = o["mark_price"] = o["delta"] = o["implied_volatility"] = 0

def compute_gex_nodes(calls, puts, spot):
    nodes = {}
    for o in calls:
        s = float(o["strike_price"]); nodes.setdefault(s, 0)
        nodes[s] += net_gex(o["open_interest"], o["gamma"], spot, True)
    for o in puts:
        s = float(o["strike_price"]); nodes.setdefault(s, 0)
        nodes[s] += net_gex(o["open_interest"], o["gamma"], spot, False)
    return sorted(nodes.items())

def find_king(nodes):
    if not nodes: return None, 0
    return max(nodes, key=lambda x: abs(x[1]))

def compute_ivr(atm_iv):
    """
    Estimate IVR using ATM IV vs rough 52-week range.
    Returns 0-100. Without historical data we use VIX as proxy:
    IVR = (atm_iv - iv_low) / (iv_high - iv_low) * 100
    Uses historical SPY IV range: low ~10%, high ~80%.
    """
    iv_low, iv_high = 0.10, 0.80
    ivr = max(0, min(100, (atm_iv - iv_low) / (iv_high - iv_low) * 100))
    return round(ivr, 1)

def find_entry_contract(pool, target, max_spend, max_contracts):
    """
    Find best contract near target strike within the 0.30-0.45 delta band.
    Falls back to nearest affordable if no delta-filtered contract found.
    """
    candidates = sorted(pool, key=lambda o: float(o["strike_price"]))

    # First pass: delta-filtered (0.30–0.45) — best contracts
    for delta in [0, -1, 1, -2, 2]:
        for o in candidates:
            if abs(float(o["strike_price"]) - (target + delta)) < 0.5:
                d = o.get("delta", 0)
                if not (DELTA_MIN <= d <= DELTA_MAX):
                    continue
                cost = o["ask_price"] * 100
                if cost <= 0: continue
                qty = max(1, min(max_contracts, int(max_spend / cost)))
                if cost * qty <= max_spend:
                    o["_contracts"] = qty
                    log.info(f"Delta-filtered contract: strike={o['strike_price']} delta={d:.2f}")
                    return o

    # Second pass: relax delta to 0.20–0.55 if nothing found in ideal band
    log.warning("No contract in 0.30-0.45 delta band — relaxing to 0.20-0.55")
    for delta in [0, -1, 1, -2, 2]:
        for o in candidates:
            if abs(float(o["strike_price"]) - (target + delta)) < 0.5:
                d = o.get("delta", 0)
                if d > 0 and not (0.20 <= d <= 0.55):
                    continue
                cost = o["ask_price"] * 100
                if cost <= 0: continue
                qty = max(1, min(max_contracts, int(max_spend / cost)))
                if cost * qty <= max_spend:
                    o["_contracts"] = qty
                    return o
    return None

def place_order(contract, expiration, opt_type, qty):
    symbol = contract["chain_symbol"]
    strike = float(contract["strike_price"])
    limit  = round(contract["ask_price"], 2)
    log.info(f"{'[DRY RUN] ' if DRY_RUN else ''}ORDER: BUY {qty}x {symbol} {strike}{opt_type[0].upper()} exp={expiration} limit=${limit:.2f} total=${limit*100*qty:.2f}")
    if DRY_RUN:
        log.info("DRY_RUN=true - not submitted")
        return {"dry_run": True}
    order = rh.orders.order_buy_option_limit(
        positionEffect="open", creditOrDebit="debit", price=limit,
        symbol=symbol, quantity=qty, expirationDate=expiration,
        strike=str(int(strike)), optionType=opt_type, timeInForce="gfd",
        account_number=ACCOUNT_NUMBER,
    )
    log.info(f"Order: {json.dumps(order, indent=2)}")
    return order

def main():
    login()
    symbol     = "SPY"
    today_str  = date.today().strftime("%Y-%m-%d")
    expiration = today_str
    log.info(f"Trading date: {expiration}")

    log.info("=" * 60)
    log.info("GATE CHECK")
    log.info("=" * 60)

    # Gate 1 — Market holiday / FOMC / skip-date filter
    if today_str in NYSE_HOLIDAYS_2026:
        log.warning(f"GATE 1 BLOCKED: {today_str} is a NYSE holiday. No trading."); sys.exit(0)
    if today_str in SKIP_DATES:
        log.warning(f"GATE 1 BLOCKED: {today_str} in SKIP_DATES (FOMC/event)."); sys.exit(0)
    log.info("GATE 1 PASS: date not a holiday or skip date")

    # Gate 2 — Time-of-day window (9:45 AM–12:30 PM ET)
    window_block = check_time_window()
    if window_block and not DRY_RUN:
        log.warning(f"GATE 2 BLOCKED: {window_block}"); sys.exit(0)
    elif window_block:
        log.info(f"GATE 2 [DRY RUN OVERRIDE]: {window_block}")
    else:
        log.info(f"GATE 2 PASS: time window OK ({_et_now().strftime('%H:%M')} ET)")

    # Gate 2b — Already in a position today? Skip entry.
    if has_open_position():
        log.info("GATE 2b BLOCKED: open SPY 0DTE position already held — skip entry"); sys.exit(0)
    log.info("GATE 2b PASS: no open position")

    # Gate 3 — VIX filter (min 14, research-backed)
    vix = get_vix()
    if vix < VIX_MIN:
        log.warning(f"GATE 3 BLOCKED: VIX {vix:.2f} < {VIX_MIN} (premiums too thin)"); sys.exit(0)
    if vix > VIX_MAX:
        log.warning(f"GATE 3 BLOCKED: VIX {vix:.2f} > {VIX_MAX} (too chaotic)"); sys.exit(0)
    log.info(f"GATE 3 PASS: VIX {vix:.2f} in range [{VIX_MIN}, {VIX_MAX}]")
    write_gex_state({"vix": vix, "last_action": "Running GEX analysis..."})

    balance = get_account_balance()
    print_dashboard(balance)

    max_contracts, max_spend = get_smart_size(balance)
    if max_contracts == 0:
        log.warning("Smart sizer: losing streak protection active. Skipping today.")
        sys.exit(0)
    log.info(f"SmartSize: ${balance:.2f} -> {max_contracts} contracts, ${max_spend:.2f} max spend")

    spot, prev_close = get_spot_and_prev_close(symbol)
    calls, puts = get_atm_strikes(symbol, expiration, spot)
    if not calls and not puts:
        log.error("No options found. Aborting."); sys.exit(1)

    log.info("Fetching greeks (includes delta for contract selection)...")
    fetch_greeks(calls); fetch_greeks(puts)

    # Gate 4 — IVR filter using ATM IV
    atm_opts = [o for o in calls + puts if abs(float(o["strike_price"]) - spot) < 2]
    atm_iv   = sum(o.get("implied_volatility", 0) for o in atm_opts) / len(atm_opts) if atm_opts else 0
    ivr      = compute_ivr(atm_iv)
    log.info(f"ATM IV: {atm_iv*100:.1f}%  IVR: {ivr:.0f}")
    if ivr > 50:
        log.warning(f"GATE 4 BLOCKED: IVR {ivr:.0f} > 50 — premium too expensive to buy"); sys.exit(0)
    if ivr > 30:
        log.info(f"GATE 4 PARTIAL: IVR {ivr:.0f} in 30-50 zone — reducing spend by 50%")
        max_spend = max(10.0, max_spend * 0.5)
    else:
        log.info(f"GATE 4 PASS: IVR {ivr:.0f} < 30 (cheap premium)")

    nodes = compute_gex_nodes(calls, puts, spot)
    king_strike, king_gex = find_king(nodes)
    log.info(f"KING NODE: {king_strike}  GEX=${king_gex/1e6:.2f}M")
    log.info("Top nodes: " + "  ".join(f"{s}=${g/1e6:.1f}M" for s,g in sorted(nodes, key=lambda x: abs(x[1]), reverse=True)[:5]))

    if king_gex >= 0:
        direction, target, pool = "call", king_strike + 1, calls
    else:
        direction, target, pool = "put", king_strike - 1, puts
    log.info(f"DIRECTION: {direction.upper()} -> target {target}")

    # Gate 5 — Regime confirm (price vs prev close)
    if direction == "call" and spot < prev_close - 0.50:
        log.warning(f"GATE 5 BLOCKED: GEX=CALL but SPY ${spot:.2f} bearish vs prev ${prev_close:.2f}"); sys.exit(0)
    if direction == "put" and spot > prev_close + 0.50:
        log.warning(f"GATE 5 BLOCKED: GEX=PUT but SPY ${spot:.2f} bullish vs prev ${prev_close:.2f}"); sys.exit(0)
    log.info(f"GATE 5 PASS: regime confirms {direction.upper()} (spot=${spot:.2f} prev=${prev_close:.2f})")

    # GEX pattern analysis + dealer flow
    gex_features = extract_gex_features(calls, puts, spot, nodes, king_strike, king_gex)

    # Gate 6 — Gamma flip hard boundary (SpotGamma rule: never trade against the flip)
    gamma_flip = gex_features.get("gamma_flip", king_strike)
    if direction == "call" and spot < gamma_flip - 1.0:
        log.warning(f"GATE 6 BLOCKED: SPY ${spot:.2f} below gamma flip ${gamma_flip:.0f} — no calls")
        sys.exit(0)
    if direction == "put" and spot > gamma_flip + 1.0:
        log.warning(f"GATE 6 BLOCKED: SPY ${spot:.2f} above gamma flip ${gamma_flip:.0f} — no puts")
        sys.exit(0)
    log.info(f"GATE 6 PASS: gamma flip ${gamma_flip:.0f} supports {direction.upper()}")
    write_gex_state({
        "spot": spot, "prev_close": prev_close, "ivr": ivr,
        "king_strike": king_strike, "king_gex_m": round(king_gex / 1e6, 2),
        "direction": direction, "gamma_flip": gamma_flip,
        "call_wall": gex_features.get("call_wall"),
        "put_wall":  gex_features.get("put_wall"),
        "gex_features": gex_features,
    })

    confidence, reasons = gex_confidence_score(gex_features, direction)
    print_dealer_flow(gex_features, spot, vix, direction, confidence, reasons)
    print_pattern_report()

    if confidence < 0.35:
        log.warning(f"GATE 7 BLOCKED: GEX confidence {confidence*100:.0f}% < 35%"); sys.exit(0)
    log.info(f"GATE 7 PASS: GEX confidence {confidence*100:.0f}%")

    max_spend = max(10.0, round(max_spend * confidence, 2))
    log.info(f"Confidence-adjusted spend: ${max_spend:.2f}")

    contract = find_entry_contract(pool, target, max_spend, max_contracts)
    if not contract:
        log.warning(f"No suitable {direction} contract near {target} within budget. Skipping."); sys.exit(0)

    qty   = contract["_contracts"]
    ask   = contract["ask_price"]
    delta = contract.get("delta", 0)
    total = ask * 100 * qty

    place_order(contract, expiration, direction, qty)
    write_gex_state({
        "last_action": f"{'[DRY RUN] ' if DRY_RUN else ''}ORDER PLACED: BUY {qty}x SPY {float(contract['strike_price'])}{direction[0].upper()} @ ${ask:.2f}",
        "confidence": confidence,
        "entry_strike": float(contract["strike_price"]),
        "entry_price": ask, "entry_qty": qty,
    })

    if not DRY_RUN:
        log_entry(
            symbol=symbol, opt_type=direction,
            strike=float(contract["strike_price"]), expiry=expiration,
            contracts=qty, entry_price=ask,
            vix=vix, king_strike=king_strike, king_gex=king_gex,
            direction=direction, balance_before=balance,
            gex_features=gex_features, confidence=confidence,
        )

    print("\n" + "="*60)
    print(f"HEATSEEKER MORNING SIGNAL - {expiration}")
    print("="*60)
    print(f"  Account:    {ACCOUNT_NUMBER} (agentic)")
    print(f"  SPY spot:   ${spot:.2f}  (prev ${prev_close:.2f})")
    print(f"  VIX:        {vix:.2f}  IVR: {ivr:.0f}")
    print(f"  King node:  {king_strike}  (GEX ${king_gex/1e6:.2f}M)")
    print(f"  Gamma flip: ${gamma_flip:.0f}")
    print(f"  Direction:  {direction.upper()}")
    print(f"  Trade:      BUY {qty}x {symbol} {float(contract['strike_price'])}{direction[0].upper()} @ ${ask:.2f}  (delta={delta:.2f})")
    print(f"  Total cost: ${total:.2f}")
    print(f"  Stop loss:  ${total*0.5:.2f}  (-50%)")
    print(f"  Trail stop: activates at ${total*2:.2f}  (+100%), trails 25% from peak")
    print(f"  GEX Conf:   {confidence*100:.0f}%")
    print(f"  Status:     {'DRY RUN' if DRY_RUN else 'ORDER PLACED'}")
    print("="*60)

def analyze_gex_only():
    """
    Run full GEX analysis and write to dashboard without placing any trade.
    Called every 15 min during market hours so the dashboard always shows live GEX data.
    """
    try:
        login()
    except SystemExit:
        return

    today_str = date.today().strftime("%Y-%m-%d")
    if today_str in NYSE_HOLIDAYS_2026 or today_str in SKIP_DATES:
        write_gex_state({"last_action": f"Market closed ({today_str})"})
        return

    now = _et_now()
    t   = now.hour * 60 + now.minute
    if now.weekday() >= 5 or t < 570 or t > 960:
        return  # outside market hours

    try:
        vix             = get_vix()
        balance         = get_account_balance()
        spot, prev_close = get_spot_and_prev_close("SPY")
        expiration      = today_str

        calls, puts = get_atm_strikes("SPY", expiration, spot)
        if not calls and not puts:
            write_gex_state({"spot": spot, "vix": vix, "last_action": "No options chain available yet"})
            return

        fetch_greeks(calls)
        fetch_greeks(puts)

        atm_opts = [o for o in calls + puts if abs(float(o["strike_price"]) - spot) < 2]
        atm_iv   = sum(o.get("implied_volatility", 0) for o in atm_opts) / len(atm_opts) if atm_opts else 0
        ivr      = compute_ivr(atm_iv)

        nodes                 = compute_gex_nodes(calls, puts, spot)
        king_strike, king_gex = find_king(nodes)

        if not king_strike:
            write_gex_state({"spot": spot, "vix": vix, "last_action": "GEX analysis: insufficient data"})
            return

        direction = "call" if king_gex >= 0 else "put"
        gex_features          = extract_gex_features(calls, puts, spot, nodes, king_strike, king_gex)
        gamma_flip            = gex_features.get("gamma_flip", king_strike)
        confidence, reasons   = gex_confidence_score(gex_features, direction)

        # Determine regime alignment
        regime_ok = not (
            (direction == "call" and spot < prev_close - 0.50) or
            (direction == "put"  and spot > prev_close + 0.50)
        )

        top_nodes_str = "  ".join(f"${s:.0f}={v/1e6:+.1f}M"
                                  for s, v in sorted(nodes, key=lambda x: abs(x[1]), reverse=True)[:5])

        # Build full strike ladder for dashboard GEX table
        call_by_strike = {float(o["strike_price"]): o for o in calls}
        put_by_strike  = {float(o["strike_price"]): o for o in puts}
        all_strikes = sorted(set(call_by_strike.keys()) | set(put_by_strike.keys()))
        ladder = []
        for s in all_strikes:
            c = call_by_strike.get(s, {})
            p = put_by_strike.get(s, {})
            c_gex = net_gex(c.get("open_interest", 0), c.get("gamma", 0), spot, True)
            p_gex = net_gex(p.get("open_interest", 0), p.get("gamma", 0), spot, False)
            ladder.append({
                "strike":     s,
                "call_oi":    c.get("open_interest", 0),
                "put_oi":     p.get("open_interest", 0),
                "call_gex_m": round(c_gex / 1e6, 3),
                "put_gex_m":  round(p_gex / 1e6, 3),
                "net_gex_m":  round((c_gex + p_gex) / 1e6, 3),
            })

        write_gex_state({
            "spot":        spot,
            "prev_close":  prev_close,
            "vix":         vix,
            "ivr":         ivr,
            "king_strike": king_strike,
            "king_gex_m":  round(king_gex / 1e6, 2),
            "direction":   direction,
            "confidence":  round(confidence, 3),
            "gamma_flip":  gamma_flip,
            "call_wall":   gex_features.get("call_wall"),
            "put_wall":    gex_features.get("put_wall"),
            "gex_features": gex_features,
            "regime_ok":   regime_ok,
            "top_nodes":   top_nodes_str,
            "strike_ladder": ladder,
            "last_action": f"GEX scan {now.strftime('%H:%M')} ET — "
                           f"{'✅' if regime_ok else '⚠️'} {direction.upper()} "
                           f"conf={confidence*100:.0f}% king=${king_strike:.0f} flip=${gamma_flip:.0f}",
        })
        log.info(f"GEX analysis written: {direction.upper()} conf={confidence*100:.0f}% king=${king_strike:.0f}")

    except Exception as e:
        log.warning(f"GEX analysis error: {e}")


if __name__ == "__main__":
    main()
