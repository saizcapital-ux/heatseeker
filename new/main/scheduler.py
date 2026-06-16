#!/usr/bin/env python3
import logging
import sys
import os
from apscheduler.schedulers.blocking import BlockingScheduler
from apscheduler.triggers.cron import CronTrigger
import pytz

logging.basicConfig(level=logging.INFO, format="%(asctime)s  %(levelname)s  %(message)s", handlers=[logging.StreamHandler(sys.stdout)])
log = logging.getLogger("heatseeker.scheduler")
ET = pytz.timezone("America/New_York")

def run_entry():
    log.info("SCHEDULER: Firing ENTRY job")
    os.environ["DRY_RUN"] = "false"
    os.environ["FORCE_CLOSE"] = "false"
    try:
        import importlib, scripts.spy_trader as trader
        importlib.reload(trader)
        trader.main()
    except SystemExit:
        pass
    except Exception as e:
        log.error(f"Entry job error: {e}", exc_info=True)

def run_exit(force=False):
    log.info(f"SCHEDULER: Firing EXIT job  FORCE_CLOSE={force}")
    os.environ["DRY_RUN"] = "false"
    os.environ["FORCE_CLOSE"] = "true" if force else "false"
    try:
        import importlib, scripts.spy_exit as exiter
        importlib.reload(exiter)
        exiter.main()
    except SystemExit:
        pass
    except Exception as e:
        log.error(f"Exit job error: {e}", exc_info=True)

def main():
    scheduler = BlockingScheduler(timezone=ET)
    scheduler.add_job(run_entry, CronTrigger(day_of_week="mon-fri", hour=9, minute=45, timezone=ET), id="entry")
    scheduler.add_job(lambda: run_exit(False), CronTrigger(day_of_week="mon-fri", hour=11, minute=0, timezone=ET), id="exit_11am")
    scheduler.add_job(lambda: run_exit(False), CronTrigger(day_of_week="mon-fri", hour=13, minute=0, timezone=ET), id="exit_1pm")
    scheduler.add_job(lambda: run_exit(True), CronTrigger(day_of_week="mon-fri", hour=15, minute=45, timezone=ET), id="force_close")
    log.info("HEATSEEKER Scheduler started:")
    log.info("  9:45 AM ET  -> Entry")
    log.info("  11:00 AM ET -> Exit check")
    log.info("  1:00 PM ET  -> Exit check")
    log.info("  3:45 PM ET  -> Force close")
    try:
        scheduler.start()
    except (KeyboardInterrupt, SystemExit):
        log.info("Scheduler stopped.")

if __name__ == "__main__":
    main()
