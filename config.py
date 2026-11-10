"""Static configuration for FX SCAN."""

from __future__ import annotations

import os
from datetime import timedelta
from pathlib import Path
from typing import Final

APP_NAME: Final = "FX SCAN"
VERSION: Final = "1.0.0"

# Watch list: the 7 majors, the Yen crosses and the European crosses (16 pairs).
# Edit the groups below - every other module reads PAIRS.
MAJORS: Final[tuple[str, ...]] = (
    "EURUSD",
    "GBPUSD",
    "USDJPY",
    "USDCHF",
    "AUDUSD",
    "USDCAD",
    "NZDUSD",
)
YEN_CROSSES: Final[tuple[str, ...]] = (
    "EURJPY",
    "GBPJPY",
    "AUDJPY",
    "NZDJPY",
    "CADJPY",
    "CHFJPY",
)
# Crosses between the European currencies only (EUR, GBP, CHF).
# EURJPY/GBPJPY/CHFJPY are Yen crosses and are listed once, under YEN_CROSSES.
EUROPEAN_CROSSES: Final[tuple[str, ...]] = (
    "EURGBP",
    "EURCHF",
    "GBPCHF",
)
PAIRS: Final[tuple[str, ...]] = MAJORS + YEN_CROSSES + EUROPEAN_CROSSES

# Output sections: the majors are shown apart from the Yen/European crosses.
CROSSES: Final[tuple[str, ...]] = YEN_CROSSES + EUROPEAN_CROSSES
GROUPS: Final[tuple[tuple[str, tuple[str, ...]], ...]] = (
    ("MAJORS", MAJORS),
    ("YEN & EUROPEAN CROSSES", CROSSES),
)
GROUP_KEYS: Final[dict[str, tuple[str, ...]]] = {"majors": MAJORS, "crosses": CROSSES}

# --- Data ---------------------------------------------------------------
HISTORY_PERIOD: Final = "3mo"  # how much history yfinance is asked for
INTRADAY_INTERVAL: Final = "1h"  # daily candles are built from these bars

# A trading "day" runs from SESSION_CUTOFF_HOUR to SESSION_CUTOFF_HOUR in SESSION_TZ.
# 17:00 New York is the FX daily close used by TradingView/OANDA and most brokers.
SESSION_TZ: Final = "America/New_York"
SESSION_CUTOFF_HOUR: Final = 17

# Bump when the meaning of cached candles changes; old caches are wiped on connect.
SCHEMA_VERSION: Final = 2
MIN_BARS: Final = 5  # fewer cached bars than this => cache is unusable
MIN_BARS_FOR_SIGNAL: Final = 2  # latest bar + previous bar

# --- MetaTrader 5 source -------------------------------------------------
MT5_DAILY_BARS: Final = 60  # D1 bars requested per pair
MT5_MIN_INTERVAL: Final = 5  # watch polling floor (seconds); local terminal, no rate limit
YAHOO_MIN_INTERVAL: Final = 15

# --- Close confirmation ---------------------------------------------------
# --confirm choices: alert only when a candle of this size CLOSES beyond the level.
CONFIRM_FRAMES: Final[dict[str, int]] = {"5m": 5, "15m": 15, "30m": 30, "1h": 60, "4h": 240}


def confirm_label(minutes: int) -> str:
    """``15`` -> ``"15m"``, ``60`` -> ``"1h"``."""
    return next(label for label, value in CONFIRM_FRAMES.items() if value == minutes)


# --- Cache freshness ----------------------------------------------------
# If we already hit the network this recently and still lack the newest
# completed bar (bank holiday, yfinance lag), do not hammer the API again.
RETRY_COOLDOWN: Final = timedelta(minutes=30)
DEFAULT_DB_PATH: Final = Path.home() / ".fx_scan" / "cache.sqlite"


def db_path(source: str = "yahoo") -> Path:
    """Cache location (override with FX_SCAN_DB). Each data source gets its own file so
    Yahoo and MT5 candles are never mixed in one cache."""
    override = os.environ.get("FX_SCAN_DB")
    base = Path(override) if override else DEFAULT_DB_PATH
    if source == "yahoo":
        return base
    return base.with_name(f"{base.stem}_{source}{base.suffix}")


def yahoo_symbol(pair: str) -> str:
    """Map ``EURUSD`` to yfinance's ``EURUSD=X``."""
    return f"{pair.upper()}=X"


def pip_size(pair: str) -> float:
    """Price value of one pip (0.01 for JPY crosses, 0.0001 otherwise)."""
    return 0.01 if pair.upper().endswith("JPY") else 0.0001


def price_decimals(pair: str) -> int:
    """Display precision (pipette) for a pair."""
    return 3 if pair.upper().endswith("JPY") else 5


def display_pair(pair: str) -> str:
    """``EURUSD`` -> ``EUR/USD``."""
    p = pair.upper()
    return f"{p[:3]}/{p[3:]}"
