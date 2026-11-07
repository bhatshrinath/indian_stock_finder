"""Vectorised technical indicators. Every function works on a wide DataFrame
(dates x symbols) or a Series, so the whole universe is computed in one shot."""
from __future__ import annotations

import numpy as np
import pandas as pd


def sma(x, n):
    return x.rolling(n, min_periods=n).mean()


def ema(x, n):
    return x.ewm(span=n, adjust=False, min_periods=n).mean()


def rsi(close, n=14):
    """Wilder's RSI."""
    d = close.diff()
    up = d.clip(lower=0).ewm(alpha=1 / n, adjust=False, min_periods=n).mean()
    dn = (-d.clip(upper=0)).ewm(alpha=1 / n, adjust=False, min_periods=n).mean()
    rs = up / dn.replace(0, np.nan)
    out = 100 - 100 / (1 + rs)
    return out.where(dn != 0, 100.0)


def true_range(high, low, close):
    pc = close.shift(1)
    a = high - low
    b = (high - pc).abs()
    c = (low - pc).abs()
    if isinstance(a, pd.DataFrame):
        return np.maximum(np.maximum(a, b), c)
    return pd.concat([a, b, c], axis=1).max(axis=1)


def atr(high, low, close, n=14):
    return true_range(high, low, close).ewm(alpha=1 / n, adjust=False, min_periods=n).mean()


def pct_change(close, n):
    return close / close.shift(n) - 1


def annual_vol(close, n=252):
    return np.log(close).diff().rolling(n, min_periods=int(n * 0.8)).std() * np.sqrt(252)


def rolling_high(x, n):
    return x.rolling(n, min_periods=n).max()


def rolling_low(x, n):
    return x.rolling(n, min_periods=n).min()


def zscore_rows(df: pd.DataFrame) -> pd.DataFrame:
    """Cross-sectional z-score (per date, across stocks), clipped to +-3."""
    m = df.mean(axis=1)
    s = df.std(axis=1).replace(0, np.nan)
    return df.sub(m, axis=0).div(s, axis=0).clip(-3, 3)


def pct_rank_rows(df: pd.DataFrame) -> pd.DataFrame:
    return df.rank(axis=1, pct=True)
