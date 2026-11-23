"""Fetch price history + options chains and compute indicators.

Writes a JSON snapshot to data/snapshots/<date>.json that Claude reads
when it makes its CALL / PUT / NO TRADE decision.

Usage:  python -m bot.fetch_data
"""
from __future__ import annotations

import json
import math
from datetime import date, datetime, timezone
from pathlib import Path

import numpy as np
import pandas as pd
import yfinance as yf

from bot import twelvedata

ROOT = Path(__file__).resolve().parent.parent
CONFIG = json.loads((ROOT / "config.json").read_text())
SNAP_DIR = ROOT / "data" / "snapshots"


def _f(x) -> float | None:
    """Float that is JSON-safe (NaN/inf -> None)."""
    try:
        x = float(x)
    except (TypeError, ValueError):
        return None
    return None if math.isnan(x) or math.isinf(x) else round(x, 4)


# ---------------------------------------------------------------- indicators
def rsi(close: pd.Series, n: int = 14) -> pd.Series:
    delta = close.diff()
    gain = delta.clip(lower=0).ewm(alpha=1 / n, adjust=False).mean()
    loss = (-delta.clip(upper=0)).ewm(alpha=1 / n, adjust=False).mean()
    return 100 - 100 / (1 + gain / loss)


def macd(close: pd.Series):
    fast = close.ewm(span=12, adjust=False).mean()
    slow = close.ewm(span=26, adjust=False).mean()
    line = fast - slow
    signal = line.ewm(span=9, adjust=False).mean()
    return line, signal, line - signal


def atr(df: pd.DataFrame, n: int = 14) -> pd.Series:
    prev = df["Close"].shift()
    tr = pd.concat(
        [df["High"] - df["Low"], (df["High"] - prev).abs(), (df["Low"] - prev).abs()],
        axis=1,
    ).max(axis=1)
    return tr.rolling(n).mean()


def technicals(hist: pd.DataFrame) -> dict:
    c = hist["Close"]
    last = c.iloc[-1]
    sma20, sma50, sma200 = (c.rolling(n).mean().iloc[-1] for n in (20, 50, 200))
    std20 = c.rolling(20).std().iloc[-1]
    m_line, m_sig, m_hist = macd(c)
    rets = np.log(c / c.shift())
    rv20 = rets.rolling(20).std().iloc[-1] * math.sqrt(252)
    vol_ratio = hist["Volume"].iloc[-1] / hist["Volume"].rolling(20).mean().iloc[-1]
    return {
        "price": _f(last),
        "change_1d_pct": _f((last / c.iloc[-2] - 1) * 100),
        "change_5d_pct": _f((last / c.iloc[-6] - 1) * 100),
        "change_20d_pct": _f((last / c.iloc[-21] - 1) * 100),
        "sma20": _f(sma20),
        "sma50": _f(sma50),
        "sma200": _f(sma200),
        "above_sma20": bool(last > sma20),
        "above_sma50": bool(last > sma50),
        "above_sma200": bool(last > sma200) if not math.isnan(sma200) else None,
        "rsi14": _f(rsi(c).iloc[-1]),
        "macd": _f(m_line.iloc[-1]),
        "macd_signal": _f(m_sig.iloc[-1]),
        "macd_hist": _f(m_hist.iloc[-1]),
        "macd_hist_prev": _f(m_hist.iloc[-2]),
        "bollinger_pct_b": _f((last - (sma20 - 2 * std20)) / (4 * std20)),
        "atr14": _f(atr(hist).iloc[-1]),
        "realized_vol_20d": _f(rv20),
        "volume_vs_20d_avg": _f(vol_ratio),
        "high_52w": _f(c.tail(252).max()),
        "low_52w": _f(c.tail(252).min()),
    }


# ---------------------------------------------------------------- options
def pick_expiry(expiries: list[str]) -> str | None:
    today = date.today()
    lo, hi = CONFIG["min_days_to_expiry"], CONFIG["max_days_to_expiry"]
    for e in expiries:
        dte = (datetime.strptime(e, "%Y-%m-%d").date() - today).days
        if lo <= dte <= hi:
            return e
    return expiries[-1] if expiries else None


def max_pain(calls: pd.DataFrame, puts: pd.DataFrame) -> float | None:
    strikes = sorted(set(calls["strike"]) | set(puts["strike"]))
    if not strikes:
        return None
    best, best_pain = None, float("inf")
    for s in strikes:
        pain = ((s - calls["strike"]).clip(lower=0) * calls["openInterest"].fillna(0)).sum()
        pain += ((puts["strike"] - s).clip(lower=0) * puts["openInterest"].fillna(0)).sum()
        if pain < best_pain:
            best, best_pain = s, pain
    return best


