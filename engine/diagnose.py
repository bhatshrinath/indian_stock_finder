"""Data-source diagnostics. Prints GitHub Actions annotations (::notice::) so the
results are visible on the run's summary page without downloading logs.

    python -m engine.diagnose
"""
from __future__ import annotations

import pandas as pd


def note(msg: str) -> None:
    print(f"::notice title=diag::{msg}", flush=True)


def main() -> None:
    cors_probe()
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

    # the engine's own loader (with the latest-day fix) on a few stocks
    from . import data
    pr = data.load_yfinance(["RELIANCE", "TCS", "HDFCBANK", "INFY", "ITC"], period="1y")
    if pr is not None:
        cov = pr.close.notna().sum(axis=1).tail(3)
        note(f"engine loader: last={pr.last_date.date()} index_last={pr.index_close.index[-1].date()} "
             "coverage last 3: " + " | ".join(f"{d.date()}:{c}" for d, c in cov.items()))


def cors_probe() -> None:
    """Can a browser on the app's own address call Yahoo directly? (needs a CORS allow header)"""
    import requests
    origin = "https://bhatshrinath.github.io"
    for host in ("query1", "query2"):
        url = "https://%s.finance.yahoo.com/v8/finance/chart/TCS.NS?range=1d&interval=1m" % host
        try:
            r = requests.get(url, headers={"Origin": origin, "User-Agent": "indian-stock-finder-diagnostic"}, timeout=20)
            note("CORS probe %s: HTTP %s, allow-origin=%s, allow-credentials=%s, bytes=%d" % (
                host, r.status_code, r.headers.get("access-control-allow-origin"),
                r.headers.get("access-control-allow-credentials"), len(r.content)))
        except Exception as exc:
            note("CORS probe %s failed: %s" % (host, exc))


if __name__ == "__main__":
    main()
