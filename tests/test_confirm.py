"""Alert only when a candle CLOSES beyond the previous day's high/low (--confirm)."""

from __future__ import annotations

from datetime import date, datetime, timezone
from pathlib import Path

import numpy as np
import pandas as pd
import pytest
from rich.console import Console

import cli
import config
import data
import mt5feed
import strategy
from strategy import Bar, LevelBreak, Signal
from tests.conftest import WED
from tests.test_cli import _fetcher_factory
from tests.test_mt5 import DTYPE, FakeMt5, _history, _rates
from tests.test_sessions import _hourly


def _signal() -> Signal:
    prev = Bar(date(2026, 3, 2), 1.0, 1.10, 1.00, 1.05)
    bar = Bar(date(2026, 3, 3), 1.04, 1.09, 1.01, 1.06)
    return Signal("EURUSD", "INSIDE", bar, prev, "bull")


def _five_minute(*rows: tuple[str, float, float, float, float]) -> pd.DataFrame:
    return _hourly(list(rows))


class TestConfirmBreak:
    def test_close_beyond_high_confirms_with_furthest_close(self) -> None:
        brk = LevelBreak("EURUSD", "HIGH", 1.09, 1.10)
        out = strategy.confirm_break(brk, [1.085, 1.092, 1.095])
        assert out.confirmed
        assert out.confirmed_close == 1.095
        assert out.close_distance == pytest.approx(0.005)

    def test_wick_only_stays_unconfirmed(self) -> None:
        brk = LevelBreak("EURUSD", "HIGH", 1.09, 1.10)
        out = strategy.confirm_break(brk, [1.085, 1.09])  # a close exactly on the level
        assert not out.confirmed
        assert out.confirmed_close is None
        assert out.close_distance == 0.0

    def test_low_side_uses_lowest_close(self) -> None:
        brk = LevelBreak("EURUSD", "LOW", 1.01, 1.00)
        assert strategy.confirm_break(brk, [1.02, 1.005, 1.003]).confirmed_close == 1.003

    def test_buffer_applies_to_closes(self) -> None:
        brk = LevelBreak("EURUSD", "HIGH", 1.09, 1.10)
        assert not strategy.confirm_break(brk, [1.0905], buffer=0.001).confirmed
        assert strategy.confirm_break(brk, [1.0915], buffer=0.001).confirmed

    def test_no_closes_means_unconfirmed(self) -> None:
        assert not strategy.confirm_break(LevelBreak("EURUSD", "LOW", 1.01, 1.0), []).confirmed


class TestClosesFromIntraday:
    # Session opened Tue 3 Mar 22:00 UTC (17:00 EST); now is Wed 4 Mar 12:00 UTC.
    def test_only_completed_candles_count(self) -> None:
        raw = _five_minute(
            ("2026-03-03 22:00", 1.0, 1.0, 1.0, 1.01),
            ("2026-03-03 22:55", 1.0, 1.0, 1.0, 1.02),  # last bar of the 22:00 hour
            ("2026-03-04 11:50", 1.0, 1.0, 1.0, 1.03),
            ("2026-03-04 11:55", 1.0, 1.0, 1.0, 1.04),  # last bar of the 11:00 hour
        )
        assert data.closes_from_intraday(raw, WED, 60) == [1.02, 1.04]  # 11:00 hour ended at 12:00
        early = datetime(2026, 3, 4, 11, 58, tzinfo=timezone.utc)
        assert data.closes_from_intraday(raw, early, 60) == [1.02]  # 11:00 hour still forming

    def test_fifteen_minute_candles(self) -> None:
        raw = _five_minute(
            ("2026-03-04 10:00", 1.0, 1.0, 1.0, 1.01),
            ("2026-03-04 10:05", 1.0, 1.0, 1.0, 1.02),
            ("2026-03-04 10:10", 1.0, 1.0, 1.0, 1.03),
            ("2026-03-04 10:15", 1.0, 1.0, 1.0, 1.04),
        )
        assert data.closes_from_intraday(raw, WED, 15) == [1.03, 1.04]

    def test_four_hour_candles_anchor_at_session_open(self) -> None:
        # 22:00 -> 02:00 is one 4h candle when anchored to the 17:00 NY open.
        raw = _five_minute(
            ("2026-03-03 22:00", 1.0, 1.0, 1.0, 1.01),
            ("2026-03-04 01:55", 1.0, 1.0, 1.0, 1.05),
        )
        assert data.closes_from_intraday(raw, WED, 240) == [1.05]

    def test_previous_session_ignored(self) -> None:
        raw = _five_minute(("2026-03-03 15:00", 1.0, 1.0, 1.0, 9.99))
        assert data.closes_from_intraday(raw, WED, 15) is None

    def test_empty_frame(self) -> None:
        assert data.closes_from_intraday(pd.DataFrame(), WED, 15) is None


