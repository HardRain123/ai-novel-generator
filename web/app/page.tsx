"use client";

import { useCallback, useEffect, useMemo, useState } from "react";
import type { FormEvent } from "react";

const API = process.env.NEXT_PUBLIC_API_BASE_URL || "http://localhost:8000/api";

type Work = {
  id: string; title: string; genre: string; target_audience: string; estimated_words: number;
  writing_style: string; premise: string; status: string; updated_at: string; model_profile_id?: string | null;
  story_bible?: StoryBible; characters?: Character[]; plot_arcs?: PlotArc[];
  chapter_plans?: ChapterPlan[]; chapters?: Chapter[]; foreshadows?: { id: string }[]; quality_reports?: QualityReport[];
};
type StoryBible = { id?: string; summary: string; theme: string; world: string; ending: string; style_rules: string; locked?: number | boolean };
type Character = { id: string; name: string; role: string; goal: string; conflict: string; personality: string; background: string; status: string; knowledge: string };
type PlotArc = { id: string; title: string; synopsis: string; sequence: number };
type ChapterPlan = { chapter_no: number; title: string; goal: string; conflict: string; beats: string[]; hook: string };
type Chapter = { chapter_no: number; title: string; content: string; status: string };
type Issue = { kind: string; severity: string; message: string; evidence?: string; suggestion?: string };
type QualityReport = { chapter_no: number; score: number; issues: Issue[] };
type StateChange = { id: string; character_name: string; field: string; old_value: unknown; new_value: unknown; evidence: string; confidence: number; status: string };
type TimelineCandidate = { id: string; title: string; description: string; story_time_text: string; time_type: string; location: string; participants: string[]; evidence: string; confidence: number; review_status: string };
type AliasCandidate = { id: string; character_name: string; alias: string; status: string };
type ForeshadowCandidate = { id: string; clue: string; kind: string; planted_chapter: number; expected_reveal_chapter: number; evidence: string; confidence: number; status: string };
type StateExtraction = { id: string; status: string; model: string; warning: string; chapter_version_id: string; characters: StateChange[]; aliases: AliasCandidate[]; timeline_events: TimelineCandidate[]; foreshadows?: ForeshadowCandidate[] };
type ModelProfile = { id: string; name: string; provider: string; base_url: string; model: string; reasoning_effort: string; timeout_seconds: number; is_default: number | boolean; has_api_key: boolean; api_key_masked: string; last_test_status: string; last_test_at?: string };
type GenerationJob = { id: string; status: string; error: string; output: Record<string, unknown>; progress: number; stage: string; stage_label: string; message: string; model_profile_id?: string | null };
type Foreshadow = { id: string; clue: string; kind: string; planted_chapter: number; expected_reveal_chapter: number; actual_reveal_chapter: number; status: string; note: string; evidence: string };
type TrendItem = { id: string; source: string; rank: number; board: string; category: string; title: string; author: string; synopsis: string; metric_label: string; metric_value: string; source_url: string; captured_at: string };
type TrendIdea = { title: string; genre: string; audience: string; hook: string; premise: string; synopsis: string; differentiation: string; risk: string };

async function api<T>(path: string, options?: RequestInit): Promise<T> {
  const response = await fetch(`${API}${path}`, { headers: { "Content-Type": "application/json" }, ...options });
  const contentType = response.headers.get("content-type") || "";
  const data = contentType.includes("application/json") ? await response.json() : { detail: await response.text() };
  if (!response.ok) throw new Error(data.detail || "请求失败");
  return data as T;
}

async function waitForGenerationJob(workId: string, initial: GenerationJob, onUpdate: (job: GenerationJob) => void) {
  let job = initial;
  onUpdate(job);
  for (let attempt = 0; attempt < 300 && (job.status === "queued" || job.status === "running" || job.status === "cancel_requested"); attempt += 1) {
    await new Promise((resolve) => window.setTimeout(resolve, 1000));
    job = await api<GenerationJob>(`/works/${workId}/generation-jobs/${initial.id}`);
    onUpdate(job);
  }
  if (job.status !== "completed") throw new Error(job.error || "生成任务未完成");
  return job.output;
}

async function runGenerationJob(workId: string, kind: "setup" | "outline" | "chapter" | "state_extraction", payload: Record<string, unknown>, modelProfileId: string | null, onUpdate: (job: GenerationJob) => void) {
  const created = await api<GenerationJob>(`/works/${workId}/generation-jobs`, {
    method: "POST",
    body: JSON.stringify({ kind, payload, model_profile_id: modelProfileId, idempotency_key: `${kind}-${Date.now()}` }),
  });
  return waitForGenerationJob(workId, created, onUpdate);
}

