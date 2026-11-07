"""Robustness study for the SAFE strategy on real data.

Runs in GitHub Actions (Tests workflow) and prints one annotation, so results
are readable on the run page. The point is to pick a variant that is
*consistently* good across years, not the one with the best headline number
(which is usually luck / over-fitting).

    python -m engine.research
"""
from __future__ import annotations

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
    return [
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
    print("::notice title=safe-study::" + "%0A".join(rows), flush=True)
    print("\n".join(rows))


if __name__ == "__main__":
    main()
