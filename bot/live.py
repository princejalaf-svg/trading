"""Live intraday trend watcher: tick-driven, rules-based, no LLM in the hot loop.

Every price update is processed the moment it arrives (in well under a
millisecond). There are two ways to feed it:

  * REST polling (works on the Twelve Data Grow plan): all tickers in one batched
    /price call every ~3-4 s, sized to the plan's per-minute credit limit.
  * WebSocket streaming (--ws, needs Twelve Data Pro or above): pushed prices
    with about 170 ms average latency (Twelve Data's figure).

Signals are computed on the live price stream itself, with time-based EMAs
(30 s fast / 2 min slow), 60 s momentum and 30 s spike detection. 1-minute
bars are only used as background context (session VWAP, volatility, SPY/QQQ
direction) and are refreshed every 5 minutes.

Per ticker it tracks BULLISH (buy-side trend) / BEARISH (sell-side trend) /
NEUTRAL and alerts as soon as the state flips and holds for `confirm_sec`, on
fast spikes, and on prior-day high/low breaks. Alerts go to the console,
data/live/alerts-<date>.jsonl, and optionally ntfy (phone push) or a
Discord/Slack webhook. Pushes are sent off the hot path, so they never slow
the loop.

Paper trading / research only. Trend labels describe what price is doing,
not advice to trade.

Usage:
    python -m bot.live                          # REST polling during market hours
    python -m bot.live --ws                     # WebSocket stream (Pro plan + TWELVE_DATA_API_KEY)
    python -m bot.live --ntfy my-secret-topic   # push alerts to the ntfy phone app
    python -m bot.live --webhook https://discord.com/api/webhooks/...
    python -m bot.live --replay 2026-11-20      # replay a past session from 1-min bars (test)
"""
from __future__ import annotations

import argparse
import json
import math
import os
import threading
import time
from collections import deque
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
WS_URL = "wss://ws.twelvedata.com/v1/quotes/price"

