"""Fetching, normalising and caching daily candles."""

from __future__ import annotations

import sqlite3
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import cast

import pandas as pd

import cache
import config
from strategy import Bar

Fetcher = Callable[[str], pd.DataFrame]
_OHLC = cache.COLUMNS


class DataError(RuntimeError):
    """Raised when no usable candles can be obtained for a pair."""


@dataclass(frozen=True)
class Candles:
    frame: pd.DataFrame
    from_cache: bool
    stale: bool = False


def _clean_columns(raw: pd.DataFrame) -> pd.DataFrame:
    """Flatten/lowercase columns and keep only OHLC (index untouched)."""
    frame = raw.copy()
    if isinstance(frame.columns, pd.MultiIndex):
        frame.columns = frame.columns.get_level_values(0)
    frame.columns = [str(c).lower() for c in frame.columns]
    missing = [c for c in _OHLC if c not in frame.columns]
    if missing:
        msg = f"missing columns: {', '.join(missing)}"
        raise DataError(msg)
    return cast("pd.DataFrame", frame[_OHLC])


def normalize_frame(raw: pd.DataFrame) -> pd.DataFrame:
    """Return a clean, ascending, tz-naive, midnight-indexed OHLC frame.

    Expects one row per session already labelled by date. Timezone-aware indexes
    keep their wall-clock date (no UTC shift).
    """
    if raw is None or raw.empty:
        return _empty()

    frame = _clean_columns(raw)
    index = pd.DatetimeIndex(frame.index)
    if index.tz is not None:
        index = index.tz_localize(None)
    frame.index = index.normalize()

    numeric = pd.DataFrame(frame.apply(pd.to_numeric, errors="coerce")).dropna()
    valid = pd.DataFrame(numeric[numeric["high"] >= numeric["low"]])
    unique = pd.DataFrame(valid[~valid.index.duplicated(keep="last")])
    return cast("pd.DataFrame", unique.sort_index())


def sessions_from_intraday(raw: pd.DataFrame) -> pd.DataFrame:
    """Aggregate intraday bars into daily candles cut at 17:00 New York.

    Timestamps are converted to ``config.SESSION_TZ`` (naive input is UTC), shifted
    so the cutoff lands on midnight, and grouped by the resulting date. Weekend
    labels (the stray bar at Friday's close) are dropped.
    """
    if raw is None or raw.empty:
        return _empty()

    frame = _clean_columns(raw)
    index = pd.DatetimeIndex(frame.index)
    if index.tz is None:
        index = index.tz_localize("UTC")
    order = index.argsort(kind="stable")
    frame = cast("pd.DataFrame", frame.iloc[order])
    index = index[order]

    local = index.tz_convert(config.SESSION_TZ).tz_localize(None)
    shift = pd.Timedelta(hours=24 - config.SESSION_CUTOFF_HOUR)
    frame.index = (local + shift).normalize()

    numeric = pd.DataFrame(frame.apply(pd.to_numeric, errors="coerce")).dropna()
    grouped = numeric.groupby(level=0).agg(
        open=("open", "first"),
        high=("high", "max"),
        low=("low", "min"),
        close=("close", "last"),
    )
    sessions = pd.DataFrame(grouped)
    weekdays = pd.DatetimeIndex(sessions.index).dayofweek < 5
    return normalize_frame(pd.DataFrame(sessions[weekdays]))


def drop_incomplete(frame: pd.DataFrame, now: datetime) -> pd.DataFrame:
    """Remove the session still in progress and anything dated later."""
    today = pd.Timestamp(cache.session_label(now))
    return cast("pd.DataFrame", frame[frame.index < today])


def to_bars(frame: pd.DataFrame) -> list[Bar]:
    """Convert a normalised frame to strategy ``Bar`` objects."""
    values: list[list[float]] = frame[_OHLC].to_numpy(dtype=float).tolist()
    return [
        Bar(pd.Timestamp(ts).date(), o, h, lo, c)
        for ts, (o, h, lo, c) in zip(frame.index, values, strict=True)
    ]


def yf_fetch(pair: str) -> pd.DataFrame:
    """Download hourly bars from Yahoo Finance and build 17:00-NY daily candles."""
    import yfinance as yf

    raw = yf.download(
        config.yahoo_symbol(pair),
        period=config.HISTORY_PERIOD,
        interval=config.INTRADAY_INTERVAL,
        auto_adjust=False,
        progress=False,
        threads=False,
    )
    return sessions_from_intraday(pd.DataFrame(raw))


def get_candles(
    conn: sqlite3.Connection,
    pair: str,
    now: datetime | None = None,
    fetcher: Fetcher | None = None,
    *,
    force: bool = False,
) -> Candles:
    """Return completed candles for ``pair``, using the cache when fresh."""
    moment = now or datetime.now(timezone.utc)
    if not force and cache.is_fresh(conn, pair, moment):
        return Candles(cache.load_candles(conn, pair), from_cache=True)

    try:
        frame = drop_incomplete(normalize_frame((fetcher or yf_fetch)(pair)), moment)
        if frame.empty:
            msg = "no completed candles returned"
            raise DataError(msg)
    except Exception as exc:  # network/parse failures all fall back the same way
        cached = cache.load_candles(conn, pair)
        if len(cached) >= config.MIN_BARS_FOR_SIGNAL:
            return Candles(cached, from_cache=True, stale=True)
        msg = f"{pair}: {exc}"
        raise DataError(msg) from exc

    cache.upsert_candles(conn, pair, frame, moment)
    return Candles(cache.load_candles(conn, pair), from_cache=False)


def _empty() -> pd.DataFrame:
    return cast(
        "pd.DataFrame", pd.DataFrame(columns=_OHLC, index=pd.DatetimeIndex([]), dtype=float)
    )
