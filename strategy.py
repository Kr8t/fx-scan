"""Pure signal logic: no I/O, no pandas, no clock.

Both signals compare the **latest completed bar** against the bar before it.

* INSIDE DAY - high strictly below the previous high *and* low strictly above
  the previous low.
* CIB (closed in breakout) - close strictly beyond the previous high or low.
  A wick that pokes out but closes back inside is *not* a CIB.

``buffer`` is a price distance (use pips * pip size). It is a margin the bar
must clear: inside days must sit ``buffer`` inside the previous range, and
breakouts must close ``buffer`` outside it. The two zones can therefore never
overlap; a buffer only widens the "neither" gap between them.
"""

from __future__ import annotations

import math
from collections.abc import Sequence
from dataclasses import dataclass, replace
from datetime import date
from typing import Literal

Direction = Literal["bull", "bear", "neutral"]
Side = Literal["UP", "DOWN"]
Kind = Literal["INSIDE", "CIB"]
Level = Literal["HIGH", "LOW"]


@dataclass(frozen=True)
class Bar:
    """One completed daily candle."""

    day: date
    open: float
    high: float
    low: float
    close: float

    def is_valid(self) -> bool:
        values = (self.open, self.high, self.low, self.close)
        return all(math.isfinite(v) for v in values) and self.high >= self.low


@dataclass(frozen=True)
class Signal:
    """A detected signal on the latest bar of a pair."""

    pair: str
    kind: Kind
    bar: Bar
    prev: Bar
    direction: Direction
    side: Side | None = None  # only set for CIB

    @property
    def break_distance(self) -> float:
        """Price distance the close finished beyond the broken level (CIB)."""
        if self.side == "UP":
            return self.bar.close - self.prev.high
        if self.side == "DOWN":
            return self.prev.low - self.bar.close
        return 0.0

    @property
    def range_ratio(self) -> float:
        """Latest range as a fraction of the previous range."""
        prev_range = self.prev.high - self.prev.low
        if prev_range <= 0:
            return 0.0
        return (self.bar.high - self.bar.low) / prev_range


@dataclass(frozen=True)
class LevelBreak:
    """Today's price has traded beyond the previous day's high or low."""

    pair: str
    level: Level
    level_price: float
    extreme: float  # today's highest high (HIGH) or lowest low (LOW) so far
    confirmed: bool = True  # a candle CLOSED beyond the level (True when not checking closes)
    confirmed_close: float | None = None  # the furthest such close

    @property
    def distance(self) -> float:
        return abs(self.extreme - self.level_price)

    @property
    def close_distance(self) -> float:
        """How far the confirming close finished beyond the level (0 if unconfirmed)."""
        if self.confirmed_close is None:
            return 0.0
        return abs(self.confirmed_close - self.level_price)


def candle_direction(open_: float, close: float) -> Direction:
    """Bull if close > open, bear if close < open, otherwise neutral."""
    if close > open_:
        return "bull"
    if close < open_:
        return "bear"
    return "neutral"


def is_inside_day(
    high: float,
    low: float,
    prev_high: float,
    prev_low: float,
    buffer: float = 0.0,
) -> bool:
    """True when the bar is strictly inside the previous range (minus buffer)."""
    _check_buffer(buffer)
    return high < prev_high - buffer and low > prev_low + buffer


def breakout_side(
    close: float,
    prev_high: float,
    prev_low: float,
    buffer: float = 0.0,
) -> Side | None:
    """``UP``/``DOWN`` when the *close* is strictly beyond the previous range."""
    _check_buffer(buffer)
    if close > prev_high + buffer:
        return "UP"
    if close < prev_low - buffer:
        return "DOWN"
    return None


def resolve_signals(inside: bool, side: Side | None) -> tuple[bool, Side | None]:
    """Suppress the inside-day signal whenever a CIB fired on the latest bar."""
    if side is not None:
        return False, side
    return inside, None


def evaluate(pair: str, bars: Sequence[Bar], buffer: float = 0.0) -> Signal | None:
    """Classify the last bar of ``bars`` against the one before it.

    ``bars`` must be completed candles in ascending date order. Returns
    ``None`` when there is no signal or the data is unusable.
    """
    _check_buffer(buffer)
    if len(bars) < 2:
        return None
    prev, bar = bars[-2], bars[-1]
    if not (prev.is_valid() and bar.is_valid()):
        return None

    side = breakout_side(bar.close, prev.high, prev.low, buffer)
    inside = is_inside_day(bar.high, bar.low, prev.high, prev.low, buffer)
    inside, side = resolve_signals(inside, side)

    direction = candle_direction(bar.open, bar.close)
    if side is not None:
        return Signal(pair, "CIB", bar, prev, direction, side)
    if inside:
        return Signal(pair, "INSIDE", bar, prev, direction)
    return None


def _check_buffer(buffer: float) -> None:
    if buffer < 0 or not math.isfinite(buffer):
        msg = f"buffer must be a finite number >= 0, got {buffer!r}"
        raise ValueError(msg)


def detect_breaks(
    pair: str,
    prev_high: float,
    prev_low: float,
    session_high: float,
    session_low: float,
    *,
    buffer: float = 0.0,
) -> list[LevelBreak]:
    """Levels of the previous day that the current session has traded beyond.

    A touch is not a break: price must exceed the level by more than ``buffer``.
    Both sides are reported when today has taken out the high *and* the low.
    """
    _check_buffer(buffer)
    found: list[LevelBreak] = []
    if session_high > prev_high + buffer:
        found.append(LevelBreak(pair, "HIGH", prev_high, session_high))
    if session_low < prev_low - buffer:
        found.append(LevelBreak(pair, "LOW", prev_low, session_low))
    return found


def confirm_break(brk: LevelBreak, closes: Sequence[float], buffer: float = 0.0) -> LevelBreak:
    """Mark a touched level as confirmed if any completed candle closed beyond it.

    ``closes`` are the closes of today's completed candles on the chosen timeframe.
    Closing exactly on the level (or within ``buffer`` of it) does not count.
    """
    _check_buffer(buffer)
    if brk.level == "HIGH":
        beyond = [c for c in closes if c > brk.level_price + buffer]
        furthest = max(beyond) if beyond else None
    else:
        beyond = [c for c in closes if c < brk.level_price - buffer]
        furthest = min(beyond) if beyond else None
    return replace(brk, confirmed=furthest is not None, confirmed_close=furthest)
