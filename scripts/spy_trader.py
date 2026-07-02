#!/usr/bin/env python3
import os, sys, json, logging
from datetime import date, datetime
from zoneinfo import ZoneInfo
import robin_stocks.robinhood as rh
from scripts.journal import get_smart_size, log_entry, print_dashboard
from scripts.gex_study import (extract_gex_features, gex_confidence_score,
                                print_dealer_flow, print_pattern_report)

logging.basicConfig(level=logging.INFO, format="%(asctime)s  %(levelname)s  %(message)s", handlers=[logging.StreamHandler(sys.stdout)])
log = logging.getLogger("heatseeker")

DRY_RUN        = os.getenv("DRY_RUN", "false").lower() == "true"
AGENTIC_ACCOUNT = "634079917"   # the ONLY account this bot may ever trade
ACCOUNT_NUMBER = AGENTIC_ACCOUNT

def _assert_agentic(acct):
    """Hard guard: refuse to place any order on a non-agentic account."""
    if str(acct) != AGENTIC_ACCOUNT:
        raise SystemExit(f"REFUSED: order targeted account {acct!r}, not the agentic account {AGENTIC_ACCOUNT}")
    return acct
STRIKE_WINDOW  = 20       # expanded: low balance needs further-OTM cheap contracts
VIX_MIN        = 14.0
VIX_MAX        = 28.0
DELTA_MIN      = 0.15     # lowered: allow cheap OTM contracts when balance < $100
DELTA_MAX      = 0.55
DELTA_FLOOR    = 0.10     # hard floor: never buy below this delta, even on the
                          # low-balance fallback — deeper-OTM 0DTE puts/calls are
                          # near-certain-loss lottery tickets. Skip the trade instead.
DIP_BUY_MAX_PCT = 0.010   # dip-buy calls only within 1% below prev close (else it's a real breakdown)
# Rip-fade (mirror of dip-buy): fade the CALL WALL with puts in a pinning regime
RIP_FADE_NEAR      = 0.50  # spot within $ below the call wall counts as "at" it
RIP_FADE_MAX_BREAK = 0.30  # if spot breaks above the wall by more than this, it's a breakout — don't fade
RIP_FADE_MIN_ROOM  = 1.50  # need $ of downside to max pain to make the fade worth it

# ── Creator-derived constants ─────────────────────────────────────────────────
# Whaley: IV beats realized vol ~78-85% of the time — use this as premium gate
VRP_CHEAP_IV_THRESHOLD = 0.03   # IV < RV + 3pts → cheap premium, normal size
VRP_RICH_IV_THRESHOLD  = 0.08   # IV > RV + 8pts → rich premium, tighten spend
# Galai: OPEX put-wall floors create mechanical dealer bids — treat as support
OPEX_PUT_WALL_FLOOR    = 680.0  # approximate near-term institutional floor
OPEX_CRASH_FLOOR       = 650.0  # ultimate crash hedge floor (41k OI)
# Monthly OPEX rubber band cycle — update these each month as OI shifts
MONTHLY_OPEX_DATE      = "2026-07-17"  # next monthly expiry
MONTHLY_PUT_WALL       = 730.0         # dominant monthly put OI strike → structural floor
MONTHLY_CALL_TARGET    = 752.0         # July call resistance (updated Jun 30, SPY @$740.88)
# Brenner: jump risk premium is 2x larger in 0DTE — favor early entries
JUMP_PREMIUM_DECAY_HOUR = 11    # after 11 AM ET, jump premium starts collapsing
# Term structure: contango depth drives size multiplier (Whaley/Galai framework)
CONTANGO_BOOST_SLOPE   = 0.10   # VIX3M/VIX slope > 10% → deep contango → scale up
CONTANGO_NEUTRAL_SLOPE = 0.00   # slope 0-10% → neutral
# contango neutral slope 0–0.10 → no boost; backwardation already gated
ET             = ZoneInfo("America/New_York")
# Known NYSE market holidays 2026 — bot will skip these automatically
NYSE_HOLIDAYS_2026 = {
    "2026-01-01",  # New Year's Day
    "2026-01-19",  # MLK Day
    "2026-02-16",  # Presidents Day
    "2026-04-03",  # Good Friday
    "2026-05-25",  # Memorial Day
    "2026-06-19",  # Juneteenth
    "2026-07-03",  # Independence Day (observed)
    "2026-09-07",  # Labor Day
    "2026-11-26",  # Thanksgiving
    "2026-11-27",  # Day after Thanksgiving (early close — skip)
    "2026-12-25",  # Christmas
}
SKIP_DATES = NYSE_HOLIDAYS_2026 | (set(os.getenv("SKIP_DATES", "").split(",")) - {""})
GEX_STATE_FILE = os.path.join(os.getenv("RH_SESSION_DIR", "/data/rh_session"), "gex_state.json")

def write_gex_state(update: dict):
    try:
        try:
            with open(GEX_STATE_FILE) as f:
                state = json.load(f)
        except Exception:
            state = {}
        state.update(update)
        state["last_updated"] = datetime.now(ET).isoformat()
        with open(GEX_STATE_FILE, "w") as f:
            json.dump(state, f, indent=2)
    except Exception as e:
        log.warning(f"Could not write GEX state: {e}")

def _et_now():
    return datetime.now(ET)

def check_time_window():
    """Allow entries 9:45 AM–12:30 PM ET. After 10:30 extra GEX confidence required (handled at Gate 7).
    After 12:30 PM theta decay on 0DTE makes buying too risky."""
    now = _et_now()
    h, m = now.hour, now.minute
    t = h * 60 + m
    if t < 585:
        return f"Too early ({now.strftime('%H:%M')} ET) — market opens 9:30, best entry 9:45 AM ET"
    if t > 750:   # after 12:30 PM
        return f"Too late ({now.strftime('%H:%M')} ET) — 0DTE theta too high after 12:30 PM"
    return None  # 9:45 AM – 12:30 PM: entry allowed

def net_gex(oi, gamma, spot, is_call):
    return (1 if is_call else -1) * oi * gamma * spot * 100

def _get_mfa_code():
    """Generate MFA code from TOTP secret (permanent) or use static code (one-time)."""
    totp_secret = os.getenv("RH_TOTP_SECRET", "").strip()
    if totp_secret:
        try:
            import pyotp
            code = pyotp.TOTP(totp_secret).now()
            log.info(f"TOTP code generated: {code}")
            return code
        except ImportError:
            log.warning("pyotp not installed — install it: pip install pyotp")
        except Exception as e:
            log.warning(f"TOTP generation failed: {e}")
    # Fallback: static code from env (only valid for ~30s)
    return os.getenv("RH_MFA_CODE", "") or None

def login():
    user = os.getenv("RH_USERNAME", "")
    pwd  = os.getenv("RH_PASSWORD", "")
    if not user or not pwd:
        log.error("RH_USERNAME and RH_PASSWORD must be set.")
        sys.exit(1)
    import pathlib
    session_dir = pathlib.Path(os.getenv("RH_SESSION_DIR", "/data/rh_session"))
    session_dir.mkdir(parents=True, exist_ok=True)
    tokens_dir = pathlib.Path.home() / ".tokens"
    if not tokens_dir.exists():
        tokens_dir.symlink_to(session_dir)
        log.info(f"Symlinked ~/.tokens → {session_dir}")
    elif tokens_dir.is_symlink() and tokens_dir.resolve() != session_dir.resolve():
        tokens_dir.unlink()
        tokens_dir.symlink_to(session_dir)
        log.info(f"Updated symlink ~/.tokens → {session_dir}")
    mfa = _get_mfa_code()
    log.info(f"Logging in to Robinhood (mfa={'yes' if mfa else 'no'})...")
    try:
        rh.login(
            username=user, password=pwd,
            mfa_code=mfa,
            store_session=True,
            expiresIn=86400,
            pickle_name="heatseeker",
        )
    except Exception as e:
        log.error(f"Robinhood login exception: {e}")
        sys.exit(1)
    try:
        if not rh.profiles.load_portfolio_profile():
            raise Exception("empty profile")
        log.info("Login verified OK")
    except Exception as e:
        log.error(f"Login failed: {e}")
        sys.exit(1)

