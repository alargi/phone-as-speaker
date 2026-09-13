# 架构与原理

[← 返回 README](../README.md)

---

## 目录

- [一、数据通路](#一数据通路)
- [二、抖动缓冲：客户端的关键](#二抖动缓冲客户端的关键)
- [三、RTT 探针](#三rtt-探针)
- [四、四条链路，自动择优](#四四条链路自动择优)
- [五、链路类型决定推荐参数](#五链路类型决定推荐参数)
- [六、与 spacedesk 的原理对应](#六与-spacedesk-的原理对应)
- [七、实现札记（两个坑，都踩过）](#七实现札记两个坑都踩过)

---

## 一、数据通路

```
Windows 播放流
     │
     ▼
WASAPI 回环捕获（soundcard，默认输出设备）
     │  float32 [-1,1]
     ▼
声道归一 + int16 量化
     │
     ├─（可选）滑动平均抗混叠 + 线性插值重采样
     ▼
定长分帧（Framer；有线 10ms / 无线 20ms）
     │
     ▼
asyncio 队列 ──► 每客户端独立发送队列 ──► WebSocket 广播
     ▲                                        │  ▲
     │                                        │  └── {"type":"pong","t":...}
     │                                        ▼       （RTT 探针回程）
     │                            手机浏览器 Web Audio API
     │                            抖动缓冲 → AudioBufferSourceNode 定时调度
     │                            → GainNode（音量）→ AnalyserNode（电平表）
     │                            → 扬声器
     │
  控制台 /console ──► /api/status（每秒轮询）
                     /api/config、/api/control（写操作 → 重启采集 → 通知客户端）
```

关键取舍：**不做压缩**。PCM 16-bit 立体声 48 kHz 约 1.5 Mbps，对 ADB / USB 网络共享 / 千兆以太网 /
甚至 300 Mbps 的 Wi-Fi 都不构成压力；而音频不像视频需要上百倍压缩比，加一层编解码只会白送一个延迟环节
和一处可能的音质损失。

---

## 二、抖动缓冲：客户端的关键

WebSocket 只保证「按序到达」，不保证「按节奏到达」。客户端因此**不采用「收到就播」**，
而是维护一个 `nextTime` 播放时刻表：

```js
nextTime = Math.max(nextTime, ctx.currentTime + 起始提前量);
source.start(nextTime);
nextTime += 帧长;
```

每收到一帧就排到 `nextTime` 之后，把网络抖动**吸收成一段固定延迟**。
这段固定延迟就是「缓冲深度」——它是本项目唯一真正可调的延迟旋钮：

- 有线链路（ADB / USB 网络共享 / 以太网）抖动极小，可以压到 **60 ms**；
- Wi-Fi 的抖动大得多，必须给到 **200 ms**，否则会听到断续。

播放页提供三档缓冲按钮，并会**按下发的链路档位自动切换**：
有线档 40 / 60 / 120 ms，无线档 120 / 200 / 350 ms。

> 一个易踩的细节：`maxLead`（判定位积压的上限）必须随目标缓冲动态计算
> （`Math.max(0.3, target * 3)`）。若写成固定值，选到 350 ms 厚缓冲档时会被误判成「积压过多」
> 而反复重排，声音就碎了。

---

## 三、RTT 探针

客户端每 2 秒发一次：

```json
{"type": "ping", "t": <performance.now()>}
```

服务端原样回带时间戳：

```json
{"type": "pong", "t": <同一个值>}
```

客户端算出 RTT 均值与标准差（即抖动），再把结果回报给服务端：

```json
{"type": "rtt", "ms": 1.8, "jitter": 0.4, "buffer_ms": 60}
```

所以控制台的客户端表格里能直接看到**每条链路的实时质量**，而不用去猜。
这条上行通道只有几十字节，是纯下行场景里唯一的上行流量。

---

## 四、四条链路，自动择优

`usblink.py` 启动时探测本机所有可用通路，按优先级排序：

| 优先级 | 链路 | 手机 / 平板打开 | 特点 |
|---|---|---|---|
| 1 | **ADB 反向转发**（Android） | `http://localhost:8787` | 请求走 USB 原始通道，不经网络协议栈；RTT 通常 1~2 ms，延迟最低 |
| 2 | **USB 网络共享** | `http://192.168.42.x:8787`（Android）<br>`http://172.20.10.x:8787`（iOS） | 也走 USB，但多一层 NAT / DHCP |
| 3 | **有线以太网** | `http://<以太网 IP>:8787` | PC 一侧抖动消失 |
| 4 | **Wi-Fi** | `http://<无线网卡 IP>:8787` | 零配置兜底，抖动最明显 |

排序逻辑在 `usblink.LINK_ORDER`：

```python
LINK_ORDER = {"adb": 0, "tether": 1, "lan": 2, "wifi": 3}
```

> **注意：插上数据线 ≠ 有网。**
> USB 线本身只承载 MTP 这类 USB 协议，不承载 TCP/IP。
> 要走链路 1 或 2，必须在设备上分别开启「USB 调试」（并经 adb 授权）或「USB 网络共享」；
> 否则只会有链路 3 / 4 可用。

---

## 五、链路类型决定推荐参数

```
有线档（adb / tether / lan） → 10 ms 分帧、60 ms 客户端缓冲
无线档（wifi）              → 20 ms 分帧、200 ms 客户端缓冲
```

- 服务端启动时若发现是无线链路，会自动把分帧从 10 ms 调成 20 ms（可用 `--frame-ms` 覆盖）。
- 播放页收到链路类型后，会**自动切换缓冲档位按钮**。
- Wi-Fi 的瓶颈从来不是带宽（1.5 Mbps 对 300+ Mbps 毫无压力）而是**抖动**；
  抖动迫使客户端加厚缓冲，而缓冲厚度直接等于延迟。所以链路一变，这一整套参数都跟着变。

---

## 六、与 spacedesk 的原理对应

spacedesk 把「电脑扩展出一块屏幕」这件事做到了产品级，本项目做的是它的**音频对偶**。
两者的架构对照如下：

| spacedesk 的做法 | 本项目的做法 | 为什么这样换 |
|---|---|---|
| 写一个 IddCx 虚拟显示适配器（UMDF），让 Windows 以为插了新显示器 | **WASAPI 回环捕获**默认输出设备 | Windows 没有「回环截屏」的标准接口，所以 spacedesk 必须造虚拟显示器；但音频有 WASAPI Loopback，直接就能拿到正在播放的内容，**整块内核驱动与签名问题都省掉了** |
| DWM 把桌面合成后交付给驱动 | 音频引擎把混音后的数据交给回环客户端 | 同一个道理：在数据产生处取数，不做二次搬运 |
| 帧内压缩 + 色度子采样 | **PCM 线性量化（不压缩）**，可选降采样 / 降声道 | 1.5 Mbps 对任何链路都不构成压力；音频不需要上百倍压缩比，加编解码只会平白多一个延迟环节 |
| TCP/UDP 28252，自研分片协议 | **单端口 HTTP + WebSocket（TCP）** | TCP 自带按序到达与重传，省掉一整层私有协议；弱网下的队头阻塞由客户端抖动缓冲吸收 |
| HTML5 Viewer：浏览器打开即用，零安装 | **手机浏览器打开即用，零安装** | 不用为 Android 和 iOS 各写一个原生播放器 |
| 官方推荐 USB 直连 / 网络共享 | **ADB 反向转发 / USB 网络共享** | 思路一致，都是让数据线承担传输，绕开无线链路 |
| **Driver Console**（图形化，含链路开关与诊断） | **浏览器控制台 `/console`** | 对应关系最直接的一处：同样是「主机端图形化控制 + 链路状态可视化」 |
| 虚拟 HID 把触摸回传成鼠标 | 只有 RTT 探针这一条极小上行 | 扬声器是纯下行场景 |

对 spacedesk 全链（驱动、帧交付、压缩、网络、输入回传、时延预算）的逐段拆解与可信度分级，
见 [spacedesk 全链技术原理分析](spacedesk-deep-dive.html)。

---

## 七、实现札记（两个坑，都踩过）

这两个 bug 都由 `selftest.py` 抓出，且都属于「状态页看起来完全正常、功能却静默失效」的类型——
所以它们被特意留在这里，改造代码时请勿删掉对应修复。

### 1. `soundcard` 的 COM 是按线程初始化的

`soundcard` 只在**模块首次导入**时调用一次 `CoInitializeEx`（其 `_COMLibrary.__init__`）。
所以第一个用到它的线程没问题，但之后任何**新建线程**再用 WASAPI 都会撞
`0x800401f0`（`CO_E_NOTINITIALIZED`）——而「改参数 / 重启采集」正是重建采集线程。

修法是每个要用 WASAPI 的线程自己补一次 `CoInitializeEx`（见 `ensure_com()`）。

**反直觉的地方：不能抢在 `soundcard` 前面初始化。**
它用 `check_error(CoInitializeEx(...))` 判断结果，而 `check_error` 把任何非 `S_OK` 都当错误——
**包括 `S_FALSE`**。如果线程已经初始化过，`soundcard` 拿到 `S_FALSE` 就会抛出莫名其妙的
`0x100000001`。所以 `ensure_com()` 只在音频后端**已经导入之后**才动手：

```python
def ensure_com() -> None:
    if sys.platform != "win32":
        return
    # 关键：必须等 soundcard 的 COM 单例先建好，否则会触发 S_FALSE 误判
    if "soundcard.mediafoundation" not in sys.modules:
        return
    ctypes.windll.ole32.CoInitializeEx(None, 0x0)
```

并且启动时统一 `preload_audio_backend()` 把导入时机固定下来。

### 2. 重启采集时不能新建 `audio_q`

`broadcast()` 协程正在 `await self.audio_q.get()`，它已经绑定在那个具体的队列对象上。
一旦 `start_capture()` 把 `self.audio_q` 换成新队列，消费端就永远挂在旧队列上——
采集线程照常写入，但**再也发不出任何一帧**，客户端静默无声而状态页看起来一切正常。

修法是复用同一个队列，重启时只把残留帧清掉（`_drain_audio_queue()`）。

---

[← 返回 README](../README.md)
