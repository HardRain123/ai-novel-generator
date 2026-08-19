# 作品状态自动提取 Phase 1

## 目标

从新生成或保存的章节中提取：

- 角色状态变化：位置、目标、情绪、关系、持有物、身体状态、已知信息、秘密暴露、冲突和阵营
- 时间线候选：事件、故事内时间、地点、参与人物和证据句

Phase 1 的结果全部是 `pending`，不会静默写入正式角色状态。

## 流程

```text
章节生成/保存
  -> chapter_versions 保存正文版本和 content_hash
  -> 状态提取器读取本章正文和已知人物
  -> 角色名称/别名解析
  -> 结构化结果校验
  -> state_extractions + character_state_changes + pending timeline_events
  -> 前端显示 Diff，等待作者审核
```

提取器要求每个状态变化提供正文 `evidence`。不确定的时间保留原始表达，使用 `unknown` 或 `relative`，不强行补成绝对日期。

## 数据边界

- `chapter_versions` 保存章节历史，避免修改正文后无法重算。
- `state_extractions` 保存一次提取的原始 JSON、模型和版本。
- `character_state_changes` 保存角色字段级 Diff，当前状态仍不变。
- `timeline_events.review_status = pending` 的记录是候选，不是已确认事实。
- `character_states` 预留 `as_of_chapter` 和 `source_version_id`，由 Phase 2 审核应用时更新。

## 当前 API

```text
GET  /api/works/{work_id}/state-extractions?status=pending
GET  /api/works/{work_id}/state-extractions/{extraction_id}
POST /api/works/{work_id}/chapters/{chapter_no}/extract-state
```

没有配置 `LLM_API_KEY` 时使用低置信度 fallback，只生成一个基于首句的时间线候选并附带 warning；接入模型后才启用角色字段级提取。

## Phase 2 预留

审核接口应支持逐项 accept/reject/edit，并在一个事务内：

1. 更新 `character_states` 当前快照；
2. 将 `character_state_changes.status` 改为 `applied` 或 `rejected`；
3. 将时间线候选改为 `confirmed` 或 `rejected`；
4. 记录来源章节版本，保证可追溯。

