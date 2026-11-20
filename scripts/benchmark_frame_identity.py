#!/usr/bin/env python3
"""Measure index build cost, per-frame lookup cost and index growth.

Builds a synthetic database and log of configurable size, then reports:

* how long one ``refresh`` (index build) takes,
* how long a single frame lookup takes,
* the size of the persistent index state, and
* whether repeated ``refresh`` calls keep that state size constant.

Only the public lookup API and the documented free functions are used;
the two size counters are internal attributes of ``Database``.

Run from the repository root:

    python3 scripts/benchmark_frame_identity.py [messages] [frames]
"""

from __future__ import annotations

import sys
import time

from cantools.database.can.database import Database
from cantools.database.can.message import Message
from cantools.database.can.signal import Signal


def build_database(message_count):
    messages = []
    for index in range(message_count):
        signal = Signal('S', start=0, length=8)
        # Reuse ids so standard and extended ids actually collide.
        frame_id = 0x100 + (index % 0x700)
        messages.append(Message(frame_id=frame_id,
                                name=f'M{index}',
                                length=8,
                                signals=[signal],
                                is_extended_frame=bool(index % 2)))
    return messages


def build_log(frame_count, frame_id_mask=0x1FFFFFFF):
    # Every frame is (numeric id, extended flag); no 0x80000000 flag.
    return [(0x100 + (index % 0x700), bool(index % 2))
            for index in range(frame_count)]


def time_refresh(db, count):
    started = time.perf_counter()
    for _ in range(count):
        db.refresh()
    return (time.perf_counter() - started) / count


def time_lookup(db, log, count):
    started = time.perf_counter()
    for index in range(count):
        frame_id, extended = log[index % len(log)]
        db.get_message_by_frame_id(frame_id, is_extended_frame=extended)
    return (time.perf_counter() - started) / count


def main():
    message_count = int(sys.argv[1]) if len(sys.argv) > 1 else 2000
    frame_count = int(sys.argv[2]) if len(sys.argv) > 2 else 200_000

    db = Database(build_database(message_count))
    log = build_log(frame_count)

    refresh_seconds = time_refresh(db, 5)
    lookup_seconds = time_lookup(db, log, frame_count)

    sizes = []
    for _ in range(10):
        db.refresh()
        sizes.append((len(db._frame_id_to_message),
                      len(db._ambiguous_frame_ids)))

    print(f'messages:              {message_count}')
    print(f'frames:                {frame_count}')
    print(f'refresh (index build): {refresh_seconds * 1000:.2f} ms')
    print(f'lookup (per frame):    {lookup_seconds * 1e6:.3f} us')
    print(f'index entries:         {sizes[0][0]} resolved, '
          f'{sizes[0][1]} ambiguous')
    print(f'state after 10 refresh: '
          f'{"constant" if len(set(sizes)) == 1 else "growing"} {sizes[0]}')


if __name__ == '__main__':
    main()
