"""Long-history strategy study on real prices: about 18 years, through the 2008 crash, 2011, 2015-16,
2018, COVID 2020, 2022 and 2024-26.

What it answers, using at every moment only the prices known then, with costs:
  * Long term: the current method (Nifty 50 momentum) against "quality" methods that prefer shares which
    rose in most years and fell less than the market in bad months (the long-term champions idea), with
    and without the market guard (sell everything while the Nifty is below its 200-day average) and the
    25% rising safety price. Turnover and a rough tax estimate are included: selling within a year costs
    20% of the gain in tax, while a small investor's long-term gains stay under the yearly tax-free limit.
  * Short term: whether the "safe dip" trade has a real edge over 18 years.
  * Round 4, "is it too late to buy?": do shares that have already risen a lot, or for a long time, still do
    well after the app picks them, and would skipping such buys have helped?

Runs in GitHub Actions (Tests workflow) when the commit message contains [study]; the results are notes on
the run page. Survivorship bias: only today's Nifty 200 companies are tested, so companies that collapsed
in the past are missing. Every method benefits; treat the numbers as optimistic and compare methods.

    python -m engine.study                # real data (Yahoo, a few minutes to download): DEFAULT_PART
    python -m engine.study --part late    # round 4: too late to buy the strongest shares?
    python -m engine.study --part rules   # round 3: holding rules and the short-term trade
    python -m engine.study --demo         # synthetic data, to test the code
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
    universe: str = "n50"           # "n50" (today's Nifty 50), "n200", or "top50"/"top100": the most traded shares
                                    # at that time (no hindsight about which companies later became big)
    rank: str = "mom"               # "mom" (6/12-month strength) or "quality" (champion score)
    filt: str = "none"              # "quality": only champion-like shares may be bought
    trend: bool = False             # buy only while the share's 200-day average is rising
    n: int = 3                      # shares held (one per industry)
    hold_mult: float = 2.0          # rank rule: keep while in the top hold_mult x n
    hold_rule: str = "rank"         # "rank" or "quality" (keep while the champion score stays in the top half)
    hold_q: float = 0.5
    index_w: float = 1 / 3          # part kept in a Nifty 50 index fund
    guard: str = "g1"               # "none"; "g1" Nifty below its 200-day average; "g2" 3% below, back above it
                                    # ("g2:0.05" = 5% below); "g3" below it while the average itself falls;
                                    # "gm" checked only at month ends
    trail: float | None = 0.25      # rising safety price (fraction below the highest close since buying)
    tax: bool = False
    # round 4 ("is it too late to buy?"): do not BUY a share that has already run far (one already held is
    # kept by the usual rules). None = no such rule.
    max_ext: float | None = None    # price more than this above its 200-day average
    max_r12: float | None = None    # risen more than this over the last 12 months
    max_r36: float | None = None    # risen more than this over the last 3 years
    max_streak: int | None = None   # in the keep zone (top hold_mult x n) at more than this many month ends in a row
    ext_pen: float = 0.0            # softer: the buy order uses score - ext_pen x z(stretch above the 200-day average)

    @property
    def late_rules(self) -> bool:
        return bool(self.ext_pen) or any(x is not None for x in (self.max_ext, self.max_r12, self.max_r36,
                                                                  self.max_streak))


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
        turnover = (Cf * p.volume.reindex_like(C).fillna(0)).rolling(126, min_periods=60).mean()
        trank = turnover.where(turnover > 0).rank(axis=1, ascending=False)
        self.top = {50: f(trank) <= 50, 100: f(trank) <= 100}
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
        self.guard: dict[str, np.ndarray] = {}
        self._qcache: dict = {}
        self._streaks: dict = {}

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
        key = (m, universe.tobytes())
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
    def guard_out(self, kind: str) -> np.ndarray:
        if kind not in self.guard:
            self.guard[kind] = self._guard(kind)
        return self.guard[kind]

    def _guard(self, kind: str) -> np.ndarray:
        out = np.zeros(self.T, bool)
        if kind == "none":
            return out
        name, _, band = kind.partition(":")
        band = float(band) if band else 0.03
        state = False
        for i in range(self.T):
            x, s, sp = self.ix[i], self.ixs200[i], self.ixs200_prev[i]
            if not (np.isfinite(x) and np.isfinite(s)):
                continue
            if name == "g1":
                state = x < s
            elif name == "g2":
                state = (x < s) if state else (x < (1 - band) * s)
            elif name == "g3":
                state = (x < s) if state else (x < s and np.isfinite(sp) and s < sp)
            elif name == "gm" and self.is_me[i]:
                state = x < s
            out[i] = state
        return out

    # ---------------------------------------------------------------- what to buy / keep on day i
    def universe(self, v: Variant, i: int) -> np.ndarray:
        if v.universe == "n50":
            return self.n50
        if v.universe.startswith("top"):
            return self.top[int(v.universe[3:])][i]
        return self.all

    def candidates(self, v: Variant, i: int):
        uni = self.universe(v, i)
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
            score = 0.5 * z(a) + 0.5 * z(b)
            order = [int(k) for k in idx[np.argsort(-score, kind="stable")]]
            top = int(v.hold_mult * max(v.n, 1))
            hold = set(order[:top])
            if v.late_rules:                             # what is bought changes; a held share is still kept while
                order = self.late_order(v, i, idx, score, order)    # in the top, or while it would still be bought
                hold |= set(order[:top])
            return order, (lambda k: k in hold)
        if qs is None:
            return [], (lambda k: True)
        score, passes = qs
        elig = base & passes & (rising if v.trend else True)
        idx = np.flatnonzero(elig)
        order = [int(k) for k in idx[np.argsort(-score[idx], kind="stable")]]
        if v.late_rules and len(idx):
            order = self.late_order(v, i, idx, score[idx], order)
        if v.hold_rule == "quality":
            return order, (lambda k: bool(np.isfinite(score[k]) and score[k] >= v.hold_q))
        ranked = [int(k) for k in np.flatnonzero(np.isfinite(score) & passes)]
        ranked.sort(key=lambda k: -score[k])
        hold = set(ranked[: int(v.hold_mult * max(v.n, 1))])
        return order, (lambda k: k in hold)

    # ---------------------------------------------------------------- round 4: how far a share has already run
    def streak(self, universe: str, top: int, i: int, rank: str = "mom") -> np.ndarray:
        """For every share: at how many month ends in a row (up to the latest one on or before day i) it ranked
        in the top `top` of the plain ranking (`rank`: "mom" or "quality") of `universe`."""
        key = (universe, top, rank)
        if key not in self._streaks:
            base = Variant("streak", universe=universe, rank=rank)
            me = np.flatnonzero(self.is_me)
            tab = np.zeros((len(me), self.K), np.int16)
            cur = np.zeros(self.K, np.int16)
            for r, m in enumerate(me):
                order, _ = self.candidates(base, int(m))
                nxt = np.zeros(self.K, np.int16)
                if order:
                    t = order[:top]
                    nxt[t] = cur[t] + 1
                cur = nxt
                tab[r] = cur
            self._streaks[key] = (me, tab)
        me, tab = self._streaks[key]
        r = int(np.searchsorted(me, i, side="right")) - 1
        return tab[r] if r >= 0 else np.zeros(self.K, np.int16)

    def late_order(self, v: Variant, i: int, idx: np.ndarray, score: np.ndarray, order: list[int]) -> list[int]:
        """The buy order without the shares that have already run too far (Variant.max_*), or re-ranked so that
        stretched shares come later (Variant.ext_pen). Unknown values never block a share."""
        c = self.Cf[i]
        if v.ext_pen:
            ext = c[idx] / self.sma200[i, idx] - 1
            zz = np.clip((ext - np.nanmean(ext)) / (np.nanstd(ext, ddof=1) or 1.0), -3, 3)
            order = [int(k) for k in idx[np.argsort(-(score - v.ext_pen * np.nan_to_num(zz)), kind="stable")]]
        st = self.streak(v.universe, int(v.hold_mult * max(v.n, 1)), i, v.rank) if v.max_streak is not None else None
        out = []
        with np.errstate(invalid="ignore"):
            for k in order:
                if v.max_ext is not None and c[k] > (1 + v.max_ext) * self.sma200[i, k]:
                    continue
                if v.max_r12 is not None and i >= 252 and c[k] > (1 + v.max_r12) * self.Cf[i - 252, k]:
                    continue
                if v.max_r36 is not None and i >= 756 and c[k] > (1 + v.max_r36) * self.Cf[i - 756, k]:
                    continue
                if st is not None and st[k] > v.max_streak:
                    continue
                out.append(k)
        return out

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
    out = ctx.guard_out(v.guard)
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
# Round 3: the proposed method R = the current one with the slower guard (out 3% below the 200-day average,
# back above it) and fewer swaps (keep a share while it ranks in the top 9). Rounds 1-2 are in git history.
R_ = dict(guard="g2", hold_mult=3.0)
LT_VARIANTS = [
    Variant("A NOW N50 momentum, 3 + 1/3 index, guard, 25% stop"),
    Variant("R NEW: slower guard, keep while in top 9", **R_),
    Variant("R5 R with a 5% guard band", **{**R_, "guard": "g2:0.05"}),
    Variant("RT50 R on the 50 most traded then (no hindsight)", universe="top50", **R_),
    Variant("RT100 R on the 100 most traded then", universe="top100", **R_),
    Variant("RX50 R with 1/2 in the index fund", index_w=0.5, **R_),
    Variant("RS0 R without the 25% stop", trail=None, **R_),
    Variant("H Champions in uptrend N200, no guard", universe="n200", rank="quality", trend=True, hold_rule="quality", guard="none"),
]
BENCH = [Variant("Z1 Nifty 50 index fund, held", n=0, index_w=1.0, guard="none", trail=None),
         Variant("Z3 Index fund + slower guard", n=0, index_w=1.0, guard="g2", trail=None)]


def long_term(ctx: Ctx) -> list[list[str]]:
    res = {}
    for v in BENCH + LT_VARIANTS:
        t0 = time.time()
        eq, closed, _ = simulate(ctx, v)
        res[v.name] = (v, summary(ctx, eq, closed if v.n else None))
        log.info("%s: %.1fs", v.name, time.time() - t0)
    head = [f"LONG TERM, real prices {ctx.dates[ctx.start].date()}..{ctx.dates[-1].date()}, {ctx.K} shares "
            f"(N50 {int(ctx.n50.sum())}); costs {COST * 200:.1f}% a round trip, cash {CASH_YIELD:.0%}/yr, Nifty + {IDX_DIV:.1%} dividends"]
    group = lambda *keys: [line(n, s) for n, (v, s) in res.items() if n.split(" ")[0] in keys]
    rows1 = head + group("Z1", "Z3", "A", "R")
    rows2 = group("R5", "RT50", "RT100")
    rows2b = group("RX50", "RS0", "H")
    rows3 = ["AFTER TAX (20% on gains from anything sold within a year; long-term gains assumed under the tax-free limit)"]
    for key in ("Z1", "Z3", "A", "R", "RT50", "RT100", "RX50", "H"):
        v = next((v for n, (v, s) in res.items() if n.split(" ")[0] == key), None)
        if v is None:
            continue
        eq, closed, paid = simulate(ctx, replace(v, tax=True))
        s = summary(ctx, eq, closed if v.n else None)
        rows3.append(f"{v.name[:46]} | before {pct(res[v.name][1]['cagr'], 1)}/yr, after tax {pct(s['cagr'], 1)}/yr | "
                     f"worst fall {pct(s['maxdd'])}")
    rows4 = ["YEARS (% return): " + " ".join(str(y)[2:] for y in sorted(res[LT_VARIANTS[0].name][1]["yearly"]))]
    for n, (v, s) in res.items():
        rows4.append(f"{n.split(' ')[0]}: " + " ".join(f"{100 * r:+.0f}" for _, r in sorted(s["yearly"].items())))
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


# ======================================================================= round 4: is it too late to buy?
# The worry (Nov 2026): the app suggests shares that have already been rising for a long time, so the chance
# may be gone. Two answers on the same 18 years, using only what was known at each moment:
#  1. Event study: at every month end while the market guard lets the app buy, the shares the method ranks
#     highest, split by how far and for how long they had already risen; then what they did over the next 6 and
#     12 months (bought at the next open; against the Nifty 50 index fund with its dividends over the same days).
#  2. The method with rules that skip such "late" buys (a share already held is kept by the usual rules),
#     simulated day by day, on today's Nifty 50 and on the most traded shares of each time (no hindsight).
LATE_H = (126, 252)
LATE_FEATURES = (   # key, short name, bucket edges (fractions; streak in month ends)
    ("r12", "up in 12 mo", (-9, 0.2, 0.4, 0.7, 1.0, 99)),
    ("ext", "above 200d avg", (-9, 0.1, 0.2, 0.3, 0.5, 99)),
    ("streak", "months in the top in a row", (1, 2, 4, 7, 13, 999)),
    ("r36", "up in 3 yrs", (-9, 0.5, 1.0, 2.0, 99)),
    ("pre", "up in the 2 yrs before that", (-9, 0.0, 0.3, 0.8, 99)),
    ("r1", "up in the last month", (-9, 0.0, 0.05, 0.1, 99)),
    ("hi", "vs its 1-yr high", (-9, -0.25, -0.15, -0.05, 99)),
    ("trend", "trend", (0, 1, 2)),
)


def late_events(ctx: Ctx, universe: str, top: int, n_buy: int = 3, rank: str = "mom",
                guard: bool = True) -> pd.DataFrame:
    """One row per (month end, share) for the top `top` of the method's ranking ("top") and the other shares
    that passed its rules ("rest"): how far each had run, and what followed. `buy` marks the shares the plan
    would actually buy (the first n_buy, one per industry). rank="quality": the champion-like shares (rose in
    most 12-month spells, fell less than the market in bad months, grew faster, calm), best score first."""
    v = Variant("late", universe=universe, rank=rank)
    out = ctx.guard_out(f"g2:{config.GUARD_BAND}") if guard else np.zeros(ctx.T, bool)
    rows = []
    for i in np.flatnonzero(ctx.is_me):
        i = int(i)
        if i < ctx.start or out[i] or i + 1 >= ctx.T or i < 252:
            continue
        order, _ = ctx.candidates(v, i)
        if not order:
            continue
        st = ctx.streak(universe, top, i, rank)
        buy = set(ctx.pick(order, [], n_buy))
        e = i + 1
        for pos, k in enumerate(order):
            o, c = ctx.O[e, k], ctx.Cf[i, k]
            entry = o if np.isfinite(o) and o > 0 else c
            if not (np.isfinite(entry) and entry > 0):
                continue
            with np.errstate(invalid="ignore", divide="ignore"):
                row = {"i": i, "k": k, "grp": "top" if pos < top else "rest", "buy": k in buy,
                       "r12": c / ctx.Cf[i - 252, k] - 1, "ext": c / ctx.sma200[i, k] - 1,
                       "r36": c / ctx.Cf[i - 756, k] - 1 if i >= 756 else np.nan,
                       "pre": ctx.Cf[i - 252, k] / ctx.Cf[i - 756, k] - 1 if i >= 756 else np.nan,
                       "r1": c / ctx.Cf[i - 21, k] - 1, "streak": int(st[k]), "hi": c / ctx.hi252[i, k] - 1}
                s, sp = ctx.sma200[i, k], ctx.sma200_prev[i, k]
                row["trend"] = float(c > s and s > sp) if np.isfinite(s) and np.isfinite(sp) else np.nan
            for h in LATE_H:
                j = i + h
                if j < ctx.T and np.isfinite(ctx.Cf[j, k]):
                    r = ctx.Cf[j, k] / entry - 1
                    rn = ctx.ix[j] / ctx.ixo[e] * (1 + IDX_DIV) ** (h / 252) - 1
                    row[f"r{h}"], row[f"x{h}"] = r, r - rn
                else:
                    row[f"r{h}"] = row[f"x{h}"] = np.nan
            if e + 126 <= ctx.T:                  # did the 25% rising safety price sell it within ~6 months?
                cl, lo = ctx.Cf[e:e + 126, k], ctx.L[e:e + 126, k]
                prev_hi = np.fmax.accumulate(np.concatenate([[entry], cl[:-1]]))
                with np.errstate(invalid="ignore"):
                    row["stop"] = float(np.any(lo <= (1 - config.HOLD_STOCK_TRAIL) * prev_hi))
            else:
                row["stop"] = np.nan
            rows.append(row)
    return pd.DataFrame(rows)


def _late_stats(d: pd.DataFrame) -> str:
    parts = []
    for h, lab in ((126, "6m"), (252, "12m")):
        r, x = d[f"r{h}"].dropna(), d[f"x{h}"].dropna()
        if len(r) < 10:
            parts.append(f"{lab} -")
            continue
        parts.append(f"{lab} med{100 * r.median():+.0f} beat{100 * (x > 0).mean():.0f} lead{100 * x.mean():+.0f} "
                     f"bad{100 * r.quantile(0.1):+.0f}")
    s = d["stop"].dropna()
    parts.append(f"stop{100 * s.mean():.0f}" if len(s) else "stop -")
    return " | ".join(parts)


def _late_label(key: str, lo: float, hi: float) -> str:
    if key == "streak":
        return f"{lo:.0f}" if hi - lo == 1 else (f">={lo:.0f}" if hi >= 999 else f"{lo:.0f}-{hi - 1:.0f}")
    if key == "trend":
        return "rising (above a rising 200d avg)" if lo >= 1 else "resting"
    if key == "hi":
        g = lambda x: f"{-100 * x:.0f}%"
        return (f"over {g(hi)} below" if lo <= -9 else f"within {g(lo)}" if hi >= 99 else f"{g(hi)}-{g(lo)} below")
    f = lambda x: f"{100 * x:.0f}%"
    return f"<{f(hi)}" if lo <= -9 else (f">={f(lo)}" if hi >= 99 else f"{f(lo)}-{f(hi)}")


def late_buckets(ev: pd.DataFrame, title: str, keys: tuple[str, ...], head: bool = True) -> list[str]:
    top = ev[ev.grp == "top"]
    rows = []
    if head:
        rows = [f"{title}: {len(top)} picks over {top.i.nunique()} month ends. n/months | after 6m and 12m: med = "
                "median gain %, beat = % that beat the Nifty fund, lead = average lead over it (pts), bad = 1 in 10 "
                "did worse (%) | stop = % sold by the 25% safety price within 6m",
                f"ALL TOP: n{len(top)} | {_late_stats(top)}",
                f"PLAN BUYS (3, one per industry): n{int(top.buy.sum())} | {_late_stats(top[top.buy])}",
                f"REST (passed the rules, ranked lower): n{int((ev.grp == 'rest').sum())} | "
                f"{_late_stats(ev[ev.grp == 'rest'])}"]
    for key, name, edges in LATE_FEATURES:
        if key not in keys:
            continue
        for lo, hi in zip(edges[:-1], edges[1:]):
            d = top[(top[key] >= lo) & (top[key] < hi)]
            if len(d) >= 15:
                rows.append(f"{name} {_late_label(key, lo, hi)}: n{len(d)}/{d.i.nunique()}mo | {_late_stats(d)}")
    return rows


def thirds_diff(ev: pd.DataFrame, key: str, h: int = 252) -> np.ndarray:
    """For each month end: the lead over the Nifty fund of the third of the top picks that had run furthest by
    `key`, minus that of the third that had run least (one number per month end)."""
    top = ev[ev.grp == "top"]
    diffs = []
    for _, g in top.groupby("i"):
        g = g.dropna(subset=[key, f"x{h}"])
        n3 = len(g) // 3
        if n3 < 2:
            continue
        g = g.sort_values(key, kind="stable")
        diffs.append(g[f"x{h}"].iloc[-n3:].mean() - g[f"x{h}"].iloc[:n3].mean())
    return np.asarray(diffs, dtype=float)


def late_thirds(ev: pd.DataFrame, title: str,
                keys: tuple[str, ...] = ("r12", "ext", "streak", "r36", "pre", "r1")) -> list[str]:
    """Within each month's top picks: the third that had run furthest minus the third that had run least
    (lead over the Nifty fund). Windows overlap, so the t value is divided by sqrt(months in the window)."""
    rows = [f"{title}: furthest-run third minus least-run third of each month's top picks (lead over the Nifty fund)"]
    for key, name, _ in LATE_FEATURES:
        if key not in keys:
            continue
        parts = []
        for h, mo in ((126, 6), (252, 12)):
            d = thirds_diff(ev, key, h)
            if len(d) < 12:
                parts.append(f"{mo}m -")
                continue
            sd = d.std(ddof=1)
            t = d.mean() / (sd / math.sqrt(len(d))) / math.sqrt(mo) if sd > 0 else float("nan")
            parts.append(f"{mo}m {100 * d.mean():+.1f} pts, late third better in {100 * (d > 0).mean():.0f}% of "
                         f"{len(d)} months, t~{t:.1f}")
        rows.append(f"{name}: " + " | ".join(parts))
    return rows


def late_variants(universe: str) -> list[Variant]:
    from .longterm import method                     # My Plan's rules (longterm imports this module lazily)
    tag = {"n50": "", "top50": " T50", "top100": " T100"}.get(universe, f" {universe}")
    k = config.LT_KEEP_TOP
    return [method(f"P{tag} My Plan now", universe=universe),
            method(f"E25{tag} no buy >25% above 200d avg", universe=universe, max_ext=0.25),
            method(f"E35{tag} no buy >35% above", universe=universe, max_ext=0.35),
            method(f"E50{tag} no buy >50% above", universe=universe, max_ext=0.50),
            method(f"G60{tag} no buy up >60% in 12 mo", universe=universe, max_r12=0.60),
            method(f"G100{tag} no buy up >100% in 12 mo", universe=universe, max_r12=1.00),
            method(f"L200{tag} no buy up >200% in 3 yrs", universe=universe, max_r36=2.00),
            method(f"S6{tag} no buy in top {k} >6 mo in a row", universe=universe, max_streak=6),
            method(f"S12{tag} no buy in top {k} >12 mo in a row", universe=universe, max_streak=12),
            method(f"Q{tag} stretched shares bought later", universe=universe, ext_pen=0.5)]


def late_line(name: str, s: dict, tax_cagr: float | None = None) -> str:
    wy, wr = s["worst_year"]
    p1, p2, p3 = s["periods"]
    return (f"{name} | {pct(s['cagr'], 1)}/yr, after tax {pct(tax_cagr, 1)} | worst fall {pct(s['maxdd'])} | worst yr {wy} "
            f"{pct(wr, 0, True)} | up {s['up_years']}/{s['n_years']} | 08-13 {pct(p1, 0, True)} 14-19 {pct(p2, 0, True)} "
            f"20-26 {pct(p3, 0, True)} | {s['sells_per_year']:.1f} sells/yr (swap {s['why']['rotate']})")


def late_entry(ctx: Ctx) -> list[list[str]]:
    """Round 4. At most 9 notes (GitHub shows 10 per step)."""
    notes = []
    for uni, top, title in (("n50", config.LT_KEEP_TOP, f"LATE? Nifty 50 (today's), top {config.LT_KEEP_TOP}"),
                            ("top100", 15, "LATE? 100 most traded then (no hindsight), top 15"),
                            ("n200", 15, "LATE? Nifty 200 (today's, like the Advanced list), top 15")):
        t0 = time.time()
        ev = late_events(ctx, uni, top)
        log.info("late events %s: %d rows (%.1fs)", uni, len(ev), time.time() - t0)
        if ev.empty:
            notes.append([f"{title}: no data"])
            continue
        notes.append(late_buckets(ev, title, ("r12", "ext", "streak")))
        notes.append(late_buckets(ev, title, ("r36", "pre", "r1"), head=False) + late_thirds(ev, title))
    for uni in ("n50", "top50", "top100"):
        rows = [f"LATE RULES, day by day ({uni}; only buys change, a held share is kept while in the top 9 or still "
                f"buyable), {ctx.dates[ctx.start].date()}..{ctx.dates[-1].date()}"]
        for v in late_variants(uni):
            t0 = time.time()
            eq, closed, _ = simulate(ctx, v)
            teq, _, _ = simulate(ctx, replace(v, tax=True))
            rows.append(late_line(v.name, summary(ctx, eq, closed), summary(ctx, teq)["cagr"]))
            log.info("%s: %.1fs", v.name, time.time() - t0)
        notes.append(rows)
    return notes


# Round 5: the same question for the long-term champions (Advanced -> Long-term picks): shares that rose in most
# years and held up in crashes, i.e. shares that have "gone up for a long time" by design. The app's advice is to
# buy one only while it is rising (above a rising 200-day average); this checks that advice and the "too late" worry.
def champ_variants(universe: str) -> list[Variant]:
    tag = {"n200": "", "top100": " T100"}.get(universe, f" {universe}")
    base = dict(universe=universe, rank="quality", trend=True, hold_rule="quality", guard="none")
    return [Variant(f"H{tag} champions while rising (the app's advice), no guard", **base),
            Variant(f"H0{tag} champions, rising or not", **{**base, "trend": False}),
            Variant(f"HE{tag} H, no buy >30% above 200d avg", **base, max_ext=0.30),
            Variant(f"HG{tag} H, no buy up >50% in 12 mo", **base, max_r12=0.50),
            Variant(f"HL{tag} H, no buy up >150% in 3 yrs", **base, max_r36=1.50)]


def late_champ(ctx: Ctx) -> list[list[str]]:
    """Round 5. At most 6 notes."""
    notes = []
    for uni, title in (("n200", "CHAMPION-LIKE, Nifty 200 (today's), top 10 by quality score, every month end"),
                       ("top100", "CHAMPION-LIKE among the 100 most traded then (no hindsight), top 10")):
        t0 = time.time()
        ev = late_events(ctx, uni, 10, rank="quality", guard=False)
        log.info("champion events %s: %d rows (%.1fs)", uni, len(ev), time.time() - t0)
        if ev.empty:
            notes.append([f"{title}: no data"])
            continue
        notes.append(late_buckets(ev, title, ("r12", "ext", "hi", "trend")))
        notes.append(late_buckets(ev, title, ("r36", "streak", "r1"), head=False)
                     + late_thirds(ev, title, ("r12", "ext", "hi", "r36", "streak")))
    for uni in ("n200", "top100"):
        rows = [f"CHAMPION RULES, day by day ({uni}; 3 champions one per industry + 1/3 index fund, 25% safety price, "
                f"kept while the quality score stays in the top half), {ctx.dates[ctx.start].date()}..{ctx.dates[-1].date()}"]
        for v in champ_variants(uni):
            t0 = time.time()
            eq, closed, _ = simulate(ctx, v)
            teq, _, _ = simulate(ctx, replace(v, tax=True))
            rows.append(late_line(v.name, summary(ctx, eq, closed), summary(ctx, teq)["cagr"]))
            log.info("%s: %.1fs", v.name, time.time() - t0)
        notes.append(rows)
    return notes


DEFAULT_PART = "champ"      # what a [study] commit runs (GitHub shows at most 10 notes per step)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--demo", action="store_true", help="synthetic data (tests the code only)")
    ap.add_argument("--part", choices=("late", "champ", "rules", "all"), default=DEFAULT_PART,
                    help="late = round 4 (too late to buy?), champ = round 5 (the same for champions), "
                         "rules = round 3 (holding rules, short trades)")
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
    parts = []
    if args.part in ("late", "all"):
        parts.append(("late", lambda: late_entry(ctx)))
    if args.part in ("champ", "all"):
        parts.append(("champ", lambda: late_champ(ctx)))
    if args.part in ("rules", "all"):
        parts += [("long-term", lambda: long_term(ctx)), ("short-term", lambda: [short_term(ctx, n50)])]
    for title, fn in parts:
        try:
            for k, rows in enumerate(fn()):
                note(f"{title}-{k + 1}", rows)
        except Exception:
            import traceback
            note(f"{title}-failed", traceback.format_exc()[-1500:].splitlines())
    log.info("study done in %.0fs", time.time() - t0)
    return 0


if __name__ == "__main__":
    sys.exit(main())
