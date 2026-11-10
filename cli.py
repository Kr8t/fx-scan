"""FX SCAN command line interface."""

from __future__ import annotations

import argparse
import os
import sys
import time
from collections.abc import Callable, Mapping, Sequence
from collections.abc import Set as AbstractSet
from contextlib import closing
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from pathlib import Path

from rich import box
from rich.console import Console
from rich.table import Table
from rich.text import Text

import cache
import config
import data
import mt5feed
import notify
import strategy
from strategy import Direction, LevelBreak, Signal

ROW_STYLES: dict[Direction, str] = {"bull": "green", "bear": "red", "neutral": "dim white"}
LEFT_COLUMNS = {"Pair", "Date", "Candle", "Broke"}
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
    breaks: dict[str, list[LevelBreak] | None] = field(default_factory=dict)
    as_of: date | None = None
    scanned: int = 0
    pairs: list[str] = field(default_factory=list)
    source: str = ""
    confirm_label: str = ""


BreakMap = Mapping[str, "list[LevelBreak] | None"]


def _sides(levels: AbstractSet[str]) -> str:
    return "BOTH" if len(levels) == 2 else next(iter(levels))


def break_cell(breaks: list[LevelBreak] | None) -> Text:
    """Dot column: ● broke (closed beyond) the previous day's high/low today, ◐ wick only,
    ○ not yet, - no live data. Without --confirm every touch counts as ●."""
    if breaks is None:
        return Text("-", style="dim")
    if not breaks:
        return Text("○", style="dim")
    closed = {b.level for b in breaks if b.confirmed}
    wick = {b.level for b in breaks if not b.confirmed}
    parts = []
    if closed:
        parts.append(f"● {_sides(closed)}")
    if wick:
        parts.append(f"◐ {_sides(wick)}")
    return Text(" ".join(parts), style="bold yellow" if closed else "yellow")


def current_breaks(
    signal: Signal,
    now: datetime,
    live_fetcher: data.Fetcher,
    buffer_pips: float,
    *,
    confirm: int | None = None,
    closes_fetcher: data.ClosesFetcher | None = None,
) -> list[LevelBreak] | None:
    """Previous-day levels today's session has traded beyond (None = no live data).

    With ``confirm`` (candle minutes) each touched level is marked confirmed only if a
    completed candle of that size closed beyond it.
    """
    try:
        session = data.current_session_range(live_fetcher(signal.pair), now)
    except Exception:  # feed error: treat as unknown rather than failing the scan
        return None
    if session is None:
        return None
    buffer = buffer_pips * config.pip_size(signal.pair)
    touched = strategy.detect_breaks(
        signal.pair,
        signal.bar.high,
        signal.bar.low,
        session.high,
        session.low,
        buffer=buffer,
    )
    if confirm is None or not touched:
        return touched
    fetch = closes_fetcher or data.closes_via(live_fetcher)
    try:
        closes = fetch(signal.pair, confirm, now) or []
    except Exception:  # no candle data: the touch stays unconfirmed
        closes = []
    return [strategy.confirm_break(brk, closes, buffer) for brk in touched]


def row_style(direction: Direction) -> str:
    """Rows are coloured by candle direction, never by signal type."""
    return ROW_STYLES[direction]


def parse_pairs(values: Sequence[str] | None, group: str | None = None) -> list[str]:
    """Normalise user pair input (``eur/usd`` -> ``EURUSD``) and validate it.

    Explicit pairs win; otherwise ``group`` (``majors``/``crosses``) or everything.
    """
    if not values:
        return list(config.GROUP_KEYS[group]) if group else list(config.PAIRS)
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
        "-g",
        "--group",
        choices=sorted(config.GROUP_KEYS),
        help="scan only the majors or only the Yen/European crosses",
    )
    parser.add_argument(
        "-s",
        "--source",
        choices=["auto", "mt5", "yahoo"],
        default="auto",
        help="data source: your MT5 terminal, Yahoo, or auto (MT5 if available) (default: auto)",
    )
    parser.add_argument(
        "--mt5-suffix",
        default=os.environ.get("FX_SCAN_MT5_SUFFIX", ""),
        help="broker symbol suffix, e.g. .a or m (default: auto-detect)",
    )
    parser.add_argument("--mt5-path", help="path to terminal64.exe if you have several MT5s")
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
        "-w",
        "--watch",
        action="store_true",
        help="keep running and alert when a matched pair trades beyond the previous day's high/low",
    )
    parser.add_argument(
        "--interval",
        type=int,
        default=60,
        help="seconds between watch checks (default: 60; minimum 15 for Yahoo, 5 for MT5)",
    )
    parser.add_argument("--ntfy", metavar="TOPIC", help="also push alerts to ntfy.sh/TOPIC")
    parser.add_argument("--no-toast", action="store_true", help="no Windows desktop notification")
    parser.add_argument("--no-beep", action="store_true", help="no beep when a level breaks")
    parser.add_argument(
        "-c",
        "--confirm",
        choices=list(config.CONFIRM_FRAMES),
        help="alert only when a candle of this size CLOSES beyond the level (wicks do not count)",
    )
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
    live_fetcher: data.Fetcher | None = None,
    source: str = "",
    confirm: int | None = None,
    closes_fetcher: data.ClosesFetcher | None = None,
) -> ScanReport:
    """Fetch candles for every pair, collect signals and check today's level breaks."""
    live = live_fetcher or data.yf_intraday
    moment = now or datetime.now(timezone.utc)
    closed_day = cache.expected_last_complete(moment)
    report = ScanReport(
        as_of=closed_day,
        pairs=list(pairs),
        source=source,
        confirm_label=config.confirm_label(confirm) if confirm else "",
    )
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
            report.breaks[pair] = current_breaks(
                signal,
                moment,
                live,
                buffer_pips,
                confirm=confirm,
                closes_fetcher=closes_fetcher,
            )
    return report


