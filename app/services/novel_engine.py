"""AI 小说三角色引擎：故事导演、小说作者、责任编辑。

没有配置 LLM 时使用可重复的本地 fallback，保证产品原型可以先跑通；
配置 LLM_API_KEY 后切换到 OpenAI-compatible API（默认 DeepSeek）。
"""

import json
import hashlib
import logging
import re
import shutil
import subprocess
import tempfile
from pathlib import Path
from typing import Any, TypedDict

from app.config import LLM_API_KEY, LLM_BASE_URL, LLM_MODEL, LLM_TIMEOUT_SECONDS
from app.services.context_builder import build_context

from langgraph.graph import END, StateGraph

logger = logging.getLogger(__name__)
STATE_EXTRACTOR_VERSION = "v2"
STATE_FIELDS = {
    "location",
    "goal",
    "emotion",
    "relationship",
    "possession",
    "physical_state",
    "knowledge",
    "secret_exposed",
    "conflict",
    "faction",
}

PROMPT_VERSION = "novel-writing-v2"

SETUP_SYSTEM_PROMPT = """你是一名长篇中文类型小说的故事架构师。请建立可长期执行的故事档案，而不是写宣传文案。

要求：
1. 明确主角的外部目标、内在缺口、现实阻力和必须付出的代价。
2. 世界规则必须能影响人物选择；不要堆砌与剧情无关的设定。
3. 为每个主要人物写清目标、冲突、已知信息、隐藏信息、行为边界和说话/行动倾向。
4. 主线必须有因果链：事件为什么发生、人物为什么介入、失败会失去什么。
5. 结局方向必须能由前期选择逐步推导，不能只写抽象主题。
6. 文风规则必须可执行，描述节奏、叙事视角、对白特点和应减少的表达习惯。
7. 所有设定服务于后续章节写作，避免“宏大但不可写”的空泛设定。
"""

OUTLINE_SYSTEM_PROMPT = """你是一名长篇中文类型小说的故事导演。请把故事档案拆成可以连续写作的章节合同。

每章必须回答：上一章结束时人物处于什么状态？本章为什么从这里开始？人物采取什么行动？阻力如何产生？行动造成什么不可逆后果？下一章从哪个具体状态接续？

要求：
1. 以卷级主线和已确认事实为约束，不为了凑章节数制造无因事件。
2. 每个 beat 都要包含前因、行动、阻力和结果，结果必须推动下一步。
3. 标明人物知识变化，禁止让人物提前知道尚未获得的信息。
4. 标明本章推进、埋设、误导或回收的伏笔，禁止提前揭露后续秘密。
5. 每章必须有 opening_state 和 ending_state，便于正文连续承接。
6. 结尾钩子必须是具体的新局面、发现、选择或代价，不写空泛预告。
"""

CHAPTER_SYSTEM_PROMPT = """你是一名成熟的中文类型小说作者。你的任务是把章节合同写成正在发生的场景，而不是解释、概括或评价故事。

事实优先级：已确认的世界规则和人物事实 > 生成本章之前的状态快照和时间线 > 当前章节合同 > 作者本次补充指令 > 文风偏好。发生冲突时保留已确认事实，并在 continuity_warnings 中说明，不能自行圆谎。

连续性规则：
1. 从 opening_state 的时间、地点、动作和人物状态自然接续，不重新开场，不重复上一章摘要。
2. 人物只能使用已经获得的信息；物品、伤势、关系、承诺、怀疑和未完成行动必须产生后果。
3. 每个主要情节节点都必须由前一个动作、信息或选择触发，不能为了完成大纲突然跳转。
4. 未经本章合同允许，不提前揭露秘密，不提前完成后续章节事件。
5. 结尾必须形成具体的新局面，并与 ending_state 和 next_chapter_boundary 对齐。

自然表达规则：
1. 严格使用指定视角，只写视角人物能够感知、回忆或合理推断的信息。
2. 优先用动作、对白、物件和环境反馈表现情绪，减少“他意识到/他明白/他感到”的解释。
3. 对话允许停顿、打断、回避、误解和答非所问；人物不轮流发表完整观点。
4. 背景信息在人物确实需要时自然出现，不集中讲解设定。
5. 句式和段落长度自然变化，不机械排比，不堆叠形容词和万能比喻。
6. 删除不影响人物判断、行动、关系或气氛的套话、空泛升华和重复解释。
7. 章末不总结主题，不使用“这一切才刚刚开始”一类万能悬念。
8. 不模仿具体作者，不复用其他作品的标志性表达。

写完后静默检查：是否承接上一章最后的具体状态；是否出现知识越界或状态跳变；是否漏掉必要事件；是否提前消耗后续剧情；是否存在可以删除而不影响内容的套话。
"""

