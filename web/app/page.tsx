"use client";

import { useCallback, useEffect, useMemo, useState } from "react";

const API = process.env.NEXT_PUBLIC_API_BASE_URL || "http://localhost:8000/api";

type Work = {
  id: string; title: string; genre: string; target_audience: string; estimated_words: number;
  writing_style: string; premise: string; status: string; updated_at: string;
  story_bible?: StoryBible; characters?: Character[]; plot_arcs?: PlotArc[];
  chapter_plans?: ChapterPlan[]; chapters?: Chapter[]; foreshadows?: { id: string }[]; quality_reports?: QualityReport[];
};
type StoryBible = { summary: string; theme: string; world: string; ending: string; style_rules: string; locked?: number | boolean };
type Character = { id: string; name: string; role: string; goal: string; conflict: string; personality: string; background: string; status: string; knowledge: string };
type PlotArc = { id: string; title: string; synopsis: string; sequence: number };
type ChapterPlan = { chapter_no: number; title: string; goal: string; conflict: string; beats: string[]; hook: string };
type Chapter = { chapter_no: number; title: string; content: string; status: string };
type Issue = { kind: string; severity: string; message: string; evidence?: string; suggestion?: string };
type QualityReport = { chapter_no: number; score: number; issues: Issue[] };

async function api<T>(path: string, options?: RequestInit): Promise<T> {
  const response = await fetch(`${API}${path}`, { headers: { "Content-Type": "application/json" }, ...options });
  const data = await response.json();
  if (!response.ok) throw new Error(data.detail || "请求失败");
  return data as T;
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
  const [loading, setLoading] = useState(true);
  const [busy, setBusy] = useState("");
  const [error, setError] = useState("");
  const [chapterNo, setChapterNo] = useState(1);
  const [draft, setDraft] = useState<Chapter | null>(null);

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
    setChapterNo((current) => data.chapters?.some((item) => item.chapter_no === current) || data.chapter_plans?.some((item) => item.chapter_no === current) ? current : first);
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

  const selectedPlan = useMemo(() => work?.chapter_plans?.find((item) => item.chapter_no === chapterNo), [work, chapterNo]);
  const latestReport = useMemo(() => work?.quality_reports?.find((item) => item.chapter_no === chapterNo), [work, chapterNo]);

  async function createWork(form: HTMLFormElement) {
    const formData = new FormData(form);
    setBusy("create"); setError("");
    try {
      const created = await api<Work>("/works", { method: "POST", body: JSON.stringify({
        title: formData.get("title"), genre: formData.get("genre"), target_audience: formData.get("audience"),
        estimated_words: Number(formData.get("words") || 100000), writing_style: formData.get("style"), premise: formData.get("premise"),
      }) });
      form.reset(); await refreshWorks(); setSelectedId(created.id); setWork(created); setTab("overview");
    } catch (e) { setError((e as Error).message); } finally { setBusy(""); }
  }

  async function generate(kind: "setup" | "outline") {
    if (!work) return;
    setBusy(kind); setError("");
    try {
      const path = kind === "setup" ? `/works/${work.id}/generate/setup` : `/works/${work.id}/generate/outline`;
      const body = kind === "outline" ? JSON.stringify({ chapter_count: 12 }) : undefined;
      const data = await api<{ work: Work }>(path, { method: "POST", ...(body ? { body } : {}) });
      setWork(data.work); await refreshWorks(); setTab(kind === "setup" ? "assets" : "outline");
    } catch (e) { setError((e as Error).message); } finally { setBusy(""); }
  }

  async function generateChapter() {
    if (!work) return;
    setBusy("chapter"); setError("");
    try {
      const data = await api<{ work: Work; data: Chapter; quality: QualityReport }>(`/works/${work.id}/generate/chapter`, { method: "POST", body: JSON.stringify({ chapter_no: chapterNo, mode: "chapter" }) });
      setWork(data.work); setDraft(data.data); setTab("write");
    } catch (e) { setError((e as Error).message); } finally { setBusy(""); }
  }

  async function saveChapter() {
    if (!work || !draft) return;
    setBusy("save"); setError("");
    try {
      const data = await api<{ work: Work }>(`/works/${work.id}/chapters/${chapterNo}`, { method: "PATCH", body: JSON.stringify({ title: draft.title, content: draft.content, status: draft.status }) });
      setWork(data.work); setDraft(data.work.chapters?.find((item) => item.chapter_no === chapterNo) || draft);
    } catch (e) { setError((e as Error).message); } finally { setBusy(""); }
  }

  function selectChapter(no: number) {
    setChapterNo(no); setDraft(work?.chapters?.find((item) => item.chapter_no === no) || null);
  }

  if (loading) return <div className="loading">正在打开织梦台…</div>;

  return (
    <div className="shell">
      <aside className="sidebar">
        <p className="brand">织梦台</p>
        <p className="brand-note">AI 主写，作者把控，作品持续记忆</p>
        <div className="side-label">我的作品</div>
        <div className="work-list">
          {works.map((item) => <button key={item.id} className={`work-item ${item.id === selectedId ? "active" : ""}`} onClick={() => setSelectedId(item.id)}><strong>{item.title}</strong><span>{item.genre || "未设题材"} · {item.status === "writing" ? "写作中" : "草稿"}</span></button>)}
          {!works.length && <div className="empty-side">还没有作品。<br />从右侧创建第一本。</div>}
        </div>
        <div className="side-label">当前版本</div>
        <div className="empty-side">MVP 0.1<br />故事规划 · 章节写作 · 一致性检查</div>
      </aside>
      <main className="main">
        <div className="topbar">
          <div><div className="eyebrow">AI NOVEL STUDIO / MVP</div><h1>{work?.title || "开始你的第一部长篇"}</h1><p className="subtitle">先给 AI 一个方向，剩下的由故事规划、正文生成和作品状态共同推进。</p></div>
          {work && <div className="top-actions"><button className="button" onClick={() => downloadWork(work)}>导出 Markdown</button><button className="button primary" onClick={() => generate("setup")} disabled={!!busy}>{busy === "setup" ? "正在规划…" : "生成故事方案"}</button><button className="button dark" onClick={() => generate("outline")} disabled={!!busy}>{busy === "outline" ? "正在拆大纲…" : "生成 12 章大纲"}</button></div>}
        </div>
        {error && <div className="notice">{error}</div>}
        {!work ? <CreatePanel busy={busy} onCreate={createWork} /> : <>
          <div className="grid"><div className="card stat span-4"><span className="stat-label">预计篇幅</span><span className="stat-value">{(work.estimated_words / 10000).toFixed(1)}万字</span></div><div className="card stat span-4"><span className="stat-label">章节进度</span><span className="stat-value">{work.chapters?.length || 0} <small className="muted">/ {work.chapter_plans?.length || "—"}</small></span></div><div className="card stat span-4"><span className="stat-label">作品资产</span><span className="stat-value">{(work.characters?.length || 0) + (work.foreshadows?.length || 0)} <small className="muted">项</small></span></div></div>
          <div className="tabs">{[["overview", "总览"], ["outline", "章节大纲"], ["write", "写作台"], ["assets", "故事档案"]].map(([key, label]) => <button key={key} className={`tab ${tab === key ? "active" : ""}`} onClick={() => setTab(key)}>{label}</button>)}</div>
          {tab === "overview" && <Overview work={work} onTab={setTab} />}
          {tab === "outline" && <Outline work={work} />}
          {tab === "assets" && <Assets work={work} onSaved={() => loadWork(work.id)} />}
          {tab === "write" && <Writing work={work} chapterNo={chapterNo} draft={draft} plan={selectedPlan} report={latestReport} busy={busy} onSelect={selectChapter} onGenerate={generateChapter} onSave={saveChapter} onChange={setDraft} />}
        </>}
      </main>
    </div>
  );
}

