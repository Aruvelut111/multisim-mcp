"""
.ms14 容器读写

结构：
  [36 字节 magic]
  [u32 明文总长][u32 0][u32 首块明文长][u32 首块压缩长]
  [首块 DCL 数据]
  [u32 块明文长][u32 块压缩长][块数据] ...   ← 后续块只有 8 字节头
"""
import struct
from .dcl import explode, implode

MAGIC_MSM = b"MSMCompressedElectronicsWorkbenchXML"
MAGIC_PLAIN = b"CompressedElectronicsWorkbenchXML"
CHUNK_LIMIT = 900000


def read_ms14(path):
    """返回 (xml_bytes, meta)"""
    with open(path, "rb") as f:
        d = f.read()

    if d.startswith(MAGIC_MSM):
        magic = MAGIC_MSM
    elif d.startswith(MAGIC_PLAIN):
        magic = MAGIC_PLAIN
    else:
        raise ValueError("不是 .ms14 文件：magic 不匹配")

    base = len(magic)
    total, zero, c_unc, c_comp = struct.unpack_from("<IIII", d, base)
    pos = base + 16

    parts = []
    blocks = []
    while True:
        xml, used = explode(d[pos:pos + c_comp])
        if used != c_comp:
            raise ValueError(f"压缩块长度不符：used={used} expect={c_comp}")
        parts.append(xml)
        blocks.append((c_unc, c_comp))
        pos += c_comp
        if pos >= len(d):
            break
        c_unc, c_comp = struct.unpack_from("<II", d, pos)
        pos += 8

    xml = b"".join(parts)
    if len(xml) != total:
        raise ValueError(f"总长度不符：{len(xml)} != {total}")

    return xml, {
        "magic": magic.decode(),
        "total": total,
        "blocks": blocks,
        "file_size": len(d),
    }


def write_ms14(path, xml, chunk_limit=CHUNK_LIMIT, min_match=3, chain=64):
    """把 XML 写成 .ms14"""
    if isinstance(xml, str):
        xml = xml.encode("utf-8")

    chunks = []
    for i in range(0, len(xml), chunk_limit):
        part = xml[i:i + chunk_limit]
        comp = implode(part, dict_=6, min_match=min_match, chain=chain)
        chunks.append((len(part), comp))

    out = bytearray(MAGIC_MSM)
    out += struct.pack("<IIII", len(xml), 0, chunks[0][0], len(chunks[0][1]))
    out += chunks[0][1]
    for unc, comp in chunks[1:]:
        out += struct.pack("<II", unc, len(comp))
        out += comp

    with open(path, "wb") as f:
        f.write(out)
    return len(out)