# Tunables (override any of them in config.json under "live").
DEFAULTS = {
    "credit_budget_per_min": 50,   # Grow plan = 55/min
    "context_refresh_sec": 300,    # how often 1-min bars (VWAP/vol/market) are refreshed
    "ema_fast_sec": 30,
    "ema_slow_sec": 120,
    "momentum_sec": 60,
    "enter_threshold": 0.5,        # |score| needed to enter BULLISH/BEARISH
    "exit_threshold": -0.15,       # trend is dropped once the score turns this far against it
    "confirm_sec": 15,             # a new state must hold this long before alerting
    "spike_window_sec": 30,
    "spike_pct_min": 0.4,          # min % move inside the spike window...
    "spike_sigma": 4.0,            # ...and at least this many standard deviations of that window
    "spike_cooldown_sec": 60,
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


# --------------------------------------------------------------------------- context
@dataclass
class Context:
    """Slow-moving background per symbol, refreshed from 1-min bars."""
    vwap: float
    sigma_1m_pct: float      # std of 1-min returns, in %


def build_context(bars: dict[str, pd.DataFrame]) -> tuple[dict[str, Context], float]:
    ctx, votes = {}, []
    for s, b in bars.items():
        if b.empty:
            continue
        tp = (b["high"] + b["low"] + b["close"]) / 3
        vwap = float((tp * b["volume"]).sum() / max(b["volume"].sum(), 1))
        r = b["close"].pct_change().dropna().iloc[-60:]
        ctx[s] = Context(vwap=vwap, sigma_1m_pct=float(r.std() * 100) if len(r) > 10 else 0.1)
        if s in MARKET:
            votes.append(1 if b["close"].iloc[-1] > vwap else -1)
    return ctx, (sum(votes) / len(votes) if votes else 0.0)


# --------------------------------------------------------------------------- engine
@dataclass
class TickerState:
    trend: str = "NEUTRAL"
    pending: str | None = None
    pending_since: float = 0.0
    ema_fast: float | None = None
    ema_slow: float | None = None
    last_ts: float | None = None
    ticks: deque = field(default_factory=lambda: deque(maxlen=5000))   # (ts, price)
    last_alert: dict = field(default_factory=dict)
    broke: set = field(default_factory=set)


class Watcher:
    def __init__(self, tickers, cfg, notify, prior):
        self.tickers, self.cfg, self.notify, self.prior = tickers, cfg, notify, prior
        self.state = {t: TickerState() for t in tickers}
        self.ctx: dict[str, Context] = {}
        self.market_bias = 0.0
        self.proc_us = deque(maxlen=10000)   # engine processing time per tick, microseconds

    def set_context(self, bars):
        self.ctx, self.market_bias = build_context(bars)

    @staticmethod
    def _price_at(ticks, ts_back):
        for t, p in ticks:            # oldest first; first tick at/after the lookback
            if t >= ts_back:
                return p
        return ticks[-1][1]

    def on_tick(self, sym: str, ts: float, price: float, recv_ts: float | None = None):
        """Process one price update. ts = exchange/fetch epoch seconds."""
        t0 = time.perf_counter()
        if sym not in self.state or sym not in self.ctx:
            return
        c, st, cx = self.cfg, self.state[sym], self.ctx[sym]

        # time-based EMAs (correct for irregular tick spacing)
        if st.last_ts is None:
            st.ema_fast = st.ema_slow = price
        else:
            dt = max(ts - st.last_ts, 0.0)
            st.ema_fast += (1 - math.exp(-dt / c["ema_fast_sec"])) * (price - st.ema_fast)
            st.ema_slow += (1 - math.exp(-dt / c["ema_slow_sec"])) * (price - st.ema_slow)
        st.last_ts = ts
        st.ticks.append((ts, price))
        while st.ticks and ts - st.ticks[0][0] > 900:
            st.ticks.popleft()

        mom = (price / self._price_at(st.ticks, ts - c["momentum_sec"]) - 1) * 100
        mom_sigma = cx.sigma_1m_pct * math.sqrt(c["momentum_sec"] / 60)
        prior = self.prior[sym]
        parts = {
            "vwap": 0.25 if price > cx.vwap else -0.25,
            "ema": 0.25 if st.ema_fast > st.ema_slow else -0.25,
            "momentum": max(-0.25, min(0.25, 0.25 * mom / max(2 * mom_sigma, 1e-6))),
            "levels": 0.15 if price > prior["high"] else (-0.15 if price < prior["low"] else 0.0),
            "market": 0.1 * self.market_bias,
        }
        sc = round(max(-1.0, min(1.0, sum(parts.values()))), 2)
        r = {"price": round(price, 2), "vwap": round(cx.vwap, 2), "ema_fast": round(st.ema_fast, 2),
             "ema_slow": round(st.ema_slow, 2), f"mom{c['momentum_sec']}s_pct": round(mom, 3), "score": sc}

        alerts = []
        # 1) trend state machine: hysteresis + short time debounce
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
                st.pending, st.pending_since = want, ts
            if ts - st.pending_since >= c["confirm_sec"]:
                old, st.trend, st.pending = st.trend, want, None
                label = {"BULLISH": "BUY-side trend", "BEARISH": "SELL-side trend", "NEUTRAL": "trend faded"}[want]
                alerts.append(("TREND", f"{old} -> {want} ({label}), score {sc:+.2f}"))
        else:
            st.pending = None

        # 2) fast spike inside spike_window_sec, scaled to this stock's volatility
        w = c["spike_window_sec"]
        move = (price / self._price_at(st.ticks, ts - w) - 1) * 100
        thresh = max(c["spike_pct_min"], c["spike_sigma"] * cx.sigma_1m_pct * math.sqrt(w / 60))
        if abs(move) >= thresh and ts - st.last_alert.get("SPIKE", -1e18) >= c["spike_cooldown_sec"]:
            st.last_alert["SPIKE"] = ts
            alerts.append(("SPIKE", f"{'up' if move > 0 else 'down'} {move:+.2f}% in {w}s"))

        # 3) key level breaks (once per day each)
        for name, lvl, cond in (("prior-day high", prior["high"], price > prior["high"]),
                                ("prior-day low", prior["low"], price < prior["low"])):
            if cond and name not in st.broke:
                st.broke.add(name)
                alerts.append(("LEVEL", f"broke {name} {lvl:.2f}"))

        self.proc_us.append((time.perf_counter() - t0) * 1e6)
        for kind, msg in alerts:
            self.notify(ts, sym, kind, msg, r, recv_ts)


# --------------------------------------------------------------------------- output
def make_notifier(log_path: Path, ntfy: str | None, webhook: str | None, live: bool):
    log_path.parent.mkdir(parents=True, exist_ok=True)

    def _push(sym, kind, line):
        try:
            if ntfy:
                requests.post(f"https://ntfy.sh/{ntfy}", data=line.encode(), timeout=5,
                              headers={"Title": f"{sym} {kind}", "Priority": "high" if kind != "LEVEL" else "default"})
            if webhook:
                requests.post(webhook, json={"content": line, "text": line}, timeout=5)
        except requests.RequestException as e:
            print(f"  (push failed: {e})", flush=True)

    def notify(ts, sym, kind, msg, readings, recv_ts=None):
        when = datetime.fromtimestamp(ts, ET)
        lag = f" | data age {(time.time() - ts) * 1000:.0f}ms" if live else ""
        line = f"[{when:%H:%M:%S}] {sym:5} {kind:5} {msg} | px {readings['price']} vwap {readings['vwap']}{lag}"
        print(line, flush=True)
        with log_path.open("a") as f:
            f.write(json.dumps({"ts": when.isoformat(), "symbol": sym, "kind": kind, "msg": msg, **readings}) + "\n")
        if ntfy or webhook:
            threading.Thread(target=_push, args=(sym, kind, line), daemon=True).start()

    return notify


# --------------------------------------------------------------------------- modes
def _market_open(now: datetime) -> bool:
    return now.weekday() < 5 and now.replace(hour=9, minute=30, second=0) <= now <= now.replace(hour=16, minute=0, second=0)


def _refresh_context(w: Watcher, syms: list[str]):
    today = datetime.now(ET).strftime("%Y-%m-%d")
    w.set_context(_bars(syms, start_date=f"{today} 09:30:00", outputsize=400))


def run_poll(tickers, cfg, notify):
    syms = tickers + MARKET
    ctx_cost = len(syms) * 60 / cfg["context_refresh_sec"]
    poll = max(1.0, 60 * len(tickers) / max(cfg["credit_budget_per_min"] - ctx_cost, 1))
    print(f"REST mode: {', '.join(tickers)} polled together every {poll:.1f}s "
          f"(~{60 / poll * len(tickers) + ctx_cost:.0f} credits/min). For ~170 ms streaming use --ws (Pro plan).",
          flush=True)
    w = Watcher(tickers, cfg, notify, _prior_day(syms))
    last_ctx = 0.0
    while True:
        now = datetime.now(ET)
        if not _market_open(now):
            print(f"[{now:%H:%M}] market closed; sleeping 60s", flush=True)
            time.sleep(60)
            continue
        t0 = time.time()
        try:
            if t0 - last_ctx >= cfg["context_refresh_sec"] or not w.ctx:
                _refresh_context(w, syms)
                last_ctx = t0
            prices = _prices(tickers)
            ts = time.time()
            for s, p in prices.items():
                w.on_tick(s, ts, p)
        except (twelvedata.TwelveDataError, requests.RequestException) as e:
            print(f"[{now:%H:%M:%S}] data error: {e}", flush=True)
            time.sleep(5)
        time.sleep(max(0.0, poll - (time.time() - t0)))


def run_ws(tickers, cfg, notify):
    try:
        import websocket   # pip install websocket-client
    except ImportError:
        raise SystemExit("--ws needs: python -m pip install websocket-client")
    key = os.environ.get("TWELVE_DATA_API_KEY")
    if not key:
        raise SystemExit("--ws needs TWELVE_DATA_API_KEY set (WebSocket requires Twelve Data Pro or above).")
    syms = tickers + MARKET
    w = Watcher(tickers, cfg, notify, _prior_day(syms))
    _refresh_context(w, syms)

    def ctx_loop():
        while True:
            time.sleep(cfg["context_refresh_sec"])
            try:
                _refresh_context(w, syms)
            except Exception as e:  # keep streaming even if a refresh fails
                print(f"context refresh failed: {e}", flush=True)

    threading.Thread(target=ctx_loop, daemon=True).start()

    def on_open(ws):
        ws.send(json.dumps({"action": "subscribe", "params": {"symbols": ",".join(tickers)}}))

        def heartbeat():
            while ws.keep_running:
                time.sleep(10)
                try:
                    ws.send(json.dumps({"action": "heartbeat"}))
                except Exception:
                    return

        threading.Thread(target=heartbeat, daemon=True).start()
        print(f"WebSocket connected; streaming {', '.join(tickers)}", flush=True)

    def on_message(ws, raw):
        recv = time.time()
        m = json.loads(raw)
        if m.get("event") == "price":
            w.on_tick(m["symbol"], float(m.get("timestamp", recv)), float(m["price"]), recv)
        elif m.get("event") == "subscribe-status" and m.get("status") != "ok":
            print(f"subscribe problem: {m}", flush=True)

    while True:
        app = websocket.WebSocketApp(f"{WS_URL}?apikey={key}", on_open=on_open, on_message=on_message,
                                     on_error=lambda ws, e: print(f"ws error: {e}", flush=True))
        app.run_forever(ping_interval=20, ping_timeout=10)
        print("WebSocket closed; reconnecting in 3s", flush=True)
        time.sleep(3)


def run_replay(tickers, cfg, notify, day: str):
    """Replay a past session from 1-min bars (one tick per bar, so coarser than live)."""
    syms = tickers + MARKET
    allbars = _bars(syms, start_date=f"{day} 09:30:00", end_date=f"{day} 16:00:00", outputsize=400)
    w = Watcher(tickers, cfg, notify, _prior_day(syms, session=day))
    idx = allbars[tickers[0]].index
    last_ctx = None
    for i in range(1, len(idx)):
        ts_i = idx[i]
        if last_ctx is None or (ts_i - last_ctx).total_seconds() >= cfg["context_refresh_sec"]:
            w.set_context({s: b[b.index < ts_i] for s, b in allbars.items()})
            last_ctx = ts_i
        epoch = ts_i.to_pydatetime().replace(tzinfo=ET).timestamp()
        for t in tickers:
            if ts_i in allbars[t].index:
                w.on_tick(t, epoch, float(allbars[t]["close"].loc[ts_i]))
    p = sorted(w.proc_us)
    print("Final states: " + ", ".join(f"{t}={w.state[t].trend}" for t in tickers))
    print(f"Engine time per tick: median {p[len(p) // 2]:.0f} µs, p99 {p[int(len(p) * 0.99)]:.0f} µs")


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--ws", action="store_true", help="stream via Twelve Data WebSocket (Pro plan)")
    ap.add_argument("--replay", metavar="YYYY-MM-DD", help="replay a past session instead of running live")
    ap.add_argument("--ntfy", metavar="TOPIC", help="push alerts to https://ntfy.sh/TOPIC (use a hard-to-guess topic)")
    ap.add_argument("--webhook", metavar="URL", help="Discord/Slack incoming-webhook URL")
    ap.add_argument("--tickers", help="comma list; default = config.json tickers")
    a = ap.parse_args()

    conf = json.loads((ROOT / "config.json").read_text())
    cfg = {**DEFAULTS, **conf.get("live", {})}
    tickers = a.tickers.split(",") if a.tickers else conf["tickers"]
    day = a.replay or datetime.now(ET).strftime("%Y-%m-%d")
    log = ROOT / "data" / "live" / f"alerts-{day}{'-replay' if a.replay else ''}.jsonl"
    if a.replay:
        run_replay(tickers, cfg, make_notifier(log, None, None, live=False), a.replay)
    elif a.ws:
        run_ws(tickers, cfg, make_notifier(log, a.ntfy, a.webhook, live=True))
    else:
        run_poll(tickers, cfg, make_notifier(log, a.ntfy, a.webhook, live=True))


if __name__ == "__main__":
    main()
