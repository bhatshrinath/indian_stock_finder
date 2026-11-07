"""Market data layer.

Primary source : Yahoo Finance via `yfinance` (split/bonus adjusted, fast batch download).
Fallback source: NSE official daily "bhavcopy" archives (also gives delivery %).
Demo source    : synthetic random-walk prices, only for offline testing (--demo).

All loaders return a `Prices` object holding wide DataFrames (index = date,
columns = NSE symbol) so indicators can be computed for all stocks at once.
"""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from io import StringIO

import numpy as np
import pandas as pd
import requests

from . import config
from .universe import yahoo_ticker

log = logging.getLogger(__name__)

IST_OFFSET = timedelta(hours=5, minutes=30)


def now_ist() -> datetime:
    return datetime.utcnow() + IST_OFFSET


@dataclass
class Prices:
    open: pd.DataFrame
    high: pd.DataFrame
    low: pd.DataFrame
    close: pd.DataFrame
    volume: pd.DataFrame
    index_close: pd.Series                       # Nifty 50
    index_high: pd.Series | None = None
    index_low: pd.Series | None = None
    delivery_pct: pd.Series = field(default_factory=pd.Series)  # latest day, by symbol
    source: str = "unknown"
    index_pe: float | None = None
    index_pb: float | None = None

    @property
    def last_date(self) -> pd.Timestamp:
        return self.close.index[-1]

    def trim(self, symbols: list[str]) -> "Prices":
        keep = [s for s in symbols if s in self.close.columns]
        return Prices(self.open[keep], self.high[keep], self.low[keep], self.close[keep],
                      self.volume[keep], self.index_close, self.index_high, self.index_low,
                      self.delivery_pct, self.source, self.index_pe, self.index_pb)


# ======================================================================= yfinance
def load_yfinance(symbols: list[str], period: str = config.HISTORY_PERIOD) -> Prices | None:
    try:
        import yfinance as yf
    except ImportError:
        log.warning("yfinance not installed")
        return None

    tickers = [yahoo_ticker(s) for s in symbols] + [config.INDEX_SYMBOL]

    def download(per: str) -> pd.DataFrame | None:
        frames = []
        # chunked to be gentle with the endpoint and to survive partial failures
        for i in range(0, len(tickers), 60):
            chunk = tickers[i:i + 60]
            for attempt in range(3):
                try:
                    df = yf.download(chunk, period=per, interval="1d", auto_adjust=True,
                                     group_by="column", progress=False, threads=True)
                    if df is not None and not df.empty:
                        frames.append(df)
                        break
                except Exception as exc:
                    log.warning("yfinance %s chunk %d attempt %d failed: %s", per, i, attempt + 1, exc)
                time.sleep(2 * (attempt + 1))
        if not frames:
            return None
        raw = pd.concat(frames, axis=1)
        return raw.loc[:, ~raw.columns.duplicated()]

    raw = download(period)
    if raw is None:
        return None
    # Yahoo's long-range download often leaves out the latest session for stocks
    # (but not for the index). A short-range download includes it, so use it to
    # fill the most recent days.
    recent = download("1mo")

    def norm_index(ix):
        ix = pd.to_datetime(ix)
        if getattr(ix, "tz", None) is not None:
            ix = ix.tz_convert("Asia/Kolkata").tz_localize(None)
        return ix.normalize()

    # If stocks still lag the index, fetch the last few sessions one ticker at a
    # time: Yahoo's single-ticker history includes the latest day.
    singles: dict[str, pd.DataFrame] = {}
    probe = raw["Close"] if recent is None else recent["Close"]
    idx_last = probe[config.INDEX_SYMBOL].dropna().index.max() if config.INDEX_SYMBOL in probe else None
    stocks_last = probe.drop(columns=[config.INDEX_SYMBOL], errors="ignore").dropna(how="all").index.max()
    if idx_last is not None and stocks_last is not None and stocks_last < idx_last:
        from concurrent.futures import ThreadPoolExecutor

        def one(t):
            try:
                h = yf.Ticker(t).history(period="5d", interval="1d", auto_adjust=True)
                return t, h if h is not None and not h.empty else None
            except Exception:
                return t, None
        stock_tickers = [t for t in tickers if t != config.INDEX_SYMBOL]
        with ThreadPoolExecutor(max_workers=8) as ex:
            for t, h in ex.map(one, stock_tickers):
                if h is not None:
                    h.index = norm_index(h.index)
                    singles[t.replace(".NS", "")] = h
        log.info("Filled latest sessions for %d/%d stocks from single-ticker history",
                 len(singles), len(stock_tickers))

    def field_frame(name: str) -> pd.DataFrame:
        def prep(r):
            f = r[name].copy()
            f.columns = [c.replace(".NS", "") for c in f.columns]
            f.index = norm_index(f.index)
            return f[~f.index.duplicated(keep="last")]
        f = prep(raw)
        if recent is not None and name in recent.columns.get_level_values(0):
            f = f.combine_first(prep(recent))
        if singles:
            extra = pd.DataFrame({s: h[name] for s, h in singles.items() if name in h.columns})
            extra = extra[~extra.index.duplicated(keep="last")]
            f = extra.combine_first(f)            # fresh single-ticker values win
        return f.sort_index()

    close = field_frame("Close")
    if config.INDEX_SYMBOL not in close.columns or close[config.INDEX_SYMBOL].dropna().empty:
        log.warning("yfinance returned no index data")
        return None
    idx_close = close.pop(config.INDEX_SYMBOL)
    o, h, l, v = (field_frame(n) for n in ("Open", "High", "Low", "Volume"))
    idx_high, idx_low = h.pop(config.INDEX_SYMBOL), l.pop(config.INDEX_SYMBOL)
    o.pop(config.INDEX_SYMBOL)
    v.pop(config.INDEX_SYMBOL)

    # drop rows where the index didn't trade (holidays that sneak in)
    valid = idx_close.notna()
    close, o, h, l, v = (x.loc[valid] for x in (close, o, h, l, v))
    idx_close, idx_high, idx_low = idx_close[valid], idx_high[valid], idx_low[valid]

    good = close.columns[close.notna().sum() > 60]
    p = Prices(o[good], h[good], l[good], close[good], v[good], idx_close,
               idx_high, idx_low, source="Yahoo Finance")
    p = drop_incomplete_tail(p)
    log.info("yfinance: %d symbols x %d days, last %s", len(good), len(p.close), p.last_date.date())
    return p


