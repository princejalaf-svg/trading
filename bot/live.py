"""Live intraday trend watcher: fast, rules-based, no LLM in the hot loop.

Polls Twelve Data for real-time prices every few seconds and 1-minute bars
every minute. For each ticker it keeps an intraday trend state:

    BULLISH (buy-side trend) / BEARISH (sell-side trend) / NEUTRAL

and raises an alert the moment that state flips, or when price spikes,
volume surges, or a key level (prior-day high/low, the daily report's
invalidation level) breaks. Alerts go to the console, to
data/live/alerts-<date>.jsonl, and optionally to your phone (ntfy) or to a
Discord/Slack webhook.

Paper trading / research only. Trend labels describe what price is doing,
not advice to trade.

Usage:
    python -m bot.live                       # run during market hours
    python -m bot.live --ntfy my-secret-topic   # push alerts to the ntfy app
    python -m bot.live --webhook https://discord.com/api/webhooks/...
    python -m bot.live --replay 2026-11-20   # replay a past session (test)
"""
from __future__ import annotations

import argparse
import json
import math
import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import pandas as pd
import requests

from bot import twelvedata

ROOT = Path(__file__).resolve().parent.parent
ET = ZoneInfo("America/New_York")
MARKET = ["SPY", "QQQ"]

# Tunables (also overridable in config.json under "live").
DEFAULTS = {
    "credit_budget_per_min": 45,   # Grow plan = 55/min; leave headroom for the daily run
    "enter_threshold": 0.5,        # |score| needed to enter BULLISH/BEARISH
    "exit_threshold": -0.15,       # trend is dropped once the score turns this far against it
    "confirm_sec": 120,            # a new state must hold this long before alerting (debounce)
    "spike_pct_5m": 1.0,           # min % move within 5 minutes for a spike alert...
    "spike_sigma": 4.0,            # ...and at least this many 5-min standard deviations
    "volume_surge_x": 5.0,         # 1-min volume vs median of last 20 bars
    "cooldown_sec": 300,           # min seconds between repeated alerts of the same kind
}


# --------------------------------------------------------------------------- data
def _bars(symbols: list[str], **params) -> dict[str, pd.DataFrame]:
    """1-min bars for several symbols in one request (1 credit per symbol)."""
    data = twelvedata._get("time_series", symbol=",".join(symbols), interval="1min",
                           timezone="America/New_York", **params)
    if len(symbols) == 1:
        data = {symbols[0]: data}
    out = {}
    for s in symbols:
        vals = (data.get(s) or {}).get("values") or []
        if not vals:
            continue
        df = pd.DataFrame(vals)
        df["datetime"] = pd.to_datetime(df["datetime"])
        df = df.set_index("datetime").sort_index()
        out[s] = df[["open", "high", "low", "close", "volume"]].apply(pd.to_numeric, errors="coerce")
    return out


def _prices(symbols: list[str]) -> dict[str, float]:
    data = twelvedata._get("price", symbol=",".join(symbols))
    if len(symbols) == 1:
        data = {symbols[0]: data}
    return {s: float(v["price"]) for s, v in data.items() if isinstance(v, dict) and "price" in v}


def _prior_day(symbols: list[str], session: str | None = None) -> dict[str, dict]:
    """Prior session high/low/close per symbol (before `session` date if given)."""
    out = {}
    for s in symbols:
        df = twelvedata.daily_history(s, days=10)
        if session:
            df = df[df.index < pd.Timestamp(session)]
        elif datetime.now(ET).date() == df.index[-1].date():
            df = df.iloc[:-1]
        last = df.iloc[-1]
        out[s] = {"high": float(last["High"]), "low": float(last["Low"]), "close": float(last["Close"])}
    return out


def _report_levels() -> dict[str, dict]:
    """Invalidation/target levels from the latest predictions.csv rows, if any."""
    p = ROOT / "predictions.csv"
    if not p.exists():
        return {}
    df = pd.read_csv(p)
    if df.empty:
        return {}
    latest = df[df["date"] == df["date"].max()]
    return {r.symbol: {"decision": r.decision, "entry": r.entry_price} for r in latest.itertuples()}


# --------------------------------------------------------------------------- engine
def _ema(s: pd.Series, n: int) -> float:
    return float(s.ewm(span=n, adjust=False).mean().iloc[-1])


def _rsi(s: pd.Series, n: int = 14) -> float:
    d = s.diff()
    up = d.clip(lower=0).ewm(alpha=1 / n, adjust=False).mean()
    dn = (-d.clip(upper=0)).ewm(alpha=1 / n, adjust=False).mean()
    rs = up.iloc[-1] / dn.iloc[-1] if dn.iloc[-1] else math.inf
    return float(100 - 100 / (1 + rs))


