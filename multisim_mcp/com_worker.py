# -*- coding: utf-8 -*-
"""
Multisim COM Worker（32 位进程）

为什么必须单独一个 32 位进程：
    Multisim 的 COM 服务器 MSInterface.dll 只注册在 WOW6432Node，
    64 位解释器调 Connect() 会拿到 E_ACCESSDENIED。MCP host 跑 64 位，
    所以所有 COM 动作都转发到这个 worker 执行。

协议：stdin/stdout 各一行一个 JSON
    请求  {"id": <int>, "cmd": "<name>", "args": {...}}
    响应  {"id": <int>, "ok": true,  "data": ...}
          {"id": <int>, "ok": false, "error": {"code": "...", "message": "..."}}

设计约束：
    - 只依赖 pywin32 + 标准库，不 import mcp
    - 任何异常都必须转成结构化错误，绝不让进程死掉
    - Multisim 实例是隐藏的，不弹窗口
"""
import json
import os
import sys
import time
import traceback

import pythoncom
import win32com.client
from win32com.client import VARIANT

PROG_ID = "MultisimInterface.MultisimApp"

# 业务错误码
E_BAD_ARGS = "BAD_ARGS"
E_NO_COM = "NO_COM"
E_NO_CIRCUIT = "NO_CIRCUIT"
E_COM = "COM_ERROR"
E_INTERNAL = "INTERNAL"

_app = None
_circuit = None
_connected = False


class WorkerError(Exception):
    def __init__(self, code, message, detail=None):
        super().__init__(message)
        self.code = code
        self.message = message
        self.detail = detail


def _to_jsonable(v, depth=0):
    """把 COM 返回的 VARIANT / 嵌套 tuple / 数组转成可 JSON 序列化的结构"""
    if depth > 6:
        return str(v)
    if v is None or isinstance(v, (bool, int, float, str)):
        return v
    if isinstance(v, dict):
        return {str(k): _to_jsonable(x, depth + 1) for k, x in v.items()}
    if isinstance(v, (list, tuple)):
        return [_to_jsonable(x, depth + 1) for x in v]
    try:
        n = len(v)
        return [_to_jsonable(v[i], depth + 1) for i in range(n)]
    except Exception:
        pass
    return str(v)


def _flatten_pairs(data):
    """GetOutputData 返回 ((x, y), (x, y), ...) 或 ((x,), (y,)) 形式，统一成点列表"""
    out = []
    if not data:
        return out
    try:
        for row in data:
            if isinstance(row, (list, tuple)):
                if len(row) >= 2:
                    out.append([_num(row[0]), _num(row[1])])
                elif len(row) == 1:
                    out.append([_num(row[0])])
            else:
                out.append([_num(row)])
    except Exception:
        pass
    return out


def _num(x):
    try:
        if isinstance(x, (list, tuple)):
            return _num(x[0])
        return float(x)
    except Exception:
        return None


# ---------------------------------------------------------------- 连接管理

def ensure_app():
    global _app, _connected
    if _app is not None and _connected:
        return _app
    try:
        _app = win32com.client.Dispatch(PROG_ID)
    except Exception as e:
        raise WorkerError(E_NO_COM, "无法创建 Multisim COM 对象：%s" % e)
    last = None
    for i in range(6):
        try:
            _app.Connect()
            _connected = True
            return _app
        except Exception as e:
            last = e
            time.sleep(1)
    raise WorkerError(E_NO_COM, "Connect() 连续 6 次失败：%s" % last)


def ensure_circuit():
    if _circuit is None:
        raise WorkerError(E_NO_CIRCUIT, "当前没有打开的电路，先调 open 或 new")
    return _circuit


# ---------------------------------------------------------------- 命令实现

def cmd_ping(a):
    app = ensure_app()
    return {"connected": True, "version": app.VersionInfo, "path": app.Path}


def cmd_new(a):
    global _circuit
    app = ensure_app()
    _circuit = app.NewFile()
    return {"name": _circuit.CircuitName}