function downloadWork(work: Work) {
  const lines = [`# ${work.title}`, "", `题材：${work.genre || "未设"}`, `一句话设想：${work.premise || "未设"}`, "", "## 故事档案", `- 梗概：${work.story_bible?.summary || ""}`, `- 主题：${work.story_bible?.theme || ""}`, `- 世界观：${work.story_bible?.world || ""}`, `- 结局方向：${work.story_bible?.ending || ""}`, "", "## 主要人物"];
  for (const character of work.characters || []) lines.push(`### ${character.name}`, `- 身份：${character.role}`, `- 目标：${character.goal}`, `- 冲突：${character.conflict}`, "");
  lines.push("## 章节");
  for (const chapter of work.chapters || []) lines.push(`### ${chapter.title || `第${chapter.chapter_no}章`}`, "", chapter.content, "");
  const blob = new Blob([lines.join("\n")], { type: "text/markdown;charset=utf-8" });
  const url = URL.createObjectURL(blob); const link = document.createElement("a");
  link.href = url; link.download = `${work.title || "作品"}.md`; link.click(); URL.revokeObjectURL(url);
}

export default function Home() {
  const [works, setWorks] = useState<Work[]>([]);
  const [selectedId, setSelectedId] = useState("");
  const [work, setWork] = useState<Work | null>(null);
  const [tab, setTab] = useState("overview");
  const [globalView, setGlobalView] = useState<"work" | "trends" | "settings">("work");
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

  useEffect(() => {
    const path = window.location.pathname;
    if (path.startsWith("/trends")) setGlobalView("trends");
    else if (path.startsWith("/settings/models")) setGlobalView("settings");
    else {
      const match = path.match(/^\/works\/([^/]+)/);
      if (match) setSelectedId(decodeURIComponent(match[1]));
    }
  }, []);

  const refreshWorks = useCallback(async () => {
    const data = await api<{ items: Work[] }>("/works");
    setWorks(data.items);
    if (!selectedId && data.items[0]) setSelectedId(data.items[0].id);
  }, [selectedId]);

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
    api<{ items: GenerationJob[] }>(`/works/${selectedId}/generation-jobs?active=true`).then((data) => setActiveJob(data.items[0] || null)).catch(() => undefined);
  }, [selectedId]);

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
      form.reset(); await refreshWorks(); setSelectedId(created.id); setWork(created); setTab("overview"); window.history.pushState({}, "", `/works/${created.id}`);
    } catch (e) { setError((e as Error).message); } finally { setBusy(""); }
  }

  async function generate(kind: "setup" | "outline") {
    if (!work) return;
    setBusy(kind); setError("");
    try {
      if (healthMode === "demo" && !window.confirm("当前未配置可用模型，将使用演示数据继续。确定继续吗？")) return;
      if (kind === "outline" && (work.chapter_plans?.length || 0) > 0 && !window.confirm(`当前已有 ${work.chapter_plans?.length} 章大纲，重新生成会覆盖它。确定继续吗？`)) return;
      await runGenerationJob(work.id, kind, kind === "outline" ? { chapter_count: chapterCount } : {}, work.model_profile_id || profiles.find((item) => item.is_default)?.id || null, setActiveJob);
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
      setTab("write");
    } catch (e) { setError((e as Error).message); } finally { setBusy(""); }
  }

  async function saveChapter() {
    if (!work || !draft) return;
    setBusy("save"); setError("");
    try {
      const data = await api<{ work: Work; state_extraction: StateExtraction }>(`/works/${work.id}/chapters/${chapterNo}`, { method: "PATCH", body: JSON.stringify({ title: draft.title, content: draft.content, status: draft.status }) });
      setWork(data.work); setDraft(data.work.chapters?.find((item) => item.chapter_no === chapterNo) || draft); setStateDiff(data.state_extraction);
    } catch (e) { setError((e as Error).message); } finally { setBusy(""); }
  }

  function selectChapter(no: number) {
    setChapterNo(no); setDraft(work?.chapters?.find((item) => item.chapter_no === no) || null); setStateDiff(null);
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

  if (loading) return <div className="loading">正在打开织梦台…</div>;

  const navigate = (view: "work" | "trends" | "settings", path: string) => {
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
        </div>
        <div className="side-label">我的作品</div>
        <div className="work-list">
          {works.map((item) => <button key={item.id} className={`work-item ${item.id === selectedId ? "active" : ""}`} onClick={() => { setSelectedId(item.id); navigate("work", `/works/${item.id}`); }}><strong>{item.title}</strong><span>{item.genre || "未设题材"} · {item.status === "writing" ? "写作中" : "草稿"}</span></button>)}
          {!works.length && <div className="empty-side">还没有作品。<br />从右侧创建第一本。</div>}
        </div>
        <div className="side-label">当前版本</div>
        <div className="empty-side">MVP 0.1<br />故事规划 · 章节写作 · 一致性检查</div>
      </aside>
      <main className="main">
        {globalView === "settings" ? <ModelSettings profiles={profiles} onChanged={() => Promise.all([api<{ items: ModelProfile[] }>("/model-profiles").then((data) => setProfiles(data.items)), api<{ mode: "live" | "demo" }>("/health").then((data) => setHealthMode(data.mode))]).then(() => undefined)} /> : globalView === "trends" ? <Trends profiles={profiles} onCreate={(created) => { navigate("work", `/works/${created.id}`); setSelectedId(created.id); setWork(created); }} /> : <>
        <div className="topbar">
          <div><div className="eyebrow">AI NOVEL STUDIO / MVP</div><h1>{work?.title || "开始你的第一部长篇"}</h1><p className="subtitle">先给 AI 一个方向，剩下的由故事规划、正文生成和作品状态共同推进。</p><span className={`connection ${healthMode}`}>{healthMode === "live" ? "● AI 已连接" : "● 演示模式：未配置模型"}</span>{work && <label className="work-model">本作品模型<select value={work.model_profile_id || ""} onChange={(e) => setWorkProfile(e.target.value)}><option value="">使用默认模型</option>{profiles.map((profile) => <option key={profile.id} value={profile.id}>{profile.name} · {profile.model}</option>)}</select></label>}</div>
          {work && <div className="top-actions"><button className="button" onClick={() => downloadWork(work)}>导出 Markdown</button><button className="button primary" onClick={() => generate("setup")} disabled={!!busy}>{busy === "setup" ? "正在规划…" : "生成故事方案"}</button><label className="chapter-count">章数<input type="number" min="1" max="200" value={chapterCount} onChange={(e) => setChapterCount(Math.max(1, Math.min(200, Number(e.target.value) || 12)))} /></label><button className="button dark" onClick={() => generate("outline")} disabled={!!busy}>{busy === "outline" ? "正在拆大纲…" : `生成 ${chapterCount} 章大纲`}</button></div>}
        </div>
        {activeJob && <GenerationProgress job={activeJob} workId={work?.id || selectedId} onCancel={() => api(`/works/${work?.id || selectedId}/generation-jobs/${activeJob.id}/cancel`, { method: "POST" }).then((job) => setActiveJob(job as GenerationJob)).catch((e) => setError(e.message))} onRetry={retryActiveJob} />}
        {error && <div className="notice">{error}</div>}
        {!work ? <CreatePanel busy={busy} profiles={profiles} onCreate={createWork} /> : <>
          <div className="grid"><div className="card stat span-4"><span className="stat-label">预计篇幅</span><span className="stat-value">{(work.estimated_words / 10000).toFixed(1)}万字</span></div><div className="card stat span-4"><span className="stat-label">章节进度</span><span className="stat-value">{work.chapters?.length || 0} <small className="muted">/ {work.chapter_plans?.length || "—"}</small></span></div><div className="card stat span-4"><span className="stat-label">作品资产</span><span className="stat-value">{(work.characters?.length || 0) + (work.foreshadows?.length || 0)} <small className="muted">项</small></span></div></div>
          <div className="tabs">{[["overview", "总览"], ["outline", "章节大纲"], ["write", "写作台"], ["assets", "故事档案"], ["foreshadows", "伏笔"]].map(([key, label]) => <button key={key} className={`tab ${tab === key ? "active" : ""}`} onClick={() => setTab(key)}>{label}</button>)}</div>
          {tab === "overview" && <Overview work={work} onTab={setTab} />}
          {tab === "outline" && <Outline work={work} />}
          {tab === "assets" && <Assets key={work.id} work={work} onSaved={() => loadWork(work.id)} />}
          {tab === "foreshadows" && <Foreshadows work={work} onSaved={() => loadWork(work.id)} />}
          {tab === "write" && <Writing work={work} chapterNo={chapterNo} draft={draft} plan={selectedPlan} report={latestReport} stateDiff={stateDiff} busy={busy} onSelect={selectChapter} onGenerate={generateChapter} onSave={saveChapter} onChange={setDraft} onReview={reviewStateItem} />}
        </>}
        </>}
      </main>
    </div>
  );
}

