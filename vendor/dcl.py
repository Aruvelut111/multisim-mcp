# -*- coding: utf-8 -*-
"""PKWARE DCL (Data Compression Library) —— 纯 Python 实现。

Multisim 的 .ms14 文件 = 头部 + DCL 压缩流。

格式（依据 Mark Adler 的 blast.c 格式说明）：
    第 0 字节  lit   0=字面量不编码  1=字面量用固定 Huffman 编码
    第 1 字节  dict  字典大小 = 2**(dict+6)，合法值 4/5/6 -> 1K/2K/4K
    之后是位流（LSB first）：
        1 bit 标志：0 = 字面量，1 = 长度/距离对
        字面量：lit=0 时 8 bit 原始值；lit=1 时查固定 Huffman 表
        长度/距离对：长度符号 -> base+extra 位；519 为结束码
                     距离符号 -> 左移 (len==2 ? 2 : dict) 位，再加 extra 位，最后 +1

三个 Huffman 表是格式里写死的（rep 为"重复计数+码长"的紧凑编码）。
"""
import struct

MAXBITS = 13
MAXWIN = 4096

# ---- 固定 Huffman 表（紧凑编码：高 4 位=重复次数-1，低 4 位=码长）----
_LITLEN = bytes([
    11, 124, 8, 7, 28, 7, 188, 13, 76, 4, 10, 8, 12, 10, 12, 10, 8, 23, 8,
    9, 7, 6, 7, 8, 7, 6, 55, 8, 23, 24, 12, 11, 7, 9, 11, 12, 6, 7, 22, 5,
    7, 24, 6, 11, 9, 6, 7, 22, 7, 11, 38, 7, 9, 8, 25, 11, 8, 11, 9, 12,
    8, 12, 5, 38, 5, 38, 5, 11, 7, 5, 6, 21, 6, 10, 53, 8, 7, 24, 10, 27,
    44, 253, 253, 253, 252, 252, 252, 13, 12, 45, 12, 45, 12, 61, 12, 45,
    44, 173])
_LENLEN = bytes([2, 35, 36, 53, 38, 23])
_DISTLEN = bytes([2, 20, 53, 230, 247, 151, 248])

BASE = [3, 2, 4, 5, 6, 7, 8, 9, 10, 12, 16, 24, 40, 72, 136, 264]
EXTRA = [0, 0, 0, 0, 0, 0, 0, 0, 1, 2, 3, 4, 5, 6, 7, 8]


def _expand(rep):
    """把紧凑表展开成每个符号的码长列表。"""
    out = []
    for b in rep:
        n = (b >> 4) + 1
        L = b & 15
        out.extend([L] * n)
    return out


def _construct(rep):
    """码长列表 -> (count[], symbol[])，与 blast.c 的 construct() 等价。"""
    length = _expand(rep)
    n = len(length)
    count = [0] * (MAXBITS + 1)
    for L in length:
        count[L] += 1
    offs = [0] * (MAXBITS + 1)
    for L in range(1, MAXBITS):
        offs[L + 1] = offs[L] + count[L]
    symbol = [0] * n
    for s in range(n):
        if length[s]:
            symbol[offs[length[s]]] = s
            offs[length[s]] += 1
    return count, symbol, length


_LIT = _construct(_LITLEN)
_LEN = _construct(_LENLEN)
_DIST = _construct(_DISTLEN)


class _Bits:
    """LSB-first 位读取器。"""
    __slots__ = ("d", "p", "buf", "cnt")

    def __init__(self, data, pos=0):
        self.d = data
        self.p = pos
        self.buf = 0
        self.cnt = 0

    def get(self, need):
        if need == 0:
            return 0
        while self.cnt < need:
            if self.p >= len(self.d):
                raise EOFError("输入耗尽（offset %d）" % self.p)
            self.buf |= self.d[self.p] << self.cnt
            self.p += 1
            self.cnt += 8
        v = self.buf & ((1 << need) - 1)
        self.buf >>= need
        self.cnt -= need
        return v


def _decode(b, table):
    count, symbol, _ = table
    code = first = index = 0
    ln = 1
    while True:
        code |= b.get(1) ^ 1          # 取反，见 blast.c 注释
        c = count[ln]
        if code < first + c:
            return symbol[index + (code - first)]
        index += c
        first = (first + c) << 1
        code <<= 1
        ln += 1
        if ln > MAXBITS:
            raise ValueError("Huffman 解码失败（码字超长）")


