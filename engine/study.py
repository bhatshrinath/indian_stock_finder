"""Long-history strategy study on real prices: about 18 years, through the 2008 crash, 2011, 2015-16,
2018, COVID 2020, 2022 and 2024-26.

What it answers, using at every moment only the prices known then, with costs:
  * Long term: the current method (Nifty 50 momentum) against "quality" methods that prefer shares which
    rose in most years and fell less than the market in bad months (the long-term champions idea), with
    and without the market guard (sell everything while the Nifty is below its 200-day average) and the
    25% rising safety price. Turnover and a rough tax estimate are included: selling within a year costs
    20% of the gain in tax, while a small investor's long-term gains stay under the yearly tax-free limit.
  * Short term: whether the "safe dip" trade has a real edge over 18 years.

Runs in GitHub Actions (Tests workflow) when the commit message contains [study]; the results are notes on
the run page. Survivorship bias: only today's Nifty 200 companies are tested, so companies that collapsed
in the past are missing. Every method benefits; treat the numbers as optimistic and compare methods.

    python -m engine.study          # real data (Yahoo, a few minutes to download)
    python -m engine.study --demo   # synthetic data, to test the code
"""
from __future__ import annotations

import argparse
import logging
import math
import sys
import time
from dataclasses import dataclass, replace

import numpy as np
import pandas as pd

from . import backtest, config, data
from . import indicators as ind
from .champions import clean_close
from .strategies import Features, SwingStrategy, _safe_dip
from .universe import load_nifty50, load_universe, yahoo_ticker

log = logging.getLogger("study")

COST = 0.0025       # per side for shares: brokerage, STT, exchange charges and the fixed DP charge on small orders
IDX_COST = 0.0015   # per side for the index fund (ETF)
CASH_YIELD = 0.06   # liquid fund / FD while money is out of shares
IDX_DIV = 0.013     # the Nifty price index leaves out dividends (~1.3% a year); an index fund gets them
STCG = 0.20         # tax on gains from shares sold within a year
START = pd.Timestamp("2008-07-01")
PERIODS = ((2008, 2013), (2014, 2019), (2020, 2100))

# "quality" (champion-like), measured each month over the previous 7 years (at least 3)
Q_WIN, Q_MIN = 84, 36
Q_POS12 = 0.70      # up over the previous 12 months in at least 70% of months
Q_DOWN = 0.85       # in months when the market fell, fell at most 85% as much as the market
Q_VOL = 0.35        # yearly price swing at most 35%
Q_MAXDD = -0.55     # never more than 55% below an earlier high in those years


# ======================================================================= data
def _norm(ix) -> pd.DatetimeIndex:
    ix = pd.to_datetime(ix)
    if getattr(ix, "tz", None) is not None:
        ix = ix.tz_convert("Asia/Kolkata").tz_localize(None)
    return ix.normalize()


def load_long(symbols: list[str]) -> tuple[data.Prices, pd.Series] | None:
    """All of Yahoo's history. Shares often start around 2002 and the Nifty in Sept 2007; the years before
    the Nifty are kept so the first quality checks (2008) already have several years behind them."""
    import yfinance as yf
    tickers = [yahoo_ticker(s) for s in symbols] + [config.INDEX_SYMBOL]
    frames = []
    for i in range(0, len(tickers), 50):
        chunk = tickers[i:i + 50]
        for attempt in range(3):
            try:
                df = yf.download(chunk, period="max", interval="1d", auto_adjust=True,
                                 group_by="column", progress=False, threads=True)
                if df is not None and not df.empty:
                    frames.append(df)
                    break
            except Exception as exc:
                log.warning("download %d attempt %d: %s", i, attempt + 1, exc)
            time.sleep(2 * (attempt + 1))
    if not frames:
        return None
    raw = pd.concat(frames, axis=1)
    raw = raw.loc[:, ~raw.columns.duplicated()]

    def fld(name: str) -> pd.DataFrame:
        f = raw[name].copy()
        f.columns = [str(c).replace(".NS", "") for c in f.columns]
        f.index = _norm(f.index)
        return f[~f.index.duplicated(keep="last")].sort_index()

    C, O, H, L, V = (fld(n) for n in ("Close", "Open", "High", "Low", "Volume"))
    if config.INDEX_SYMBOL not in C.columns or C[config.INDEX_SYMBOL].dropna().empty:
        return None
    ixc, ixo, ixh, ixl = (x.pop(config.INDEX_SYMBOL) for x in (C, O, H, L))
    V.pop(config.INDEX_SYMBOL)
    cnt = C.notna().sum(axis=1)
    med = cnt.rolling(21, center=True, min_periods=1).median()
    keep = (cnt >= 0.5 * med) & (cnt >= 5)
    keep &= (C.index < ixc.first_valid_index()) | ixc.notna()      # after the Nifty starts: its trading days
    C, O, H, L, V = (x.loc[keep] for x in (C, O, H, L, V))
    ixc, ixo, ixh, ixl = (x.loc[keep] for x in (ixc, ixo, ixh, ixl))
    good = C.columns[C.notna().sum() > 260]
    p = data.Prices(O[good], H[good], L[good], C[good], V[good], ixc, ixh, ixl, source="Yahoo Finance (max)")
    p = data.drop_unfinished_session(p)
    return p, ixo.reindex(p.close.index)


