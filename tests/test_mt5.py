"""MetaTrader 5 data source, tested against a fake MT5 API module."""

from __future__ import annotations

from datetime import date, datetime, timezone
from pathlib import Path
from types import SimpleNamespace

import numpy as np
import pandas as pd
import pytest
from rich.console import Console

import cache
import cli
import config
import data
import mt5feed
import strategy
from tests.conftest import WED, weekdays_ending
from tests.test_cli import _fetcher_factory

REAL_CONNECT = mt5feed.connect  # captured before the autouse fixture disables it

DTYPE = [("time", "i8"), ("open", "f8"), ("high", "f8"), ("low", "f8"), ("close", "f8")]


def _rates(rows: list[tuple[date, float, float, float, float]]) -> np.ndarray:
    return np.array(
        [(int(pd.Timestamp(d).timestamp()), o, h, lo, c) for d, o, h, lo, c in rows], dtype=DTYPE
    )


class FakeMt5:
    TIMEFRAME_D1 = 16408

    def __init__(
        self,
        names: list[str],
        rates: dict[str, np.ndarray] | None = None,
        *,
        init_ok: bool = True,
    ) -> None:
        self.names = names
        self.rates = rates or {}
        self.init_ok = init_ok
        self.init_kwargs: dict[str, str] | None = None
        self.selected: list[str] = []
        self.shutdown_called = False
        self.tf_rates: dict[tuple[str, int], np.ndarray] = {}

    def initialize(self, **kwargs: str) -> bool:
        self.init_kwargs = kwargs
        return self.init_ok

    def shutdown(self) -> None:
        self.shutdown_called = True

    def last_error(self) -> tuple[int, str]:
        return (-1, "boom")

    def symbols_get(self) -> list[SimpleNamespace]:
        return [SimpleNamespace(name=n) for n in self.names]

    def symbol_select(self, name: str, enable: bool) -> bool:
        self.selected.append(name)
        return True

    def copy_rates_from_pos(
        self, symbol: str, timeframe: int, start: int, count: int
    ) -> np.ndarray | None:
        if timeframe == self.TIMEFRAME_D1:
            rows = self.rates.get(symbol)
        else:
            rows = self.tf_rates.get((symbol, timeframe))
        return None if rows is None else rows[-count:]

    def symbol_info_tick(self, symbol: str) -> SimpleNamespace | None:
        return None


def _history() -> list[tuple[date, float, float, float, float]]:
    """Weekday D1 bars to Wed 4 Mar (forming): Mon 1.10/1.00, Tue 1.09/1.01 inside, Wed forming."""
    days = weekdays_ending(date(2026, 3, 4), 10)
    rows = [(d, 1.0, 1.10, 1.00, 1.05) for d in days[:-2]]
    rows.append((days[-2], 1.04, 1.09, 1.01, 1.06))  # Tue 3 Mar: inside day
    rows.append((days[-1], 1.06, 1.095, 1.04, 1.07))  # Wed 4 Mar: forming, broke the 1.09 high
    return rows


class TestResolveSymbol:
    def test_exact_name(self) -> None:
        assert mt5feed.resolve_symbol("EURUSD", ["GBPUSD", "EURUSD"]) == "EURUSD"

    def test_exact_beats_suffixed(self) -> None:
        assert mt5feed.resolve_symbol("EURUSD", ["EURUSD.a", "EURUSD"]) == "EURUSD"

    def test_autodetects_suffix(self) -> None:
        assert mt5feed.resolve_symbol("EURUSD", ["GBPUSDm", "EURUSDm"]) == "EURUSDm"

    def test_prefers_shortest_candidate(self) -> None:
        assert mt5feed.resolve_symbol("EURUSD", ["EURUSD.pro", "EURUSD.a"]) == "EURUSD.a"

    def test_ignores_unrelated_long_names(self) -> None:
        assert mt5feed.resolve_symbol("EURUSD", ["EURUSD_FUTURES_DEC26"]) is None

    def test_explicit_suffix_is_strict_and_case_insensitive(self) -> None:
        names = ["EURUSD", "EURUSD.A"]
        assert mt5feed.resolve_symbol("EURUSD", names, ".a") == "EURUSD.A"
        assert mt5feed.resolve_symbol("EURUSD", ["EURUSD"], ".a") is None


