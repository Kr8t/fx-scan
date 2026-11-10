"""Watch mode: alert when today trades beyond the previous day's high/low."""

from __future__ import annotations

import sys
from datetime import date
from pathlib import Path

import pandas as pd
import pytest
from rich.console import Console

import cli
import data
import notify
import strategy
from strategy import Bar, Signal
from tests.conftest import WED
from tests.test_cli import _fetcher_factory
from tests.test_sessions import _hourly

REAL_BEEP = notify.beep  # captured before the autouse fixture silences it


def _signal(kind: str = "INSIDE", side: str | None = None) -> Signal:
    prev = Bar(date(2026, 3, 2), 1.0, 1.10, 1.00, 1.05)
    bar = Bar(date(2026, 3, 3), 1.04, 1.09, 1.01, 1.06)
    return Signal("EURUSD", kind, bar, prev, "bull", side)  # type: ignore[arg-type]


class TestDetectBreaks:
    def test_high_only(self) -> None:
        found = strategy.detect_breaks("EURUSD", 1.09, 1.01, 1.095, 1.02)
        assert [(b.level, b.level_price, b.extreme) for b in found] == [("HIGH", 1.09, 1.095)]

    def test_low_only(self) -> None:
        found = strategy.detect_breaks("EURUSD", 1.09, 1.01, 1.08, 1.005)
        assert [b.level for b in found] == ["LOW"]

    def test_both_sides(self) -> None:
        found = strategy.detect_breaks("EURUSD", 1.09, 1.01, 1.10, 1.00)
        assert [b.level for b in found] == ["HIGH", "LOW"]

    def test_touch_is_not_a_break(self) -> None:
        assert strategy.detect_breaks("EURUSD", 1.09, 1.01, 1.09, 1.01) == []

    def test_buffer_requires_extra_distance(self) -> None:
        assert strategy.detect_breaks("EURUSD", 1.09, 1.01, 1.0905, 1.02, buffer=0.001) == []
        assert len(strategy.detect_breaks("EURUSD", 1.09, 1.01, 1.0915, 1.02, buffer=0.001)) == 1


class TestCurrentSessionRange:
    def test_ignores_previous_session(self) -> None:
        # Now = Wed 4 Mar 12:00 UTC; the session started Tue 22:00 UTC (17:00 EST).
        raw = _hourly(
            [
                ("2026-03-03 15:00", 1.00, 9.99, 0.01, 1.00),  # previous session: must be ignored
                ("2026-03-03 23:00", 1.05, 1.07, 1.04, 1.06),
                ("2026-03-04 10:00", 1.06, 1.08, 1.03, 1.05),
            ]
        )
        rng = data.current_session_range(raw, WED)
        assert rng is not None
        assert (rng.high, rng.low, rng.last) == (1.08, 1.03, 1.05)

    def test_none_when_no_bars_in_session(self) -> None:
        raw = _hourly([("2026-03-03 15:00", 1.0, 1.1, 0.9, 1.0)])
        assert data.current_session_range(raw, WED) is None

    def test_none_for_empty_frame(self) -> None:
        assert data.current_session_range(pd.DataFrame(), WED) is None


class TestWatchOnce:
    @staticmethod
    def _live(high: float, low: float = 1.04):  # type: ignore[no-untyped-def]
        return lambda pair: _hourly([("2026-03-04 10:00", 1.05, high, low, 1.06)])

    def test_alerts_once_per_level(self) -> None:
        alerted: set[tuple[str, str]] = set()
        first = cli.watch_once([_signal()], alerted, WED, self._live(1.095), 0.0)
        again = cli.watch_once([_signal()], alerted, WED, self._live(1.099), 0.0)
        assert [b.level for _, b in first] == ["HIGH"]
        assert again == []

    def test_other_side_still_alerts_later(self) -> None:
        alerted: set[tuple[str, str]] = set()
        cli.watch_once([_signal()], alerted, WED, self._live(1.095), 0.0)
        later = cli.watch_once([_signal()], alerted, WED, self._live(1.095, 1.005), 0.0)
        assert [b.level for _, b in later] == ["LOW"]

    def test_feed_error_is_skipped(self) -> None:
        def boom(pair: str) -> pd.DataFrame:
            raise ConnectionError("offline")

        assert cli.watch_once([_signal()], set(), WED, boom, 0.0) == []

    def test_pip_buffer_applied(self) -> None:
        # 1.0901 is 1 pip above 1.09; a 2-pip buffer suppresses it.
        assert cli.watch_once([_signal()], set(), WED, self._live(1.0901), 2.0) == []


class TestFormatAlert:
    def test_text(self) -> None:
        brk = strategy.LevelBreak("EURUSD", "HIGH", 1.09, 1.0935)
        title, body = cli.format_alert(_signal(), brk)
        assert title == "EUR/USD broke previous day high"
        assert "1.09000 taken out" in body
        assert "+35.0 pips" in body
        assert "INSIDE DAY" in body
        assert title.isascii()
        assert body.isascii()

    def test_cib_context(self) -> None:
        brk = strategy.LevelBreak("EURUSD", "LOW", 1.01, 1.0)
        _, body = cli.format_alert(_signal("CIB", "UP"), brk)
        assert "CIB UP" in body


