"""Honest backtests.

Swing: every historical signal is simulated with the exact entry/exit rules the
app shows: buy next day's open (skipped if it gaps too far up or opens below
the stop), stop-loss checked before target on the same day (pessimistic),
gaps through the stop filled at the open, ~0.3% round-trip costs deducted.

Long-term: monthly rebalanced top-N momentum portfolio vs the Nifty 50, with
costs on turnover.

Known bias (stated in the app): the universe is TODAY's Nifty 200, so stocks
that fell out of the index in the past are missing (survivorship bias). This
flatters results, mostly for the long-term model. Treat figures as an upper
bound, and rely on the live track record over time.
"""
from __future__ import annotations

import math

import numpy as np
import pandas as pd

from . import config
from .strategies import Features, SwingStrategy, momentum_scores, pick_portfolio


# ======================================================================= swing
def simulate_trade(f: Features, strat: SwingStrategy, col: int, s: int, stop_px: float):
    """Simulate one trade from signal bar `s`. Returns dict or None if not filled."""
    O, H, L, C = f._np["open"], f._np["high"], f._np["low"], f._np["close"]
    SMA5, ATR = f._np["sma5"], f._np["atr14"]
    n = O.shape[0]
    e = s + 1
    if e >= n:
        return None
    sig_close = C[s, col]
    entry = O[e, col]
    if not np.isfinite(entry) or entry <= 0:
        return None
    risk = sig_close - stop_px
    if not np.isfinite(risk) or risk <= 0:
        return None
    if entry > sig_close * (1 + strat.max_gap_up):
        return None                              # don't chase gap-ups
    stop = entry - risk
    if entry <= stop:
        return None
    target = entry + strat.target_r * risk if strat.target_r else math.inf
    atr_e = ATR[s, col]
    hi_close = entry
    exit_px, exit_i, reason = None, None, None
    last = min(n - 1, e + strat.max_hold - 1)
    for j in range(e, last + 1):
        o, h, l, c = O[j, col], H[j, col], L[j, col], C[j, col]
        if not np.isfinite(c):
            continue
        if l <= stop:
            exit_px = stop if j == e or o > stop else o
            exit_i, reason = j, "stop"
            break
        if h >= target:
            exit_px = target if j == e or o < target else o
            exit_i, reason = j, "target"
            break
        if strat.exit_above_sma5 and np.isfinite(SMA5[j, col]) and c > SMA5[j, col]:
            # sell at next open (conservative for an end-of-day system)
            if j + 1 < n and np.isfinite(O[j + 1, col]):
                exit_px, exit_i = O[j + 1, col], j + 1
            else:
                exit_px, exit_i = c, j
            reason = "exit_rule"
            break
        hi_close = max(hi_close, c)
        if strat.trail_atr and np.isfinite(atr_e):
            stop = max(stop, hi_close - strat.trail_atr * atr_e)
    if exit_px is None:
        if last >= n - 1 and last - e + 1 < strat.max_hold:
            return {"open": True}                # still running at the end of data
        # time limit reached: the user is told after the close and sells at the next open
        if last + 1 < n and np.isfinite(O[last + 1, col]):
            exit_i, exit_px = last + 1, O[last + 1, col]
        else:
            exit_i, exit_px = last, C[last, col]
        reason = "time"
    ret = exit_px / entry - 1 - config.ROUND_TRIP_COST
    return {"entry_i": e, "exit_i": exit_i, "entry": entry, "exit": exit_px,
            "ret": ret, "r": (exit_px - entry) / risk, "reason": reason,
            "days": exit_i - e + 1}


def _prep_numpy(f: Features):
    if not hasattr(f, "_np"):
        f._np = {k: getattr(f, k).to_numpy(dtype=float, copy=True) for k in
                 ("open", "high", "low", "close", "sma5", "atr14")}


def backtest_swing(f: Features, strat: SwingStrategy) -> dict:
    _prep_numpy(f)
    sig, stop = strat.signal_fn(f)
    S = sig.to_numpy(dtype=bool)
    ST = stop.to_numpy(dtype=float)
    dates = f.close.index
    syms = list(f.close.columns)
    trades = []
    for col in range(S.shape[1]):
        idx = np.flatnonzero(S[:, col])
        busy_until = -1
        for s in idx:
            if s <= busy_until:
                continue                          # one position per stock at a time
            t = simulate_trade(f, strat, col, s, ST[s, col])
            if t is None or t.get("open"):
                continue
            busy_until = t["exit_i"]
            t["symbol"] = syms[col]
            t["date"] = dates[t["entry_i"]]
            trades.append(t)
    return summarize(trades, dates)


