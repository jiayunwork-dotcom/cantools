# Quality-aware signal time series — delivery notes

## What was added

A new `cantools.quality` package plus two optional CLI entry points. The
existing decode return values and `cantools plot` behavior are unchanged
unless the new quality mode is explicitly enabled.

### New package: `src/cantools/quality/`

| Module | Responsibility |
| --- | --- |
| `types.py` | `SignalQuality`, `SignalKey`, `QualityPoint`, `Source`, `StalenessInfo`, `Diagnostic`, etc. |
| `logsource.py` | Forward-only, single-pass reader for absolute-time `candump -L` text; microsecond precision, line-numbered diagnostics, time-regression error. |
| `decoder.py` | Per-signal quality decoding built on the existing codec chain; distinguishes raw invalid, truncated bit field, inactive mux branch, frame-level failure. |
| `decoding.py` / `result.py` | The `with_quality=True` bridge for `Message.decode` / `Database.decode_message`. |
| `staleness.py` | Online, bounded staleness-limit estimator and `StalenessPolicy`. |
| `engine.py` | The streaming fixed-grid `QualityTimeSeries` (the single-pass state machine). |
| `selection.py` | Channel/frame-type/message/signal selectors with ambiguity checks. |
| `curves.py` | Groups detail points into drawable segments; never joins across an unavailable interval. |
| `csv_export.py` | Streaming long- and wide-format CSV writers. |
| `plot_integration.py` | `cantools plot --quality` glue (lazily imported, needs matplotlib). |

### New CLI

* `cantools export-quality DBC MSG.SIG ... --channel ... --grid-start/stop/step`
  streams the log (default stdin) to a long or `--wide` CSV.
* `cantools plot --quality --grid-start/stop/step ...` adds the optional
  quality plotting mode; legacy plotting is untouched without the flag.

## Quality model

Eight per-grid-point qualities: `unseen`, `fresh`, `held`, `expired`,
`invalid`, `branch_inactive`, `decode_failed`, `truncated`.

Key semantics implemented and tested:

* A value is `held` while age `<= limit`; age exactly equal to the limit
  is still usable; only strictly older ages expire.
* `unseen` has no source and is never zero-filled.
* A raw invalid code, a truncated bit field, an inactive mux branch or a
  frame decode failure **blocks** the previous good value; the engine
  never skips the blocking event to revive an older sample.
* Switching a multiplexer branch immediately deactivates the old branch;
  switching back requires a fresh valid sample.
* In a short payload, complete active signals are still decoded while
  incomplete ones are individually `truncated` and block their own old
  value.
* When the mux selector cannot be determined or the whole frame fails,
  every selected signal of that message is unavailable.
* `raw_invalid` is matched on the **raw** (unscaled) value and reported
  in `raw_value`; it never becomes a scaled/enumerated trusted reading.

## Staleness-limit policy (no look-ahead)

Resolution order at every grid point, using only evidence seen so far:

1. **Explicit** per-signal limit (finite positive seconds only); always
   wins.
2. **Database cycle** (`GenMsgCycleTime` / ARXML cycle), multiplied by a
   configurable `jitter_factor` (default 2.0), while observed evidence is
   still scarce.
3. **Observed history** once `min_evidence` intervals have arrived: the
   median of a bounded deque (default 16) of recent intervals times the
   jitter factor. The median tolerates occasional long gaps.
4. **Insufficient evidence** for event-driven messages (no cycle):
   provisional limit = largest observed gap × `event_multiplier`, or a
   `default_event_limit_s` when nothing has been seen. With
   `expire_without_evidence=False` the limit is `None` (never expires) —
   the documented trade-off: no false positives, but a stoppage of a
   rarely/never seen signal cannot be flagged.

Costs/trade-offs to be aware of:

* A single global timeout is deliberately avoided; jitter factor > 1
  delays real stoppage detection in exchange for avoiding false
  expiries during normal scheduling jitter.
* The provisional event limit is conservative by design; tighten
  `default_event_limit_s` / `event_multiplier` for quicker detection at
  the price of more false positives on quiet event buses.
* Every `StalenessInfo` carries a machine-readable `basis` and a human
  explanation, so reports distinguish explicit/database/observed/
  insufficient evidence.

## Streaming and memory

* The log is read strictly forward, exactly once; a one-frame lookahead
  bridges grid points, and frames after the last grid point are drained
  so diagnostics and rhythm evidence stay complete.
* Resident state is one small state object per selected signal, one
  bounded interval deque per distinct message identity, and the current
  output batch. No raw frames or past grid points are retained.
  Measured on a 100k-frame/3.2 MB log: peak RSS delta ≈ 128 KiB,
  3 signal states, interval history capped at the configured size.
* A file opened by `build_series_from_file` is owned by the series and
  released on normal completion, early `break` + `close()`, or the
  context manager.
* Plot is explicitly allowed to retain the final selected points it
  draws; the producing engine itself stays bounded.

## Identity handling

Signals are keyed by `(channel, frame_id, is_extended_frame, message,
signal)`. The same frame id on `can0`/`can1`, or a standard vs extended
frame, can never merge. An empty-channel selector acts as a wildcard
that materializes a separate per-channel state on first sight (no
pre-scan of the log).

## Verification

All fixtures are synthetic and local (no bus hardware):

* `tests/files/quality/quality_demo.dbc` — two-channel same-id frame,
  multiplexed message, short message, cyclic and event messages.
* `tests/files/quality/invalid_demo.arxml` — generated by
  `scripts/make_invalid_arxml.py`; carries scaled and enumerated signals
  with ARXML `INVALID-VALUE`.
* `tests/files/quality/demo_short.log` — generated by
  `scripts/make_demo_log.py`; hand-computable, covers same-timestamp
  line-order updates, both channels, mux switching, short payload,
  cross-limit stoppage, and all four diagnostic kinds.
* `tests/test_quality.py` — 27 tests including grid hand calculations,
  invalid-blocks-good recovery, CSV/curve consistency, policy causality
  and the bounded-memory check.
* `scripts/demo_staleness_policy.py` — prints the startup/jitter/event
  causal sequences and asserts the false-positive trade-offs.

### How to rerun

```sh
# environment used during development
python -m venv --without-pip .venv && . .venv/bin/activate
# bootstrap pip if the system python ships without ensurepip
python /tmp/get-pip.py
pip install -e '.[plot]' pytest freezegun parameterized

# full suite (C-code tests excluded; they need a toolchain)
python -m pytest tests/ --ignore=tests/test_c_code

# only the new feature
python -m pytest tests/test_quality.py -q

# policy walkthrough
python -I scripts/demo_staleness_policy.py

# CLI examples
cantools export-quality tests/files/quality/quality_demo.dbc \
    EngineStatus.EngineSpeed --channel can0 \
    --grid-start 1000 --grid-stop 1000.4 --grid-step 0.1 \
    --input tests/files/quality/demo_short.log

MPLBACKEND=Agg cantools plot --quality \
    --grid-start 1000 --grid-stop 1000.9 --grid-step 0.1 \
    --channel can0 --channel can1 -o /tmp/quality.png \
    tests/files/quality/quality_demo.dbc < tests/files/quality/demo_short.log
```

Result at delivery: **411 passed** (384 pre-existing + 27 new).
