from __future__ import annotations

import sqlite3
from datetime import date, datetime, timedelta

import pandas as pd
import pytest

import cache
import data
from tests.conftest import UTC, WED, make_frame, weekdays_ending

PAIR = "EURUSD"


def _seed(conn: sqlite3.Connection, last: date, fetched: datetime, count: int = 10) -> None:
    cache.upsert_candles(conn, PAIR, make_frame(weekdays_ending(last, count)), fetched)


class TestExpectedLastComplete:
    @pytest.mark.parametrize(
        ("now", "expected"),
        [
            (datetime(2026, 3, 3, 9, tzinfo=UTC), date(2026, 3, 2)),  # Tue -> Mon
            (datetime(2026, 3, 2, 9, tzinfo=UTC), date(2026, 2, 27)),  # Mon -> Fri
            (datetime(2026, 2, 28, 9, tzinfo=UTC), date(2026, 2, 27)),  # Sat -> Fri
        ],
    )
    def test_rolls_over_weekends(self, now: datetime, expected: date) -> None:
        assert cache.expected_last_complete(now) == expected


class TestFreshness:
    def test_empty_cache_is_stale(self, conn: sqlite3.Connection) -> None:
        assert not cache.is_fresh(conn, PAIR, WED)

    def test_up_to_date_is_fresh(self, conn: sqlite3.Connection) -> None:
        _seed(conn, date(2026, 3, 3), WED - timedelta(days=3))
        assert cache.is_fresh(conn, PAIR, WED)

    def test_behind_by_a_day_is_stale(self, conn: sqlite3.Connection) -> None:
        _seed(conn, date(2026, 3, 2), WED - timedelta(hours=5))
        assert not cache.is_fresh(conn, PAIR, WED)

    def test_cooldown_prevents_refetch_loop(self, conn: sqlite3.Connection) -> None:
        _seed(conn, date(2026, 3, 2), WED - timedelta(minutes=10))
        assert cache.is_fresh(conn, PAIR, WED)

    def test_cooldown_expires(self, conn: sqlite3.Connection) -> None:
        _seed(conn, date(2026, 3, 2), WED - timedelta(minutes=45))
        assert not cache.is_fresh(conn, PAIR, WED)

    @pytest.mark.parametrize("day", [28, 1])  # Sat 28 Feb, Sun 1 Mar
    def test_weekend_with_friday_cached_is_fresh(self, conn: sqlite3.Connection, day: int) -> None:
        month = 2 if day == 28 else 3
        _seed(conn, date(2026, 2, 27), datetime(2026, 2, 28, 1, tzinfo=UTC))
        now = datetime(2026, month, day, 20, tzinfo=UTC)
        assert cache.is_fresh(conn, PAIR, now)

    def test_saturday_with_only_thursday_is_stale(self, conn: sqlite3.Connection) -> None:
        _seed(conn, date(2026, 2, 26), datetime(2026, 2, 27, tzinfo=UTC))
        assert not cache.is_fresh(conn, PAIR, datetime(2026, 2, 28, 12, tzinfo=UTC))


class TestStorage:
    def test_roundtrip(self, conn: sqlite3.Connection) -> None:
        frame = make_frame(weekdays_ending(date(2026, 3, 3), 6))
        cache.upsert_candles(conn, PAIR, frame, WED)
        out = cache.load_candles(conn, PAIR)
        assert len(out) == 6
        assert out["high"].iloc[0] == pytest.approx(frame["high"].iloc[0])
        assert cache.latest_cached_date(conn, PAIR) == date(2026, 3, 3)

    def test_upsert_is_idempotent_and_updates(self, conn: sqlite3.Connection) -> None:
        frame = make_frame([date(2026, 3, 3)])
        cache.upsert_candles(conn, PAIR, frame, WED)
        frame["close"] = 1.5
        cache.upsert_candles(conn, PAIR, frame, WED)
        out = cache.load_candles(conn, PAIR)
        assert len(out) == 1
        assert out["close"].iloc[0] == pytest.approx(1.5)


class TestGetCandles:
    @staticmethod
    def _fetch_ok(pair: str) -> pd.DataFrame:
        frame = make_frame(weekdays_ending(date(2026, 3, 3), 10))
        frame.columns = [c.title() for c in frame.columns]
        return frame

    def test_fetches_and_caches_when_empty(self, conn: sqlite3.Connection) -> None:
        out = data.get_candles(conn, PAIR, WED, self._fetch_ok)
        assert not out.from_cache
        assert len(out.frame) == 10

    def test_uses_cache_when_fresh(self, conn: sqlite3.Connection) -> None:
        _seed(conn, date(2026, 3, 3), WED - timedelta(days=1))
        calls: list[str] = []

        def fetcher(pair: str) -> pd.DataFrame:
            calls.append(pair)
            return pd.DataFrame()

        out = data.get_candles(conn, PAIR, WED, fetcher)
        assert out.from_cache
        assert not calls

    def test_weekend_does_not_refetch(self, conn: sqlite3.Connection) -> None:
        _seed(conn, date(2026, 2, 27), datetime(2026, 2, 28, tzinfo=UTC))

        def boom(pair: str) -> pd.DataFrame:
            raise AssertionError("must not hit network on weekend")

        out = data.get_candles(conn, PAIR, datetime(2026, 3, 1, 15, tzinfo=UTC), boom)
        assert out.from_cache

    def test_partial_bar_never_cached(self, conn: sqlite3.Connection) -> None:
        def fetcher(pair: str) -> pd.DataFrame:
            frame = make_frame(weekdays_ending(date(2026, 3, 4), 10))  # includes "today"
            frame.columns = [c.title() for c in frame.columns]
            return frame

        data.get_candles(conn, PAIR, WED, fetcher)
        assert cache.latest_cached_date(conn, PAIR) == date(2026, 3, 3)

    def test_falls_back_to_stale_cache_on_error(self, conn: sqlite3.Connection) -> None:
        _seed(conn, date(2026, 3, 1 + 0 if False else 2), WED - timedelta(hours=6))

        def boom(pair: str) -> pd.DataFrame:
            raise ConnectionError("offline")

        out = data.get_candles(conn, PAIR, WED, boom)
        assert out.stale
        assert out.from_cache

    def test_raises_without_cache_on_error(self, conn: sqlite3.Connection) -> None:
        def boom(pair: str) -> pd.DataFrame:
            raise ConnectionError("offline")

        with pytest.raises(data.DataError, match="offline"):
            data.get_candles(conn, PAIR, WED, boom)
