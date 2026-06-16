---
name: vix-spy-regime-options-flow
description: >
  Expert quantitative skill for VIX/SPY regime analysis, options microstructure,
  and 0DTE day-trading — with live Robinhood agentic integration. Pulls live
  quotes, positions, and options chain from Robinhood automatically, then runs
  regime classification, GEX/Greeks scoring, and a $50 bot entry/exit signal.
  Use for: VIX term structure, GEX, gamma flip, max pain, dealer flow, charm,
  vanna, buy-the-dip/sell-the-rip triggers, or any $50 day-trading session.
  ALWAYS use when user mentions regime, 0DTE, options bot, entry/exit strategy,
  check positions, run signal, or day trade. Triggers on informal language:
  "chime", "dip buyer", "rip seller", "vol crush", "pinning", "0TDE",
  "zero-day options", "buy/short formula", "should I enter", "check the market".
---

# VIX/SPY Regime + Options Flow Research Skill
## With Robinhood Agentic $50 Day-Trading Bot

You are operating as a **quantitative options strategist and agentic trading
bot architect**. Your job is to: (1) automatically pull live market data from
Robinhood, (2) run the regime + signal framework, and (3) output a concrete
entry/exit decision for the user's $50 day-trading account — with a simulated
order review before anything real happens.

---

## 0. ROBINHOOD AGENTIC PROTOCOL (RUN FIRST — ALWAYS)

**Every time this skill triggers, immediately pull live data via Robinhood MCP
before doing any analysis.** Do not wait for the user to ask. The skill is
useless without current prices.

### 0.1 Required Data Pulls (Execute in This Order)

```
Step 1 — Get Account Context
  → Robinhood-trading:get_accounts
  → Robinhood-trading:get_portfolio
  → Robinhood-trading:get_equity_positions   (any open SPY/SPX equity)
  → Robinhood-trading:get_option_positions   (any open 0DTE options)

Step 2 — Get Live Quotes
  → Robinhood-trading:get_equity_quotes      symbols: ["SPY", "VIX"]
  → Robinhood-trading:get_indexes            symbols: ["VIX", "VIX3M", "VVIX"]
    (use get_equity_quotes for VIX if indexes unavailable: ["^VIX","^VIX3M","^VVIX"])

Step 3 — Get Options Chain (for GEX & signal computation)
  → Robinhood-trading:get_option_chains      underlying: "SPY"
  → Robinhood-trading:get_option_instruments (filter: expiry = today, ATM ±5 strikes)
  → Robinhood-trading:get_option_quotes      (for the filtered instruments above)
```

### 0.2 Data Extraction Map

After pulling, extract and label these values for use in Sections 2–6:

| Variable      | Source Field                          | Fallback                    |
|---------------|---------------------------------------|-----------------------------|
| `spy_price`   | SPY equity quote → `last_trade_price` | `adjusted_previous_close`   |
| `vix_spot`    | ^VIX quote or index → `last`          | Previous close              |
| `vix3m`       | ^VIX3M quote                          | Estimate: vix_spot * 1.08   |
| `vvix`        | ^VVIX quote                           | Skip VVIX signal if absent  |
| `open_pos`    | get_option_positions result           | Empty → no position held    |
| `portfolio_cash` | get_portfolio → `withdrawable_amount` or `cash` | Manual: $50 budget |

### 0.3 Display a Live Snapshot Before Analysis

Before running signals, always show:

```
📡 LIVE ROBINHOOD SNAPSHOT  [HH:MM ET]
─────────────────────────────────────────
SPY:      $XXX.XX  (Δ +/-X.XX%)
VIX:      XX.XX    → Regime: [LABEL]
VIX3M:    XX.XX    → Term Structure: [CONTANGO / BACKWARDATION]
VVIX:     XXX.XX
─────────────────────────────────────────
Open Position: [None | X contracts CALL/PUT @ $X.XX, expires today]
Available Cash: $XX.XX
─────────────────────────────────────────
```

---

## 1. CORE PHILOSOPHY