def get_account_balance():
    try:
        p = rh.profiles.load_account_profile(account_number=ACCOUNT_NUMBER, info=None)
        if p:
            bp = float(p.get("buying_power", 0) or 0)
            log.info(f"Account buying power: ${bp:.2f}")
            write_gex_state({"account_balance": round(bp, 2), "buying_power": round(bp, 2)})
            return bp
    except Exception as e:
        log.warning(f"Could not fetch balance: {e}")
    return 21.64

def has_open_position():
    """Return True if we already hold an open SPY 0DTE options position today."""
    try:
        today = date.today().strftime("%Y-%m-%d")
        positions = rh.options.get_open_option_positions(account_number=ACCOUNT_NUMBER) or []
        for p in positions:
            qty = float(p.get("quantity", 0) or 0)
            if qty > 0:
                exp = p.get("expiration_date", "")
                symbol = p.get("chain_symbol", "")
                if symbol == "SPY" and exp == today:
                    log.info(f"Open position found: {symbol} exp={exp} qty={qty}")
                    return True
    except Exception as e:
        log.warning(f"Position check error: {e}")
    return False

_VIX_UUID = "3b912aa2-88f9-4682-8ae3-e39520bdf4db"  # Robinhood index UUID for VIX

def get_vix():
    # Primary: Robinhood index-instruments endpoint (correct path for index values)
    try:
        data = rh.helper.request_get(
            f"https://api.robinhood.com/marketdata/index-instruments/{_VIX_UUID}/quotes/"
        )
        if data and data.get("value"):
            v = round(float(data["value"]), 2)
            log.info(f"VIX (RH index): {v:.2f}")
            return v
    except Exception:
        pass
    # Fallback: yfinance
    try:
        import yfinance as yf
        v = yf.Ticker("^VIX").fast_info.last_price
        if v and float(v) > 0:
            log.info(f"VIX (yf): {float(v):.2f}")
            return round(float(v), 2)
    except Exception:
        pass
    log.warning("VIX unavailable - defaulting to 15.0")
    return 15.0

def get_vix3m():
    """Fetch VIX3M (93-day forward vol) for term structure slope calculation."""
    try:
        import yfinance as yf
        v = yf.Ticker("^VIX3M").fast_info.last_price
        if v and float(v) > 0:
            return round(float(v), 2)
    except Exception:
        pass
    return None

def compute_term_structure(vix, vix3m):
    """
    Slope = (VIX3M - VIX) / VIX
    > 0.10  → deep contango  → size boost
    0-0.10  → flat/neutral   → no adjustment
    < 0     → backwardation  → FLAT gate (risk-off)
    """
    if vix3m is None or vix <= 0:
        return 0.0, "unknown"
    slope = (vix3m - vix) / vix
    if slope > CONTANGO_BOOST_SLOPE:
        label = "deep_contango"
    elif slope >= CONTANGO_NEUTRAL_SLOPE:
        label = "contango"
    else:
        label = "backwardation"
    log.info(f"Term structure: VIX={vix:.2f} VIX3M={vix3m:.2f} slope={slope:+.3f} → {label}")
    return round(slope, 4), label

def compute_vrp(atm_iv, vix):
    """
    Volatility Risk Premium gate (Whaley).
    IV as fraction vs VIX as rough 30-day realized proxy.
    Returns (vrp_spread, label).
    vrp_spread = ATM IV annualized - VIX/100 (realized vol proxy)
    """
    if atm_iv <= 0 or vix <= 0:
        return 0.0, "unknown"
    realized_proxy = vix / 100.0
    vrp = atm_iv - realized_proxy
    if vrp > VRP_RICH_IV_THRESHOLD:
        label = "rich"
    elif vrp > VRP_CHEAP_IV_THRESHOLD:
        label = "elevated"
    else:
        label = "cheap"
    log.info(f"VRP: ATM_IV={atm_iv*100:.1f}% realized_proxy={realized_proxy*100:.1f}% spread={vrp*100:+.1f}pp → {label}")
    return round(vrp, 4), label

def jump_premium_time_multiplier():
    """
    Brenner: 0DTE jump risk premium is 2x larger early in session.
    Decays as the day progresses — favor entries before 11 AM.
    Returns a size multiplier: 1.0 early, scaling down after 11 AM.
    """
    now = _et_now()
    t = now.hour * 60 + now.minute
    open_t   = 9 * 60 + 30    # 9:30 AM = 570
    peak_t   = 9 * 60 + 45    # 9:45 AM = 585 (entry start)
    decay_t  = JUMP_PREMIUM_DECAY_HOUR * 60   # 11:00 AM = 660
    close_t  = 12 * 60 + 30   # 12:30 PM = 750 (entry end)

    if t <= peak_t:
        mult = 1.0
    elif t <= decay_t:
        # Linear decay from 1.0 down to 0.75 between 9:45 and 11:00
        pct = (t - peak_t) / (decay_t - peak_t)
        mult = 1.0 - 0.25 * pct
    else:
        # Continue decay to 0.60 by 12:30 PM
        pct = (t - decay_t) / (close_t - decay_t)
        mult = 0.75 - 0.15 * pct
    mult = round(max(0.60, mult), 2)
    log.info(f"Jump premium time multiplier: {mult:.2f} (entry at {now.strftime('%H:%M')} ET)")
    return mult

def opex_gravity_check(spot, direction):
    """
    Galai: monthly OPEX put walls at $680 (34k OI) and $650 (41k OI) create
    mechanical dealer bids. Check SPY position relative to these floors.
    Returns (ok, size_adj, note).
    """
    if spot < OPEX_CRASH_FLOOR:
        return False, 0.0, f"SPY ${spot:.0f} below crash floor ${OPEX_CRASH_FLOOR:.0f} — FLAT"
    if spot < OPEX_PUT_WALL_FLOOR:
        if direction == "call":
            return False, 0.0, f"SPY ${spot:.0f} below put wall ${OPEX_PUT_WALL_FLOOR:.0f} — negative gamma zone, no calls"
        else:
            # Puts valid in this zone — dealers buying stock to hedge, volatility high
            return True, 1.1, f"SPY ${spot:.0f} in put-wall zone — put direction valid, slight boost"
    elif spot < OPEX_PUT_WALL_FLOOR + 15:
        # Within 15 pts of put wall — be careful, tighten a bit
        note = f"SPY ${spot:.0f} near put wall ${OPEX_PUT_WALL_FLOOR:.0f} — normal but cautious"
        return True, 0.9, note
    else:
        # Well above put walls — dealer bid floor in place, calls have structural support
        note = f"SPY ${spot:.0f} above put walls — dealer bid floor active, {direction.upper()} supported"
        return True, 1.05 if direction == "call" else 1.0, note

def get_spot_and_prev_close(symbol="SPY"):
    data = rh.stocks.get_quotes(symbol)
    if not data:
        raise RuntimeError(f"No quote for {symbol}")
    q = data[0]
    spot, prev = float(q["last_trade_price"]), float(q["adjusted_previous_close"])
    log.info(f"{symbol} spot={spot:.2f}  prev_close={prev:.2f}")
    return spot, prev

def get_atm_strikes(symbol, expiration, spot):
    calls, puts = [], []
    all_opts = rh.options.find_options_by_expiration(inputSymbols=symbol, expirationDate=expiration)
    for o in (all_opts or []):
        if abs(float(o["strike_price"]) - spot) > STRIKE_WINDOW:
            continue
        if o.get("type") == "call": calls.append(o)
        elif o.get("type") == "put": puts.append(o)
    log.info(f"Found {len(calls)} calls and {len(puts)} puts within +/-${STRIKE_WINDOW}")
    return calls, puts

