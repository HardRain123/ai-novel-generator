"use client";

import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import type { FormEvent } from "react";

const desktopApiBase = typeof window === "undefined"
  ? null
  : new URLSearchParams(window.location.search).get("apiBase");
const API = desktopApiBase || process.env.NEXT_PUBLIC_API_BASE_URL || "http://localhost:8000/api";

type Work = {
  id: string; title: string; genre: string; target_audience: string; estimated_words: number;
  average_chapter_words?: number; target_chapter_count?: number;
  writing_style: string; premise: string; status: string; updated_at: string; model_profile_id?: string | null;
  story_bible?: StoryBible; characters?: Character[]; plot_arcs?: PlotArc[];
  chapter_plans?: ChapterPlan[]; chapters?: Chapter[]; foreshadows?: { id: string }[]; quality_reports?: QualityReport[];
  story_phases?: StoryPhase[]; story_volumes?: StoryVolume[]; narrative_stages?: NarrativeStage[]; factions?: Faction[]; goals?: StoryGoal[]; story_events?: StoryEvent[]; character_states?: CharacterState[]; long_term_facts?: LongTermFact[]; future_plans?: FuturePlan[]; fact_version?: number; outline_versions?: OutlineVersion[]; inspiration_blueprint?: InspirationBlueprint | null; inspiration_sources?: { source: string; title: string; source_url: string }[];
};
type StoryBible = { id?: string; summary: string; theme: string; world: string; ending: string; style_rules: string; title_interpretation: string; reader_promise: string; core_hook: string; core_conflict: string; stakes: string; must_have_elements: string[]; avoid_drift: string[]; generation_source?: string; repair_attempts?: number; parse_status?: string; quality_status?: string; quality_score?: number; quality_issues?: string[]; locked?: number | boolean };
type Character = { id: string; name: string; role: string; story_function?: string; biography: string; dramatic_core?: { goal?: string; motivation?: string; flaw?: string; conflict?: string }; appearance?: string; portrayal?: string; arc?: string; facets?: Record<string, { content?: string }>; goal: string; conflict: string; personality: string; background: string; status: string; knowledge: string; motivation: string; flaw: string; character_arc: string; secret: string; relationships: string; voice: string };
type PlotArc = { id: string; title: string; synopsis: string; sequence: number };
type StoryVolume = { id: string; sequence: number; title: string; start_chapter: number; end_chapter: number; target_words?: number; synopsis: string; goal: string; opposition: string; ending_state?: Record<string, unknown>; status?: string };
type NarrativeStage = { id: string; volume_id: string; sequence: number; title: string; start_chapter: number; end_chapter: number; purpose: string; entry_state?: Record<string, unknown>; exit_state?: Record<string, unknown>; allowed_payoffs?: string[]; forbidden_payoffs: string[]; prerequisites?: string[]; status?: string };
type VolumeOutlineIssue = { scope: string; message: string; severity: "warning" | "error" };
type VolumeOutlineDraft = { volume: StoryVolume; stages: NarrativeStage[]; target_stage_id?: string | null; quality_issues: VolumeOutlineIssue[]; quality_ok: boolean; generation_source: "model" | "fallback"; repair_attempts?: number; parse_status?: string; quality_status?: string; prompt_version?: string };
type ChapterPlan = { chapter_no: number; title: string; goal: string; conflict: string; failure_cost?: string; beats: string[]; hook: string; pov_character?: string; opening_state?: Record<string, unknown>; causal_beats?: Record<string, unknown>[]; knowledge_changes?: unknown[]; state_changes?: unknown[]; foreshadow_actions?: unknown[]; forbidden_reveals?: string[]; ending_state?: Record<string, unknown>; appearing_characters?: string[]; appearing_factions?: string[]; task_progress?: Record<string, unknown>[]; plot_arc?: string; title_promise_progress?: string; character_arc_progress?: string; story_day?: number | null; phase_key?: string; timeline_phase_key?: string; volume_id?: string; narrative_stage_id?: string; time_mode?: "linear" | "flashback" | "parallel"; start_time?: string; end_time?: string; previous_chapter_no?: number | null; fact_version?: number; outline_version?: number; calibration_status?: string; dependencies?: Record<string, unknown>[]; version?: number; stale_reason?: string };
type Chapter = { chapter_no: number; title: string; content: string; status: string; source_plan_version?: number; stale_reason?: string };
type StoryPhase = { phase_key: string; name: string; start_day?: number | null; end_day?: number | null; rules: string[]; locked: number | boolean };
type Faction = { id: string; name: string; precursor_name: string; lifecycle: string; formed_day?: number | null; first_appearance_chapter: number; description: string; state: Record<string, unknown> };
type StoryGoal = { id: string; title: string; status: string; priority: number; started_day?: number | null; ended_day?: number | null; progress?: Record<string, unknown> };
type StoryEvent = { id: string; chapter_no: number; story_day?: number | null; event_type: string; evidence: string };
type CharacterState = { character_id: string; state: Record<string, unknown> };
type LongTermFact = { id: string; entity_type: string; entity_id?: string | null; fact_key: string; value: Record<string, unknown>; source: string; locked: number | boolean };
type FuturePlan = { id: string; entity_type: string; entity_id?: string | null; plan_type: string; target_chapter?: number | null; content: Record<string, unknown>; status: string };
type OutlineVersion = { id: string; version_no: number; mode: string; from_chapter: number; to_chapter: number; fact_version: number; status: string };
type Issue = { kind: string; severity: string; message: string; evidence?: string; suggestion?: string };
type QualityReport = { chapter_no: number; score: number; issues: Issue[] };
type StateChange = { id: string; character_name: string; field: string; old_value: unknown; new_value: unknown; evidence: string; confidence: number; status: string };
type TimelineCandidate = { id: string; title: string; description: string; story_time_text: string; time_type: string; location: string; participants: string[]; evidence: string; confidence: number; review_status: string };
type AliasCandidate = { id: string; character_name: string; alias: string; status: string };
type ForeshadowCandidate = { id: string; clue: string; kind: string; planted_chapter: number; expected_reveal_chapter: number; evidence: string; confidence: number; status: string };
type StateExtraction = { id: string; status: string; model: string; warning: string; chapter_version_id: string; job_id?: string; characters: StateChange[]; aliases: AliasCandidate[]; timeline_events: TimelineCandidate[]; foreshadows?: ForeshadowCandidate[] };
type ModelProfile = { id: string; name: string; provider: string; base_url: string; model: string; reasoning_effort: string; timeout_seconds: number; is_default: number | boolean; has_api_key: boolean; api_key_masked: string; last_test_status: string; last_test_at?: string };
type ProxySettings = { enabled: boolean; host: string; port: number; ok: boolean; message: string; updated_at?: string | null };
type PromptSetting = { key: string; title: string; stage: string; description: string; content: string; default_content: string; is_customized: boolean; updated_at?: string | null };
type GenerationMetrics = { queue_ms?: number | null; run_ms?: number | null; model_ms?: number | null };
type GenerationJob = { id: string; kind: string; status: string; error: string; output: Record<string, unknown>; progress: number; stage: string; stage_label: string; message: string; model_profile_id?: string | null; resolved_provider?: string; resolved_model?: string; started_at?: string | null; model_started_at?: string | null; metrics?: GenerationMetrics };
type ModelCallStats = { total: number; today: number; success: number; failed: number; success_rate: number; avg_duration_ms?: number | null; p95_duration_ms?: number | null; total_tokens: number; failure_categories?: Record<string, number> };
type ModelCall = { id: string; work_id?: string | null; work_title?: string | null; generation_job_id?: string | null; generation_task_url?: string | null; model_profile_id?: string | null; call_kind: string; task_name?: string; planning_step?: string | null; item_key?: string | null; provider: string; model: string; base_url: string; status: string; transmission_status?: string; parse_status?: string; quality_status?: string; adoption_status?: string; failure_category?: string | null; generation_status?: string | null; repair_of_call_id?: string | null; context_char_counts?: Record<string, number>; context_chars_total?: number; context_char_shares?: Record<string, number>; effective_parameters?: Record<string, unknown>; error: string; started_at: string; first_output_at?: string | null; completed_at?: string | null; duration_ms?: number | null; first_output_ms?: number | null; input_tokens?: number | null; output_tokens?: number | null; total_tokens?: number | null; created_at: string; request_chars?: number | null; response_chars?: number | null };
type ModelCallDetail = Omit<ModelCall, "request_chars" | "response_chars"> & { request: unknown; response: unknown; response_text: string; request_chars?: number | null; response_chars?: number | null; call_chain?: ModelCall[]; chain_root_call_id?: string };
type PlanningContent = Record<string, unknown>;
type PlanningRosterItem = PlanningContent & { item_key: string; name?: string; role?: string; story_function?: string; relationship_to_protagonist?: string };
type PlanningCandidate = PlanningContent & { title?: string; target_experience?: string; protagonist_principle?: string; power_curve?: string; payoff_cadence?: string; moral_boundary?: string; forbidden?: string[] };
type PlanningArtifact = { id: string; step: string; item_key: string; content: PlanningContent; status: string; version: number; source: string; feedback: string; checks: { blocking?: string[]; warnings?: string[]; ok?: boolean }; parent_versions: Record<string, number>; input_tokens?: number | null; output_tokens?: number | null; total_tokens?: number | null; model?: string };
type PlanningStep = { step: string; label: string; description: string; status: string; items: PlanningArtifact[] };
type PlanningSnapshot = { id: string; artifact_id: string; step: string; item_key: string; content: PlanningContent; status: string; version: number; source: string; feedback: string; checks: PlanningContent; created_at: string };
type PlanningCoverage = { blocking: string[]; warnings: string[]; full_book_ready: boolean; coverage: { estimated_words: number; average_chapter_words: number; target_chapters: number; suggested_volume_count: number; planned_volume_count: number; planned_chapters: number; planned_words: number; coverage_ratio: number; chapter_estimate: string; opening_planned?: boolean; escalation_planned?: boolean; midpoint_planned?: boolean; low_point_planned?: boolean; final_reckoning_planned?: boolean; summary_scope?: string; single_volume_complete?: boolean } };
type PlanningSession = { id: string; work_id: string; status: string; current_step: string; preset: string; steps: PlanningStep[]; artifacts: PlanningArtifact[]; usage: { input_tokens: number; output_tokens: number; total_tokens: number; known: boolean }; coverage_checks?: PlanningCoverage; presets?: Record<string, PlanningContent> };
type Foreshadow = { id: string; clue: string; kind: string; planted_chapter: number; expected_reveal_chapter: number; actual_reveal_chapter: number; status: string; note: string; evidence: string };
type TrendItem = { id: string; source: string; rank: number; board: string; category: string; title: string; author: string; synopsis: string; metric_label: string; metric_value: string; source_url: string; captured_at: string };
type TrendSourceModel = { id?: string; trend_item_id: string; completeness: string; model: { market_positioning?: string; narrative_engine?: { opening?: string; protagonist?: string; conflict?: string; stakes?: string }; serial_engine?: { payoff_cadence?: string; hook_types?: string[] }; safe_signals?: string[]; avoid_copying?: string[] } };
type InspirationBrief = { market_signals?: string[]; creative_direction?: string; transformation_contract?: { retain?: string[]; change?: string[]; entity_rules?: { characters?: string; places?: string; items?: string }; avoid?: string[] }; story_seed?: { hook?: string; premise?: string; reader_promise?: string } };
type InspirationBlueprint = { id: string; analysis_id?: string; content: { title?: string; genre?: string; audience?: string; hook?: string; premise?: string; synopsis?: string; differentiation?: string; creative_brief?: InspirationBrief }; originality: { status?: string; checks?: string[]; risk?: string } };
type TrendIdea = { title: string; genre: string; audience: string; hook: string; premise: string; synopsis: string; differentiation: string; risk: string; blueprint_id?: string; blueprint?: InspirationBrief };

type ApiOptions = RequestInit & { timeoutMs?: number };

async function api<T>(path: string, options: ApiOptions = {}): Promise<T> {
  const { timeoutMs = 15000, ...requestOptions } = options;
  const controller = new AbortController();
  const timeout = window.setTimeout(() => controller.abort(), timeoutMs);
  try {
    const response = await fetch(`${API}${path}`, { headers: { "Content-Type": "application/json" }, ...requestOptions, cache: "no-store", signal: requestOptions.signal || controller.signal });
    const contentType = response.headers.get("content-type") || "";
    const data = contentType.includes("application/json") ? await response.json() : { detail: await response.text() };
    if (!response.ok) throw new Error(data.detail || "请求失败");
    return data as T;
  } catch (error) {
    if ((error as Error).name === "AbortError") throw new Error("连接后端超时，请确认 start.bat 已正常启动，或查看 logs 目录中的错误日志。");
    throw error;
  } finally {
    window.clearTimeout(timeout);
  }
}

async function waitForGenerationJob(workId: string, initial: GenerationJob, onUpdate: (job: GenerationJob) => void) {
  let job = initial;
  onUpdate(job);
  for (let attempt = 0; attempt < 900 && (job.status === "queued" || job.status === "running" || job.status === "cancel_requested"); attempt += 1) {
    await new Promise((resolve) => window.setTimeout(resolve, 1000));
    job = await api<GenerationJob>(`/works/${workId}/generation-jobs/${initial.id}`);
    onUpdate(job);
  }
  if (job.status !== "completed") throw new Error(job.error || "生成任务未完成");
  return job.output;
}

async function runGenerationJob(workId: string, kind: "setup" | "outline" | "volume_outline" | "chapter" | "state_extraction", payload: Record<string, unknown>, modelProfileId: string | null, onUpdate: (job: GenerationJob) => void) {
  const created = await api<GenerationJob>(`/works/${workId}/generation-jobs`, {
    method: "POST",
    body: JSON.stringify({ kind, payload, model_profile_id: modelProfileId, idempotency_key: `${kind}-${Date.now()}` }),
  });
  return waitForGenerationJob(workId, created, onUpdate);
}

async function waitForStateExtraction(workId: string, extraction: StateExtraction, onReady: (value: StateExtraction) => void) {
  if (!extraction.job_id) return;
  for (let attempt = 0; attempt < 900; attempt += 1) {
    await new Promise((resolve) => window.setTimeout(resolve, 1000));
    const job = await api<GenerationJob>(`/works/${workId}/generation-jobs/${extraction.job_id}`);
    if (job.status === "failed" || job.status === "canceled") return;
    if (job.status !== "completed") continue;
    const data = await api<{ items: StateExtraction[] }>(`/works/${workId}/state-extractions`);
    const ready = data.items.find((item) => item.chapter_version_id === extraction.chapter_version_id && item.id !== extraction.id && item.status !== "queued");
    if (ready) { onReady(ready); return; }
  }
}

function downloadWork(work: Work) {
  const lines = [`# ${work.title}`, "", `题材：${work.genre || "未设"}`, `一句话设想：${work.premise || "未设"}`, "", "## 故事档案", `- 梗概：${work.story_bible?.summary || ""}`, `- 主题：${work.story_bible?.theme || ""}`, `- 世界观：${work.story_bible?.world || ""}`, `- 结局方向：${work.story_bible?.ending || ""}`, "", "## 主要人物"];
  for (const character of work.characters || []) lines.push(`### ${character.name}`, character.biography || "", `- 身份：${character.role}`, character.story_function ? `- 剧情作用：${character.story_function}` : "", character.appearance || character.portrayal ? `- 外貌：${character.appearance || character.portrayal}` : "", character.personality ? `- 性格：${character.personality}` : "", character.voice ? `- 语言习惯：${character.voice}` : "", `- 目标：${character.dramatic_core?.goal || character.goal}`, `- 深层动机：${character.dramatic_core?.motivation || character.motivation}`, `- 缺陷：${character.dramatic_core?.flaw || character.flaw}`, `- 人物弧：${character.arc || character.character_arc}`, ...Object.entries(character.facets || {}).map(([key, value]) => `- ${key}：${value?.content || ""}`), "");
  lines.push("## 章节");
  for (const chapter of work.chapters || []) lines.push(`### ${chapter.title || `第${chapter.chapter_no}章`}`, "", chapter.content, "");
  const blob = new Blob([lines.join("\n")], { type: "text/markdown;charset=utf-8" });
  const url = URL.createObjectURL(blob); const link = document.createElement("a");
  link.href = url; link.download = `${work.title || "作品"}.md`; link.click(); URL.revokeObjectURL(url);
}

