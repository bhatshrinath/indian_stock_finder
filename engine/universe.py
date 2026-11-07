"""Stock universe: current Nifty 200 constituents from NSE, with a bundled fallback."""
from __future__ import annotations

import io
import logging

import pandas as pd
import requests

from . import config

log = logging.getLogger(__name__)


def _parse(text: str) -> pd.DataFrame:
    df = pd.read_csv(io.StringIO(text))
    df.columns = [c.strip() for c in df.columns]
    df = df.rename(columns={"Company Name": "name", "Industry": "industry", "Symbol": "symbol"})
    df = df[["symbol", "name", "industry"]].dropna()
    df["symbol"] = df["symbol"].str.strip()
    return df.reset_index(drop=True)


def load_universe() -> pd.DataFrame:
    """Return DataFrame[symbol, name, industry]. Tries NSE live list, then bundled CSV."""
    for url in config.UNIVERSE_URLS:
        try:
            r = requests.get(url, headers={"User-Agent": config.USER_AGENT}, timeout=10)
            if r.ok and "Symbol" in r.text[:300]:
                df = _parse(r.text)
                if len(df) >= 150:
                    # keep the fallback fresh for the next time NSE is unreachable
                    try:
                        config.UNIVERSE_FALLBACK.write_text(r.text, encoding="utf-8")
                    except OSError:
                        pass
                    log.info("Universe: %d stocks from %s", len(df), url)
                    return df
        except Exception as exc:  # network issues are expected on some hosts
            log.warning("Universe fetch failed from %s: %s", url, exc)
    df = _parse(config.UNIVERSE_FALLBACK.read_text(encoding="utf-8-sig"))
    log.info("Universe: %d stocks from bundled fallback", len(df))
    return df


def yahoo_ticker(symbol: str) -> str:
    return f"{symbol}.NS"
