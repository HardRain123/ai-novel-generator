import json
import re
from copy import deepcopy
from math import ceil
from typing import Any
from difflib import SequenceMatcher

from app.services.character_cards import compact_character


GENERIC_CHARACTER_NAMES = {"主角", "男主", "女主", "主人公", "关键对手", "反派", "配角", "导师", "朋友"}


def _planning_rule(
    minimum: int | None = None,
    maximum: int | None = None,
    *,
    required: bool = True,
    hard: bool = True,
    value_type: str = "text",
) -> dict[str, Any]:
    rule: dict[str, Any] = {"type": value_type, "required": required, "hard": hard}
    if minimum is not None:
        rule["min"] = minimum
    if maximum is not None:
        rule["max"] = maximum
    return rule


_GENERIC_SHORT_RULE = _planning_rule(20, 100)

# R1-C: this is the single source of truth for the planning editor's field
# ranges.  ``hard`` means planning_checks blocks confirmation; a false value
# is an authoring suggestion only and is deliberately not a server blocker.
PLANNING_FIELD_RULES: dict[str, Any] = {
    "version": "r1c-1",
    "default_rule": deepcopy(_GENERIC_SHORT_RULE),
    "steps": {
        "contract": {
            "title": _planning_rule(4, 80, required=False, hard=False),
            **{
                field: deepcopy(_GENERIC_SHORT_RULE)
                for field in (
                    "target_experience", "protagonist_principle", "power_curve", "payoff_cadence",
                    "power_cost", "moral_boundary", "style_rules", "title_interpretation", "reader_promise",
                )
            },
        },
        "setting": {
            **{field: deepcopy(_GENERIC_SHORT_RULE) for field in ("core_hook", "core_conflict", "world", "stakes", "ending")},
            "style_rules": _planning_rule(10, 240, required=False, hard=False),
        },
        "protagonist": {
            "name": _planning_rule(2, 40, required=False, hard=False),
            "role": _planning_rule(2, 80, required=False, hard=False),
            "biography": _planning_rule(120, 260),
            **{field: deepcopy(_GENERIC_SHORT_RULE) for field in ("goal", "conflict", "motivation", "flaw", "arc", "voice")},
            "personality": deepcopy(_GENERIC_SHORT_RULE) | {"required": False},
        },
        "character": {
            "name": _planning_rule(2, 40, required=False, hard=False),
            "role": _planning_rule(2, 80, required=False, hard=False),
            "biography": _planning_rule(120, 260),
            **{field: deepcopy(_GENERIC_SHORT_RULE) for field in ("goal", "conflict", "motivation", "flaw", "arc", "voice")},
            "personality": deepcopy(_GENERIC_SHORT_RULE) | {"required": False},
        },
        "cast_roster": {
            "name": _planning_rule(2, 40, required=False, hard=False),
            "role": _planning_rule(2, 80, required=False, hard=False),
            "story_function": _planning_rule(8, 160, required=False, hard=False),
            "relationship_to_protagonist": _planning_rule(8, 180, required=False, hard=False),
        },
        "arc": {
            "title": _planning_rule(1, 40),
            "sequence": _planning_rule(required=False, value_type="number"),
            "start_chapter": _planning_rule(required=False, value_type="number"),
            "end_chapter": _planning_rule(required=False, value_type="number"),
            **{field: deepcopy(_GENERIC_SHORT_RULE) for field in ("goal", "opposition", "turning_point", "ending_state")},
            "synopsis": _planning_rule(120, 300),
        },
        "summary": {
            "summary": _planning_rule(180, 350),
            "theme": deepcopy(_GENERIC_SHORT_RULE) | {"required": False},
            "ending": _planning_rule(20, 300, required=False, hard=False),
            "style_rules": deepcopy(_GENERIC_SHORT_RULE) | {"required": False},
        },
    },
}


def planning_field_rules() -> dict[str, Any]:
    """Return a JSON-safe copy so API callers cannot mutate the rule source."""
    return deepcopy(PLANNING_FIELD_RULES)


def _planning_rule_for(step: str | None, field: str) -> dict[str, Any] | None:
    if not step:
        return None
    step_rules = PLANNING_FIELD_RULES["steps"].get(step, {})
    return step_rules.get(field) or PLANNING_FIELD_RULES["default_rule"]