def summarize(trades: list[dict], dates) -> dict:
    if not trades:
        return {"trades": 0}
    df = pd.DataFrame(trades).sort_values("date")
    out = _stats(df)
    one_year_ago = dates[-1] - pd.Timedelta(days=365)
    recent = df[df["date"] >= one_year_ago]
    out["last_12m"] = _stats(recent) if len(recent) else {"trades": 0}
    yearly = []
    for y, g in df.groupby(df["date"].dt.year):
        st = _stats(g)
        yearly.append({"year": int(y), "trades": st["trades"], "win_rate": st["win_rate"],
                       "avg_return": st["avg_return"]})
    out["yearly"] = yearly
    out["exit_reasons"] = df["reason"].value_counts().to_dict()
    out["sample"] = [
        {"symbol": r.symbol, "date": str(r.date.date()), "ret": round(r.ret * 100, 2),
         "days": int(r.days), "reason": r.reason}
        for r in df.tail(15).itertuples()]
    return out


def _stats(df: pd.DataFrame) -> dict:
    wins, losses = df[df.ret > 0], df[df.ret <= 0]
    gross_win, gross_loss = wins.ret.sum(), -losses.ret.sum()
    streak = mx = 0
    for r in df.ret:
        streak = streak + 1 if r <= 0 else 0
        mx = max(mx, streak)
    return {
        "trades": int(len(df)),
        "win_rate": round(100 * len(wins) / len(df), 1),
        "avg_return": round(100 * df.ret.mean(), 2),
        "median_return": round(100 * df.ret.median(), 2),
        "avg_win": round(100 * wins.ret.mean(), 2) if len(wins) else 0,
        "avg_loss": round(100 * losses.ret.mean(), 2) if len(losses) else 0,
        "profit_factor": round(gross_win / gross_loss, 2) if gross_loss > 0 else None,
        "expectancy_r": round(df.r.mean(), 2),
        "avg_days": round(df.days.mean(), 1),
        "max_losing_streak": int(mx),
    }


