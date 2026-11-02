from __future__ import annotations

from datetime import date
from pathlib import Path

import pandas as pd
import pytest
from rich.console import Console

import cli
import config
from strategy import Bar, Signal
from tests.conftest import WED, weekdays_ending


def _fetcher_factory(last_bars: dict[str, tuple[float, float, float, float]]):  # type: ignore[no-untyped-def]
    """Ten flat bars, then a custom (o, h, l, c) final bar per pair."""

    def fetch(pair: str) -> pd.DataFrame:
        days = weekdays_ending(date(2026, 3, 3), 10)
        rows = [{"Open": 1.0, "High": 1.10, "Low": 1.00, "Close": 1.05}] * 9
        o, h, lo, c = last_bars.get(pair, (1.05, 1.20, 0.90, 1.05))  # default: outside bar
        rows.append({"Open": o, "High": h, "Low": lo, "Close": c})
        return pd.DataFrame(rows, index=pd.DatetimeIndex([pd.Timestamp(d) for d in days]))

    return fetch


def _signal(kind: str, direction: str, side: str | None = None) -> Signal:
    prev = Bar(date(2026, 3, 2), 1.0, 1.10, 1.00, 1.05)
    bar = Bar(date(2026, 3, 3), 1.04, 1.09, 1.01, 1.06)
    return Signal("EURUSD", kind, bar, prev, direction, side)  # type: ignore[arg-type]


class TestRowStyle:
    def test_inside_rows_use_candle_colour(self) -> None:
        table = cli.build_inside_table([_signal("INSIDE", "bull"), _signal("INSIDE", "bear")])
        assert [r.style for r in table.rows] == ["green", "red"]

    def test_cib_rows_use_candle_colour_not_side(self) -> None:
        # A bearish candle that broke UP must still be red.
        table = cli.build_cib_table([_signal("CIB", "bear", "UP")])
        assert table.rows[0].style == "red"


class TestParsing:
    def test_normalises_and_dedupes(self) -> None:
        assert cli.parse_pairs(["eur/usd", "EURUSD", "gbp_jpy"]) == ["EURUSD", "GBPJPY"]

    def test_unknown_pair_rejected(self) -> None:
        with pytest.raises(ValueError, match="XXXYYY"):
            cli.parse_pairs(["XXXYYY"])

    def test_pip_helpers(self) -> None:
        assert config.pip_size("USDJPY") == 0.01
        assert config.pip_size("EURUSD") == 0.0001
        assert config.yahoo_symbol("eurusd") == "EURUSD=X"
        assert config.display_pair("GBPJPY") == "GBP/JPY"


class TestMain:
    def _run(self, tmp_path: Path, fetcher, *args: str) -> tuple[int, str]:  # type: ignore[no-untyped-def]
        console = Console(record=True, width=130, force_terminal=False)
        code = cli.main(
            [*args, "--db", str(tmp_path / "c.sqlite")], fetcher=fetcher, console=console, now=WED
        )
        return code, console.export_text()

    def test_inside_and_cib_land_in_right_tables(self, tmp_path: Path) -> None:
        fetcher = _fetcher_factory(
            {
                "EURUSD": (1.04, 1.09, 1.01, 1.06),  # inside
                "GBPUSD": (1.06, 1.14, 1.05, 1.13),  # CIB up
                "USDJPY": (1.04, 1.06, 0.95, 0.96),  # CIB down
            }
        )
        code, text = self._run(tmp_path, fetcher, "EURUSD", "GBPUSD", "USDJPY")
        assert code == 0
        inside_part, cib_part = text.split("CLOSED IN BREAKOUT (CIB)")
        assert "EUR/USD" in inside_part
        assert "GBP/USD" not in inside_part
        assert "GBP/USD" in cib_part
        assert "USD/JPY" in cib_part
        assert "ABOVE HIGH" in cib_part
        assert "BELOW LOW" in cib_part

    def test_empty_state(self, tmp_path: Path) -> None:
        code, text = self._run(tmp_path, _fetcher_factory({}), "EURUSD")
        assert code == 0
        assert text.count("no signals") == 2

    def test_buffer_flag_filters_marginal_break(self, tmp_path: Path) -> None:
        # Closes 0.5 pip above prior high; a 2-pip buffer should remove it.
        fetcher = _fetcher_factory({"EURUSD": (1.06, 1.12, 1.05, 1.10005)})
        _, with_zero = self._run(tmp_path, fetcher, "EURUSD")
        assert "ABOVE HIGH" in with_zero
        _, with_buffer = self._run(tmp_path, fetcher, "EURUSD", "--buffer-pips", "2", "--refresh")
        assert "ABOVE HIGH" not in with_buffer

    def test_all_failures_exit_code_one(self, tmp_path: Path) -> None:
        def boom(pair: str) -> pd.DataFrame:
            raise ConnectionError("offline")

        code, text = self._run(tmp_path, boom, "EURUSD")
        assert code == 1
        assert "failed EURUSD" in text

    def test_invalid_pair_exits_two(self, tmp_path: Path) -> None:
        with pytest.raises(SystemExit) as exc:
            cli.main(["NOPE", "--db", str(tmp_path / "c.sqlite")])
        assert exc.value.code == 2

    def test_second_run_served_from_cache(self, tmp_path: Path) -> None:
        calls: list[str] = []
        base = _fetcher_factory({})

        def counting(pair: str) -> pd.DataFrame:
            calls.append(pair)
            return base(pair)

        self._run(tmp_path, counting, "EURUSD")
        self._run(tmp_path, counting, "EURUSD")
        assert calls == ["EURUSD"]


