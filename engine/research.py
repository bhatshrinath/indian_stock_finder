"""Robustness study for the SAFE strategy on real data.

Runs in GitHub Actions (Tests workflow) and prints one annotation, so results
are readable on the run page. The point is to pick a variant that is
*consistently* good across years, not the one with the best headline number
(which is usually luck / over-fitting).

    python -m engine.research
"""
from __future__ import annotations

import pandas as pd

from . import backtest, data
from .strategies import Features, SwingStrategy
from .universe import load_nifty50, load_universe


def variants():
    def base(f):
        return f.large_frame() & f.uptrend & f.liquid & f.broadcast(f.regime_ok) & (f.rsi2 < 10)

    def safe(f):  # the current production rule
        return (base(f) & (f.rs6 > -0.05) & (f.close > f.sma200 * 1.03) & (f.vol252 < 0.30)
                & (f.sma200 > f.sma200.shift(20)))

    two_down = lambda f: (f.close < f.close.shift(1)) & (f.close.shift(1) < f.close.shift(2))
    bull = lambda f: f.broadcast(f.regime == 2)
    return [
        # --- round 2: neighbours of the best round-1 variant (market must be rising)
        ("B1 V1+bull", lambda f: base(f) & bull(f), 2.5, 10),
        ("B2 V3(lowvol)+bull", lambda f: base(f) & (f.vol252 < 0.30) & bull(f), 2.5, 10),
        ("B3 SAFE+bull", lambda f: safe(f) & bull(f), 2.5, 10),
        ("B4 B3 stop2ATR", lambda f: safe(f) & bull(f), 2.0, 10),
        ("B5 B3 hold5", lambda f: safe(f) & bull(f), 2.5, 5),
        ("B6 B3+rsi2<5", lambda f: safe(f) & bull(f) & (f.rsi2 < 5), 2.5, 10),
        # --- round 1
        # name, signal rule, stop in ATRs, max hold days
        ("V1 N50 rsi2<10", base, 2.5, 10),
        ("V2 V1+rsi2<5", lambda f: base(f) & (f.rsi2 < 5), 2.5, 10),
        ("V3 V1+lowvol", lambda f: base(f) & (f.vol252 < 0.30), 2.5, 10),
        ("V4 SAFE(current)", safe, 2.5, 10),
        ("V5 SAFE+bull only", lambda f: safe(f) & f.broadcast(f.regime == 2), 2.5, 10),
        ("V6 SAFE stop3ATR", safe, 3.0, 10),
        ("V7 SAFE stop2ATR", safe, 2.0, 10),
        ("V8 SAFE hold5", safe, 2.5, 5),
        ("V9 SAFE+2 down days", lambda f: safe(f) & two_down(f), 2.5, 10),
        ("V10 N200 dip (ref)", lambda f: f.uptrend & f.liquid & f.broadcast(f.regime_ok) & (f.rsi2 < 10)
         & (f.rs6 > 0) & (f.close > f.sma200 * 1.02), 2.5, 10),
    ]


