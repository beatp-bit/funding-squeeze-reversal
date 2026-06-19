# FUNDING SQUEEZE REVERSAL — AI Derivatives Mean-Reversion Skill

🟩 **Developed for the CoinMarketCap BNB HACK 2026**  
🎯 **Track Focus:** Track 2 — Strategy Skills  
⚡ **Core Strategy:** Derivatives Mean-Reversion & Liquidation Squeezes

FUNDING SQUEEZE REVERSAL is a standalone, production-ready AI Strategy Skill designed for the CoinMarketCap Marketplace. It identifies over-crowded, high-leverage positions via funding rates and Open Interest (OI) to generate actionable mean-reversion signals for trading bots.

---

## 🎯 Strategic Core Logic

Identifies market imbalances where extremes in funding rates suggest a high probability of a mean-reversion, utilizing a weighted formula (Funding, OI Divergence, Volume) with a minimum confidence threshold of 0.45.

### Confirmation & Risk Filters
- **Short Signals:** Funding `> +0.030%/8h` + Declining OI + High Volume.
- **Long Signals:** Funding `< -0.020%/8h` + Rising OI + High Volume.
- **Safety:** Skips entry if OI changes rapidly (`> +8%` or `<-6%`).

---

## ⛓️ Ecosystem & Implementation

Engineered to operate seamlessly with the **BNB AI Agent SDK** or **Trust Wallet Agent Kit (TWAK)** via standard JSON input (`find_skill`).

### `find_skill` Input Schema
```json
{
  "unique_name": "funding_squeeze_reversal",
  "description": "Detects high-probability mean-reversion setups...",
  "input_schema": {
    "type": "object",
    "properties": {
      "token_target": { "type": "string", "default": "BNB" },
      "preview": { "type": "boolean", "default": true }
    },
    "required": ["token_target"]
  }
}
```

---

## 🚀 Quick Start
```bash
# Preview Mode
python track2_exporter_funding_squeeze.py BNB

# Live Execution
python track2_exporter_funding_squeeze.py '{"token_target": "BNB", "preview": false}'

# Backtest
cd backtest && python backtest_funding_squeeze.py --symbol BNB --candles 500
```
