# Migration of CAN log frames between two database layouts.
#
# A migration plan is compiled from a JSON-serializable mapping list and
# then reused for arbitrary many frames:
#
#     plan = source_db.compile_migration(target_db, mapping,
#                                       max_abs_error=0.1)
#     target_frame = plan.transcode(source_frame)
#
# The plan snapshots the relevant database objects, so later
# ``refresh()`` calls or edits of either database cannot change its
# behavior. Nested multiplexers are handled by walking the already
# built codec trees at compile and transcode time; selector value
# combinations are never enumerated.

from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass, field
from fractions import Fraction
from typing import TYPE_CHECKING, Any, Optional

from ..logreader import DataFrame
from .errors import DecodeError, EncodeError
from .namedsignalvalue import NamedSignalValue
from .representability import (
    REPRESENTABILITY_KEY,
    validate_max_abs_error,
)

if TYPE_CHECKING:
    from .can.database import Database
    from .can.message import Message
    from .can.signal import Signal


class MigrationError(Exception):
    """Raised when a frame cannot be migrated, or when a migration
    mapping is invalid.

    For frame-level failures the optional attributes carry enough
    information to locate the offending log entry.
    """

    def __init__(self,
                 message: str,
                 *,
                 frame_id: Optional[int] = None,
                 is_extended_frame: Optional[bool] = None,
                 channel: Optional[str] = None,
                 line_number: Optional[int] = None) -> None:
        super().__init__(message)
        self.frame_id = frame_id
        self.is_extended_frame = is_extended_frame
        self.channel = channel
        self.line_number = line_number


# ---------------------------------------------------------------------------
# Internal immutable structures built at compile time
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class _SignalMigration:
    source_signal: "Signal"
    target_signal: "Signal"
    #: True for numeric signals, False for fully enumerated signals.
    is_numeric: bool


@dataclass
class _NodePlan:
    """Migration instructions for one codec-tree node.

    A node mirrors one entry of ``Message._codecs``; the plan therefore
    grows with the number of tree nodes (linear in the number of
    signals plus selector branches), never with the cartesian product
    of selector values.
    """

    #: Source signal name -> migration instructions.
    signals: dict[str, _SignalMigration]
    #: Target signal name -> source signal name (inverse lookup).
    target_to_source: dict[str, str]
    #: Multiplexer name -> selector-specific migration data.
    multiplexers: dict[str, "_MuxPlan"] = field(default_factory=dict)


@dataclass
class _MuxPlan:
    """Instructions for one multiplexer selector."""

    source_signal: "Signal"
    target_signal: "Signal"
    #: True when both selectors are fully enumerated (label migration).
    by_labels: bool
    #: Source selector raw code -> target selector raw code.
    raw_code_map: dict[int, int]
    #: Source branch codec id -> node plan of the paired target branch.
    branches: dict[int, _NodePlan]


@dataclass
class _MessagePlan:
    source_message: "Message"
    target_message: "Message"
    root: _NodePlan
    budget: Fraction


# ---------------------------------------------------------------------------
# Compilation
# ---------------------------------------------------------------------------


def compile_migration(source_db: "Database",
                      target_db: "Database",
                      mapping: list[dict[str, Any]],
                      *,
                      max_abs_error: float = 0.0) -> "MigrationPlan":
    """Compile `mapping` into a reusable :class:`MigrationPlan`.

    See :class:`MigrationPlan` and the project documentation for the
    exact structure of the JSON-serializable mapping.
    """

    budget = validate_max_abs_error(max_abs_error)

    if not isinstance(mapping, list):
        raise MigrationError(
            "the migration mapping must be a list of message mappings")

    compiler = _Compiler(source_db, target_db, budget)
    message_plans: dict[tuple[int, bool], _MessagePlan] = {}

    for index, entry in enumerate(mapping):
        prefix = f"mapping entry {index}"
        plan = compiler.compile_entry(entry, prefix)
        key = (plan.source_message.frame_id,
               plan.source_message.is_extended_frame)
        if key in message_plans:
            raise MigrationError(
                f"{prefix}: duplicate mapping for source frame "
                f"0x{key[0]:x} (extended={key[1]})")
        message_plans[key] = plan

    return MigrationPlan(message_plans)