def _price(pair: str, value: float) -> str:
    return f"{value:.{config.price_decimals(pair)}f}"


def build_inside_table(signals: Sequence[Signal], breaks: BreakMap | None = None) -> Table:
    table = Table(title="INSIDE DAY", box=box.SIMPLE_HEAVY, title_style="bold")
    for name in ("Pair", "Date", "Candle", "Range %", "Broke"):
        table.add_column(name, justify="left" if name in LEFT_COLUMNS else "right")
    for s in signals:
        table.add_row(
            config.display_pair(s.pair),
            s.bar.day.isoformat(),
            CANDLE_LABELS[s.direction],
            f"{s.range_ratio * 100:.0f}%",
            break_cell((breaks or {}).get(s.pair)),
            style=row_style(s.direction),
        )
    return table


def build_cib_table(signals: Sequence[Signal], breaks: BreakMap | None = None) -> Table:
    """Pips is signed: + closed above the previous high, - closed below the previous low."""
    table = Table(title="CLOSED IN BREAKOUT (CIB)", box=box.SIMPLE_HEAVY, title_style="bold")
    for name in ("Pair", "Date", "Candle", "Pips", "Broke"):
        table.add_column(name, justify="left" if name in LEFT_COLUMNS else "right")
    for s in signals:
        pips = s.break_distance / config.pip_size(s.pair)
        table.add_row(
            config.display_pair(s.pair),
            s.bar.day.isoformat(),
            CANDLE_LABELS[s.direction],
            f"{pips if s.side == 'UP' else -pips:+.1f}",
            break_cell((breaks or {}).get(s.pair)),
            style=row_style(s.direction),
        )
    return table


def _legend(report: ScanReport) -> str:
    if report.confirm_label:
        return (
            f"Broke: ● a {report.confirm_label} candle closed beyond the previous day's high/low, "
            "◐ wick only (no close yet), ○ not yet, - no live data"
        )
    return "Broke: ● traded beyond the previous day's high/low today, ○ not yet, - no live data"


def render(report: ScanReport, console: Console) -> None:
    """Print both tables (or an empty-state line) plus a footer."""
    closed = report.as_of.isoformat() if report.as_of else "n/a"
    console.print(
        f"[bold]{config.APP_NAME}[/bold]  most recent closed day: {closed} "
        f"(daily close {config.SESSION_CUTOFF_HOUR}:00 {config.SESSION_TZ})"
        + (f"  | source: {report.source}" if report.source else "")
        + (f"  | alerts on {report.confirm_label} closes" if report.confirm_label else ""),
        highlight=False,
    )
    for group_name, members in config.GROUPS:
        if not any(pair in report.pairs for pair in members):
            continue  # nothing from this group was scanned
        console.rule(f"[bold cyan]{group_name}[/bold cyan]", style="cyan")
        for title, signals, builder in (
            ("INSIDE DAY", report.inside, build_inside_table),
            ("CLOSED IN BREAKOUT (CIB)", report.cib, build_cib_table),
        ):
            in_group = [sig for sig in signals if sig.pair in members]
            if in_group:
                console.print(builder(in_group, report.breaks))
            else:
                console.print(f"\n[bold]{title}[/bold]\n  no signals", highlight=False)
    if report.inside or report.cib:
        console.print(
            _legend(report),
            highlight=False,
            markup=False,
            style="dim",
        )
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