def explode(data, pos=0):
    """解压 DCL 流。返回 (明文 bytes, 结束时的字节位置)。"""
    b = _Bits(data, pos)
    lit = b.get(8)
    if lit > 1:
        raise ValueError("lit 标志非法: %d" % lit)
    dict_ = b.get(8)
    if not (4 <= dict_ <= 6):
        raise ValueError("字典大小非法: %d" % dict_)

    out = bytearray()
    while True:
        if b.get(1):
            sym = _decode(b, _LEN)
            ln = BASE[sym] + b.get(EXTRA[sym])
            if ln == 519:                      # 结束码
                break
            shift = 2 if ln == 2 else dict_
            dist = (_decode(b, _DIST) << shift) + b.get(shift) + 1
            if dist > len(out):
                raise ValueError("距离越界: %d > %d" % (dist, len(out)))
            start = len(out) - dist
            if dist >= ln:
                out += out[start:start + ln]
            else:
                for i in range(ln):            # 重叠复制
                    out.append(out[start + i])
        else:
            out.append(_decode(b, _LIT) if lit else b.get(8))
    return bytes(out), b.p


def _build_encoder(table):
    """由解码表反推编码表：符号 -> 位序列。

    必须按该符号的真实码长 L 去搜，否则零填充会让短码误判成别的符号。
    """
    _, _, length = table
    enc = {}
    for s, L in enumerate(length):
        if L == 0:
            continue
        for v in range(1 << L):
            bits = [(v >> i) & 1 for i in range(L)]
            try:
                got = _decode(_Bits(_pack(bits)), table)
            except Exception:
                continue
            if got == s:
                enc[s] = bits
                break
    return enc


def _pack(bits):
    """把位序列按 LSB-first 装成字节。"""
    out = bytearray()
    acc = 0
    n = 0
    for x in bits:
        acc |= (x & 1) << n
        n += 1
        if n == 8:
            out.append(acc)
            acc = 0
            n = 0
    if n:
        out.append(acc)
    return bytes(out)


class _BitWriter:
    __slots__ = ("out", "acc", "n")

    def __init__(self):
        self.out = bytearray()
        self.acc = 0
        self.n = 0

    def put(self, val, nbits):
        if nbits == 0:
            return
        self.acc |= (val & ((1 << nbits) - 1)) << self.n
        self.n += nbits
        while self.n >= 8:
            self.out.append(self.acc & 0xFF)
            self.acc >>= 8
            self.n -= 8

    def put_bits(self, bits):
        for x in bits:
            self.put(x, 1)

    def flush(self):
        if self.n:
            self.out.append(self.acc & 0xFF)
            self.acc = 0
            self.n = 0
        return bytes(self.out)


_ENC_LEN = None
_ENC_DIST = None


def _enc_tables():
    global _ENC_LEN, _ENC_DIST
    if _ENC_LEN is None:
        _ENC_LEN = _build_encoder(_LEN)
        _ENC_DIST = _build_encoder(_DIST)
    return _ENC_LEN, _ENC_DIST


def implode(data, dict_=6, matches=True, min_match=3, max_match=518):
    """压缩成 DCL 流。

    matches=True  -> 字面量不编码(lit=0) + LZ77 匹配，体积通常优于原文
    matches=False -> 纯字面量，体积 = 9/8 * len（最保险）
    """
    enc_len, enc_dist = _enc_tables()
    w = _BitWriter()
    w.put(0, 8)          # lit = 0（字面量不编码，最省事且不会错）
    w.put(dict_, 8)

    n = len(data)
    if not matches:
        for i in range(n):
            w.put(0, 1)
            w.put(data[i], 8)
    else:
        max_dist = 1 << (dict_ + 6)
        # 哈希链必须"边扫描边插入"，否则 head 会指向尚未处理的未来位置，
        # 算出负距离。
        head = {}
        prev = [-1] * n

        def _insert(pos):
            if pos + 2 < n:
                key = data[pos:pos + 3]
                prev[pos] = head.get(key, -1)
                head[key] = pos

        i = 0
        while i < n:
            best_len = 0
            best_dist = 0
            if i + min_match <= n:
                cand = head.get(data[i:i + 3], -1)
                limit = i - max_dist
                chain = 0
                maxl = min(max_match, n - i)
                while cand >= 0 and cand >= limit and chain < 128:
                    ml = 0
                    while ml < maxl and data[cand + ml] == data[i + ml]:
                        ml += 1
                    if ml > best_len:
                        best_len = ml
                        best_dist = i - cand
                        if ml >= maxl:
                            break
                    cand = prev[cand]
                    chain += 1
            if best_len >= min_match:
                w.put(1, 1)
                # 长度编码
                sym = _len_symbol(best_len)
                w.put_bits(enc_len[sym])
                w.put(best_len - BASE[sym], EXTRA[sym])
                # 距离编码：d = dist-1，拆成 高位符号 + 低位额外位
                d = best_dist - 1
                shift = 2 if best_len == 2 else dict_
                w.put_bits(enc_dist[d >> shift])
                w.put(d & ((1 << shift) - 1), shift)
                for pos in range(i, i + best_len):
                    _insert(pos)
                i += best_len
            else:
                w.put(0, 1)
                w.put(data[i], 8)
                _insert(i)
                i += 1

    # 结束码：长度符号 15（264 + 8 extra bits = 519）
    w.put(1, 1)
    w.put_bits(enc_len[15])
    w.put(255, 8)
    return w.flush()


