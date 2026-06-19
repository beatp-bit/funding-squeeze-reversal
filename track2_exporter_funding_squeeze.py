"""
track2_exporter_funding_squeeze.py
────────────────────────────────────
CMC Agent Hub — Track 2 Strategy Skill Exporter
Skill name: funding_squeeze_reversal

This is the programmatic endpoint that the CMC Skills Marketplace calls
via execute_skill. It accepts a FundingObservation payload and returns
a standardized signal vector.

Usage (terminal):
  python track2_exporter_funding_squeeze.py '{"token_target": "BNB", "preview": true}'
  python track2_exporter_funding_squeeze.py BNB

Usage (CMC execute_skill):
  The hub POSTs { "token_target": "BNB", "funding_rate": 0.0008, ... }
  and receives the standardized JSON output.

Standalone mode (preview=true, no live data):
  Returns a demo signal using synthetic extremes, so judges can test
  the skill interface without a CMC API key.
"""

import sys
import json
import time
import math
import os

# ── Path fix so this file can be called from repo root ───────────────────────
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from signals.funding_squeeze import (
    FundingObservation,
    evaluate_funding_squeeze,
    FUNDING_EXTREME_HIGH,
    FUNDING_EXTREME_LOW,
)


# ─── Skill manifest (find_skill schema) ──────────────────────────────────────

SKILL_MANIFEST = {
    "unique_name": "funding_squeeze_reversal",
    "display_name": "Funding Rate Squeeze Reversal",
    "version": "1.0.0",
    "author": "quantum-bro / beatp-bit",
    "track": "Track 2 — Strategy Skills",
    "description": (
        "Detects mean-reversion setups in perpetual futures markets where "
        "extreme funding rates signal an over-crowded directional position. "
        "Confirms via OI divergence and volume. Returns a backtestable signal "
        "vector with entry, stop, and two take-profit levels. "
        "Use it before taking a contrarian perp position, not as a trend-follow tool. "
        "Limits: requires funding_rate, OI, and volume inputs; returns HOLD if data "
        "is unavailable or conditions are neutral."
    ),
    "tags": ["derivatives", "funding-rate", "mean-reversion", "perp", "risk", "intraday"],
    "data_requirements": [
        "funding_rate_8h",
        "open_interest_usd",
        "open_interest_prev_usd",
        "volume_1h_usd",
        "volume_24h_avg_usd",
        "price_usd",
    ],
    "input_schema": {
        "type": "object",
        "properties": {
            "token_target": {
                "type": "string",
                "description": "Target cryptocurrency ticker (e.g. BNB, BTC, ETH)",
                "default": "BNB"
            },
            "preview": {
                "type": "boolean",
                "description": "If true, returns a demo signal with synthetic extreme data",
                "default": True
            },
            "funding_rate": {
                "type": "number",
                "description": "Current 8h funding rate (decimal). Required if preview=false."
            },
            "open_interest_usd": {
                "type": "number",
                "description": "Current open interest in USD. Required if preview=false."
            },
            "open_interest_prev_usd": {
                "type": "number",
                "description": "Previous-window OI in USD. Required if preview=false."
            },
            "volume_1h_usd": {
                "type": "number",
                "description": "1h rolling trading volume in USD. Required if preview=false."
            },
            "volume_24h_avg_usd": {
                "type": "number",
                "description": "Average hourly volume (24h volume / 24). Required if preview=false."
            },
            "price_usd": {
                "type": "number",
                "description": "Current spot/perp price in USD. Required if preview=false."
            },
        },
        "required": ["token_target"]
    },
    "output_schema": {
        "type": "object",
        "properties": {
            "status"   : {"type": "string"},
            "signal"   : {"type": "string", "enum": ["LONG", "SHORT", "HOLD"]},
            "confidence": {"type": "number"},
            "entry_price": {"type": "number"},
            "invalidation_price": {"type": "number"},
            "tp1_price": {"type": "number"},
            "tp2_price": {"type": "number"},
            "reason"   : {"type": "string"},
            "meta"     : {"type": "object"},
        }
    },
    "composability_interface": "CMC Agent Hub Skills Marketplace / BNB AI Agent SDK",
}


# ─── Preview fixtures (synthetic extremes for demo/testing) ───────────────────

PREVIEW_FIXTURES = {
    "BNB": {
        "price_usd"             : 645.0,
        "funding_rate"          : 0.00065,   # Very high → SHORT squeeze candidate
        "open_interest_usd"     : 490_000_000,
        "open_interest_prev_usd": 510_000_000,  # OI falling → confirms squeeze
        "volume_1h_usd"         : 380_000_000,
        "volume_24h_avg_usd"    : 220_000_000,
    },
    "BTC": {
        "price_usd"             : 107_500.0,
        "funding_rate"          : -0.00045,  # Very negative → LONG squeeze candidate
        "open_interest_usd"     : 18_500_000_000,
        "open_interest_prev_usd": 18_000_000_000,
        "volume_1h_usd"         : 4_200_000_000,
        "volume_24h_avg_usd"    : 2_800_000_000,
    },
    "ETH": {
        "price_usd"             : 2_690.0,
        "funding_rate"          : 0.00012,   # Neutral
        "open_interest_usd"     : 8_800_000_000,
        "open_interest_prev_usd": 8_750_000_000,
        "volume_1h_usd"         : 1_100_000_000,
        "volume_24h_avg_usd"    : 1_200_000_000,
    },
}

