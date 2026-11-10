# -*- coding: utf-8 -*-
"""Helpers for Multisim's XSPICE command channel.

`IMultisimCircuit.DoCommandLine(commandFileName, logFileName)` runs a command
file against the simulator engine. It needs an open document (NewFile() is
enough), and the command file sources a SPICE deck, runs one or more analyses,
and writes each result to an ASCII SPICE3 raw file.

This channel is independent of probes and instruments: it is the reliable way
to get clean, repeatable numeric results from a netlist when the schematic has
no named outputs.

File paths handed to the command engine must not contain spaces.
"""

import os
import re

# Only analysis / plotting commands may come from the caller.
COMMAND_PREFIXES = (
    "op", "dc", "ac", "tran", "tf", "noise", "pz", "sens", "disto",
    "four", "meas", "measure", "let", "print", "plot", "set",
    "linearize", "fourier", "sp",
)

_VAR_LINE = re.compile(r"^\s*(\d+)\s+(\S+)\s+(\S+)\s*(\S*)")
_IDX_LINE = re.compile(r"^\s*(\d+)\s+(.+?)\s*$")


def check_command(cmd):
    """Reject anything that is not an analysis/plot command."""
    s = str(cmd).strip()
    head = s.split(None, 1)[0].lower() if s else ""
    if head not in COMMAND_PREFIXES:
        raise ValueError("command %r is not an analysis command" % s)
    return s


def normalize_netlist(text):
    """Enforce the deck constraints the command engine actually has.

    The engine reads the deck as ASCII: a BOM or a non-ASCII first card makes
    the whole first branch disappear and yields "No nodes present in circuit".
    """
    if not isinstance(text, str) or not text.strip():
        raise ValueError("netlist is empty")
    text = text.lstrip("\ufeff")
    lines = text.replace("\r\n", "\n").replace("\r", "\n").split("\n")
    out = []
    for i, line in enumerate(lines):
        if i == 0 and not line.lstrip().startswith("*"):
            out.append("* multisim mcp deck")
        try:
            line.encode("ascii")
        except UnicodeEncodeError:
            if line.lstrip().startswith("*"):
                line = line.encode("ascii", "replace").decode("ascii")
            else:
                raise ValueError(
                    "netlist card %d contains non-ASCII text: %r" % (i + 1, line))
        out.append(line)
    body = "\n".join(out).rstrip()
    if not any(l.strip().lower() == ".end" for l in out):
        body += "\n.end"
    return body + "\n"


def write_deck(outdir, netlist, commands, prefix="plot"):
    """Write circuit.cir + run.cmd. Returns (cir_path, cmd_path, raw_paths)."""
    outdir = os.path.abspath(outdir)
    if " " in outdir:
        raise ValueError("output directory must not contain spaces: %s" % outdir)
    os.makedirs(outdir, exist_ok=True)
    cmds = [check_command(c) for c in commands]
    if not cmds:
        raise ValueError("no analysis commands given")

    cir = os.path.join(outdir, "circuit.cir")
    with open(cir, "w", encoding="ascii", newline="\n") as f:
        f.write(normalize_netlist(netlist))

    raws = [os.path.join(outdir, "%s%d.raw" % (prefix, i))
            for i in range(len(cmds))]
    lines = ["source %s" % cir]
    for cmd, raw in zip(cmds, raws):
        lines.append(cmd)
        lines.append("write %s" % raw)
    lines.append("quit")
    cmd_path = os.path.join(outdir, "run.cmd")
    with open(cmd_path, "w", encoding="ascii", newline="\n") as f:
        f.write("\n".join(lines) + "\n")
    return cir, cmd_path, raws