def fetch_greeks(instruments):
    for o in instruments:
        try:
            md_list = rh.options.get_option_market_data_by_id(o["id"])
            md = md_list[0] if isinstance(md_list, list) and md_list else (md_list or {})
            o["gamma"]         = float(md.get("gamma", 0) or 0)
            o["open_interest"] = int(md.get("open_interest", 0) or 0)
            o["volume"]        = int(md.get("volume", 0) or 0)
            o["ask_price"]     = float(md.get("ask_price", 0) or 0)
            o["bid_price"]     = float(md.get("bid_price", 0) or 0)
            o["mark_price"]    = float(md.get("adjusted_mark_price", 0) or 0)
            o["delta"]         = abs(float(md.get("delta", 0) or 0))
            o["implied_volatility"] = float(md.get("implied_volatility", 0) or 0)
        except Exception:
            o["gamma"] = o["open_interest"] = o["volume"] = o["ask_price"] = 0
            o["bid_price"] = o["mark_price"] = o["delta"] = o["implied_volatility"] = 0

def compute_gex_nodes(calls, puts, spot):
    nodes = {}
    for o in calls:
        s = float(o["strike_price"]); nodes.setdefault(s, 0)
        nodes[s] += net_gex(o["open_interest"], o["gamma"], spot, True)
    for o in puts:
        s = float(o["strike_price"]); nodes.setdefault(s, 0)
        nodes[s] += net_gex(o["open_interest"], o["gamma"], spot, False)
    return sorted(nodes.items())

def find_king(nodes):
    if not nodes: return None, 0
    return max(nodes, key=lambda x: abs(x[1]))

def compute_max_pain(calls, puts):
    """Strike that minimizes total intrinsic payout to option holders — the
    classic 0DTE gravity magnet. None if the chain is empty."""
    all_k = sorted(set(float(o["strike_price"]) for o in calls) |
                   set(float(o["strike_price"]) for o in puts))
    if not all_k:
        return None
    coi = {float(o["strike_price"]): float(o.get("open_interest", 0) or 0) for o in calls}
    poi = {float(o["strike_price"]): float(o.get("open_interest", 0) or 0) for o in puts}
    max_pain, best = None, None
    for k in all_k:
        pay = (sum(coi.get(s, 0) * max(0.0, k - s) for s in all_k) +
               sum(poi.get(s, 0) * max(0.0, s - k) for s in all_k))
        if best is None or pay < best:
            best, max_pain = pay, k
    return max_pain

def pin_reversion_direction(spot, gamma_flip, pin, king_gex=None):
    """Regime-aware direction. king_gex's SIGN is a gamma-regime tell, not a
    price bet — so we read the regime from the gamma flip and the bias from
    where spot sits relative to the pin (gravity magnet):

      * spot BELOW the gamma flip  → negative gamma (trending) → breakdown
        continuation → PUT.
      * spot ABOVE the flip (positive gamma / pinning) → mean-revert to the pin:
          spot ABOVE pin → drift DOWN → PUT
          spot BELOW pin → drift UP   → CALL
      * at/near the pin, or no pin → default CALL bias (dealer bid).

    Returns 'call' or 'put'.
    """
    tol = max(0.5, spot * 0.0007)   # ~$0.5 neutral band around the pin
    if gamma_flip is not None and spot < gamma_flip - tol:
        return "put"                # negative-gamma trend zone → follow the break down
    if pin is not None:
        if spot > pin + tol:
            return "put"            # extended above the magnet → revert down
        if spot < pin - tol:
            return "call"           # below the magnet → revert up
    return "call"

def compute_opex_cycle(spot):
    """
    Monthly OPEX rubber band cycle.
    Heavy put OI at the monthly strike creates a mechanical floor:
      dealers who sold puts must short SPY to hedge → when puts decay,
      they buy back → V-shape snap → new highs.
    Phase 1 FLUSH:    spot approaching wall (within $10), caution
    Phase 2 PIN:      spot within $3 of wall — rubber band fully loaded
    Phase 3 SNAP:     bouncing off wall, V-shape in progress
    Phase 4 NEW HIGH: above call target — new-high momentum
    Returns dict with phase, call_boost, put_block, note.
    """
    wall   = MONTHLY_PUT_WALL
    target = MONTHLY_CALL_TARGET
    from datetime import date as _date
    try:
        opex_dt = _date.fromisoformat(MONTHLY_OPEX_DATE)
        days_to_opex = max(0, (opex_dt - _date.today()).days)
    except Exception:
        days_to_opex = 0

    dist = spot - wall  # positive = above wall

    if dist < 0:
        phase, label = 1, "BELOW WALL"
        call_boost, put_block = 0.0, False
        note = f"SPY ${spot:.0f} BELOW ${wall:.0f} put wall — floor broken, FLAT"
    elif dist <= 3:
        phase, label = 2, "PINNED AT WALL"
        call_boost, put_block = 1.25, True
        note = f"SPY ${spot:.0f} pinned at ${wall:.0f} monthly wall — rubber band LOADED, calls favored"
    elif dist <= 10:
        phase, label = 3, "V-SNAP RECOVERY"
        call_boost, put_block = 1.15, True
        note = f"SPY ${spot:.0f} snapping from ${wall:.0f} — V-shape in progress, calls favored"
    elif spot >= target:
        phase, label = 4, "NEW HIGHS"
        call_boost, put_block = 1.10, False
        note = f"SPY ${spot:.0f} above target ${target:.0f} — new high territory, trend calls"
    else:
        phase, label = 3, "RECOVERY"
        call_boost, put_block = 1.10, False
        note = f"SPY ${spot:.0f} recovering above ${wall:.0f} wall — {days_to_opex}d to OPEX, bullish structure"

    return {
        "opex_phase":        phase,
        "opex_phase_label":  label,
        "opex_call_boost":   call_boost,
        "opex_put_block":    put_block,
        "opex_cycle_note":   note,
        "opex_days":         days_to_opex,
        "monthly_put_wall":  wall,
        "monthly_call_target": target,
        "monthly_opex_date": MONTHLY_OPEX_DATE,
    }

def compute_ivr(atm_iv):
    """
    Estimate IVR using ATM IV vs rough 52-week range.
    Returns 0-100. Without historical data we use VIX as proxy:
    IVR = (atm_iv - iv_low) / (iv_high - iv_low) * 100
    Uses historical SPY IV range: low ~10%, high ~80%.
    """
    iv_low, iv_high = 0.10, 0.80
    ivr = max(0, min(100, (atm_iv - iv_low) / (iv_high - iv_low) * 100))
    return round(ivr, 1)

def get_vix_prev_close():
    """Fetch VIX previous session close for vanna flow calculation."""
    try:
        import yfinance as yf
        hist = yf.Ticker("^VIX").history(period="3d")
        if len(hist) >= 2:
            return round(float(hist["Close"].iloc[-2]), 2)
    except Exception:
        pass
    return None

def compute_vanna_flow(vix_now, vix_prev_close):
    """
    Cem Karsan / Kai Volatility: vanna is the rate of change of delta w.r.t. vol.
    When VIX drops intraday, dealers who are long calls see their delta rise →
    they must BUY underlying to stay delta-neutral → mechanical bid under SPY.
    When VIX rises, the reverse: dealers sell → mechanical pressure on SPY.
    Returns (size_multiplier, label).
    """
    if vix_prev_close is None or vix_prev_close <= 0:
        return 1.0, "unknown"
    change_pct = (vix_now - vix_prev_close) / vix_prev_close
    if change_pct < -0.07:
        label, mult = "strong_vanna_bid", 1.25    # VIX -7%+: strong mechanical buying
    elif change_pct < -0.03:
        label, mult = "vanna_bid", 1.12           # VIX -3-7%: vanna tailwind for calls
    elif change_pct < -0.01:
        label, mult = "vanna_mild_bid", 1.05      # VIX -1-3%: mild tailwind
    elif change_pct > 0.07:
        label, mult = "vanna_sell", 0.65          # VIX +7%+: mechanical selling — no calls
    elif change_pct > 0.03:
        label, mult = "vanna_headwind", 0.80      # VIX +3-7%: reduce call size
    elif change_pct > 0.01:
        label, mult = "vanna_mild_headwind", 0.92
    else:
        label, mult = "vanna_neutral", 1.00
    log.info(f"Vanna flow (Karsan): VIX {vix_prev_close:.2f}→{vix_now:.2f} ({change_pct:+.1%}) → {label} ×{mult}")
    return mult, label

