"""Feed-health and freshness rules for the near-live market snapshot.

The daily strategy engine remains authoritative.  This module only describes
the health of intraday observations and guarantees that a provider gap cannot
erase the last useful display snapshot or create a stale trading alert.
"""
from __future__ import annotations

from datetime import datetime, timezone


DATA_DELAY_NOTE = (
    "Free Yahoo Finance intraday data, refreshed by GitHub Actions about every 15 minutes. "
    "It can be delayed or unavailable and is not an exchange-grade real-time feed."
)


def empty_snapshot(updated: str, market_open: bool = False) -> dict:
    """A complete, backwards-compatible live.json value with no quotes."""
    return {
        "updated": updated,
        "market_open": market_open,
        "feed_status": "unavailable" if market_open else "closed",
        "requested": 0,
        "received": 0,
        "fresh_quotes": 0,
        "coverage_pct": 0.0,
        "fresh_coverage_pct": 0.0,
        "last_good_update": None,
        "provider": "Yahoo Finance",
        "data_delay_note": DATA_DELAY_NOTE,
        "quotes": {},
    }


def _stale_copy(quote: dict, now: datetime) -> dict:
    out = dict(quote)
    out["is_fresh"] = False
    out["stale"] = True
    asof = out.get("asof")
    if asof:
        try:
            stamp = datetime.fromisoformat(asof.replace("Z", "+00:00"))
            if stamp.tzinfo is None:
                stamp = stamp.replace(tzinfo=timezone.utc)
            out["delay_seconds"] = max(0, int((now - stamp.astimezone(timezone.utc)).total_seconds()))
        except (TypeError, ValueError):
            pass
    return out


def _asof(quote: dict) -> datetime | None:
    try:
        stamp = datetime.fromisoformat(str(quote.get("asof", "")).replace("Z", "+00:00"))
        return (stamp if stamp.tzinfo else stamp.replace(tzinfo=timezone.utc)).astimezone(timezone.utc)
    except (TypeError, ValueError):
        return None


def fresh_quotes(quotes: dict[str, dict]) -> dict[str, dict]:
    """Only observations safe to use for intraday stop/target evaluation."""
    return {symbol: quote for symbol, quote in quotes.items()
            if quote.get("is_fresh") is True and not quote.get("stale")}


def build_snapshot(current: dict[str, dict], requested_symbols: list[str],
                   previous: dict | None, market_open: bool,
                   updated: str | None = None) -> dict:
    """Build feed metadata, retaining missing prior quotes as explicitly stale."""
    now = datetime.now(timezone.utc) if updated is None else datetime.fromisoformat(updated.replace("Z", "+00:00"))
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)
    now = now.astimezone(timezone.utc)
    updated = now.isoformat()

    expected = sorted(set(requested_symbols))
    prior_quotes = (previous or {}).get("quotes", {})
    merged = {symbol: dict(quote) for symbol, quote in current.items()}
    for symbol, quote in prior_quotes.items():
        if symbol not in expected:
            continue
        current_quote = merged.get(symbol)
        # A failed minute request can still return an older daily fallback.
        # Keep the newer last-good minute observation for display, but mark it
        # stale so neither version can fire an alert.
        prefer_prior = (current_quote is not None
                        and not current_quote.get("is_fresh")
                        and _asof(quote) is not None
                        and (_asof(current_quote) is None or _asof(quote) > _asof(current_quote)))
        if current_quote is None or prefer_prior:
            merged[symbol] = _stale_copy(quote, now)

    requested = len(expected)
    received = sum(symbol in current for symbol in expected)
    usable = fresh_quotes(current)
    fresh = sum(symbol in usable for symbol in expected)
    coverage = round(100 * received / requested, 1) if requested else 0.0
    fresh_coverage = round(100 * fresh / requested, 1) if requested else 0.0

    if not market_open:
        status = "closed"
    elif received == 0:
        status = "unavailable"
    elif fresh == 0:
        status = "delayed"
    elif fresh_coverage < 75:
        status = "partial"
    else:
        status = "near_live"

    prior_good = (previous or {}).get("last_good_update")
    if not prior_good and (previous or {}).get("feed_status") in ("near_live", "partial"):
        prior_good = (previous or {}).get("updated")
    last_good = updated if fresh else prior_good
    sources = sorted({q.get("source") for q in current.values() if q.get("source")})

    return {
        "updated": updated,
        "market_open": market_open,
        "feed_status": status,
        "requested": requested,
        "received": received,
        "fresh_quotes": fresh,
        "coverage_pct": coverage,
        "fresh_coverage_pct": fresh_coverage,
        "last_good_update": last_good,
        "provider": ", ".join(sources) or "Yahoo Finance",
        "data_delay_note": DATA_DELAY_NOTE,
        "quotes": merged,
    }