def drop_incomplete_tail(p: Prices, min_coverage: float = 0.6) -> Prices:
    """Never base signals on a final day where most stocks have no price yet."""
    cov = p.close.notna().mean(axis=1)
    n = len(cov)
    while n > 1 and cov.iloc[n - 1] < min_coverage:
        n -= 1
    if n < len(cov):
        log.warning("Dropping %d incomplete trailing day(s) (coverage %.0f%%)",
                    len(cov) - n, 100 * cov.iloc[-1])
        cut = lambda x: x.iloc[:n] if x is not None else None
        p = Prices(cut(p.open), cut(p.high), cut(p.low), cut(p.close), cut(p.volume),
                   cut(p.index_close), cut(p.index_high), cut(p.index_low),
                   p.delivery_pct, p.source, p.index_pe, p.index_pb)
    return p


def live_quotes(symbols: list[str]) -> dict[str, dict]:
    """Latest intraday price for a handful of symbols (used by the live monitor)."""
    out: dict[str, dict] = {}
    if not symbols:
        return out
    try:
        import yfinance as yf
        from concurrent.futures import ThreadPoolExecutor
        tickers = [yahoo_ticker(s) for s in symbols] + [config.INDEX_SYMBOL]

        # one ticker at a time: Yahoo's batch download can omit the current session
        def one(t):
            try:
                return t, yf.Ticker(t).history(period="5d", interval="1d", auto_adjust=True)
            except Exception:
                return t, None
        with ThreadPoolExecutor(max_workers=8) as ex:
            hist = dict(ex.map(one, tickers))
        for t in tickers:
            try:
                h = hist.get(t)
                if h is None or h.empty:
                    continue
                c, hi, lo = h["Close"].dropna(), h["High"].dropna(), h["Low"].dropna()
                if c.empty:
                    continue
                key = "NIFTY" if t == config.INDEX_SYMBOL else t.replace(".NS", "")
                out[key] = {
                    "price": round(float(c.iloc[-1]), 2),
                    "open": round(float(h["Open"].dropna().iloc[-1]), 2) if "Open" in h else None,
                    "prev_close": round(float(c.iloc[-2]), 2) if len(c) > 1 else None,
                    "day_high": round(float(hi.iloc[-1]), 2),
                    "day_low": round(float(lo.iloc[-1]), 2),
                    "date": str(c.index[-1].date()),
                }
            except Exception:
                continue
    except Exception as exc:
        log.warning("live quotes failed: %s", exc)
    return out


# ======================================================================= NSE bhavcopy
BHAV_URL = "https://nsearchives.nseindia.com/products/content/sec_bhavdata_full_{d}.csv"
INDEX_URL = "https://nsearchives.nseindia.com/content/indices/ind_close_all_{d}.csv"


def _session() -> requests.Session:
    s = requests.Session()
    s.headers.update({"User-Agent": config.USER_AGENT, "Accept": "text/csv,*/*"})
    return s


