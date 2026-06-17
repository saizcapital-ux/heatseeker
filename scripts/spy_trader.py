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
STRIKE_WINDOW  = 15
VIX_MIN        = 14.0    # below 14 premiums too thin (raised from 12, per research)
VIX_MAX        = 28.0
DELTA_MIN      = 0.30    # near-ATM only — 0.30-0.45 delta band
DELTA_MAX      = 0.45
ET             = ZoneInfo("America/New_York")

# Economic/FOMC dates to skip — update monthly
SKIP_DATES = set(os.getenv("SKIP_DATES", "").split(",")) - {""}

def _et_now():
    return datetime.now(ET)

def check_time_window():
    """Only enter during the proven high-win windows. Return reason if blocked."""
    now = _et_now()
    h, m = now.hour, now.minute
    t = h * 60 + m
    # Valid windows: 9:45–10:15 AM ET
    if 585 <= t <= 615:   # 9:45–10:15
        return None
    # Outside valid window
    if t < 585:
        return f"Too early ({now.strftime('%H:%M')} ET) — wait for 9:45 AM"
    return f"Outside entry window ({now.strftime('%H:%M')} ET) — valid: 9:45–10:15 AM"

def net_gex(oi, gamma, spot, is_call):
    return (1 if is_call else -1) * oi * gamma * spot * 100

def login():
    user = os.getenv("RH_USERNAME", "")
    pwd  = os.getenv("RH_PASSWORD", "")
    mfa  = os.getenv("RH_MFA_CODE", "")
    if not user or not pwd:
        log.error("RH_USERNAME and RH_PASSWORD must be set.")
        sys.exit(1)
    log.info("Logging in to Robinhood...")
    rh.login(username=user, password=pwd, mfa_code=mfa or None, store_session=False)
    try:
        if not rh.profiles.load_portfolio_profile():
            raise Exception("empty")
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
            return bp
    except Exception as e:
        log.warning(f"Could not fetch balance: {e}")
    return 50.0

def get_vix():
    try:
        data = rh.stocks.get_quotes("VIX")
        if data and data[0]:
            v = float(data[0].get("last_trade_price", 0) or 0)
            if v > 0:
                log.info(f"VIX: {v:.2f}")
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

    # Gate 1 — FOMC / skip-date filter
    if today_str in SKIP_DATES:
        log.warning(f"Date {today_str} in SKIP_DATES (FOMC/event). Skipping."); sys.exit(0)

    # Gate 2 — Time-of-day window (9:45–10:15 AM ET only)
    window_block = check_time_window()
    if window_block and not DRY_RUN:
        log.warning(f"TIME GATE: {window_block}. Skipping."); sys.exit(0)
    elif window_block:
        log.info(f"[DRY RUN] Time gate would block: {window_block} — proceeding anyway")

    # Gate 3 — VIX filter (raised to 14, research-backed)
    vix = get_vix()
    if vix < VIX_MIN:
        log.warning(f"VIX {vix:.2f} < {VIX_MIN} (premiums too thin). Skipping."); sys.exit(0)
    if vix > VIX_MAX:
        log.warning(f"VIX {vix:.2f} > {VIX_MAX} (too chaotic). Skipping."); sys.exit(0)

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
        log.warning(f"IVR {ivr:.0f} > 50 — premium too expensive to buy. Skipping."); sys.exit(0)
    if ivr > 30:
        log.info(f"IVR {ivr:.0f} in 30-50 zone — reducing spend by 50%")
        max_spend = max(10.0, max_spend * 0.5)

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
        log.warning("GEX=CALL but price bearish vs prev close. Skipping."); sys.exit(0)
    if direction == "put" and spot > prev_close + 0.50:
        log.warning("GEX=PUT but price bullish vs prev close. Skipping."); sys.exit(0)

    # GEX pattern analysis + dealer flow
    gex_features = extract_gex_features(calls, puts, spot, nodes, king_strike, king_gex)

    # Gate 6 — Gamma flip hard boundary (SpotGamma rule: never trade against the flip)
    gamma_flip = gex_features.get("gamma_flip", king_strike)
    if direction == "call" and spot < gamma_flip - 1.0:
        log.warning(f"GAMMA FLIP GATE: SPY ${spot:.2f} below flip ${gamma_flip:.0f} — no calls. Skipping.")
        sys.exit(0)
    if direction == "put" and spot > gamma_flip + 1.0:
        log.warning(f"GAMMA FLIP GATE: SPY ${spot:.2f} above flip ${gamma_flip:.0f} — no puts. Skipping.")
        sys.exit(0)

    confidence, reasons = gex_confidence_score(gex_features, direction)
    print_dealer_flow(gex_features, spot, vix, direction, confidence, reasons)
    print_pattern_report()

    if confidence < 0.35:
        log.warning(f"GEX confidence {confidence*100:.0f}% too low. Skipping."); sys.exit(0)

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

if __name__ == "__main__":
    main()