export default function Home() {
  const [works, setWorks] = useState<Work[]>([]);
  const [selectedId, setSelectedId] = useState("");
  const [creatingWork, setCreatingWork] = useState(false);
  const [work, setWork] = useState<Work | null>(null);
  const [tab, setTab] = useState("overview");
  const [globalView, setGlobalView] = useState<"work" | "trends" | "settings" | "calls">("work");
  const [loading, setLoading] = useState(true);
  const [busy, setBusy] = useState("");
  const [error, setError] = useState("");
  const [healthMode, setHealthMode] = useState<"live" | "demo">("demo");
  const [profiles, setProfiles] = useState<ModelProfile[]>([]);
  const [activeJob, setActiveJob] = useState<GenerationJob | null>(null);
  const [chapterCount, setChapterCount] = useState(12);
  const [chapterNo, setChapterNo] = useState(1);
  const [draft, setDraft] = useState<Chapter | null>(null);
  const [stateDiff, setStateDiff] = useState<StateExtraction | null>(null);
  const [planning, setPlanning] = useState<PlanningSession | null>(null);
  const planningRequestVersion = useRef(0);

  useEffect(() => {
    const timer = window.setTimeout(() => {
      const path = window.location.pathname;
      if (path.startsWith("/trends")) setGlobalView("trends");
      else if (path.startsWith("/settings/models")) setGlobalView("settings");
      else if (path.startsWith("/model-calls")) setGlobalView("calls");
      else {
        const match = path.match(/^\/works\/([^/]+)/);
        if (match) setSelectedId(decodeURIComponent(match[1]));
      }
    }, 0);
    return () => window.clearTimeout(timer);
  }, []);

  const refreshWorks = useCallback(async () => {
    const data = await api<{ items: Work[] }>("/works");
    setWorks(data.items);
    if (!selectedId && !creatingWork && data.items[0]) setSelectedId(data.items[0].id);
  }, [selectedId, creatingWork]);

  const loadWork = useCallback(async (id: string) => {
    if (!id) return;
    const data = await api<Work>(`/works/${id}`);
    setWork(data);
    const first = data.chapters?.[0]?.chapter_no || data.chapter_plans?.[0]?.chapter_no || 1;
    setChapterNo(first);
    setDraft(data.chapters?.find((item) => item.chapter_no === first) || null);
  }, []);

  useEffect(() => {
    const timer = window.setTimeout(() => { refreshWorks().catch((e) => setError(e.message)).finally(() => setLoading(false)); }, 0);
    return () => window.clearTimeout(timer);
  }, [refreshWorks]);
  useEffect(() => {
    if (!selectedId) return;
    const timer = window.setTimeout(() => { loadWork(selectedId).catch((e) => setError(e.message)); }, 0);
    return () => window.clearTimeout(timer);
  }, [selectedId, loadWork]);
  useEffect(() => {
    api<{ mode: "live" | "demo" }>("/health").then((data) => setHealthMode(data.mode)).catch(() => undefined);
    api<{ items: ModelProfile[] }>("/model-profiles").then((data) => setProfiles(data.items)).catch(() => undefined);
  }, []);
  useEffect(() => {
    if (!selectedId) return;
    let disposed = false;
    const resumeActiveJob = async () => {
      const data = await api<{ items: GenerationJob[] }>(`/works/${selectedId}/generation-jobs?active=true`);
      const initial = data.items[0] || null;
      if (disposed) return;
      setActiveJob(initial);
      if (!initial) return;
      if (initial.kind === "setup" || initial.kind === "outline" || initial.kind === "volume_outline" || initial.kind === "chapter") setBusy(initial.kind);
      else if (initial.kind === "planning_step" || initial.kind === "planning_character_batch") setBusy("planning");

      let current = initial;
      while (!disposed && (current.status === "queued" || current.status === "running" || current.status === "cancel_requested")) {
        await new Promise((resolve) => window.setTimeout(resolve, 1000));
        current = await api<GenerationJob>(`/works/${selectedId}/generation-jobs/${initial.id}`);
        if (!disposed) setActiveJob(current);
      }
      if (disposed) return;
      await loadWork(selectedId);
      await refreshWorks();
      setActiveJob(null);
      setBusy("");
      if (current.status === "completed") {
        if (initial.kind === "setup") setTab("assets");
        else if (initial.kind === "outline" || initial.kind === "volume_outline") setTab("outline");
        else if (initial.kind === "chapter") setTab("write");
      } else if (current.status === "failed") {
        setError(current.error || "生成任务失败");
      }
    };
    resumeActiveJob().catch((e) => {
      if (!disposed) { setBusy(""); setError((e as Error).message); }
    });
    return () => { disposed = true; };
  }, [selectedId, loadWork, refreshWorks]);
  const loadPlanning = useCallback(async (id: string) => {
    if (!id) return;
    const requestVersion = ++planningRequestVersion.current;
    const data = await api<PlanningSession>(`/works/${id}/planning-session`);
    if (requestVersion === planningRequestVersion.current) setPlanning(data);
  }, []);
  useEffect(() => {
    if (!selectedId) return;
    loadPlanning(selectedId).catch((e) => setError(e.message));
  }, [selectedId, loadPlanning]);

  const selectedPlan = useMemo(() => work?.chapter_plans?.find((item) => item.chapter_no === chapterNo), [work, chapterNo]);
  const latestReport = useMemo(() => work?.quality_reports?.find((item) => item.chapter_no === chapterNo), [work, chapterNo]);

  async function createWork(form: HTMLFormElement) {
    const formData = new FormData(form);
    setBusy("create"); setError("");
    try {
      const created = await api<Work>("/works", { method: "POST", body: JSON.stringify({
        title: formData.get("title"), genre: formData.get("genre"), target_audience: formData.get("audience"),
        estimated_words: Number(formData.get("words") || 100000), writing_style: formData.get("style"), premise: formData.get("premise"), model_profile_id: formData.get("model_profile_id") || null,
      }) });
      form.reset(); await refreshWorks(); setCreatingWork(false); setSelectedId(created.id); setWork(created); setTab("overview"); window.history.pushState({}, "", `/works/${created.id}`);
    } catch (e) { setError((e as Error).message); } finally { setBusy(""); }
  }

  async function generate(kind: "setup" | "outline", outlineRequest?: Record<string, unknown>) {
    if (!work) return;
    setBusy(kind); setError("");
    try {
      if (healthMode === "demo" && !window.confirm("当前未配置可用模型，将使用演示数据继续。确定继续吗？")) return;
      if (kind === "setup" && ((work.chapter_plans?.length || 0) > 0 || (work.chapters?.length || 0) > 0) && !window.confirm("重新生成故事方案会替换当前书名契约、主要人物和卷级主线；已有大纲与正文不会自动改写，之后需要重新生成大纲并检查正文。确定继续吗？")) return;
      if (kind === "outline" && (work.chapter_plans?.length || 0) > 0 && !window.confirm(`当前已有 ${work.chapter_plans?.length} 章大纲，重新生成会覆盖它。确定继续吗？`)) return;
      await runGenerationJob(work.id, kind, kind === "outline" ? (outlineRequest || { chapter_count: chapterCount, mode: "initial", from_chapter: 1, to_chapter: chapterCount }) : {}, work.model_profile_id || profiles.find((item) => item.is_default)?.id || null, setActiveJob);
      await loadWork(work.id); await refreshWorks(); setActiveJob(null);
      setTab(kind === "setup" ? "assets" : "outline");
    } catch (e) { setError((e as Error).message); } finally { setBusy(""); }
  }

  async function generateChapter() {
    if (!work) return;
    setBusy("chapter"); setError("");
    try {
      if (healthMode === "demo" && !window.confirm("当前未配置可用模型，将使用演示数据继续。确定继续吗？")) return;
      const output = await runGenerationJob(work.id, "chapter", { chapter_no: chapterNo, mode: "chapter" }, work.model_profile_id || profiles.find((item) => item.is_default)?.id || null, setActiveJob);
      const data = output as unknown as { data: Chapter; state_extraction: StateExtraction };
      await loadWork(work.id); setDraft(data.data); setStateDiff(data.state_extraction); setActiveJob(null);
      void waitForStateExtraction(work.id, data.state_extraction, setStateDiff).catch(() => undefined);
      setTab("write");
    } catch (e) { setError((e as Error).message); } finally { setBusy(""); }
  }

  async function saveChapter() {
    if (!work || !draft) return;
    setBusy("save"); setError("");
    try {
      const data = await api<{ work: Work; state_extraction: StateExtraction }>(`/works/${work.id}/chapters/${chapterNo}`, { method: "PATCH", body: JSON.stringify({ title: draft.title, content: draft.content, status: draft.status }) });
      setWork(data.work); setDraft(data.work.chapters?.find((item) => item.chapter_no === chapterNo) || draft); setStateDiff(data.state_extraction);
      void waitForStateExtraction(work.id, data.state_extraction, setStateDiff).catch(() => undefined);
    } catch (e) { setError((e as Error).message); } finally { setBusy(""); }
  }

  function selectChapter(no: number) {
    setChapterNo(no); setDraft(work?.chapters?.find((item) => item.chapter_no === no) || null); setStateDiff(null);
  }

  async function saveChapterPlan(chapterNoToSave: number, changes: Partial<ChapterPlan>) {
    if (!work) return;
    setBusy("outline-save"); setError("");
    try {
      const current = work.chapter_plans?.find((item) => item.chapter_no === chapterNoToSave);
      const updated = await api<Work>(`/works/${work.id}/chapter-plans/${chapterNoToSave}`, { method: "PUT", body: JSON.stringify({ ...changes, expected_version: current?.version }) });
      setWork(updated); await refreshWorks();
    } catch (e) { setError((e as Error).message); throw e; } finally { setBusy(""); }
  }

  async function generatePlanningStep(step: string, itemKey: string, feedback: string, preset: string) {
    if (!work) return;
    setBusy("planning"); setError("");
    try {
      const queued = await api<GenerationJob>(`/works/${work.id}/planning-steps/${encodeURIComponent(step)}/generate`, {
        method: "POST", body: JSON.stringify({ item_key: itemKey, feedback, preset, model_profile_id: work.model_profile_id || null, idempotency_key: `planning-${step}-${itemKey}-${Date.now()}` }),
      });
      await waitForGenerationJob(work.id, queued, setActiveJob);
      await loadPlanning(work.id); await loadWork(work.id); setActiveJob(null);
    } catch (e) { setError((e as Error).message); } finally { setBusy(""); }
  }

  async function generateVolumeOutline(volumeId: string, targetStageId?: string, instruction = ""): Promise<VolumeOutlineDraft> {
    if (!work) throw new Error("请先选择作品");
    setBusy("volume_outline"); setError("");
    try {
      const output = await runGenerationJob(
        work.id,
        "volume_outline",
        { volume_id: volumeId, target_stage_id: targetStageId || undefined, instruction },
        work.model_profile_id || profiles.find((item) => item.is_default)?.id || null,
        setActiveJob,
      ) as unknown as { data: VolumeOutlineDraft };
      // Generation only returns a draft.  Refreshing here picks up any repaired
      // target chapter count, but it never saves the draft into the work.
      await loadWork(work.id); await refreshWorks(); setActiveJob(null);
      return output.data;
    } catch (e) { setError((e as Error).message); throw e; } finally { setBusy(""); }
  }

  async function generateAllCharacterBiographies(preset: string, feedback: string) {
    if (!work) return;
    setBusy("planning"); setError("");
    try {
      const queued = await api<GenerationJob>(`/works/${work.id}/planning-steps/character/generate-all`, {
        method: "POST", body: JSON.stringify({ feedback, preset, model_profile_id: work.model_profile_id || null, idempotency_key: `planning-character-batch-${Date.now()}` }),
      });
      await waitForGenerationJob(work.id, queued, setActiveJob);
      await loadPlanning(work.id); await loadWork(work.id); setActiveJob(null);
    } catch (e) { setError((e as Error).message); } finally { setBusy(""); }
  }

  async function savePlanningArtifact(step: string, itemKey: string, content: PlanningContent, feedback: string) {
    if (!work) return;
    try {
      await api(`/works/${work.id}/planning-steps/${encodeURIComponent(step)}/${encodeURIComponent(itemKey)}`, { method: "PUT", body: JSON.stringify({ content, feedback }) });
      await loadPlanning(work.id);
    } catch (e) { setError((e as Error).message); throw e; }
  }

  async function restorePlanningSnapshot(snapshotId: string) {
    if (!work) return;
    try {
      const data = await api<PlanningSession>(`/works/${work.id}/planning-snapshots/${encodeURIComponent(snapshotId)}/restore`, { method: "POST" });
      setPlanning(data);
      setError("");
    } catch (e) { setError((e as Error).message); throw e; }
  }

  async function confirmPlanningArtifact(step: string, itemKey: string, candidateIndex?: number): Promise<PlanningSession | undefined> {
    if (!work) return;
    try {
      const data = await api<PlanningSession>(`/works/${work.id}/planning-steps/${encodeURIComponent(step)}/${encodeURIComponent(itemKey)}/confirm`, { method: "POST", body: JSON.stringify({ candidate_index: candidateIndex ?? null }) });
      setPlanning(data);
      return data;
    } catch (e) { setError((e as Error).message); throw e; }
  }

  async function finalizePlanningSession() {
    if (!work) return;
    setBusy("planning"); setError("");
    try {
      await api(`/works/${work.id}/planning-session/finalize`, { method: "POST" });
      await loadPlanning(work.id); await loadWork(work.id); setTab("assets");
    } catch (e) { setError((e as Error).message); } finally { setBusy(""); }
  }

  async function resetPlanningSession() {
    if (!work || !window.confirm("重置会清空当前故事档案、人物、卷级主线和章节大纲，正文存在时不会执行。确定继续吗？")) return;
    setBusy("planning"); setError("");
    try {
      await api(`/works/${work.id}/planning-session/reset`, { method: "POST", body: JSON.stringify({ confirm: true }) });
      await loadPlanning(work.id); await loadWork(work.id); setTab("assets");
    } catch (e) { setError((e as Error).message); } finally { setBusy(""); }
  }

  async function retryActiveJob() {
    if (!work || !activeJob) return;
    setBusy("retry"); setError("");
    try {
      const queued = await api<GenerationJob>(`/works/${work.id}/generation-jobs/${activeJob.id}/retry`, { method: "POST" });
      await waitForGenerationJob(work.id, queued, setActiveJob);
      await loadWork(work.id); await refreshWorks(); setActiveJob(null);
    } catch (e) { setError((e as Error).message); } finally { setBusy(""); }
  }

  async function setWorkProfile(profileId: string) {
    if (!work) return;
    try { const updated = await api<Work>(`/works/${work.id}`, { method: "PATCH", body: JSON.stringify({ model_profile_id: profileId || null }) }); setWork(updated); } catch (e) { setError((e as Error).message); }
  }

  function startNewWork() {
    if (busy) return;
    setCreatingWork(true); setSelectedId(""); setWork(null); setDraft(null); setStateDiff(null); setPlanning(null); setActiveJob(null); setTab("overview"); setError("");
    setGlobalView("work");
    window.history.pushState({}, "", "/");
  }

  async function deleteWork() {
    if (!work) return;
    if (!window.confirm(`确定删除作品《${work.title}》吗？作品、章节、故事档案和状态记录都会被永久删除，且无法恢复。`)) return;
    setBusy("delete"); setError("");
    try {
      await api(`/works/${work.id}`, { method: "DELETE" });
      const data = await api<{ items: Work[] }>("/works");
      setWorks(data.items);
      setWork(null); setSelectedId(""); setCreatingWork(!data.items.length); setDraft(null); setStateDiff(null); setPlanning(null); setActiveJob(null); setTab("overview");
      window.history.pushState({}, "", "/");
      if (data.items[0]) {
        setCreatingWork(false);
        setSelectedId(data.items[0].id);
        window.history.pushState({}, "", `/works/${data.items[0].id}`);
      }
    } catch (e) { setError((e as Error).message); } finally { setBusy(""); }
  }

  async function reviewStateItem(kind: "character" | "timeline" | "alias" | "foreshadow", id: string, action: "accept" | "reject") {
    if (!work || !stateDiff) return;
    setBusy("review"); setError("");
    try {
      const updated = await api<StateExtraction>(`/works/${work.id}/state-extractions/${stateDiff.id}/review`, {
        method: "POST",
        body: JSON.stringify({ items: [{ id, kind, action }] }),
      });
      setStateDiff(updated);
      await loadWork(work.id);
    } catch (e) { setError((e as Error).message); } finally { setBusy(""); }
  }

  async function retryStateExtraction() {
    if (!work) return;
    setBusy("state-extraction"); setError("");
    try {
      const queued = await api<StateExtraction>(`/works/${work.id}/chapters/${chapterNo}/extract-state`, { method: "POST" });
      setStateDiff(queued);
      void waitForStateExtraction(work.id, queued, setStateDiff).catch(() => undefined);
    } catch (e) { setError((e as Error).message); } finally { setBusy(""); }
  }

  if (loading) return <div className="loading">正在打开织梦台…</div>;

  const navigate = (view: "work" | "trends" | "settings" | "calls", path: string) => {
    window.history.pushState({}, "", path);
    setGlobalView(view);
  };

  return (
    <div className="shell">
      <aside className="sidebar">
        <p className="brand">织梦台</p>
        <p className="brand-note">AI 主写，作者把控，作品持续记忆</p>
        <div className="global-nav">
          <button className={`nav-button ${globalView === "work" ? "active" : ""}`} onClick={() => navigate("work", selectedId ? `/works/${selectedId}` : "/")}>我的作品</button>
          <button className={`nav-button ${globalView === "trends" ? "active" : ""}`} onClick={() => navigate("trends", "/trends")}>热门灵感</button>
          <button className={`nav-button ${globalView === "settings" ? "active" : ""}`} onClick={() => navigate("settings", "/settings/models")}>模型设置</button>
          <button className={`nav-button ${globalView === "calls" ? "active" : ""}`} onClick={() => navigate("calls", "/model-calls")}>模型调用</button>
        </div>
        <div className="side-label-row"><div className="side-label">我的作品</div><button className="nav-button new-work-button" onClick={startNewWork} disabled={!!busy}>＋ 新建作品</button></div>
        <div className="work-list">
          {works.map((item) => <button key={item.id} className={`work-item ${item.id === selectedId ? "active" : ""}`} onClick={() => { setCreatingWork(false); setSelectedId(item.id); navigate("work", `/works/${item.id}`); }}><strong>{item.title}</strong><span>{item.genre || "未设题材"} · {item.status === "writing" ? "写作中" : "草稿"}</span></button>)}
          {!works.length && <div className="empty-side">还没有作品。<br />从右侧创建第一本。</div>}
        </div>
        <div className="side-label">当前版本</div>
        <div className="empty-side">MVP 0.1<br />故事规划 · 章节写作 · 一致性检查</div>
      </aside>
      <main className="main">
        {globalView === "settings" ? <ModelSettings profiles={profiles} onChanged={() => Promise.all([api<{ items: ModelProfile[] }>("/model-profiles").then((data) => setProfiles(data.items)), api<{ mode: "live" | "demo" }>("/health").then((data) => setHealthMode(data.mode))]).then(() => undefined)} /> : globalView === "calls" ? <ModelCalls /> : globalView === "trends" ? <Trends profiles={profiles} onCreate={(created) => { setCreatingWork(false); navigate("work", `/works/${created.id}`); setSelectedId(created.id); setWork(created); }} /> : <>
        <div className="topbar">
          <div><div className="eyebrow">AI NOVEL STUDIO / MVP</div><h1>{work?.title || "开始你的第一部长篇"}</h1><p className="subtitle">先给 AI 一个方向，剩下的由故事规划、正文生成和作品状态共同推进。</p><span className={`connection ${healthMode}`}>{healthMode === "live" ? "● AI 已连接" : "● 演示模式：未配置模型"}</span>{work && <label className="work-model">本作品模型<select value={work.model_profile_id || ""} onChange={(e) => setWorkProfile(e.target.value)}><option value="">使用默认模型</option>{profiles.map((profile) => <option key={profile.id} value={profile.id}>{profile.name} · {profile.model}</option>)}</select></label>}</div>
          {work && <div className="top-actions"><button className="button" onClick={() => downloadWork(work)}>导出 Markdown</button><button className="button danger" onClick={deleteWork} disabled={!!busy}>删除作品</button><button className="button primary" onClick={() => setTab("assets")} disabled={!!busy}>{planning?.status === "completed" ? "查看故事档案" : planning?.current_step === "contract" ? "开始故事档案" : "继续故事档案"}</button><label className="chapter-count">章数<input type="number" min="1" max="200" value={chapterCount} onChange={(e) => setChapterCount(Math.max(1, Math.min(200, Number(e.target.value) || 12)))} /></label><button className="button dark" onClick={() => generate("outline")} disabled={!!busy || planning?.status !== "completed"}>{busy === "outline" ? "正在拆大纲…" : planning?.status === "completed" ? `生成 ${chapterCount} 章大纲` : "完成故事档案后生成大纲"}</button></div>}
        </div>
        {activeJob && <GenerationProgress job={activeJob} onCancel={() => api(`/works/${work?.id || selectedId}/generation-jobs/${activeJob.id}/cancel`, { method: "POST" }).then((job) => setActiveJob(job as GenerationJob)).catch((e) => setError(e.message))} onRetry={retryActiveJob} />}
        {error && <div className="notice">{error}</div>}
        {!work ? <CreatePanel busy={busy} profiles={profiles} onCreate={createWork} /> : <>
          <div className="grid"><div className="card stat span-4"><span className="stat-label">预计篇幅</span><span className="stat-value">{(work.estimated_words / 10000).toFixed(1)}万字</span></div><div className="card stat span-4"><span className="stat-label">章节进度</span><span className="stat-value">{work.chapters?.length || 0} <small className="muted">/ {work.chapter_plans?.length || "—"}</small></span></div><div className="card stat span-4"><span className="stat-label">作品资产</span><span className="stat-value">{(work.characters?.length || 0) + (work.foreshadows?.length || 0)} <small className="muted">项</small></span></div></div>
          <div className="tabs">{[["overview", "总览"], ["outline", "章节大纲"], ["state", "故事状态"], ["write", "写作台"], ["assets", "故事档案"], ["foreshadows", "伏笔"]].map(([key, label]) => <button key={key} className={`tab ${tab === key ? "active" : ""}`} onClick={() => setTab(key)}>{label}</button>)}</div>
          {tab === "overview" && <Overview work={work} planning={planning} onTab={setTab} />}
          {tab === "outline" && <OutlineV2 work={work} busy={busy} onSave={saveChapterPlan} onGenerate={(request) => generate("outline", request)} onGenerateVolume={generateVolumeOutline} onRefresh={async () => { await loadWork(work.id); await refreshWorks(); }} />}
          {tab === "state" && <StoryStateV2 work={work} onChanged={() => loadWork(work.id)} />}
           {tab === "assets" && (planning && planning.status !== "completed" ? <PlanningWizard work={work} planning={planning} busy={busy} onGenerate={generatePlanningStep} onGenerateAllCharacters={generateAllCharacterBiographies} onSave={savePlanningArtifact} onRestore={restorePlanningSnapshot} onConfirm={confirmPlanningArtifact} onFinalize={finalizePlanningSession} onReset={resetPlanningSession} /> : <Assets key={work.id} work={work} onSaved={() => loadWork(work.id)} />)}
          {tab === "foreshadows" && <Foreshadows work={work} onSaved={() => loadWork(work.id)} />}
          {tab === "write" && <Writing work={work} chapterNo={chapterNo} draft={draft} plan={selectedPlan} report={latestReport} stateDiff={stateDiff} busy={busy} onSelect={selectChapter} onGenerate={generateChapter} onSave={saveChapter} onChange={setDraft} onReview={reviewStateItem} onRetryState={retryStateExtraction} />}
        </>}
        </>}
      </main>
    </div>
  );
}

function formatDuration(milliseconds: number) {
  const totalSeconds = Math.max(0, Math.floor(milliseconds / 1000));
  return totalSeconds >= 60 ? `${Math.floor(totalSeconds / 60)}分${totalSeconds % 60}秒` : `${totalSeconds}秒`;
}

function generationStatusLabels(output: Record<string, unknown>) {
  const nested = output.data && typeof output.data === "object" && !Array.isArray(output.data)
    ? output.data as Record<string, unknown>
    : {};
  const pick = (key: string) => output[key] ?? nested[key];
  const source = String(pick("generation_source") || "");
  const parseStatus = String(pick("parse_status") || "");
  const qualityStatus = String(pick("quality_status") || "");
  return {
    source: source === "fallback"
      ? "演示模板（未调用模型）"
      : source === "model" && parseStatus === "repaired"
        ? "模型返回成功（结构已修复）"
        : source === "model"
          ? "模型返回成功"
          : "",
    quality: qualityStatus === "passed"
      ? "内容通过质检"
      : qualityStatus === "failed"
        ? "内容未通过质检"
        : qualityStatus === "not_checked"
          ? "内容尚未质检"
          : "",
  };
}