def _fetch_day(sess: requests.Session, d: date) -> tuple[pd.DataFrame | None, pd.DataFrame | None]:
    """Return (equity bhavcopy, index closes) for a date; cached on disk."""
    tag = d.strftime("%d%m%Y")
    cache = config.CACHE_DIR / "bhav"
    cache.mkdir(parents=True, exist_ok=True)
    eq_file, ix_file, miss_file = cache / f"eq_{tag}.csv", cache / f"ix_{tag}.csv", cache / f"none_{tag}"
    if miss_file.exists():
        return None, None
    if not eq_file.exists():
        try:
            r = sess.get(BHAV_URL.format(d=tag), timeout=12)
            if r.status_code == 404 or "SYMBOL" not in r.text[:100]:
                if d < now_ist().date():
                    miss_file.touch()          # holiday/weekend: remember
                return None, None
            eq_file.write_text(r.text, encoding="utf-8")
            r2 = sess.get(INDEX_URL.format(d=tag), timeout=12)
            if r2.ok and "Index Name" in r2.text[:100]:
                ix_file.write_text(r2.text, encoding="utf-8")
            time.sleep(0.25)                   # be polite to NSE
        except (requests.Timeout, requests.ConnectionError):
            raise                              # NSE unreachable: let caller stop early
        except Exception as exc:
            log.warning("bhavcopy %s failed: %s", tag, exc)
            return None, None
    eq = pd.read_csv(eq_file, skipinitialspace=True)
    eq.columns = [c.strip() for c in eq.columns]
    ix = pd.read_csv(ix_file) if ix_file.exists() else None
    return eq, ix


def load_nse_bhavcopy(symbols: list[str], days: int = config.BHAV_HISTORY_DAYS) -> Prices | None:
    """Build adjusted OHLCV history from NSE daily archives.

    Split/bonus adjustment: NSE's PREV_CLOSE column is already adjusted for
    corporate actions, so ratio = PREV_CLOSE(t) / CLOSE(t-1) gives the factor
    to back-adjust all earlier prices.
    """
    sess = _session()
    end = now_ist().date()
    rows, idx_rows = [], []
    d = end
    cal_days = int(days * 1.5) + 10
    fetched = 0
    for _ in range(cal_days):
        if d.weekday() < 5:
            try:
                eq, ix = _fetch_day(sess, d)
            except (requests.Timeout, requests.ConnectionError) as exc:
                log.warning("NSE archives unreachable: %s", exc)
                return None
            if eq is not None:
                eq = eq[eq["SERIES"].str.strip().isin(["EQ", "BE"]) & eq["SYMBOL"].isin(symbols)]
                eq = eq.assign(date=pd.Timestamp(d))
                rows.append(eq)
                if ix is not None:
                    n = ix[ix["Index Name"].str.strip().str.upper() == "NIFTY 50"]
                    if not n.empty:
                        idx_rows.append({"date": pd.Timestamp(d),
                                         "high": float(n["High Index Value"].iloc[0]),
                                         "low": float(n["Low Index Value"].iloc[0]),
                                         "close": float(n["Closing Index Value"].iloc[0]),
                                         "pe": pd.to_numeric(n["P/E"].iloc[0], errors="coerce"),
                                         "pb": pd.to_numeric(n["P/B"].iloc[0], errors="coerce")})
                fetched += 1
                if fetched >= days:
                    break
        d -= timedelta(days=1)
    if len(rows) < 60 or len(idx_rows) < 60:
        log.warning("bhavcopy: not enough history (%d days)", len(rows))
        return None

    df = pd.concat(rows, ignore_index=True)
    num = ["PREV_CLOSE", "OPEN_PRICE", "HIGH_PRICE", "LOW_PRICE", "CLOSE_PRICE", "TTL_TRD_QNTY", "DELIV_PER"]
    for c in num:
        df[c] = pd.to_numeric(df[c], errors="coerce")
    df = df.sort_values(["SYMBOL", "date"]).drop_duplicates(["SYMBOL", "date"], keep="last")

    piv = lambda c: df.pivot(index="date", columns="SYMBOL", values=c).sort_index()
    o, h, l, c, v, pc = (piv(x) for x in ("OPEN_PRICE", "HIGH_PRICE", "LOW_PRICE", "CLOSE_PRICE",
                                         "TTL_TRD_QNTY", "PREV_CLOSE"))
    factor = adjustment_factor(pc, c)
    o, h, l, c = (x * factor for x in (o, h, l, c))
    v = v / factor

    ixdf = pd.DataFrame(idx_rows).set_index("date").sort_index()
    common = c.index.intersection(ixdf.index)
    last_day = df[df["date"] == df["date"].max()].set_index("SYMBOL")["DELIV_PER"]
    good = c.columns[c.loc[common].notna().sum() > 60]
    log.info("bhavcopy: %d symbols x %d days", len(good), len(common))
    return Prices(o.loc[common, good], h.loc[common, good], l.loc[common, good], c.loc[common, good],
                  v.loc[common, good], ixdf.loc[common, "close"], ixdf.loc[common, "high"],
                  ixdf.loc[common, "low"], delivery_pct=last_day.dropna(),
                  source="NSE bhavcopy", index_pe=_f(ixdf["pe"].iloc[-1]), index_pb=_f(ixdf["pb"].iloc[-1]))


