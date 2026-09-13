#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
端到端自检：不起浏览器也能确认整条链路与控制台 API 都是通的。

依次验证：
  1. 服务与采集
  2. 播放页与控制台页面
  3. 链路探测
  4. 只读 API（状态 / 设备 / 二维码）
  5. WebSocket 音频流（帧长 / 帧率 / 码率 / RTT 探针）
  6. 参数变更（改分帧会重启采集并下发新配置）
  7. 运行控制（停止 / 启动）
  8. 多客户端并发与断开清理
"""
from __future__ import annotations

import asyncio
import json
import subprocess
import sys
import time
from pathlib import Path

import aiohttp

HERE = Path(__file__).resolve().parent
PORT = 8799
BASE = f"http://127.0.0.1:{PORT}"


def out(*a) -> None:
    print(*a, flush=True)


async def wait_ready(session, timeout=30.0) -> dict:
    t0 = time.time()
    last = None
    while time.time() - t0 < timeout:
        try:
            async with session.get(f"{BASE}/api/status") as r:
                if r.status == 200:
                    return json.loads(await r.text())
        except Exception as exc:                      # noqa: BLE001
            last = exc
        await asyncio.sleep(0.4)
    raise RuntimeError(f"服务未在 {timeout:.0f}s 内就绪：{last}")


async def wait_capture(session, timeout=10.0) -> dict:
    """等采集真正跑起来（设备名被填上），避免刚启动就断言。"""
    st = {}
    t0 = time.time()
    while time.time() - t0 < timeout:
        async with session.get(f"{BASE}/api/status") as r:
            st = json.loads(await r.text())
        cap = st.get("capture", {})
        if cap.get("device") and cap["device"] != "-":
            return st
        await asyncio.sleep(0.3)
    return st


async def collect_frames(session, seconds=3.0, cap=150):
    """连一路 WS，返回 (配置帧, 帧长列表, 耗时)。

    收流超时不再抛出，而是提前收工——让上层的 check 去判定失败，
    免得一个用例把整轮自检打崩。
    """
    sizes = []
    cfg = None
    async with session.ws_connect(f"{BASE}/ws") as ws:
        t0 = time.time()
        while time.time() - t0 < seconds and len(sizes) < cap:
            try:
                msg = await asyncio.wait_for(ws.receive(), timeout=6)
            except asyncio.TimeoutError:
                break
            if msg.type == aiohttp.WSMsgType.TEXT:
                cfg = json.loads(msg.data)
            elif msg.type == aiohttp.WSMsgType.BINARY:
                sizes.append(len(msg.data))
            elif msg.type in (aiohttp.WSMsgType.CLOSED, aiohttp.WSMsgType.ERROR):
                break
        elapsed = max(time.time() - t0, 0.001)
    return cfg, sizes, elapsed


async def run_checks() -> int:
    failures = []

    def check(name: str, ok: bool, detail: str = "") -> None:
        out(f"  [{'PASS' if ok else 'FAIL'}] {name}" + (f"  {detail}" if detail else ""))
        if not ok:
            failures.append(name)

    proc = subprocess.Popen(
        [sys.executable, str(HERE / "server.py"), "--port", str(PORT), "--host", "127.0.0.1"],
        cwd=str(HERE),
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
    )
    try:
        timeout = aiohttp.ClientTimeout(total=45)
        async with aiohttp.ClientSession(timeout=timeout) as session:
            out("\n1. 服务与采集")
            await wait_ready(session)
            st = await wait_capture(session)
            check("HTTP 服务就绪", True)
            cap = st["capture"]
            check("音频采集无错误、正在运行",
                  cap["error"] is None and cap["running"],
                  f"device={cap['device']}  rate={cap['capture_rate']}Hz")

            out("\n2. 播放页与控制台")
            async with session.get(f"{BASE}/") as r:
                player = await r.text()
            check("播放页 GET / 返回 200", r.status == 200, f"{len(player)} 字节")
            check("播放页标题为「外置扬声器」", "外置扬声器" in player)
            check("播放页含自适应缓冲档位", "TIERS" in player and "renderTiers" in player)
            check("播放页含抖动缓冲与 RTT 探针",
                  "nextTime" in player and '"ping"' in player and '"pong"' in player)

            async with session.get(f"{BASE}/console") as r:
                console = await r.text()
            check("控制台 GET /console 返回 200", r.status == 200, f"{len(console)} 字节")
            check("控制台含标题", "控制台" in console)
            for block in ("运行状态", "连接信息", "参数调节", "运行控制",
                          "实时曲线", "运行日志"):
                check(f"控制台含「{block}」区块", block in console)

            out("\n3. 链路探测")
            try:
                sys.path.insert(0, str(HERE))
                import usblink
                rep = usblink.detect_report(PORT)
                check("usblink 可导入并完成探测", True,
                      f"adb={'有' if rep.adb_path else '无'} 设备={len(rep.devices)} "
                      f"链路={len(rep.links)}")
                check("探测到至少一条可用链路", len(rep.links) > 0,
                      (rep.best.label if rep.best else "无"))
                order = [usblink.LINK_ORDER.get(l.kind, 9) for l in rep.ordered]
                check("链路按优先级排序", order == sorted(order),
                      " < ".join(l.kind for l in rep.ordered) or "无")
                prof = usblink.link_profile(rep.best.kind if rep.best else "none")
                expect_tier = "wireless" if (rep.best and rep.best.kind == "wifi") else "wired"
                check("推荐档位与链路类型匹配", prof["tier"] == expect_tier,
                      f"{rep.best.kind if rep.best else 'none'} -> {prof['tier']}")
            except Exception as exc:                   # noqa: BLE001
                check("usblink 可导入并完成探测", False, f"{type(exc).__name__}: {exc}")

            link = st["link"]
            check("状态接口带 link.kind 与 profile",
                  "kind" in link and "profile" in link,
                  f"kind={link['kind']} profile={link['profile'].get('tier')}")

            out("\n4. 只读 API")
            for key in ("ok", "capture", "config", "totals", "clients", "history", "link", "log"):
                check(f"/api/status 含字段 {key}", key in st)

            async with session.get(f"{BASE}/api/devices") as r:
                dev = await r.json()
            check("GET /api/devices 返回回环设备列表",
                  r.status == 200 and isinstance(dev.get("loopback"), list)
                  and len(dev["loopback"]) > 0,
                  f"{len(dev.get('loopback', []))} 个回环设备")

            async with session.get(f"{BASE}/api/qr.svg") as r:
                qr = await r.text()
            check("GET /api/qr.svg 返回 SVG 二维码",
                  r.status == 200 and "image/svg" in r.headers.get("Content-Type", "")
                  and "<svg" in qr,
                  f"{len(qr)} 字节")

            out("\n5. WebSocket 音频流")
            rate = st["config"]["rate"]
            ch = st["config"]["channels"]
            fms = st["config"]["frame_ms"]
            expect = int(rate * fms / 1000) * ch * 2
            out(f"       期望帧长 = {rate}Hz × {fms}ms × {ch}ch × 2B = {expect} 字节")

            cfg, sizes, elapsed = await collect_frames(session)
            check("收到配置帧", cfg is not None and cfg.get("type") == "config")
            check("配置帧携带链路类型与档位",
                  bool(cfg) and "link" in cfg and "profile" in cfg,
                  f"link={cfg.get('link') if cfg else None}")
            check("收到音频帧", len(sizes) > 20, f"{len(sizes)} 帧 / {elapsed:.2f}s")
            check("帧长全部正确", bool(sizes) and set(sizes) == {expect},
                  f"取值 {sorted(set(sizes))[:4]}")
            if sizes:
                fps = len(sizes) / elapsed
                kbps = sum(sizes) * 8 / elapsed / 1000
                check("帧率接近预期", abs(fps - 1000 / fms) < 10,
                      f"实测 {fps:.1f} fps（期望 {1000/fms:.0f}）")
                check("码率接近理论值", abs(kbps - rate * ch * 16 / 1000) < 350,
                      f"实测 {kbps:.0f} kbps（理论 {rate * ch * 16 / 1000:.0f}）")

            async with session.ws_connect(f"{BASE}/ws") as ws:
                await ws.receive()                    # 吞掉 config 帧
                await ws.send_str(json.dumps({"type": "ping", "t": 12345}))
                pong = None
                t1 = time.time()
                while time.time() - t1 < 4:
                    m2 = await asyncio.wait_for(ws.receive(), timeout=6)
                    if m2.type == aiohttp.WSMsgType.TEXT:
                        cand = json.loads(m2.data)
                        if cand.get("type") == "pong":
                            pong = cand
                            break
                check("RTT 探针返回 pong 且时间戳原样回带",
                      bool(pong) and pong.get("t") == 12345, f"pong={pong}")
                await ws.send_str(json.dumps(
                    {"type": "rtt", "ms": 1.4, "jitter": 0.3, "buffer_ms": 60}))

                out("\n6. 参数变更")
                async with session.post(f"{BASE}/api/config",
                                        json={"frame_ms": 20}) as r:
                    j = await r.json()
                new_expect = int(rate * 20 / 1000) * ch * 2
                check("POST /api/config 接受新分帧",
                      r.status == 200 and j["config"]["frame_ms"] == 20,
                      f"frame_ms={j['config']['frame_ms']}")
                check("单帧字节随分帧同步变化",
                      j["config"]["frame_bytes"] == new_expect,
                      f"{j['config']['frame_bytes']}（期望 {new_expect}）")
                check("参数变更后采集已重启",
                      j["capture"]["running"] and j["config"]["rev"] >= 1,
                      f"rev={j['config']['rev']}")

                await asyncio.sleep(0.5)
                _c2, sizes2, _e2 = await collect_frames(session, seconds=2.0, cap=60)
                check("重启后仍能收到新帧长的音频",
                      bool(sizes2) and set(sizes2) == {new_expect},
                      f"取值 {sorted(set(sizes2))[:4]}")

                async with session.post(f"{BASE}/api/config", json={"frame_ms": fms}) as r:
                    await r.json()
                check("分帧可还原", r.status == 200)

                async with session.post(f"{BASE}/api/config",
                                        json={"channels": 3}) as r:
                    j2 = await r.json()
                check("非法参数被拒绝且不生效",
                      j2["config"]["channels"] in (1, 2) and bool(j2["rejected"]),
                      f"rejected={j2.get('rejected')}")

                out("\n7. 运行控制")
                async with session.post(f"{BASE}/api/control",
                                        json={"action": "stop"}) as r:
                    j3 = await r.json()
                check("stop 后采集停止", j3["capture"]["running"] is False)
                async with session.post(f"{BASE}/api/control",
                                        json={"action": "start"}) as r:
                    await r.json()
                await asyncio.sleep(0.4)
                async with session.get(f"{BASE}/api/status") as r:
                    st2 = await r.json()
                check("start 后采集恢复", st2["capture"]["running"] is True)
                async with session.post(f"{BASE}/api/control",
                                        json={"action": "bogus"}) as r:
                    check("未知动作返回 400", r.status == 400)

                out("\n8. 多客户端并发")
                async with session.ws_connect(f"{BASE}/ws") as ws2:
                    m = await asyncio.wait_for(ws2.receive(), timeout=8)
                    check("第二路客户端可接入", m.type == aiohttp.WSMsgType.TEXT)
                    async with session.get(f"{BASE}/api/status") as r:
                        h2 = await r.json()
                    check("服务端统计到 2 路客户端", h2["totals"]["clients"] == 2,
                          f"clients={h2['totals']['clients']}")
                    check("客户端列表含基本信息",
                          all("ua" in c and "sent_frames" in c for c in h2["clients"]))

        out("\n9. 断开清理")
        await asyncio.sleep(0.8)
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=5)) as s:
            async with s.get(f"{BASE}/api/status") as r:
                h3 = await r.json()
        check("客户端断开后计数归零", h3["totals"]["clients"] == 0,
              f"clients={h3['totals']['clients']}")

    finally:
        proc.terminate()
        try:
            raw = proc.communicate(timeout=8)[0]
        except Exception:                             # noqa: BLE001
            proc.kill()
            raw = b""
        text = raw.decode("utf-8", "replace")
        out("\n---- 服务端日志（节选） ----")
        shown = 0
        for line in text.splitlines():
            if line.strip() and "█" not in line and "▀" not in line and "▄" not in line:
                out("  " + line)
                shown += 1
                if shown >= 30:
                    break
        # 服务端被 terminate 时可能来不及清理 ADB 反向转发，这里补一次
        try:
            sys.path.insert(0, str(HERE))
            import usblink
            usblink.teardown(PORT)
        except Exception:                             # noqa: BLE001
            pass

    out("")
    if failures:
        out(f"结果：{len(failures)} 项未通过 -> {', '.join(failures)}")
        return 1
    out("结果：全部通过 ✅")
    return 0


if __name__ == "__main__":
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:                                 # noqa: BLE001
        pass
    raise SystemExit(asyncio.run(run_checks()))