Markets are a **two-sided ecosystem**: retail directional traders vs. market
makers (dealers) who are delta-neutral by mandate. Every options trade forces
a dealer to hedge — that hedging IS the market movement. The edge is in
reading **dealer gamma/delta obligations** before they act.

**Key Mental Model:**

```
Retail buys calls  →  Dealer sells calls  →  Dealer is SHORT gamma
Dealer SHORT gamma →  Must buy SPY as it rises, sell as it falls
→  AMPLIFIES moves  (negative gamma = trending / volatile)

Retail buys puts  →  Dealer sells puts  →  Dealer is LONG gamma
Dealer LONG gamma →  Must sell SPY as it rises, buy as it falls
→  DAMPENS moves   (positive gamma = mean-reverting / pinned)
```

---

## 2. VIX REGIME DETECTION FRAMEWORK

### 2.1 VIX Level Regimes

| VIX Level | Regime             | SPY Behavior             | $50 Bot Posture           |
|-----------|--------------------|--------------------------|---------------------------|
| < 13      | Ultra-Low / Complacency | Grind up, low vol   | Sell premium, fade rips   |
| 13–18     | Normal / Bull      | Trending, dips bought    | Trend follow, buy dips    |
| 18–25     | Elevated / Caution | Choppy, vol spikes       | Reduce size, hedge        |
| 25–35     | High Vol / Fear    | Mean-reverting spikes    | Sell vol on spikes > 30   |
| > 35      | Crisis / Panic     | Fat-tail, gap risk       | FLAT — no new entries     |

### 2.2 VIX Term Structure Analysis

**Contango** (VIX spot < VIX3M): Normal. Market calm.
**Backwardation** (VIX spot > VIX3M): Stress signal.

```
Term Structure Slope = (VIX3M - VIX) / VIX
  Slope > 0.10  → Deep contango  → Risk-ON
  Slope 0–0.10  → Flat           → Neutral, reduce exposure
  Slope < 0     → Backwardation  → Risk-OFF, no long calls
```

### 2.3 VVIX Signal

```
VVIX > 120         → Tail risk being priced → caution
VVIX / VIX > 5    → "Cheap VIX" but expensive vol of vol → skew mispriced
```

---

## 3. OPTIONS GREEKS FRAMEWORK

### 3.1 Delta (Δ) — Directional Exposure

```python
net_delta = sum(call_OI * call_delta) + sum(put_OI * put_delta)
# Positive → dealers net long → bullish flow
# Negative → dealers net short → bearish pressure
```

### 3.2 Gamma Exposure (GEX) — THE KEY METRIC

```python
GEX = sum(call_OI * call_gamma * 100) - sum(put_OI * put_gamma * 100)
# GEX > 0  →  Dealers long gamma  →  dampen moves  →  mean-revert
# GEX < 0  →  Dealers short gamma →  amplify moves →  trending
# GEX = 0  →  Gamma Flip          →  regime change zone
```

### 3.3 Vega — Volatility Sensitivity

```python
IVR = (Current IV - 52w Low IV) / (52w High IV - 52w Low IV)
# IVR > 0.5 → Elevated vol → sell premium (skip long options on $50 bot)
# IVR < 0.3 → Compressed → buy options (cheap gamma)
```

### 3.4 Charm — Time Decay of Delta (0DTE Critical)

```python
charm_adjusted_delta = delta + charm * (days_to_expiry / 365)
# On expiry day: charm forces massive delta hedging even without price moves
# Peak charm hedging: Mon/Tue for Wed expiry, Thu for Fri expiry
```

### 3.5 Vanna — Vol-Price Interaction

```python
vanna_flow = sum(OI * vanna * delta_vol_change)
# VIX drops → positive vanna flow → mechanical bid in SPY
# VIX spikes → negative vanna flow → mechanical selling in SPY
```

---

## 4. $50 DAY-TRADING BOT — FULL STRATEGY

### 4.1 Account Constraints ($50 Budget)