class TestLatestClosedDayFilter:
    def _run(self, tmp_path: Path, fetcher, *args: str) -> tuple[int, str]:  # type: ignore[no-untyped-def]
        console = Console(record=True, width=130, force_terminal=False)
        code = cli.main(
            [*args, "--db", str(tmp_path / "c.sqlite")], fetcher=fetcher, console=console, now=WED
        )
        return code, console.export_text()

    @staticmethod
    def _behind_fetcher(pair: str) -> pd.DataFrame:
        # Newest bar is Mon 2 Mar and is an inside day, but Tue 3 Mar is the latest closed day.
        days = weekdays_ending(date(2026, 3, 2), 10)
        rows = [{"Open": 1.0, "High": 1.10, "Low": 1.00, "Close": 1.05}] * 9
        rows.append({"Open": 1.04, "High": 1.09, "Low": 1.01, "Close": 1.06})
        return pd.DataFrame(rows, index=pd.DatetimeIndex([pd.Timestamp(d) for d in days]))

    def test_signal_on_older_bar_is_filtered_out(self, tmp_path: Path) -> None:
        code, text = self._run(tmp_path, self._behind_fetcher, "EURUSD")
        assert "EUR/USD" not in text.split("skipped")[0].split("INSIDE DAY")[-1]
        assert "skipped EURUSD" in text
        assert "2026-03-02" in text
        assert code == 1  # nothing usable

    def test_header_shows_latest_closed_day(self, tmp_path: Path) -> None:
        _, text = self._run(tmp_path, _fetcher_factory({}), "EURUSD")
        assert "most recent closed day: 2026-03-03" in text

    def test_current_pairs_still_reported_next_to_skipped(self, tmp_path: Path) -> None:
        good = _fetcher_factory({"EURUSD": (1.04, 1.09, 1.01, 1.06)})

        def mixed(pair: str) -> pd.DataFrame:
            return good(pair) if pair == "EURUSD" else self._behind_fetcher(pair)

        code, text = self._run(tmp_path, mixed, "EURUSD", "GBPUSD")
        assert code == 0
        assert "EUR/USD" in text
        assert "skipped GBPUSD" in text


class TestWatchList:
    def test_groups_make_up_18_unique_pairs(self) -> None:
        assert len(config.PAIRS) == 18
        assert len(set(config.PAIRS)) == 18

    def test_only_majors_yen_and_eur_crosses(self) -> None:
        assert len(config.MAJORS) == 7
        for pair in config.YEN_CROSSES:
            assert pair.endswith("JPY")
            assert "USD" not in pair
        for pair in config.EUR_CROSSES:
            assert pair.startswith("EUR")
            assert "USD" not in pair
            assert not pair.endswith("JPY")

    def test_dropped_crosses_are_gone(self) -> None:
        for pair in ("GBPAUD", "GBPCHF"):
            assert pair not in config.PAIRS
