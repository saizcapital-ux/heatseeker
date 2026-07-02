#!/usr/bin/env python3
"""
Massive.com real-time options data → GEX engine.

Replaces the 15-min-delayed CBOE feed with Massive's real-time Option Chain
Snapshot (delta/gamma/IV/OI per contract, all 17 US options exchanges).

API: GET https://api.massive.com/v3/snapshot/options/{TICKER}?apiKey=...
  TICKER: "SPY" for equity, "I:SPX" for the index.
  filters: expiration_date=YYYY-MM-DD, strike_price.gte / .lte, contract_type
  response: { results:[ { details:{contract_type,strike_price,expiration_date},
              greeks:{delta,gamma,...}, implied_volatility, open_interest,
              day:{volume}, underlying_asset:{price} } ... ], next_url }

The API key is read from MASSIVE_API_KEY — never hard-coded. Every call is
best-effort: any failure returns None so callers fall back to CBOE.
"""
import os, json, math, urllib.request, urllib.parse, urllib.error
from datetime import datetime
from zoneinfo import ZoneInfo

ET = ZoneInfo("America/New_York")
BASE = "https://api.massive.com"
STRIKE_WINDOW_SPY = 25
STRIKE_WINDOW_SPX = 250


def api_key():
    return os.getenv("MASSIVE_API_KEY", "").strip()

def enabled():
    return bool(api_key())