def _len_symbol(ln):
    for s in range(15, -1, -1):
        if ln >= BASE[s] and (ln - BASE[s]) < (1 << EXTRA[s]):
            return s
    raise ValueError("长度超出范围: %d" % ln)


# ---------------- .ms14 容器 ----------------

MAGIC_MSM = b"MSMCompressedElectronicsWorkbenchXML"
MAGIC_PLAIN = b"CompressedElectronicsWorkbenchXML"


def read_ms14(path):
    """读 .ms14 -> (明文 XML, 头部信息)

    容器结构（magic 之后）：
        u32 总明文长度
        u32 0
        u32 第 1 块明文长度      (<= 900000)
        u32 第 1 块压缩长度
        [第 1 块压缩数据]
        u32 第 N 块明文长度       <- 只有 8 字节头
        u32 第 N 块压缩长度
        [第 N 块压缩数据]
        ...
    小文件只有一块，此时"总长 == 第 1 块长"。
    """
    d = open(path, "rb").read()
    if d.startswith(MAGIC_MSM):
        magic = MAGIC_MSM
    elif d.startswith(MAGIC_PLAIN):
        magic = MAGIC_PLAIN
    else:
        raise ValueError("不是 .ms14 文件: %r" % d[:40])
    base = len(magic)
    total, zero, c_unc, c_comp = struct.unpack_from("<IIII", d, base)
    pos = base + 16

    parts = []
    chunks = 0
    while True:
        blob = d[pos:pos + c_comp]
        if len(blob) < c_comp:
            raise ValueError("第 %d 块数据不足：需要 %d 实得 %d"
                             % (chunks + 1, c_comp, len(blob)))
        x, used = explode(blob)
        if used != c_comp:
            raise ValueError("第 %d 块解压后消耗 %d != 压缩长度 %d"
                             % (chunks + 1, used, c_comp))
        parts.append(x)
        chunks += 1
        pos += c_comp
        if pos >= len(d):
            break
        c_unc, c_comp = struct.unpack_from("<II", d, pos)
        pos += 8

    xml = b"".join(parts)
    if len(xml) != total:
        raise ValueError("总长度不符：头部 %d 实得 %d" % (total, len(xml)))
    return xml, {"magic": magic, "total": total, "zero": zero,
                 "chunks": chunks, "body_len": len(d) - base - 16}


def write_ms14(path, xml, matches=True, chunk_limit=900000):
    """把明文 XML 打包成 .ms14（超过 chunk_limit 自动分块）。"""
    chunks = []
    for i in range(0, len(xml), chunk_limit):
        piece = xml[i:i + chunk_limit]
        chunks.append((len(piece), implode(piece, dict_=6, matches=matches)))
    head = bytearray(MAGIC_MSM)
    head += struct.pack("<IIII", len(xml), 0, chunks[0][0], len(chunks[0][1]))
    with open(path, "wb") as f:
        f.write(head)
        f.write(chunks[0][1])
        for unc, body in chunks[1:]:
            f.write(struct.pack("<II", unc, len(body)))
            f.write(body)
    return len(head) + sum(8 + len(b) for _, b in chunks[1:]) + sum(len(b) for _, b in chunks), \
        sum(len(b) for _, b in chunks)


if __name__ == "__main__":
    import os
    import sys

    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    src = sys.argv[1] if len(sys.argv) > 1 else os.path.join(root, "out", "ref", "acceptance.ms14")
    xml, info = read_ms14(src)
    print("解出 %d 字节" % len(xml))
    print("头部:", {k: v for k, v in info.items() if k != "magic"})
    print("开头:", xml[:80])
    for m in (False, True):
        body = implode(xml, matches=m)
        back, _ = explode(body)
        print("压缩 matches=%-5s -> %6d 字节  往返%s" %
              (m, len(body), "一致" if back == xml else "不一致!"))
