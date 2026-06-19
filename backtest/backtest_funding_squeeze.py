"""
backtest_funding_squeeze.py
────────────────────────────
Walk-forward backtester for the Funding Squeeze Reversal skill.
Produces a backtestable spec output: per-trade log + summary metrics.

Usage:
  python backtest_funding_squeeze.py                  # Uses built-in synthetic dataset
  python backtest_funding_squeeze.py --data my.json   # Load real CMC data export

Output JSON shape (CMC Track 2 backtestable spec):
{
  "skill": "funding_squeeze_reversal",
  "backtest_spec": {
    "universe": [...],
    "signal_rules": {...},
    "execution_rules": {...},
    "risk_rules": {...}
  },
  "results": {
    "trades": [...],
    "metrics": {...}
  }
}
"""

import json
import math
import sys
import argparse
import random
from typing import List, Dict, Any
import os
import sys

# Forza Python ad aggiungere la root principale del progetto ai moduli di ricerca
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from signals.funding_squeeze import (
    FundingObservation,
    SqueezeSignal,
    evaluate_funding_squeeze,
    FUNDING_EXTREME_HIGH,
    FUNDING_EXTREME_LOW,
    OI_SURGE_THRESHOLD,
    OI_DROP_THRESHOLD,
    VOLUME_CONFIRM_RATIO,
    COOLDOWN_CANDLES,
)


# ─── Synthetic dataset generator (no API key required for demo) ───────────────

def generate_synthetic_dataset(
    symbol: str = "BNB",
    n_candles: int = 500,
    seed: int = 42,
) -> List[FundingObservation]:
    """
    Generates a realistic synthetic dataset of FundingObservations.
    Simulates:
      - Random walk price
      - Mean-reverting funding rate with periodic extremes
      - OI correlated with funding (with noise)
      - Volume spikes around squeeze events
    """
    random.seed(seed)
    obs_list = []
    price = 600.0          # BNB starting price
    funding = 0.0001       # Neutral start
    oi = 500_000_000.0     # $500M OI start
    base_ts = 1_750_000_000

    for i in range(n_candles):
        # Price: geometric random walk, ±1.5% per candle
        price *= (1 + random.gauss(0, 0.008))
        price = max(100.0, price)

        # Funding: mean-reverting AR(1) with periodic extreme spikes
        funding = 0.7 * funding + 0.3 * random.gauss(0.00005, 0.00015)
        if i % 40 == 0:   # Every ~40 candles inject an extreme
            funding = random.choice([
                random.uniform(0.0004, 0.0010),   # Extreme long
                random.uniform(-0.0006, -0.0003), # Extreme short
            ])

        # OI: correlated with funding, with noise
        oi_change = (funding / 0.001) * 0.05 + random.gauss(0, 0.03)
        oi_prev = oi
        oi = max(50_000_000, oi * (1 + oi_change))

        # Volume: spikes when |funding| is high
        vol_multiplier = 1 + abs(funding) / 0.0003 + random.uniform(0, 0.5)
        avg_hourly_vol = price * 200_000   # $200k/h baseline
        vol_1h = avg_hourly_vol * vol_multiplier

        obs_list.append(FundingObservation(
            timestamp=base_ts + i * 3600,
            symbol=symbol,
            price=round(price, 4),
            funding_rate=round(funding, 7),
            open_interest_usd=round(oi, 0),
            open_interest_prev_usd=round(oi_prev, 0),
            volume_1h_usd=round(vol_1h, 0),
            volume_24h_avg_usd=round(avg_hourly_vol, 0),
            price_change_pct_1h=round((price / obs_list[-1].price - 1) if obs_list else 0, 5),
            candle_index=i,
        ))

    return obs_list


# ─── Simple trade simulator ───────────────────────────────────────────────────

