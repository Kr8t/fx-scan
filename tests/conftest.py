from __future__ import annotations

from collections.abc import Iterator
from datetime import date, datetime, timedelta, timezone

import pandas as pd
import pytest

import cache
from strategy import Bar

UTC = timezone.utc
# Wednesday 2026-03-04 12:00 UTC
WED = datetime(2026, 3, 4, 12, 0, tzinfo=UTC)


def bar(
    high: float,
    low: float,
    open_: float | None = None,
    close: float | None = None,
    day: date = date(2026, 3, 3),
) -> Bar:
    mid = (high + low) / 2
    return Bar(day, mid if open_ is None else open_, high, low, mid if close is None else close)


def make_frame(days: list[date], base: float = 1.1) -> pd.DataFrame:
    rows = [
        {"open": base, "high": base + 0.01, "low": base - 0.01, "close": base + 0.002} for _ in days
    ]
    return pd.DataFrame(rows, index=pd.DatetimeIndex([pd.Timestamp(d) for d in days]))


def weekdays_ending(last: date, count: int) -> list[date]:
    out: list[date] = []
    d = last
    while len(out) < count:
        if d.weekday() < 5:
            out.append(d)
        d -= timedelta(days=1)
    return sorted(out)


@pytest.fixture
def conn() -> Iterator[object]:
    c = cache.connect(":memory:")
    yield c
    c.close()
