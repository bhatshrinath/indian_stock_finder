"""Trading rules.

Design principles
-----------------
* Only well-documented, robust edges: trend-following + buying short-term dips
  inside long-term uptrends, and cross-sectional momentum for long-term
  investing (the same idea behind NSE's Nifty200 Momentum 30 index).
* A market "regime" filter: no new swing buys while the Nifty 50 is below its
  200-day average (this alone avoids most of the damage in bear markets).
* Every swing signal comes with a pre-defined stop-loss, target and time limit,
  so the user always knows the exit before entering.
* The SAME functions generate today's picks and the historical backtest, so the
  win rates shown in the app describe exactly what is being recommended.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Callable

import numpy as np
import pandas as pd

from . import config
from . import indicators as ind
from .data import Prices


# ======================================================================= features
class Features:
    """All indicators computed once for the whole universe."""

    def __init__(self, p: Prices):
        self.p = p
        c, h, l, o, v = p.close, p.high, p.low, p.open, p.volume
        self.close, self.high, self.low, self.open, self.volume = c, h, l, o, v
        self.sma5 = ind.sma(c, 5)
        self.sma20 = ind.sma(c, 20)
        self.sma50 = ind.sma(c, 50)
        self.sma200 = ind.sma(c, 200)
        self.ema20 = ind.ema(c, 20)
        self.rsi2 = ind.rsi(c, 2)
        self.rsi14 = ind.rsi(c, 14)
        self.atr14 = ind.atr(h, l, c, 14)
        self.avgvol50 = ind.sma(v, 50)
        self.vol_ratio = v / self.avgvol50.shift(1)
        self.turnover_cr = ind.sma(c * v, 20) / 1e7
        self.prior_high50 = ind.rolling_high(h, 50).shift(1)
        self.high252 = ind.rolling_high(h, 252)
        self.low5 = ind.rolling_low(l, 5)
        self.ret21 = ind.pct_change(c, 21)
        self.ret63 = ind.pct_change(c, 63)
        self.ret126 = ind.pct_change(c, 126)
        self.ret252 = ind.pct_change(c, 252)
        self.vol252 = ind.annual_vol(c, 252)

        ix = p.index_close
        self.ix = ix
        self.ix_sma50 = ind.sma(ix, 50)
        self.ix_sma200 = ind.sma(ix, 200)
        self.ix_ret126 = ind.pct_change(ix, 126)
        # relative strength vs Nifty over 6 months, and its percentile across stocks
        self.rs6 = self.ret126.sub(self.ix_ret126, axis=0)
        self.rs_rank = ind.pct_rank_rows(self.ret126)

        self.liquid = (c >= config.MIN_PRICE) & (self.turnover_cr >= config.MIN_TURNOVER_CR)
        self.uptrend = (c > self.sma200) & (self.sma50 > self.sma200)
        # regime: 2 = bullish, 1 = neutral (above 200d but weak), 0 = bearish
        bull = (ix > self.ix_sma200) & (self.ix_sma50 > self.ix_sma200) & (ix > self.ix_sma50)
        above = ix > self.ix_sma200
        self.regime = pd.Series(np.where(bull, 2, np.where(above, 1, 0)), index=ix.index)
        self.regime_ok = (self.regime >= 1)

    def broadcast(self, s: pd.Series) -> pd.DataFrame:
        """Turn a per-date Series into a frame aligned with the stock columns."""
        return pd.DataFrame(np.repeat(s.values[:, None], self.close.shape[1], axis=1),
                            index=self.close.index, columns=self.close.columns)


# ======================================================================= swing strategies
@dataclass
class SwingStrategy:
    key: str
    name: str
    plain_english: str
    how_to_exit: str
    signal_fn: Callable[[Features], tuple[pd.DataFrame, pd.DataFrame]]
    target_r: float | None          # target as a multiple of risk (None = dynamic exit)
    max_hold: int                   # trading days
    exit_above_sma5: bool = False   # mean-reversion exit
    trail_atr: float | None = None  # trailing stop distance in ATRs (None = no trail)
    max_gap_up: float = 0.02        # skip entry if next open gaps > this above signal close


def _dip_in_uptrend(f: Features):
    """Connors-style: 2-day RSI deeply oversold while the long-term trend is up."""
    sig = (f.uptrend & f.liquid & (f.rsi2 < 10) & (f.rs6 > 0)
           & (f.close > f.sma200 * 1.02) & f.broadcast(f.regime_ok))
    stop = f.close - 2.5 * f.atr14
    return sig.fillna(False), stop


def _breakout(f: Features):
    """New 50-day high on strong volume, in an established uptrend, from a market leader."""
    rng = (f.high - f.low).replace(0, np.nan)
    close_strength = (f.close - f.low) / rng
    sig = (f.uptrend & f.liquid & (f.close > f.prior_high50) & (f.vol_ratio > 1.5)
           & (close_strength > 0.6) & (f.rs_rank > 0.7) & (f.rsi14 < 80)
           & (f.close < f.sma50 * 1.15)              # not over-extended
           & f.broadcast(f.regime == 2))
    stop = f.close - 2.0 * f.atr14
    return sig.fillna(False), stop


def _ema_bounce(f: Features):
    """Pullback to the 20-day average in a strong trend, then a bullish reversal day."""
    sma50_rising = f.sma50 > f.sma50.shift(20)
    touched = f.low <= f.ema20 * 1.01
    reversal = (f.close > f.ema20) & (f.close > f.open) & (f.close > f.high.shift(1))
    sig = (f.uptrend & f.liquid & sma50_rising & touched & reversal
           & (f.rsi14 >= 40) & (f.rsi14 <= 65) & (f.rs6 > 0) & f.broadcast(f.regime_ok))
    raw_stop = f.low5 - 0.2 * f.atr14
    # keep the stop between 1 and 3 ATR away so risk is sensible
    stop = raw_stop.clip(lower=f.close - 3 * f.atr14, upper=f.close - 1 * f.atr14)
    return sig.fillna(False), stop


SWING_STRATEGIES: list[SwingStrategy] = [
    SwingStrategy(
        key="dip_buy", name="Dip in an uptrend",
        plain_english=("A strong, rising stock has fallen sharply for 2-3 days. Such short dips "
                       "in leading stocks usually bounce back within days."),
        how_to_exit=("Sell when the price closes above its 5-day average (usually 3-7 days), "
                     "or at the stop-loss, or after 10 trading days, whichever comes first."),
        signal_fn=_dip_in_uptrend, target_r=None, max_hold=10, exit_above_sma5=True),
    SwingStrategy(
        key="breakout", name="Breakout with volume",
        plain_english=("The stock just made a new 50-day high on 1.5x normal volume and is one "
                       "of the strongest 30% in the market. Big buyers are often behind such moves."),
        how_to_exit=("Sell at the target, or if the trailing stop is hit (it rises as the price "
                     "rises), or after 30 trading days."),
        signal_fn=_breakout, target_r=3.0, max_hold=30, trail_atr=3.0),
    SwingStrategy(
        key="ema_bounce", name="Bounce from support",
        plain_english=("A stock in a healthy uptrend pulled back to its 20-day average (a common "
                       "support level) and closed strongly higher today."),
        how_to_exit="Sell at the target (2x the risk), at the stop-loss, or after 20 trading days.",
        signal_fn=_ema_bounce, target_r=2.0, max_hold=20),
]


# ======================================================================= long-term model
def momentum_scores(f: Features, at: int | None = None) -> pd.DataFrame:
    """Normalised momentum score (NSE Momentum-30 style) + trend/quality filters.

    Score = average of z-scores of (12-1 month return / volatility) and
    (6-1 month return / volatility). Skipping the latest month avoids
    buying stocks right after a short-term spike.
    """
    c = f.close
    i = len(c) - 1 if at is None else at
    if i < config.LT_MIN_HISTORY:
        return pd.DataFrame()
    row = lambda df: df.iloc[i]
    m12 = c.iloc[i - 21] / c.iloc[i - 252] - 1
    m6 = c.iloc[i - 21] / c.iloc[i - 126] - 1
    vol = row(f.vol252)
    df = pd.DataFrame({"m12": m12, "m6": m6, "vol": vol})
    eligible = (row(f.close) > row(f.sma200)) & row(f.liquid) \
        & (row(f.close) > 0.75 * row(f.high252)) & vol.notna()
    df = df[eligible.reindex(df.index).fillna(False)].dropna()
    if len(df) < 20:
        return pd.DataFrame()
    a, b = df.m12 / df.vol, df.m6 / df.vol
    z = lambda s: ((s - s.mean()) / s.std()).clip(-3, 3)
    df["score"] = 0.5 * z(a) + 0.5 * z(b)
    df["price"] = row(c).reindex(df.index)
    return df.sort_values("score", ascending=False)


def pick_portfolio(scores: pd.DataFrame, industries: dict[str, str], n: int,
                   current: list[str] | None = None, max_per_industry: int = 4,
                   buffer_rank: int | None = None) -> list[str]:
    """Top-n by score with an industry cap. Existing holdings are kept while they
    stay inside the buffer rank, which cuts needless churn and costs."""
    if scores.empty:
        return []
    ranked = list(scores.index)
    buffer_rank = buffer_rank or 2 * n
    keep = [s for s in (current or []) if s in ranked[:buffer_rank]]
    chosen, per_ind = [], {}
    for s in keep + ranked:
        if s in chosen:
            continue
        ind_ = industries.get(s, "Other")
        if per_ind.get(ind_, 0) >= max_per_industry:
            continue
        chosen.append(s)
        per_ind[ind_] = per_ind.get(ind_, 0) + 1
        if len(chosen) >= n:
            break
    return chosen
