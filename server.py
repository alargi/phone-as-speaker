#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
phone-as-speaker · 主机端（Windows）
======================================

把本机「正在播放的声音」实时送到手机 / 平板播放，并提供一个
**浏览器图形化控制台**来管理和监控整个过程。

链路层自动探测四条通路并择优使用（见 usblink.py）：

    1. ADB 反向端口转发（Android，纯 USB）   RTT 1~2 ms，延迟最低
    2. USB 网络共享（Android / iOS）          走 USB，多一层 NAT
    3. 有线以太网 LAN                         PC 一侧抖动消失
    4. Wi-Fi                                  零配置兜底

链路类型还会自动决定推荐参数：
    有线档（adb / tether / lan） → 10 ms 分帧、60 ms 客户端缓冲
    无线档（wifi）              → 20 ms 分帧、200 ms 客户端缓冲

与 spacedesk 的原理对应关系
--------------------------------------------------------------
  spacedesk                          |  本项目
  -----------------------------------|---------------------------
  虚拟显示适配器（IddCx / UMDF）      |  WASAPI 回环捕获（无需内核驱动）
  DWM 合成帧交付                      |  音频引擎回环缓冲
  帧内压缩 + 色度子采样               |  PCM 线性量化 + 可选重采样/降声道
  TCP/UDP 28252                       |  单端口 HTTP + WebSocket
  客户端解码渲染                      |  Web Audio API + 抖动缓冲
  HTML5 Viewer（零安装）              |  手机浏览器打开即用（零安装）
  USB 直连 / 网络共享（官方推荐）      |  ADB 反向转发 / USB 网络共享
  Driver Console（图形化）            |  **浏览器控制台 /console**

设计取舍
--------------------------------------------------------------
1. 不写虚拟音频驱动。音频有 WASAPI Loopback，直接就能拿到默认输出设备的
   正在播放内容，省掉一整个内核驱动与签名问题。
2. 用 WebSocket(TCP) 而不是 UDP。它自带的按序到达与重传省掉了一整层
   私有分片协议；弱网下的队头阻塞由客户端抖动缓冲吸收。
3. 不压缩（PCM 直传）。1.5 Mbps 对任何链路都不构成压力，加编解码只会
   平白多出一个延迟环节。

用法
--------------------------------------------------------------
    python server.py                     # 自动探测链路并启动
    python server.py --open-console      # 启动后自动打开控制台
    python server.py --link adb          # 强制走 ADB 反向转发
    python server.py --link none         # 跳过链路探测，只听局域网
    python server.py --list-devices      # 列出可用的回环设备

启动后：
    · 主机端控制台   http://127.0.0.1:8787/console
    · 手机 / 平板    http://<终端里打印的地址>:8787
    · 结构化状态     http://127.0.0.1:8787/api/status
