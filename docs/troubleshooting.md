# 排查手册与已知限制

[← 返回 README](../README.md)

---

## 目录

- [一分钟定位：先看这几项](#一分钟定位先看这几项)
- [现象 → 原因 → 处理](#现象--原因--处理)
- [已知限制与注意事项](#已知限制与注意事项)
- [与 spacedesk 的 USB 直连冲突](#与-spacedesk-的-usb-直连冲突)

---

## 一分钟定位：先看这几项

拿到问题先按这个顺序过一遍，绝大多数情况三步内就定位了。

| 顺序 | 看什么 | 结论 |
|---|---|---|
| 1 | 控制台的状态行：`采集` 是否在跑 | 不在跑 → 音频设备问题，与网络无关 |
| 2 | 客户端表格的行数（`客户端 N`） | **N = 0 → 连接 / 链路问题；N > 0 仍无声 → 才查音频与浏览器策略** |
| 3 | 客户端表格里的 RTT 与抖动 | 抖动远大于正常值 → 切更厚的缓冲档，或换有线链路 |
| 4 | 电脑当前是否真的在播放声音 | 回环捕获的是播放流，静音播放时电平表就是 0 |

> **第 2 项是第一判据。** 「客户端 0」与音频质量、编解码、缓冲全都无关——
> 它只说明没有任何设备连上来。

---

## 现象 → 原因 → 处理

| 现象 | 原因与处理 |
|---|---|
| 启动报端口被占用 / 绑定失败 | 旧的服务还在跑。有窗口就 `Ctrl+C`；无窗口就**双击 `stop.bat`**；或改 `--port` |
| 控制台显示 `客户端 0` 且设备没声音 | **先看这一项**：0 就是没有任何设备连上，与音频无关。依次查：设备能否打开提示的地址、是否同一局域网或已开网络共享、防火墙是否放行 python |
| 设备打不开页面 | 地址不是它可达的那条链路。用控制台「连接信息」里的**其它可用地址**逐条试，或点「重新探测链路」 |
| 双击 `run-silent.vbs` 后没反应 | 本来就不会有窗口，去看浏览器控制台是否打开、`speaker.log` 是否有内容。若弹了英文提示框，说明启动失败（多为缺 Python / 缺依赖），按提示跑一次 `setup.bat` |
| 静默运行后不知道在哪 / 怎么关 | 控制台「运行控制 → **退出程序**」；或双击 `stop.bat`。进程与端口记在 `.speaker.pid` |
| `stop.bat` 说「进程不是 Python」 | 服务其实已经退出了（pid 被系统回收）。脚本会顺手删掉过期的 `.speaker.pid`，直接再启动即可 |
| `adb devices` 为空 | 设备没开 USB 调试 / 没授权 / 数据线不支持传输；或 spacedesk 抢占（见下文） |
| `usblink.py detect` 显示「未找到 adb」 | 装 `platform-tools` 并解压到常见位置，或加进 PATH |
| 有声音但断续 | 看控制台里的**链路 RTT 与抖动**；切到「抗卡顿」档；无线可用 `--rate 16000 --channels 1` 降带宽 |
| 改了参数但音质 / 延迟没变 | 参数变更会重启采集，播放页收到新配置后**重建缓冲**；若设备仍播放旧缓冲，稍等 1~2 秒 |
| `ModuleNotFoundError: No module named 'numpy'` | 启动用的解释器不是装好依赖的那个。**双击 `setup.bat`** 建本地 `.venv` 即可根治 |
| 页面电平表不动 | 电脑当前没有在播放声音。回环捕获的是播放流，静音播放时就是 0 |
| 采集报 `0x800401f0` | 采集线程没有初始化 COM。本版已修（`ensure_com()`）；若自行改造代码请保留该调用，详见 [架构说明](architecture.md#七实现札记两个坑都踩过) |
| 任务管理器里有两个 `pythonw.exe` | 正常。venv 的 `pythonw.exe` 是转发器，会以子进程方式拉起真正的解释器 |

---

## 已知限制与注意事项

### 声音会重复

回环捕获的是播放流，电脑自己的扬声器仍在发声，同一房间会听到两重声音并产生梳状滤波。
处理办法：把电脑音量拉到 0 试试（不同 Windows 版本行为不一致，**请自行验证一次**），
或把默认播放设备切到一个不会实际出声的输出。

### 数据线必须支持数据传输

很多随充电头附赠的线只有电源芯，插上只充电，adb 与网络共享都不会出现。

### 插线 ≠ 有网

USB 数据线只承载 MTP / PTP 协议，**不承载 TCP/IP**。要让设备浏览器访问 PC 上的 HTTP 服务，
必须另有 IP 通路：

1. 开「USB 网络共享」，使 PC 出现 `192.168.42.x`（Android）/ `172.20.10.x`（iOS）；
2. 或两端在同一 Wi-Fi；
3. 或用 ADB 反向转发 + `localhost`（仅 Android）。

### ADB 仅限 Android

iOS 不出现在 adb 设备列表里，只能走「USB 网络共享」（`172.20.10.x`）。

### USB 网络共享会改写设备的联网路由

用完记得关掉。

### 仅支持 Windows 主机

WASAPI 回环是 Windows 特性。macOS 需改用 BlackHole 之类的虚拟音频设备，
Linux 需改用 PulseAudio monitor 源——只需替换 `_resolve_loopback()` 与捕获段落。
`usblink.py` 的 `ipconfig` 解析同样是 Windows 专属，其它平台会返回空列表并自然回退。

### 无加密

与 spacedesk 一样是明文传输。有线链路是点对点数据线，暴露面小；
但用 Wi-Fi / `--link none` 时，同网段他人可能截获音频内容。

### iOS 需要一次点击

Safari 要求音频在用户手势中启动。页面进来会**自动尝试连接一次**；
若浏览器拒绝自动解锁，按钮会变成「点击启用声音」，点一下即可。

### 小米设备的 USB 模式

`VID_2717&PID_FF40` = MTP 文件传输（**不含 ADB**）。
需在设备上开启 USB 调试才会出现 ADB 接口，`adb devices` 才能看到。

---

## 与 spacedesk 的 USB 直连冲突

**现象**：装了 spacedesk 的机器上，插好数据线、设备也开了 USB 调试，但 `adb devices` 始终为空，
ADB 反向转发也就起不来。

**原因**：spacedesk 的「USB Cable Driver Android」开着时，它会用
**Android Open Accessory（AOA）协议**把设备拉进配件模式，**独占数据线**。
此时设备会以厂商自定义接口（Class_FF/FF/00）枚举，
`VID 18D1`（Google，AOA 配件模式）`PID 2D00 / 2D01`，**总线上没有 ADB 接口**。

可在设备管理器里确认：

| 项 | 值 |
|---|---|
| 硬件 ID | `USB\VID_18D1&PID_2D00` 或 `...&PID_2D01` |
| 服务 | `spacedeskAndroidUsb` |
| 驱动 | `spacedeskDriverAndroidUsb.sys` |
| INF | `oem104.inf`（Provider = datronicsoft） |

spacedesk Driver Console 自己的原话就是：
「Android USB ON disables other USB cable applications」。

**处理**：不需要卸载 spacedesk 的驱动。

1. 打开 spacedesk Driver Console
2. 进入 **COMMUNICATION INTERFACES**
3. 把 **USB Cable Driver Android 设为 OFF**
4. 拔插数据线，设备上选择「文件传输」并确认 USB 调试已授权

设备回到正常模式后用的是厂商自己的 VID，不会被 spacedesk 的驱动命中。

---

[← 返回 README](../README.md)
