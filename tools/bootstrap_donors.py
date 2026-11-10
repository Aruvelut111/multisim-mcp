# -*- coding: utf-8 -*-
"""从本机已有的 .ms14 文件提取本项目的 donor 模板包。

NI 样例衍生的电路文件不属于 MIT 授权范围，因此仓库不携带它们；
本脚本扫描你指定的目录（NI 样例目录、你自己的旧电路、任何含 .ms14
的集合），按用途挑出需要的文件、复制成固定命名的模板包：

    data/blank.ms14          最小基底（元件最少的样例）
    data/donors/probe_donor.ms14       含探针的样例（探针符号克隆源）
    data/donors/oscilloscope.ms14      示波器 XSC
    data/donors/multimeter.ms14        万用表 XMM
    data/donors/function_generator.ms14 函数发生器 XFG
    data/donors/<family>.ms14          KIND_MAP 里每个元件家族一份

用法：
    python tools/bootstrap_donors.py --scan "C:\\path\\to\\samples" [--scan ...]

重复执行安全：已存在的输出文件会被覆盖，SOURCES.md 记录每个文件
的来源路径（仅本地保留）。
"""
import argparse
import json
import os
import shutil
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "vendor"))
import msmodel  # noqa: E402

KIND_MAP = {
    "resistor": ("RESISTOR", None), "resistor_rated": ("RATED_VIRTUAL", None),
    "capacitor": ("CAPACITOR", None), "capacitor_rated": ("RATED_VIRTUAL", None),
    "inductor": ("INDUCTOR", None),
    "dc_source": ("POWER_SOURCES", None), "ac_source": ("POWER_SOURCES", None),
    "ground": ("POWER_SOURCES", None),
    "signal_ac": ("SIGNAL_VOLTAGE_SOURCES", None),
    "diode": ("DIODE", None), "led": ("LED", None),
    "npn": ("BJT_NPN", None), "nmos": ("MOS_ENH_N", None),
    "opamp": ("OPAMP", None), "opamp_ideal": ("ANALOG_VIRTUAL", None),
    "zener": ("ZENER", None), "potentiometer": ("POTENTIOMETER", None),
    "capacitor_el": ("CAP_ELECTROLIT", None), "switch_spdt": ("SWITCH", None),
    "electrolytic_probe": ("CAP_ELECTROLIT", None),
}
FAMILIES = sorted({fam for fam, _ in KIND_MAP.values()})
INSTRUMENTS = {"oscilloscope": "XSC", "multimeter": "XMM",
               "function_generator": "XFG"}


