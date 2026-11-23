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

A fast, rules-based watcher for market hours. It does not wait on the AI in the loop, so alerts arrive within seconds.

- Polls Twelve Data real-time prices every ~5 s and 1-minute bars every 60 s.
  With 3 tickers plus SPY/QQQ that's about 41 credits/min (the Grow plan allows 55).
- Per ticker it tracks **BULLISH (buy-side trend) / BEARISH (sell-side trend) / NEUTRAL**,
  scored from price vs VWAP, EMA 9/21, 15-minute momentum, 1-minute RSI, prior-day
  high/low and the SPY/QQQ direction. It alerts when the state flips and the new state
  holds for 2 minutes. It also alerts on volatility-scaled 5-minute spikes and on breaks
  of the prior-day high or low. Volume surges are logged but not pushed.
- Alerts are printed, appended to `data/live/alerts-<date>.jsonl`, and optionally pushed:

```bash
export TWELVE_DATA_API_KEY=...            # needed when running on your own machine
python -m bot.live --ntfy some-long-random-topic   # phone push via the free ntfy app
python -m bot.live --webhook <discord-or-slack-webhook-url>
python -m bot.live --replay 2026-11-20    # replay a past session to see what it would have said
```

Tune thresholds with a `"live": {...}` block in `config.json` (see `DEFAULTS` in `bot/live.py`).
Options data (Yahoo) is about 15 minutes delayed, so live alerts are stock-price based.
Trend labels describe price action. They are not trade instructions, and this is research only.