EDITOR_SYSTEM_PROMPT = """你是中文类型小说责任编辑。请在不改变事件顺序、事实结果、人物知识、伏笔含义和章节结尾状态的前提下，修改正文初稿。

重点处理：
1. 删除剧情总结、重复解释和空泛升华；
2. 将抽象情绪改为可观察的动作、生理反应、对白或选择；
3. 修正过度完整、用于讲解剧情的对白；
4. 删除机械排比、万能比喻和重复句式；
5. 修正视角越界和人物声音趋同；
6. 保留有叙事作用的具体细节，不把全文润色成统一腔调。

禁止新增重大事件、人物、线索、设定或事实。若发现连续性问题，只列入 continuity_warnings，不得擅自改动事实。
"""

EXTRACTION_SYSTEM_PROMPT = """你是小说作品状态提取器。只提取本章正文中明确出现、可以逐字找到证据的事实。

禁止猜测、补全、根据常识推断，禁止替正文决定旧状态、伏笔预计回收章节或人物真实动机。
old_value 由系统根据上一章状态快照提供；你只返回正文明确产生的新状态、证据和置信度。
emotion 默认视为本场景临时情绪，只有正文明确表现为持续变化时才标记为 persistent。
无法确定的时间保留原文并标记 unknown 或 relative。
"""


class ChapterGraphState(TypedDict):
    work: dict[str, Any]
    chapter_no: int
    mode: str
    instruction: str
    data: dict[str, Any]


def _strip_json(text: str) -> str:
    value = text.strip()
    if value.startswith("```"):
        value = re.sub(r"^```(?:json)?\s*", "", value, flags=re.I)
        value = re.sub(r"\s*```$", "", value)
    return value.strip()


def _parse_json(text: str) -> dict[str, Any] | list[Any]:
    value = _strip_json(text)
    try:
        return json.loads(value)
    except json.JSONDecodeError:
        start = min([p for p in (value.find("{"), value.find("[")) if p >= 0], default=-1)
        end = max(value.rfind("}"), value.rfind("]"))
        if start >= 0 and end > start:
            return json.loads(value[start : end + 1])
        raise