function GenerationProgress({ job, onCancel, onRetry }: { job: GenerationJob; onCancel: () => void; onRetry: () => void }) {
  const terminal = ["completed", "failed", "canceled"].includes(job.status);
  const [now, setNow] = useState(() => Date.now());
  useEffect(() => {
    if (terminal) return;
    const timer = window.setInterval(() => setNow(Date.now()), 1000);
    return () => window.clearInterval(timer);
  }, [terminal]);
  const started = job.model_started_at || job.started_at;
  const liveElapsed = started ? Math.max(0, now - new Date(started).getTime()) : 0;
  const duration = terminal ? (job.metrics?.model_ms ?? job.metrics?.run_ms) : liveElapsed;
  const actualModel = [job.resolved_provider, job.resolved_model].filter(Boolean).join(" / ");
  const statusLabels = generationStatusLabels(job.output || {});
  return <div className={`card job-panel ${terminal ? "job-terminal" : ""}`}><div className="toolbar"><div><strong>{job.stage_label || "生成任务"}</strong><p className="muted">{job.message || (job.status === "queued" ? "等待 worker 接手" : "任务正在处理")}</p>{actualModel && <p className="muted">实际模型：{actualModel}{duration != null && <span> · 耗时 {formatDuration(duration)}</span>}</p>}{terminal && job.status === "completed" && (statusLabels.source || statusLabels.quality) && <p className="muted">模型状态：{statusLabels.source || "未记录"}　质检状态：{statusLabels.quality || "未记录"}</p>}</div><div className="job-meta"><span>{job.stage === "generating" ? "生成中" : `${job.progress || 0}%`}</span>{!terminal && job.status !== "cancel_requested" && <button className="button" onClick={onCancel}>取消</button>}{job.status === "failed" && <button className="button" onClick={onRetry}>重试</button>}</div></div><div className="progress-track"><div className="progress-value" style={{ width: `${Math.max(0, Math.min(100, job.progress || 0))}%` }} /></div>{job.error && <p className="error-text">{job.error}</p>}</div>;
}

const PLANNING_STATUS_LABEL: Record<string, string> = { not_started: "未开始", draft: "草稿", confirmed: "已确认", stale: "待复核", completed: "已完成" };

type PlanningRule = { type?: "text" | "number"; min?: number; max?: number; required?: boolean; hard?: boolean };
type PlanningRules = { version: string; default_rule: PlanningRule; steps: Record<string, Record<string, PlanningRule>> };
type PlanningFieldSpec = { key: string; label: string; path: string[]; min?: number; max?: number; required?: boolean; hard?: boolean; multiline?: boolean; valueType?: "text" | "number" };

const PLANNING_NUMERIC_FIELDS = new Set(["sequence", "start_chapter", "end_chapter"]);

function isPlanningRecord(value: unknown): value is PlanningContent {
  return Boolean(value) && typeof value === "object" && !Array.isArray(value);
}

function normalizePlanningContent(step: string, content: PlanningContent): PlanningContent {
  const next = JSON.parse(JSON.stringify(content || {})) as PlanningContent;
  if (step === "protagonist" || step === "character") {
    const character = isPlanningRecord(next.character) ? next.character : {};
    const existingCore = isPlanningRecord(character.dramatic_core) ? character.dramatic_core : {};
    const dramaticCore = { ...existingCore };
    for (const key of ["goal", "motivation", "flaw", "conflict"]) {
      if (!String(dramaticCore[key] || "").trim() && character[key] != null) dramaticCore[key] = character[key];
    }
    character.dramatic_core = dramaticCore;
    if (!String(character.arc || "").trim() && character.character_arc != null) character.arc = character.character_arc;
    if (!String(character.appearance || "").trim() && character.portrayal != null) character.appearance = character.portrayal;
    for (const key of ["goal", "motivation", "flaw", "conflict", "character_arc", "portrayal"]) delete character[key];
    next.character = character;
  }
  if (step === "arc") {
    const arc = isPlanningRecord(next.arc) ? next.arc : {};
    for (const key of ["sequence", "start_chapter", "end_chapter"]) {
      if (arc[key] === "" || arc[key] == null) {
        arc[key] = arc[key] === "" ? null : arc[key];
      } else if (typeof arc[key] !== "number") {
        const numeric = Number(arc[key]);
        if (Number.isFinite(numeric)) arc[key] = Math.trunc(numeric);
      }
    }
    next.arc = arc;
  }
  return next;
}

function planningFieldValue(content: PlanningContent, path: string[]): string {
  const value = planningFieldRawValue(content, path);
  if (value == null) return "";
  return typeof value === "string" || typeof value === "number" ? String(value) : JSON.stringify(value);
}

function planningFieldRawValue(content: PlanningContent, path: string[]): unknown {
  let value: unknown = content;
  for (const part of path) {
    if (Array.isArray(value)) value = value[Number(part)];
    else value = isPlanningRecord(value) ? value[part] : undefined;
  }
  return value;
}

function planningFieldPath(content: PlanningContent, step: string, key: string, index?: number): string[] {
  if (step === "contract") {
    if (content.selected && typeof content.selected === "object") return ["selected", key];
    if (Array.isArray(content.candidates) && index != null && content.candidates[index]) return ["candidates", String(index), key];
    return [key];
  }
  if (step === "cast_roster") return ["characters", String(index ?? 0), key];
  if (step === "setting" || step === "summary") return ["story_bible", key];
  if (step === "protagonist" || step === "character") {
    if (["goal", "motivation", "flaw", "conflict"].includes(key)) return ["character", "dramatic_core", key];
    return ["character", key];
  }
  if (step === "arc") return ["arc", key];
  return [key];
}

function planningFieldSpecs(step: string, content: PlanningContent, rules: PlanningRules): PlanningFieldSpec[] {
  const ruleFor = (key: string): PlanningRule => rules.steps[step]?.[key] || rules.default_rule;
  const simple = (fields: Array<[string, string, boolean?]>, index?: number, prefix = "") => fields.map(([key, label, multiline]) => {
    const rule = ruleFor(key);
    return {
      key, label: prefix ? `${prefix} · ${label}` : label, path: planningFieldPath(content, step, key, index),
      min: rule.min, max: rule.max, required: rule.required, hard: rule.hard, multiline,
      valueType: rule.type || (PLANNING_NUMERIC_FIELDS.has(key) ? "number" : "text"),
    };
  });
  const contractFields: Array<[string, string, boolean?]> = [
    ["title", "方向标题"], ["target_experience", "目标读者体验", true],
    ["protagonist_principle", "主角原则", true], ["power_curve", "成长曲线", true],
    ["payoff_cadence", "回报节奏", true], ["power_cost", "能力代价", true],
    ["moral_boundary", "道德边界", true], ["style_rules", "文风规则", true],
    ["title_interpretation", "书名解读", true], ["reader_promise", "读者承诺", true],
  ];
  if (step === "contract") {
    if (isPlanningRecord(content.selected)) return simple(contractFields, undefined, "已选方向");
    if (Array.isArray(content.candidates) && content.candidates.length) {
      return content.candidates.flatMap((_candidate: unknown, index: number) => simple(contractFields, index, `方向 ${index + 1}`));
    }
    return simple(contractFields);
  }
  if (step === "setting") return simple([
    ["core_hook", "核心钩子", true], ["core_conflict", "核心冲突", true],
    ["world", "世界规则", true], ["stakes", "失败代价", true],
    ["ending", "结局方向", true], ["style_rules", "文风规则", true],
  ]);
  if (step === "protagonist" || step === "character") return simple([
    ["name", "姓名"], ["role", "身份"], ["biography", "人物小传", true],
    ["goal", "人物目标", true], ["conflict", "人物阻力", true],
    ["motivation", "人物动机", true], ["flaw", "人物缺陷", true],
    ["arc", "人物弧", true], ["voice", "语言与行动特征", true],
  ]);
  if (step === "cast_roster") {
    const characters = Array.isArray(content.characters) ? content.characters : [];
    return characters.flatMap((character: unknown, index: number) => simple([
      ["name", `${isPlanningRecord(character) ? String(character.name || `角色 ${index + 1}`) : `角色 ${index + 1}`} · 姓名`], ["role", "身份"],
      ["story_function", "剧情作用", true], ["relationship_to_protagonist", "与主角关系", true],
    ], index));
  }
  if (step === "arc") return simple([
    ["title", "卷名"], ["sequence", "卷序"], ["start_chapter", "起始章节"], ["end_chapter", "结束章节"],
    ["goal", "本卷目标", true], ["opposition", "主要对手与边界", true],
    ["turning_point", "关键转折", true], ["ending_state", "卷末状态", true], ["synopsis", "本卷梗概", true],
  ]);
  if (step === "summary") return simple([
    ["summary", "全书总梗概", true], ["theme", "主题", true],
    ["ending", "结局与最终清算", true], ["style_rules", "文风规则", true],
  ]);
  return [];
}

function setPlanningField(content: PlanningContent, path: string[], value: unknown): PlanningContent {
  const next = JSON.parse(JSON.stringify(content)) as PlanningContent;
  let cursor: PlanningContent | unknown[] = next;
  path.forEach((part, index) => {
    const numericKey = Number(part);
    if (index === path.length - 1) {
      if (Array.isArray(cursor)) cursor[numericKey] = value;
      else cursor[part] = value;
      return;
    }
    const child = path[index + 1];
    let current = Array.isArray(cursor) ? cursor[numericKey] : cursor[part];
    if (!current || typeof current !== "object") {
      current = /^\d+$/.test(child) ? [] : {};
      if (Array.isArray(cursor)) cursor[numericKey] = current;
      else cursor[part] = current;
    }
    cursor = current as PlanningContent | unknown[];
  });
  return next;
}

function PlanningFieldForm({ step, draftText, rules, onChange }: { step: string; draftText: string; rules: PlanningRules | null; onChange: (value: string) => void }) {
  let content: PlanningContent | null = null;
  try { content = JSON.parse(draftText); } catch { content = null; }
  if (!content) return <div className="notice">{draftText.trim() ? "内容格式有误，暂时无法显示编辑表单。请在高级编辑中修正，或重新生成这一步。" : "这一步还没有内容，点击下方“生成当前步骤”开始。"}</div>;
  if (!rules) return <div className="notice">正在准备编辑内容，请稍候…</div>;
  content = normalizePlanningContent(step, content);
  const fields = planningFieldSpecs(step, content, rules);
  if (!fields.length) return <p className="muted">这一步暂不支持表单编辑，可展开“高级编辑”修改。</p>;
  return <div className="planning-fields">{fields.map((field) => {
    const value = planningFieldValue(content, field.path);
    const rawValue = planningFieldRawValue(content, field.path);
    const count = Array.from(value).length;
    const hasValue = value.trim().length > 0;
    const hasRange = field.min != null && field.max != null;
    const outsideHardRange = field.valueType !== "number" && field.hard && hasRange && hasValue && (count < field.min! || count > field.max!);
    const missingRequired = field.valueType !== "number" && field.required && !hasValue;
    const rangeLabel = hasRange
      ? `${field.hard ? "需填写" : "建议"} ${field.min}—${field.max} 字`
      : field.valueType === "number" ? "数字字段 · 保存时保持数字类型" : "以服务端字段规则为准";
    const update = (raw: string) => {
      const nextValue = field.valueType === "number"
        ? (raw === "" ? null : Number(raw))
        : raw;
      if (field.valueType === "number" && nextValue !== null && !Number.isFinite(nextValue)) return;
      onChange(JSON.stringify(setPlanningField(content!, field.path, nextValue), null, 2));
    };
    const structuredValue = Array.isArray(rawValue) || isPlanningRecord(rawValue);
    const input = structuredValue
      ? <textarea value={value} onChange={(event) => { try { onChange(JSON.stringify(setPlanningField(content!, field.path, JSON.parse(event.target.value)), null, 2)); } catch { return; } }} />
      : field.multiline
      ? <textarea value={value} onChange={(event) => update(event.target.value)} />
      : <input type={field.valueType === "number" ? "number" : "text"} step={field.valueType === "number" ? 1 : undefined} value={value} onChange={(event) => update(event.target.value)} />;
    const fieldStatus = missingRequired
      ? " · 此项需要填写"
      : outsideHardRange
      ? ` · 确认前请调整到 ${field.min}—${field.max} 字`
      : !field.hard && hasRange
      ? " · 可按内容需要调整"
      : "";
    return <div className="field" key={field.path.join(".")}><label>{field.label} <span className="muted">（{field.required === false ? "可选，" : ""}{rangeLabel}）</span></label>{input}<div className={`field-meta ${outsideHardRange || missingRequired ? "warning-text" : "muted"}`}>{field.valueType === "number" ? `${rangeLabel} · 当前值：${value || "未填写"}` : `当前 ${count} 字${fieldStatus}`}</div></div>;
  })}</div>;
}