# ======================================================================= investor's real setup
def portfolio_sim(f: Features, strat: SwingStrategy, money: float, slots: int = 1, per_day: int = 1,
                  idle_yield: float = 0.0) -> dict:
    """Simulate the investor's actual account: `money` split into `slots` trades,
    at most `per_day` new trades a day, whole shares only, the strongest stock
    (relative strength) first when several qualify, real costs including the
    fixed depository charge. Same selection rule as the live system."""
    _prep_numpy(f)
    sig, stop = strat.signal_fn(f)
    S, ST = sig.to_numpy(dtype=bool), stop.to_numpy(dtype=float)
    rank = f.rs_rank.to_numpy(dtype=float)
    per = money / slots
    dates = f.close.index
    open_exits, rows, busy = [], [], 0.0
    for i in range(S.shape[0] - 1):
        e = i + 1
        open_exits = [x for x in open_exits if x >= e]
        free = min(slots - len(open_exits), per_day)
        if free <= 0 or not S[i].any():
            continue
        for c in sorted(np.flatnonzero(S[i]), key=lambda c: -np.nan_to_num(rank[i, c])):
            if free <= 0:
                break
            t = simulate_trade(f, strat, c, i, ST[i, c])
            if t is None or t.get("open"):
                continue
            qty = int(per // (f._np["close"][i, c] * (1 + strat.max_gap_up)))
            if qty < 1:
                continue
            gain = qty * (t["exit"] - t["entry"]) - qty * t["entry"] * config.BASE_COST - config.DP_CHARGE_RS
            rows.append({"exit_date": dates[t["exit_i"]], "gain": gain})
            busy += (t["exit_i"] - t["entry_i"] + 1) * qty * t["entry"]
            open_exits.append(t["exit_i"])
            free -= 1
    if not rows:
        return {"trades": 0, "money": money, "slots": slots}
    df = pd.DataFrame(rows).sort_values("exit_date")
    years = max((dates[-1] - dates[0]).days / 365.25, 0.1)
    invested_frac = busy / len(dates) / money
    idle_income = idle_yield * years * money * max(0.0, 1 - invested_frac)
    total = float(df.gain.sum())
    eq = pd.concat([pd.Series([money]), df.groupby("exit_date")["gain"].sum().cumsum() + money],
                   ignore_index=True)
    wins = df.gain > 0
    return {
        "money": money, "slots": slots, "years": round(years, 1), "trades": int(len(df)),
        "trades_per_year": round(len(df) / years, 1), "win_rate": round(100 * float(wins.mean()), 1),
        "total_profit": round(total), "per_year_profit": round(total / years),
        "cagr_pct": round(100 * ((1 + total / money) ** (1 / years) - 1), 1),
        "worst_drop_pct": round(float((eq / eq.cummax() - 1).min() * 100), 1),
        "worst_drop_rs": round(float((eq - eq.cummax()).min())),
        "avg_win_rs": round(float(df.gain[wins].mean())) if wins.any() else 0,
        "avg_loss_rs": round(float(df.gain[~wins].mean())) if (~wins).any() else 0,
        "invested_pct_of_time": round(100 * invested_frac), "idle_income": round(idle_income),
        "yearly": [{"year": int(y), "profit": round(float(v))}
                   for y, v in df.groupby(df.exit_date.dt.year)["gain"].sum().items()],
    }


# ======================================================================= long-term
def backtest_momentum(f: Features, industries: dict[str, str], n: int = config.LT_PORTFOLIO_SIZE,
                      universe: set | None = None, max_per_industry: int = 4,
                      index_weight: float = 0.0, guard_frac: float = 0.5,
                      trend_guard: bool = False) -> dict:
    """Monthly rebalance. trend_guard=True holds 50% cash (earning ~6%/yr) whenever
    the Nifty is below its 200-day average at the rebalance date."""
    c = f.close
    dates = c.index
    month_ends = [i for i in range(len(dates) - 1)
                  if dates[i].month != dates[i + 1].month and i >= config.LT_MIN_HISTORY]
    if len(month_ends) < 6:
        return {}
    holdings: list[str] = []
    eq, bench = [1.0], [1.0]
    curve_dates = [dates[month_ends[0]]]
    monthly_rets, bench_rets = [], []
    cash_yield_m = 0.06 / 12
    for k, i in enumerate(month_ends):
        j = month_ends[k + 1] if k + 1 < len(month_ends) else len(dates) - 1
        if j <= i:
            break
        sc = momentum_scores(f, at=i)
        if universe is not None and not sc.empty:
            sc = sc[sc.index.isin(list(universe))]
        new = pick_portfolio(sc, industries, n, holdings, max_per_industry=max_per_industry)
        if not new:
            continue
        turnover = len(set(new) - set(holdings)) / max(len(new), 1)
        holdings = new
        r = (c[holdings].iloc[j] / c[holdings].iloc[i] - 1).fillna(0).mean()
        r -= turnover * config.ROUND_TRIP_COST
        br_ = f.ix.iloc[j] / f.ix.iloc[i] - 1
        r = (1 - index_weight) * r + index_weight * br_      # part held in a Nifty 50 index fund
        if trend_guard and f.ix.iloc[i] < f.ix_sma200.iloc[i]:
            r = (1 - guard_frac) * r + guard_frac * cash_yield_m * (j - i) / 21
        br = f.ix.iloc[j] / f.ix.iloc[i] - 1
        monthly_rets.append(r)
        bench_rets.append(br)
        eq.append(eq[-1] * (1 + r))
        bench.append(bench[-1] * (1 + br))
        curve_dates.append(dates[j])
    if not monthly_rets:
        return {}
    years = (curve_dates[-1] - curve_dates[0]).days / 365.25
    cagr = lambda x: (x[-1] ** (1 / years) - 1) * 100 if years > 0 else 0
    mdd = lambda x: float(np.min(np.array(x) / np.maximum.accumulate(x) - 1) * 100)
    mr, br = np.array(monthly_rets), np.array(bench_rets)
    return {
        "years": round(years, 1),
        "cagr": round(cagr(eq), 1), "bench_cagr": round(cagr(bench), 1),
        "max_drawdown": round(mdd(eq), 1), "bench_max_drawdown": round(mdd(bench), 1),
        "pct_months_positive": round(100 * (mr > 0).mean(), 1),
        "pct_months_beat_nifty": round(100 * (mr > br).mean(), 1),
        "total_return": round((eq[-1] - 1) * 100, 1),
        "bench_total_return": round((bench[-1] - 1) * 100, 1),
        "curve": [{"d": str(d.date()), "p": round(a * 100, 1), "b": round(b * 100, 1)}
                  for d, a, b in zip(curve_dates, eq, bench)],
    }