class _Compiler:

    def __init__(self,
                 source_db: "Database",
                 target_db: "Database",
                 budget: Fraction) -> None:
        self.source_db = source_db
        self.target_db = target_db
        self.budget = budget

    # -- entry level ------------------------------------------------------

    def compile_entry(self,
                      entry: Any,
                      prefix: str) -> _MessagePlan:
        if not isinstance(entry, dict):
            raise MigrationError(f"{prefix}: must be an object")

        try:
            source_ref = entry['source']
            target_ref = entry['target']
            signals_ref = entry['signals']
        except KeyError as exc:
            raise MigrationError(
                f"{prefix}: missing required field {exc.args[0]!r}") from None

        source_message = self._lookup_message(
            self.source_db, source_ref, prefix, side='source')
        target_message = self._lookup_message(
            self.target_db, target_ref, prefix, side='target')

        if source_message.is_container or target_message.is_container:
            raise MigrationError(
                f"{prefix}: container messages are not supported")
        if any(s.conversion.is_float for s in source_message.signals) or \
                any(s.conversion.is_float for s in target_message.signals):
            raise MigrationError(
                f"{prefix}: floating-point bit fields are not supported")
        if source_message.is_fd != target_message.is_fd:
            raise MigrationError(
                f"{prefix}: CAN/CAN FD format mismatch: source "
                f"is_fd={source_message.is_fd}, target "
                f"is_fd={target_message.is_fd}")

        signal_pairs = self._compile_signal_pairs(
            source_message, target_message, signals_ref, prefix)

        # Snapshot the messages so later database edits and refresh()
        # calls cannot affect the plan.
        source_snapshot = deepcopy(source_message)
        target_snapshot = deepcopy(target_message)

        # Rebuild the pair dictionary on top of the snapshots.
        name_to_snapshot_src = {s.name: s for s in source_snapshot.signals}
        name_to_snapshot_tgt = {s.name: s for s in target_snapshot.signals}
        snapshot_pairs = [
            (name_to_snapshot_src[src.name], name_to_snapshot_tgt[tgt.name])
            for src, tgt in signal_pairs
        ]

        root = self._compile_node(source_snapshot,
                                  target_snapshot,
                                  source_snapshot._codecs,
                                  target_snapshot._codecs,
                                  snapshot_pairs,
                                  prefix)

        return _MessagePlan(source_snapshot,
                            target_snapshot,
                            root,
                            self.budget)

    def _lookup_message(self,
                        db: "Database",
                        ref: Any,
                        prefix: str,
                        *,
                        side: str) -> "Message":
        if not isinstance(ref, dict):
            raise MigrationError(
                f"{prefix}: '{side}' must be an object with frame_id and "
                f"is_extended_frame")
        try:
            frame_id = ref['frame_id']
            is_extended_frame = ref['is_extended_frame']
        except KeyError as exc:
            raise MigrationError(
                f"{prefix}: '{side}' is missing field {exc.args[0]!r}") \
                from None
        if not isinstance(frame_id, int) or isinstance(frame_id, bool):
            raise MigrationError(
                f"{prefix}: '{side}.frame_id' must be an integer")
        if not isinstance(is_extended_frame, bool):
            raise MigrationError(
                f"{prefix}: '{side}.is_extended_frame' must be a boolean")
        try:
            return db.get_message_by_frame_id(
                frame_id, force_extended_id=is_extended_frame)
        except KeyError:
            raise MigrationError(
                f"{prefix}: no {side} message with frame id "
                f"0x{frame_id:x} (extended={is_extended_frame})") from None

    # -- signal level -----------------------------------------------------

    def _compile_signal_pairs(self,
                              source_message: "Message",
                              target_message: "Message",
                              signals_ref: Any,
                              prefix: str) -> list[tuple["Signal", "Signal"]]:
        if not isinstance(signals_ref, list):
            raise MigrationError(
                f"{prefix}: 'signals' must be a list of signal mappings")

        source_names = set()
        target_names = set()
        pairs: list[tuple["Signal", "Signal"]] = []

        for j, item in enumerate(signals_ref):
            sp = f"{prefix}.signals[{j}]"
            if not isinstance(item, dict):
                raise MigrationError(f"{sp}: must be an object")
            try:
                src_name = item['source_signal']
                tgt_name = item['target_signal']
            except KeyError as exc:
                raise MigrationError(
                    f"{sp}: missing field {exc.args[0]!r}") from None

            try:
                src_sig = source_message.get_signal_by_name(src_name)
            except KeyError:
                raise MigrationError(
                    f"{sp}: source signal '{src_name}' does not exist in "
                    f"message '{source_message.name}'") from None
            try:
                tgt_sig = target_message.get_signal_by_name(tgt_name)
            except KeyError:
                raise MigrationError(
                    f"{sp}: target signal '{tgt_name}' does not exist in "
                    f"message '{target_message.name}'") from None

            if src_name in source_names:
                raise MigrationError(
                    f"{sp}: source signal '{src_name}' is mapped more "
                    f"than once")
            if tgt_name in target_names:
                raise MigrationError(
                    f"{sp}: target signal '{tgt_name}' has more than one "
                    f"source signal")
            source_names.add(src_name)
            target_names.add(tgt_name)

            self._check_signal_compatibility(src_sig, tgt_sig, sp)
            pairs.append((src_sig, tgt_sig))

        return pairs

    def _check_signal_compatibility(self,
                                    src: "Signal",
                                    tgt: "Signal",
                                    prefix: str) -> None:
        src_enum = src.conversion.choices is not None
        tgt_enum = tgt.conversion.choices is not None
        if src_enum != tgt_enum:
            raise MigrationError(
                f"{prefix}: signal '{src.name}' -> '{tgt.name}': one side "
                f"is enumerated and the other is numeric")

        if (src.unit or None) != (tgt.unit or None):
            raise MigrationError(
                f"{prefix}: unit mismatch: '{src.unit}' vs '{tgt.unit}'")

        if src_enum:
            src_labels = {str(v) for v in src.conversion.choices.values()}
            tgt_labels = {str(v) for v in tgt.conversion.choices.values()}
            missing = src_labels - tgt_labels
            if missing:
                raise MigrationError(
                    f"{prefix}: target signal '{tgt.name}' has no labels "
                    f"{sorted(missing)} present in source signal "
                    f"'{src.name}'")
            if self._duplicate_labels(src) or self._duplicate_labels(tgt):
                raise MigrationError(
                    f"{prefix}: enumerated signal has duplicate labels, "
                    f"which makes label-based migration ambiguous")

    @staticmethod
    def _duplicate_labels(signal: "Signal") -> bool:
        labels = [str(v) for v in signal.conversion.choices.values()]
        return len(labels) != len(set(labels))

    # -- codec-tree level -------------------------------------------------

    def _compile_node(self,
                      source_message: "Message",
                      target_message: "Message",
                      source_node: dict,
                      target_node: dict,
                      pairs: list[tuple["Signal", "Signal"]],
                      prefix: str) -> _NodePlan:
        pair_by_source = {src.name: (src, tgt) for src, tgt in pairs}
        pair_by_target = {tgt.name: (src, tgt) for src, tgt in pairs}

        # Every non-multiplexer target signal of this node needs a
        # source. Multiplexer selectors are validated separately
        # below.
        for tgt_signal in target_node['signals']:
            if tgt_signal.name in target_node['multiplexers']:
                continue
            if tgt_signal.name not in pair_by_target:
                raise MigrationError(
                    f"{prefix}: target signal '{tgt_signal.name}' of "
                    f"message '{target_message.name}' has no source "
                    f"mapping")

        signals: dict[str, _SignalMigration] = {}
        target_to_source: dict[str, str] = {}
        for src_signal in source_node['signals']:
            if src_signal.name in source_node['multiplexers']:
                continue
            pair = pair_by_source.get(src_signal.name)
            if pair is None:
                # Unmapped source fields are allowed.
                continue
            _, tgt_signal = pair
            signals[src_signal.name] = _SignalMigration(
                source_signal=src_signal,
                target_signal=tgt_signal,
                is_numeric=src_signal.conversion.choices is None)
            target_to_source[tgt_signal.name] = src_signal.name

        mux_plans: dict[str, _MuxPlan] = {}
        for mux_name, src_branches in source_node['multiplexers'].items():
            mux_plans[mux_name] = self._compile_mux(
                source_message,
                target_message,
                source_node,
                target_node,
                mux_name,
                pairs,
                prefix)

        return _NodePlan(signals=signals,
                         target_to_source=target_to_source,
                         multiplexers=mux_plans)

    def _compile_mux(self,
                     source_message: "Message",
                     target_message: "Message",
                     source_node: dict,
                     target_node: dict,
                     mux_name: str,
                     pairs: list[tuple["Signal", "Signal"]],
                     prefix: str) -> _MuxPlan:
        pair_by_source = {src.name: (src, tgt) for src, tgt in pairs}

        if mux_name not in pair_by_source:
            raise MigrationError(
                f"{prefix}: multiplexer selector signal '{mux_name}' must "
                f"be mapped explicitly")
        src_mux_signal, tgt_mux_signal = pair_by_source[mux_name]
        tgt_mux_name = tgt_mux_signal.name

        if tgt_mux_name not in target_node['multiplexers']:
            raise MigrationError(
                f"{prefix}: the target of selector '{mux_name}' is not a "
                f"multiplexer selector in message "
                f"'{target_message.name}'")

        src_branches = source_node['multiplexers'][mux_name]
        tgt_branches = target_node['multiplexers'][tgt_mux_name]

        by_labels = src_mux_signal.conversion.choices is not None
        if by_labels != (tgt_mux_signal.conversion.choices is not None):
            raise MigrationError(
                f"{prefix}: selector '{mux_name}' -> "
                f"'{tgt_mux_signal.name}': one side is enumerated and the "
                f"other is numeric")

        raw_code_map: dict[int, int] = {}
        branch_plans: dict[int, _NodePlan] = {}

        if by_labels:
            raw_code_map = self._label_selector_map(
                src_mux_signal, tgt_mux_signal, prefix)
            # Every source branch id must be a known selector code, and
            # its mapped target code must identify a target branch.
            for src_id in src_branches:
                if src_id not in raw_code_map:
                    raise MigrationError(
                        f"{prefix}: selector '{mux_name}' source branch "
                        f"{src_id} has no matching label in the target "
                        f"signal")
                tgt_id = raw_code_map[src_id]
                if tgt_id not in tgt_branches:
                    raise MigrationError(
                        f"{prefix}: target selector "
                        f"'{tgt_mux_signal.name}' has no branch for "
                        f"mapped value {tgt_id}")
            self._assert_full_branch_coverage(
                src_branches, tgt_branches, prefix, mux_name, pairs)
            for src_id, src_branch in src_branches.items():
                tgt_id = raw_code_map[src_id]
                branch_plans[src_id] = self._compile_node(
                    source_message,
                    target_message,
                    src_branch,
                    tgt_branches[tgt_id],
                    pairs,
                    f"{prefix} (branch {mux_name}={src_id})")
        else:
            # Numeric selectors: pair branches by their active signal
            # sets expressed through the mapping.
            pairing = self._pair_branches_by_signals(
                src_branches, tgt_branches, pairs, prefix, mux_name)
            for src_id, tgt_id in pairing.items():
                raw_code_map[src_id] = tgt_id
                branch_plans[src_id] = self._compile_node(
                    source_message,
                    target_message,
                    src_branches[src_id],
                    tgt_branches[tgt_id],
                    pairs,
                    f"{prefix} (branch {mux_name}={src_id})")

        return _MuxPlan(source_signal=src_mux_signal,
                        target_signal=tgt_mux_signal,
                        by_labels=by_labels,
                        raw_code_map=raw_code_map,
                        branches=branch_plans)

    def _label_selector_map(self,
                            src: "Signal",
                            tgt: "Signal",
                            prefix: str) -> dict[int, int]:
        tgt_label_to_raw = {
            str(choice): raw for raw, choice in tgt.conversion.choices.items()
        }
        result: dict[int, int] = {}
        for src_raw, src_choice in src.conversion.choices.items():
            label = str(src_choice)
            if label not in tgt_label_to_raw:
                raise MigrationError(
                    f"{prefix}: target selector '{tgt.name}' has no label "
                    f"'{label}'")
            result[src_raw] = tgt_label_to_raw[label]
        return result

    def _pair_branches_by_signals(self,
                                  src_branches: dict[int, dict],
                                  tgt_branches: dict[int, dict],
                                  pairs: list[tuple["Signal", "Signal"]],
                                  prefix: str,
                                  mux_name: str) -> dict[int, int]:
        pair_by_target = {tgt.name: (src, tgt) for src, tgt in pairs}
        pair_by_source = {src.name: (src, tgt) for src, tgt in pairs}

        # Describe each branch by the set of mapped non-selector
        # signals active in it, canonically using *source* signal
        # names on both sides (selectors themselves are excluded since
        # their ids are allowed to differ).
        def source_keys(node: dict) -> frozenset[str]:
            return frozenset(
                s.name for s in node['signals']
                if s.name not in node['multiplexers']
                and s.name in pair_by_source)

        def target_keys(node: dict) -> frozenset[str]:
            return frozenset(
                pair_by_target[s.name][0].name
                for s in node['signals']
                if s.name not in node['multiplexers']
                and s.name in pair_by_target)

        # Non-empty signatures must be unique on the target side:
        # otherwise two target branches are indistinguishable.
        tgt_by_signature: dict[frozenset[str], list[int]] = {}
        for tgt_id, node in tgt_branches.items():
            tgt_by_signature.setdefault(target_keys(node), []).append(tgt_id)
        for sig, ids in tgt_by_signature.items():
            if sig and len(ids) > 1:
                raise MigrationError(
                    f"{prefix}: target branches {ids} of multiplexer "
                    f"'{mux_name}' are indistinguishable by their mapped "
                    f"active signals")

        pairing: dict[int, int] = {}
        # Branches carrying no mapped signal are paired deterministically
        # in sorted branch-id order; their counts must match.
        empty_sources = sorted(
            sid for sid, node in src_branches.items()
            if not source_keys(node))
        empty_targets = sorted(tgt_by_signature.get(frozenset(), []))
        if len(empty_sources) != len(empty_targets):
            raise MigrationError(
                f"{prefix}: multiplexer '{mux_name}' has {len(empty_sources)} "
                f"source branch(es) without mapped signals but "
                f"{len(empty_targets)} such target branch(es)")
        pairing.update(zip(empty_sources, empty_targets))

        for src_id, node in src_branches.items():
            sig = source_keys(node)
            if not sig:
                continue
            ids = tgt_by_signature.get(sig)
            if not ids:
                raise MigrationError(
                    f"{prefix}: source branch {mux_name}={src_id} has no "
                    f"target branch carrying the same mapped signals "
                    f"({sorted(sig)})")
            pairing[src_id] = ids[0]

        if len(set(pairing.values())) != len(pairing):
            raise MigrationError(
                f"{prefix}: two source branches of multiplexer "
                f"'{mux_name}' resolve to the same target branch")

        return pairing

    def _assert_full_branch_coverage(self,
                                     src_branches: dict[int, dict],
                                     tgt_branches: dict[int, dict],
                                     prefix: str,
                                     mux_name: str,
                                     pairs: list) -> None:
        # Branch coverage for the label-based case: every target
        # branch must be reachable from some source branch, otherwise
        # target active signals could never get a value. Signatures
        # use source signal names on both sides, like the numeric
        # branch pairing does.
        pair_by_source = {src.name: (src, tgt) for src, tgt in pairs}
        pair_by_target = {tgt.name: (src, tgt) for src, tgt in pairs}

        def source_keys(node: dict) -> frozenset[str]:
            return frozenset(
                s.name for s in node['signals']
                if s.name not in node['multiplexers']
                and s.name in pair_by_source)

        def target_keys(node: dict) -> frozenset[str]:
            return frozenset(
                pair_by_target[s.name][0].name
                for s in node['signals']
                if s.name not in node['multiplexers']
                and s.name in pair_by_target)

        src_sigs = {source_keys(n) for n in src_branches.values()}
        for tgt_id, node in tgt_branches.items():
            if target_keys(node) not in src_sigs:
                raise MigrationError(
                    f"{prefix}: target branch {mux_name}={tgt_id} is not "
                    f"reachable from any source branch")