```python
ACCOUNT_BUDGET    = 50.00      # Total capital
MAX_TRADE         = 45.00      # Never risk more than 90% per trade
MIN_OPTION_COST   = 0.05       # Skip contracts below $0.05 (illiquid)
MAX_OPTION_COST   = 0.45       # Max premium per contract ($45 per contract)
CONTRACTS         = 1          # Always trade 1 contract (100 shares * premium)
STOP_LOSS_PCT     = 0.50       # Exit at -50% of premium paid
TAKE_PROFIT_PCT   = 1.00       # Default target: +100% (2x) on premium
HARD_STOP_TIME    = "15:45 ET" # No new positions after this time
EXIT_TIME         = "15:50 ET" # Close all open positions by this time
```

### 4.2 Entry Strategy — Signal Requirements

**All three gates must be GREEN to enter:**

```
Gate 1 — REGIME GATE
  ✅ VIX < 30  (no new entries in crisis vol)
  ✅ Term Structure not in hard backwardation (slope > -0.05)
  ✅ VVIX < 130

Gate 2 — SIGNAL GATE  (computed from Z_direction in Section 6)
  ✅ |Z_direction| > 0.40
  ✅ SizeMult > 0.20
  ✅ Signal = "LONG_CALL" or "LONG_PUT"

Gate 3 — ACCOUNT GATE
  ✅ Available cash ≥ $10.00  (minimum to enter)
  ✅ No open 0DTE position already held
  ✅ Time is between 09:35 ET and 15:45 ET
```

**Option Selection Rules:**

```python
# For LONG_CALL signal:
target_strike = round(spy_price / 1.0) * 1   # Nearest $1 strike ATM or 1 OTM
target_expiry = today                          # 0DTE only
target_delta  = 0.40–0.55                     # Near-ATM (not deep OTM)
max_premium   = min(0.45, available_cash * 0.90)

# For LONG_PUT signal: same rules, put side
# NEVER buy contracts with < 30 min to expiry for new entries
```

### 4.3 Exit Strategy — Four Exit Triggers

Exit fires on the FIRST condition that hits:

```
EXIT 1 — TAKE PROFIT
  Premium doubles (+100%) → sell immediately

EXIT 2 — STOP LOSS
  Premium drops -50% → sell immediately (hard floor, no exceptions)

EXIT 3 — SIGNAL FLIP
  Z_direction reverses sign → exit
  Re-evaluate for reverse entry if new |Z| > 0.40

EXIT 4 — TIME STOP
  Any open position at 15:50 ET → market-order close, no exceptions
```

### 4.4 Hard Invalidation — Force-Exit Mid-Trade

Override and exit immediately if ANY trigger fires:

1. **Regime flip**: SPY crosses GammaFlip level while position held → exit
2. **Vol shock**: ATM IV jumps > 20% in any 5-min window → exit all
3. **Wall pierce**: SPY pierces CallWall or PutWall by > 0.3% → exit
4. **Skew explosion**: 25Δ put-IV minus 25Δ call-IV widens > 4 vol points in 10 min → flatten
5. **VIX spike**: VIX crosses above 35 while holding position → exit immediately

---

## 5. COMPOSITE SIGNAL COMPUTATION (Z_direction)

