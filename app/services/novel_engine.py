"""AI 小说三角色引擎：故事导演、小说作者、责任编辑。

没有配置 LLM 时使用可重复的本地 fallback，保证产品原型可以先跑通；
配置 LLM_API_KEY 后切换到 OpenAI-compatible API（默认 DeepSeek）。
"""

import json
import hashlib
import logging
import os
import re
import shutil
import subprocess
import tempfile
import time
from pathlib import Path
from typing import Any, Callable, TypedDict

from app.config import LLM_API_KEY, LLM_BASE_URL, LLM_MODEL, LLM_TIMEOUT_SECONDS
from app.services.app_settings import get_proxy_settings, proxy_url
from app.services.character_cards import compact_character, planning_character
from app.services.context_builder import build_context, build_chapter_generation_context, record_context_audit
from app.services.model_call_logs import (
    finish_model_call,
    mark_model_call_first_output,
    start_model_call,
)
from app.services.prompt_settings import get_prompt_setting
from app.services.planning_quality import (
    evaluate_outline,
    evaluate_setup,
    normalize_outline,
    normalize_setup,
    outline_readiness_issues,
)

from langgraph.graph import END, StateGraph

logger = logging.getLogger(__name__)


class GenerationCancelled(Exception):
    """Raised when a user cancels an in-flight model process."""


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

PROMPT_VERSION = "novel-writing-v3"


def codex_process_env() -> dict[str, str]:
    """Give project-launched Codex processes an isolated writable state home.

    The desktop Codex process may keep the user's global state database open. A
    separate home prevents short-lived ``codex exec`` processes from failing
    when that database is read-only or locked, while reusing the existing
    ChatGPT login file. An explicit CODEX_HOME remains authoritative.
    """
    environment = os.environ.copy()
    proxy = get_proxy_settings()
    if proxy["enabled"]:
        configured_proxy = proxy_url(proxy)
        environment["HTTP_PROXY"] = configured_proxy
        environment["HTTPS_PROXY"] = configured_proxy
        environment["ALL_PROXY"] = configured_proxy
        environment["NO_PROXY"] = "localhost,127.0.0.1,::1"
    configured_home = environment.get("CODEX_HOME", "").strip()
    if configured_home:
        return environment

    # The managed desktop sandbox can deny writes to parts of LOCALAPPDATA;
    # the process temp directory is writable and is sufficient for CLI state.
    codex_home = Path(tempfile.gettempdir()) / "ai-novel-generator-codex"
    codex_home.mkdir(parents=True, exist_ok=True)

    global_home = Path.home() / ".codex"
    source_auth = global_home / "auth.json"
    target_auth = codex_home / "auth.json"
    if source_auth.is_file():
        try:
            if not target_auth.exists() or source_auth.stat().st_mtime > target_auth.stat().st_mtime:
                shutil.copy2(source_auth, target_auth)
        except OSError as exc:
            logger.warning("无法同步 Codex 登录状态到隔离目录：%s", exc)

    environment["CODEX_HOME"] = str(codex_home)
    return environment

PLANNING_PRESETS = {
    "extreme爽文": {
        "target_experience": "持续获得明确、快速、可感知的胜利回报",
        "protagonist_principle": "主角主动掌控局面，受辱和吃亏不能长期悬置",
        "payoff_cadence": "小冲突当章或下一章回收，大冲突在本卷内完成阶段性清算",
        "power_cost": "只允许资源、时间和行动成本，不用牺牲无辜或削弱主角尊严制造代价",
        "forbidden": ["圣母式牺牲", "长期憋屈不回报", "配角轮流教育主角", "用道德困境替代主线爽点"],
    },
    "成长冒险": {
        "target_experience": "看见主角在具体挑战中不断变强并获得能力回报",
        "protagonist_principle": "主角允许失败，但每次失败必须留下可利用的经验或资源",
        "payoff_cadence": "每个阶段都有能力、关系或地位的可见提升",
        "power_cost": "能力提升需要可解释的训练、资源或风险",
        "forbidden": ["无因升级", "只靠旁白宣布变强", "重复同一种挑战"],
    },
    "悬疑推理": {
        "target_experience": "持续获得线索、推断和真相翻转的满足感",
        "protagonist_principle": "主角通过行动和证据推进，不靠作者临时揭示答案",
        "payoff_cadence": "每个阶段回收一条线索，同时制造更具体的新问题",
        "power_cost": "信息差、时间和可信度构成主要限制",
        "forbidden": ["无证据反转", "全知旁白泄底", "用巧合代替推理"],
    },
    "情感关系": {
        "target_experience": "在关系变化、选择和回报中获得情绪满足",
        "protagonist_principle": "人物通过具体行为表达立场，关系变化必须有前因",
        "payoff_cadence": "每个阶段至少完成一次关系推进、误会解除或情感选择",
        "power_cost": "选择会改变信任、边界和现实利益",
        "forbidden": ["无理由误会", "重复拉扯", "只靠告白解决关系"],
    },
    "custom": {
        "target_experience": "由作者自定义的核心阅读体验",
        "protagonist_principle": "由作者定义主角主动性、边界和选择规则",
        "payoff_cadence": "由作者定义回报的频率和尺度",
        "power_cost": "由作者定义能力或选择的代价",
        "forbidden": [],
    },
}

PLANNING_SYSTEM_PROMPT = """你是中文长篇小说的分阶段策划编辑。当前只处理一个规划步骤，不能提前生成其他步骤，也不能擅自改变已确认的创作契约。

所有输出必须是自然、具体、能直接交给作者确认的中文。优先使用清楚的主谓宾和人物行动，避免伪深刻比喻、口号式排比、抽象名词堆叠、管理术语和模型化总结。不要把“燃烧、吞噬、撕裂”等动词随意搭配“存活率、指标、概率、数值”等抽象量词；发现这种表达时改写成具体的选择、资源变化、伤害或后果。

这是逐步确认界面，不是完整小说。字段要短而具体：除人物小传外每个字符串通常不超过 80 个汉字；人物小传不超过 260 个汉字；数组通常不超过 5 项。不要写解释、复述提示词或隐藏推理，只返回可确认的 JSON。

不要为了制造戏剧性添加未被当前步骤允许的重大人物、世界规则、秘密或结局。若 confirmed_context 含 inspiration_brief，它只代表可吸收的抽象市场信号与原创变换约束：必须生成新的世界、人物、地点、物品、机制、关系结构和事件链，不能把它当成可复述的来源故事。只返回当前步骤要求的 JSON 结构。"""

SETUP_SYSTEM_PROMPT = """你是一名长篇中文类型小说的故事架构师。请建立可长期执行的故事档案，而不是写宣传文案。

书名是最高优先级的创作契约，不是一个可忽略的标签。先解释书名的字面指向、叙事意象和类型承诺，再建立故事。故事的核心事件、人物命运与结局必须共同兑现书名；若一句话设想较弱或与书名冲突，应调整设想的实现方式，不得另起一个只符合题材却与书名无关的故事。

要求：
1. 明确主角的外部目标、内在缺口、现实阻力和必须付出的代价。
2. 世界规则必须能影响人物选择；不要堆砌与剧情无关的设定。
3. 生成 3—5 名有姓名的主要人物，禁止使用“主角、反派、关键对手”等占位名。每人必须分别提供人物小传、戏剧核心（目标、动机、缺陷、阻力）、外貌、性格、语言习惯和人物弧。外貌只写可见细节，至少覆盖体态、五官发型、衣着配饰、辨识痕迹中的三类；性格只写稳定的行为倾向；语言习惯只写说话节奏、措辞或动作习惯。不要用“绝色、冷艳、英俊”等空泛标签代替细节；只在题材或作者要求时增加感情、战斗、悬疑等额外维度。
4. 主线必须有因果链：事件为什么发生、人物为什么介入、失败会失去什么。
5. 结局方向必须能由前期选择逐步推导，不能只写抽象主题。
6. 文风规则必须可执行，描述节奏、叙事视角、对白特点和应减少的表达习惯。
7. 所有设定服务于后续章节写作，避免“宏大但不可写”的空泛设定。
8. 明确列出至少 3 个“必须出现”的书名兑现元素，以及至少 2 条防止故事滑向同题材通用套路的跑偏边界。
"""

OUTLINE_SYSTEM_PROMPT = """你是一名长篇中文类型小说的故事导演。请把故事档案拆成可以连续写作的章节合同。

书名契约、读者承诺和人物小传是硬约束。先让每章承担卷级主线中的明确位置，再拆场景；不能只围绕一个泛化的“主角遇到困难”模板排章节。

每章必须回答：上一章结束时人物处于什么状态？本章为什么从这里开始？人物采取什么行动？阻力如何产生？行动造成什么不可逆后果？下一章从哪个具体状态接续？

要求：
1. 以卷级主线和已确认事实为约束，不为了凑章节数制造无因事件。
2. 每个 beat 都要包含前因、行动、阻力和结果，结果必须推动下一步。
3. 标明人物知识变化，禁止让人物提前知道尚未获得的信息。
4. 标明本章推进、埋设、误导或回收的伏笔，禁止提前揭露后续秘密。
5. 每章必须有 opening_state 和 ending_state，便于正文连续承接。
6. 结尾钩子必须是具体的新局面、发现、选择或代价，不写空泛预告。
7. 每章标明所属卷级主线、书名承诺推进和人物弧推进；首章、中点、末章必须分别完成书名承诺的建立、升级和兑现。
8. 必须严格返回指定章数，章节编号从 1 连续递增，视角人物只能使用人物小传中的真实姓名。
9. phase_key 必须逐字使用输入 story_phases 中的 phase_key，不能填写阶段名称或叙事词（如“建立”“升级”）。
"""

VOLUME_OUTLINE_SYSTEM_PROMPT = """你是一名中文长篇连载小说的卷级策划编辑。你的任务是生成一卷的可审核草稿，不是生成章节大纲，更不能改动该卷的章节范围或把单次章节生成窗口误当成分卷长度。

当前卷的 start_chapter、end_chapter 和每个叙事阶段的 start_chapter、end_chapter 都是作者已经确定的坐标，绝对不可修改。必须覆盖整卷范围；例如第1—40章就是40章的卷级规划，与任何“每次生成12章”的章节批次无关。

只根据作品设定、人物、主线、已确认规划与当前卷结构编写。existing_chapter_plans 不是本次依据，不能让其中可能错误的旧章节大纲限制或复制本卷的剧情。尤其要遵守故事的时间线、人物与势力的出现条件；未到应当登场的阶段，不得提前引入组织、角色、力量或终局成果。

输出必须具体、可执行、中文自然。卷梗概要写清本卷从什么局面出发，经由哪些升级，最终停在什么不可逆状态；目标、主要阻力、结局状态均应可检验。每个阶段的 purpose 写该阶段推进的具体任务；allowed_payoffs 写该阶段允许获得的小回报；forbidden_payoffs 写本阶段不能提前兑现的成果。不要为了补满字段虚构超过当前卷承受能力的势力或高潮。

若只要求重写一个阶段，只返回该阶段；但仍要以相邻阶段和本卷结局为边界，确保它能承接前后。只返回 JSON，不要解释。"""

SETUP_REPAIR_SYSTEM_PROMPT = """你是故事方案质检与修订编辑。根据给出的质量问题重做完整方案。保留原稿中有效的具体设定，但必须修复全部问题，尤其是书名承诺、人物小传和因果主线。只返回修订后的完整结构。"""

OUTLINE_REPAIR_SYSTEM_PROMPT = """你是章节大纲质检与修订编辑。根据质量问题重做完整大纲。必须严格满足章数、连续编号、书名承诺、人物弧和因果承接要求；只返回修订后的完整结构。"""