def _reset_instance():
    """已废弃：反复 Disconnect + 重新 Dispatch 会让 multisim.exe 堆损坏
    （实测弹 "Microsoft Visual C++ Runtime Library / Runtime Error!"）。

    现在改为进程级隔离：需要换实例时由 host 直接杀掉 worker 进程，
    让操作系统回收 COM 引用，Multisim 隐藏实例随之消失。
    """
    return


def cmd_open(a):
    global _circuit
    path = a.get("path")
    if not path or not os.path.exists(path):
        raise WorkerError(E_BAD_ARGS, "文件不存在：%r" % path)
    if not path.lower().endswith(".ms14"):
        raise WorkerError(E_BAD_ARGS, "COM 只支持 .ms14，收到 %r" % path)

    app = ensure_app()
    _circuit = app.OpenFile(path)

    outputs = None
    for _ in range(3):
        try:
            outputs = _to_jsonable(_circuit.EnumOutputs(0))
        except Exception:
            outputs = None
        if outputs:
            break
        time.sleep(0.4)

    return {
        "name": _circuit.CircuitName,
        "components": _to_jsonable(_circuit.EnumComponents(0)),
        "outputs": outputs,
        "state": _circuit.SimulationState,
    }


def cmd_save_as(a):
    global _circuit
    path = a.get("path")
    if not path:
        raise WorkerError(E_BAD_ARGS, "缺少 path")
    d = os.path.dirname(os.path.abspath(path))
    if d and not os.path.isdir(d):
        os.makedirs(d, exist_ok=True)
    c = ensure_circuit()
    c.SaveAs(path)
    return {"path": path, "exists": os.path.exists(path),
            "size": os.path.getsize(path) if os.path.exists(path) else 0}


def cmd_info(a):
    c = ensure_circuit()
    return {
        "name": c.CircuitName,
        "components": _to_jsonable(c.EnumComponents(0)),
        "outputs": _to_jsonable(c.EnumOutputs(0)),
        "variants": _to_jsonable(c.EnumVariants()),
        "active_variant": c.ActiveVariant,
        "state": c.SimulationState,
    }


def cmd_get_value(a):
    comp = a.get("refdes")
    if not comp:
        raise WorkerError(E_BAD_ARGS, "缺少 refdes")
    c = ensure_circuit()
    try:
        return {"refdes": comp, "value": float(c.RLCValue(comp))}
    except Exception as e:
        raise WorkerError(E_COM, "读 %s 的值失败（非 RLC 元件？）：%s" % (comp, e))


def cmd_set_value(a):
    comp = a.get("refdes")
    val = a.get("value")
    if comp is None or val is None:
        raise WorkerError(E_BAD_ARGS, "缺少 refdes 或 value")
    c = ensure_circuit()
    try:
        c.RLCValue = (comp, float(val))
    except Exception as e:
        raise WorkerError(E_COM, "写 %s 失败：%s" % (comp, e))
    try:
        back = float(c.RLCValue(comp))
    except Exception:
        back = None
    return {"refdes": comp, "value": back}


def _run_analysis(outputs, runner):
    c = ensure_circuit()
    names = [str(x) for x in (outputs or [])]
    if not names:
        names = [str(x) for x in (c.EnumOutputs(0) or [])]
    if not names:
        raise WorkerError(E_BAD_ARGS,
                          "电路里没有探针，任何分析都不会有输出。先加探针。")
    arr = VARIANT(pythoncom.VT_ARRAY | pythoncom.VT_BSTR, names)
    t0 = time.time()
    runner(c, arr)
    return names, time.time() - t0


def cmd_run_dc(a):
    names, dt = _run_analysis(a.get("outputs"), lambda c, arr: c.DoDCOperatingPoint(arr))
    return {"analysis": "dc", "outputs": names, "elapsed": round(dt, 3)}


def cmd_run_ac(a):
    sweep = int(a.get("sweep_type", 0))       # 0=Decade 1=Octave 2=Linear
    pts = int(a.get("num_points", 100))
    f0 = float(a.get("start", 1.0))
    f1 = float(a.get("stop", 1e6))
    if pts < 2 or f0 <= 0 or f1 <= f0:
        raise WorkerError(E_BAD_ARGS, "AC 参数非法：num_points>=2, 0<start<stop")
    names, dt = _run_analysis(
        a.get("outputs"),
        lambda c, arr: c.DoACSweep(sweep, pts, f0, f1, arr))
    return {"analysis": "ac", "outputs": names, "num_points": pts,
            "start": f0, "stop": f1, "elapsed": round(dt, 3)}


