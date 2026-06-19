import os
import requests
from dotenv import load_dotenv
from typing import Dict, Any, Optional

load_dotenv()

CMC_API_KEY = os.getenv("CMC_API_KEY")
BASE_URL = "https://pro-api.coinmarketcap.com/v1"
HEADERS = {"X-CMC_PRO_API_KEY": CMC_API_KEY, "Accept": "application/json"}

def get_token_info(symbol: str = "ZEC") -> Optional[Dict[str, Any]]:
    """Get token price, volume, market cap, percent changes."""
    url = f"{BASE_URL}/cryptocurrency/quotes/latest"
    params = {"symbol": symbol.upper(), "convert": "USD"}
    
    try:
        r = requests.get(url, headers=HEADERS, params=params, timeout=10)
        r.raise_for_status()
        data = r.json().get("data", {})
        token = data.get(symbol.upper())
        if not token:
            print(f"Token {symbol} not found")
            return None
            
        quote = token.get("quote", {}).get("USD", {})
        return {
            "symbol": symbol.upper(),
            "price": quote.get("price", 0.0),
            "volume_24h": quote.get("volume_24h", 0.0),
            "market_cap": quote.get("market_cap", 0.0),
            "percent_change_1h": quote.get("percent_change_1h", 0.0),
            "percent_change_24h": quote.get("percent_change_24h", 0.0),
            "percent_change_7d": quote.get("percent_change_7d", 0.0),
            "volume_change_24h": quote.get("volume_change_24h", 0.0),
        }
    except Exception as e:
        print(f"[CMC] Error fetching {symbol}: {e}")
        return None

def get_global_metrics() -> Dict[str, Any]:
    """Get BTC dominance, total market cap, etc."""
    url = f"{BASE_URL}/global-metrics/quotes/latest"
    try:
        r = requests.get(url, headers=HEADERS, timeout=10)
        r.raise_for_status()
        data = r.json().get("data", {})
        quote = data.get("quote", {}).get("USD", {})
        return {
            "btc_dominance": data.get("btc_dominance", 50.0),
            "total_market_cap": quote.get("total_market_cap", 0.0),
            "total_volume_24h": quote.get("total_volume_24h", 0.0),
            "market_cap_change_24h": quote.get("total_market_cap_yesterday_percentage_change", 0.0),
        }
    except Exception as e:
        print(f"[CMC] Error fetching global metrics: {e}")
        return {}

def get_listings(limit: int = 50) -> list:
    """Get top tokens by volume for comparison."""
    url = f"{BASE_URL}/cryptocurrency/listings/latest"
    params = {"limit": limit, "convert": "USD", "sort": "volume_24h", "sort_dir": "desc"}
    try:
        r = requests.get(url, headers=HEADERS, params=params, timeout=10)
        r.raise_for_status()
        return r.json().get("data", [])
    except Exception as e:
        print(f"[CMC] Error fetching listings: {e}")
        return []
def get_historical_prices(symbol: str = "ZEC", days: int = 30) -> list:
    """
    Fetch historical daily prices for backtesting.
    Uses CMC historical OHLCV endpoint.
    """
    url = f"{BASE_URL}/cryptocurrency/ohlcv/historical"
    params = {
        "symbol": symbol.upper(),
        "convert": "USD",
        "time_period": "daily",
        "limit": days
    }
    
    try:
        r = requests.get(url, headers=HEADERS, params=params, timeout=15)
        r.raise_for_status()
        data = r.json().get("data", {}).get("quotes", [])
        
        historical = []
        for item in data:
            quote = item.get("quote", {}).get("USD", {})
            historical.append({
                "timestamp": item.get("timestamp"),
                "open": quote.get("open", 0),
                "high": quote.get("high", 0),
                "low": quote.get("low", 0),
                "close": quote.get("close", 0),
                "volume": quote.get("volume", 0),
                "price": quote.get("close", 0)  # For compatibility
            })
        return historical
    except Exception as e:
        print(f"[CMC] Historical data error: {e}")
        return []