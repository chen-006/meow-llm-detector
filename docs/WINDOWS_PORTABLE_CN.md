# meow LLM 检测器 v4.5.0 · Windows 便携版

适用 Windows 10 / 11，Intel / AMD 64 位（x64）。内置 Python 3.13.15 和全部项目依赖，无需另装 Python、配置 PATH 或首次联网安装依赖；调用模型 API 仍需联网。

1. 下载文件名含 `windows-x64-portable-zh-CN.zip` 的中文包（英文选 `en`）。
2. **完整解压到新的可写文件夹，双击 `start.bat`**。不要在压缩包内直接运行，也不要删除 `portable-python`。
3. 浏览器未自动打开时，访问 `http://127.0.0.1:8765/`。使用期间保持终端开启，关闭终端即可停止。

数据保存在 `meow_runs`。升级请先关闭旧后台，再向新目录迁移该文件夹；系统凭据库里的 Key 不随文件夹迁移到另一台电脑。

此包与普通 v4.5.0 的检测核心和基准相同，无需重新校准。macOS / Linux 请下载原源码包。`README_SOURCE_CN.md` 中的安装步骤仅用于源码包；便携版请使用 `start.bat`，无需运行 `launch.py`。

构建信息见 `PORTABLE_BUILD.json`，包内校验见 `SHA256SUMS.txt`；Python 许可位于 `portable-python/LICENSE.txt`，依赖许可随各 `.dist-info` 目录保留。算法与统计限制见 `TECHNICAL_REPORT_CN.md`。