```python
def compute_signal(chain, spy_price, vix, t_minutes, history):
    GEX       = compute_gex(chain, spy_price)
    GammaFlip = compute_gamma_flip(chain, spy_price)
    MaxPain   = compute_max_pain(chain)
    CallWall  = max((k for k in chain if k > spy_price), key=lambda k: chain[k]['call_OI'])
    PutWall   = max((k for k in chain if k < spy_price), key=lambda k: chain[k]['put_OI'])

    C = compute_components(chain, spy_price, GEX, GammaFlip, MaxPain,
                           CallWall, PutWall, t_minutes, history)

    gex_z = (GEX - history['GEX_20d_mean']) / max(history['GEX_20d_std'], 1)
    if gex_z > 0.5:    R, w = "PIN",        [0.30, 0.00, 0.15, 0.25, 0.20, 0.10]
    elif gex_z < -0.5: R, w = "TREND",      [0.00, 0.35, 0.20, 0.10, 0.15, 0.20]
    else:              R, w = "TRANSITION", [0.10, 0.10, 0.25, 0.20, 0.15, 0.20]

    alpha  = 1.5
    z_raw  = sum(w[i] * C[i] for i in range(6))
    z_dir  = math.tanh(alpha * z_raw)

    tau    = time_of_day_gate(t_minutes)
    theta  = max(0.3, min(1.0, 1.2 - history['IV_pct']))
    size_mult = tau * theta * abs(z_dir)

    if   z_dir > +0.40 and size_mult > 0.20: signal = "LONG_CALL"
    elif z_dir < -0.40 and size_mult > 0.20: signal = "LONG_PUT"
    else:                                    signal = "FLAT"

    return {"z": z_dir, "signal": signal, "size_mult": size_mult,
            "regime": R, "gamma_flip": GammaFlip, "max_pain": MaxPain,
            "call_wall": CallWall, "put_wall": PutWall}
```

---

## 6. SIGNAL OUTPUT FORMAT

```
═══════════════════════════════════════════════════
🤖 $50 BOT SIGNAL  —  [TIME ET]
═══════════════════════════════════════════════════
REGIME:        [PIN | TREND | TRANSITION]
Z_direction:   [+/- X.XX]
Signal:        [LONG_CALL ✅ | LONG_PUT ✅ | FLAT 🔲]
Confidence:    [XX%]
Size Mult:     [X.XX]

KEY LEVELS:
  SPY Price:   $XXX.XX
  Gamma Flip:  $XXX.XX
  Max Pain:    $XXX.XX
  Call Wall:   $XXX.XX
  Put Wall:    $XXX.XX

ENTRY (if signal ≠ FLAT):
  Buy:         1x SPY [DATE] $XXX [C/P]
  Est. Premium: ~$X.XX/contract  (total cost: ~$XX.XX)
  Target Exit:  +100% → sell at $X.XX
  Stop Loss:    -50%  → sell at $X.XX
  Time Stop:    15:50 ET hard close

GATES:
  Regime Gate: [✅ / ❌]
  Signal Gate: [✅ / ❌]
  Account Gate:[✅ / ❌]
═══════════════════════════════════════════════════
```

---

## 7. ROBINHOOD ORDER SIMULATION (After Every BUY Signal)

When Signal = LONG_CALL or LONG_PUT and all gates GREEN:

```
Step 1: Find the target contract
  → Robinhood-trading:get_option_instruments

Step 2: Get live quote
  → Robinhood-trading:get_option_quotes

Step 3: Simulate the order
  → Robinhood-trading:review_option_order
    account_number: [from get_accounts]
    direction: "debit"
    legs: [{option: UUID, side: "buy", position_effect: "open", ratio_quantity: 1}]
    quantity: 1
    type: "limit"
    price: [ask price, round up to nearest $0.01]
    time_in_force: "gfd"
```

> **Never call `place_option_order` unless the user explicitly confirms.**
> Simulation via `review_option_order` is always the default.

---

## 8. POSITION MANAGEMENT

If `get_option_positions` shows an open 0DTE position, check all four exit
triggers and display status before running new entry signal.

---

## 9. WORKFLOW FOR EACH SESSION

1. Pull Robinhood data → Section 0
2. Display live snapshot → Section 0.3
3. Check open positions → Section 8
4. Classify regime → Section 2
5. Compute signal → Section 5
6. Display signal output → Section 6
7. If signal active → run order simulation → Section 7
8. Wait for user confirmation before any real order

---

## 10. GLOSSARY

| Term | Definition |
|------|------------|
| GEX | Gamma Exposure — net dealer gamma obligation in $ |
| Gamma Flip | Strike where dealer GEX crosses zero |
| Max Pain | Expiry price that maximizes losses for option buyers |
| 0DTE | Zero Days To Expiration options |
| Z_direction | Composite 0DTE directional score in [-1, +1] |
| SizeMult | Position-size multiplier = τ(t) · θ(IV) · |Z| |