def compute_skew_signal(calls, puts, spot):
    """
    Karsan: implied skew = put IV premium over call IV.
    Elevated put skew means market is pricing downside fear — bearish structural signal.
    Reduces call confidence when market is pricing in crash risk.
    Returns (size_multiplier, label, skew_ratio).
    """
    atm_range = 5.0
    atm_calls = [o for o in calls if abs(float(o["strike_price"]) - spot) <= atm_range and o.get("implied_volatility", 0) > 0]
    atm_puts  = [o for o in puts  if abs(float(o["strike_price"]) - spot) <= atm_range and o.get("implied_volatility", 0) > 0]
    if not atm_calls or not atm_puts:
        return 1.0, "unknown", 1.0
    call_iv = sum(o["implied_volatility"] for o in atm_calls) / len(atm_calls)
    put_iv  = sum(o["implied_volatility"] for o in atm_puts)  / len(atm_puts)
    skew = put_iv / call_iv if call_iv > 0 else 1.0
    if skew > 1.25:
        label, mult = "extreme_put_skew", 0.60   # crash fear priced in — no calls
    elif skew > 1.15:
        label, mult = "heavy_put_skew", 0.75
    elif skew > 1.08:
        label, mult = "elevated_put_skew", 0.88
    elif skew < 0.90:
        label, mult = "call_skew", 1.08           # rare — calls pricier than puts
    else:
        label, mult = "neutral_skew", 1.00
    log.info(f"Skew (Karsan): put_iv={put_iv:.3f} call_iv={call_iv:.3f} ratio={skew:.2f} → {label} ×{mult}")
    return mult, label, round(skew, 3)

def compute_flow_premium_ratio(calls, puts):
    """
    SpotGamma: compare total call premium traded vs put premium traded.
    This is the real-time money flow signal — shows where institutional money is moving.
    Call dominance (>60%) = bullish institutional flow.
    Put dominance (>60%) = defensive/bearish institutional flow.
    Returns (ratio 0-1, label) where >0.5 = call-dominant.
    """
    call_prem = sum(o.get("volume", 0) * o.get("mark_price", 0) * 100 for o in calls)
    put_prem  = sum(o.get("volume", 0) * o.get("mark_price", 0) * 100 for o in puts)
    total = call_prem + put_prem
    if total <= 0:
        return 0.5, "no_flow"
    ratio = call_prem / total
    if ratio > 0.65:
        label = "call_dominant"     # institutional call buying — bullish
    elif ratio > 0.55:
        label = "call_leaning"
    elif ratio < 0.35:
        label = "put_dominant"      # defensive hedging flow — bearish
    elif ratio < 0.45:
        label = "put_leaning"
    else:
        label = "balanced"
    log.info(f"Flow premium (SpotGamma): calls=${call_prem/1000:.0f}k puts=${put_prem/1000:.0f}k ratio={ratio:.2f} → {label}")
    return round(ratio, 3), label

def find_absolute_gamma_strike(calls, puts):
    """
    SpotGamma AGS (Absolute Gamma Strike): the strike with maximum TOTAL dealer
    gamma × OI summed across calls AND puts. Unlike GEX (which nets them),
    AGS finds the strongest hedging concentration — the true pinning magnet.
    SPY tends to gravitate toward AGS on expiry day via charm/delta hedging.
    """
    gamma_by_strike = {}
    for o in calls + puts:
        s = float(o["strike_price"])
        gamma_by_strike[s] = gamma_by_strike.get(s, 0) + o.get("gamma", 0) * o.get("open_interest", 0)
    if not gamma_by_strike:
        return None
    ags = max(gamma_by_strike, key=gamma_by_strike.get)
    log.info(f"Absolute Gamma Strike / Pin Target (SpotGamma AGS): ${ags:.0f}")
    return ags

def find_entry_contract(pool, target, max_spend, max_contracts):
    """
    Find best contract near target strike within budget.
    Three passes: ideal delta → relaxed delta → any affordable (for low-balance accounts).
    """
    candidates = sorted(pool, key=lambda o: float(o["strike_price"]))
    low_balance = max_spend < 50.0  # sub-$50 account: widen search aggressively

    # Pass 1: delta-filtered (DELTA_MIN–DELTA_MAX) near target
    for delta_offset in [0, -1, 1, -2, 2, -3, 3, -4, 4, -5, 5]:
        for o in candidates:
            if abs(float(o["strike_price"]) - (target + delta_offset)) < 0.5:
                d = o.get("delta", 0)
                if not (DELTA_MIN <= d <= DELTA_MAX):
                    continue
                cost = o["ask_price"] * 100
                if cost <= 0: continue
                qty = max(1, min(max_contracts, int(max_spend / cost)))
                if cost * qty <= max_spend:
                    o["_contracts"] = qty
                    log.info(f"Pass-1 contract: strike={o['strike_price']} delta={d:.2f} cost=${cost:.2f}")
                    return o

    # Pass 2: relax delta to 0.05–0.65 — any reasonably priced contract
    log.warning("Pass 1 failed — relaxing delta to 0.05-0.65")
    for o in candidates:
        d = o.get("delta", 0)
        if d > 0 and not (0.05 <= d <= 0.65):
            continue
        cost = o["ask_price"] * 100
        if cost <= 0: continue
        qty = max(1, min(max_contracts, int(max_spend / cost)))
        if cost * qty <= max_spend:
            o["_contracts"] = qty
            log.info(f"Pass-2 contract: strike={o['strike_price']} delta={d:.2f} cost=${cost:.2f}")
            return o

    # Pass 3: low-balance fallback — cheapest affordable contract, but NEVER below
    # the delta floor. A sub-0.10-delta 0DTE contract is a lottery ticket that
    # decays to zero on a normal day; taking no trade beats lighting money on fire.
    if low_balance:
        log.warning("Pass 2 failed — low-balance fallback: scanning full chain for cheapest affordable")
        affordable = [(o, o["ask_price"] * 100) for o in candidates
                      if 0 < o["ask_price"] * 100 <= max_spend
                      and o.get("delta", 0) >= DELTA_FLOOR]      # skip junk-delta tickets
        if affordable:
            # prefer the one closest to target strike that is affordable
            affordable.sort(key=lambda x: abs(float(x[0]["strike_price"]) - target))
            o, cost = affordable[0]
            o["_contracts"] = 1
            log.info(f"Pass-3 contract: strike={o['strike_price']} delta={o.get('delta',0):.2f} cost=${cost:.2f}")
            return o
        log.warning(f"Pass 3: no contract >= {DELTA_FLOOR} delta within ${max_spend:.0f} "
                    f"— skipping trade (near-ATM 0DTE unaffordable at this balance)")

    return None

def place_order(contract, expiration, opt_type, qty):
    symbol = contract["chain_symbol"]
    strike = float(contract["strike_price"])
    limit  = round(contract["ask_price"], 2)
    log.info(f"{'[DRY RUN] ' if DRY_RUN else ''}ORDER: BUY {qty}x {symbol} {strike}{opt_type[0].upper()} exp={expiration} limit=${limit:.2f} total=${limit*100*qty:.2f}")
    if DRY_RUN:
        log.info("DRY_RUN=true - not submitted")
        return {"dry_run": True}
    _assert_agentic(ACCOUNT_NUMBER)
    log.info(f"LIVE ORDER on agentic account {ACCOUNT_NUMBER}")
    order = rh.orders.order_buy_option_limit(
        positionEffect="open", creditOrDebit="debit", price=limit,
        symbol=symbol, quantity=qty, expirationDate=expiration,
        strike=str(int(strike)), optionType=opt_type, timeInForce="gfd",
        account_number=ACCOUNT_NUMBER,
    )
    log.info(f"Order: {json.dumps(order, indent=2)}")
    return order

