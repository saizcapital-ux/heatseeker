#!/usr/bin/env python3
import os
import sys
import json
import logging
from datetime import date
import robin_stocks.robinhood as rh

logging.basicConfig(level=logging.INFO, format="%(asctime)s  %(levelname)s  %(message)s", handlers=[logging.StreamHandler(sys.stdout)])
log = logging.getLogger("heatseeker")

DRY_RUN        = os.getenv("DRY_RUN", "false").lower() == "true"
ACCOUNT_NUMBER = "634079917"
STRIKE_WINDOW  = 15
VIX_MIN        = 12.0
VIX_MAX        = 28.0

def net_gex(oi, gamma, spot, is_call):
    return (1 if is_call else -1) * oi * gamma * spot * 100

def login():
    user = os.getenv("RH_USERNAME", "")
    pwd  = os.getenv("RH_PASSWORD", "")
    mfa  = os.getenv("RH_MFA_CODE", "")
    if not user or not pwd:
        log.error("RH_USERNAME and RH_PASSWORD must be set as GitHub Actions secrets.")
        sys.exit(1)
    log.info("Logging in to Robinhood...")
    rh.login(username=user, password=pwd, mfa_code=mfa or None, store_session=False)
    log.info("Login OK")

def get_account_balance():
    try:
        profiles = rh.profiles.load_account_profile(account_number=ACCOUNT_NUMBER, info=None)
        if profiles:
            buying_power = float(profiles.get("buying_power", 0) or 0)
            log.info(f"Account buying power: ${buying_power:.2f}")
            return buying_power
    except Exception as e:
        log.warning(f"Could not fetch account balance: {e}")
    return 50.0

def get_position_size(balance):
    if balance < 150:
        return 1, min(balance * 0.9, 50)
    elif balance < 500:
        return 3, min(balance * 0.25, 150)
    elif balance < 1500:
        return 10, min(balance * 0.25, 500)
    else:
        spend = balance * 0.15
        return max(1, int(spend / 50)), spend

def get_vix():
    try:
        data = rh.stocks.get_quotes("VIX")
        if data and data[0]:
            vix = float(data[0].get("last_trade_price", 0) or 0)
            if vix == 0:
                vix = float(data[0].get("last_extended_hours_trade_price", 0) or 0)
            if vix > 0:
                log.info(f"VIX: {vix:.2f}")
                return vix
    except Exception:
        pass
    log.warning("VIX unavailable - defaulting to 15.0")
    return 15.0

def get_spot_and_prev_close(symbol="SPY"):
    data = rh.stocks.get_quotes(symbol)
    if not data:
        raise RuntimeError(f"No quote returned for {symbol}")
    q = data[0]
    spot       = float(q["last_trade_price"])
    prev_close = float(q["adjusted_previous_close"])
    log.info(f"{symbol} spot={spot:.2f}  prev_close={prev_close:.2f}")
    return spot, prev_close

def get_atm_strikes(symbol, expiration, spot):
    calls, puts = [], []
    all_opts = rh.options.find_options_by_expiration(inputSymbols=symbol, expirationDate=expiration)
    if not all_opts:
        return calls, puts
    for o in all_opts:
        if abs(float(o["strike_price"]) - spot) > STRIKE_WINDOW:
            continue
        if o.get("type") == "call":
            calls.append(o)
        elif o.get("type") == "put":
            puts.append(o)
    log.info(f"Found {len(calls)} calls and {len(puts)} puts within +/-${STRIKE_WINDOW} of spot")
    return calls, puts

def fetch_greeks(instruments):
    ids = [o["id"] for o in instruments]
    if not ids:
        return
    data_by_id = {}
    for i in range(0, len(ids), 20):
        batch = ids[i:i+20]
        market_data = rh.options.get_option_market_data_by_id(batch)
        if isinstance(market_data, list):
            for item in market_data:
                if item:
                    data_by_id[item.get("instrument_id", "")] = item
    for o in instruments:
        md = data_by_id.get(o["id"], {})
        o["gamma"]         = float(md.get("gamma", 0) or 0)
        o["open_interest"] = int(md.get("open_interest", 0) or 0)
        o["ask_price"]     = float(md.get("ask_price", 0) or 0)
        o["bid_price"]     = float(md.get("bid_price", 0) or 0)
        o["mark_price"]    = float(md.get("adjusted_mark_price", 0) or 0)

def compute_gex_nodes(calls, puts, spot):
    nodes = {}
    for o in calls:
        s = float(o["strike_price"])
        nodes.setdefault(s, 0)
        nodes[s] += net_gex(o["open_interest"], o["gamma"], spot, True)
    for o in puts:
        s = float(o["strike_price"])
        nodes.setdefault(s, 0)
        nodes[s] += net_gex(o["open_interest"], o["gamma"], spot, False)
    return sorted(nodes.items())

def find_king(nodes):
    if not nodes:
        return None, 0
    return max(nodes, key=lambda x: abs(x[1]))