def cmd_get_output(a):
    name = a.get("name")
    if not name:
        raise WorkerError(E_BAD_ARGS, "缺少 name")
    c = ensure_circuit()
    # 分析刚跑完时探针数据可能还没就绪，轮询等待
    ready = False
    last_err = None
    for _ in range(25):
        try:
            ready = bool(c.OutputReady(name))
        except Exception as e:
            last_err = e
            ready = False
        if ready:
            break
        time.sleep(0.2)
    if not ready:
        return {"name": name, "ready": False, "points": [],
                "hint": "探针无数据（分析没跑，或该探针不在分析输出里）",
                "last_error": str(last_err) if last_err else None}

    raw = c.GetOutputData(name, None, None)
    # raw 可能是：
    #   (data, method)              第二项是标量枚举 —— 取 raw[0]
    #   (x[], y[])                  DC / 瞬态 —— 全部是序列，整体就是数据
    #   (freq[], re[], im[])        AC —— 三个都是序列，整体就是数据
    #   ((x1,y1),(x2,y2),...)       逐点
    #
    # 判据必须是"全部都是序列"，不能用"任一元素是序列"：后者会把
    # (data, method) 包装也当成完整数据，_shape_points 落到 point_rows
    # 兜底，于是把 method 和 x 数组的首元素当成读数返回（典型症状：
    # DC 工作点永远读到 0.0，shape="point_rows"）。
    if isinstance(raw, tuple) and len(raw) >= 2:
        if all(_is_seq(x) for x in raw):
            data = raw
        else:
            data = raw[0]
    elif isinstance(raw, tuple) and raw:
        data = raw[0]
    else:
        data = raw

    if not raw:
        return {"name": name, "ready": False, "points": [],
                "hint": "GetOutputData 返回空"}

    pts, shape = _shape_points(data)
    limit = int(a.get("max_points") or 4000)
    if limit <= 0:
        limit = 4000                  # 0/负数按默认抽稀，别除零
    if len(pts) > limit:
        import math as _m
        step = _m.ceil(len(pts) / float(limit))
        kept = pts[::step]
        if kept and kept[-1] is not pts[-1]:
            kept.append(pts[-1])          # 保留末点，避免抽稀丢尾部
        pts = kept
    return {"name": name, "ready": True, "n_points": len(pts),
            "shape": shape, "points": pts}


def _is_seq(v):
    """pywin32 的 SAFEARRAY 走序列协议（__getitem__），不一定有 __len__。
    用 len() 试探比 hasattr 可靠。"""
    if isinstance(v, (str, bytes)):
        return False
    try:
        len(v)
        return True
    except Exception:
        return False