def simulate_trade(
    signal: SqueezeSignal,
    future_candles: List[FundingObservation],
    max_hold_candles: int = 12,
) -> Dict[str, Any]:
    """
    Walks forward through future_candles to determine trade outcome.
    Closes on: TP1 hit, TP2 hit, stop hit, or max hold expiry.
    Returns trade result dict.
    """
    entry = signal.entry_price
    sl    = signal.invalidation_price
    tp1   = signal.tp1_price
    tp2   = signal.tp2_price
    long  = signal.direction == "LONG"

    for j, c in enumerate(future_candles[:max_hold_candles]):
        price = c.price
        # Check stop
        if long and price <= sl:
            pnl_pct = (sl - entry) / entry
            return {"outcome": "STOP", "exit_price": sl, "pnl_pct": round(pnl_pct, 5), "hold_candles": j + 1}
        if not long and price >= sl:
            pnl_pct = (entry - sl) / entry
            return {"outcome": "STOP", "exit_price": sl, "pnl_pct": round(-pnl_pct, 5), "hold_candles": j + 1}
        # Check TP2 (full target)
        if long and price >= tp2:
            pnl_pct = (tp2 - entry) / entry
            return {"outcome": "TP2", "exit_price": tp2, "pnl_pct": round(pnl_pct, 5), "hold_candles": j + 1}
        if not long and price <= tp2:
            pnl_pct = (entry - tp2) / entry
            return {"outcome": "TP2", "exit_price": tp2, "pnl_pct": round(pnl_pct, 5), "hold_candles": j + 1}
        # Check TP1
        if long and price >= tp1:
            pnl_pct = (tp1 - entry) / entry
            return {"outcome": "TP1", "exit_price": tp1, "pnl_pct": round(pnl_pct, 5), "hold_candles": j + 1}
        if not long and price <= tp1:
            pnl_pct = (entry - tp1) / entry
            return {"outcome": "TP1", "exit_price": tp1, "pnl_pct": round(pnl_pct, 5), "hold_candles": j + 1}

    # Time exit
    exit_price = future_candles[min(max_hold_candles - 1, len(future_candles) - 1)].price
    pnl_pct = (exit_price - entry) / entry if long else (entry - exit_price) / entry
    return {"outcome": "TIME", "exit_price": round(exit_price, 4), "pnl_pct": round(pnl_pct, 5), "hold_candles": max_hold_candles}


# ─── Backtest runner ──────────────────────────────────────────────────────────

def run_backtest(
    obs_list: List[FundingObservation],
    min_confidence: float = 0.45,
    max_hold_candles: int = 12,
) -> Dict[str, Any]:
    trades = []
    last_signal_candle = -999

    for i, obs in enumerate(obs_list):
        signal = evaluate_funding_squeeze(obs, last_signal_candle=last_signal_candle)

        if signal.direction == "HOLD" or signal.confidence < min_confidence:
            continue

        # We have a signal — simulate trade on remaining candles
        future = obs_list[i + 1: i + 1 + max_hold_candles]
        if not future:
            break

        result = simulate_trade(signal, future, max_hold_candles)
        last_signal_candle = obs.candle_index

        trade = {
            "candle_index"  : i,
            "timestamp"     : obs.timestamp,
            "symbol"        : obs.symbol,
            "direction"     : signal.direction,
            "confidence"    : signal.confidence,
            "entry_price"   : signal.entry_price,
            "invalidation"  : signal.invalidation_price,
            "tp1"           : signal.tp1_price,
            "tp2"           : signal.tp2_price,
            "funding_rate"  : signal.funding_rate,
            "oi_delta_pct"  : round(signal.oi_delta_pct, 5),
            "volume_confirm": signal.volume_confirm,
            "reason"        : signal.reason,
            **result,
        }
        trades.append(trade)

    # ── Metrics ───────────────────────────────────────────────────────────────
    n = len(trades)
    if n == 0:
        return {"trades": [], "metrics": {"total_trades": 0}}

    wins      = [t for t in trades if t["pnl_pct"] > 0]
    losses    = [t for t in trades if t["pnl_pct"] <= 0]
    win_rate  = len(wins) / n
    avg_win   = sum(t["pnl_pct"] for t in wins) / len(wins) if wins else 0
    avg_loss  = sum(t["pnl_pct"] for t in losses) / len(losses) if losses else 0
    total_pnl = sum(t["pnl_pct"] for t in trades)
    expectancy = win_rate * avg_win + (1 - win_rate) * avg_loss

    # Equity curve for max drawdown
    equity = 1.0
    peak   = 1.0
    max_dd = 0.0
    for t in trades:
        equity *= (1 + t["pnl_pct"])
        peak    = max(peak, equity)
        dd      = (peak - equity) / peak
        max_dd  = max(max_dd, dd)

    by_outcome = {}
    for t in trades:
        by_outcome[t["outcome"]] = by_outcome.get(t["outcome"], 0) + 1

    metrics = {
        "total_trades"       : n,
        "win_rate"           : round(win_rate, 4),
        "avg_win_pct"        : round(avg_win * 100, 3),
        "avg_loss_pct"       : round(avg_loss * 100, 3),
        "profit_factor"      : round(abs(avg_win / avg_loss) if avg_loss else 0, 3),
        "expectancy_pct"     : round(expectancy * 100, 4),
        "total_pnl_pct"      : round(total_pnl * 100, 3),
        "final_equity_x"     : round(equity, 4),
        "max_drawdown_pct"   : round(max_dd * 100, 3),
        "by_outcome"         : by_outcome,
        "avg_hold_candles"   : round(sum(t["hold_candles"] for t in trades) / n, 1),
        "long_trades"        : sum(1 for t in trades if t["direction"] == "LONG"),
        "short_trades"       : sum(1 for t in trades if t["direction"] == "SHORT"),
    }

    return {"trades": trades, "metrics": metrics}


