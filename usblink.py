#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
链路助手（有线优先，无线兜底）
==============================

负责把「PC 上跑的 HTTP 服务」变成「手机 / 平板浏览器能直接打开的东西」。
四种通路，按优先级自动排序：

  1. ADB 反向端口转发（Android，最佳）
        adb reverse tcp:8787 tcp:8787
     手机浏览器打开 http://localhost:8787 —— 请求经 USB 原始通道回到 PC，
     不经过任何网络协议栈，不需要开网络共享，也不需要知道 IP。

  2. USB 网络共享（Android / iOS）
     Android 开「USB 网络共享」→ PC 出现 192.168.42.x 接口
     iOS 开「个人热点 · 仅 USB」→ PC 出现 172.20.10.x 接口
     手机浏览器打开 http://<该接口 IP>:8787

  3. 有线以太网 LAN
     PC 用网线接路由器时的地址。手机侧可能仍是无线，
     但至少 PC 一侧的抖动消失了。

  4. Wi-Fi（兜底，零配置）
     两端在同一个无线网里，直接开 PC 的无线网卡地址即可。
     抖动最明显，但不需要任何额外设置。

优先级排序的理由：
  · ADB reverse 把 TCP 整个压在 USB 上，实测 RTT 通常 1~2 ms 且几乎无抖动；
  · 网络共享也走 USB，但多一层 NAT/DHCP，略差于 ADB；
  · 有线以太网比无线稳定，所以排在 Wi-Fi 前面。

链路类型同时决定服务端的推荐分帧长度与客户端缓冲档位：
    adb / tether / lan  → 10 ms 分帧、60 ms 缓冲（有线档）
    wifi                → 20 ms 分帧、200 ms 缓冲（无线档）

命令行
------
    python usblink.py                 # 探测并打印可用链路
    python usblink.py connect --port 8787   # 建立 ADB 反向转发
    python usblink.py disconnect --port 8787
    python usblink.py json            # 以 JSON 输出探测结果
