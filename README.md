# 织梦台：AI 长篇小说生成器 MVP

定位：AI 主写、作者把控、作品持续记忆。当前版本围绕一条闭环实现：

> 新建作品 → 故事方案 → 章节大纲 → AI 写正文 → 一致性检查 → 作者保存

## 一键启动（Windows / macOS）

推荐安装 Docker Desktop 后：

- Windows：双击 `start.bat`
- macOS：双击 `start.command`（首次如果被系统拦截，右键选择“打开”）

启动脚本会自动构建并启动前端、后端和生成 worker，浏览器打开 <http://localhost:3000>。首次启动需要下载依赖和构建镜像，之后可直接启动。

首次启动会创建 `.env` 并生成 `APP_SECRET_KEY`。之后可直接在页面“模型设置”里保存 DeepSeek、通义千问、Kimi 或任意 OpenAI 兼容服务；API Key 会加密存储，SQLite 数据保存在 Docker volume `novel_data` 中，不会随容器重建丢失。

模型设置还支持 `Codex Auth`：先在运行后端 worker 的同一台机器安装 Codex CLI 并执行 `codex login`，再在页面选择 Codex Auth、填写模型并测试连接。该方式调用本机已登录的 `codex exec`，不读取或回传 ChatGPT 浏览器 Cookie，也不使用未公开的 Codex HTTP 接口。Windows 主机安装 Codex CLI 时建议使用本机模式启动；Docker 容器默认看不到主机上的 Windows CLI。若后端进程找不到 CLI，可在 `.env` 设置 `CODEX_CLI_PATH` 为可执行文件路径。

如果没有 Docker，脚本会尝试本机模式，但需要预先安装 Python 3.11+、Node.js 22+ 和 uv。

## 启动后端

```bash
uv sync --dev
source .venv/bin/activate
cp .env.example .env
uv run uvicorn main:app --reload --port 8000
```

生成任务 worker（异步接口需要单独运行）：

```bash
uv run python -m app.worker
```

没有配置页面模型时，界面会显示“演示模式”，生成前会要求确认，fallback 结果也会被明确标记。仍可用环境变量引导首次迁移：

```env
LLM_API_KEY=...
LLM_BASE_URL=https://api.deepseek.com
LLM_MODEL=deepseek-v4-flash
```

## 启动前端

```bash
cd web
npm install
npm run dev
```

所有故事方案、大纲和章节生成都走持久化任务队列，页面会显示阶段、进度、错误和取消状态；worker 必须保持运行。

打开 <http://localhost:3000>。

## 当前模块

- 作品与故事档案
- 人物、世界观和主线资产
- 章节大纲
- 分章正文生成与编辑
- 人物/重复内容/伏笔规则型质检
- 章节版本和作品状态提取（角色 Diff、时间线候选，待审核）
- 作者审核后应用角色状态、时间线和人物别名
- 持久化生成任务、幂等键和失败重试基础设施
- 模型配置中心、连接测试、作品级模型选择和推理强度
- Codex Auth 本机 CLI 登录桥接（`codex login` + `codex exec`）
- 伏笔工作台、AI 伏笔候选审核与回收提醒
- 番茄、起点、晋江公开榜单缓存、趋势分析和原创灵感创建作品
- SQLite 数据持久化

## 下一步

1. 章节修改后从指定章节向后重放角色状态和时间线。
2. 为质检增加 LLM 编辑角色和章节来源定位。
3. 接入真实用户验证，再决定账号、订阅和团队协作。

## Phase 1 状态提取接口

- `GET /api/works/{work_id}/state-extractions?status=pending`：查看待审核结果
- `GET /api/works/{work_id}/state-extractions/{extraction_id}`：查看单次提取
- `POST /api/works/{work_id}/chapters/{chapter_no}/extract-state`：重新提取指定章节
- `POST /api/works/{work_id}/state-extractions/{extraction_id}/review`：批量接受/拒绝/编辑提取项

## 异步生成接口

- `POST /api/works/{work_id}/generation-jobs`：创建 `setup`、`outline`、`chapter` 或 `state_extraction` 任务
- `GET /api/works/{work_id}/generation-jobs/{job_id}`：查询任务状态和结果
- `GET /api/works/{work_id}/generation-jobs?active=true`：恢复当前未完成任务
- `POST /api/works/{work_id}/generation-jobs/{job_id}/cancel`：取消任务

## 新增接口

- `/api/model-profiles`：模型配置、连接测试和模型列表
- `/api/works/{work_id}/foreshadows`：伏笔增删改查
- `/api/trends/search`、`/api/trends/analyze`：公开榜单搜索与趋势分析
- `/api/works/from-trend-idea`：从原创创意创建作品

请求可以带 `idempotency_key`，重复提交会返回同一个任务。榜单只保存公开排名元数据和简介，不保存小说全文。

章节生成或手动保存后会自动产生一条 pending 提取记录。当前不会自动更新 `character_states`，也不会把候选事件当作已确认事实。
