"""Long-term champions: shares that rise year after year and bounce back fastest after crashes.

The idea (a common rule of thumb among long-term investors): the best shares to own for years
  1. go up in most years,
  2. hold up in crashes: fall much less than the market, or get back to their old price sooner
     (2008 world financial crash, 2011, 2015-16, COVID 2020, 2022, ...),
  3. grow faster than the Nifty over the long run,
  4. are steady: holding them for any 3 years almost always made money,
and, as a business, 5. carry little debt and 6. earn real profits.

Everything here is measured on ~19 years of real prices (Yahoo's Nifty history starts in Sept 2007).
`rule_test` checks honestly whether choosing shares this way in the past, using only the data
available at the time, would have beaten the Nifty afterwards.

    python -m engine.run --mode champions        # build docs/data/champions.json now
"""
from __future__ import annotations

import json
import logging
import math
from datetime import datetime, timezone

import numpy as np
import pandas as pd

from . import config, data, fundamentals, news

log = logging.getLogger(__name__)

VERSION = 3                    # bump when the study changes, so the weekly file is rebuilt at once
FILE = "champions.json"
REFRESH_DAYS = 6               # the study changes slowly: rebuilt about once a week
CRASH_MIN_FALL = 0.15          # a "crash" = the Nifty fell at least 15% from its high
MIN_YEARS = 10                 # a champion has a long record: listed for at least 10 years
MIN_CRASHES = 2                # ... and came through at least 2 finished market crashes
MONTH_DAYS = 30.44

# the signs, as plain rules (the app shows them as ticks)
UP_YEARS_MIN = 70              # rose in at least 7 of every 10 calendar years
ROLL3_MIN = 85                 # any 3-year holding period made money at least 85% of the time
FELL_LESS = 0.7                # "holds up in crashes": back sooner than the Nifty, OR fell at most 70% as far
VOL_MAX = 35                   # yearly price swing (volatility) at most 35%
DEBT_MAX = 100                 # debt/equity at most 1.0x (Yahoo reports it in %)
ROE_MIN = 12                   # return on equity at least 12%
PE_MAX = 60                    # above this the price is rich: buy in small parts
MIN_SCORE = 50                 # and ranks in the better half overall

TEST_STARTS = (2013, 2015, 2017, 2019, 2021, 2023)
TEST_PICKS = 10

CRASH_NAMES = (  # by the year the Nifty hit bottom
    ((2008, 2009), "2008 world financial crash", "2008"),
    ((2010, 2011, 2012), "2011 euro debt scare", "2011"),
    ((2015, 2016), "2015-16 slowdown", "2016"),
    ((2018, 2019), "2018 IL&FS scare", "2018"),
    ((2020,), "COVID crash", "COVID 2020"),
    ((2021, 2022, 2023), "2022 war & inflation", "2022"),
    ((2024, 2025), "2024-25 correction", "2025"),
)


# ======================================================================= helpers
def months_between(a, b) -> float:
    return (pd.Timestamp(b) - pd.Timestamp(a)).days / MONTH_DAYS


def crash_name(peak: pd.Timestamp, trough: pd.Timestamp) -> tuple[str, str]:
    for years, name, short in CRASH_NAMES:
        if trough.year in years:
            return name, short
    return f"Fall of {peak:%b %Y}", f"{trough.year}"


def clean_close(close: pd.DataFrame) -> pd.DataFrame:
    """Drop one-day data errors (a price that jumps >35% and straight back), which would fake a crash."""
    prev = close.ffill().shift(1)
    nxt = close.bfill().shift(-1)
    up = close / prev - 1
    back = close / nxt - 1
    spike = (up.abs() > 0.35) & (back.abs() > 0.35) & ((up > 0) == (back > 0))
    return close.mask(spike)


def sig3(v):
    """Round to 3 significant digits (keeps the published file small)."""
    if v is None or not np.isfinite(v) or v <= 0:
        return None
    v = float(f"{v:.3g}")
    return int(v) if v >= 100 else v


def _r(x, d=1):
    try:
        x = float(x)
    except (TypeError, ValueError):
        return None
    return round(x, d) if np.isfinite(x) else None


def _clean(o):
    if isinstance(o, dict):
        return {str(k): _clean(v) for k, v in o.items()}
    if isinstance(o, (list, tuple)):
        return [_clean(v) for v in o]
    if isinstance(o, np.integer):
        return int(o)
    if isinstance(o, (np.floating, float)):
        f = float(o)
        return None if math.isnan(f) or math.isinf(f) else f
    if isinstance(o, np.bool_):
        return bool(o)
    if isinstance(o, (pd.Timestamp, datetime)):
        return o.strftime("%Y-%m-%d")
    return o


