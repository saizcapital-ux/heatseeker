#!/usr/bin/env python3
import os
import sys
import json
import logging
from datetime import date
import robin_stocks.robinhood as rh

logging.basicConfig(level=logging.INFO, format="%(asctime)s  %(levelname)s  %(message)s", handlers=[logging.StreamHandler(sys.stdout)])
log = logging.getLogger("heatseeker.exit")

DRY_RUN        = os.getenv("DRY_RUN", "false").lower() == "true"
FORCE_CLOSE    = os.getenv("FORCE_CLOSE", "false").lower() == "true"
ACCOUNT_NUMBER = os.getenv("RH_ACCOUNT_NUMBER", "634079917")
PROFIT_TARGET  = 1.00
STOP_LOSS      = -0.50

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

def get_spy_0dte_positions():
    today = date.today().strftime("%Y-%m-%d")
    positions = rh.options.get_open_option_positions()
    if not positions:
        return []
    spy_0dte = []
    for p in positions:
        qty = float(p.get("quantity", 0))
        if qty <= 0:
            continue
        instrument_url = p.get("option")
        if not instrument_url:
            continue
        try:
            instrument = rh.options.get_option_instrument_data_by_url(instrument_url)
        except Exception:
            continue
        if not instrument:
            continue
        chain = instrument.get("chain_symbol", "")
        expiration = instrument.get("expiration_date", "")
        if chain == "SPY" and expiration == today:
            p["instrument"] = instrument
            p["expiration"] = expiration
            p["opt_type"]   = instrument.get("type", "call")
            p["strike"]     = float(instrument.get("strike_price", 0))
            p["qty"]        = int(qty)
            spy_0dte.append(p)
    return spy_0dte

def get_current_price(position):
    instrument_url = position.get("option")
    try:
        md = rh.options.get_option_market_data_by_url(instrument_url)
        if md:
            bid  = float(md.get("bid_price", 0) or 0)
            ask  = float(md.get("ask_price", 0) or 0)
            mark = float(md.get("adjusted_mark_price", 0) or 0)
            return bid, ask, mark
    except Exception:
        pass
    return 0.0, 0.0, 0.0

def close_position(position, limit_price):
    strike     = position["strike"]
    expiration = position["expiration"]
    opt_type   = position["opt_type"]
    qty        = position["qty"]
    symbol     = position["instrument"].get("chain_symbol", "SPY")
    log.info(f"{'[DRY RUN] ' if DRY_RUN else ''}CLOSE: SELL {qty}x {symbol} {strike}{opt_type[0].upper()} exp={expiration} limit=${limit_price:.2f}")
    if DRY_RUN:
        log.info("DRY_RUN=true - close order not submitted")
        return {"dry_run": True}
    order = rh.orders.order_sell_option_limit(
        positionEffect="close", creditOrDebit="credit", price=limit_price,
        symbol=symbol, quantity=qty, expirationDate=expiration,
        strike=str(int(strike)), optionType=opt_type, timeInForce="gfd",
        account_number=ACCOUNT_NUMBER,
    )
    log.info(f"Close order: {json.dumps(order, indent=2)}")
    return order

def main():
    login()
    today = date.today().strftime("%Y-%m-%d")
    log.info(f"Exit check: {today}  FORCE_CLOSE={FORCE_CLOSE}")
    positions = get_spy_0dte_positions()

    print("\n" + "="*60)
    print(f"HEATSEEKER EXIT CHECK - {today}")
    print("="*60)

    if not positions:
        log.info("No open SPY 0DTE positions. Nothing to do.")
        print("  No open SPY 0DTE positions.")
        print("="*60)
        return

    for p in positions:
        strike   = p["strike"]
        opt_type = p["opt_type"]
        qty      = p["qty"]
        avg_cost = float(p.get("average_price", 0) or 0)
        bid, ask, mark = get_current_price(p)

        pnl_pct     = (mark - avg_cost) / avg_cost if avg_cost > 0 else 0
        pnl_dollars = (mark - avg_cost) * qty * 100

        log.info(f"POSITION: SPY {strike}{opt_type[0].upper()} qty={qty} avg=${avg_cost:.2f} mark=${mark:.2f} P&L={pnl_pct*100:+.1f}% (${pnl_dollars:+.2f})")
        print(f"  SPY {strike}{opt_type[0].upper()}: avg=${avg_cost:.2f} mark=${mark:.2f} P&L={pnl_pct*100:+.1f}% (${pnl_dollars:+.2f})")

        should_close = False
        reason = ""
        if FORCE_CLOSE:
            should_close = True
            reason = "FORCE CLOSE (3:45 PM ET)"
        elif pnl_pct >= PROFIT_TARGET:
            should_close = True
            reason = f"PROFIT TARGET ({pnl_pct*100:.0f}% gain)"
        elif pnl_pct <= STOP_LOSS:
            should_close = True
            reason = f"STOP LOSS ({pnl_pct*100:.0f}% loss)"

        if should_close:
            log.info(f"ACTION: {reason} - CLOSING")
            print(f"  --> {reason} - CLOSING")
            close_position(p, max(round(bid, 2), 0.01))
        else:
            log.info("ACTION: HOLD")
            print(f"  --> HOLD (waiting for target)")

    print("="*60)

if __name__ == "__main__":
    main()