"""

from __future__ import annotations

import argparse
import asyncio
import io
import json
import os
import socket
import sys
import threading
import time
import webbrowser
from collections import deque
from pathlib import Path

try:
    import numpy as np
    from aiohttp import WSMsgType, web
except ImportError as _exc:                            # noqa: BLE001
    raise SystemExit(
        f"\n[!] 缺少依赖：{_exc.name}\n"
        f"\n    当前解释器：{sys.executable}\n"
        f"\n    安装方式（任选其一）：\n"
        f"      · 双击运行 setup.bat —— 会自动建本地 .venv 并装好依赖（推荐）\n"
        f"      · 或手动执行：pip install -r requirements.txt\n"
    ) from None

try:
    import usblink
except ImportError:                                    # 允许单独复制 server.py 使用
    usblink = None


APP_DIR = Path(__file__).resolve().parent
STATIC_DIR = APP_DIR / "static"

# 隐藏终端运行时的落点：进程号写在这里给 stop.bat 用，
# 标准输出追加到日志文件，保证"没有终端"但仍有排查线索。
PID_FILE = APP_DIR / ".speaker.pid"
LOG_FILE = APP_DIR / "speaker.log"


def _redirect_output(path: Path) -> None:
    """把 stdout/stderr 接到日志文件。

    pythonw.exe 启动时 sys.stdout 是 None，此时把输出丢给文件，
    出问题还能打开 speaker.log 看，而不是两眼一抹黑。
    """
    try:
        f = open(path, "a", encoding="utf-8", buffering=1)
    except Exception:                                 # noqa: BLE001
        return
    sys.stdout = f
    sys.stderr = f


def _write_pidfile(port: int) -> None:
    """写下 pid + 端口，让 stop.bat 能精确找到本实例。"""
    try:
        PID_FILE.write_text(f"{os.getpid()}\n{port}\n", encoding="utf-8")
    except Exception:                                 # noqa: BLE001
        pass


def _remove_pidfile() -> None:
    """只删自己写的那个 pid 文件，避免误删后来实例的记录。"""
    try:
        if PID_FILE.exists():
            first = PID_FILE.read_text(encoding="utf-8", errors="ignore").split("\n", 1)[0].strip()
            if first == str(os.getpid()):
                PID_FILE.unlink()
    except Exception:                                 # noqa: BLE001
        pass


# 音频参数量级：<= 0 表示跟随设备原生值
DEFAULT_RATE = 48000
DEFAULT_CHANNELS = 2
DEFAULT_FRAME_MS = 10
DEFAULT_QUEUE_MAX = 15

# 控制台允许的取值（防止 API 被灌入离谱参数）
ALLOWED_FRAME_MS = (5, 10, 20, 30, 40, 60, 100)
ALLOWED_RATES = (0, 48000, 44100, 32000, 24000, 16000, 8000)
ALLOWED_CHANNELS = (1, 2)

LOG_RING: deque[str] = deque(maxlen=300)


def log(msg: str = "") -> None:
    """打印并同时记入日志环形缓冲，供控制台读取。"""
    line = f"{time.strftime('%H:%M:%S')}  {msg}" if msg else ""
    LOG_RING.append(line)
    print(msg, flush=True)


# --------------------------------------------------------------------------
# 音频处理：重采样与分帧
# --------------------------------------------------------------------------

def _moving_average(x: np.ndarray, width: int) -> np.ndarray:
    """沿时间轴做滑动平均，用于降采样前的抗混叠。

    用前缀和实现，复杂度 O(n)，比 np.convolve 更省内存。
    x 形状 (frames, channels)，返回同形状 float64。
    """
    if width < 2:
        return x.astype(np.float64, copy=False)

    n = x.shape[0]
    csum = np.zeros((n + 1, x.shape[1]), dtype=np.float64)
    np.cumsum(x, axis=0, out=csum[1:])

    idx = np.arange(n)
    lo = np.clip(idx - width // 2, 0, n)
    hi = np.clip(idx - width // 2 + width, 0, n)
    counts = np.maximum(hi - lo, 1)[:, None]
    return (csum[hi] - csum[lo]) / counts


def resample_i16(x: np.ndarray, src_rate: int, dst_rate: int) -> np.ndarray:
    """把 int16 PCM 从 src_rate 重采样到 dst_rate。

    先滑动平均抗混叠，再线性插值。这是刻意保持简单的实现：
    48k -> 24k/16k 这类整数比场景下，滑动平均就是标准的盒式低通，
    质量足够；非整数比场景会有轻微额外失真，可用 --rate 0 规避。
    """
    if src_rate == dst_rate or x.shape[0] == 0:
        return x

    ratio = src_rate / float(dst_rate)
    y = _moving_average(x, int(round(ratio))) if ratio > 1.0 else x.astype(np.float64)

    n_out = int(round(x.shape[0] * dst_rate / float(src_rate)))
    if n_out <= 0:
        return np.zeros((0, x.shape[1]), dtype="<i2")

    pos = np.linspace(0.0, x.shape[0] - 1, n_out)
    i0 = np.floor(pos).astype(np.int64)
    i1 = np.minimum(i0 + 1, x.shape[0] - 1)
    frac = (pos - i0)[:, None]

    out = y[i0] * (1.0 - frac) + y[i1] * frac
    return np.clip(np.round(out), -32768, 32767).astype("<i2")


class Framer:
    """把不定长音频块切成固定长度帧，保证客户端调度节拍稳定。"""

    def __init__(self, frame_samples: int, channels: int) -> None:
        self.frame_samples = frame_samples
        self.channels = channels
        self._buf = np.zeros((0, channels), dtype="<i2")

    def push(self, block: np.ndarray) -> list[np.ndarray]:
        if block.shape[0]:
            self._buf = np.concatenate([self._buf, block], axis=0)

        # 设备比设定更快时，残留不能无限堆积
        max_residual = self.frame_samples * 4
        if self._buf.shape[0] > max_residual:
            self._buf = self._buf[-max_residual:]

        frames = []
        while self._buf.shape[0] >= self.frame_samples:
            frames.append(np.ascontiguousarray(self._buf[: self.frame_samples]))
            self._buf = self._buf[self.frame_samples:]
        return frames


def to_int16(block: np.ndarray, want_channels: int) -> np.ndarray:
    """float32 [-1,1] -> int16，并做声道数归一。"""
    if block.ndim == 1:
        block = block[:, None]

    have = block.shape[1]
    if want_channels == 1 and have > 1:
        block = block.mean(axis=1, keepdims=True)
    elif want_channels == 2 and have == 1:
        block = np.repeat(block, 2, axis=1)
    elif have > want_channels:
        block = block[:, :want_channels]

    np.clip(block, -1.0, 1.0, out=block)
    return (block * 32767.0).astype("<i2")


# --------------------------------------------------------------------------
# 捕获线程
# --------------------------------------------------------------------------

def ensure_com() -> None:
    """确保当前线程已初始化 COM（**只在音频后端已经导入之后**才动手）。

    背景：COM 是按线程初始化的，而 soundcard 只在模块首次导入时调用一次
    CoInitializeEx（其 _COMLibrary.__init__）。于是第一个用到 soundcard 的
    线程没问题，之后任何**新建的线程**再用 WASAPI 都会撞
    0x800401f0（CO_E_NOTINITIALIZED）——我们的采集线程在「改参数 / 重启
    采集」时正是被重建的，所以每个新线程都得自己补一次。

    为什么必须等到模块导入之后：soundcard 用
    `check_error(CoInitializeEx(...))` 判断结果，而 check_error 把任何
    非 S_OK 都当错误，**包括 S_FALSE**。如果我们先把这个线程初始化好，
    soundcard 再调用只会拿到 S_FALSE，于是抛出一个莫名其妙的
    0x100000001。所以模块还没导入时，我们什么都不做，交给它自己初始化。
    """
    if sys.platform != "win32":
        return
    if "soundcard.mediafoundation" not in sys.modules:
        return
    try:
        import ctypes
        ctypes.windll.ole32.CoInitializeEx(None, 0x0)   # COINIT_MULTITHREADED
    except Exception:                                 # noqa: BLE001
        pass


def preload_audio_backend() -> bool:
    """启动时把音频后端导入一次，把 COM 初始化的时机与线程固定下来。

    这样模块级 _COMLibrary 只会构造一次（拿到确定的 S_OK），之后每个要用
    WASAPI 的线程只需自己 ensure_com()，不再依赖「谁先导入」的偶然顺序。
    """
    try:
        import soundcard  # noqa: F401
        return True
    except Exception as exc:                          # noqa: BLE001
        log(f"[!] 音频后端导入失败：{type(exc).__name__}: {exc}")
        return False


def _resolve_loopback(name_hint: str):
    """找到默认输出设备对应的 WASAPI 回环录音设备。"""
    import soundcard as sc

    mics = [m for m in sc.all_microphones(include_loopback=True)
            if getattr(m, "isloopback", False)]

    if name_hint:
        for m in mics:
            if name_hint.lower() in m.name.lower():
                return m
        raise RuntimeError(
            f"没有匹配 «{name_hint}» 的回环设备。可用设备：\n  "
            + "\n  ".join(m.name for m in mics)
        )

    if not mics:
        raise RuntimeError(
            "系统里找不到任何回环（loopback）录音设备。"
            "请确认已启用播放设备且驱动正常。"
        )

    speaker = sc.default_speaker()
    if speaker is not None:
        for m in mics:
            if m.name == speaker.name:
                return m
        for m in mics:
            if str(getattr(m, "id", "")) == str(getattr(speaker, "id", "")):
                return m

    return mics[0]


def _negotiate_capture_rate(device, preferred: int) -> int:
    """协商捕获采样率。

    WASAPI 共享模式要求以设备混音格式打开，而我们无从直接查询该值
    （soundcard 未暴露），因此逐个候选试开一次，成功即用。
    """
    candidates = [preferred] + [r for r in (48000, 44100, 32000, 96000) if r != preferred]
    last_err: Exception | None = None
    for rate in candidates:
        try:
            with device.recorder(samplerate=rate, blocksize=256) as rec:
                rec.record(numframes=256)
            return rate
        except Exception as exc:                      # noqa: BLE001
            last_err = exc
    raise RuntimeError(f"回环设备无法以任何候选采样率打开：{last_err!r}")


class AudioCapture:
    """在独立线程里跑 WASAPI 回环捕获，通过回调把帧交给事件循环。

    参数在 _run() 开头一次性快照，运行期间不再读 cfg——因此修改参数
    必须走「停掉旧的、新建一个」的路子（见 SpeakerServer.restart_capture）。
    """

    def __init__(self, cfg, emit) -> None:
        self.cfg = cfg
        self.emit = emit                # callable(np.ndarray) -> None
        self.stop_event = threading.Event()
        self.error: str | None = None
        self.device_name = "-"
        self.capture_rate = 0
        self._thread = threading.Thread(target=self._run, name="audio-capture", daemon=True)

    # ---- 生命周期 ----
    def start(self) -> None:
        if not self._thread.is_alive():
            self._thread = threading.Thread(target=self._run, name="audio-capture", daemon=True)
        self._thread.start()

    def stop(self, wait: float = 3.0) -> None:
        self.stop_event.set()
        if self._thread.is_alive():
            self._thread.join(timeout=wait)

    @property
    def alive(self) -> bool:
        return self._thread.is_alive()

    # ---- 采集主循环 ----
    def _run(self) -> None:
        ensure_com()                                  # 新线程必须自己初始化 COM
        try:
            # 参数快照：之后改 cfg 不会影响本次运行
            dev_hint = self.cfg.device
            want_rate = self.cfg.effective_rate
            want_ch = self.cfg.channels
            frame_ms = self.cfg.frame_ms

            dev = _resolve_loopback(dev_hint)
            self.device_name = dev.name

            self.capture_rate = _negotiate_capture_rate(dev, self.cfg.capture_rate)
            block = max(int(self.capture_rate * frame_ms / 1000), 128)

            framer = Framer(max(int(want_rate * frame_ms / 1000), 1), want_ch)

            if want_rate != self.capture_rate:
                log(f"[*] 重采样：{self.capture_rate} Hz -> {want_rate} Hz")

            with dev.recorder(samplerate=self.capture_rate, blocksize=block) as rec:
                while not self.stop_event.is_set():
                    data = rec.record(numframes=block)
                    pcm = to_int16(data, want_ch)
                    if want_rate != self.capture_rate:
                        pcm = resample_i16(pcm, self.capture_rate, want_rate)
                    for frame in framer.push(pcm):
                        self.emit(frame)

        except Exception as exc:                      # noqa: BLE001
            if not self.stop_event.is_set():
                self.error = f"{type(exc).__name__}: {exc}"
                log(f"[!] 音频采集出错：{self.error}")


# --------------------------------------------------------------------------
# HTTP / WebSocket 服务
# --------------------------------------------------------------------------

class Viewer:
    """一路客户端连接。每路独立队列，避免慢客户端拖累其它人。"""

    _seq = 0

    def __init__(self, ws: web.WebSocketResponse, queue_max: int, ua: str = "") -> None:
        Viewer._seq += 1
        self.id = Viewer._seq
        self.ws = ws
        self.queue: asyncio.Queue = asyncio.Queue(maxsize=queue_max)
        self.dropped = 0
        self.sent_frames = 0
        self.sent_bytes = 0
        self.rtt_ms: float | None = None       # 由客户端回报的链路往返时延
        self.jitter_ms: float | None = None    # 客户端回报的 RTT 标准差
        self.buffer_ms: int | None = None      # 客户端当前缓冲档位
        self.ua = ua[:70]
        self.since = time.time()

    def offer(self, frame: bytes) -> None:
        if self.queue.full():
            try:
                self.queue.get_nowait()
                self.dropped += 1
            except asyncio.QueueEmpty:
                pass
        try:
            self.queue.put_nowait(frame)
        except asyncio.QueueFull:
            self.dropped += 1

    async def writer(self) -> None:
        while True:
            frame = await self.queue.get()
            await self.ws.send_bytes(frame)
            self.sent_frames += 1
            self.sent_bytes += len(frame)

    def snapshot(self) -> dict:
        return {
            "id": self.id,
            "ua": self.ua,
            "since": round(time.time() - self.since, 1),
            "sent_frames": self.sent_frames,
            "dropped": self.dropped,
            "rtt_ms": round(self.rtt_ms, 1) if self.rtt_ms is not None else None,
            "jitter_ms": round(self.jitter_ms, 1) if self.jitter_ms is not None else None,
            "buffer_ms": self.buffer_ms,
        }


class ServerState:
    def __init__(self, cfg) -> None:
        self.cfg = cfg
        self.clients: dict[int, Viewer] = {}
        self.frames_out = 0
        self.bytes_out = 0
        self.started = time.time()
        self.capture: AudioCapture | None = None
        self.link_kind = "none"
        self.best_url = ""
        self.link_report = None
        self.link_error = ""
        self.config_rev = 0
        self.history: deque[dict] = deque(maxlen=120)
        self._last_t = time.time()
        self._last_frames = 0
        self._last_bytes = 0

    def sample(self) -> None:
        """每秒采一次瞬时帧率 / 码率 / 客户端数，供控制台画曲线。"""
        now = time.time()
        dt = now - self._last_t
        if dt <= 0:
            return
        fps = (self.frames_out - self._last_frames) / dt
        kbps = (self.bytes_out - self._last_bytes) * 8 / dt / 1000.0
        self.history.append({
            "t": round(now - self.started, 1),
            "fps": round(fps, 1),
            "kbps": round(kbps, 1),
            "clients": len(self.clients),
        })
        self._last_t, self._last_frames, self._last_bytes = now, self.frames_out, self.bytes_out


class SpeakerServer:
    def __init__(self, cfg) -> None:
        self.cfg = cfg
        self.state = ServerState(cfg)
        self.loop: asyncio.AbstractEventLoop | None = None
        self.audio_q: asyncio.Queue = asyncio.Queue(maxsize=cfg.queue_max)
        self.tasks: list[asyncio.Task] = []

    # ---- 捕获线程 -> 事件循环 ----
    def _emit(self, frame: np.ndarray) -> None:
        payload = frame.tobytes()
        if self.loop is None:
            return
        try:
            self.loop.call_soon_threadsafe(self._enqueue, payload)
        except RuntimeError:
            pass

    def _enqueue(self, payload: bytes) -> None:
        if self.audio_q.full():
            try:
                self.audio_q.get_nowait()
            except asyncio.QueueEmpty:
                pass
        try:
            self.audio_q.put_nowait(payload)
        except asyncio.QueueFull:
            pass

    # ---- 捕获生命周期（可重启） ----
    def _drain_audio_queue(self) -> None:
        """丢弃队列里的残留帧，避免重启后先播出上一轮的声音。"""
        while True:
            try:
                self.audio_q.get_nowait()
            except asyncio.QueueEmpty:
                return

    def start_capture(self) -> None:
        if self.state.capture and self.state.capture.alive:
            return
        # 注意：这里**不能**新建 audio_q。broadcast() 正在 await 这个队列对象的
        # get()，一旦把它替换掉，消费端就永远挂在旧队列上，重启后再也发不出帧。
        # 复用同一个队列，重启时把残留清掉即可。
        self._drain_audio_queue()
        self.state.capture = AudioCapture(self.cfg, self._emit)
        self.state.capture.start()
        log("[*] 开始采集")

    def stop_capture(self) -> None:
        cap = self.state.capture
        if cap:
            cap.stop()
        self.state.capture = None
        log("[*] 已停止采集")

    def restart_capture(self) -> None:
        self.stop_capture()
        self.start_capture()

    # ---- HTTP：页面 ----
    async def handle_index(self, request: web.Request) -> web.StreamResponse:
        page = STATIC_DIR / "index.html"
        if not page.exists():
            return web.Response(status=500, text="static/index.html 缺失")
        return web.FileResponse(page)

    async def handle_console(self, request: web.Request) -> web.StreamResponse:
        page = STATIC_DIR / "console.html"
        if not page.exists():
            return web.Response(status=500, text="static/console.html 缺失")
        return web.FileResponse(page)

    async def handle_favicon(self, request: web.Request) -> web.StreamResponse:
        return web.Response(status=204)

    # ---- HTTP：旧版健康检查（保持兼容） ----
    async def handle_health(self, request: web.Request) -> web.StreamResponse:
        s = self.state
        cap = s.capture
        elapsed = max(time.time() - s.started, 0.001)
        rtts = [v.rtt_ms for v in s.clients.values() if v.rtt_ms is not None]
        return web.json_response({
            "ok": cap is not None and cap.error is None,
            "device": cap.device_name if cap else None,
            "capture_rate": (cap.capture_rate or self.cfg.capture_rate) if cap else None,
            "rate": self.cfg.effective_rate,
            "channels": self.cfg.channels,
            "frame_ms": self.cfg.frame_ms,
            "frame_bytes": self.cfg.frame_bytes,
            "clients": len(s.clients),
            "frames_out": s.frames_out,
            "kbps": round(s.bytes_out * 8 / elapsed / 1000.0, 1),
            "link": s.link_kind,
            "best_url": s.best_url,
            "rtt_ms": round(min(rtts), 1) if rtts else None,
            "capture_error": cap.error if cap else "not started",
        })

    # ---- HTTP：控制台 API ----
    def link_payload(self) -> dict:
        rep = self.state.link_report
        profile = usblink.link_profile(self.state.link_kind) if usblink else \
            {"frame_ms": 20, "buffer_ms": 200, "tier": "wireless"}
        return {
            "kind": self.state.link_kind,
            "best_url": self.state.best_url,
            "profile": profile,
            "error": self.state.link_error,
            "links": [l.__dict__ for l in rep.ordered] if rep else [],
            "warnings": list(rep.warnings) if rep else [],
            "adb_available": bool(rep and rep.adb_path),
            "adb_version": rep.adb_versions if rep else "",
            "devices": [d for d in rep.devices] if rep else [],
        }

    def status_payload(self) -> dict:
        s = self.state
        cap = s.capture
        elapsed = max(time.time() - s.started, 0.001)
        return {
            "ok": bool(cap) and cap.error is None,
            "uptime": round(elapsed, 1),
            "capture": {
                "running": bool(cap and cap.alive),
                "device": cap.device_name if cap else None,
                "capture_rate": cap.capture_rate if cap else 0,
                "error": cap.error if cap else None,
            },
            "config": {
                "rate": self.cfg.effective_rate,
                "requested_rate": self.cfg.rate,
                "capture_rate_req": self.cfg.capture_rate,
                "channels": self.cfg.channels,
                "frame_ms": self.cfg.frame_ms,
                "frame_bytes": self.cfg.frame_bytes,
                "queue_max": self.cfg.queue_max,
                "device": self.cfg.device,
                "link": self.cfg.link,
                "rev": s.config_rev,
            },
            "totals": {
                "frames": s.frames_out,
                "bytes": s.bytes_out,
                "kbps": round(s.bytes_out * 8 / elapsed / 1000.0, 1),
                "clients": len(s.clients),
            },
            "clients": [v.snapshot() for v in s.clients.values()],
            "history": list(s.history),
            "link": self.link_payload(),
            "log": list(LOG_RING)[-80:],
        }

    async def handle_status(self, request: web.Request) -> web.StreamResponse:
        return web.json_response(self.status_payload())

    async def handle_devices(self, request: web.Request) -> web.StreamResponse:
        # 设备枚举要开 COM、还会枚举硬件，放到线程里跑，别卡住事件循环
        try:
            data = await asyncio.to_thread(collect_devices)
        except Exception as exc:                      # noqa: BLE001
            return web.json_response(
                {"speakers": [], "loopback": [],
                 "error": f"{type(exc).__name__}: {exc}"}, status=500)
        cap = self.state.capture
        data["current"] = cap.device_name if cap else None
        data["hint"] = self.cfg.device
        return web.json_response(data)

    async def handle_config(self, request: web.Request) -> web.StreamResponse:
        try:
            data = await request.json()
        except Exception:                             # noqa: BLE001
            return web.json_response({"ok": False, "error": "请求体不是合法 JSON"}, status=400)
        if not isinstance(data, dict):
            return web.json_response({"ok": False, "error": "请求体必须是对象"}, status=400)

        cfg = self.cfg
        changed: list[str] = []
        rejected: list[str] = []

        if "frame_ms" in data:
            try:
                v = int(data["frame_ms"])
            except (TypeError, ValueError):
                v = -1
            if v in ALLOWED_FRAME_MS:
                if v != cfg.frame_ms:
                    cfg.frame_ms = v
                    changed.append(f"分帧 {v}ms")
            else:
                rejected.append(f"frame_ms 只允许 {ALLOWED_FRAME_MS}")

        if "channels" in data:
            try:
                v = int(data["channels"])
            except (TypeError, ValueError):
                v = -1
            if v in ALLOWED_CHANNELS:
                if v != cfg.channels:
                    cfg.channels = v
                    changed.append(f"{v} 声道")
            else:
                rejected.append("channels 只允许 1 或 2")

        if "rate" in data:
            try:
                v = int(data["rate"])
            except (TypeError, ValueError):
                v = -1
            if v in ALLOWED_RATES:
                if v != cfg.rate:
                    cfg.rate = v
                    changed.append("跟随捕获采样率" if v == 0 else f"目标 {v}Hz")
            else:
                rejected.append(f"rate 只允许 {ALLOWED_RATES}")

        if "device" in data:
            v = str(data["device"] or "").strip()
            if v != cfg.device:
                cfg.device = v
                changed.append(f"设备 «{v or '默认'}»")

        if changed:
            self.state.config_rev += 1
            log(f"[*] 参数已更新：{'、'.join(changed)}（重启采集生效）")
            self.restart_capture()
            await self.broadcast_config()
        elif not rejected:
            log("[*] 参数未变化")

        resp = {"ok": True, "changed": changed, "rejected": rejected}
        resp.update(self.status_payload())
        return web.json_response(resp)

    async def handle_control(self, request: web.Request) -> web.StreamResponse:
        try:
            data = await request.json()
        except Exception:                             # noqa: BLE001
            data = {}
        action = str((data or {}).get("action", ""))

        if action == "start":
            self.start_capture()
        elif action == "stop":
            self.stop_capture()
        elif action == "restart":
            self.restart_capture()
        elif action == "clear_log":
            LOG_RING.clear()
            log("[*] 日志已清空")
        elif action == "disconnect_clients":
            n = len(self.state.clients)
            for v in list(self.state.clients.values()):
                try:
                    await v.ws.close()
                except Exception:                     # noqa: BLE001
                    pass
            log(f"[*] 已断开 {n} 路客户端")
        elif action == "refresh_link":
            await self.refresh_link()
        elif action == "shutdown":
            # 隐藏终端运行时没有 Ctrl+C 可用，所以退出这件事必须能从控制台发起。
            log("[*] 收到退出指令，正在关闭…")
            asyncio.create_task(self._shutdown_soon())
        else:
            return web.json_response(
                {"ok": False, "error": f"未知动作 «{action}»"}, status=400)

        resp = {"ok": True, "action": action}
        resp.update(self.status_payload())
        return web.json_response(resp)

    async def _shutdown_soon(self) -> None:
        """先把 HTTP 响应发出去，再清理链路并退出进程。"""
        await asyncio.sleep(0.6)
        try:
            if usblink is not None and self.cfg.link in ("auto", "adb"):
                await asyncio.to_thread(usblink.teardown, self.cfg.port, log)
        except Exception:                             # noqa: BLE001
            pass
        self.stop_capture()
        log("[*] 已退出")
        # os._exit 会跳过 main 的 finally，所以要在这里自己清 pid 文件。
        _remove_pidfile()
        # 用 os._exit 而不是 sys.exit：这里在事件循环的任务里，
        # 抛异常只会让这个任务结束，进程还会继续跑。
        os._exit(0)

    async def handle_quit(self, request: web.Request) -> web.StreamResponse:
        """给 stop.bat 用的"优雅退出"入口：GET /api/quit。

        只允许本机发起——服务默认监听 0.0.0.0，若放开到局域网，
        同网段任何人都能一句话把服务关掉。
        """
        peer = (request.remote or "").strip()
        if peer not in ("127.0.0.1", "::1", "localhost"):
            return web.json_response(
                {"ok": False, "error": "仅允许本机调用"}, status=403)
        log("[*] 收到本机退出请求（stop.bat），正在关闭…")
        asyncio.create_task(self._shutdown_soon())
        return web.json_response({"ok": True, "action": "quit"})

    async def handle_qr(self, request: web.Request) -> web.StreamResponse:
        data = request.query.get("data") or self.state.best_url or ""
        if not data:
            return web.Response(status=404, text="没有可编码的地址")
        try:
            import qrcode
            import qrcode.image.svg
            qr = qrcode.QRCode(border=1, box_size=10)
            qr.add_data(data)
            qr.make(fit=True)
            img = qr.make_image(image_factory=qrcode.image.svg.SvgPathImage)
            buf = io.BytesIO()
            img.save(buf)
            svg = buf.getvalue().decode("utf-8")
        except Exception as exc:                      # noqa: BLE001
            return web.Response(status=500, text=f"二维码生成失败：{exc}")
        return web.Response(text=svg, content_type="image/svg+xml",
                            headers={"Cache-Control": "no-store"})

    async def refresh_link(self) -> None:
        """重新探测链路（用户刚打开网络共享 / 刚插上数据线时用）。"""
        if usblink is None:
            self.state.link_error = "未找到 usblink 模块"
            return
        try:
            rep = await asyncio.to_thread(usblink.setup, self.cfg.port, self.cfg.link, log)
            self.state.link_report = rep
            self.state.link_error = ""
            best = rep.best
            if best:
                self.state.link_kind = best.kind
                self.state.best_url = best.url
                log(f"[*] 链路：{best.kind} → {best.url}")
            else:
                self.state.link_kind = "none"
                self.state.best_url = ""
                log("[*] 未发现任何可用链路")
        except Exception as exc:                      # noqa: BLE001
            self.state.link_error = f"{type(exc).__name__}: {exc}"
            log(f"[!] 链路探测失败：{self.state.link_error}")

    # ---- WebSocket ----
    def config_msg(self) -> str:
        cap = self.state.capture
        return json.dumps({
            "type": "config",
            "rate": self.cfg.effective_rate,
            "channels": self.cfg.channels,
            "frame_ms": self.cfg.frame_ms,
            "frame_bytes": self.cfg.frame_bytes,
            "source": cap.device_name if cap else None,
            "link": self.state.link_kind,
            "profile": usblink.link_profile(self.state.link_kind) if usblink else None,
            "rev": self.state.config_rev,
        })

    async def broadcast_config(self) -> None:
        """参数变了以后通知所有客户端重建 AudioBuffer。"""
        msg = self.config_msg()
        for v in list(self.state.clients.values()):
            try:
                await v.ws.send_str(msg)
            except Exception:                         # noqa: BLE001
                pass

    async def handle_ws(self, request: web.Request) -> web.StreamResponse:
        ws = web.WebSocketResponse(heartbeat=30, max_msg_size=1 << 20)
        await ws.prepare(request)

        viewer = Viewer(ws, self.cfg.queue_max,
                        request.headers.get("User-Agent") or "")
        self.state.clients[viewer.id] = viewer
        log(f"[+] 客户端 #{viewer.id} 接入（共 {len(self.state.clients)} 路）  {viewer.ua}")

        writer = asyncio.create_task(viewer.writer())
        try:
            await ws.send_str(self.config_msg())

            async for msg in ws:
                if msg.type == WSMsgType.ERROR:
                    break
                if msg.type != WSMsgType.TEXT:
                    continue
                try:
                    req = json.loads(msg.data)
                except (ValueError, TypeError):
                    continue

                mtype = req.get("type")
                if mtype == "ping":
                    # 链路质量探针：原样回带时间戳，客户端据此算 RTT 与抖动
                    await ws.send_str(json.dumps({"type": "pong", "t": req.get("t")}))
                elif mtype == "rtt":
                    try:
                        viewer.rtt_ms = float(req.get("ms"))
                    except (TypeError, ValueError):
                        pass
                    try:
                        viewer.jitter_ms = float(req.get("jitter"))
                    except (TypeError, ValueError):
                        pass
                    try:
                        viewer.buffer_ms = int(req.get("buffer_ms"))
                    except (TypeError, ValueError):
                        pass
        except Exception as exc:                      # noqa: BLE001
            print(f"[!] 客户端 #{viewer.id} 处理异常：{type(exc).__name__}: {exc}", flush=True)
        finally:
            writer.cancel()
            self.state.clients.pop(viewer.id, None)
            log(f"[-] 客户端 #{viewer.id} 断开，已发送 {viewer.sent_frames} 帧"
                f"，丢弃 {viewer.dropped} 帧（剩余 {len(self.state.clients)} 路）")

        return ws

    # ---- 后台任务 ----
    async def broadcast(self) -> None:
        while True:
            payload = await self.audio_q.get()
            s = self.state
            s.frames_out += 1
            s.bytes_out += len(payload)
            for viewer in list(s.clients.values()):
                viewer.offer(payload)

    async def sampler(self) -> None:
        while True:
            await asyncio.sleep(1)
            self.state.sample()

    async def stats(self) -> None:
        while True:
            await asyncio.sleep(5)
            s = self.state
            cap = s.capture
            err = f"  ⚠ {cap.error}" if cap and cap.error else ""
            rtts = [v.rtt_ms for v in s.clients.values() if v.rtt_ms is not None]
            rtt = f"  链路RTT {min(rtts):.1f} ms" if rtts else ""
            log(f"[*] 帧 {s.frames_out:>7}  "
                f"码率 {s.bytes_out * 8 / 1000 / max(time.time() - s.started, 0.001):>7.0f} kbps  "
                f"客户端 {len(s.clients)}{rtt}{err}")

    # ---- 生命周期 ----
    async def on_startup(self, app: web.Application) -> None:
        self.loop = asyncio.get_running_loop()
        self.start_capture()
        self.tasks = [
            asyncio.create_task(self.broadcast()),
            asyncio.create_task(self.sampler()),
            asyncio.create_task(self.stats()),
        ]

    async def on_cleanup(self, app: web.Application) -> None:
        for t in self.tasks:
            t.cancel()
        self.stop_capture()

    def build_app(self) -> web.Application:
        app = web.Application()
        app.router.add_get("/", self.handle_index)
        app.router.add_get("/console", self.handle_console)
        app.router.add_get("/ws", self.handle_ws)
        app.router.add_get("/health", self.handle_health)
        app.router.add_get("/favicon.ico", self.handle_favicon)
        app.router.add_get("/api/status", self.handle_status)
        app.router.add_get("/api/devices", self.handle_devices)
        app.router.add_get("/api/qr.svg", self.handle_qr)
        app.router.add_post("/api/config", self.handle_config)
        app.router.add_post("/api/control", self.handle_control)
        app.router.add_get("/api/quit", self.handle_quit)
        app.router.add_post("/api/quit", self.handle_quit)
        app.on_startup.append(self.on_startup)
        app.on_cleanup.append(self.on_cleanup)
        return app


# --------------------------------------------------------------------------
# 命令行
# --------------------------------------------------------------------------

class Config:
    def __init__(self, args) -> None:
        self.host = args.host
        self.port = args.port
        self.rate = args.rate
        self.capture_rate = args.capture_rate
        self.channels = args.channels
        self.frame_ms = args.frame_ms
        self.device = args.device
        self.queue_max = args.queue_max
        self.link = args.link

    @property
    def effective_rate(self) -> int:
        """实际推送给客户端的采样率。--rate 为 0 时等同于捕获采样率。"""
        return self.rate if self.rate > 0 else self.capture_rate

    @property
    def frame_bytes(self) -> int:
        return max(int(self.effective_rate * self.frame_ms / 1000), 1) * self.channels * 2


def collect_devices() -> dict:
    """枚举输出设备与回环设备，供 HTTP API 与控制台使用。"""
    ensure_com()
    import soundcard as sc

    return {
        "speakers": [s.name for s in sc.all_speakers()],
        "loopback": [m.name for m in sc.all_microphones(include_loopback=True)
                     if getattr(m, "isloopback", False)],
    }


def list_devices() -> None:
    ensure_com()
    import soundcard as sc

    log("输出设备（扬声器）：")
    for s in sc.all_speakers():
        log(f"  - {s.name}")

    mics = [m for m in sc.all_microphones(include_loopback=True)
            if getattr(m, "isloopback", False)]
    log("\n可用回环（loopback）录音设备：")
    for m in mics:
        log(f"  - {m.name}")
    log("\n用 --device 加设备名关键字可指定捕获哪一路输出，例如：")
    log('  python server.py --device "扬声器"')


def print_banner(cfg, link_report=None, console_url: str = "") -> None:
    rate = cfg.effective_rate
    kbps = rate * cfg.channels * 16 / 1000
    if cfg.rate <= 0:
        rate_txt = f"跟随捕获采样率（请求 {cfg.capture_rate} Hz）"
    else:
        rate_txt = f"{cfg.rate} Hz（捕获 {cfg.capture_rate} Hz → 重采样）"

    best = link_report.best if link_report else None
    profile = usblink.link_profile(best.kind if best else "none") if usblink else None
    qr_url = best.url if best else f"http://{local_ip()}:{cfg.port}"

    log()
    log("=" * 66)
    log("  外置扬声器 · 主机端")
    log("=" * 66)
    log(f"  音频格式   {rate_txt} / {cfg.channels} 声道 / 16 bit")
    log(f"  分帧长度   {cfg.frame_ms} ms" + (f"   （{profile['tier']}档推荐 "
        f"{profile['frame_ms']}ms）" if profile else ""))
    log(f"  未压缩码率 ≈ {kbps:.0f} kbps")
    log(f"  监听地址   {cfg.host}:{cfg.port}")
    log("-" * 66)

    if best is not None:
        log(f"  链路类型   [{best.kind}] {best.label}")
        if best.model:
            log(f"  目标设备   {best.model}")
        if best.adapter:
            log(f"  承载网卡   {best.adapter}")
    else:
        log("  链路类型   未发现可用链路（可稍后在控制台点「重新探测」）")

    log()
    log(f"  >>> 手机 / 平板浏览器打开： {qr_url}")
    if console_url:
        log(f"  >>> 主机端控制台：         {console_url}")
    log("=" * 66)

    others = [l for l in (link_report.ordered if link_report else []) if l is not best]
    if others:
        log()
        log("  其它可用地址：")
        for l in others:
            log(f"    [{l.kind:<6}] {l.url:<34} {l.label}")

    if link_report and link_report.warnings:
        log()
        log("  提示：")
        for w in link_report.warnings:
            log(f"    ! {w}")

    try:
        import qrcode
        qr = qrcode.QRCode(border=1)
        qr.add_data(qr_url)
        qr.make(fit=True)
        log()
        qr.print_ascii(invert=True)
    except Exception:                                 # noqa: BLE001
        pass

    log("\n按 Ctrl+C 停止。\n")


def local_ip() -> str:
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        ip = s.getsockname()[0]
        s.close()
        return ip
    except Exception:                                 # noqa: BLE001
        return "127.0.0.1"


def main() -> int:
    p = argparse.ArgumentParser(
        description="把 Windows 正在播放的声音送到手机 / 平板当外置扬声器，带浏览器控制台",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    p.add_argument("--host", default="0.0.0.0", help="监听地址")
    p.add_argument("--port", type=int, default=8787, help="监听端口")
    p.add_argument("--link", default="auto",
                   choices=["auto", "adb", "tether", "lan", "wifi", "none"],
                   help="链路类型；auto=自动探测并择优，none=跳过探测只听局域网")
    p.add_argument("--open-console", action="store_true",
                   help="启动后自动在默认浏览器打开控制台")
    p.add_argument("--no-console", action="store_true",
                   help="不自动打开控制台（优先级高于 --open-console）")
    p.add_argument("--rate", type=int, default=0,
                   help="推送给客户端的采样率；0 = 与捕获采样率一致（不重采样）")
    p.add_argument("--capture-rate", type=int, default=DEFAULT_RATE,
                   help="向系统请求的捕获采样率，会按 48000/44100/32000/96000 顺序协商")
    p.add_argument("--channels", type=int, choices=[1, 2], default=DEFAULT_CHANNELS,
                   help="声道数；1 可省一半带宽")
    p.add_argument("--frame-ms", type=int, default=DEFAULT_FRAME_MS,
                   help="网络分帧长度（毫秒）")
    p.add_argument("--device", default="", help="回环设备名关键字，留空用默认扬声器")
    p.add_argument("--queue-max", type=int, default=DEFAULT_QUEUE_MAX,
                   help="每路客户端待发队列上限（帧），超出丢最旧")
    p.add_argument("--list-devices", action="store_true", help="列出设备后退出")
    p.add_argument("--silent", action="store_true",
                   help="静默模式：不向控制台输出（run-silent.vbs 用，输出改写进 speaker.log）")

    args = p.parse_args()

    # 隐藏终端运行时 stdout/stderr 可能是 None（pythonw.exe）。
    # 这种情况下一律把输出追加进 speaker.log，保留排查线索；
    # 普通前台运行时只是把控制台编码固定成 UTF-8。
    if args.silent or sys.stdout is None:
        _redirect_output(LOG_FILE)
    else:
        try:
            sys.stdout.reconfigure(encoding="utf-8")
        except Exception:                             # noqa: BLE001
            pass

    # 先把音频后端导入一次，固定 COM 初始化的时机（见 ensure_com 的说明）
    preload_audio_backend()

    if args.list_devices:
        list_devices()
        return 0

    cfg = Config(args)

    # 先探测链路，再打印横幅——这样横幅上的地址一定是真实可用的
    link_report = None
    if usblink is not None and cfg.link != "none":
        try:
            link_report = usblink.setup(cfg.port, cfg.link, log=log)
        except Exception as exc:                      # noqa: BLE001
            log(f"[!] 链路探测失败：{type(exc).__name__}: {exc}")

    # 没有显式指定分帧时，按链路类型自动选档
    if link_report is not None and link_report.best and args.frame_ms == DEFAULT_FRAME_MS:
        prof = usblink.link_profile(link_report.best.kind)
        if prof["frame_ms"] != cfg.frame_ms:
            log(f"[*] 按{prof['tier']}档自动调整分帧：{cfg.frame_ms}ms → {prof['frame_ms']}ms")
            cfg.frame_ms = prof["frame_ms"]

    console_url = f"http://127.0.0.1:{cfg.port}/console"
    print_banner(cfg, link_report, console_url)

    server = SpeakerServer(cfg)
    server.state.link_report = link_report
    if link_report and link_report.best:
        server.state.link_kind = link_report.best.kind
        server.state.best_url = link_report.best.url

    if args.open_console and not args.no_console:
        def _open() -> None:
            time.sleep(1.5)
            try:
                webbrowser.open(console_url)
            except Exception:                         # noqa: BLE001
                pass
        threading.Thread(target=_open, daemon=True).start()

    try:
        _write_pidfile(cfg.port)
        web.run_app(server.build_app(), host=cfg.host, port=cfg.port,
                    print=None, access_log=None)
    except KeyboardInterrupt:
        pass
    finally:
        _remove_pidfile()
        log()
        if usblink is not None and cfg.link in ("auto", "adb"):
            try:
                usblink.teardown(cfg.port, log=log)
            except Exception:                         # noqa: BLE001
                pass
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
