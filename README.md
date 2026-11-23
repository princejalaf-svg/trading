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