"""

from __future__ import annotations

import argparse
import glob
import json
import os
import re
import shutil
import socket
import subprocess
import sys
from dataclasses import dataclass, field

# --------------------------------------------------------------------------
# 常量
# --------------------------------------------------------------------------

ADB_TIMEOUT = 12

# 隐藏终端运行时，子进程不能再弹出黑框。
# CREATE_NO_WINDOW 只存在于 Windows，非 Windows 平台给 0 即可。
_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0) if os.name == "nt" else 0

# 已知的 USB 网络共享网段
TETHER_SUBNETS = (
    ("192.168.42.", "Android USB 网络共享"),
    ("172.20.10.", "iOS USB 个人热点"),
    ("192.168.137.", "Windows ICS 共享"),
    ("192.168.49.", "Android USB 网络共享"),
)

# 虚拟 / 无关适配器，探测时忽略
IGNORED_ADAPTER_HINTS = (
    "vethernet", "hyper-v", "vmware", "virtualbox", "loopback",
    "bluetooth", "蓝牙", "tap-", "teredo", "isatap",
)

ADB_EXTRA_CANDIDATES = (
    r"%LOCALAPPDATA%\Android\Sdk\platform-tools\adb.exe",
    r"C:\Program Files\Android\platform-tools\adb.exe",
    r"C:\platform-tools\adb.exe",
    r"%USERPROFILE%\scoop\shims\adb.exe",
    r"C:\ProgramData\chocolatey\bin\adb.exe",
)

ADB_GLOB_PATTERNS = (
    r"C:\Program Files\platform-tools*\**\adb.exe",
    r"C:\platform-tools*\**\adb.exe",
    r"D:\Program\platform-tools*\**\adb.exe",
    r"D:\platform-tools*\**\adb.exe",
    r"D:\Program Files\platform-tools*\**\adb.exe",
    r"E:\Program\platform-tools*\**\adb.exe",
)

# 链路优先级：数字越小越优先。同时决定推荐的分帧长度与客户端缓冲档位。
LINK_ORDER = {"adb": 0, "tether": 1, "lan": 2, "wifi": 3}

# 各链路类型对应的推荐参数（有线档 / 无线档）
LINK_PROFILE = {
    "adb":    {"frame_ms": 10, "buffer_ms": 60,  "tier": "wired"},
    "tether": {"frame_ms": 10, "buffer_ms": 60,  "tier": "wired"},
    "lan":    {"frame_ms": 10, "buffer_ms": 60,  "tier": "wired"},
    "wifi":   {"frame_ms": 20, "buffer_ms": 200, "tier": "wireless"},
    "none":   {"frame_ms": 20, "buffer_ms": 200, "tier": "wireless"},
}


def link_profile(kind: str) -> dict:
    """取某链路类型的推荐参数。"""
    return LINK_PROFILE.get(kind, LINK_PROFILE["none"])


# --------------------------------------------------------------------------
# 数据结构
# --------------------------------------------------------------------------

@dataclass
class Link:
    kind: str                 # "adb" | "tether" | "lan"
    url: str                  # 手机应当打开的地址
    label: str                # 人类可读说明
    adapter: str = ""
    ip: str = ""
    serial: str = ""          # adb 设备序列号
    model: str = ""
    note: str = ""


@dataclass
class Report:
    adb_path: str = ""
    adb_versions: str = ""
    devices: list[dict] = field(default_factory=list)
    adapters: list[dict] = field(default_factory=list)
    links: list[Link] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    @property
    def ordered(self) -> list["Link"]:
        """按优先级排好序的链路列表。"""
        return sorted(self.links, key=lambda l: LINK_ORDER.get(l.kind, 9))

    @property
    def best(self) -> Link | None:
        return self.ordered[0] if self.links else None


# --------------------------------------------------------------------------
# adb 定位与调用
# --------------------------------------------------------------------------

def find_adb() -> str:
    """在 PATH 与常见安装位置中寻找 adb。"""
    found = shutil.which("adb") or shutil.which("adb.exe")
    if found:
        return found

    for cand in ADB_EXTRA_CANDIDATES:
        path = os.path.expandvars(cand)
        if os.path.exists(path):
            return path

    for pattern in ADB_GLOB_PATTERNS:
        hits = glob.glob(pattern, recursive=True)
        if hits:
            return hits[0]

    return ""


def _run(cmd: list[str], timeout: int = ADB_TIMEOUT) -> tuple[int, str, str]:
    try:
        proc = subprocess.run(cmd, capture_output=True, timeout=timeout,
                              creationflags=_NO_WINDOW)
    except FileNotFoundError:
        return 127, "", "找不到可执行文件"
    except subprocess.TimeoutExpired:
        return 124, "", f"超时（{timeout}s）"
    out = proc.stdout.decode("utf-8", "replace")
    err = proc.stderr.decode("utf-8", "replace")
    if not out and not err:
        # 少数 Windows 环境下需要 GBK
        out = proc.stdout.decode("gbk", "replace")
        err = proc.stderr.decode("gbk", "replace")
    return proc.returncode, out, err


def adb_version(adb: str) -> str:
    code, out, err = _run([adb, "version"])
    if code != 0:
        return ""
    for line in out.splitlines():
        if line.startswith("Android Debug Bridge version"):
            return line.split("version", 1)[-1].strip()
    return ""


def adb_devices(adb: str) -> list[dict]:
    """列出已连接的 adb 设备。"""
    code, out, err = _run([adb, "devices", "-l"])
    if code != 0:
        return []

    devices = []
    for line in out.splitlines():
        line = line.strip()
        if not line or line.startswith("List of devices"):
            continue
        parts = line.split()
        if len(parts) < 2:
            continue
        serial, state = parts[0], parts[1]
        info = {}
        for token in parts[2:]:
            if ":" in token:
                k, v = token.split(":", 1)
                info[k] = v
        devices.append({
            "serial": serial,
            "state": state,
            "model": info.get("model", "").replace("_", " "),
            "product": info.get("product", ""),
            "transport": info.get("transport_id", ""),
        })
    return devices


def adb_reverse_list(adb: str, serial: str = "") -> list[str]:
    cmd = [adb]
    if serial:
        cmd += ["-s", serial]
    cmd += ["reverse", "--list"]
    code, out, err = _run(cmd)
    if code != 0:
        return []
    return [l.strip() for l in out.splitlines() if l.strip()]


def adb_reverse_add(adb: str, port: int, serial: str = "") -> tuple[bool, str]:
    """建立 tcp:port -> tcp:port 的反向转发。"""
    cmd = [adb]
    if serial:
        cmd += ["-s", serial]
    cmd += ["reverse", f"tcp:{port}", f"tcp:{port}"]
    code, out, err = _run(cmd)
    if code == 0:
        return True, out.strip() or f"tcp:{port} -> tcp:{port} 已建立"
    return False, (err or out).strip() or f"adb 返回码 {code}"


def adb_reverse_remove(adb: str, port: int, serial: str = "") -> tuple[bool, str]:
    cmd = [adb]
    if serial:
        cmd += ["-s", serial]
    cmd += ["reverse", "--remove", f"tcp:{port}"]
    code, out, err = _run(cmd)
    if code == 0:
        return True, out.strip() or f"tcp:{port} 已移除"
    return False, (err or out).strip() or f"adb 返回码 {code}"


# --------------------------------------------------------------------------
# 网络接口探测
# --------------------------------------------------------------------------

def _short_name(full: str) -> str:
    """把「以太网适配器 以太网 2」压缩成「以太网 2」。"""
    name = re.sub(r"^\S*适配器\s*", "", full)
    name = re.sub(r"^(?:Ethernet|Wireless LAN|Bluetooth|Tunnel)\s+adapter\s+",
                  "", name, flags=re.IGNORECASE)
    return name.strip() or full


def _parse_ipconfig(text: str) -> list[dict]:
    adapters: list[dict] = []
    cur: dict | None = None

    for raw in text.splitlines():
        line = raw.strip()
        if not line:
            continue
        is_header = line.endswith(":") and (
            "适配器" in line or "adapter" in line.lower()
        )
        if is_header:
            cur = {"name": line.rstrip(":").strip(), "ips": []}
            adapters.append(cur)
            continue
        if cur is not None and "IPv4" in line:
            ip = line.rsplit(":", 1)[-1].strip()
            if re.fullmatch(r"\d{1,3}(?:\.\d{1,3}){3}", ip):
                cur["ips"].append(ip)
    return adapters


def list_adapters() -> list[dict]:
    """用 ipconfig 拿到「适配器名 + IPv4」，比 getaddrinfo 的信息量大得多。"""
    try:
        proc = subprocess.run(["ipconfig"], capture_output=True, timeout=15,
                              creationflags=_NO_WINDOW)
    except Exception:
        return []

    raw = proc.stdout
    text = ""
    for enc in ("gbk", "utf-8", "cp936"):
        try:
            text = raw.decode(enc)
            break
        except UnicodeDecodeError:
            continue
    if not text:
        text = raw.decode("utf-8", "replace")

    adapters = _parse_ipconfig(text)
    for a in adapters:
        a["short"] = _short_name(a["name"])
        low = a["name"].lower()
        a["ignored"] = any(h in low for h in IGNORED_ADAPTER_HINTS)
        a["kind"], a["kind_label"] = _classify_adapter(a)
    return adapters


def _classify_adapter(adapter: dict) -> tuple[str, str]:
    name = adapter["name"]
    low = name.lower()

    for ip in adapter.get("ips", []):
        for prefix, label in TETHER_SUBNETS:
            if ip.startswith(prefix):
                return "tether", label

    if adapter.get("ignored"):
        return "virtual", "虚拟/无关适配器"
    if "wlan" in low or "无线" in name or "wi-fi" in low or "wifi" in low:
        return "wifi", "无线网卡"
    if "以太网" in name or "ethernet" in low:
        return "ethernet", "以太网"
    return "other", "其它"


def detect_report(port: int = 0) -> Report:
    """探测当前可用的有线链路。"""
    rep = Report()
    rep.adb_path = find_adb()

    if rep.adb_path:
        rep.adb_versions = adb_version(rep.adb_path)
        rep.devices = adb_devices(rep.adb_path)
        if not rep.devices:
            rep.warnings.append(
                "adb 已就绪，但没有检测到已授权设备。"
                "请在手机上开启「USB 调试」，插线后确认授权弹窗。"
            )
    else:
        rep.warnings.append(
            "未找到 adb。若要用 Android 的纯 USB 反向转发，请安装 platform-tools；"
            "否则可改用手机端的「USB 网络共享」。"
        )

    rep.adapters = list_adapters()

    # --- 1) ADB 反向转发 ---
    for dev in rep.devices:
        if dev["state"] != "device":
            rep.warnings.append(
                f"adb 设备 {dev['serial']} 状态为 {dev['state']}，"
                f"通常表示手机端还没点「允许 USB 调试」。"
            )
            continue
        url = f"http://localhost:{port}" if port else "http://localhost:<port>"
        rep.links.append(Link(
            kind="adb",
            url=url,
            label="ADB 反向端口转发（纯 USB，最佳）",
            serial=dev["serial"],
            model=dev["model"],
            note=f"运行 usblink.py connect 后，手机浏览器打开 {url}",
        ))

    # --- 2) USB 网络共享 ---
    for a in rep.adapters:
        if a.get("kind") != "tether":
            continue
        for ip in a["ips"]:
            url = f"http://{ip}:{port}" if port else f"http://{ip}:<port>"
            rep.links.append(Link(
                kind="tether",
                url=url,
                label=f"USB 网络共享（{a['kind_label']}）",
                adapter=a["short"],
                ip=ip,
                note="手机浏览器直接打开该地址",
            ))

    # --- 3) 有线以太网 / 4) Wi-Fi 兜底 ---
    for a in rep.adapters:
        if a.get("ignored") or a.get("kind") in ("tether", "virtual"):
            continue
        kind = a.get("kind")
        if kind not in ("ethernet", "wifi", "other"):
            continue
        # 无线单独成类：优先级低于有线，且服务端会据此选无线档参数
        link_kind = "wifi" if kind == "wifi" else "lan"
        for ip in a["ips"]:
            if ip.startswith("169.254.") or ip.startswith("127."):
                continue
            url = f"http://{ip}:{port}" if port else f"http://{ip}:<port>"
            rep.links.append(Link(
                kind=link_kind,
                url=url,
                label=f"{a['kind_label']}（{a['short']}）",
                adapter=a["short"],
                ip=ip,
            ))

    return rep


def local_ipv4() -> str:
    """取一个用于对外展示的 IPv4。"""
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        ip = s.getsockname()[0]
        s.close()
        return ip
    except Exception:
        return "127.0.0.1"


# --------------------------------------------------------------------------
# 对外主接口（供 server.py 调用）
# --------------------------------------------------------------------------

def setup(port: int, prefer: str = "auto", log=print) -> Report:
    """按优先级建立链路，返回探测报告。

    prefer: auto | adb | tether | lan | wifi

    目前只有 ADB 反向转发需要「建立」这一动作，其余通路（网络共享、
    以太网、Wi-Fi）只要物理上通了就自动可用，探测到即可。
    """
    rep = detect_report(port)

    if prefer in ("auto", "adb") and rep.adb_path:
        ready = [d for d in rep.devices if d["state"] == "device"]
        if ready:
            dev = ready[0]
            ok, msg = adb_reverse_add(rep.adb_path, port, dev["serial"])
            if ok:
                log(f"[USB] ADB 反向转发已建立：tcp:{port} -> tcp:{port}"
                    f"（设备 {dev['model'] or dev['serial']}）")
            else:
                log(f"[USB] ADB 反向转发失败：{msg}")
                rep.warnings.append(f"ADB 反向转发失败：{msg}")
        elif prefer == "adb":
            log("[USB] 指定了 adb 但没有可用设备。")
    return rep


def teardown(port: int, log=print) -> None:
    adb = find_adb()
    if not adb:
        return
    for dev in adb_devices(adb):
        if dev["state"] == "device":
            ok, msg = adb_reverse_remove(adb, port, dev["serial"])
            if ok:
                log(f"[USB] 已移除反向转发：{dev['serial']}")


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------

def _print_report(rep: Report) -> None:
    print()
    print("=" * 62)
    print("  USB 有线链路探测")
    print("=" * 62)

    if rep.adb_path:
        print(f"  adb        {rep.adb_path}")
        print(f"  版本       {rep.adb_versions or '未知'}")
    else:
        print("  adb        未找到")

    print(f"  adb 设备   {len(rep.devices)} 台")
    for d in rep.devices:
        print(f"             - {d['model'] or d['serial']}  [{d['state']}]")

    print()
    print("  网络适配器：")
    for a in rep.adapters:
        flag = " (忽略)" if a.get("ignored") else ""
        ips = ", ".join(a["ips"]) or "无 IPv4"
        print(f"    - {a['short']:<28} {a['kind_label']:<16} {ips}{flag}")

    print()
    if rep.links:
        print("  可用链路（按优先级）：")
        for i, l in enumerate(rep.ordered, 1):
            print(f"    {i}. [{l.kind}] {l.label}")
            print(f"       手机打开 -> {l.url}")
            if l.note:
                print(f"       {l.note}")
    else:
        print("  未发现可用链路。")

    if rep.warnings:
        print()
        print("  提示：")
        for w in rep.warnings:
            print(f"    ! {w}")
    print("=" * 62)
    print()


def main() -> int:
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass

    p = argparse.ArgumentParser(
        description="USB 有线链路助手：为手机浏览器建立到 PC 的 USB 数据通道",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    p.add_argument("action", nargs="?", default="detect",
                   choices=["detect", "connect", "disconnect", "json"],
                   help="detect=探测 / connect=建立转发 / disconnect=移除 / json=机器可读输出")
    p.add_argument("--port", type=int, default=8787, help="服务端口")
    p.add_argument("--prefer", default="auto", choices=["auto", "adb", "tether", "lan", "wifi"],
                   help="优先使用的链路类型")

    args = p.parse_args()

    if args.action == "json":
        rep = detect_report(args.port)
        data = {
            "adb_path": rep.adb_path,
            "adb_version": rep.adb_versions,
            "devices": rep.devices,
            "adapters": rep.adapters,
            "links": [l.__dict__ for l in rep.ordered],
            "warnings": rep.warnings,
            "best": rep.best.__dict__ if rep.best else None,
            "profile": link_profile(rep.best.kind) if rep.best else link_profile("none"),
        }
        print(json.dumps(data, ensure_ascii=False, indent=2))
        return 0

    if args.action == "detect":
        _print_report(detect_report(args.port))
        return 0

    if args.action == "connect":
        rep = setup(args.port, args.prefer)
        _print_report(rep)
        best = rep.best
        if best:
            print(f"  >>> 手机浏览器打开： {best.url}\n")
        return 0

    if args.action == "disconnect":
        teardown(args.port)
        print("已清理。")
        return 0

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
