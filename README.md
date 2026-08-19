# 织梦台：AI 长篇小说生成器 MVP

定位：AI 主写、作者把控、作品持续记忆。当前版本围绕一条闭环实现：

> 新建作品 → 故事方案 → 章节大纲 → AI 写正文 → 一致性检查 → 作者保存

## 启动后端

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
uvicorn main:app --reload --port 8000
```

没有配置 `LLM_API_KEY` 时，后端使用本地 fallback 生成示例内容，便于先验证产品闭环。配置 OpenAI-compatible API 后使用实际模型：

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

打开 <http://localhost:3000>。

## 当前模块

- 作品与故事档案
- 人物、世界观和主线资产
- 章节大纲
- 分章正文生成与编辑
- 人物/重复内容/伏笔规则型质检
- 章节版本和作品状态提取（角色 Diff、时间线候选，待审核）
- SQLite 数据持久化

## 下一步

1. Phase 2：作者审核并应用状态 Diff。
2. 章节修改后从指定章节向后重放角色状态和时间线。
3. 为质检增加 LLM 编辑角色和章节来源定位。
4. 接入真实用户验证，再决定账号、订阅和团队协作。

## Phase 1 状态提取接口

- `GET /api/works/{work_id}/state-extractions?status=pending`：查看待审核结果
- `GET /api/works/{work_id}/state-extractions/{extraction_id}`：查看单次提取
- `POST /api/works/{work_id}/chapters/{chapter_no}/extract-state`：重新提取指定章节

章节生成或手动保存后会自动产生一条 pending 提取记录。当前不会自动更新 `character_states`，也不会把候选事件当作已确认事实。