function GenerationProgress({ job, workId, onCancel, onRetry }: { job: GenerationJob; workId: string; onCancel: () => void; onRetry: () => void }) {
  const terminal = ["completed", "failed", "canceled"].includes(job.status);
  return <div className={`card job-panel ${terminal ? "job-terminal" : ""}`}><div className="toolbar"><div><strong>{job.stage_label || "生成任务"}</strong><p className="muted">{job.message || (job.status === "queued" ? "等待 worker 接手" : "任务正在处理")}</p></div><div className="job-meta"><span>{job.progress || 0}%</span>{!terminal && job.status !== "cancel_requested" && <button className="button" onClick={onCancel}>取消</button>}{job.status === "failed" && <button className="button" onClick={onRetry}>重试</button>}</div></div><div className="progress-track"><div className="progress-value" style={{ width: `${Math.max(0, Math.min(100, job.progress || 0))}%` }} /></div>{job.error && <p className="error-text">{job.error}</p>}</div>;
}

function ModelSettingsEditor({ profiles, onChanged }: { profiles: ModelProfile[]; onChanged: () => Promise<void> }) {
  const [editing, setEditing] = useState<ModelProfile | null>(null);
  const [form, setForm] = useState({ name: "", provider: "openai_compatible", base_url: "", model: "", api_key: "", reasoning_effort: "auto", timeout_seconds: "90", is_default: false });
  const [message, setMessage] = useState("");
  const [modelOptions, setModelOptions] = useState<string[]>([]);
  const reset = () => { setEditing(null); setForm({ name: "", provider: "openai_compatible", base_url: "", model: "", api_key: "", reasoning_effort: "auto", timeout_seconds: "90", is_default: false }); };
  const edit = (profile: ModelProfile) => { setEditing(profile); setForm({ name: profile.name, provider: profile.provider, base_url: profile.base_url, model: profile.model, api_key: "", reasoning_effort: profile.reasoning_effort || "auto", timeout_seconds: String(profile.timeout_seconds || 90), is_default: Boolean(profile.is_default) }); };
  const applyPreset = (name: string) => api<{ presets: Record<string, { name: string; base_url: string; model: string }> }>("/model-profiles").then((data) => { const preset = data.presets[name]; if (preset) setForm((current) => ({ ...current, name: preset.name, base_url: preset.base_url, model: preset.model, provider: name })); });
  async function fetchModelList(id: string) { setMessage("读取模型列表中…"); try { const data = await api<{ items: string[] }>(`/model-profiles/${id}/models`); setModelOptions(data.items); setMessage(data.items.length ? `已读取 ${data.items.length} 个模型，可从模型名称输入框选择` : "服务未返回可用模型列表，请手动填写模型名称"); } catch (e) { setMessage((e as Error).message); } }
  async function save(event: FormEvent) { event.preventDefault(); setMessage(""); try { const { api_key, ...rest } = form; const body = { ...rest, timeout_seconds: Number(form.timeout_seconds), ...(api_key ? { api_key } : {}) }; if (editing) await api(`/model-profiles/${editing.id}`, { method: "PATCH", body: JSON.stringify(body) }); else await api("/model-profiles", { method: "POST", body: JSON.stringify({ ...body, api_key: api_key || "" }) }); await onChanged(); reset(); setMessage("已保存模型配置"); } catch (e) { setMessage((e as Error).message); } }
  async function test(id: string) { setMessage("测试连接中…"); try { const result = await api<{ message: string }>(`/model-profiles/${id}/test`, { method: "POST" }); setMessage(result.message); await onChanged(); } catch (e) { setMessage((e as Error).message); } }
  const codexAuth = form.provider === "codex_auth";
  return <div><div className="topbar"><div><div className="eyebrow">MODEL CENTER</div><h1>模型服务</h1><p className="subtitle">保存多个 OpenAI 兼容配置，或使用本机 Codex Auth 登录；Key 只在服务端加密保存。</p></div><button className="button" onClick={reset}>新建配置</button></div>{message && <div className="notice">{message}</div>}<div className="grid"><div className="card span-5"><h2>{editing ? "编辑模型配置" : "添加模型配置"}</h2><div className="preset-row"><button className="button" type="button" onClick={() => applyPreset("deepseek")}>DeepSeek</button><button className="button" type="button" onClick={() => applyPreset("qwen")}>通义千问</button><button className="button" type="button" onClick={() => applyPreset("kimi")}>Kimi</button><button className="button" type="button" onClick={() => applyPreset("codex_auth")}>Codex Auth</button></div><form onSubmit={save}><div className="field"><label>配置名称</label><input required value={form.name} onChange={(e) => setForm({ ...form, name: e.target.value })} placeholder="例如：HotAI GPT-5.6 Luna" /></div>{codexAuth ? <div className="notice">Codex Auth 使用运行 worker 的机器上的 Codex CLI 登录状态。先执行 <code>codex login</code>，然后点击“测试连接”。</div> : <div className="field"><label>Base URL</label><input required value={form.base_url} onChange={(e) => setForm({ ...form, base_url: e.target.value })} placeholder="https://api.example.com/v1" /></div>}<div className="field"><label>模型名称</label><div className="toolbar"><input required list="available-models" value={form.model} onChange={(e) => setForm({ ...form, model: e.target.value })} placeholder="手动填写模型 ID" />{editing && <button className="button" type="button" onClick={() => fetchModelList(editing.id)}>获取模型列表</button>}</div><datalist id="available-models">{modelOptions.map((model) => <option key={model} value={model} />)}</datalist></div>{!codexAuth && <div className="field"><label>API Key {editing && <span className="muted">（留空保持原 Key）</span>}</label><input type="password" value={form.api_key} onChange={(e) => setForm({ ...form, api_key: e.target.value })} placeholder={editing ? "已加密保存，不回显" : "sk-…"} /></div>}<div className="two-col"><div className="field"><label>推理强度</label><select value={form.reasoning_effort} onChange={(e) => setForm({ ...form, reasoning_effort: e.target.value })}><option value="auto">自动</option><option value="low">低</option><option value="medium">中</option><option value="high">高</option><option value="xhigh">极高 / xhigh</option></select></div><div className="field"><label>超时（秒）</label><input type="number" min="1" max="600" value={form.timeout_seconds} onChange={(e) => setForm({ ...form, timeout_seconds: e.target.value })} /></div></div><label className="check"><input type="checkbox" checked={form.is_default} onChange={(e) => setForm({ ...form, is_default: e.target.checked })} />设为默认模型</label><div className="toolbar"><button className="button primary" type="submit">保存配置</button>{editing && <button className="button" type="button" onClick={reset}>取消编辑</button>}</div></form></div><div className="card span-7"><h2>已保存配置</h2><p className="subtitle">连接测试会验证模型、JSON 输出和推理参数；不支持的参数会直接提示。</p>{profiles.length ? <div className="asset-list">{profiles.map((profile) => <div className="asset" key={profile.id}><div className="toolbar"><div><strong>{profile.name} {Boolean(profile.is_default) && <span className="tag">默认</span>}</strong><p>{profile.provider === "codex_auth" ? "Codex Auth · 本机 CLI" : `${profile.model} · ${profile.base_url}`}</p></div><span className={`tag ${profile.last_test_status === "ok" ? "success" : ""}`}>{profile.provider === "codex_auth" ? (profile.last_test_status === "ok" ? "Codex 已登录" : "待登录") : profile.has_api_key ? `Key ${profile.api_key_masked}` : "未配置 Key"}</span></div><div className="toolbar"><span className="muted">推理：{profile.reasoning_effort || "自动"} · {profile.last_test_status === "ok" ? "已验证" : "未验证"}</span><div className="toolbar-actions"><button className="button" onClick={() => test(profile.id)}>测试连接</button><button className="button" onClick={() => edit(profile)}>编辑</button></div></div></div>)}</div> : <p className="muted">还没有模型配置。添加后生成按钮会显示为实时 AI 模式。</p>}</div></div></div>;
}

