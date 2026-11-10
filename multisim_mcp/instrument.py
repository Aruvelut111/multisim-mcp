# -*- coding: utf-8 -*-
"""Virtual instruments (oscilloscope, multimeter, function generator, ...).

Instruments in a `.ms14` design are ordinary `CiComponent` objects with a
`CIITSymbolComp` drawing, so they go through the same donor-clone path as a
resistor: `Assembler.add_part(donor_path=..., donor_refdes=...)`.

They are *not* `CiInstComponent` / `CIITMacroComponent`. That pair is the
hierarchical subcircuit block — in the Buck converter sample, `SC1`/`SC2` are
LC subcircuits, not oscilloscopes. A macro needs the whole hierarchical sheet
registration (ViewState, SheetGroupInfo, XMLSubSheet, ...); inserting one into
an empty design makes Multisim crash with 0xC0000005. The real oscilloscope in
that same sample is `XSC1`.

Reading data back is a separate matter: COM's `EnumOutputs` only knows about
probes, so `msim_connect(probe=True)` is still how an agent gets numbers. The
instrument's job here is to make the schematic correct and readable.
"""

import os

# kind -> donor template. `pins` are the CiPort LocalName values in the donor;
# pin numbering is not always ascending, so keep the real order.
INSTRUMENTS = {
    "oscilloscope": {
        "aliases": ("scope", "oscilloscope", "xsc", "osc", "示波器"),
        "prefix": "XSC",
        "donor": "oscilloscope.ms14",
        "donor_refdes": "XSC1",
        "pins": ["1", "2", "4", "3", "5", "6"],
        "note": "channel A = pin 1, channel B = pin 2; official designs wire "
                "only those two, the rest stay unconnected",
    },
    "multimeter": {
        "aliases": ("multimeter", "meter", "dmm", "xmm", "万用表"),
        "prefix": "XMM",
        "donor": "multimeter.ms14",
        "donor_refdes": "XMM1",
        "pins": ["2", "1"],
        "note": "2-pin multimeter; configure the measurement mode on the panel",
    },
    "function_generator": {
        "aliases": ("function_generator", "fgen", "generator", "xfg",
                    "signal_generator", "信号发生器", "函数发生器"),
        "prefix": "XFG",
        "donor": "function_generator.ms14",
        "donor_refdes": "XFG1",
        "pins": ["1", "2", "3"],
        "note": "3-pin function generator; pin 1 = + (signal), pin 2 = common, "
                "pin 3 = - (verified against donor wiring: 1 drives the load, "
                "2 goes to ground)",
    },
}


def resolve(kind):
    """Resolve an alias / refdes prefix to the canonical kind name."""
    k = str(kind or "").strip().lower()
    for name, sp in INSTRUMENTS.items():
        if k == name or k in sp["aliases"] or k == sp["prefix"].lower():
            return name
    raise KeyError("unknown instrument kind %r; use one of %s"
                   % (kind, ", ".join(sorted(INSTRUMENTS))))


def list_instruments(donor_dir):
    """Return the built-in instrument catalogue for an agent to pick from."""
    out = []
    for kind, sp in sorted(INSTRUMENTS.items()):
        path = os.path.join(donor_dir, sp["donor"])
        out.append({
            "kind": kind,
            "refdes_prefix": sp["prefix"],
            "donor": path,
            "donor_refdes": sp["donor_refdes"],
            "pins": sp["pins"],
            "pin_count": len(sp["pins"]),
            "note": sp["note"],
            "available": os.path.exists(path),
        })
    return out


def default_refdes(kind, used):
    """Pick the next free designator, e.g. XSC1 -> XSC2."""
    return next_refdes(INSTRUMENTS[kind]["prefix"], used)


def next_refdes(prefix, used):
    """Next free designator for an arbitrary prefix (built-in or custom)."""
    prefix = (prefix or "X").upper()
    n = 1
    while "%s%d" % (prefix, n) in used:
        n += 1
    return "%s%d" % (prefix, n)


def add(asm, kind, refdes, x, y, donor_dir, donor=None, donor_refdes=None):
    """Clone one instrument into `asm`. Returns (ciid, pins, donor, refdes)."""
    sp = INSTRUMENTS.get(kind, {})
    path = donor or (os.path.join(donor_dir, sp["donor"])
                     if sp.get("donor") else None)
    dref = donor_refdes or sp.get("donor_refdes")
    if not path or not dref:
        raise ValueError("give both donor and donor_refdes for a custom instrument")
    if not os.path.exists(path):
        raise FileNotFoundError("instrument donor not found: %s" % path)
    ciid, ports = asm.add_part(
        "", "", refdes, donor_path=path, donor_refdes=dref,
        x=float(x), y=float(y))
    return ciid, [p[1] for p in ports], path, dref


# --------------------------------------------------------------- 面板参数
# 仪器的可调参数不在 CiaParamList 里，而在 CiModel 的 SPICE 子电路模板
# 字符串里 —— 函数发生器的模型就是两个受 sin(offset, amplitude, freq,
# delay) 调制的电压源，改模板里的数字等于改面板上的频率/幅值/偏置。
# 万用表同理是 XXMM 子电路，模式由模板内部的源/表结构决定。
WAVEFORMS = ("sine",)