class TestCurrentBreaksConfirm:
    @staticmethod
    def _feed(close: float):  # type: ignore[no-untyped-def]
        # One 5-minute bar at 10:00 that wicks to 1.095 (> the 1.09 high) and closes at `close`.
        return lambda pair: _five_minute(("2026-03-04 10:00", 1.05, 1.095, 1.04, close))

    def test_wick_is_unconfirmed(self) -> None:
        out = cli.current_breaks(_signal(), WED, self._feed(1.085), 0.0, confirm=60)
        assert out is not None
        assert [(b.level, b.confirmed) for b in out] == [("HIGH", False)]

    def test_close_beyond_confirms(self) -> None:
        out = cli.current_breaks(_signal(), WED, self._feed(1.093), 0.0, confirm=60)
        assert out is not None
        assert out[0].confirmed
        assert out[0].confirmed_close == 1.093

    def test_without_confirm_a_touch_counts(self) -> None:
        out = cli.current_breaks(_signal(), WED, self._feed(1.085), 0.0)
        assert out is not None
        assert out[0].confirmed

    def test_closes_feed_failure_leaves_touch_unconfirmed(self) -> None:
        def boom(pair: str, minutes: int, now: datetime) -> list[float] | None:
            raise ConnectionError("offline")

        out = cli.current_breaks(
            _signal(), WED, self._feed(1.093), 0.0, confirm=60, closes_fetcher=boom
        )
        assert out is not None
        assert not out[0].confirmed

    def test_custom_closes_fetcher_is_used(self) -> None:
        out = cli.current_breaks(
            _signal(),
            WED,
            self._feed(1.085),
            0.0,
            confirm=15,
            closes_fetcher=lambda pair, minutes, now: [1.092],
        )
        assert out is not None
        assert out[0].confirmed


class TestWatchOnceConfirm:
    @staticmethod
    def _feed(close: float):  # type: ignore[no-untyped-def]
        return lambda pair: _five_minute(("2026-03-04 10:00", 1.05, 1.095, 1.04, close))

    def test_wick_does_not_alert_then_close_does_once(self) -> None:
        alerted: set[tuple[str, str]] = set()
        wick = cli.watch_once([_signal()], alerted, WED, self._feed(1.085), 0.0, confirm=60)
        closed = cli.watch_once([_signal()], alerted, WED, self._feed(1.093), 0.0, confirm=60)
        again = cli.watch_once([_signal()], alerted, WED, self._feed(1.094), 0.0, confirm=60)
        assert wick == []
        assert [b.level for _, b in closed] == ["HIGH"]
        assert again == []


class TestDisplay:
    def test_break_cell_wick_and_close_states(self) -> None:
        closed = LevelBreak("EURUSD", "HIGH", 1.09, 1.1, confirmed=True, confirmed_close=1.095)
        wick = LevelBreak("EURUSD", "LOW", 1.01, 1.0, confirmed=False)
        assert cli.break_cell([closed]).plain == "● HIGH"
        assert cli.break_cell([wick]).plain == "◐ LOW"
        assert cli.break_cell([closed, wick]).plain == "● HIGH ◐ LOW"
        both = LevelBreak("EURUSD", "LOW", 1.01, 1.0, confirmed=True, confirmed_close=1.005)
        assert cli.break_cell([closed, both]).plain == "● BOTH"

    def test_confirm_alert_text(self) -> None:
        brk = LevelBreak("EURUSD", "HIGH", 1.09, 1.10, confirmed=True, confirmed_close=1.0935)
        title, body = cli.format_alert(_signal(), brk, "15m")
        assert title == "EUR/USD 15m close beyond previous day high"
        assert "15m candle closed at 1.09350" in body
        assert "+" not in body.split("pips")[0]  # distance is of the close, unsigned
        assert "35.0 pips beyond 1.09000" in body
        assert title.isascii()
        assert body.isascii()

    def test_confirm_labels(self) -> None:
        assert config.confirm_label(15) == "15m"
        assert config.confirm_label(60) == "1h"
        assert config.confirm_label(240) == "4h"


