from __future__ import annotations

import math
from datetime import date

import pytest

import strategy
from strategy import Bar, evaluate
from tests.conftest import bar


class TestInsideDay:
    def test_strictly_inside(self) -> None:
        assert strategy.is_inside_day(1.09, 1.01, 1.10, 1.00)

    def test_equal_high_is_not_inside(self) -> None:
        assert not strategy.is_inside_day(1.10, 1.01, 1.10, 1.00)

    def test_buffer_shrinks_inside_zone(self) -> None:
        assert strategy.is_inside_day(1.095, 1.005, 1.10, 1.00, buffer=0.001)
        assert not strategy.is_inside_day(1.0995, 1.005, 1.10, 1.00, buffer=0.001)

    def test_negative_buffer_rejected(self) -> None:
        with pytest.raises(ValueError, match="buffer"):
            strategy.is_inside_day(1.09, 1.01, 1.10, 1.00, buffer=-0.1)


class TestBreakout:
    def test_close_above_high(self) -> None:
        assert strategy.breakout_side(1.101, 1.10, 1.00) == "UP"

    def test_close_below_low(self) -> None:
        assert strategy.breakout_side(0.999, 1.10, 1.00) == "DOWN"

    def test_close_equal_to_high_is_not_cib(self) -> None:
        assert strategy.breakout_side(1.10, 1.10, 1.00) is None

    def test_buffer_requires_extra_distance(self) -> None:
        assert strategy.breakout_side(1.1005, 1.10, 1.00, buffer=0.001) is None
        assert strategy.breakout_side(1.1011, 1.10, 1.00, buffer=0.001) == "UP"
        assert strategy.breakout_side(0.9989, 1.10, 1.00, buffer=0.001) == "DOWN"


class TestResolve:
    def test_cib_suppresses_inside(self) -> None:
        assert strategy.resolve_signals(True, "UP") == (False, "UP")


class TestEvaluate:
    def test_needs_two_bars(self) -> None:
        assert evaluate("EURUSD", [bar(1.1, 1.0)]) is None
        assert evaluate("EURUSD", []) is None

    def test_inside_signal(self) -> None:
        sig = evaluate("EURUSD", [bar(1.10, 1.00), bar(1.09, 1.01)])
        assert sig is not None
        assert sig.kind == "INSIDE"
        assert sig.side is None
        assert sig.range_ratio == pytest.approx(0.8)

    def test_cib_up_signal(self) -> None:
        sig = evaluate("EURUSD", [bar(1.10, 1.00), bar(1.13, 1.05, 1.06, 1.12)])
        assert sig is not None
        assert (sig.kind, sig.side, sig.direction) == ("CIB", "UP", "bull")
        assert sig.break_distance == pytest.approx(0.02)

    def test_cib_down_signal(self) -> None:
        sig = evaluate("EURUSD", [bar(1.10, 1.00), bar(1.05, 0.96, 1.04, 0.97)])
        assert sig is not None
        assert (sig.kind, sig.side, sig.direction) == ("CIB", "DOWN", "bear")
        assert sig.break_distance == pytest.approx(0.03)

    def test_wick_only_break_is_not_cib(self) -> None:
        # Pokes above the previous high but closes back inside.
        assert evaluate("EURUSD", [bar(1.10, 1.00), bar(1.12, 1.02, 1.05, 1.08)]) is None

    def test_outside_bar_closing_inside_is_no_signal(self) -> None:
        assert evaluate("EURUSD", [bar(1.10, 1.00), bar(1.12, 0.98, 1.05, 1.06)]) is None

    def test_only_last_two_bars_matter(self) -> None:
        bars = [bar(2.0, 0.5), bar(1.10, 1.00), bar(1.09, 1.01)]
        sig = evaluate("EURUSD", bars)
        assert sig is not None
        assert sig.kind == "INSIDE"

    def test_nan_bar_returns_none(self) -> None:
        bad = Bar(date(2026, 3, 3), 1.0, math.nan, 1.0, 1.0)
        assert evaluate("EURUSD", [bar(1.1, 1.0), bad]) is None

    def test_buffer_applied_in_evaluate(self) -> None:
        bars = [bar(1.10, 1.00), bar(1.13, 1.05, 1.06, 1.1005)]
        assert evaluate("EURUSD", bars, buffer=0.0) is not None
        assert evaluate("EURUSD", bars, buffer=0.001) is None

    def test_inside_doji_is_neutral(self) -> None:
        sig = evaluate("EURUSD", [bar(1.10, 1.00), bar(1.09, 1.01, 1.05, 1.05)])
        assert sig is not None
        assert sig.direction == "neutral"