def score(bars: pd.DataFrame, price: float, prior: dict, market_bias: float) -> tuple[float, dict]:
    """Intraday trend score in [-1, 1] plus the readings that produced it."""
    close = pd.concat([bars["close"], pd.Series([price])], ignore_index=True)
    tp = (bars["high"] + bars["low"] + bars["close"]) / 3
    vwap = float((tp * bars["volume"]).sum() / max(bars["volume"].sum(), 1))
    e9, e21 = _ema(close, 9), _ema(close, 21)
    rsi = _rsi(close) if len(close) > 15 else 50.0
    ref = close.iloc[-16] if len(close) > 15 else close.iloc[0]
    mom15 = (price / ref - 1) * 100

    parts = {
        "vwap": 0.25 if price > vwap else -0.25,
        "ema": 0.25 if e9 > e21 else -0.25,
        "mom15": max(-0.2, min(0.2, mom15 / 1.0 * 0.2)),       # ±1% in 15 min = full weight
        "rsi": 0.1 if rsi > 55 else (-0.1 if rsi < 45 else 0.0),
        "levels": 0.15 if price > prior["high"] else (-0.15 if price < prior["low"] else 0.0),
        "market": 0.05 * market_bias,
    }
    s = max(-1.0, min(1.0, sum(parts.values())))
    readings = {"price": round(price, 2), "vwap": round(vwap, 2), "ema9": round(e9, 2),
                "ema21": round(e21, 2), "rsi1m": round(rsi, 1), "mom15_pct": round(mom15, 2),
                "prior_high": prior["high"], "prior_low": prior["low"]}
    return round(s, 2), readings


@dataclass
class TickerState:
    trend: str = "NEUTRAL"
    pending: str | None = None
    pending_since: float = 0.0
    last_alert: dict = field(default_factory=dict)
    history: list = field(default_factory=list)   # (ts, price) for spike detection
    broke: set = field(default_factory=set)       # levels already announced today


class Watcher:
    def __init__(self, tickers: list[str], cfg: dict, notify):
        self.tickers, self.cfg, self.notify = tickers, cfg, notify
        self.state = {t: TickerState() for t in tickers}

    def _cool(self, st: TickerState, kind: str, now: float) -> bool:
        if now - st.last_alert.get(kind, -1e18) < self.cfg["cooldown_sec"]:
            return False
        st.last_alert[kind] = now
        return True

    def step(self, ts: datetime, bars: dict, prices: dict, prior: dict, market_bias: float):
        now = ts.timestamp()
        for t in self.tickers:
            if t not in bars or t not in prices or bars[t].empty:
                continue
            st, price, b = self.state[t], prices[t], bars[t]
            sc, r = score(b, price, prior[t], market_bias)

            # 1) trend state machine with hysteresis + debounce
            c = self.cfg
            if st.trend == "NEUTRAL":
                want = "BULLISH" if sc >= c["enter_threshold"] else "BEARISH" if sc <= -c["enter_threshold"] else "NEUTRAL"
            else:
                sign = 1 if st.trend == "BULLISH" else -1
                if sc * sign <= -c["enter_threshold"]:
                    want = "BEARISH" if sign == 1 else "BULLISH"
                elif sc * sign < c["exit_threshold"]:
                    want = "NEUTRAL"
                else:
                    want = st.trend
            if want != st.trend:
                if st.pending != want:
                    st.pending, st.pending_since = want, now
                if now - st.pending_since >= c["confirm_sec"]:
                    old, st.trend, st.pending = st.trend, want, None
                    label = {"BULLISH": "BUY-side trend", "BEARISH": "SELL-side trend", "NEUTRAL": "trend faded"}[want]
                    self.notify(ts, t, "TREND", f"{old} -> {want} ({label}), score {sc:+.2f}", r)
            else:
                st.pending = None

            # 2) fast price spike over ~5 minutes
            st.history = [(x, p) for x, p in st.history if now - x <= 300] + [(now, price)]
            base = st.history[0][1]
            move = (price / base - 1) * 100
            r1 = b["close"].pct_change().dropna()
            sigma5 = float(r1.iloc[-60:].std() * math.sqrt(5) * 100) if len(r1) > 10 else 0.0
            thresh = max(c["spike_pct_5m"], c["spike_sigma"] * sigma5)
            if abs(move) >= thresh and self._cool(st, "SPIKE", now):
                self.notify(ts, t, "SPIKE", f"{'up' if move > 0 else 'down'} {move:+.2f}% in 5 min", r)

            # 3) volume surge on the latest completed bar
            if len(b) > 21:
                rel = b["volume"].iloc[-1] / max(b["volume"].iloc[-21:-1].median(), 1)
                if rel >= c["volume_surge_x"] and self._cool(st, "VOLUME", now):
                    self.notify(ts, t, "VOLUME", f"1-min volume {rel:.1f}x average", r)

            # 4) key level breaks (once per day each)
            for name, lvl, cond in (("prior-day high", prior[t]["high"], price > prior[t]["high"]),
                                    ("prior-day low", prior[t]["low"], price < prior[t]["low"])):
                if cond and name not in st.broke:
                    st.broke.add(name)
                    self.notify(ts, t, "LEVEL", f"broke {name} {lvl:.2f}", r)


