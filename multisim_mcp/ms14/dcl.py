"""
PKWARE DCL (implode) 解压/压缩 —— .ms14 的载荷算法

依据 zlib contrib/blast/blast.c（Mark Adler，公有领域）里的格式说明。
该格式在 1990 年代的 Windows 软件中广泛使用。

格式要点：
  位流 LSB first
  [8bit] lit   0=字面量不编码 / 1=字面量走 Huffman
  [8bit] dict  log2(字典)-6 → 4/5/6 表示 1K/2K/4K
  循环：
    [1bit] flag
      0 → 字面量
      1 → 长度/距离对
  结束码：长度符号 15 + 8 个额外位全 1（=519）
"""

# 三个固定 Huffman 表的紧凑编码：高 4 位=重复次数-1，低 4 位=码长
_LITLEN = bytes([
    11, 124, 8, 7, 28, 7, 188, 13, 76, 4, 10, 8, 12, 10, 12, 10, 8, 23, 8,
    9, 7, 6, 7, 8, 7, 6, 55, 8, 23, 24, 12, 11, 7, 9, 11, 12, 6, 7, 22, 5,
    7, 24, 6, 11, 9, 6, 7, 22, 7, 11, 38, 7, 9, 8, 25, 11, 8, 11, 9, 12,
    8, 12, 5, 38, 5, 38, 5, 11, 7, 5, 6, 21, 6, 10, 53, 8, 7, 24, 10, 27,
    44, 253, 253, 253, 252, 252, 252, 13, 12, 45, 12, 45, 12, 61, 12, 45,
    44, 173])
_LENLEN = bytes([2, 35, 36, 53, 38, 23])
_DISTLEN = bytes([2, 20, 53, 230, 247, 151, 248])

# 长度符号表
_LEN_BASE = [3, 2, 4, 5, 6, 7, 8, 9, 10, 12, 16, 24, 40, 72, 136, 264]
_LEN_EXTRA = [0, 0, 0, 0, 0, 0, 0, 0, 1, 2, 3, 4, 5, 6, 7, 8]


def _expand(rep):
    out = []
    for b in rep:
        out.extend([b & 15] * ((b >> 4) + 1))
    return out


def _build_table(rep):
    """由码长序列构造 canonical Huffman 解码表：(count, symbol, length)"""
    length = _expand(rep)
    max_len = max(length)
    count = [0] * (max_len + 1)
    for ln in length:
        if ln:
            count[ln] += 1
    offs = [0] * (max_len + 2)
    for i in range(1, max_len + 1):
        offs[i + 1] = offs[i] + count[i]
    symbol = [0] * sum(count)
    for s, ln in enumerate(length):
        if ln:
            symbol[offs[ln]] = s
            offs[ln] += 1
    return count, symbol, length


class _Bits:
    __slots__ = ("data", "pos", "acc", "cnt")

    def __init__(self, data, start=0):
        self.data = data
        self.pos = start
        self.acc = 0
        self.cnt = 0

    def get(self, n):
        while self.cnt < n:
            if self.pos >= len(self.data):
                raise EOFError("bit stream exhausted")
            self.acc |= self.data[self.pos] << self.cnt
            self.pos += 1
            self.cnt += 8
        v = self.acc & ((1 << n) - 1)
        self.acc >>= n
        self.cnt -= n
        return v


def _decode(b, table):
    count, symbol, _ = table
    code = first = index = 0
    ln = 1
    while True:
        code |= b.get(1) ^ 1
        c = count[ln]
        if code < first + c:
            return symbol[index + (code - first)]
        index += c
        first = (first + c) << 1
        code <<= 1
        ln += 1


def explode(src, max_out=200 * 1024 * 1024):
    """解压一段 DCL 流，返回 (明文, 消耗的输入字节数)"""
    b = _Bits(src)
    lit = b.get(8)
    dict_ = b.get(8)
    if dict_ < 4 or dict_ > 6:
        raise ValueError(f"bad dict size: {dict_}")
    dict_size = 1 << (dict_ + 6)

    lit_table = _build_table(_LITLEN) if lit else None
    len_table = _build_table(_LENLEN)
    dist_table = _build_table(_DISTLEN)

    out = bytearray()
    while True:
        if b.get(1):
            sym = _decode(b, len_table)
            extra = b.get(_LEN_EXTRA[sym])
            # 结束码：符号 15 + 8 个额外位全 1（=519）
            if sym == 15 and extra == 0xFF:
                break
            ln = _LEN_BASE[sym] + extra
            dsym = _decode(b, dist_table)
            shift = 2 if ln == 2 else dict_
            dist = (dsym << shift) + b.get(shift) + 1
            if dist > len(out):
                raise ValueError(f"bad distance {dist} > out {len(out)}")
            start = len(out) - dist
            for k in range(ln):
                out.append(out[start + k])
        else:
            if lit:
                out.append(_decode(b, lit_table))
            else:
                out.append(b.get(8))

        if len(out) > max_out:
            raise ValueError("output too large")

    return bytes(out), b.pos


