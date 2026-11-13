"""The long-term plan: what to buy with new money, what to keep, and when to sell everything.

These are exactly the rules tested on 18 years of real prices in engine/study.py (a test checks that the
ranking here and there agree):
  * Buy: the strongest Nifty 50 shares by 6- and 12-month price strength (the latest month skipped),
    rising (above the 200-day average, within 25% of the 1-year high), one per industry, next to a
    Nifty 50 index fund holding about a third of the money.
  * Keep: at each month-end review a share stays while it ranks among the strongest LT_KEEP_TOP.
  * Safety price: sell a share that falls 25% below its highest price since it was bought.
  * Market guard: sell everything (gold may stay) when the Nifty closes GUARD_BAND (3%) below its
    200-day average, and buy again once it closes back above the average.

The phone does not know which shares the user owns (that stays on the phone), so `signals` reports
"if you own X" sells for the shares the app suggested: it keeps a small log of when each share was
first suggested and applies the safety price from then.
"""
from __future__ import annotations

import dataclasses
import json
import logging
from datetime import date, timedelta

import numpy as np
import pandas as pd

from . import config
from . import indicators as ind
from .champions import clean_close
from .strategies import Features

log = logging.getLogger(__name__)

LOG_FILE = "suggested.json"
TEST_FILE = "longtest.json"     # the 18-year test, rebuilt weekly with the champions study
LOG_DAYS = 400              # forget a suggestion this long after it was last on the list


def _r(x, d=2):
    try:
        x = float(x)
    except (TypeError, ValueError):
        return None
    return round(x, d) if np.isfinite(x) else None


# ======================================================================= market guard
def guard_series(ix: pd.Series, band: float = config.GUARD_BAND) -> pd.Series:
    """True on days the guard says "be out of shares": out when the Nifty closes `band` below its
    200-day average, back in once it closes above the average (the gap stops sell-low-buy-high flapping)."""
    s200 = ix.rolling(200, min_periods=190).mean()
    out, state = [], False
    for x, s in zip(ix.to_numpy(float), s200.to_numpy(float)):
        if np.isfinite(x) and np.isfinite(s):
            state = (x < s) if state else (x < (1 - band) * s)
        out.append(state)
    return pd.Series(out, index=ix.index)


def guard_state(ix: pd.Series, band: float = config.GUARD_BAND) -> dict:
    ix = ix.dropna()
    g = guard_series(ix, band)
    s200 = ix.rolling(200, min_periods=190).mean()
    flips = g.index[g.ne(g.shift(1)).to_numpy() & (np.arange(len(g)) > 0)]
    sma = float(s200.iloc[-1]) if len(s200) else float("nan")
    return {"out": bool(g.iloc[-1]), "changed": bool(len(g) > 1 and g.iloc[-1] != g.iloc[-2]),
            "since": str(flips[-1].date()) if len(flips) else None,
            "nifty": _r(ix.iloc[-1]), "sma200": _r(sma), "sell_below": _r((1 - band) * sma), "buy_above": _r(sma),
            "band_pct": round(band * 100, 1), "below_avg": bool(ix.iloc[-1] < sma)}


# ======================================================================= ranking (same as study.Ctx.candidates, "mom")
def ranking(f: Features, universe: set[str], at: int = -1) -> list[str]:
    """Shares of `universe` that may be bought on day `at`, strongest first."""
    i = at if at >= 0 else len(f.close) + at
    if i < 260:
        return []
    raw = clean_close(f.close.iloc[:i + 1])
    cf = raw.ffill(limit=10)
    c = cf.iloc[i]
    sma200 = cf.rolling(200, min_periods=180).mean().iloc[i]
    hi252 = cf.rolling(252, min_periods=200).max().iloc[i]
    vol = ind.annual_vol(cf).iloc[i]
    m12 = cf.iloc[i - 21] / cf.iloc[i - 252] - 1
    m6 = cf.iloc[i - 21] / cf.iloc[i - 126] - 1
    recent = raw.iloc[max(0, i - 19):i + 1].notna().sum() >= 15
    uni = pd.Series(cf.columns.isin(list(universe)), index=cf.columns)
    elig = uni & recent & c.notna() & (c > 0) & (c > sma200) & (c > 0.75 * hi252) & vol.notna() & (vol > 0) \
        & m12.notna() & m6.notna()
    syms = elig[elig].index
    if len(syms) < 3:
        return []
    a, b = m12[syms] / vol[syms], m6[syms] / vol[syms]
    z = lambda s: ((s - s.mean()) / (s.std(ddof=1) or 1.0)).clip(-3, 3)
    score = 0.5 * z(a) + 0.5 * z(b)
    return list(score.sort_values(ascending=False, kind="stable").index)


