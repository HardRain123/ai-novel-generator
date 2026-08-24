import re
from typing import Any
from difflib import SequenceMatcher

from app.services.character_cards import compact_character


GENERIC_CHARACTER_NAMES = {"主角", "男主", "女主", "主人公", "关键对手", "反派", "配角", "导师", "朋友"}
LANGUAGE_RISK_PATTERNS = (
    re.compile(r"(?:烧|燃烧|吞噬|撕裂|点燃).{0,8}(?:存活率|指标|概率|数值|风险值)"),
    re.compile(r"(?:赋能|抓手|闭环|颗粒度|对齐|拉满).{0,8}(?:命运|情绪|人生|关系)"),
)


def _text(value: Any) -> str:
    return str(value or "").strip()


def _normalized_text(value: Any) -> str:
    return "".join(char for char in _text(value) if char.isalnum() or "\u4e00" <= char <= "\u9fff")


def _too_similar(left: Any, right: Any, *, threshold: float = 0.82, minimum: int = 18) -> bool:
    left_text = _normalized_text(left)
    right_text = _normalized_text(right)
    if not left_text or not right_text or min(len(left_text), len(right_text)) < minimum:
        return False
    return left_text in right_text or right_text in left_text or SequenceMatcher(None, left_text, right_text).ratio() >= threshold


APPEARANCE_SIGNALS = {
    "体态": ("身高", "身形", "瘦", "胖", "高个", "矮", "肩", "腰", "手", "指节", "背", "腿", "体格", "虎背"),
    "五官发型": ("脸", "眉", "眼", "鼻", "嘴", "唇", "发", "头发", "短发", "长发", "光头", "胡茬", "肤", "皮肤"),
    "衣着配饰": ("穿", "衣", "裤", "鞋", "袖", "外套", "工装", "白大褂", "马甲", "围巾", "戒指", "眼镜", "帽"),
    "辨识痕迹": ("疤", "痕", "烫", "胎记", "纹身", "缺", "旧伤", "伤口", "耳洞", "跛", "烧伤"),
}
GENERIC_APPEARANCE_WORDS = ("英俊", "绝美", "漂亮", "冷艳", "帅气", "气质非凡", "倾国倾城")


def _appearance_issues(appearance: Any) -> list[str]:
    content = _text(appearance)
    issues: list[str] = []
    if len(content) < 32:
        issues.append("外貌过短；请补充可视化细节")
        return issues
    kinds = [label for label, signals in APPEARANCE_SIGNALS.items() if any(signal in content for signal in signals)]
    if len(kinds) < 3:
        issues.append("外貌至少应包含体态、五官发型、衣着配饰、辨识痕迹中的三类细节")
    if any(word in content for word in GENERIC_APPEARANCE_WORDS) and len(kinds) < 3:
        issues.append("外貌不能只用英俊、冷艳等评价词，应写出具体可见细节")
    if sum(content.count(marker) for marker in ("说话", "开口", "习惯", "总是", "喜欢")) >= 2:
        issues.append("外貌中混入过多性格或语言描写，请移到 personality 或 voice")
    return issues


def _intra_character_duplicate_issues(character: dict[str, Any]) -> list[str]:
    core = character.get("dramatic_core") if isinstance(character.get("dramatic_core"), dict) else {}
    fields = {
        "人物小传": character.get("biography"),
        "外貌": character.get("appearance"),
        "性格": character.get("personality"),
        "语言习惯": character.get("voice"),
        "人物弧": character.get("arc"),
        "目标": core.get("goal"),
        "动机": core.get("motivation"),
        "缺陷": core.get("flaw"),
        "阻力": core.get("conflict"),
    }
    pairs = (
        ("人物小传", "人物弧"), ("人物小传", "目标"), ("人物小传", "动机"),
        ("外貌", "性格"), ("外貌", "语言习惯"), ("性格", "语言习惯"),
        ("目标", "动机"), ("目标", "阻力"), ("动机", "缺陷"),
    )
    return [f"{left}与{right}内容重复，请按字段职责分别重写" for left, right in pairs if _too_similar(fields[left], fields[right])]


