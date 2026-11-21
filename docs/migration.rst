Database migration
==================

.. note::

   The migration feature moves **physical quantities** (numeric
   signals) and **complete labels** (enumerated signals) between two
   database layouts. It works with ordinary integer-raw CAN and CAN FD
   messages, including nested multiplexers. Container messages and
   floating-point bit fields are not supported.

Compiling a plan
----------------

A plan is compiled from the *source* database (the one matching the
historical log) against a *target* database (the new layout)::

    plan = source_db.compile_migration(target_db,
                                       mapping,
                                       max_abs_error=0.5)

The returned :class:`~cantools.database.MigrationPlan` is reusable for
arbitrarily many frames and is independent of later changes to either
database: the plan keeps deep copies of the involved messages, so
editing a signal and calling ``refresh()`` cannot change the behavior
of an already compiled plan, and transcoding never mutates the
databases.

The mapping is a JSON-serializable list of message mappings. Each
entry explicitly names both frame identities and a one-to-one
correspondence between source and target signal names::

    [
      {
        "source": {
          "frame_id": 256,
          "is_extended_frame": false
        },
        "target": {
          "frame_id": 512,
          "is_extended_frame": true
        },
        "signals": [
          {"source_signal": "VehicleSpeed_old",
           "target_signal": "VehicleSpeed"},
          {"source_signal": "Gear_old",
           "target_signal": "Gear"}
        ]
      }
    ]

Mapping entry fields
^^^^^^^^^^^^^^^^^^^^

``source`` / ``target`` (objects, required)
    Each holds ``frame_id`` (integer) and ``is_extended_frame``
    (boolean). The frame id and the extended flag together identify
    the message, so a standard and an extended frame sharing the same
    numeric id remain distinct.

``signals`` (list, required)
    Each item holds ``source_signal`` and ``target_signal`` (strings).
    The correspondence is one-to-one: a source signal may appear at
    most once and a target signal may receive at most one source.
    Unused source fields may be omitted. Every signal active in a
    reachable target branch (including multiplexer selectors) must
    have a source.

What may change
^^^^^^^^^^^^^^^

* message and signal names;
* frame ids, bit positions, byte order, signedness and linear scaling
  (scale and offset);
* multiplexer branch numbers (they are paired through the mapped
  active signals; selector signals themselves are part of the
  mapping);
* payload length.

What must match
^^^^^^^^^^^^^^^

* the unit string of every mapped signal pair (``None``/unset counts
  as a unit and must match on both sides);
* the CAN/CAN FD format of the two messages (``is_fd`` must agree);
* for enumerated pairs, the *set of labels* available on the source
  must exist on the target (the raw codes may differ). An enumerated
  signal cannot be paired with a numeric one, and duplicate labels are
  rejected as ambiguous.

Compile-time errors
^^^^^^^^^^^^^^^^^^^

The following are reported when the plan is built (before any log line
is consumed), each message identifying the offending mapping entry
and signal index:

* references to unknown messages or signals;
* a source signal mapped twice or a target signal with two sources;
* incompatible units, enum/numeric kind mismatches, missing or
  ambiguous enum labels;
* CAN/CAN FD format mismatches;
* container or floating-point signals;
* target signals (including multiplexer branches) that have no source,
  i.e. insufficient branch coverage;
* a multiplexer selector that is not itself mapped, or whose target is
  not a selector in the target message.

Transcoding frames
------------------

:meth:`MigrationPlan.transcode` takes a
:class:`~cantools.logreader.DataFrame` and returns a new target
``DataFrame``. Channel and timestamp (including microsecond precision
and same-timestamp ordering) are preserved; the frame identity, FD/BRS
flags and payload are taken from the target definition.

Frames are rejected as a whole (no partial values are ever emitted)
when:

* the frame id/extended combination has no mapping (unknown message);
* it is a remote request;
* its FD flag disagrees with the source message definition;
* the payload is shorter than the message, decoding fails, a
  multiplexer selector carries an undefined value, or an active signal
  carries its ``raw_invalid`` sentinel;
* a value cannot be represented on the target within the error budget.

:class:`~cantools.database.MigrationError` carries ``frame_id``,
``is_extended_frame``, ``channel`` and (when used by the command line)
``line_number`` for locating the offending entry.

The output frame is re-decoded inside ``transcode`` and the physical
error, enum labels and the selected multiplexer branch are verified
again, so the error guarantee is enforced on the actual produced bytes
rather than only promised in documentation.

