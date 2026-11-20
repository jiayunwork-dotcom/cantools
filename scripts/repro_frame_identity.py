#!/usr/bin/env python3
"""Reproduce the two frame-identity failures this change fixes.

Group 1 -- a standard and an extended message share the numeric id
0x123. The API and the log reader already distinguish them, but the
``decode`` command (and the legacy plot path) used to look the message
up by number only, so the extended log line was decoded as the standard
message.

Group 2 -- with ``frame_id_mask = 0x1FFFFF00`` the masked lookup of
0x18FF0102 finds a message, while ``encode_message`` /
``decode_message`` raised ``KeyError`` because they used the unmasked id.
With two different definitions under one masked identity, the last
loaded one silently won; the result flipped when the load order was
reversed.

Run from the repository root:

    python3 scripts/repro_frame_identity.py

The script only reads the fixtures in ``tests/files/frame_identity``.
"""

from __future__ import annotations

import io
import os
import subprocess
import sys

import cantools
from cantools import logreader

FILES = os.path.join(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__))), 'tests', 'files', 'frame_identity')


def read(name):
    with open(os.path.join(FILES, name), encoding='utf-8') as fin:
        return fin.read()


def cli_decode(database, log, *extra):
    result = subprocess.run([sys.executable, '-m', 'cantools', 'decode',
                             '-s', *extra, os.path.join(FILES, database)],
                            input=log, text=True, capture_output=True)
    return result.stdout + result.stderr


def group_one():
    print('== group 1: same numeric id, standard vs extended ==')
    database = cantools.database.load_file(os.path.join(FILES, 'same_id.dbc'))
    print('database:',
          [(message.name, hex(message.frame_id),
            message.is_extended_frame)
           for message in database.messages])

    for frame in logreader.Parser(io.StringIO(read('same_id.log'))):
        print('log reader:', hex(frame.frame_id),
              'extended =', frame.is_extended_frame)

    print('decode_message(0x123) ->',
          database.decode_message(0x123, bytes([5]) + bytes(7)))
    print('decode_message(0x123, force_extended_id=True) ->',
          database.decode_message(0x123, bytes([6]) + bytes(7),
                                  force_extended_id=True))
    print('CLI decode:')
    print(cli_decode('same_id.dbc', read('same_id.log')), end='')


def group_two():
    print('\n== group 2: mask lookup vs encode/decode ==')
    database = cantools.database.load_file(
        os.path.join(FILES, 'masked.dbc'), frame_id_mask=0x1FFFFF00)
    print('masked lookup ->',
          database.get_message_by_frame_id(
              0x18FF0102, force_extended_id=True).name)
    print('encode ->',
          database.encode_message(0x18FF0102, {'V': 1},
                                  force_extended_id=True).hex())
    print('decode ->',
          database.decode_message(0x18FF0102, bytes([1]) + bytes(7),
                                  force_extended_id=True))

    print('\n-- two different layouts under one masked identity --')
    for name in ('masked_conflict.dbc', 'masked_conflict_reversed.dbc'):
        database = cantools.database.load_file(
            os.path.join(FILES, name), frame_id_mask=0x1FFFFF00)
        try:
            chosen = database.get_message_by_frame_id(
                0x18FF0102, force_extended_id=True)
            print(f'{name}: lookup -> {chosen.name}')
        except Exception as error:
            print(f'{name}: {type(error).__name__}: {error}')


if __name__ == '__main__':
    group_one()
    group_two()
