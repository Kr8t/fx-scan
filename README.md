# FX SCAN

Scans 16 pairs (the 7 majors, the Yen crosses and the European crosses) on the **daily** timeframe (candles cut at **17:00 New York**, the same
close TradingView/OANDA use; built from hourly Yahoo data, change `SESSION_TZ` / `SESSION_CUTOFF_HOUR` in `config.py`) and prints two tables:

| Table | Rule (latest *completed* candle vs. the one before it) |
|---|---|
| **INSIDE DAY** | high < previous high **and** low > previous low |
| **CLOSED IN BREAKOUT (CIB)** | **close** > previous high, or **close** < previous low. A wick that closes back inside is ignored |

Results are split into two sections: **MAJORS** and **YEN & EUROPEAN CROSSES**, each with
its own Inside Day and CIB tables.
Rows are coloured by candle direction (green bull, red bear, dim doji), not by signal type.
Only the most recent closed day can produce a signal: a pair whose newest candle is older
(stale cache, holiday, missing bar) is skipped and listed under the tables.
A CIB on the latest bar suppresses the inside-day signal. Today's still-forming bar is never used.

## Run

Windows: double-click `fx_scan.bat` or run it from a terminal (it builds `.venv` on first use).

```
fx_scan.bat                      # all 16 pairs
fx_scan.bat EURUSD gbp/jpy       # specific pairs
fx_scan.bat --group majors       # only the 7 majors (or: --group crosses)
fx_scan.bat --buffer-pips 2      # candle must clear the level by 2 pips
fx_scan.bat --refresh            # ignore cache freshness
```

Other platforms: `pip install -r requirements.txt && python cli.py`.

## Data source: MetaTrader 5 (recommended) or Yahoo

```
fx_scan.bat                          # auto: MT5 if available, otherwise Yahoo
fx_scan.bat --source mt5             # require MT5 (error if it cannot connect)
fx_scan.bat --source yahoo           # force Yahoo
fx_scan.bat --mt5-suffix .a          # broker symbols like EURUSD.a (default: auto-detect)
fx_scan.bat --mt5-path "C:\\Program Files\\MetaTrader 5\\terminal64.exe"
```

With MT5 the daily candles are your broker's own D1 bars (identical to your charts) and the
Broke column / watch mode read today's forming D1 bar in real time, so `--interval` can go
down to 5 seconds. The launcher installs the optional `MetaTrader5` package (Windows only).
Requirements: the MT5 terminal installed and logged in (it is started automatically if
needed) and the pairs available at your broker. MT5 and Yahoo keep separate caches.

Assumption: your broker's D1 bar follows the usual FX day (opens 17:00 New York, i.e. server
midnight on a GMT+2/+3 server). If a pair's Prev High/Low differ from your chart, tell me
your broker's server time zone.

## The Broke column

Both tables end with a **Broke** column showing whether today's session (17:00 NY to
17:00 NY) has already traded beyond the previous day's high or low: `● HIGH`, `● LOW`
or `● BOTH` if it has, `○` if not yet, `-` if there is no live data (market closed or
Yahoo unavailable).

## Watch mode (intraday alerts)

```
fx_scan.bat --watch                    # scan, then watch every matched pair
fx_scan.bat --watch --ntfy my-secret-topic   # also push to your phone (ntfy app)
fx_scan.bat --watch --interval 30 --buffer-pips 1
```

Every pair that shows up as an Inside Day or CIB is watched. When the current session
(17:00 NY to 17:00 NY) trades beyond the previous day's high or low you get a console
alert, three rising beeps (`--no-beep` to silence; one beep per check even if several
pairs break), the tables redrawn with the Broke dot updated, a running **BREAKS TODAY**
list at the bottom (time, pair, side) so you can see which pair beeped when you get back,
a Windows notification (`--no-toast` to disable) and, with `--ntfy`, a phone push.
Each pair/side alerts once per session; when a new session closes the scan re-runs.
Prices are Yahoo 5-minute bars, so expect a short delay; a spike between polls is still
caught because the session high/low is read from the bars. ntfy topics are public by
name, so pick a long random one.

## Confirm on close (`--confirm`)

```
fx_scan.bat --watch --confirm 15m      # alert only when a 15-minute candle CLOSES beyond the level
fx_scan.bat --watch --confirm 1h       # choices: 5m, 15m, 30m, 1h, 4h
```

Without `--confirm` an alert fires as soon as price trades beyond yesterday's high/low, wick
included. With it, wick-only pokes stay silent until a completed candle of that size closes
beyond the level (closing exactly on the level does not count; `--buffer-pips` applies to the
close too). The Broke column then shows `●` for a confirmed close, `◐` for a wick only and
`○` for nothing yet. Alerts, the beep and the BREAKS TODAY list all use the confirmed close.

Candles come from MT5's own M5/M15/M30/H1/H4 bars when MT5 is the source; with Yahoo they are
built from 5-minute bars, anchored at the 17:00 New York session open so 1h/4h candles line up
with a New-York-close broker.

## Pip buffer

`--buffer-pips N` is a margin the bar must clear. Breakouts must close N pips beyond the
previous high/low; inside days must sit N pips inside the previous range. Pip size is 0.01 for
JPY pairs, 0.0001 otherwise.

## Cache (SQLite, `~/.fx_scan/cache.sqlite`, override with `FX_SCAN_DB` or `--db`)

A pair is served from cache when it has at least 5 bars and either:
1. its newest bar is on/after the last completed weekday (yesterday UTC, rolled back over
   Sat/Sun, so weekends and Monday morning reuse Friday's bar), or
2. we already fetched in the last 30 minutes (stops refetch loops on holidays/data lag).

Caches written by older versions are wiped automatically (`SCHEMA_VERSION`).
Network failure falls back to stale cache with a warning. Only completed bars are stored.

## Dev

```
pip install -r requirements-dev.txt
ruff check . && ruff format --check . && mypy && pytest
```

Exit codes: 0 ok, 1 every pair failed, 2 bad arguments.
