# 更新日志

本文件记录项目的显著变更。格式参考 [Keep a Changelog](https://keepachangelog.com/zh-CN/1.1.0/)，
版本号遵循 [语义化版本](https://semver.org/lang/zh-CN/)。

## [未发布]

### 修复

**RTT 只报「最近一次」，一次尖峰就被当成链路变差**
- 每个客户端保留最近 8 次 RTT 采样（约 16 秒），`/api/status` 与 `/health` 同时给出
  `rtt_ms`（最近一次）、`rtt_med_ms`（中位）、`rtt_max_ms`（峰值）；5 秒统计行也从
  「取最小值」改成「中位 / 峰」，尖峰不再被抹平，也不会再冒充常态。
- 客户端回报的 `link_ms`（已扣掉服务端发送反压）此前被服务端丢弃，现在一并记录，
  控制台显示「中位 · 峰 · 链路」三个数，能直接看出多出来的时间是我们的积压还是链路。

**采集端丢音频（音频在进网络之前就已经少了）**
- WASAPI 环形缓冲此前隐式等于读取块（默认 10 ms），采集线程一旦被系统抢走十几毫秒就会
  溢出，旧音频被 WASAPI 直接丢弃，听感是「随机跳一下」，而服务端丢帧计数仍是 0。
  现在环形缓冲独立放大到 100 ms，读取块不变，因此播放延迟不变。
  实测：10 ms 缓冲在 30 ms 停顿下丢 240 ms 音频、报 32 次 discontinuity；100 ms 缓冲零丢失。
- 采集线程提到 `ABOVE_NORMAL` 优先级；放宽 CPython 分代 GC 阈值并冻结启动期对象，
  减少采集线程自身的停顿来源。
- `soundcard` 的 `SoundcardRuntimeWarning: data discontinuity in recording` 此前被 warnings
  按位置去重、只打印一次，且不进日志环。现在接管 `warnings.showwarning` 逐次计数。

### 新增

- 启动参数 `--capture-buffer-ms`（默认 `100`）：WASAPI 环形缓冲时长。
- `/api/status` 的 `capture` 增加 `buffer_ms`、`buffer_fallback`、`lost_ms`、`discontinuities`；
  `/health` 增加 `capture_buffer_ms`、`capture_lost_ms`、`capture_discontinuities`。
- 控制台显示「采集缓冲」与「音频断点」，出现断点时标红。

## [1.0.0] — 2026-09-13

首个公开发布版本。

### 新增

**音频链路**
- 基于 WASAPI 回环捕获（`soundcard`）抓取 Windows 默认输出设备的播放流，**无需安装虚拟音频驱动**。
- PCM 16-bit 线性量化、不压缩；可选抗混叠降采样与单声道转换。
- 单端口 HTTP + WebSocket 传输，多路客户端独立发送队列与丢帧策略。
- 运行时可切换捕获设备、分帧长度、声道数、推送采样率，改参数自动重启采集并通知播放页重建缓冲。

**链路自适应**
- 四类链路自动探测并按 `adb > tether > lan > wifi` 优先级择优：
  ADB 反向端口转发、USB 网络共享、有线以太网、Wi-Fi。
- 链路类型自动决定推荐参数：有线档 10 ms 分帧 / 60 ms 客户端缓冲，无线档 20 ms / 200 ms。
- `usblink.py` 可独立作为命令行工具使用（探测 / JSON 输出 / 建立与撤销反向转发）。

**Web 端**
- 手机 / 平板播放页：零安装，浏览器打开即用；抖动缓冲（`nextTime` 播放时刻表）+ 音量 + 电平表。
- 主机端浏览器控制台 `/console`：运行状态、连接信息（含二维码与链路重探测）、参数调节、
  运行控制、客户端明细、实时曲线、运行日志，每秒刷新。
- RTT 探针：客户端回报 RTT 均值与抖动，控制台可看到每条链路的实时质量。
- 播放页会按链路档位自动切换缓冲档按钮；未收到用户手势时自动尝试解锁音频。

**运行与运维**
- 无窗口运行：`run-silent.vbs` → `run.bat --silent` → `pythonw.exe`，输出重定向进 `speaker.log`。
- 停止方式三条：控制台「退出程序」按钮、`stop.bat`（读 `.speaker.pid` 校验进程后优雅退出）、`Ctrl+C`。
- `GET /api/quit` 仅允许本机调用，防止同网段他人关停服务。
- 一键脚本：`setup.bat`（建本地 `.venv` 并装依赖）、`run.bat`、`run-silent.vbs`、`stop.bat`、`selftest.bat`。

**测试与文档**
- `selftest.py`：9 组 56 项端到端自检，覆盖采集、页面、链路探测、只读 API、
  WebSocket 音频流（帧长 / 帧率 / 码率 / RTT）、参数变更与采集重启、运行控制、多客户端并发、断开清理。
- `docs/` 收录架构说明、HTTP API 手册、排查手册，以及 spacedesk 全链技术原理分析。

### 修复

- **采集线程的 COM 未初始化**：`soundcard` 只在模块首次导入时调用一次 `CoInitializeEx`，
  而 COM 是按线程初始化的，因此「改参数 / 重启采集」新建的线程必撞 `0x800401f0`。
  改为每个使用 WASAPI 的线程自行补一次 `CoInitializeEx`，且必须等音频后端已导入之后再进行
  （抢先初始化会让 `soundcard` 的 `check_error` 拿到 `S_FALSE` 并抛出 `0x100000001`）。
- **重启采集后一帧都发不出去**：`broadcast()` 已 `await` 在旧的队列对象上，
  而重启时替换了 `self.audio_q`，导致消费端永远挂在旧队列——采集正常、状态页正常、客户端静默。
  改为复用同一队列，重启时只清空残留帧。
- **厚缓冲档被误判为积压**：播放页的 `maxLead` 由固定 300 ms 改为随目标缓冲动态计算，
  否则 350 ms 档会被反复重排。
