#!/usr/bin/env python3
"""
Standalone Gamma Exposure (GEX) calculator on the Massive REST API.

Implements the classic dollar-gamma GEX convention:

    Contract GEX = gamma * open_interest * shares_per_contract * spot**2 / 100
    Net GEX(strike) = Call GEX - Put GEX     (calls +, puts -)

Market makers are assumed net short options: call gamma adds positive dealer
gamma (they buy into strength / sell into weakness -> price is pinned), put
gamma is negative. Where cumulative net GEX crosses zero is the gamma flip:
above it dealers dampen moves (mean-revert), below it they amplify (trend).

This is a self-contained analysis/plotting tool. The production dashboard GEX
lives in scripts/massive.py (analytics()); this script reuses that module only
for the authenticated, paginated chain fetch.

Usage:
    export MASSIVE_API_KEY=...
    python -m scripts.gex_massive SPY                     # nearest 0DTE+ expiry
    python -m scripts.gex_massive QQQ --expiration 2026-07-18
    python -m scripts.gex_massive SPY --all-expiries --term-weighted
    python -m scripts.gex_massive AAPL --chart /tmp/gex_aapl.png
"""
import os
import sys
import argparse
from datetime import datetime
from zoneinfo import ZoneInfo

from scripts import massive  # reuse auth + paginated chain_snapshot

ET = ZoneInfo("America/New_York")
SHARES_PER_CONTRACT_DEFAULT = 100