class TestNotify:
    def test_ntfy_posts_to_topic(self) -> None:
        seen: dict[str, object] = {}

        def post(url: str, body: bytes, headers: dict[str, str]) -> None:
            seen.update(url=url, body=body, headers=headers)

        assert notify.send_ntfy("my topic", "EUR/USD broke previous day high", "msg", post)
        assert seen["url"] == "https://ntfy.sh/my%20topic"
        assert seen["body"] == b"msg"

    def test_ntfy_failure_returns_false(self) -> None:
        def post(url: str, body: bytes, headers: dict[str, str]) -> None:
            raise OSError("offline")

        assert not notify.send_ntfy("t", "x", "y", post)

    def test_desktop_unavailable_returns_false(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setitem(sys.modules, "winotify", None)
        assert not notify.send_desktop("x", "y")


class TestRunWatch:
    def _main(self, tmp_path: Path, scan, live, *extra: str, cycles: int = 3):  # type: ignore[no-untyped-def]
        console = Console(record=True, width=130, force_terminal=False)
        code = cli.main(
            ["EURUSD", "--watch", "--no-toast", "--db", str(tmp_path / "c.sqlite"), *extra],
            fetcher=scan,
            live_fetcher=live,
            sleep=lambda _s: None,
            max_cycles=cycles,
            console=console,
            now=WED,
        )
        return code, console.export_text()

    def test_alerts_exactly_once_when_level_breaks(self, tmp_path: Path) -> None:
        scan = _fetcher_factory({"EURUSD": (1.04, 1.09, 1.01, 1.06)})  # inside day
        calls = {"n": 0}

        def live(pair: str) -> pd.DataFrame:
            calls["n"] += 1
            high = 1.085 if calls["n"] <= 2 else 1.095  # quiet for the scan and cycle 1
            return _hourly([("2026-03-04 10:00", 1.05, high, 1.04, 1.06)])

        code, text = self._main(tmp_path, scan, live)
        assert code == 0
        assert "Watching 1 pair(s)" in text
        assert text.count("EUR/USD broke previous day high") == 1
        assert calls["n"] == 4  # 1 poll for the scan's Broke column + 1 per cycle

    def test_nothing_to_watch(self, tmp_path: Path) -> None:
        def live(pair: str) -> pd.DataFrame:
            raise AssertionError("must not poll when no pair matched")

        code, text = self._main(tmp_path, _fetcher_factory({}), live)
        assert code == 0
        assert "Watching 0 pair(s)" in text

    def test_interval_below_minimum_exits_two(self, tmp_path: Path) -> None:
        with pytest.raises(SystemExit) as exc:
            cli.main(["--watch", "--interval", "5", "--db", str(tmp_path / "c.sqlite")])
        assert exc.value.code == 2


class TestBrokeColumn:
    def test_break_cell_states(self) -> None:
        high = strategy.LevelBreak("EURUSD", "HIGH", 1.09, 1.095)
        low = strategy.LevelBreak("EURUSD", "LOW", 1.01, 1.005)
        assert cli.break_cell(None).plain == "-"
        assert cli.break_cell([]).plain == "○"
        assert cli.break_cell([high]).plain == "● HIGH"
        assert cli.break_cell([low]).plain == "● LOW"
        assert cli.break_cell([high, low]).plain == "● BOTH"

    def test_current_breaks_unknown_when_feed_fails(self) -> None:
        def boom(pair: str) -> pd.DataFrame:
            raise ConnectionError("offline")

        assert cli.current_breaks(_signal(), WED, boom, 0.0) is None

    def test_current_breaks_unknown_when_no_bars_today(self) -> None:
        old = lambda pair: _hourly([("2026-03-03 15:00", 1.0, 1.1, 0.9, 1.0)])  # noqa: E731
        assert cli.current_breaks(_signal(), WED, old, 0.0) is None

    def test_tables_show_dot_per_row(self) -> None:
        broke = [strategy.LevelBreak("EURUSD", "HIGH", 1.09, 1.095)]
        console = Console(record=True, width=130, force_terminal=False)
        console.print(cli.build_inside_table([_signal()], {"EURUSD": broke}))
        console.print(cli.build_cib_table([_signal("CIB", "UP")], {"EURUSD": []}))
        text = console.export_text()
        assert "● HIGH" in text
        assert "○" in text

    def test_scan_marks_broken_and_unbroken_pairs(self, tmp_path: Path) -> None:
        scan = _fetcher_factory(
            {"EURUSD": (1.04, 1.09, 1.01, 1.06), "GBPUSD": (1.04, 1.09, 1.01, 1.06)}
        )

        def live(pair: str) -> pd.DataFrame:
            high = 1.095 if pair == "EURUSD" else 1.08
            return _hourly([("2026-03-04 10:00", 1.05, high, 1.04, 1.06)])

        console = Console(record=True, width=130, force_terminal=False)
        cli.main(
            ["EURUSD", "GBPUSD", "--db", str(tmp_path / "c.sqlite")],
            fetcher=scan,
            live_fetcher=live,
            console=console,
            now=WED,
        )
        text = console.export_text()
        eur = next(line for line in text.splitlines() if "EUR/USD" in line)
        gbp = next(line for line in text.splitlines() if "GBP/USD" in line)
        assert "● HIGH" in eur
        assert "○" in gbp
        assert "Broke:" in text  # legend

    def test_scan_without_live_data_shows_dash(self, tmp_path: Path) -> None:
        console = Console(record=True, width=130, force_terminal=False)
        cli.main(
            ["EURUSD", "--db", str(tmp_path / "c.sqlite")],
            fetcher=_fetcher_factory({"EURUSD": (1.04, 1.09, 1.01, 1.06)}),
            console=console,
            now=WED,
        )
        eur = next(line for line in console.export_text().splitlines() if "EUR/USD" in line)
        assert eur.rstrip().endswith("-")


class TestBeepOnBreak:
    @staticmethod
    def _run(tmp_path: Path, pairs: list[str], live, *extra: str, beeps: list[int]):  # type: ignore[no-untyped-def]
        console = Console(record=True, width=130, force_terminal=False)
        scan = _fetcher_factory(dict.fromkeys(pairs, (1.04, 1.09, 1.01, 1.06)))
        cli.main(
            [*pairs, "--watch", "--no-toast", "--db", str(tmp_path / "c.sqlite"), *extra],
            fetcher=scan,
            live_fetcher=live,
            sleep=lambda _s: None,
            max_cycles=4,
            console=console,
            now=WED,
            beeper=lambda: beeps.append(1),
        )
        return console.export_text()

    def test_beeps_once_per_cycle_even_with_two_pairs(self, tmp_path: Path) -> None:
        beeps: list[int] = []
        calls = {"n": 0}

        def live(pair: str) -> pd.DataFrame:
            calls["n"] += 1
            high = 1.085 if calls["n"] <= 4 else 1.095  # scan + cycle 1 quiet (2 pairs each)
            return _hourly([("2026-03-04 10:00", 1.05, high, 1.04, 1.06)])

        text = self._run(tmp_path, ["EURUSD", "GBPUSD"], live, beeps=beeps)
        assert beeps == [1]  # both pairs broke in the same cycle -> one beep
        assert text.count("taken out, extreme") == 2

    def test_no_beep_without_a_break(self, tmp_path: Path) -> None:
        beeps: list[int] = []
        live = lambda pair: _hourly([("2026-03-04 10:00", 1.05, 1.08, 1.04, 1.06)])  # noqa: E731
        self._run(tmp_path, ["EURUSD"], live, beeps=beeps)
        assert beeps == []

    def test_no_beep_flag(self, tmp_path: Path) -> None:
        beeps: list[int] = []
        live = lambda pair: _hourly([("2026-03-04 10:00", 1.05, 1.095, 1.04, 1.06)])  # noqa: E731
        self._run(tmp_path, ["EURUSD"], live, "--no-beep", beeps=beeps)
        assert beeps == []

    def test_break_redraws_tables_and_lists_the_pair(self, tmp_path: Path) -> None:
        beeps: list[int] = []
        calls = {"n": 0}

        def live(pair: str) -> pd.DataFrame:
            calls["n"] += 1
            high = 1.085 if calls["n"] <= 2 else 1.095
            return _hourly([("2026-03-04 10:00", 1.05, high, 1.04, 1.06)])

        text = self._run(tmp_path, ["EURUSD"], live, beeps=beeps)
        tail = text[text.index("taken out, extreme") :]
        assert "● HIGH" in tail  # tables redrawn with the dot
        assert "BREAKS TODAY" in tail
        assert "EUR/USD  high taken out (1.09500)" in tail


class TestRealBeep:
    def test_three_rising_tones(self, monkeypatch: pytest.MonkeyPatch) -> None:
        tones: list[int] = []

        class Fake:
            @staticmethod
            def Beep(freq: int, ms: int) -> None:  # noqa: N802
                tones.append(freq)

        monkeypatch.setitem(sys.modules, "winsound", Fake)
        REAL_BEEP()
        assert tones == sorted(tones)
        assert len(tones) == 3

    def test_falls_back_to_system_sound(self, monkeypatch: pytest.MonkeyPatch) -> None:
        played: list[str] = []

        class Fake:
            @staticmethod
            def Beep(freq: int, ms: int) -> None:  # noqa: N802
                raise RuntimeError("no beep device")

            @staticmethod
            def MessageBeep() -> None:  # noqa: N802
                played.append("system")

        monkeypatch.setitem(sys.modules, "winsound", Fake)
        REAL_BEEP()
        assert played == ["system"]

    def test_terminal_bell_when_not_windows(
        self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        monkeypatch.setitem(sys.modules, "winsound", None)
        REAL_BEEP()
        assert "\a" in capsys.readouterr().out