CHAPTER_SYSTEM_PROMPT = """你是一名成熟的中文类型小说作者。根据既定世界规则、人物事实、时间线、opening_state 和 chapter_contract，写出连续发生的章节正文。
1. 事实优先。 已确认事实和 opening_state 优先于章节合同；冲突时不得自行增加设定圆补。人物只能使用已经获得的信息，已有伤势、资源、关系、承诺和未完成行动必须继续产生后果。
2. 自然承接。 从上一章最后的时间、地点、动作和人物状态直接继续，不重新开场，不总结前情。
3. 因果推进。 chapter_contract 是本章必须抵达的结果，不是逐项打卡的大纲。重要事件必须由此前的行动、信息、选择、资源或环境变化触发。每个场景都有一个当前问题，推进中信息、风险、资源、关系、目标或可选行动应持续发生变化。
4. 人物按自己反应。 职业、经历、性格和当前状态要影响人物先注意什么、怎么判断、怎么行动和怎么犯错。不要套用统一的“震惊—分析—验证—接受”流程。人物可以误判、迟钝、冲动、重复确认或短暂失去效率。
5. 情绪必须产生后果。 可以直接写紧张、害怕、兴奋、愤怒，但重要情绪必须改变人物的动作、说话、注意力、判断或风险偏好。关键转折至少呈现“触发—具体主观反应—被改变的选择/动作/对白”中的完整链条，不能让角色只像执行剧情任务。不要把各种情绪都写成沉默、停顿、看一眼；也不要凭空补造童年创伤、亲属关系或未给出的前史。关键反应必须建立在读者已经理解的尺度上；依赖专业知识、人物习惯或世界规则才能看懂的异常，必须已有铺垫或同时给出直观后果。
6. 效果用剧情实现，不要直接命名。 用户要求的“爽、甜、虐、燃、恐怖、压抑、搞笑”等是阅读效果，不是要求正文直接使用这些评价词。不要替读者宣布“这里很爽”“真正的好戏开始了”之类效果；优先通过人物行动、局面反转、得失变化、对手反馈和环境后果，让效果自然成立。
7. 信息服务于行动。 以动作、对白、物件和环境反馈为主，必要背景可以简短直接交代。已经表现清楚的信息不要再解释。资源、装备、能力、计划、数据等可以具体写，但不要为了表现“很多”“很强”“准备充分”而连续罗列大量同类项目。优先选少数有代表性的细节、数字或异常选择建立尺度，其余概括处理，并尽快让这些信息影响人物下一步行动。允许口癖、废话、冷幽默、生活动作和少量无关紧要的细节，不要求每句话推进剧情，也不要把所有细节都写成伏笔。
8. 对话是行为。 人物说话应带有试探、拒绝、隐瞒、求助、争取、拖延等当下目的，允许打断、改口、误解和答非所问，不要轮流向读者解释设定。
9. 控制边界。 完成本章要求，但不得为了强化效果一次性耗尽所有升级、奖励、秘密、反转或资源，也不得提前揭露秘密或完成 next_chapter_boundary 之后的剧情。结尾形成一个已经发生的具体新局面，与 ending_state 一致，不总结主题，不用万能悬念。
输出正文；存在无法兼容的事实冲突时另列 continuity_warnings，否则为空。
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

TREND_SYSTEM_PROMPT = """你是网络文学市场编辑与原创策划编辑。只根据公开榜单元数据、官方短简介和分类标签提炼市场信号；不得假装读过正文，不得复述、续写或仿写任何来源作品。

先为每一部来源作品输出不含来源专名的“作品模型”：只写市场定位、叙事引擎、连载节奏、可安全吸收的抽象信号和不可复用风险。证据不足时明确标记信息不足，不得补造人物、地名、道具、世界规则或情节。

