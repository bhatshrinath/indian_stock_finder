"""Basic quality/valuation data for long-term candidates (best effort via yfinance).

Used as a *filter* on top of momentum: avoid highly indebted, loss-making or
wildly expensive companies. If the data is unavailable the stock is not
penalised (we never invent numbers)."""
from __future__ import annotations

import logging
from concurrent.futures import ThreadPoolExecutor

from .universe import yahoo_ticker

log = logging.getLogger(__name__)

FIELDS = {
    "returnOnEquity": "roe", "debtToEquity": "debt_to_equity", "trailingPE": "pe",
    "priceToBook": "pb", "profitMargins": "profit_margin", "earningsGrowth": "earnings_growth",
    "revenueGrowth": "revenue_growth", "marketCap": "market_cap", "dividendYield": "dividend_yield",
    "sector": "sector",
}


def _one(symbol: str) -> dict:
    try:
        import yfinance as yf
        info = yf.Ticker(yahoo_ticker(symbol)).info or {}
    except Exception as exc:
        log.debug("fundamentals %s failed: %s", symbol, exc)
        return {}
    out = {}
    for k, v in FIELDS.items():
        val = info.get(k)
        if val is None:
            continue
        if isinstance(val, (int, float)):
            if k in ("returnOnEquity", "profitMargins", "earningsGrowth", "revenueGrowth"):
                val = round(val * 100, 1)
            elif k == "marketCap":
                val = round(val / 1e7)                 # ₹ crore
            else:
                val = round(float(val), 2)
        out[v] = val
    return out


def fetch(symbols: list[str], workers: int = 4) -> dict[str, dict]:
    with ThreadPoolExecutor(max_workers=workers) as ex:
        res = list(ex.map(_one, symbols))
    return {s: r for s, r in zip(symbols, res) if r}


def quality_check(f: dict) -> tuple[int, list[str]]:
    """Return (quality points -2..+3, list of warnings)."""
    if not f:
        return 0, []
    pts, warn = 0, []
    roe, de, pe = f.get("roe"), f.get("debt_to_equity"), f.get("pe")
    eg, pm = f.get("earnings_growth"), f.get("profit_margin")
    if roe is not None:
        if roe >= 15:
            pts += 1
        elif roe < 8:
            pts -= 1
            warn.append(f"Low return on equity ({roe}%)")
    if de is not None and de > 150:                    # yfinance reports D/E in %
        pts -= 1
        warn.append(f"High debt (D/E {de / 100:.1f}x)")
    if pm is not None and pm < 0:
        pts -= 1
        warn.append("Company is loss-making")
    if eg is not None and eg > 10:
        pts += 1
    if pe is not None and pe > 90:
        warn.append(f"Very expensive (P/E {pe:.0f})")
    elif pe is not None and 0 < pe < 35:
        pts += 1
    return pts, warn