def _num(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def fetch_chain(underlying, expiration=None):
    """Authenticated, fully-paginated option chain via Massive (limit=250)."""
    if not massive.enabled():
        raise RuntimeError("MASSIVE_API_KEY not set")
    return massive.chain_snapshot(
        underlying,
        expiration=expiration,
        expiration_gte=None if expiration else datetime.now(ET).strftime("%Y-%m-%d"),
        limit=250,
        max_pages=20,
    )


def _dte(expiration):
    try:
        exp = datetime.strptime(expiration, "%Y-%m-%d").replace(tzinfo=ET)
        return max((exp - datetime.now(ET)).total_seconds() / 86400.0, 0.0)
    except Exception:
        return None


def compute_gex(results, only_expiration=None, term_weighted=False):
    """Return (spot, rows, net_by_strike, gamma_flip, totals).

    rows: per-contract dicts with signed gex. net_by_strike: {strike: {call,put,net}}.
    Filters out contracts missing gamma or open_interest, per spec.
    """
    spot = None
    for r in results:
        p = massive._f(r, "underlying_asset", "price") or \
            massive._f(r, "underlying_asset", "last_price")
        if p:
            spot = float(p)
            break
    if not spot:
        raise RuntimeError("no underlying price in snapshot")

    rows = []
    net_by_strike = {}
    call_total = put_total = 0.0
    for r in results:
        typ, k, exp = massive._contract(r)
        if typ is None or k is None:
            continue
        if only_expiration and exp != only_expiration:
            continue
        gamma = _num(massive._f(r, "greeks", "gamma"))
        oi = _num(massive._f(r, "open_interest"))
        if gamma is None or oi is None or oi <= 0:   # spec: drop missing gamma/OI
            continue
        spc = _num(massive._f(r, "details", "shares_per_contract")) or SHARES_PER_CONTRACT_DEFAULT
        is_call = str(typ).lower().startswith("c")

        gex = gamma * oi * spc * (spot ** 2) / 100.0        # dollar gamma
        if term_weighted:
            dte = _dte(exp)
            if dte is not None:
                gex *= 1.0 / (1.0 + dte)                     # discount far expiries
        signed = gex if is_call else -gex

        strike = float(k)
        b = net_by_strike.setdefault(strike, {"call": 0.0, "put": 0.0, "net": 0.0})
        if is_call:
            b["call"] += gex
            call_total += gex
        else:
            b["put"] += gex
            put_total += gex
        b["net"] += signed
        rows.append({"strike": strike, "type": "C" if is_call else "P",
                     "expiration": exp, "gamma": gamma, "oi": oi, "gex": signed})

    if not net_by_strike:
        raise RuntimeError("no contracts with usable gamma + open interest")

    # Gamma flip: strike where cumulative net GEX crosses zero.
    strikes = sorted(net_by_strike)
    cum = 0.0
    prev_cum = None
    prev_strike = None
    gamma_flip = None
    for s in strikes:
        cum += net_by_strike[s]["net"]
        if prev_cum is not None and (
            (prev_cum < 0 <= cum) or (prev_cum > 0 >= cum)
        ):
            # linear-interpolate the zero-cross between the two strikes
            span = cum - prev_cum
            gamma_flip = prev_strike if span == 0 else \
                round(prev_strike + (0 - prev_cum) / span * (s - prev_strike), 2)
        prev_cum = cum
        prev_strike = s

    totals = {"call": call_total, "put": -put_total, "net": call_total - put_total}
    return spot, rows, net_by_strike, gamma_flip, totals


def _fmt_b(v):
    """Human dollar-gamma: billions with sign."""
    return f"{v / 1e9:+.3f}B"


def print_table(spot, net_by_strike, gamma_flip, totals, top=None):
    strikes = sorted(net_by_strike)
    if top:
        strikes = sorted(strikes, key=lambda s: abs(net_by_strike[s]["net"]),
                         reverse=True)[:top]
        strikes = sorted(strikes)
    print(f"\nSpot: {spot:.2f}    Gamma flip: "
          f"{'%.2f' % gamma_flip if gamma_flip else 'n/a'}    "
          f"Net GEX: {_fmt_b(totals['net'])}")
    print(f"{'STRIKE':>9}  {'CALL GEX':>12}  {'PUT GEX':>12}  {'NET GEX':>12}  {'':>3}")
    print("-" * 58)
    cum = 0.0
    for s in sorted(net_by_strike):
        cum += net_by_strike[s]["net"]
        if top and s not in strikes:
            continue
        b = net_by_strike[s]
        mark = ""
        if gamma_flip and abs(s - gamma_flip) < 1e-6:
            mark = "<FLIP"
        elif spot and abs(s - round(spot)) < 0.5:
            mark = "<SPOT"
        print(f"{s:>9.2f}  {_fmt_b(b['call']):>12}  {_fmt_b(-b['put']):>12}  "
              f"{_fmt_b(b['net']):>12}  {mark}")


def plot_chart(spot, net_by_strike, gamma_flip, path, underlying):
    try:
        import matplotlib
        matplotlib.use("Agg")            # headless
        import matplotlib.pyplot as plt
    except Exception as e:
        print(f"[chart skipped: matplotlib unavailable — {e}]")
        return False
    strikes = sorted(net_by_strike)
    call_g = [net_by_strike[s]["call"] / 1e9 for s in strikes]
    put_g = [-net_by_strike[s]["put"] / 1e9 for s in strikes]  # plot as negative
    fig, ax = plt.subplots(figsize=(12, 6))
    ax.bar(strikes, call_g, color="#16a34a", width=0.6, label="Call GEX")
    ax.bar(strikes, put_g, color="#dc2626", width=0.6, label="Put GEX")
    ax.axhline(0, color="#888", lw=0.8)
    if gamma_flip:
        ax.axvline(gamma_flip, color="#f59e0b", lw=1.8, ls="--",
                   label=f"Gamma flip {gamma_flip:.2f}")
    if spot:
        ax.axvline(spot, color="#2563eb", lw=1.8,
                   label=f"Spot {spot:.2f}")
    ax.set_title(f"{underlying} — Net Gamma Exposure by Strike (dollar-gamma, $B)")
    ax.set_xlabel("Strike")
    ax.set_ylabel("GEX ($B)")
    ax.legend()
    ax.grid(True, alpha=0.2)
    fig.tight_layout()
    fig.savefig(path, dpi=110)
    plt.close(fig)
    print(f"\nChart written: {path}")
    return True


def main(argv=None):
    ap = argparse.ArgumentParser(description="GEX from the Massive REST API")
    ap.add_argument("underlying", nargs="?", default="SPY",
                    help="ticker (SPY, AAPL, QQQ, I:SPX ...)")
    ap.add_argument("--expiration", help="only this expiry YYYY-MM-DD")
    ap.add_argument("--all-expiries", action="store_true",
                    help="aggregate every expiry in the chain (default: nearest)")
    ap.add_argument("--term-weighted", action="store_true",
                    help="discount each contract by 1/(1+DTE)")
    ap.add_argument("--top", type=int, help="only print the N heaviest strikes")
    ap.add_argument("--chart", help="write bar chart PNG to this path")
    args = ap.parse_args(argv)

    if not massive.enabled():
        print("MASSIVE_API_KEY not set — export it first.")
        return 1

    und = args.underlying
    print(f"host={massive.BASE}  underlying={und}  "
          f"expiration={args.expiration or ('all' if args.all_expiries else 'nearest 0DTE+')}"
          f"{'  term-weighted' if args.term_weighted else ''}")
    try:
        results = fetch_chain(und, expiration=args.expiration)
    except Exception as e:
        print(f"fetch failed: {type(e).__name__}: {e}")
        return 2
    if not results:
        print("0 contracts returned (check ticker / entitlement / market hours).")
        return 3

    # default: restrict to the nearest expiry unless --all-expiries / --expiration
    only_exp = args.expiration
    if not only_exp and not args.all_expiries:
        today = datetime.now(ET).strftime("%Y-%m-%d")
        exps = sorted({e for e in
                       (massive._contract(r)[2] for r in results)
                       if e and e >= today})
        only_exp = exps[0] if exps else None

    try:
        spot, rows, net_by_strike, gamma_flip, totals = compute_gex(
            results, only_expiration=only_exp, term_weighted=args.term_weighted)
    except Exception as e:
        print(f"GEX compute failed: {e}")
        return 4

    print(f"{len(rows)} contracts used"
          f"{f' (expiry {only_exp})' if only_exp else ' (all expiries)'}")
    print_table(spot, net_by_strike, gamma_flip, totals, top=args.top)
    if args.chart:
        plot_chart(spot, net_by_strike, gamma_flip, args.chart, und)
    return 0


if __name__ == "__main__":
    sys.exit(main())
