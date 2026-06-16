#!/usr/bin/env python3
import os, sys, json, logging
from datetime import date
import robin_stocks.robinhood as rh

logging.basicConfig(level=logging.INFO, format="%(asctime)s  %(levelname)s  %(message)s", handlers=[logging.StreamHandler(sys.stdout)])
log = logging.getLogger("heatseeker.exit")

DRY_RUN        = os.getenv("DRY_RUN", "false").lower() == "true"
FORCE_CLOSE    = os.getenv("FORCE_CLOSE", "false").lower() == "true"
ACCOUNT_NUMBER = "634079917"
PROFIT_TARGET  = 1.00
STOP_LOSS      = -0.50

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

def main():
    login()
    today = date.today().strftime("%Y-%m-%d")
    log.info(f"Exit check: {today}  FORCE_CLOSE={FORCE_CLOSE}")
    positions = get_spy_0dte_positions()
    print("\n" + "="*60)
    print(f"HEATSEEKER EXIT CHECK - {today}")
    print("="*60)
    print(f"  Account: {ACCOUNT_NUMBER} (agentic)")
    if not positions:
        log.info("No open SPY 0DTE positions.")
        print("  No open positions.")
        print("="*60)
        return
    for p in positions:
        avg  = float(p.get("average_price", 0) or 0)
        bid, ask, mark = get_current_price(p)
        pnl_pct = (mark - avg) / avg if avg > 0 else 0
        pnl_usd = (mark - avg) * p["qty"] * 100
        log.info(f"POSITION: SPY {p['strike']}{p['opt_type'][0].upper()} qty={p['qty']} avg=${avg:.2f} mark=${mark:.2f} P&L={pnl_pct*100:+.1f}% (${pnl_usd:+.2f})")
        print(f"  SPY {p['strike']}{p['opt_type'][0].upper()}: avg=${avg:.2f} mark=${mark:.2f} P&L={pnl_pct*100:+.1f}% (${pnl_usd:+.2f})")
        reason = ""
        if FORCE_CLOSE:                reason = "FORCE CLOSE (3:45 PM ET)"
        elif pnl_pct >= PROFIT_TARGET: reason = f"PROFIT TARGET ({pnl_pct*100:.0f}%)"
        elif pnl_pct <= STOP_LOSS:     reason = f"STOP LOSS ({pnl_pct*100:.0f}%)"
        if reason:
            log.info(f"ACTION: {reason} - CLOSING")
            print(f"  --> {reason} - CLOSING")
            close_position(p, max(round(bid, 2), 0.01))
        else:
            log.info("ACTION: HOLD")
            print(f"  --> HOLD")
    print("="*60)

if __name__ == "__main__":
    main()