function PlanningWizard({ work, planning, busy, onGenerate, onGenerateAllCharacters, onSave, onRestore, onConfirm, onFinalize, onReset }: {
  work: Work;
  planning: PlanningSession;
  busy: string;
  onGenerate: (step: string, itemKey: string, feedback: string, preset: string) => Promise<void>;
  onGenerateAllCharacters: (preset: string, feedback: string) => Promise<void>;
  onSave: (step: string, itemKey: string, content: PlanningContent, feedback: string) => Promise<void>;
  onRestore: (snapshotId: string) => Promise<void>;
  onConfirm: (step: string, itemKey: string, candidateIndex?: number) => Promise<PlanningSession | undefined>;
  onFinalize: () => Promise<void>;
  onReset: () => Promise<void>;
}) {
  const [selectedStep, setSelectedStep] = useState(planning.current_step === "complete" ? "summary" : planning.current_step);
  const [selectedItemKey, setSelectedItemKey] = useState("");
  const [preset, setPreset] = useState(planning.preset || "custom");
  const [feedback, setFeedback] = useState("");
  const [draftText, setDraftText] = useState("");
  const [saveMessage, setSaveMessage] = useState("");
  const [snapshots, setSnapshots] = useState<PlanningSnapshot[]>([]);
  const [diff, setDiff] = useState<{ before: PlanningContent; after: PlanningContent } | null>(null);
  const [planningRules, setPlanningRules] = useState<PlanningRules | null>(null);
  const pendingGenerated = useRef<{ step: string; itemKey: string; version: number; before: PlanningContent } | null>(null);
  const previousCurrentStep = useRef(planning.current_step);
  const step = planning.steps.find((item) => item.step === selectedStep) || planning.steps[0];
  const items = step?.items || [];
  const rosterList = (planning.steps.find((item) => item.step === "cast_roster")?.items[0]?.content?.characters || []) as PlanningRosterItem[];
  const characterItems = planning.steps.find((item) => item.step === "character")?.items || [];
  const draftedCharacterKeys = new Set(characterItems.map((item) => item.item_key));
  const pendingCharacters = rosterList.filter((item) => item.item_key && !draftedCharacterKeys.has(item.item_key));
  const activeItem = items.find((item) => item.item_key === selectedItemKey) || (selectedItemKey ? undefined : items[0]);
  const activeKey = selectedItemKey || activeItem?.item_key || (selectedStep === "contract" ? "contract" : selectedStep === "character" ? rosterList[0]?.item_key || "default" : selectedStep === "arc" ? "arc:1" : "default");
  const coverage = planning.coverage_checks?.coverage;

  useEffect(() => {
    if (previousCurrentStep.current !== planning.current_step) {
      previousCurrentStep.current = planning.current_step;
      if (planning.current_step !== "complete") {
        const timer = window.setTimeout(() => setSelectedStep(planning.current_step), 0);
        return () => window.clearTimeout(timer);
      }
    }
  }, [planning.current_step]);
  useEffect(() => {
    const timer = window.setTimeout(() => {
      setSelectedItemKey(""); setFeedback(""); setSaveMessage(""); setDiff(null);
    }, 0);
    return () => window.clearTimeout(timer);
  }, [selectedStep]);
  useEffect(() => {
    let disposed = false;
    api<PlanningRules>("/planning-rules")
      .then((data) => { if (!disposed) setPlanningRules(data); })
      .catch(() => undefined);
    return () => { disposed = true; };
  }, []);
  useEffect(() => {
    const timer = window.setTimeout(() => {
      setDraftText(activeItem ? JSON.stringify(activeItem.content, null, 2) : "");
      setFeedback(activeItem?.feedback || "");
      const pending = pendingGenerated.current;
      if (pending && activeItem && pending.step === selectedStep && pending.itemKey === activeKey && activeItem.version > pending.version) {
        setDiff({ before: pending.before, after: activeItem.content });
        pendingGenerated.current = null;
      }
    }, 0);
    return () => window.clearTimeout(timer);
  }, [activeItem, selectedStep, activeKey]);
  useEffect(() => {
    let disposed = false;
    const timer = window.setTimeout(() => {
      setSnapshots([]);
      api<{ items: PlanningSnapshot[] }>(`/works/${work.id}/planning-snapshots?step=${encodeURIComponent(selectedStep)}&item_key=${encodeURIComponent(activeKey)}`)
        .then((data) => { if (!disposed) setSnapshots(data.items); })
        .catch(() => undefined);
    }, 0);
    return () => { disposed = true; window.clearTimeout(timer); };
  }, [work.id, selectedStep, activeKey, activeItem?.version]);

  const candidateList = (activeItem?.content?.candidates || []) as PlanningCandidate[];
  const blockingIssues = activeItem?.checks?.blocking || [];
  const hasBlockingIssues = blockingIssues.length > 0;
  const canFinalize = planning.steps.every((item) => item.status === "confirmed") && planning.status !== "completed";
  const checkpointRows = [
    ["起点", coverage?.opening_planned], ["升级", coverage?.escalation_planned], ["中点", coverage?.midpoint_planned],
    ["最低谷", coverage?.low_point_planned], ["结局", coverage?.final_reckoning_planned],
  ] as [string, boolean | undefined][];

  function parseDraft(): PlanningContent | null {
    try {
      const parsed = JSON.parse(draftText);
      return parsed && typeof parsed === "object" && !Array.isArray(parsed) ? normalizePlanningContent(selectedStep, parsed) : null;
    } catch { return null; }
  }
  async function saveDraft() {
    const parsed = parseDraft();
    if (!parsed) { window.alert("当前内容不是合法 JSON，请修正后再保存。"); return; }
    try { await onSave(selectedStep, activeKey, parsed, feedback); setDraftText(JSON.stringify(parsed, null, 2)); setSaveMessage("草稿已保存。确认后才会进入下一步。"); } catch { return; }
  }
  async function saveAndContinue() {
    const parsed = parseDraft();
    if (!parsed) { window.alert("当前内容不是合法 JSON，请修正后再保存。"); return; }
    try { await onSave(selectedStep, activeKey, parsed, feedback); setDraftText(JSON.stringify(parsed, null, 2)); await onConfirm(selectedStep, activeKey); setSaveMessage("已保存并确认，正在进入下一步。"); } catch { return; }
  }
  async function selectContractCandidate(candidateIndex: number) {
    const parsed = parseDraft();
    if (!parsed) { window.alert("当前内容不是合法 JSON，请修正后再选择方向。"); return; }
    try {
      await onSave("contract", activeKey, parsed, feedback);
      await onConfirm("contract", activeKey, candidateIndex);
      setDraftText(JSON.stringify(parsed, null, 2));
      setSaveMessage(`已保存并选择方向 ${candidateIndex + 1}。`);
    } catch { return; }
  }
  async function confirmAndContinue() {
    const parsed = parseDraft();
    if (!parsed) { window.alert("当前内容不是合法 JSON，请修正后再确认。"); return; }
    try {
      await onSave(selectedStep, activeKey, parsed, feedback);
      const updated = await onConfirm(selectedStep, activeKey);
      if (selectedStep !== "character" || !updated) return;
      const characterStep = updated.steps.find((item) => item.step === "character");
      const statusByKey = new Map((characterStep?.items || []).map((item) => [item.item_key, item.status]));
      const currentIndex = rosterList.findIndex((item) => item.item_key === activeKey);
      const orderedRoster = currentIndex >= 0 ? [...rosterList.slice(currentIndex + 1), ...rosterList.slice(0, currentIndex + 1)] : rosterList;
      const next = orderedRoster.find((item) => statusByKey.get(item.item_key) !== "confirmed");
      if (next) setSelectedItemKey(next.item_key);
    } catch { return; }
  }
  async function generateCurrent() {
    const before = parseDraft();
    if (before) pendingGenerated.current = { step: selectedStep, itemKey: activeKey, version: activeItem?.version || 0, before };
    await onGenerate(selectedStep, activeKey, feedback, preset);
  }
  async function revertGenerated() {
    if (!diff) return;
    const restored = normalizePlanningContent(selectedStep, diff.before);
    await onSave(selectedStep, activeKey, restored, feedback);
    setDraftText(JSON.stringify(restored, null, 2)); setDiff(null); setSaveMessage("已撤销本次生成，恢复为生成前版本。");
  }
  function selectCharacter(key: string) { setSelectedItemKey(key); }
  function selectArc(key: string) { setSelectedItemKey(key); }
  const nextArcKey = `arc:${items.length + 1}`;
  const brief = work.inspiration_blueprint?.content?.creative_brief;
  return <><>{brief && <div className="notice inspiration-brief"><strong>原创蓝图已接入本次规划。</strong> 吸收：{(brief.market_signals || []).join("；") || "抽象市场信号"}。必须重构：{(brief.transformation_contract?.change || []).join("；")}。</div>}</><div className="planning-layout">
    <div className="card planning-sidebar">
      <div className="toolbar"><div><h2>故事档案向导</h2><p className="subtitle">每一步确认后，下一步才会引用它。</p></div><button className="button" onClick={onReset} disabled={!!busy}>重置</button></div>
      <div className="planning-preset field"><label>创作契约预设</label><select value={preset} onChange={(event) => setPreset(event.target.value)}>{Object.entries(planning.presets || {}).map(([key, value]) => <option key={key} value={key}>{String(value.name || key)}</option>)}</select></div>
      <div className="planning-steps">{planning.steps.map((item) => <button key={item.step} className={`planning-step ${selectedStep === item.step ? "active" : ""}`} onClick={() => setSelectedStep(item.step)}><span>{PLANNING_STATUS_LABEL[item.status] || item.status}</span><strong>{item.label}</strong><small>{item.description}</small></button>)}</div>
      <div className="notice token-notice">本次会话：输入 {planning.usage.known ? planning.usage.input_tokens : "未知"} · 输出 {planning.usage.known ? planning.usage.output_tokens : "未知"} · 总计 {planning.usage.known ? planning.usage.total_tokens : "未知"}</div>
    </div>
    <div className="card planning-main">
      <div className="toolbar"><div><h2>{step.label}</h2><p className="subtitle">{step.description}</p></div><span className={`tag ${step.status === "confirmed" ? "success" : step.status === "stale" ? "warning" : ""}`}>{PLANNING_STATUS_LABEL[step.status] || step.status}</span></div>
      {coverage && <div className="planning-coverage"><div className="toolbar"><div><strong>全书覆盖状态</strong><p className="muted">卷级主线 {coverage.planned_volume_count} 卷 · 已规划约 {coverage.planned_chapters}/{coverage.target_chapters || "?"} 章 · 覆盖率 {Math.round(coverage.coverage_ratio * 100)}%</p></div><span className={`tag ${planning.coverage_checks?.full_book_ready ? "success" : "warning"}`}>{planning.coverage_checks?.full_book_ready ? "全书可收束" : "仍需补齐"}</span></div><div className="toolbar-actions">{checkpointRows.map(([label, ready]) => <span className={`tag ${ready ? "success" : "warning"}`} key={label}>{label}：{ready ? "已覆盖" : "待补充"}</span>)}</div></div>}
      {selectedStep === "character" && rosterList.length > 0 && <div className="planning-subitems"><div className="toolbar"><div><strong>选择要生成的人物</strong><p className="muted">待生成 {pendingCharacters.length} 位；已生成的草稿和已确认角色不会被覆盖。</p></div><button className="button primary" onClick={() => onGenerateAllCharacters(preset, feedback)} disabled={!!busy || pendingCharacters.length === 0}>{busy === "planning" ? "批量生成中…" : pendingCharacters.length ? `一键生成 ${pendingCharacters.length} 位小传` : "全部已有草稿"}</button></div><div className="toolbar-actions">{rosterList.map((item) => <button key={item.item_key} className={`button ${activeKey === item.item_key ? "primary" : ""}`} onClick={() => selectCharacter(item.item_key)}>{item.name || item.item_key}</button>)}</div></div>}
      {selectedStep === "arc" && <div className="planning-subitems"><strong>已生成卷级主线</strong><div className="toolbar-actions">{items.map((item) => <button key={item.item_key} className={`button ${activeKey === item.item_key ? "primary" : ""}`} onClick={() => selectArc(item.item_key)}>第{item.item_key.split(":")[1] || "?"}卷</button>)}<button className="button" onClick={() => { setSelectedItemKey(nextArcKey); }}>新增第{items.length + 1}卷</button></div></div>}
      {selectedStep === "contract" && candidateList.length > 0 && <div className="planning-candidates">{candidateList.map((candidate, index) => <div className="card planning-candidate" key={`${candidate.title || "candidate"}-${index}`}><div className="toolbar"><h3>{candidate.title || `方向 ${index + 1}`}</h3><button className="button primary" onClick={() => void selectContractCandidate(index)} disabled={!!busy || hasBlockingIssues}>选择并确认</button></div><p><strong>读者体验：</strong>{candidate.target_experience}</p><p><strong>主角原则：</strong>{candidate.protagonist_principle}</p><p><strong>成长与回报：</strong>{candidate.power_curve} · {candidate.payoff_cadence}</p><p><strong>边界：</strong>{candidate.moral_boundary}</p><p><strong>禁区：</strong>{(candidate.forbidden || []).join("；")}</p></div>)}</div>}
      {selectedStep === "cast_roster" && Array.isArray(activeItem?.content?.characters) && <div className="planning-roster">{(activeItem.content.characters as PlanningRosterItem[]).map((item) => <div className="asset" key={item.item_key}><strong>{item.name}</strong><p>{item.role} · {item.story_function}</p><p>与主角：{item.relationship_to_protagonist}</p></div>)}</div>}
      {activeItem && <p className="muted">本步 token：{activeItem.total_tokens == null ? "未知" : `输入 ${activeItem.input_tokens || 0} · 输出 ${activeItem.output_tokens || 0} · 总计 ${activeItem.total_tokens}`}</p>}
      <PlanningFieldForm step={selectedStep} draftText={draftText} rules={planningRules} onChange={setDraftText} />
      <details className="field"><summary>高级编辑（JSON）</summary><textarea className="planning-editor" value={draftText} onChange={(event) => setDraftText(event.target.value)} placeholder="先生成当前步骤…" /></details>
      {diff && <div className="planning-diff"><div className="toolbar"><strong>本次重生成差异</strong><div className="toolbar-actions"><button className="button primary" onClick={() => { setDiff(null); setSaveMessage("已接受本次生成结果。"); }}>接受修改</button><button className="button" onClick={() => void revertGenerated()} disabled={!!busy}>撤销</button></div></div><div className="two-col"><div><label>生成前</label><pre>{JSON.stringify(diff.before, null, 2)}</pre></div><div><label>生成后</label><pre>{JSON.stringify(diff.after, null, 2)}</pre></div></div></div>}
      {snapshots.length > 0 && <details className="planning-snapshots"><summary>版本快照（可恢复上一版）</summary>{snapshots.map((snapshot) => <div className="asset" key={snapshot.id}><div className="toolbar"><div><strong>v{snapshot.version} · {snapshot.source === "manual" ? "手动保存" : "生成结果"}</strong><p className="muted">{new Date(snapshot.created_at).toLocaleString("zh-CN")}</p></div><button className="button" disabled={!!busy} onClick={() => void onRestore(snapshot.id).then(() => setSaveMessage(`已恢复 v${snapshot.version}，当前内容成为新的草稿版本。`))}>恢复此版</button></div></div>)}</details>}
      <div className="field"><label>重生成意见（可选）</label><textarea value={feedback} onChange={(event) => setFeedback(event.target.value)} placeholder="例如：主角不能被配角教育，回报要更快，删掉道德牺牲逻辑。" /></div>
      {activeItem?.checks && <div className="planning-checks"><strong>自动检查</strong>{(activeItem.checks.blocking || []).map((item) => <p className="error-text" key={item}>需要修改：{item}</p>)}{(activeItem.checks.warnings || []).map((item) => <p className="warning-text" key={item}>语言提醒：{item}</p>)}{!(activeItem.checks.blocking || []).length && !(activeItem.checks.warnings || []).length && <p className="muted">暂未发现结构或语言风险，最终以你的确认意见为准。</p>}</div>}
      {hasBlockingIssues && <div className="notice">存在阻断问题，已禁用确认。请先保存修改或重新生成当前步骤，直到自动检查通过。</div>}
      {saveMessage && <div className="notice">{saveMessage}</div>}
      <div className="toolbar planning-actions"><button className="button" onClick={saveDraft} disabled={!!busy || !draftText}>保存修改</button><button className="button" onClick={generateCurrent} disabled={!!busy}>{busy === "planning" ? "生成中…" : activeItem ? "根据意见重生成" : "生成当前步骤"}</button>{activeItem && selectedStep === "contract" && <button className="button primary" onClick={saveAndContinue} disabled={!!busy || hasBlockingIssues}>保存并确认，进入下一步</button>}{activeItem && selectedStep !== "contract" && <button className="button primary" onClick={confirmAndContinue} disabled={!!busy || hasBlockingIssues}>确认并继续</button>}</div>
      {canFinalize && <div className="finalize-panel"><p>所有步骤都已确认，可以将规划正式写入故事档案。</p><button className="button dark" onClick={onFinalize} disabled={!!busy}>最终确认并生成故事档案</button></div>}
    </div>
  </div></>;
}

function ModelSettingsEditor({ profiles, onChanged }: { profiles: ModelProfile[]; onChanged: () => Promise<void> }) {
  const [editing, setEditing] = useState<ModelProfile | null>(null);
  const [form, setForm] = useState({ name: "", provider: "openai_compatible", base_url: "", model: "", api_key: "", reasoning_effort: "auto", timeout_seconds: "90", is_default: false });
  const [message, setMessage] = useState("");
  const [modelOptions, setModelOptions] = useState<string[]>([]);
  const [testingId, setTestingId] = useState<string | null>(null);
  const reset = () => { setEditing(null); setForm({ name: "", provider: "openai_compatible", base_url: "", model: "", api_key: "", reasoning_effort: "auto", timeout_seconds: "90", is_default: false }); };
  const edit = (profile: ModelProfile) => { setEditing(profile); setForm({ name: profile.name, provider: profile.provider, base_url: profile.base_url, model: profile.model, api_key: "", reasoning_effort: profile.reasoning_effort || "auto", timeout_seconds: String(profile.timeout_seconds || 90), is_default: Boolean(profile.is_default) }); };
  const applyPreset = async (name: string) => { try { const data = await api<{ presets: Record<string, { name: string; base_url: string; model: string; timeout_seconds?: number }> }>("/model-profiles"); const selected = data.presets[name]; if (!selected) return; const existing = profiles.find((profile) => profile.provider === name && profile.model === selected.model); if (existing) { setEditing(existing); setForm({ name: existing.name, base_url: selected.base_url, model: selected.model, provider: name, api_key: "", reasoning_effort: existing.reasoning_effort || "auto", timeout_seconds: String(existing.timeout_seconds || selected.timeout_seconds || 90), is_default: Boolean(existing.is_default) }); setMessage("已载入已有配置，可直接修改并保存"); } else { setEditing(null); setForm((current) => ({ ...current, name: selected.name, base_url: selected.base_url, model: selected.model, provider: name, timeout_seconds: String(selected.timeout_seconds || current.timeout_seconds) })); } } catch (e) { setMessage((e as Error).message); } };
  async function fetchModelList(id: string) { setMessage("读取模型列表中…"); try { const data = await api<{ items: string[] }>(`/model-profiles/${id}/models`); setModelOptions(data.items); setMessage(data.items.length ? `已读取 ${data.items.length} 个模型，可从模型名称输入框选择` : "服务未返回可用模型列表，请手动填写模型名称"); } catch (e) { setMessage((e as Error).message); } }
  async function save(event: FormEvent) { event.preventDefault(); setMessage(""); try { const { api_key, ...rest } = form; const body = { ...rest, timeout_seconds: Number(form.timeout_seconds), ...(api_key ? { api_key } : {}) }; if (editing) await api(`/model-profiles/${editing.id}`, { method: "PATCH", body: JSON.stringify(body) }); else await api("/model-profiles", { method: "POST", body: JSON.stringify({ ...body, api_key: api_key || "" }) }); await onChanged(); reset(); setMessage("已保存模型配置"); } catch (e) { setMessage((e as Error).message); } }
  async function test(id: string) { if (testingId) return; setTestingId(id); setMessage("测试连接中…"); try { const result = await api<{ message: string }>(`/model-profiles/${id}/test`, { method: "POST", timeoutMs: 135000 }); setMessage(result.message); await onChanged(); } catch (e) { setMessage((e as Error).message); } finally { setTestingId(null); } }
  const codexAuth = form.provider === "codex_auth";
  return <div><div className="topbar"><div><div className="eyebrow">MODEL CENTER</div><h1>模型服务</h1><p className="subtitle">保存多个 OpenAI 兼容配置，或使用本机 Codex Auth 登录；Key 只在服务端加密保存。</p></div><button className="button" onClick={reset}>新建配置</button></div>{message && <div className="notice">{message}</div>}<div className="grid"><div className="card span-5"><h2>{editing ? "编辑模型配置" : "添加模型配置"}</h2><div className="preset-row"><button className="button" type="button" onClick={() => applyPreset("deepseek")}>DeepSeek</button><button className="button" type="button" onClick={() => applyPreset("qwen")}>通义千问</button><button className="button" type="button" onClick={() => applyPreset("kimi")}>Kimi</button><button className="button" type="button" onClick={() => applyPreset("codex_auth")}>Codex Auth</button></div><form onSubmit={save}><div className="field"><label>配置名称</label><input required value={form.name} onChange={(e) => setForm({ ...form, name: e.target.value })} placeholder="例如：HotAI GPT-5.6 Luna" /></div>{codexAuth ? <div className="notice">Codex Auth 使用运行 worker 的机器上的 Codex CLI 登录状态。先执行 <code>codex login</code>，然后点击“测试连接”。</div> : <div className="field"><label>Base URL</label><input required value={form.base_url} onChange={(e) => setForm({ ...form, base_url: e.target.value })} placeholder="https://api.example.com/v1" /></div>}<div className="field"><label>模型名称</label><div className="toolbar"><input required list="available-models" value={form.model} onChange={(e) => setForm({ ...form, model: e.target.value })} placeholder="手动填写模型 ID" />{editing && <button className="button" type="button" onClick={() => fetchModelList(editing.id)}>获取模型列表</button>}</div><datalist id="available-models">{modelOptions.map((model) => <option key={model} value={model} />)}</datalist></div>{!codexAuth && <div className="field"><label>API Key {editing && <span className="muted">（留空保持原 Key）</span>}</label><input type="password" value={form.api_key} onChange={(e) => setForm({ ...form, api_key: e.target.value })} placeholder={editing ? "已加密保存，不回显" : "sk-…"} /></div>}<div className="two-col"><div className="field"><label>推理强度</label><select value={form.reasoning_effort} onChange={(e) => setForm({ ...form, reasoning_effort: e.target.value })}><option value="auto">自动</option><option value="low">低</option><option value="medium">中</option><option value="high">高</option><option value="xhigh">极高 / xhigh</option></select></div><div className="field"><label>超时（秒）</label><input type="number" min="1" max="600" value={form.timeout_seconds} onChange={(e) => setForm({ ...form, timeout_seconds: e.target.value })} /></div></div><label className="check"><input type="checkbox" checked={form.is_default} onChange={(e) => setForm({ ...form, is_default: e.target.checked })} />设为默认模型</label><div className="toolbar"><button className="button primary" type="submit">保存配置</button>{editing && <button className="button" type="button" onClick={reset}>取消编辑</button>}</div></form></div><div className="card span-7"><h2>已保存配置</h2><p className="subtitle">连接测试会验证模型、JSON 输出和推理参数；不支持的参数会直接提示。</p>{profiles.length ? <div className="asset-list">{profiles.map((profile) => <div className="asset" key={profile.id}><div className="toolbar"><div><strong>{profile.name} {Boolean(profile.is_default) && <span className="tag">默认</span>}</strong><p>{profile.provider === "codex_auth" ? "Codex Auth · 本机 CLI" : `${profile.model} · ${profile.base_url}`}</p></div><span className={`tag ${profile.last_test_status === "ok" ? "success" : ""}`}>{profile.provider === "codex_auth" ? (profile.last_test_status === "ok" ? "Codex 已登录" : "待登录") : profile.has_api_key ? `Key ${profile.api_key_masked}` : "未配置 Key"}</span></div><div className="toolbar"><span className="muted">推理：{profile.reasoning_effort || "自动"} · {profile.last_test_status === "ok" ? "已验证" : "未验证"}</span><div className="toolbar-actions"><button className="button" onClick={() => test(profile.id)}>测试连接</button><button className="button" onClick={() => edit(profile)}>编辑</button></div></div></div>)}</div> : <p className="muted">还没有模型配置。添加后生成按钮会显示为实时 AI 模式。</p>}</div></div></div>;
}

function ModelSettings({ profiles, onChanged }: { profiles: ModelProfile[]; onChanged: () => Promise<void> }) {
  const [activeTab, setActiveTab] = useState<"models" | "prompts" | "proxy">("models");
  const [message, setMessage] = useState("");
  const [codexStatus, setCodexStatus] = useState<{ ok: boolean; message: string } | null>(null);
  const loadCodexStatus = useCallback(async () => {
    const data = await api<{ codex_auth: { ok: boolean; message: string } }>("/model-profiles");
    setCodexStatus(data.codex_auth);
  }, []);
  useEffect(() => {
    const timer = window.setTimeout(() => { loadCodexStatus().catch(() => undefined); }, 0);
    return () => window.clearTimeout(timer);
  }, [loadCodexStatus]);

  async function refresh() {
    await onChanged();
    await loadCodexStatus();
  }

  async function remove(profile: ModelProfile) {
    if (!window.confirm(`确定删除模型配置“${profile.name}”吗？`)) return;
    setMessage("删除中…");
    try {
      await api(`/model-profiles/${profile.id}`, { method: "DELETE" });
      await refresh();
      setMessage(`已删除模型配置“${profile.name}”`);
    } catch (e) {
      setMessage((e as Error).message);
    }
  }

  return <div><div className="tabs settings-tabs"><button className={`tab ${activeTab === "models" ? "active" : ""}`} onClick={() => setActiveTab("models")}>模型服务</button><button className={`tab ${activeTab === "prompts" ? "active" : ""}`} onClick={() => setActiveTab("prompts")}>提示词</button><button className={`tab ${activeTab === "proxy" ? "active" : ""}`} onClick={() => setActiveTab("proxy")}>代理设置</button></div>
    {activeTab === "prompts" ? <PromptSettingsPanel /> : activeTab === "proxy" ? <ProxySettingsPanel /> : <>{codexStatus && <div className="notice">Codex CLI：{codexStatus.message}</div>}
    <ModelSettingsEditor profiles={profiles} onChanged={refresh} />
    {message && <div className="notice">{message}</div>}
    {profiles.length > 0 && <div className="card model-delete-panel">
      <div className="toolbar"><div><h2>删除模型配置</h2><p className="subtitle">删除后该配置将从模型列表中移除，已生成的内容不受影响。</p></div></div>
      <div className="asset-list">{profiles.map((profile) => <div className="asset" key={`delete-${profile.id}`}><div className="toolbar"><div><strong>{profile.name}</strong><p className="muted">{profile.model}</p></div><button className="button" type="button" onClick={() => remove(profile)}>删除</button></div></div>)}</div>
    </div>}</>}
  </div>;
}