class TestConnect:
    def test_init_failure_raises(self) -> None:
        with pytest.raises(mt5feed.Mt5Error, match="could not connect"):
            REAL_CONNECT(api=FakeMt5([], init_ok=False))

    def test_passes_terminal_path(self) -> None:
        api = FakeMt5([])
        REAL_CONNECT(path=r"C:\MT5\terminal64.exe", api=api)
        assert api.init_kwargs == {"path": r"C:\MT5\terminal64.exe"}

    def test_missing_package_raises(self, monkeypatch: pytest.MonkeyPatch) -> None:
        import sys

        monkeypatch.setitem(sys.modules, "MetaTrader5", None)
        with pytest.raises(mt5feed.Mt5Error, match="not installed"):
            REAL_CONNECT()


class TestMt5Source:
    def _source(self) -> tuple[mt5feed.Mt5Source, FakeMt5]:
        api = FakeMt5(["EURUSD.a"], {"EURUSD.a": _rates(_history())})
        return mt5feed.Mt5Source(api), api

    def test_daily_is_date_indexed_ohlc(self) -> None:
        source, api = self._source()
        frame = source.daily("EURUSD")
        assert list(frame.columns) == ["open", "high", "low", "close"]
        assert frame.index[-1] == pd.Timestamp("2026-03-04")
        assert api.selected == ["EURUSD.a"]  # added to Market Watch

    def test_daily_drops_weekend_bars(self) -> None:
        rows = [*_history(), (date(2026, 3, 8), 1.0, 1.1, 0.9, 1.0)]  # a Sunday bar
        source = mt5feed.Mt5Source(FakeMt5(["EURUSD"], {"EURUSD": _rates(rows)}))
        assert pd.Timestamp("2026-03-08") not in source.daily("EURUSD").index

    def test_symbol_not_found(self) -> None:
        with pytest.raises(mt5feed.Mt5Error, match="--mt5-suffix"):
            self._source()[0].daily("USDJPY")

    def test_no_data_raises(self) -> None:
        source = mt5feed.Mt5Source(FakeMt5(["EURUSD"], {}))
        with pytest.raises(mt5feed.Mt5Error, match="no D1 data"):
            source.daily("EURUSD")

    def test_daily_through_pipeline_gives_inside_day(self, conn) -> None:  # type: ignore[no-untyped-def]
        source, _ = self._source()
        candles = data.get_candles(conn, "EURUSD", WED, source.daily, force=True)
        assert candles.frame.index[-1] == pd.Timestamp("2026-03-03")  # forming Wed dropped
        signal = strategy.evaluate("EURUSD", data.to_bars(candles.frame))
        assert signal is not None
        assert signal.kind == "INSIDE"

    def test_live_range_is_todays_forming_bar(self) -> None:
        source, _ = self._source()
        session = data.current_session_range(source.live("EURUSD"), WED)
        assert session is not None
        assert (session.high, session.low) == (1.095, 1.04)

    def test_live_range_unknown_on_weekend(self) -> None:
        # Saturday: MT5's newest bar is Friday's, which is not "today's" session.
        rows = [*_history()[:-1], (date(2026, 3, 6), 1.0, 1.1, 0.9, 1.0)]
        source = mt5feed.Mt5Source(FakeMt5(["EURUSD"], {"EURUSD": _rates(rows)}))
        saturday = datetime(2026, 3, 7, 15, 0, tzinfo=timezone.utc)
        assert data.current_session_range(source.live("EURUSD"), saturday) is None

    def test_close_shuts_down_terminal_connection(self) -> None:
        source, api = self._source()
        source.close()
        assert api.shutdown_called


