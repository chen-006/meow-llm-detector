# meow llm 检测器 v4.5.0

[English](README_EN.md) · [下载](https://github.com/chen-006/meow-llm-detector/releases/latest)

网页版：https://meowllm.top

下载中文包并解压，Windows 双击 `start.bat`；macOS / Linux 在文件夹里运行 `sh start.sh`。需要 Python 3.11 或更新版本，首次启动会询问是否安装依赖。页面打开后，选模型、填 API 地址和 key，点“开始检测”即可。

参考论文：[One Token Is Enough](https://arxiv.org/abs/2607.10252)。友情链接：[Linux.do 讨论](https://linux.do/t/topic/2704354) · [路由现象讨论](https://linux.do/t/topic/2728901)。实现参考与致谢：[hlwy-ai-checker](https://github.com/hanlinwenyuan/hlwy-ai-checker)。

## 它是做什么的？

向你的 API 问几道短题，看看回答的分布更接近哪个模型。比如同样让模型随便说一个国家，不同模型常选的答案可能不同。

程序在本机运行，附带 GPT 和 Claude 基准，不需要你另外提供可信 API。绿色表示强指向你选择的模型，黄色表示证据不足，红色表示强指向另一个候选模型。**它不是模型身份证，也不能单凭一份报告判断商家造假。**

## 怎么用

1. 选择 GPT、Claude 或其他。内置 GPT 支持 Astra、Sol、Terra、Luna；Claude 支持 Fable 5.1、Opus 5、Sonnet 5、Haiku 4.5。
2. 填服务商提供的 API 根地址，例如 `https://example.com/v1`。实际请求模型会自动填写，你也可以改成服务商的别名；OpenRouter 通常还需要公司前缀。
3. 填 key，选择低 / 中 / 高档。内置基准分别发 **20 / 50 / 100 次**，重试另算，费用由你的服务商收取。
4. 开始检测。报告会分别列出基准采集网址和本次检测网址，并显示每个模型的匹配度与强指向线。

开始旁边可以选择保留请求和响应，结束后导出。临时 key 留在当前页面，方便连测；刷新、关页或后台断开后清空。想下次继续用，可以保存 API 连接，key 只存系统凭据库。分享报告前仍请检查网址和响应里有没有私人信息。

## 基准、生成器和更新

- 两组基准随包提供，也能在“基准库”检查目录并下载；[公开索引](benchmarks/index.json)与[基准文件](benchmarks/official)一起发布。没有联网也能用已安装的基准。
- “生成基准”可以手填、导入或用 AI 生成候选题，再向你选择的可信 API 采样、推荐选题、模拟和导出。采样和 AI 出题会收费；模拟在本机运行，不发模型请求。
- 新采集窗口至少间隔 1 分钟。可以手选题目，不必照单接受推荐。
- 定时检测每次独立计算，不累积旧答案。“检查更新”会提示新版本，经你确认下载；解压到新文件夹再启动，不会自动覆盖正在运行的程序。
- 4.5.0 不再包含 Juice、长上下文和工具包装，也不需要 Node.js。

更详细的使用与复算方法见[技术文档](TECHNICAL_REPORT_CN.md)。两种语言的程序共用代码，**题面不会随界面语言翻译**，否则旧基准就不适用了。

## 启动不了？

先安装 [Python](https://www.python.org/downloads/)，Windows 安装时勾选加入 PATH。首次安装依赖需要联网，启动器只在本目录创建 `.venv`，不自动安装 Python，也不要求管理员权限。macOS / Linux 还需要可用的系统凭据库；保存连接失败时，仍可临时输入 key 检测。

也可以手动启动：

```sh
python -m venv .venv
# Windows: .venv\Scripts\python.exe；macOS / Linux: .venv/bin/python
.venv/bin/python -m pip install -r requirements.txt
.venv/bin/python -B -m gpt56_vnext --locale zh-CN
```

然后打开 `http://127.0.0.1:8765/`。退出启动终端即可关闭后台。运行数据在 `meow_runs`；新版本请先用新目录体验，需要迁移时先关闭旧后台，再复制该目录。

## 注意

固定题目可能被专门路由，模型更新、隐藏提示词和采样参数也可能改变结果。池内模拟不是实际线路准确率；黄色不一定是坏模型，绿色也不能证明每次请求都由同一个模型回答。建议使用限额、可撤销的专用 key，不把本地后台公开到互联网。

许可证为 [PolyForm Noncommercial 1.0.0](LICENSE)，保留 chen-006 与贡献者署名。非 OSI 开源许可证；使用和分发请遵守原许可证。