class NovelEngine:
    def __init__(self):
        self._clients: dict[str, Any] = {}

    def _codex_json(self, system: str, user: str, profile: dict[str, Any]) -> Any:
        """Use the officially supported local Codex CLI session, not a private HTTP endpoint."""
        command = shutil.which("codex") or shutil.which("codex.cmd")
        if not command:
            raise RuntimeError("Codex Auth 需要在运行 worker 的机器安装 Codex CLI")
        output_path = ""
        try:
            with tempfile.NamedTemporaryFile(prefix="novel-codex-", suffix=".txt", delete=False) as output:
                output_path = output.name
            prompt = (
                f"{system}\n只输出合法 JSON，不要 Markdown 代码块，不要解释。\n"
                f"{user}"
            )
            command_args = [
                command,
                "exec",
                "--skip-git-repo-check",
                "--sandbox",
                "read-only",
                "--output-last-message",
                output_path,
            ]
            model = str(profile.get("model") or "").strip()
            if model:
                command_args.extend(["--model", model])
            reasoning = str(profile.get("reasoning_effort") or "auto")
            if reasoning != "auto":
                command_args.extend(["--config", f"model_reasoning_effort={reasoning}"])
            result = subprocess.run(
                command_args,
                input=prompt,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=float(profile.get("timeout_seconds") or LLM_TIMEOUT_SECONDS),
                check=False,
            )
            if result.returncode != 0:
                detail = (result.stderr or result.stdout or "Codex exec 失败").strip()
                raise RuntimeError(detail[-1000:])
            content = Path(output_path).read_text(encoding="utf-8").strip()
            if not content:
                content = result.stdout.strip()
            return _parse_json(content)
        finally:
            if output_path:
                try:
                    Path(output_path).unlink(missing_ok=True)
                except OSError:
                    pass

    def _llm_json(
        self,
        system: str,
        user: str,
        profile: dict[str, Any] | None = None,
        temperature: float = 0.7,
    ):
        if (profile or {}).get("provider") == "codex_auth":
            return self._codex_json(system, user, profile or {})
        api_key = (profile or {}).get("api_key") or LLM_API_KEY
        if not api_key:
            return None
        try:
            from langchain_openai import ChatOpenAI

            base_url = (profile or {}).get("base_url") or LLM_BASE_URL
            model = (profile or {}).get("model") or LLM_MODEL
            timeout = (profile or {}).get("timeout_seconds") or LLM_TIMEOUT_SECONDS
            key_fingerprint = hashlib.sha256(api_key.encode("utf-8")).hexdigest()[:12]
            profile_key = str((profile or {}).get("id") or f"{base_url}:{model}") + f":{base_url}:{model}:{key_fingerprint}:{(profile or {}).get('reasoning_effort', 'auto')}:{temperature}"
            if profile_key not in self._clients:
                kwargs: dict[str, Any] = {
                    "api_key": api_key,
                    "base_url": base_url,
                    "model": model,
                    "temperature": temperature,
                    "timeout": timeout,
                }
                reasoning = (profile or {}).get("reasoning_effort")
                if reasoning and reasoning != "auto":
                    kwargs["model_kwargs"] = {"reasoning_effort": reasoning}
                    if (profile or {}).get("provider") == "deepseek":
                        kwargs["model_kwargs"]["thinking"] = {"type": "enabled"}
                self._clients[profile_key] = ChatOpenAI(**kwargs)
            result = self._clients[profile_key].invoke(
                [
                    ("system", system + "\n只输出合法 JSON，不要 Markdown 代码块，不要解释。"),
                    ("user", user),
                ]
            )
            return _parse_json(str(result.content))
        except Exception:
            logger.exception("novel_llm_generation_failed")
            return None

    @staticmethod
    def setup(work: dict[str, Any]) -> dict[str, Any]:
        title = work["title"]
        genre = work.get("genre") or "都市成长"
        premise = work.get("premise") or f"一个普通人在{genre}世界里，被迫面对改变人生的选择。"
        return {
            "story_bible": {
                "summary": premise,
                "theme": "人在压力和选择中完成成长，同时承担选择的代价。",
                "world": f"故事发生在{genre}背景下。现实规则清晰，冲突围绕人物目标、资源和关系展开。",
                "ending": "主角完成一次关键选择，解决主线冲突，但保留人物继续成长的空间。",
                "style_rules": work.get("writing_style") or "节奏清晰，场景具体，少用空泛抒情，多用动作和对白推动情节。",
            },
            "characters": [
                {
                    "name": "主角",
                    "role": "主视角人物",
                    "goal": "在外部压力下完成当前目标，并重新定义自己想要的生活。",
                    "conflict": "能力和资源不足，且不愿面对自己的真实需求。",
                    "personality": "克制、敏感、遇到重要的人和事会坚持到底。",
                    "background": "从普通生活进入主线事件。",
                    "status": "故事开始时尚未进入核心冲突。",
                    "knowledge": "只知道自己的经历，不知道完整真相。",
                },
                {
                    "name": "关键对手",
                    "role": "主线阻力",
                    "goal": "维护自己的利益和既有秩序。",
                    "conflict": "必须隐藏一个会改变局面的秘密。",
                    "personality": "理性、强势，做事有明确规则。",
                    "background": "与主线事件存在直接利益关系。",
                    "status": "掌握先手，暂时占据优势。",
                    "knowledge": "知道部分真相，但低估主角的选择。",
                },
            ],
            "plot_arcs": [
                {"title": "第一卷：进入冲突", "synopsis": "主角遇到无法回避的事件，建立目标并第一次付出代价。", "sequence": 1},
                {"title": "第二卷：真相升级", "synopsis": "人物关系和利益冲突扩大，前期线索逐渐指向更大的问题。", "sequence": 2},
                {"title": "第三卷：最终选择", "synopsis": "主角面对最重要的选择，解决主线并完成成长。", "sequence": 3},
            ],
        }

    def generate_setup(self, work: dict[str, Any], profile: dict[str, Any] | None = None) -> dict[str, Any]:
        fallback = self.setup(work)
        result = self._llm_json(
            SETUP_SYSTEM_PROMPT,
            json.dumps(
                {
                    "task": "生成故事初始化方案",
                    "work": {key: work.get(key, "") for key in ("title", "genre", "target_audience", "estimated_words", "writing_style", "premise")},
                    "schema": {
                        "story_bible": {
                            "summary": "",
                            "theme": "",
                            "world": "",
                            "ending": "",
                            "style_rules": "包含叙事视角、句式节奏、对白特点、情绪表现方式和应减少的套话。",
                        },
                        "characters": [{
                            "name": "", "role": "", "goal": "", "conflict": "",
                            "personality": "包含行为边界和说话/行动倾向",
                            "background": "", "status": "", "knowledge": "",
                        }],
                        "plot_arcs": [{
                            "title": "", "synopsis": "包含起点、关键因果和结束状态", "sequence": 1,
                        }],
                    },
                },
                ensure_ascii=False,
            ), profile, 0.5,
        )
        output = result if isinstance(result, dict) and result.get("story_bible") else fallback
        output["prompt_version"] = PROMPT_VERSION
        return output

    def generate_outline(self, work: dict[str, Any], chapter_count: int, profile: dict[str, Any] | None = None) -> dict[str, Any]:
        bible = work.get("story_bible") or {}
        characters = work.get("characters") or []
        plot_arcs = work.get("plot_arcs") or []
        fallback_items = []
        for chapter_no in range(1, chapter_count + 1):
            phase = "建立悬念" if chapter_no <= 3 else ("冲突升级" if chapter_no < chapter_count else "留下选择")
            fallback_items.append(
                {
                    "chapter_no": chapter_no,
                    "title": f"第{chapter_no}章 {phase}",
                    "goal": f"让主角在第{chapter_no}章完成一个不可逆的小选择。",
                    "conflict": "主角的目标与现实限制发生正面冲突。",
                    "beats": ["具体场景切入", "出现阻力", "人物做出选择"],
                    "hook": "结尾留下一个新的信息或问题。",
                    "pov_character": "主视角人物",
                    "opening_state": {"time": "承接上一章", "location": "", "carry_over_action": ""},
                    "causal_beats": [{"cause": "", "action": "", "obstacle": "", "consequence": ""}],
                    "knowledge_changes": [],
                    "state_changes": [],
                    "foreshadow_actions": [],
                    "forbidden_reveals": [],
                    "ending_state": {"location": "", "new_problem": "", "next_action": ""},
                }
            )
        user = json.dumps(
            {
                "task": "生成章节大纲",
                "chapter_count": chapter_count,
                "story_bible": bible,
                "characters": characters,
                "plot_arcs": plot_arcs,
                "existing_chapters": [
                    {"chapter_no": item.get("chapter_no"), "title": item.get("title", "")}
                    for item in (work.get("chapters") or [])
                ],
                "schema": [{
                    "chapter_no": 1, "title": "", "pov_character": "", "goal": "", "conflict": "",
                    "beats": [""], "hook": "", "opening_state": {}, "causal_beats": [],
                    "knowledge_changes": [], "state_changes": [], "foreshadow_actions": [],
                    "forbidden_reveals": [], "ending_state": {},
                }],
            },
            ensure_ascii=False,
        )
        result = self._llm_json(
            OUTLINE_SYSTEM_PROMPT,
            user, profile, 0.45,
        )
        items = result.get("chapters") if isinstance(result, dict) else result
        return {
            "chapters": items if isinstance(items, list) and items else fallback_items,
            "prompt_version": PROMPT_VERSION,
        }

    def generate_trend_ideas(self, items: list[dict[str, Any]], profile: dict[str, Any] | None = None) -> dict[str, Any]:
        compact = [{key: item.get(key, "") for key in ("title", "author", "category", "synopsis", "rank", "source")} for item in items]
        result = self._llm_json(
            "你是网络文学市场编辑。只根据公开榜单元数据分析趋势，禁止复述或仿写任何来源作品。输出5个完全原创的创意。",
            json.dumps({
                "task": "热门网文趋势与原创灵感",
                "sources": compact,
                "schema": {
                    "trend_summary": "",
                    "rising_themes": [""],
                    "overcrowded_directions": [""],
                    "ideas": [{"title": "", "genre": "", "audience": "", "hook": "", "premise": "", "synopsis": "", "differentiation": "", "risk": ""}],
                },
            }, ensure_ascii=False), profile,
        )
        if isinstance(result, dict) and isinstance(result.get("ideas"), list) and result["ideas"]:
            result["ideas"] = result["ideas"][:5]
            return result
        titles = [str(item.get("title", "热门作品")) for item in items[:3]]
        return {
            "trend_summary": "当前榜单显示，读者偏好集中在强冲突、明确目标和持续悬念。",
            "rising_themes": ["高概念开局", "关系冲突", "连续悬念"],
            "overcrowded_directions": ["直接套用热门书名或人物关系", "只替换背景的同质化设定"],
            "ideas": [{
                "title": f"未命名的{index + 1}号潮汐",
                "genre": item.get("category") or "都市成长",
                "audience": "长篇连载读者",
                "hook": "一个看似普通的选择牵动隐藏秩序。",
                "premise": "主角必须在真相公开前完成一次不可逆的选择。",
                "synopsis": "本创意仅参考榜单题材信号，不复用来源作品的正文、角色或情节。",
                "differentiation": f"从榜单作品的共同题材信号出发，与来源《{titles[0] if titles else '热门作品'}》保持人物和情节独立。",
                "risk": "需要避免使用过于相似的标题和开局桥段。",
            } for index, item in enumerate((items or [{}])[:5])],
        }

    @staticmethod
    def _normalize_extraction(result: Any, chapter: dict[str, Any]) -> dict[str, Any]:
        if not isinstance(result, dict):
            result = {}
        characters: list[dict[str, Any]] = []
        for item in result.get("characters", []) if isinstance(result.get("characters"), list) else []:
            if not isinstance(item, dict) or not str(item.get("character_name", "")).strip():
                continue
            changes: list[dict[str, Any]] = []
            raw_changes = item.get("changes") if isinstance(item.get("changes"), list) else item.get("state_changes", [])
            for change in raw_changes if isinstance(raw_changes, list) else []:
                if not isinstance(change, dict) or change.get("field") not in STATE_FIELDS:
                    continue
                try:
                    confidence = max(0.0, min(1.0, float(change.get("confidence", item.get("confidence", 0.5)))))
                except (TypeError, ValueError):
                    confidence = 0.5
                changes.append({
                    "field": change["field"],
                    "old_value": change.get("old_value"),
                    "new_value": change.get("new_value"),
                    "durability": str(change.get("durability", "unknown")).strip() or "unknown",
                    "evidence": str(change.get("evidence", "")).strip(),
                    "confidence": confidence,
                })
            if changes:
                characters.append({
                    "character_name": str(item["character_name"]).strip(),
                    "aliases": [str(alias).strip() for alias in item.get("aliases", []) if str(alias).strip()],
                    "changes": changes,
                })

        timeline_events: list[dict[str, Any]] = []
        for item in result.get("timeline_events", []) if isinstance(result.get("timeline_events"), list) else []:
            if not isinstance(item, dict) or not str(item.get("title", "")).strip():
                continue
            try:
                confidence = max(0.0, min(1.0, float(item.get("confidence", 0.5))))
            except (TypeError, ValueError):
                confidence = 0.5
            timeline_events.append({
                "title": str(item["title"]).strip(),
                "description": str(item.get("description", "")).strip(),
                "story_time_text": str(item.get("story_time_text", "")).strip(),
                "time_type": str(item.get("time_type", "unknown")).strip() or "unknown",
                "location": str(item.get("location", "")).strip(),
                "participants": [str(name).strip() for name in item.get("participants", []) if str(name).strip()],
                "evidence": str(item.get("evidence", "")).strip(),
                "confidence": confidence,
            })
        foreshadows: list[dict[str, Any]] = []
        for item in result.get("foreshadows", []) if isinstance(result.get("foreshadows"), list) else []:
            if not isinstance(item, dict) or not str(item.get("clue", "")).strip():
                continue
            try:
                confidence = max(0.0, min(1.0, float(item.get("confidence", 0.5))))
            except (TypeError, ValueError):
                confidence = 0.5
            foreshadows.append({
                "clue": str(item["clue"]).strip(),
                "kind": str(item.get("kind", "clue")).strip() or "clue",
                "planted_chapter": int(item.get("planted_chapter") or chapter.get("chapter_no", 0)),
                "expected_reveal_chapter": 0,
                "evidence": str(item.get("evidence", "")).strip(),
                "confidence": confidence,
            })
        return {
            "chapter_no": int(chapter.get("chapter_no", 0)),
            "characters": characters,
            "timeline_events": timeline_events,
            "foreshadows": foreshadows,
            "warnings": [str(item) for item in result.get("warnings", []) if str(item).strip()],
        }

    def extract_state_changes(self, work: dict[str, Any], chapter: dict[str, Any], profile: dict[str, Any] | None = None) -> dict[str, Any]:
        """只提取本章明确证据，结果用于审核，不直接写入正式状态。"""
        content = str(chapter.get("content", "")).strip()
        chapter_context = build_context(work, int(chapter.get("chapter_no") or 0))
        context_by_name = {item.get("name"): item for item in chapter_context.get("characters", [])}
        character_context = [
            {
                key: item.get(key, "")
                for key in ("name", "role", "goal", "status", "knowledge")
            }
            | {"previous_confirmed_state": context_by_name.get(item.get("name"), {}).get("confirmed_state", {})}
            for item in work.get("characters", [])
        ]
        result = self._llm_json(
            EXTRACTION_SYSTEM_PROMPT,
            json.dumps({
                "task": "从本章正文提取可审核的作品状态变化",
                "chapter": {"chapter_no": chapter.get("chapter_no"), "title": chapter.get("title"), "content": content},
                "known_characters": character_context,
                "schema": {
                    "characters": [{
                        "character_name": "",
                        "aliases": [],
                        "changes": [{"field": "location|goal|emotion|relationship|possession|physical_state|knowledge|secret_exposed|conflict|faction", "old_value": "从 previous_confirmed_state 复制，不要猜测", "new_value": None, "durability": "scene|persistent|unknown", "evidence": "", "confidence": 0.0}],
                    }],
                    "timeline_events": [{"title": "", "description": "", "story_time_text": "", "time_type": "absolute|relative|sequence|unknown", "location": "", "participants": [], "evidence": "", "confidence": 0.0}],
                    "foreshadows": [{"clue": "", "kind": "clue", "planted_chapter": 0, "expected_reveal_chapter": 0, "evidence": "", "confidence": 0.0}],
                    "warnings": [],
                },
            }, ensure_ascii=False), profile, 0.1,
        )
        if result is None:
            first_sentence = next((line.strip() for line in content.replace("。", "。\n").splitlines() if line.strip()), "")
            names = [str(item.get("name")) for item in work.get("characters", []) if item.get("name") and str(item["name"]) in content]
            result = {
                "characters": [],
                "timeline_events": ([{
                    "title": chapter.get("title") or f"第{chapter.get('chapter_no', 0)}章事件",
                    "description": first_sentence[:240],
                    "story_time_text": "",
                    "time_type": "unknown",
                    "location": "",
                    "participants": names,
                    "evidence": first_sentence,
                    "confidence": 0.35,
                }] if first_sentence else []),
                "warnings": ["当前未配置 LLM，使用本地低置信度提取；建议配置模型后重新提取。"],
            }
        return self._normalize_extraction(result, chapter)

    def _write_chapter(self, work: dict[str, Any], chapter_no: int, mode: str, instruction: str = "", profile: dict[str, Any] | None = None) -> dict[str, Any]:
        plan = next(
            (item for item in work.get("chapter_plans", []) if item.get("chapter_no") == chapter_no),
            None,
        ) or {
            "chapter_no": chapter_no,
            "title": f"第{chapter_no}章",
            "goal": "推进主线并制造一个新的问题。",
            "conflict": "角色目标与现实限制发生冲突。",
            "beats": [],
            "hook": "留下下一章的动力。",
        }
        context = build_context(work, chapter_no)
        mode_rules = {
            "plan": "只根据本章计划写作，不改变计划中的事实和顺序。",
            "chapter": "生成完整的新章节。",
            "continue": "必须紧接上一章末尾继续，不得重新开场、复述前文或跳过承接动作。",
            "rewrite": "保留原章节的事实、事件顺序、人物知识和结尾结果，只改写表达。",
        }.get(mode, "生成完整的新章节。")
        current_chapter = next(
            (item for item in (work.get("chapters") or []) if int(item.get("chapter_no") or 0) == chapter_no),
            None,
        )
        fallback_title = plan.get("title") or f"第{chapter_no}章"
        fallback_content = (
            f"{fallback_title}\n\n"
            f"{plan.get('opening_state', {}).get('carry_over_action') or '人物刚准备处理眼前的问题'}，"
            f"却被{plan.get('conflict') or '一个具体的阻力'}打断。"
            "他没有急着解释，只先确认手里能用的东西和对方已经知道的部分。"
            f"\n\n事情留下了一个必须在下一步处理的后果：{plan.get('hook') or '一个新的问题摆到了面前'}。"
        )
        result = self._llm_json(
            CHAPTER_SYSTEM_PROMPT,
            json.dumps(
                {
                    "task": "生成章节正文",
                    "mode": mode,
                    "mode_rules": mode_rules,
                    "author_instruction": instruction,
                    "chapter_plan": plan,
                    "context": context,
                    "existing_content": current_chapter.get("content", "") if current_chapter and mode == "rewrite" else "",
                    "schema": {"chapter_no": chapter_no, "title": "", "content": "", "continuity_warnings": []},
                },
                ensure_ascii=False,
            ), profile, 0.82,
        )
        if isinstance(result, dict) and result.get("content"):
            return {
                "chapter_no": chapter_no,
                "title": result.get("title", fallback_title),
                "content": result["content"],
                "continuity_warnings": result.get("continuity_warnings", []),
                "generation_source": "llm",
                "prompt_version": PROMPT_VERSION,
            }
        return {
            "chapter_no": chapter_no,
            "title": fallback_title,
            "content": fallback_content,
            "continuity_warnings": ["模型未返回正文，使用本地 fallback。"],
            "generation_source": "fallback",
            "prompt_version": PROMPT_VERSION,
        }

    def _edit_chapter(
        self,
        work: dict[str, Any],
        chapter_no: int,
        draft: dict[str, Any],
        profile: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        if not draft.get("content") or draft.get("generation_source") == "fallback":
            return draft
        context = build_context(work, chapter_no)
        result = self._llm_json(
            EDITOR_SYSTEM_PROMPT,
            json.dumps(
                {
                    "task": "在事实锁定下编辑章节正文",
                    "chapter_no": chapter_no,
                    "chapter_plan": next((item for item in work.get("chapter_plans", []) if item.get("chapter_no") == chapter_no), {}),
                    "context": context,
                    "draft": draft["content"],
                    "schema": {
                        "title": draft.get("title", ""),
                        "content": "",
                        "continuity_warnings": [],
                        "edit_notes": [],
                    },
                },
                ensure_ascii=False,
            ),
            profile,
            0.25,
        )
        if isinstance(result, dict) and result.get("content"):
            return {
                **draft,
                "title": result.get("title") or draft.get("title"),
                "content": result["content"],
                "continuity_warnings": [
                    *(draft.get("continuity_warnings") or []),
                    *(result.get("continuity_warnings") or []),
                ],
                "editor_notes": result.get("edit_notes") or [],
            }
        return draft

    def generate_chapter(self, work: dict[str, Any], chapter_no: int, mode: str, instruction: str = "", profile: dict[str, Any] | None = None) -> dict[str, Any]:
        """用 LangGraph 串起作者节点和责任编辑节点，保留后续扩展空间。"""
        if mode != "rewrite":
            continuity_warnings = build_context(work, chapter_no).get("continuity_warnings", [])
            blocking = [
                warning for warning in continuity_warnings
                if "未来信息" in str(warning) or "重写章节" in str(warning)
            ]
            if blocking:
                raise ValueError("当前章节的前置状态尚未重建：" + "；".join(str(item) for item in blocking[:3]))

        def writer(state: ChapterGraphState) -> ChapterGraphState:
            return {**state, "data": self._write_chapter(state["work"], state["chapter_no"], state["mode"], state["instruction"], profile)}

        def editor(state: ChapterGraphState) -> ChapterGraphState:
            data = self._edit_chapter(state["work"], state["chapter_no"], state["data"], profile)
            normalized = {
                "chapter_no": state["chapter_no"],
                "title": str(data.get("title") or f"第{state['chapter_no']}章").strip(),
                "content": str(data.get("content") or "").strip(),
                "continuity_warnings": data.get("continuity_warnings") if isinstance(data.get("continuity_warnings"), list) else [],
                "generation_source": data.get("generation_source", "unknown"),
                "editor_notes": data.get("editor_notes") if isinstance(data.get("editor_notes"), list) else [],
            }
            return {**state, "data": normalized}

        graph = StateGraph(ChapterGraphState)
        graph.add_node("writer", writer)
        graph.add_node("editor", editor)
        graph.set_entry_point("writer")
        graph.add_edge("writer", "editor")
        graph.add_edge("editor", END)
        result = graph.compile().invoke({
            "work": work,
            "chapter_no": chapter_no,
            "mode": mode,
            "instruction": instruction,
            "data": {},
        })
        return result["data"]


engine = NovelEngine()