function ModelCalls() {
  const [items, setItems] = useState<ModelCall[]>([]);
  const [stats, setStats] = useState<ModelCallStats | null>(null);
  const [detail, setDetail] = useState<ModelCallDetail | null>(null);
  const [works, setWorks] = useState<Pick<Work, "id" | "title">[]>([]);
  const [loading, setLoading] = useState(true);
  const [message, setMessage] = useState("");
  const [page, setPage] = useState(0);
  const pageSize = 25;
  const [total, setTotal] = useState(0);
  const [filters, setFilters] = useState({ work_id: "", status: "", provider: "", model: "", call_kind: "" });

  const load = useCallback(async () => {
    try {
      setLoading(true);
      const params = new URLSearchParams({ limit: String(pageSize), offset: String(page * pageSize), ...Object.fromEntries(Object.entries(filters).filter(([, value]) => value)) });
      const statsParams = filters.work_id ? `?work_id=${encodeURIComponent(filters.work_id)}` : "";
      const [logs, summary] = await Promise.all([
        api<{ items: ModelCall[]; total: number }>(`/model-call-logs?${params.toString()}`),
        api<ModelCallStats>(`/model-call-logs/stats${statsParams}`),
      ]);
      setItems(logs.items);
      setTotal(logs.total);
      setStats(summary);
      setMessage("");
    } catch (error) {
      setMessage((error as Error).message);
    } finally {
      setLoading(false);
    }
  }, [filters, page]);

  useEffect(() => {
    api<{ items: Work[] }>("/works").then((data) => setWorks(data.items.map((item) => ({ id: item.id, title: item.title })))).catch(() => undefined);
  }, []);
  useEffect(() => {
    const firstLoad = window.setTimeout(() => void load(), 0);
    const timer = window.setInterval(() => void load(), 5000);
    return () => { window.clearTimeout(firstLoad); window.clearInterval(timer); };
  }, [load]);

  async function openDetail(id: string) {
    try {
      setDetail(await api<ModelCallDetail>(`/model-call-logs/${id}`));
    } catch (error) {
      setMessage((error as Error).message);
    }
  }

  const duration = (value?: number | null) => value == null ? "—" : formatDuration(value);
  const charCount = (value?: number | null) => (value ?? 0).toLocaleString();
  const date = (value?: string | null) => value ? new Date(value).toLocaleString("zh-CN", { hour12: false }) : "—";
  const pretty = (value: unknown) => {
    if (typeof value === "string") return value;
    try { return JSON.stringify(value, null, 2); } catch { return String(value ?? ""); }
  };
  const statusLabels: Record<string, string> = { running: "进行中", in_flight: "传输中", success: "成功", delivered: "已传输", failed: "失败", error: "传输错误", timeout: "超时", canceled: "用户取消", user_canceled: "用户取消", not_recorded: "未记录", not_applicable: "不适用", not_attempted: "未尝试", not_checked: "未检查", adopted: "已采用", not_adopted: "未采用", pending: "待确认", passed: "通过", failed_quality: "质量失败", invalid: "结构错误", structure_error: "结构错误", repaired: "已修复" };
  const label = (value?: string | null) => statusLabels[value || ""] || value || "—";
  const statusTag = (value?: string | null) => <span className={`tag model-call-status ${value || "unknown"}`}>{label(value)}</span>;
  const updateFilter = (key: keyof typeof filters, value: string) => { setPage(0); setFilters((current) => ({ ...current, [key]: value })); };
  const pageCount = Math.max(1, Math.ceil(total / pageSize));
  const pageLabel = total ? `${page * pageSize + 1}—${Math.min((page + 1) * pageSize, total)} / ${total}` : "0 / 0";

  return <div>
    <div className="topbar"><div><div className="eyebrow">MODEL OBSERVABILITY</div><h1>模型调用</h1><p className="subtitle">按作品、任务和调用类型追踪真实模型请求；每 5 秒刷新当前页。</p></div><button className="button" onClick={() => void load()} disabled={loading}>{loading ? "读取中…" : "刷新"}</button></div>
    {message && <div className="notice">{message}</div>}
    <div className="grid model-call-stats">
      <div className="card stat span-4"><span className="stat-label">今日调用</span><span className="stat-value">{stats?.today ?? "—"}</span></div>
      <div className="card stat span-4"><span className="stat-label">成功率</span><span className="stat-value">{stats ? `${stats.success_rate}%` : "—"}</span></div>
      <div className="card stat span-4"><span className="stat-label">平均耗时 / P95</span><span className="stat-value">{stats ? `${duration(stats.avg_duration_ms)} / ${duration(stats.p95_duration_ms)}` : "—"}</span></div>
    </div>
    <div className="card model-call-card">
      <div className="toolbar"><div><h2>调用记录</h2><p className="subtitle">当前页 {pageLabel} · Token：{stats?.total_tokens?.toLocaleString() ?? "—"}</p></div><div className="toolbar-actions">{Object.entries(stats?.failure_categories || {}).map(([key, value]) => <span className="tag warning" key={key}>{key === "timeout" ? "超时" : key === "structure_error" ? "结构错误" : key === "quality_failure" ? "质量失败" : key === "user_canceled" ? "用户取消" : "传输失败"}：{value}</span>)}</div></div>
      <div className="model-call-filters">
        <select value={filters.work_id} onChange={(event) => updateFilter("work_id", event.target.value)}><option value="">全部作品</option>{works.map((workItem) => <option key={workItem.id} value={workItem.id}>{workItem.title}</option>)}</select>
        <select value={filters.status} onChange={(event) => updateFilter("status", event.target.value)}><option value="">全部传输状态</option><option value="running">进行中</option><option value="success">成功</option><option value="failed">失败</option><option value="timeout">超时</option><option value="canceled">用户取消</option></select>
        <input value={filters.provider} onChange={(event) => updateFilter("provider", event.target.value)} placeholder="Provider，例如 codex_auth" />
        <input value={filters.model} onChange={(event) => updateFilter("model", event.target.value)} placeholder="模型名称" />
        <input value={filters.call_kind} onChange={(event) => updateFilter("call_kind", event.target.value)} placeholder="调用类型，例如 chapter" />
      </div>
      {items.length ? <div className="model-call-table-wrap"><table className="model-call-table"><thead><tr><th>时间 / 作品</th><th>任务</th><th>规划</th><th>实际模型</th><th>传输</th><th>解析</th><th>质量</th><th>采用</th><th>字符 / Token</th><th>链路</th><th></th></tr></thead><tbody>{items.map((item) => <tr key={item.id} onClick={() => void openDetail(item.id)}><td>{date(item.created_at)}<small>{item.work_title || "未关联作品"}</small></td><td><strong>{item.task_name || item.call_kind}</strong><small>{item.generation_task_url ? <a href={`${API}${item.generation_task_url}`} target="_blank" rel="noreferrer" onClick={(event) => event.stopPropagation()}>打开任务</a> : item.call_kind}</small></td><td>{item.planning_step || "—"}<small>{item.item_key || "—"}</small></td><td><strong>{String(item.effective_parameters?.model || item.model || "未指定")}</strong><small>{String(item.effective_parameters?.provider || item.provider || "—")}</small></td><td>{statusTag(item.transmission_status)}</td><td>{statusTag(item.parse_status)}</td><td>{statusTag(item.quality_status)}</td><td>{statusTag(item.adoption_status)}</td><td>{charCount(item.request_chars)} / {charCount(item.response_chars)}<small>{item.total_tokens?.toLocaleString() ?? "—"} token</small></td><td>{item.repair_of_call_id ? <span className="tag">定向修复</span> : item.generation_job_id ? <span className="tag success">首次调用</span> : "—"}</td><td><button className="button" onClick={(event) => { event.stopPropagation(); void openDetail(item.id); }}>详情</button></td></tr>)}</tbody></table></div> : <p className="muted">{loading ? "正在读取模型调用…" : "还没有模型调用记录。完成一次模型生成或连接测试后会显示在这里。"}</p>}
      <div className="toolbar-actions" style={{ marginTop: 16 }}><button className="button" onClick={() => setPage((value) => Math.max(0, value - 1))} disabled={page === 0 || loading}>上一页</button><span className="muted">第 {page + 1} / {pageCount} 页</span><button className="button" onClick={() => setPage((value) => Math.min(pageCount - 1, value + 1))} disabled={page >= pageCount - 1 || loading}>下一页</button></div>
    </div>
    {detail && <div className="model-call-detail-backdrop" onClick={() => setDetail(null)}><section className="model-call-detail" onClick={(event) => event.stopPropagation()}><div className="toolbar"><div><h2>调用详情</h2><p className="subtitle">{date(detail.created_at)} · {detail.work_title || "未关联作品"} · {detail.task_name || detail.call_kind}</p></div><button className="button" onClick={() => setDetail(null)}>关闭</button></div><div className="model-call-detail-meta"><span>传输：{statusTag(detail.transmission_status)}</span><span>解析：{statusTag(detail.parse_status)}</span><span>质量：{statusTag(detail.quality_status)}</span><span>采用：{statusTag(detail.adoption_status)}</span><span>总耗时：{duration(detail.duration_ms)}</span><span>首字：{duration(detail.first_output_ms)}</span><span>请求/响应：{charCount(detail.request_chars)} / {charCount(detail.response_chars)} 字符</span><span>输入/输出：{detail.input_tokens ?? "—"} / {detail.output_tokens ?? "—"}</span></div><div className="two-col"><div><h3>任务信息</h3><p>规划步骤：{detail.planning_step || "—"} · item key：{detail.item_key || "—"}</p><p>任务状态：{label(detail.generation_status)} · 调用类型：{detail.call_kind}</p>{detail.generation_task_url && <p><a href={`${API}${detail.generation_task_url}`} target="_blank" rel="noreferrer">打开生成任务</a></p>}</div><div><h3>真实生效参数</h3><pre className="model-call-payload">{pretty(detail.effective_parameters)}</pre></div></div>{detail.context_char_counts && Object.keys(detail.context_char_counts).length > 0 && <><h3>上下文区块占比</h3><div className="toolbar-actions">{Object.entries(detail.context_char_shares || {}).map(([key, value]) => <span className="tag" key={key}>{key}：{Math.round(value * 10000) / 100}%（{detail.context_char_counts?.[key] || 0} 字）</span>)}</div></>}{detail.call_chain && detail.call_chain.length > 0 && <><h3>同一任务调用链</h3><div className="diff-list">{detail.call_chain.map((call, index) => <div className="asset" key={call.id}><div className="toolbar"><div><strong>{index === 0 ? "首次调用" : "定向修复调用"} · {call.id.slice(0, 12)}</strong><p className="muted">{date(call.created_at)} · {label(call.transmission_status)} · {label(call.parse_status)} · {label(call.quality_status)}</p></div><button className="button" onClick={() => void openDetail(call.id)}>查看</button></div>{call.repair_of_call_id && <p className="muted">修复自：{call.repair_of_call_id}</p>}</div>)}</div></>}<h3>请求</h3><pre className="model-call-payload">{pretty(detail.request)}</pre><h3>响应</h3><pre className="model-call-payload">{detail.response != null ? pretty(detail.response) : detail.response_text || "—"}</pre>{detail.error && <><h3>错误</h3><pre className="model-call-payload error-text">{detail.error}</pre></>}</section></div>}
  </div>;
}

function PromptSettingsPanel() {
  const [items, setItems] = useState<PromptSetting[]>([]);
  const [selectedKey, setSelectedKey] = useState("");
  const [draft, setDraft] = useState("");
  const [message, setMessage] = useState("");
  const [busy, setBusy] = useState(false);
  const selected = items.find((item) => item.key === selectedKey) || null;
  const dirty = Boolean(selected && draft !== selected.content);
  const load = useCallback(async (preferredKey?: string) => {
    const data = await api<{ items: PromptSetting[] }>("/settings/prompts");
    setItems(data.items);
    const key = preferredKey && data.items.some((item) => item.key === preferredKey) ? preferredKey : data.items[0]?.key || "";
    setSelectedKey(key);
    setDraft(data.items.find((item) => item.key === key)?.content || "");
  }, []);
  useEffect(() => {
    const timer = window.setTimeout(() => { load().catch((e) => setMessage((e as Error).message)); }, 0);
    return () => window.clearTimeout(timer);
  }, [load]);
  function selectPrompt(item: PromptSetting) {
    if (dirty && !window.confirm("当前提示词尚未保存，确定切换吗？")) return;
    setSelectedKey(item.key); setDraft(item.content); setMessage("");
  }
  async function save() {
    if (!selected || !draft.trim()) return;
    setBusy(true); setMessage("");
    try {
      await api(`/settings/prompts/${selected.key}`, { method: "PUT", body: JSON.stringify({ content: draft }) });
      await load(selected.key); setMessage(`已保存“${selected.title}”，后续生成任务将立即使用新提示词。`);
    } catch (e) { setMessage((e as Error).message); } finally { setBusy(false); }
  }
  async function restoreDefault() {
    if (!selected || !window.confirm(`确定将“${selected.title}”恢复为系统默认提示词吗？`)) return;
    setBusy(true); setMessage("");
    try {
      await api(`/settings/prompts/${selected.key}`, { method: "DELETE" });
      await load(selected.key); setMessage(`“${selected.title}”已恢复默认。`);
    } catch (e) { setMessage((e as Error).message); } finally { setBusy(false); }
  }
  return <div><div className="topbar"><div><div className="eyebrow">PROMPT CENTER</div><h1>提示词</h1><p className="subtitle">查看并优化各生成阶段的系统提示词；保存后对后续任务立即生效。</p></div></div>{message && <div className="notice">{message}</div>}
    <div className="prompt-settings-layout"><div className="card prompt-list"><h2>生成阶段</h2><p className="subtitle">共 {items.length} 个提示词</p><div className="prompt-list-items">{items.map((item) => <button type="button" key={item.key} className={`prompt-list-item ${selectedKey === item.key ? "active" : ""}`} onClick={() => selectPrompt(item)}><span><strong>{item.title}</strong><small>{item.stage}</small></span>{item.is_customized && <em>已自定义</em>}</button>)}</div></div>
      <div className="card prompt-editor">{selected ? <><div className="toolbar"><div><h2>{selected.title}</h2><p className="subtitle">{selected.description}</p></div><span className={`tag ${selected.is_customized ? "" : "success"}`}>{selected.is_customized ? "自定义版本" : "系统默认"}</span></div><div className="field"><label>系统提示词</label><textarea value={draft} onChange={(event) => setDraft(event.target.value)} spellCheck={false} /></div><div className="toolbar"><span className="muted">{draft.length.toLocaleString()} 字符{dirty ? " · 有未保存修改" : ""}</span><div className="toolbar-actions"><button className="button" type="button" onClick={restoreDefault} disabled={busy || !selected.is_customized}>恢复默认</button><button className="button primary" type="button" onClick={save} disabled={busy || !dirty || !draft.trim()}>{busy ? "处理中…" : "保存提示词"}</button></div></div></> : <p className="loading">正在读取提示词…</p>}</div></div>
  </div>;
}

function ProxySettingsPanel() {
  const [settings, setSettings] = useState<ProxySettings | null>(null);
  const [enabled, setEnabled] = useState(false);
  const [port, setPort] = useState("10808");
  const [message, setMessage] = useState("");
  const [busy, setBusy] = useState(false);
  const load = useCallback(async () => {
    const data = await api<ProxySettings>("/settings/proxy");
    setSettings(data); setEnabled(data.enabled); setPort(String(data.port));
  }, []);
  useEffect(() => {
    const timer = window.setTimeout(() => { load().catch((e) => setMessage((e as Error).message)); }, 0);
    return () => window.clearTimeout(timer);
  }, [load]);
  async function save(event: FormEvent) {
    event.preventDefault(); setBusy(true); setMessage("");
    try {
      const data = await api<ProxySettings>("/settings/proxy", { method: "PUT", body: JSON.stringify({ enabled, port: Number(port) }) });
      setSettings(data); setMessage(data.enabled ? `${data.message}；Codex 后续调用将使用该代理。` : "代理已关闭，Codex 将使用进程默认网络设置。");
    } catch (e) { setMessage((e as Error).message); } finally { setBusy(false); }
  }
  async function testPort() {
    setBusy(true); setMessage("");
    try {
      const data = await api<{ ok: boolean; message: string }>("/settings/proxy/test", { method: "POST", body: JSON.stringify({ enabled, port: Number(port) }) });
      setMessage(data.message);
    } catch (e) { setMessage((e as Error).message); } finally { setBusy(false); }
  }
  return <div><div className="topbar"><div><div className="eyebrow">NETWORK</div><h1>代理设置</h1><p className="subtitle">为 Codex Auth 配置本机 v2rayN 混合或 HTTP 代理端口。</p></div></div>{message && <div className="notice">{message}</div>}<div className="grid"><div className="card span-6"><h2>Codex 网络代理</h2><form onSubmit={save}><label className="check"><input type="checkbox" checked={enabled} onChange={(e) => setEnabled(e.target.checked)} />启用 Codex 代理</label><div className="field"><label>代理主机</label><input value="127.0.0.1" disabled /></div><div className="field"><label>v2rayN 端口</label><input type="number" min="1" max="65535" required value={port} onChange={(e) => setPort(e.target.value)} placeholder="10808" /></div><div className="toolbar-actions"><button className="button primary" disabled={busy}>{busy ? "处理中…" : "保存设置"}</button><button className="button" type="button" onClick={testPort} disabled={busy}>测试端口</button></div></form></div><div className="card span-6"><h2>当前状态</h2><p><strong>{settings?.enabled ? "代理已启用" : "代理未启用"}</strong></p><p className="muted">生效地址：{settings?.enabled ? `http://${settings.host}:${settings.port}` : "使用 worker 默认网络"}</p><p className="muted">端口检测：{settings ? settings.message : "正在读取…"}</p><div className="notice">请填写 v2rayN“本地监听端口”中的混合端口或 HTTP 端口。保存后只影响 Codex Auth，不改变 DeepSeek、Kimi、通义千问等模型配置。</div></div></div></div>;
}

function Foreshadows({ work, onSaved }: { work: Work; onSaved: () => Promise<void> }) {
  const [items, setItems] = useState<Foreshadow[]>([]); const [stats, setStats] = useState({ open: 0, soon: 0, overdue: 0, revealed: 0 }); const [showForm, setShowForm] = useState(false); const [form, setForm] = useState({ clue: "", kind: "clue", planted_chapter: "0", expected_reveal_chapter: "0", note: "" }); const [message, setMessage] = useState("");
  const load = useCallback(() => api<{ items: Foreshadow[]; stats: { open: number; soon: number; overdue: number; revealed: number } }>(`/works/${work.id}/foreshadows`).then((data) => { setItems(data.items); setStats(data.stats); }), [work.id]);
  useEffect(() => { load().catch((e) => setMessage(e.message)); }, [load]);
  async function add(event: FormEvent) { event.preventDefault(); try { await api(`/works/${work.id}/foreshadows`, { method: "POST", body: JSON.stringify({ ...form, planted_chapter: Number(form.planted_chapter), expected_reveal_chapter: Number(form.expected_reveal_chapter) }) }); setForm({ clue: "", kind: "clue", planted_chapter: "0", expected_reveal_chapter: "0", note: "" }); setShowForm(false); await load(); await onSaved(); } catch (e) { setMessage((e as Error).message); } }
  async function status(id: string, value: string) { await api(`/works/${work.id}/foreshadows/${id}`, { method: "PATCH", body: JSON.stringify({ status: value, actual_reveal_chapter: value === "revealed" ? Number(prompt("实际回收章节", "1") || 0) : 0 }) }); await load(); await onSaved(); }
  return <div><div className="toolbar"><div><h2>伏笔工作台</h2><p className="subtitle">把线索从埋设、推进到回收，写作时自动提醒临近和逾期伏笔。</p></div><button className="button primary" onClick={() => setShowForm(!showForm)}>新增伏笔</button></div><div className="notice">开放 {stats.open} · 三章内待回收 {stats.soon} · 已逾期 {stats.overdue} · 已回收 {stats.revealed}</div>{message && <div className="notice">{message}</div>}{showForm && <form className="card foreshadow-form" onSubmit={add}><div className="field"><label>线索内容</label><textarea required value={form.clue} onChange={(e) => setForm({ ...form, clue: e.target.value })} placeholder="例如：对手总是避开旧车站这个地点" /></div><div className="two-col"><div className="field"><label>埋设章节</label><input type="number" min="0" value={form.planted_chapter} onChange={(e) => setForm({ ...form, planted_chapter: e.target.value })} /></div><div className="field"><label>预计回收章节</label><input type="number" min="0" value={form.expected_reveal_chapter} onChange={(e) => setForm({ ...form, expected_reveal_chapter: e.target.value })} /></div></div><div className="field"><label>备注</label><input value={form.note} onChange={(e) => setForm({ ...form, note: e.target.value })} /></div><button className="button primary">保存伏笔</button></form>}<div className="asset-list">{items.length ? items.map((item) => <div className={`card foreshadow-card ${item.status}`} key={item.id}><div className="toolbar"><strong>{item.clue}</strong><span className="tag">{item.status === "open" ? "开放" : item.status === "revealed" ? "已回收" : item.status === "deferred" ? "已延期" : "已放弃"}</span></div><p>第{item.planted_chapter || "?"}章埋设 · 第{item.expected_reveal_chapter || "?"}章预计回收</p>{item.note && <p className="muted">{item.note}</p>}{item.status === "open" && <div className="toolbar-actions"><button className="button" onClick={() => status(item.id, "revealed")}>标记回收</button><button className="button" onClick={() => status(item.id, "deferred")}>延期</button><button className="button" onClick={() => status(item.id, "abandoned")}>放弃</button></div>}</div>) : <div className="card"><p className="muted">还没有伏笔。可以手动新增，也可以在章节状态提取后审核 AI 候选。</p></div>}</div></div>;
}