Quantization policy
-------------------

Numeric migration works in physical units. For each target numeric
signal the plan chooses an integer raw code under this deterministic
policy (implemented once in
:mod:`cantools.database.representability` and shared with the regular
encoding API):

1. The ideal raw value ``(physical - offset) / scale`` is computed
   with exact rational arithmetic (``fractions.Fraction``). A
   ``float`` input is converted through its exact binary value, so an
   over-tolerance physical input can never be hidden by rounding
   before the check.
2. Candidate codes are those fitting the bit field (respecting
   signedness), satisfying the declared ``minimum``/``maximum`` and
   excluding the signal's ``raw_invalid`` sentinel.
3. The candidate with the smallest absolute physical error wins. An
   exact tie (the ideal point is the midpoint between two legal codes)
   is broken toward the **smaller raw code** -- the lower physical
   grid point for positive scales, the upper one for negative scales.
4. If no candidate exists, or the best error exceeds
   ``max_abs_error`` (a per-signal budget measured in the signal's own
   physical unit, finite and non-negative), encoding fails. A zero
   budget therefore requires an exactly representable grid point.

If the ideal grid point is itself occupied by ``raw_invalid`` (or lies
outside the declared range), the search walks outward to the nearest
legal neighbor on each side; the neighbor is only accepted when its
error is within the budget, otherwise the frame is rejected.

Consequences:

* **Bias**: the tie-break gives a deterministic, reproducible choice
  for midpoints instead of depending on floating-point rounding.
* **Representable range**: values outside ``[minimum, maximum]`` or
  further than the budget from any legal code cannot be migrated; a
  larger budget widens the accepted physical interval by one grid
  step on each side at most.
* **Cost**: at most the two codes adjacent to the ideal point are
  inspected, so the per-signal cost is constant time and independent
  of the bit width.

Plan size
---------

Multiplexers are compiled by walking the existing codec trees. For a
message whose selectors offer two valid branches at each of ten
levels, the codec tree contains ``2**11 - 1 = 2047`` nodes, and the
plan contains exactly one migration node per codec-tree node -- the
selector combinations are never expanded into their cartesian product
(which would be ``2**10`` leaf tuples here and grows exponentially
with the nesting depth). At transcode time only the single active
branch per level is visited.

Shared encoding API
-------------------

The same policy is available outside migrations. Both
:meth:`cantools.database.can.Message.encode` and
:meth:`cantools.database.can.Database.encode_message` accept::

    representability={'max_abs_error': x}

The return value stays ``bytes``. Omitting the argument preserves the
historical behavior. It is only accepted together with
``scaling=True`` and ``strict=True`` (and is not available for
container messages); every other combination raises an error.
Enumerated signals keep their exact label resolution, and multiplexer
encoding goes through the same quantization path -- there is no
migration-specific bypass. Encoding the same physical value through
either API yields the same raw decision as a migration of that value.

Command line
------------

The ``decode`` subcommand gains a migration mode::

    cantools decode --migrate-to target.dbc \
                    --migration-map mapping.json \
                    [--max-abs-error X] \
                    source.dbc < old.log > new.log

In this mode stdin must be ``candump -L`` and stdout is written as
``candump -L`` again (CAN FD keeps its ``##n`` BRS/ESI marker,
extended ids keep eight hex digits), with no decode annotation prose.
The mapping and both databases are audited completely before the first
input line is read or the first output line is written; a bad mapping
therefore consumes no input. The file is processed strictly forward,
one line at a time. A bad line or a frame that cannot be migrated
stops processing with a non-zero exit code and an ``error: line N:``
message on stderr; everything already written is a valid, re-readable
log prefix. ``--migrate-to`` requires ``--migration-map`` (the error
budget is optional and defaults to zero); without these options the
command behaves exactly as before.

Current limitations
-------------------

* Only ordinary messages with integer raw values are supported;
  container messages and floating-point (``is_float``) bit fields are
  rejected at compile time.
* The mapping is static: branch pairing for unlabeled numeric
  selectors is derived from the mapped active signals, so two target
  branches carrying identical mapped signal sets cannot be
  distinguished and are rejected.
* ``raw_invalid`` is honored where the database format provides it
  (e.g. ARXML). Formats without the concept cannot mark a code invalid.
* The error budget applies per signal independently; there is no
  frame-level aggregate budget.
* Timestamps are preserved verbatim and serialized with microsecond
  precision; sub-microsecond information present in other tooling is
  not represented.
