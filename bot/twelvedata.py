"""Twelve Data client (https://twelvedata.com) for official price history.

The API key is never stored in this repo. It is read from either:
  * the TWELVE_DATA_API_KEY environment variable, or
  * a Claude environment "network secret" for api.twelvedata.com, which the
    network proxy injects as the Authorization header (we send a placeholder
    that the proxy replaces).
If Twelve Data is unreachable or rejects the request, callers fall back to Yahoo.
"""
from __future__ import annotations

import os

import pandas as pd
import requests

BASE = "https://api.twelvedata.com"


class TwelveDataError(RuntimeError):
    pass


def _get(path: str, **params) -> dict:
    key = os.environ.get("TWELVE_DATA_API_KEY", "placeholder-replaced-by-proxy")
    r = requests.get(
        f"{BASE}/{path}",
        params=params,
        headers={"Authorization": f"apikey {key}"},
        timeout=30,
    )
    try:
        data = r.json()
    except ValueError:
        raise TwelveDataError(f"HTTP {r.status_code}: non-JSON response")
    if r.status_code != 200 or (isinstance(data, dict) and data.get("status") == "error"):
        raise TwelveDataError(f"HTTP {r.status_code}: {data.get('message', data)}")
    return data


def daily_history(symbol: str, days: int = 260) -> pd.DataFrame:
    """Daily OHLCV, oldest first, with the same columns yfinance returns."""
    data = _get("time_series", symbol=symbol, interval="1day", outputsize=days)
    values = data.get("values") or []
    if not values:
        raise TwelveDataError(f"no values for {symbol}")
    df = pd.DataFrame(values)
    df["datetime"] = pd.to_datetime(df["datetime"])
    df = df.set_index("datetime").sort_index()
    df = df.rename(columns=str.capitalize)[["Open", "High", "Low", "Close", "Volume"]]
    return df.apply(pd.to_numeric, errors="coerce")


def quote(symbol: str) -> dict:
    """Latest quote (real-time during market hours on plans that include it)."""
    return _get("quote", symbol=symbol)
