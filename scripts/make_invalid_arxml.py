#!/usr/bin/env python3
"""Generate a minimal ARXML 4.2 database with a raw invalid value.

Derived from tests/files/arxml/system-float-values.arxml (a compact
single-frame template). The produced frame Test_Frame contains two
signals:

* IMessage_1: 5-bit linear scale 0.5 offset 10, raw invalid value 31,
* IMessage_2: 7-bit enumerated signal, raw invalid value 127.

The script only reads a checked-in template and writes a new file under
tests/files/quality; it is run manually, not at import time.
"""

from __future__ import annotations

import pathlib

SRC = pathlib.Path(__file__).resolve().parents[1] / "tests/files/arxml/system-float-values.arxml"
DST = pathlib.Path(__file__).resolve().parents[1] / "tests/files/quality/invalid_demo.arxml"

text = SRC.read_text()

# 1) IMessage_1: add INVALID-VALUE inside its NETWORK-REPRESENTATION-PROPS.
old_1 = """            <I-SIGNAL UUID="O9cc19501763c0616ba716743XO9cc19501763c0616ba71672510O9cc19501763c0616ba716725">
              <SHORT-NAME>IMessage_1</SHORT-NAME>"""
new_1 = """            <I-SIGNAL UUID="11111111111111111111111111111111">
              <SHORT-NAME>IMessage_1</SHORT-NAME>"""
assert old_1 in text
text = text.replace(old_1, new_1)

old_1_props = """              <NETWORK-REPRESENTATION-PROPS>
                <SW-DATA-DEF-PROPS-VARIANTS>
                  <SW-DATA-DEF-PROPS-CONDITIONAL>
                    <BASE-TYPE-REF DEST="SW-BASE-TYPE">/DataTypes/BaseTypes/uint8</BASE-TYPE-REF>
                    <COMPU-METHOD-REF DEST="COMPU-METHOD">/DataTypes/CompuMethods/Message_1</COMPU-METHOD-REF>
                  </SW-DATA-DEF-PROPS-CONDITIONAL>
                </SW-DATA-DEF-PROPS-VARIANTS>
              </NETWORK-REPRESENTATION-PROPS>
              <SYSTEM-SIGNAL-REF DEST="SYSTEM-SIGNAL">/Communication/SystemSignals/Message_1</SYSTEM-SIGNAL-REF>"""
new_1_props = """              <NETWORK-REPRESENTATION-PROPS>
                <SW-DATA-DEF-PROPS-VARIANTS>
                  <SW-DATA-DEF-PROPS-CONDITIONAL>
                    <BASE-TYPE-REF DEST="SW-BASE-TYPE">/DataTypes/BaseTypes/uint8</BASE-TYPE-REF>
                    <COMPU-METHOD-REF DEST="COMPU-METHOD">/DataTypes/CompuMethods/Message_1</COMPU-METHOD-REF>
                    <INVALID-VALUE>
                      <NUMERICAL-VALUE-SPECIFICATION>
                        <VALUE>31</VALUE>
                      </NUMERICAL-VALUE-SPECIFICATION>
                    </INVALID-VALUE>
                  </SW-DATA-DEF-PROPS-CONDITIONAL>
                </SW-DATA-DEF-PROPS-VARIANTS>
              </NETWORK-REPRESENTATION-PROPS>
              <SYSTEM-SIGNAL-REF DEST="SYSTEM-SIGNAL">/Communication/SystemSignals/Message_1</SYSTEM-SIGNAL-REF>"""
assert old_1_props in text
text = text.replace(old_1_props, new_1_props)

# 2) IMessage_2: add INVALID-VALUE 127.
old_2_props = """              <NETWORK-REPRESENTATION-PROPS>
                <SW-DATA-DEF-PROPS-VARIANTS>
                  <SW-DATA-DEF-PROPS-CONDITIONAL>
                    <BASE-TYPE-REF DEST="SW-BASE-TYPE">/DataTypes/BaseTypes/uint8</BASE-TYPE-REF>
                    <COMPU-METHOD-REF DEST="COMPU-METHOD">/DataTypes/CompuMethods/Message_2</COMPU-METHOD-REF>
                  </SW-DATA-DEF-PROPS-CONDITIONAL>
                </SW-DATA-DEF-PROPS-VARIANTS>
              </NETWORK-REPRESENTATION-PROPS>
              <SYSTEM-SIGNAL-REF DEST="SYSTEM-SIGNAL">/Communication/SystemSignals/Message_2</SYSTEM-SIGNAL-REF>"""
new_2_props = """              <NETWORK-REPRESENTATION-PROPS>
                <SW-DATA-DEF-PROPS-VARIANTS>
                  <SW-DATA-DEF-PROPS-CONDITIONAL>
                    <BASE-TYPE-REF DEST="SW-BASE-TYPE">/DataTypes/BaseTypes/uint8</BASE-TYPE-REF>
                    <COMPU-METHOD-REF DEST="COMPU-METHOD">/DataTypes/CompuMethods/Message_2</COMPU-METHOD-REF>
                    <INVALID-VALUE>
                      <NUMERICAL-VALUE-SPECIFICATION>
                        <VALUE>127</VALUE>
                      </NUMERICAL-VALUE-SPECIFICATION>
                    </INVALID-VALUE>
                  </SW-DATA-DEF-PROPS-CONDITIONAL>
                </SW-DATA-DEF-PROPS-VARIANTS>
              </NETWORK-REPRESENTATION-PROPS>
              <SYSTEM-SIGNAL-REF DEST="SYSTEM-SIGNAL">/Communication/SystemSignals/Message_2</SYSTEM-SIGNAL-REF>"""