# ======================================================================= crashes
def find_crashes(ix: pd.Series, min_fall: float = CRASH_MIN_FALL) -> list[dict]:
    """Every time the Nifty fell at least `min_fall` from a high: when it peaked, bottomed and got back."""
    ix = ix.dropna()
    out: list[dict] = []
    if len(ix) < 2:
        return out
    vals, dates = ix.to_numpy(float), ix.index
    peak_i = trough_i = 0
    deep = False

    def record(r):
        peak, trough = dates[peak_i], dates[trough_i]
        name, short = crash_name(peak, trough)
        out.append({"name": name, "short": short, "peak": peak, "trough": trough,
                    "recovered": dates[r] if r is not None else None,
                    "fall": round(100 * (vals[trough_i] / vals[peak_i] - 1), 1),
                    "months_down": round(months_between(peak, trough), 1),
                    "months": round(months_between(peak, dates[r]), 1) if r is not None else None,
                    "months_so_far": round(months_between(peak, dates[-1]), 1)})

    for i in range(1, len(vals)):
        v = vals[i]
        if v >= vals[peak_i]:
            if deep:
                record(i)
                deep = False
            peak_i = trough_i = i
            continue
        if v < vals[trough_i]:
            trough_i = i
        if vals[trough_i] <= vals[peak_i] * (1 - min_fall):
            deep = True
    if deep:
        record(None)
    return out


def crash_effects(D: pd.DataFrame, crash: dict) -> dict[str, dict]:
    """How far each share fell from its price on the day the Nifty peaked, and how many months
    (counted from that day, like the Nifty) it took to get back to that price."""
    peak = crash["peak"]
    before = D.loc[:peak]
    if before.empty:
        return {}
    p0 = before.ffill().iloc[-1]
    listed = before.notna().sum() >= 20                 # traded for at least a month before the fall
    after = D.loc[peak:]
    arr, dates = after.to_numpy(float), after.index
    nwin = int((dates <= crash["trough"] + pd.Timedelta(days=92)).sum())
    out = {}
    for j, s in enumerate(D.columns):
        base = p0.iloc[j]
        if not listed.iloc[j] or not np.isfinite(base) or base <= 0:
            continue
        col = arr[:, j]
        win = col[:nwin]
        if not np.isfinite(win).any():
            continue
        k = int(np.nanargmin(win))
        low = win[k]
        if low >= base:
            months, rec = 0.0, True
        else:
            with np.errstate(invalid="ignore"):
                hit = np.nonzero(col[k:] >= base)[0]
            rec = bool(hit.size)
            months = months_between(peak, dates[k + hit[0]] if rec else dates[-1])
        out[s] = {"fall": round(100 * min(0.0, low / base - 1), 1), "months": round(months, 1), "recovered": rec}
    return out


# ======================================================================= the study
def _cagr(first, last, years):
    if not (np.isfinite(first) and np.isfinite(last)) or first <= 0 or last <= 0 or years < 1:
        return np.nan
    return 100 * ((last / first) ** (1 / years) - 1)