def normalize_setup(work: dict[str, Any], data: Any) -> dict[str, Any]:
    result = data if isinstance(data, dict) else {}
    bible = result.get("story_bible") if isinstance(result.get("story_bible"), dict) else {}
    for field in (
        "summary", "theme", "world", "ending", "style_rules", "title_interpretation",
        "reader_promise", "core_hook", "core_conflict", "stakes",
    ):
        bible[field] = _text(bible.get(field))
    for field in ("must_have_elements", "avoid_drift"):
        value = bible.get(field)
        bible[field] = [_text(item) for item in value if _text(item)] if isinstance(value, list) else []

    characters: list[dict[str, Any]] = []
    for raw in result.get("characters", []) if isinstance(result.get("characters"), list) else []:
        if not isinstance(raw, dict) or not _text(raw.get("name")):
            continue
        characters.append(compact_character(raw))

    arcs: list[dict[str, Any]] = []
    for index, raw in enumerate(result.get("plot_arcs", []) if isinstance(result.get("plot_arcs"), list) else [], start=1):
        if not isinstance(raw, dict):
            continue
        arcs.append({
            "title": _text(raw.get("title")) or f"第{index}卷",
            "synopsis": _text(raw.get("synopsis")),
            "sequence": int(raw.get("sequence") or index),
        })
    return {"story_bible": bible, "characters": characters, "plot_arcs": arcs}


def evaluate_setup(work: dict[str, Any], data: dict[str, Any]) -> tuple[list[str], int]:
    issues: list[str] = []
    bible = data.get("story_bible") or {}
    title = _text(work.get("title"))
    title_context = " ".join(_text(bible.get(key)) for key in ("title_interpretation", "reader_promise", "summary"))
    if title and title not in title_context:
        issues.append("书名没有在标题解读、读者承诺或故事梗概中被明确解释。")
    for field, label, minimum in (
        ("summary", "故事梗概", 100),
        ("title_interpretation", "书名解读", 30),
        ("reader_promise", "读者承诺", 30),
        ("core_hook", "核心钩子", 25),
        ("core_conflict", "核心冲突", 25),
        ("stakes", "失败代价", 20),
        ("ending", "结局方向", 30),
    ):
        if len(_text(bible.get(field))) < minimum:
            issues.append(f"{label}过短或缺失，暂时不足以约束后续大纲。")
    if len(bible.get("must_have_elements") or []) < 3:
        issues.append("至少需要 3 个能兑现书名的必写元素。")
    if len(bible.get("avoid_drift") or []) < 2:
        issues.append("至少需要 2 条防跑偏边界。")

    characters = data.get("characters") or []
    if len(characters) < 3:
        issues.append("主要人物少于 3 人，关系和冲突支点不足。")
    names = [_text(item.get("name")) for item in characters]
    if len(names) != len(set(names)):
        issues.append("主要人物存在重名。")
    if any(name in GENERIC_CHARACTER_NAMES for name in names):
        issues.append("人物仍使用“主角/反派”等占位名，没有形成可写的人物。")
    for item in characters:
        name = _text(item.get("name")) or "未命名人物"
        if len(_text(item.get("biography"))) < 60:
            issues.append(f"{name}的人物小传过短。")
        for issue in _appearance_issues(item.get("appearance")):
            issues.append(f"{name}{issue}。")
        for field, label in (("motivation", "深层动机"), ("flaw", "缺陷"), ("character_arc", "人物弧"), ("voice", "语言行动特征")):
            if len(_text(item.get(field))) < 12:
                issues.append(f"{name}缺少可执行的{label}。")
    arcs = data.get("plot_arcs") or []
    if len(arcs) < 3:
        issues.append("卷级主线少于 3 段，缺少建立、升级和兑现。")
    if any(len(_text(item.get("synopsis"))) < 35 for item in arcs):
        issues.append("卷级主线没有写清起点、关键因果和结束状态。")
    return issues, max(0, 100 - min(90, len(issues) * 10))


def normalize_outline(result: Any) -> list[dict[str, Any]]:
    raw_items = result.get("chapters") if isinstance(result, dict) else result
    items: list[dict[str, Any]] = []
    for raw in raw_items if isinstance(raw_items, list) else []:
        if not isinstance(raw, dict):
            continue
        item = dict(raw)
        for field in ("title", "pov_character", "goal", "conflict", "failure_cost", "hook", "plot_arc", "title_promise_progress", "character_arc_progress"):
            item[field] = _text(item.get(field))
        for field in ("beats", "causal_beats", "knowledge_changes", "state_changes", "foreshadow_actions", "forbidden_reveals", "appearing_characters", "appearing_factions", "task_progress"):
            if not isinstance(item.get(field), list):
                item[field] = []
        for field in ("opening_state", "ending_state"):
            if not isinstance(item.get(field), dict):
                item[field] = {}
        try:
            item["chapter_no"] = int(item.get("chapter_no"))
        except (TypeError, ValueError):
            item["chapter_no"] = 0
        try:
            item["story_day"] = int(item.get("story_day")) if item.get("story_day") is not None else None
        except (TypeError, ValueError):
            item["story_day"] = None
        item["phase_key"] = _text(item.get("phase_key"))
        item["time_mode"] = _text(item.get("time_mode")) or "linear"
        item["start_time"] = _text(item.get("start_time"))
        item["end_time"] = _text(item.get("end_time"))
        try:
            item["previous_chapter_no"] = int(item.get("previous_chapter_no")) if item.get("previous_chapter_no") else None
        except (TypeError, ValueError):
            item["previous_chapter_no"] = None
        items.append(item)
    return items


