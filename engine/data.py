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
from datetime import date, datetime, timedelta, timezone
from io import StringIO

import numpy as np
import pandas as pd
import requests

from . import config
from .universe import yahoo_ticker

log = logging.getLogger(__name__)

IST_OFFSET = timedelta(hours=5, minutes=30)
LIVE_FRESH_SECONDS = 20 * 60


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


def _number(value, digits=2):
    """Finite float rounded for JSON, or None for a missing market-data value."""
    try:
        value = float(value)
    except (TypeError, ValueError):
        return None
    return round(value, digits) if np.isfinite(value) else None


def _as_ist_index(frame: pd.DataFrame) -> pd.DataFrame:
    """Normalise a Yahoo history frame to a timezone-aware IST index."""
    out = frame.copy()
    idx = pd.DatetimeIndex(pd.to_datetime(out.index))
    if idx.tz is None:
        idx = idx.tz_localize("Asia/Kolkata")
    else:
        idx = idx.tz_convert("Asia/Kolkata")
    out.index = idx
    return out[~out.index.duplicated(keep="last")].sort_index()


def intraday_quote(history: pd.DataFrame, now: datetime | None = None) -> dict | None:
    """Convert one ticker's 1-minute history into a compact session snapshot.

    This function is deliberately pure so quote freshness and all displayed
    intraday metrics can be tested without contacting a data provider.
    """
    if history is None or history.empty or "Close" not in history:
        return None
    h = _as_ist_index(history)
    h = h[h["Close"].notna()].between_time("09:15", "15:30")
    if h.empty:
        return None

    session_date = h.index[-1].date()
    current = h[np.asarray(h.index.date) == session_date]
    if current.empty:
        return None
    previous = h[np.asarray(h.index.date) < session_date]

    close = pd.to_numeric(current["Close"], errors="coerce").dropna()
    if close.empty:
        return None
    latest_at = close.index[-1]
    price = float(close.iloc[-1])
    open_ = _number(current["Open"].dropna().iloc[0]) if "Open" in current and current["Open"].notna().any() else None
    high = _number(pd.to_numeric(current.get("High"), errors="coerce").max()) if "High" in current else None
    low = _number(pd.to_numeric(current.get("Low"), errors="coerce").min()) if "Low" in current else None
    prev = _number(pd.to_numeric(previous["Close"], errors="coerce").dropna().iloc[-1]) \
        if not previous.empty and previous["Close"].notna().any() else None

    volume = pd.to_numeric(current.get("Volume", pd.Series(index=current.index, dtype=float)),
                           errors="coerce").fillna(0).clip(lower=0)
    total_volume = float(volume.sum())
    vwap = None
    if total_volume > 0 and {"High", "Low", "Close"}.issubset(current.columns):
        typical = (pd.to_numeric(current["High"], errors="coerce")
                   + pd.to_numeric(current["Low"], errors="coerce")
                   + pd.to_numeric(current["Close"], errors="coerce")) / 3
        valid = typical.notna() & volume.gt(0)
        if valid.any():
            vwap = _number((typical[valid] * volume[valid]).sum() / volume[valid].sum())

    def return_over(minutes: int):
        before = close[close.index <= latest_at - pd.Timedelta(minutes=minutes)]
        if before.empty or not before.iloc[-1]:
            return None
        return _number((price / float(before.iloc[-1]) - 1) * 100)

    now_ts = pd.Timestamp(now or datetime.now(timezone.utc))
    if now_ts.tz is None:
        now_ts = now_ts.tz_localize("UTC")
    else:
        now_ts = now_ts.tz_convert("UTC")
    latest_utc = latest_at.tz_convert("UTC")
    delay = max(0, int((now_ts - latest_utc).total_seconds()))
    today_ist = now_ts.tz_convert("Asia/Kolkata").date()
    fresh = session_date == today_ist and delay <= LIVE_FRESH_SECONDS
    change = price - prev if prev is not None else None
    day_range = (high - low) if high is not None and low is not None else None
    range_position = ((price - low) / day_range * 100) if day_range and day_range > 0 else None

    return {
        "price": _number(price),
        "open": open_,
        "prev_close": prev,
        "day_high": high,
        "day_low": low,
        "date": session_date.isoformat(),
        "asof": latest_utc.isoformat(),
        "source": "Yahoo Finance intraday",
        "is_intraday": True,
        "is_fresh": fresh,
        "stale": not fresh,
        "delay_seconds": delay,
        "change": _number(change),
        "change_pct": _number(change / prev * 100) if change is not None and prev else None,
        "volume": int(total_volume),
        "vwap": vwap,
        "return_5m": return_over(5),
        "return_15m": return_over(15),
        "range_position": _number(range_position, 1),
    }


