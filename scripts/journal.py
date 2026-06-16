#!/usr/bin/env python3
"""
Trade journal — persists every entry/exit to data/trades.json,
pushes to GitHub so history survives Railway restarts, and computes
Kelly-inspired smart sizing toward the $10k goal.
"""
import json, os, subprocess, logging
from datetime import date, datetime

log = logging.getLogger("heatseeker.journal")

GOAL       = 10_000.0
TRAIL_DROP = 0.25

_ROOT       = os.path.join(os.path.dirname(__file__), "..")
JOURNAL_FILE = os.path.join(_ROOT, "data", "trades.json")


# ── Persistence ──────────────────────────────────────────────────────────────

def load():
    try:
        os.makedirs(os.path.dirname(JOURNAL_FILE), exist_ok=True)
        with open(JOURNAL_FILE) as f:
            return json.load(f)
    except Exception:
        return {"trades": []}

def save(data):
    os.makedirs(os.path.dirname(JOURNAL_FILE), exist_ok=True)
    with open(JOURNAL_FILE, "w") as f:
        json.dump(data, f, indent=2)
    _git_push()

def _git_push():
    pat = os.getenv("GITHUB_PAT", "")
    if not pat:
        log.debug("No GITHUB_PAT set — journal not pushed to GitHub")
        return
    try:
        subprocess.run(["git", "-C", _ROOT, "add", "data/trades.json"],
                       check=True, capture_output=True)
        result = subprocess.run(
            ["git", "-C", _ROOT, "commit", "-m",
             f"[bot] trade journal {date.today()}"],
            capture_output=True
        )
        if result.returncode == 0:
            subprocess.run(
                ["git", "-C", _ROOT, "-c",
                 f"url.https://{pat}@github.com/.insteadOf=https://github.com/",
                 "push", "origin", "main"],
                capture_output=True, timeout=30
            )
            log.info("Journal pushed to GitHub")
    except Exception as e:
        log.warning(f"Journal git push failed (non-fatal): {e}")


# ── Log helpers ───────────────────────────────────────────────────────────────

def log_entry(symbol, opt_type, strike, expiry, contracts, entry_price,
              vix, king_strike, king_gex, direction, balance_before):
    data = load()
    trade = {
        "id":             f"{date.today()}_{strike}_{opt_type}",
        "date":           str(date.today()),
        "ts_entry":       datetime.utcnow().isoformat(),
        "symbol":         symbol,
        "opt_type":       opt_type,
        "strike":         strike,
        "expiry":         expiry,
        "contracts":      contracts,
        "entry_price":    round(entry_price, 4),
        "exit_price":     None,
        "exit_reason":    None,
        "pnl_pct":        None,
        "pnl_usd":        None,
        "vix":            round(vix, 2),
        "king_strike":    king_strike,
        "king_gex_m":     round(king_gex / 1e6, 2),
        "direction":      direction,
        "balance_before": round(balance_before, 2),
        "balance_after":  None,
    }
    data["trades"].append(trade)
    save(data)
    log.info(f"Journal: entry logged  id={trade['id']}")
    return trade["id"]

def log_exit(trade_id, exit_price, exit_reason, balance_after):
    data = load()
    for t in data["trades"]:
        if t["id"] == trade_id:
            ep  = t["entry_price"]
            t["exit_price"]   = round(exit_price, 4)
            t["exit_reason"]  = exit_reason
            t["pnl_pct"]      = round((exit_price - ep) / ep * 100, 2) if ep else 0
            t["pnl_usd"]      = round((exit_price - ep) * t["contracts"] * 100, 2)
            t["balance_after"] = round(balance_after, 2)
            t["ts_exit"]      = datetime.utcnow().isoformat()
            save(data)
            log.info(f"Journal: exit logged  id={trade_id}  P&L={t['pnl_pct']:+.1f}%  ${t['pnl_usd']:+.2f}")
            return t
    log.warning(f"Journal: trade_id {trade_id} not found for exit")


# ── Analytics ─────────────────────────────────────────────────────────────────