# ---------------------------------------------------------------------------
# The reusable plan
# ---------------------------------------------------------------------------


class MigrationPlan:
    """A compiled, reusable frame migration plan.

    Plans are created by :meth:`Database.compile_migration`. They hold
    deep copies of the involved messages, so modifying or refreshing
    the source/target databases after compilation cannot change the
    behavior of an existing plan, and the plan never mutates either
    database.
    """

    def __init__(self, message_plans: dict[tuple[int, bool],
                                           _MessagePlan]) -> None:
        self._message_plans = message_plans

    # -- introspection ----------------------------------------------------

    def describe(self) -> list[dict[str, Any]]:
        """Return a JSON-serializable description of the compiled plan.

        Useful for auditing a mapping before any frame is processed.
        Each entry restates the frame identities and the per-signal
        migration in effect (including the quantization budget).
        """

        result = []
        for plan in self._message_plans.values():
            signals = []
            self._describe_node(plan.root, signals)
            result.append({
                'source': {
                    'frame_id': plan.source_message.frame_id,
                    'is_extended_frame':
                        plan.source_message.is_extended_frame,
                    'name': plan.source_message.name,
                    'is_fd': plan.source_message.is_fd,
                    'length': plan.source_message.length,
                },
                'target': {
                    'frame_id': plan.target_message.frame_id,
                    'is_extended_frame':
                        plan.target_message.is_extended_frame,
                    'name': plan.target_message.name,
                    'is_fd': plan.target_message.is_fd,
                    'length': plan.target_message.length,
                },
                REPRESENTABILITY_KEY: float(plan.budget),
                'signals': signals,
            })
        return result

    def _describe_node(self,
                       node: _NodePlan,
                       out: list[dict[str, Any]]) -> None:
        for migration in node.signals.values():
            out.append({
                'source_signal': migration.source_signal.name,
                'target_signal': migration.target_signal.name,
                'kind': 'numeric' if migration.is_numeric else 'enum',
                'source_unit': migration.source_signal.unit,
                'target_unit': migration.target_signal.unit,
            })
        for mux in node.multiplexers.values():
            out.append({
                'source_signal': mux.source_signal.name,
                'target_signal': mux.target_signal.name,
                'kind': 'multiplexer',
                'selector_map': dict(mux.raw_code_map),
            })
            for branch in mux.branches.values():
                self._describe_node(branch, out)

    # -- frame processing -------------------------------------------------

    def transcode(self, frame: DataFrame) -> DataFrame:
        """Migrate one :class:`~cantools.logreader.DataFrame`.

        Channel and timestamp are preserved. Frame identity
        (id/extended/FD flags) and payload are replaced by the target
        layout. A :class:`MigrationError` is raised for unknown
        messages, remote requests, format mismatches, undefined
        multiplexer selector values, invalid source values or any
        value that does not fit the target within the error budget.
        """

        key = (frame.frame_id, frame.is_extended_frame)
        plan = self._message_plans.get(key)
        if plan is None:
            raise MigrationError(
                f"no migration mapping for frame 0x{frame.frame_id:x} "
                f"(extended={frame.is_extended_frame})",
                frame_id=frame.frame_id,
                is_extended_frame=frame.is_extended_frame,
                channel=frame.channel)

        if frame.is_remote_frame:
            raise MigrationError(
                "remote request frames cannot be migrated",
                frame_id=frame.frame_id,
                is_extended_frame=frame.is_extended_frame,
                channel=frame.channel)

        source_message = plan.source_message
        target_message = plan.target_message

        if frame.is_fd != source_message.is_fd:
            raise MigrationError(
                f"frame 0x{frame.frame_id:x} is marked "
                f"is_fd={frame.is_fd} but the source definition expects "
                f"is_fd={source_message.is_fd}",
                frame_id=frame.frame_id,
                is_extended_frame=frame.is_extended_frame,
                channel=frame.channel)

        if len(frame.data) != source_message.length:
            raise MigrationError(
                f"frame 0x{frame.frame_id:x} payload length "
                f"{len(frame.data)} does not match the source message "
                f"length {source_message.length}",
                frame_id=frame.frame_id,
                is_extended_frame=frame.is_extended_frame,
                channel=frame.channel)

        try:
            decoded = source_message.decode_simple(
                frame.data,
                decode_choices=True,
                scaling=True,
                allow_truncated=False,
                allow_excess=False)
        except (DecodeError, ValueError) as exc:
            raise MigrationError(
                f"source frame 0x{frame.frame_id:x} could not be decoded: "
                f"{exc}",
                frame_id=frame.frame_id,
                is_extended_frame=frame.is_extended_frame,
                channel=frame.channel) from None

        self._reject_invalid_raw(source_message, frame.data, decoded)

        target_values: dict[str, Any] = {}
        try:
            self._transcode_node(plan,
                                 source_message._codecs,
                                 plan.root,
                                 frame.data,
                                 decoded,
                                 target_values)
        except MigrationError:
            raise
        except (EncodeError, DecodeError, ValueError) as exc:
            raise MigrationError(
                f"frame 0x{frame.frame_id:x} cannot be encoded in the "
                f"target layout: {exc}",
                frame_id=frame.frame_id,
                is_extended_frame=frame.is_extended_frame,
                channel=frame.channel) from None

        try:
            payload = target_message.encode(
                target_values,
                scaling=True,
                padding=False,
                strict=True,
                representability={
                    REPRESENTABILITY_KEY: float(plan.budget)
                })
        except EncodeError as exc:
            raise MigrationError(
                f"frame 0x{frame.frame_id:x} cannot be encoded in the "
                f"target layout: {exc}",
                frame_id=frame.frame_id,
                is_extended_frame=frame.is_extended_frame,
                channel=frame.channel) from None

        # Re-decode the produced frame and verify the guarantee instead
        # of merely promising it in documentation.
        self._verify_output(plan, payload, decoded)

        return DataFrame(
            channel=frame.channel,
            frame_id=target_message.frame_id,
            is_extended_frame=target_message.is_extended_frame,
            data=payload,
            is_remote_frame=False,
            timestamp=frame.timestamp,
            timestamp_format=frame.timestamp_format,
            is_fd=target_message.is_fd,
            bitrate_switch=frame.bitrate_switch,
            error_state_indicator=frame.error_state_indicator)

    # -- internals --------------------------------------------------------

    def _reject_invalid_raw(self,
                            message: "Message",
                            data: bytes,
                            decoded: dict[str, Any]) -> None:
        from ..quality.decoder import _read_raw

        for signal in self._active_signals(message, decoded):
            if signal.raw_invalid is None:
                continue
            raw = _read_raw(signal, data)
            if raw is not None and raw == signal.raw_invalid:
                raise MigrationError(
                    f"source signal '{signal.name}' carries its "
                    f"raw_invalid value {raw}; the frame is rejected")

    @staticmethod
    def _active_signals(message: "Message",
                        decoded: dict[str, Any]) -> list["Signal"]:
        """All signals that are live for the decoded selector values."""
        result: list["Signal"] = []
        node = message._codecs
        assert node is not None
        while True:
            result.extend(node['signals'])
            if not node['multiplexers']:
                return result
            next_node = None
            for mux_name, branches in node['multiplexers'].items():
                value = decoded[mux_name]
                if isinstance(value, (str, NamedSignalValue)):
                    raw = message.get_signal_by_name(mux_name) \
                        .conversion.choice_to_number(str(value))
                else:
                    raw = int(value)
                next_node = branches[raw]
            node = next_node

    def _transcode_node(self,
                        plan: _MessagePlan,
                        source_codec_node: dict,
                        node_plan: _NodePlan,
                        raw_data: bytes,
                        decoded: dict[str, Any],
                        target_values: dict[str, Any]) -> None:
        for src_name, migration in node_plan.signals.items():
            value = decoded[src_name]
            tgt_signal = migration.target_signal
            if migration.is_numeric:
                # Keep the exact physical value; the target quantizer
                # does the rounding decision.
                target_values[tgt_signal.name] = value
            else:
                # Enumerated: migrate the complete label. Passing the
                # label keeps the enum resolution in the shared encode
                # path.
                label = str(value)
                tgt_labels = {str(c) for c in
                              tgt_signal.conversion.choices.values()}
                if label not in tgt_labels:
                    raise MigrationError(
                        f"target signal '{tgt_signal.name}' has no label "
                        f"'{label}'")
                target_values[tgt_signal.name] = label

        for mux_name, mux_plan in node_plan.multiplexers.items():
            src_mux = source_codec_node['multiplexers'][mux_name]
            selector_decoded = decoded[mux_name]
            if isinstance(selector_decoded, (str, NamedSignalValue)):
                selector_raw = mux_plan.source_signal.conversion \
                    .choice_to_number(str(selector_decoded))
            else:
                selector_raw = int(selector_decoded)
            if selector_raw not in src_mux:
                raise MigrationError(
                    f"undefined selector value {selector_raw} for "
                    f"multiplexer '{mux_name}'")
            if selector_raw not in mux_plan.raw_code_map:
                raise MigrationError(
                    f"selector value {selector_raw} for multiplexer "
                    f"'{mux_name}' has no target branch")

            target_selector_raw = mux_plan.raw_code_map[selector_raw]
            target_mux_signal = mux_plan.target_signal
            if mux_plan.by_labels:
                # Migrate the selector label, not the raw code.
                target_values[target_mux_signal.name] = str(
                    target_mux_signal.conversion.choices[target_selector_raw])
            else:
                # Numeric selector: pass the exact physical value
                # corresponding to the desired target raw code so the
                # shared quantizer reproduces it (exactly under a zero
                # budget) without any migration-only bypass.
                target_values[target_mux_signal.name] = Fraction(
                    target_selector_raw) * Fraction(
                        target_mux_signal.conversion.scale) + Fraction(
                            target_mux_signal.conversion.offset)

            self._transcode_node(
                plan,
                src_mux[selector_raw],
                mux_plan.branches[selector_raw],
                raw_data,
                decoded,
                target_values)

    def _verify_output(self,
                       plan: _MessagePlan,
                       payload: bytes,
                       source_decoded: dict[str, Any]) -> None:
        target_message = plan.target_message
        try:
            redecoded = target_message.decode_simple(
                payload,
                decode_choices=True,
                scaling=True,
                allow_truncated=False,
                allow_excess=False)
        except (DecodeError, ValueError) as exc:
            raise MigrationError(
                f"internal error: produced target frame cannot be "
                f"decoded: {exc}") from None

        budget = plan.budget
        self._verify_node(plan.root, redecoded, source_decoded, budget)

    def _verify_node(self,
                     node: _NodePlan,
                     redecoded: dict[str, Any],
                     source_decoded: dict[str, Any],
                     budget: Fraction) -> None:
        for src_name, migration in node.signals.items():
            tgt_name = migration.target_signal.name
            if tgt_name not in redecoded:
                raise MigrationError(
                    f"internal error: target signal '{tgt_name}' missing "
                    f"from re-decoded frame")
            target_value = redecoded[tgt_name]
            source_value = source_decoded[src_name]
            if migration.is_numeric:
                # Exact comparison through the rationals represented by
                # the observed floats so float display rounding can
                # neither hide an overshoot nor fake one.
                error = abs(Fraction(target_value)
                            - Fraction(source_value))
                if error > budget:
                    raise MigrationError(
                        f"internal error: signal '{tgt_name}' error "
                        f"{float(error)} exceeds budget "
                        f"{float(budget)}")
            else:
                if str(target_value) != str(source_value):
                    raise MigrationError(
                        f"internal error: enum label changed from "
                        f"'{source_value}' to '{target_value}'")

        for mux in node.multiplexers.values():
            target_selector = redecoded[mux.target_signal.name]
            source_selector = source_decoded[mux.source_signal.name]
            if mux.by_labels:
                if str(target_selector) != str(source_selector):
                    raise MigrationError(
                        "internal error: multiplexer label changed")
                tgt_raw = mux.target_signal.conversion.choice_to_number(
                    str(target_selector))
            else:
                tgt_raw = int(target_selector)
            if tgt_raw not in mux.raw_code_map.values():
                raise MigrationError(
                    "internal error: multiplexer selected an unmapped "
                    "branch")
            for src_branch_raw, branch_node in mux.branches.items():
                if mux.raw_code_map[src_branch_raw] == tgt_raw:
                    self._verify_node(branch_node,
                                      redecoded,
                                      source_decoded,
                                      budget)
                    break