def study(D: pd.DataFrame, ix: pd.Series, min_years: float = MIN_YEARS,
          min_crashes: int = MIN_CRASHES) -> dict:
    """Price facts, the four price signs and a 0-100 score for every share, using only the data given."""
    ix = ix.dropna()
    D = D.reindex(ix.index)
    D = D.loc[:, D.notna().sum() > 260]
    end = ix.index[-1]
    crashes = find_crashes(ix)
    effects = [crash_effects(D, c) for c in crashes]

    # calendar years (the last one is "this year so far" unless the data reaches the end of December)
    Y = D.resample("YE").last()
    yix = ix.resample("YE").last()
    partial = end < pd.Timestamp(end.year, 12, 24)
    yr = (Y / Y.shift(1) - 1) * 100
    yr_ix = (yix / yix.shift(1) - 1) * 100
    full = yr.iloc[:-1] if partial else yr
    full_ix = yr_ix.iloc[:-1] if partial else yr_ix
    years = [int(d.year) for d in full_ix.index[1:]]
    full, full_ix = full.iloc[1:], full_ix.iloc[1:]

    # any 3 years held (monthly starting points)
    M = D.resample("ME").last()
    mix = ix.resample("ME").last()
    r3 = M / M.shift(36) - 1
    r3_ix = mix / mix.shift(36) - 1

    with np.errstate(all="ignore"):
        vol = np.log(D).diff().std() * math.sqrt(252) * 100
    filled = D.ffill()
    max_dd = (filled / filled.cummax() - 1).min() * 100

    first_date = D.apply(lambda c: c.first_valid_index())
    rows = []
    for s in D.columns:
        f0 = first_date[s]
        yrs = (end - f0).days / 365.25
        col = D[s].dropna()
        cagr = _cagr(col.iloc[0], col.iloc[-1], yrs)
        ix0 = ix.asof(f0)
        n_cagr = _cagr(ix0, ix.iloc[-1], yrs)
        ys = full[s].dropna()
        n_up = int((ys > 0).sum())
        up_pct = 100 * n_up / len(ys) if len(ys) else np.nan
        w3 = r3[s].dropna()
        roll3 = 100 * float((w3 > 0).mean()) if len(w3) >= 12 else np.nan
        worst3 = 100 * float(w3.min()) if len(w3) >= 12 else np.nan

        cr, logs, depth = [], [], []
        fin_lived = fin_rec = 0
        s_months, n_months, s_falls, n_falls = [], [], [], []
        for c, eff in zip(crashes, effects):
            e = eff.get(s)
            cr.append(e)
            if not e:
                continue
            nm = c["months"] if c["months"] is not None else c["months_so_far"]
            logs.append(math.log((e["months"] + 1) / (nm + 1)))
            # how far it fell compared with the Nifty (falls in %, negative): 0.5 = fell half as far
            depth.append(math.log((abs(e["fall"]) + 2) / (abs(c["fall"]) + 2)))
            if c["months"] is not None:                 # the market itself has recovered
                fin_lived += 1
                fin_rec += e["recovered"]
                s_months.append(e["months"])
                n_months.append(c["months"])
                s_falls.append(abs(e["fall"]))
                n_falls.append(abs(c["fall"]))
        # falling less counts as much as getting back sooner
        bounce_raw = (-float(np.mean(logs)) - float(np.mean(depth))) if logs else np.nan
        fast = bool(fin_lived and fin_rec == fin_lived and (
            np.median(s_months) <= np.median(n_months) or np.median(s_falls) <= FELL_LESS * np.median(n_falls)))
        signs = {
            "rises": bool(len(ys) >= 3 and up_pct >= UP_YEARS_MIN),
            "bounce": fast,
            "beats": bool(np.isfinite(cagr) and np.isfinite(n_cagr) and cagr > n_cagr),
            "steady": bool(np.isfinite(roll3) and roll3 >= ROLL3_MIN and vol[s] <= VOL_MAX),
        }
        rows.append({
            "symbol": s, "first": f0, "years": round(yrs, 1), "cagr": _r(cagr), "nifty_cagr": _r(n_cagr),
            "up_years": n_up, "n_years": len(ys), "up_pct": _r(up_pct, 0), "roll3": _r(roll3, 0),
            "worst3": _r(worst3, 0), "vol": _r(vol[s], 0), "max_dd": _r(max_dd[s], 0),
            "crashes": [[e["fall"], e["months"], int(e["recovered"])] if e else None for e in cr],
            "crashes_lived": fin_lived, "crashes_recovered": fin_rec,
            "yearly": [_r(full.at[d, s], 0) for d in full.index],
            "ytd": _r(yr[s].iloc[-1], 0) if partial else None,
            "signs": signs, "_bounce": bounce_raw,
            "price_champion": bool(yrs >= min_years and fin_lived >= min_crashes and all(signs.values())),
        })

    # 0-100 score from the share's rank on each quality (higher = better)
    if rows:
        t = pd.DataFrame(rows).set_index("symbol")
        rank = lambda x: pd.Series(x, index=t.index).rank(pct=True).fillna(0.3)
        consistency = rank((t.up_pct.astype(float) + t.roll3.astype(float)) / 2)
        bounce = rank(t._bounce.astype(float))
        growth = rank(t.cagr.astype(float))
        calm = (rank(-t.vol.astype(float)) + rank(t.max_dd.astype(float))) / 2
        score = 100 * (0.3 * consistency + 0.3 * bounce + 0.25 * growth + 0.15 * calm)
        for r in rows:
            r["score"] = int(round(score[r["symbol"]]))
            del r["_bounce"]
        rows.sort(key=lambda r: -r["score"])

    nys = full_ix.dropna()
    nw3 = r3_ix.dropna()
    n0 = float(ix.iloc[0])
    nifty = {"years": round((end - ix.index[0]).days / 365.25, 1),
             "cagr": _r(_cagr(n0, float(ix.iloc[-1]), (end - ix.index[0]).days / 365.25)),
             "up_years": int((nys > 0).sum()), "n_years": len(nys),
             "up_pct": _r(100 * (nys > 0).mean(), 0) if len(nys) else None,
             "roll3": _r(100 * (nw3 > 0).mean(), 0) if len(nw3) else None,
             "worst3": _r(100 * nw3.min(), 0) if len(nw3) else None,
             "yearly": [_r(v, 0) for v in nys.tolist()],
             "ytd": _r(yr_ix.iloc[-1], 0) if partial else None}
    return {"end": end, "crashes": crashes, "effects": effects, "years": years,
            "ytd_year": int(end.year) if partial else None, "rows": rows, "nifty": nifty, "M": M, "mix": mix}