# ======================================================================= the long-term simulator
@dataclass(frozen=True)
class Variant:
    name: str
    universe: str = "n50"           # "n50" (today's Nifty 50) or "n200"
    rank: str = "mom"               # "mom" (6/12-month strength) or "quality" (champion score)
    filt: str = "none"              # "quality": only champion-like shares may be bought
    trend: bool = False             # buy only while the share's 200-day average is rising
    n: int = 3                      # shares held (one per industry)
    hold_mult: float = 2.0          # rank rule: keep while in the top hold_mult x n
    hold_rule: str = "rank"         # "rank" or "quality" (keep while the champion score stays in the top half)
    hold_q: float = 0.5
    index_w: float = 1 / 3          # part kept in a Nifty 50 index fund
    guard: str = "g1"               # "none"; "g1" Nifty below its 200-day average; "g2" 3% below, back above it;
                                    # "g3" below it while the average itself falls
    trail: float | None = 0.25      # rising safety price (fraction below the highest close since buying)
    tax: bool = False


class Ctx:
    """Every array the simulations need, computed once."""

    def __init__(self, p: data.Prices, ixo: pd.Series | None, uni: pd.DataFrame, n50: set[str]):
        C = clean_close(p.close)
        mask = C.notna()
        O = p.open.reindex_like(C).where(mask).fillna(C)
        H = p.high.reindex_like(C).where(mask).fillna(C)
        L = p.low.reindex_like(C).where(mask).fillna(C)
        Cf = C.ffill(limit=10)
        f = lambda x: x.to_numpy(dtype=float, copy=True)
        self.p = p
        self.dates = C.index
        self.T, self.K = C.shape
        self.syms = list(C.columns)
        inds = dict(zip(uni.symbol, uni.industry))
        self.ind = [inds.get(s, s) for s in self.syms]
        self.n50 = np.array([s in n50 for s in self.syms])
        self.all = np.ones(self.K, bool)
        self.C, self.O, self.H, self.L, self.Cf = f(C), f(O), f(H), f(L), f(Cf)
        sma200 = Cf.rolling(200, min_periods=180).mean()
        self.sma200, self.sma200_prev = f(sma200), f(sma200.shift(21))
        self.hi252 = f(Cf.rolling(252, min_periods=200).max())
        self.vol = f(ind.annual_vol(Cf))
        self.m12 = f(Cf.shift(21) / Cf.shift(252) - 1)
        self.m6 = f(Cf.shift(21) / Cf.shift(126) - 1)
        self.recent = f(mask.astype(float).rolling(20, min_periods=1).sum()) >= 15
        ix = p.index_close.reindex(C.index)
        self.ix = f(ix.ffill())
        io = ixo.reindex(C.index) if ixo is not None else ix
        self.ixo = f(io.fillna(ix).ffill())
        s200, s50 = ix.rolling(200, min_periods=190).mean(), ix.rolling(50, min_periods=48).mean()
        self.ixs200, self.ixs200_prev, self.ixs50 = f(s200), f(s200.shift(21)), f(s50)
        ok = np.flatnonzero((self.dates >= START) & np.isfinite(self.ixs200) & np.isfinite(self.ix))
        self.start = int(ok[0]) if len(ok) else self.T - 1
        d = self.dates
        self.is_me = np.array([i < self.T - 1 and d[i].month != d[i + 1].month for i in range(self.T)])
        self._quality(Cf)
        self.guard = {g: self._guard(g) for g in ("g1", "g2", "g3")}
        self._qcache: dict = {}

    # ---------------------------------------------------------------- monthly quality facts
    def _quality(self, Cf: pd.DataFrame) -> None:
        me = np.flatnonzero(self.is_me)
        self.me_pos = me
        MC = Cf.iloc[me]
        mr = MC / MC.shift(1) - 1
        valid = mr.notna()
        mkt = mr.mean(axis=1)                       # equal-weight market of the shares listed then
        r12 = MC / MC.shift(12) - 1
        pos12 = (r12 > 0).astype(float).where(r12.notna()).rolling(Q_WIN, min_periods=Q_MIN - 12).mean()
        nh = valid.astype(float).rolling(Q_WIN, min_periods=1).sum()
        mrz, vf = mr.fillna(0.0), valid.astype(float)

        def capture(sel: pd.Series) -> pd.DataFrame:
            num = mrz.mul(sel, axis=0).rolling(Q_WIN, min_periods=1).sum()
            den = vf.mul(mkt.fillna(0.0) * sel, axis=0).rolling(Q_WIN, min_periods=1).sum()
            return num / den.where(den.abs() > 0.05)

        down = capture((mkt < 0).astype(float))
        up = capture((mkt > 0).astype(float))
        lvl = (1 + mkt.fillna(0.0)).cumprod()
        cagr = lambda x, n: (x / x.shift(n)) ** (12 / n) - 1
        g60, g36 = cagr(MC, 60), cagr(MC, 36)
        m60, m36 = cagr(lvl, 60), cagr(lvl, 36)
        growth = g60.where(g60.notna(), g36)
        mgrowth = pd.DataFrame(np.where(g60.notna(), m60.to_numpy()[:, None], m36.to_numpy()[:, None]),
                               index=MC.index, columns=MC.columns)
        vol36 = mr.rolling(36, min_periods=24).std() * math.sqrt(12)
        dd = MC / MC.rolling(Q_WIN, min_periods=1).max() - 1
        maxdd = dd.rolling(Q_WIN, min_periods=1).min()
        ok = (nh >= Q_MIN) & pos12.notna() & down.notna() & growth.notna() & vol36.notna()
        f = lambda x: x.where(ok).to_numpy(dtype=float, copy=True)
        self.q = {"pos12": f(pos12), "down": f(down), "up": f(up), "growth": f(growth),
                  "mgrowth": f(mgrowth), "vol": f(vol36), "maxdd": f(maxdd)}
        self.qrow = np.searchsorted(me, np.arange(self.T), side="right") - 1

    def quality(self, i: int, universe: np.ndarray) -> tuple[np.ndarray, np.ndarray] | None:
        """Champion score (0-1, rank within the universe) and the champion-like filter, as of day i."""
        m = int(self.qrow[i])
        if m < 0:
            return None
        key = (m, universe is self.n50)
        if key in self._qcache:
            return self._qcache[key]
        q = {k: v[m] for k, v in self.q.items()}
        ok = universe & np.isfinite(q["pos12"]) & np.isfinite(q["down"]) & np.isfinite(q["growth"]) \
            & np.isfinite(q["vol"]) & np.isfinite(q["maxdd"])
        res = None
        if ok.sum() >= 5:
            def prank(x: np.ndarray, higher: bool = True) -> np.ndarray:
                r = np.full(self.K, np.nan)
                vals = x[ok] if higher else -x[ok]
                r[ok] = (vals.argsort().argsort() + 1) / len(vals)
                return r
            score = (0.3 * prank(q["pos12"]) + 0.3 * prank(q["down"], False) + 0.25 * prank(q["growth"])
                     + 0.15 * (prank(q["vol"], False) + prank(q["maxdd"])) / 2)
            with np.errstate(invalid="ignore"):
                passes = ok & (q["pos12"] >= Q_POS12) & (q["down"] <= Q_DOWN) & (q["growth"] > q["mgrowth"]) \
                    & (q["vol"] <= Q_VOL) & (q["maxdd"] >= Q_MAXDD)
            res = (score, passes)
        self._qcache[key] = res
        return res

    def quality_mask(self) -> pd.DataFrame:
        """Daily True/False: passed the champion-like filter at the latest month end (Nifty 200)."""
        rows = np.zeros((self.T, self.K), bool)
        for i in range(self.T):
            qs = self.quality(i, self.all)
            if qs is not None:
                rows[i] = qs[1]
        return pd.DataFrame(rows, index=self.dates, columns=self.syms)

    # ---------------------------------------------------------------- market guard (True = stay out)
    def _guard(self, kind: str) -> np.ndarray:
        out = np.zeros(self.T, bool)
        state = False
        for i in range(self.T):
            x, s, sp = self.ix[i], self.ixs200[i], self.ixs200_prev[i]
            if not (np.isfinite(x) and np.isfinite(s)):
                continue
            if kind == "g1":
                state = x < s
            elif kind == "g2":
                state = (x < s) if state else (x < 0.97 * s)
            elif kind == "g3":
                state = (x < s) if state else (x < s and np.isfinite(sp) and s < sp)
            out[i] = state
        return out

    # ---------------------------------------------------------------- what to buy / keep on day i
    def candidates(self, v: Variant, i: int):
        uni = self.n50 if v.universe == "n50" else self.all
        c = self.Cf[i]
        with np.errstate(invalid="ignore"):
            base = uni & self.recent[i] & np.isfinite(c) & (c > 0)
            rising = (c > self.sma200[i]) & (self.sma200[i] > self.sma200_prev[i])
        qs = self.quality(i, uni) if (v.filt == "quality" or v.rank == "quality") else None
        if v.rank == "mom":
            vol = self.vol[i]
            with np.errstate(invalid="ignore"):
                elig = base & (c > self.sma200[i]) & (c > 0.75 * self.hi252[i]) & np.isfinite(vol) & (vol > 0) \
                    & np.isfinite(self.m12[i]) & np.isfinite(self.m6[i])
            if v.filt == "quality":
                elig &= qs[1] if qs is not None else False
            if v.trend:
                elig &= rising
            idx = np.flatnonzero(elig)
            if len(idx) < 3:
                return [], (lambda k: True)              # nothing to compare: keep what is held
            a, b = self.m12[i, idx] / vol[idx], self.m6[i, idx] / vol[idx]
            z = lambda s: np.clip((s - s.mean()) / (s.std(ddof=1) or 1.0), -3, 3)
            order = [int(k) for k in idx[np.argsort(-(0.5 * z(a) + 0.5 * z(b)), kind="stable")]]
            hold = set(order[: int(v.hold_mult * max(v.n, 1))])
            return order, (lambda k: k in hold)
        if qs is None:
            return [], (lambda k: True)
        score, passes = qs
        elig = base & passes & (rising if v.trend else True)
        idx = np.flatnonzero(elig)
        order = [int(k) for k in idx[np.argsort(-score[idx], kind="stable")]]
        if v.hold_rule == "quality":
            return order, (lambda k: bool(np.isfinite(score[k]) and score[k] >= v.hold_q))
        ranked = [int(k) for k in np.flatnonzero(np.isfinite(score) & passes)]
        ranked.sort(key=lambda k: -score[k])
        hold = set(ranked[: int(v.hold_mult * max(v.n, 1))])
        return order, (lambda k: k in hold)

    def pick(self, order: list[int], held: list[int], n_free: int) -> list[int]:
        used = {self.ind[k] for k in held}
        out = []
        for k in order:
            if n_free <= 0:
                break
            if k in held or self.ind[k] in used:
                continue
            out.append(k)
            used.add(self.ind[k])
            n_free -= 1
        return out


