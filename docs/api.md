# HTTP API 与命令行

[← 返回 README](../README.md)

浏览器控制台就是这些接口的消费方，你也可以直接调它们做自动化或接入自己的脚本。

---

## 目录

- [HTTP API](#http-api)
- [调用示例](#调用示例)
- [服务端命令行选项](#服务端命令行选项)
- [链路助手 usblink.py](#链路助手-usblinkpy)

---

## HTTP API

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/` | 播放页（给手机 / 平板） |
| GET | `/console` | 主机端图形化控制台 |
| GET | `/ws` | 音频 WebSocket（二进制帧 + 文本控制消息） |
| GET | `/api/status` | 完整状态：采集、配置、合计、客户端列表、历史采样、链路、日志 |
| GET | `/api/devices` | 输出设备与回环设备列表 |
| GET | `/api/qr.svg?data=<url>` | 生成 SVG 二维码 |
| POST | `/api/config` | `{device?, frame_ms?, channels?, rate?}` 改参数（会重启采集） |
| POST | `/api/control` | `{action}`：`start` / `stop` / `restart` / `refresh_link` / `disconnect_clients` / `clear_log` / `shutdown` |
| GET / POST | `/api/quit` | 优雅退出，**仅允许本机调用**（`stop.bat` 用）；非本机返回 `403` |
| GET | `/health` | 轻量健康检查 |

### WebSocket 协议

`/ws` 上跑两种东西：

| 方向 | 类型 | 内容 |
|---|---|---|
| 服务端 → 客户端 | **二进制** | 定长 PCM 帧（16-bit 小端交错） |
| 服务端 → 客户端 | 文本 JSON | `{"type":"config", ...}` 参数与链路变更通知 |
| 客户端 → 服务端 | 文本 JSON | `{"type":"ping","t":<ms>}` RTT 探针 |
| 服务端 → 客户端 | 文本 JSON | `{"type":"pong","t":<同一个值>}` |
| 客户端 → 服务端 | 文本 JSON | `{"type":"rtt","ms":..,"jitter":..,"buffer_ms":..}` 回报链路质量 |

单帧字节数 = `采样率 × 分帧毫秒 / 1000 × 声道数 × 2`。
例如 48 kHz / 10 ms / 2 声道 = 1920 字节。

### `/api/status` 主要字段

| 字段 | 内容 |
|---|---|
| `ok` | 请求是否成功 |
| `capture` | 采集是否运行、捕获设备名、捕获采样率、错误信息 |
| `config` | 捕获设备、分帧长度、声道数、捕获 / 推送采样率、单帧字节 |
| `totals` | 累计发送帧数、码率、帧率、运行时长 |
| `clients` | 每路连接：标识、接入时刻、已发帧、丢弃帧、RTT、抖动、缓冲档位 |
| `history` | 最近 120 个每秒采样点（码率 / 帧率），供实时曲线使用 |
| `link` | 链路探测结果与推荐档位 |
| `log` | 服务端最近若干行输出 |

---

## 调用示例

```bash
# 看完整状态
curl http://127.0.0.1:8787/api/status

# 改分帧长度（会重启采集，并通知所有播放页重建缓冲）
curl -X POST http://127.0.0.1:8787/api/config \
     -H "Content-Type: application/json" \
     -d "{\"frame_ms\": 20}"

# 重新探测链路
curl -X POST http://127.0.0.1:8787/api/control \
     -H "Content-Type: application/json" \
     -d "{\"action\": \"refresh_link\"}"

# 列出可捕获的输出设备
curl http://127.0.0.1:8787/api/devices

# 优雅退出（仅本机可用）
curl http://127.0.0.1:8787/api/quit
```

> `POST /api/config` 的参数会做白名单校验：
> `frame_ms ∈ {5,10,20,30,40,60,100}`、`channels ∈ {1,2}`、
> `rate ∈ {0,48000,44100,32000,24000,16000,8000}`。
> 非法值会被拒绝且不生效，返回 `400`。

---

## 服务端命令行选项

```
python server.py [选项]
```

| 选项 | 默认 | 说明 |
|---|---|---|
| `--host` | `0.0.0.0` | 监听地址 |
| `--port` | `8787` | 监听端口 |
| `--link` | `auto` | 链路类型：`auto` / `adb` / `tether` / `lan` / `wifi` / `none` |
| `--open-console` | 关 | 启动后自动打开控制台（`run.bat` 会带上） |
| `--no-console` | 关 | 不自动打开控制台（优先级更高） |
| `--capture-rate` | `48000` | 向系统请求的捕获采样率；按 48000/44100/32000/96000 顺序协商 |
| `--rate` | `0` | 推送采样率；`0` = 与捕获一致（不重采样） |
| `--channels` | `2` | 声道数；`1` 省一半带宽 |
| `--frame-ms` | `10` | 分帧长度；**不传时按链路类型自动选档** |
| `--device` | 空 | 回环设备名关键字，留空用默认扬声器 |
| `--queue-max` | `15` | 每路客户端待发队列上限，超出丢最旧帧 |
| `--list-devices` | — | 列出所有输出设备与回环设备后退出 |
| `--silent` | 关 | 不向控制台输出；stdout/stderr 改写进 `speaker.log`（`run-silent.vbs` 内部使用） |

### `--rate` 是音质 / 带宽的交换旋钮

只需语音或想大幅降带宽时：

```bat
python server.py --rate 16000 --channels 1
```

带宽从约 1536 kbps 降到约 256 kbps，代价是高频与立体声信息丢失。

---

## 链路助手 `usblink.py`

单独也能用，是用来排查链路问题的好工具：

```bash
python usblink.py                      # 探测并打印所有可用链路
python usblink.py json                 # 以 JSON 输出探测结果
python usblink.py connect --port 8787  # 探测并建立 ADB 反向转发
python usblink.py disconnect --port 8787
```

| 命令 | 说明 |
|---|---|
| `detect`（默认） | 列出所有可用链路及其推荐地址、推荐档位 |
| `json` | 同上，结构化输出，便于脚本消费 |
| `connect --port N` | 建立 ADB 反向转发（`adb reverse tcp:N tcp:N`） |
| `disconnect --port N` | 撤销反向转发 |
| `--prefer` | 强制指定优先链路：`adb` / `tether` / `lan` / `wifi` |

> `server.py` 以 try/except 方式可选导入 `usblink`。
> 即使单独拿走 `server.py` 也能跑，只是退化为纯局域网监听。

---

[← 返回 README](../README.md)