DEFAULT_FIXTURE = {
    "price_usd"             : 100.0,
    "funding_rate"          : 0.00055,
    "open_interest_usd"     : 100_000_000,
    "open_interest_prev_usd": 108_000_000,
    "volume_1h_usd"         : 80_000_000,
    "volume_24h_avg_usd"    : 40_000_000,
}


# ─── Skill executor ───────────────────────────────────────────────────────────

def execute_skill(parameters: dict) -> dict:
    """
    Main CMC execute_skill handler.
    Validates inputs, runs signal logic, returns standardized payload.
    """
    token   = parameters.get("token_target", "BNB").upper()
    preview = parameters.get("preview", True)

    # ── Data sourcing ─────────────────────────────────────────────────────────
    if preview:
        fixture = PREVIEW_FIXTURES.get(token, DEFAULT_FIXTURE)
        data    = {**fixture}
        data_source = "preview_synthetic"
    else:
        required = ["funding_rate", "open_interest_usd", "open_interest_prev_usd",
                    "volume_1h_usd", "volume_24h_avg_usd", "price_usd"]
        missing = [k for k in required if k not in parameters]
        if missing:
            return {
                "status" : "error",
                "message": f"Missing required fields for live mode: {missing}",
                "hint"   : "Pass preview=true to test with synthetic data, or supply all required fields.",
                "skill"  : SKILL_MANIFEST["unique_name"],
            }
        data = {k: parameters[k] for k in required}
        data_source = "caller_supplied"

    # ── Build observation ─────────────────────────────────────────────────────
    obs = FundingObservation(
        timestamp              = int(time.time()),
        symbol                 = token,
        price                  = data["price_usd"],
        funding_rate           = data["funding_rate"],
        open_interest_usd      = data["open_interest_usd"],
        open_interest_prev_usd = data["open_interest_prev_usd"],
        volume_1h_usd          = data["volume_1h_usd"],
        volume_24h_avg_usd     = data["volume_24h_avg_usd"],
        price_change_pct_1h    = 0.0,
        candle_index           = 0,
    )

    # ── Evaluate signal ───────────────────────────────────────────────────────
    sig = evaluate_funding_squeeze(obs)

    # ── Build output ──────────────────────────────────────────────────────────
    oi_delta = (obs.open_interest_usd - obs.open_interest_prev_usd) / obs.open_interest_prev_usd if obs.open_interest_prev_usd else 0

    return {
        "status"            : "success",
        "skill"             : SKILL_MANIFEST["unique_name"],
        "version"           : SKILL_MANIFEST["version"],
        "timestamp"         : obs.timestamp,
        "data_source"       : data_source,
        "token_target"      : token,
        "signal"            : sig.direction,
        "confidence"        : sig.confidence,
        "entry_price"       : sig.entry_price,
        "invalidation_price": sig.invalidation_price,
        "tp1_price"         : sig.tp1_price,
        "tp2_price"         : sig.tp2_price,
        "reason"            : sig.reason,
        "meta": {
            "funding_rate"      : obs.funding_rate,
            "oi_delta_pct"      : round(oi_delta, 5),
            "volume_confirm"    : sig.volume_confirm,
            "raw_score"         : sig.raw_score,
            "threshold_long"    : FUNDING_EXTREME_LOW,
            "threshold_short"   : FUNDING_EXTREME_HIGH,
            "data_source"       : data_source,
        },
        "composability_interface": SKILL_MANIFEST["composability_interface"],
    }


# ─── CLI entry point ──────────────────────────────────────────────────────────

if __name__ == "__main__":
    default_params = {"token_target": "BNB", "preview": True}

    if len(sys.argv) > 1:
        try:
            user_input = json.loads(sys.argv[1])
            if isinstance(user_input, dict):
                default_params.update(user_input)
        except json.JSONDecodeError:
            default_params["token_target"] = sys.argv[1].upper()

    print("─" * 60)
    print("⚡ QUANTUM BRO — Funding Squeeze Reversal Skill")
    print(f"   Track 2 Strategy Skill | Submission #3")
    print("─" * 60)
    print(f"📥 Input parameters: {json.dumps(default_params)}")

    result = execute_skill(default_params)

    print(f"\n📡 [Standardized Skill Payload]:")
    print(json.dumps(result, indent=2))

    direction = result.get("signal", "HOLD")
    conf      = result.get("confidence", 0)
    emoji     = {"LONG": "🟢", "SHORT": "🔴", "HOLD": "⚪"}.get(direction, "⚪")
    print(f"\n{emoji} Signal: {direction} | Confidence: {conf:.0%}")
    print("✅ Skill execution complete. Return code 0.")
