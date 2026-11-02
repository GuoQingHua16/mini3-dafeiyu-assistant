# 电脑端安装与运行

验证环境为 Windows、Python 3.12.3、PowerShell 7。程序需要 Python 3.11 及以上；推荐在英文路径克隆仓库，固件工具链对中文路径的支持需要单独确认。

## 1. Python 环境

在仓库根目录打开 PowerShell：

```powershell
pwsh -File .\tools\setup-host.ps1 -Python python
```

脚本只创建本项目 `.venv` 并安装依赖。`requirements-lock.txt` 记录整理时实际使用的版本；遇到平台兼容问题可按 `requirements.txt` 的范围调整，并重新验证。

## 2. ADB

从 [Android SDK Platform-Tools 官方页面](https://developer.android.com/tools/releases/platform-tools) 获取 Windows 版本，按官方条款解压至：

```text
tools/platform-tools/adb.exe
```

ADB二进制不随此仓库分发。设备驱动和连接准备参考 [Mini3 官方文档](https://docs2.listenai.com/x/1KVekpl1e)。

初次连接会选择唯一在线、非Recovery的ListenAI兼容应用设备，并在进程中固定该设备。连接多台设备时，请显式指定：

```powershell
$env:MINI3_ADB_SERIAL = '你的设备序列号'
```

后台运行时不要另外打开 `adb shell`；查看 `mini3-agent/runtime/usb-link-status.json` 和日志即可。调试前先用 `stop.ps1` 关闭后台。

## 3. 本地识别模型

```powershell
.\.venv\Scripts\python.exe .\tools\fetch_models.py
```

下载位置与来源：

| 文件 | 位置与官方来源 |
| --- | --- |
| SenseVoice int8 模型、tokens | `models/sherpa-onnx-sense-voice-zh-en-ja-ko-yue-int8-2024-07-17/`；[sherpa-onnx模型说明](https://k2-fsa.github.io/sherpa/onnx/sense-voice/pretrained.html) |
| Silero VAD | `models/vad/silero_vad.onnx`；[官方说明](https://k2-fsa.github.io/sherpa/onnx/vad/silero-vad.html) |

脚本从官方GitHub Release下载并核对项目记录的SHA256。模型权重不在Git仓库内，首次下载需要时间和磁盘空间；许可证以各自上游为准。

## 4. 固件、网络和密钥

先按 [固件说明](firmware.md) 准备匹配的 Mini3 CP 应用。设备通过 USB 接电脑，同时接入与电脑互通的2.4GHz Wi-Fi。MP3回传服务使用本机TCP 8765，仅用于可信局域网；热点客户端隔离或防火墙可能阻止设备访问电脑。

前台启动：

```powershell
.\mini3-agent\start.ps1 -Mode button
```

未提供 DeepSeek 密钥时，会隐藏输入并仅在当前进程使用。也可以在当前PowerShell会话中以隐藏输入配置：

```powershell
$secureKey = Read-Host 'DeepSeek API Key（隐藏输入）' -AsSecureString
$keyPointer = [Runtime.InteropServices.Marshal]::SecureStringToBSTR($secureKey)
try { $env:DEEPSEEK_API_KEY = [Runtime.InteropServices.Marshal]::PtrToStringBSTR($keyPointer) }
finally { [Runtime.InteropServices.Marshal]::ZeroFreeBSTR($keyPointer) }
```

不要将真实密钥粘进README、配置示例或提交记录。程序也兼容读取使用者已自行配置的用户环境变量，但安装脚本不会替你永久保存密钥。

## 5. 后台与拍摄

```powershell
.\mini3-agent\run-background.ps1 -Mode button -StartResting
.\mini3-agent\set-mode.ps1 -Mode continuous
.\mini3-agent\stop.ps1
```

`StartResting` 使启动后采音保持关闭，按主键唤起。拍摄时双击 `mini3-agent/开始拍摄.cmd`；缺少密钥时，启动窗口会隐藏输入，只供本次后台使用。

## 6. 检查

先停止后台，再运行诊断，避免争用USB：

```powershell
.\.venv\Scripts\python.exe .\mini3-agent\agent.py --check
.\.venv\Scripts\python.exe .\mini3-agent\agent.py --speak '你好，连接测试。'
```

电脑检查在 `mini3-agent` 目录执行 `..\.venv\Scripts\python.exe -m unittest -q`。完整检查需要模型及随模型提供的测试音频；它不等于真人口述或所有USB拔插情况的验收。