function Trends({ profiles, onCreate }: { profiles: ModelProfile[]; onCreate: (work: Work) => void }) {
  const [items, setItems] = useState<TrendItem[]>([]); const [selected, setSelected] = useState<string[]>([]); const [ideas, setIdeas] = useState<TrendIdea[]>([]); const [sourceModels, setSourceModels] = useState<TrendSourceModel[]>([]); const [keyword, setKeyword] = useState(""); const [loading, setLoading] = useState(false); const [creatingIndex, setCreatingIndex] = useState<number | null>(null); const [message, setMessage] = useState("");
  const usableProfiles = profiles.filter((profile) => profile.has_api_key || (profile.provider === "codex_auth" && profile.last_test_status === "ok"));
  async function search(force = false) { setLoading(true); setMessage(""); setSelected([]); setIdeas([]); setSourceModels([]); try { const data = await api<{ items: TrendItem[]; sources: { source: string; stale?: boolean; error?: string }[] }>("/trends/search", { method: "POST", body: JSON.stringify({ sources: ["fanqie", "qidian", "jjwxc"], keyword, refresh: force }) }); setItems(data.items); setMessage(data.sources.map((source) => `${source.source}${source.stale ? "（离线缓存）" : ""}${source.error ? `：${source.error}` : ""}`).join(" · ")); } catch (e) { setMessage((e as Error).message); } finally { setLoading(false); } }
  async function analyze() { if (!selected.length) return; setLoading(true); setMessage(""); try { const usable = usableProfiles.find((profile) => Boolean(profile.is_default)) || usableProfiles[0]; const data = await api<{ ideas: TrendIdea[]; source_models: TrendSourceModel[] }>("/trends/analyze", { method: "POST", body: JSON.stringify({ item_ids: selected, model_profile_id: usable?.id || null }) }); setIdeas(data.ideas); setSourceModels(data.source_models || []); } catch (e) { setMessage((e as Error).message); } finally { setLoading(false); } }
  async function createIdea(index: number) { const idea = ideas[index]; if (!idea?.blueprint_id) { setMessage("该创意缺少原创蓝图，请重新分析。"); return; } setCreatingIndex(index); try { const usable = usableProfiles.find((profile) => Boolean(profile.is_default)) || usableProfiles[0]; const work = await api<Work>("/works/from-inspiration-blueprint", { method: "POST", body: JSON.stringify({ blueprint_id: idea.blueprint_id, model_profile_id: usable?.id || null, idempotency_key: `blueprint-${idea.blueprint_id}` }) }); onCreate(work); } catch (e) { setMessage((e as Error).message); } finally { setCreatingIndex(null); } }
  const toggle = (id: string, checked: boolean) => setSelected((current) => checked ? [...current, id] : current.filter((value) => value !== id));
  return <div><div className="topbar"><div><div className="eyebrow">TREND → BLUEPRINT</div><h1>热门灵感</h1><p className="subtitle">从公开榜单提炼抽象作品模型，再生成带原创约束的故事档案蓝图；不抓取或保存小说全文。</p></div><div className="toolbar-actions"><button className="button" onClick={() => search(false)} disabled={loading}>{loading ? "读取中…" : "读取缓存"}</button><button className="button primary" onClick={() => search(true)} disabled={loading}>强制刷新</button></div></div><div className="card trend-search"><div className="field"><label>关键词</label><input value={keyword} onChange={(e) => setKeyword(e.target.value)} onKeyDown={(e) => e.key === "Enter" && void search(false)} placeholder="搜索书名、作者或题材" /></div><div className="toolbar"><span className="muted">步骤 1：选 1–5 本作品；步骤 2：提取抽象模型；步骤 3：选择原创蓝图并进入故事档案。</span><button className="button primary" onClick={() => void analyze()} disabled={!selected.length || loading || !usableProfiles.length}>提取 {selected.length} 本作品模型</button></div>{!usableProfiles.length && <p className="muted">趋势分析需要先到“模型服务”配置 API Key 或完成 Codex Auth 登录；榜单浏览不受影响。</p>}</div>{message && <div className="notice">{message}</div>}<div className="grid"><div className="card span-7"><h2>榜单作品</h2>{items.length ? <div className="trend-list">{items.map((item) => <label className={`trend-row ${selected.includes(item.id) ? "selected" : ""}`} key={item.id}><input type="checkbox" checked={selected.includes(item.id)} onChange={(e) => toggle(item.id, e.target.checked)} /><span className="rank">{item.rank}</span><span><strong>{item.title}</strong><small>{item.source} · {item.category || "综合"} · {item.metric_label} {item.metric_value}</small><em>{item.synopsis}</em></span><a href={item.source_url} target="_blank" rel="noreferrer" onClick={(event) => event.stopPropagation()}>来源</a></label>)}</div> : <p className="muted">先读取榜单，再选择需要建模的作品。</p>}</div><div className="card span-5"><h2>来源作品模型</h2>{sourceModels.length ? <div className="asset-list">{sourceModels.map((source) => <div className="asset" key={source.trend_item_id}><div className="toolbar"><strong>抽象作品模型</strong><span className="tag">信息完整度：{source.completeness}</span></div><p>{source.model.market_positioning || "仅根据公开条目提炼市场信号。"}</p><p>开篇/冲突：{source.model.narrative_engine?.opening || "待补充"}；{source.model.narrative_engine?.conflict || "待补充"}</p><p className="muted">可吸收：{(source.model.safe_signals || []).join("；") || "节奏与目标感"}</p></div>)}</div> : <p className="muted">完成分析后，这里只展示去实体化的叙事与连载规律。</p>}</div><div className="card span-12"><h2>原创故事蓝图</h2>{ideas.length ? <div className="blueprint-grid">{ideas.map((idea, index) => <div className="asset idea-card" key={`${idea.title}-${index}`}><div className="toolbar"><strong>{idea.title}</strong><button className="button primary" onClick={() => void createIdea(index)} disabled={creatingIndex !== null}>{creatingIndex === index ? "正在创建…" : "采用蓝图并创建作品"}</button></div><p>{idea.genre} · {idea.audience}</p><p><strong>新故事钩子：</strong>{idea.hook}</p><p>{idea.synopsis}</p><p className="muted">吸收：{(idea.blueprint?.market_signals || []).join("；") || "抽象市场信号"}</p><p className="muted">必须重构：{(idea.blueprint?.transformation_contract?.change || []).join("；")}</p><p className="muted">原创边界：{(idea.blueprint?.transformation_contract?.avoid || []).join("；") || idea.risk}</p></div>)}</div> : <p className="muted">作品模型完成后，这里会给出可直接进入故事档案向导的原创蓝图。</p>}</div></div></div>;
}

function CreatePanel({ busy, profiles, onCreate }: { busy: string; profiles: ModelProfile[]; onCreate: (form: HTMLFormElement) => void }) {
  return <div className="grid"><div className="card span-7"><h2>创建一部作品</h2><p className="subtitle">先填写最少的信息，故事资产由 AI 继续补齐。</p><form onSubmit={(event) => { event.preventDefault(); onCreate(event.currentTarget); }}><div className="field"><label>作品名</label><input name="title" required placeholder="例如：潮汐之后" /></div><div className="two-col"><div className="field"><label>题材</label><input name="genre" placeholder="都市、悬疑、科幻…" /></div><div className="field"><label>目标读者/平台</label><input name="audience" placeholder="女频 / 长篇连载" /></div></div><div className="two-col"><div className="field"><label>预计字数</label><input name="words" type="number" defaultValue="100000" min="0" /></div><div className="field"><label>文风</label><input name="style" placeholder="克制、快节奏、对白多" /></div></div><div className="field"><label>一句话设想（可选）</label><textarea name="premise" placeholder="一个人必须在……之前……" /></div>{profiles.length > 0 && <div className="field"><label>本作品模型</label><select name="model_profile_id" defaultValue=""><option value="">使用默认模型</option>{profiles.map((profile) => <option key={profile.id} value={profile.id}>{profile.name} · {profile.model}</option>)}</select></div>}<button className="button primary" disabled={!!busy}>{busy === "create" ? "正在创建…" : "创建作品"}</button></form></div><div className="card span-5"><h3>第一版会帮你完成</h3><div className="asset-list"><div className="asset"><strong>故事方案</strong><p>梗概、主题、世界观、主线冲突和主要人物。</p></div><div className="asset"><strong>章节大纲</strong><p>按卷和章节拆出目标、冲突、节奏和结尾钩子。</p></div><div className="asset"><strong>正文生成</strong><p>按章节计划写作，支持保存、重写和继续下一章。</p></div><div className="asset"><strong>一致性检查</strong><p>检查人物、时间线、伏笔和重复内容。</p></div></div></div></div>;
}

function Overview({ work, planning, onTab }: { work: Work; planning: PlanningSession | null; onTab: (tab: string) => void }) {
  const planningReady = planning?.status === "completed";
  const currentLabel = planning?.steps.find((item) => item.step === planning.current_step)?.label || "创作契约";
  return <div className="grid"><div className="card span-7"><h2>作品总览</h2><p className="subtitle">{work.premise || "还没有一句话设想，先完成故事档案向导。"}</p><div className="field" style={{ marginTop: 22 }}><label>当前创作状态</label><p>{work.status === "writing" ? "正在写作，可以进入写作台继续生成。" : planningReady ? "故事档案已确认，可以生成章节大纲。" : `故事档案向导尚未完成，当前需要确认：${currentLabel}。`}</p></div><div className="toolbar"><button className="button" onClick={() => onTab("assets")}>{planningReady ? "查看故事档案" : "继续故事档案向导"}</button><button className="button" onClick={() => onTab("write")} disabled={!planningReady}>进入写作台</button></div></div><div className="card span-5"><h3>下一步建议</h3><div className="outline"><div className="outline-item"><strong>01 · 确认创作契约</strong><p>先确定读者要获得的体验和故事禁区。</p></div><div className="outline-item"><strong>02 · 逐步确认故事资产</strong><p>设定、人物、主线和梗概分别确认，避免一次跑偏。</p></div><div className="outline-item"><strong>03 · 解锁章节大纲</strong><p>只有全部确认后，章节大纲才会引用正式故事档案。</p></div></div></div></div>;
}

