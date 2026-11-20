#!/usr/bin/env python3
"""Generate the hand-computable candump -L fixtures used by the tests.

The log uses absolute epoch seconds with six fractional digits so every
timestamp is an exact microsecond. Two channels (can0, can1) carry the
same frame ids to prove channel disambiguation.

Grid used by the tests: start=1000.000000, stop=1000.900000,
step=0.1s -> ten grid points at .0 ... .9.

Scenario map (all frame ids refer to quality_demo.dbc):

* 0x64 EngineStatus (100ms cycle, EngineSpeed scale .25 / EngineTemp
  offset -40) on can0 and can1 -> same id, different channel.
* Two frames at the identical timestamp -> line order wins.
* 0xC8 MuxMessage -> branch switch A -> B -> unknown selector.
* 0x12C ShortMessage -> a short 2-byte payload truncating ThirdByte.
* 0x190 EventMessage -> event-driven, long silence.
* malformed line, remote request, unknown frame, bad payload hex.
* a stoppage longer than the EngineStatus limit.

All recognized frames are emitted in non-decreasing timestamp order;
same-timestamp rows rely on line order, which is part of the test.
"""

from __future__ import annotations

import pathlib

OUT = pathlib.Path(__file__).resolve().parents[1] / "tests/files/quality"


def line(ts: str, channel: str, can_id: str, payload: str) -> str:
    return f"({ts}) {channel} {can_id}#{payload}"


def engine(speed_raw: int, temp_raw: int) -> str:
    # EngineSpeed u16 little-endian at bit 0, EngineTemp u8 at bit 16.
    return (speed_raw & 0xFFFF).to_bytes(2, "little").hex() \
        + bytes([temp_raw & 0xFF, 0]).hex()


def mux(selector: int, common: int, branch: int) -> str:
    # Selector u4@0, CommonSig u8@4, branch bytes at 16 and 24.
    low = (common << 4) | (selector & 0xF)
    return bytes([low, 0, branch & 0xFF, branch & 0xFF]).hex()


def main() -> None:
    rows: list[str] = []

    # t=0.000 -----------------------------------------------------------
    rows.append(line("1000.000000", "can0", "064", engine(400, 60)))
    # same id on can1 at the same instant: must stay a separate series
    rows.append(line("1000.000000", "can1", "064", engine(800, 80)))
    # second can0 update at the identical timestamp (line order wins)
    rows.append(line("1000.000000", "can0", "064", engine(440, 60)))
    # mux branch A active (BranchA raw 20 -> 40 with scale 2)
    rows.append(line("1000.050000", "can0", "0C8", mux(1, 7, 20)))
    # short payload: FirstByte=0x0A, SecondByte=0x1B, ThirdByte missing
    rows.append(line("1000.050000", "can0", "12C", "0A1B"))

    # t=0.100 -----------------------------------------------------------
    rows.append(line("1000.100000", "can0", "064", engine(480, 62)))
    # event message: EventCounter=5, EventState raw 1 (Active)
    rows.append(line("1000.100000", "can0", "190", "0501"))

    # t=0.150 mux switches to branch B (BranchB raw 30)
    rows.append(line("1000.150000", "can0", "0C8", mux(2, 8, 30)))

    # t=0.200 last periodic EngineStatus before the long silence
    rows.append(line("1000.200000", "can0", "064", engine(520, 64)))

    # t=0.250 unknown multiplexer value -> whole mux frame decode fails
    rows.append(line("1000.250000", "can0", "0C8", mux(9, 9, 0)))
    # remote request: diagnostic, never a valid report
    rows.append(line("1000.250000", "can0", "064", "R"))

    # t=0.260 unknown database message
    rows.append(line("1000.260000", "can0", "7FF", "11223344"))
    # t=0.270 bad payload hex
    rows.append(line("1000.270000", "can0", "064", "ZZ"))
    # a syntactically invalid line without timestamp
    rows.append("not a candump line at all")

    # t=0.350 full-length short message recovers all three bytes
    rows.append(line("1000.350000", "can0", "12C", "010203"))

    # t=0.700 EngineStatus fresh again after a >0.2s silence (expired)
    rows.append(line("1000.700000", "can0", "064", engine(560, 66)))

    # t=0.800 event message fires again
    rows.append(line("1000.800000", "can0", "190", "0902"))

    log_path = OUT / "demo_short.log"
    log_path.write_text("\n".join(rows) + "\n")

    # A second log that regresses in time on a recognized frame.
    bad = [
        line("1000.200000", "can0", "064", engine(1, 1)),
        line("1000.100000", "can0", "064", engine(2, 2)),
    ]
    (OUT / "demo_regression.log").write_text("\n".join(bad) + "\n")

    print(f"wrote {log_path} ({len(rows)} lines)")


if __name__ == "__main__":
    main()