# ─── Backtestable spec builder ────────────────────────────────────────────────

def build_backtestable_spec(metrics: Dict, trades: List) -> Dict[str, Any]:
    """Produces the CMC Track 2 required backtestable spec."""
    return {
        "skill": "funding_squeeze_reversal",
        "version": "1.0.0",
        "author": "quantum-bro / beatp-bit",
        "description": (
            "Mean-reversion strategy triggered by extreme perpetual funding rates "
            "confirmed by OI divergence and volume. Targets de-levering squeezes "
            "in over-crowded directional positions. Backtestable spec — not a live agent."
        ),
        "backtest_spec": {
            "universe": {
                "asset_class": "crypto_perpetuals",
                "symbols": ["BNB", "BTC", "ETH", "SOL", "ARB"],
                "timeframe": "1h",
                "data_source": "CMC Agent Hub pre-computed indicators",
                "required_fields": [
                    "funding_rate_8h",
                    "open_interest_usd",
                    "open_interest_prev_usd",
                    "volume_1h_usd",
                    "volume_24h_avg_usd",
                    "price",
                ]
            },
            "signal_rules": {
                "entry_conditions": {
                    "SHORT": [
                        f"funding_rate > {FUNDING_EXTREME_HIGH} (over-long crowd)",
                        f"oi_delta_pct <= 0 OR oi_delta_pct < {OI_SURGE_THRESHOLD} (OI not surging)",
                        f"volume_1h >= volume_24h_avg × {VOLUME_CONFIRM_RATIO} (optional, boosts confidence)",
                        f"cooldown: no signal in last {COOLDOWN_CANDLES} candles",
                    ],
                    "LONG": [
                        f"funding_rate < {FUNDING_EXTREME_LOW} (over-short crowd)",
                        f"oi_delta_pct >= 0 OR oi_delta_pct > {OI_DROP_THRESHOLD} (OI not collapsing)",
                        f"volume_1h >= volume_24h_avg × {VOLUME_CONFIRM_RATIO} (optional, boosts confidence)",
                        f"cooldown: no signal in last {COOLDOWN_CANDLES} candles",
                    ]
                },
                "abort_conditions": [
                    "SHORT: OI surging >8% while funding extreme → momentum, skip",
                    "LONG: OI collapsing >6% while funding extreme → capitulation, skip",
                ],
                "confidence_model": {
                    "funding_extremity_weight": 0.40,
                    "oi_divergence_weight"    : 0.35,
                    "volume_confirm_weight"   : 0.25,
                    "min_confidence_to_trade" : 0.45,
                }
            },
            "execution_rules": {
                "entry"       : "Market at candle close on signal",
                "tp1"         : "1.5× ATR proxy from entry",
                "tp2"         : "3.0× ATR proxy from entry",
                "stop"        : "1.0× ATR proxy (opposite side)",
                "max_hold"    : "12 candles (12 hours at 1h TF)",
                "time_exit"   : "Close at market if neither TP nor SL hit",
                "atr_proxy"   : "abs(funding_rate) × 80, clamped 0.5%–3% of price",
            },
            "risk_rules": {
                "position_sizing": "Fixed fractional — 1% account risk per trade",
                "max_concurrent" : 1,
                "per_symbol_max" : 1,
                "cooldown_candles": COOLDOWN_CANDLES,
            }
        },
        "backtest_results_summary": metrics,
        "sample_trades_count": len(trades),
        "composability_interface": "CMC Agent Hub Skills Marketplace / BNB AI Agent SDK",
    }