def _shape_points(data):
    """GetOutputData 返回三态，统一成 [[x, 值, 相位?], ...]：

        AC 分析 : (频率[..], 实部[..], 虚部[..])    → [f, |V|, φ°]
        DC/瞬态 : (x[..], y[..])                    → [x, y]
        单点    : ((x,), (y,)) / 嵌套元组            → [x, y]
    """
    import math

    if not data:
        return [], "empty"

    def as_list(v):
        if isinstance(v, (list, tuple)):
            return list(v)
        if _is_seq(v):
            try:
                return [v[i] for i in range(len(v))]
            except Exception:
                pass
        return [v]

    top = as_list(data)

    # 逐点形状优先判定：((x1,y1),(x2,y2),...)。
    # 结构上它和 (x[], y[]) 同构——都是「序列的序列、内层全标量」，区别只在
    # 语义：列语义最多 3 列（DC/瞬态 2 列、AC 3 列）。所以 len(top) > 3 或
    # len(top) == 1 时明确按逐点读；2/3 列的歧义让位给实测确认过的列语义
    # 主路径（DC 2.5V / AC 频响都走那里，不能为理论形状牺牲实测行为）。
    # 以前没有这个分支：4+ 个点会被当成 point_rows，每行只取 row[0]，
    # 真正的读数 y 被整个丢掉。
    if len(top) > 3 or len(top) == 1:
        if all(_is_seq(t) and all(not _is_seq(e) for e in as_list(t))
               for t in top):
            return _flatten_pairs(top), "point_pairs"

    # AC：频率 + 实部 + 虚部
    if len(top) == 3 and all(_is_seq(x) for x in top):
        fs, res, ims = [as_list(x) for x in top]
        n = min(len(fs), len(res), len(ims))
        pts = []
        for i in range(n):
            f = _num(fs[i])
            re = _num(res[i])
            im = _num(ims[i])
            if f is None:
                continue
            if re is None or im is None:
                pts.append([f, None, None])
                continue
            pts.append([f, math.hypot(re, im), math.degrees(math.atan2(im, re))])
        return pts, "freq_re_im"

    # DC / 瞬态：x 数组 + y 数组
    if len(top) == 2 and _is_seq(top[0]) and _is_seq(top[1]):
        xs, ys = as_list(top[0]), as_list(top[1])
        n = min(len(xs), len(ys))
        pts = []
        for i in range(n):
            z = _as_complex(ys[i])
            if z is not None and not isinstance(ys[i], (int, float)):
                pts.append([_num(xs[i]), abs(z), math.degrees(math.atan2(z.imag, z.real))])
            else:
                pts.append([_num(xs[i]), _num(ys[i])])
        return pts, "xy_arrays"

    # 兜底：每行一个点。用 _flatten_pairs 取 (x, y)；旧的写法丢掉每行的
    # 第二个元素，等于把读数扔了。
    return _flatten_pairs(top), "point_rows"


def _as_complex(v):
    """把 (re, im) 元组或 complex 统一成 complex"""
    try:
        if isinstance(v, complex):
            return v
        if isinstance(v, (list, tuple)) and len(v) == 2:
            return complex(float(v[0]), float(v[1]))
        return complex(float(v))
    except Exception:
        return None


def cmd_export_image(a):
    path = a.get("path")
    if not path:
        raise WorkerError(E_BAD_ARGS, "缺少 path")
    fmt = int(a.get("format", 0))     # 0=png 1=jpg 2=bmp（按 enum 顺序）
    c = ensure_circuit()
    d = os.path.dirname(os.path.abspath(path))
    if d and not os.path.isdir(d):
        os.makedirs(d, exist_ok=True)
    c.GetCircuitImage(fmt, path)
    return {"path": path, "exists": os.path.exists(path),
            "size": os.path.getsize(path) if os.path.exists(path) else 0}


def cmd_close(a):
    """直接结束进程，不调用 Disconnect()。

    实测 app.Disconnect() 会拆 COM 连接，多次之后 multisim.exe 弹
    C++ Runtime Error 崩溃。进程退出时操作系统会回收 COM 引用，
    Multisim 隐藏实例也随之退出，效果等价且不踩堆。
    """
    os._exit(0)


def cmd_run_tran(a):
    """瞬态分析：给指定探针配采集参数，跑一段时间，之后用 get_output 读波形。

    SetOutputRequest(outputName, method, sampleRate, numSamples, repeatFlag)
    method 是 SimulationIOType 枚举，实测传 0 可用；不同版本可能是 0/1/2，
    这里按顺序试，成功即停。
    """
    c = ensure_circuit()
    name = a.get("output")
    if not name:
        outs = [str(x) for x in (c.EnumOutputs(0) or [])]
        if not outs:
            raise WorkerError(E_BAD_ARGS, "电路里没有探针")
        name = outs[0]

    dur = float(a.get("duration", 3e-3))
    rate = float(a.get("sample_rate", 200000.0))
    if dur <= 0 or rate <= 0:
        raise WorkerError(E_BAD_ARGS, "duration 和 sample_rate 必须为正")
    n = int(dur * rate)
    if n < 10:
        raise WorkerError(E_BAD_ARGS, "采样点数太少（%d），调大 duration 或 sample_rate" % n)

    last = None
    ok_method = None
    for method in (0, 1, 2):
        try:
            c.SetOutputRequest(name, method, rate, n, False)
            ok_method = method
            break
        except Exception as e:
            last = e
    if ok_method is None:
        raise WorkerError(E_COM, "SetOutputRequest 三种 method 全部失败：%s" % last)

    t0 = time.time()
    c.RunSimulation(dur, False)
    state = c.SimulationState
    # 等仿真跑完/数据就绪
    for _ in range(60):
        try:
            if c.OutputReady(name):
                break
        except Exception:
            pass
        time.sleep(0.1)
    try:
        c.StopSimulation()
    except Exception:
        pass

    return {"output": name, "method": ok_method, "duration": dur,
            "sample_rate": rate, "num_samples": n,
            "state": state, "elapsed": round(time.time() - t0, 3)}