def evaluate_outline(
    work: dict[str, Any],
    items: list[dict[str, Any]],
    chapter_count: int,
    *,
    expected_from_chapter: int = 1,
) -> tuple[list[str], int]:
    issues: list[str] = []
    if len(items) != chapter_count:
        issues.append(f"要求 {chapter_count} 章，实际返回 {len(items)} 章。")
    expected_numbers = list(range(expected_from_chapter, expected_from_chapter + chapter_count))
    if [item.get("chapter_no") for item in items] != expected_numbers:
        issues.append("章节编号不连续或顺序错误。")
    known_names = {_text(item.get("name")) for item in work.get("characters") or []}
    for item in items:
        no = item.get("chapter_no") or "?"
        for field, label in (("title", "标题"), ("goal", "目标"), ("conflict", "冲突"), ("failure_cost", "失败代价"), ("hook", "钩子")):
            if len(_text(item.get(field))) < 4:
                issues.append(f"第{no}章缺少具体{label}。")
        # A volume title such as “第1卷” is a valid, concrete arc label even
        # though it has only three characters.  It must never be judged by the
        # generic prose-length rule used for chapter goals and conflicts.
        if not _text(item.get("plot_arc")):
            issues.append(f"第{no}章缺少所属主线。")
        if known_names and _text(item.get("pov_character")) not in known_names:
            issues.append(f"第{no}章视角人物不在人物小传中。")
        if not item.get("opening_state") or not item.get("ending_state"):
            issues.append(f"第{no}章缺少开场或结尾状态。")
        beats = item.get("beats") or []
        if not 5 <= len(beats) <= 8 or any(len(_text(beat)) < 4 for beat in beats):
            issues.append(f"第{no}章需要5至8个可直接写成场景的情节点。")
        causal = item.get("causal_beats") or []
        if not causal or any(
            not isinstance(beat, dict)
            or not all(_text(beat.get(key)) for key in ("cause", "action", "obstacle", "consequence"))
            for beat in causal
        ):
            issues.append(f"第{no}章因果节点不完整。")
        if item.get("time_mode") not in {"linear", "flashback", "parallel"}:
            issues.append(f"第{no}章缺少有效时间模式。")
        if item.get("story_day") is None or not _text(item.get("phase_key")):
            issues.append(f"第{no}章缺少故事日或故事阶段。")
        if not item.get("appearing_characters"):
            issues.append(f"第{no}章缺少登场人物。")
        if not item.get("task_progress"):
            issues.append(f"第{no}章缺少当前任务推进。")
        if not _text(item.get("character_arc_progress")):
            issues.append(f"第{no}章没有说明人物弧推进。")
    checkpoints = {
        expected_from_chapter,
        expected_from_chapter + chapter_count - 1,
        expected_from_chapter + max(0, (chapter_count - 1) // 2),
    }
    for no in checkpoints:
        item = next((candidate for candidate in items if candidate.get("chapter_no") == no), {})
        if not _text(item.get("title_promise_progress")):
            issues.append(f"第{no}章没有说明如何兑现书名承诺。")
    return issues, max(0, 100 - min(90, len(issues) * 5))


def outline_readiness_issues(work: dict[str, Any]) -> list[str]:
    bible = work.get("story_bible") or {}
    issues = []
    if not _text(bible.get("summary")):
        issues.append("尚未生成故事方案")
    if not _text(bible.get("reader_promise")):
        issues.append("故事方案缺少书名对应的读者承诺")
    if len(work.get("characters") or []) < 2:
        issues.append("人物小传不足")
    if not work.get("plot_arcs"):
        issues.append("卷级主线为空")
    return issues


def _flatten_text(value: Any) -> str:
    if isinstance(value, dict):
        return " ".join(_flatten_text(item) for item in value.values())
    if isinstance(value, list):
        return " ".join(_flatten_text(item) for item in value)
    return _text(value)


def language_risks(value: Any) -> list[str]:
    text = _flatten_text(value)
    risks: list[str] = []
    for pattern in LANGUAGE_RISK_PATTERNS:
        for match in pattern.findall(text):
            risks.append(f"可疑搭配：{match}")
    return list(dict.fromkeys(risks))


def planning_checks(step: str, data: dict[str, Any], context: dict[str, Any] | None = None) -> dict[str, Any]:
    """Return explainable checks; warnings never silently rewrite an author's draft."""
    issues: list[str] = []
    warnings = language_risks(data)
    if step == "contract":
        candidates = data.get("candidates") if isinstance(data.get("candidates"), list) else []
        if len(candidates) < 3:
            issues.append("创作契约需要 3 个短方向供选择")
        for index, item in enumerate(candidates, start=1):
            if not isinstance(item, dict) or len(_flatten_text(item)) < 80:
                issues.append(f"第{index}个创作方向信息不足")
    elif step == "setting":
        bible = data.get("story_bible") if isinstance(data.get("story_bible"), dict) else {}
        for field in ("core_hook", "core_conflict", "world", "stakes", "ending"):
            if len(_text(bible.get(field))) < 20:
                issues.append(f"核心设定缺少可执行的{field}")
    elif step in {"protagonist", "character"}:
        character = compact_character(data.get("character") if isinstance(data.get("character"), dict) else {})
        if _text(character.get("name")) in GENERIC_CHARACTER_NAMES:
            issues.append("人物不能使用主角、反派等占位名")
        for field in ("goal", "conflict", "motivation", "flaw", "character_arc", "voice"):
            if len(_text(character.get(field))) < 12:
                issues.append(f"人物缺少可执行的{field}")
        for issue in _appearance_issues(character.get("appearance")):
            issues.append(issue)
        for issue in _intra_character_duplicate_issues(character):
            issues.append(issue)
        if step == "character" and context:
            existing = []
            for group in (context.get("protagonist", []), context.get("character", [])):
                for artifact in group or []:
                    if isinstance(artifact, dict) and isinstance(artifact.get("character"), dict):
                        existing.append(compact_character(artifact["character"]))
            biography = _text(character.get("biography"))
            for other in existing:
                other_name = _text(other.get("name")) or "已确认人物"
                other_biography = _text(other.get("biography"))
                if biography and other_biography and SequenceMatcher(None, biography, other_biography).ratio() >= 0.72:
                    issues.append(f"人物小传与{other_name}过于相似，请重写独有经历和因果")
                repeated_fields = [
                    field for field in ("goal", "motivation", "flaw", "character_arc", "secret", "voice")
                    if len(_text(character.get(field))) >= 8 and _text(character.get(field)) == _text(other.get(field))
                ]
                if repeated_fields:
                    issues.append(f"与{other_name}重复字段：{'、'.join(repeated_fields)}")
    elif step == "cast_roster":
        characters = data.get("characters") if isinstance(data.get("characters"), list) else []
        if not characters:
            issues.append("角色阵容不能为空")
        if any(_text(item.get("name")) in GENERIC_CHARACTER_NAMES for item in characters if isinstance(item, dict)):
            issues.append("角色阵容不能使用占位名")
    elif step == "arc":
        arc = data.get("arc") if isinstance(data.get("arc"), dict) else {}
        for field in ("title", "goal", "opposition", "turning_point", "ending_state", "synopsis"):
            if len(_text(arc.get(field))) < 12:
                issues.append(f"卷级主线缺少{field}")
    elif step == "summary":
        bible = data.get("story_bible") if isinstance(data.get("story_bible"), dict) else {}
        if len(_text(bible.get("summary"))) < 100:
            issues.append("总梗概过短，无法承接已确认设定")
    return {"blocking": issues, "warnings": warnings, "ok": not issues}


def character_batch_checks(items: dict[str, dict[str, Any]], context: dict[str, Any]) -> dict[str, dict[str, Any]]:
    """Validate every draft and flag similarities introduced within one batch."""
    checks = {
        item_key: planning_checks("character", {"character": character}, context)
        for item_key, character in items.items()
    }
    keys = list(items)
    for index, item_key in enumerate(keys):
        character = items[item_key]
        for other_key in keys[index + 1:]:
            other = items[other_key]
            for field, label in (("biography", "人物小传"), ("appearance", "外貌"), ("personality", "性格"), ("voice", "语言习惯")):
                if _too_similar(character.get(field), other.get(field), threshold=0.72):
                    checks[item_key]["blocking"].append(f"{label}与本批 {other.get('name') or other_key} 过于相似，请重写独有细节")
                    checks[other_key]["blocking"].append(f"{label}与本批 {character.get('name') or item_key} 过于相似，请重写独有细节")
    for result in checks.values():
        result["blocking"] = list(dict.fromkeys(result["blocking"]))
        result["ok"] = not result["blocking"]
    return checks