def simulate(ctx: Ctx, v: Variant):
    """Day by day: orders decided at a close are filled at the next open; safety prices are checked during
    the day; the list is reviewed at each month end; the guard is checked every day. Returns the value of
    1 rupee, the closed holdings (days held, gain, cost, why) and the tax paid."""
    T, dates = ctx.T, ctx.dates
    out = ctx.guard[v.guard] if v.guard != "none" else np.zeros(T, bool)
    cash, iu, ib, ie = 1.0, 0.0, 0.0, ctx.start          # cash, index-fund units, its cost and buy day
    pos: dict[int, list] = {}                              # share -> [units, cost, buy day, highest close, last price]
    in_mkt, orders = False, None
    eq = np.full(T, np.nan)
    closed: list[tuple] = []
    tax_paid = 0.0

    def taxed(gain: float, days: int) -> float:
        nonlocal tax_paid
        t = STCG * gain if (v.tax and gain > 0 and days < 365) else 0.0
        tax_paid += t
        return t

    def sell(k: int, px: float, i: int, why: str) -> float:
        u, cost, e, _hi, _last = pos.pop(k)
        proceeds = u * px * (1 - COST)
        days = (dates[i] - dates[e]).days
        closed.append((days, proceeds - cost, cost, why))
        return proceeds - taxed(proceeds - cost, days)

    for i in range(ctx.start, T):
        if orders is not None:
            for k, why in orders["sell"]:
                if k in pos:
                    o = ctx.O[i, k]
                    cash += sell(k, o if np.isfinite(o) and o > 0 else pos[k][4], i, why)
            if orders.get("idx_sell") and iu > 0:
                proceeds = iu * ctx.ixo[i] * (1 - IDX_COST)
                cash += proceeds - taxed(proceeds - ib, (dates[i] - dates[ie]).days)
                iu = ib = 0.0
            amt = min(orders.get("idx_buy", 0.0), cash)
            if amt > 1e-9:
                iu += amt * (1 - IDX_COST) / ctx.ixo[i]
                ib, ie = ib + amt, i
                cash -= amt
            for k, a in orders["buy"]:
                o = ctx.O[i, k]
                a = min(a, cash)
                if np.isfinite(o) and o > 0 and a > 1e-9:
                    pos[k] = [a * (1 - COST) / o, a, i, o, o]
                    cash -= a
            orders = None
        if v.trail:
            for k in list(pos):
                lo, stop = ctx.L[i, k], pos[k][3] * (1 - v.trail)
                if np.isfinite(lo) and lo <= stop:
                    o = ctx.O[i, k]
                    cash += sell(k, o if (np.isfinite(o) and o < stop) else stop, i, "stop")
        cash *= 1 + CASH_YIELD / 252
        iu *= 1 + IDX_DIV / 252
        value = cash + iu * ctx.ix[i]
        for k, p_ in pos.items():
            c = ctx.Cf[i, k]
            if np.isfinite(c):
                p_[4] = c
                p_[3] = max(p_[3], c)
            value += p_[0] * p_[4]
        eq[i] = value
        if i == T - 1:
            break
        if out[i]:
            if in_mkt:
                orders = {"sell": [(k, "guard") for k in pos], "idx_sell": True, "buy": []}
                in_mkt = False
            continue
        if not in_mkt:
            order, _ = ctx.candidates(v, i) if v.n else ([], None)
            per = value * (1 - v.index_w) / v.n if v.n else 0.0
            orders = {"sell": [], "idx_buy": value * v.index_w, "buy": [(k, per) for k in ctx.pick(order, [], v.n)]}
            in_mkt = True
            continue
        if ctx.is_me[i] and v.n:
            order, hold_ok = ctx.candidates(v, i)
            sells = [k for k in pos if not hold_ok(k)]
            keep = [k for k in pos if k not in sells]
            buys = ctx.pick(order, keep, v.n - len(keep))
            if sells or buys:
                avail = cash + sum(pos[k][0] * pos[k][4] * (1 - COST) for k in sells)
                per = value * (1 - v.index_w) / v.n
                each = min(per, avail / len(buys)) if buys else 0.0
                orders = {"sell": [(k, "rotate") for k in sells], "buy": [(k, each) for k in buys]}
    for k in list(pos):                                    # still held at the end (not sold, no tax)
        u, cost, e, _hi, last = pos[k]
        closed.append(((dates[T - 1] - dates[e]).days, u * last - cost, cost, "end"))
    return eq, closed, tax_paid


