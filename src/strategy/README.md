# Strategies

Every signal Crossline records comes from a registered strategy version, identified as `key@version`.
The scheduled scan, the outcome evaluator and the backtest harness all run the same two methods of
that version, so a live number and a backtest number mean the same thing.

| Piece | File |
|---|---|
| Interface: `Strategy`, `StrategyParams`, `SignalIntent`, `ResolveContext` | `base.py` |
| As-of market context (closed candles only, per timeframe) | `context.py` |
| Registry, lock file, the frozen-version rule | `registry.py`, `versions.lock.json` |
| `macd_cross@2.2.0` (the production rules, status `live`) | `macd_cross.py` (math in `macd.py`, `outcome.py`) |
| `macd_cross_ema50d_oppexit@1.0.0` (pre-registered candidate, status `shadow`) | `macd_cross_ema50d_oppexit.py` |
| Exit labels of strategies without a target, and their cost-model fill | `exits.py` |
| CLI: `list`, `verify`, `freeze`, `rehash` | `__main__.py` |

## The interface

- `key`, `version`, `name`, `status` (`live`, `shadow` or `research`), `timeframe` (`"2h"`),
  `context_timeframes` (for example `("1d",)`), `context_bars`.
- `params`: a frozen pydantic model. Unknown fields are rejected and nothing can be assigned after
  construction.
- `warmup_bars`, `history_bars` (klines fetched live), `max_hold_bars` (the longest a trade can stay
  open), and `resolve_history_bars` (history the exit rule needs; 0 for `macd_cross`).
- `detect(ctx) -> list[SignalIntent]` decides on closed candles only. It is pure: no clock, no I/O.
  `ctx.next_open` is the open of the candle that starts at the decision instant, which is the first
  price anyone can trade.
- `resolve(signal, candles, ctx) -> TradeOutcome | None` settles a signal from the candles after its
  decision candle. Exit rules belong to the strategy, so two strategies can exit differently.
- `params_hash` is the SHA-256 of the canonical JSON of the params (sorted keys, no whitespace).
  `code_hash` is the SHA-256 of the source of the strategy's module plus the modules it lists in
  `code_modules`, with line endings normalized. For `macd_cross` that means `macd_cross.py`,
  `macd.py` and `outcome.py`, so a change to the MACD or exit math also changes its hash.

## Market context and look-ahead

`MarketContext` holds klines per timeframe and a `decision_ms`. A candle counts as closed once
`open_time_ms + interval <= decision_ms`. `closed()` and `asof()` never return a later candle, even
when asked for a later instant. Every comparison uses integer `open_time_ms`. pandas 3 parses epoch
milliseconds as `datetime64[ms]`, where pandas 2 used `[ns]`, so code that casts a timestamp column
to int64 and assumes nanoseconds can be off by 10^6 and let future candles through. `normalize_klines`
converts through `.dt.as_unit("ms")`, and the tests check every resolution.

- **Live:** `decision_ms` is the latest boundary of the strategy's timeframe at or before the run
  time. The forming candle contributes only its open; `bars_with_entry()` sets its high, low and
  close to that open.
- **Replay:** the decision is made after each closed candle, with the next candle's open as the
  entry price.

## Frozen versions

A version is frozen when `versions.lock.json` records its params, `params_hash`, `code_hash` and
`frozen_at`. On boot the registry is mirrored into `strategies` and `strategy_versions`:

- A new version is inserted, frozen with the lock's hashes when the lock has them.
- A frozen row never changes its hashes. The one exception is a `code_hash` listed under the lock
  entry's `code_hash_history` (see `rehash` below).
- Each row's `status` follows the code.

The scheduled scan emits only for a `live` or `shadow` version whose stored row is frozen and whose
hashes equal those of the running code. Any other version is logged as an error, skipped, and listed
under `cron.strategies.refused` in `/api/cron/status`. Without a database, the lock file stands in
for the stored rows.

`tests/test_strategy_framework.py::test_every_live_and_shadow_strategy_is_frozen_and_matches_the_lock`
fails on any drift. A change to a frozen strategy's code or parameters therefore fails CI before the
engine starts refusing it in production. To ship such a change:

