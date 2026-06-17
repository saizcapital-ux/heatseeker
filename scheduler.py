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

DATA_DIR   = os.path.join(os.path.dirname(__file__), "data")
GEX_STATE  = os.path.join(DATA_DIR, "gex_state.json")

def run_entry():
    log.info("=" * 60)
    log.info("SCHEDULER: Firing ENTRY job")
    log.info("=" * 60)
    os.environ["DRY_RUN"] = "false"
    os.environ["FORCE_CLOSE"] = "false"
    try:
        import importlib, scripts.spy_trader as trader
        importlib.reload(trader)
        trader.main()
    except SystemExit:
        pass
    except Exception as e:
        log.error(f"Entry error: {e}", exc_info=True)

def run_exit(force=False):
    log.info("=" * 60)
    log.info(f"SCHEDULER: Firing EXIT job FORCE_CLOSE={force}")
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

def _write_gex_update(update: dict):
    try:
        with open(GEX_STATE) as f:
            state = json.load(f)
    except Exception:
        state = {}
    state.update(update)
    with open(GEX_STATE, "w") as f:
        json.dump(state, f, indent=2)

def refresh_market_data():
    """Write live SPY price, VIX, and Robinhood balance to gex_state.json every 5 minutes."""
    now = datetime.now(ET)
    t = now.hour * 60 + now.minute
    update = {"market_updated": now.strftime("%H:%M:%S ET")}

    # Always refresh balance regardless of market hours
    try:
        import robin_stocks.robinhood as rh
        login = rh.login(os.getenv("ROBINHOOD_USERNAME"), os.getenv("ROBINHOOD_PASSWORD"),
                         store_session=True, expiresIn=86400)
        profile = rh.profiles.load_portfolio_profile(account_number="634079917")
        bp = profile.get("buying_power") or profile.get("withdrawable_amount")
        if bp:
            update["account_balance"] = round(float(bp), 2)
            update["buying_power"]    = round(float(bp), 2)
            log.info(f"Balance refresh: ${float(bp):.2f}")
    except Exception as e:
        log.warning(f"Balance refresh error: {e}")

    # Market data only during hours
    if now.weekday() < 5 and 570 <= t <= 960:
        try:
            import yfinance as yf
            spy_info = yf.Ticker("SPY").fast_info
            vix_info = yf.Ticker("^VIX").fast_info
            spot = getattr(spy_info, "last_price", None)
            vix  = getattr(vix_info, "last_price", None)
            if spot: update["spot"] = round(float(spot), 2)
            if vix:  update["vix"]  = round(float(vix), 2)
            log.info(f"Market refresh: SPY=${spot:.2f}  VIX={vix:.2f}")
        except Exception as e:
            log.warning(f"Market data error: {e}")

    _write_gex_update(update)

def main():
    scheduler = BlockingScheduler(timezone=ET)

    # Trading jobs
    scheduler.add_job(run_entry,               CronTrigger(day_of_week="mon-fri", hour=9,  minute=45, timezone=ET), id="entry",       name="Morning entry")
    scheduler.add_job(lambda: run_exit(False), CronTrigger(day_of_week="mon-fri", hour=11, minute=0,  timezone=ET), id="exit_11am",   name="11 AM exit check")
    scheduler.add_job(lambda: run_exit(False), CronTrigger(day_of_week="mon-fri", hour=13, minute=0,  timezone=ET), id="exit_1pm",    name="1 PM exit check")
    scheduler.add_job(lambda: run_exit(True),  CronTrigger(day_of_week="mon-fri", hour=15, minute=30, timezone=ET), id="force_close", name="3:30 PM force close")

    # Live data refresh every 5 minutes
    scheduler.add_job(refresh_market_data, IntervalTrigger(minutes=5), id="market_refresh", name="Market data refresh")

    log.info("HEATSEEKER Scheduler running. All times ET:")
    log.info("  9:45 AM  Mon-Fri -> Entry (buy signal)")
    log.info("  11:00 AM Mon-Fri -> Exit check")
    log.info("  1:00 PM  Mon-Fri -> Exit check")
    log.info("  3:30 PM  Mon-Fri -> Force close")
    log.info("  Every 5m          -> Market data refresh (SPY/VIX)")
    try:
        scheduler.start()
    except (KeyboardInterrupt, SystemExit):
        log.info("Scheduler stopped.")

if __name__ == "__main__":
    main()
