"""AI 小说三角色引擎：故事导演、小说作者、责任编辑。

没有配置 LLM 时使用可重复的本地 fallback，保证产品原型可以先跑通；
配置 LLM_API_KEY 后切换到 OpenAI-compatible API（默认 DeepSeek）。
"""

import json
import logging
import re
from typing import Any, TypedDict

from app.config import LLM_API_KEY, LLM_BASE_URL, LLM_MODEL

from langgraph.graph import END, StateGraph

logger = logging.getLogger(__name__)


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
        self._client = None

    def _llm_json(self, system: str, user: str):
        if not LLM_API_KEY:
            return None
        try:
            from langchain_openai import ChatOpenAI

            if self._client is None:
                self._client = ChatOpenAI(
                    api_key=LLM_API_KEY,
                    base_url=LLM_BASE_URL,
                    model=LLM_MODEL,
                    temperature=0.7,
                    timeout=90,
                )
            result = self._client.invoke(
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

    def generate_setup(self, work: dict[str, Any]) -> dict[str, Any]:
        fallback = self.setup(work)
        result = self._llm_json(
            "你是故事导演。为网络长篇小说建立可执行的故事档案，避免空泛设定。",
            json.dumps(
                {
                    "task": "生成故事初始化方案",
                    "work": {key: work.get(key, "") for key in ("title", "genre", "target_audience", "estimated_words", "writing_style", "premise")},
                    "schema": {
                        "story_bible": {"summary": "", "theme": "", "world": "", "ending": "", "style_rules": ""},
                        "characters": [{"name": "", "role": "", "goal": "", "conflict": "", "personality": "", "background": "", "status": "", "knowledge": ""}],
                        "plot_arcs": [{"title": "", "synopsis": "", "sequence": 1}],
                    },
                },
                ensure_ascii=False,
            ),
        )
        return result if isinstance(result, dict) and result.get("story_bible") else fallback

    def generate_outline(self, work: dict[str, Any], chapter_count: int) -> dict[str, Any]:
        bible = work.get("story_bible") or {}
        characters = work.get("characters") or []
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
                }
            )
        user = json.dumps(
            {
                "task": "生成章节大纲",
                "chapter_count": chapter_count,
                "story_bible": bible,
                "characters": characters,
                "schema": [{"chapter_no": 1, "title": "", "goal": "", "conflict": "", "beats": [""], "hook": ""}],
            },
            ensure_ascii=False,
        )
        result = self._llm_json(
            "你是故事导演。把长篇故事拆成有推进、有冲突、有结尾钩子的章节计划。",
            user,
        )
        items = result.get("chapters") if isinstance(result, dict) else result
        return {"chapters": items if isinstance(items, list) and items else fallback_items}

    def _write_chapter(self, work: dict[str, Any], chapter_no: int, mode: str, instruction: str = "") -> dict[str, Any]:
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
        previous = work.get("chapters", [])[-3:]
        fallback_title = plan.get("title") or f"第{chapter_no}章"
        fallback_content = (
            f"{fallback_title}\n\n"
            f"主角原本只想把事情按计划推进，但{plan.get('conflict') or '新的阻力突然出现'}。"
            "他先确认了眼前能掌握的线索，又发现关键人物隐瞒了一部分信息。"
            "房间里短暂安静下来，主角没有立刻回答，而是把那件事重新问了一遍。"
            f"\n\n这一章结束时，{plan.get('hook') or '一个新的问题浮出水面'}"
        )
        result = self._llm_json(
            "你是网络小说作者。按章节计划写具体、有场景、有动作和对白的正文，避免总结式流水账。",
            json.dumps(
                {
                    "task": "生成章节正文",
                    "mode": mode,
                    "instruction": instruction,
                    "chapter_plan": plan,
                    "story_bible": work.get("story_bible") or {},
                    "characters": work.get("characters") or [],
                    "previous_chapters": previous,
                    "schema": {"chapter_no": chapter_no, "title": "", "content": "", "state_updates": []},
                },
                ensure_ascii=False,
            ),
        )
        if isinstance(result, dict) and result.get("content"):
            return {"chapter_no": chapter_no, "title": result.get("title", fallback_title), "content": result["content"], "state_updates": result.get("state_updates", [])}
        return {"chapter_no": chapter_no, "title": fallback_title, "content": fallback_content, "state_updates": []}

    def generate_chapter(self, work: dict[str, Any], chapter_no: int, mode: str, instruction: str = "") -> dict[str, Any]:
        """用 LangGraph 串起作者节点和责任编辑节点，保留后续扩展空间。"""
        def writer(state: ChapterGraphState) -> ChapterGraphState:
            return {**state, "data": self._write_chapter(state["work"], state["chapter_no"], state["mode"], state["instruction"])}

        def editor(state: ChapterGraphState) -> ChapterGraphState:
            data = state["data"]
            normalized = {
                "chapter_no": state["chapter_no"],
                "title": str(data.get("title") or f"第{state['chapter_no']}章").strip(),
                "content": str(data.get("content") or "").strip(),
                "state_updates": data.get("state_updates") if isinstance(data.get("state_updates"), list) else [],
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
