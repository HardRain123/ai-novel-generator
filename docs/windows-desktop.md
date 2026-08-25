# Windows 单机版发布说明

最终用户只需要下载并运行 GitHub Release 中的 `WeDream-Setup-*.exe`。安装包内含 Electron 前端和打包后的 Python 后端/worker，因此不需要预装 Python、Node.js、npm、uv 或 Docker。

首次启动会在 `%APPDATA%\\织梦台\\data` 创建 SQLite 数据库、加密密钥和日志。升级不会覆盖该目录。

## 模型连接

- OpenAI 兼容 API、DeepSeek、通义千问和 Kimi：用户只需要在应用内的“模型服务”填写自己的 API Key。
- Codex Auth：用户需要自行安装 Codex CLI 并执行 `codex login`。桌面安装包不会捆绑或复制用户的 Codex 登录状态。

## 发布与自动更新

客户端会在启动后检查 GitHub Releases。发布一个更高版本的 Windows NSIS 安装包后，旧版会提示用户下载；下载完成可立即重启安装，或在下次退出后安装。

发布步骤：

1. 将 `desktop/package.json` 的 `version` 提升为新的语义化版本，例如 `0.2.1`。
2. 提交并推送代码。
3. 创建并推送同版本 Git tag，例如 `v0.2.1`。
4. GitHub Actions 的 `Release Windows Desktop` 工作流会构建安装程序、上传 GitHub Release，并发布 `latest.yml` 更新元数据。

只有正式 GitHub Release 会触发客户端更新；普通 `git push`、草稿 Release 和未提升版本号的构建不会推送更新。