function ModelSettings({ profiles, onChanged }: { profiles: ModelProfile[]; onChanged: () => Promise<void> }) {
  const [message, setMessage] = useState("");

  async function remove(profile: ModelProfile) {
    if (!window.confirm(`确定删除模型配置“${profile.name}”吗？`)) return;
    setMessage("删除中…");
    try {
      await api(`/model-profiles/${profile.id}`, { method: "DELETE" });
      await onChanged();
      setMessage(`已删除模型配置“${profile.name}”`);
    } catch (e) {
      setMessage((e as Error).message);
    }
  }

  return <div>
    <ModelSettingsEditor profiles={profiles} onChanged={onChanged} />
    {message && <div className="notice">{message}</div>}
    {profiles.length > 0 && <div className="card model-delete-panel">
      <div className="toolbar"><div><h2>删除模型配置</h2><p className="subtitle">删除后该配置将从模型列表中移除，已生成的内容不受影响。</p></div></div>
      <div className="asset-list">{profiles.map((profile) => <div className="asset" key={`delete-${profile.id}`}><div className="toolbar"><div><strong>{profile.name}</strong><p className="muted">{profile.model}</p></div><button className="button" type="button" onClick={() => remove(profile)}>删除</button></div></div>)}</div>
    </div>}
  </div>;
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
  const [items, setItems] = useState<TrendItem[]>([]); const [selected, setSelected] = useState<string[]>([]); const [ideas, setIdeas] = useState<TrendIdea[]>([]); const [analysisId, setAnalysisId] = useState(""); const [keyword, setKeyword] = useState(""); const [loading, setLoading] = useState(false); const [message, setMessage] = useState("");
  const usableProfiles = profiles.filter((profile) => profile.has_api_key || (profile.provider === "codex_auth" && profile.last_test_status === "ok"));
  async function search() { setLoading(true); setMessage(""); try { const data = await api<{ items: TrendItem[]; sources: { source: string; stale?: boolean; error?: string }[] }>("/trends/search", { method: "POST", body: JSON.stringify({ sources: ["fanqie", "qidian", "jjwxc"], keyword, refresh: true }) }); setItems(data.items); setMessage(data.sources.map((source) => `${source.source}${source.stale ? "（缓存）" : ""}${source.error ? `：${source.error}` : ""}`).join(" · ")); } catch (e) { setMessage((e as Error).message); } finally { setLoading(false); } }
  async function analyze() { if (!selected.length) return; setLoading(true); try { const usable = usableProfiles.find((profile) => Boolean(profile.is_default)) || usableProfiles[0]; const data = await api<{ id: string; ideas: TrendIdea[] }>("/trends/analyze", { method: "POST", body: JSON.stringify({ item_ids: selected, model_profile_id: usable?.id || null }) }); setIdeas(data.ideas); setAnalysisId(data.id); } catch (e) { setMessage((e as Error).message); } finally { setLoading(false); } }
  async function createIdea(index: number) { try { const work = await api<Work>("/works/from-trend-idea", { method: "POST", body: JSON.stringify({ analysis_id: analysisId, idea_index: index, model_profile_id: profiles.find((profile) => profile.is_default)?.id || null }) }); onCreate(work); } catch (e) { setMessage((e as Error).message); } }
  return <div><div className="topbar"><div><div className="eyebrow">TREND RADAR</div><h1>热门灵感</h1><p className="subtitle">读取公开榜单元数据，分析题材信号，生成原创创意；不抓取小说全文。</p></div><button className="button primary" onClick={search} disabled={loading}>{loading ? "搜索中…" : "刷新榜单"}</button></div><div className="card trend-search"><div className="field"><label>关键词</label><input value={keyword} onChange={(e) => setKeyword(e.target.value)} onKeyDown={(e) => e.key === "Enter" && search()} placeholder="搜索书名、作者或题材" /></div><div className="toolbar"><span className="muted">来源：番茄 · 起点 · 晋江（公开榜单缓存30分钟）</span><button className="button" onClick={analyze} disabled={!selected.length || loading || !usableProfiles.length}>分析选中 {selected.length} 本</button></div>{!usableProfiles.length && <p className="muted">趋势分析需要先到“模型服务”配置 API Key 或完成 Codex Auth 登录；榜单浏览不受影响。</p>}</div>{message && <div className="notice">{message}</div>}<div className="grid"><div className="card span-7"><h2>榜单作品</h2>{items.length ? <div className="trend-list">{items.map((item) => <label className={`trend-row ${selected.includes(item.id) ? "selected" : ""}`} key={item.id}><input type="checkbox" checked={selected.includes(item.id)} onChange={(e) => setSelected(e.target.checked ? [...selected, item.id] : selected.filter((id) => id !== item.id))} /><span className="rank">{item.rank}</span><span><strong>{item.title}</strong><small>{item.source} · {item.category || "综合"} · {item.metric_label} {item.metric_value}</small><em>{item.synopsis}</em></span><a href={item.source_url} target="_blank" rel="noreferrer">来源</a></label>)}</div> : <p className="muted">点击“刷新榜单”获取公开热门作品。</p>}</div><div className="card span-5"><h2>原创创意</h2>{ideas.length ? <div className="asset-list">{ideas.map((idea, index) => <div className="asset idea-card" key={`${idea.title}-${index}`}><div className="toolbar"><strong>{idea.title}</strong><button className="button" onClick={() => createIdea(index)}>创建作品</button></div><p>{idea.genre} · {idea.audience}</p><p>{idea.hook}</p><p>{idea.synopsis}</p><p className="muted">差异化：{idea.differentiation}</p></div>)}</div> : <p className="muted">选中榜单作品后点击“分析选中”，这里会出现书名、题材和内容建议。</p>}</div></div></div>;
}