class TestSelectSource:
    def test_yahoo_choice_never_touches_mt5(self) -> None:
        source = cli.select_source("yahoo")
        assert source.name == "Yahoo"
        assert source.db_key == "yahoo"

    def test_explicit_mt5_failure_propagates(self) -> None:
        with pytest.raises(mt5feed.Mt5Error):
            cli.select_source("mt5")

    def test_auto_falls_back_to_yahoo_with_note(self) -> None:
        source = cli.select_source("auto")
        assert source.name == "Yahoo"
        assert "MT5 not used" in source.note

    def test_auto_uses_mt5_when_available(self, monkeypatch: pytest.MonkeyPatch) -> None:
        api = FakeMt5(["EURUSD"], {"EURUSD": _rates(_history())})
        monkeypatch.setattr(mt5feed, "connect", lambda suffix="", path=None: mt5feed.Mt5Source(api))
        source = cli.select_source("auto")
        assert source.name == "MT5"
        assert source.always_refresh
        assert source.min_interval == config.MT5_MIN_INTERVAL
        assert source.db_key == "mt5"


class TestMainWithMt5:
    @staticmethod
    def _patch(monkeypatch: pytest.MonkeyPatch) -> FakeMt5:
        api = FakeMt5(["EURUSD.a"], {"EURUSD.a": _rates(_history())})
        monkeypatch.setattr(mt5feed, "connect", lambda suffix="", path=None: mt5feed.Mt5Source(api))
        return api

    def test_scan_uses_mt5_candles_and_live_range(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        api = self._patch(monkeypatch)
        console = Console(record=True, width=130, force_terminal=False)
        code = cli.main(
            ["EURUSD", "--source", "mt5", "--db", str(tmp_path / "c.sqlite")],
            console=console,
            now=WED,
        )
        text = console.export_text()
        assert code == 0
        assert "source: MT5" in text
        assert "EUR/USD" in text
        assert "● HIGH" in text  # forming Wed bar traded above the 1.09 high
        assert api.shutdown_called

    def test_watch_accepts_five_second_interval_on_mt5(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        self._patch(monkeypatch)
        code = cli.main(
            [
                "EURUSD",
                "--source",
                "mt5",
                "--watch",
                "--no-toast",
                "--no-beep",
                "--interval",
                "5",
                "--db",
                str(tmp_path / "c.sqlite"),
            ],
            console=Console(record=True, width=130),
            now=WED,
            sleep=lambda _s: None,
            max_cycles=1,
        )
        assert code == 0

    def test_interval_below_mt5_minimum_rejected(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        api = self._patch(monkeypatch)
        with pytest.raises(SystemExit) as exc:
            cli.main(
                ["EURUSD", "--watch", "--interval", "4", "--db", str(tmp_path / "c.sqlite")],
                now=WED,
            )
        assert exc.value.code == 2
        assert api.shutdown_called  # connection released even on a bad argument

    def test_mt5_required_but_unavailable_exits_two(self, tmp_path: Path) -> None:
        with pytest.raises(SystemExit) as exc:
            cli.main(["EURUSD", "--source", "mt5", "--db", str(tmp_path / "c.sqlite")])
        assert exc.value.code == 2

    def test_auto_fallback_prints_note_and_uses_yahoo(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        monkeypatch.setattr(data, "yf_fetch", _fetcher_factory({}))
        console = Console(record=True, width=130, force_terminal=False)
        cli.main(["EURUSD", "--db", str(tmp_path / "c.sqlite")], console=console, now=WED)
        text = console.export_text()
        assert "MT5 not used" in text
        assert "source: Yahoo" in text


class TestCachePaths:
    def test_each_source_has_its_own_cache(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("FX_SCAN_DB", raising=False)
        assert config.db_path("yahoo").name == "cache.sqlite"
        assert config.db_path("mt5").name == "cache_mt5.sqlite"

    def test_env_override_applies_to_both(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("FX_SCAN_DB", str(Path("x") / "mine.sqlite"))
        assert config.db_path("yahoo").name == "mine.sqlite"
        assert config.db_path("mt5").name == "mine_mt5.sqlite"

    def test_expected_day_unchanged_by_source(self) -> None:
        assert cache.expected_last_complete(WED) == date(2026, 3, 3)