然后融合多部作品的共同信号，输出最多5个完全原创的创意。每个创意必须带原创蓝图：保留的是抽象读者体验和节奏；必须改变世界、主角起点、目标与代价、核心机制、关系结构、冲突升级和结局。人物、地点、物品、组织与能力名称必须全部重新设计。禁止只换名字或仅换背景。不要在输出中复述任何来源作品的具体角色、地名、物品、专有设定或关键事件链。"""

PROMPT_DEFAULTS = {
    "planning": PLANNING_SYSTEM_PROMPT,
    "setup": SETUP_SYSTEM_PROMPT,
    "setup_repair": SETUP_REPAIR_SYSTEM_PROMPT,
    "outline": OUTLINE_SYSTEM_PROMPT,
    "volume_outline": VOLUME_OUTLINE_SYSTEM_PROMPT,
    "outline_repair": OUTLINE_REPAIR_SYSTEM_PROMPT,
    "chapter": CHAPTER_SYSTEM_PROMPT,
    "editor": EDITOR_SYSTEM_PROMPT,
    "extraction": EXTRACTION_SYSTEM_PROMPT,
    "trend": TREND_SYSTEM_PROMPT,
}


def configured_prompt(prompt_key: str) -> str:
    """Resolve an editable prompt at call time so worker processes see saved changes."""
    return get_prompt_setting(prompt_key, PROMPT_DEFAULTS[prompt_key])


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
    except json.JSONDecodeError as original_error:
        # Some OpenAI-compatible providers occasionally append a second JSON
        # value or a short explanation after an otherwise valid response.
        # Parsing from the first opening delimiter to the final closing one
        # turns that recoverable response into ``Extra data``.  Decode the
        # first complete JSON value instead; the generation prompt already
        # requires the requested payload to be the first and only value.
        decoder = json.JSONDecoder()
        starts = sorted({
            position
            for delimiter in ("{", "[")
            for position in [value.find(delimiter)]
            if position >= 0
        })
        for start in starts:
            try:
                parsed, _end = decoder.raw_decode(value[start:])
            except json.JSONDecodeError:
                continue
            if isinstance(parsed, (dict, list)):
                return parsed
        raise original_error


def _stream_text(value: Any) -> str:
    """Normalize text deltas emitted by OpenAI-compatible LangChain clients."""
    if isinstance(value, str):
        return value
    if not isinstance(value, list):
        return str(value or "")
    parts: list[str] = []
    for item in value:
        if isinstance(item, str):
            parts.append(item)
        elif isinstance(item, dict):
            parts.append(str(item.get("text") or item.get("content") or ""))
        else:
            text = getattr(item, "text", None)
            if text:
                parts.append(str(text))
    return "".join(parts)


def _stream_reasoning_text(chunk: Any) -> str:
    """Return optional reasoning deltas without mixing them into final JSON."""
    additional = getattr(chunk, "additional_kwargs", None) or {}
    value = additional.get("reasoning_content") or additional.get("reasoning") or ""
    return _stream_text(value)


class NovelEngine:
    def __init__(self):
        self._clients: dict[str, Any] = {}

    @staticmethod
    def _terminate_process_tree(process: subprocess.Popen[str]) -> None:
        """Terminate the cmd wrapper and every child it started on Windows.

        ``codex.cmd`` launches a second executable. Killing only the wrapper
        leaves that child holding the output pipes open, which made a 180-second
        timeout take several extra minutes in practice.
        """
        if process.poll() is not None:
            return
        try:
            if os.name == "nt":
                result = subprocess.run(
                    ["taskkill", "/PID", str(process.pid), "/T", "/F"],
                    capture_output=True,
                    text=True,
                    timeout=5,
                    check=False,
                )
                # ``taskkill`` can fail silently when the cmd wrapper has
                # already detached from its child. Still terminate the process
                # we own, so a timeout never blocks on an inherited pipe.
                if result.returncode != 0 and process.poll() is None:
                    process.kill()
            else:  # pragma: no cover - production desktop deployment is Windows
                process.kill()
        except (OSError, subprocess.TimeoutExpired):
            try:
                process.kill()
            except OSError:
                pass

    def _codex_json(
        self,
        system: str,
        user: str,
        profile: dict[str, Any],
        *,
        on_progress: Callable[[str], None] | None = None,
        is_cancelled: Callable[[], bool] | None = None,
    ) -> Any:
        """Use the officially supported local Codex CLI session, not a private HTTP endpoint."""
        command = shutil.which("codex") or shutil.which("codex.cmd")
        if not command:
            raise RuntimeError("Codex Auth 需要在运行 worker 的机器安装 Codex CLI")
        output_path = ""
        stdout_path = ""
        stderr_path = ""
        response_text = ""
        model = str(profile.get("model") or "").strip()
        reasoning = str(profile.get("reasoning_effort") or "auto")
        call_id = start_model_call(
            profile,
            {
                "transport": "codex_cli",
                "model": model,
                "reasoning_effort": reasoning,
                "system": system,
                "user": user,
            },
        )
        try:
            with tempfile.NamedTemporaryFile(prefix="novel-codex-", suffix=".txt", delete=False) as output:
                output_path = output.name
            with tempfile.NamedTemporaryFile(prefix="novel-codex-stdout-", suffix=".log", delete=False) as stdout_file:
                stdout_path = stdout_file.name
            with tempfile.NamedTemporaryFile(prefix="novel-codex-stderr-", suffix=".log", delete=False) as stderr_file:
                stderr_path = stderr_file.name
            prompt = (
                f"{system}\n只输出合法 JSON，不要 Markdown 代码块，不要解释。\n"
                f"{user}"
            )
            command_args = [
                command,
                "exec",
                "--skip-git-repo-check",
                "--ephemeral",
                "--sandbox",
                "read-only",
                "--output-last-message",
                output_path,
            ]
            if model:
                command_args.extend(["--model", model])
            if reasoning != "auto":
                command_args.extend(["--config", f"model_reasoning_effort={reasoning}"])
            # Codex CLI requires ``-`` to explicitly consume a non-interactive
            # prompt from stdin. Without it, recent CLI versions can wait for
            # input until the model-call timeout even after stdin is closed.
            command_args.append("-")
            creationflags = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
            with open(stdout_path, "w", encoding="utf-8", errors="replace") as stdout_file, open(
                stderr_path, "w", encoding="utf-8", errors="replace"
            ) as stderr_file:
                process = subprocess.Popen(
                    command_args,
                    stdin=subprocess.PIPE,
                    stdout=stdout_file,
                    stderr=stderr_file,
                    env=codex_process_env(),
                    text=True,
                    encoding="utf-8",
                    errors="replace",
                    creationflags=creationflags,
                )
            assert process.stdin is not None
            process.stdin.write(prompt)
            process.stdin.close()
            process.stdin = None
            timeout_seconds = float(profile.get("timeout_seconds") or LLM_TIMEOUT_SECONDS)
            started = time.monotonic()
            next_heartbeat = 0.0
            stdout = ""
            stderr = ""
            while process.poll() is None:
                elapsed = time.monotonic() - started
                if is_cancelled and is_cancelled():
                    self._terminate_process_tree(process)
                    try:
                        process.wait(timeout=5)
                    except subprocess.TimeoutExpired:
                        pass
                    raise GenerationCancelled("用户已取消生成")
                if elapsed >= timeout_seconds:
                    self._terminate_process_tree(process)
                    try:
                        process.wait(timeout=5)
                    except subprocess.TimeoutExpired:
                        pass
                    raise TimeoutError(f"Codex 模型调用超过 {int(timeout_seconds)} 秒，已终止进程树")
                if on_progress and elapsed >= next_heartbeat:
                    on_progress(f"Codex 正在生成，已等待 {int(elapsed)} 秒；可随时取消")
                    next_heartbeat = elapsed + 5
                time.sleep(0.25)
            process.wait(timeout=5)
            stdout = Path(stdout_path).read_text(encoding="utf-8", errors="replace")
            stderr = Path(stderr_path).read_text(encoding="utf-8", errors="replace")
            result = subprocess.CompletedProcess(command_args, process.returncode, stdout, stderr)
            if result.returncode != 0:
                detail = (result.stderr or result.stdout or "Codex exec 失败").strip()
                raise RuntimeError(detail[-1000:])
            content = Path(output_path).read_text(encoding="utf-8").strip()
            if not content:
                content = result.stdout.strip()
            response_text = content
            parsed = _parse_json(content)
            finish_model_call(call_id, status="success", response_text=content, response=parsed)
            return parsed
        except GenerationCancelled as exc:
            finish_model_call(call_id, status="canceled", response_text=response_text, error=exc)
            raise
        except Exception as exc:  # noqa: BLE001 - preserve the existing CLI error behavior
            finish_model_call(
                call_id,
                status="timeout" if isinstance(exc, TimeoutError) else "failed",
                response_text=response_text,
                error=exc,
            )
            raise
        finally:
            for path in (output_path, stdout_path, stderr_path):
                if not path:
                    continue
                try:
                    Path(path).unlink(missing_ok=True)
                except OSError:
                    pass

    def probe_codex_auth(self, profile: dict[str, Any]) -> dict[str, Any]:
        """Run the same Codex execution path used by generation, not only `codex login status`."""
        result = self._codex_json(
            "你是连接测试器。",
            '请返回 {"ok": true}。',
            profile,
        )
        if not isinstance(result, dict) or result.get("ok") is not True:
            raise RuntimeError("Codex Auth 已登录，但模型没有返回预期 JSON。")
        return result

    def _llm_json(
        self,
        system: str,
        user: str,
        profile: dict[str, Any] | None = None,
        temperature: float = 0.7,
        *,
        on_progress: Callable[[str], None] | None = None,
        is_cancelled: Callable[[], bool] | None = None,
        stream: bool = True,
    ):
        result, _usage = self._llm_json_with_usage(
            system,
            user,
            profile,
            temperature,
            on_progress=on_progress,
            is_cancelled=is_cancelled,
            stream=stream,
        )
        return result

    def _llm_json_with_usage(
        self,
        system: str,
        user: str,
        profile: dict[str, Any] | None = None,
        temperature: float = 0.7,
        *,
        on_progress: Callable[[str], None] | None = None,
        is_cancelled: Callable[[], bool] | None = None,
        stream: bool = True,
    ) -> tuple[Any, dict[str, int]]:
        if (profile or {}).get("provider") == "codex_auth":
            return self._codex_json(system, user, profile or {}, on_progress=on_progress, is_cancelled=is_cancelled), {}
        api_key = (profile or {}).get("api_key") or LLM_API_KEY
        if not api_key:
            return None, {}
        base_url = (profile or {}).get("base_url") or LLM_BASE_URL
        model = (profile or {}).get("model") or LLM_MODEL
        reasoning = (profile or {}).get("reasoning_effort") or "auto"
        request_payload = {
            "messages": [
                {"role": "system", "content": system + "\n只输出合法 JSON，不要 Markdown 代码块，不要解释。"},
                {"role": "user", "content": user},
            ],
            "model": model,
            "temperature": temperature,
            "reasoning_effort": reasoning,
            "stream": bool(stream and (on_progress or is_cancelled)),
        }
        call_id = start_model_call(profile, request_payload)
        response_text = ""
        try:
            from langchain_openai import ChatOpenAI

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
                reasoning = (profile or {}).get("reasoning_effort") or "auto"
                provider = str((profile or {}).get("provider") or "openai_compatible")
                # OpenAI-style gateways commonly support reasoning_effort. Named
                # third-party providers use their own controls, so do not send an
                # unknown optional parameter that could reject the whole request.
                if provider in {"openai", "openai_compatible"} and reasoning != "auto":
                    kwargs["reasoning_effort"] = reasoning
                if provider == "deepseek":
                    # ``model_kwargs`` is flattened into the OpenAI SDK call.
                    # Provider extensions such as DeepSeek's thinking switch
                    # must travel through ``extra_body`` or the SDK rejects the
                    # request before it reaches the configured endpoint.
                    # Omit the extension entirely for normal/low reasoning.
                    if reasoning in {"high", "xhigh"}:
                        kwargs["extra_body"] = {"thinking": {"type": "enabled"}}
                self._clients[profile_key] = ChatOpenAI(**kwargs)
            messages = [
                ("system", system + "\n只输出合法 JSON，不要 Markdown 代码块，不要解释。"),
                ("user", user),
            ]
            result: Any
            raw_usage: dict[str, Any] = {}
            use_stream = stream and bool(on_progress or is_cancelled)
            if use_stream:
                # Streaming does not expose partial JSON to the user or database;
                # it only makes the waiting state observable and cancelable.
                parts: list[str] = []
                received_chars = 0
                reasoning_chars = 0
                last_report = 0.0
                output_reported = False
                if on_progress:
                    on_progress("已发送请求，正在等待模型首字；可随时取消")
                try:
                    for chunk in self._clients[profile_key].stream(messages):
                        if is_cancelled and is_cancelled():
                            raise GenerationCancelled("用户已取消生成")
                        piece = _stream_text(getattr(chunk, "content", ""))
                        reasoning_piece = _stream_reasoning_text(chunk)
                        if piece:
                            parts.append(piece)
                            received_chars += len(piece)
                            mark_model_call_first_output(call_id)
                        if reasoning_piece:
                            reasoning_chars += len(reasoning_piece)
                        chunk_usage = getattr(chunk, "usage_metadata", None) or {}
                        if not chunk_usage:
                            chunk_usage = (getattr(chunk, "response_metadata", None) or {}).get("token_usage", {})
                        if chunk_usage:
                            raw_usage = chunk_usage
                        now = time.monotonic()
                        if on_progress and ((piece and not output_reported) or now - last_report >= 1):
                            if received_chars:
                                on_progress(f"模型正在流式输出，已接收约 {received_chars} 个响应字符；可随时取消")
                                output_reported = True
                            elif reasoning_chars:
                                on_progress(f"模型正在思考，已接收约 {reasoning_chars} 个推理字符；可随时取消")
                            last_report = now
                    result = "".join(parts)
                except GenerationCancelled:
                    raise
                except Exception:
                    if parts:
                        raise
                    # A few OpenAI-compatible gateways reject stream=true.
                    # Retrying once without streaming preserves model coverage.
                    logger.warning("model_stream_unavailable_retrying_non_stream", exc_info=True)
                    if on_progress:
                        on_progress("当前模型不支持流式输出，已切换为普通生成；请继续等待")
                    if is_cancelled and is_cancelled():
                        raise GenerationCancelled("用户已取消生成")
                    result = self._clients[profile_key].invoke(messages)
                    raw_usage = getattr(result, "usage_metadata", None) or {}
                if on_progress:
                    on_progress(f"模型输出完成，正在校验结构（约 {received_chars or len(_stream_text(getattr(result, 'content', '')))} 个响应字符）")
            else:
                if is_cancelled and is_cancelled():
                    raise GenerationCancelled("用户已取消生成")
                if on_progress:
                    on_progress("模型正在生成完整响应；本次不使用流式传输")
                result = self._clients[profile_key].invoke(messages)
                raw_usage = getattr(result, "usage_metadata", None) or {}
            if not raw_usage:
                raw_usage = (getattr(result, "response_metadata", None) or {}).get("token_usage", {})
            input_tokens = raw_usage.get("input_tokens", raw_usage.get("prompt_tokens"))
            output_tokens = raw_usage.get("output_tokens", raw_usage.get("completion_tokens"))
            total_tokens = raw_usage.get("total_tokens")
            if total_tokens is None and input_tokens is not None and output_tokens is not None:
                total_tokens = int(input_tokens) + int(output_tokens)
            usage = {
                "input_tokens": int(input_tokens),
                "output_tokens": int(output_tokens),
                "total_tokens": int(total_tokens),
            } if input_tokens is not None and output_tokens is not None and total_tokens is not None else {}
            content = result if isinstance(result, str) else str(result.content)
            response_text = content
            parsed = _parse_json(content)
            finish_model_call(call_id, status="success", response_text=content, response=parsed, usage=usage)
            return parsed, usage
        except GenerationCancelled as exc:
            finish_model_call(call_id, status="canceled", response_text=response_text, error=exc)
            raise
        except Exception as exc:
            finish_model_call(
                call_id,
                status="timeout" if isinstance(exc, TimeoutError) else "failed",
                response_text=response_text,
                error=exc,
            )
            logger.exception("novel_llm_generation_failed")
            return None, {}

    def generate_planning_step(
        self,
        work: dict[str, Any],
        step: str,
        item_key: str,
        context: dict[str, Any],
        *,
        feedback: str = "",
        preset: str = "custom",
        candidate_count: int = 1,
        profile: dict[str, Any] | None = None,
        on_progress: Callable[[str], None] | None = None,
        is_cancelled: Callable[[], bool] | None = None,
    ) -> tuple[dict[str, Any], dict[str, int], str]:
        preset_rules = PLANNING_PRESETS.get(preset, PLANNING_PRESETS["custom"])
        schemas: dict[str, Any] = {
            "contract": {
                "candidates": [{
                    "title": "方向名称", "target_experience": "读者具体获得的快感或情绪回报",
                    "protagonist_principle": "主角主动性、性格原则和行为边界",
                    "power_curve": "主角如何获得优势或完成变化",
                    "payoff_cadence": "冲突、打脸、关系或线索的回报节奏",
                    "power_cost": "能力和选择的代价类型",
                    "moral_boundary": "主角不做什么",
                    "forbidden": ["明确禁止的跑偏方向"],
                    "style_rules": "句式、对白、叙事和语感约束",
                    "title_interpretation": "书名如何兑现类型承诺",
                    "reader_promise": "读者可以期待的具体体验",
                    "must_have_elements": ["必须出现的可感知元素"],
                }],
                "selected": None,
            },
            "setting": {"story_bible": {
                "core_hook": "开篇具体事件和主角为何必须介入",
                "core_conflict": "谁要什么、谁阻止、为何不能两全",
                "world": "只写会限制人物选择的规则",
                "stakes": "失败损失和成功回报",
                "ending": "能够由已确认规则推导出的结局方向",
                "must_have_elements": ["书名和契约必须兑现的元素"],
                "avoid_drift": ["防止滑向通用套路的边界"],
            }},
            "protagonist": {"character": {
                "name": "真实姓名", "role": "主角身份", "story_function": "主角在主线中不可替代的作用",
                "biography": "120—220字的因果经历，只讲出身、关键经历与为何进入当前故事",
                "dramatic_core": {"goal": "外部目标", "motivation": "深层动机", "flaw": "会造成错误选择的缺陷", "conflict": "当前主要阻力"},
                "appearance": "60—120字，只写可见外貌；至少包含体态、五官发型、衣着配饰、辨识痕迹中的三类，不得混入性格和台词",
                "personality": "40—90字，只写稳定行为倾向、价值取舍和压力下的反应，不得复述外貌或台词",
                "voice": "30—70字，只写措辞、语速、句式或标志性动作习惯，不得复述外貌或性格",
                "arc": "起点、关键转折、终点", "facets": {"可选题材维度": {"content": "仅在契约或作者反馈明确要求时填写"}},
            }},
            "cast_roster": {"characters": [{"item_key": "character:1", "name": "真实姓名", "role": "功能和立场", "relationship_to_protagonist": "关系", "story_function": "不可替代的作用"}]},
            "character": {"character": {
                "name": "真实姓名", "role": "人物身份", "story_function": "该人物不可替代的剧情作用",
                "biography": "120—220字的因果经历，只讲出身、关键经历与为何进入当前故事",
                "dramatic_core": {"goal": "外部目标", "motivation": "深层动机", "flaw": "会造成错误选择的缺陷", "conflict": "与主角或主线的主要阻力"},
                "appearance": "60—120字，只写可见外貌；至少包含体态、五官发型、衣着配饰、辨识痕迹中的三类，不得混入性格和台词",
                "personality": "40—90字，只写稳定行为倾向、价值取舍和压力下的反应，不得复述外貌或台词",
                "voice": "30—70字，只写措辞、语速、句式或标志性动作习惯，不得复述外貌或性格",
                "arc": "起点、关键转折、终点", "facets": {"可选题材维度": {"content": "仅在契约或作者反馈明确要求时填写"}},
            }},
            "arc": {"arc": {
                "title": "卷名", "sequence": 1, "goal": "本卷要完成的目标", "opposition": "本卷主要阻力",
                "payoffs": ["本卷具体回报"], "turning_point": "中段转折", "ending_state": "卷末不可逆状态",
                "synopsis": "只使用已确认事实写出的卷级主线",
            }},
            "summary": {"story_bible": {
                "summary": "只整合已确认内容的总梗概，包含起点、升级、转折、低谷、结局和因果",
                "theme": "从已确认选择中自然归纳的主题",
                "style_rules": "整合后的可执行写作规则",
            }},
        }
        if step not in schemas:
            raise ValueError(f"不支持的规划步骤：{step}")
        target_character = self._roster_character(context, item_key) if step == "character" else None
        if step == "character" and not target_character:
            raise ValueError(f"角色阵容中不存在 {item_key}，请先确认角色阵容")
        user = json.dumps({
            "task": f"生成规划步骤：{step}",
            "item_key": item_key,
            "candidate_count": 3 if step == "contract" else 1,
            "work": {key: work.get(key, "") for key in ("title", "genre", "target_audience", "estimated_words", "writing_style", "premise")},
            "preset": preset,
            "preset_rules": preset_rules,
            "confirmed_context": context,
            "selected_roster_character": target_character,
            "character_distinction_rules": (
                "人物小传必须只写当前 selected_roster_character。与 confirmed_context 中已确认人物相比，"
                "其经历、目标、动机、缺陷、人物弧、外貌、性格和语言习惯都要有可辨识差异；"
                "不得复用整句、同一段经历或仅替换姓名和身份。biography 只负责因果经历；"
                "appearance 必须提供至少三类可直接写入正文的视觉细节；personality 与 voice 不能复制 appearance 或彼此复用。作者反馈是本次人物卡的硬要求；"
                "只在创作契约、已确认设定或作者反馈明确需要时，才在 facets 中增加 romance、combat、mystery 等题材维度。"
                if step == "character" else None
            ),
            "author_feedback": feedback,
            "schema": schemas[step],
        }, ensure_ascii=False)
        result, usage = self._llm_json_with_usage(
            configured_prompt("planning"),
            user,
            profile,
            0.45,
            on_progress=on_progress,
            is_cancelled=is_cancelled,
        )
        if isinstance(result, dict):
            if step == "character" and target_character:
                result["character"] = self._apply_roster_identity(self._result_character(result), target_character)
                result.pop("candidates", None)
            elif step == "protagonist":
                result["character"] = planning_character(self._result_character(result))
                result.pop("candidates", None)
            return result, usage, "model"
        fallback = self._planning_fallback(work, step, item_key, context, preset, candidate_count)
        if step == "character" and target_character:
            fallback["character"] = self._apply_roster_identity(dict(fallback.get("character") or {}), target_character)
        elif step == "protagonist":
            fallback["character"] = planning_character(dict(fallback.get("character") or {}))
        return fallback, usage, "fallback"

    def generate_character_batch(
        self,
        work: dict[str, Any],
        item_keys: list[str],
        context: dict[str, Any],
        *,
        feedback: str = "",
        preset: str = "custom",
        profile: dict[str, Any] | None = None,
        on_progress: Callable[[str], None] | None = None,
        is_cancelled: Callable[[], bool] | None = None,
    ) -> tuple[dict[str, dict[str, Any]], dict[str, int], str]:
        """Generate a roster's missing biographies in one request, preserving each roster identity."""
        target_characters = []
        for item_key in item_keys:
            target = self._roster_character(context, item_key)
            if not target:
                raise ValueError(f"角色阵容中不存在 {item_key}，请先确认角色阵容")
            target_characters.append(target)
        character_schema = {
            "name": "真实姓名", "role": "人物身份", "story_function": "不可替代的剧情作用",
            "biography": "120—220字因果经历",
            "dramatic_core": {"goal": "外部目标", "motivation": "深层动机", "flaw": "会造成错误选择的缺陷", "conflict": "当前主要阻力"},
            "appearance": "60—120字，只写外貌；至少覆盖体态、五官发型、衣着配饰、辨识痕迹中的三类",
            "personality": "40—90字，只写稳定行为倾向和压力下反应",
            "voice": "30—70字，只写措辞、句式、语速或标志性动作习惯",
            "arc": "起点、关键转折、终点", "facets": {"可选题材维度": {"content": "仅在需要时填写"}},
        }
        preset_rules = PLANNING_PRESETS.get(preset, PLANNING_PRESETS["custom"])
        user = json.dumps({
            "task": "批量生成人物小传",
            "work": {key: work.get(key, "") for key in ("title", "genre", "target_audience", "estimated_words", "writing_style", "premise")},
            "preset": preset,
            "preset_rules": preset_rules,
            "confirmed_context": context,
            "selected_roster_characters": target_characters,
            "character_distinction_rules": (
                "为每个 selected_roster_characters 成员恰好输出一项，item_key 必须原样保留，不能遗漏或新增角色。"
                "阵容拥有姓名、身份和剧情作用，不能改写。每人的经历、目标、动机、缺陷、人物弧、外貌、性格和语言习惯"
                "必须彼此可辨识，也不得复用已确认人物的整句、同一段经历或只替换姓名。biography 只负责因果经历；"
                "appearance 必须有至少三类可直接写入正文的视觉细节；personality 与 voice 必须独立，不能重复其他字段。作者反馈是硬要求；只在明确需要时填写 facets。"
            ),
            "author_feedback": feedback,
            "schema": {"characters": [{"item_key": "character:1", "character": character_schema}]},
        }, ensure_ascii=False)
        result, usage = self._llm_json_with_usage(
            configured_prompt("planning"), user, profile, 0.45,
            on_progress=on_progress, is_cancelled=is_cancelled,
        )
        generated: dict[str, dict[str, Any]] = {}
        raw_by_key: dict[str, dict[str, Any]] = {}
        if isinstance(result, dict) and isinstance(result.get("characters"), list):
            for item in result["characters"]:
                if not isinstance(item, dict) or not item.get("item_key"):
                    continue
                raw = item.get("character") if isinstance(item.get("character"), dict) else item
                if isinstance(raw, dict):
                    raw_by_key[str(item["item_key"])] = raw
        source = "model" if raw_by_key else "fallback"
        for target in target_characters:
            item_key = str(target["item_key"])
            raw = raw_by_key.get(item_key)
            if raw:
                generated[item_key] = self._apply_roster_identity(dict(raw), target)
            else:
                fallback = self._planning_fallback(work, "character", item_key, context, preset, 1)
                generated[item_key] = self._apply_roster_identity(dict(fallback.get("character") or {}), target)
        return generated, usage, source

    @staticmethod
    def _result_character(result: dict[str, Any]) -> dict[str, Any]:
        """Read a character from the expected slot, tolerating a candidate wrapper.

        Some compatible models follow the contract-step shape and wrap a valid
        protagonist in ``candidates[0].character`` while leaving the requested
        ``character`` template empty.  Prefer the normal shape, then promote a
        named candidate rather than persisting an empty compatibility card.
        """
        direct = result.get("character")
        if isinstance(direct, dict) and str(direct.get("name") or "").strip():
            return dict(direct)
        candidates = result.get("candidates")
        if isinstance(candidates, list):
            for candidate in candidates:
                nested = candidate.get("character") if isinstance(candidate, dict) else None
                if isinstance(nested, dict) and str(nested.get("name") or "").strip():
                    return dict(nested)
        return dict(direct) if isinstance(direct, dict) else {}

    @staticmethod
    def _apply_roster_identity(character: dict[str, Any], target_character: dict[str, Any]) -> dict[str, Any]:
        """The confirmed roster owns identity and story function, never the model response."""
        character = compact_character(character)
        character["name"] = target_character.get("name", character.get("name", ""))
        character["role"] = target_character.get("role", character.get("role", ""))
        roster_relation = target_character.get("relationship_to_protagonist", "")
        roster_function = target_character.get("story_function", "")
        character["story_function"] = roster_function or character.get("story_function", "")
        if roster_relation or roster_function:
            character["relationships"] = (
                f"阵容关系：{roster_relation}；剧情作用：{roster_function}。"
                f"人物补充：{character.get('relationships', '')}"
            ).strip("；")
        return planning_character(character)

    @staticmethod
    def _roster_character(context: dict[str, Any], item_key: str) -> dict[str, Any] | None:
        for roster in context.get("cast_roster", []) or []:
            if not isinstance(roster, dict):
                continue
            for character in roster.get("characters", []) or []:
                if isinstance(character, dict) and character.get("item_key") == item_key:
                    return character
        return None

    @staticmethod
    def _planning_fallback(work: dict[str, Any], step: str, item_key: str, context: dict[str, Any], preset: str, candidate_count: int) -> dict[str, Any]:
        title = work.get("title") or "未命名作品"
        rules = PLANNING_PRESETS.get(preset, PLANNING_PRESETS["custom"])
        if step == "contract":
            candidates = []
            for index in range(max(3, candidate_count)):
                candidates.append({
                    "title": f"{title}·方向{index + 1}",
                    "target_experience": rules["target_experience"],
                    "protagonist_principle": rules["protagonist_principle"],
                    "power_curve": "主角从第一个关键选择开始获得可验证优势，并用行动持续扩大优势。",
                    "payoff_cadence": rules["payoff_cadence"],
                    "power_cost": rules["power_cost"],
                    "moral_boundary": "不主动伤害无关者，核心利益和承诺必须由主角自己守住。",
                    "forbidden": rules["forbidden"],
                    "style_rules": "短句、具体动作、对白推动，少用抽象总结和万能比喻。",
                    "title_interpretation": f"书名《{title}》必须在开篇提出核心承诺，在中段用行动升级，结局用事实兑现。",
                    "reader_promise": f"读者会持续看到《{title}》对应的核心能力、冲突和回报发生在具体场景里。",
                    "must_have_elements": [f"《{title}》对应的核心事件在开篇出现", "主角的每次关键选择带来可见收益", "结局兑现书名承诺"],
                })
            return {"candidates": candidates, "selected": None}
        contract = ((context.get("contract") or [{}])[0].get("selected") or {})
        if step == "setting":
            return {"story_bible": {
                "core_hook": f"《{title}》中的核心事件在开篇打破主角原有生活，主角必须主动介入才能获得第一项优势。",
                "core_conflict": "主角要掌握核心资源并兑现自己的目标，主要对手要夺走资源或改变规则，双方不能同时达成目标。",
                "world": "所有能力和资源都必须通过具体行动取得；信息、空间和时间限制会直接影响人物选择。",
                "stakes": "失败会失去核心资源和主动权，成功会获得新的能力、地位或选择空间。",
                "ending": "结局由主角前期确认的原则和行动推导，核心资源在最终冲突中完成最大一次兑现。",
                "must_have_elements": contract.get("must_have_elements", []),
                "avoid_drift": contract.get("forbidden", []),
            }}
        if step == "protagonist":
            return {"character": {
                "name": "沈砚", "role": "主视角人物", "goal": "掌握核心资源并完成自己的目标",
                "conflict": "既有势力试图夺走他的主动权，并利用他的过去逼他退让。",
                "appearance": "二十七八岁，肩背结实，短发常被汗压在额角，洗旧的深色冲锋衣袖口磨白，左手虎口留着一道浅裂伤。",
                "personality": "先行动后解释，重承诺，面对挑衅会迅速判断并给出回应。",
                "background": "长期靠自己的技能和判断解决问题，因此不习惯把选择权交给别人。",
                "status": "故事开篇掌握一项尚未被外界理解的优势。", "knowledge": "只知道优势的基础规则，不知道主要对手的完整计划。",
                "biography": "沈砚习惯把问题拆成可执行的步骤，别人退让时他会记住，别人越界时他会追问代价。故事开始后，他第一次拥有能改变局面的核心优势，也第一次被迫决定自己愿意守住什么。",
                "motivation": "证明自己的选择不需要由别人批准，并让曾经轻视他的人承担真实后果。",
                "flaw": "过度相信自己能控制所有变量，容易把盟友的提醒当成阻碍。",
                "character_arc": "从只相信个人判断，到学会让可靠的盟友成为自己扩大胜势的工具。",
                "secret": "他曾经因为一次退让失去重要机会，因此对再次被迫低头极其敏感。",
                "relationships": "与配角的关系由利益和行动建立，不靠空泛的口头忠诚。",
                "voice": "句子短，少解释；做决定前问成本，做决定后立刻行动。",
            }}
        if step == "cast_roster":
            return {"characters": [
                {"item_key": "character:1", "name": "顾临川", "role": "核心盟友", "relationship_to_protagonist": "互相利用后建立信任", "story_function": "提供行动能力和另一种判断"},
                {"item_key": "character:2", "name": "陆明川", "role": "主要对手", "relationship_to_protagonist": "试图夺走主角的主动权", "story_function": "制造持续升级的外部压力"},
                {"item_key": "character:3", "name": "苏晚晴", "role": "关键见证者", "relationship_to_protagonist": "看见主角选择的代价", "story_function": "提供信息、关系和情绪回报"},
            ]}
        if step == "character":
            roster_character = NovelEngine._roster_character(context, item_key) or {}
            name = roster_character.get("name") or item_key.split(":", 1)[-1]
            role = roster_character.get("role") or "已确认阵容中的角色"
            relationship = roster_character.get("relationship_to_protagonist") or "与主角通过利益、承诺或冲突建立关系"
            story_function = roster_character.get("story_function") or "在主线中承担不可替代的作用"
            if any(marker in role for marker in ("对手", "反派", "阻力")):
                details = {
                    "goal": "守住现有秩序和自己掌握的核心资源，不让主角改变规则",
                    "conflict": "越想维持控制，越必须暴露自己曾经越界的手段",
                    "appearance": "四十岁上下，身形挺拔，鬓角梳得一丝不乱，深色大衣袖口总压着银色袖扣，右眉尾有一道浅疤。",
                    "personality": "礼貌克制，习惯给别人有限选项，把威胁包装成理性建议",
                    "background": f"多年经营才取得如今的位置，也因此最清楚{story_function}一旦失败会失去什么",
                    "status": "占有信息和资源先手，尚未把主角视为真正威胁",
                    "knowledge": "知道核心资源的来历，却误判了主角拒绝妥协的程度",
                    "motivation": "证明自己建立的秩序值得一切代价，避免承认过去的牺牲毫无必要",
                    "flaw": "把控制局面当成承担责任，无法容忍计划之外的自主选择",
                    "character_arc": "从从容安排局面，到因主角夺回选择权而不断加码，最终被自己的规则反噬",
                    "secret": "曾在最早可以阻止危机时选择隐瞒，并从后续秩序中获益",
                    "voice": "措辞完整，很少直接威胁；总给两个选项，并藏起真正的第三条路",
                    "biography": f"{name}并非一开始就站在主角对面。早年一次混乱让其相信，只有把资源和决定权集中起来才能避免更坏结果。此后每次成功收拾残局，都加深了这种信念，也让其逐渐成为{story_function}的实际掌控者。如今主角拒绝接受安排，迫使{name}第一次面对一个无法被谈判和利益收编的变量。",
                }
            elif any(marker in role for marker in ("见证", "信息", "证人")):
                details = {
                    "goal": "在不再次伤害无辜者的前提下，让自己掌握的关键事实得到正确使用",
                    "conflict": "公开信息能推动真相，却也会暴露自己曾经沉默的责任",
                    "appearance": "二十六七岁，身形偏瘦，长发常低低束起，细框眼镜后有明显黑眼圈，旧帆布包的边角磨得发白。",
                    "personality": "观察细致，对细节敏感；表面回避冲突，触及底线时会突然坚定",
                    "background": f"曾亲眼见过一次错误决定的后果，此后被迫承担{story_function}带来的风险",
                    "status": "掌握一块能改变判断的拼图，但没有足够安全感主动交出",
                    "knowledge": "知道事件的局部真相，不知道主角和对手各自隐瞒的完整动机",
                    "motivation": "不愿再让自己的沉默成为别人伤害无辜者的工具",
                    "flaw": "总想等到绝对安全才表态，容易错过唯一能改变结果的时机",
                    "character_arc": "从把沉默当成自保，到主动选择证言的对象、方式和代价",
                    "secret": "保存着一份未公开记录，其中也留下了自己曾经配合隐瞒的证据",
                    "voice": "先纠正时间、地点和用词；紧张时反复确认细节，决定后会一次说完整",
                    "biography": f"{name}原本只想远离那场改变许多人生活的旧事，却因记得一个被所有人忽略的细节而始终无法真正脱身。多年里，{name}把记录藏在日常生活之下，既害怕它被销毁，也害怕它证明自己的退让同样造成了后果。主线重启后，{story_function}不再是旁观位置，而成为必须亲自决定真相交给谁的责任。",
                }
            else:
                details = {
                    "goal": "借合作完成自己无法独立推进的目标，同时保留退出和反制的能力",
                    "conflict": "需要主角的判断，却不相信主角会把共同利益放在个人原则之前",
                    "appearance": "三十岁上下，肩宽腿长，短发剪得利落，旧夹克肘部磨白，右手腕缠着褪色的运动护腕，指节有新旧交叠的伤痕。",
                    "personality": "行动迅速、重交换轻承诺；一旦认定同伴便会承担实际风险",
                    "background": f"长期靠个人能力处理危险局面，因一次单独行动失败而卷入{story_function}",
                    "status": "拥有行动能力和零散线索，缺少能把线索变成结果的关键判断",
                    "knowledge": "知道外围执行者和行动路径，不清楚核心决策者的真实目的",
                    "motivation": "证明当年的失败不是能力不足，并弥补自己没能保护重要之人的遗憾",
                    "flaw": "把速度当成诚意，信息不足时也会用行动逼别人立即站队",
                    "character_arc": "从把合作视为临时交换，到学会为共同决定及其后果承担责任",
                    "secret": "曾擅自使用来源不明的线索，导致一个无辜者暴露在风险中",
                    "voice": "多用短句和反问，倾向边走边谈；不耐烦时会先动手处理眼前障碍",
                    "biography": f"{name}习惯靠速度和胆量弥补资源不足，曾经因此解决过许多别人不敢碰的问题，也曾让一次看似果断的行动伤及无辜。为了追回那次失误留下的后果，{name}一路追到《{title}》的核心事件，并以{role}身份接近主角。真正的考验不是敢不敢冒险，而是能否把选择权交给同伴并共同承担结果。",
                }
            return {"character": {"name": name, "role": role, **details, "relationships": f"阵容关系：{relationship}；剧情作用：{story_function}"}}
        if step == "arc":
            sequence = int(item_key.rsplit(":", 1)[-1]) if ":" in item_key else 1
            return {"arc": {"title": f"第{sequence}卷·主动权升级", "sequence": sequence, "goal": "主角取得本卷的核心目标并扩大主动权", "opposition": "对手用资源、信息或关系阻止主角", "payoffs": ["主角获得一次可见胜利", "对手承担一次实质损失"], "turning_point": "主角发现继续退让会失去更大的主动权", "ending_state": "主角完成阶段性清算并进入更高一级冲突", "synopsis": "本卷从具体冲突开始，由主角的主动行动推动升级，在中段改变双方力量关系，卷末以可验证的胜利和新的更高目标结束。"}}
        if step == "summary":
            return {"story_bible": {"summary": f"《{title}》从一个具体事件开始，主角在已确认的核心规则下主动介入，先取得局部优势，再面对主要对手的升级阻力。每次选择都会带来下一步行动和可见后果，主线在中段改变力量关系，最终让主角以自己的原则完成核心冲突。", "theme": "人物的选择通过行动和后果获得意义。", "style_rules": contract.get("style_rules", "短句、具体动作、对白推动，少用抽象总结和万能比喻。")}}
        return {}

    @staticmethod
    def setup(work: dict[str, Any]) -> dict[str, Any]:
        title = work["title"]
        genre = work.get("genre") or "都市成长"
        premise = work.get("premise") or f"一个普通人在{genre}世界里，被迫面对改变人生的选择。"
        return {
            "story_bible": {
                "summary": f"《{title}》围绕“{premise}”展开。沈知微发现，书名所指的核心事件既是她追查真相的入口，也是她过去一次沉默留下的后果。她必须与立场并不一致的陆沉舟合作，在周闻远持续施压和误导的情况下取得证据；每推进一步，都会损害一段关系或失去一项现实资源。故事经过异常事件出现、同盟破裂、标题含义翻转和代价升级，最终迫使她以不可撤销的公开选择解决核心冲突，让书名在事实真相与人物命运两个层面同时得到解释。",
                "theme": "获得真相并不等于完成正义，人物还必须承担自己选择真相的方式和代价。",
                "world": f"故事发生在{genre}背景下。信息、资源和关系都有明确取得路径，任何能力或权力都必须付出成本；这些规则会直接限制人物调查、结盟和自保的方式。",
                "ending": f"沈知微确认《{title}》所指事件的完整真相后，放弃能保护自己的退路并公开关键证据。危机得到解决，但她必须承担关系破裂和职业受损的后果，完成从逃避责任到主动选择的转变。",
                "style_rules": work.get("writing_style") or "节奏清晰，场景具体，少用空泛抒情，多用动作和对白推动情节。",
                "title_interpretation": f"《{title}》既指向推动主线的核心事件或事物，也象征人物无法绕开的旧选择；它会在开篇被提出、中段改变含义、结局获得完整解释。",
                "reader_promise": f"读者从《{title}》这个书名将得到一个围绕同一核心谜题持续升级的{genre}故事，并看到标题含义随真相和人物选择逐层翻转。",
                "core_hook": f"沈知微发现一条只可能与《{title}》有关的异常线索，而线索同时证明她自己并非无辜旁观者。",
                "core_conflict": "沈知微必须公开真相才能阻止更大伤害，但真相会摧毁她赖以生存的关系和身份；周闻远则依靠现有秩序阻止她继续追查。",
                "stakes": "失败会让关键证据永久消失、无辜者继续承担后果；成功也会使沈知微失去职业、信任和回到原来生活的可能。",
                "must_have_elements": [f"开篇出现与《{title}》直接相关的异常事件", "中点揭示书名含义与主角旧选择有关", "结局用不可撤销的行动完整兑现书名"],
                "avoid_drift": ["不得把核心矛盾替换成与书名无关的通用升级打怪", "不得只在标题或结尾口头提到书名意象而不让它改变人物选择"],
            },
            "characters": [
                {
                    "name": "沈知微",
                    "role": "主视角人物",
                    "goal": "查清标题事件的真相，并阻止同类伤害再次发生。",
                    "conflict": "她需要的证据掌握在自己不信任的人手中，继续调查又会暴露她曾经的错误选择。",
                    "appearance": "二十九岁左右，身形清瘦，黑发总挽在颈后，鼻梁架着细边眼镜，浅灰风衣的袖口沾着洗不净的墨迹，右手食指有长期握笔留下的薄茧。",
                    "personality": "克制、观察敏锐，习惯先验证再表态；被逼到道德边界时会异常固执。",
                    "background": "曾因一次看似合理的沉默保住生活，却让别人承担了后果，此后刻意远离相关人和事。",
                    "status": "维持着表面稳定，尚不知道旧事已经重新进入现实。",
                    "knowledge": "知道自己的经历和一部分旧事，不知道谁利用了她当年的选择。",
                    "biography": "沈知微长期靠谨慎和专业能力维持生活。多年前她在证据不足时选择沉默，自以为避免了更坏结果，却一直回避受害者后来的人生。标题事件重新出现后，她最擅长的求证能力反而迫使她面对自己的责任；她越接近真相，越无法继续把自己当成旁观者。",
                    "motivation": "表层是查明事实，深层是证明自己当年的沉默并非懦弱，同时渴望获得承担错误的勇气。",
                    "flaw": "过度依赖证据确定性，用理性拖延必须及时做出的道德选择。",
                    "character_arc": "从把安全与正确等同，转变为愿意在无法保证结果时仍承担选择及其代价。",
                    "secret": "她保存着一份足以改变旧案判断、也会证明自己曾隐瞒信息的材料。",
                    "relationships": "需要陆沉舟的行动能力却不信任他的动机；与周闻远既有旧日恩情，也有责任冲突。",
                    "voice": "说话简短，爱追问具体时间和证据；情绪越强烈语气反而越平静，行动前会确认退路。",
                },
                {
                    "name": "周闻远",
                    "role": "主线阻力",
                    "goal": "维护自己的利益和既有秩序。",
                    "conflict": "必须隐藏一个会改变局面的秘密。",
                    "appearance": "四十多岁，肩背挺直，鬓角梳得一丝不乱，深色大衣配银边袖扣，右眉尾横着一道很浅的旧疤，皮鞋总擦得发亮。",
                    "personality": "理性、强势，做事有明确规则。",
                    "background": "与主线事件存在直接利益关系。",
                    "status": "掌握先手，暂时占据优势。",
                    "knowledge": "知道部分真相，但低估主角的选择。",
                    "biography": "周闻远是现有秩序的受益者，也是最熟悉其脆弱之处的人。他曾在关键时刻帮助沈知微，因此相信自己有资格要求她保持沉默。他并不以伤害为乐，而是确信公开真相会造成更大混乱；为了证明这一点，他不断制造只能在坏与更坏之间选择的局面。",
                    "motivation": "保护由自己建立的秩序，并证明过去所有牺牲都是必要的。",
                    "flaw": "把控制局面误认为承担责任，无法接受别人拥有不受他安排的选择权。",
                    "character_arc": "从从容掌控信息，到因主角拒绝按其规则选择而不断加码，最终直面秩序究竟保护了谁。",
                    "secret": "他并非标题事件的唯一制造者，却掩盖了最早可以阻止后果的节点。",
                    "relationships": "把沈知微视为需要保护也需要约束的旧人；利用陆沉舟的急迫制造两人不信任。",
                    "voice": "措辞完整有礼，很少直接威胁；总给出两个看似理性的选项，并隐藏真正存在的第三条路。",
                },
                {
                    "name": "陆沉舟",
                    "role": "不稳定盟友与行动支点",
                    "goal": "在证据消失前找到标题事件中被隐去的责任人。",
                    "conflict": "他需要沈知微掌握的材料，却认为她过去的沉默使她不值得信任。",
                    "appearance": "二十七八岁，肩宽腿长，短发剪得很利落，旧夹克的肘部磨白，右腕缠着褪色护腕，指节上留着新旧不一的擦伤。",
                    "personality": "行动快、容忍风险，面对权威时带有攻击性，但会保护已经作出承诺的人。",
                    "background": "他的家人曾承受旧事后果，因此比任何人都迫切，也容易只接受符合自身判断的证据。",
                    "status": "已经追查一段时间，手里有碎片线索但缺少能闭合因果链的证据。",
                    "knowledge": "知道受害一方的经历，不知道沈知微保存的材料及周闻远最早的介入。",
                    "biography": "陆沉舟从旧事受害者家属的身份进入调查，靠强硬和冒险弥补资源不足。他把迟疑视为背叛，因此一开始只把沈知微当成取得证据的渠道。共同经历失败后，他逐渐发现自己的急迫同样会让无辜者付出代价，必须学会区分追责、报复与真正阻止伤害。",
                    "motivation": "表层是替家人讨回公道，深层是摆脱自己当年无力保护重要之人的羞耻。",
                    "flaw": "把速度和决绝当成诚意，容易在信息不足时把人推向敌对位置。",
                    "character_arc": "从只要求别人交出真相，转变为愿意为获取和公开真相的方式承担共同责任。",
                    "secret": "他曾擅自使用一条来源不明的线索，导致一名证人暴露。",
                    "relationships": "需要沈知微的判断力却无法原谅她的旧选择；被周闻远抓住复仇心理并加以利用。",
                    "voice": "多用短句和反问，倾向边走边谈；不耐烦时会移动关键物件，以行动迫使对方表态。",
                },
            ],
            "plot_arcs": [
                {"title": "第一卷：标题事件重现", "synopsis": f"《{title}》相关的异常事件打破沈知微的稳定生活，她与陆沉舟因互相需要而结盟。调查第一次指向她保存的旧材料，周闻远迫使她在交出线索和保护生活之间选择；卷末她付出职业风险，决定继续查下去。", "sequence": 1},
                {"title": "第二卷：标题含义翻转", "synopsis": "调查证明旧案并非单一恶人的阴谋，而是一连串自保选择共同造成的后果。沈知微与陆沉舟因秘密暴露而决裂，周闻远取得主动；卷末两人分别承认自己的责任，重新建立有边界的合作。", "sequence": 2},
                {"title": "第三卷：兑现书名", "synopsis": f"周闻远制造最后的两难局面，试图证明公开《{title}》真相只会扩大伤害。沈知微放弃自保方案，与陆沉舟完成证据链并公开关键事实；结局解决现实危机，也让三人的选择共同解释书名。", "sequence": 3},
            ],
        }

    def generate_setup(self, work: dict[str, Any], profile: dict[str, Any] | None = None) -> dict[str, Any]:
        fallback = normalize_setup(work, self.setup(work))
        schema = {
            "story_bible": {
                "title_interpretation": "明确写出书名，解释字面指向、意象、类型承诺和结局兑现",
                "reader_promise": "读者因书名会期待的核心体验", "core_hook": "具体异常事件+主角为何不能置身事外",
                "core_conflict": "谁要什么、谁阻止、为何不能两全", "stakes": "失败损失与成功代价",
                "summary": "不少于300字，包含起因、升级、中点反转、最低谷、最终选择和结局因果",
                "theme": "", "world": "会限制人物选择的规则与代价", "ending": "具体事实结果、人物选择和代价",
                "style_rules": "", "must_have_elements": ["至少3个书名兑现元素"], "avoid_drift": ["至少2条防跑偏边界"],
            },
            "characters": [{
                "name": "真实姓名", "role": "", "story_function": "不可替代的剧情作用",
                "biography": "120-220字因果经历",
                "dramatic_core": {"goal": "外部目标", "motivation": "深层动机", "flaw": "会制造错误选择的缺陷", "conflict": "当前主要阻力"},
                "appearance": "60-120字，只写可见外貌，至少覆盖三类视觉细节",
                "personality": "40-90字，只写稳定行为倾向和压力下反应",
                "voice": "30-70字，只写措辞、句式、语速或标志性动作习惯",
                "arc": "起点-转折-终点", "facets": {},
            }],
            "plot_arcs": [{"title": "", "synopsis": "起点、关键因果、关系变化、代价和结束状态", "sequence": 1}],
        }
        result = self._llm_json(
            configured_prompt("setup"),
            json.dumps(
                {
                    "task": "生成故事初始化方案",
                    "work": {key: work.get(key, "") for key in ("title", "genre", "target_audience", "estimated_words", "writing_style", "premise")},
                    "inspiration_brief": (
                        ((work.get("inspiration_blueprint") or {}).get("content") or {}).get("creative_brief", {})
                        if isinstance(work.get("inspiration_blueprint"), dict) else {}
                    ),
                    "originality_requirements": (
                        ((work.get("inspiration_blueprint") or {}).get("originality") or {}).get("checks", [])
                        if isinstance(work.get("inspiration_blueprint"), dict) else []
                    ),
                    "schema": schema,
                },
                ensure_ascii=False,
            ), profile, 0.5,
        )
        model_output = isinstance(result, dict) and bool(result.get("story_bible"))
        output = normalize_setup(work, result) if model_output else fallback
        issues, score = evaluate_setup(work, output)
        if model_output and issues:
            repaired = self._llm_json(
                configured_prompt("setup_repair"),
                json.dumps({"work": work, "draft": output, "quality_issues": issues, "schema": schema}, ensure_ascii=False),
                profile, 0.35,
            )
            repaired_output = normalize_setup(work, repaired)
            repaired_issues, repaired_score = evaluate_setup(work, repaired_output)
            if repaired_score > score:
                output, issues, score = repaired_output, repaired_issues, repaired_score
        if model_output and issues:
            raise ValueError("故事方案未通过质量闸门：" + "；".join(issues[:6]))
        output["generation_source"] = "model" if model_output else "fallback"
        output["quality_score"] = score
        output["quality_issues"] = issues
        output["prompt_version"] = PROMPT_VERSION
        return output

    @staticmethod
    def _volume_outline_issue(scope: str, message: str, severity: str = "error") -> dict[str, str]:
        return {"scope": scope, "message": message, "severity": severity}

    @staticmethod
    def _outline_text(value: Any) -> str:
        return str(value or "").strip()

    @staticmethod
    def _outline_dict(value: Any) -> dict[str, Any]:
        return dict(value) if isinstance(value, dict) else {}

    @staticmethod
    def _outline_list(value: Any) -> list[str]:
        if not isinstance(value, list):
            return []
        return [str(item).strip() for item in value if str(item).strip()]

    def _volume_outline_fallback(
        self,
        work: dict[str, Any],
        volume: dict[str, Any],
        stages: list[dict[str, Any]],
    ) -> tuple[dict[str, Any], list[dict[str, Any]]]:
        """Produce an editable local draft when a model is unavailable.

        This fallback fills structural blanks but intentionally does not save
        anything.  It gives the author a usable form instead of hiding a whole
        volume because an external model or JSON response failed.
        """
        start = int(volume.get("start_chapter") or 1)
        end = int(volume.get("end_chapter") or start)
        title = self._outline_text(volume.get("title")) or f"第{int(volume.get('sequence') or 1)}卷"
        draft_volume = {
            **volume,
            "title": title,
            "synopsis": self._outline_text(volume.get("synopsis")) or (
                f"第{start}—{end}章围绕《{work.get('title') or '本作'}》的当前主线建立局面、"
                "逐步加压，并在卷末留下可继续推进的新问题。"
            ),
            "goal": self._outline_text(volume.get("goal")) or (
                f"在第{end}章前完成当前卷的阶段目标，获得可见但不越过后续卷边界的成果。"
            ),
            "opposition": self._outline_text(volume.get("opposition")) or (
                "主要阻力必须与当前时间线和已登场人物相符；不得提前引入未具备登场条件的势力或终局冲突。"
            ),
            "ending_state": self._outline_dict(volume.get("ending_state")) or {
                "summary": f"第{end}章形成阶段性成果，同时留下下一卷必须处理的新局面。",
            },
        }
        draft_stages: list[dict[str, Any]] = []
        for stage in stages:
            stage_start = int(stage.get("start_chapter") or start)
            stage_end = int(stage.get("end_chapter") or stage_start)
            stage_title = self._outline_text(stage.get("title")) or f"第{int(stage.get('sequence') or 1)}阶段"
            draft_stages.append({
                **stage,
                "title": stage_title,
                "purpose": self._outline_text(stage.get("purpose")) or (
                    f"在第{stage_start}—{stage_end}章推进本卷目标，并让人物承担一项可见的行动代价。"
                ),
                "entry_state": self._outline_dict(stage.get("entry_state")) or {
                    "summary": f"承接第{stage_start}章前已经确认的局面。",
                },
                "exit_state": self._outline_dict(stage.get("exit_state")) or {
                    "summary": f"到第{stage_end}章时形成下一阶段可承接的具体变化。",
                },
                "allowed_payoffs": self._outline_list(stage.get("allowed_payoffs")) or [
                    "获得一项与本阶段任务对应的局部信息、资源、关系或行动空间。",
                ],
                "forbidden_payoffs": self._outline_list(stage.get("forbidden_payoffs")) or [
                    "不得提前完成本卷终局、彻底击败主要对手或获得后续卷的核心成果。",
                ],
                "prerequisites": self._outline_list(stage.get("prerequisites")),
            })
        return draft_volume, draft_stages

    def generate_volume_outline(
        self,
        work: dict[str, Any],
        volume_id: str,
        *,
        target_stage_id: str | None = None,
        instruction: str = "",
        profile: dict[str, Any] | None = None,
        on_progress: Callable[[str], None] | None = None,
        is_cancelled: Callable[[], bool] | None = None,
    ) -> tuple[dict[str, Any], dict[str, int], str]:
        """Generate a review-only volume-outline draft with immutable coordinates.

        The returned volume/stages are never persisted by this method.  Bounds,
        identifiers and ordering are copied from the stored narrative structure
        so a model cannot silently turn a 40-chapter volume into a 12-chapter
        one (or move a stage while trying to improve prose).
        """
        volume = next((item for item in work.get("story_volumes", []) if item.get("id") == volume_id), None)
        if not volume:
            raise ValueError("指定分卷不存在")
        all_stages = sorted(
            [item for item in work.get("narrative_stages", []) if item.get("volume_id") == volume_id],
            key=lambda item: (int(item.get("sequence") or 0), int(item.get("start_chapter") or 0)),
        )
        if not all_stages:
            raise ValueError("当前分卷尚未建立叙事阶段，请先重建分卷结构")
        target_stages = [item for item in all_stages if not target_stage_id or item.get("id") == target_stage_id]
        if target_stage_id and not target_stages:
            raise ValueError("指定叙事阶段不存在")

        fallback_volume, fallback_stages = self._volume_outline_fallback(work, volume, all_stages)
        stage_schema = {
            "id": "必须原样使用 fixed_stage_coordinates 中的 id",
            "sequence": "必须原样使用 fixed_stage_coordinates 中的 sequence",
            "title": "阶段名",
            "purpose": "本阶段具体要完成的剧情任务",
            "entry_state": {"summary": "进入阶段时人物、资源、关系或局势"},
            "exit_state": {"summary": "离开阶段时形成的具体新状态"},
            "allowed_payoffs": ["允许在本阶段获得的小回报"],
            "forbidden_payoffs": ["禁止提前兑现的成果"],
            "prerequisites": ["进入本阶段必须已经成立的前提"],
        }
        fixed_coordinates = [
            {
                "id": stage.get("id"), "sequence": stage.get("sequence"),
                "start_chapter": stage.get("start_chapter"), "end_chapter": stage.get("end_chapter"),
            }
            for stage in target_stages
        ]
        compact_characters = [
            {key: item.get(key, "") for key in ("name", "role", "story_function", "goal", "conflict", "status", "arc", "character_arc")}
            for item in (work.get("characters") or [])
        ]
        result, usage = self._llm_json_with_usage(
            configured_prompt("volume_outline"),
            json.dumps({
                "task": "重写指定叙事阶段" if target_stage_id else "生成整卷卷纲草稿",
                "author_instruction": instruction.strip(),
                "important_rule": (
                    "卷范围和阶段范围由系统固定。不要输出或修改 start_chapter/end_chapter；"
                    "章节大纲的单次12章窗口与本卷长度无关。existing_chapter_plans 不在本次上下文中，不能引用。"
                ),
                "work": {key: work.get(key, "") for key in ("title", "genre", "target_audience", "estimated_words", "writing_style", "premise", "target_chapter_count")},
                "story_bible": work.get("story_bible") or {},
                "characters": compact_characters,
                "plot_arcs": [
                    {key: item.get(key, "") for key in ("sequence", "title", "synopsis")}
                    for item in (work.get("plot_arcs") or [])
                ],
                "story_phases": [
                    {key: item.get(key, "") for key in ("phase_key", "name", "start_day", "end_day", "rules")}
                    for item in (work.get("story_phases") or [])
                ],
                "factions": [
                    {key: item.get(key, "") for key in ("name", "lifecycle", "formed_day", "first_appearance_chapter", "description")}
                    for item in (work.get("factions") or [])
                ],
                "future_plans": [
                    {key: item.get(key, "") for key in ("entity_type", "plan_type", "target_chapter", "content", "status")}
                    for item in (work.get("future_plans") or [])
                ],
                "target_volume": {
                    "id": volume.get("id"), "sequence": volume.get("sequence"), "title": volume.get("title"),
                    "start_chapter": volume.get("start_chapter"), "end_chapter": volume.get("end_chapter"),
                    "chapter_count": int(volume.get("end_chapter") or 0) - int(volume.get("start_chapter") or 1) + 1,
                    "current_draft": {key: volume.get(key, {}) for key in ("synopsis", "goal", "opposition", "ending_state")},
                },
                "neighboring_volumes": [
                    {key: item.get(key, "") for key in ("sequence", "title", "start_chapter", "end_chapter", "synopsis", "goal", "opposition")}
                    for item in (work.get("story_volumes") or []) if item.get("id") != volume_id
                ],
                "fixed_stage_coordinates": fixed_coordinates,
                "current_stage_drafts": [
                    {
                        "id": stage.get("id"), "sequence": stage.get("sequence"), "title": stage.get("title"),
                        "purpose": stage.get("purpose"), "entry_state": stage.get("entry_state") or {},
                        "exit_state": stage.get("exit_state") or {}, "allowed_payoffs": stage.get("allowed_payoffs") or [],
                        "forbidden_payoffs": stage.get("forbidden_payoffs") or [], "prerequisites": stage.get("prerequisites") or [],
                    }
                    for stage in target_stages
                ],
                "schema": (
                    {"stage": stage_schema}
                    if target_stage_id else
                    {
                        "volume": {
                            "title": "卷名", "synopsis": "本卷完整推进梗概", "goal": "卷末前必须完成的阶段目标",
                            "opposition": "主要阻力、对手强度与不可提前发生的边界", "ending_state": {"summary": "卷末不可逆状态"},
                        },
                        "stages": [stage_schema],
                    }
                ),
            }, ensure_ascii=False),
            profile,
            0.45,
            on_progress=on_progress,
            is_cancelled=is_cancelled,
        )

        issues: list[dict[str, str]] = []
        source = "model" if isinstance(result, dict) else "fallback"
        if source == "fallback":
            issues.append(self._volume_outline_issue(
                "volume", "模型不可用或未返回合法 JSON，当前显示的是可编辑的本地草稿；请补充后保存，或配置模型后重新生成。", "warning",
            ))
            draft_volume = fallback_volume
            draft_stages = fallback_stages if not target_stage_id else [
                item for item in fallback_stages if item.get("id") == target_stage_id
            ]
        else:
            raw_volume = self._outline_dict(result.get("volume"))
            draft_volume = dict(volume)
            if not target_stage_id:
                for field in ("title", "synopsis", "goal", "opposition"):
                    value = self._outline_text(raw_volume.get(field))
                    if value:
                        draft_volume[field] = value
                    else:
                        issues.append(self._volume_outline_issue("volume", f"模型未返回本卷{field}，已保留原值供你修改。"))
                ending_state = self._outline_dict(raw_volume.get("ending_state"))
                if ending_state:
                    draft_volume["ending_state"] = ending_state
                else:
                    issues.append(self._volume_outline_issue("volume", "模型未返回本卷卷末状态，已保留原值供你修改。"))
            else:
                # A targeted stage retry must not replace local volume edits.
                draft_volume = dict(volume)

            raw_stages = result.get("stages") if isinstance(result.get("stages"), list) else []
            if target_stage_id and isinstance(result.get("stage"), dict):
                raw_stages = [result["stage"]]
            raw_by_id = {
                str(item.get("id")): item for item in raw_stages
                if isinstance(item, dict) and str(item.get("id") or "").strip()
            }
            raw_by_sequence = {
                int(item.get("sequence")): item for item in raw_stages
                if isinstance(item, dict) and str(item.get("sequence") or "").strip().lstrip("-").isdigit()
            }
            draft_stages = []
            for stage in target_stages:
                scope = f"stage:{stage.get('id')}"
                raw_stage = raw_by_id.get(str(stage.get("id"))) or raw_by_sequence.get(int(stage.get("sequence") or 0))
                draft_stage = dict(stage)
                if not raw_stage:
                    issues.append(self._volume_outline_issue(scope, "模型未返回这个阶段；其原有内容已保留，可手动修改或单独重新生成。"))
                    draft_stages.append(draft_stage)
                    continue
                for field in ("title", "purpose"):
                    value = self._outline_text(raw_stage.get(field))
                    if value:
                        draft_stage[field] = value
                    else:
                        issues.append(self._volume_outline_issue(scope, f"模型未返回阶段{field}，已保留原值供你修改。"))
                for field in ("entry_state", "exit_state"):
                    value = self._outline_dict(raw_stage.get(field))
                    if value:
                        draft_stage[field] = value
                    else:
                        issues.append(self._volume_outline_issue(scope, f"模型未返回阶段{field}，已保留原值供你修改。"))
                for field in ("allowed_payoffs", "forbidden_payoffs", "prerequisites"):
                    value = self._outline_list(raw_stage.get(field))
                    if value or field == "prerequisites":
                        draft_stage[field] = value
                    else:
                        issues.append(self._volume_outline_issue(scope, f"模型未返回阶段{field}，已保留原值供你修改。"))
                # Coordinates, ownership and order remain from the persisted stage.
                draft_stages.append(draft_stage)

        if not target_stage_id:
            for field, label in (("title", "卷名"), ("synopsis", "本卷梗概"), ("goal", "本卷目标"), ("opposition", "主要对手与强度边界")):
                if not self._outline_text(draft_volume.get(field)):
                    issues.append(self._volume_outline_issue("volume", f"{label}仍为空，请作者补充后保存。"))
            if not self._outline_dict(draft_volume.get("ending_state")):
                issues.append(self._volume_outline_issue("volume", "卷末状态仍为空，请作者补充后保存。"))
        for stage in draft_stages:
            scope = f"stage:{stage.get('id')}"
            if not self._outline_text(stage.get("title")):
                issues.append(self._volume_outline_issue(scope, "阶段标题仍为空，请作者补充。"))
            if not self._outline_text(stage.get("purpose")):
                issues.append(self._volume_outline_issue(scope, "阶段任务仍为空，请作者补充。"))

        return {
            "volume": draft_volume,
            "stages": draft_stages,
            "target_stage_id": target_stage_id,
            "quality_issues": issues,
            "quality_ok": not any(item["severity"] == "error" for item in issues),
            "generation_source": source,
            "prompt_version": PROMPT_VERSION,
        }, usage, source

    def generate_outline(
        self,
        work: dict[str, Any],
        chapter_count: int,
        profile: dict[str, Any] | None = None,
        *,
        generation_context: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        readiness = outline_readiness_issues(work)
        if readiness:
            raise ValueError("暂不能生成章节大纲：" + "；".join(readiness) + "。请先重新生成或补全故事方案。")
        bible = work.get("story_bible") or {}
        characters = work.get("characters") or []
        plot_arcs = work.get("plot_arcs") or []
        generation_context = generation_context or {}
        from_chapter = int(generation_context.get("from_chapter") or 1)
        to_chapter = int(generation_context.get("to_chapter") or (from_chapter + chapter_count - 1))
        total_target_chapters = int(generation_context.get("total_target_chapters") or work.get("target_chapter_count") or chapter_count)
        pov_name = characters[0].get("name", "")
        fallback_items = []
        for chapter_no in range(from_chapter, to_chapter + 1):
            phase = "建立悬念" if chapter_no <= 3 else ("冲突升级" if chapter_no < total_target_chapters else "留下选择")
            fallback_items.append(
                {
                    "chapter_no": chapter_no,
                    "title": f"第{chapter_no}章 {phase}",
                    "goal": f"让{pov_name}在第{chapter_no}章完成一个不可逆的小选择。",
                    "conflict": f"{pov_name}的目标与书名契约中的现实限制发生正面冲突。",
                    "failure_cost": "失去关键证据、关系信任或下一步行动窗口。",
                    "beats": ["具体场景切入", "确认眼前目标", "阻力改变行动路径", "人物承担代价作出选择", "结尾形成下一步必须处理的新局面"],
                    "hook": "结尾留下一个新的信息或问题。",
                    "pov_character": pov_name,
                    "opening_state": {"time": "承接上一章的当日", "location": "上一章结束地点", "carry_over_action": "处理上一章留下的具体问题"},
                    "causal_beats": [
                        {"cause": "上一章留下的新证据或代价", "action": f"{pov_name}确认本章必须完成的具体目标", "obstacle": "信息不足限制判断", "consequence": "行动目标被拆成可执行步骤"},
                        {"cause": "目标已明确", "action": f"{pov_name}尝试取得关键资源或证据", "obstacle": "对手利用规则或关系设限", "consequence": "原计划失效，必须改变路径"},
                        {"cause": "原计划受阻", "action": f"{pov_name}选择承担一个具体风险", "obstacle": "已知秘密和资源不足造成代价", "consequence": "获得局部突破但关系或资源受损"},
                        {"cause": "局部突破出现", "action": "人物核实结果并决定是否公开或继续推进", "obstacle": "对手提出新的条件或威胁", "consequence": "人物知识与当前任务发生可记录变化"},
                        {"cause": "选择已作出", "action": "人物落实本章结尾行动", "obstacle": "结尾前出现无法立即解决的新问题", "consequence": "下一章的开场任务和钩子明确"},
                    ],
                    "knowledge_changes": [],
                    "state_changes": [],
                    "foreshadow_actions": [],
                    "forbidden_reveals": [],
                    "ending_state": {"location": "冲突后的新位置", "new_problem": "本章选择造成的新问题", "next_action": "下一章必须执行的行动"},
                    "appearing_characters": [pov_name],
                    "appearing_factions": [],
                    "task_progress": [{"task": "主线调查", "progress": "本章获得局部线索并产生新阻力"}],
                    "plot_arc": plot_arcs[min(len(plot_arcs) - 1, (chapter_no - 1) * len(plot_arcs) // max(1, total_target_chapters))].get("title", "主线"),
                    "title_promise_progress": f"改变人物对《{work['title']}》核心事件的理解，并让该理解影响下一步选择。",
                    "character_arc_progress": f"{pov_name}因本章代价暴露一个缺陷，并作出与开篇状态不同的选择。",
                    "story_day": chapter_no - 1,
                    "phase_key": "default",
                    "time_mode": "linear",
                    "start_time": "",
                    "end_time": "",
                    "previous_chapter_no": chapter_no - 1 if chapter_no > 1 else None,
                }
            )
        schema = [{
            "chapter_no": from_chapter, "title": "", "pov_character": "人物小传中的姓名", "goal": "", "conflict": "", "failure_cost": "失败造成的具体代价",
            "beats": ["具体可写场景"], "hook": "具体新局面",
            "opening_state": {"time": "", "location": "", "carry_over_action": ""},
            "causal_beats": [{"cause": "", "action": "", "obstacle": "", "consequence": ""}],
            "knowledge_changes": [], "state_changes": [], "foreshadow_actions": [], "forbidden_reveals": [],
            "ending_state": {"location": "", "new_problem": "", "next_action": ""}, "plot_arc": "卷级主线标题",
            "appearing_characters": [""], "appearing_factions": [], "task_progress": [{"task": "", "progress": ""}],
            "title_promise_progress": "本章如何建立/升级/兑现书名承诺", "character_arc_progress": "人物因选择产生的变化",
            "story_day": 0, "phase_key": "story_phases 中提供的 phase_key", "time_mode": "linear|flashback|parallel",
            "start_time": "可选精确开始时间", "end_time": "可选精确结束时间", "previous_chapter_no": 0,
        }]
        user = json.dumps(
            {
                "task": "生成章节大纲",
                "chapter_count": chapter_count,
                "absolute_chapter_range": {"from_chapter": from_chapter, "to_chapter": to_chapter},
                "total_target_chapters": total_target_chapters,
                "instruction": "chapter_no 必须使用 absolute_chapter_range 中的绝对章节号。当前范围只是全书的一小段；除非范围覆盖某卷或阶段的结束章节，禁止把本卷或全书高潮、最终决战、终局结算塞入本批。",
                "work": {key: work.get(key, "") for key in ("title", "genre", "target_audience", "estimated_words", "premise")},
                "story_bible": bible,
                "characters": characters,
                "plot_arcs": plot_arcs,
                "existing_chapters": [
                    {"chapter_no": item.get("chapter_no"), "title": item.get("title", "")}
                    for item in (work.get("chapters") or [])
                ],
                "existing_outline_context": [
                    {key: item.get(key) for key in ("chapter_no", "title", "story_day", "phase_key", "ending_state", "knowledge_changes", "state_changes")}
                    for item in (work.get("chapter_plans") or [])[-24:]
                ],
                "state_at_range_start": work.get("outline_state_context") or {},
                "future_planning_constraints": work.get("future_planning_context") or [],
                "story_phases": [
                    {key: phase.get(key) for key in ("phase_key", "name", "start_day", "end_day", "rules", "allowed", "forbidden")}
                    for phase in (work.get("story_phases") or [])
                ],
                "story_volumes": generation_context.get("volumes") or work.get("story_volumes") or [],
                "narrative_stages": generation_context.get("narrative_stages") or work.get("narrative_stages") or [],
                "schema": {"chapters": schema},
            },
            ensure_ascii=False,
        )
        result = self._llm_json(
            configured_prompt("outline"),
            user, profile, 0.45,
        )
        items = normalize_outline(result) if result is not None else fallback_items
        # Some OpenAI-compatible models number a local batch from 1 despite an
        # absolute-range instruction.  The item order remains meaningful, so
        # normalize that harmless transport error before quality validation.
        if [item.get("chapter_no") for item in items] == list(range(1, chapter_count + 1)) and from_chapter != 1:
            for offset, item in enumerate(items):
                item["chapter_no"] = from_chapter + offset
        issues, score = evaluate_outline(work, items, chapter_count, expected_from_chapter=from_chapter)
        # Outline checks are author-facing diagnostics, not a publication gate.
        # Saving the first complete model response prevents a long retry from
        # discarding every usable chapter merely because one optional field is
        # weak.  Hard story-state conflicts remain enforced by the job layer.
        return {
            "chapters": items,
            "generation_source": "model" if result is not None else "fallback",
            "quality_score": score,
            "quality_issues": issues,
            "prompt_version": PROMPT_VERSION,
        }

    def generate_trend_ideas(self, items: list[dict[str, Any]], profile: dict[str, Any] | None = None) -> dict[str, Any]:
        compact = [{key: item.get(key, "") for key in ("id", "title", "author", "category", "synopsis", "rank", "source")} for item in items]
        result = self._llm_json(
            configured_prompt("trend"),
            json.dumps({
                "task": "热门网文作品模型与原创灵感蓝图",
                "sources": compact,
                "schema": {
                    "trend_summary": "",
                    "rising_themes": [""],
                    "overcrowded_directions": [""],
                    "source_models": [{
                        "trend_item_id": "必须逐字使用 sources.id", "completeness": "low|medium|high",
                        "model": {"market_positioning": "", "narrative_engine": {"opening": "", "protagonist": "", "conflict": "", "stakes": ""}, "serial_engine": {"payoff_cadence": "", "hook_types": [""]}, "safe_signals": [""], "avoid_copying": [""]},
                    }],
                    "ideas": [{
                        "title": "", "genre": "", "audience": "", "hook": "", "premise": "", "synopsis": "", "differentiation": "", "risk": "",
                        "blueprint": {"market_signals": [""], "creative_direction": "", "transformation_contract": {"retain": [""], "change": [""], "entity_rules": {"characters": "", "places": "", "items": ""}, "avoid": [""]}, "story_seed": {"hook": "", "premise": "", "reader_promise": ""}},
                    }],
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
            configured_prompt("extraction"),
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
            model_configured = bool((profile or {}).get("provider") == "codex_auth" or (profile or {}).get("api_key") or LLM_API_KEY)
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
                "warnings": [
                    "已配置模型，但本次提取调用失败或返回格式无效；已使用本地低置信度提取，请检查 worker 日志后重试。"
                    if model_configured else
                    "当前未配置 LLM，使用本地低置信度提取；建议配置模型后重新提取。"
                ],
            }
        return self._normalize_extraction(result, chapter)

    def _write_chapter(
        self,
        work: dict[str, Any],
        chapter_no: int,
        mode: str,
        instruction: str = "",
        profile: dict[str, Any] | None = None,
        *,
        on_progress: Callable[[str], None] | None = None,
        is_cancelled: Callable[[], bool] | None = None,
    ) -> dict[str, Any]:
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
        context = build_chapter_generation_context(work, chapter_no)
        context_audit_id = record_context_audit(work["id"], chapter_no, "chapter", context)
        writer_context = {key: value for key, value in context.items() if key != "chapter_contract"}
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
            configured_prompt("chapter"),
            json.dumps(
                {
                    "task": "生成章节正文",
                    "mode": mode,
                    "mode_rules": mode_rules,
                    "author_instruction": instruction,
                    "chapter_contract": context["chapter_contract"],
                    "context": writer_context,
                    "existing_content": current_chapter.get("content", "") if current_chapter and mode == "rewrite" else "",
                    "schema": {"chapter_no": chapter_no, "title": "", "content": "", "continuity_warnings": []},
                },
                ensure_ascii=False,
            ),
            profile,
            0.82,
            on_progress=on_progress,
            is_cancelled=is_cancelled,
            # The editor does not render token deltas.  A complete response is
            # more reliable for long JSON chapters and preserves Thinking.
            stream=False,
        )
        if isinstance(result, dict) and result.get("content"):
            return {
                "chapter_no": chapter_no,
                "title": result.get("title", fallback_title),
                "content": result["content"],
                "continuity_warnings": result.get("continuity_warnings", []),
                "context_audit_id": context_audit_id,
                "generation_source": "llm",
                "prompt_version": PROMPT_VERSION,
            }
        return {
            "chapter_no": chapter_no,
            "title": fallback_title,
            "content": fallback_content,
            "continuity_warnings": ["模型未返回正文，使用本地 fallback。"],
            "context_audit_id": context_audit_id,
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
        record_context_audit(work["id"], chapter_no, "chapter_edit", context)
        result = self._llm_json(
            configured_prompt("editor"),
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

    def generate_chapter(
        self,
        work: dict[str, Any],
        chapter_no: int,
        mode: str,
        instruction: str = "",
        profile: dict[str, Any] | None = None,
        *,
        on_progress: Callable[[str], None] | None = None,
        is_cancelled: Callable[[], bool] | None = None,
    ) -> dict[str, Any]:
        """用 LangGraph 执行章节作者节点；责任编辑节点暂时停用。"""
        if mode != "rewrite":
            continuity_warnings = build_context(work, chapter_no).get("continuity_warnings", [])
            blocking = [
                warning for warning in continuity_warnings
                if "未来信息" in str(warning) or "重写章节" in str(warning)
            ]
            if blocking:
                raise ValueError("当前章节的前置状态尚未重建：" + "；".join(str(item) for item in blocking[:3]))

        def writer(state: ChapterGraphState) -> ChapterGraphState:
            return {
                **state,
                "data": self._write_chapter(
                    state["work"], state["chapter_no"], state["mode"], state["instruction"], profile,
                    on_progress=on_progress, is_cancelled=is_cancelled,
                ),
            }

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
        graph.set_entry_point("writer")
        graph.add_edge("writer", END)
        result = graph.compile().invoke({
            "work": work,
            "chapter_no": chapter_no,
            "mode": mode,
            "instruction": instruction,
            "data": {},
        })
        return result["data"]


engine = NovelEngine()