function CreatePanel({ busy, profiles, onCreate }: { busy: string; profiles: ModelProfile[]; onCreate: (form: HTMLFormElement) => void }) {
  return <div className="grid"><div className="card span-7"><h2>创建一部作品</h2><p className="subtitle">先填写最少的信息，故事资产由 AI 继续补齐。</p><form onSubmit={(event) => { event.preventDefault(); onCreate(event.currentTarget); }}><div className="field"><label>作品名</label><input name="title" required placeholder="例如：潮汐之后" /></div><div className="two-col"><div className="field"><label>题材</label><input name="genre" placeholder="都市、悬疑、科幻…" /></div><div className="field"><label>目标读者/平台</label><input name="audience" placeholder="女频 / 长篇连载" /></div></div><div className="two-col"><div className="field"><label>预计字数</label><input name="words" type="number" defaultValue="100000" min="0" /></div><div className="field"><label>文风</label><input name="style" placeholder="克制、快节奏、对白多" /></div></div><div className="field"><label>一句话设想（可选）</label><textarea name="premise" placeholder="一个人必须在……之前……" /></div>{profiles.length > 0 && <div className="field"><label>本作品模型</label><select name="model_profile_id" defaultValue=""><option value="">使用默认模型</option>{profiles.map((profile) => <option key={profile.id} value={profile.id}>{profile.name} · {profile.model}</option>)}</select></div>}<button className="button primary" disabled={!!busy}>{busy === "create" ? "正在创建…" : "创建作品"}</button></form></div><div className="card span-5"><h3>第一版会帮你完成</h3><div className="asset-list"><div className="asset"><strong>故事方案</strong><p>梗概、主题、世界观、主线冲突和主要人物。</p></div><div className="asset"><strong>章节大纲</strong><p>按卷和章节拆出目标、冲突、节奏和结尾钩子。</p></div><div className="asset"><strong>正文生成</strong><p>按章节计划写作，支持保存、重写和继续下一章。</p></div><div className="asset"><strong>一致性检查</strong><p>检查人物、时间线、伏笔和重复内容。</p></div></div></div></div>;
}

