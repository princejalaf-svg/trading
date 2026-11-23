"""Rules-based baseline score. Claude uses this as ONE input, not the answer.

Score is in [-1, +1]: positive leans CALL, negative leans PUT.
Deliberately conservative: anything near zero is NO TRADE.
"""
from __future__ import annotations

import json
from pathlib import Path

CONFIG = json.loads((Path(__file__).resolve().parent.parent / "config.json").read_text())


def score_ticker(t: dict, market: dict) -> dict:
    tech, opt = t.get("technicals") or {}, t.get("options") or {}
    if not tech:
        return {"score": 0.0, "lean": "NO TRADE", "reasons": ["missing data"]}
    pts, reasons = [], []

    def add(w, why):
        pts.append(w)
        reasons.append(f"{'+' if w > 0 else ''}{w}: {why}")

    # Trend
    if tech.get("above_sma20") and tech.get("above_sma50"):
        add(0.15, "price above 20 & 50 SMA (uptrend)")
    elif tech.get("above_sma20") is False and tech.get("above_sma50") is False:
        add(-0.15, "price below 20 & 50 SMA (downtrend)")
    if tech.get("above_sma200") is True:
        add(0.05, "above 200 SMA")
    elif tech.get("above_sma200") is False:
        add(-0.05, "below 200 SMA")

    # Momentum
    h, hp = tech.get("macd_hist"), tech.get("macd_hist_prev")
    if h is not None and hp is not None:
        if h > 0 and h > hp:
            add(0.1, "MACD histogram positive and rising")
        elif h < 0 and h < hp:
            add(-0.1, "MACD histogram negative and falling")

    # Mean reversion extremes
    r = tech.get("rsi14")
    if r is not None:
        if r > 75:
            add(-0.15, f"RSI {r:.0f} overbought")
        elif r < 25:
            add(0.15, f"RSI {r:.0f} oversold")
        elif 50 < r < 70:
            add(0.05, f"RSI {r:.0f} bullish range")
        elif 30 < r < 50:
            add(-0.05, f"RSI {r:.0f} bearish range")

    # Options positioning (contrarian at extremes)
    pcr = opt.get("put_call_volume_ratio")
    if pcr is not None:
        if pcr > 1.3:
            add(0.05, f"P/C volume {pcr:.2f} very high (contrarian bullish)")
        elif pcr < 0.4:
            add(-0.05, f"P/C volume {pcr:.2f} very low (complacent)")
    skew = opt.get("skew_otm_put_minus_call_iv")
    if skew is not None and skew > 0.08:
        add(-0.05, f"steep put skew {skew:.2f} (hedging demand)")

    # Market regime
    spy = market.get("SPY") or {}
    if spy.get("above_sma50") is True:
        add(0.05, "SPY above 50 SMA")
    elif spy.get("above_sma50") is False:
        add(-0.05, "SPY below 50 SMA")
    vix = (market.get("^VIX") or {}).get("last")
    if vix and vix > 25:
        reasons.append(f"VIX {vix:.1f} elevated: confidence haircut")

    score = max(-1.0, min(1.0, sum(pts)))
    if vix and vix > 25:
        score *= 0.7
    lean = "CALL" if score >= 0.2 else "PUT" if score <= -0.2 else "NO TRADE"

    warnings = []
    ivr = opt.get("iv_to_realized_vol")
    if ivr and ivr > 1.4:
        warnings.append(f"options expensive (IV/RV {ivr:.2f}); prefer spreads over naked long options")
    if t.get("next_earnings"):
        warnings.append(f"earnings scheduled {t['next_earnings']}: check if inside expiry window")
    return {"score": round(score, 3), "lean": lean, "reasons": reasons, "warnings": warnings}


def score_all(snap: dict) -> dict:
    m = snap.get("market_context", {})
    return {s: score_ticker(t, m) for s, t in snap.get("tickers", {}).items()}
