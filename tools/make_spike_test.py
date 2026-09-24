"""Regenerate mt5/XauSpike_Test.mq5 from mt5/XauSpike.mq5.

The test copy must run exactly the live EA's logic, so it is generated, not
edited: fixed 0.01 lots, its own magic numbers and labels, nothing else.
Run after every change to XauSpike.mq5:  python3 tools/make_spike_test.py
"""
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "mt5" / "XauSpike.mq5"
DST = ROOT / "mt5" / "XauSpike_Test.mq5"

REPLACEMENTS = [
    ("//|                                                   XauSpike.mq5   |",
     "//|                                              XauSpike_Test.mq5   |"),
    ("//| WHAT THIS IS",
     "//| *** TEST COPY *** of XauSpike for the Strategy Tester.\n"
     "//| Identical logic; the only differences are FixedLots = 0.01 on both\n"
     "//| cores, so every trade has the same weight in the report, and its own\n"
     "//| magic numbers. For live trading use XauSpike, whose lots follow the\n"
     "//| account.\n"
     "//|\n"
     "//| WHAT THIS IS"),
    ('#property copyright "XauSpike"', '#property copyright "XauSpike_Test"'),
    ("input long   InpA_Magic            = 770644;", "input long   InpA_Magic            = 770666;"),
    ("input long   InpB_Magic            = 770655;", "input long   InpB_Magic            = 770677;"),
    ("input double InpA_FixedLots        = 0.0;", "input double InpA_FixedLots        = 0.01;"),
    ("input double InpB_FixedLots        = 0.0;", "input double InpB_FixedLots        = 0.01;"),
    ('   string comment = "XauSpike " + g_coreName[i];', '   string comment = "XauSpikeT " + g_coreName[i];'),
    ("   if(tester)\n      Print(\"TESTER",
     '   PrintFormat("TEST COPY: fixed lots A=%.2f B=%.2f. For live trading use XauSpike.", InpA_FixedLots, InpB_FixedLots);\n'
     "   if(!tester)\n"
     '      Print("WARNING: XauSpike_Test is running on a LIVE chart. Its lots do not follow the account. Use XauSpike for live trading.");\n'
     "\n"
     "   if(tester)\n      Print(\"TESTER"),
    ('"XauSpike  |  %s', '"XauSpike TEST (fixed lots)  |  %s'),
]


def main() -> None:
    s = SRC.read_text()
    for old, new in REPLACEMENTS:
        if s.count(old) != 1:
            raise SystemExit(f"expected exactly one occurrence of: {old[:60]!r}")
        s = s.replace(old, new)
    DST.write_text(s)
    print(f"wrote {DST.relative_to(ROOT)} ({DST.stat().st_size} bytes)")


if __name__ == "__main__":
    main()
