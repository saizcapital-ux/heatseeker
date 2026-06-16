#!/usr/bin/env python3
"""
HEATSEEKER™ Morning SPY Regime Trader
Runs at market open, computes 0DTE GEX regime, places 1 high-probability call.

Regime rules:
  - Only trade if SPY >= prev_close (bull or neutral bias)
  - Compute king node = strike with highest net GEX near ATM
  - Entry = 1 strike above king (breakout play)
  - Limit price = ask price, max $0.50/contract ($50 max spend)
  - 1 contract only, 0DTE
  - Skip if no strike found within budget or regime is bearish
"""

import os
import sys
import json
import logging
from datetime import date

import robin_stocks.robinhood as rh

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)s  %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)],
)
log = logging.getLogger("heatseeker")

MAX_SPEND      = float(os.getenv("MAX_SPEND", "50"))
MAX_CONTRACTS  = 1
DRY_RUN        = os.getenv("DRY_RUN", "false").lower() == "true"
ACCOUNT_NUMBER = os.getenv("RH_ACCOUNT_NUMBER", "634079917")
STRIKE_WINDOW  = 15

def net_gex(oi, gamma, spot, is_call):
    sign = 1 if is_call else -1
    return sign * oi * gamma * spot * 100


def login():
    user = os.getenv("RH_USERNAME", "")
    pwd  = os.getenv("RH_PASSWORD", "")
    mfa  = os.getenv("RH_MFA_CODE", "")
    if not user or not pwd:
        log.error(
            "RH_USERNAME and RH_PASSWORD must be set as GitHub Actions secrets.\n"
            "Go to: github.com/saizcapital-ux/heatseeker/settings/secrets/actions"
        )
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


def get_atm_strikes(symbol, expiration, spot, window=STRIKE_WINDOW):
    calls, puts = [], []
    for opt_type, bucket in [("call", calls), ("put", puts)]:
        opts = rh.options.find_options_by_expiration_and_type(
            inputSymbols=symbol,
            expirationDate=expiration,
            optionType=opt_type,
        )
        for o in opts:
            strike = float(o["strike_price"])
            if abs(strike - spot) <= window:
                bucket.append(o)
    log.info(f"Found {len(calls)} calls and {len(puts)} puts within +/-${window} of spot")
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
        nodes[s] += net_gex(o["open_interest"], o["gamma"], spot, is_call=True)
    for o in puts:
        s = float(o["strike_price"])
        nodes.setdefault(s, 0)
        nodes[s] += net_gex(o["open_interest"], o["gamma"], spot, is_call=False)
    return sorted(nodes.items())


def find_king(nodes):
    if not nodes:
        return None, 0
    king_strike, king_gex = max(nodes, key=lambda x: abs(x[1]))
    return king_strike, king_gex


def find_entry_contract(calls, king_strike, spot, max_spend=MAX_SPEND):
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


def place_order(contract, expiration, dry_run=DRY_RUN):
    symbol      = contract["chain_symbol"]
    strike      = float(contract["strike_price"])
    ask         = contract["ask_price"]
    limit_price = round(ask, 2)

    log.info(
        f"{'[DRY RUN] ' if dry_run else ''}ORDER: BUY 1x {symbol} {strike}C "
        f"exp={expiration}  limit=${limit_price:.2f}  cost=${limit_price*100:.2f}"
    )

    if dry_run:
        log.info("DRY_RUN=true - order not submitted")
        return {"dry_run": True, "strike": strike, "limit": limit_price}

    order = rh.orders.order_buy_option_limit(
        positionEffect="open",
        creditOrDebit="debit",
        price=limit_price,
        symbol=symbol,
        quantity=MAX_CONTRACTS,
        expirationDate=expiration,
        strike=str(int(strike)),
        optionType="call",
        timeInForce="gfd",
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
        log.warning(
            f"REGIME: BEARISH - SPY {spot:.2f} is below prev close {prev_close:.2f}. No trade today."
        )
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

    top_nodes = sorted(nodes, key=lambda x: abs(x[1]), reverse=True)[:5]
    log.info("Top GEX nodes: " + "  ".join(f"{s}=${g/1e6:.1f}M" for s, g in top_nodes))

    contract = find_entry_contract(calls, king_strike, spot, MAX_SPEND)

    if not contract:
        log.warning(f"No affordable call found within ${MAX_SPEND} near king {king_strike}. Skipping.")
        sys.exit(0)

    entry_strike = float(contract["strike_price"])
    entry_ask    = contract["ask_price"]
    log.info(f"ENTRY: {symbol} {entry_strike}C  ask=${entry_ask:.2f}  cost=${entry_ask*100:.2f}")

    place_order(contract, expiration)

    print("\n" + "="*60)
    print(f"HEATSEEKER MORNING SIGNAL - {expiration}")
    print("="*60)
    print(f"  SPY spot:    ${spot:.2f}  (prev close ${prev_close:.2f})")
    print(f"  King node:   {king_strike}  (GEX ${king_gex/1e6:.2f}M)")
    print(f"  Trade:       BUY 1x {symbol} {entry_strike}C  @ ${entry_ask:.2f}")
    print(f"  Max loss:    ${entry_ask*100:.2f}")
    print(f"  Status:
