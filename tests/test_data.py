from __future__ import annotations

from datetime import date, datetime, timezone

import pandas as pd
import pytest

import data
from tests.conftest import WED, make_frame


def _raw(index: pd.DatetimeIndex, **over: list[float]) -> pd.DataFrame:
    n = len(index)
    cols: dict[str, list[float]] = {
        "Open": [1.0] * n,
        "High": [1.1] * n,
        "Low": [0.9] * n,
        "Close": [1.05] * n,
    }
    cols.update(over)
    return pd.DataFrame(cols, index=index)


class TestNormalize:
    def test_london_tz_keeps_local_date(self) -> None:
        idx = pd.DatetimeIndex(["2026-07-01", "2026-07-02"]).tz_localize("Europe/London")
        out = data.normalize_frame(_raw(idx))
        assert out.index.tz is None
        assert list(out.index) == [pd.Timestamp("2026-07-01"), pd.Timestamp("2026-07-02")]

    def test_intraday_timestamps_snap_to_midnight(self) -> None:
        idx = pd.DatetimeIndex(["2026-03-02 22:00", "2026-03-03 22:00"])
        out = data.normalize_frame(_raw(idx))
        assert all(ts.hour == 0 for ts in out.index)

    def test_multiindex_columns_flattened(self) -> None:
        raw = _raw(pd.DatetimeIndex(["2026-03-02"]))
        raw.columns = pd.MultiIndex.from_product([raw.columns, ["EURUSD=X"]])
        out = data.normalize_frame(raw)
        assert list(out.columns) == ["open", "high", "low", "close"]

    def test_drops_nan_rows(self) -> None:
        idx = pd.DatetimeIndex(["2026-03-02", "2026-03-03"])
        out = data.normalize_frame(_raw(idx, Close=[1.0, float("nan")]))
        assert len(out) == 1

    def test_drops_inverted_high_low(self) -> None:
        idx = pd.DatetimeIndex(["2026-03-02", "2026-03-03"])
        out = data.normalize_frame(_raw(idx, High=[0.5, 1.1]))
        assert len(out) == 1

    def test_dedupes_keeping_last(self) -> None:
        idx = pd.DatetimeIndex(["2026-03-02", "2026-03-02"])
        out = data.normalize_frame(_raw(idx, Close=[1.01, 1.02]))
        assert len(out) == 1
        assert out["close"].iloc[0] == pytest.approx(1.02)

    def test_missing_columns_raise(self) -> None:
        raw = pd.DataFrame({"Open": [1.0]}, index=pd.DatetimeIndex(["2026-03-02"]))
        with pytest.raises(data.DataError, match="missing"):
            data.normalize_frame(raw)


class TestDropIncomplete:
    def test_drops_today_and_future(self) -> None:
        frame = make_frame([date(2026, 3, 3), date(2026, 3, 4), date(2026, 3, 5)])
        out = data.drop_incomplete(frame, WED)
        assert list(out.index) == [pd.Timestamp("2026-03-03")]

    def test_non_utc_now_converted(self) -> None:
        # 23:30 on 3 Mar in New York is already 4 Mar in UTC.
        from datetime import timedelta

        ny = timezone(timedelta(hours=-5))
        frame = make_frame([date(2026, 3, 3), date(2026, 3, 4)])
        out = data.drop_incomplete(frame, datetime(2026, 3, 3, 23, 30, tzinfo=ny))
        assert list(out.index) == [pd.Timestamp("2026-03-03")]