def cmd_do_commandline(a):
    """跑 XSPICE 命令文件（IMultisimCircuit.DoCommandLine）。

    a: command_file  run.cmd 绝对路径（ASCII，无空格）
       log_file      日志输出路径

    当前打开的文档是空白的就行：命令文件里 `source <netlist>` 会把
    deck 加载成当前电路，之后 op/dc/ac/tran 的结果用 write 写成
    ASCII SPICE3 raw。日志全文返回，方便 agent 自查语法错误。
    """
    cir = ensure_circuit()
    cmd_file = a.get("command_file")
    log_file = a.get("log_file")
    if not cmd_file or not log_file:
        raise WorkerError(E_BAD_ARGS, "需要 command_file 和 log_file")
    ret = cir.DoCommandLine(cmd_file, log_file)
    log = ""
    try:
        with open(log_file, "r", encoding="utf-8", errors="replace") as f:
            log = f.read()
    except Exception:
        pass
    return {"ret": str(ret), "log": log[-6000:]}


def cmd_report_netlist(a):
    """⚠ 已废弃：Multisim 14.0 的 ReportNetlist COM 实现自身崩溃
    （0xC0000005，任意参数组合、任何电路，含标准空白基底——实测）。
    疑似依赖报告窗口的 GUI 线程，隐藏实例下必死。
    网表核对请走 do_commandline（XSPICE 命令通道），全分析类型可用。"""
    raise WorkerError(E_BAD_ARGS,
                      "ReportNetlist 在 Multisim 14.0 COM 上必然崩溃（NI "
                      "接口缺陷，已实测多种参数组合与电路）。网表请用 "
                      "do_commandline 通道自建。")


COMMANDS = {
    "ping": cmd_ping,
    "new": cmd_new,
    "open": cmd_open,
    "save_as": cmd_save_as,
    "info": cmd_info,
    "get_value": cmd_get_value,
    "set_value": cmd_set_value,
    "run_dc": cmd_run_dc,
    "run_ac": cmd_run_ac,
    "run_tran": cmd_run_tran,
    "get_output": cmd_get_output,
    "export_image": cmd_export_image,
    "do_commandline": cmd_do_commandline,
    "close": cmd_close,
}


def main():
    # Windows 管道默认 cp936，中文路径会在解码时直接崩掉进程，必须强制 UTF-8
    try:
        sys.stdin.reconfigure(encoding="utf-8", errors="replace")
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
    pythoncom.CoInitialize()
    out = sys.stdout
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        rid = None
        try:
            req = json.loads(line)
            rid = req.get("id")
            cmd = req.get("cmd")
            args = req.get("args") or {}
            fn = COMMANDS.get(cmd)
            if fn is None:
                raise WorkerError(E_BAD_ARGS, "未知命令：%r" % cmd)
            data = fn(args)
            resp = {"id": rid, "ok": True, "data": _to_jsonable(data)}
        except WorkerError as e:
            resp = {"id": rid, "ok": False,
                    "error": {"code": e.code, "message": e.message, "detail": e.detail}}
        except Exception as e:
            resp = {"id": rid, "ok": False,
                    "error": {"code": E_INTERNAL, "message": str(e),
                              "detail": traceback.format_exc()[-800:]}}
        out.write(json.dumps(resp, ensure_ascii=False) + "\n")
        out.flush()


if __name__ == "__main__":
    main()