def scan_file(path):
    """返回 (families 集合, 仪器位号前缀集合, 元件数, 有无探针)。"""
    try:
        c = msmodel.Circuit.load(path)
    except Exception as e:
        return None, None, 0, False, str(e)
    fams = set()
    count = 0
    for x in c.components():
        fam = x.get("family") or ""
        if fam:
            fams.add(fam)
        count += 1
    instrs = set()
    has_probe = False
    for eobj in c.objects:
        # 仪器位号（CIITSymTextCompName@Output）和探针都是内嵌在
        # 符号 Item 子树里的，必须深扫
        for sub in eobj.iter():
            cls = sub.get("Class")
            if cls == "CIITProbeExtComponent":
                has_probe = True
            elif cls == "CIITSymTextCompName":
                nm = (sub.get("Output") or "").replace("&ASC", "")
                for tag in INSTRUMENTS.values():
                    if nm.startswith(tag):
                        instrs.add(tag)
    # 仪器也可能是普通元件家族（XFG 家族 '@'）
    for x in c.components():
        if (x.get("family") or "") == "@":
            pass  # 仪器以绘图对象为准
    return fams, instrs, count, has_probe, None


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--scan", action="append", required=True,
                    help="要扫描的目录（可多次）")
    ap.add_argument("--out", default=os.path.join(ROOT, "data"),
                    help="模板包输出根目录（默认 data/）")
    args = ap.parse_args()

    out_donor = os.path.join(args.out, "donors")
    os.makedirs(out_donor, exist_ok=True)
    sources = []

    # 扫描
    catalog = []
    seen_dirs = []
    for d in args.scan:
        d = os.path.abspath(d)
        if not os.path.isdir(d):
            print("跳过（不是目录）:", d)
            continue
        seen_dirs.append(d)
        for root, _dirs, files in os.walk(d):
            for f in sorted(files):
                if not f.lower().endswith(".ms14"):
                    continue
                p = os.path.join(root, f)
                fams, instrs, count, has_probe, err = scan_file(p)
                if err is not None:
                    print("  无法解析:", p, err[:80])
                    continue
                catalog.append({"path": p, "families": sorted(fams),
                                "instruments": sorted(instrs),
                                "components": count, "probe": has_probe})
    print("扫描完成： %d 个 .ms14" % len(catalog))
    if not catalog:
        sys.exit("扫描目录里没有可解析的 .ms14 —— 给一个含 NI 样例或你"
                 "自己电路的目录")

    def pick(pred, prefer_small=True):
        cand = [x for x in catalog if pred(x)]
        if not cand:
            return None
        return min(cand, key=lambda x: x["components"]) if prefer_small \
            else cand[0]

    def copy(src, dst_name):
        if os.sep in dst_name or "/" in dst_name:
            dst = os.path.join(args.out, dst_name)
        else:
            dst = os.path.join(args.out if dst_name == "blank.ms14"
                               else out_donor, dst_name)
        shutil.copy2(src["path"], dst)
        sources.append({"file": dst_name, "source": src["path"],
                        "components": src["components"]})
        print("  %-34s <- %s" % (dst_name, os.path.basename(src["path"])))
        return dst

    print("提取：")
    # blank：元件最少的样例
    blank = pick(lambda x: x["components"] >= 1)
    got_blank = False
    if blank:
        copy(blank, "blank.ms14")
        got_blank = True
    # 探针 donor：含探针对象
    pd = pick(lambda x: x["probe"])
    if pd:
        copy(pd, os.path.join("donors", "probe_donor.ms14"))
    # 仪器
    for name, tag in INSTRUMENTS.items():
        hit = pick(lambda x, tag=tag: tag in x["instruments"])
        if hit:
            copy(hit, os.path.join("donors", name + ".ms14"))
        else:
            print("  缺 %s donor（扫描目录里没有含 %s 的样例）" % (name, tag))
    # 家族覆盖
    missing_fams = []
    for fam in FAMILIES:
        hit = pick(lambda x, fam=fam: fam in x["families"])
        if hit:
            safe = "".join(ch for ch in fam if ch.isalnum() or ch == "_")
            copy(hit, os.path.join("donors", "family_%s.ms14" % safe))
        else:
            missing_fams.append(fam)

    # SOURCES.md（本地保留，不入库）
    with open(os.path.join(args.out, "SOURCES.md"), "w",
              encoding="utf-8") as f:
        f.write("# 模板包来源（本地文件，不入库）\n\n")
        f.write("扫描目录：\n")
        for d in seen_dirs:
            f.write("- %s\n" % d)
        f.write("\n| 文件 | 来源 | 元件数 |\n|---|---|---|\n")
        for s in sources:
            f.write("| `%s` | `%s` | %d |\n" % (s["file"], s["source"],
                                                s["components"]))
        if missing_fams:
            f.write("\n缺失家族：%s\n" % ", ".join(missing_fams))
    with open(os.path.join(args.out, "SOURCES.json"), "w",
              encoding="utf-8") as f:
        json.dump({"scanned": seen_dirs, "files": sources,
                   "missing_families": missing_fams}, f, indent=1)

    print("完成：%d 个文件 -> %s" % (len(sources), args.out))
    if missing_fams:
        print("缺失家族（对应 kind 将不可用）:", ", ".join(missing_fams))
    if not got_blank:
        print("⚠ 没有 blank 基底 —— MSIM_BLANK 环境变量指向一个"
              "最小的 .ms14 才能建新电路")


if __name__ == "__main__":
    main()