LANGUAGE_RISK_PATTERNS = (
    re.compile(r"(?:烧|燃烧|吞噬|撕裂|点燃).{0,8}(?:存活率|指标|概率|数值|风险值)"),
    re.compile(r"(?:赋能|抓手|闭环|颗粒度|对齐|拉满).{0,8}(?:命运|情绪|人生|关系)"),
    re.compile(r"拉林守诚修暖|转守仓对峙"),
    re.compile(r"(?<![A-Za-z])[A-Z]{2,8}(?![A-Za-z])"),
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
            risks.append(f"语言表达疑似不自然：{match}")
    return list(dict.fromkeys(risks))


def _length_issue(issues: list[str], label: str, value: Any, minimum: int, maximum: int) -> None:
    length = len(_text(value))
    if length < minimum:
        issues.append(f"{label}过短，应为{minimum}—{maximum}字")
    elif length > maximum:
        issues.append(f"{label}过长，应为{minimum}—{maximum}字")


def _check_short_fields(
    issues: list[str],
    values: dict[str, Any],
    labels: dict[str, str],
    *,
    step: str | None = None,
    minimum: int = 20,
    maximum: int = 100,
    required: bool = True,
) -> None:
    for field, label in labels.items():
        value = values.get(field)
        rule = _planning_rule_for(step, field)
        field_minimum = int(rule.get("min", minimum)) if rule else minimum
        field_maximum = int(rule.get("max", maximum)) if rule else maximum
        if required or _text(value):
            _length_issue(issues, label, value, field_minimum, field_maximum)


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
            elif isinstance(item, dict):
                _check_short_fields(
                    issues,
                    item,
                    {
                        "target_experience": f"第{index}个方向的读者体验",
                        "protagonist_principle": f"第{index}个方向的主角原则",
                        "power_curve": f"第{index}个方向的成长曲线",
                        "payoff_cadence": f"第{index}个方向的回报节奏",
                        "power_cost": f"第{index}个方向的能力代价",
                        "moral_boundary": f"第{index}个方向的道德边界",
                        "style_rules": f"第{index}个方向的文风规则",
                        "title_interpretation": f"第{index}个方向的书名解读",
                        "reader_promise": f"第{index}个方向的读者承诺",
                    },
                    step="contract",
                    required=False,
                )
    elif step == "setting":
        bible = data.get("story_bible") if isinstance(data.get("story_bible"), dict) else {}
        _check_short_fields(
            issues,
            bible,
            {
                "core_hook": "核心钩子",
                "core_conflict": "核心冲突",
                "world": "世界规则",
                "stakes": "失败代价",
                "ending": "结局方向",
            },
            step="setting",
        )
    elif step in {"protagonist", "character"}:
        character = compact_character(data.get("character") if isinstance(data.get("character"), dict) else {})
        if _text(character.get("name")) in GENERIC_CHARACTER_NAMES:
            issues.append("人物不能使用主角、反派等占位名")
        biography_rule = _planning_rule_for(step, "biography") or _GENERIC_SHORT_RULE
        _length_issue(
            issues,
            "人物小传",
            character.get("biography"),
            int(biography_rule["min"]),
            int(biography_rule["max"]),
        )
        _check_short_fields(
            issues,
            character,
            {
                "goal": "人物目标",
                "conflict": "人物阻力",
                "motivation": "人物动机",
                "flaw": "人物缺陷",
                "character_arc": "人物弧",
                "voice": "语言行动特征",
            },
            step=step,
        )
        _check_short_fields(issues, character, {"personality": "人物性格"}, step=step, required=False)
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
        protagonist_names = set()
        for artifact in (context or {}).get("protagonist", []) or []:
            if not isinstance(artifact, dict) or not isinstance(artifact.get("character"), dict):
                continue
            name = _normalized_text(artifact["character"].get("name"))
            if name:
                protagonist_names.add(name)
        for item in characters:
            if not isinstance(item, dict):
                continue
            name = _text(item.get("name"))
            if name and _normalized_text(name) in protagonist_names:
                issues.append(f"角色阵容不能包含主角“{name}”，请移除该人物")
    elif step == "arc":
        arc = data.get("arc") if isinstance(data.get("arc"), dict) else {}
        title_rule = _planning_rule_for(step, "title") or {"min": 1, "max": 40}
        title_length = len(_text(arc.get("title")))
        title_minimum = int(title_rule["min"])
        title_maximum = int(title_rule["max"])
        if not title_minimum <= title_length <= title_maximum:
            issues.append(f"卷标题不能为空且不能超过{title_maximum}字")
        _check_short_fields(
            issues,
            arc,
            {
                "goal": "卷目标",
                "opposition": "卷级阻力",
                "turning_point": "卷转折",
                "ending_state": "卷结束状态",
            },
            step="arc",
        )
        synopsis_rule = _planning_rule_for(step, "synopsis") or _GENERIC_SHORT_RULE
        _length_issue(
            issues,
            "卷梗概",
            arc.get("synopsis"),
            int(synopsis_rule["min"]),
            int(synopsis_rule["max"]),
        )
    elif step == "summary":
        bible = data.get("story_bible") if isinstance(data.get("story_bible"), dict) else {}
        if not bible:
            candidates = data.get("candidates") if isinstance(data.get("candidates"), list) else []
            if len(candidates) == 1 and isinstance(candidates[0], dict) and isinstance(candidates[0].get("story_bible"), dict):
                bible = candidates[0]["story_bible"]
        summary_rule = _planning_rule_for(step, "summary") or _GENERIC_SHORT_RULE
        _length_issue(
            issues,
            "总梗概",
            bible.get("summary"),
            int(summary_rule["min"]),
            int(summary_rule["max"]),
        )
        _check_short_fields(
            issues,
            bible,
            {"theme": "主题", "style_rules": "文风规则"},
            step="summary",
            required=False,
        )
    return {"blocking": issues, "warnings": warnings, "ok": not issues}


def _context_records(value: Any, path: str) -> list[tuple[dict[str, Any], str]]:
    if isinstance(value, dict):
        return [(value, path)]
    if isinstance(value, list):
        return [
            (item, f"{path}[{index}]")
            for index, item in enumerate(value)
            if isinstance(item, dict)
        ]
    return []


def _context_section_records(context: dict[str, Any], section: str) -> list[tuple[dict[str, Any], str]]:
    records: list[tuple[dict[str, Any], str]] = []
    for key in (section, f"{section}s"):
        if key in context:
            records.extend(_context_records(context[key], f"context.{key}"))
    return records


def _nested_section(record: dict[str, Any], section: str) -> dict[str, Any]:
    direct = record.get(section)
    if isinstance(direct, dict):
        return direct
    selected = record.get("selected")
    if isinstance(selected, dict) and isinstance(selected.get(section), dict):
        return selected[section]
    candidates = record.get("candidates")
    if isinstance(candidates, list):
        for candidate in candidates:
            if isinstance(candidate, dict) and isinstance(candidate.get(section), dict):
                return candidate[section]
    if section == "character" and "name" in record:
        return record
    if section == "story_bible" and any(key in record for key in ("summary", "ending", "world")):
        return record
    return {}


def _planning_character_records(context: dict[str, Any], section: str) -> list[tuple[dict[str, Any], str]]:
    records: list[tuple[dict[str, Any], str]] = []
    for record, path in _context_section_records(context, section):
        character = _nested_section(record, "character")
        if character:
            records.append((character, f"{path}.character" if "character" in record else path))
    return records


def _planning_story_records(context: dict[str, Any], section: str) -> list[tuple[dict[str, Any], str]]:
    records: list[tuple[dict[str, Any], str]] = []
    for record, path in _context_section_records(context, section):
        story_bible = _nested_section(record, "story_bible")
        if story_bible:
            records.append((story_bible, f"{path}.story_bible" if "story_bible" in record else path))
    return records


def _planning_roster_records(context: dict[str, Any]) -> list[tuple[dict[str, Any], str]]:
    records: list[tuple[dict[str, Any], str]] = []
    for record, path in _context_section_records(context, "cast_roster"):
        characters = record.get("characters")
        if isinstance(characters, list):
            records.extend(
                (item, f"{path}.characters[{index}]")
                for index, item in enumerate(characters)
                if isinstance(item, dict)
            )
    for record, path in _context_section_records(context, "characters"):
        records.append((record, path))
    return records


def _planning_arc_records(context: dict[str, Any]) -> list[tuple[dict[str, Any], str]]:
    records: list[tuple[dict[str, Any], str]] = []
    for section in ("arc", "arcs", "plot_arcs", "volumes", "story_volumes"):
        for record, path in _context_section_records(context, section):
            arc = _nested_section(record, "arc")
            records.append((arc or record, f"{path}.arc" if arc and "arc" in record else path))
    return records


def _walk_mappings(value: Any, path: str = "context") -> list[tuple[dict[str, Any], str]]:
    found: list[tuple[dict[str, Any], str]] = []
    if isinstance(value, dict):
        found.append((value, path))
        for key, child in value.items():
            found.extend(_walk_mappings(child, f"{path}.{key}"))
    elif isinstance(value, list):
        for index, child in enumerate(value):
            found.extend(_walk_mappings(child, f"{path}[{index}]"))
    return found


def _named_mappings(context: dict[str, Any], pattern: re.Pattern[str]) -> list[tuple[dict[str, Any], str]]:
    found: list[tuple[dict[str, Any], str]] = []
    for mapping, path in _walk_mappings(context):
        for key, value in mapping.items():
            if not pattern.search(str(key)):
                continue
            if isinstance(value, dict):
                found.append((value, f"{path}.{key}"))
            elif isinstance(value, list):
                found.extend(
                    (item, f"{path}.{key}[{index}]")
                    for index, item in enumerate(value)
                    if isinstance(item, dict)
                )
            else:
                found.append(({str(key): value}, f"{path}.{key}"))
    return found


def _field_value(mapping: dict[str, Any], aliases: tuple[str, ...]) -> Any:
    available = [(str(key).casefold(), value) for key, value in mapping.items() if value not in (None, "", [], {})]
    for alias in aliases:
        alias = alias.casefold()
        for normalized_key, value in available:
            if normalized_key == alias:
                return value
    for alias in aliases:
        alias = alias.casefold()
        if len(alias) < 3:
            continue
        for normalized_key, value in available:
            if alias in normalized_key and not any(
                other != alias and alias in other and other in normalized_key for other in aliases
            ):
                return value
    return None


def _value_tokens(value: Any) -> set[str]:
    if isinstance(value, dict):
        for key in ("name", "title", "id", "value"):
            if value.get(key):
                return _value_tokens(value[key])
        return set()
    if isinstance(value, (list, tuple, set)):
        result: set[str] = set()
        for item in value:
            result.update(_value_tokens(item))
        return result
    return {
        token.casefold()
        for token in re.split(r"[,，、;；/\\|\n]+", _text(value))
        if token.strip()
    }


def _normalized_value(value: Any) -> str:
    if isinstance(value, bool):
        return str(value).casefold()
    if isinstance(value, (dict, list, tuple, set)):
        return _normalized_text(json.dumps(value, ensure_ascii=False, sort_keys=True, default=str))
    return _normalized_text(value).casefold()


def _shared_ending_marker(ending: str, arc_text: str) -> bool:
    ending = _normalized_text(ending)
    arc_text = _normalized_text(arc_text)
    if not ending or not arc_text:
        return False
    if ending in arc_text:
        return True
    if re.search(r"[\u4e00-\u9fff]", ending):
        run = "".join(re.findall(r"[\u4e00-\u9fff]", ending))
        marker_lengths = (4, 3, 2)
        return any(
            len(run) >= length and any(run[index:index + length] in arc_text for index in range(len(run) - length + 1))
            for length in marker_lengths
        )
    words = re.findall(r"[A-Za-z0-9]{3,}", ending.casefold())
    return any(word in arc_text.casefold() for word in words)


def planning_consistency_checks(context: dict[str, Any]) -> dict[str, Any]:
    """Inspect confirmed planning context without changing any author content."""
    blocking: list[str] = []
    warnings: list[str] = []
    evidence: list[dict[str, str]] = []
    suggestions: list[str] = []

    def add_issue(severity: str, message: str, path: str, suggestion: str) -> None:
        target = blocking if severity == "blocking" else warnings
        if message not in target:
            target.append(message)
        entry = {"severity": severity, "message": message, "path": path, "suggestion": suggestion}
        if entry not in evidence:
            evidence.append(entry)
        if suggestion and suggestion not in suggestions:
            suggestions.append(suggestion)

    context = context if isinstance(context, dict) else {}
    protagonists = _planning_character_records(context, "protagonist")
    roster = _planning_roster_records(context)
    side_cards = _planning_character_records(context, "character")

    def check_duplicate_group(entries: list[tuple[dict[str, Any], str]], label: str) -> None:
        seen: dict[str, tuple[dict[str, Any], str]] = {}
        seen_identities: dict[str, tuple[dict[str, Any], str]] = {}
        for character, path in entries:
            name = _normalized_text(character.get("name"))
            if not name:
                continue
            identity = _normalized_text(
                character.get("stable_id") or character.get("character_id") or character.get("id") or ""
            )
            if identity and identity in seen_identities:
                previous, previous_path = seen_identities[identity]
                previous_name = _normalized_text(previous.get("name"))
                if previous_name != name:
                    add_issue(
                        "blocking",
                        f"{label}中的稳定身份“{identity}”对应多个姓名",
                        f"{previous_path}.id 与 {path}.id",
                        "为每个稳定身份保留唯一姓名，或为不同人物分配不同的稳定身份标识。",
                    )
            elif identity:
                seen_identities[identity] = (character, path)
            if name in seen:
                previous, previous_path = seen[name]
                add_issue(
                    "blocking",
                    f"{label}出现重复人物“{character.get('name') or previous.get('name', '')}”",
                    f"{previous_path}.name 与 {path}.name",
                    "合并同一人物的记录，或为不同人物补充稳定且唯一的姓名与身份。",
                )
            else:
                seen[name] = (character, path)

    check_duplicate_group(roster, "角色阵容")
    check_duplicate_group(side_cards, "人物小传")
    protagonist_names = {
        _normalized_text(character.get("name"))
        for character, _ in protagonists
        if _normalized_text(character.get("name"))
    }
    for character, path in roster:
        name = _normalized_text(character.get("name"))
        if name in protagonist_names:
            add_issue(
                "blocking",
                f"重复主角“{character.get('name', '')}”同时出现在主角和角色阵容中",
                f"{path}.name",
                "从角色阵容移除主角，保留主角小传作为唯一主角记录。",
            )
    for character, path in side_cards:
        name = _normalized_text(character.get("name"))
        if name in protagonist_names:
            add_issue(
                "blocking",
                f"重复主角“{character.get('name', '')}”同时出现在主角和人物小传中",
                f"{path}.name",
                "不要为主角生成配角人物小传，或明确区分该人物的稳定身份。",
            )

    arc_records = _planning_arc_records(context)
    sequences: list[tuple[int, str]] = []
    for arc, path in arc_records:
        sequence = _field_value(arc, ("sequence", "卷序", "顺序"))
        if isinstance(sequence, (int, float)) or str(sequence).isdigit():
            sequences.append((int(sequence), path))
        start = _field_value(arc, ("start_chapter", "chapter_start", "开始章节", "起始章节"))
        end = _field_value(arc, ("end_chapter", "chapter_end", "结束章节", "终止章节"))
        if str(start).isdigit() and str(end).isdigit() and int(start) > int(end):
            add_issue(
                "blocking",
                f"卷级时间范围倒置：起始章节 {start} 晚于结束章节 {end}",
                f"{path}.start_chapter / {path}.end_chapter",
                "重新核对本卷的章节范围，确保时间线从起点推进到终点。",
            )
    for (previous, previous_path), (current, current_path) in zip(sequences, sequences[1:]):
        if current < previous:
            add_issue(
                "blocking",
                f"卷级时间线倒退：第{previous}卷之后出现第{current}卷",
                f"{previous_path}.sequence -> {current_path}.sequence",
                "按故事发生顺序重排卷序，或明确补充跨卷倒叙的时间依据。",
            )

    rule_pattern = re.compile(r"^(?:rules?|constraints?|world_rules?|规则|约束)$", re.IGNORECASE)
    for mapping, path in _walk_mappings(context):
        for key, value in mapping.items():
            if not rule_pattern.search(str(key)) or not isinstance(value, dict):
                continue
            allowed = _field_value(value, ("allowed", "permitted", "can", "允许", "可用"))
            forbidden = _field_value(value, ("forbidden", "prohibited", "cannot", "禁止", "不能"))
            overlap = _value_tokens(allowed) & _value_tokens(forbidden)
            if overlap:
                add_issue(
                    "blocking",
                    f"规则直接冲突：{', '.join(sorted(overlap))} 同时被允许和禁止",
                    f"{path}.{key}",
                    "保留唯一的规则结论，并说明例外条件或适用范围。",
                )

    medical_pattern = re.compile(r"(?:medical|disease|illness|medicine|medication|treatment|symptom|疾病|病症|药物|用药|治疗|症状)", re.IGNORECASE)
    medical_seen: set[str] = set()
    for mapping, path in _walk_mappings(context):
        keys = [str(key) for key in mapping]
        if not any(medical_pattern.search(key) for key in keys):
            continue
        if path in medical_seen:
            continue
        medical_seen.add(path)
        has_condition = any(re.search(r"disease|illness|疾病|病症|症状", key, re.IGNORECASE) for key in keys)
        has_treatment = any(re.search(r"medicine|medication|treatment|药物|用药|治疗", key, re.IGNORECASE) for key in keys)
        purpose = _field_value(mapping, ("purpose", "indication", "用途", "目的", "适应症", "治疗目标"))
        if (has_condition or has_treatment) and not purpose:
            add_issue(
                "warning",
                "疾病、药物或治疗缺少明确的治疗目的，医学因果链需要复核",
                path,
                "补充疾病与治疗手段的对应关系、目的和限制，避免把治疗效果写成无条件成立。",
            )
        treats = _value_tokens(_field_value(mapping, ("treats", "治疗", "可治疗")))
        cannot_treat = _value_tokens(_field_value(mapping, ("cannot_treat", "不能治疗", "禁忌")))
        if treats & cannot_treat:
            add_issue(
                "warning",
                "同一治疗手段同时被写成可治疗和不可治疗，医学设定存在可疑冲突",
                path,
                "补充适用条件、剂量或例外来源，明确两种表述的边界。",
            )

    cheat_pattern = re.compile(r"(?:goldfinger|cheat|power_system|外挂|金手指|异能|系统)", re.IGNORECASE)
    cheat_records = _named_mappings(context, cheat_pattern)
    cheat_values: dict[str, set[str]] = {"代价": set(), "使用者": set(), "作用范围": set(), "可携带性": set()}
    cheat_aliases = {
        "代价": ("cost", "price", "代价"),
        "使用者": ("user", "owner", "使用者", "持有者"),
        "作用范围": ("scope", "range", "作用范围", "范围"),
        "可携带性": ("portable", "portability", "可携带", "可转移", "转移"),
    }
    for mapping, path in cheat_records:
        missing = [label for label, aliases in cheat_aliases.items() if _field_value(mapping, aliases) is None]
        if missing:
            add_issue(
                "warning",
                f"金手指约束缺少：{'、'.join(missing)}",
                path,
                "在契约或设定中补齐金手指的代价、使用者、作用范围和可携带性。",
            )
        for label, aliases in cheat_aliases.items():
            value = _field_value(mapping, aliases)
            if value is not None:
                cheat_values[label].add(_normalized_value(value))
    for label, values in cheat_values.items():
        if len(values) > 1:
            add_issue(
                "blocking",
                f"金手指的{label}在不同步骤中不一致",
                "context.setting / context.contract",
                f"统一金手指的{label}，并让所有卷级主线引用同一版本。",
            )

    antagonist_pattern = re.compile(r"(?:antagonist|villain|opponent|反派|对手)", re.IGNORECASE)
    for mapping, path in _named_mappings(context, antagonist_pattern):
        resources = _field_value(mapping, ("resource", "resources", "资源", "筹码"))
        position = _field_value(mapping, ("position", "rank", "职位", "地位"))
        control = _field_value(mapping, ("control", "power", "influence", "控制", "势力", "权限"))
        cause = _field_value(mapping, ("cause", "because", "source", "support", "因果", "来源", "凭借"))
        missing = []
        if resources in (None, "", [], {}):
            missing.append("资源")
        if position in (None, "", [], {}):
            missing.append("职位/位置")
        if control in (None, "", [], {}):
            missing.append("控制力")
        if cause in (None, "", [], {}):
            missing.append("因果来源")
        if missing:
            add_issue(
                "warning",
                f"对手设定缺少资源、位置或控制力的因果支撑：{'、'.join(missing)}",
                path,
                "补充对手如何获得资源、占据位置并形成实际控制力的因果链。",
            )

    twist_pattern = re.compile(r"(?:twist|turning_point|turning|转折|反转|关键节点)", re.IGNORECASE)
    for mapping, path in _named_mappings(context, twist_pattern):
        required = _value_tokens(_field_value(mapping, ("required_resources", "required_resource", "prerequisite", "前置资源", "所需资源")))
        available = _value_tokens(_field_value(mapping, ("available_resources", "obtained_resources", "resources", "已获得资源", "现有资源")))
        missing = required - available
        if missing:
            add_issue(
                "blocking",
                f"转折使用尚未获得的资源：{'、'.join(sorted(missing))}",
                path,
                "把资源获取节点前置到转折之前，或调整转折所需条件。",
            )

    story_records = (
        _planning_story_records(context, "setting")
        + _planning_story_records(context, "summary")
        + _context_records(context.get("story_bible"), "context.story_bible")
    )
    for story, path in story_records:
        ending = _field_value(story, ("ending", "finale", "结局", "结局方向"))
        if not ending:
            continue
        arc_text = " ".join(_flatten_text(arc) for arc, _ in arc_records)
        if not arc_records or not _shared_ending_marker(_text(ending), arc_text):
            add_issue(
                "warning",
                "全局结局尚未在卷级主线中找到可追踪的实现路径",
                f"{path}.ending",
                "在至少一卷的目标、转折或结束状态中落下结局所需的关键行动和回报。",
            )

    contract_records = _context_section_records(context, "contract")
    style_values = []
    for record, _ in contract_records:
        selected = record.get("selected") if isinstance(record.get("selected"), dict) else record
        if isinstance(selected, dict) and selected.get("style_rules"):
            style_values.append(selected["style_rules"])
    for story, _ in _planning_story_records(context, "summary"):
        if story.get("style_rules"):
            style_values.append(story["style_rules"])
    explicit_style = _field_value(context, ("writing_style", "style_requirements", "writing_style_requirements", "文风要求"))
    if not style_values and (explicit_style or context):
        add_issue(
            "warning",
            "文风要求没有体现在创作契约或文风规则中",
            "context.contract.style_rules / context.summary.style_rules",
            "把文风要求写入创作契约的 style_rules，并在总梗概的文风规则中保持一致。",
        )

    blocking = list(dict.fromkeys(blocking))
    warnings = list(dict.fromkeys(warnings))
    return {
        "blocking": blocking,
        "warnings": warnings,
        "evidence": evidence,
        "suggestions": suggestions,
        "ok": not blocking,
    }


def _coverage_records(context: dict[str, Any], keys: tuple[str, ...]) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    sources = [context]
    if isinstance(context.get("planning"), dict):
        sources.append(context["planning"])
    for source in sources:
        for key in keys:
            value = source.get(key)
            if isinstance(value, dict):
                value = [value]
            if not isinstance(value, list):
                continue
            for item in value:
                if not isinstance(item, dict):
                    continue
                if key in {"arc", "arcs", "plot_arcs"} and isinstance(item.get("arc"), dict):
                    records.append(item["arc"])
                else:
                    records.append(item)
    return records


def _coverage_find_value(context: dict[str, Any], aliases: tuple[str, ...]) -> Any:
    for mapping, _path in _walk_mappings(context):
        value = _field_value(mapping, aliases)
        if value is not None:
            return value
    return None


def _coverage_story_text(context: dict[str, Any]) -> str:
    values: list[Any] = []
    for key in ("story_bible", "summary", "setting"):
        value = context.get(key)
        if value not in (None, "", [], {}):
            values.append(value)
    for story, _path in _planning_story_records(context, "setting") + _planning_story_records(context, "summary"):
        values.append(story)
    return _flatten_text(values)


def _coverage_summary_scope(context: dict[str, Any], story_text: str) -> str:
    explicit = _coverage_find_value(context, ("summary_scope", "outline_scope", "梗概范围", "摘要范围"))
    explicit_text = _text(explicit).casefold()
    if any(marker in explicit_text for marker in ("current_volume", "volume", "当前卷", "本卷", "卷级")):
        return "current_volume"
    if any(marker in explicit_text for marker in ("full_book", "book", "全书", "整本")):
        return "full_book"
    if re.search(r"当前卷|本卷|卷梗概|本阶段", story_text):
        return "current_volume"
    if re.search(r"全书|整本|总梗概|从起点到结局", story_text):
        return "full_book"
    return "unknown"


def _coverage_bool(context: dict[str, Any], aliases: tuple[str, ...]) -> bool:
    value = _coverage_find_value(context, aliases)
    if isinstance(value, bool):
        return value
    return _text(value).casefold() in {"1", "true", "yes", "y", "是", "单卷完结", "single_volume"}


def _coverage_interval_total(records: list[dict[str, Any]]) -> int:
    intervals: list[tuple[int, int]] = []
    for record in records:
        start = _field_value(record, ("start_chapter", "chapter_start", "起始章节", "开始章节"))
        end = _field_value(record, ("end_chapter", "chapter_end", "终止章节", "结束章节"))
        if str(start).isdigit() and str(end).isdigit() and int(end) >= int(start):
            intervals.append((int(start), int(end)))
    if not intervals:
        return 0
    intervals.sort()
    total = 0
    current_start, current_end = intervals[0]
    for start, end in intervals[1:]:
        if start > current_end + 1:
            total += current_end - current_start + 1
            current_start, current_end = start, end
        else:
            current_end = max(current_end, end)
    return total + current_end - current_start + 1


def planning_coverage_checks(work: dict[str, Any], planning: dict[str, Any] | None = None) -> dict[str, Any]:
    """Report long-form coverage without silently expanding or rewriting a plan."""
    context = dict(work) if isinstance(work, dict) else {}
    if isinstance(planning, dict):
        context.update(planning)

    estimated_words = int(_coverage_find_value(context, ("estimated_words", "目标字数", "预计字数")) or 0)
    average_chapter_words = int(_coverage_find_value(context, ("average_chapter_words", "平均章字数")) or 2500)
    average_chapter_words = max(800, average_chapter_words)
    target_chapters = int(_coverage_find_value(context, ("target_chapter_count", "total_target_chapters", "目标章节数")) or 0)
    if target_chapters <= 0 and estimated_words > 0:
        target_chapters = ceil(estimated_words / average_chapter_words)
    if estimated_words <= 0 and target_chapters > 0:
        estimated_words = target_chapters * average_chapter_words

    arcs = _coverage_records(context, ("plot_arcs", "arc", "arcs"))
    volumes = _coverage_records(context, ("story_volumes", "volumes"))
    planned_records = volumes or arcs
    volume_count = len(planned_records)
    suggested_volume_count = max(1, min(20, ceil(estimated_words / 25000))) if estimated_words else max(1, ceil(target_chapters / 12))
    explicit_chapters = _coverage_interval_total(planned_records)
    summary_text = _coverage_story_text(context)
    summary_scope = _coverage_summary_scope(context, summary_text)
    if explicit_chapters:
        planned_chapters = explicit_chapters
        chapter_estimate = "explicit_ranges"
    elif planned_records and summary_scope == "full_book" and target_chapters:
        planned_chapters = target_chapters
        chapter_estimate = "full_book_target"
    else:
        planned_chapters = 0
        chapter_estimate = "not_declared"

    target_words = sum(
        int(_field_value(record, ("target_words", "planned_words", "目标字数", "计划字数")) or 0)
        for record in planned_records
    )
    planned_words = target_words or planned_chapters * average_chapter_words
    coverage_ratio = round(min(1.0, planned_chapters / target_chapters), 3) if target_chapters else 0.0
    all_text = " ".join([summary_text, _flatten_text(arcs), _flatten_text(volumes)])
    opening_planned = bool(re.search(r"起点|开篇|开局|开场|异常事件|opening|inciting", all_text, re.IGNORECASE))
    escalation_planned = bool(re.search(r"升级|加码|力量关系变化|反制|升级冲突|escalat|power shift", all_text, re.IGNORECASE))
    midpoint_planned = bool(re.search(r"中点|中段|中期|midpoint|mid-point", all_text, re.IGNORECASE))
    low_point_planned = bool(re.search(r"最低谷|低谷|最低点|low[_ -]?point|lowest", all_text, re.IGNORECASE))
    final_reckoning_planned = bool(re.search(r"最终清算|最终决战|最终对决|最终选择|终局|大结局|结局|final[_ -]?(reckoning|battle|e|confrontation)", all_text, re.IGNORECASE))
    confrontation_ending = bool(re.search(r"对峙|对决|决战|正面对抗|最终冲突|confrontation|final battle", _flatten_text(planned_records), re.IGNORECASE))
    single_volume_complete = _coverage_bool(
        context,
        ("single_volume_complete", "single_volume", "single_volume_finished", "单卷完结", "单卷完成"),
    )

    blocking: list[str] = []
    warnings: list[str] = []
    evidence: list[dict[str, str]] = []
    suggestions: list[str] = []

    def add_issue(severity: str, message: str, path: str, suggestion: str) -> None:
        target = blocking if severity == "blocking" else warnings
        if message not in target:
            target.append(message)
        entry = {"severity": severity, "message": message, "path": path, "suggestion": suggestion}
        if entry not in evidence:
            evidence.append(entry)
        if suggestion and suggestion not in suggestions:
            suggestions.append(suggestion)

    if volume_count < suggested_volume_count:
        add_issue(
            "warning",
            f"当前规划覆盖 {volume_count} 卷，按目标篇幅建议约 {suggested_volume_count} 卷",
            "context.plot_arcs / context.story_volumes",
            "补充中后段卷级主线，或明确说明这是当前卷规划而非全书规划。",
        )
    if target_chapters and planned_chapters < target_chapters:
        add_issue(
            "warning",
            f"当前规划约覆盖 {planned_chapters} 章，低于全书目标 {target_chapters} 章",
            "context.target_chapter_count / context.story_volumes",
            "补齐卷级章节范围，确保主线覆盖从开篇到最终清算的全书区间。",
        )
    if not midpoint_planned:
        add_issue("warning", "尚未明确全书中点或中段力量关系变化", "context.story_bible / context.plot_arcs", "补充中点事件、力量关系变化及其不可逆后果。")
    if not low_point_planned:
        add_issue("warning", "尚未明确全书最低谷", "context.story_bible / context.plot_arcs", "补充主角损失最大、旧方案失效且必须重新选择的最低谷。")
    if not final_reckoning_planned:
        add_issue("warning", "尚未明确最终清算或终局结算", "context.story_bible / context.plot_arcs", "补充最终冲突、代价兑现和人物关系的结算方式。")
    if summary_scope == "current_volume":
        add_issue("warning", "summary 被识别为当前卷梗概，不是全书总梗概", "context.summary.story_bible.summary", "保留当前卷梗概，同时补充包含中点、最低谷和最终清算的全书总梗概。")
    elif summary_scope == "unknown":
        add_issue("warning", "无法判断 summary 是全书总梗概还是当前卷梗概", "context.summary.story_bible.summary", "显式标注 summary_scope，避免把当前卷内容误当作全书规划。")

    long_form_target = estimated_words >= 100000 or target_chapters >= 40
    if long_form_target and volume_count == 1 and confrontation_ending and summary_scope == "full_book" and not single_volume_complete:
        add_issue(
            "blocking",
            "10万字级全书只有一条以对峙收束的卷级主线，不能标记为完整全书规划",
            "context.estimated_words / context.plot_arcs[0] / context.summary.story_bible.summary",
            "补充中点、最低谷和最终清算对应的后续卷，或明确选择“单卷完结”。",
        )

    full_book_ready = bool(
        summary_scope == "full_book"
        and midpoint_planned
        and low_point_planned
        and final_reckoning_planned
        and (coverage_ratio >= 1 or single_volume_complete)
        and (volume_count >= suggested_volume_count or single_volume_complete)
        and not blocking
    )
    return {
        "blocking": list(dict.fromkeys(blocking)),
        "warnings": list(dict.fromkeys(warnings)),
        "evidence": evidence,
        "suggestions": suggestions,
        "ok": not blocking,
        "full_book_ready": full_book_ready,
        "coverage": {
            "estimated_words": estimated_words,
            "average_chapter_words": average_chapter_words,
            "target_chapters": target_chapters,
            "suggested_volume_count": suggested_volume_count,
            "planned_volume_count": volume_count,
            "planned_chapters": planned_chapters,
            "planned_words": planned_words,
            "coverage_ratio": coverage_ratio,
            "chapter_estimate": chapter_estimate,
            "opening_planned": opening_planned,
            "escalation_planned": escalation_planned,
            "midpoint_planned": midpoint_planned,
            "low_point_planned": low_point_planned,
            "final_reckoning_planned": final_reckoning_planned,
            "summary_scope": summary_scope,
            "single_volume_complete": single_volume_complete,
        },
    }


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