function Overview({ work, onTab }: { work: Work; onTab: (tab: string) => void }) {
  return <div className="grid"><div className="card span-7"><h2>作品总览</h2><p className="subtitle">{work.premise || "还没有一句话设想，先生成故事方案吧。"}</p><div className="field" style={{ marginTop: 22 }}><label>当前创作状态</label><p>{work.status === "writing" ? "正在写作，可以进入写作台继续生成。" : work.story_bible?.summary ? "故事档案已建立，可以生成章节大纲。" : "故事还在草稿阶段，建议先生成故事方案。"}</p></div><div className="toolbar"><button className="button" onClick={() => onTab("assets")}>查看故事档案</button><button className="button" onClick={() => onTab("write")}>进入写作台</button></div></div><div className="card span-5"><h3>下一步建议</h3><div className="outline"><div className="outline-item"><strong>01 · 建立故事档案</strong><p>让 AI 先明确人物、规则和主线，减少后续跑偏。</p></div><div className="outline-item"><strong>02 · 拆解章节计划</strong><p>每章先确定目标、冲突和结尾钩子，再写正文。</p></div><div className="outline-item"><strong>03 · 写作并检查</strong><p>生成后先看质检报告，再确认写入作品状态。</p></div></div></div></div>;
}

function Outline({ work }: { work: Work }) {
  return <div className="grid"><div className="card span-8"><div className="toolbar"><div><h2>章节大纲</h2><p className="subtitle">每一章都是可确认、可修改的写作计划。</p></div><span className="tag">{work.chapter_plans?.length || 0} 章</span></div>{work.chapter_plans?.length ? <div className="outline">{work.chapter_plans.map((item) => <div className="outline-item" key={item.chapter_no}><strong>第{item.chapter_no}章 · {item.title}</strong><p>目标：{item.goal}</p><p>冲突：{item.conflict}</p><p>钩子：{item.hook}</p></div>)}</div> : <p className="muted">还没有章节大纲，请点击右上角“生成 12 章大纲”。</p>}</div><div className="card span-4"><h3>故事主线</h3>{work.plot_arcs?.length ? <div className="outline">{work.plot_arcs.map((arc) => <div className="outline-item" key={arc.id}><strong>{arc.title}</strong><p>{arc.synopsis}</p></div>)}</div> : <p className="muted">先生成故事方案。</p>}</div></div>;
}

