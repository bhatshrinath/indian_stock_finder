"""Robustness study for the SAFE strategy on real data.

Runs in GitHub Actions (Tests workflow) and prints one annotation, so results
are readable on the run page. The point is to pick a variant that is
*consistently* good across years, not the one with the best headline number
(which is usually luck / over-fitting).

    python -m engine.research
"""
from __future__ import annotations

import pandas as pd

from . import backtest, config, data
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
        ("B3 SAFE+bull", lambda f: safe(f) & bull(f), 2.5, 10),
        ("B4 B3 stop2ATR", lambda f: safe(f) & bull(f), 2.0, 10),
        ("B5 B3 hold5", lambda f: safe(f) & bull(f), 2.5, 5),
        # --- round 1
        # name, signal rule, stop in ATRs, max hold days
        ("V1 N50 rsi2<10", base, 2.5, 10),
        ("V4 SAFE(current)", safe, 2.5, 10),
        ("V5 SAFE+bull only", lambda f: safe(f) & f.broadcast(f.regime == 2), 2.5, 10),
        ("V10 N200 dip (ref)", lambda f: f.uptrend & f.liquid & f.broadcast(f.regime_ok) & (f.rsi2 < 10)
         & (f.rs6 > 0) & (f.close > f.sma200 * 1.02), 2.5, 10),
    ]


def portfolio_sim(f, rule, stop_atr, hold, money=20000.0, slots=2, per_day=1, idle_yield=0.0):
    strat = SwingStrategy(key="sim", name="sim", plain_english="", how_to_exit="",
                          signal_fn=lambda f: (rule(f).fillna(False), f.close - stop_atr * f.atr14),
                          target_r=None, max_hold=hold, exit_above_sma5=True)
    r = backtest.portfolio_sim(f, strat, money=money, slots=slots, per_day=per_day, idle_yield=idle_yield)
    if not r.get("trades"):
        return "no trades"
    total = r["total_profit"] + r["idle_income"]
    return (f"{r['trades']} trades ({r['trades_per_year']:.0f}/yr), won {r['win_rate']:.0f}%, "
            f"in trades {r['invested_pct_of_time']}% of the time, idle income Rs {r['idle_income']:,}, "
            f"total Rs {total:,} on Rs {money:,.0f}, worst drop {r['worst_drop_pct']}%, "
            f"avg win Rs {r['avg_win_rs']}, avg loss Rs {r['avg_loss_rs']}; by year: "
            + ", ".join(f"{y['year']}: Rs {y['profit']:,}" for y in r["yearly"]))


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
    # the investor's real setup, with realistic costs, for the production rule
    vs = {v[0]: v for v in variants()}
    name, rule, stop_atr, hold = vs["B3 SAFE+bull"]
    rows.append(f"costs: {config.BASE_COST * 100:.2f}% + Rs {config.DP_CHARGE_RS} per sell "
                f"(= {config.ROUND_TRIP_COST * 100:.2f}% on Rs {config.TRADE_AMOUNT:,.0f})")
    setups = (("2 x Rs 10,000", 20000.0, 2, 0.0), ("1 x Rs 20,000", 20000.0, 1, 0.0),
              ("2 x Rs 10,000 + idle cash in liquid fund 6.5%", 20000.0, 2, 0.065),
              ("1 x Rs 20,000 + idle cash in liquid fund 6.5%", 20000.0, 1, 0.065),
              ("2 x Rs 5,000 (Rs 10,000 total)", 10000.0, 2, 0.0))
    for label, money, slots, idle in setups:
        rows.append(f"SIM {label}: " + portfolio_sim(f, rule, stop_atr, hold, money=money, slots=slots,
                                                      idle_yield=idle))
    rows.append("Nifty 50 buy-and-hold over the same period: "
                f"{100 * ((f.ix.iloc[-1] / f.ix.iloc[0]) ** (1 / years) - 1):.1f}%/yr")
    # ---- long-term: the app's suggestion method with risk controls
    inds = dict(zip(uni.symbol, uni.industry))
    n50 = set(f.large[f.large].index)
    rows.append("LONG-TERM (Nifty 50, 1 per sector, monthly) | per yr | worst fall | months up | yearly")
    for label, kw in (("4 stocks", dict(n=4)),
                      ("4 stocks + guard 100%", dict(n=4, trend_guard=True, guard_frac=1.0)),
                      ("2 stocks + 50% index", dict(n=2, index_weight=0.5)),
                      ("2 stocks + 50% index + guard 100%", dict(n=2, index_weight=0.5, trend_guard=True, guard_frac=1.0)),
                      ("3 stocks + 33% index + guard 100%", dict(n=3, index_weight=1 / 3, trend_guard=True, guard_frac=1.0)),
                      ("3 stocks + 33% index + guard 50%", dict(n=3, index_weight=1 / 3, trend_guard=True, guard_frac=0.5)),
                      ("index only", dict(n=1, index_weight=1.0)),
                      ("index + guard 100%", dict(n=1, index_weight=1.0, trend_guard=True, guard_frac=1.0))):
        r = backtest.backtest_momentum(f, inds, universe=n50, max_per_industry=1, **kw)
        if not r:
            continue
        cur = r["curve"]
        yearly = {}
        for a, b in zip(cur, cur[1:]):
            yearly.setdefault(b["d"][:4], [a["p"], b["p"]])[1] = b["p"]
        rows.append(f"{label} | {r['cagr']}% (Nifty {r['bench_cagr']}%) | {r['max_drawdown']}% (Nifty {r['bench_max_drawdown']}%) | "
                    f"{r['pct_months_positive']}% | " + ", ".join(f"{y}: {100 * (v[1] / v[0] - 1):+.1f}%" for y, v in yearly.items()))
    print("::notice title=safe-study::" + "%0A".join(rows), flush=True)
    print("\n".join(rows))


if __name__ == "__main__":
    main()
