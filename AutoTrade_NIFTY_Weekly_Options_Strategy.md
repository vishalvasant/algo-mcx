# AutoTrade Strategy Framework for NIFTY Weekly Options (CE/PE)

## Objective

Build an adaptive intraday trading engine that trades **NIFTY weekly
expiry CE/PE options** by first identifying the market regime, then
selecting the best strategy, and finally executing only high-confidence
trades.

------------------------------------------------------------------------

# Pipeline

``` text
Market Data
    │
    ▼
Market Regime Detection
    │
    ▼
Strategy Selection
    │
    ▼
Confidence Engine
    │
    ▼
Risk Validation
    │
    ▼
Order Execution
    │
    ▼
Trade Management
```

------------------------------------------------------------------------

# Market Data Inputs

## Underlying (Highest Priority)

-   NIFTY Spot
-   India VIX
-   Market Breadth
-   Opening Range
-   Previous Day High/Low
-   CPR
-   VWAP
-   EMA 9 / 21 / 50
-   ATR

## Option Chain

-   ATM
-   ATM+1
-   ATM-1

Collect

-   LTP
-   Volume
-   OI
-   OI Change
-   IV
-   Delta
-   Gamma
-   Theta
-   Bid/Ask Spread

------------------------------------------------------------------------

# Market Regime Detection

## Trending

Conditions

-   Spot above VWAP
-   EMA9 \> EMA21
-   Rising VWAP
-   Higher Highs
-   Strong Volume

Strategies

-   EMA Pullback
-   VWAP Bounce
-   Momentum Breakout

------------------------------------------------------------------------

## Sideways

Conditions

-   Price oscillates around VWAP
-   Flat EMA
-   Low ATR

Strategies

-   No Trade
-   Breakout Wait

------------------------------------------------------------------------

## Breakout

Conditions

-   Opening Range Break
-   Volume Spike
-   OI Increase
-   Spot above VWAP

Strategies

-   ORB
-   VWAP Reclaim

------------------------------------------------------------------------

## Reversal

Conditions

-   Exhaustion
-   VWAP Rejection
-   Divergence

Strategies

-   Mean Reversion
-   VWAP Rejection

------------------------------------------------------------------------

## High Volatility (Expiry)

Conditions

-   Rapid premium expansion
-   Gamma acceleration
-   Large candles

Strategies

-   Momentum only
-   Avoid fading

------------------------------------------------------------------------

# VWAP Engine

Spot VWAP has highest weight.

Rules

Bullish

-   Spot \> VWAP
-   Option \> VWAP

Bearish

-   Spot \< VWAP
-   Option \< VWAP

Reject trades if spot and option disagree.

------------------------------------------------------------------------

# Supported Strategies

## 1. VWAP Reclaim

Entry

-   Spot below VWAP
-   Strong break above VWAP
-   Retest successful
-   Volume increasing
-   Option above VWAP

Exit

-   Spot closes below VWAP
-   Target
-   Trailing SL

------------------------------------------------------------------------

## 2. EMA Pullback

Conditions

-   EMA9 \> EMA21
-   Price pulls back
-   Bounce
-   Spot above VWAP

------------------------------------------------------------------------

## 3. ORB Breakout

Conditions

-   Break opening range
-   Volume spike
-   OI increasing
-   Spot above VWAP

------------------------------------------------------------------------

## 4. Momentum Breakout

Conditions

-   Higher highs
-   Rising Delta
-   Rising Gamma
-   Rising Volume
-   Above VWAP

------------------------------------------------------------------------

## 5. Reversal

Conditions

-   Price extended
-   VWAP rejection
-   Delta weakening

------------------------------------------------------------------------

# Confidence Engine (100)

  Signal             Weight
  ---------------- --------
  Spot VWAP              20
  Option VWAP            15
  Volume                 10
  OI                     10
  EMA Trend              10
  Delta                  10
  Gamma                   5
  ORB                     5
  Strategy Match         10
  Risk Filters            5

Trade Rules

-   =85 : Strong Buy

-   75-84 : Buy

-   65-74 : Watch

-   \<65 : No Trade

------------------------------------------------------------------------

# Entry Validation

Required

-   Bid/Ask spread acceptable
-   Volume above average
-   OI supports direction
-   Spot agrees with option
-   Confidence \>=75

Reject

-   News spike
-   IV extremely high
-   Price \>2% away from VWAP
-   Conflicting signals

------------------------------------------------------------------------

# Position Sizing

Risk per trade

-   1% of account

Daily max loss

-   3%

Maximum consecutive losses

-   3

Maximum open positions

-   1

------------------------------------------------------------------------

# Trade Management

Initial SL

-   Swing Low (CE)
-   Swing High (PE)
-   Or ATR based

Trail

-   After 1R move
-   Trail behind EMA9 or VWAP

Exit

-   Target
-   SL
-   Spot loses VWAP
-   Momentum fades

------------------------------------------------------------------------

# Scan Frequency

Every 5 seconds

Heavy calculations

Every 15--30 seconds

Regime refresh

Every 1 minute

------------------------------------------------------------------------

# Trade Decision (Pseudo Logic)

``` text
Read Market

↓

Detect Regime

↓

Select Strategy

↓

Calculate Confidence

↓

Confidence >= 75 ?

    No → No Trade

    Yes

↓

Validate Risk

↓

Execute Order

↓

Manage Position

↓

Exit
```

------------------------------------------------------------------------

# Core Principles

1.  Spot drives the trade; option confirms it.
2.  VWAP is a market filter, not a standalone trigger.
3.  Trade only when the selected strategy matches the current regime.
4.  Never force a trade---"No Trade" is a valid outcome.
5.  Prioritize capital preservation over trade frequency.