function CreatePanel({ busy, onCreate }: { busy: string; onCreate: (form: HTMLFormElement) => void }) {
  return <div className="grid"><div className="card span-7"><h2>创建一部作品</h2><p className="subtitle">先填写最少的信息，故事资产由 AI 继续补齐。</p><form onSubmit={(event) => { event.preventDefault(); onCreate(event.currentTarget); }}><div className="field"><label>作品名</label><input name="title" required placeholder="例如：潮汐之后" /></div><div className="two-col"><div className="field"><label>题材</label><input name="genre" placeholder="都市、悬疑、科幻…" /></div><div className="field"><label>目标读者/平台</label><input name="audience" placeholder="女频 / 长篇连载" /></div></div><div className="two-col"><div className="field"><label>预计字数</label><input name="words" type="number" defaultValue="100000" min="0" /></div><div className="field"><label>文风</label><input name="style" placeholder="克制、快节奏、对白多" /></div></div><div className="field"><label>一句话设想（可选）</label><textarea name="premise" placeholder="一个人必须在……之前……" /></div><button className="button primary" disabled={!!busy}>{busy === "create" ? "正在创建…" : "创建作品"}</button></form></div><div className="card span-5"><h3>第一版会帮你完成</h3><div className="asset-list"><div className="asset"><strong>故事方案</strong><p>梗概、主题、世界观、主线冲突和主要人物。</p></div><div className="asset"><strong>章节大纲</strong><p>按卷和章节拆出目标、冲突、节奏和结尾钩子。</p></div><div className="asset"><strong>正文生成</strong><p>按章节计划写作，支持保存、重写和继续下一章。</p></div><div className="asset"><strong>一致性检查</strong><p>检查人物、时间线、伏笔和重复内容。</p></div></div></div></div>;
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

function Writing({ work, chapterNo, draft, plan, report, busy, onSelect, onGenerate, onSave, onChange }: { work: Work; chapterNo: number; draft: Chapter | null; plan?: ChapterPlan; report?: QualityReport; busy: string; onSelect: (no: number) => void; onGenerate: () => void; onSave: () => void; onChange: (chapter: Chapter | null) => void }) {
  const items = work.chapter_plans?.length ? work.chapter_plans : [{ chapter_no: 1, title: "第1章", goal: "", conflict: "", beats: [], hook: "" }];
  return <div className="grid"><div className="card span-4"><div className="toolbar"><div><h3>章节</h3><span className="muted">选择要生成或修改的章节</span></div><span className="tag">{work.chapters?.length || 0} 已写</span></div><div className="chapter-list">{items.map((item) => <button key={item.chapter_no} className={`chapter-row ${item.chapter_no === chapterNo ? "active" : ""}`} onClick={() => onSelect(item.chapter_no)}><span>第{item.chapter_no}章 {item.title?.replace(/^第\d+章\s*/, "")}</span><small>{work.chapters?.some((chapterItem) => chapterItem.chapter_no === item.chapter_no) ? "已生成" : "待写"}</small></button>)}</div></div><div className="card span-8"><div className="toolbar"><div><h2>{draft?.title || plan?.title || `第${chapterNo}章`}</h2><p className="subtitle">{plan?.goal || "先生成本章，或直接输入正文。"}</p></div><div className="toolbar-actions"><button className="button primary" onClick={onGenerate} disabled={!!busy}>{busy === "chapter" ? "正在写作…" : "AI 写本章"}</button><button className="button" onClick={onSave} disabled={!!busy || !draft}>{busy === "save" ? "保存中…" : "保存修改"}</button></div></div>{plan && <div className="notice">本章冲突：{plan.conflict || "未设置"}　结尾钩子：{plan.hook || "未设置"}</div>}<div className="field"><textarea className="chapter-content" value={draft?.content || ""} onChange={(event) => onChange({ chapter_no: chapterNo, title: draft?.title || plan?.title || `第${chapterNo}章`, content: event.target.value, status: draft?.status || "draft" })} placeholder="点击“AI 写本章”，或在这里输入正文…" /></div>{report && <div className="card" style={{ padding: 15, background: "#fbfaf6" }}><div className="toolbar"><h3>本章质检</h3><span className="score">{report.score}</span></div>{report.issues?.length ? report.issues.map((issue, index) => <div className="issue" key={`${issue.kind}-${index}`}><strong>{issue.severity === "high" ? "高" : issue.severity === "medium" ? "中" : "低"} · {issue.message}</strong><p>{issue.evidence || issue.suggestion}</p></div>) : <p className="muted">暂未发现明显问题，可以继续修改或确认。</p>}</div>}</div></div>;
}
