import re
from typing import Any

from app.services.context_builder import build_context


AI_TELLING_PATTERNS = (
    r"(?:他|她|主角)(?:终于|突然|不由得|开始)?(?:意识到|明白|知道|感到|觉得)",
    r"这一刻[，,]?他(?:终于)?",
    r"这一切(?:才刚刚开始|只是开始)",
    r"命运的齿轮",
    r"空气(?:仿佛|似乎)?凝固",
    r"嘴角勾起一抹",
)


def quality_check(work: dict[str, Any], chapter_no: int, content: str) -> tuple[list[dict[str, Any]], int]:
    issues: list[dict[str, Any]] = []
    text = content.strip()
    if not text:
        issues.append({"kind": "empty", "severity": "high", "message": "章节正文为空。", "suggestion": "先生成正文或补充本章场景。"})
    if len(text) < 180 and text:
        issues.append({"kind": "length", "severity": "low", "message": "本章内容较短，可能还没有完成完整的场景推进。", "evidence": f"当前约 {len(text)} 字。", "suggestion": "补充一个具体场景、阻力和结尾钩子。"})

    sentences = [line.strip() for line in text.replace("。", "。\n").splitlines() if line.strip()]
    repeated = [line for index, line in enumerate(sentences[1:], start=1) if line == sentences[index - 1]]
    if repeated:
        issues.append({"kind": "repetition", "severity": "medium", "message": "发现连续重复的句子。", "evidence": repeated[0][:100], "suggestion": "删除重复句或改成新的动作推进。"})

    try:
        context = build_context(work, chapter_no)
    except Exception:  # pragma: no cover - quality checks must not block saving
        context = {}
    for warning in context.get("continuity_warnings", []):
        issues.append({
            "kind": "continuity_context",
            "severity": "high",
            "message": "生成上下文存在连续性风险。",
            "evidence": str(warning),
            "suggestion": "先确认前置章节状态，或从最近有效快照重建后再生成。",
        })

    telling_hits = []
    for pattern in AI_TELLING_PATTERNS:
        telling_hits.extend(re.findall(pattern, text))
    if len(telling_hits) >= 3:
        issues.append({
            "kind": "ai_style",
            "severity": "medium",
            "message": "发现较多模板化情绪解释或万能表达。",
            "evidence": "、".join(telling_hits[:5]),
            "suggestion": "优先改为动作、对白、物件或环境反馈；只保留语境确实需要的表达。",
        })

    plan = next((item for item in (work.get("chapter_plans") or []) if int(item.get("chapter_no") or 0) == chapter_no), {})
    required_beats = plan.get("causal_beats") or []
    if required_beats and len(text) < max(500, len(required_beats) * 220):
        issues.append({
            "kind": "scene_progress",
            "severity": "low",
            "message": "章节长度可能不足以展开全部因果节点。",
            "evidence": f"因果节点 {len(required_beats)} 个，当前约 {len(text)} 字。",
            "suggestion": "检查每个节点是否包含行动、阻力和结果，不要只写剧情概述。",
        })

    for item in work.get("foreshadows", []):
        expected = int(item.get("expected_reveal_chapter") or 0)
        if expected and expected <= chapter_no and item.get("status") == "open":
            issues.append({"kind": "foreshadow", "severity": "medium", "message": f"伏笔“{item.get('clue', '')}”预计在本章前回收，但仍处于未回收状态。", "suggestion": "确认本章是否需要回收，或调整预计回收章节。"})

    penalty = sum({"low": 5, "medium": 12, "high": 35}.get(issue["severity"], 0) for issue in issues)
    return issues, max(0, 100 - penalty)
