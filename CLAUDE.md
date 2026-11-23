# Options Signal Bot — instructions for Claude

This repo is a paper-trading research bot. Claude is the "brain": a Python
script gathers data, Claude reasons over it and decides **CALL / PUT / NO TRADE**
for each ticker in `config.json` (currently INTC, NVDA, AAPL).

**This is not financial advice and never places real trades.** It only writes
predictions and grades them later, so we can see whether it has any edge.

## Daily run (what to do when asked to "run the bot")

1. **Setup**: `pip install -q -r requirements.txt`
2. **Grade old calls**: `python -m bot.grade` — note the scorecard.
3. **Fetch data**: `python -m bot.fetch_data` → writes `data/snapshots/<today>.json`.
   If Yahoo is unreachable, stop and report the error. Never invent numbers.
4. **Read the snapshot** fully. `quant_signals` is a simple rules-based baseline:
   treat it as one input, not the answer.
5. **Check news** (WebSearch, if available) for each ticker from the last 48h:
   earnings dates, guidance, analyst moves, macro (Fed, CPI, jobs), sector news
   (semis, AI capex, China export rules).
6. **Decide carefully** for each ticker. Rules:
   - Default is **NO TRADE**. Only pick CALL or PUT when trend, momentum, options
     positioning and news mostly agree. Confidence 0.0–1.0; below
     `min_confidence_to_trade` in config.json means NO TRADE.
   - If earnings fall before the chosen expiry, say so explicitly. IV crush risk
     means prefer NO TRADE or a defined-risk spread.
   - If `iv_to_realized_vol` > 1.4, options are expensive: suggest a debit spread
     instead of a naked long option.
   - Suggest a concrete contract: expiry from the snapshot (14–45 DTE), strike
     near ATM or slightly OTM, plus an invalidation level (price that proves the
     idea wrong) and a target.
   - Be honest about uncertainty. Don't force a trade to have something to say.
7. **Write the report** to `reports/<today>.md` with: market context, scorecard,
   then per ticker: decision, confidence, contract, entry/target/invalidation,
   key reasons for and against, risks.
8. **Log predictions**: append one row per ticker to `predictions.csv` (keep the
   header; leave `exit_price,move_pct,outcome` empty; quote `rationale`).
   `entry_price` = snapshot price, `horizon_days` = config value.
9. **Commit and push** the snapshot, report and CSV:
   `git add -A && git commit -m "Bot run <today>" && git push`.
10. Finish with a short summary table (ticker | decision | confidence | contract).

## Files
- `bot/fetch_data.py` — prices, indicators (SMA, RSI, MACD, ATR, Bollinger,
  realized vol), options chain stats (ATM IV, expected move, put/call ratios,
  skew, max pain, unusual activity), earnings date, headlines.
- `bot/signals.py` — conservative baseline score in [-1, 1].
- `bot/grade.py` — grades past CALL/PUT calls after `horizon_days` trading days.
- `config.json` — tickers and parameters. Add a ticker there to monitor it.