# ======================================================================= summaries
def _cagr(a: float, b: float, years: float) -> float:
    return (b / a) ** (1 / years) - 1 if a > 0 and b > 0 and years > 0.2 else float("nan")


def summary(ctx: Ctx, eq: np.ndarray, closed: list[tuple] | None = None) -> dict:
    e = pd.Series(eq[ctx.start:], index=ctx.dates[ctx.start:]).dropna()
    yrs = (e.index[-1] - e.index[0]).days / 365.25
    dd = e / e.cummax() - 1
    longest, since = 0, None
    for d, u in (dd < -0.001).items():
        if u and since is None:
            since = d
        elif not u and since is not None:
            longest, since = max(longest, (d - since).days), None
    if since is not None:
        longest = max(longest, (e.index[-1] - since).days)
    ye = e.resample("YE").last()
    yr = (ye / ye.shift(1) - 1)
    yr.iloc[0] = ye.iloc[0] / e.iloc[0] - 1
    full = yr[[d.year < e.index[-1].year or e.index[-1].month == 12 for d in yr.index]]
    per = []
    for a, b in PERIODS:
        s = e[(e.index.year >= a) & (e.index.year <= b)]
        per.append(_cagr(s.iloc[0], s.iloc[-1], (s.index[-1] - s.index[0]).days / 365.25) if len(s) > 60 else np.nan)
    out = {"cagr": _cagr(e.iloc[0], e.iloc[-1], yrs), "maxdd": float(dd.min()), "under_mo": longest / 30.44,
           "worst_year": (int(full.idxmin().year), float(full.min())) if len(full) else (0, np.nan),
           "up_years": int((full > 0).sum()), "n_years": int(len(full)), "periods": per,
           "yearly": {int(d.year): float(r) for d, r in yr.items()}, "x": float(e.iloc[-1] / e.iloc[0]), "years": yrs}
    if closed is not None:
        done = [c for c in closed if c[3] != "end"]
        out["sells_per_year"] = len(done) / yrs if yrs else 0
        out["held_days"] = float(np.median([c[0] for c in closed])) if closed else 0
        out["short_pct"] = 100 * float(np.mean([c[0] < 365 for c in done])) if done else 0
        out["why"] = {w: sum(1 for c in done if c[3] == w) for w in ("stop", "guard", "rotate")}
    return out


