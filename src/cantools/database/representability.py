# Deterministic quantization with a representability error budget.
#
# The same policy is used by:
#   * ``Message.encode(..., representability={'max_abs_error': x})``
#   * ``Database.encode_message(..., representability=...)``
#   * the frame migration plan built by ``Database.compile_migration()``
#
# There is deliberately no migration-only shortcut: choosing the target
# raw code goes through this single function.

from __future__ import annotations

import math
from fractions import Fraction
from typing import TYPE_CHECKING, Optional, Union

from .errors import EncodeError

if TYPE_CHECKING:
    from .can.signal import Signal

#: Accepted keyword of the ``representability`` mapping.
REPRESENTABILITY_KEY = 'max_abs_error'


def validate_max_abs_error(max_abs_error: float) -> Fraction:
    """Validate an error budget and return it as an exact :class:`Fraction`.

    The budget is measured in the signal's own physical unit. It must be
    a finite, non-negative number.
    """

    if isinstance(max_abs_error, bool) or not isinstance(
            max_abs_error, (int, float)):
        raise TypeError(
            f"'{REPRESENTABILITY_KEY}' must be a finite, non-negative "
            f"number, but got {max_abs_error!r}")

    if not math.isfinite(max_abs_error) or max_abs_error < 0:
        raise ValueError(
            f"'{REPRESENTABILITY_KEY}' must be a finite, non-negative "
            f"number, but got {max_abs_error!r}")

    return Fraction(max_abs_error)


def validate_representability(
        representability: Optional[dict[str, float]]) -> Optional[Fraction]:
    """Validate the public ``representability`` keyword argument.

    ``None`` disables the check (historical behavior). Otherwise exactly
    one key, ``'max_abs_error'``, holding a finite non-negative number is
    accepted.
    """

    if representability is None:
        return None

    if not isinstance(representability, dict):
        raise TypeError(
            "'representability' must be a mapping like "
            f"{{'{REPRESENTABILITY_KEY}': x}}, or None")

    keys = set(representability)
    if keys != {REPRESENTABILITY_KEY}:
        raise ValueError(
            "'representability' accepts exactly the key "
            f"'{REPRESENTABILITY_KEY}', but got keys {sorted(keys)}")

    return validate_max_abs_error(representability[REPRESENTABILITY_KEY])


def _as_exact(value: Union[int, float, Fraction]) -> Fraction:
    """Turn a physical value into an exact rational.

    ``float`` values are converted through their exact binary value, so
    an over-tolerance physical input can never be made to pass by
    rounding it before the comparison.
    """

    if isinstance(value, Fraction):
        return value
    if isinstance(value, int):
        return Fraction(value)
    if isinstance(value, float):
        return Fraction(value)
    raise TypeError(
        f"numeric signal value must be int, float or Fraction, got "
        f"{type(value).__name__}")


def quantize_signal_value(signal: "Signal",
                          physical_value: Union[int, float, Fraction],
                          max_abs_error: Fraction) -> int:
    """Choose the target raw integer for one numeric signal.

    Policy (deterministic, all arithmetic is exact rational arithmetic):

    1. The ideal (unrounded) raw value is computed exactly from the
       signal's scale and offset.
    2. Candidate codes are the integers representable in the signal's
       bit field that satisfy the declared minimum/maximum and are not
       the signal's ``raw_invalid`` sentinel.
    3. The candidate minimizing the absolute physical error is chosen.
       Exact ties (the ideal point lies on the midpoint between two
       legal codes) are broken toward the smaller raw code, which for
       positive scales is the lower physical grid point and for
       negative scales the upper one.
    4. If no candidate exists, or the best error exceeds
       `max_abs_error`, an :class:`EncodeError` is raised. A zero budget
       therefore means the physical value must land exactly on a legal
       grid point.

    The cost per signal is constant time in the field width: at most
    the two codes adjacent to the ideal point are inspected.
    """

    conversion = signal.conversion
    scale = Fraction(conversion.scale)
    offset = Fraction(conversion.offset)

    if scale == 0:
        raise EncodeError(
            f"Signal '{signal.name}' has a zero scale and cannot be "
            f"quantized")

    exact = _as_exact(physical_value)
    ideal_raw = (exact - offset) / scale

    # All codes that fit the integer bit field.
    bits = signal.length
    if signal.is_signed:
        min_code = -(1 << (bits - 1))
        max_code = (1 << (bits - 1)) - 1
    else:
        min_code = 0
        max_code = (1 << bits) - 1

    # The declared min/max constrain physical values. Translate them
    # to raw-code bounds. min/max are physical-world limits, so the
    # bound inclusion is expressed on the physical side before
    # rounding; the floor/ceil below only narrow onto representable
    # integer codes.
    if signal.minimum is not None:
        min_phys = Fraction(signal.minimum)
        if exact < min_phys:
            raise EncodeError(
                f"Signal '{signal.name}' value {physical_value} is below "
                f"its declared minimum {signal.minimum}")
        bound = (min_phys - offset) / scale
        if scale > 0:
            min_code = max(min_code, math.ceil(bound))
        else:
            max_code = min(max_code, math.floor(bound))
    if signal.maximum is not None:
        max_phys = Fraction(signal.maximum)
        if exact > max_phys:
            raise EncodeError(
                f"Signal '{signal.name}' value {physical_value} is above "
                f"its declared maximum {signal.maximum}")
        bound = (max_phys - offset) / scale
        if scale > 0:
            max_code = min(max_code, math.floor(bound))
        else:
            min_code = max(min_code, math.ceil(bound))

    if min_code > max_code:
        raise EncodeError(
            f"No legal raw code exists for signal '{signal.name}' within "
            f"its bit field and declared min/max")

    raw_invalid = signal.raw_invalid
    invalid_codes = set()
    if raw_invalid is not None and isinstance(raw_invalid, int) \
            and min_code <= raw_invalid <= max_code:
        invalid_codes.add(raw_invalid)

    lower = math.floor(ideal_raw)
    upper = math.ceil(ideal_raw)

    candidates = []
    for code in (lower, upper):
        if min_code <= code <= max_code and code not in invalid_codes:
            candidates.append(code)

    # The ideal grid point may itself be the invalid sentinel, or the
    # adjacent codes may fall outside the legal range. Walk outward to
    # the nearest legal code on each side instead of giving up.
    if not candidates:
        down = lower
        while down >= min_code and down in invalid_codes:
            down -= 1
        if down >= min_code:
            candidates.append(down)
        up = upper
        while up <= max_code and up in invalid_codes:
            up += 1
        if up <= max_code:
            candidates.append(up)

    if not candidates:
        raise EncodeError(
            f"Signal '{signal.name}' has no legal raw code (every code in "
            f"range is invalid) for value {physical_value}")

    def physical_error(code: int) -> Fraction:
        return abs(Fraction(code) * scale + offset - exact)

    best_code = candidates[0]
    best_error = physical_error(best_code)
    for code in candidates[1:]:
        error = physical_error(code)
        # Deterministic tie-break: keep the smaller raw code.
        if error < best_error or (error == best_error and code < best_code):
            best_code = code
            best_error = error

    if best_error > max_abs_error:
        raise EncodeError(
            f"Signal '{signal.name}' value {physical_value} cannot be "
            f"represented within an absolute error of "
            f"{float(max_abs_error)}: best achievable error is "
            f"{float(best_error)} (raw code {best_code})")

    return best_code
