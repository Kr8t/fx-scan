"""MetaTrader 5 data source (Windows, needs the MT5 terminal installed).

Daily candles are the broker's own D1 bars, so they match your MT5 charts exactly.
Today's forming D1 bar is the live range used for the Broke column and watch mode, which
is real time (no Yahoo delay).

Assumption: the broker's D1 bars follow the usual FX day, opening at 17:00 New York
(server midnight on a GMT+2/+3 server). Check one pair against your chart after switching.
"""

from __future__ import annotations

import importlib
from collections.abc import Iterable
from datetime import datetime
from typing import Any, cast

import pandas as pd

import cache
import config

_OHLC = ["open", "high", "low", "close"]
_TIMEFRAMES = {
    5: "TIMEFRAME_M5",
    15: "TIMEFRAME_M15",
    30: "TIMEFRAME_M30",
    60: "TIMEFRAME_H1",
    240: "TIMEFRAME_H4",
}
_MAX_SUFFIX = 6  # longest broker suffix we will auto-accept (".pro", "m", ".ecn", "_i" ...)


class Mt5Error(RuntimeError):
    """MT5 is unavailable or a symbol/data request failed."""


def resolve_symbol(pair: str, names: Iterable[str], suffix: str = "") -> str | None:
    """Find the broker's name for ``pair`` (``EURUSD`` -> ``EURUSD.a``, ``EURUSDm``...).

    With an explicit ``suffix`` only ``pair + suffix`` is accepted. Otherwise an exact
    name wins, then the shortest name that starts with the pair and adds a short suffix.
    """
    wanted = pair.upper()
    listed = list(names)
    if suffix:
        target = (wanted + suffix).upper()
        return next((n for n in listed if n.upper() == target), None)
    exact = next((n for n in listed if n.upper() == wanted), None)
    if exact:
        return exact
    candidates = [
        n for n in listed if n.upper().startswith(wanted) and len(n) <= len(wanted) + _MAX_SUFFIX
    ]
    return min(candidates, key=lambda n: (len(n), n)) if candidates else None


class Mt5Source:
    """Daily and live-range feeds backed by an initialised MT5 API module."""

    def __init__(self, api: Any, suffix: str = "") -> None:
        self._api = api
        self._suffix = suffix
        self._names: list[str] | None = None
        self._resolved: dict[str, str] = {}

    def close(self) -> None:
        self._api.shutdown()

    def symbol(self, pair: str) -> str:
        """Broker symbol for ``pair`` (cached, and added to Market Watch)."""
        if pair in self._resolved:
            return self._resolved[pair]
        if self._names is None:
            self._names = [info.name for info in (self._api.symbols_get() or ())]
        symbol = resolve_symbol(pair, self._names, self._suffix)
        if symbol is None:
            msg = f"{pair}: not found in this broker's symbol list (try --mt5-suffix)"
            raise Mt5Error(msg)
        self._api.symbol_select(symbol, True)
        self._resolved[pair] = symbol
        return symbol

    def _rates(self, pair: str, count: int) -> pd.DataFrame:
        symbol = self.symbol(pair)
        rates = self._api.copy_rates_from_pos(symbol, self._api.TIMEFRAME_D1, 0, count)
        if rates is None or len(rates) == 0:
            msg = f"{symbol}: no D1 data from MT5 ({self._api.last_error()})"
            raise Mt5Error(msg)
        frame = pd.DataFrame(rates)
        # MT5 times are server time; read them as naive and keep the bar's own date.
        stamps = pd.DatetimeIndex(pd.to_datetime(frame["time"], unit="s"))
        out = pd.DataFrame(frame[_OHLC])
        out.index = stamps.normalize()
        return cast("pd.DataFrame", out)

    def daily(self, pair: str) -> pd.DataFrame:
        """Broker D1 candles indexed by session date (weekend-dated bars dropped)."""
        frame = self._rates(pair, config.MT5_DAILY_BARS)
        weekdays = pd.DatetimeIndex(frame.index).dayofweek < 5
        return cast("pd.DataFrame", frame[weekdays])

    def live(self, pair: str) -> pd.DataFrame:
        """Today's forming D1 bar as a one-row frame that ``data.current_session_range`` reads.

        The row is stamped at 12:00 New York on the bar's date, which always falls inside
        that session, so it only matches today's session while the market is open.
        """
        frame = self._rates(pair, 1)
        day = pd.Timestamp(frame.index[0]).date()
        stamp = pd.Timestamp(f"{day} 12:00", tz=config.SESSION_TZ).tz_convert("UTC")
        frame.index = pd.DatetimeIndex([stamp])
        return frame

    def closes(self, pair: str, minutes: int, now: datetime) -> list[float] | None:
        """Closes of today's completed ``minutes`` candles (None if no live session).

        Works in broker server time throughout: candles since today's D1 open, counted as
        complete once their end is at or before the terminal's last tick time.
        """
        name = _TIMEFRAMES.get(minutes)
        if name is None:
            msg = f"{minutes}-minute candles are not supported"
            raise Mt5Error(msg)
        symbol = self.symbol(pair)
        day = self._api.copy_rates_from_pos(symbol, self._api.TIMEFRAME_D1, 0, 1)
        if day is None or len(day) == 0:
            msg = f"{symbol}: no D1 data from MT5 ({self._api.last_error()})"
            raise Mt5Error(msg)
        opened = int(day[0]["time"])
        if pd.Timestamp(opened, unit="s").date() != cache.session_label(now):
            return None  # market closed: the newest D1 bar is not today's session
        per_day = 24 * 60 // minutes + 2
        rates = self._api.copy_rates_from_pos(symbol, getattr(self._api, name), 0, per_day)
        if rates is None or len(rates) == 0:
            return None
        tick = self._api.symbol_info_tick(symbol)
        bars = [r for r in rates if int(r["time"]) >= opened]
        if tick is not None:
            done = [r for r in bars if int(r["time"]) + minutes * 60 <= int(tick.time)]
        else:
            done = bars[:-1]  # no tick time: assume the newest bar is still forming
        return [float(r["close"]) for r in done]


def connect(suffix: str = "", path: str | None = None, api: Any = None) -> Mt5Source:
    """Initialise the MT5 terminal connection or raise ``Mt5Error``."""
    if api is None:
        try:
            api = importlib.import_module("MetaTrader5")
        except ImportError as exc:
            msg = "MetaTrader5 package not installed (Windows only: pip install MetaTrader5)"
            raise Mt5Error(msg) from exc
    kwargs = {"path": path} if path else {}
    if not api.initialize(**kwargs):
        msg = f"could not connect to the MT5 terminal ({api.last_error()}); is it installed?"
        raise Mt5Error(msg)
    return Mt5Source(api, suffix)
