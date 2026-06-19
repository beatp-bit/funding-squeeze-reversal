"""
funding_squeeze.py
──────────────────
Funding Rate Squeeze Reversal Signal Engine
Part of QUANTUM BRO — Track 2 Strategy Skill Submission #3

Logic:
  - Extreme positive funding  → market is over-long → mean-reversion SHORT signal
  - Extreme negative funding  → market is over-short → mean-reversion LONG signal
  - OI divergence check       → confirms or weakens the signal
  - Cooldown filter           → avoids re-entry after a recent squeeze event

CMC Data inputs used:
  - /v1/cryptocurrency/quotes/latest    (price, volume_24h)
  - /v1/global-metrics/quotes/latest    (btc_dominance, total_market_cap)
  - Funding rate & OI sourced via CMC Agent Hub pre-computed indicators
"""

from dataclasses import dataclass, field
from typing import Optional
import math


# ─── Thresholds (tunable for backtest grid search) ───────────────────────────

FUNDING_EXTREME_HIGH   = 0.0003   # +0.03% per 8h → crowd is over-long
FUNDING_EXTREME_LOW    = -0.0002  # −0.02% per 8h → crowd is over-short
OI_SURGE_THRESHOLD     = 0.08     # OI increased >8% while price flat/down
OI_DROP_THRESHOLD      = -0.06    # OI dropped >6% while price flat/up
VOLUME_CONFIRM_RATIO   = 1.4      # Current 1h vol must be ≥1.4× 24h avg hourly
COOLDOWN_CANDLES       = 6        # Skip signal if last squeeze < 6 candles ago


# ─── Data Containers ─────────────────────────────────────────────────────────

@dataclass
class FundingObservation:
    """Single snapshot of perp market state from CMC pre-computed indicators."""
    timestamp: int                    # Unix epoch (seconds)
    symbol: str                       # e.g. "BNB", "BTC"
    price: float                      # Spot mid price (USD)
    funding_rate: float               # Current 8h funding rate (decimal, e.g. 0.0001)
    open_interest_usd: float          # Total OI in USD
    open_interest_prev_usd: float     # OI from previous window (for delta)
    volume_1h_usd: float              # 1h rolling volume USD
    volume_24h_avg_usd: float         # 24h volume / 24 (average hourly USD)
    price_change_pct_1h: float        # % price change in last 1h
    candle_index: int = 0             # Used by backtester to track sequence


@dataclass
class SqueezeSignal:
    """Output signal produced by evaluate_funding_squeeze()."""
    timestamp: int
    symbol: str
    direction: str                    # "LONG", "SHORT", "HOLD"
    confidence: float                 # 0.0 – 1.0
    reason: str
    entry_price: float
    invalidation_price: float         # Hard stop level
    tp1_price: float                  # First target (1.5× ATR proxy)
    tp2_price: float                  # Second target (3× ATR proxy)
    funding_rate: float
    oi_delta_pct: float
    volume_confirm: bool
    raw_score: float                  # Composite pre-normalization score


# ─── ATR Proxy (no OHLC needed — uses price volatility estimate) ──────────────

def estimate_atr_proxy(price: float, funding_rate: float) -> float:
    """
    Lightweight ATR proxy for stop/target placement.
    Uses absolute funding rate as a volatility proxy:
      higher |funding| → more volatile regime → wider stops.
    Clamps between 0.5% and 3% of price.
    """
    vol_factor = max(0.005, min(0.03, abs(funding_rate) * 80))
    return price * vol_factor


# ─── OI Delta Calculation ─────────────────────────────────────────────────────

def oi_delta_pct(obs: FundingObservation) -> float:
    if obs.open_interest_prev_usd == 0:
        return 0.0
    return (obs.open_interest_usd - obs.open_interest_prev_usd) / obs.open_interest_prev_usd


# ─── Volume Confirmation ──────────────────────────────────────────────────────

def volume_is_confirming(obs: FundingObservation) -> bool:
    if obs.volume_24h_avg_usd == 0:
        return False
    return obs.volume_1h_usd >= obs.volume_24h_avg_usd * VOLUME_CONFIRM_RATIO


# ─── Core Signal Evaluator ────────────────────────────────────────────────────

