# -*- coding: utf-8 -*-
"""
Multisim MCP Server

设计：
    host 跑 64 位（能用 mcp 包），所有 COM 动作转发给 32 位 worker 子进程。
    电路"搭建"不经过 COM——直接生成 .ms14 文件（XML + DCL 压缩），
    因为 Multisim 的 COM 根本没有"放元件/连线"的方法。

两条腿：
    build_*  纯 Python 生成/修改电路文件，不需要 Multisim 在跑
    sim_*    走 COM（隐藏实例），负责打开、跑分析、读波形、导图

启动：
    python server.py                 # stdio，供 MCP 客户端连接
"""
import json
import os
import queue
import subprocess
import sys
import threading
import time

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)


def _env(name, default):
    return os.environ.get(name) or default


# 32 位 Python：Multisim 的自动化服务器只注册在 32 位注册表视图，
# 64 位解释器调 Connect() 会拿到 E_ACCESSDENIED
PY32 = _env("RUNTIME_PY32", os.path.join(ROOT, "runtime", "py32", "python.exe"))
WORKER = os.path.join(HERE, "com_worker.py")

# 电路构建依赖
VENDOR = os.path.join(ROOT, "vendor")
if VENDOR not in sys.path:
    sys.path.insert(0, VENDOR)
if HERE not in sys.path:
    sys.path.insert(0, HERE)

BLANK_MS14 = _env("MSIM_BLANK", os.path.join(ROOT, "data", "blank.ms14"))
DONOR_DIR = _env("MSIM_DONORS", os.path.join(ROOT, "data", "donors"))
_CACHE_DIR = _env("MSIM_CACHE", os.path.join(ROOT, ".cache"))

_last_reap = 0.0          # _reap_sim_servers 的 30 秒节流戳


def _win_path(p):
    """COM 的 OpenFile/ExportImage 只认标准 Windows 反斜杠绝对路径。

    传 'E:/a/b.ms14' 进去不会报错，而是让 Multisim 在读文件时崩掉，
    host 只看到 worker 退出。统一在这里收敛掉。
    """
    if not isinstance(p, str):
        return p
    return os.path.normpath(os.path.abspath(p))


