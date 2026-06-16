#!/usr/bin/env python3
import os, sys, json, logging
from datetime import date
import robin_stocks.robinhood as rh
from scripts.journal import get_smart_size, log_entry, print_dashboard

logging.basicConfig(level=logging.INFO, format="%(asctime)s  %(levelname)s  %(message)s", handlers=[logging.StreamHandler(sys.stdout)])
log = logging.getLogger("heatseeker")

DRY_RUN        = os.getenv("DRY_RUN", "false").lower() == "true"
ACCOUNT_NUMBER = "634079917"
STRIKE_WINDOW  = 15
VIX_MIN, VIX_MAX = 12.0, 28.0

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
        except Exception:
            o["gamma"] = o["open_interest"] = o["ask_price"] = o["bid_price"] = o["mark_price"] = 0

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

def find_entry_contract(pool, target, max_spend, max_contracts):
    candidates = sorted(pool, key=lambda o: float(o["strike_price"]))
    for delta in [0, -1, 1, -2, 2]:
        for o in candidates:
            if abs(float(o["strike_price"]) - (target + delta)) < 0.5:
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
    expiration = date.today().strftime("%Y-%m-%d")
    log.info(f"Trading date: {expiration}")

    vix = get_vix()
    if vix < VIX_MIN: log.warning(f"VIX {vix:.2f} too low. Skipping."); sys.exit(0)
    if vix > VIX_MAX: log.warning(f"VIX {vix:.2f} too high. Skipping."); sys.exit(0)

    balance = get_account_balance()
    print_dashboard(balance)

    max_contracts, max_spend = get_smart_size(balance)
    if max_contracts == 0:
        log.warning("Smart sizer returned 0 contracts (losing streak protection). Skipping today.")
        sys.exit(0)
    log.info(f"SmartSize: ${balance:.2f} -> {max_contracts} contracts, ${max_spend:.2f} max spend")

    spot, prev_close = get_spot_and_prev_close(symbol)
    calls, puts = get_atm_strikes(symbol, expiration, spot)
    if not calls and not puts:
        log.error("No options found. Aborting."); sys.exit(1)

    log.info("Fetching greeks...")
    fetch_greeks(calls); fetch_greeks(puts)

    nodes = compute_gex_nodes(calls, puts, spot)
    king_strike, king_gex = find_king(nodes)
    log.info(f"KING NODE: {king_strike}  GEX=${king_gex/1e6:.2f}M")
    log.info("Top nodes: " + "  ".join(f"{s}=${g/1e6:.1f}M" for s,g in sorted(nodes, key=lambda x: abs(x[1]), reverse=True)[:5]))

    if king_gex >= 0:
        direction, target, pool = "call", king_strike + 1, calls
    else:
        direction, target, pool = "put", king_strike - 1, puts
    log.info(f"DIRECTION: {direction.upper()} -> target {target}")

    if direction == "call" and spot < prev_close - 0.50:
        log.warning("GEX=CALL but bearish. Skipping."); sys.exit(0)
    if direction == "put" and spot > prev_close + 0.50:
        log.warning("GEX=PUT but bullish. Skipping."); sys.exit(0)

    contract = find_entry_contract(pool, target, max_spend, max_contracts)
    if not contract:
        log.warning(f"No affordable {direction} near {target}. Skipping."); sys.exit(0)

    qty   = contract["_contracts"]
    ask   = contract["ask_price"]
    total = ask * 100 * qty

    place_order(contract, expiration, direction, qty)

    # Log entry to journal
    if not DRY_RUN:
        log_entry(
            symbol=symbol, opt_type=direction,
            strike=float(contract["strike_price"]), expiry=expiration,
            contracts=qty, entry_price=ask,
            vix=vix, king_strike=king_strike, king_gex=king_gex,
            direction=direction, balance_before=balance,
        )

    print("\n" + "="*60)
    print(f"HEATSEEKER MORNING SIGNAL - {expiration}")
    print("="*60)
    print(f"  Account:    {ACCOUNT_NUMBER} (agentic)")
    print(f"  SPY spot:   ${spot:.2f}  (prev ${prev_close:.2f})")
    print(f"  VIX:        {vix:.2f}")
    print(f"  King node:  {king_strike}  (GEX ${king_gex/1e6:.2f}M)")
    print(f"  Direction:  {direction.upper()}")
    print(f"  Trade:      BUY {qty}x {symbol} {float(contract['strike_price'])}{direction[0].upper()} @ ${ask:.2f}")
    print(f"  Total cost: ${total:.2f}")
    print(f"  Stop loss:  ${total*0.5:.2f}  (-50%)")
    print(f"  Trail stop: activates at ${total*2:.2f}  (+100%), trails 25% from peak")
    print(f"  Status:     {'DRY RUN' if DRY_RUN else 'ORDER PLACED'}")
    print("="*60)

if __name__ == "__main__":
    main()