def options_summary(tk: yf.Ticker, spot: float) -> dict:
    expiries = list(tk.options or [])
    expiry = pick_expiry(expiries)
    if not expiry:
        return {"error": "no options data"}
    chain = tk.option_chain(expiry)
    calls, puts = chain.calls, chain.puts
    dte = (datetime.strptime(expiry, "%Y-%m-%d").date() - date.today()).days

    def atm(df):
        return df.iloc[(df["strike"] - spot).abs().argsort()[:1]].iloc[0]

    atm_c, atm_p = atm(calls), atm(puts)
    atm_iv = np.nanmean([atm_c["impliedVolatility"], atm_p["impliedVolatility"]])
    straddle = (atm_c["lastPrice"] or 0) + (atm_p["lastPrice"] or 0)

    # 25-delta-ish skew proxy: IV of ~5% OTM put vs ~5% OTM call
    otm_put = puts.iloc[(puts["strike"] - spot * 0.95).abs().argsort()[:1]].iloc[0]
    otm_call = calls.iloc[(calls["strike"] - spot * 1.05).abs().argsort()[:1]].iloc[0]

    cv, pv = calls["volume"].fillna(0).sum(), puts["volume"].fillna(0).sum()
    coi, poi = calls["openInterest"].fillna(0).sum(), puts["openInterest"].fillna(0).sum()

    # Unusual activity: contracts where today's volume > open interest
    def unusual(df, kind):
        d = df[(df["volume"].fillna(0) > df["openInterest"].fillna(0)) & (df["volume"].fillna(0) > 500)]
        return [
            {"type": kind, "strike": _f(r.strike), "volume": int(r.volume), "oi": int(r.openInterest or 0)}
            for r in d.nlargest(3, "volume").itertuples()
        ]

    return {
        "expiry": expiry,
        "days_to_expiry": dte,
        "all_expiries": expiries[:12],
        "atm_strike": _f(atm_c["strike"]),
        "atm_iv": _f(atm_iv),
        "atm_call_price": _f(atm_c["lastPrice"]),
        "atm_put_price": _f(atm_p["lastPrice"]),
        "expected_move_dollars": _f(straddle),
        "expected_move_pct": _f(straddle / spot * 100),
        "put_call_volume_ratio": _f(pv / cv) if cv else None,
        "put_call_oi_ratio": _f(poi / coi) if coi else None,
        "skew_otm_put_minus_call_iv": _f(otm_put["impliedVolatility"] - otm_call["impliedVolatility"]),
        "max_pain": _f(max_pain(calls, puts)),
        "unusual_activity": unusual(calls, "call") + unusual(puts, "put"),
    }


# ---------------------------------------------------------------- main
def price_history(symbol: str) -> tuple[pd.DataFrame, str]:
    """Twelve Data first (official feed), Yahoo as fallback."""
    try:
        return twelvedata.daily_history(symbol), "twelvedata"
    except Exception as e:
        print(f"  Twelve Data failed for {symbol} ({e}); falling back to Yahoo")
    return yf.Ticker(symbol).history(period="1y", auto_adjust=True), "yahoo"


def snapshot_ticker(symbol: str) -> dict:
    tk = yf.Ticker(symbol)
    hist, source = price_history(symbol)
    if hist.empty:
        return {"symbol": symbol, "error": "no price history"}
    tech = technicals(hist)
    out = {"symbol": symbol, "price_source": source, "technicals": tech}
    try:
        out["options"] = options_summary(tk, tech["price"])
        if out["options"].get("atm_iv") and tech["realized_vol_20d"]:
            out["options"]["iv_to_realized_vol"] = _f(out["options"]["atm_iv"] / tech["realized_vol_20d"])
    except Exception as e:  # options endpoints are flaky; keep going
        out["options"] = {"error": str(e)}
    try:
        cal = tk.calendar or {}
        ed = cal.get("Earnings Date") if isinstance(cal, dict) else None
        out["next_earnings"] = [str(d) for d in ed] if ed else None
    except Exception:
        out["next_earnings"] = None
    try:
        out["recent_headlines"] = [
            (n.get("content") or n).get("title") for n in (tk.news or [])[:6]
        ]
    except Exception:
        out["recent_headlines"] = []
    return out


def main() -> Path:
    SNAP_DIR.mkdir(parents=True, exist_ok=True)
    snap = {
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "market_context": {},
        "tickers": {},
    }
    for idx in ("SPY", "QQQ", "^VIX"):
        try:
            h = yf.Ticker(idx).history(period="3mo") if idx.startswith("^") else price_history(idx)[0]
            c = h["Close"]
            snap["market_context"][idx] = {
                "last": _f(c.iloc[-1]),
                "change_5d_pct": _f((c.iloc[-1] / c.iloc[-6] - 1) * 100),
                "above_sma50": bool(c.iloc[-1] > c.rolling(50).mean().iloc[-1]),
            }
        except Exception as e:
            snap["market_context"][idx] = {"error": str(e)}
    for sym in CONFIG["tickers"]:
        print(f"fetching {sym} ...")
        try:
            snap["tickers"][sym] = snapshot_ticker(sym)
        except Exception as e:
            snap["tickers"][sym] = {"symbol": sym, "error": str(e)}

    from bot.signals import score_all  # local import to avoid cycle

    snap["quant_signals"] = score_all(snap)
    snap["price_sources"] = {s: t.get("price_source") for s, t in snap["tickers"].items()}
    path = SNAP_DIR / f"{date.today().isoformat()}.json"
    path.write_text(json.dumps(snap, indent=2))
    print(f"wrote {path}")
    print(json.dumps(snap["quant_signals"], indent=2))
    return path


if __name__ == "__main__":
    main()
