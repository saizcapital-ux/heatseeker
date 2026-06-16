#!/usr/bin/env python3
import os
import sys
import json
import logging
from datetime import date
import robin_stocks.robinhood as rh

logging.basicConfig(level=logging.INFO, format="%(asctime)s  %(levelname)s  %(message)s", handlers=[logging.StreamHandler(sys.stdout)])
log = logging.getLogger("heatseeker")

MAX_SPEND      = float(os.getenv("MAX_SPEND", "50"))
MAX_CONTRACTS  = 1
DRY_RUN        = os.getenv("DRY_RUN", "false").lower() == "true"
ACCOUNT_NUMBER = os.getenv("RH_ACCOUNT_NUMBER", "634079917")
STRIKE_WINDOW  = 15

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
    for opt_type, bucket in [("call", calls), ("put", puts)]:
        opts = rh.options.find_options_by_expiration_and_type(inputSymbols=symbol, expirationDate=expiration, optionType=opt_type)
        for o in opts:
            if abs(float(o["strike_price"]) - spot) <= STRIKE_WINDOW:
                bucket.append(o)
    log.info(f"Found {len(calls)} calls and {len(puts)} puts within +/-${STRIKE_WINDOW} of spot")
    return calls, puts

def fetch_greeks(instruments):
    ids = [o["id"] for o in instruments]
    if not ids:
        return
    market_data = rh.options.get_option_market_data_by_id(ids)
    data_by_id = {}
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

def find_entry_contract(calls, king_strike, max_spend=MAX_SPEND):
    target     = king_strike + 1
    candidates = sorted(calls, key=lambda o: float(o["strike_price"]))
    for delta in [0, -1, 1, -2, 2]:
        desired = target + delta
        for o in candidates:
            if abs(float(o["strike_price"]) - desired) < 0.5:
                cost = o["ask_price"] * 100
                if 0 < cost <= max_spend:
                    return o
    return None

def place_order(contract, expiration):
    symbol      = contract["chain_symbol"]
    strike      = float(contract["strike_price"])
    limit_price = round(contract["ask_price"], 2)
    log.info(f"{'[DRY RUN] ' if DRY_RUN else ''}ORDER: BUY 1x {symbol} {strike}C exp={expiration} limit=${limit_price:.2f}")
    if DRY_RUN:
        log.info("DRY_RUN=true - order not submitted")
        return {"dry_run": True}
    order = rh.orders.order_buy_option_limit(
        positionEffect="open", creditOrDebit="debit", price=limit_price,
        symbol=symbol, quantity=MAX_CONTRACTS, expirationDate=expiration,
        strike=str(int(strike)), optionType="call", timeInForce="gfd",
        account_number=ACCOUNT_NUMBER,
    )
    log.info(f"Order response: {json.dumps(order, indent=2)}")
    return order

def main():
    login()
    symbol     = "SPY"
    expiration = date.today().strftime("%Y-%m-%d")
    log.info(f"Trading date: {expiration}")
    spot, prev_close = get_spot_and_prev_close(symbol)
    if spot < prev_close - 0.50:
        log.warning(f"REGIME: BEARISH - SPY {spot:.2f} below prev close {prev_close:.2f}. No trade.")
        sys.exit(0)
    log.info(f"REGIME: BULL/NEUT - SPY {spot:.2f} >= prev close {prev_close:.2f}")
    calls, puts = get_atm_strikes(symbol, expiration, spot)
    if not calls:
        log.error("No call options found near ATM. Aborting.")
        sys.exit(1)
    fetch_greeks(calls)
    fetch_greeks(puts)
    nodes = compute_gex_nodes(calls, puts, spot)
    king_strike, king_gex = find_king(nodes)
    log.info(f"KING NODE: {king_strike}  GEX=${king_gex/1e6:.2f}M")
    contract = find_entry_contract(calls, king_strike)
    if not contract:
        log.warning(f"No affordable call near king {king_strike}. Skipping.")
        sys.exit(0)
    entry_strike = float(contract["strike_price"])
    entry_ask    = contract["ask_price"]
    log.info(f"ENTRY: {symbol} {entry_strike}C  ask=${entry_ask:.2f}  cost=${entry_ask*100:.2f}")
    place_order(contract, expiration)
    print("\n" + "="*60)
    print(f"HEATSEEKER MORNING SIGNAL - {expiration}")
    print("="*60)
    print(f"  SPY spot:  ${spot:.2f}  (prev close ${prev_close:.2f})")
    print(f"  King node: {king_strike}  (GEX ${king_gex/1e6:.2f}M)")
    print(f"  Trade:     BUY 1x {symbol} {entry_strike}C @ ${entry_ask:.2f}")
    print(f"  Max loss:  ${entry_ask*100:.2f}")
    print(f"  Status:    {'DRY RUN' if DRY_RUN else 'ORDER PLACED'}")
    print("="*60)

if __name__ == "__main__":
    main()