def daily_quote(history: pd.DataFrame, now: datetime | None = None) -> dict | None:
    """Compatibility fallback when Yahoo does not return intraday bars.

    Daily bars are always marked stale: they are useful for display, but must
    never be allowed to fire an intraday stop/target notification.
    """
    if history is None or history.empty or "Close" not in history:
        return None
    h = _as_ist_index(history)
    close = pd.to_numeric(h["Close"], errors="coerce").dropna()
    if close.empty:
        return None
    last_at = close.index[-1]
    row = h.loc[last_at]
    price = float(close.iloc[-1])
    prev = _number(close.iloc[-2]) if len(close) > 1 else None
    high = _number(row.get("High"))
    low = _number(row.get("Low"))
    volume = _number(row.get("Volume"), 0) or 0
    change = price - prev if prev is not None else None
    now_ts = pd.Timestamp(now or datetime.now(timezone.utc))
    now_ts = now_ts.tz_localize("UTC") if now_ts.tz is None else now_ts.tz_convert("UTC")
    session_close = pd.Timestamp.combine(last_at.date(), pd.Timestamp("15:30").time()) \
        .tz_localize("Asia/Kolkata").tz_convert("UTC")
    # Yahoo can expose today's still-forming daily candle before the close.
    # Never label that fallback with a timestamp in the future.
    asof = min(session_close, now_ts)
    return {
        "price": _number(price),
        "open": _number(row.get("Open")),
        "prev_close": prev,
        "day_high": high,
        "day_low": low,
        "date": last_at.date().isoformat(),
        "asof": asof.isoformat(),
        "source": "Yahoo Finance daily fallback",
        "is_intraday": False,
        "is_fresh": False,
        "stale": True,
        "delay_seconds": max(0, int((now_ts - asof).total_seconds())),
        "change": _number(change),
        "change_pct": _number(change / prev * 100) if change is not None and prev else None,
        "volume": int(volume),
        "vwap": None,
        "return_5m": None,
        "return_15m": None,
        "range_position": _number((price - low) / (high - low) * 100, 1)
        if high is not None and low is not None and high > low else None,
    }


def live_quotes(symbols: list[str]) -> dict[str, dict]:
    """Near-live 1-minute session snapshots for the monitored symbols.

    Yahoo is a free, best-effort source rather than an exchange-grade feed.  A
    daily-bar fallback keeps the screen informative during provider gaps, while
    explicit freshness fields prevent that fallback from triggering alerts.
    """
    out: dict[str, dict] = {}
    try:
        import yfinance as yf
        from concurrent.futures import ThreadPoolExecutor
        tickers = list(dict.fromkeys([yahoo_ticker(s) for s in symbols] + [config.INDEX_SYMBOL]))

        # One ticker at a time: Yahoo's batch endpoint often omits the current
        # session. Fetch the small monitored list concurrently and fall back to
        # a daily candle only when no minute history is available.
        def one(t):
            try:
                ticker = yf.Ticker(t)
                minute = ticker.history(period="5d", interval="1m", auto_adjust=True, prepost=False)
                quote = intraday_quote(minute)
                if quote is None:
                    daily = ticker.history(period="5d", interval="1d", auto_adjust=True)
                    quote = daily_quote(daily)
                return t, quote
            except Exception:
                return t, None
        with ThreadPoolExecutor(max_workers=8) as ex:
            hist = dict(ex.map(one, tickers))
        for t in tickers:
            try:
                quote = hist.get(t)
                if quote is None:
                    continue
                key = "NIFTY" if t == config.INDEX_SYMBOL else t.replace(".NS", "")
                out[key] = quote
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
def drop_unfinished_session(p: Prices, now: datetime | None = None) -> Prices:
    """The daily engine must only see COMPLETED sessions. While today's session is still running
    (or Yahoo has not finalised it, before 4:15 PM IST), today's partial candle is ignored."""
    now = now or now_ist()
    if len(p.close) < 2:
        return p
    last = p.close.index[-1].date()
    finished = now.date() > last or (now.date() == last and now.hour * 60 + now.minute >= 16 * 60 + 15)
    if finished:
        return p
    log.info("Ignoring today's unfinished session (%s)", last)
    cut = lambda x: x.iloc[:-1] if x is not None else None
    return Prices(cut(p.open), cut(p.high), cut(p.low), cut(p.close), cut(p.volume),
                  cut(p.index_close), cut(p.index_high), cut(p.index_low),
                  p.delivery_pct, p.source, p.index_pe, p.index_pb)


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
        return drop_unfinished_session(p)
    log.warning("Falling back to NSE bhavcopy history")
    p = load_nse_bhavcopy(symbols)
    if p is None:
        raise RuntimeError("No market data source available (Yahoo and NSE both failed)")
    return drop_unfinished_session(p)