def _model_string(c, ciid):
    """取元件 CiModel 的第一个 CiaCString 内容。"""
    comp = next(e for e in c.elements
                if e.get("Class") == "CiComponent" and e.get("CiID") == str(ciid))
    model = comp.find("CiComponent").get("Model")
    mel = next(e for e in c.elements
               if e.get("Class") == "CiModel" and e.get("CiID") == str(model))
    return mel, mel.find(".//CiaCString")


def _fmt(v):
    return "%.15e" % float(v)


def configure(asm, refdes, freq_hz=None, amplitude=None, offset=None,
              waveform=None):
    """配置函数发生器面板参数。返回 (修改前, 修改后) 的模型文本。

    Semantics match the Multisim panel:
        amplitude  peak amplitude in volts (面板 Amplitude，峰值不是有效值)
        offset     DC offset in volts
        freq_hz    frequency in hertz
        waveform   "sine"（其余波形是另一套子电路模板，未支持前明确报错）

    函数发生器的模型是两个受 sin(offset, amplitude, freq, delay) 调制的
    电压源（ac 幅值与 sin 幅值同步），改模板里的数字等于改面板参数。
    面板上没有参数文字缓存，Multisim 打开文件时按模型实时渲染。
    """
    kind = _kind_of(asm, refdes)
    if kind != "function_generator":
        raise ValueError(
            "%s is a %s; panel parameters are only wired up for the "
            "function generator right now (readings come from probes)"
            % (refdes, kind or "unknown instrument"))
    wf = (waveform or "sine").lower()
    if wf not in WAVEFORMS:
        raise ValueError(
            "waveform %r not supported yet; built-in models only ship "
            "sine. Supported: %s" % (waveform, ", ".join(WAVEFORMS)))

    import re as _re

    c = asm.c
    ref = refdes.upper()
    comp = next(x for x in c.components() if x["refdes"] == ref)
    mel, st = _model_string(c, comp["ciid"])
    old = st.get("String") or ""

    num = r"[-+0-9.eE]+"
    s = old
    if freq_hz is not None or amplitude is not None or offset is not None:
        def repl_sin(m):
            off, amp, frq, dly = m.group(1), m.group(2), m.group(3), m.group(4)
            return "sin(%s %s %s %s)" % (
                _fmt(offset) if offset is not None else off,
                _fmt(amplitude) if amplitude is not None else amp,
                _fmt(freq_hz) if freq_hz is not None else frq,
                dly)
        s, n = _re.subn(r"sin\((%s)\s+(%s)\s+(%s)\s+(%s)\)" % (num, num, num, num),
                        repl_sin, s)
        if n == 0:
            raise ValueError("function-generator model has no sin() source; "
                             "this donor may use another waveform template")

    if amplitude is not None:
        # AC 小信号幅值与瞬态幅值保持一致
        s, n = _re.subn(r"ac\s+(%s)" % num, "ac " + _fmt(amplitude), s)
        if n == 0:
            raise ValueError("no ac amplitude field in the generator model")

    if s == old:
        raise ValueError("nothing to change; give at least one of "
                         "freq_hz / amplitude / offset / waveform")
    st.set("String", s)
    return old, s


def _kind_of(asm, refdes):
    """从会话外部反查位号对应的仪器种类（不依赖 server 的 _session）。"""
    ref = str(refdes).upper()
    try:
        c = asm.c
        comp = next(x for x in c.components() if x["refdes"] == ref)
    except StopIteration:
        raise KeyError("no component %s in the design" % refdes)
    # 元件属性槽 [1] 是家族识别串（&ASCfgen / &ASCscope / &ASCSCOPE…）
    el = next(e for e in c.elements
              if e.get("Class") == "CiComponent"
              and e.get("CiID") == comp["ciid"])
    attrs = el.find("CiComponent/Attributes")
    if attrs is not None:
        texts = []
        for it in attrs:
            cs = it.find("CiaCollString")
            if cs is None:
                continue
            for s in cs.findall(".//Item"):
                texts.append((s.get("Value") or "").replace("&ASC", ""))
        joined = " ".join(texts).lower()
        if "fgen" in joined:
            return "function_generator"
        if "scope" in joined:
            return "oscilloscope"
        if "mulmeter" in joined or "multimeter" in joined:
            return "multimeter"
    return None


def scan_design(path):
    """List every X-prefixed instrument in an arbitrary design (advanced use).

    Handy for instruments outside the built-in three (Bode plotter XBP,
    logic analyser XLA, current probe XCP, LabVIEW instruments XLV, ...).
    """
    import msmodel

    c = msmodel.Circuit.load(path) if str(path).lower().endswith(".ms14") \
        else msmodel.Circuit.from_xml_file(path)
    ports_by_comp = {}
    for el in c.elements:
        if el.get("Class") != "CiPort":
            continue
        cp = el.find("CiPort")
        if cp is not None:
            ports_by_comp.setdefault(cp.get("Component"), []).append(
                (cp.get("LocalName") or "").replace("&ASC", ""))
    out = []
    for comp in c.components():
        ref = (comp["refdes"] or "").strip()
        if not ref.upper().startswith("X"):
            continue
        out.append({
            "refdes": ref,
            "ciid": comp["ciid"],
            "family": comp["family"],
            "part": comp["part"],
            "pins": ports_by_comp.get(comp["ciid"], []),
        })
    return out