def format_alert(
    signal: Signal, brk: LevelBreak, confirm_label: str | None = None
) -> tuple[str, str]:
    """Plain-ASCII (title, body) so it is safe in HTTP headers and toasts."""
    pair = config.display_pair(signal.pair)
    side = "high" if brk.level == "HIGH" else "low"
    setup = "INSIDE DAY" if signal.kind == "INSIDE" else f"CIB {signal.side}"
    context = f"Yesterday: {setup}, {signal.direction} candle."
    if confirm_label and brk.confirmed_close is not None:
        pips = brk.close_distance / config.pip_size(signal.pair)
        title = f"{pair} {confirm_label} close beyond previous day {side}"
        body = (
            f"{confirm_label} candle closed at {_price(signal.pair, brk.confirmed_close)}, "
            f"{pips:.1f} pips beyond {_price(signal.pair, brk.level_price)}. {context}"
        )
        return title, body
    pips = brk.distance / config.pip_size(signal.pair)
    title = f"{pair} broke previous day {side}"
    body = (
        f"{_price(signal.pair, brk.level_price)} taken out, "
        f"extreme {_price(signal.pair, brk.extreme)} (+{pips:.1f} pips). {context}"
    )
    return title, body


def watch_once(
    items: Sequence[Signal],
    alerted: set[tuple[str, str]],
    now: datetime,
    live_fetcher: data.Fetcher,
    buffer_pips: float,
    *,
    confirm: int | None = None,
    closes_fetcher: data.ClosesFetcher | None = None,
) -> list[tuple[Signal, LevelBreak]]:
    """One polling pass. Returns breaks not alerted before (and records them).

    With ``confirm`` only levels a completed candle has closed beyond alert; wick-only
    touches stay silent until a close confirms them.
    """
    fresh: list[tuple[Signal, LevelBreak]] = []
    for signal in items:
        breaks = current_breaks(
            signal, now, live_fetcher, buffer_pips, confirm=confirm, closes_fetcher=closes_fetcher
        )
        for brk in breaks or []:
            key = (signal.pair, brk.level)
            if brk.confirmed and key not in alerted:
                alerted.add(key)
                fresh.append((signal, brk))
    return fresh


def _deliver(
    title: str, body: str, *, console: Console, toast: bool, ntfy: str | None, stamp: str
) -> None:
    console.print(f"[bold yellow]{stamp}  {title}[/bold yellow]  {body}", highlight=False)
    if toast:
        notify.send_desktop(title, body)
    if ntfy:
        notify.send_ntfy(ntfy, title, body)


def render_break_log(log: Sequence[str], console: Console) -> None:
    """Running list of today's breaks, so an alert heard from afar can be traced."""
    if not log:
        return
    console.print("\nBREAKS TODAY", style="bold yellow", highlight=False)
    for line in log:
        console.print(f"  {line}", highlight=False, markup=False)


def run_watch(
    pairs: Sequence[str],
    *,
    buffer_pips: float,
    db: Path | str,
    interval: int,
    console: Console,
    toast: bool = True,
    ntfy: str | None = None,
    force: bool = False,
    fetcher: data.Fetcher | None = None,
    live_fetcher: data.Fetcher | None = None,
    clock: Callable[[], datetime] | None = None,
    sleep: Callable[[float], None] = time.sleep,
    max_cycles: int | None = None,
    beeper: Callable[[], None] | None = None,
    source: str = "",
    confirm: int | None = None,
    closes_fetcher: data.ClosesFetcher | None = None,
) -> int:
    """Scan, then poll today's range for every matched pair until Ctrl+C."""
    now_fn = clock or (lambda: datetime.now(timezone.utc))
    live = live_fetcher or data.yf_intraday

    def scan() -> ScanReport:
        report = run_scan(
            pairs,
            buffer_pips=buffer_pips,
            db=db,
            force=force,
            now=now_fn(),
            fetcher=fetcher,
            live_fetcher=live,
            source=source,
            confirm=confirm,
            closes_fetcher=closes_fetcher,
        )
        render(report, console)
        return report

    report = scan()
    label = config.confirm_label(confirm) if confirm else None
    alerted: set[tuple[str, str]] = set()
    log: list[str] = []
    cycles = 0
    try:
        while max_cycles is None or cycles < max_cycles:
            now = now_fn()
            if cache.expected_last_complete(now) != report.as_of:  # a new session has closed
                report = scan()
                alerted.clear()
                log.clear()
            items = [*report.inside, *report.cib]
            if cycles == 0:
                names = ", ".join(config.display_pair(s.pair) for s in items) or "nothing"
                console.print(
                    f"\nWatching {len(items)} pair(s) every {interval}s: {names}. Ctrl+C to stop.",
                    highlight=False,
                    markup=False,
                )
            fresh = watch_once(
                items,
                alerted,
                now,
                live,
                buffer_pips,
                confirm=confirm,
                closes_fetcher=closes_fetcher,
            )
            stamp = datetime.now().strftime("%H:%M:%S")  # local clock, display only
            for signal, brk in fresh:
                title, body = format_alert(signal, brk, label)
                _deliver(title, body, console=console, toast=toast, ntfy=ntfy, stamp=stamp)
                earlier = [b for b in report.breaks.get(signal.pair) or [] if b.level != brk.level]
                report.breaks[signal.pair] = [*earlier, brk]  # a confirmed close replaces a wick
                side = "high" if brk.level == "HIGH" else "low"
                name = config.display_pair(signal.pair)
                if label and brk.confirmed_close is not None:
                    price = _price(signal.pair, brk.confirmed_close)
                    log.append(f"{stamp}  {name}  {side} {label} close beyond ({price})")
                else:
                    price = _price(signal.pair, brk.extreme)
                    log.append(f"{stamp}  {name}  {side} taken out ({price})")
            if fresh:
                if beeper:
                    beeper()
                render(report, console)  # tables again, with the Broke dots updated
                render_break_log(log, console)
            cycles += 1
            if max_cycles is None or cycles < max_cycles:
                sleep(interval)
    except KeyboardInterrupt:
        console.print("\nwatch stopped", markup=False)
    return 0


