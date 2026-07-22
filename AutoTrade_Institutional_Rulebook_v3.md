# AutoTrade Institutional Rulebook

## NIFTY Weekly Options (CE/PE Buying)

### Version 3.0

> This document is a complete design specification for an adaptive
> intraday autotrading engine. It is intended as the single source of
> truth for implementation.

------------------------------------------------------------------------

# 1. Objectives

-   Trade only NIFTY weekly expiry options.
-   Prefer ATM, ATM±1 strikes.
-   Trade only when market regime, strategy, risk and Greeks agree.
-   "No Trade" is a valid outcome.

------------------------------------------------------------------------

# 2. System Architecture

``` text
Live Feed (5s)
   ↓
Data Validation
   ↓
Market Regime Detection
   ↓
Strategy Detection
   ↓
Underlying Confirmation
   ↓
Option Confirmation
   ↓
Greeks Validation
   ↓
Confidence Engine
   ↓
Risk Engine
   ↓
Execution
   ↓
Trade Management
   ↓
Exit Engine
   ↓
Analytics & Learning
```

------------------------------------------------------------------------

# 3. Market Data

## Underlying

-   Spot
-   VWAP
-   EMA 9/21/50
-   ATR
-   CPR
-   PDH/PDL
-   Opening Range
-   Market Breadth
-   India VIX

## Option Chain

-   ATM, ATM±1, ATM±2
-   LTP
-   Volume
-   Volume Delta
-   OI
-   OI Change
-   Bid/Ask
-   IV
-   Delta
-   Gamma
-   Theta
-   Vega
-   Rho (optional)

------------------------------------------------------------------------

# 4. Market Regime Engine

Trending: - Spot above/below VWAP - EMA alignment - Rising VWAP - HH/HL
or LL/LH

Sideways: - Flat VWAP - Low ATR - Repeated VWAP crossing

Breakout: - ORB - Volume expansion - OI expansion

Reversal: - VWAP rejection - Exhaustion - Divergence

Expiry Momentum: - Gamma expansion - Rapid premium expansion -
Institutional participation

------------------------------------------------------------------------

# 5. Strategy Library

1.  VWAP Reclaim
2.  VWAP Bounce
3.  VWAP Rejection
4.  EMA Pullback
5.  Opening Range Breakout
6.  Momentum Continuation
7.  Trend Continuation
8.  Reversal
9.  CPR Breakout
10. Previous Day High/Low Break
11. OI Breakout
12. Delta Momentum
13. Gamma Expansion
14. IV Expansion
15. Gap & Go
16. Trend Day
17. Mean Reversion
18. Liquidity Sweep
19. Expiry Scalping
20. No Trade

Each strategy must define: - Preconditions - Entry - Stop Loss -
Target - Trailing - Cancellation rules - Exit rules

------------------------------------------------------------------------

# 6. VWAP Framework

Master filter = Spot VWAP

Bullish: - Spot \> VWAP - Option \> VWAP

Bearish: - Spot \< VWAP - Option \< VWAP

Patterns: - Reclaim - Bounce - Rejection - Compression

Distance: - \<0.8% ideal - 0.8-1.5% acceptable - \>2% avoid

------------------------------------------------------------------------

# 7. Greeks Validation

## Delta

CE: preferred +0.45 to +0.65 increasing PE: preferred -0.45 to -0.65
increasing in magnitude

Reject if spot and delta diverge.

## Gamma

Prefer increasing gamma. Critical on expiry day.

## Theta

Avoid buying when theta decay dominates expected premium gain. Reduce
new entries late afternoon unless momentum is exceptional.

## Vega

Normal days: low weight. News/volatility: validate IV expansion.

## IV

IV percentile: - \<20 cheap - 20-60 normal - 60-80 elevated - \>80
expensive

Avoid buying into extreme IV without strong directional conviction.

------------------------------------------------------------------------

# 8. Option Chain Validation

Validate: - OI build-up - OI unwinding - Long build-up - Short
build-up - PCR - Max Pain (context) - Bid/Ask spread - Liquidity

------------------------------------------------------------------------

# 9. Confidence Engine (100)

  Component         Score
  --------------- -------
  Spot VWAP            20
  Option VWAP          15
  Market Regime        15
  Volume               10
  OI                   10
  EMA                  10
  Delta                 8
  Gamma                 5
  Theta                 2
  Vega/IV               3
  Spread                2

Trade: 85-100 Strong 75-84 Valid 65-74 Watch \<65 No Trade

------------------------------------------------------------------------

# 10. Risk Engine

-   Risk/trade: 1%
-   Daily loss: 3%
-   Max open trades: 1
-   Max consecutive losses: 3

Avoid: - First 3 minutes - Major news - Illiquid contracts - Wide
spreads

------------------------------------------------------------------------

# 11. Multi-Timeframe Confirmation

Use: - 1m - 3m - 5m - 15m (trend only)

Higher timeframe must not contradict lower timeframe.

------------------------------------------------------------------------

# 12. Time-of-Day Rules

09:15-09:20 Observe 09:20-10:30 Highest opportunity 10:30-12:00 Trend
continuation 12:00-13:30 Lower quality 13:30-15:00 Fresh opportunities
15:00-15:20 Manage exits 15:20+ Avoid fresh entries except defined
expiry scalps

------------------------------------------------------------------------

# 13. Entry Checklist

-   Regime identified
-   Strategy selected
-   Spot confirms
-   Option confirms
-   Greeks confirm
-   OI confirms
-   Volume confirms
-   Confidence \>=75
-   Risk acceptable

------------------------------------------------------------------------

# 14. Exit Engine

Exit on: - Target - Stop - Spot loses VWAP - Strategy invalidated -
Confidence deterioration - Opposite regime

Trail using: - VWAP - EMA9 - Swing

------------------------------------------------------------------------

# 15. Performance Analytics

Track: - Win rate - Expectancy - Avg R - Drawdown - Strategy-wise P/L -
Time-slot P/L - Expiry vs normal - Greeks at entry - Confidence at entry

Reduce strategy priority if sustained underperformance.

------------------------------------------------------------------------

# 16. JSON Mapping

Every rule should map to backend fields:

-   regime
-   strategy
-   confidence
-   spot_vwap
-   option_vwap
-   volume_score
-   oi_score
-   delta_score
-   gamma_score
-   theta_score
-   iv_score
-   spread_score
-   risk_score
-   entry_reason
-   exit_reason

------------------------------------------------------------------------

# 17. Core Principles

1.  Spot leads.
2.  Option confirms.
3.  Greeks validate.
4.  Risk overrides strategy.
5.  No Trade is profitable.
6.  Every decision must be logged.
7.  Every trade must be reproducible from rule inputs.