def _reap_sim_servers(force=False):
    """回收 worker 被杀后遗留的 Multisim 隐藏仿真服务器。

    COM 的 /SimServer 实例不会随 worker 退出；堆到几个之后新的 Connect()
    就会失败，表现是「时好时坏、莫名报错」。只匹配带 /SimServer: 参数的
    进程，用户手动打开的 Multisim 界面不在这个范围里。
    设 MSIM_NO_REAP=1 可关闭（例如另有用 Multisim COM 的程序在跑）。

    一次 msim_open 里 host 与 worker 启动各会触发一次本函数，PowerShell
    冷启动 + WMI 查询不便宜，所以带 30 秒节流；需要立即清理时 kill worker
    的路径会传 force=True。
    """
    global _last_reap
    if os.environ.get("MSIM_NO_REAP", "").strip().lower() in ("1", "true", "yes", "on"):
        return
    if sys.platform != "win32":
        return
    if not force and (time.time() - _last_reap) < 30.0:
        return
    _last_reap = time.time()
    ps = (
        "Get-CimInstance Win32_Process -Filter \"Name='multisim.exe'\" | "
        "Where-Object { $_.CommandLine -like '*/SimServer:*' } | "
        "ForEach-Object { Stop-Process -Id $_.ProcessId -Force "
        "-ErrorAction SilentlyContinue }"
    )
    try:
        subprocess.run(["powershell", "-NoProfile", "-NonInteractive",
                        "-Command", ps], timeout=20,
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except Exception:
        pass


def _blank_xml_path():
    """把压缩的空白设计解成明文 XML 供构建层使用（带缓存）。"""
    cache = os.path.join(_CACHE_DIR, "blank.xml")
    if os.path.exists(cache):
        return cache
    if not os.path.exists(BLANK_MS14):
        raise MsimError("NO_BLANK", "找不到空白设计基底：%s" % BLANK_MS14)
    from .ms14.container import read_ms14
    xml, _ = read_ms14(BLANK_MS14)
    os.makedirs(os.path.dirname(cache), exist_ok=True)
    with open(cache, "wb") as f:
        f.write(xml)
    return cache

try:
    from mcp.server.fastmcp import FastMCP as MCPServer  # mcp 1.x
except ImportError:  # mcp 2.x
    from mcp.server.mcpserver import MCPServer

mcp = MCPServer("multisim")


# ---------------------------------------------------------------- 错误类型

class MsimError(Exception):
    def __init__(self, code, message, detail=None):
        super().__init__(message)
        self.code = code
        self.message = message
        self.detail = detail

    def as_dict(self):
        d = {"ok": False, "error": {"code": self.code, "message": self.message}}
        if self.detail:
            d["error"]["detail"] = str(self.detail)[:600]
        return d


# ---------------------------------------------------------------- COM worker

class ComWorker:
    """32 位 COM 子进程的 JSON-RPC 客户端。

    三个关键设计（都是踩坑后改的）：
      1. 子进程强制 UTF-8：Windows 管道默认走 cp936，中文路径会在 worker
         侧解码失败直接把进程搞死，父端只看到 "worker 退出"。
      2. 读取用独立线程 + Queue 带超时：直接 readline() 一旦 COM 卡住
         就永久挂起且持锁，连 kill 都抢不到锁。
      3. 校验响应 id：管道一旦错位（半行/多行），读到的是上一条命令的
         结果，json 解析成功、ok=true，于是静默返回错误数据。
    """

    def __init__(self):
        self._proc = None
        self._id = 0
        self._call_lock = threading.Lock()
        self._kill_lock = threading.Lock()
        self._queue = None
        self._reader = None
        self._err_lines = None

    def _ensure(self):
        if self._proc is not None and self._proc.poll() is None:
            return
        if not os.path.exists(PY32):
            raise MsimError("NO_PY32", "找不到 32 位 Python：%s" % PY32)
        _reap_sim_servers()
        env = dict(os.environ)
        env["PYTHONUTF8"] = "1"
        env["PYTHONIOENCODING"] = "utf-8"
        self._proc = subprocess.Popen(
            [PY32, WORKER],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            cwd=HERE, text=True, encoding="utf-8", errors="replace",
            bufsize=1, env=env,
        )
        self._queue = queue.Queue()
        self._err_lines = queue.Queue(maxsize=500)
        self._reader = threading.Thread(
            target=self._read_loop, args=(self._proc, self._queue), daemon=True)
        self._reader.start()
        threading.Thread(
            target=self._err_loop, args=(self._proc, self._err_lines),
            daemon=True).start()

    @staticmethod
    def _read_loop(proc, q):
        try:
            for line in proc.stdout:
                q.put(line)
        except Exception:
            pass
        finally:
            q.put(None)          # EOF 哨兵

    @staticmethod
    def _err_loop(proc, q):
        """持续排空 stderr：不读会管道写满把 worker 卡死。"""
        try:
            for line in proc.stderr:
                if q.full():
                    try:
                        q.get_nowait()
                    except Exception:
                        pass
                q.put(line.rstrip())
        except Exception:
            pass

    @staticmethod
    def _err_tail(q):
        if q is None:
            return None
        lines = []
        while True:
            try:
                lines.append(q.get_nowait())
            except queue.Empty:
                break
        return "\n".join(lines[-12:]) or None

    def kill(self):
        """杀掉 worker（连带它持有的 Multisim 隐藏实例）。

        不调用 app.Disconnect()：实测反复拆 COM 连接会把 multisim.exe
        的堆搞坏（C++ Runtime Error），进程退出交给 OS 回收最安全。
        独立一把锁，避免 call() 卡住时 kill 抢不到锁。
        """
        with self._kill_lock:
            p = self._proc
            self._proc = None
            self._queue = None
            if p is not None:
                try:
                    p.kill()
                except Exception:
                    pass
                try:
                    p.wait(timeout=5)
                except Exception:
                    pass

    def call(self, cmd, timeout=180, **args):
        if isinstance(args.get("path"), str):
            args["path"] = _win_path(args["path"])
        with self._call_lock:
            self._ensure()
            self._id += 1
            rid = self._id
            req = json.dumps({"id": rid, "cmd": cmd, "args": args}, ensure_ascii=False)
            try:
                self._proc.stdin.write(req + "\n")
                self._proc.stdin.flush()
            except Exception as e:
                self.kill()
                raise MsimError("WORKER_IO", "写入 worker 失败：%s" % e)

            try:
                line = self._queue.get(timeout=timeout)
            except queue.Empty:
                detail = self._err_tail(self._err_lines)
                self.kill()
                raise MsimError("WORKER_TIMEOUT",
                                "worker 超过 %ss 无响应，已强制重启" % timeout,
                                detail)

            if line is None:
                detail = self._err_tail(self._err_lines)
                self.kill()
                raise MsimError("WORKER_DIED", "worker 已退出（无响应）",
                                detail or "Multisim 可能在读文件时异常退出；"
                                "检查 .ms14 结构或换用标准 Windows 路径")

            try:
                resp = json.loads(line)
            except Exception:
                self.kill()
                raise MsimError("WORKER_BAD_JSON", "worker 返回非 JSON", line[:300])

            if resp.get("id") != rid:
                self.kill()
                raise MsimError(
                    "WORKER_DESYNC",
                    "响应 id 不匹配（期望 %s 收到 %s），管道已错位，已重启 worker"
                    % (rid, resp.get("id")))

            if not resp.get("ok"):
                e = resp.get("error") or {}
                raise MsimError(e.get("code", "COM_ERROR"),
                                e.get("message", "未知错误"), e.get("detail"))
            return resp.get("data")


_worker = ComWorker()


# ---------------------------------------------------------------- 电路构建

_msbuild = None
_msmodel = None


def _libs():
    """惰性导入电路构建库（纯 Python，64 位可用）"""
    global _msbuild, _msmodel
    if _msbuild is None:
        import msbuild
        import msmodel
        msbuild.BLANK = _blank_xml_path()
        msbuild.DONOR_DIRS = [DONOR_DIR]
        _msbuild, _msmodel = msbuild, msmodel
    return _msbuild, _msmodel


# 允许的元件种类 → (family, part)
KIND_MAP = {
    "resistor": ("RESISTOR", "RESISTOR_VIRTUAL"),
    "resistor_rated": ("RATED_VIRTUAL", "RESISTOR_RATED"),
    "capacitor": ("CAPACITOR", "CAPACITOR_VIRTUAL"),
    "capacitor_rated": ("RATED_VIRTUAL", "CAPACITOR_RATED"),
    "inductor": ("INDUCTOR", "INDUCTOR_VIRTUAL"),
    "dc_source": ("POWER_SOURCES", "DC_POWER"),
    "ac_source": ("POWER_SOURCES", "AC_POWER"),
    "signal_ac": ("SIGNAL_VOLTAGE_SOURCES", "AC_VOLTAGE"),
    "ground": ("POWER_SOURCES", "GROUND"),
    "dgnd": ("POWER_SOURCES", "DGND"),
    "vcc": ("POWER_SOURCES", "VCC"),
    "diode": ("DIODE", "1N4007G"),
    "led": ("LED", "LED_red"),
    "npn": ("BJT_NPN", "2N2222A"),
    "npn_bc548": ("BJT_NPN", "BC548BP"),
    "nmos": ("MOS_ENH_N", "IRF840"),
    "opamp": ("OPAMP", "LM358AD"),
    "opamp_ideal": ("ANALOG_VIRTUAL", "OPAMP_3T_VIRTUAL"),
    "zener": ("ZENER", "1N4734A"),
    "timer555": ("RATED_VIRTUAL", "555_TIMER_RATED"),
    "switch_spdt": ("SWITCH", "SPDT"),
    "ina128": ("SPECIAL_FUNCTION", "INA128PA"),
}

# 会话内的构建状态
_session = {"asm": None, "circuit": None, "parts": {}, "nets": {}, "path": None}


def _reset_session():
    _session.update({"asm": None, "circuit": None, "parts": {},
                     "nets": {}, "path": None, "pin_owner": {}})


def _new_asm():
    _reset_session()          # 先清，建失败时旧电路不会残留
    msbuild, _ = _libs()
    blank = _blank_xml_path()
    asm = msbuild.Assembler(base_path=blank)
    _session["asm"] = asm
    _session["circuit"] = asm.c
    return asm


def _asm():
    if _session["asm"] is None:
        raise MsimError("NO_SESSION", "还没建电路，先调 msim_new_circuit 或 msim_build")
    return _session["asm"]


def _instrument_slot():
    """仪器默认摆到已有元件右侧一列，逐台往下排。

    早期用"元件总数"推算坐标，结果第 5 个对象会折回第一列，示波器面板
    正好压在电源上。仪器面板比普通元件大得多，必须单独留一列。
    y 不按台数数，而是看已有仪器已经摆到哪了——手工放过位置的也要避开。
    """
    parts = _session["parts"]
    xs = [float((v.get("at") or [0, 0])[0] or 0)
          for v in parts.values() if v.get("kind") != "instrument"]
    base_x = max(660.0, (max(xs) + 180.0) if xs else 0.0)
    ys = [float((v.get("at") or [0, 0])[1] or 0)
          for v in parts.values() if v.get("kind") == "instrument"]
    return base_x, (max(ys) + 240.0) if ys else 160.0


def _resolve_kind(kind):
    k = (kind or "").strip().lower()
    if k in KIND_MAP:
        return KIND_MAP[k]
    # 也允许直接写 "FAMILY/PART"
    if "/" in (kind or ""):
        f, p = kind.split("/", 1)
        return (f.strip(), p.strip())
    raise MsimError("BAD_KIND", "不认识的元件种类：%r" % kind,
                    "可用：%s" % ", ".join(sorted(KIND_MAP)))


def _insert_midpoint(asm, node_ciid):
    """在指定网络的**水平**导线段中间插入一个共线点，返回该点坐标。

    两条硬约束（都来自实测）：
      1. 探针落点必须是导线顶点，落在段中间不吸附；
      2. 必须是**水平**段，落在垂直段或 L 形拐点上 EnumOutputs 取不到。

    一个网络可能有**多条** CIITLinkComp（分段绘制），所以要扫全部，
    不能只看第一条。一条水平段都没有时明确报错，让调用方改布局，
    而不是退而求其次放到垂直段上假装成功。
    """
    import xml.etree.ElementTree as ET

    cands = []          # (段长, link, points, index)
    for el in asm.objs:
        inner = list(el)
        if not inner or inner[0].get("Class") != "CIITLinkComp":
            continue
        link = inner[0]
        if link.get("Node") != node_ciid:
            continue
        points = link.find("Points")
        if points is None:
            continue
        items = list(points)
        for i in range(len(items) - 1):
            x1 = float(items[i].get("X") or 0)
            y1 = float(items[i].get("Y") or 0)
            x2 = float(items[i + 1].get("X") or 0)
            y2 = float(items[i + 1].get("Y") or 0)
            if abs(y2 - y1) > 1e-6:
                continue                       # 垂直段，跳过
            d = abs(x2 - x1)
            if d <= 0:
                continue                       # 零长段
            cands.append((d, link, items, i))

    if not cands:
        raise MsimError(
            "PROBE_NO_HORIZONTAL",
            "网络 %s 没有水平导线段，探针无法放置（探针只能落在水平段上）。"
            "调整元件坐标让这段连线变成水平，或换一个网络放探针。" % node_ciid)

    cands.sort(key=lambda t: -t[0])
    _d, link, items, best_i = cands[0]
    points = link.find("Points")

    x1 = float(items[best_i].get("X") or 0)
    y1 = float(items[best_i].get("Y") or 0)
    x2 = float(items[best_i + 1].get("X") or 0)
    y2 = float(items[best_i + 1].get("Y") or 0)
    mx, my = (x1 + x2) / 2.0, (y1 + y2) / 2.0

    for it in items:                            # 已有点就不重复插
        if abs(float(it.get("X") or 0) - mx) < 1e-6 \
                and abs(float(it.get("Y") or 0) - my) < 1e-6:
            return mx, my

    new = ET.Element("Item")
    new.set("X", "%g" % mx)
    new.set("Y", "%g" % my)
    points.insert(best_i + 1, new)
    return mx, my


def _pin_of(ports, which):
    """ports = [(ciid, 引脚名), ...]

    先按引脚名精确匹配（"B" 就是名为 B 的引脚）。
    只有该元件**所有引脚名都是纯数字**（电阻/电容这类 1、2 脚）时，
    才允许按序号回退——否则像三极管 ['C','B','E'] 那样按位置取，
    会把基极和集电极接反，且不报错，非常危险。
    """
    names = [p[1] for p in ports]
    key = str(which)
    if key in names:
        return key
    if key.isdigit() and all(n.isdigit() for n in names):
        idx = int(key)
        if 1 <= idx <= len(ports):
            return ports[idx - 1][1]
    raise MsimError(
        "BAD_PIN",
        "引脚 %r 不存在。该元件的引脚名是 %s，请写成 '元件.引脚名'（如 Q1.B）"
        % (which, names))


# ---------------------------------------------------------------- 工具：会话

@mcp.tool()
def msim_status() -> dict:
    """查询当前环境：32 位 worker 是否可用、Multisim COM 是否连得上、当前电路状态。

    这是任何操作前应该先调的工具。不启动 Multisim 的可见窗口。"""
    try:
        info = _worker.call("ping")
        return {"ok": True, "com": info,
                "session": {
                    "has_circuit": _session["asm"] is not None,
                    "components": sorted(_session["parts"]),
                    "nets": sorted(_session["nets"]),
                    "path": _session["path"],
                }}
    except MsimError as e:
        return e.as_dict()


@mcp.tool()
def msim_new_circuit() -> dict:
    """新建一个空电路（只在内存里，还没落盘）。

    之后用 msim_add_part / msim_connect / msim_add_probe 搭电路，最后 msim_save。
    也可以直接用 msim_build 一步到位。"""
    try:
        _new_asm()
        return {"ok": True, "message": "空白电路已就绪"}
    except MsimError as e:
        return e.as_dict()
    except Exception as e:
        return {"ok": False, "error": {"code": "INTERNAL", "message": str(e)}}


@mcp.tool()
def msim_edit_open(path: str) -> dict:
    """把一个已有的 .ms14 装进编辑会话，之后所有编辑工具对它生效。

    典型用途：用户前台 Multisim 里 Ctrl+S 保存后，agent 用 msim_read_design
    看结构，再用本工具装进会话做修改（删元件、断线、改值、换模型……），
    最后 msim_save_as 存成新文件（原文件不会被覆盖，除非显式指到原路径）。

    会话里的元件/网络缓存会从文件重建；已有探针的位置关联比较复杂，
    不做重建——编辑会话里再放探针可能与既有探针重复，需要探针时优先
    用 msim_add_probe 按网络名补。"""
    try:
        src = _win_path(path)
        if not os.path.exists(src):
            raise MsimError("BAD_PATH", "文件不存在：%s" % src)
        if not src.lower().endswith(".ms14"):
            raise MsimError("BAD_PATH", "只支持 .ms14，收到 %r" % src)
        from .ms14.container import read_ms14
        xml, _info = read_ms14(src)
        tmp = src + ".session.xml"
        with open(tmp, "wb") as f:
            f.write(xml)
        msbuild, _ = _libs()
        _session["asm"] = msbuild.Assembler(base_path=tmp)
        c = _session["asm"].c
        comp_by = {x["ciid"]: x["refdes"] for x in c.components()}
        ports_by_comp = {}
        pin_owner = {}
        nets = {}
        for p in c.ports():
            ref = comp_by.get(p["component"], "?")
            ports_by_comp.setdefault(p["component"], []).append(p["name"])
        for n in c.nodes():
            pins = []
            for pid in n["ports"]:
                p = next((p for p in c.ports() if p["ciid"] == pid), None)
                if p is None:
                    continue
                ref = comp_by.get(p["component"], "?")
                pins.append("%s.%s" % (ref, p["name"]))
                pin_owner[(ref, p["name"])] = n["name"]
            nets[n["name"] or ("n_%s" % n["ciid"][-6:])] = {
                "pins": pins, "node_id": n["ciid"], "probe": False}
        parts = {}
        for x in c.components():
            at = [0.0, 0.0]
            sym = c.symbol_of(x["ciid"])
            if sym is not None:
                inner = list(sym)
                if inner:
                    at = [float(inner[0].get("Transformer-M20") or 0),
                          float(inner[0].get("Transformer-M21") or 0)]
            parts[x["refdes"]] = {"ciid": x["ciid"],
                                  "pins": ports_by_comp.get(x["ciid"], []),
                                  "kind": x.get("family") or "",
                                  "at": at}
        _session["circuit"] = c
        _session["parts"] = parts
        _session["nets"] = nets
        _session["pin_owner"] = pin_owner
        _session["path"] = src
        try:
            os.remove(tmp)
        except OSError:
            pass
        return {"ok": True, "path": src,
                "components": [x["refdes"] for x in c.components()],
                "nets": {k: v["pins"] for k, v in nets.items()}}
    except MsimError as e:
        return e.as_dict()
    except Exception as e:
        return {"ok": False, "error": {"code": "BUILD_ERROR", "message": str(e)}}


@mcp.tool()
def msim_rotate_part(refdes: str, turns: int = 1) -> dict:
    """把元件符号旋转 180°（turns 次），电气连接点跟着转。

    用于二极管/稳压管/LED 这类有极性的元件摆向不对的场合。
    ⚠ 只对**尚未连线**或接受重连的元件使用：已画好的导线不会跟随
    旋转，端点会错开，旋转后需要断开重连。"""
    try:
        asm = _asm()
        c = asm.c
        ref = str(refdes).upper()
        comp = next((x for x in c.components() if x["refdes"] == ref), None)
        if comp is None:
            raise MsimError("NO_PART", "电路里没有元件 %s" % refdes)
        for _ in range(max(1, int(turns))):
            c.rotate180(comp["ciid"])
        return {"ok": True, "refdes": ref,
                "note": "已旋转 180°×%d；已接线的导线不跟随，必要时重连"
                        % max(1, int(turns))}
    except MsimError as e:
        return e.as_dict()
    except Exception as e:
        return {"ok": False, "error": {"code": "BUILD_ERROR", "message": str(e)}}


@mcp.tool()
def msim_add_probe(net: str) -> dict:
    """给一个已经存在的网络补电压探针（之后 run_dc/run_tran 可读它）。

    适合两种场合：msim_disconnect_pin 摘掉引脚后探针变孤儿的网；
    或者编辑既有设计（msim_edit_open）后想读某个节点的值。"""
    try:
        asm = _asm()
        name = str(net)
        rec = _session["nets"].get(name)
        if rec is None or not rec.get("node_id"):
            raise MsimError("NO_NODE", "网络 %r 不存在或没有节点" % name)
        nid = rec["node_id"]
        px, py = _insert_midpoint(asm, nid)
        asm.add_probe_ext(px, py,
                          donor_path=os.path.join(DONOR_DIR,
                                                  "probe_donor.ms14"))
        rec["probe"] = True
        return {"ok": True, "net": name, "probe_at": [px, py],
                "note": "COM 通道里读数名是 V(PRn)（编号由 Multisim 分配，"
                        "EnumOutputs 顺序返回）；精确数值建议走 msim_spice_run"}
    except MsimError as e:
        return e.as_dict()
    except Exception as e:
        return {"ok": False, "error": {"code": "BUILD_ERROR", "message": str(e)}}


@mcp.tool()
def msim_report_bom(path: str = None) -> dict:
    """元件清单（BOM）：位号、型号、图面标注值。

    path 省略时读当前编辑会话；给了路径则直接读那个 .ms14（原件不动）。"""
    try:
        import msmodel  # noqa: F401
        if path:
            c = msmodel.Circuit.load(_win_path(path))
        else:
            c = _asm().c
        items = []
        for x in c.components():
            value = None
            sym = c.symbol_of(x["ciid"])
            if sym is not None:
                for t in sym.iter("CIITSymTextCompValue"):
                    v = (t.get("Output") or "").replace("&ASC", "").strip()
                    if v:
                        value = v
                        break
            items.append({"refdes": x["refdes"],
                          "family": x.get("family") or "",
                          "part": x.get("part") or "",
                          "value": value})
        lines = ["%-8s %-24s %-22s %s" % ("RefDes", "Family", "Part", "Value")]
        for it in items:
            lines.append("%-8s %-24s %-22s %s" % (it["refdes"], it["family"],
                                                  it["part"], it["value"] or ""))
        return {"ok": True, "data": {"count": len(items), "items": items,
                                     "text": "\n".join(lines)}}
    except MsimError as e:
        return e.as_dict()
    except Exception as e:
        return {"ok": False, "error": {"code": "BUILD_ERROR", "message": str(e)}}


@mcp.tool()
def msim_list_parts() -> dict:
    """列出可用的元件种类（kind 字符串）以及样例库里实际存在的库元件。

    搭电路前先看这个，避免用了不存在的 kind。"""
    try:
        msbuild, _ = _libs()
        idx = msbuild.part_index()
        lib = ["%s/%s" % (f, p) for (f, p) in sorted(idx)]
        return {"ok": True, "kinds": dict(sorted(KIND_MAP.items())), "library": lib}
    except MsimError as e:
        return e.as_dict()
    except Exception as e:
        return {"ok": False, "error": {"code": "INTERNAL", "message": str(e)}}


@mcp.tool()
def msim_add_part(refdes: str, kind: str, value: float = None,
                  x: float = None, y: float = None) -> dict:
    """往当前电路里放一个元件。

    refdes  位号，如 "R1"、"V1"（同一电路内必须唯一）
    kind    元件种类，见 msim_list_parts，如 "resistor"、"dc_source"、"ground"
    value   主参数（电阻 Ω、电容 F、电感 H、电源 V）。ground 不需要
    x, y    放置坐标（Multisim 图纸单位，栅格 9）。省略则自动布局

    返回该元件的引脚列表，供 msim_connect 使用。"""
    try:
        asm = _asm()
        refdes = str(refdes).upper().strip()
        if not refdes:
            raise MsimError("BAD_ARGS", "位号不能为空")
        if refdes in _session["parts"]:
            raise MsimError("DUP_REFDES", "位号 %s 已存在" % refdes)
        family, part = _resolve_kind(kind)
        if x is None or y is None:
            # 只给了一半坐标时补另一半，别把给过的那个也覆盖掉
            n = len(_session["parts"])
            if x is None:
                x = 180 + (n % 6) * 180
            if y is None:
                y = 300 + (n // 6) * 180
        ciid, ports = asm.add_part(family, part, refdes,
                                   value if value is not None else None,
                                   x=float(x), y=float(y))
        pins = [p[1] for p in ports]
        _session["parts"][refdes] = {"ciid": ciid, "pins": pins,
                                     "kind": kind, "at": [x, y]}
        return {"ok": True, "refdes": refdes, "kind": kind,
                "pins": pins, "pin_count": len(pins),
                "at": [x, y], "family": family, "part": part}
    except MsimError as e:
        return e.as_dict()
    except Exception as e:
        return {"ok": False, "error": {"code": "BUILD_ERROR", "message": str(e)}}


@mcp.tool()
def msim_list_instruments(donor: str = None) -> dict:
    """列出可放进电路的虚拟仪器：示波器、万用表、函数发生器，以及引脚名。

    kind 直接传给 msim_add_instrument。传 donor（一个 .ms14 文件）可额外列出
    该设计里所有 X 开头的仪器，用来接波特图仪、逻辑分析仪等自定义仪器。"""
    try:
        from . import instrument as inst_mod
        found = inst_mod.list_instruments(DONOR_DIR)
        if donor:
            p = _win_path(donor)
            if not os.path.exists(p):
                raise MsimError("NO_DONOR", "donor 不存在：%s" % p)
            for it in inst_mod.scan_design(p):
                found.append({"kind": "custom", "refdes_prefix":
                              "".join(c for c in it["refdes"] if not c.isdigit()),
                              "donor": p, "donor_refdes": it["refdes"],
                              "pins": it["pins"], "pin_count": len(it["pins"]),
                              "note": "自定义仪器：把 donor + donor_refdes 传给 msim_add_instrument",
                              "available": True})
        return {"ok": True, "instruments": found,
                "note": "仪器是可连线的真实元件；要靠 MCP 读数值仍需 msim_connect(probe=True)"}
    except MsimError as e:
        return e.as_dict()
    except Exception as e:
        return {"ok": False, "error": {"code": "INTERNAL", "message": str(e)}}


@mcp.tool()
def msim_add_instrument(kind: str, refdes: str = None,
                        x: float = None, y: float = None,
                        donor: str = None, donor_refdes: str = None) -> dict:
    """在当前电路里放一台虚拟仪器。

    kind        "oscilloscope" / "multimeter" / "function_generator"，
                也接受 scope、meter、fgen、XSC、XMM、XFG 等别名
    refdes      位号，省略则自动取下一个空位（XSC1、XSC2…）
    x, y        面板在图纸上的位置
    donor, donor_refdes
                可选：从任意 .ms14 里复制任意仪器（配合 msim_list_instruments(donor=...)）

    仪器引脚像普通元件一样可以写进 msim_connect 的 pins 列表。
    COM 的 EnumOutputs 只认探针，所以仪器负责图和连线，读数仍用
    msim_connect(probe=True) 加的探针。"""
    try:
        asm = _asm()
        from . import instrument as inst_mod

        donor_override = False
        note = None
        try:
            k = inst_mod.resolve(kind)
            prefix = inst_mod.INSTRUMENTS[k]["prefix"]
            if donor:
                donor_override = True    # 明确给了 donor：以 donor 为准，如实回报
        except KeyError:
            if not (donor and donor_refdes):
                raise
            k = "custom"
            prefix = "".join(c for c in str(donor_refdes).upper()
                             if not c.isdigit()) or "X"
            note = ("kind %r 不是内置仪器，按自定义仪器处理（donor=%s, "
                    "donor_refdes=%s）" % (kind, donor, donor_refdes))

        if refdes:
            name = str(refdes).upper().strip()
        else:
            # custom 同样自增（XBP1、XBP2…），否则同一 donor 放第二台
            # 必撞 DUP_REFDES
            name = inst_mod.next_refdes(prefix, _session["parts"])
        if name in _session["parts"]:
            raise MsimError("DUP_REFDES", "位号 %s 已存在" % name)

        if x is None or y is None:
            sx, sy = _instrument_slot()
            x = sx if x is None else x
            y = sy if y is None else y

        dp = _win_path(donor) if donor else None
        ciid, pins, used_donor, used_ref = inst_mod.add(
            asm, k, name, x, y, DONOR_DIR, donor=dp, donor_refdes=donor_refdes)

        _session["parts"][name] = {
            "ciid": ciid, "pins": pins, "kind": "instrument", "at": [x, y]}
        result = {"ok": True, "refdes": name, "kind": k,
                  "donor_refdes": used_ref, "donor": used_donor,
                  "pins": pins, "pin_count": len(pins),
                  "at": [x, y],
                  "note": note or "引脚可连；要靠 MCP 读数值请另加探针"}
        if donor_override:
            result["donor_override"] = True
        return result
    except MsimError as e:
        return e.as_dict()
    except KeyError as e:
        return {"ok": False, "error": {"code": "NO_INSTRUMENT",
                                       "message": str(e)}}
    except FileNotFoundError as e:
        return {"ok": False, "error": {"code": "NO_DONOR", "message": str(e)}}
    except Exception as e:
        return {"ok": False, "error": {"code": "BUILD_ERROR", "message": str(e)}}


@mcp.tool()
def msim_delete_part(refdes: str) -> dict:
    """把一个元件从当前设计里整体移除（引脚、模型、符号、相关导线一起清）。

    文件层操作：改的是构建会话，之后 msim_save 保存、msim_open 才进入
    仿真通道。被删探针/仪器的输出会随之消失。"""
    try:
        asm = _asm()
        info = asm.remove_part(refdes)
        ref = str(refdes).upper()
        _session["parts"].pop(ref, None)
        for k in [k for k in list(_session["pin_owner"]) if k[0] == ref]:
            del _session["pin_owner"][k]
        for rec in _session["nets"].values():
            rec["pins"] = [p for p in rec["pins"]
                           if not str(p).upper().startswith(ref + ".")]
        # 引脚全空的网记录清掉：节点在文件里也已删空，残留记录会让
        # 之后的同名 connect 走 connect_more 分支撞已删除的 node_id
        for net in [n for n, rec in _session["nets"].items()
                    if not rec["pins"]]:
            del _session["nets"][net]
        return {"ok": True, "refdes": ref, **info}
    except KeyError as e:
        return {"ok": False, "error": {"code": "NO_PART", "message": str(e)}}
    except MsimError as e:
        return e.as_dict()
    except Exception as e:
        return {"ok": False, "error": {"code": "BUILD_ERROR", "message": str(e)}}


@mcp.tool()
def msim_disconnect_pin(refdes: str, pin: str) -> dict:
    """把一个引脚从它所在的网络断开（该引脚的导线一并删除）。

    例如断开负载电阻的一端来模拟 RL=∞。文件层操作，保存后生效。"""
    try:
        asm = _asm()
        info = asm.disconnect_pin(refdes, pin)
        ref = str(refdes).upper()
        _session["pin_owner"].pop((ref, str(pin)), None)
        for rec in _session["nets"].values():
            rec["pins"] = [p for p in rec["pins"]
                           if str(p).upper() != "%s.%s" % (ref, str(pin).upper())]
        return {"ok": True, "refdes": ref, "pin": str(pin), **info}
    except KeyError as e:
        return {"ok": False, "error": {"code": "NO_PART", "message": str(e)}}
    except MsimError as e:
        return e.as_dict()
    except Exception as e:
        return {"ok": False, "error": {"code": "BUILD_ERROR", "message": str(e)}}


@mcp.tool()
def msim_read_design(path: str) -> dict:
    """读取一个 .ms14 的完整结构：元件、引脚、网络、探针数（纯文件层）。

    典型用途：你在前台 Multisim 里 Ctrl+S 保存后，agent 用它读取你的
    最新电路。Multisim 的 COM 接口无法连接到第三方启动的 GUI 实例
    （实测无 ROT 注册），所以「前台协作」的唯一可靠通道就是文件：
    你保存 → agent 读这个文件 → agent 产出新文件供你打开。
    原件不会被修改。"""
    try:
        import msmodel  # noqa: F401  (vendor 路径已在 sys.path)
        c = msmodel.Circuit.load(_win_path(path))
        comp_by = {x["ciid"]: x["refdes"] for x in c.components()}
        comps = [{"refdes": x["refdes"],
                  "family": x.get("family") or "",
                  "part": x.get("part") or ""}
                 for x in c.components()]
        port_by = {p["ciid"]: p for p in c.ports()}
        nets = []
        for n in c.nodes():
            pins = []
            for pid in n["ports"]:
                p = port_by.get(pid)
                if p:
                    pins.append("%s.%s" % (comp_by.get(p["component"], "?"),
                                           p["name"]))
            nets.append({"net": n["name"] or "(unnamed)", "pins": pins})
        probe_count = sum(1 for el in c.objects
                          if list(el) and
                          list(el)[0].get("Class") == "CIITProbeExtComponent")
        return {"ok": True, "data": {"components": comps, "nets": nets,
                                     "probe_count": probe_count}}
    except MsimError as e:
        return e.as_dict()
    except Exception as e:
        return {"ok": False, "error": {"code": "BUILD_ERROR", "message": str(e)}}


@mcp.tool()
def msim_set_model(refdes: str, model_name: str, model_text: str = None) -> dict:
    """换元件的显示型号与模型参数（BJT/二极管换型号用）。

    model_name  新显示型号，如 "2N3904"（图上标注和属性同步替换）
    model_text  新模型参数体，如 "NPN IS=6.734E-15 BF=416.4 ..."；
                省略则只换显示名

    注意：内部 .MODEL 键保留原名不改（改了会与 Multisim 元件数据库
    失绑，文件打开即崩——实测），所以仿真参数换、网表 .MODEL 名不变，
    对仿真结果无影响。同步 SPICE 参数体、属性槽、符号显示文字，
    返回替换后的模型文本。"""
    try:
        asm = _asm()
        s = asm.set_model(refdes, model_name, model_text)
        return {"ok": True, "refdes": str(refdes).upper(),
                "model": s[:500]}
    except MsimError as e:
        return e.as_dict()
    except (StopIteration, ValueError) as e:
        return {"ok": False, "error": {"code": "NO_PART", "message": str(e)}}
    except Exception as e:
        return {"ok": False, "error": {"code": "BUILD_ERROR", "message": str(e)}}


@mcp.tool()
def msim_configure_instrument(refdes: str, freq_hz: float = None,
                              amplitude: float = None, offset: float = None,
                              waveform: str = None) -> dict:
    """调函数发生器面板参数：频率、幅值、偏置、波形。

    amplitude 是峰值电压（与 Multisim 面板一致，不是有效值）。
    waveform 目前只支持 "sine"，其余波形会明确报错而不是静默忽略。
    参数直接写进仪器的 SPICE 模型子电路（面板显示与仿真行为都跟着变），
    返回修改前后的模型文本供核对。"""
    try:
        asm = _asm()
        from . import instrument as inst_mod
        old, new = inst_mod.configure(
            asm, refdes, freq_hz=freq_hz, amplitude=amplitude,
            offset=offset, waveform=waveform)
        return {"ok": True, "refdes": refdes.upper(),
                "model_before": old[:400], "model_after": new[:400]}
    except MsimError as e:
        return e.as_dict()
    except (ValueError, KeyError) as e:
        return {"ok": False, "error": {"code": "BAD_ARGS", "message": str(e)}}
    except Exception as e:
        return {"ok": False, "error": {"code": "BUILD_ERROR", "message": str(e)}}


@mcp.tool()
def msim_connect(net: str, pins: list, probe: bool = False,
                 probe_at: list = None) -> dict:
    """把若干引脚连成一条网络（自动画导线）。

    net    网络名，如 "out"。地网统一写 "0"（"gnd"/"ground" 会自动归一化）
    pins   引脚引用列表，格式 "元件.引脚名"，如 "R1.1"、"Q1.B"、"Q1.C"、"Q1.E"。
           **注意三极管引脚名是字母 C/B/E，不是数字**，写错会直接报错
    probe  是否在这条网络上放一个电压探针（有探针才能跑分析读波形）
    probe_at  [x, y] 探针落点；**必须落在该网络的水平导线段上**，
           省略则在最长水平段中点自动插点。给了非法坐标会报错而不是静默失效

    一个引脚只能属于一条网络，重复连接会报 PIN_CONFLICT。
    地网需要多段时，重复调用本函数、net 都写 "0" 即可。"""
    try:
        asm = _asm()
        if not pins:
            raise MsimError("BAD_ARGS", "pins 不能为空")

        # 地网名归一化，避免 "0"/"GND"/"ground" 被当成不同网络而把地拆开
        raw_net = net
        net = "0" if str(net).lower() in ("0", "gnd", "ground", "agnd", "dgnd") else str(net)
        if str(raw_net).lower() in ("vcc", "vdd"):
            net = str(raw_net).upper()

        # 1) 先把所有引脚解析完（这一步不碰 _session，失败不留脏数据）
        resolved = []
        keys = []
        used_refs = []
        skipped = []
        for ref in pins:
            if "." not in ref:
                raise MsimError("BAD_PIN_REF",
                                "引脚写法应为 '元件.引脚名'，收到 %r（例：R1.1 / Q1.B）" % ref)
            refdes, which = ref.rsplit(".", 1)
            key = refdes.upper()
            if key not in _session["parts"]:
                raise MsimError("NO_PART", "电路里没有元件 %s" % refdes)
            ciid = _session["parts"][key]["ciid"]
            ports = [(ciid, p) for p in _session["parts"][key]["pins"]]
            pin = _pin_of(ports, which)
            # 2) 引脚冲突检查：同一引脚接进两条网络会静默短路
            owner = _session["pin_owner"].get((key, pin))
            if owner is not None:
                if owner != net:
                    raise MsimError(
                        "PIN_CONFLICT",
                        "引脚 %s.%s 已经属于网络 %r，不能再接到 %r（会造成短路）"
                        % (refdes, pin, owner, net))
                skipped.append(ref)
                continue        # 已在这条网上：不重复挂节点、不重复画导线
            resolved.append((ciid, pin))
            keys.append((key, pin))
            used_refs.append(ref)

        is_gnd = (net == "0")
        rec = _session["nets"].get(net)
        node_id = (rec or {}).get("node_id")
        wires = None
        if node_id is not None:
            # 同名网络必须复用同一个 CiNode。以前每次都新建，结果
            # "把示波器接到 net 2" 只是造了个同名孤儿节点：图上没有
            # 导线，SPICE 里也和被测点断开，而这里照样返回 ok。
            wires = asm.connect_more(node_id, resolved) if resolved else 0
        elif resolved:
            node_id = asm.connect(resolved, name=net, tap=probe and not is_gnd)
        else:
            raise MsimError("NO_NODE", "网络 %r 还没有任何引脚" % net)

        # 3) 连线成功后才登记状态（只登记这次真正挂上的引脚，
        #    重复引用不进 pins，免得状态和文件对不上）
        for k in keys:
            _session["pin_owner"][k] = net
        if rec is None:
            rec = _session["nets"][net] = {
                "pins": [], "probe": False, "segments": 0, "node_id": node_id}
        rec["node_id"] = node_id
        rec["pins"].extend(used_refs)
        rec["segments"] += 1

        result = {"ok": True, "net": net, "pins": used_refs}
        if skipped:
            result["already_on_net"] = skipped
        if wires is not None:
            result["wires_added"] = wires

        if probe:
            if is_gnd:
                raise MsimError('BAD_ARGS', '地网不能放电压探针（"0" 恒为参考点）')
            # 直接用缓存的节点 ID。按名字反查会撞上基底自带的同名节点，
            # 而且同名网络现在只会有一个 CiNode。
            nid = str(node_id)
            if probe_at and len(probe_at) >= 2:
                px, py = float(probe_at[0]), float(probe_at[1])
                _assert_on_horizontal_segment(asm, nid, px, py)
            else:
                px, py = _insert_midpoint(asm, nid)
            asm.add_probe_ext(px, py, donor_path=os.path.join(DONOR_DIR, "probe_donor.ms14"))
            _session["nets"][net]["probe"] = True
            result["probe_at"] = [px, py]
        return result
    except MsimError as e:
        return e.as_dict()
    except Exception as e:
        return {"ok": False, "error": {"code": "BUILD_ERROR", "message": str(e)}}


def _assert_on_horizontal_segment(asm, node_ciid, px, py):
    """校验 (px, py) 真的落在该网络的某条水平导线段的共线位置上。

    探针落点不在水平段上是"看起来放上了但 EnumOutputs 永远取不到"的
    典型静默失败，所以必须前置拦截。
    """
    import xml.etree.ElementTree as ET

    link = None
    for el in asm.objs:
        inner = list(el)
        if inner and inner[0].get("Class") == "CIITLinkComp" \
                and inner[0].get("Node") == node_ciid:
            link = inner[0]
            break
    if link is None:
        raise MsimError("NO_WIRE", "网络 %s 没有导线对象" % node_ciid)

    points = link.find("Points")
    items = list(points) if points is not None else []
    for i in range(len(items) - 1):
        x1 = float(items[i].get("X") or 0)
        y1 = float(items[i].get("Y") or 0)
        x2 = float(items[i + 1].get("X") or 0)
        y2 = float(items[i + 1].get("Y") or 0)
        if abs(y1 - y2) > 1e-6:
            continue                      # 不是水平段
        if abs(py - y1) > 1e-6:
            continue
        if min(x1, x2) - 1e-6 <= px <= max(x1, x2) + 1e-6:
            return
    raise MsimError(
        "PROBE_OFF_WIRE",
        "探针坐标 (%.3f, %.3f) 不在网络 %s 的水平导线段上；"
        "省略 probe_at 可自动选点" % (px, py, node_ciid))


@mcp.tool()
def msim_save(path: str) -> dict:
    """把当前电路写成 .ms14 文件（必须 .ms14 后缀，否则 COM 打不开）。"""
    try:
        if not str(path).lower().endswith(".ms14"):
            raise MsimError("BAD_PATH", "文件名必须以 .ms14 结尾，收到 %r" % path)
        asm = _asm()
        d = os.path.dirname(os.path.abspath(path))
        if d and not os.path.isdir(d):
            os.makedirs(d, exist_ok=True)
        asm.save(path)
        _session["path"] = path
        return {"ok": True, "path": path,
                "size": os.path.getsize(path) if os.path.exists(path) else 0,
                "components": sorted(_session["parts"]),
                "nets": sorted(_session["nets"])}
    except MsimError as e:
        return e.as_dict()
    except Exception as e:
        return {"ok": False, "error": {"code": "SAVE_ERROR", "message": str(e)}}


@mcp.tool()
def msim_build(spec: dict) -> dict:
    """一步到位：按电路描述生成完整 .ms14（元件 + 连线 + 探针 + 落盘）。

    spec 结构：
      {
        "path": "E:\\\\work\\\\rc_lpf.ms14",
        "components": [
          {"ref": "V1", "kind": "signal_ac", "value": 1, "at": [180, 360]},
          {"ref": "R1", "kind": "resistor", "value": 1000, "at": [360, 360]},
          {"ref": "C1", "kind": "capacitor", "value": 1e-6, "at": [540, 360]},
          {"ref": "GND1", "kind": "ground", "at": [360, 540]}
        ],
        "nets": [
          {"name": "in",  "pins": ["V1.1", "R1.1"]},
          {"name": "out", "pins": ["R1.2", "C1.1"], "probe": true},
          {"name": "0",   "pins": ["C1.2", "V1.2", "GND1.1"]}
        ]
      }

    "at" 可省略（自动网格布局）。probe=true 的网络会自动挂电压探针。"""
    try:
        comps = spec.get("components") or []
        nets = spec.get("nets") or []
        path = spec.get("path")
        if not comps:
            raise MsimError("BAD_SPEC", "components 不能为空")
        if not path:
            raise MsimError("BAD_SPEC", "缺少 path")

        _new_asm()
        added = []
        for i, c in enumerate(comps):
            at = c.get("at") or [180 + (i % 6) * 180, 300 + (i // 6) * 180]
            r = msim_add_part(c["ref"], c["kind"], c.get("value"), at[0], at[1])
            if not r.get("ok"):
                raise MsimError("BUILD_ERROR", "加元件 %s 失败：%s"
                                % (c["ref"], r.get("error", {}).get("message")), r)
            added.append(r)

        for n in nets:
            r = msim_connect(n["name"], n["pins"],
                             probe=bool(n.get("probe")),
                             probe_at=n.get("probe_at"))
            if not r.get("ok"):
                raise MsimError("BUILD_ERROR", "连线 %s 失败：%s"
                                % (n["name"], r.get("error", {}).get("message")), r)

        r = msim_save(path)
        if not r.get("ok"):
            raise MsimError("SAVE_ERROR", "保存失败：%s" % r.get("error", {}).get("message"))

        return {"ok": True, "path": path, "size": r["size"],
                "components": [a["refdes"] for a in added],
                "nets": [n["name"] for n in nets],
                "probed_nets": [n["name"] for n in nets if n.get("probe")]}
    except MsimError as e:
        return e.as_dict()
    except Exception as e:
        return {"ok": False, "error": {"code": "INTERNAL", "message": str(e)}}


# ---------------------------------------------------------------- 工具：仿真

@mcp.tool()
def msim_open(path: str) -> dict:
    """用 Multisim（隐藏实例）打开一个 .ms14，返回元件和探针列表。

    每次打开都换全新 worker + 全新 Multisim 实例（复用会因多文档状态残留
    让 EnumOutputs 交替返回 None）。上一个实例偶发还没退干净就连上去，
    COM 会抛「拒绝访问」——这种瞬时错误自动换实例重试一次。
    """
    last = None
    for attempt in range(2):
        _worker.kill()
        _reap_sim_servers(force=True)
        if attempt:
            time.sleep(1.0)
        try:
            return {"ok": True, "data": _worker.call("open", path=path)}
        except MsimError as e:
            last = e
            msg = (e.message or "").lower()
            # 瞬时 COM 错误才值得换实例重试：worker 侧结构性失败
            # （WORKER_*）一律重试；COM_ERROR 只认「忙碌/拒绝」类；
            # 带十进制 HRESULT 的异常（-2147...）都是 OLE 层的
            # busy / call rejected / access denied 家族，同样重试。
            # 确定性错误（文件不存在、路径错）绝不重试，直接如实返回。
            transient = e.code in ("WORKER_DIED", "WORKER_TIMEOUT", "E_NO_COM") \
                or (e.code == "COM_ERROR" and any(
                    s in msg for s in ("拒绝访问", "access is denied",
                                       "call was rejected", "server busy",
                                       "拒绝接收呼叫", "rpc_", "-2147", "0x800"))) \
                or (e.code == "INTERNAL" and any(
                    s in msg for s in ("拒绝访问", "access is denied",
                                       "call was rejected", "server busy",
                                       "rpc_", "-2147", "0x800")))
            if not transient or attempt:
                return e.as_dict()
    return last.as_dict() if last else \
        {"ok": False, "error": {"code": "OPEN_FAILED", "message": "打开失败"}}


@mcp.tool()
def msim_inspect() -> dict:
    """查看当前已打开电路的元件、探针、变体、仿真状态。"""
    try:
        return {"ok": True, "data": _worker.call("info")}
    except MsimError as e:
        return e.as_dict()


@mcp.tool()
def msim_get_value(refdes: str) -> dict:
    """读某个 RLC 元件的当前值（Ω / F / H）。"""
    try:
        return {"ok": True, "data": _worker.call("get_value", refdes=refdes)}
    except MsimError as e:
        return e.as_dict()


@mcp.tool()
def msim_set_value(refdes: str, value: float) -> dict:
    """改某个 RLC 元件的值（Ω / F / H），改完可以用 msim_save_as 存盘。"""
    try:
        return {"ok": True, "data": _worker.call("set_value", refdes=refdes, value=float(value))}
    except MsimError as e:
        return e.as_dict()


@mcp.tool()
def msim_save_as(path: str) -> dict:
    """把当前打开的电路另存为新的 .ms14。"""
    try:
        return {"ok": True, "data": _worker.call("save_as", path=path)}
    except MsimError as e:
        return e.as_dict()


@mcp.tool()
def msim_run_dc(outputs: list = None) -> dict:
    """跑直流工作点分析。

    outputs 省略则跑电路里所有探针。前提：电路里必须有探针，否则没有输出。"""
    try:
        return {"ok": True, "data": _worker.call("run_dc", outputs=outputs)}
    except MsimError as e:
        return e.as_dict()


@mcp.tool()
def msim_run_ac(num_points: int = 100, start: float = 1.0, stop: float = 1e6,
                sweep_type: int = 0, outputs: list = None) -> dict:
    """跑交流扫频分析。

    sweep_type: 0=Decade 1=Octave 2=Linear
    start/stop: 起止频率 Hz
    同样要求电路里有探针。"""
    try:
        return {"ok": True, "data": _worker.call(
            "run_ac", num_points=int(num_points), start=float(start),
            stop=float(stop), sweep_type=int(sweep_type), outputs=outputs)}
    except MsimError as e:
        return e.as_dict()


@mcp.tool()
def msim_run_tran(duration: float = 0.003, sample_rate: float = 200000,
                  output: str = None) -> dict:
    """跑瞬态分析，用来拿波形（相当于示波器看到的曲线）。

    duration     仿真时长（秒），默认 3ms（3 个 1kHz 周期）
    sample_rate  采样率（Hz），默认 200kHz
    output       探针名，省略则用第一个探针

    跑完用 msim_get_output 读数据，points 里是 [时间, 电压] 的点列。"""
    try:
        return {"ok": True, "data": _worker.call(
            "run_tran", duration=float(duration),
            sample_rate=float(sample_rate), output=output)}
    except MsimError as e:
        return e.as_dict()


@mcp.tool()
def msim_spice_run(netlist: str, commands: list, outdir: str) -> dict:
    """走 Multisim 内核的命令通道跑一张**纯网表**，返回每个分析的结果数据。

    与原理图仿真完全独立：不依赖探针和仪器，只要先用 msim_open 打开任意
    .ms14（空白基底即可）建立 COM 上下文。流程：写 deck → 内核执行 →
    解析每个 write 出来的 ASCII raw → JSON。

    netlist    SPICE 网表文本（纯 ASCII，首行是标题注释，末行 .end 可省）
    commands   分析命令列表，如 ["op", "tran 0 3m", "ac dec 50 10 10meg"]；
               只接受分析/绘图命令，其它命令会被拒绝
    outdir     结果目录（**不能含空格**）；deck 和 plotN.raw 都写在这里

    返回每条命令对应的 plotname/列名/行数据（复数列自动拆成 dB+相位）。"""
    try:
        from . import spice
        outdir = _win_path(outdir)
        if " " in outdir:
            raise MsimError("BAD_ARGS", "outdir 不能包含空格：%s" % outdir)
        # 每次调用写进独立的子目录：deck 里的命令按 plotN 落盘，同目录
        # 连续调用会互相覆盖，前面的分析结果直接丢失。
        import time as _ts
        subdir = os.path.join(
            outdir, "%s_%06d" % (_ts.strftime("%Y%m%d_%H%M%S"),
                                 _ts.time_ns() % 1000000))
        cir, cmd_path, raws = spice.write_deck(subdir, netlist, commands)
        log_path = os.path.join(outdir, "run.log")

        plots = []
        attempt = 0
        for attempt in range(3):
            if attempt:
                # 同一实例连续第二次 DoCommandLine 偶发不执行（内核状态
                # 残留）。重试换全新 worker + 空白基底，恢复到确定状态。
                _worker.kill()
                _reap_sim_servers()
                _worker.call("open", path=BLANK_MS14)
            r = _worker.call("do_commandline",
                             command_file=_win_path(cmd_path),
                             log_file=_win_path(log_path))
            # DoCommandLine 是异步的：调用返回时内核可能还在跑。等全部
            # raw 就绪（每条命令一个），最长 60s。
            import time as _t
            deadline = _t.time() + 60.0
            while _t.time() < deadline:
                if all(os.path.exists(p) for p in raws):
                    _t.sleep(0.3)      # 最后一条也留出写盘余量
                    break
                _t.sleep(0.2)
            plots = []
            ok = True
            for raw in raws:
                if not os.path.exists(raw):
                    plots.append({"file": raw,
                                  "error": "raw 未生成（该命令可能失败）"})
                    ok = False
                    continue
                plotname, variables, rows = spice.parse_raw(raw)
                cols, out_rows = spice.complex_columns(variables, rows)
                # 内核偶发不执行分析（plotname 退化为 "Constant values"、
                # 0 行数据）—— 重跑同一 deck 再试
                if not out_rows or plotname == "Constant values":
                    ok = False
                plots.append({"file": os.path.basename(raw),
                              "plotname": plotname,
                              "columns": cols,
                              "n_points": len(out_rows),
                              "rows": out_rows[:2000]})
            if ok:
                break
            _t.sleep(1.0)
        return {"ok": True,
                "data": {"dir": subdir, "attempts": attempt + 1,
                         "all_plots_ok": ok,
                         "ret": r.get("ret"),
                         "log": (r.get("log") or "")[-4000:],
                         "plots": plots}}
    except MsimError as e:
        return e.as_dict()
    except ValueError as e:
        return {"ok": False, "error": {"code": "BAD_ARGS", "message": str(e)}}
    except Exception as e:
        return {"ok": False, "error": {"code": "BUILD_ERROR", "message": str(e)}}


@mcp.tool()
def msim_get_output(name: str, max_points: int = 2000) -> dict:
    """读取一个探针的波形数据（分析跑完之后调）。

    返回 points = [[x, y], ...]。DC 分析通常只有 1 个点。"""
    try:
        return {"ok": True, "data": _worker.call(
            "get_output", name=name, max_points=int(max_points))}
    except MsimError as e:
        return e.as_dict()


@mcp.tool()
def msim_export_image(path: str, fmt: int = 0) -> dict:
    """把当前电路原理图导出成图片。fmt: 0=png 1=jpg 2=bmp。"""
    try:
        return {"ok": True, "data": _worker.call(
            "export_image", path=path, format=int(fmt))}
    except MsimError as e:
        return e.as_dict()


@mcp.tool()
def msim_close() -> dict:
    """关闭隐藏的 Multisim 实例，释放资源（直接结束 worker 进程）。"""
    try:
        _worker.kill()
        _reap_sim_servers()
        return {"ok": True, "data": {"closed": True}}
    except Exception as e:
        return {"ok": False, "error": {"code": "CLOSE_ERROR", "message": str(e)}}


if __name__ == "__main__":
    mcp.run()