# ---------------------------------------------------------------- 压缩

def _build_encoder(table):
    _, _, length = table
    enc = {}
    for s, L in enumerate(length):
        if L == 0:
            continue
        for v in range(1 << L):
            bits = [(v >> i) & 1 for i in range(L)]
            if _decode(_Bits(_packbits(bits)), table) == s:
                enc[s] = bits
                break
    return enc


def _packbits(bits):
    out = bytearray()
    acc = 0
    cnt = 0
    for bit in bits:
        acc |= bit << cnt
        cnt += 1
        if cnt == 8:
            out.append(acc)
            acc = 0
            cnt = 0
    if cnt:
        out.append(acc)
    return bytes(out)


class _Writer:
    def __init__(self):
        self.out = bytearray()
        self.acc = 0
        self.cnt = 0

    def put(self, value, n):
        self.acc |= value << self.cnt
        self.cnt += n
        while self.cnt >= 8:
            self.out.append(self.acc & 0xFF)
            self.acc >>= 8
            self.cnt -= 8

    def put_bits(self, bits):
        for bit in bits:
            self.put(bit, 1)

    def flush(self):
        if self.cnt:
            self.out.append(self.acc & 0xFF)
            self.acc = 0
            self.cnt = 0
        return bytes(self.out)


_LEN_ENC = None
_DIST_ENC = None


def _encoders():
    global _LEN_ENC, _DIST_ENC
    if _LEN_ENC is None:
        _LEN_ENC = _build_encoder(_build_table(_LENLEN))
        _DIST_ENC = _build_encoder(_build_table(_DISTLEN))
    return _LEN_ENC, _DIST_ENC


def _emit_len(w, enc, sym, extra_val, extra_bits):
    w.put_bits(enc[sym])
    if extra_bits:
        w.put(extra_val, extra_bits)


def implode(data, dict_=6, min_match=3, chain=64, matches=True):
    """压缩为 DCL 流。lit=0（字面量不编码），最稳。"""
    n = len(data)
    w = _Writer()
    w.put(0, 8)          # lit = 0
    w.put(dict_, 8)      # dict
    dict_size = 1 << (dict_ + 6)

    len_enc, dist_enc = _encoders()

    head = {}
    prev = [-1] * n

    def insert(pos):
        if pos + 2 < n:
            key = data[pos:pos + 3]
            prev[pos] = head.get(key, -1)
            head[key] = pos

    pos = 0
    while pos < n:
        best_len = 0
        best_dist = 0
        if matches and pos + 2 < n:
            key = data[pos:pos + 3]
            cand = head.get(key, -1)
            limit = min(n - pos, 518)
            tries = 0
            while cand >= 0 and tries < chain:
                if pos - cand > dict_size:
                    break
                ln = 0
                while ln < limit and data[cand + ln] == data[pos + ln]:
                    ln += 1
                if ln > best_len:
                    best_len = ln
                    best_dist = pos - cand
                    if ln >= limit:
                        break
                cand = prev[cand]
                tries += 1

        if best_len >= min_match:
            w.put(1, 1)
            # 长度符号：按 [base, base+2^extra-1] 精确匹配
            # 注意 _LEN_BASE 不是单调的（[3,2,4,5,...]：长度 3 在符号 0，长度 2 在符号 1）
            sym = None
            for s in range(16):
                lo = _LEN_BASE[s]
                hi = lo + (1 << _LEN_EXTRA[s]) - 1
                if lo <= best_len <= hi:
                    sym = s
                    break
            if sym is None:
                sym = 15
            if sym == 15 and best_len >= 519:
                sym = 14
            extra_bits = _LEN_EXTRA[sym]
            extra_val = best_len - _LEN_BASE[sym]
            _emit_len(w, len_enc, sym, extra_val, extra_bits)

            # 距离
            shift = 2 if best_len == 2 else dict_
            d = best_dist - 1
            dsym = d >> shift
            extra = d & ((1 << shift) - 1)
            w.put_bits(dist_enc[dsym])
            if shift:
                w.put(extra, shift)

            for k in range(best_len):
                insert(pos + k)
            pos += best_len
        else:
            w.put(0, 1)
            w.put(data[pos], 8)
            insert(pos)
            pos += 1

    # 结束码
    w.put(1, 1)
    w.put_bits(len_enc[15])
    w.put(0xFF, 8)

    return w.flush()