# ======================================================================= honest test
def _choose(rows: list[dict], inds: dict, top: int, min_years: float, per_industry: int = 2) -> tuple[list[str], int]:
    champs = [r for r in rows if r["price_champion"]]
    pool = champs + [r for r in rows if not r["price_champion"] and r["years"] >= min_years]
    out, used = [], {}
    for r in pool:
        ind = inds.get(r["symbol"], r["symbol"])
        if used.get(ind, 0) >= per_industry:
            continue
        used[ind] = used.get(ind, 0) + 1
        out.append(r["symbol"])
        if len(out) >= top:
            break
    return out, len(champs)


def _hold(fwd: pd.DataFrame) -> pd.Series:
    """Equal amounts in each share on day one, then simply held (value of 1 rupee)."""
    fwd = fwd.ffill()
    start = fwd.iloc[0]
    ok = start[start > 0].index
    if not len(ok):
        return pd.Series(dtype=float)
    return (fwd[ok] / start[ok]).mean(axis=1)


def _summ(v: pd.Series, years: float) -> dict:
    v = v.dropna()
    if v.empty:
        return {}
    return {"cagr": _r(_cagr(float(v.iloc[0]), float(v.iloc[-1]), years)),
            "x": _r(float(v.iloc[-1] / v.iloc[0]), 2),
            "worst": _r(100 * float((v / v.cummax() - 1).min()), 0)}


def rule_test(D: pd.DataFrame, ix: pd.Series, inds: dict, full_crashes: list[dict], full_effects: list[dict],
              starts=TEST_STARTS, top: int = TEST_PICKS, min_years: float = 5) -> list[dict]:
    """For each start year: pick the champions using ONLY the prices before that year, buy them on the
    first day of the year, hold to today. Compared with the Nifty and with an equal amount in every
    share of the same list (the fair comparison: both use today's list of companies)."""
    ix = ix.dropna()
    D = D.reindex(ix.index)
    out = []
    for y in starts:
        cut = pd.Timestamp(y, 1, 1)
        hist_ix = ix.loc[:cut - pd.Timedelta(days=1)]
        fwd_ix = ix.loc[cut:]
        if len(hist_ix) < 252 * min_years or len(fwd_ix) < 250:
            continue
        hist = D.loc[:hist_ix.index[-1]]
        st = study(hist, hist_ix, min_years=min_years, min_crashes=1)
        alive = hist.iloc[-10:].notna().any()
        rows = [r for r in st["rows"] if alive.get(r["symbol"], False)]
        picks, n_champ = _choose(rows, inds, top, min_years)
        if not picks:
            continue
        fwd = D.loc[fwd_ix.index[0]:]
        everyone = [r["symbol"] for r in rows if r["years"] >= 1]
        yrs = (fwd_ix.index[-1] - fwd_ix.index[0]).days / 365.25
        row = {"start": y, "years": round(yrs, 1), "picks": picks, "n_champions": n_champ,
               "champ": _summ(_hold(fwd[picks]), yrs), "all": _summ(_hold(fwd[everyone]), yrs),
               "nifty": _summ(fwd_ix, yrs)}
        # the next crash after the picks were made: did they bounce back faster?
        nxt = next((i for i, c in enumerate(full_crashes) if c["peak"] >= cut), None)
        if nxt is not None:
            c, eff = full_crashes[nxt], full_effects[nxt]
            med = lambda syms, k: _r(np.median([eff[s][k] for s in syms if s in eff]), 0) if any(s in eff for s in syms) else None
            row["next_crash"] = {"name": c["name"], "short": c["short"],
                                 "champ_months": med(picks, "months"), "all_months": med(everyone, "months"),
                                 "nifty_months": _r(c["months"] if c["months"] is not None else c["months_so_far"], 0),
                                 "nifty_recovered": c["months"] is not None,
                                 "champ_fall": med(picks, "fall"), "all_fall": med(everyone, "fall"),
                                 "nifty_fall": c["fall"]}
        out.append(row)
    return out