function OutlineV2({ work, busy, onSave, onGenerate, onGenerateVolume, onRefresh }: { work: Work; busy: string; onSave: (chapterNo: number, changes: Partial<ChapterPlan>) => Promise<void>; onGenerate: (request: Record<string, unknown>) => Promise<void>; onGenerateVolume: (volumeId: string, targetStageId?: string, instruction?: string) => Promise<VolumeOutlineDraft>; onRefresh: () => Promise<void> }) {
  const plans = work.chapter_plans || [];
  const currentCount = plans.length;
  const structureEnd = Math.max(...(work.story_volumes || []).map((item) => item.end_chapter || 0), 0);
  const targetFloor = Math.max(work.target_chapter_count || 0, plans.length, structureEnd, 1);
  const wholeBookTarget = Math.max(work.target_chapter_count || 0, structureEnd, plans.length, 1);
  const volumeCoveredChapters = useMemo(() => {
    const chapters = new Set<number>();
    for (const volume of work.story_volumes || []) {
      for (let chapter = Math.max(1, volume.start_chapter); chapter <= Math.min(wholeBookTarget, volume.end_chapter); chapter += 1) chapters.add(chapter);
    }
    return chapters.size;
  }, [work.story_volumes, wholeBookTarget]);
  const [mode, setMode] = useState<"initial" | "replan" | "extend">("initial");
  const [totalTarget, setTotalTarget] = useState(Math.max(targetFloor, 12));
  const [fromChapter, setFromChapter] = useState(1);
  const [toChapter, setToChapter] = useState(Math.min(totalTarget, Math.max(plans.length || 12, 12)));
  const [editing, setEditing] = useState<number | null>(null);
  const [draft, setDraft] = useState<ChapterPlan | null>(null);
  const [history, setHistory] = useState<Record<number, unknown[]>>({});
  const [context, setContext] = useState<unknown>(null);
  const [editingVolume, setEditingVolume] = useState<StoryVolume | null>(null);
  const [stageDrafts, setStageDrafts] = useState<NarrativeStage[]>([]);
  const [volumeDraftIssues, setVolumeDraftIssues] = useState<VolumeOutlineIssue[]>([]);
  const [volumeInstruction, setVolumeInstruction] = useState("");
  const [structureBusy, setStructureBusy] = useState(false);
  const [structureMessage, setStructureMessage] = useState("");
  const jsonText = (value: unknown) => JSON.stringify(value ?? (Array.isArray(value) ? [] : {}), null, 2);
  const parseJson = (value: string, fallback: unknown) => { try { return JSON.parse(value); } catch { return fallback; } };
  useEffect(() => {
    const timer = window.setTimeout(() => setTotalTarget((value) => Math.max(value, targetFloor)), 0);
    return () => window.clearTimeout(timer);
  }, [targetFloor]);
  const begin = (plan: ChapterPlan) => { setEditing(plan.chapter_no); setDraft({ ...plan, beats: [...(plan.beats || [])] }); };
  const save = async () => {
    if (!draft) return;
    await onSave(draft.chapter_no, draft);
    setEditing(null); setDraft(null);
  };
  const generateRange = async () => {
    const start = mode === "extend" ? currentCount + 1 : mode === "replan" ? fromChapter : 1;
    const end = Math.max(start, Math.min(totalTarget, toChapter));
    const request = { total_target_chapters: totalTarget, mode, from_chapter: start, to_chapter: end, expected_outline_version: Math.max(...(work.outline_versions || []).map((item) => item.version_no), 0), expected_fact_version: work.fact_version || 0 };
    await onGenerate(request);
  };
  const loadHistory = async (chapterNo: number) => {
    const response = await api<{ items: unknown[] }>(`/works/${work.id}/chapter-plans/${chapterNo}/history`);
    setHistory((value) => ({ ...value, [chapterNo]: response.items }));
  };
  const previewContext = async (chapterNo: number) => {
    const response = await api<{ context: unknown }>(`/works/${work.id}/contexts/chapter?chapter_no=${chapterNo}`);
    setContext(response.context);
  };
  const editVolume = (volume: StoryVolume) => {
    setEditingVolume({ ...volume, ending_state: { ...(volume.ending_state || {}) } });
    setStageDrafts((work.narrative_stages || []).filter((stage) => stage.volume_id === volume.id).map((stage) => ({ ...stage, allowed_payoffs: [...(stage.allowed_payoffs || [])], forbidden_payoffs: [...(stage.forbidden_payoffs || [])], prerequisites: [...(stage.prerequisites || [])] })));
    setVolumeDraftIssues([]); setVolumeInstruction(""); setStructureMessage("");
  };
  const patchStage = (id: string, changes: Partial<NarrativeStage>) => setStageDrafts((items) => items.map((stage) => stage.id === id ? { ...stage, ...changes } : stage));
  const saveVolume = async () => {
    if (!editingVolume) return;
    setStructureBusy(true); setStructureMessage("");
    try {
      if (editingVolume.end_chapter < editingVolume.start_chapter) throw new Error("本卷结束章节不能早于开始章节");
      await api(`/works/${work.id}/story-volumes/${editingVolume.id}`, { method: "PUT", body: JSON.stringify({ ...editingVolume, target_words: editingVolume.target_words || 0, ending_state: editingVolume.ending_state || {}, status: editingVolume.status || "planned" }) });
      for (const stage of stageDrafts) {
        if (stage.end_chapter < stage.start_chapter) throw new Error(`“${stage.title}”的结束章节不能早于开始章节`);
        await api(`/works/${work.id}/narrative-stages/${stage.id}`, { method: "PUT", body: JSON.stringify({ ...stage, entry_state: stage.entry_state || {}, exit_state: stage.exit_state || {}, allowed_payoffs: stage.allowed_payoffs || [], forbidden_payoffs: stage.forbidden_payoffs || [], prerequisites: stage.prerequisites || [], status: stage.status || "planned" }) });
      }
      await onRefresh(); setEditingVolume(null); setStageDrafts([]); setVolumeDraftIssues([]); setVolumeInstruction(""); setStructureMessage("已保存。本卷的新目标、对手和阶段禁区会进入下一次大纲生成。");
    } catch (error) { setStructureMessage((error as Error).message); } finally { setStructureBusy(false); }
  };
  const rebuildStructure = async () => {
    if (!window.confirm("这会按已确认的卷级主线重建卷与阶段结构。不会删除已生成章节，但旧章节需要按新结构重新规划。确定继续吗？")) return;
    setStructureBusy(true); setStructureMessage("");
    try { await api(`/works/${work.id}/narrative-structure/bootstrap`, { method: "POST", body: JSON.stringify({ replace: true }) }); await onRefresh(); setEditingVolume(null); setStageDrafts([]); setVolumeDraftIssues([]); setVolumeInstruction(""); setStructureMessage("卷与阶段结构已重建，请编辑后再重写受影响的章节。"); }
    catch (error) { setStructureMessage((error as Error).message); } finally { setStructureBusy(false); }
  };
  const replanVolume = async (volume: StoryVolume) => {
    const lastDetailed = Math.max(...plans.map((plan) => plan.chapter_no), 0);
    const end = Math.min(volume.end_chapter, lastDetailed);
    if (end < volume.start_chapter) { setStructureMessage("本卷还没有已细化章节；请先从第 1 章生成短窗口大纲。"); return; }
    await onGenerate({ total_target_chapters: totalTarget, mode: "replan", from_chapter: volume.start_chapter, to_chapter: end, expected_outline_version: Math.max(...(work.outline_versions || []).map((item) => item.version_no), 0), expected_fact_version: work.fact_version || 0 });
  };
  const generateVolumeDraft = async (volume: StoryVolume, targetStageId?: string) => {
    setStructureBusy(true); setStructureMessage("");
    try {
      const generated = await onGenerateVolume(volume.id, targetStageId, volumeInstruction);
      if (targetStageId) {
        const replacement = generated.stages.find((stage) => stage.id === targetStageId);
        if (replacement) setStageDrafts((items) => items.map((stage) => stage.id === targetStageId ? { ...stage, ...replacement } : stage));
        setVolumeDraftIssues((items) => [
          ...items.filter((item) => item.scope !== `stage:${targetStageId}`),
          ...generated.quality_issues,
        ]);
        setStructureMessage(generated.quality_ok ? "该阶段草稿已更新，请检查后保存。" : "该阶段已返回可编辑草稿；未通过项已标在阶段上方。");
        return;
      }
      setEditingVolume({ ...generated.volume, ending_state: { ...(generated.volume.ending_state || {}) } });
      setStageDrafts(generated.stages.map((stage) => ({ ...stage, allowed_payoffs: [...(stage.allowed_payoffs || [])], forbidden_payoffs: [...(stage.forbidden_payoffs || [])], prerequisites: [...(stage.prerequisites || [])] })));
      setVolumeDraftIssues(generated.quality_issues || []);
      setStructureMessage(generated.quality_ok ? "卷纲草稿已生成，尚未保存；请检查后确认。" : "卷纲草稿已返回，未通过项已标注；可修改或只重新生成失败阶段。");
    } catch (error) { setStructureMessage((error as Error).message); } finally { setStructureBusy(false); }
  };
  return <div className="grid">
    <div className="card span-12">
      <div className="toolbar"><div><h2>章节大纲 V2</h2><p className="subtitle">当前已细化 {plans.length} 章 · 全书目标 {totalTarget} 章 · 卷级主线覆盖 {volumeCoveredChapters}/{wholeBookTarget} 章（{Math.round((volumeCoveredChapters / wholeBookTarget) * 100)}%）· 每次只生成一个可审核的短窗口。</p></div><span className="tag">最新大纲 v{Math.max(...(work.outline_versions || []).map((item) => item.version_no), 0)}</span></div>
      <div className="two-col"><div className="field"><label>生成模式</label><select value={mode} onChange={(event) => setMode(event.target.value as "initial" | "replan" | "extend")}><option value="initial">首次生成近期大纲</option><option value="replan">从指定章节重新规划</option><option value="extend">补充下一段大纲</option></select></div><div className="field"><label>全书目标章节数</label><input type="number" min="1" max="10000" value={totalTarget} onChange={(event) => setTotalTarget(Math.max(1, Math.min(10000, Number(event.target.value) || 1)))} /></div></div>
      <div className="two-col">{mode === "replan" && <div className="field"><label>从第几章重新规划</label><input type="number" min="1" max={totalTarget} value={fromChapter} onChange={(event) => setFromChapter(Math.max(1, Math.min(totalTarget, Number(event.target.value) || 1)))} /></div>}<div className="field"><label>本次生成至第几章</label><input type="number" min={mode === "extend" ? currentCount + 1 : mode === "replan" ? fromChapter : 1} max={totalTarget} value={toChapter} onChange={(event) => setToChapter(Math.max(1, Math.min(totalTarget, Number(event.target.value) || 1)))} /></div></div>
      <div className="toolbar-actions"><button className="button dark" disabled={!!busy} onClick={generateRange}>{busy === "outline" ? "正在按批生成…" : `生成第${mode === "extend" ? currentCount + 1 : mode === "replan" ? fromChapter : 1}—${Math.min(totalTarget, toChapter)}章`}</button><a className="button" href={`${API}/works/${work.id}/outline/export`} target="_blank">导出完整大纲</a></div>
      {(work.story_volumes || []).length > 0 && <div className="outline" style={{ marginTop: 18 }}>
        <div className="toolbar"><div><strong>全书结构</strong><p className="muted">卷章节范围动态读取；“每次12章”只代表章节大纲批次，不会改变分卷长度。</p></div><button className="button" disabled={!!busy || structureBusy} onClick={() => void rebuildStructure()}>{structureBusy ? "处理中…" : "按卷级主线重建结构"}</button></div>
        {structureMessage && <p className="notice">{structureMessage}</p>}
        {work.story_volumes?.map((volume) => <div className="outline-item" key={volume.id}>
          {editingVolume?.id === volume.id && editingVolume ? <div className="field">
            <div className="two-col"><div><label>卷名</label><input value={editingVolume.title} onChange={(event) => setEditingVolume({ ...editingVolume, title: event.target.value })} /></div><div><label>卷序号</label><input type="number" min="1" value={editingVolume.sequence} onChange={(event) => setEditingVolume({ ...editingVolume, sequence: Number(event.target.value) || 1 })} /></div><div><label>起始章节</label><input type="number" min="1" value={editingVolume.start_chapter} onChange={(event) => setEditingVolume({ ...editingVolume, start_chapter: Number(event.target.value) || 1 })} /></div><div><label>结束章节</label><input type="number" min="1" max={totalTarget} value={editingVolume.end_chapter} onChange={(event) => setEditingVolume({ ...editingVolume, end_chapter: Number(event.target.value) || 1 })} /></div></div>
            {volumeDraftIssues.filter((item) => item.scope === "volume").length > 0 && <div className="notice"><strong>本卷草稿待处理</strong>{volumeDraftIssues.filter((item) => item.scope === "volume").map((item, index) => <p key={`${item.message}-${index}`}>{item.message}</p>)}</div>}
            <label>本卷梗概</label><textarea value={editingVolume.synopsis || ""} onChange={(event) => setEditingVolume({ ...editingVolume, synopsis: event.target.value })} placeholder="本卷如何从起点推进到卷末状态？" />
            <label>本卷目标</label><textarea value={editingVolume.goal || ""} onChange={(event) => setEditingVolume({ ...editingVolume, goal: event.target.value })} placeholder="本卷结束时主角获得什么阶段成果？" />
            <label>主要对手与强度边界</label><textarea value={editingVolume.opposition || ""} onChange={(event) => setEditingVolume({ ...editingVolume, opposition: event.target.value })} placeholder="只写当前时间与卷内承受范围允许的对手、资源和成果。" />
            <label>卷末状态 JSON</label><textarea value={jsonText(editingVolume.ending_state)} onChange={(event) => setEditingVolume({ ...editingVolume, ending_state: parseJson(event.target.value, editingVolume.ending_state || {}) as Record<string, unknown> })} />
            <label>本次 AI 补充要求（仅用于生成草稿，不会保存）</label><textarea value={volumeInstruction} onChange={(event) => setVolumeInstruction(event.target.value)} placeholder="例如：末日第二天，不能让尚未形成的势力提前登场。" />
            <div className="toolbar-actions"><button className="button dark" disabled={!!busy || structureBusy} onClick={() => void generateVolumeDraft(editingVolume)}>{busy === "volume_outline" || structureBusy ? "正在生成草稿…" : "AI 重新生成整卷草稿"}</button></div>
            <h3>本卷叙事阶段</h3>
            {stageDrafts.map((stage) => <div className="asset" key={stage.id}>
              {volumeDraftIssues.filter((item) => item.scope === `stage:${stage.id}`).length > 0 && <div className="notice"><strong>该阶段草稿待处理</strong>{volumeDraftIssues.filter((item) => item.scope === `stage:${stage.id}`).map((item, index) => <p key={`${item.message}-${index}`}>{item.message}</p>)}<button className="button" disabled={!!busy || structureBusy} onClick={() => void generateVolumeDraft(editingVolume, stage.id)}>重新生成此阶段</button></div>}
              <div className="two-col"><div><label>阶段名</label><input value={stage.title} onChange={(event) => patchStage(stage.id, { title: event.target.value })} /></div><div><label>章节范围</label><div className="two-col"><input type="number" value={stage.start_chapter} onChange={(event) => patchStage(stage.id, { start_chapter: Number(event.target.value) || 1 })} /><input type="number" value={stage.end_chapter} onChange={(event) => patchStage(stage.id, { end_chapter: Number(event.target.value) || 1 })} /></div></div></div>
              <label>阶段任务</label><textarea value={stage.purpose || ""} onChange={(event) => patchStage(stage.id, { purpose: event.target.value })} />
              <label>进入状态 JSON</label><textarea value={jsonText(stage.entry_state)} onChange={(event) => patchStage(stage.id, { entry_state: parseJson(event.target.value, stage.entry_state || {}) as Record<string, unknown> })} />
              <label>退出状态 JSON</label><textarea value={jsonText(stage.exit_state)} onChange={(event) => patchStage(stage.id, { exit_state: parseJson(event.target.value, stage.exit_state || {}) as Record<string, unknown> })} />
              <label>允许的小回报（每行一项）</label><textarea value={(stage.allowed_payoffs || []).join("\n")} onChange={(event) => patchStage(stage.id, { allowed_payoffs: event.target.value.split("\n").map((item) => item.trim()).filter(Boolean) })} />
              <label>禁止提前兑现（每行一项）</label><textarea value={(stage.forbidden_payoffs || []).join("\n")} onChange={(event) => patchStage(stage.id, { forbidden_payoffs: event.target.value.split("\n").map((item) => item.trim()).filter(Boolean) })} placeholder="例如：不得提前完成卷末清算。" />
              <label>进入前提（每行一项）</label><textarea value={(stage.prerequisites || []).join("\n")} onChange={(event) => patchStage(stage.id, { prerequisites: event.target.value.split("\n").map((item) => item.trim()).filter(Boolean) })} />
            </div>)}
            <div className="toolbar-actions"><button className="button primary" disabled={structureBusy} onClick={() => void saveVolume()}>{structureBusy ? "保存中…" : "确认保存卷纲"}</button><button className="button" disabled={structureBusy} onClick={() => { setEditingVolume(null); setStageDrafts([]); setVolumeDraftIssues([]); setVolumeInstruction(""); }}>取消</button></div>
          </div> : <div className="toolbar"><div><strong>第{volume.sequence}卷 · {volume.title}（{volume.start_chapter}—{volume.end_chapter}章）</strong><p>{volume.synopsis || volume.goal || "待补充分卷目标"}</p>{volume.opposition && <p className="muted">对手与边界：{volume.opposition}</p>}<p className="muted">本卷覆盖全书 {Math.max(0, Math.min(wholeBookTarget, volume.end_chapter) - Math.max(1, volume.start_chapter) + 1)}/{wholeBookTarget} 章 · {Math.round((Math.max(0, Math.min(wholeBookTarget, volume.end_chapter) - Math.max(1, volume.start_chapter) + 1) / wholeBookTarget) * 100)}% · {(work.narrative_stages || []).filter((stage) => stage.volume_id === volume.id).map((stage) => `${stage.start_chapter}—${stage.end_chapter}章：${stage.title}`).join(" · ")}</p></div><div className="toolbar-actions"><button className="button primary" disabled={!!busy || structureBusy} onClick={() => void generateVolumeDraft(volume)}>{busy === "volume_outline" || structureBusy ? "正在生成卷纲…" : "AI 生成卷纲"}</button><button className="button" disabled={!!busy || structureBusy} onClick={() => editVolume(volume)}>编辑本卷</button><button className="button dark" disabled={!!busy || structureBusy} onClick={() => void replanVolume(volume)}>按本卷约束重写已细化章节</button></div></div>}
        </div>)}
      </div>}
    </div>
    <div className="card span-12">
      {plans.length ? <div className="outline">{plans.map((plan) => <div className="outline-item" key={plan.chapter_no}>
        <div className="toolbar"><div><strong>第{plan.chapter_no}章 · {plan.title}</strong><p className="muted">故事日 {plan.story_day ?? "待校准"} · {plan.phase_key || "待校准"} · {plan.time_mode || "linear"} · POV {plan.pov_character || "未设"} · 情节点 {plan.beats?.length || 0} · 状态变化 {plan.state_changes?.length || 0}</p></div><div className="toolbar-actions"><button className="button" disabled={!!busy} onClick={() => begin(plan)}>完整编辑</button><button className="button" disabled={!!busy} onClick={() => void loadHistory(plan.chapter_no)}>版本历史</button><button className="button" disabled={!!busy} onClick={() => void previewContext(plan.chapter_no)}>上下文预览</button></div></div>
        {plan.stale_reason && <p className="notice">需复核：{plan.stale_reason}</p>}
        {editing === plan.chapter_no && draft ? <div className="field">
          <label>标题</label><input value={draft.title} onChange={(event) => setDraft({ ...draft, title: event.target.value })} />
          <div className="two-col"><div><label>故事日</label><input type="number" value={draft.story_day ?? ""} onChange={(event) => setDraft({ ...draft, story_day: event.target.value === "" ? null : Number(event.target.value) })} /></div><div><label>阶段</label><select value={draft.phase_key || ""} onChange={(event) => setDraft({ ...draft, phase_key: event.target.value })}><option value="">未设</option>{(work.story_phases || []).map((phase) => <option key={phase.phase_key} value={phase.phase_key}>{phase.name}</option>)}</select></div><div><label>时间模式</label><select value={draft.time_mode || "linear"} onChange={(event) => setDraft({ ...draft, time_mode: event.target.value as "linear" | "flashback" | "parallel" })}><option value="linear">linear</option><option value="flashback">flashback</option><option value="parallel">parallel</option></select></div></div>
          <label>POV 人物</label><input value={draft.pov_character || ""} onChange={(event) => setDraft({ ...draft, pov_character: event.target.value })} />
          <label>目标</label><textarea value={draft.goal} onChange={(event) => setDraft({ ...draft, goal: event.target.value })} /><label>核心冲突</label><textarea value={draft.conflict} onChange={(event) => setDraft({ ...draft, conflict: event.target.value })} /><label>失败代价</label><textarea value={draft.failure_cost || ""} onChange={(event) => setDraft({ ...draft, failure_cost: event.target.value })} /><label>情节点（每行一项，5-8 项）</label><textarea value={(draft.beats || []).join("\n")} onChange={(event) => setDraft({ ...draft, beats: event.target.value.split("\n").filter(Boolean) })} />
          <label>开场状态 JSON</label><textarea value={jsonText(draft.opening_state)} onChange={(event) => setDraft({ ...draft, opening_state: parseJson(event.target.value, draft.opening_state || {}) as Record<string, unknown> })} /><label>因果节点 JSON</label><textarea value={jsonText(draft.causal_beats)} onChange={(event) => setDraft({ ...draft, causal_beats: parseJson(event.target.value, draft.causal_beats || []) as Record<string, unknown>[] })} /><label>知识变化 JSON</label><textarea value={jsonText(draft.knowledge_changes)} onChange={(event) => setDraft({ ...draft, knowledge_changes: parseJson(event.target.value, draft.knowledge_changes || []) as unknown[] })} /><label>状态变化 JSON</label><textarea value={jsonText(draft.state_changes)} onChange={(event) => setDraft({ ...draft, state_changes: parseJson(event.target.value, draft.state_changes || []) as unknown[] })} /><label>登场人物（每行一项）</label><textarea value={(draft.appearing_characters || []).join("\n")} onChange={(event) => setDraft({ ...draft, appearing_characters: event.target.value.split("\n").filter(Boolean) })} /><label>登场势力（每行一项）</label><textarea value={(draft.appearing_factions || []).join("\n")} onChange={(event) => setDraft({ ...draft, appearing_factions: event.target.value.split("\n").filter(Boolean) })} /><label>任务推进 JSON</label><textarea value={jsonText(draft.task_progress)} onChange={(event) => setDraft({ ...draft, task_progress: parseJson(event.target.value, draft.task_progress || []) as Record<string, unknown>[] })} /><label>伏笔动作 JSON</label><textarea value={jsonText(draft.foreshadow_actions)} onChange={(event) => setDraft({ ...draft, foreshadow_actions: parseJson(event.target.value, draft.foreshadow_actions || []) as unknown[] })} /><label>禁止提前揭露（每行一项）</label><textarea value={(draft.forbidden_reveals || []).join("\n")} onChange={(event) => setDraft({ ...draft, forbidden_reveals: event.target.value.split("\n").filter(Boolean) })} /><label>结尾状态 JSON</label><textarea value={jsonText(draft.ending_state)} onChange={(event) => setDraft({ ...draft, ending_state: parseJson(event.target.value, draft.ending_state || {}) as Record<string, unknown> })} /><label>章节钩子</label><textarea value={draft.hook} onChange={(event) => setDraft({ ...draft, hook: event.target.value })} />
          <div className="toolbar-actions"><button className="button primary" disabled={!!busy} onClick={() => void save()}>保存完整大纲</button><button className="button" onClick={() => { setEditing(null); setDraft(null); }}>取消</button></div>
        </div> : <><p>目标：{plan.goal}</p><p>冲突：{plan.conflict}</p><p>失败代价：{plan.failure_cost || "未设"}</p><p>登场：{(plan.appearing_characters || []).join("、") || "未设"}{plan.appearing_factions?.length ? ` · 势力 ${(plan.appearing_factions || []).join("、")}` : ""}</p><p>任务推进：{JSON.stringify(plan.task_progress || [])}</p><p>开场：{JSON.stringify(plan.opening_state || {})}</p><p>结尾：{JSON.stringify(plan.ending_state || {})}</p><p>钩子：{plan.hook}</p></>}
        {history[plan.chapter_no] && <details open><summary>版本历史</summary><pre>{JSON.stringify(history[plan.chapter_no], null, 2)}</pre></details>}
      </div>)}</div> : <p className="muted">还没有章节大纲。完成故事档案后可从上方生成。</p>}
      {context !== null && <details open><summary>实际正文上下文预览（未来信息及旧动态字段均带排除原因）</summary><pre>{JSON.stringify(context, null, 2)}</pre></details>}
    </div>
  </div>;
}

function StoryStateV2({ work, onChanged }: { work: Work; onChanged: () => Promise<void> }) {
  const [chapterNo, setChapterNo] = useState(0);
  const [view, setView] = useState<Record<string, unknown> | null>(null);
  const [message, setMessage] = useState("");
  useEffect(() => {
    const query = chapterNo ? `?chapter_no=${chapterNo}&before_chapter=true` : "";
    api<Record<string, unknown>>(`/works/${work.id}/story-state${query}`).then(setView).catch((error) => setMessage((error as Error).message));
  }, [work.id, chapterNo]);
  const rollback = async (eventId: string) => {
    if (!window.confirm("回滚此已确认事件会从该章重新计算后续状态，确定继续吗？")) return;
    try {
      await api(`/works/${work.id}/story-events/${eventId}/rollback`, { method: "POST", body: JSON.stringify({ reason: "作者在状态页面回滚" }) });
      await onChanged(); setMessage("已回滚事件并重放后续状态。");
    } catch (error) { setMessage((error as Error).message); }
  };
  const saveLongTermFact = async (event: FormEvent<HTMLFormElement>) => {
    event.preventDefault(); const form = new FormData(event.currentTarget);
    try {
      const value = JSON.parse(String(form.get("value") || "{}"));
      await api(`/works/${work.id}/long-term-facts`, { method: "POST", body: JSON.stringify({ entity_type: form.get("entity_type"), entity_id: String(form.get("entity_id") || "") || null, fact_key: form.get("fact_key"), value, locked: form.get("locked") === "on" }) });
      event.currentTarget.reset(); await onChanged(); setMessage("长期事实已保存，并已使受影响大纲进入复核状态。");
    } catch (error) { setMessage(error instanceof SyntaxError ? "长期事实 JSON 格式无效。" : (error as Error).message); }
  };
  const createFuturePlan = async (event: FormEvent<HTMLFormElement>) => {
    event.preventDefault(); const form = new FormData(event.currentTarget);
    try {
      const content = JSON.parse(String(form.get("content") || "{}")); const rawChapter = String(form.get("target_chapter") || "");
      await api(`/works/${work.id}/future-plans`, { method: "POST", body: JSON.stringify({ entity_type: form.get("entity_type"), plan_type: form.get("plan_type"), target_chapter: rawChapter ? Number(rawChapter) : null, content }) });
      event.currentTarget.reset(); await onChanged(); setMessage("未来计划已保存；它只会约束大纲，不会泄露到当前正文上下文。");
    } catch (error) { setMessage(error instanceof SyntaxError ? "未来计划 JSON 格式无效。" : (error as Error).message); }
  };
  const updateGoalStatus = async (goalId: string, status: string) => {
    try { await api(`/works/${work.id}/goals/${goalId}`, { method: "PATCH", body: JSON.stringify({ status, evidence: "作者在故事状态页面更新任务。" }) }); await onChanged(); setMessage("任务状态已记录为可回放事实。"); }
    catch (error) { setMessage((error as Error).message); }
  };
  const canonical = (view?.canonical_state || {}) as Record<string, unknown>;
  return <div className="grid">
    <div className="card span-12"><div className="toolbar"><div><h2>故事状态 V2</h2><p className="subtitle">事件日志为真实来源；此处可查看当前状态或任一章节开始前的可重放状态。</p></div><span className="tag">事实版本 {work.fact_version || 0}</span></div><div className="field"><label>状态视图</label><select value={chapterNo} onChange={(event) => setChapterNo(Number(event.target.value))}><option value="0">当前状态</option>{(work.chapter_plans || []).map((plan) => <option key={plan.chapter_no} value={plan.chapter_no}>第 {plan.chapter_no} 章开始前</option>)}</select></div>{message && <div className="notice">{message}</div>}<details open><summary>可重放的正式事实</summary><pre>{JSON.stringify(canonical, null, 2)}</pre></details></div>
    <div className="card span-8"><h3>长期人物卡与动态状态</h3><div className="asset-list">{(work.characters || []).map((character) => <div className="asset" key={character.id}><strong>{character.name}<span className="tag">{character.role}</span></strong><p>长期资料：{character.biography || character.background}</p><p>动态状态：{JSON.stringify(((canonical.characters as Record<string, unknown> | undefined) || {})[character.id] || {})}</p><p className="muted">旧人物卡状态/知识字段仅标记为 legacy_unscoped，不会注入正文模型。</p></div>)}</div></div>
    <div className="card span-4"><h3>确认事件与来源</h3>{(work.story_events || []).slice().reverse().map((event) => <div className="outline-item" key={event.id}><strong>第{event.chapter_no}章 · {event.event_type}</strong><p>{event.evidence || "无正文证据"}</p><button className="button danger" onClick={() => void rollback(event.id)}>回滚事件</button></div>)}</div>
    <div className="card span-6"><h3>长期事实</h3><p className="muted">这类信息会写入事实版本；保存后旧大纲会提示复核。</p>{(work.long_term_facts || []).map((fact) => <div className="outline-item" key={fact.id}><strong>{fact.entity_type} · {fact.fact_key}</strong><p>{JSON.stringify(fact.value)}</p><span className="tag">{fact.locked ? "已锁定" : "可编辑"}</span></div>)}<form onSubmit={saveLongTermFact}><div className="two-col"><input name="entity_type" required placeholder="实体类型，如 world" /><input name="entity_id" placeholder="实体 ID（可选）" /></div><div className="field"><input name="fact_key" required placeholder="事实键，如 old_contract" /></div><div className="field"><textarea name="value" defaultValue={'{}'} placeholder="事实 JSON" /></div><label><input name="locked" type="checkbox" /> 锁定此事实</label><div className="toolbar-actions"><button className="button">保存长期事实</button></div></form></div>
    <div className="card span-6"><h3>未来计划与任务</h3><p className="muted">未来计划只提供给大纲引擎，正文上下文会明确排除它。</p>{(work.future_plans || []).map((plan) => <div className="outline-item" key={plan.id}><strong>第{plan.target_chapter || "未定"}章 · {plan.plan_type}</strong><p>{JSON.stringify(plan.content)}</p></div>)}<form onSubmit={createFuturePlan}><div className="two-col"><input name="entity_type" required defaultValue="work" /><input name="plan_type" required defaultValue="reveal" /></div><div className="field"><input name="target_chapter" type="number" min="1" placeholder="目标章节（可选）" /></div><div className="field"><textarea name="content" defaultValue={'{}'} placeholder="未来计划 JSON" /></div><button className="button">新增未来计划</button></form><h3 style={{ marginTop: 20 }}>任务状态</h3>{(work.goals || []).map((goal) => <div className="outline-item" key={goal.id}><strong>{goal.title}</strong><div className="toolbar-actions"><select value={goal.status} onChange={(event) => void updateGoalStatus(goal.id, event.target.value)}><option value="planned">计划中</option><option value="active">进行中</option><option value="completed">已完成</option><option value="failed">已失败</option><option value="suspended">暂停</option></select><span className="tag">优先级 {goal.priority}</span></div><p>{JSON.stringify(goal.progress || {})}</p></div>)}</div>
    <div className="span-12"><StoryState work={work} onChanged={onChanged} /></div>
  </div>;
}