def pct(x: float, d: int = 0, sign: bool = False) -> str:
    if x is None or not np.isfinite(x):
        return "-"
    return f"{100 * x:+.{d}f}%" if sign else f"{100 * x:.{d}f}%"


def line(name: str, s: dict) -> str:
    wy, wr = s["worst_year"]
    p1, p2, p3 = s["periods"]
    txt = (f"{name} | {pct(s['cagr'], 1)}/yr (x{s['x']:.1f}) | worst fall {pct(s['maxdd'])} | "
           f"under water {s['under_mo']:.0f} mo | worst yr {wy} {pct(wr, 0, True)} | up yrs {s['up_years']}/{s['n_years']} | "
           f"08-13 {pct(p1, 0, True)} 14-19 {pct(p2, 0, True)} 20-26 {pct(p3, 0, True)}")
    if "sells_per_year" in s:
        w = s["why"]
        txt += (f" | {s['sells_per_year']:.1f} sells/yr (stop {w['stop']}, guard {w['guard']}, swap {w['rotate']}), "
                f"held {s['held_days']:.0f}d median, {s['short_pct']:.0f}% within 1 yr")
    return txt


def note(title: str, rows: list[str]) -> None:
    """One annotation on the run page (each kept small: GitHub truncates long notes)."""
    msg = "\n".join(rows)
    esc = msg.replace("%", "%25").replace("\r", "").replace("\n", "%0A")
    print(f"::notice title={title}::{esc}", flush=True)
    print(msg, flush=True)