# --------------------------------------------------------------------------- output
def make_notifier(log_path: Path, ntfy: str | None, webhook: str | None, quiet_kinds=()):
    log_path.parent.mkdir(parents=True, exist_ok=True)

    def notify(ts, sym, kind, msg, readings):
        line = f"[{ts:%H:%M:%S}] {sym:5} {kind:6} {msg} | px {readings['price']} vwap {readings['vwap']} rsi {readings['rsi1m']}"
        print(line, flush=True)
        with log_path.open("a") as f:
            f.write(json.dumps({"ts": ts.isoformat(), "symbol": sym, "kind": kind, "msg": msg, **readings}) + "\n")
        if kind in quiet_kinds:
            return
        try:
            if ntfy:
                requests.post(f"https://ntfy.sh/{ntfy}", data=line.encode(), timeout=5,
                              headers={"Title": f"{sym} {kind}", "Priority": "high" if kind in ("TREND", "SPIKE") else "default"})
            if webhook:
                requests.post(webhook, json={"content": line, "text": line}, timeout=5)
        except requests.RequestException as e:
            print(f"  (push failed: {e})", flush=True)

    return notify


def market_bias(bars: dict) -> float:
    """+1 / -1 / 0 from SPY & QQQ being above/below their session VWAP."""
    votes = []
    for m in MARKET:
        b = bars.get(m)
        if b is None or b.empty:
            continue
        tp = (b["high"] + b["low"] + b["close"]) / 3
        vwap = (tp * b["volume"]).sum() / max(b["volume"].sum(), 1)
        votes.append(1 if b["close"].iloc[-1] > vwap else -1)
    return sum(votes) / len(votes) if votes else 0.0


# --------------------------------------------------------------------------- modes
def run_live(tickers, cfg, notify):
    syms = tickers + MARKET
    # credits/min = (60/poll)*len(tickers) [prices] + len(syms) [bars once a minute]
    poll = max(2.0, 60 * len(tickers) / max(cfg["credit_budget_per_min"] - len(syms), 1))
    print(f"Watching {', '.join(tickers)} (market: {', '.join(MARKET)}); price poll every {poll:.1f}s, "
          f"bars every 60s, ~{60 / poll * len(tickers) + len(syms):.0f} credits/min", flush=True)
    w = Watcher(tickers, cfg, notify)
    prior, bars, last_bars = _prior_day(syms), {}, 0.0
    while True:
        now = datetime.now(ET)
        if now.weekday() >= 5 or not (now.replace(hour=9, minute=30, second=0) <= now <= now.replace(hour=16, minute=0, second=0)):
            print(f"[{now:%H:%M}] market closed; sleeping 60s", flush=True)
            time.sleep(60)
            continue
        t0 = time.time()
        try:
            if t0 - last_bars >= 60:
                bars = _bars(syms, start_date=now.strftime("%Y-%m-%d 09:30:00"), outputsize=400)
                last_bars = t0
            prices = _prices(tickers)
            w.step(now, bars, prices, prior, market_bias(bars))
        except twelvedata.TwelveDataError as e:
            print(f"[{now:%H:%M:%S}] data error: {e}", flush=True)
            time.sleep(10)
        time.sleep(max(0.0, poll - (time.time() - t0)))


def run_replay(tickers, cfg, notify, day: str):
    """Replay a past session minute by minute (bars' close stands in for the live price)."""
    syms = tickers + MARKET
    allbars = _bars(syms, start_date=f"{day} 09:30:00", end_date=f"{day} 16:00:00", outputsize=400)
    prior = _prior_day(syms, session=day)
    w = Watcher(tickers, cfg, notify)
    idx = allbars[tickers[0]].index
    for i in range(1, len(idx)):
        ts = idx[i].to_pydatetime().replace(tzinfo=ET)
        sl = {s: b[b.index < idx[i]] for s, b in allbars.items()}
        prices = {t: float(allbars[t]["close"].loc[idx[i]]) for t in tickers if idx[i] in allbars[t].index}
        w.step(ts, sl, prices, prior, market_bias(sl))
    print("Final states: " + ", ".join(f"{t}={w.state[t].trend}" for t in tickers))


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--replay", metavar="YYYY-MM-DD", help="replay a past session instead of running live")
    ap.add_argument("--ntfy", metavar="TOPIC", help="push alerts to https://ntfy.sh/TOPIC (use a hard-to-guess topic)")
    ap.add_argument("--webhook", metavar="URL", help="Discord/Slack incoming-webhook URL")
    ap.add_argument("--tickers", help="comma list; default = config.json tickers")
    a = ap.parse_args()

    conf = json.loads((ROOT / "config.json").read_text())
    cfg = {**DEFAULTS, **conf.get("live", {})}
    tickers = a.tickers.split(",") if a.tickers else conf["tickers"]
    day = a.replay or datetime.now(ET).strftime("%Y-%m-%d")
    suffix = "-replay" if a.replay else ""
    notify = make_notifier(ROOT / "data" / "live" / f"alerts-{day}{suffix}.jsonl",
                           None if a.replay else a.ntfy, None if a.replay else a.webhook,
                           quiet_kinds=("VOLUME",))
    if a.replay:
        run_replay(tickers, cfg, notify, a.replay)
    else:
        run_live(tickers, cfg, notify)


if __name__ == "__main__":
    main()