# ======================================================================= business checks
def business_signs(f: dict) -> dict:
    """True / False / None (unknown) for: little debt, real profits, fair price, pays dividends."""
    if not f:
        return {"debt": None, "profit": None, "price": None, "dividend": None}
    de, roe, pm, pe, dy = (f.get(k) for k in ("debt_to_equity", "roe", "profit_margin", "pe", "dividend_yield"))
    profit = None
    if pm is not None and pm < 0:
        profit = False
    elif roe is not None:
        profit = roe >= ROE_MIN
    return {"debt": None if de is None else de <= DEBT_MAX,
            "profit": profit,
            "price": None if pe is None else (0 < pe <= PE_MAX),
            "dividend": True if (dy or 0) > 0 else None}


def verdict(r: dict) -> str:
    """champion / strong / average / risky, for the "check any stock" report card."""
    sg, b = r["signs"], r.get("business", {})
    if r.get("champion"):
        return "champion"
    risky = (r["crashes_lived"] and r["crashes_recovered"] < r["crashes_lived"]) or (r["vol"] or 0) > 45 \
        or b.get("profit") is False or b.get("debt") is False or r.get("red_flags")
    if risky:
        return "risky"
    if sum(sg.values()) >= 3 and r["score"] >= 55:
        return "strong"
    return "average"