class TestMainConfirm:
    @staticmethod
    def _run(tmp_path: Path, live, *extra: str, phase=None, cycles: int = 3):  # type: ignore[no-untyped-def]
        console = Console(record=True, width=130, force_terminal=False)
        beeps: list[int] = []
        cli.main(
            ["EURUSD", "--no-toast", "--db", str(tmp_path / "c.sqlite"), *extra],
            fetcher=_fetcher_factory({"EURUSD": (1.04, 1.09, 1.01, 1.06)}),
            live_fetcher=live,
            sleep=lambda _s: phase.update(v=2) if phase is not None else None,
            max_cycles=cycles,
            console=console,
            now=WED,
            beeper=lambda: beeps.append(1),
        )
        return console.export_text(), beeps

    def test_scan_shows_wick_dot(self, tmp_path: Path) -> None:
        live = lambda pair: _five_minute(("2026-03-04 10:00", 1.05, 1.095, 1.04, 1.085))  # noqa: E731
        text, _ = self._run(tmp_path, live, "--confirm", "1h", cycles=1)
        eur = next(line for line in text.splitlines() if "EUR/USD" in line)
        assert "◐ HIGH" in eur
        assert "alerts on 1h closes" in text
        assert "wick only" in text  # legend

    def test_scan_shows_closed_dot(self, tmp_path: Path) -> None:
        live = lambda pair: _five_minute(("2026-03-04 10:00", 1.05, 1.095, 1.04, 1.093))  # noqa: E731
        text, _ = self._run(tmp_path, live, "--confirm", "1h", cycles=1)
        eur = next(line for line in text.splitlines() if "EUR/USD" in line)
        assert "● HIGH" in eur

    def test_watch_silent_on_wick_then_alerts_on_close(self, tmp_path: Path) -> None:
        phase = {"v": 1}

        def live(pair: str) -> pd.DataFrame:
            close = 1.085 if phase["v"] == 1 else 1.093  # wick first, then a closing candle
            return _five_minute(("2026-03-04 10:00", 1.05, 1.095, 1.04, close))

        text, beeps = self._run(tmp_path, live, "--watch", "--confirm", "1h", phase=phase)
        assert text.count("1h close beyond previous day high") == 1
        assert "high 1h close beyond (1.09300)" in text  # BREAKS TODAY entry
        assert beeps == [1]  # one beep, only once the close confirmed

    def test_watch_without_confirm_alerts_on_the_wick(self, tmp_path: Path) -> None:
        live = lambda pair: _five_minute(("2026-03-04 10:00", 1.05, 1.095, 1.04, 1.085))  # noqa: E731
        text, beeps = self._run(tmp_path, live, "--watch", cycles=2)
        assert "EUR/USD broke previous day high" in text
        assert beeps == [1]


class TestMt5Closes:
    OPEN = int(pd.Timestamp("2026-03-04").timestamp())  # Wed's D1 open, server time

    def _source(self, tick_offset: int = 10 * 3600) -> mt5feed.Mt5Source:
        api = FakeMt5(["EURUSD"], {"EURUSD": _rates(_history())})
        api.TIMEFRAME_M15 = 15
        api.TIMEFRAME_H1 = 60
        api.tf_rates = {  # type: ignore[attr-defined]
            ("EURUSD", 15): np.array(
                [(self.OPEN - 900, 9, 9, 9, 9.99)]  # last bar of the previous session
                + [(self.OPEN + 900 * k, 1, 1, 1, 1.0 + k / 1000) for k in range(41)],
                dtype=DTYPE,
            )
        }
        api.symbol_info_tick = lambda symbol: type("T", (), {"time": self.OPEN + tick_offset})()  # type: ignore[attr-defined]
        return mt5feed.Mt5Source(api)

    def test_returns_only_completed_candles_of_todays_session(self) -> None:
        closes = self._source().closes("EURUSD", 15, WED)
        # Tick at 10:00 server time: bars k=0..39 ended by then; k=40 (10:00-10:15) is forming.
        assert closes is not None
        assert len(closes) == 40
        assert closes[0] == pytest.approx(1.0)
        assert 9.99 not in closes
        assert closes[-1] == pytest.approx(1.039)

    def test_none_when_market_is_closed(self) -> None:
        saturday = datetime(2026, 3, 7, 15, tzinfo=timezone.utc)
        assert self._source().closes("EURUSD", 15, saturday) is None

    def test_unsupported_timeframe_raises(self) -> None:
        with pytest.raises(mt5feed.Mt5Error, match="not supported"):
            self._source().closes("EURUSD", 7, WED)

    def test_source_exposes_closes_for_cli(self, monkeypatch: pytest.MonkeyPatch) -> None:
        source = self._source()
        monkeypatch.setattr(mt5feed, "connect", lambda suffix="", path=None: source)
        assert cli.select_source("mt5").closes == source.closes