def _noop() -> None:
    return None


@dataclass
class DataSource:
    """Where candles and live ranges come from, plus how to shut it down."""

    name: str
    fetcher: data.Fetcher | None = None
    live_fetcher: data.Fetcher | None = None
    closes: data.ClosesFetcher | None = None
    close: Callable[[], None] = _noop
    note: str = ""
    always_refresh: bool = False
    min_interval: int = config.YAHOO_MIN_INTERVAL
    db_key: str = "yahoo"


def select_source(choice: str, *, suffix: str = "", path: str | None = None) -> DataSource:
    """Pick MT5 or Yahoo. ``auto`` tries MT5 and falls back to Yahoo with a note."""
    if choice in {"auto", "mt5"}:
        try:
            mt5 = mt5feed.connect(suffix, path)
        except mt5feed.Mt5Error as exc:
            if choice == "mt5":
                raise
            return DataSource("Yahoo", note=f"MT5 not used ({exc}); using Yahoo data")
        return DataSource(
            "MT5",
            fetcher=mt5.daily,
            live_fetcher=mt5.live,
            closes=mt5.closes,
            close=mt5.close,
            always_refresh=True,  # local and instant: always re-read the broker's candles
            min_interval=config.MT5_MIN_INTERVAL,
            db_key="mt5",
        )
    return DataSource("Yahoo")


def main(
    argv: Sequence[str] | None = None,
    *,
    fetcher: data.Fetcher | None = None,
    console: Console | None = None,
    now: datetime | None = None,
    live_fetcher: data.Fetcher | None = None,
    sleep: Callable[[float], None] = time.sleep,
    max_cycles: int | None = None,
    beeper: Callable[[], None] | None = None,
) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.buffer_pips < 0:
        parser.error("--buffer-pips must be >= 0")
    try:
        pairs = parse_pairs(args.pairs, args.group)
    except ValueError as exc:
        parser.error(str(exc))

    out = console or Console(no_color=args.no_color)
    if fetcher or live_fetcher:
        source = DataSource("", fetcher, live_fetcher)  # feeds injected by tests
    else:
        try:
            source = select_source(args.source, suffix=args.mt5_suffix, path=args.mt5_path)
        except mt5feed.Mt5Error as exc:
            parser.error(str(exc))

    try:
        if args.interval < source.min_interval:
            parser.error(f"--interval must be at least {source.min_interval} seconds")
        if source.note:
            out.print(source.note, markup=False, style="yellow")
        db = args.db or config.db_path(source.db_key)
        force = args.refresh or source.always_refresh
        confirm = config.CONFIRM_FRAMES[args.confirm] if args.confirm else None
        if args.watch:
            return run_watch(
                pairs,
                buffer_pips=args.buffer_pips,
                db=db,
                interval=args.interval,
                console=out,
                toast=not args.no_toast,
                ntfy=args.ntfy,
                force=force,
                fetcher=source.fetcher,
                live_fetcher=source.live_fetcher,
                clock=(lambda: now) if now else None,
                sleep=sleep,
                max_cycles=max_cycles,
                beeper=None if args.no_beep else (beeper or notify.beep),
                source=source.name,
                confirm=confirm,
                closes_fetcher=source.closes,
            )
        report = run_scan(
            pairs,
            buffer_pips=args.buffer_pips,
            db=db,
            force=force,
            now=now,
            fetcher=source.fetcher,
            live_fetcher=source.live_fetcher,
            source=source.name,
            confirm=confirm,
            closes_fetcher=source.closes,
        )
        render(report, out)
        unusable = len(report.failures) + len(report.outdated)
        return 1 if report.scanned and unusable == report.scanned else 0
    finally:
        source.close()


if __name__ == "__main__":
    sys.exit(main())
