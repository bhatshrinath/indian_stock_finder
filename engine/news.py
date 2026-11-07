"""Latest news per stock + market headlines, with simple finance sentiment and
red-flag detection. Sources are public RSS feeds (no API keys needed)."""
from __future__ import annotations

import logging
import re
import time
import xml.etree.ElementTree as ET
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from urllib.parse import quote_plus

import requests

from . import config

log = logging.getLogger(__name__)

GOOGLE_NEWS = "https://news.google.com/rss/search?q={q}&hl=en-IN&gl=IN&ceid=IN:en"
MARKET_FEEDS = [
    ("Moneycontrol", "https://www.moneycontrol.com/rss/marketreports.xml"),
    ("Economic Times", "https://economictimes.indiatimes.com/markets/rssfeeds/1977021501.cms"),
    ("Google News", GOOGLE_NEWS.format(q=quote_plus("Sensex Nifty stock market when:1d"))),
]

# Compact finance lexicon (inspired by Loughran-McDonald word lists, tuned for Indian news).
POSITIVE = {
    "surge", "surges", "soar", "soars", "jump", "jumps", "rally", "rallies", "gain", "gains",
    "beat", "beats", "record", "high", "upgrade", "upgrades", "upgraded", "buy", "outperform",
    "strong", "growth", "profit", "profits", "rises", "rise", "rose", "order", "orders", "wins",
    "win", "bags", "expansion", "dividend", "bonus", "approval", "approved", "robust", "boost",
    "positive", "bullish", "accumulate", "target", "raised", "raises", "acquire", "partnership",
    "launch", "launches", "contract", "beats estimates", "margin expansion", "turnaround",
}
NEGATIVE = {
    "fall", "falls", "fell", "drop", "drops", "plunge", "plunges", "slump", "slumps", "crash",
    "decline", "declines", "loss", "losses", "miss", "misses", "downgrade", "downgrades",
    "downgraded", "sell", "underperform", "weak", "weakness", "cut", "cuts", "lower", "slows",
    "bearish", "concern", "concerns", "pressure", "probe", "penalty", "fine", "lawsuit",
    "resigns", "resignation", "exit", "warning", "tumble", "tumbles", "sinks", "sink", "low",
}
# Serious items: a stock with any of these in recent headlines is flagged, not recommended.
RED_FLAGS = [
    r"\bsebi\b.*\b(order|ban|probe|penalty|notice|show.?cause)", r"\bfraud\b", r"\bscam\b",
    r"\braid(s|ed)?\b", r"\bed\b.*\b(search|raid|probe)", r"\bdefault(s|ed)?\b",
    r"\bpledge(d|s)?\b", r"\bauditor\b.*\bresign", r"\bcfo\b.*\bresign", r"\binsolvency\b",
    r"\bforensic audit\b", r"\bmoney laundering\b", r"\bdowngrade(d)? to (sell|junk)",
    r"\bincome tax\b.*\b(raid|search)", r"\bwhistle.?blower\b", r"\bnclt\b",
]
_RF = [re.compile(p, re.I) for p in RED_FLAGS]
_WORD = re.compile(r"[a-z]+")


def score_headline(text: str) -> float:
    words = _WORD.findall(text.lower())
    pos = sum(w in POSITIVE for w in words)
    neg = sum(w in NEGATIVE for w in words)
    if pos == neg == 0:
        return 0.0
    return (pos - neg) / (pos + neg)


def red_flags(text: str) -> list[str]:
    return [m.group(0) for r in _RF if (m := r.search(text))]


def _fetch_rss(url: str, limit: int = 12) -> list[dict]:
    r = requests.get(url, headers={"User-Agent": config.USER_AGENT}, timeout=15)
    r.raise_for_status()
    root = ET.fromstring(r.content)
    items = []
    for it in root.iter("item"):
        title = (it.findtext("title") or "").strip()
        if not title:
            continue
        link = (it.findtext("link") or "").strip()
        src = it.find("source")
        pub = it.findtext("pubDate")
        try:
            dt = parsedate_to_datetime(pub) if pub else None
            if dt and dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
        except (TypeError, ValueError):
            dt = None
        items.append({
            "title": title, "link": link,
            "source": src.text.strip() if src is not None and src.text else "",
            "published": dt.isoformat() if dt else None,
            "_ts": dt.timestamp() if dt else 0,
        })
    items.sort(key=lambda x: x["_ts"], reverse=True)
    return items[:limit]


def stock_news(symbol: str, company: str, days: int = 10) -> dict:
    name = re.sub(r"\b(Ltd|Limited|Ltd\.)\b\.?", "", company).strip()
    q = quote_plus(f'"{name}" (share OR stock OR shares) when:{days}d')
    try:
        items = _fetch_rss(GOOGLE_NEWS.format(q=q), limit=8)
    except Exception as exc:
        log.debug("news %s failed: %s", symbol, exc)
        return {"items": [], "sentiment": 0.0, "red_flags": []}
    now = time.time()
    weighted, wsum, flags = 0.0, 0.0, []
    for it in items:
        s = score_headline(it["title"])
        it["sentiment"] = round(s, 2)
        age_days = max(0.0, (now - it["_ts"]) / 86400) if it["_ts"] else days
        w = 0.5 ** (age_days / 3)                       # recent news matters more
        weighted += s * w
        wsum += w
        flags += red_flags(it["title"])
        it.pop("_ts", None)
    return {"items": items[:5], "sentiment": round(weighted / wsum, 2) if wsum else 0.0,
            "red_flags": sorted(set(flags))}


def news_for(symbols: dict[str, str], workers: int = 6) -> dict[str, dict]:
    """symbols: {symbol: company name}"""
    out = {}
    with ThreadPoolExecutor(max_workers=workers) as ex:
        futs = {ex.submit(stock_news, s, n): s for s, n in symbols.items()}
        for fut, s in futs.items():
            try:
                out[s] = fut.result(timeout=40)
            except Exception:
                out[s] = {"items": [], "sentiment": 0.0, "red_flags": []}
    return out


def market_news(limit: int = 15) -> list[dict]:
    seen, out = set(), []
    for name, url in MARKET_FEEDS:
        try:
            for it in _fetch_rss(url, limit=8):
                key = it["title"][:60].lower()
                if key in seen:
                    continue
                seen.add(key)
                it["source"] = it["source"] or name
                it["sentiment"] = round(score_headline(it["title"]), 2)
                out.append(it)
        except Exception as exc:
            log.debug("market feed %s failed: %s", name, exc)
    out.sort(key=lambda x: x.get("_ts", 0), reverse=True)
    for it in out:
        it.pop("_ts", None)
    return out[:limit]