def portfolio_sim(f, rule, stop_atr, hold, money=20000.0, slots=2, per_day=1):
    """The investor's real setup: Rs 20,000, at most 2 trades open, 1 new per day,
    about Rs 10,000 per trade, whole shares only."""
    import numpy as np
    strat = SwingStrategy(key="sim", name="sim", plain_english="", how_to_exit="",
                          signal_fn=lambda f: (rule(f).fillna(False), f.close - stop_atr * f.atr14),
                          target_r=None, max_hold=hold, exit_above_sma5=True)
    backtest._prep_numpy(f)
    sig, stop = strat.signal_fn(f)
    S, ST = sig.to_numpy(bool), stop.to_numpy(float)
    rank = f.rs_rank.to_numpy(float)
    per = money / slots
    open_exits, pnl, taken, wins = [], [], 0, 0
    dates = f.close.index
    curve = {}
    for i in range(S.shape[0] - 1):
        e = i + 1
        open_exits = [x for x in open_exits if x >= e]
        free = min(slots - len(open_exits), per_day)
        if free <= 0 or not S[i].any():
            continue
        cols = sorted(np.flatnonzero(S[i]), key=lambda c: -np.nan_to_num(rank[i, c]))
        for c in cols:
            if free <= 0:
                break
            t = backtest.simulate_trade(f, strat, c, i, ST[i, c])
            if t is None or t.get("open"):
                continue
            qty = int(per // (f._np["close"][i, c] * 1.02))
            if qty < 1:
                continue
            gain = qty * (t["exit"] - t["entry"]) - qty * t["entry"] * 0.003
            pnl.append(gain)
            taken += 1
            wins += gain > 0
            curve[dates[t["exit_i"]]] = curve.get(dates[t["exit_i"]], 0) + gain
            open_exits.append(t["exit_i"])
            free -= 1
    if not pnl:
        return "no trades"
    eq = pd.Series(curve).sort_index().cumsum() + money
    dd = float((eq / eq.cummax() - 1).min() * 100)
    years = (dates[-1] - dates[0]).days / 365.25
    total = sum(pnl)
    yearly = pd.Series(curve).groupby(lambda d: d.year).sum()
    return (f"{taken} trades ({taken / years:.0f}/yr), won {100 * wins / taken:.0f}%, total profit Rs {total:,.0f} "
            f"on Rs {money:,.0f} ({100 * ((1 + total / money) ** (1 / years) - 1):.1f}%/yr), worst drop {dd:.1f}%, "
            f"avg win Rs {np.mean([x for x in pnl if x > 0]):,.0f}, avg loss Rs {np.mean([x for x in pnl if x <= 0]):,.0f}; "
            "by year: " + ", ".join(f"{y}: Rs {v:,.0f}" for y, v in yearly.items()))


def main() -> None:
    uni = load_universe()
    p = data.load_yfinance(list(uni.symbol))
    f = Features(p)
    f.set_large(load_nifty50())
    years = (f.close.index[-1] - f.close.index[0]).days / 365.25
    rows = [f"data {f.close.index[0].date()}..{f.close.index[-1].date()} ({years:.1f}y), N50={int(f.large.sum())}",
            "name | trades(/yr) | win% | avg% | PF | avgW/avgL | worst-yr avg% (win%) | streak | last12m n/win/avg"]
    for name, rule, stop_atr, hold in variants():
        strat = SwingStrategy(
            key="study", name=name, plain_english="", how_to_exit="",
            signal_fn=lambda f, rule=rule, k=stop_atr: (rule(f).fillna(False), f.close - k * f.atr14),
            target_r=None, max_hold=hold, exit_above_sma5=True)
        r = backtest.backtest_swing(f, strat)
        if not r.get("trades"):
            rows.append(f"{name} | 0 trades")
            continue
        yrs = [y for y in r.get("yearly", []) if y["trades"] >= 5]
        worst = min(yrs, key=lambda y: y["avg_return"]) if yrs else None
        l12 = r.get("last_12m") or {}
        rows.append(
            f"{name} | {r['trades']}({r['trades'] / years:.0f}) | {r['win_rate']} | {r['avg_return']} | "
            f"{r['profit_factor']} | {r['avg_win']}/{r['avg_loss']} | "
            f"{worst['year'] if worst else '-'}: {worst['avg_return'] if worst else '-'} ({worst['win_rate'] if worst else '-'}) | "
            f"{r['max_losing_streak']} | {l12.get('trades', 0)}/{l12.get('win_rate', '-')}/{l12.get('avg_return', '-')}")
    # yearly detail + the investor's real setup for the leading candidates
    vs = {v[0]: v for v in variants()}
    for key in ("B1 V1+bull", "B3 SAFE+bull"):
        name, rule, stop_atr, hold = vs[key]
        strat = SwingStrategy(key="y", name=name, plain_english="", how_to_exit="",
                              signal_fn=lambda f, rule=rule, k=stop_atr: (rule(f).fillna(False), f.close - k * f.atr14),
                              target_r=None, max_hold=hold, exit_above_sma5=True)
        r = backtest.backtest_swing(f, strat)
        rows.append(f"{key} yearly: " + ", ".join(f"{y['year']}: {y['trades']}t {y['win_rate']}% {y['avg_return']}%"
                                                  for y in r.get("yearly", [])))
        rows.append(f"{key} with Rs 20,000 (2 slots, 1/day): " + portfolio_sim(f, rule, stop_atr, hold))
    print("::notice title=safe-study::" + "%0A".join(rows), flush=True)
    print("\n".join(rows))


if __name__ == "__main__":
    main()