assert old_2_props in text
text = text.replace(old_2_props, new_2_props)

# 3) Give Message_1 a physical scale 0.5 / offset 10 so raw_invalid is
#    demonstrably *not* scaled into a trusted reading.
old_scale = """                  <COMPU-SCALE>
                    <SHORT-LABEL>Message_1</SHORT-LABEL>
                    <LOWER-LIMIT INTERVAL-TYPE="CLOSED">0.0</LOWER-LIMIT>
                    <UPPER-LIMIT INTERVAL-TYPE="CLOSED">31.0</UPPER-LIMIT>
                    <COMPU-RATIONAL-COEFFS>
                      <COMPU-NUMERATOR>
                        <V>0.0</V>
                        <V>1.0</V>
                      </COMPU-NUMERATOR>
                      <COMPU-DENOMINATOR>
                        <V>1</V>
                      </COMPU-DENOMINATOR>
                    </COMPU-RATIONAL-COEFFS>
                  </COMPU-SCALE>"""
new_scale = """                  <COMPU-SCALE>
                    <SHORT-LABEL>Message_1</SHORT-LABEL>
                    <LOWER-LIMIT INTERVAL-TYPE="CLOSED">0.0</LOWER-LIMIT>
                    <UPPER-LIMIT INTERVAL-TYPE="CLOSED">30.0</UPPER-LIMIT>
                    <COMPU-RATIONAL-COEFFS>
                      <COMPU-NUMERATOR>
                        <V>10.0</V>
                        <V>0.5</V>
                      </COMPU-NUMERATOR>
                      <COMPU-DENOMINATOR>
                        <V>1</V>
                      </COMPU-DENOMINATOR>
                    </COMPU-RATIONAL-COEFFS>
                  </COMPU-SCALE>"""
assert old_scale in text
text = text.replace(old_scale, new_scale)

# 4) Turn Message_2 into an enumerated signal: add a value table scale
#    for values 0..2 (the template uses a single LINEAR scale).
old_m2_scale = """            <COMPU-METHOD UUID="O9cc19501763c0616ba70641fXO9cc19501763c0616ba70640110O9cc19501763c0616ba706401">
              <SHORT-NAME>Message_2</SHORT-NAME>
              <CATEGORY>LINEAR</CATEGORY>
              <UNIT-REF DEST="UNIT">/DataTypes/Units/NoUnit</UNIT-REF>
              <COMPU-INTERNAL-TO-PHYS>
                <COMPU-SCALES>
                  <COMPU-SCALE>
                    <SHORT-LABEL>Message_2</SHORT-LABEL>
                    <LOWER-LIMIT INTERVAL-TYPE="CLOSED">0.0</LOWER-LIMIT>
                    <UPPER-LIMIT INTERVAL-TYPE="CLOSED">127</UPPER-LIMIT>
                    <COMPU-RATIONAL-COEFFS>
                      <COMPU-NUMERATOR>
                        <V>0.0</V>
                        <V>1.0</V>
                      </COMPU-NUMERATOR>
                      <COMPU-DENOMINATOR>
                        <V>1</V>
                      </COMPU-DENOMINATOR>
                    </COMPU-RATIONAL-COEFFS>
                  </COMPU-SCALE>
                </COMPU-SCALES>
              </COMPU-INTERNAL-TO-PHYS>
            </COMPU-METHOD>"""
new_m2_scale = """            <COMPU-METHOD UUID="22222222222222222222222222222222">
              <SHORT-NAME>Message_2</SHORT-NAME>
              <CATEGORY>TEXTTABLE</CATEGORY>
              <UNIT-REF DEST="UNIT">/DataTypes/Units/NoUnit</UNIT-REF>
              <COMPU-INTERNAL-TO-PHYS>
                <COMPU-SCALES>
                  <COMPU-SCALE>
                    <SHORT-LABEL>Off</SHORT-LABEL>
                    <LOWER-LIMIT INTERVAL-TYPE="CLOSED">0</LOWER-LIMIT>
                    <UPPER-LIMIT INTERVAL-TYPE="CLOSED">0</UPPER-LIMIT>
                    <COMPU-CONST>
                      <VT>Off</VT>
                    </COMPU-CONST>
                  </COMPU-SCALE>
                  <COMPU-SCALE>
                    <SHORT-LABEL>On</SHORT-LABEL>
                    <LOWER-LIMIT INTERVAL-TYPE="CLOSED">1</LOWER-LIMIT>
                    <UPPER-LIMIT INTERVAL-TYPE="CLOSED">1</UPPER-LIMIT>
                    <COMPU-CONST>
                      <VT>On</VT>
                    </COMPU-CONST>
                  </COMPU-SCALE>
                  <COMPU-SCALE>
                    <SHORT-LABEL>Standby</SHORT-LABEL>
                    <LOWER-LIMIT INTERVAL-TYPE="CLOSED">2</LOWER-LIMIT>
                    <UPPER-LIMIT INTERVAL-TYPE="CLOSED">2</UPPER-LIMIT>
                    <COMPU-CONST>
                      <VT>Standby</VT>
                    </COMPU-CONST>
                  </COMPU-SCALE>
                </COMPU-SCALES>
              </COMPU-INTERNAL-TO-PHYS>
            </COMPU-METHOD>"""
assert old_m2_scale in text
text = text.replace(old_m2_scale, new_m2_scale)

DST.write_text(text)
print(f"wrote {DST}")
