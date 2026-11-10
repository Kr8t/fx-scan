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
# (pair, candle minutes, now) -> closes of today's completed candles; None = no live session
ClosesFetcher = Callable[[str, int, datetime], list[float] | None]
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


def _session_labels(index: pd.DatetimeIndex) -> pd.DatetimeIndex:
    """Session label (date the 17:00-NY-to-17:00-NY session closes on) per timestamp."""
    if index.tz is None:
        index = index.tz_localize("UTC")
    local = index.tz_convert(config.SESSION_TZ).tz_localize(None)
    shift = pd.Timedelta(hours=24 - config.SESSION_CUTOFF_HOUR)
    return cast("pd.DatetimeIndex", (local + shift).normalize())


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

    frame.index = _session_labels(index)

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


@dataclass(frozen=True)
class SessionRange:
    """High/low/last of the session in progress."""

    high: float
    low: float
    last: float


def current_session_range(raw: pd.DataFrame, now: datetime) -> SessionRange | None:
    """Range traded so far in the session containing ``now`` (None if no bars)."""
    if raw is None or raw.empty:
        return None
    numeric = pd.DataFrame(_clean_columns(raw).apply(pd.to_numeric, errors="coerce")).dropna()
    if numeric.empty:
        return None
    numeric = pd.DataFrame(numeric.sort_index())
    labels = _session_labels(pd.DatetimeIndex(numeric.index))
    mask = labels == pd.Timestamp(cache.session_label(now))
    if not mask.any():
        return None
    current = pd.DataFrame(numeric[mask])
    return SessionRange(
        high=float(current["high"].max()),
        low=float(current["low"].min()),
        last=float(current["close"].iloc[-1]),
    )


def closes_from_intraday(raw: pd.DataFrame, now: datetime, minutes: int) -> list[float] | None:
    """Closes of today's completed ``minutes``-minute candles, built from 5-minute bars.

    Candles are anchored at the session open (17:00 New York) so 1h/4h candles line up
    with a New-York-close broker's. A candle counts only once its end time has passed.
    """
    if raw is None or raw.empty:
        return None
    numeric = pd.DataFrame(_clean_columns(raw).apply(pd.to_numeric, errors="coerce")).dropna()
    if numeric.empty:
        return None
    numeric = pd.DataFrame(numeric.sort_index())
    index = pd.DatetimeIndex(numeric.index)
    if index.tz is None:
        index = index.tz_localize("UTC")
    today = pd.Timestamp(cache.session_label(now))
    mask = _session_labels(index) == today
    if not mask.any():
        return None
    current = pd.DataFrame(numeric[mask])
    current.index = index[mask].tz_convert("UTC")

    opened = (today - pd.Timedelta(days=1)).date()
    origin = pd.Timestamp(f"{opened} {config.SESSION_CUTOFF_HOUR:02d}:00", tz=config.SESSION_TZ)
    bins = current["close"].resample(f"{minutes}min", origin=origin.tz_convert("UTC")).last()
    now_utc = pd.Timestamp(cache.as_utc(now))
    ends = bins.index + pd.Timedelta(minutes=minutes)
    closed = bins[(ends <= now_utc) & bins.notna()]
    return [float(value) for value in closed]


def closes_via(live_fetcher: Fetcher) -> ClosesFetcher:
    """Closes fetcher that derives candles from the same 5-minute feed (Yahoo path)."""

    def fetch(pair: str, minutes: int, now: datetime) -> list[float] | None:
        return closes_from_intraday(live_fetcher(pair), now, minutes)

    return fetch


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


def yf_intraday(pair: str) -> pd.DataFrame:
    """Recent 5-minute bars (raw) used by watch mode to track today's range."""
    import yfinance as yf

    raw = yf.download(
        config.yahoo_symbol(pair),
        period="2d",
        interval="5m",
        auto_adjust=False,
        progress=False,
        threads=False,
    )
    return cast("pd.DataFrame", pd.DataFrame(raw))


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
