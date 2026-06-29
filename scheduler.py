#!/usr/bin/env python3
import logging, sys, os, json
from datetime import datetime
from apscheduler.schedulers.blocking import BlockingScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.interval import IntervalTrigger
import pytz

logging.basicConfig(level=logging.INFO, format="%(asctime)s  %(levelname)s  %(message)s", handlers=[logging.StreamHandler(sys.stdout)])
log = logging.getLogger("heatseeker.scheduler")
ET = pytz.timezone("America/New_York")

DATA_DIR  = os.path.join(os.path.dirname(__file__), "data")
GEX_STATE = os.path.join(DATA_DIR, "gex_state.json")

def _write_gex_update(update: dict):
    os.makedirs(DATA_DIR, exist_ok=True)
    try:
        with open(GEX_STATE) as f:
            state = json.load(f)
    except Exception:
        state = {}
    state.update(update)
    with open(GEX_STATE, "w") as f:
        json.dump(state, f, indent=2)

def run_gex_analysis():
    """Live GEX scan every 15 min during market hours — populates dashboard without trading."""
    now = datetime.now(ET)
    t   = now.hour * 60 + now.minute
    if now.weekday() >= 5 or t < 570 or t > 960:
        return
    try:
        import importlib, scripts.spy_trader as trader
        importlib.reload(trader)
        trader.analyze_gex_only()
    except SystemExit:
        pass
    except Exception as e:
        log.warning(f"GEX analysis error: {e}")

def run_entry():
    now = datetime.now(ET)
    t   = now.hour * 60 + now.minute
    # Entry window: 9:45 AM – 12:30 PM ET, weekdays only
    if now.weekday() >= 5 or not (585 <= t <= 750):
        return
    log.info("=" * 60)
    log.info(f"ENTRY CHECK at {now.strftime('%H:%M')} ET")
    log.info("=" * 60)
    os.environ["DRY_RUN"] = "false"
    try:
        import importlib, scripts.spy_trader as trader
        importlib.reload(trader)
        trader.main()
    except SystemExit:
        pass
    except Exception as e:
        log.error(f"Entry error: {e}", exc_info=True)

def run_exit(force=False):
    now = datetime.now(ET)
    t   = now.hour * 60 + now.minute
    # Exit checks: 10:00 AM – 3:30 PM ET, weekdays only
    if now.weekday() >= 5 or t < 600 or t > 930:
        return
    log.info("=" * 60)
    log.info(f"EXIT CHECK at {now.strftime('%H:%M')} ET  FORCE={force}")
    log.info("=" * 60)
    os.environ["DRY_RUN"] = "false"
    os.environ["FORCE_CLOSE"] = "true" if force else "false"
    try:
        import importlib, scripts.spy_exit as exiter
        importlib.reload(exiter)
        exiter.main()
    except SystemExit:
        pass
    except Exception as e:
        log.error(f"Exit error: {e}", exc_info=True)

def force_close():
    now = datetime.now(ET)
    if now.weekday() >= 5:
        return
    log.info("=" * 60)
    log.info("3:30 PM FORCE CLOSE")
    log.info("=" * 60)
    os.environ["DRY_RUN"] = "false"
    os.environ["FORCE_CLOSE"] = "true"
    try:
        import importlib, scripts.spy_exit as exiter
        importlib.reload(exiter)
        exiter.main()
    except SystemExit:
        pass
    except Exception as e:
        log.error(f"Force close error: {e}", exc_info=True)

def refresh_market_data():
    """Write live SPY + VIX to gex_state.json via Robinhood (yfinance fallback)."""
    now = datetime.now(ET)
    if now.weekday() >= 5:
        return
    t = now.hour * 60 + now.minute
    if t < 570 or t > 960:
        return
    spot = vix = None
    try:
        import robin_stocks.robinhood as rh
        u = os.getenv("RH_USERNAME", "")
        p = os.getenv("RH_PASSWORD", "")
        if u and p:
            rh.login(u, p, store_session=True)
            quotes = rh.get_quotes(["SPY"], info=None) or [{}]
            raw = quotes[0].get("last_trade_price") or quotes[0].get("adjusted_previous_close")
            if raw:
                spot = round(float(raw), 2)
    except Exception as e:
        log.debug(f"RH price fetch: {e}")
    if not spot:
        try:
            import yfinance as yf
            fi = yf.Ticker("SPY").fast_info
            for attr in ("last_price", "regularMarketPrice", "previousClose"):
                v = getattr(fi, attr, None)
                if v:
                    spot = round(float(v), 2)
                    break
            fi2 = yf.Ticker("^VIX").fast_info
            for attr in ("last_price", "regularMarketPrice", "previousClose"):
                v = getattr(fi2, attr, None)
                if v:
                    vix = round(float(v), 2)
                    break
        except Exception as e:
            log.debug(f"yfinance fallback: {e}")
    update = {"market_updated": now.strftime("%H:%M:%S ET")}
    if spot: update["spot"] = spot
    if vix:  update["vix"]  = vix
    _write_gex_update(update)
    log.info(f"Market refresh: SPY={spot}  VIX={vix}")

def main():
    scheduler = BlockingScheduler(timezone=ET)

    # Entry: every 15 minutes — bot self-gates on time window + open position check
    scheduler.add_job(run_entry, IntervalTrigger(minutes=15), id="entry_scan", name="Entry scan")

    # Exit monitor: every 15 minutes — checks trailing stop and hard stop
    scheduler.add_job(lambda: run_exit(False), IntervalTrigger(minutes=15), id="exit_scan", name="Exit scan")

    # Force close: 3:30 PM sharp every weekday
    scheduler.add_job(force_close, CronTrigger(day_of_week="mon-fri", hour=15, minute=30, timezone=ET), id="force_close", name="3:30 PM force close")

    # Live GEX analysis every 15 min during market hours — keeps dashboard populated
    scheduler.add_job(run_gex_analysis, IntervalTrigger(minutes=15), id="gex_scan", name="Live GEX scan")

    # Market data refresh: every 5 minutes (yfinance, no login)
    scheduler.add_job(refresh_market_data, IntervalTrigger(minutes=5), id="market_refresh", name="Market data refresh")

    log.info("HEATSEEKER Scheduler running:")
    log.info("  GEX scan    : every 15 min (live king node, direction, confidence → dashboard)")
    log.info("  Entry scan  : every 15 min (self-gates: 9:45 AM–12:30 PM ET, no open position)")
    log.info("  Exit scan   : every 15 min (trailing stop, -50% hard stop)")
    log.info("  Force close : 3:30 PM ET Mon-Fri")
    log.info("  Market data : every 5 min (SPY price + VIX via yfinance)")

    try:
        scheduler.start()
    except (KeyboardInterrupt, SystemExit):
        log.info("Scheduler stopped.")

if __name__ == "__main__":
    main()