function StoryState({ work, onChanged }: { work: Work; onChanged: () => Promise<void> }) {
  const [message, setMessage] = useState("");
  async function submit(event: FormEvent<HTMLFormElement>, path: string, method = "PUT") {
    event.preventDefault(); setMessage("");
    const form = new FormData(event.currentTarget);
    const number = (name: string) => form.get(name) === "" ? null : Number(form.get(name));
    const body = path.startsWith("/story-phases/") ? { phase_key: form.get("phase_key"), name: form.get("name"), start_day: number("start_day"), end_day: number("end_day"), rules: String(form.get("rules") || "").split("\n").filter(Boolean), locked: true }
      : path.startsWith("/factions/") ? { name: form.get("name"), precursor_name: form.get("precursor_name"), lifecycle: form.get("lifecycle"), formed_day: number("formed_day"), first_appearance_chapter: Number(form.get("first_appearance_chapter") || 0), description: form.get("description"), state: {} }
      : { title: form.get("title"), owner_type: form.get("owner_type"), status: form.get("status"), priority: Number(form.get("priority") || 0), started_day: number("started_day"), details: {} };
    try { await api(`/works/${work.id}${path}`, { method, body: JSON.stringify(body) }); event.currentTarget.reset(); await onChanged(); setMessage("已保存到正式故事状态"); }
    catch (error) { setMessage((error as Error).message); }
  }
  const stateByCharacter = new Map(work.character_states?.map((item) => [item.character_id, item.state]) || []);
  return <div className="grid"><div className="card span-8"><div className="toolbar"><div><h2>人物卡与当前状态</h2><p className="subtitle">人物档案是稳定资料；“当前状态”只来自已审核的章节事实。</p></div></div>{work.characters?.length ? <div className="asset-list">{work.characters.map((character) => { const current = stateByCharacter.get(character.id); return <div className="asset" key={character.id}><strong>{character.name}<span className="tag">{character.role}</span></strong><p>{character.biography || character.background}</p><p>目标：{character.goal || "未设"}</p><p>性格：{character.personality || "未设"}</p><p>秘密：{character.secret || "未设"}</p><p>当前状态：{current && Object.keys(current).length ? Object.entries(current).map(([key, value]) => `${key}=${typeof value === "string" ? value : JSON.stringify(value)}`).join("；") : "尚无已审核的章节状态"}</p></div>; })}</div> : <p className="muted">完成故事档案后，这里会显示人物卡。</p>}</div><div className="card span-4"><h3>状态事件</h3>{message && <div className="notice">{message}</div>}<p className="muted">已确认事件 {work.story_events?.length || 0} 条</p>{(work.story_events || []).slice(-8).reverse().map((event) => <div className="outline-item" key={event.id}><strong>第{event.chapter_no}章 · {event.event_type}</strong><p>{event.evidence || "已确认状态变化"}</p></div>)}</div><div className="card span-4"><h3>故事阶段</h3>{(work.story_phases || []).map((phase) => <div className="outline-item" key={phase.phase_key}><strong>{phase.name}</strong><p>{phase.start_day ?? "—"} ～ {phase.end_day ?? "—"}</p></div>)}<form onSubmit={(event) => { const form = new FormData(event.currentTarget); submit(event, `/story-phases/${encodeURIComponent(String(form.get("phase_key") || ""))}`); }}><div className="field"><label>阶段标识</label><input name="phase_key" required placeholder="pre_disaster" /></div><div className="field"><label>阶段名称</label><input name="name" required placeholder="灾前普通世界" /></div><div className="two-col"><input name="start_day" type="number" placeholder="开始故事日" /><input name="end_day" type="number" placeholder="结束故事日" /></div><div className="field"><textarea name="rules" placeholder="规则，每行一项" /></div><button className="button">新增/更新阶段</button></form></div><div className="card span-4"><h3>势力</h3>{(work.factions || []).map((faction) => <div className="outline-item" key={faction.id}><strong>{faction.name}</strong><p>{faction.lifecycle} · 成立日 {faction.formed_day ?? "未设"}</p><p>{faction.description}</p></div>)}<form onSubmit={(event) => { const form = new FormData(event.currentTarget); submit(event, `/factions/${encodeURIComponent(String(form.get("name") || ""))}`); }}><div className="field"><label>势力名称</label><input name="name" required /></div><div className="field"><label>前身</label><input name="precursor_name" /></div><div className="two-col"><input name="formed_day" type="number" placeholder="成立故事日" /><select name="lifecycle" defaultValue="planned"><option value="planned">计划中</option><option value="forming">形成中</option><option value="active">活跃</option><option value="disbanded">已解散</option></select></div><div className="field"><textarea name="description" placeholder="势力描述" /></div><input name="first_appearance_chapter" type="hidden" value="0" /><button className="button">新增/更新势力</button></form></div><div className="card span-4"><h3>任务</h3>{(work.goals || []).map((goal) => <div className="outline-item" key={goal.id}><strong>{goal.title}</strong><p>{goal.status} · 优先级 {goal.priority}</p></div>)}<form onSubmit={(event) => submit(event, "/goals", "POST")}><div className="field"><label>任务名称</label><input name="title" required /></div><div className="two-col"><select name="owner_type" defaultValue="character"><option value="character">人物任务</option><option value="faction">势力任务</option><option value="work">主线任务</option></select><select name="status" defaultValue="planned"><option value="planned">计划中</option><option value="active">进行中</option><option value="completed">已完成</option><option value="failed">已失败</option><option value="suspended">暂停</option></select></div><div className="two-col"><input name="priority" type="number" defaultValue="0" /><input name="started_day" type="number" placeholder="开始故事日" /></div><button className="button">新建任务</button></form></div></div>;
}

function Assets({ work, onSaved }: { work: Work; onSaved: () => Promise<void> }) {
  const bible = work.story_bible;
  const [draft, setDraft] = useState<StoryBible>(bible || { summary: "", theme: "", world: "", ending: "", style_rules: "", title_interpretation: "", reader_promise: "", core_hook: "", core_conflict: "", stakes: "", must_have_elements: [], avoid_drift: [] });
  const [saving, setSaving] = useState(false);
  const [characterDraft, setCharacterDraft] = useState<Character | null>(null);
  const [savingCharacter, setSavingCharacter] = useState(false);
  const [characterMessage, setCharacterMessage] = useState("");
  if (!bible) return <div className="card"><p className="muted">还没有故事档案，请先点击右上角“生成故事方案”。</p></div>;
  async function save() {
    setSaving(true);
    try { await api(`/works/${work.id}/story-bible`, { method: "PUT", body: JSON.stringify({ ...draft, locked: Boolean(draft.locked) }) }); await onSaved(); } finally { setSaving(false); }
  }
  async function saveCharacter() {
    if (!characterDraft) return;
    setSavingCharacter(true);
    setCharacterMessage("");
    try {
      const result = await api<{ impact: { affected_plan_count: number; affected_chapter_count: number } }>(`/works/${work.id}/characters/${characterDraft.id}`, { method: "PUT", body: JSON.stringify(characterDraft) });
      await onSaved();
      setCharacterDraft(null);
      const { affected_plan_count: plans, affected_chapter_count: chapters } = result.impact;
      setCharacterMessage(plans || chapters ? `已保存；${plans} 个大纲、${chapters} 章正文已标记为待复核，正文没有自动改写。` : "人物卡已保存；当前没有关联的大纲或正文需要复核。");
    } catch (error) { setCharacterMessage((error as Error).message); }
    finally { setSavingCharacter(false); }
  }
  const setCharacterField = <K extends keyof Character>(key: K, value: Character[K]) => setCharacterDraft((current) => current ? { ...current, [key]: value } : current);
  return <div className="grid">
    <div className="card span-7"><div className="toolbar"><div><h2>故事档案</h2><p className="subtitle">先锁定书名向读者承诺的故事，再展开梗概与世界观。</p></div><button className="button primary" onClick={save} disabled={saving}>{saving ? "保存中…" : "保存档案"}</button></div>{draft.generation_source && <div className="notice">生成来源：{draft.generation_source === "fallback" ? "演示模板" : "AI 模型"}　方案质量：{draft.quality_score || 0} 分{draft.quality_issues?.length ? `　待改进：${draft.quality_issues.join("；")}` : ""}</div>}<div className="field"><label>书名解读</label><textarea value={draft.title_interpretation || ""} onChange={(event) => setDraft({ ...draft, title_interpretation: event.target.value })} /></div><div className="field"><label>读者承诺</label><textarea value={draft.reader_promise || ""} onChange={(event) => setDraft({ ...draft, reader_promise: event.target.value })} /></div><div className="field"><label>核心钩子</label><textarea value={draft.core_hook || ""} onChange={(event) => setDraft({ ...draft, core_hook: event.target.value })} /></div><div className="field"><label>核心冲突与代价</label><textarea value={`${draft.core_conflict || ""}\n${draft.stakes || ""}`} onChange={(event) => { const [core_conflict = "", ...rest] = event.target.value.split("\n"); setDraft({ ...draft, core_conflict, stakes: rest.join("\n") }); }} /></div><div className="field"><label>故事梗概</label><textarea value={draft.summary} onChange={(event) => setDraft({ ...draft, summary: event.target.value })} /></div><div className="field"><label>必须兑现的元素（每行一项）</label><textarea value={(draft.must_have_elements || []).join("\n")} onChange={(event) => setDraft({ ...draft, must_have_elements: event.target.value.split("\n").filter(Boolean) })} /></div><div className="field"><label>防跑偏边界（每行一项）</label><textarea value={(draft.avoid_drift || []).join("\n")} onChange={(event) => setDraft({ ...draft, avoid_drift: event.target.value.split("\n").filter(Boolean) })} /></div><div className="field"><label>主题</label><textarea value={draft.theme} onChange={(event) => setDraft({ ...draft, theme: event.target.value })} /></div><div className="field"><label>世界观</label><textarea value={draft.world} onChange={(event) => setDraft({ ...draft, world: event.target.value })} /></div><div className="field"><label>结局方向</label><textarea value={draft.ending} onChange={(event) => setDraft({ ...draft, ending: event.target.value })} /></div><div className="field"><label>文风规则</label><textarea value={draft.style_rules} onChange={(event) => setDraft({ ...draft, style_rules: event.target.value })} /></div></div>
    <div className="card span-5"><div className="toolbar"><div><h3>人物小传</h3><p className="subtitle">可直接编辑正式人物卡；已有正文不会自动重写。</p></div></div>{characterMessage && <div className="notice">{characterMessage}</div>}{work.characters?.length ? <div className="asset-list">{work.characters.map((character) => {
      const editing = characterDraft?.id === character.id;
      return <div className="asset" key={character.id}>{editing && characterDraft ? <><div className="two-col"><div className="field"><label>姓名</label><input value={characterDraft.name} onChange={(event) => setCharacterField("name", event.target.value)} /></div><div className="field"><label>身份</label><input value={characterDraft.role} onChange={(event) => setCharacterField("role", event.target.value)} /></div></div><div className="field"><label>剧情作用</label><textarea value={characterDraft.story_function || ""} onChange={(event) => setCharacterField("story_function", event.target.value)} /></div><div className="field"><label>人物小传</label><textarea value={characterDraft.biography} onChange={(event) => setCharacterField("biography", event.target.value)} /></div><div className="field"><label>外貌</label><textarea value={characterDraft.appearance || characterDraft.portrayal || ""} onChange={(event) => setCharacterField("appearance", event.target.value)} /></div><div className="field"><label>性格</label><textarea value={characterDraft.personality} onChange={(event) => setCharacterField("personality", event.target.value)} /></div><div className="field"><label>语言习惯</label><textarea value={characterDraft.voice} onChange={(event) => setCharacterField("voice", event.target.value)} /></div><div className="field"><label>目标</label><textarea value={characterDraft.dramatic_core?.goal || characterDraft.goal || ""} onChange={(event) => setCharacterField("dramatic_core", { ...characterDraft.dramatic_core, goal: event.target.value })} /></div><div className="field"><label>深层动机</label><textarea value={characterDraft.dramatic_core?.motivation || characterDraft.motivation || ""} onChange={(event) => setCharacterField("dramatic_core", { ...characterDraft.dramatic_core, motivation: event.target.value })} /></div><div className="field"><label>缺陷</label><textarea value={characterDraft.dramatic_core?.flaw || characterDraft.flaw || ""} onChange={(event) => setCharacterField("dramatic_core", { ...characterDraft.dramatic_core, flaw: event.target.value })} /></div><div className="field"><label>人物弧</label><textarea value={characterDraft.arc || characterDraft.character_arc || ""} onChange={(event) => setCharacterField("arc", event.target.value)} /></div><div className="field"><label>秘密</label><textarea value={characterDraft.secret} onChange={(event) => setCharacterField("secret", event.target.value)} /></div><div className="field"><label>关系</label><textarea value={characterDraft.relationships} onChange={(event) => setCharacterField("relationships", event.target.value)} /></div><div className="toolbar-actions"><button className="button primary" onClick={saveCharacter} disabled={savingCharacter}>{savingCharacter ? "保存中…" : "保存人物卡"}</button><button className="button" onClick={() => setCharacterDraft(null)} disabled={savingCharacter}>取消</button></div></> : <><div className="toolbar"><strong>{character.name}<span className="tag">{character.role}</span></strong><button className="button" onClick={() => { setCharacterMessage(""); setCharacterDraft({ ...character, dramatic_core: { ...character.dramatic_core } }); }}>编辑</button></div><p>{character.biography}</p><p>外貌：{character.appearance || character.portrayal || "未填写"}</p><p>性格：{character.personality || "未填写"}</p><p>语言习惯：{character.voice || "未填写"}</p><p>目标：{character.dramatic_core?.goal || character.goal}</p><p>深层动机：{character.dramatic_core?.motivation || character.motivation}</p><p>缺陷：{character.dramatic_core?.flaw || character.flaw}</p><p>人物弧：{character.arc || character.character_arc}</p><p>秘密：{character.secret}</p><p>关系：{character.relationships}</p></>}</div>;
    })}</div> : <p className="muted">生成故事方案后，这里会出现完整人物小传。</p>}</div>
  </div>;
}

function Writing({ work, chapterNo, draft, plan, report, stateDiff, busy, onSelect, onGenerate, onSave, onChange, onReview, onRetryState }: { work: Work; chapterNo: number; draft: Chapter | null; plan?: ChapterPlan; report?: QualityReport; stateDiff: StateExtraction | null; busy: string; onSelect: (no: number) => void; onGenerate: () => void; onSave: () => void; onChange: (chapter: Chapter | null) => void; onReview: (kind: "character" | "timeline" | "alias" | "foreshadow", id: string, action: "accept" | "reject") => void; onRetryState: () => void }) {
  const items = work.chapter_plans?.length ? work.chapter_plans : [{ chapter_no: 1, title: "第1章", goal: "", conflict: "", beats: [], hook: "" }];
  return <div className="grid"><div className="card span-4"><div className="toolbar"><div><h3>章节</h3><span className="muted">选择要生成或修改的章节</span></div><span className="tag">{work.chapters?.length || 0} 已写</span></div><div className="chapter-list">{items.map((item) => <button key={item.chapter_no} className={`chapter-row ${item.chapter_no === chapterNo ? "active" : ""}`} onClick={() => onSelect(item.chapter_no)}><span>第{item.chapter_no}章 {item.title?.replace(/^第\d+章\s*/, "")}</span><small>{work.chapters?.some((chapterItem) => chapterItem.chapter_no === item.chapter_no) ? "已生成" : "待写"}</small></button>)}</div></div><div className="card span-8"><div className="toolbar"><div><h2>{draft?.title || plan?.title || `第${chapterNo}章`}</h2><p className="subtitle">{plan?.goal || "先生成本章，或直接输入正文。"}</p></div><div className="toolbar-actions"><button className="button primary" onClick={onGenerate} disabled={!!busy}>{busy === "chapter" ? "正在写作…" : "AI 写本章"}</button><button className="button" onClick={onSave} disabled={!!busy || !draft}>{busy === "save" ? "保存中…" : "保存修改"}</button></div></div>{plan && <div className="notice">本章冲突：{plan.conflict || "未设置"}　结尾钩子：{plan.hook || "未设置"}</div>}<div className="field"><textarea className="chapter-content" value={draft?.content || ""} onChange={(event) => onChange({ chapter_no: chapterNo, title: draft?.title || plan?.title || `第${chapterNo}章`, content: event.target.value, status: draft?.status || "draft" })} placeholder="点击“AI 写本章”，或在这里输入正文…" /></div>{stateDiff && <StateDiffPanel extraction={stateDiff} busy={busy} onReview={onReview} onRetry={onRetryState} />}{report && <div className="card" style={{ padding: 15, background: "#fbfaf6" }}><div className="toolbar"><h3>本章质检</h3><span className="score">{report.score}</span></div>{report.issues?.length ? report.issues.map((issue, index) => <div className="issue" key={`${issue.kind}-${index}`}><strong>{issue.severity === "high" ? "高" : issue.severity === "medium" ? "中" : "低"} · {issue.message}</strong><p>{issue.evidence || issue.suggestion}</p></div>) : <p className="muted">暂未发现明显问题，可以继续修改或确认。</p>}</div>}</div></div>;
}

function StateDiffPanel({ extraction, busy, onReview, onRetry }: { extraction: StateExtraction; busy: string; onReview: (kind: "character" | "timeline" | "alias" | "foreshadow", id: string, action: "accept" | "reject") => void; onRetry: () => void }) {
  const changeCount = extraction.characters?.length || 0;
  const eventCount = extraction.timeline_events?.length || 0;
  const aliasCount = extraction.aliases?.length || 0;
  const foreshadowCount = extraction.foreshadows?.length || 0;
  const reviewButtons = (kind: "character" | "timeline" | "alias" | "foreshadow", id: string, status: string) => status === "pending" ? <div className="toolbar-actions"><button className="button primary" onClick={() => onReview(kind, id, "accept")} disabled={!!busy}>接受</button><button className="button" onClick={() => onReview(kind, id, "reject")} disabled={!!busy}>拒绝</button></div> : <span className="tag">{status === "accepted" || status === "confirmed" ? "已接受" : status === "rejected" ? "已拒绝" : status}</span>;
  return <div className="card state-diff"><div className="toolbar"><div><h3>本章作品状态变化</h3><p className="subtitle">检测到 {changeCount} 项角色变化、{aliasCount} 个别名候选、{eventCount} 个时间线候选、{foreshadowCount} 个伏笔候选。</p></div><div className="toolbar-actions"><span className="tag">{extraction.status === "queued" ? "后台提取中" : extraction.status === "pending" ? "待审核" : extraction.status}</span><button className="button" onClick={onRetry} disabled={!!busy || extraction.status === "queued"}>{busy === "state-extraction" ? "正在重提…" : "重新提取"}</button></div></div><p className="muted">接受后才会写入正式作品状态；每一项都保留正文证据和章节版本。</p>{extraction.warning && <div className="notice">{extraction.warning}</div>}{extraction.status === "queued" ? <p className="muted">正文已生成，状态提取正在后台执行，完成后会自动显示候选项。</p> : <>{changeCount > 0 && <><h3>角色状态 Diff</h3><div className="diff-list">{extraction.characters.map((change) => <div className="asset" key={change.id}><div className="toolbar"><strong>{change.character_name} · {change.field}</strong>{reviewButtons("character", change.id, change.status)}</div><p>{String(change.old_value ?? "未记录")} → {String(change.new_value ?? "未记录")}</p><p>证据：{change.evidence || "未提供"}　置信度：{Math.round(change.confidence * 100)}%</p></div>)}</div></>}{aliasCount > 0 && <><h3 style={{ marginTop: 16 }}>人物别名候选</h3><div className="diff-list">{extraction.aliases.map((alias) => <div className="asset" key={alias.id}><div className="toolbar"><strong>{alias.character_name} · {alias.alias}</strong>{reviewButtons("alias", alias.id, alias.status)}</div></div>)}</div></>}{eventCount > 0 && <><h3 style={{ marginTop: 16 }}>时间线候选</h3><div className="diff-list">{extraction.timeline_events.map((event) => <div className="asset" key={event.id}><div className="toolbar"><strong>{event.title}</strong>{reviewButtons("timeline", event.id, event.review_status)}</div><p>{event.description || "暂无描述"}</p><p>时间：{event.story_time_text || "待确认"}　地点：{event.location || "未提取"}</p><p>证据：{event.evidence || "未提供"}　置信度：{Math.round(event.confidence * 100)}%</p></div>)}</div></>}{foreshadowCount > 0 && <><h3 style={{ marginTop: 16 }}>伏笔候选</h3><div className="diff-list">{(extraction.foreshadows || []).map((item) => <div className="asset" key={item.id}><div className="toolbar"><strong>{item.clue}</strong>{reviewButtons("foreshadow", item.id, item.status)}</div><p>第{item.planted_chapter || "?"}章埋设 · 第{item.expected_reveal_chapter || "?"}章预计回收</p><p>证据：{item.evidence || "未提供"}　置信度：{Math.round(item.confidence * 100)}%</p></div>)}</div></>}{changeCount === 0 && aliasCount === 0 && eventCount === 0 && foreshadowCount === 0 && <p className="muted">本章没有提取到可审核的状态变化。</p>}</>}</div>;
}
