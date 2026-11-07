"""Data-source diagnostics. Prints GitHub Actions annotations (::notice::) so the
results are visible on the run's summary page without downloading logs.

    python -m engine.diagnose
"""
from __future__ import annotations

import pandas as pd


def note(msg: str) -> None:
    print(f"::notice title=diag::{msg}", flush=True)


def main() -> None:
    import yfinance as yf
    note(f"yfinance {yf.__version__}, pandas {pd.__version__}")
    tickers = ["RELIANCE.NS", "TCS.NS", "HDFCBANK.NS", "^NSEI"]
    df = yf.download(tickers, period="1y", interval="1d", auto_adjust=True, group_by="column",
                     progress=False, threads=True)
    idx = df.index
    note(f"index tz={getattr(idx, 'tz', None)} rows={len(idx)} first={idx[0]} last={idx[-1]}")
    note("last 6 raw timestamps: " + " | ".join(str(x) for x in idx[-6:]))
    close = df["Close"]
    for t in tickers:
        s = close[t].dropna()
        note(f"{t}: n={len(s)} first={s.index[0] if len(s) else None} last={s.index[-1] if len(s) else None}")
    # how many tickers have data per row, last 8 rows
    cnt = close.notna().sum(axis=1).tail(8)
    note("non-NaN per row (last 8): " + " | ".join(f"{d}:{c}" for d, c in cnt.items()))
    # single-ticker history to compare timestamps
    for t in ("RELIANCE.NS", "^NSEI"):
        h = yf.Ticker(t).history(period="1mo", interval="1d", auto_adjust=True)
        note(f"history {t}: tz={getattr(h.index, 'tz', None)} last3=" + " | ".join(str(x) for x in h.index[-3:]))
    # rows where only some tickers have data (misalignment)
    part = close.notna().sum(axis=1)
    mis = part[(part > 0) & (part < len(tickers))]
    note(f"partial rows: {len(mis)}; sample: " + " | ".join(str(d) for d in mis.index[-6:]))


if __name__ == "__main__":
    main()
