"""Regression tests for multisim-mcp.

Run from the project root:
    python tests/test_regression.py
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
        print(f"  [PASS] {label} {extra}")
    else:
        FAIL += 1
        print(f"  [FAIL] {label} {extra}")


print("=== 1. UTF-8 path handling ===")
cn_path = os.path.join(OUT, "中文测试电路.ms14")
spec = {
    "path": cn_path,
    "components": [
        {"ref": "V1", "kind": "dc_source", "value": 5, "at": [180, 360]},
        {"ref": "R1", "kind": "resistor", "value": 1000, "at": [360, 300]},
        # 故意与 R1 错开 X：探针只能落在水平导线段上，两个电阻完全
        # 垂直对齐时这条网络没有水平段，PROBE_NO_HORIZONTAL 是预期行为。
        {"ref": "R2", "kind": "resistor", "value": 1000, "at": [480, 450]},
        {"ref": "GND1", "kind": "ground", "at": [180, 540]},
    ],
    "nets": [
        {"name": "1", "pins": ["V1.1", "R1.1"]},
        {"name": "2", "pins": ["R1.2", "R2.1"], "probe": True},
        {"name": "0", "pins": ["R2.2", "GND1.1"]},
        {"name": "0", "pins": ["V1.2", "GND1.1"]},
    ],
}
r = S.msim_build(spec)
ck("build with non-ASCII path", r.get("ok"), r.get("error", {}).get("message", ""))

o = S.msim_open(cn_path)
outs = (o.get("data") or {}).get("outputs") or []
ck("open with non-ASCII path", o.get("ok") and outs, str(outs))

if outs:
    S.msim_run_dc()
    g = S.msim_get_output(outs[0])
    pts = (g.get("data") or {}).get("points") or []
    v = pts[0][1] if pts else None
    ck("divider midpoint = 2.5V", v is not None and abs(v - 2.5) < 0.01, f"got {v}")
S.msim_close()

print("\n=== 2. Pin conflict detection ===")
S.msim_new_circuit()
S.msim_add_part("R1", "resistor", 1000, 200, 200)
S.msim_add_part("R2", "resistor", 1000, 400, 200)
a = S.msim_connect("na", ["R1.1", "R2.1"])
ck("first net accepted", a.get("ok"))
b = S.msim_connect("nb", ["R1.1", "R2.2"])
ck("same pin in second net rejected",
   not b.get("ok") and b["error"]["code"] == "PIN_CONFLICT",
   b.get("error", {}).get("code", ""))

print("\n=== 3. Transistor pins must use letters ===")
S.msim_new_circuit()
S.msim_add_part("Q1", "npn", None, 400, 300)
c = S.msim_connect("base", ["Q1.1"])
ck("Q1.1 rejected", not c.get("ok") and c["error"]["code"] == "BAD_PIN",
   c.get("error", {}).get("code", ""))
d = S.msim_connect("base", ["Q1.B"])
ck("Q1.B accepted", d.get("ok"), d.get("error", {}).get("message", ""))

print("\n=== 4. Probe on ground net rejected ===")
e = S.msim_connect("gnd", ["Q1.E"], probe=True)
ck("probe on ground rejected", not e.get("ok"), e.get("error", {}).get("code", ""))

print("\n=== 5. Invalid probe_at rejected ===")
S.msim_new_circuit()
S.msim_add_part("R1", "resistor", 1000, 300, 300)
S.msim_add_part("R2", "resistor", 1000, 300, 450)
f = S.msim_connect("mid", ["R1.2", "R2.1"], probe=True, probe_at=[300, 9999])
ck("off-wire coordinate rejected", not f.get("ok"), f.get("error", {}).get("code", ""))

print("\n=== 6. Designator case folding ===")
S.msim_new_circuit()
S.msim_add_part("R1", "resistor", 1000, 200, 200)
g2 = S.msim_add_part("r1", "resistor", 1000, 400, 200)
ck("r1 treated as duplicate",
   not g2.get("ok") and g2["error"]["code"] == "DUP_REFDES",
   g2.get("error", {}).get("code", ""))

print("\n=== 7. Output extension check ===")
h = S.msim_save(os.path.join(OUT, "bad.txt"))
ck("non-.ms14 rejected",
   not h.get("ok") and h["error"]["code"] == "BAD_PATH",
   h.get("error", {}).get("code", ""))

print(f"\n=== {PASS} passed, {FAIL} failed ===")
sys.exit(1 if FAIL else 0)