def main():
    login()
    symbol     = "SPY"
    today_str  = date.today().strftime("%Y-%m-%d")
    expiration = today_str
    log.info(f"Trading date: {expiration}")

    log.info("=" * 60)
    log.info("GATE CHECK")
    log.info("=" * 60)

    # Gate 1 — Market holiday / FOMC / skip-date filter
    if today_str in NYSE_HOLIDAYS_2026:
        log.warning(f"GATE 1 BLOCKED: {today_str} is a NYSE holiday. No trading."); sys.exit(0)
    if today_str in SKIP_DATES:
        log.warning(f"GATE 1 BLOCKED: {today_str} in SKIP_DATES (FOMC/event)."); sys.exit(0)
    log.info("GATE 1 PASS: date not a holiday or skip date")

    # Gate 2 — Time-of-day window (9:45 AM–12:30 PM ET)
    window_block = check_time_window()
    if window_block and not DRY_RUN:
        log.warning(f"GATE 2 BLOCKED: {window_block}"); sys.exit(0)
    elif window_block:
        log.info(f"GATE 2 [DRY RUN OVERRIDE]: {window_block}")
    else:
        log.info(f"GATE 2 PASS: time window OK ({_et_now().strftime('%H:%M')} ET)")

    # Gate 2b — Already in a position today? Skip entry.
    if has_open_position():
        log.info("GATE 2b BLOCKED: open SPY 0DTE position already held — skip entry"); sys.exit(0)
    log.info("GATE 2b PASS: no open position")

    # Gate 3 — VIX filter + term structure (Whaley/Galai)
    vix  = get_vix()
    vix3m = get_vix3m()
    ts_slope, ts_label = compute_term_structure(vix, vix3m)
    if vix < VIX_MIN:
        log.warning(f"GATE 3 BLOCKED: VIX {vix:.2f} < {VIX_MIN} (premiums too thin)"); sys.exit(0)
    if vix > VIX_MAX:
        log.warning(f"GATE 3 BLOCKED: VIX {vix:.2f} > {VIX_MAX} (too chaotic)"); sys.exit(0)
    # Term structure gate: backwardation = market pricing imminent spike → FLAT
    if ts_label == "backwardation":
        log.warning(f"GATE 3b BLOCKED: VIX term structure in backwardation (slope={ts_slope:+.3f}) — no new entries"); sys.exit(0)
    log.info(f"GATE 3 PASS: VIX {vix:.2f} in range  |  term structure: {ts_label} (slope={ts_slope:+.3f})")
    write_gex_state({"vix": vix, "vix3m": vix3m, "ts_slope": ts_slope, "ts_label": ts_label,
                     "last_action": "Running GEX analysis..."})

    balance = get_account_balance()
    print_dashboard(balance)

    max_contracts, max_spend = get_smart_size(balance)
    if max_contracts == 0:
        log.warning("Smart sizer: losing streak protection active. Skipping today.")
        sys.exit(0)
    log.info(f"SmartSize: ${balance:.2f} -> {max_contracts} contracts, ${max_spend:.2f} max spend")

    spot, prev_close = get_spot_and_prev_close(symbol)
    calls, puts = get_atm_strikes(symbol, expiration, spot)
    if not calls and not puts:
        log.error("No options found. Aborting."); sys.exit(1)

    log.info("Fetching greeks (includes delta for contract selection)...")
    fetch_greeks(calls); fetch_greeks(puts)

    # Gate 4 — IVR + VRP filter (Whaley: IV overestimates RV 78-85% of the time)
    atm_opts = [o for o in calls + puts if abs(float(o["strike_price"]) - spot) < 2]
    atm_iv   = sum(o.get("implied_volatility", 0) for o in atm_opts) / len(atm_opts) if atm_opts else 0
    ivr      = compute_ivr(atm_iv)
    vrp_spread, vrp_label = compute_vrp(atm_iv, vix)
    log.info(f"ATM IV: {atm_iv*100:.1f}%  IVR: {ivr:.0f}  VRP: {vrp_label} ({vrp_spread*100:+.1f}pp)")
    if ivr > 50:
        log.warning(f"GATE 4 BLOCKED: IVR {ivr:.0f} > 50 — premium too expensive to buy"); sys.exit(0)
    if ivr > 30 or vrp_label == "rich":
        adj = 0.5 if vrp_label == "rich" else 0.6
        log.info(f"GATE 4 PARTIAL: IVR {ivr:.0f} / VRP={vrp_label} — reducing spend to {adj*100:.0f}%")
        max_spend = max(10.0, max_spend * adj)
    elif vrp_label == "cheap":
        log.info(f"GATE 4 BOOST: VRP cheap ({vrp_spread*100:+.1f}pp) — options fairly priced, normal size")
    else:
        log.info(f"GATE 4 PASS: IVR {ivr:.0f} / VRP {vrp_label}")

    nodes = compute_gex_nodes(calls, puts, spot)
    king_strike, king_gex = find_king(nodes)
    log.info(f"KING NODE: {king_strike}  GEX=${king_gex/1e6:.2f}M")
    log.info("Top nodes: " + "  ".join(f"{s}=${g/1e6:.1f}M" for s,g in sorted(nodes, key=lambda x: abs(x[1]), reverse=True)[:5]))

    # GEX pattern analysis + dealer flow (needed for direction + the overrides below)
    gex_features = extract_gex_features(calls, puts, spot, nodes, king_strike, king_gex)

    # ── Direction: pin-reversion model (regime from gamma flip, bias from spot
    # vs the pin) — NOT the raw king_gex sign, which only tells you the gamma
    # regime, not which way price is headed. ──
    _gflip = gex_features.get("gamma_flip", king_strike)
    _pin   = compute_max_pain(calls, puts) or king_strike
    direction = pin_reversion_direction(spot, _gflip, _pin, king_gex)
    if direction == "call":
        target, pool = king_strike + 1, calls
    else:
        target, pool = king_strike - 1, puts
    setup = "gex_signal"   # which procedure produced this entry (for journal + X tag)
    log.info(f"DIRECTION: {direction.upper()} -> target {target}  "
             f"(spot=${spot:.2f} flip=${_gflip:.0f} pin=${_pin:.0f} kingGEX=${king_gex/1e6:+.1f}M)")

    # ── Rip-fade override (the mirror of the dip-buy) ──
    # In a positive-gamma PIN regime, price that has tagged the CALL WALL while
    # sitting extended ABOVE max pain tends to revert DOWN toward the pin — a PUT
    # setup, even though it's above the flip. Tightly gated; every later gate
    # (OPEX, monthly wall, vanna, SPX confirm, confidence) still applies, so it
    # only fires when the rest of the procedure also supports a put.
    # Detect the fade setup independently of the initial direction — pin-reversion
    # may already have produced a PUT here, and Gate 5 needs the rip_fade flag to
    # allow a put that sits above prev close.
    rip_fade = False
    cw = gex_features.get("call_wall", 0)
    max_pain = _pin   # the revert magnet, computed above
    _vix_prev = get_vix_prev_close()
    _vix_calm = (_vix_prev is None) or (vix <= _vix_prev * 1.02)
    _concentration = gex_features.get("gex_concentration", 0) or 0
    if (cw and (cw - RIP_FADE_NEAR) <= spot <= (cw + RIP_FADE_MAX_BREAK)   # AT the call wall
            and max_pain and (spot - max_pain) >= RIP_FADE_MIN_ROOM        # extended above the pin
            and _vix_calm                                                  # vol flat/falling
            and king_gex >= 0                                              # positive gamma → pinning
            and _concentration >= 0.20):                                  # a GENUINE pin (skill PIN regime)
        direction, pool, target = "put", puts, int(round(spot)) - 1
        rip_fade = True
        setup = "rip_fade"
        log.info(f"RIP-FADE: SPY ${spot:.2f} AT call wall ${cw:.0f}, extended "
                 f"${spot-max_pain:.1f} above max pain ${max_pain:.0f}, VIX calm → fade DOWN "
                 f"with PUT (target {target})")

    # Gate 5 — Regime confirm (price vs prev close)
    if direction == "call" and spot < prev_close - 0.50:
        # Dip-buy override: on a positive-gamma (pinning) day a dip below prev
        # close is a BUY signal — dealers pin price back up toward VWAP/max-pain —
        # not a headwind. Only when support holds and vol isn't spiking. Gate 6
        # (spot must stay above the gamma flip) remains the hard floor beneath this.
        put_wall = gex_features.get("put_wall", 0)
        vix_prev = get_vix_prev_close()
        vix_calm = (vix_prev is None) or (vix <= vix_prev * 1.02)
        dip_ok = (
            king_gex >= 0                                      # positive dealer gamma → pinning
            and put_wall and spot > put_wall                   # holding above put-wall support
            and spot >= prev_close - prev_close * DIP_BUY_MAX_PCT  # shallow dip, not a breakdown
            and vix_calm                                       # vol flat/falling, not risk-off
        )
        if dip_ok:
            setup = "dip_buy"
            log.info(f"GATE 5 DIP-BUY OVERRIDE: SPY ${spot:.2f} below prev ${prev_close:.2f} but "
                     f"positive gamma + above put wall ${put_wall:.0f} + VIX calm "
                     f"({vix:.1f}<=~{(vix_prev or vix):.1f}) → buy the dip")
        else:
            log.warning(f"GATE 5 BLOCKED: GEX=CALL but SPY ${spot:.2f} bearish vs prev ${prev_close:.2f} "
                        f"(dip-buy override not met: gamma+={king_gex>=0}, "
                        f"above_putwall={bool(put_wall and spot>put_wall)}, vix_calm={vix_calm})")
            sys.exit(0)
    elif direction == "put" and spot > prev_close + 0.50:
        if rip_fade:
            log.info(f"GATE 5 RIP-FADE OVERRIDE: PUT above prev close ${prev_close:.2f} is intentional "
                     f"(fading the call wall back to max pain)")
        else:
            log.warning(f"GATE 5 BLOCKED: GEX=PUT but SPY ${spot:.2f} bullish vs prev ${prev_close:.2f}"); sys.exit(0)
    else:
        log.info(f"GATE 5 PASS: regime confirms {direction.upper()} (spot=${spot:.2f} prev=${prev_close:.2f})")

    # Gate 6 — Gamma flip hard boundary (SpotGamma rule: never trade against the flip)
    gamma_flip = gex_features.get("gamma_flip", king_strike)
    if direction == "call" and spot < gamma_flip - 1.0:
        log.warning(f"GATE 6 BLOCKED: SPY ${spot:.2f} below gamma flip ${gamma_flip:.0f} — no calls")
        sys.exit(0)
    if direction == "put" and spot > gamma_flip + 1.0:
        if rip_fade:
            log.info(f"GATE 6 RIP-FADE OVERRIDE: PUT above the flip ${gamma_flip:.0f} is intentional "
                     f"(pin-range mean-reversion fade of the call wall)")
        else:
            log.warning(f"GATE 6 BLOCKED: SPY ${spot:.2f} above gamma flip ${gamma_flip:.0f} — no puts")
            sys.exit(0)
    else:
        log.info(f"GATE 6 PASS: gamma flip ${gamma_flip:.0f} supports {direction.upper()}")

    # Gate 6b — OPEX gravity (Galai: OI chain IS the market forecast)
    opex_ok, opex_adj, opex_note = opex_gravity_check(spot, direction)
    if not opex_ok:
        log.warning(f"GATE 6b BLOCKED: {opex_note}"); sys.exit(0)
    log.info(f"GATE 6b PASS: {opex_note}")
    max_spend = max(10.0, max_spend * opex_adj)

    # Gate 6c — Term structure size boost/neutral (Whaley: contango = premium seller advantage)
    if ts_label == "deep_contango":
        ts_mult = 1.20
        log.info(f"GATE 6c: deep contango → size boost ×{ts_mult}")
    elif ts_label == "contango":
        ts_mult = 1.00
        log.info(f"GATE 6c: contango → neutral size")
    else:
        ts_mult = 1.00  # backwardation already blocked at Gate 3b
    max_spend = min(max_spend * ts_mult, balance * 0.90)

    # Gate 6d — Monthly OPEX rubber band cycle (Galai: put wall = structural floor → new highs)
    opex_cycle = compute_opex_cycle(spot)
    log.info(f"GATE 6d OPEX CYCLE: Phase {opex_cycle['opex_phase']} {opex_cycle['opex_phase_label']} — {opex_cycle['opex_cycle_note']}")
    if opex_cycle["opex_put_block"] and direction == "put":
        log.warning(f"GATE 6d BLOCKED: Monthly put wall ${MONTHLY_PUT_WALL:.0f} active — puts blocked, rubber band favors calls")
        sys.exit(0)
    if direction == "call" and opex_cycle["opex_call_boost"] > 0:
        pre = max_spend
        max_spend = min(max_spend * opex_cycle["opex_call_boost"], balance * 0.90)
        log.info(f"GATE 6d BOOST: OPEX cycle Phase {opex_cycle['opex_phase']} → call boost ×{opex_cycle['opex_call_boost']} (${pre:.2f} → ${max_spend:.2f})")
    elif opex_cycle["opex_phase"] == 1:
        log.warning("GATE 6d CAUTION: SPY below monthly put wall — size cut 50%")
        max_spend = max(5.0, max_spend * 0.50)

    # Gate 6e — Vanna flow (Cem Karsan / Kai Volatility)
    # VIX dropping intraday → dealers mechanically buy SPY → tailwind for calls.
    # VIX rising intraday → dealers mechanically sell SPY → headwind for calls.
    vix_prev = get_vix_prev_close()
    vanna_mult, vanna_label = compute_vanna_flow(vix, vix_prev)
    if vanna_label == "vanna_sell" and direction == "call":
        log.warning(f"GATE 6e BLOCKED: VIX spike ({vanna_label}) → vanna sell pressure — no calls"); sys.exit(0)
    if vanna_mult != 1.0:
        pre = max_spend
        max_spend = min(max_spend * vanna_mult, balance * 0.90)
        log.info(f"GATE 6e: vanna {vanna_label} ×{vanna_mult} → spend ${pre:.2f}→${max_spend:.2f}")
    else:
        log.info(f"GATE 6e PASS: vanna neutral")

    # Gate 6f — Implied skew signal (Karsan: put IV premium = downside fear pricing)
    skew_mult, skew_label, skew_ratio = compute_skew_signal(calls, puts, spot)
    if skew_label == "extreme_put_skew" and direction == "call":
        log.warning(f"GATE 6f BLOCKED: extreme put skew ({skew_ratio:.2f}) — crash fear priced in, no calls"); sys.exit(0)
    if skew_mult != 1.0:
        pre = max_spend
        max_spend = min(max_spend * skew_mult, balance * 0.90)
        log.info(f"GATE 6f: skew {skew_label} (ratio={skew_ratio:.2f}) ×{skew_mult} → spend ${pre:.2f}→${max_spend:.2f}")
    else:
        log.info(f"GATE 6f PASS: skew neutral (ratio={skew_ratio:.2f})")

    # Gate 6g — Flow premium ratio (SpotGamma: institutional money direction)
    flow_ratio, flow_label = compute_flow_premium_ratio(calls, puts)
    if flow_label == "put_dominant" and direction == "call":
        log.warning(f"GATE 6g CAUTION: put-dominant flow ({flow_ratio:.2f}) vs call signal — reducing size 20%")
        max_spend = max(5.0, max_spend * 0.80)
    elif flow_label in ("call_dominant", "call_leaning") and direction == "call":
        pre = max_spend
        max_spend = min(max_spend * 1.10, balance * 0.90)
        log.info(f"GATE 6g BOOST: {flow_label} flow confirms call direction → spend ${pre:.2f}→${max_spend:.2f}")
    else:
        log.info(f"GATE 6g: flow {flow_label} ({flow_ratio:.2f}) — no adjustment")

    # Compute Absolute Gamma Strike (SpotGamma AGS) — true pin/magnet level
    ags = find_absolute_gamma_strike(calls, puts)

    write_gex_state({
        "spot": spot, "prev_close": prev_close, "ivr": ivr,
        "vrp_label": vrp_label, "vrp_spread": vrp_spread,
        "ts_slope": ts_slope, "ts_label": ts_label,
        "king_strike": king_strike, "king_gex_m": round(king_gex / 1e6, 2),
        "direction": direction, "gamma_flip": gamma_flip,
        "call_wall": gex_features.get("call_wall"),
        "put_wall":  gex_features.get("put_wall"),
        "ags": ags,
        "vanna_label": vanna_label, "vanna_mult": vanna_mult,
        "skew_label": skew_label, "skew_ratio": skew_ratio,
        "flow_label": flow_label, "flow_ratio": flow_ratio,
        "gex_features": gex_features,
        **opex_cycle,
    })

    confidence, reasons = gex_confidence_score(gex_features, direction)
    print_dealer_flow(gex_features, spot, vix, direction, confidence, reasons)
    print_pattern_report()

    # Gate 6b — SPX "real-money" confirmation. SPX/SPXW carry the institutional
    # dealer gamma that actually drives the tape; require it not to contradict us.
    spx_state = {}
    try:
        from scripts.spx_signal import confirmation as spx_confirmation
        spxc = spx_confirmation(direction, spy_spot=spot)
        if spxc:
            confidence = max(0.0, min(1.0, confidence * spxc["mult"]))
            reasons.append(spxc["reason"])
            log.info(f"SPX CONFIRM: {spxc['label']} (x{spxc['mult']}) → conf now {confidence*100:.0f}%")
            spx_state = {
                "spx_spot": spxc["spx_spot"], "spx_direction": spxc["direction"],
                "spx_regime": spxc["regime"], "spx_confirm": spxc["label"],
                "spx_king_spy": spxc.get("levels_spy", {}).get("king"),
            }
            if spxc["label"] == "DIVERGENT" and confidence < 0.45:
                log.warning(f"GATE 6b BLOCKED: SPX diverges & conf {confidence*100:.0f}% < 45%"); sys.exit(0)
    except SystemExit:
        raise
    except Exception as e:
        log.info(f"SPX confirmation unavailable (non-fatal): {e}")

    if confidence < 0.35:
        log.warning(f"GATE 7 BLOCKED: GEX confidence {confidence*100:.0f}% < 35%"); sys.exit(0)
    log.info(f"GATE 7 PASS: GEX confidence {confidence*100:.0f}%")

    # Gate 7b — Brenner jump premium decay: scale size by time-of-day curve
    jump_mult = jump_premium_time_multiplier()
    max_spend = max(5.0, round(max_spend * confidence * jump_mult, 2))
    log.info(f"Final spend: ${max_spend:.2f} (conf={confidence*100:.0f}% jump_mult={jump_mult:.2f} opex_adj={opex_adj:.2f} ts_mult={ts_mult:.2f} vanna={vanna_mult:.2f} skew={skew_mult:.2f})")

    contract = find_entry_contract(pool, target, max_spend, max_contracts)
    if not contract:
        log.warning(f"No suitable {direction} contract near {target} within budget. Skipping."); sys.exit(0)

    qty   = contract["_contracts"]
    ask   = contract["ask_price"]
    delta = contract.get("delta", 0)
    total = ask * 100 * qty

    setup_label = {"dip_buy": "DIP-BUY (put-wall bounce)",
                   "rip_fade": "RIP-FADE (call-wall fade)",
                   "gex_signal": "GEX signal"}.get(setup, setup)

    place_order(contract, expiration, direction, qty)
    write_gex_state({
        "last_action": f"{'[DRY RUN] ' if DRY_RUN else ''}ORDER PLACED [{setup_label}]: BUY {qty}x SPY {float(contract['strike_price'])}{direction[0].upper()} @ ${ask:.2f}",
        "confidence": confidence,
        "entry_strike": float(contract["strike_price"]),
        "entry_price": ask, "entry_qty": qty,
        "entry_setup": setup,
        "entry_spot": spot, "entry_delta": abs(delta) or None, "entry_opt_type": direction,
        **spx_state,
    })

    if not DRY_RUN:
        log_entry(
            symbol=symbol, opt_type=direction,
            strike=float(contract["strike_price"]), expiry=expiration,
            contracts=qty, entry_price=ask,
            vix=vix, king_strike=king_strike, king_gex=king_gex,
            direction=direction, balance_before=balance,
            gex_features=gex_features, confidence=confidence, setup=setup,
        )
        try:
            from scripts.social import tweet_entry
            tweet_entry(direction=direction, strike=float(contract["strike_price"]),
                        price=ask, king_strike=king_strike, gamma_flip=gamma_flip,
                        confidence=confidence, spot=spot, setup=setup)
        except Exception as te:
            log.warning(f"Entry tweet failed (non-fatal): {te}")

    print("\n" + "="*60)
    print(f"HEATSEEKER MORNING SIGNAL - {expiration}")
    print("="*60)
    print(f"  Account:    {ACCOUNT_NUMBER} (agentic)")
    print(f"  SPY spot:   ${spot:.2f}  (prev ${prev_close:.2f})")
    print(f"  VIX:        {vix:.2f}  VIX3M: {vix3m or 'n/a'}  IVR: {ivr:.0f}")
    print(f"  Term Struct:{ts_label} (slope={ts_slope:+.3f})")
    print(f"  VRP:        {vrp_label} ({vrp_spread*100:+.1f}pp IV vs realized)")
    print(f"  Jump mult:  {jump_mult:.2f}  OPEX adj: {opex_adj:.2f}  TS mult: {ts_mult:.2f}")
    print(f"  King node:  {king_strike}  (GEX ${king_gex/1e6:.2f}M)")
    print(f"  Gamma flip: ${gamma_flip:.0f}  (SpotGamma HVL)")
    print(f"  AGS/Pin:    ${ags:.0f}  (SpotGamma — today's magnet)")
    print(f"  Call wall:  ${gex_features.get('call_wall', 'n/a')}")
    print(f"  Put wall:   ${gex_features.get('put_wall', 'n/a')}")
    print(f"  Vanna:      {vanna_label}  ×{vanna_mult}  (Karsan — VIX flow)")
    print(f"  Skew:       {skew_label}  ratio={skew_ratio:.2f}  (Karsan — put premium)")
    print(f"  Flow:       {flow_label}  ratio={flow_ratio:.2f}  (SpotGamma — $ direction)")
    print(f"  Direction:  {direction.upper()}")
    print(f"  Trade:      BUY {qty}x {symbol} {float(contract['strike_price'])}{direction[0].upper()} @ ${ask:.2f}  (delta={delta:.2f})")
    print(f"  Total cost: ${total:.2f}")
    print(f"  Stop loss:  ${total*0.5:.2f}  (-50%)")
    print(f"  Trail stop: activates at ${total*2:.2f}  (+100%), trails 25% from peak")
    print(f"  GEX Conf:   {confidence*100:.0f}%")
    print(f"  Status:     {'DRY RUN' if DRY_RUN else 'ORDER PLACED'}")
    print("="*60)

