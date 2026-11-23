# Options Signal Bot

A Claude-run bot that watches **INTC, NVDA and AAPL** and, each trading day,
predicts whether a **CALL**, **PUT** or **NO TRADE** is best, with a suggested
contract, target and invalidation level.

> Research / paper trading only. Not financial advice. Options can lose 100% of
> their value. Check the scorecard before ever trusting it with money.

## How it works
1. `bot/fetch_data.py` pulls prices and options chains (Yahoo Finance) and
   computes indicators.
2. Claude reads that snapshot, checks the news and makes a careful decision
   (see `CLAUDE.md` for the exact rules it follows).
3. Results go to `reports/YYYY-MM-DD.md` and `predictions.csv`.
4. `bot/grade.py` scores old predictions so you can see the real hit rate.

## Running it
- **Automatically:** a Claude scheduled task runs it every weekday after the
  US market close.
- **By hand:** open a Claude Code session on this repo and say "run the bot".
- **Data only:** `pip install -r requirements.txt && python -m bot.fetch_data`

## Changing tickers
Edit `"tickers"` in `config.json`.

## Live intraday alerts (`bot/live.py`)

A tick-driven, rules-based watcher for market hours. Each price update is processed as soon as it
arrives (about 12 µs of engine time), and there is no AI in the loop.

| Feed | Plan | Typical delay from trade to alert |
|---|---|---|
| REST polling (default) | Grow (current) | ~0.5–4 s (all tickers batched every ~3.7 s within 55 credits/min) |
| WebSocket `--ws` | Pro or above | ~170 ms average (Twelve Data's figure) |

- Signals come from the live price stream itself: time-based EMAs (30 s / 2 min), 60 s momentum,
  price vs session VWAP, prior-day high/low, and SPY/QQQ direction. 1-minute bars are only used
  as slow background (VWAP, volatility) and are refreshed every 5 minutes.
- Per ticker it tracks **BULLISH (buy-side trend) / BEARISH (sell-side trend) / NEUTRAL**. It alerts
  once a flip holds for 15 s, on volatility-scaled 30-second spikes, and on prior-day high/low breaks.
- Alerts are printed with their data age, logged to `data/live/alerts-<date>.jsonl`, and pushed
  without blocking the loop:

```bash
export TWELVE_DATA_API_KEY=...            # needed when running on your own machine
python -m bot.live --ntfy some-long-random-topic   # phone push via the free ntfy app
python -m bot.live --ws --ntfy some-long-random-topic   # streaming (Pro plan)
python -m bot.live --webhook <discord-or-slack-webhook-url>
python -m bot.live --replay 2026-11-20    # replay a past session (1 tick per minute; no spike alerts)
```

Tune thresholds with a `"live": {...}` block in `config.json` (see `DEFAULTS` in `bot/live.py`).
Options data (Yahoo) is about 15 minutes delayed, so live alerts are stock-price based.
Trend labels describe price action. They are not trade instructions, and this is research only.