function Assets({ work, onSaved }: { work: Work; onSaved: () => Promise<void> }) {
  const bible = work.story_bible;
  const [draft, setDraft] = useState<StoryBible>(bible || { summary: "", theme: "", world: "", ending: "", style_rules: "" });
  const [saving, setSaving] = useState(false);
  if (!bible) return <div className="card"><p className="muted">还没有故事档案，请先点击右上角“生成故事方案”。</p></div>;
  async function save() {
    setSaving(true);
    try { await api(`/works/${work.id}/story-bible`, { method: "PUT", body: JSON.stringify({ ...draft, locked: Boolean(draft.locked) }) }); await onSaved(); } finally { setSaving(false); }
  }
  return <div className="grid"><div className="card span-7"><div className="toolbar"><div><h2>故事档案</h2><p className="subtitle">这是 AI 写作时会持续参考的作品记忆，也可以由作者手动修正。</p></div><button className="button primary" onClick={save} disabled={saving}>{saving ? "保存中…" : "保存档案"}</button></div><div className="field"><label>故事梗概</label><textarea value={draft.summary} onChange={(event) => setDraft({ ...draft, summary: event.target.value })} /></div><div className="field"><label>主题</label><textarea value={draft.theme} onChange={(event) => setDraft({ ...draft, theme: event.target.value })} /></div><div className="field"><label>世界观</label><textarea value={draft.world} onChange={(event) => setDraft({ ...draft, world: event.target.value })} /></div><div className="field"><label>结局方向</label><textarea value={draft.ending} onChange={(event) => setDraft({ ...draft, ending: event.target.value })} /></div><div className="field"><label>文风规则</label><textarea value={draft.style_rules} onChange={(event) => setDraft({ ...draft, style_rules: event.target.value })} /></div></div><div className="card span-5"><h3>主要人物</h3>{work.characters?.length ? <div className="asset-list">{work.characters.map((character) => <div className="asset" key={character.id}><strong>{character.name}<span className="tag">{character.role}</span></strong><p>目标：{character.goal}</p><p>冲突：{character.conflict}</p><p>状态：{character.status}</p></div>)}</div> : <p className="muted">生成故事方案后，这里会出现人物卡。</p>}</div></div>;
}

function Writing({ work, chapterNo, draft, plan, report, stateDiff, busy, onSelect, onGenerate, onSave, onChange, onReview }: { work: Work; chapterNo: number; draft: Chapter | null; plan?: ChapterPlan; report?: QualityReport; stateDiff: StateExtraction | null; busy: string; onSelect: (no: number) => void; onGenerate: () => void; onSave: () => void; onChange: (chapter: Chapter | null) => void; onReview: (kind: "character" | "timeline" | "alias" | "foreshadow", id: string, action: "accept" | "reject") => void }) {
  const items = work.chapter_plans?.length ? work.chapter_plans : [{ chapter_no: 1, title: "第1章", goal: "", conflict: "", beats: [], hook: "" }];
  return <div className="grid"><div className="card span-4"><div className="toolbar"><div><h3>章节</h3><span className="muted">选择要生成或修改的章节</span></div><span className="tag">{work.chapters?.length || 0} 已写</span></div><div className="chapter-list">{items.map((item) => <button key={item.chapter_no} className={`chapter-row ${item.chapter_no === chapterNo ? "active" : ""}`} onClick={() => onSelect(item.chapter_no)}><span>第{item.chapter_no}章 {item.title?.replace(/^第\d+章\s*/, "")}</span><small>{work.chapters?.some((chapterItem) => chapterItem.chapter_no === item.chapter_no) ? "已生成" : "待写"}</small></button>)}</div></div><div className="card span-8"><div className="toolbar"><div><h2>{draft?.title || plan?.title || `第${chapterNo}章`}</h2><p className="subtitle">{plan?.goal || "先生成本章，或直接输入正文。"}</p></div><div className="toolbar-actions"><button className="button primary" onClick={onGenerate} disabled={!!busy}>{busy === "chapter" ? "正在写作…" : "AI 写本章"}</button><button className="button" onClick={onSave} disabled={!!busy || !draft}>{busy === "save" ? "保存中…" : "保存修改"}</button></div></div>{plan && <div className="notice">本章冲突：{plan.conflict || "未设置"}　结尾钩子：{plan.hook || "未设置"}</div>}<div className="field"><textarea className="chapter-content" value={draft?.content || ""} onChange={(event) => onChange({ chapter_no: chapterNo, title: draft?.title || plan?.title || `第${chapterNo}章`, content: event.target.value, status: draft?.status || "draft" })} placeholder="点击“AI 写本章”，或在这里输入正文…" /></div>{stateDiff && <StateDiffPanel extraction={stateDiff} busy={busy} onReview={onReview} />}{report && <div className="card" style={{ padding: 15, background: "#fbfaf6" }}><div className="toolbar"><h3>本章质检</h3><span className="score">{report.score}</span></div>{report.issues?.length ? report.issues.map((issue, index) => <div className="issue" key={`${issue.kind}-${index}`}><strong>{issue.severity === "high" ? "高" : issue.severity === "medium" ? "中" : "低"} · {issue.message}</strong><p>{issue.evidence || issue.suggestion}</p></div>) : <p className="muted">暂未发现明显问题，可以继续修改或确认。</p>}</div>}</div></div>;
}