# ======================================================================= build + publish
def build(p: data.Prices, uni: pd.DataFrame, n50: set[str], use_fundamentals: bool = True,
          use_news: bool = True) -> dict:
    D = clean_close(p.close)
    ix = p.index_close.dropna()
    names = dict(zip(uni.symbol, uni.name))
    inds = dict(zip(uni.symbol, uni.industry))
    st = study(D, ix)
    rows = st["rows"]
    log.info("champions study: %d shares, %d crashes, %d price champions", len(rows), len(st["crashes"]),
             sum(r["price_champion"] for r in rows))

    # business numbers for the best-scoring shares (best effort; unknown never counts against a share)
    fund = {}
    if use_fundamentals:
        want = [r["symbol"] for r in rows if r["price_champion"]]
        want += [r["symbol"] for r in rows[:60] if r["symbol"] not in want]
        try:
            fund = fundamentals.fetch(want, workers=6)
        except Exception as exc:
            log.warning("champions fundamentals failed: %s", exc)
    for r in rows:
        fd = fund.get(r["symbol"], {})
        r["fundamentals"] = {k: fd[k] for k in ("pe", "roe", "debt_to_equity", "profit_margin", "dividend_yield",
                                                "market_cap") if fd.get(k) is not None}
        r["business"] = business_signs(fund.get(r["symbol"], {}))
        r["red_flags"] = []

    cands = [r for r in rows if r["price_champion"] and r["score"] >= MIN_SCORE and r["business"]["debt"] is not False
             and r["business"]["profit"] is not False]
    if use_news and cands:
        try:
            nw = news.news_for({r["symbol"]: names.get(r["symbol"], r["symbol"]) for r in cands[:20]})
            for r in cands[:20]:
                r["red_flags"] = nw.get(r["symbol"], {}).get("red_flags", [])
                r["news"] = nw.get(r["symbol"], {}).get("items", [])[:3]
        except Exception as exc:
            log.warning("champions news failed: %s", exc)

    # today: price, distance from the 1-year high, and whether the price trend is rising now
    sma200 = D.rolling(200, min_periods=150).mean()
    last = D.ffill().iloc[-1]
    hi1y = D.iloc[-252:].max()
    M, mix = st["M"], st["mix"]
    months = [d.strftime("%Y-%m") for d in mix.index]
    for r in rows:
        s = r["symbol"]
        r["champion"] = bool(r["price_champion"] and r["score"] >= MIN_SCORE and r["business"]["debt"] is not False
                             and r["business"]["profit"] is not False and not r["red_flags"])
        r["name"] = names.get(s, s)
        r["industry"] = inds.get(s, "")
        r["size"] = "Nifty 50" if s in n50 else "Nifty 200"
        r["price"] = _r(last[s], 2)
        r["from_high"] = _r(100 * (last[s] / hi1y[s] - 1), 0)
        sm, sm_prev = sma200[s].iloc[-1], sma200[s].iloc[-21]
        r["trend"] = "rising" if (np.isfinite(sm) and last[s] > sm and np.isfinite(sm_prev) and sm > sm_prev) else "resting"
        r["verdict"] = verdict(r)
        # monthly price line (start = 100) for the charts; only for the better shares, to keep the file small
        col = M[s]
        fv = col.first_valid_index()
        r["m0"] = int(M.index.get_loc(fv)) if fv is not None else 0
        if fv is not None and (r["champion"] or r["score"] >= 50):
            base = float(col.loc[fv])
            r["series"] = [sig3(100 * v / base) for v in col.iloc[r["m0"]:].tolist()]
        r.pop("price_champion", None)
        r["first"] = r["first"].strftime("%Y-%m-%d")

    try:
        tests = rule_test(D, ix, inds, st["crashes"], st["effects"])
    except Exception as exc:                            # the study must never break the daily scan
        log.warning("champions rule test failed: %s", exc)
        tests = []
    for t in tests:
        log.info("rule test from %s: champions %s, all %s, nifty %s", t["start"], t["champ"], t["all"], t["nifty"])

    crashes = [{k: c[k] for k in ("name", "short", "peak", "trough", "recovered", "fall", "months_down",
                                  "months", "months_so_far")} for c in st["crashes"]]
    return {
        "generated": datetime.now(timezone.utc).isoformat(), "version": VERSION, "demo": p.source.startswith("DEMO"),
        "data_from": ix.index[0].strftime("%Y-%m-%d"), "data_to": ix.index[-1].strftime("%Y-%m-%d"),
        "rules": {"up_years": UP_YEARS_MIN, "roll3": ROLL3_MIN, "fell_less": FELL_LESS, "vol": VOL_MAX, "debt": DEBT_MAX / 100,
                  "roe": ROE_MIN, "pe": PE_MAX, "min_years": MIN_YEARS, "min_crashes": MIN_CRASHES,
                  "crash_fall": int(CRASH_MIN_FALL * 100)},
        "crashes": crashes, "years": st["years"], "ytd_year": st["ytd_year"],
        "months0": months[0] if months else None,
        "nifty": {**st["nifty"], "series": [sig3(100 * v / float(mix.iloc[0])) for v in mix.tolist()]},
        "stocks": rows, "test": tests,
    }


def is_fresh(path=None) -> bool:
    path = path or (config.OUT_DIR / FILE)
    try:
        old = json.loads(path.read_text(encoding="utf-8"))
        age = datetime.now(timezone.utc) - datetime.fromisoformat(old["generated"])
        return old.get("version") == VERSION and age.days < REFRESH_DAYS
    except (OSError, ValueError, KeyError, TypeError):
        return False


def publish(uni: pd.DataFrame, n50: set[str], demo: bool = False, use_news: bool = True) -> dict | None:
    """Download all available history (~19 years) and write champions.json."""
    syms = list(uni.symbol)
    if demo:
        p = data.load_demo(syms, days=4900)
    else:
        p = data.load_yfinance(syms, period="max")
        if p is None or len(p.close) < 252 * 8:
            log.warning("champions: not enough long history from Yahoo (%s days)", None if p is None else len(p.close))
            return None
        p = data.drop_unfinished_session(p)
    out = build(p, uni, n50, use_fundamentals=not demo, use_news=use_news and not demo)
    try:                                   # My Plan's methods on the same long history (longtest.json)
        from . import longterm
        longterm.publish_long_test(p, uni, n50)
    except Exception as exc:               # never lose the champions study because of the test
        log.warning("18-year test failed: %s", exc)
    config.OUT_DIR.mkdir(parents=True, exist_ok=True)
    path = config.OUT_DIR / FILE
    path.write_text(json.dumps(_clean(out), separators=(",", ":"), ensure_ascii=False), encoding="utf-8")
    log.info("wrote %s (%.0f KB): %d champions", FILE, path.stat().st_size / 1024,
             sum(r["champion"] for r in out["stocks"]))
    return out
