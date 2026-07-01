#!/usr/bin/env python3
"""
Post HEATSEEKER trade signals to X (formerly Twitter).

Opt-in and safe by design:
  * Does nothing unless TWITTER_ENABLED=true AND all four API keys are set.
  * Never raises — a failed/absent tweet can never affect trading.
  * Callers only invoke it on REAL fills (not dry-run), so it won't spam.

Required env (X developer app, OAuth 1.0a user context, "Read and write"):
  TWITTER_API_KEY, TWITTER_API_SECRET, TWITTER_ACCESS_TOKEN, TWITTER_ACCESS_SECRET
Toggle:
  TWITTER_ENABLED = true | false   (default false)
"""
import os, logging

log = logging.getLogger("heatseeker.social")


def enabled():
    return os.getenv("TWITTER_ENABLED", "false").lower() == "true"


def post_tweet(text):
    """Post a tweet. Returns the response, or None if disabled/misconfigured/failed."""
    text = (text or "").strip()[:280]
    if not text:
        return None
    if not enabled():
        log.info(f"[twitter off] would tweet:\n{text}")
        return None
    ck  = os.getenv("TWITTER_API_KEY")
    cs  = os.getenv("TWITTER_API_SECRET")
    at  = os.getenv("TWITTER_ACCESS_TOKEN")
    ats = os.getenv("TWITTER_ACCESS_SECRET")
    if not all([ck, cs, at, ats]):
        log.warning("TWITTER_ENABLED=true but API keys are incomplete — skipping tweet")
        return None
    try:
        import tweepy
        client = tweepy.Client(consumer_key=ck, consumer_secret=cs,
                               access_token=at, access_token_secret=ats)
        resp = client.create_tweet(text=text)
        log.info(f"Tweeted: {text[:60]}...")
        return resp
    except Exception as e:
        log.warning(f"Tweet failed (non-fatal): {e}")
        return None


def tweet_entry(*, direction, strike, price, king_strike=None, gamma_flip=None,
                confidence=None, spot=None):
    """Compose + post an entry signal."""
    side = "CALL" if str(direction).lower().startswith("c") else "PUT"
    emoji = "🟢📈" if side == "CALL" else "🔴📉"
    lines = [f"{emoji} HEATSEEKER 0DTE ENTRY",
             f"BUY SPY ${int(strike)} {side} @ ${price:.2f}"]
    ctx = []
    if spot:        ctx.append(f"spot ${spot:.2f}")
    if king_strike: ctx.append(f"king ${int(king_strike)}")
    if gamma_flip:  ctx.append(f"γflip ${int(gamma_flip)}")
    if confidence is not None:
        c = confidence * 100 if confidence <= 1 else confidence
        ctx.append(f"conf {c:.0f}%")
    if ctx:
        lines.append(" · ".join(ctx))
    lines.append("#0DTE #SPY #GEX #optionsflow")
    return post_tweet("\n".join(lines))


def tweet_exit(*, direction, strike, pnl_pct, pnl_usd, reason=None, balance=None):
    """Compose + post an exit signal."""
    side = "CALL" if str(direction).lower().startswith("c") else "PUT"
    win = (pnl_usd or 0) >= 0
    emoji = "✅💰" if win else "🛑"
    sign = "+" if win else ""
    lines = [f"{emoji} HEATSEEKER EXIT",
             f"SOLD SPY ${int(strike)} {side}  {sign}{pnl_pct:.0f}% ({sign}${abs(pnl_usd):.0f})"]
    if reason:  lines.append(reason)
    if balance: lines.append(f"account ${balance:.2f}")
    lines.append("#0DTE #SPY #GEX")
    return post_tweet("\n".join(lines))