# ======================================================================= the studies
LT_VARIANTS = [
    Variant("A NOW N50 momentum, 3 + 1/3 index, guard, 25% stop"),
    Variant("B NOW without guard", guard="none"),
    Variant("C NOW slower guard (3% band)", guard="g2"),
    Variant("D NOW guard only when the 200d avg falls", guard="g3"),
    Variant("E Quality momentum N200", universe="n200", filt="quality"),
    Variant("F Quality momentum N200, no guard", universe="n200", filt="quality", guard="none"),
    Variant("G Champions in uptrend N200", universe="n200", rank="quality", trend=True, hold_rule="quality"),
    Variant("H Champions in uptrend, no guard", universe="n200", rank="quality", trend=True, hold_rule="quality", guard="none"),
    Variant("I Champions in uptrend, slower guard", universe="n200", rank="quality", trend=True, hold_rule="quality", guard="g2"),
    Variant("J Champions in uptrend, no stop", universe="n200", rank="quality", trend=True, hold_rule="quality", trail=None),
    Variant("K Champions in uptrend, 5 shares", universe="n200", rank="quality", trend=True, hold_rule="quality", n=5),
    Variant("L Champions in uptrend, no index part", universe="n200", rank="quality", trend=True, hold_rule="quality", index_w=0.0),
    Variant("M Champions in uptrend, Nifty 50 only", universe="n50", rank="quality", trend=True, hold_rule="quality"),
    Variant("N Champions, monthly top-6 swap", universe="n200", rank="quality", trend=True, hold_rule="rank"),
    Variant("O Champions, no trend check, no guard", universe="n200", rank="quality", hold_rule="quality", guard="none"),
    Variant("P Champions in uptrend, no guard, no stop", universe="n200", rank="quality", trend=True, hold_rule="quality",
            guard="none", trail=None),
]
BENCH = [Variant("Z1 Nifty 50 index fund, held", n=0, index_w=1.0, guard="none", trail=None),
         Variant("Z2 Index fund + guard", n=0, index_w=1.0, guard="g1", trail=None)]