def analyze_gex_only():
    """
    Run full GEX analysis and write to dashboard without placing any trade.
    Called every 15 min during market hours so the dashboard always shows live GEX data.
    """
    try:
        login()
    except SystemExit:
        return

    today_str = date.today().strftime("%Y-%m-%d")
    if today_str in NYSE_HOLIDAYS_2026 or today_str in SKIP_DATES:
        write_gex_state({"last_action": f"Market closed ({today_str})"})
        return

    now = _et_now()
    t   = now.hour * 60 + now.minute
    if now.weekday() >= 5 or t < 570 or t > 960:
        return  # outside market hours

    try:
        vix              = get_vix()
        vix3m            = get_vix3m()
        ts_slope, ts_label = compute_term_structure(vix, vix3m)
        balance          = get_account_balance()
        spot, prev_close = get_spot_and_prev_close("SPY")
        expiration      = today_str

        calls, puts = get_atm_strikes("SPY", expiration, spot)
        if not calls and not puts:
            write_gex_state({"spot": spot, "vix": vix, "last_action": "No options chain available yet"})
            return

        fetch_greeks(calls)
        fetch_greeks(puts)

        atm_opts = [o for o in calls + puts if abs(float(o["strike_price"]) - spot) < 2]
        atm_iv   = sum(o.get("implied_volatility", 0) for o in atm_opts) / len(atm_opts) if atm_opts else 0
        ivr      = compute_ivr(atm_iv)
        vrp_spread, vrp_label = compute_vrp(atm_iv, vix)
        jump_mult = jump_premium_time_multiplier()
        _, _, opex_note = opex_gravity_check(spot, "call")  # informational only in GEX scan
        opex_cycle = compute_opex_cycle(spot)

        nodes                 = compute_gex_nodes(calls, puts, spot)
        king_strike, king_gex = find_king(nodes)

        if not king_strike:
            write_gex_state({"spot": spot, "vix": vix, "last_action": "GEX analysis: insufficient data"})
            return

        direction = "call" if king_gex >= 0 else "put"
        gex_features          = extract_gex_features(calls, puts, spot, nodes, king_strike, king_gex)
        gamma_flip            = gex_features.get("gamma_flip", king_strike)
        confidence, reasons   = gex_confidence_score(gex_features, direction)

        # SPX real-money confirmation (same layer the entry path applies).
        spx_fields = {}
        try:
            from scripts.spx_signal import confirmation as spx_confirmation
            spxc = spx_confirmation(direction, spy_spot=spot)
            if spxc:
                confidence = max(0.0, min(1.0, confidence * spxc["mult"]))
                reasons.append(spxc["reason"])
                lv = spxc.get("levels_spy", {})
                spx_fields = {
                    "spx_spot": spxc["spx_spot"], "spx_ratio": spxc["ratio"],
                    "spx_direction": spxc["direction"], "spx_regime": spxc["regime"],
                    "spx_net_gex": spxc["net_gex"], "spx_confirm": spxc["label"],
                    "spx_expiry": spxc.get("expiry"),
                    "spx_king_spy": lv.get("king"), "spx_flip_spy": lv.get("gamma_flip"),
                    "spx_call_wall_spy": lv.get("call_wall"), "spx_put_wall_spy": lv.get("put_wall"),
                    "spx_max_pain_spy": lv.get("max_pain"),
                }
        except Exception as e:
            log.info(f"SPX confirmation unavailable in scan (non-fatal): {e}")

        # Expert signal layers (informational in GEX scan — no gating, just reporting)
        vix_prev                       = get_vix_prev_close()
        vanna_mult, vanna_label        = compute_vanna_flow(vix, vix_prev)
        skew_mult, skew_label, skew_ratio = compute_skew_signal(calls, puts, spot)
        flow_ratio, flow_label         = compute_flow_premium_ratio(calls, puts)
        ags                            = find_absolute_gamma_strike(calls, puts)

        # Determine regime alignment
        regime_ok = not (
            (direction == "call" and spot < prev_close - 0.50) or
            (direction == "put"  and spot > prev_close + 0.50)
        )

        top_nodes_str = "  ".join(f"${s:.0f}={v/1e6:+.1f}M"
                                  for s, v in sorted(nodes, key=lambda x: abs(x[1]), reverse=True)[:5])

        # Build full strike ladder for dashboard GEX table
        call_by_strike = {float(o["strike_price"]): o for o in calls}
        put_by_strike  = {float(o["strike_price"]): o for o in puts}
        all_strikes = sorted(set(call_by_strike.keys()) | set(put_by_strike.keys()))
        ladder = []
        for s in all_strikes:
            c = call_by_strike.get(s, {})
            p = put_by_strike.get(s, {})
            c_gex  = net_gex(c.get("open_interest", 0), c.get("gamma", 0), spot, True)
            p_gex  = net_gex(p.get("open_interest", 0), p.get("gamma", 0), spot, False)
            c_vol  = c.get("volume", 0)
            p_vol  = p.get("volume", 0)
            c_mark = c.get("mark_price", 0)
            p_mark = p.get("mark_price", 0)
            ladder.append({
                "strike":      s,
                "call_oi":     c.get("open_interest", 0),
                "put_oi":      p.get("open_interest", 0),
                "call_vol":    c_vol,
                "put_vol":     p_vol,
                "call_gex_m":  round(c_gex / 1e6, 3),
                "put_gex_m":   round(p_gex / 1e6, 3),
                "net_gex_m":   round((c_gex + p_gex) / 1e6, 3),
                "call_prem_k": round(c_vol * c_mark * 100 / 1000, 1),
                "put_prem_k":  round(p_vol * p_mark * 100 / 1000, 1),
            })

        # Build flow feed — biggest premium prints across all strikes
        flow_prints = []
        for row in ladder:
            if row["call_vol"] > 0:
                flow_prints.append({
                    "strike": row["strike"], "type": "CALL",
                    "vol": row["call_vol"], "prem_k": row["call_prem_k"],
                })
            if row["put_vol"] > 0:
                flow_prints.append({
                    "strike": row["strike"], "type": "PUT",
                    "vol": row["put_vol"], "prem_k": row["put_prem_k"],
                })
        flow_feed = sorted(flow_prints, key=lambda x: x["prem_k"], reverse=True)[:10]

        call_vol_total = sum(row["call_vol"] for row in ladder)
        put_vol_total  = sum(row["put_vol"]  for row in ladder)
        total_vol      = call_vol_total + put_vol_total
        flow_ratio     = round(call_vol_total / total_vol, 3) if total_vol else 0.5

        # Max pain: strike that minimizes total intrinsic payout to option holders
        # (= maximizes dealer/writer benefit). Standard cash-settled formulation.
        max_pain = None
        if all_strikes:
            best_pain = None
            for k in all_strikes:
                call_pay = sum(call_by_strike.get(s, {}).get("open_interest", 0) * max(0.0, k - s)
                               for s in all_strikes)
                put_pay  = sum(put_by_strike.get(s, {}).get("open_interest", 0) * max(0.0, s - k)
                               for s in all_strikes)
                total_pain = call_pay + put_pay
                if best_pain is None or total_pain < best_pain:
                    best_pain, max_pain = total_pain, k

        write_gex_state({
            "spot":        spot,
            "prev_close":  prev_close,
            "vix":         vix,
            "vix3m":       vix3m,
            "ts_slope":    ts_slope,
            "ts_label":    ts_label,
            "ivr":         ivr,
            "vrp_label":   vrp_label,
            "vrp_spread":  vrp_spread,
            "jump_mult":   jump_mult,
            "opex_note":   opex_note,
            "king_strike": king_strike,
            "king_gex_m":  round(king_gex / 1e6, 2),
            "direction":   direction,
            "confidence":  round(confidence, 3),
            "confidence_reasons": reasons,
            **spx_fields,
            "net_gex_total": round(sum(row["net_gex_m"] for row in ladder), 2),
            "gamma_flip":  gamma_flip,
            "max_pain":    max_pain,
            "call_wall":   gex_features.get("call_wall"),
            "put_wall":    gex_features.get("put_wall"),
            "gex_features": gex_features,
            "regime_ok":   regime_ok,
            "top_nodes":   top_nodes_str,
            "strike_ladder":   ladder,
            "flow_feed":       flow_feed,
            "call_vol_total":  call_vol_total,
            "put_vol_total":   put_vol_total,
            "flow_ratio":      flow_ratio,
            **opex_cycle,
            "last_action": f"GEX scan {now.strftime('%H:%M')} ET — "
                           f"{'✅' if regime_ok else '⚠️'} {direction.upper()} "
                           f"conf={confidence*100:.0f}% king=${king_strike:.0f} flip=${gamma_flip:.0f}",
        })
        log.info(f"GEX analysis written: {direction.upper()} conf={confidence*100:.0f}% king=${king_strike:.0f}")

    except Exception as e:
        log.warning(f"GEX analysis error: {e}")


if __name__ == "__main__":
    main()