- **Different rules or parameters:** register a new version (for example `macd_cross@2.3.0`), run the
  backtest, then freeze it:

  ```bash
  venv/bin/python -m src.strategy freeze macd_cross@2.3.0 --notes "why, and the trial that justified it"
  ```

- **Refactor that provably changes nothing:** the backtest gate must still pass byte-identically, and
  `tests/test_replay_fingerprints.py` (the trade list of every locked version on the committed
  fixture, the shadow version included, which has no CI gate of its own) must pass unchanged. `rehash`
  itself does not check this; CI does. Then:

  ```bash
  venv/bin/python -m src.strategy rehash macd_cross@2.2.0 --reason "what changed"
  ```

  This keeps the old hash under `code_hash_history`, and the boot sync moves the stored row to the
  new hash with a note. Parameters can never be rehashed.

`venv/bin/python -m src.strategy list` shows every version and whether it matches the lock.
`verify` exits 1 on any problem.

## Live, shadow, research

| Status | Scheduled scan | Telegram | Track record |
|---|---|---|---|
| `live` | detects, stores, evaluates | new signals and closed trades | the headline when it is `STRATEGY_KEY@STRATEGY_VERSION` |
| `shadow` | detects, stores, evaluates | only with `SHADOW_ALERTS=true`, labelled as shadow | its own version only; excluded from the headline as `other_version` |
| `research` | not run | none | backtest only |

Signals are stored in `macd_signals` with their own `strategy_key` and `strategy_version`. The unique
key is now `(strategy_key, strategy_version, symbol, side, timeframe, cross_candle_ms)`, so a shadow
strategy can record the same candle as the live one. Redis claims for any version other than the
headline one are scoped as `sig:{key@version}:...`. The evaluator settles each pending row with the
`resolve()` of its own version. Rows whose version is not registered (for example `legacy`) are
settled by `macd_cross@2.2.0`, exactly as before.

The live scan fetches each timeframe once per symbol per run, sized for the strategy that needs the
most bars (a `1d` context fetch is `max(context_bars + 1, 35)` klines). Each strategy is then given
only the window it asked for, so its indicators match a run in which it is alone.

## The shadow candidate: `macd_cross_ema50d_oppexit@1.0.0`

The one variant the institutional review found worth paper trading, frozen and pre-registered
before its holdout test (`docs/research/preregistration-macd_cross_ema50d_oppexit-1.0.0.md`):

- **Entry:** the production MACD(12,26,9) cross on a closed 2h candle, entered at the next open,
  only when it agrees with the daily trend. The trend is the newest UTC daily candle closed by the
  close of the cross candle: its close above the EMA50 of daily closes allows LONG, below allows
  SHORT. The EMA runs over exactly the last 200 closed days, SMA-seeded like `compute_macd`. With
  fewer than 200 closed days there is no signal.
- **Stop:** the production swing stop. There is no target (`tp1 = 0`).
- **Exit:** the stop, checked first, from the entry candle on (`STOPPED`); else the open of the
  candle after the first opposite MACD cross (`OPPOSITE_CROSS`); else the close of the 96th candle
  (`TIME_EXIT`).
- **Costs:** `cm1`. A stop pays 1.5x slippage. An opposite-cross or time exit is a market fill.
  The ledger stores these labels as the trade's `result` and `win_status`.

Its exit reads MACD history, so `resolve_history_bars` is 498. The evaluator fetches the 498
candles before the cross candle, the cross candle and the trade's candles in one request.
`tests/test_shadow_strategy.py` checks three things on the fixture:

- the evaluator books every trade on the same candle as the harness;
- the cron path detects exactly the harness's signals;
- both match an independent full-series implementation.

Its `code_hash` covers its own module and every `src.strategy` module it imports (`base`,
`context`, `exits`, `macd`, `macd_cross`, `outcome`). An edit to any of them fails the lock test
until the version is renumbered, or rehashed after a proven no-op.
