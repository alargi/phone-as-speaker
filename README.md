# phone-as-speaker

**把手机 / 平板变成电脑的外置扬声器** —— 主机端带浏览器图形化控制台，链路自动探测、参数自适应。

![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)
![Python](https://img.shields.io/badge/python-3.9%2B-blue.svg)
![Platform](https://img.shields.io/badge/platform-Windows%2010%2B-0078D6.svg)
![Dependencies](https://img.shields.io/badge/dependencies-4-brightgreen.svg)
![Server test](https://img.shields.io/badge/selftest-9%20groups%20%2F%2056%20checks-success.svg)

---

## 这是什么

电脑上正在播放的声音，实时送到手机 / 平板上放出来。
设备端**零安装**——浏览器打开一个网址就能用；主机端是一个**浏览器控制台**，
链路状态、实时码率、每条连接的延迟质量都看得见。

它是 [spacedesk](https://spacedesk.net/) 思路的**音频对偶**：spacedesk 让 Windows 以为自己多了一块屏幕，
本项目让 Windows 正在播放的音频直接流到另一台设备。区别在于——音频有 **WASAPI 回环**这个标准接口，
所以**完全不需要写虚拟音频驱动**，整块内核驱动与签名问题都省掉了。

```
   ┌──────────────┐                        ┌──────────────┐
   │   Windows    │   PCM 16-bit 不压缩    │  手机 / 平板  │
   │  正在播放     │ ─────────────────────► │   浏览器      │
   │  的音频       │   WS 单端口 / TCP      │  抖动缓冲 → 🔊 │
   └──────────────┘                        └──────────────┘
          ▲                                        │
          │              控制台 /console            │ RTT 探针
          └──────────── 状态 · 参数 · 曲线 ─────────┘
```

---

## 界面预览

### 主机端控制台 `/console`

链路状态、实时码率与帧率曲线、每条连接的 RTT 与抖动、参数调节、运行控制，全在这一个页面。

![主机端控制台](screenshot-console.png)

### 设备端播放页 `/`

扫码或在浏览器里打开地址即用。缓冲档位会按当前链路自动推荐（下图是 ADB 有线链路，60 ms 档）。

<img src="screenshot-player.png" width="420" alt="设备端播放页">

---

## 特性

**音频链路**
- WASAPI 回环捕获默认输出设备，**不装虚拟音频驱动**
- PCM 16-bit 不压缩——音频不需要上百倍压缩比，少一层编解码就少一个延迟环节
- 运行时改参数（设备 / 分帧 / 声道 / 采样率），自动重启采集并通知所有播放页重建缓冲

**四条链路，自动择优**

| 优先级 | 链路 | 设备端打开 | 特点 |
|---|---|---|---|
| 1 | ADB 反向转发（Android） | `http://localhost:8787` | 走 USB 原始通道，RTT 通常 1~2 ms |
| 2 | USB 网络共享 | `http://192.168.42.x` / `172.20.10.x` | 也走 USB，多一层 NAT |
| 3 | 有线以太网 | `http://<以太网 IP>:8787` | PC 侧抖动消失 |
| 4 | Wi-Fi | `http://<无线网卡 IP>:8787` | 零配置兜底 |

链路类型还会自动决定推荐参数：**有线档 10 ms 分帧 / 60 ms 缓冲，无线档 20 ms / 200 ms**。

**Web 端**
- 播放页：零安装、零构建、零 CDN；抖动缓冲 + 音量 + 电平表 + RTT 探针 + 自适应缓冲档
- 控制台：运行状态 / 连接信息（含二维码与链路重探测）/ 参数调节 / 运行控制 /
  客户端明细 / 实时曲线 / 运行日志，每秒刷新

**运行与运维**
- 一键脚本：`setup.bat` → `run.bat` → `selftest.bat`
- **无窗口运行**：`run-silent.vbs` 起来后没有终端窗口，输出进 `speaker.log`
- 三条停止路径：控制台「退出程序」按钮、`stop.bat`、`Ctrl+C`
- 端到端自检 **9 组 56 项**，不用浏览器

---

## 快速开始

### 环境要求

- **Windows 10 1607 或更高版本**（WASAPI 回环是 Windows 特性）
- **Python 3.9+**
- 想要最低延迟的话：一台 Android 设备 + `platform-tools`（提供 `adb`）

### Windows：双击即用

| 步骤 | 操作 |
|---|---|
| 1️⃣ 首次使用 | 双击 **`setup.bat`** —— 自动创建本地 `.venv` 并装好全部依赖（只需做一次） |
| 2️⃣ 连接设备 | Android：开「USB 调试」并授权（最低延迟）；或开「USB 网络共享」；或让设备连同一 Wi-Fi |
| 3️⃣ 启动 | 双击 **`run.bat`** —— 自动探测链路，并自动打开控制台 |
| 3️⃣' 启动（无窗口） | 双击 **`run-silent.vbs`** —— 同上，但完全不出现终端窗口 |
| 4️⃣ 播放 | 控制台里扫码，或在设备浏览器里打开提示的地址 |
| 5️⃣ 停止 | 有窗口时按 `Ctrl+C`；无窗口时点控制台的**「退出程序」**按钮，或双击 **`stop.bat`** |
| 6️⃣ 验证（可选） | 双击 **`selftest.bat`** —— 不用浏览器跑完整链路自检 |

> ⚠️ **端口只能被一个程序占用。** 如果旧的服务还在跑，先停掉它（`Ctrl+C` 或 `stop.bat`），
> 否则新服务会绑不上 8787。

给 `run.bat` 加参数即可覆盖自动行为：

```bat
run.bat --link adb               :: 强制走 ADB 反向转发
run.bat --link wifi              :: 强制只走无线
run.bat --link none              :: 跳过链路探测，只听局域网
run.bat --no-console             :: 启动但不自动打开控制台
run.bat --frame-ms 20            :: 手动指定分帧，覆盖自动档位
run.bat --list-devices           :: 看看有哪些音频设备
run.bat --device "扬声器"         :: 指定捕获哪一路输出
```

### 手动安装

```bash
git clone https://github.com/<your-name>/phone-as-speaker.git
cd phone-as-speaker
python -m venv .venv
.venv\Scripts\python.exe -m pip install -r requirements.txt
.venv\Scripts\python.exe server.py --open-console
```

`run.bat` 的解释器查找顺序：项目内 `.venv` → `py` 启动器 → PATH 上的 `python`。
如果所有候选都缺依赖，它会明确告诉你用的是哪个解释器，而不是抛一堆 `ModuleNotFoundError`。

### 无窗口运行

```
双击 run-silent.vbs  →  后台运行，无任何窗口  →  浏览器控制台照常自动打开
```

三件事配合起来实现：

| 组件 | 作用 |
|---|---|
| `run-silent.vbs` | 以**隐藏窗口**方式调用 `run.bat --silent`（`WScript.Shell.Run(cmd, 0, True)`） |
| `run.bat --silent` | 检测到 `--silent` 后改用 **`pythonw.exe`**，于是进程本身也不创建窗口 |
| `speaker.log` | 没有终端 ≠ 没有线索。stdout / stderr 全部追加到这个文件 |

`stop.bat` 的设计要点：

- 进程号来自 `server.py` 启动时写下的 **`.speaker.pid`**（第一行 pid、第二行端口），而不是靠「按进程名杀」
- 杀之前先用 `tasklist` 确认该 pid **现在确实是 Python 进程**——pid 会被系统回收，不查一下就可能误杀无关程序
- 先走 `GET /api/quit`（**仅允许本机调用**）请求优雅退出，最多等 5 秒，超时才 `taskkill /f`

> 不小心关掉浏览器标签也没关系：控制台固定在 `http://127.0.0.1:8787/console`。

---

## 目录结构

```
phone-as-speaker/
├── server.py                 # 主机端：音频捕获、重采样、分帧、WebSocket 广播、控制台与 API、链路调度
├── usblink.py                # 链路助手：adb 反向转发、ipconfig 解析、四类链路优先级与推荐参数
├── selftest.py               # 端到端自检（9 组 56 项），不用浏览器
├── static/
│   ├── index.html            # 播放页（给手机 / 平板）：Web Audio + 抖动缓冲 + RTT 探针
│   └── console.html          # 主机端图形化控制台
├── docs/
│   ├── architecture.md       # 数据通路、抖动缓冲、链路择优、与 spacedesk 的原理对应、实现札记
│   ├── api.md                # HTTP API、WebSocket 协议、命令行选项
│   ├── troubleshooting.md    # 排查手册、已知限制、与 spacedesk 的 USB 冲突
│   ├── spacedesk-deep-dive.html  # spacedesk 全链技术原理分析（含事实可信度分级）
│   └── screenshot-*.png      # 上面那两张界面截图
├── run.bat                   # 一键启动（自动选解释器、自动开控制台）
├── run-silent.vbs            # 无窗口启动器
├── stop.bat                  # 停止正在运行的实例
├── setup.bat                 # 一次性安装依赖到本地 .venv
├── selftest.bat              # 自检入口
├── requirements.txt          # 依赖清单（numpy / soundcard / aiohttp / qrcode）
├── CHANGELOG.md
├── CONTRIBUTING.md
├── LICENSE                   # MIT
└── .gitignore / .gitattributes
```

---

## 自检

```bash
python selftest.py
```

会自动拉起一个临时服务（默认端口 8799），覆盖 **9 组 56 项**：

| 组 | 内容 |
|---|---|
| 1 | 服务与采集 |
| 2 | 播放页与控制台（页面存在性、标题、各功能区块） |
| 3 | 链路探测与优先级排序 |
| 4 | 只读 API（状态字段、设备列表、二维码） |
| 5 | WebSocket 音频流（帧长 / 帧率 / 码率 / RTT 探针） |
| 6 | 参数变更与采集重启（含非法参数拒绝） |
| 7 | 运行控制（stop / start / 未知动作 400） |
| 8 | 多客户端并发 |
| 9 | 断开清理 |

一次通过的结尾长这样：

```
...
8. 多客户端并发
  [PASS] 第二路客户端可接入
  [PASS] 服务端统计到 2 路客户端  clients=2
  [PASS] 客户端列表含基本信息
9. 断开清理
  [PASS] 客户端断开后计数归零  clients=0

结果：全部通过 ✅
```

---

## 文档

| 文档 | 内容 |
|---|---|
| [架构与原理](docs/architecture.md) | 数据通路、抖动缓冲为什么是关键、RTT 探针、链路择优、与 spacedesk 逐项对照、**两个踩过的真 bug** |
| [HTTP API 与命令行](docs/api.md) | 全部端点、WebSocket 协议、`/api/status` 字段表、命令行选项、`usblink.py` 独立用法 |
| [排查手册](docs/troubleshooting.md) | 一分钟定位流程、现象对照表、已知限制、**与 spacedesk 的 USB 直连冲突** |
| [spacedesk 全链技术原理分析](docs/spacedesk-deep-dive.html) | 驱动 / 帧交付 / 压缩 / 网络 / 输入回传 / 时延预算逐段拆解，附事实可信度分级 |

---

## 常见问题

**设备连上了，但没有声音？**
先看控制台的客户端表格：`客户端 0` 说明没有任何设备连上来，问题在链路而不在音频。
详见[排查手册](docs/troubleshooting.md#一分钟定位先看这几项)。

**电脑自己的扬声器还在响，房间里有回声？**
回环捕获的是播放流，电脑扬声器不会自动静音。把电脑音量拉到 0，或把默认播放设备切到不出声的输出。

**`adb devices` 是空的？**
数据线可能不支持数据传输、设备没开 USB 调试，或者 spacedesk 的 USB 直连驱动抢占了数据线。
详见[与 spacedesk 的 USB 直连冲突](docs/troubleshooting.md#与-spacedesk-的-usb-直连冲突)。

**能不能用在 macOS / Linux 上？**
主机端目前只支持 Windows。macOS 需改用 BlackHole 之类的虚拟音频设备，Linux 需改用
PulseAudio monitor 源——只需替换 `_resolve_loopback()` 与捕获段落，欢迎 PR。

---

## 贡献

欢迎提交 issue 与 PR，动手前请先读 [CONTRIBUTING.md](CONTRIBUTING.md)。
提交前请确保 `selftest.bat` 的 56 项保持全绿。

## 许可证

[MIT](LICENSE) © 2026 The phone-as-speaker authors

本项目与 spacedesk（datronicsoft）无任何隶属或背书关系，提及仅为架构对照与冲突排查的说明。
