# 贡献指南

感谢你有兴趣改进 phone-as-speaker。这是一个小而专注的项目，改动请尽量保持「单一职责、可自测」。

---

## 维护者：首次发布到 GitHub

仓库已经 `git init` 好（分支 `main`），所有文件也已 `git add` 暂存，只差一次提交：

```bash
# 1. 配置提交身份（只需一次；机器上没配过就必须先做这一步）
git config user.name  "你的名字"
git config user.email "你的邮箱@example.com"

# 2. 首次提交
git commit -m "chore: 首次发布 phone-as-speaker v1.0.0"

# 3. 在 GitHub 上新建一个空仓库（不要勾选 README / .gitignore / LICENSE），然后：
git remote add origin https://github.com/<your-name>/phone-as-speaker.git
git push -u origin main
```

> 想让提交关联到你的 GitHub 账号，`user.email` 必须是 GitHub 账号里已添加并验证过的邮箱。
> 不想暴露真实邮箱的话，用 GitHub 提供的 `<ID>+<username>@users.noreply.github.com` 隐私邮箱。

推送后建议顺手在仓库设置里补上 **Description** 与 **Topics**
（`windows` `audio` `websocket` `wasapi` `remote-speaker` `phone` `python`），
别人的检索命中率会高很多。

---

## 先决条件

- Windows 10 1607 或更高版本（WASAPI 回环是 Windows 特性，主机端无法在 macOS / Linux 上运行）
- Python 3.9+
- 想测 ADB 链路的话：`platform-tools`（提供 `adb`）+ 一台开了 USB 调试的 Android 设备

## 搭起开发环境

```bat
git clone https://github.com/<your-name>/phone-as-speaker.git
cd phone-as-speaker
setup.bat
selftest.bat
```

`setup.bat` 会在项目内建 `.venv` 并装好依赖；`run.bat` 会优先使用它。
也可以手动来：

```bash
python -m venv .venv
.venv\Scripts\python.exe -m pip install -r requirements.txt
.venv\Scripts\python.exe selftest.py
```

## 动手前先跑一遍自检

```bat
selftest.bat
```

`selftest.py` 会拉起一个临时服务（默认端口 8799）并跑 9 组 56 项检查。
**任何提交都应让这 56 项保持全绿**——它抓出过两个只在重启路径上才会显形的真 bug，
是这个项目最值钱的一层保险。

## 代码约定

### 编码与行尾

| 类型 | 规则 |
|---|---|
| `.py` / `.html` / `.md` | LF、UTF-8 |
| `.bat` / `.vbs` | **纯 ASCII**、CRLF（由 `.gitattributes` 保证检出时为 CRLF） |

**为什么 `.bat` / `.vbs` 必须纯 ASCII**：`wscript` 按系统 ANSI 代码页读取 `.vbs`，
写入中文会变成乱码；`cmd.exe` 处理带 BOM 的批处理也会出问题。
所以这两个类型只写英文，**用户可见的中文提示一律交给 Python 侧输出**（含 `speaker.log`）。

### Python

- 目标 3.9+，但不使用 3.10+ 的语法特性（保持向后兼容）。
- 风格随现有代码：4 空格缩进、类型标注、中文注释。
- 新增依赖前先想清楚必要性——目前只有 4 个依赖（numpy / soundcard / aiohttp / qrcode）。
- **不要**在模块顶层 `import soundcard`。它必须在 `preload_audio_backend()` 中按受控时机导入，
  原因见 `docs/architecture.md` 的「实现札记」。任何新线程用到 WASAPI 都必须先调 `ensure_com()`。

### 前端

- `static/index.html`（播放页）与 `static/console.html`（控制台）都是单文件、零构建、零外部资源。
  请保持这一点——不要引入 npm / CDN 依赖，否则「克隆即可运行」就破了。
- 图表是手写 SVG 折线，不引图表库。

## 提交

1. 从 `main` 切出分支：`git checkout -b fix/short-description`
2. 改动聚焦单一主题，提交信息用祈使句（如 `fix: 采集线程补 COM 初始化`）
3. 跑通 `selftest.bat`，必要时补上对应的自检项
4. 涉及行为变更时同步更新 `CHANGELOG.md` 与相关文档
5. 提 PR，说明「现象 → 原因 → 改法 → 验证方式」

## 特别欢迎的贡献

- **其它平台的捕获后端**：目前只有 Windows WASAPI。macOS 可换 BlackHole 之类的虚拟音频设备，
  Linux 可换 PulseAudio monitor 源——只需替换 `_resolve_loopback()` 与音频捕获段落。
- **新的链路类型**：`usblink.py` 的链路探测与排序是可扩展的。
- **自检项**：任何能被自动化验证的边界情况都欢迎补进 `selftest.py`。

## 报告问题

请附上：Windows 版本、Python 版本、运行方式（哪个 `.bat`）、复现步骤，
以及控制台的**运行日志**或静默模式下的 `speaker.log`。
与链路相关的，请一并说明设备型号、连接方式（USB 调试 / USB 网络共享 / Wi-Fi），
以及是否安装了 spacedesk 之类的同类软件（见 `docs/troubleshooting.md` 的冲突说明）。

## 许可证

提交代码即表示你同意以本项目的 [MIT 许可证](LICENSE) 授权你的贡献。