def review_index(dates: pd.DatetimeIndex) -> int:
    """Row of the latest month-end review: today if tomorrow (the next weekday) is in a new month,
    otherwise the last trading day of the previous month."""
    last = dates[-1]
    nxt = last + timedelta(days=1)
    while nxt.weekday() >= 5:
        nxt += timedelta(days=1)
    if nxt.month != last.month:
        return len(dates) - 1
    prev = np.flatnonzero((dates.year * 12 + dates.month) < (last.year * 12 + last.month))
    return int(prev[-1]) if len(prev) else -1


# ======================================================================= suggestion log + "if you own it" sells
def load_log() -> dict:
    try:
        d = json.loads((config.OUT_DIR / LOG_FILE).read_text(encoding="utf-8"))
        return d if isinstance(d, dict) else {}
    except (OSError, ValueError):
        return {}


def save_log(d: dict) -> None:
    config.OUT_DIR.mkdir(parents=True, exist_ok=True)
    (config.OUT_DIR / LOG_FILE).write_text(json.dumps(d, separators=(",", ":"), sort_keys=True), encoding="utf-8")


def update_log(log_: dict, buy: list[str], day: str) -> dict:
    """Remember since when each share has been suggested (a new spell starts after a stop was hit or after
    it was off the list for a long time)."""
    out = {}
    cutoff = (date.fromisoformat(day) - timedelta(days=LOG_DAYS)).isoformat()
    for sym, e in log_.items():
        if e.get("last", "") >= cutoff:
            out[sym] = e
    for sym in buy:
        e = out.get(sym)
        if e is None or e.get("stopped"):
            out[sym] = {"from": day, "last": day}
        else:
            e["last"] = day
    return out


def stop_hits(log_: dict, f: Features, trail: float = config.HOLD_STOCK_TRAIL) -> list[dict]:
    """Suggested shares that closed `trail` below their highest close since first suggested (new hits only)."""
    hits = []
    c = f.close
    for sym, e in log_.items():
        if e.get("stopped") or sym not in c.columns:
            continue
        hist = c[sym].loc[e["from"]:].dropna()
        if len(hist) < 2:
            continue
        hi, last = float(hist.max()), float(hist.iloc[-1])
        stop = hi * (1 - trail)
        low = float(f.low[sym].loc[e["from"]:].iloc[-1]) if sym in f.low.columns else last
        if min(last, low if np.isfinite(low) else last) <= stop:
            e["stopped"] = str(c.index[-1].date())
            hits.append({"symbol": sym, "high": _r(hi), "stop": _r(stop), "price": _r(last), "from": e["from"]})
    return hits


# ======================================================================= the method, tested
def method(name: str = "My Plan's method", **kw):
    """The rules above as a study variant: what the Results screen and the 18-year test simulate."""
    from . import study
    base = dict(universe="n50", rank="mom", n=config.LT_PICKS, hold_mult=config.LT_KEEP_TOP / config.LT_PICKS,
                index_w=1 / 3, guard=f"g2:{config.GUARD_BAND}", trail=config.HOLD_STOCK_TRAIL)
    return study.Variant(name, **{**base, **kw})


def _brief(ctx, eq, closed=None) -> dict:
    from . import study
    s = study.summary(ctx, eq, closed)
    out = {"cagr": _r(100 * s["cagr"], 1), "worst_fall": _r(100 * s["maxdd"], 0), "under_water_months": _r(s["under_mo"], 0),
           "up_years": s["up_years"], "n_years": s["n_years"], "worst_year": s["worst_year"][0],
           "worst_year_pct": _r(100 * s["worst_year"][1], 0), "x": _r(s["x"], 1),
           "yearly": {str(y): _r(100 * r, 0) for y, r in s["yearly"].items()}}
    if closed is not None:
        out.update(sells_per_year=_r(s["sells_per_year"], 1), held_days=_r(s["held_days"], 0),
                   within_year_pct=_r(s["short_pct"], 0))
    return out


def app_backtest(p, uni: pd.DataFrame, n50: set[str], ixo: pd.Series | None = None) -> dict:
    """My Plan's long-term method on the data given (the daily 5 years), against the index fund held:
    the "Test: the app's long-term suggestions" card on the Results screen."""
    from . import study
    ctx = study.Ctx(p, ixo, uni, n50)
    if ctx.start >= ctx.T - 120:
        return {}
    eq, closed, _ = study.simulate(ctx, method())
    bq, _, _ = study.simulate(ctx, study.Variant("index", n=0, index_w=1.0, guard="none", trail=None))
    e = pd.Series(eq[ctx.start:], index=ctx.dates[ctx.start:])
    b = pd.Series(bq[ctx.start:], index=ctx.dates[ctx.start:])
    me, mb = e.resample("ME").last(), b.resample("ME").last()
    mr, br = me.pct_change().dropna(), mb.pct_change().dropna()
    s, sb = _brief(ctx, eq, closed), _brief(ctx, bq)
    return {"years": _r((e.index[-1] - e.index[0]).days / 365.25, 1), "cagr": s["cagr"], "bench_cagr": sb["cagr"],
            "max_drawdown": s["worst_fall"], "bench_max_drawdown": sb["worst_fall"],
            "pct_months_positive": _r(100 * (mr > 0).mean(), 1), "pct_months_beat_nifty": _r(100 * (mr > br).mean(), 1),
            "sells_per_year": s.get("sells_per_year"),
            "curve": [{"d": str(d.date()), "p": _r(100 * x / e.iloc[0], 1), "b": _r(100 * y / b.iloc[0], 1)}
                      for d, x, y in zip(me.index, me.to_numpy(), mb.to_numpy())]}


