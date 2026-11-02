"""FX SCAN command line interface."""

from __future__ import annotations

import argparse
import sys
from collections.abc import Sequence
from contextlib import closing
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from pathlib import Path

from rich import box
from rich.console import Console
from rich.table import Table

import cache
import config
import data
import strategy
from strategy import Direction, Signal

ROW_STYLES: dict[Direction, str] = {"bull": "green", "bear": "red", "neutral": "dim white"}
CANDLE_LABELS: dict[Direction, str] = {
    "bull": "▲ BULL",
    "bear": "▼ BEAR",
    "neutral": "■ DOJI",
}


@dataclass
class ScanReport:
    inside: list[Signal] = field(default_factory=list)
    cib: list[Signal] = field(default_factory=list)
    failures: dict[str, str] = field(default_factory=dict)
    stale: list[str] = field(default_factory=list)
    outdated: dict[str, date | None] = field(default_factory=dict)
    as_of: date | None = None
    scanned: int = 0


def row_style(direction: Direction) -> str:
    """Rows are coloured by candle direction, never by signal type."""
    return ROW_STYLES[direction]


def parse_pairs(values: Sequence[str] | None) -> list[str]:
    """Normalise user pair input (``eur/usd`` -> ``EURUSD``) and validate it."""
    if not values:
        return list(config.PAIRS)
    pairs = [v.replace("/", "").replace("_", "").upper() for v in values]
    unknown = [p for p in pairs if p not in config.PAIRS]
    if unknown:
        msg = f"unknown pair(s): {', '.join(unknown)}"
        raise ValueError(msg)
    return list(dict.fromkeys(pairs))


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="fx_scan",
        description="Scan major FX pairs for Inside Days and Closed-In-Breakouts (CIB).",
    )
    parser.add_argument("pairs", nargs="*", help="limit to these pairs, e.g. EURUSD GBP/JPY")
    parser.add_argument(
        "-b",
        "--buffer-pips",
        type=float,
        default=0.0,
        help="pip margin a bar must clear (default: 0)",
    )
    parser.add_argument("-r", "--refresh", action="store_true", help="ignore cache freshness")
    parser.add_argument("--db", type=Path, default=None, help="SQLite cache path")
    parser.add_argument("--no-color", action="store_true", help="disable colour output")
    parser.add_argument(
        "-V", "--version", action="version", version=f"{config.APP_NAME} {config.VERSION}"
    )
    return parser


def run_scan(
    pairs: Sequence[str],
    *,
    buffer_pips: float,
    db: Path | str,
    force: bool = False,
    now: datetime | None = None,
    fetcher: data.Fetcher | None = None,
) -> ScanReport:
    """Fetch candles for every pair and collect signals."""
    moment = now or datetime.now(timezone.utc)
    closed_day = cache.expected_last_complete(moment)
    report = ScanReport(as_of=closed_day)
    with closing(cache.connect(db)) as conn:
        for pair in pairs:
            report.scanned += 1
            try:
                candles = data.get_candles(conn, pair, moment, fetcher, force=force)
            except data.DataError as exc:
                report.failures[pair] = str(exc)
                continue
            if candles.stale:
                report.stale.append(pair)
            bars = data.to_bars(candles.frame)
            # Only the most recent closed day may produce a signal.
            if not bars or bars[-1].day != closed_day:
                report.outdated[pair] = bars[-1].day if bars else None
                continue
            buffer = buffer_pips * config.pip_size(pair)
            signal = strategy.evaluate(pair, bars, buffer)
            if signal is None:
                continue
            (report.cib if signal.kind == "CIB" else report.inside).append(signal)
    return report


def _price(pair: str, value: float) -> str:
    return f"{value:.{config.price_decimals(pair)}f}"


def build_inside_table(signals: Sequence[Signal]) -> Table:
    table = Table(title="INSIDE DAY", box=box.SIMPLE_HEAVY, title_style="bold")
    for name in ("Pair", "Date", "Candle", "Close", "Prev High", "Prev Low", "Range %"):
        table.add_column(name, justify="left" if name in {"Pair", "Date", "Candle"} else "right")
    for s in signals:
        table.add_row(
            config.display_pair(s.pair),
            s.bar.day.isoformat(),
            CANDLE_LABELS[s.direction],
            _price(s.pair, s.bar.close),
            _price(s.pair, s.prev.high),
            _price(s.pair, s.prev.low),
            f"{s.range_ratio * 100:.0f}%",
            style=row_style(s.direction),
        )
    return table


def build_cib_table(signals: Sequence[Signal]) -> Table:
    table = Table(title="CLOSED IN BREAKOUT (CIB)", box=box.SIMPLE_HEAVY, title_style="bold")
    for name in ("Pair", "Date", "Candle", "Close", "Prev High", "Prev Low", "Break", "Pips"):
        table.add_column(
            name, justify="left" if name in {"Pair", "Date", "Candle", "Break"} else "right"
        )
    for s in signals:
        pips = s.break_distance / config.pip_size(s.pair)
        table.add_row(
            config.display_pair(s.pair),
            s.bar.day.isoformat(),
            CANDLE_LABELS[s.direction],
            _price(s.pair, s.bar.close),
            _price(s.pair, s.prev.high),
            _price(s.pair, s.prev.low),
            "ABOVE HIGH" if s.side == "UP" else "BELOW LOW",
            f"{pips:.1f}",
            style=row_style(s.direction),
        )
    return table


def render(report: ScanReport, console: Console) -> None:
    """Print both tables (or an empty-state line) plus a footer."""
    closed = report.as_of.isoformat() if report.as_of else "n/a"
    console.print(
        f"[bold]{config.APP_NAME}[/bold]  most recent closed day: {closed} "
        f"(daily close {config.SESSION_CUTOFF_HOUR}:00 {config.SESSION_TZ})",
        highlight=False,
    )
    for title, signals, builder in (
        ("INSIDE DAY", report.inside, build_inside_table),
        ("CLOSED IN BREAKOUT (CIB)", report.cib, build_cib_table),
    ):
        if signals:
            console.print(builder(signals))
        else:
            console.print(f"\n[bold]{title}[/bold]\n  no signals", highlight=False)
    footer = (
        f"\nscanned {report.scanned} | inside {len(report.inside)} | "
        f"CIB {len(report.cib)} | failed {len(report.failures)}"
    )
    console.print(footer, highlight=False, markup=False)
    for pair, latest in report.outdated.items():
        seen = latest.isoformat() if latest else "no data"
        console.print(
            f"skipped {pair}: newest candle {seen}, not the latest closed day", markup=False
        )
    if report.stale:
        console.print(f"stale cache used: {', '.join(report.stale)}", markup=False)
    for pair, reason in report.failures.items():
        console.print(f"failed {pair}: {reason}", markup=False, style="yellow")


def main(
    argv: Sequence[str] | None = None,
    *,
    fetcher: data.Fetcher | None = None,
    console: Console | None = None,
    now: datetime | None = None,
) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.buffer_pips < 0:
        parser.error("--buffer-pips must be >= 0")
    try:
        pairs = parse_pairs(args.pairs)
    except ValueError as exc:
        parser.error(str(exc))

    out = console or Console(no_color=args.no_color)
    report = run_scan(
        pairs,
        buffer_pips=args.buffer_pips,
        db=args.db or config.db_path(),
        force=args.refresh,
        now=now,
        fetcher=fetcher,
    )
    render(report, out)
    unusable = len(report.failures) + len(report.outdated)
    return 1 if report.scanned and unusable == report.scanned else 0


if __name__ == "__main__":
    sys.exit(main())
