# 提交基准

[English](CONTRIBUTING_EN.md)

在生成器采样、校准后导出 `.meow.json`，检查里面的来源网址、题面、作者说明是否含私人信息。不要提交 key、运行数据库或未经检查的完整响应。

1. Fork 仓库，把文件放到 `benchmarks/community/<你的包ID>/<版本>.meow.json`。
2. 在 `benchmarks/index.json` 添加一项。字段参照官方项，但 `publisher` 必须为 `community`，路径必须在 `benchmarks/community/`。`sha256` 是实际文件字节摘要，`content_sha256` 来自包内容；不要自行改包中的摘要来冒充校准。
3. 提交 Pull Request。CI校验结构、字节摘要、内容身份和校准绑定，不执行基准代码或发模型请求。维护者审核后合并；通过检查不等于模型身份认证。

同ID同版本不覆盖；新内容使用新版本。发现安全问题可在索引将相关项标为 `"withdrawn":"security"` 并说明原因，由维护者发布。离线客户端要等目录刷新后才知道新撤回。

代码改动先运行公开tests；请用合成凭据和本地假响应，不在CI调用付费模型。没有全量独立实测时请明确标注，不宣称“真实准确率99%”。