def long_test(p, uni: pd.DataFrame, n50: set[str], ixo: pd.Series | None = None) -> dict:
    """The same method over all of Yahoo's history (from 2008), with the old rules, the index fund and a
    stricter no-hindsight version for comparison. Rebuilt weekly with the long-term champions study."""
    from . import study
    ctx = study.Ctx(p, ixo, uni, n50)
    if ctx.start >= ctx.T - 252 * 3:
        return {}
    rows = {}
    for key, v in (("plan", method()),
                   ("old", method("Old rules", guard="g1", hold_mult=2.0)),
                   ("strict", method("No hindsight", universe="top50")),
                   ("index", study.Variant("Index fund", n=0, index_w=1.0, guard="none", trail=None)),
                   ("index_guard", study.Variant("Index fund + guard", n=0, index_w=1.0, guard=f"g2:{config.GUARD_BAND}", trail=None))):
        eq, closed, _ = study.simulate(ctx, v)
        rows[key] = _brief(ctx, eq, closed if v.n else None)
        taxed, closed_t, _ = study.simulate(ctx, dataclasses.replace(v, tax=True))
        rows[key]["after_tax"] = _brief(ctx, taxed)["cagr"]
    return {"from": str(ctx.dates[ctx.start].date()), "to": str(ctx.dates[-1].date()),
            "years": _r((ctx.dates[-1] - ctx.dates[ctx.start]).days / 365.25, 1), "rows": rows,
            "rules": {"guard_band_pct": config.GUARD_BAND * 100, "keep_top": config.LT_KEEP_TOP, "picks": config.LT_PICKS,
                      "stop_pct": config.HOLD_STOCK_TRAIL * 100, "index_part": "1/3"}}


def short_test(p, n50: set[str]) -> dict:
    """The short-term "safe dip" trade over all the history given (weekly, with the 18-year test). Its
    5-year record looked fine, but a method must also hold up over 18 years before the plan uses it."""
    from . import backtest
    from .strategies import SWING_STRATEGIES
    f = Features(p)
    f.set_large(n50)
    strat = next(s for s in SWING_STRATEGIES if s.key in config.SAFE_STRATEGIES)
    r = backtest.backtest_swing(f, strat)
    sim = backtest.portfolio_sim(f, strat, money=config.TRADE_AMOUNT, slots=1, per_day=1)
    first = f.close.index[0]
    years = (f.close.index[-1] - first).days / 365.25
    ok = bool(r.get("trades") and (r.get("avg_return") or 0) > 0
              and (r.get("profit_factor") or 0) >= config.SAFE_MIN_PROFIT_FACTOR)
    return {"method": strat.key, "from": str(first.date()), "years": _r(years, 1), "trades": r.get("trades", 0),
            "win_rate": r.get("win_rate"), "avg_return": r.get("avg_return"), "profit_factor": r.get("profit_factor"),
            "money": config.TRADE_AMOUNT, "trading_profit": sim.get("total_profit"),
            "liquid_fund_profit": round(config.TRADE_AMOUNT * ((1 + 0.06) ** years - 1)), "ok": ok}


def load_long_test() -> dict:
    try:
        return json.loads((config.OUT_DIR / TEST_FILE).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def publish_long_test(p, uni: pd.DataFrame, n50: set[str]) -> dict:
    """Write longtest.json (the Results screen and the short-trade safety gate read it)."""
    from datetime import datetime, timezone
    out = {"generated": datetime.now(timezone.utc).isoformat(), **long_test(p, uni, n50)}
    try:
        out["short_term"] = short_test(p, n50)
    except Exception as exc:
        log.warning("short-term long test failed: %s", exc)
    config.OUT_DIR.mkdir(parents=True, exist_ok=True)
    (config.OUT_DIR / TEST_FILE).write_text(json.dumps(out, separators=(",", ":"), ensure_ascii=False), encoding="utf-8")
    log.info("wrote %s: plan %s, short term %s", TEST_FILE, (out.get("rows") or {}).get("plan", {}).get("cagr"),
             (out.get("short_term") or {}).get("ok"))
    return out