def get_stats(n=20):
    """Return performance stats over the last n closed trades."""
    data  = load()
    closed = [t for t in data["trades"] if t.get("pnl_pct") is not None]
    recent = closed[-n:]

    if not recent:
        return {
            "total_trades": 0, "win_rate": 0.5, "avg_win_pct": 100.0,
            "avg_loss_pct": 50.0, "kelly": 0.25, "streak": 0,
            "streak_type": None, "total_pnl_usd": 0, "best_trade": None,
            "worst_trade": None, "goal_progress_pct": 0,
        }

    wins   = [t for t in recent if t["pnl_pct"] > 0]
    losses = [t for t in recent if t["pnl_pct"] <= 0]

    win_rate  = len(wins) / len(recent)
    avg_win   = sum(t["pnl_pct"] for t in wins)   / len(wins)   if wins   else 100.0
    avg_loss  = abs(sum(t["pnl_pct"] for t in losses) / len(losses)) if losses else 50.0
    R         = avg_win / avg_loss if avg_loss else 2.0
    kelly     = max(0.0, min(0.5, win_rate - (1 - win_rate) / R))

    # Streak
    streak, streak_type = 0, None
    for t in reversed(recent):
        w = t["pnl_pct"] > 0
        if streak_type is None:
            streak_type = "win" if w else "loss"
            streak = 1
        elif (streak_type == "win") == w:
            streak += 1
        else:
            break

    bal_last = closed[-1].get("balance_after") if closed else 50.0
    total_pnl = sum(t["pnl_usd"] for t in closed if t["pnl_usd"])

    return {
        "total_trades":     len(closed),
        "win_rate":         round(win_rate, 3),
        "avg_win_pct":      round(avg_win, 1),
        "avg_loss_pct":     round(avg_loss, 1),
        "kelly":            round(kelly, 3),
        "streak":           streak,
        "streak_type":      streak_type,
        "total_pnl_usd":    round(total_pnl, 2),
        "best_trade":       max(recent, key=lambda t: t["pnl_pct"] or 0),
        "worst_trade":      min(recent, key=lambda t: t["pnl_pct"] or 0),
        "goal_progress_pct": round((bal_last or 50) / GOAL * 100, 2) if bal_last else 0,
    }


# ── Smart Sizing ──────────────────────────────────────────────────────────────

def get_smart_size(balance):
    """
    Returns (max_contracts, max_spend) using Kelly criterion adjusted for
    win/loss history, losing streaks, and compounding toward $10k goal.
    """
    stats = get_stats()
    kelly = stats["kelly"]
    streak        = stats["streak"]
    streak_type   = stats["streak_type"]
    total_trades  = stats["total_trades"]

    # Not enough history — use conservative defaults
    if total_trades < 5:
        if balance < 150:   return 1, min(balance * 0.9, 50)
        elif balance < 500: return 2, min(balance * 0.20, 100)
        else:               return 3, min(balance * 0.15, 200)

    # Losing streak protection
    if streak_type == "loss":
        if streak >= 5:
            log.warning(f"5-trade losing streak — sitting out this signal")
            return 0, 0   # skip the trade entirely
        if streak >= 3:
            kelly *= 0.40  # cut size to 40% of Kelly
            log.info(f"3-trade losing streak — reducing size to 40% Kelly")
        elif streak == 2:
            kelly *= 0.65
    elif streak_type == "win" and streak >= 3:
        # On a hot streak, press slightly but cap aggression
        kelly = min(kelly * 1.20, 0.45)

    # Progress boost: as we approach $10k, tighten sizing to protect gains
    progress = balance / GOAL
    if progress > 0.75:      kelly *= 0.70   # >$7500 — protect the gain
    elif progress > 0.50:    kelly *= 0.85   # >$5000 — slightly conservative
    elif progress > 0.25:    kelly *= 1.00   # $2500+ — full Kelly
    else:                    kelly *= 1.10   # <$2500 — slight press to compound faster

    kelly = max(0.05, min(kelly, 0.45))     # hard floor/ceiling

    max_spend    = round(balance * kelly, 2)
    max_spend    = max(10.0, min(max_spend, balance * 0.90))
    max_contracts = max(1, int(max_spend / 50))

    log.info(
        f"SmartSize: balance=${balance:.2f}  kelly={kelly:.2%}  "
        f"spend=${max_spend:.2f}  contracts={max_contracts}  "
        f"streak={streak_type}x{streak}  wr={stats['win_rate']:.0%}  "
        f"goal={stats['goal_progress_pct']:.1f}%"
    )
    return max_contracts, max_spend


def print_dashboard(balance):
    stats = get_stats()
    print("\n" + "="*60)
    print(f"  HEATSEEKER PERFORMANCE DASHBOARD")
    print("="*60)
    print(f"  Goal:         $10,000.00")
    print(f"  Balance:      ${balance:.2f}  ({stats['goal_progress_pct']:.1f}% of goal)")
    print(f"  Total Trades: {stats['total_trades']}")
    print(f"  Win Rate:     {stats['win_rate']*100:.0f}%")
    print(f"  Avg Win:      +{stats['avg_win_pct']:.0f}%")
    print(f"  Avg Loss:     -{stats['avg_loss_pct']:.0f}%")
    print(f"  Kelly:        {stats['kelly']*100:.0f}%")
    print(f"  Total P&L:    ${stats['total_pnl_usd']:+.2f}")
    if stats["streak_type"]:
        print(f"  Streak:       {stats['streak']}x {stats['streak_type'].upper()}")
    if stats["best_trade"]:
        b = stats["best_trade"]
        print(f"  Best Trade:   {b['date']} {b['opt_type'].upper()} +{b['pnl_pct']:.0f}% (${b['pnl_usd']:+.2f})")
    if stats["worst_trade"]:
        w = stats["worst_trade"]
        print(f"  Worst Trade:  {w['date']} {w['opt_type'].upper()} {w['pnl_pct']:.0f}% (${w['pnl_usd']:+.2f})")
    print("="*60)
