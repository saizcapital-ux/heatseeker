#!/usr/bin/env python3
import logging, sys, os
from apscheduler.schedulers.blocking import BlockingScheduler
from apscheduler.triggers.cron import CronTrigger
import pytz

logging.basicConfig(level=logging.INFO, format="%(asctime)s  %(levelname)s  %(message)s", handlers=[logging.StreamHandler(sys.stdout)])
log = logging.getLogger("heatseeker.scheduler")
ET = pytz.timezone("America/New_York")

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

def main():
    scheduler = BlockingScheduler(timezone=ET)
    scheduler.add_job(run_entry,               CronTrigger(day_of_week="mon-fri", hour=9,  minute=45, timezone=ET), id="entry",       name="Morning entry")
    scheduler.add_job(lambda: run_exit(False), CronTrigger(day_of_week="mon-fri", hour=11, minute=0,  timezone=ET), id="exit_11am",   name="11 AM exit check")
    scheduler.add_job(lambda: run_exit(False), CronTrigger(day_of_week="mon-fri", hour=13, minute=0,  timezone=ET), id="exit_1pm",    name="1 PM exit check")
    scheduler.add_job(lambda: run_exit(True),  CronTrigger(day_of_week="mon-fri", hour=15, minute=30, timezone=ET), id="force_close", name="3:30 PM force close")
    log.info("HEATSEEKER Scheduler running. All times ET:")
    log.info("  9:45 AM  Mon-Fri -> Entry (buy signal, 9:45-10:15 ET window)")
    log.info("  11:00 AM Mon-Fri -> Exit check (trailing stop / 50% loss)")
    log.info("  1:00 PM  Mon-Fri -> Exit check (trailing stop / 50% loss)")
    log.info("  3:30 PM  Mon-Fri -> Force close all positions (moved from 3:45, research-backed)")
    try:
        scheduler.start()
    except (KeyboardInterrupt, SystemExit):
        log.info("Scheduler stopped.")

if __name__ == "__main__":
    main()