def long_term(ctx: Ctx) -> list[list[str]]:
    res = {}
    for v in BENCH + LT_VARIANTS:
        t0 = time.time()
        eq, closed, _ = simulate(ctx, v)
        res[v.name] = (v, summary(ctx, eq, closed if v.n else None))
        log.info("%s: %.1fs", v.name, time.time() - t0)
    head = [f"LONG TERM, real prices {ctx.dates[ctx.start].date()}..{ctx.dates[-1].date()}, {ctx.K} shares "
            f"(N50 {int(ctx.n50.sum())}); costs {COST * 200:.1f}% a round trip, cash {CASH_YIELD:.0%}/yr, Nifty + {IDX_DIV:.1%} dividends"]
    rows1 = head + [line(n, s) for n, (v, s) in res.items() if n[0] in "ZABCD"]
    rows2 = [line(n, s) for n, (v, s) in res.items() if n[0] in "EFGHIJ"]
    rows2b = [line(n, s) for n, (v, s) in res.items() if n[0] in "KLMNOP"]
    # after a rough tax estimate, for the main methods
    rows3 = ["AFTER TAX (20% on gains from anything sold within a year; long-term gains assumed under the tax-free limit)"]
    for key in ("A", "B", "E", "F", "G", "H", "I", "M", "O", "P"):
        v = next((v for n, (v, s) in res.items() if n.startswith(key + " ")), None)
        if v is None:
            continue
        eq, closed, paid = simulate(ctx, replace(v, tax=True))
        s = summary(ctx, eq, closed)
        rows3.append(f"{v.name[:44]} | before {pct(res[v.name][1]['cagr'], 1)}/yr, after tax {pct(s['cagr'], 1)}/yr | "
                     f"worst fall {pct(s['maxdd'])}")
    # calendar years, to see consistency
    rows4 = ["YEARS (% return): " + " ".join(str(y)[2:] for y in sorted(res[LT_VARIANTS[0].name][1]["yearly"]))]
    for n, (v, s) in res.items():
        rows4.append(f"{n[:2].strip()}: " + " ".join(f"{100 * r:+.0f}" for _, r in sorted(s["yearly"].items())))
    return [rows1, rows2, rows2b, rows3, rows4]