def parse_raw(path):
    """Parse an ASCII SPICE3 raw file into (plotname, columns, rows).

    Values section format: each point starts with a line `index<TAB>value`
    (the first variable), followed by one line per remaining variable
    containing just the value. Complex plots carry `re,im` per line.
    """
    with open(path, "r", encoding="utf-8", errors="replace") as f:
        lines = f.read().splitlines()

    plotname, flags = "", "real"
    nvars = npoints = None
    variables = []
    section = None
    values_start = None
    for i, line in enumerate(lines):
        low = line.lower()
        if low.startswith("plotname:"):
            plotname = line.split(":", 1)[1].strip()
        elif low.startswith("flags:"):
            flags = line.split(":", 1)[1].strip().lower()
        elif low.startswith("no. variables:"):
            nvars = int(line.split(":", 1)[1].strip())
        elif low.startswith("no. points:"):
            npoints = int(line.split(":", 1)[1].strip())
        elif low.startswith("variables:"):
            section = "vars"
            continue
        elif low.startswith("values:"):
            section = "values"
            values_start = i + 1
            break
        elif section == "vars":
            m = _VAR_LINE.match(line)
            if m:
                # 官方 raw 变量行是「序号 内部名 类型 可读名+修饰」：
                #   0  n1  voltage  V(vcc)
                #   0  n1  frequency  frequency grid=3
                # 列名取可读名的第一段（去 grid=/prime 等修饰尾巴）
                name = (m.group(4) or m.group(2)).split()[0]
                variables.append(name)
    if values_start is None or nvars is None:
        raise ValueError("raw file is not an ASCII SPICE3 raw: %s" % path)
    if len(variables) != nvars:
        raise ValueError("variable count mismatch: header %d, parsed %d"
                         % (nvars, len(variables)))

    complex_ = "complex" in flags
    rows = []
    cur = None
    for line in lines[values_start:]:
        if not line.strip():
            continue
        # 官方 write 输出：索引行 = " 0\t<val>"（空格开头），
        # 后续变量行 = "\t<val>"（tab 开头）。
        # 用「以 tab 开头」区分值行，别只靠正则——值本身以数字开头时
        # \s*(\d+) 会把值行误判成索引行。
        m = _IDX_LINE.match(line)
        if m and not line.startswith("\t") and not line.startswith(" " * 2 + "\t"):
            if cur is not None and len(cur) == nvars:
                rows.append(cur)
            cur = []
            val = m.group(2)
        else:
            val = line.strip()
        if cur is None:
            continue
        if complex_:
            parts = val.replace(",", " ").split()
            if len(parts) >= 2:
                try:
                    cur.append(complex(float(parts[0]), float(parts[1])))
                except ValueError:
                    cur.append(val)
            else:
                # complex plots still carry a real sweep column (frequency,
                # time, dc sweep) — don't turn those into strings
                try:
                    cur.append(float(parts[0]))
                except (ValueError, IndexError):
                    cur.append(val)
        else:
            try:
                cur.append(float(val.split()[0]))
            except (ValueError, IndexError):
                cur.append(val)
        if len(cur) == nvars:
            rows.append(cur)
            cur = None
    if cur and len(cur) == nvars:
        rows.append(cur)

    if npoints is not None and len(rows) != npoints:
        # Some engines emit a trailing partial point; keep what parsed and
        # let the caller see the mismatch instead of silently truncating.
        pass
    return plotname, variables, rows


def complex_columns(variables, rows):
    """Split complex columns into magnitude(dB) and phase(deg) pairs."""
    import cmath
    import math
    cols = []
    out_rows = [[] for _ in rows]
    for j, name in enumerate(variables):
        vals = [r[j] if j < len(r) else 0 for r in rows]
        # 扫描轴（frequency/time/sweep）在 AC raw 里也写成复数，但它
        # 是横轴：保留实部原值，别拆成 dB/相位那种对数假象。
        if any(isinstance(v, complex) for v in vals) \
                and name.lower() not in ("frequency", "time", "freq.",
                                         "frequency."):
            cols.append(name + " (dB)")
            cols.append(name + " (phase deg)")
            for i, v in enumerate(vals):
                if isinstance(v, complex):
                    mag = abs(v)
                    out_rows[i].append(20 * math.log10(mag) if mag > 0 else -300.0)
                    out_rows[i].append(math.degrees(cmath.phase(v)))
                else:
                    out_rows[i].append(v)
                    out_rows[i].append(0.0)
        elif any(isinstance(v, complex) for v in vals):
            cols.append(name)
            for i, v in enumerate(vals):
                out_rows[i].append(v.real if isinstance(v, complex) else v)
        else:
            cols.append(name)
            for i, v in enumerate(vals):
                out_rows[i].append(v)
    return cols, out_rows