def find_entry_contract(option_pool, target_strike, max_spend, max_contracts):
    candidates = sorted(option_pool, key=lambda o: float(o["strike_price"]))
    for delta in [0, -1, 1, -2, 2]:
        desired = target_strike + delta
        for o in candidates:
            if abs(float(o["strike_price"]) - desired) < 0.5:
                cost_per = o["ask_price"] * 100
                if cost_per <= 0:
                    continue
                affordable = max(1, min(max_contracts, int(max_spend / cost_per)))
                if cost_per * affordable <= max_spend:
                    o["_contracts"] = affordable
                    return o
    return None

def place_order(contract, expiration, opt_type, qty):
    symbol      = contract["chain_symbol"]
    strike      = float(contract["strike_price"])
    limit_price = round(contract["ask_price"], 2)
    log.info(f"{'[DRY RUN] ' if DRY_RUN else ''}ORDER: BUY {qty}x {symbol} {strike}{opt_type[0].upper()} exp={expiration} limit=${limit_price:.2f} total=${limit_price*100*qty:.2f}")
    if DRY_RUN:
        log.info("DRY_RUN=true - order not submitted")
        return {"dry_run": True}
    order = rh.orders.order_buy_option_limit(
        positionEffect="open", creditOrDebit="debit", price=limit_price,
        symbol=symbol, quantity=qty, expirationDate=expiration,
        strike=str(int(strike)), optionType=opt_type, timeInForce="gfd",
        account_number=ACCOUNT_NUMBER,
    )
    log.info(f"Order response: {json.dumps(order, indent=2)}")
    return order

def main():
    login()
    symbol     = "SPY"
    expiration = date.today().strftime("%Y-%m-%d")
    log.info(f"Trading date: {expiration}")

    vix = get_vix()
    if vix < VIX_MIN:
        log.warning(f"VIX {vix:.2f} too low - no premium. Skipping.")
        sys.exit(0)
    if vix > VIX_MAX:
        log.warning(f"VIX {vix:.2f} too high - too chaotic. Skipping.")
        sys.exit(0)
    log.info(f"VIX {vix:.2f} is tradeable")

    balance = get_account_balance()
    max_contracts, max_spend = get_position_size(balance)
    log.info(f"Sizing: balance=${balance:.2f} -> {max_contracts} contracts, ${max_spend:.2f} max spend")

    spot, prev_close = get_spot_and_prev_close(symbol)
    calls, puts = get_atm_strikes(symbol, expiration, spot)
    if not calls and not puts:
        log.error("No options found near ATM. Aborting.")
        sys.exit(1)

    fetch_greeks(calls)
    fetch_greeks(puts)

    nodes = compute_gex_nodes(calls, puts, spot)
    king_strike, king_gex = find_king(nodes)
    log.info(f"KING NODE: {king_strike}  GEX=${king_gex/1e6:.2f}M")

    top_nodes = sorted(nodes, key=lambda x: abs(x[1]), reverse=True)[:5]
    log.info("Top GEX nodes: " + "  ".join(f"{s}=${g/1e6:.1f}M" for s, g in top_nodes))

    if king_gex >= 0:
        direction   = "call"
        target      = king_strike + 1
        option_pool = calls
        log.info(f"DIRECTION: CALL (GEX +${king_gex/1e6:.1f}M) -> target {target}")
    else:
        direction   = "put"
        target      = king_strike - 1
        option_pool = puts
        log.info(f"DIRECTION: PUT (GEX -${abs(king_gex)/1e6:.1f}M) -> target {target}")

    if direction == "call" and spot < prev_close - 0.50:
        log.warning("GEX=CALL but price is bearish. Signals conflict - skipping.")
        sys.exit(0)
    if direction == "put" and spot > prev_close + 0.50:
        log.warning("GEX=PUT but price is bullish. Signals conflict - skipping.")
        sys.exit(0)

    contract = find_entry_contract(option_pool, target, max_spend, max_contracts)
    if not contract:
        log.warning(f"No affordable {direction} near {target} within ${max_spend:.0f}. Skipping.")
        sys.exit(0)

    entry_strike = float(contract["strike_price"])
    entry_ask    = contract["ask_price"]
    qty          = contract["_contracts"]
    total_cost   = entry_ask * 100 * qty

    place_order(contract, expiration, direction, qty)

    print("\n" + "="*60)
    print(f"HEATSEEKER MORNING SIGNAL - {expiration}")
    print("="*60)
    print(f"  Account:     {ACCOUNT_NUMBER} (agentic)")
    print(f"  SPY spot:    ${spot:.2f}  (prev close ${prev_close:.2f})")
    print(f"  VIX:         {vix:.2f}")
    print(f"  King node:   {king_strike}  (GEX ${king_gex/1e6:.2f}M)")
    print(f"  Direction:   {direction.upper()}")
    print(f"  Trade:       BUY {qty}x {symbol} {entry_strike}{direction[0].upper()} @ ${entry_ask:.2f}")
    print(f"  Total cost:  ${total_cost:.2f}")
    print(f"  Stop loss:   ${total_cost*0.5:.2f}  (-50%)")
    print(f"  Target:      ${total_cost*2:.2f}  (+100%)")
    print(f"  Status:      {'DRY RUN' if DRY_RUN else 'ORDER PLACED'}")
    print("="*60)

if __name__ == "__main__":
    main()