def short_term(ctx: Ctx, n50: set[str]) -> list[str]:
    f = Features(ctx.p)
    f.set_large(n50)
    qmask = ctx.quality_mask().reindex(index=f.close.index, columns=f.close.columns).fillna(False).astype(bool)
    base = f.uptrend & f.liquid & (f.rsi2 < 10) & (f.close > f.sma200 * 1.03) & (f.sma200 > f.sma200.shift(20)) \
        & (f.rs6 > -0.05)
    bull, ok = f.broadcast(f.regime == 2), f.broadcast(f.regime >= 1)
    calm = f.vol252 < 0.30
    rules = [("D1 safe dip NOW (N50 calm, market rising)", _safe_dip(f)[0]),
             ("D2 safe dip, market merely above its 200d avg", f.large_frame() & base & calm & ok),
             ("D3 champion dip, market rising", qmask & base & bull),
             ("D4 champion dip, market above 200d avg", qmask & base & ok),
             ("D5 champion + N50 dip, market rising", qmask & f.large_frame() & base & bull)]
    first = f.close.index[ctx.start]
    years = (f.close.index[-1] - first).days / 365.25
    rows = [f"SHORT TERM (exit above the 5-day average, 2.5 ATR stop, 10 days max), {first.date()}..{f.close.index[-1].date()}; "
            f"costs {config.ROUND_TRIP_COST * 100:.2f}% a trade. Liquid fund on Rs 20,000 would earn about Rs "
            f"{20000 * ((1 + CASH_YIELD) ** years - 1):,.0f}"]
    for name, sig in rules:
        sig = sig.fillna(False).astype(bool)
        strat = SwingStrategy(key="st", name=name, plain_english="", how_to_exit="",
                              signal_fn=lambda f, sig=sig: (sig, f.close - 2.5 * f.atr14),
                              target_r=None, max_hold=10, exit_above_sma5=True)
        r = backtest.backtest_swing(f, strat)
        if not r.get("trades"):
            rows.append(f"{name} | no trades")
            continue
        yrs = [y for y in r.get("yearly", []) if y["trades"] >= 5]
        worst = min(yrs, key=lambda y: y["avg_return"]) if yrs else None
        sim = backtest.portfolio_sim(f, strat, money=20000.0, slots=1, per_day=1, idle_yield=CASH_YIELD)
        tot = (sim.get("total_profit") or 0) + (sim.get("idle_income") or 0)
        rows.append(f"{name} | {r['trades']} trades ({r['trades'] / years:.0f}/yr) | won {r['win_rate']}% | avg {r['avg_return']}% | "
                    f"PF {r['profit_factor']} | worst yr {worst['year'] if worst else '-'} {worst['avg_return'] if worst else '-'}% | "
                    f"Rs 20k, 1 at a time: {sim.get('trades', 0)} trades, trading profit Rs {sim.get('total_profit', 0):,}, "
                    f"+ idle cash Rs {sim.get('idle_income', 0):,} = Rs {tot:,}; worst drop {sim.get('worst_drop_pct', '-')}% | "
                    "by yr: " + " ".join(f"{y['year'] % 100:02d}:{y['profit']:+,}" for y in sim.get("yearly", [])))
    return rows


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--demo", action="store_true", help="synthetic data (tests the code only)")
    args = ap.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s", stream=sys.stdout)
    logging.getLogger("yfinance").setLevel(logging.CRITICAL)
    t0 = time.time()
    uni = load_universe()
    if args.demo:
        syms = list(uni.symbol)[:60]
        p, ixo, n50 = data.load_demo(syms, days=4700, seed=7), None, set(syms[:25])
    else:
        n50 = load_nifty50()
        got = load_long(list(uni.symbol))
        if got is None:
            log.error("no long history from Yahoo")
            return 1
        p, ixo = got
    log.info("data: %d days x %d shares, %s..%s (%.0fs)", len(p.close), p.close.shape[1],
             p.close.index[0].date(), p.close.index[-1].date(), time.time() - t0)
    ctx = Ctx(p, ixo, uni, n50)
    for k, rows in enumerate(long_term(ctx)):
        note(f"long-term-{k + 1}", rows)
    note("short-term", short_term(ctx, n50))
    log.info("study done in %.0fs", time.time() - t0)
    return 0


if __name__ == "__main__":
    sys.exit(main())
