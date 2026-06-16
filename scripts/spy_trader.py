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
    data = rh.stocks.ge
