# -*- coding: utf-8 -*-
"""mssafe —— 无损 XML 往返。

## 为什么需要这个模块

Multisim 把**仿真状态**序列化成一段多行文本，塞进 `CIITDiagram` 的
`SimState` 属性里，原始 XML 长这样：

    <CIITDiagram Class="CIITDiagram" ... SimState="&amp;ASCSIMULATION_INFO:list{ANALYSES:list{
    ANALYSIS_LIST:list{}
    ANALYSIS:list{
    ...
    }}}"/>

注意属性值里是**真的换行**（不是 `&#10;`）。

XML 1.0 规范 3.3.3「属性值归一化」规定：解析器必须把属性值里的字面换行、
回车、制表符一律替换成空格。ElementTree 严格遵守这条规范 —— 于是这块
多行数据被压平成一行，Multisim 自己的解析器读不回来，仿真配置丢失。

实测后果（同一份原生文件，只做 XML 往返、逻辑零改动）：

    原生              V(PR1)=84V  I(PR1)=13.9977A   8/8 输出可读
    纯容器重打包       V(PR1)=84V  I(PR1)=13.9977A   8/8   ← 容器无损
    ET 解析+序列化     V(PR1)=84V  I(PR1)=0          3/8   ← 元凶

表现就是「电源电压还在、电阻电流为 0、部分输出读不出来」—— 也就是
长期误判为「克隆的电阻是开路的」那个现象。

## 做法

解析前把属性值内部的 \\r \\n \\t 换成 ASCII 占位符，序列化后再换回来。
占位符不含 XML 特殊字符，不会被转义，也不会被归一化。

## 用法

    import mssafe
    raw = dcl.read_ms14(path)[0]
    text = mssafe.roundtrip(raw)          # 只往返，不改
    text = mssafe.roundtrip(raw, mutator) # 先往返再交给回调改 DOM

或者直接用 msmodel.Circuit.load/save —— 它已经改用本模块。
"""

_CR = "@@WB_CR@@"
_NL = "@@WB_NL@@"
_TAB = "@@WB_TAB@@"

# 原始声明形如 <?xml version="1.0" encoding="ASCII"?>
# ElementTree 会写成单引号 + 小写 ascii，这里手动还原成原样。
_DECL = b'<?xml version="1.0" encoding="ASCII"?>\n'


def protect(raw):
    """把属性值内部的 \\r/\\n/\\t 换成占位符。raw/返回都是 bytes。"""
    if isinstance(raw, str):
        raw = raw.encode("latin-1")
    s = raw.decode("latin-1")          # latin-1 = 1:1 字节映射，不丢信息
    out = []
    i, n = 0, len(s)
    in_val = False
    q = None
    while i < n:
        ch = s[i]
        if in_val:
            if ch == q:
                in_val = False
                out.append(ch)
            elif ch == "\r":
                out.append(_CR)
            elif ch == "\n":
                out.append(_NL)
            elif ch == "\t":
                out.append(_TAB)
            else:
                out.append(ch)
        else:
            out.append(ch)
            if ch in "\"'":
                # 只有「前面（跳过空白）是 =」时，这个引号才是属性值起始
                j = i - 1
                while j >= 0 and s[j] in " \t\r\n":
                    j -= 1
                if j >= 0 and s[j] == "=":
                    in_val = True
                    q = ch
        i += 1
    return "".join(out).encode("latin-1")


def restore(raw):
    """把占位符换回真实控制字符。"""
    if isinstance(raw, str):
        raw = raw.encode("latin-1")
    return (raw.replace(_CR.encode("latin-1"), b"\r")
               .replace(_NL.encode("latin-1"), b"\n")
               .replace(_TAB.encode("latin-1"), b"\t"))


def parse(raw):
    """解析 XML，同时保住属性值里的换行。返回 (root, 无)。"""
    import xml.etree.ElementTree as ET
    return ET.fromstring(protect(raw))


def serialize(root):
    """把 DOM 序列化回字节，还原属性值里的换行，并写回原始声明。"""
    import xml.etree.ElementTree as ET
    # ET.tostring 不输出根元素的 tail（即根标签之后、文件末尾的那段文本，
    # 通常就是一个换行）。不补回来，每次往返都会少一个字节。
    tail = root.tail or ""
    body = ET.tostring(root, encoding="ascii", xml_declaration=False)
    out = _DECL + restore(body) + tail.encode("latin-1")
    # 根元素之后的那个换行不在 DOM 里（fromstring 不记录根 tail），
    # Multisim 自己写盘时一定有，这里统一补齐，保证往返字节数不变。
    if not out.endswith(b"\n"):
        out += b"\n"
    return out


def roundtrip(raw):
    """只做无损往返（用于验证）。"""
    return serialize(parse(raw))


def self_check(raw):
    """返回 (是否与原文完全一致, 首个差异位置或 None)。"""
    out = roundtrip(raw)
    if out == raw:
        return True, None
    for i in range(min(len(raw), len(out))):
        if raw[i:i + 1] != out[i:i + 1]:
            return False, i
    return False, min(len(raw), len(out))
