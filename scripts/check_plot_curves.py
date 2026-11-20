#!/usr/bin/env python3
"""Run one command-line scenario and capture what the plot would draw.

Usage:

    python3 scripts/check_plot_curves.py <database> <log> [args...]

The plot subcommand is executed in-process with matplotlib replaced by a
recording stub, so no display and no image file is needed. For every
curve that would be handed to matplotlib, its label (message.signal) and
the plotted y values are printed as JSON. This checks *which message and
signal* was selected, not merely that an image could be produced.
"""

from __future__ import annotations

import io
import json
import os
import sys
from unittest import mock

CANTOOLS_SRC = os.path.join(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__))), 'src')
sys.path.insert(0, CANTOOLS_SRC)

import cantools  # noqa: E402


class RecordingSubplot:
    """Minimal stand-in for a matplotlib subplot that records plot()."""

    def __init__(self, curves):
        self._curves = curves
        self._axis_labels = []

    def plot(self, xs, ys, fmt='', *, label=None):
        if label is not None:
            self._curves.append({'label': label, 'y': list(ys)})
        return []

    def stem(self, xs, ys, fmt='', *, label=None):
        return self.plot(xs, ys, fmt, label=label)

    def axvline(self, *args, **kwargs):
        return None

    def twinx(self):
        return self

    def legend(self, *args, **kwargs):
        return None

    def get_legend_handles_labels(self):
        return [], []

    def set(self, **kwargs):
        return None

    def set_xlabel(self, label):
        self._axis_labels.append(label)

    def set_title(self, *args, **kwargs):
        return None

    def tick_params(self, *args, **kwargs):
        return None

    @property
    def axes(self):
        return self

    @property
    def xaxis(self):
        return mock.MagicMock()

    @property
    def yaxis(self):
        return mock.MagicMock()

    def __getattr__(self, name):
        # Any other matplotlib axis call is irrelevant to which curve is
        # drawn, so tolerate it.
        return mock.MagicMock()


class RecordingPyplot:
    def __init__(self):
        self.curves = []

    def subplot(self, *args, **kwargs):
        return RecordingSubplot(self.curves)

    def figure(self, *args, **kwargs):
        return mock.MagicMock()

    def show(self):
        return None

    def savefig(self, *args, **kwargs):
        return None


def main():
    if len(sys.argv) < 3:
        sys.exit(__doc__)

    database_path, log_path, *extra = sys.argv[1:]
    with open(log_path, encoding='utf-8') as fin:
        log_text = fin.read()

    fake = RecordingPyplot()
    argv = ['cantools', 'plot', *extra, database_path]
    stdout = io.StringIO()
    # The plot module has already bound `plt`; patch that attribute.
    from cantools.subparsers import plot as plot_module
    with mock.patch.object(plot_module, 'plt', fake), \
            mock.patch('sys.stdin', io.StringIO(log_text)), \
            mock.patch('sys.stdout', stdout), \
            mock.patch('sys.argv', argv):
        cantools._main()

    print(json.dumps(fake.curves, indent=2))
    # Diagnostics such as "Unknown frame id" or an ambiguity report are
    # part of the observable result; show them on stderr.
    if stdout.getvalue():
        print(stdout.getvalue(), file=sys.stderr, end='')


if __name__ == '__main__':
    main()