def adjustment_factor(prev_close: pd.DataFrame, close: pd.DataFrame) -> pd.DataFrame:
    """Back-adjustment multipliers for splits/bonuses.

    NSE's PREV_CLOSE on the ex-date is already adjusted, so
    ratio(t) = PREV_CLOSE(t) / CLOSE(t-1) != 1 marks a corporate action, and every
    row *before* t must be multiplied by that ratio.
    """
    ratio = (prev_close / close.shift(1)).where(lambda x: (x - 1).abs() > 0.02, 1.0).fillna(1.0)
    return ratio[::-1].cumprod()[::-1].shift(-1).fillna(1.0)


def latest_nse_extras(symbols: list[str]) -> tuple[pd.Series, float | None, float | None]:
    """Delivery % for the latest session + Nifty P/E, P/B. Best effort only."""
    sess = _session()
    d = now_ist().date()
    for _ in range(7):
        if d.weekday() < 5:
            try:
                eq, ix = _fetch_day(sess, d)
            except (requests.Timeout, requests.ConnectionError) as exc:
                log.warning("NSE archives unreachable: %s", exc)
                return None
            if eq is not None:
                eq = eq[eq["SERIES"].str.strip() == "EQ"].set_index("SYMBOL")
                dp = pd.to_numeric(eq["DELIV_PER"], errors="coerce").reindex(symbols).dropna()
                pe = pb = None
                if ix is not None:
                    n = ix[ix["Index Name"].str.strip().str.upper() == "NIFTY 50"]
                    if not n.empty:
                        pe, pb = _f(n["P/E"].iloc[0]), _f(n["P/B"].iloc[0])
                return dp, pe, pb
        d -= timedelta(days=1)
    return pd.Series(dtype=float), None, None


def _f(x) -> float | None:
    try:
        x = float(x)
        return None if np.isnan(x) else round(x, 2)
    except (TypeError, ValueError):
        return None


# ======================================================================= demo
def load_demo(symbols: list[str], days: int = 900, seed: int = 33) -> Prices:
    """Synthetic data with trends/regimes so every code path can be exercised offline."""
    rng = np.random.default_rng(seed)
    dates = pd.bdate_range(end=pd.Timestamp(now_ist().date()), periods=days)
    mkt = np.cumsum(rng.normal(0.0006, 0.009, days))
    closes, vols = {}, {}
    for s in symbols:
        beta = rng.uniform(0.6, 1.4)
        drift = rng.normal(0.0002, 0.0006)
        idio = np.cumsum(rng.normal(drift, rng.uniform(0.012, 0.025), days))
        p = rng.uniform(100, 3000) * np.exp(beta * mkt + idio)
        closes[s] = p
        vols[s] = rng.lognormal(13, 0.5, days)
    c = pd.DataFrame(closes, index=dates)
    noise = lambda: pd.DataFrame(rng.uniform(0, 0.015, c.shape), index=dates, columns=c.columns)
    o = c.shift(1).fillna(c) * (1 + rng.normal(0, 0.004, c.shape))
    h = np.maximum(o, c) * (1 + noise())
    l = np.minimum(o, c) * (1 - noise())
    idx = pd.Series(18000 * np.exp(mkt), index=dates)
    dp = pd.Series(rng.uniform(25, 75, len(symbols)), index=symbols)
    return Prices(o, h, l, c, pd.DataFrame(vols, index=dates), idx, idx * 1.005, idx * 0.995,
                  delivery_pct=dp, source="DEMO (synthetic)", index_pe=21.5, index_pb=3.4)


# ======================================================================= entry point
def load_prices(symbols: list[str], demo: bool = False) -> Prices:
    if demo:
        return load_demo(symbols)
    p = load_yfinance(symbols)
    if p is not None and len(p.close) > 250:
        try:
            dp, pe, pb = latest_nse_extras(list(p.close.columns))
            p.delivery_pct, p.index_pe, p.index_pb = dp, pe, pb
        except Exception as exc:
            log.warning("NSE extras failed: %s", exc)
        return p
    log.warning("Falling back to NSE bhavcopy history")
    p = load_nse_bhavcopy(symbols)
    if p is None:
        raise RuntimeError("No market data source available (Yahoo and NSE both failed)")
    return p