# ─── Main ─────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="Funding Squeeze Reversal Backtester")
    parser.add_argument("--data",        type=str,   default=None,  help="Path to JSON file with FundingObservation list")
    parser.add_argument("--symbol",      type=str,   default="BNB", help="Symbol for synthetic data")
    parser.add_argument("--candles",     type=int,   default=500,   help="Number of synthetic candles")
    parser.add_argument("--confidence",  type=float, default=0.45,  help="Minimum confidence to trade")
    parser.add_argument("--max-hold",    type=int,   default=12,    help="Max candles to hold a trade")
    parser.add_argument("--seed",        type=int,   default=42,    help="Random seed for synthetic data")
    parser.add_argument("--out",         type=str,   default=None,  help="Write result JSON to file")
    args = parser.parse_args()

    # ── Load or generate data ─────────────────────────────────────────────────
    if args.data:
        with open(args.data) as f:
            raw = json.load(f)
        obs_list = [FundingObservation(**r) for r in raw]
        print(f"📂 Loaded {len(obs_list)} candles from {args.data}")
    else:
        print(f"🔧 Generating {args.candles} synthetic candles for {args.symbol}…")
        obs_list = generate_synthetic_dataset(args.symbol, args.candles, args.seed)

    # ── Run backtest ──────────────────────────────────────────────────────────
    print(f"⚙️  Running backtest (min_confidence={args.confidence}, max_hold={args.max_hold}h)…")
    bt = run_backtest(obs_list, min_confidence=args.confidence, max_hold_candles=args.max_hold)

    # ── Build full spec ───────────────────────────────────────────────────────
    spec = build_backtestable_spec(bt["metrics"], bt["trades"])

    # ── Output ────────────────────────────────────────────────────────────────
    output = {**spec, "all_trades": bt["trades"]}

    if args.out:
        with open(args.out, "w") as f:
            json.dump(output, f, indent=2)
        print(f"\n✅ Full spec + trades written to: {args.out}")
    else:
        print("\n" + "─" * 60)
        print("📊 BACKTEST METRICS")
        print("─" * 60)
        m = bt["metrics"]
        print(f"  Total Trades   : {m['total_trades']}")
        print(f"  Win Rate       : {m['win_rate']:.1%}")
        print(f"  Avg Win        : +{m['avg_win_pct']:.2f}%")
        print(f"  Avg Loss       : {m['avg_loss_pct']:.2f}%")
        print(f"  Profit Factor  : {m['profit_factor']:.2f}")
        print(f"  Expectancy     : {m['expectancy_pct']:.3f}%/trade")
        print(f"  Total PnL      : {m['total_pnl_pct']:.2f}%")
        print(f"  Final Equity   : {m['final_equity_x']:.4f}×")
        print(f"  Max Drawdown   : {m['max_drawdown_pct']:.2f}%")
        print(f"  Avg Hold       : {m['avg_hold_candles']:.1f} candles")
        print(f"  Outcomes       : {m['by_outcome']}")
        print(f"  LONG / SHORT   : {m['long_trades']} / {m['short_trades']}")
        print("─" * 60)
        print("\n📡 BACKTESTABLE SPEC (CMC Track 2 format):")
        spec_only = {k: v for k, v in output.items() if k != "all_trades"}
        print(json.dumps(spec_only, indent=2))


if __name__ == "__main__":
    main()