function StateDiffPanel({ extraction, busy, onReview }: { extraction: StateExtraction; busy: string; onReview: (kind: "character" | "timeline" | "alias" | "foreshadow", id: string, action: "accept" | "reject") => void }) {
  const changeCount = extraction.characters?.length || 0;
  const eventCount = extraction.timeline_events?.length || 0;
  const aliasCount = extraction.aliases?.length || 0;
  const foreshadowCount = extraction.foreshadows?.length || 0;
  const reviewButtons = (kind: "character" | "timeline" | "alias" | "foreshadow", id: string, status: string) => status === "pending" ? <div className="toolbar-actions"><button className="button primary" onClick={() => onReview(kind, id, "accept")} disabled={!!busy}>接受</button><button className="button" onClick={() => onReview(kind, id, "reject")} disabled={!!busy}>拒绝</button></div> : <span className="tag">{status === "accepted" || status === "confirmed" ? "已接受" : status === "rejected" ? "已拒绝" : status}</span>;
  return <div className="card state-diff"><div className="toolbar"><div><h3>本章作品状态变化</h3><p className="subtitle">检测到 {changeCount} 项角色变化、{aliasCount} 个别名候选、{eventCount} 个时间线候选、{foreshadowCount} 个伏笔候选。</p></div><span className="tag">{extraction.status === "pending" ? "待审核" : extraction.status}</span></div><p className="muted">接受后才会写入正式作品状态；每一项都保留正文证据和章节版本。</p>{extraction.warning && <div className="notice">{extraction.warning}</div>}{changeCount > 0 && <><h3>角色状态 Diff</h3><div className="diff-list">{extraction.characters.map((change) => <div className="asset" key={change.id}><div className="toolbar"><strong>{change.character_name} · {change.field}</strong>{reviewButtons("character", change.id, change.status)}</div><p>{String(change.old_value ?? "未记录")} → {String(change.new_value ?? "未记录")}</p><p>证据：{change.evidence || "未提供"}　置信度：{Math.round(change.confidence * 100)}%</p></div>)}</div></>}{aliasCount > 0 && <><h3 style={{ marginTop: 16 }}>人物别名候选</h3><div className="diff-list">{extraction.aliases.map((alias) => <div className="asset" key={alias.id}><div className="toolbar"><strong>{alias.character_name} · {alias.alias}</strong>{reviewButtons("alias", alias.id, alias.status)}</div></div>)}</div></>}{eventCount > 0 && <><h3 style={{ marginTop: 16 }}>时间线候选</h3><div className="diff-list">{extraction.timeline_events.map((event) => <div className="asset" key={event.id}><div className="toolbar"><strong>{event.title}</strong>{reviewButtons("timeline", event.id, event.review_status)}</div><p>{event.description || "暂无描述"}</p><p>时间：{event.story_time_text || "待确认"}　地点：{event.location || "未提取"}</p><p>证据：{event.evidence || "未提供"}　置信度：{Math.round(event.confidence * 100)}%</p></div>)}</div></>}{foreshadowCount > 0 && <><h3 style={{ marginTop: 16 }}>伏笔候选</h3><div className="diff-list">{(extraction.foreshadows || []).map((item) => <div className="asset" key={item.id}><div className="toolbar"><strong>{item.clue}</strong>{reviewButtons("foreshadow", item.id, item.status)}</div><p>第{item.planted_chapter || "?"}章埋设 · 第{item.expected_reveal_chapter || "?"}章预计回收</p><p>证据：{item.evidence || "未提供"}　置信度：{Math.round(item.confidence * 100)}%</p></div>)}</div></>}{changeCount === 0 && aliasCount === 0 && eventCount === 0 && foreshadowCount === 0 && <p className="muted">本章没有提取到可审核的状态变化。</p>}</div>;
}
