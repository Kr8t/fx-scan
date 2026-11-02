# FX SCAN

Scans 18 pairs (the 7 majors, the Yen crosses and the EUR crosses) on the **daily** timeframe (candles cut at **17:00 New York**, the same
close TradingView/OANDA use; built from hourly Yahoo data, change `SESSION_TZ` / `SESSION_CUTOFF_HOUR` in `config.py`) and prints two tables:

| Table | Rule (latest *completed* candle vs. the one before it) |
|---|---|
| **INSIDE DAY** | high < previous high **and** low > previous low |
| **CLOSED IN BREAKOUT (CIB)** | **close** > previous high, or **close** < previous low. A wick that closes back inside is ignored |

Rows are coloured by candle direction (green bull, red bear, dim doji), not by signal type.
Only the most recent closed day can produce a signal: a pair whose newest candle is older
(stale cache, holiday, missing bar) is skipped and listed under the tables.
A CIB on the latest bar suppresses the inside-day signal. Today's still-forming bar is never used.

## Run

Windows: double-click `fx_scan.bat` or run it from a terminal (it builds `.venv` on first use).

```
fx_scan.bat                      # all 18 pairs
fx_scan.bat EURUSD gbp/jpy       # specific pairs
fx_scan.bat --buffer-pips 2      # candle must clear the level by 2 pips
fx_scan.bat --refresh            # ignore cache freshness
```

Other platforms: `pip install -r requirements.txt && python cli.py`.

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
