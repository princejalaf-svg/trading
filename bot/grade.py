"""Grade past predictions in predictions.csv against what the stock actually did.

A CALL is correct if price rose over the horizon, a PUT if it fell.
NO TRADE rows are not graded. Updates the CSV in place and prints a scorecard.

Usage:  python -m bot.grade
"""
from __future__ import annotations

import csv
from datetime import date, datetime, timedelta
from pathlib import Path

import yfinance as yf

ROOT = Path(__file__).resolve().parent.parent
LOG = ROOT / "predictions.csv"
FIELDS = [
    "date", "symbol", "decision", "confidence", "entry_price", "horizon_days",
    "suggested_contract", "rationale", "exit_price", "move_pct", "outcome",
]


def load() -> list[dict]:
    if not LOG.exists():
        return []
    with LOG.open() as f:
        return list(csv.DictReader(f))


def save(rows: list[dict]) -> None:
    with LOG.open("w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=FIELDS, lineterminator="\n")
        w.writeheader()
        w.writerows(rows)


def main() -> None:
    rows = load()
    today = date.today()
    for r in rows:
        if r.get("outcome") or r["decision"] not in ("CALL", "PUT"):
            continue
        start = datetime.strptime(r["date"], "%Y-%m-%d").date()
        h = int(r.get("horizon_days") or 5)
        hist = yf.Ticker(r["symbol"]).history(start=start, end=today + timedelta(days=1))
        trading_days_after = hist[hist.index.date > start]
        if len(trading_days_after) < h:
            continue  # horizon not reached yet
        exit_px = float(trading_days_after["Close"].iloc[h - 1])
        entry = float(r["entry_price"])
        move = (exit_px / entry - 1) * 100
        win = move > 0 if r["decision"] == "CALL" else move < 0
        r.update(exit_price=f"{exit_px:.2f}", move_pct=f"{move:.2f}", outcome="WIN" if win else "LOSS")
    save(rows)

    graded = [r for r in rows if r.get("outcome")]
    if not graded:
        print("No graded predictions yet.")
        return
    wins = sum(r["outcome"] == "WIN" for r in graded)
    print(f"Scorecard: {wins}/{len(graded)} correct ({wins / len(graded):.0%})")
    for sym in sorted({r["symbol"] for r in graded}):
        g = [r for r in graded if r["symbol"] == sym]
        w = sum(r["outcome"] == "WIN" for r in g)
        print(f"  {sym}: {w}/{len(g)}")


if __name__ == "__main__":
    main()