def _get_url(url, timeout=12):
    sep = "&" if "?" in url else "?"
    if "apiKey=" not in url:
        url = url + sep + "apiKey=" + api_key()
    req = urllib.request.Request(url, headers={
        "User-Agent": "HeatseekerTerminal/1.0", "Accept": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode())

def _get(path, params, timeout=12):
    p = dict(params or {}); p["apiKey"] = api_key()
    return _get_url(BASE + path + "?" + urllib.parse.urlencode(p), timeout)


def chain_snapshot(underlying, expiration_gte=None, expiration=None,
                   strike_gte=None, strike_lte=None, limit=250, max_pages=6):
    """Fetch the option-chain snapshot (paginated) for an underlying."""
    params = {"limit": limit}
    if expiration:     params["expiration_date"] = expiration
    if expiration_gte: params["expiration_date.gte"] = expiration_gte
    if strike_gte is not None: params["strike_price.gte"] = strike_gte
    if strike_lte is not None: params["strike_price.lte"] = strike_lte
    data = _get("/v3/snapshot/options/" + underlying, params)
    results = list(data.get("results") or [])
    nxt, pages = data.get("next_url"), 0
    while nxt and pages < max_pages:
        d = _get_url(nxt)
        results += list(d.get("results") or [])
        nxt = d.get("next_url"); pages += 1
    return results


# ── field accessors (defensive: tolerate minor schema variants) ──
def _f(r, *path, default=None):
    cur = r
    for k in path:
        if not isinstance(cur, dict):
            return default
        cur = cur.get(k)
    return cur if cur is not None else default

def _contract(r):
    typ = _f(r, "details", "contract_type") or _f(r, "contract_type")
    k   = _f(r, "details", "strike_price", default=_f(r, "strike_price"))
    exp = _f(r, "details", "expiration_date", default=_f(r, "expiration_date"))
    return typ, k, exp


def _bs_dv(S, K, sig, T, is_call, r=0.045, q=0.013):
    if not (sig and S and K and T) or sig <= 0 or T <= 0:
        return 0.0, 0.0
    sq = sig * math.sqrt(T)
    d1 = (math.log(S / K) + (r - q + sig * sig / 2) * T) / sq
    pdf = math.exp(-d1 * d1 / 2) / math.sqrt(2 * math.pi)
    Nd1 = 0.5 * (1 + math.erf(d1 / math.sqrt(2)))
    delta = math.exp(-q * T) * (Nd1 if is_call else Nd1 - 1)
    vega = S * math.exp(-q * T) * pdf * math.sqrt(T) / 100.0
    return delta, vega


def analytics(results, window):
    """Pure: Massive results[] → GEX state patch (same shape as the CBOE engine)."""
    if not results:
        return None
    spot = None
    for r in results:
        p = _f(r, "underlying_asset", "price") or _f(r, "underlying_asset", "last_price")
        if p:
            spot = float(p); break
    today = datetime.now(ET).strftime("%Y-%m-%d")

    # group by expiry → nearest today-or-later
    by_exp = {}
    for r in results:
        typ, k, exp = _contract(r)
        if not typ or k is None or not exp:
            continue
        by_exp.setdefault(exp, []).append((typ, float(k), r))
    future = sorted(e for e in by_exp if e >= today)
    if not future:
        return None
    exp = future[0]

    if not spot:  # fall back to a mid-strike estimate
        ks = sorted(k for _, k, _ in by_exp[exp])
        spot = ks[len(ks) // 2] if ks else None
    if not spot:
        return None

    try:
        exp_dt = datetime.strptime(exp, "%Y-%m-%d").replace(hour=16, tzinfo=ET)
        T = max((exp_dt - datetime.now(ET)).total_seconds() / (365.0 * 86400), 1.0 / (365 * 24 * 6))
    except Exception:
        T = 1.0 / 365

    calls, puts = {}, {}
    any_gamma = False
    for typ, k, r in by_exp[exp]:
        if abs(k - spot) > window:
            continue
        oi = float(_f(r, "open_interest", default=0) or 0)
        gm = float(_f(r, "greeks", "gamma", default=0) or 0)
        dl = float(_f(r, "greeks", "delta", default=0) or 0)
        iv = float(_f(r, "implied_volatility", default=0) or 0)
        vol = float(_f(r, "day", "volume", default=0) or 0)
        if gm:
            any_gamma = True
        bucket = calls if str(typ).lower().startswith("c") else puts
        b = bucket.setdefault(k, {"oi": 0.0, "gamma": 0.0, "delta": 0.0, "iv": 0.0, "vol": 0.0})
        b["oi"] += oi; b["gamma"] = gm or b["gamma"]; b["delta"] = dl or b["delta"]
        b["iv"] = iv or b["iv"]; b["vol"] += vol
    if not any_gamma:
        return None

    strikes = sorted(set(calls) | set(puts))
    if not strikes:
        return None

    def gex(oi, gamma, is_call):
        return (1 if is_call else -1) * oi * gamma * spot * 100

    ladder, net_total, dex_shares, book_vega = [], 0.0, 0.0, 0.0
    for s in strikes:
        c = calls.get(s, {}); p = puts.get(s, {})
        c_oi, p_oi = c.get("oi", 0), p.get("oi", 0)
        cg = gex(c_oi, c.get("gamma", 0), True)
        pg = gex(p_oi, p.get("gamma", 0), False)
        net_total += (cg + pg) / 1e6
        cd = c.get("delta") or _bs_dv(spot, s, c.get("iv", 0), T, True)[0]
        pd = p.get("delta") or _bs_dv(spot, s, p.get("iv", 0), T, False)[0]
        cv = _bs_dv(spot, s, c.get("iv", 0), T, True)[1]
        pv = _bs_dv(spot, s, p.get("iv", 0), T, False)[1]
        dex_shares += cd * c_oi - pd * p_oi
        book_vega  += cv * c_oi + pv * p_oi
        ladder.append({
            "strike": s, "call_oi": int(c_oi), "put_oi": int(p_oi),
            "call_vol": int(c.get("vol", 0)), "put_vol": int(p.get("vol", 0)),
            "call_gex_m": round(cg / 1e6, 3), "put_gex_m": round(pg / 1e6, 3),
            "net_gex_m": round((cg + pg) / 1e6, 3), "call_prem_k": 0.0, "put_prem_k": 0.0,
        })

    king = max(ladder, key=lambda r: abs(r["net_gex_m"]))
    king_strike, king_gex_m = king["strike"], king["net_gex_m"]
    cum, prev, gamma_flip = 0.0, None, king_strike
    for r in ladder:
        cum += r["net_gex_m"]
        if prev is not None and ((prev < 0 <= cum) or (prev > 0 >= cum)):
            gamma_flip = r["strike"]
        prev = cum
    calls_above = [(s, calls[s]["oi"]) for s in calls if s > spot]
    puts_below  = [(s, puts[s]["oi"])  for s in puts  if s < spot]
    call_wall = max(calls_above, key=lambda x: x[1])[0] if calls_above else None
    put_wall  = max(puts_below,  key=lambda x: x[1])[0] if puts_below  else None
    max_pain, best = None, None
    for k in strikes:
        pay = sum(calls.get(s, {}).get("oi", 0) * max(0.0, k - s) for s in strikes) \
            + sum(puts.get(s, {}).get("oi", 0)  * max(0.0, s - k) for s in strikes)
        if best is None or pay < best:
            best, max_pain = pay, k
    total_abs = sum(abs(r["net_gex_m"]) for r in ladder) or 1

    return {
        "spot": round(spot, 2), "strike_ladder": ladder,
        "net_gex_total": round(net_total, 2),
        "net_delta_m": round(dex_shares * spot * 100 / 1e6, 1),
        "book_vega_m": round(book_vega * 100 / 1e6, 2),
        "king_strike": king_strike, "king_gex_m": round(king_gex_m, 2),
        "gamma_flip": gamma_flip, "max_pain": max_pain,
        "call_wall": call_wall, "put_wall": put_wall,
        "direction": "call" if king_gex_m >= 0 else "put",
        "confidence": round(abs(king_gex_m) / total_abs, 3),
        "gex_source": "massive", "gex_ts": None, "gex_expiry": exp,
    }


def compute(underlying="SPY", window=None):
    """Fetch today's 0DTE (or nearest) chain from Massive and compute GEX. None on failure."""
    if not enabled():
        return None
    window = window or (STRIKE_WINDOW_SPX if underlying.startswith("I:") else STRIKE_WINDOW_SPY)
    try:
        today = datetime.now(ET).strftime("%Y-%m-%d")
        results = chain_snapshot(underlying, expiration_gte=today, limit=250)
        return analytics(results, window)
    except Exception:
        return None


def diagnose(underlying="SPY", window=None):
    """Like compute() but returns (patch, reason). reason is a human-readable
    string explaining why the call fell back (None patch), for surfacing in logs.
    Never raises."""
    if not enabled():
        return None, "MASSIVE_API_KEY not set"
    window = window or (STRIKE_WINDOW_SPX if underlying.startswith("I:") else STRIKE_WINDOW_SPY)
    try:
        today = datetime.now(ET).strftime("%Y-%m-%d")
        results = chain_snapshot(underlying, expiration_gte=today, limit=250)
    except urllib.error.HTTPError as e:
        body = ""
        try:
            body = e.read().decode()[:200]
        except Exception:
            pass
        return None, f"HTTP {e.code} from Massive ({body.strip()})"
    except urllib.error.URLError as e:
        return None, f"network error reaching Massive: {e.reason}"
    except Exception as e:
        return None, f"{type(e).__name__}: {e}"
    if not results:
        return None, "Massive returned 0 contracts (check ticker/entitlement/market hours)"
    patch = analytics(results, window)
    if not patch:
        return None, f"parsed {len(results)} contracts but no usable gamma (no greeks?)"
    return patch, "ok"