def evaluate_funding_squeeze(
    obs: FundingObservation,
    last_signal_candle: int = -999,
) -> SqueezeSignal:
    """
    Evaluates a single FundingObservation and returns a SqueezeSignal.

    Decision matrix:
    ┌─────────────────────────────┬───────────────────┬──────────────────────┐
    │ Condition                   │ OI confirmation   │ Direction            │
    ├─────────────────────────────┼───────────────────┼──────────────────────┤
    │ Funding > EXTREME_HIGH      │ OI surging        │ HOLD (momentum)      │
    │ Funding > EXTREME_HIGH      │ OI flat/dropping  │ SHORT (squeeze)      │
    │ Funding < EXTREME_LOW       │ OI surging        │ HOLD (momentum)      │
    │ Funding < EXTREME_LOW       │ OI flat/dropping  │ LONG (squeeze)       │
    │ Between thresholds          │ any               │ HOLD                 │
    └─────────────────────────────┴───────────────────┴──────────────────────┘

    Confidence is a composite of:
      - funding extremity  (40%)
      - OI divergence      (35%)
      - volume confirm     (25%)
    """

    fr      = obs.funding_rate
    oi_d    = oi_delta_pct(obs)
    vol_ok  = volume_is_confirming(obs)
    atr     = estimate_atr_proxy(obs.price, fr)
    candles_since_last = obs.candle_index - last_signal_candle

    # ── Cooldown guard ──────────────────────────────────────────────────────
    if candles_since_last < COOLDOWN_CANDLES:
        return SqueezeSignal(
            timestamp=obs.timestamp, symbol=obs.symbol,
            direction="HOLD", confidence=0.0,
            reason=f"Cooldown active ({candles_since_last}/{COOLDOWN_CANDLES} candles)",
            entry_price=obs.price, invalidation_price=obs.price,
            tp1_price=obs.price, tp2_price=obs.price,
            funding_rate=fr, oi_delta_pct=oi_d,
            volume_confirm=vol_ok, raw_score=0.0,
        )

    # ── Funding extremity score (0–1) ────────────────────────────────────────
    if fr >= FUNDING_EXTREME_HIGH:
        fund_score = min(1.0, (fr - FUNDING_EXTREME_HIGH) / FUNDING_EXTREME_HIGH)
        candidate  = "SHORT"
    elif fr <= FUNDING_EXTREME_LOW:
        fund_score = min(1.0, (FUNDING_EXTREME_LOW - fr) / abs(FUNDING_EXTREME_LOW))
        candidate  = "LONG"
    else:
        return SqueezeSignal(
            timestamp=obs.timestamp, symbol=obs.symbol,
            direction="HOLD", confidence=0.0,
            reason=f"Funding {fr:.5f} within neutral band",
            entry_price=obs.price, invalidation_price=obs.price,
            tp1_price=obs.price, tp2_price=obs.price,
            funding_rate=fr, oi_delta_pct=oi_d,
            volume_confirm=vol_ok, raw_score=0.0,
        )

    # ── OI divergence score (0–1) ────────────────────────────────────────────
    # For a SHORT squeeze: funding is high (longs crowded).
    #   → OI falling or flat = longs closing = squeeze confirmation
    #   → OI surging = longs still piling in = stay out
    if candidate == "SHORT":
        if oi_d <= 0:
            oi_score = min(1.0, abs(oi_d) / abs(OI_DROP_THRESHOLD))
        elif oi_d >= OI_SURGE_THRESHOLD:
            # Momentum, not squeeze — abort
            return SqueezeSignal(
                timestamp=obs.timestamp, symbol=obs.symbol,
                direction="HOLD", confidence=0.0,
                reason=f"OI surging ({oi_d:.2%}) while funding high — momentum, not squeeze",
                entry_price=obs.price, invalidation_price=obs.price,
                tp1_price=obs.price, tp2_price=obs.price,
                funding_rate=fr, oi_delta_pct=oi_d,
                volume_confirm=vol_ok, raw_score=0.0,
            )
        else:
            oi_score = 0.3   # Neutral OI — weak confirmation

    else:  # LONG squeeze: funding is very negative
        if oi_d >= 0:
            oi_score = min(1.0, oi_d / OI_SURGE_THRESHOLD)
        elif oi_d <= OI_DROP_THRESHOLD:
            return SqueezeSignal(
                timestamp=obs.timestamp, symbol=obs.symbol,
                direction="HOLD", confidence=0.0,
                reason=f"OI collapsing ({oi_d:.2%}) while funding low — capitulation, not reversal",
                entry_price=obs.price, invalidation_price=obs.price,
                tp1_price=obs.price, tp2_price=obs.price,
                funding_rate=fr, oi_delta_pct=oi_d,
                volume_confirm=vol_ok, raw_score=0.0,
            )
        else:
            oi_score = 0.3

    # ── Volume confirmation score ─────────────────────────────────────────────
    vol_score = 1.0 if vol_ok else 0.3

    # ── Composite confidence ──────────────────────────────────────────────────
    raw_score  = fund_score * 0.40 + oi_score * 0.35 + vol_score * 0.25
    confidence = round(min(1.0, raw_score), 4)

    # ── Price levels ──────────────────────────────────────────────────────────
    if candidate == "SHORT":
        invalidation = obs.price + atr * 1.0    # Stop above entry
        tp1          = obs.price - atr * 1.5
        tp2          = obs.price - atr * 3.0
    else:
        invalidation = obs.price - atr * 1.0
        tp1          = obs.price + atr * 1.5
        tp2          = obs.price + atr * 3.0

    reason = (
        f"Funding {fr:.5f} → {candidate} squeeze | "
        f"OI Δ {oi_d:+.2%} | "
        f"Vol confirm: {vol_ok} | "
        f"Confidence: {confidence:.2f}"
    )

    return SqueezeSignal(
        timestamp=obs.timestamp,
        symbol=obs.symbol,
        direction=candidate,
        confidence=confidence,
        reason=reason,
        entry_price=obs.price,
        invalidation_price=round(invalidation, 4),
        tp1_price=round(tp1, 4),
        tp2_price=round(tp2, 4),
        funding_rate=fr,
        oi_delta_pct=oi_d,
        volume_confirm=vol_ok,
        raw_score=round(raw_score, 4),
    )
