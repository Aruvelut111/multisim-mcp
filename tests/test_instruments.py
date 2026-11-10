# -*- coding: utf-8 -*-
"""Instrument placement / wiring / simulation regression.

Run from the project root:
    python tests/test_instruments.py

Needs Multisim installed locally for the COM checks.
"""
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "vendor"))

import multisim_mcp.server as S

OUT = os.path.join(ROOT, "out")
os.makedirs(OUT, exist_ok=True)
PASS = FAIL = 0


def ck(label, cond, extra=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print("  [PASS] %s %s" % (label, extra))
    else:
        FAIL += 1
        print("  [FAIL] %s %s" % (label, extra))


print("=== 1. Instrument catalogue ===")
li = S.msim_list_instruments()
items = {it["kind"]: it for it in li.get("instruments", [])}
ck("catalogue ok", li.get("ok"), "")
ck("all three built-ins present",
   set(items) >= {"oscilloscope", "multimeter", "function_generator"},
   str(sorted(items)))
ck("all three donors available",
   all(it.get("available") for it in items.values()),
   str({k: v.get("available") for k, v in items.items()}))
ck("pin names published",
   all(it.get("pins") for it in items.values()),
   str({k: v.get("pins") for k, v in items.items()}))

print("\n=== 2. Custom donor discovery ===")
fg_donor = os.path.join(ROOT, "data", "donors", "function_generator.ms14")
if os.path.exists(fg_donor):
    cd = S.msim_list_instruments(donor=fg_donor)
    refs = [it.get("donor_refdes") for it in cd.get("instruments", [])]
    ck("XFG1 found in donor", cd.get("ok") and "XFG1" in refs, str(refs))
else:
    ck("XFG donor present", False, fg_donor)

print("\n=== 2b. Custom donor placement ===")
S.msim_new_circuit()
r1 = S.msim_add_instrument("custom", donor=fg_donor, donor_refdes="XFG1")
ck("custom instrument placed", r1.get("ok"), str(r1.get("error", {}))[:160])
r2 = S.msim_add_instrument("custom", donor=fg_donor, donor_refdes="XFG1")
ck("second custom auto-increments",
   r2.get("ok") and r2.get("refdes") != (r1.get("refdes") or ""),
   "%s then %s" % (r1.get("refdes"), r2.get("refdes")))
bad = S.msim_add_instrument("custom",
                            donor=os.path.join(OUT, "missing.ms14"),
                            donor_refdes="XFG1")
ck("missing donor rejected",
   not bad.get("ok") and bad["error"]["code"] == "NO_DONOR",
   bad.get("error", {}).get("code", ""))

print("\n=== 3. Build a circuit with three instruments ===")
S.msim_new_circuit()
for ref, kind, val, x, y in [
    ("V1", "dc_source", 5, 180, 360),
    ("R1", "resistor", 1000, 360, 300),
    ("R2", "resistor", 1000, 480, 450),
    ("GND1", "ground", None, 180, 540),
]:
    r = S.msim_add_part(ref, kind, val, x, y)
    ck("add %s" % ref, r.get("ok"), r.get("error", {}).get("message", ""))

placed = {}
for kind, expect in [("oscilloscope", "XSC1"),
                     ("multimeter", "XMM1"),
                     ("function_generator", "XFG1")]:
    r = S.msim_add_instrument(kind)
    placed[kind] = r
    ck("place %s" % kind, r.get("ok") and r.get("refdes") == expect,
       "%s %s" % (r.get("refdes"), r.get("error", {}).get("message", "")))

# Instruments are wide panels: they must sit in their own column to the
# right of every ordinary part (auto layout keeps >=180 units of clearance),
# and must not stack on top of each other.
ats = [r.get("at") for r in placed.values() if r.get("ok")]
ck("instruments clear of the parts",
   all(a and a[0] >= 630 for a in ats), str(ats))
ck("instrument positions distinct",
   len({tuple(a) for a in ats}) == 3, str(ats))

exp_pins = {
    "oscilloscope": ["1", "2", "4", "3", "5", "6"],
    "multimeter": ["2", "1"],
    "function_generator": ["1", "2", "3"],
}
for kind, pins in exp_pins.items():
    ck("%s pin list" % kind, placed[kind].get("pins") == pins,
       str(placed[kind].get("pins")))

bad = S.msim_add_instrument("no_such_instrument")
ck("unknown kind rejected",
   not bad.get("ok") and bad["error"]["code"] == "NO_INSTRUMENT",
   bad.get("error", {}).get("code", ""))

print("\n=== 4. Wires and probes ===")
nets = [
    ("1", ["V1.1", "R1.1"], False),
    ("2", ["R1.2", "R2.1"], True),
    ("0", ["R2.2", "GND1.1"], False),
    ("0", ["V1.2", "GND1.1"], False),
    ("2", ["XSC1.1"], False),
    ("0", ["XSC1.3"], False),
    ("2", ["XMM1.1"], False),
    ("0", ["XMM1.2"], False),
]
for name, pins, probe in nets:
    r = S.msim_connect(name, pins, probe=probe)
    ck("net %-3s %s" % (name, ",".join(pins)), r.get("ok"),
       r.get("error", {}).get("code", ""))

p = os.path.join(OUT, "test_instruments.ms14")
s = S.msim_save(p)
ck("save", s.get("ok"), s.get("error", {}).get("message", ""))
comps = s.get("components") or []
ck("all instruments in saved design",
   all(x in comps for x in ("XSC1", "XMM1", "XFG1")), str(comps))
ck("file written", os.path.exists(p) and os.path.getsize(p) > 0,
   str(os.path.getsize(p) if os.path.exists(p) else 0))

print("\n=== 5. Open, simulate, export ===")
if s.get("ok"):
    o = S.msim_open(p)
    ck("open with instruments", o.get("ok"),
       o.get("error", {}).get("message", ""))
    if o.get("ok"):
        outs = (o.get("data") or {}).get("outputs") or []
        ck("probe visible to COM", outs == ["V(PR1)"], str(outs))
        d = S.msim_run_dc()
        ck("run_dc", d.get("ok"), d.get("error", {}).get("message", ""))
        if outs:
            g = S.msim_get_output(outs[0])
            pts = (g.get("data") or {}).get("points") or []
            v = pts[0][1] if pts and len(pts[0]) > 1 else None
            ck("divider midpoint still 2.5V with instruments",
               v is not None and abs(v - 2.5) < 0.01, "got %s" % v)
        img = os.path.join(OUT, "test_instruments.png")
        e = S.msim_export_image(img, 0)
        ck("export_image", e.get("ok") and os.path.exists(img)
           and os.path.getsize(img) > 1000, str(e)[:160])
    S.msim_close()
else:
    ck("open with instruments", False, "skipped: save failed")

print("\n=== %d passed, %d failed ===" % (PASS, FAIL))
sys.exit(1 if FAIL else 0)
