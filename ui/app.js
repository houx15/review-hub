import {
  html, render, useState, useEffect, useMemo, useRef, useCallback, useLayoutEffect,
} from "./vendor/preact-htm.module.js";
import { parseBlocks, runPieces, resolveAnchor } from "./md.js";

// ---------------------------------------------------------------- labels

const SEV_ZH = { S1: "影响主要结论或研究设计的有效性", S2: "影响某一具体论断", S3: "表述问题" };
const ORIGIN_ZH = { introduced: "文本本身的问题", source_material: "源自材料或数据", inherited: "源自上游的研究决策" };
const REACTIONS = [
  { reaction: "fixed", fix_by: "ai", label: "同意，请 AI 修改", hint: "交给 review-apply 修改" },
  { reaction: "fixed", fix_by: "human", label: "同意，我来修改" },
  { reaction: "class_of_problem", fix_by: "ai", label: "同类问题", hint: "同意，并请 AI 找出同类问题" },
  { reaction: "ai_wrong", label: "不成立" },
  { reaction: "intentional", label: "有意为之" },
  { reaction: "doesnt_matter", label: "无关紧要", hint: "成立，但不必修改" },
];
const REACTION_ZH = {
  fixed: "已接受", ai_wrong: "不成立", intentional: "有意为之", doesnt_matter: "无关紧要", class_of_problem: "同类问题",
};
const SOURCE_ZH = {
  computed: "由数据计算得出", literature: "引自文献", user_stated: "作者提供",
  model_inference: "作者的推论", unsourced: "缺少出处",
};
const CHECK_ZH = { verified: "已核实", has_finding: "存在问题", unchecked: "未核实" };
const DSTATUS_ZH = {
  ai_default: "沿用原做法", awaiting_human: "待定", confirmed: "已确认", changed: "已更改", invalidated: "上游决策已更改，需重新考虑",
};
const ACTION_ZH = { confirm: "待确认", choose: "可选方案", answer: "开放问题", predict: "开放问题", none: "" };
const STAGE_COLORS = ["#7c6fd6", "#3f8fd2", "#2aa198", "#5fa23a", "#b8941e", "#d9822b", "#d1495b",
  "#b04fa8", "#6b7fa8", "#4a9d8f", "#8a6d3b"];

// ---------------------------------------------------------------- api

async function api(path, body) {
  const opt = body === undefined ? {} : {
    method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body),
  };
  const r = await fetch(path, opt);
  const j = await r.json().catch(() => ({ error: `HTTP ${r.status}` }));
  if (!r.ok) throw new Error(j.error || `HTTP ${r.status}`);
  return j;
}

// what the person opens and for how long: saved locally with the review, so it can improve future reviews
// without asking them to do anything; never shown, never sent anywhere else
const activity = { sid: null, queue: [] };
function track(kind, id) {
  if (!activity.sid || !id || !/^[A-Z]\d{1,5}$/.test(id)) return;
  activity.queue.push({ kind, id, t: Date.now() });
  if (activity.queue.length >= 50) flushActivity();
}
function flushActivity() {
  if (!activity.sid || !activity.queue.length) return;
  const events = activity.queue.splice(0);
  fetch(`/api/s/${encodeURIComponent(activity.sid)}/activity`, {
    method: "POST", keepalive: true, headers: { "Content-Type": "application/json" }, body: JSON.stringify({ events }),
  }).catch(() => {});
}
setInterval(flushActivity, 10000);
addEventListener("pagehide", flushActivity);
document.addEventListener("visibilitychange", () => { if (document.visibilityState === "hidden") flushActivity(); });

function useHashRoute() {
  const [hash, setHash] = useState(location.hash);
  useEffect(() => {
    const f = () => setHash(location.hash);
    addEventListener("hashchange", f);
    return () => removeEventListener("hashchange", f);
  }, []);
  const m = hash.match(/^#\/s\/([^/?#]+)/);
  return m ? { page: "session", sid: decodeURIComponent(m[1]) } : { page: "home" };
}

function store(key, val) {
  try {
    if (val === undefined) return JSON.parse(localStorage.getItem(key) || "null");
    localStorage.setItem(key, JSON.stringify(val));
  } catch (e) { /* storage may be unavailable */ }
  return null;
}

function shortPath(p, st) {
  if (!p) return p;
  for (const base of [st.path, st.project_root]) {
    if (base && p.startsWith(base + "/")) return p.slice(base.length + 1);
  }
  return st.home && p.startsWith(st.home + "/") ? "~" + p.slice(st.home.length) : p;
}

function fmtTime(iso) {
  if (!iso) return "";
  const d = new Date(iso);
  return `${d.getMonth() + 1}月${d.getDate()}日 ${String(d.getHours()).padStart(2, "0")}:${String(d.getMinutes()).padStart(2, "0")}`;
}

// ---------------------------------------------------------------- derived model

function derive(st) {
  const plan = st.plan;
  const mp = st.map;
  const allStages = plan ? plan.stages : [];
  const stages = allStages.filter((s) => s.included);
  const stageIdx = Object.fromEntries(allStages.map((s, i) => [s.id, i]));
  const aspectName = Object.fromEntries(((plan && plan.aspects) || []).map((a) => [a.id, a.name]));
  const stageName = Object.fromEntries(allStages.map((s) =>
    [s.id, aspectName[s.aspect] ? `${aspectName[s.aspect]} · ${s.name}` : s.name]));
  const stageColor = Object.fromEntries(allStages.map((s, i) => [s.id, STAGE_COLORS[i % STAGE_COLORS.length]]));
  const mapHash = mp ? mp.artifact_hash : null;

  // latest feedback per target and kind; decision/claim feedback only counts for the current map
  const last = {};
  for (const r of st.feedback) {
    if (!r.target) continue;
    const t = r.target.type;
    if ((t === "decision" || t === "claim") && r.artifact_hash !== mapHash) continue;
    (last[r.target.id] = last[r.target.id] || {})[r.kind] = r;
  }
  const signoffs = st.feedback.filter((r) => r.kind === "signoff");

  const findings = st.findings.filter((f) => !["superseded", "invalidated"].includes(f.status));
  const decisions = mp ? mp.decisions : [];
  const claims = mp ? mp.claims : [];
  const items = {};
  for (const f of findings) items[f.id] = { kind: "finding", obj: f };
  for (const d of decisions) items[d.id] = { kind: "decision", obj: d };
  for (const c of claims) items[c.id] = { kind: "claim", obj: c };
  const leads = (mp && mp.leads) || [];
  for (const l of leads) items[l.id] = { kind: "lead", obj: l };
  const leadAnswered = (l) => !!(last[l.id] && last[l.id].answer);
  const changes = (st.changes || []).filter((c) => c.status !== "reverted");
  for (const c of changes) items[c.id] = { kind: "change", obj: c };

  // discussion: messages by target id ("" = the artifact as a whole); a human message is pending until the AI replies
  const threads = {};
  const replied = new Set((st.threads || []).filter((m) => m.author === "ai" && m.reply_to).map((m) => m.reply_to));
  for (const m of st.threads || []) {
    const k = m.target ? m.target.id : "";
    (threads[k] = threads[k] || []).push(m);
  }
  const pending = (st.threads || []).filter((m) => m.author === "human" && !replied.has(m.id));

  const findingOpen = (f) => f.status === "open" && !(last[f.id] && last[f.id].reaction);
  const decisionHandled = (d) => {
    const l = last[d.id] || {};
    return !!(l.choice || l.answer || l.prediction || l.confirm || l.reaction);
  };
  const awaiting = decisions.filter((d) => ["awaiting_human", "invalidated"].includes(d.status));
  const preds = [];
  const openFindings = findings.filter(findingOpen);
  const openIds = new Set(openFindings.map((f) => f.id));
  const byStage = (a, b) => (stageIdx[a.stage] ?? 99) - (stageIdx[b.stage] ?? 99);
  const sevThenStage = (a, b) => a.severity.localeCompare(b.severity) || byStage(a, b) || a.id.localeCompare(b.id);

  const children = {};
  const roots = [];
  for (const f of openFindings.slice().sort(sevThenStage)) {
    if (f.caused_by && openIds.has(f.caused_by)) (children[f.caused_by] = children[f.caused_by] || []).push(f);
    else roots.push(f);
  }
  const queue = {
    predict: preds,
    decide: awaiting.filter((d) => !decisionHandled(d))
      .sort((a, b) => byStage(a, b) || b.load_bearing.score - a.load_bearing.score),
    major: roots.filter((f) => f.severity !== "S3"),
    premises: claims.filter((c) => c.source_type === "unsourced" && !(last[c.id] && last[c.id].confirm)),
    leads: leads.filter((l) => !leadAnswered(l)),
    minor: roots.filter((f) => f.severity === "S3"),
    done: [
      ...findings.filter((f) => !openIds.has(f.id)).sort(sevThenStage),
      ...decisions.filter((d) => decisionHandled(d) && d.human_action !== "none"),
      ...claims.filter((c) => last[c.id] && last[c.id].confirm),
      ...leads.filter(leadAnswered),
    ],
    aiDefault: decisions.filter((d) => d.status === "ai_default"),
    changes: changes.filter((c) => !(last[c.id] && last[c.id].confirm)),
    changesDone: changes.filter((c) => last[c.id] && last[c.id].confirm),
  };
  const counts = {
    S1: openFindings.filter((f) => f.severity === "S1").length,
    S2: openFindings.filter((f) => f.severity === "S2").length,
    S3: openFindings.filter((f) => f.severity === "S3").length,
    roots: roots.filter((f) => f.severity !== "S3").length,
  };

  // documents and anchors
  const files = st.artifact.map((a) => ({
    ...a, markdown: /\.(md|markdown)$/i.test(a.path), blocks: parseBlocks(a.text, /\.(md|markdown)$/i.test(a.path)),
  }));
  const ranges = {};
  for (const [id, it] of Object.entries(items)) {
    const an = it.obj.anchor;
    const file = an && files.find((x) => x.path === an.path);
    const r = file && resolveAnchor(file.text, an);
    if (r) ranges[id] = { path: an.path, ...r };
  }
  const needs = new Set([
    ...queue.predict.map((d) => d.id), ...queue.decide.map((d) => d.id), ...openFindings.map((f) => f.id),
    ...queue.premises.map((c) => c.id), ...queue.leads.map((l) => l.id),
  ]);
  // attention zones: stretches of the text where the person has to answer, decide or discuss something
  const zones = [];
  const zone = (id, kind, label, question, extra) => {
    const r = ranges[id];
    if (r) zones.push({ id, kind, label, question, extra, ...r });
  };
  for (const d of queue.decide) {
    const chosen = d.options.find((o) => o.chosen);
    zone(d.id, "suggest", noteLabel("suggest", "研究决策"), d.prompt_to_human || d.question,
      d.human_action === "choose" && chosen ? `目前的做法：${chosen.label}` : "");
  }
  for (const f of roots.filter((x) => x.severity !== "S3")) {
    const k = f.note === "verify" ? "verify" : "suggest";
    zone(f.id, k, noteLabel(k, f.severity === "S1" ? "重大" : ""), f.title, f.handoff_reason || "");
  }
  for (const c of queue.premises) zone(c.id, "verify", noteLabel("verify", "缺少出处"), c.text);
  for (const l of queue.leads) zone(l.id, "explore", noteLabel("explore", LEAD_ZH[l.kind]), l.title, l.questions[0]);
  for (const [tid, ms] of Object.entries(threads)) {
    if (!tid || zones.some((z) => z.id === tid)) continue;
    const lastMsg = ms[ms.length - 1];
    const waiting = ms.some((m) => m.author === "human" && !replied.has(m.id));
    zone(tid, "talk", noteLabel("talk", waiting ? "等待 AI 回复" : ""), lastMsg.text, `${ms.length} 条消息`);
  }

  let step = "sign";
  if (preds.length) step = "predict";
  else if (queue.decide.length) step = "decide";
  else if (queue.major.length || queue.premises.length || queue.leads.length) step = "check";

  const judged = Object.fromEntries(((mp && mp.assessments) || []).map((a) => [a.aspect, a]));
  return {
    judged, plan, mp, stages, stageIdx, stageName, stageColor, last, signoffs, findings, decisions, claims, items,
    findingOpen, decisionHandled, preds, queue, counts, children, files, ranges, needs, step, openFindings,
    changes, threads, pending, replied, baseline: baselineFiles(st), zones,
  };
}

// the version the session started from, when the AI has edited the copy since
function baselineFiles(st) {
  const base = st.baseline || [];
  const changed = base.some((b) => {
    const cur = st.artifact.find((a) => a.path === b.path);
    return cur && cur.text !== b.text;
  });
  if (!changed) return null;
  return base.map((b) => ({ ...b, markdown: /\.(md|markdown)$/i.test(b.path), blocks: parseBlocks(b.text, /\.(md|markdown)$/i.test(b.path)) }));
}

// ---------------------------------------------------------------- app

function App() {
  const route = useHashRoute();
  return route.page === "session" ? html`<${Review} sid=${route.sid} key=${route.sid} />` : html`<${Home} />`;
}

function Home() {
  const [data, setData] = useState(null);
  const [err, setErr] = useState(null);
  useEffect(() => {
    document.title = "审阅";
    api("/api/sessions").then(setData, (e) => setErr(e.message));
  }, []);
  if (err) return html`<div class="page-msg error">${err}</div>`;
  if (!data) return html`<div class="page-msg">读取中…</div>`;
  return html`
    <div class="home">
      <header class="home-head">
        <h1>审阅</h1>
        <p class="muted">工作区：<code>${data.workspace}</code></p>
      </header>
      ${data.sessions.length === 0 && html`<p class="muted">还没有审阅。可在 agent 中用 review-start 开始。</p>`}
      <ul class="session-list">
        ${data.sessions.map((s) => html`
          <li><a href=${"#/s/" + encodeURIComponent(s.id)} class="session-card">
            <div class="sc-title">${s.title}</div>
            <div class="sc-meta muted">${s.id}${s.tier ? ` · ${s.tier} 档` : ""}</div>
            <div class="sc-status">${sessionStatus(s)}</div>
          </a></li>`)}
      </ul>
    </div>`;
}

function sessionStatus(s) {
  if (s.error) return html`<span class="chip bad">文件格式有误</span>`;
  if (s.signoff === "deliver" && s.signed_current) return html`<span class="chip ok">已定稿</span>`;
  if (s.status === "no_plan") return html`<span class="chip">尚无审阅计划</span>`;
  if (s.status === "draft") return html`<span class="chip">计划待确认</span>`;
  if (s.status === "confirmed") return html`<span class="chip">计划已确认，审阅尚未开始</span>`;
  const o = s.open || {};
  const parts = [];
  if (o.S1) parts.push(html`<span class="sev s1">S1 ${o.S1}</span>`);
  if (o.S2) parts.push(html`<span class="sev s2">S2 ${o.S2}</span>`);
  if (s.awaiting) parts.push(html`<span class="chip suggest">待定 ${s.awaiting}</span>`);
  return parts.length ? parts : html`<span class="chip ok">没有待处理的问题</span>`;
}

function Review({ sid }) {
  const [st, setSt] = useState(null);
  const [err, setErr] = useState(null);
  const [sel, setSel] = useState(null);
  useEffect(() => { activity.sid = sid; return () => { flushActivity(); activity.sid = null; }; }, [sid]);
  const prevSel = useRef(null);
  useEffect(() => {
    if (prevSel.current) track("close", prevSel.current);
    if (sel) track("open", sel);
    prevSel.current = sel;
  }, [sel]);
  const [tab, setTab] = useState("queue");
  const [fileView, setFileView] = useState(null);
  const [stageFilter, setStageFilter] = useState(null);
  const [colorStages, setColorStages] = useState(() => store("color-stages") !== false);
  const [toast, setToast] = useState(null);
  const [view, setView] = useState("current");
  const [focus, setFocus] = useState(false);
  const docRef = useRef(null);
  const sideRef = useRef(null);
  const version = useRef(null);

  const load = useCallback(async () => {
    try {
      const s = await api(`/api/s/${encodeURIComponent(sid)}/state`);
      version.current = s.version;
      setSt(s);
      setErr(null);
    } catch (e) { setErr(e.message); }
  }, [sid]);

  useEffect(() => { load(); }, [load]);
  useEffect(() => {
    const t = setInterval(async () => {
      if (document.visibilityState !== "visible") return;
      try {
        const v = await api(`/api/s/${encodeURIComponent(sid)}/version`);
        if (v.version !== version.current) load();
      } catch (e) { /* server stopped; keep the page */ }
    }, 2000);
    return () => clearInterval(t);
  }, [sid, load]);

  const M = useMemo(() => (st ? derive(st) : null), [st]);
  const gate = false;  // nothing on the page is held back until the person does something
  const selStage = sel && M && M.items[sel] ? M.items[sel].obj.stage : null;
  useHumanTimer(sid, selStage || stageFilter);
  useEffect(() => { if (st && st.plan) document.title = `审阅 · ${st.plan.artifact.title}`; }, [st]);

  const flash = (msg, bad) => { setToast({ msg, bad }); setTimeout(() => setToast(null), bad ? 6000 : 2500); };
  const send = useCallback(async (body) => {
    try {
      const r = await api(`/api/s/${encodeURIComponent(sid)}/feedback`, body);
      await load();
      flash(`已记录（${r.record.id}）`);
      return r.record;
    } catch (e) { flash(e.message, true); return null; }
  }, [sid, load]);

  const say = useCallback(async (body, done) => {
    try {
      const r = await api(`/api/s/${encodeURIComponent(sid)}/message`, body);
      await load();
      flash(done || "已发送，AI 会在这里回复");
      return r.record;
    } catch (e) { flash(e.message, true); return null; }
  }, [sid, load]);

  const select = useCallback((id, from) => {
    setSel(id);
    setTab("queue");
    requestAnimationFrame(() => {
      if (from !== "doc" && docRef.current) {
        const el = docRef.current.querySelector(`[data-ids~="${id}"]`);
        if (el) {
          el.scrollIntoView({ behavior: "smooth", block: "center" });
          el.classList.remove("pulse");
          void el.offsetWidth;
          el.classList.add("pulse");
        }
      }
      if (from !== "side" && sideRef.current) {
        const card = sideRef.current.querySelector(`[data-card="${id}"]`);
        if (card) card.scrollIntoView({ behavior: "smooth", block: "nearest" });
      }
    });
  }, []);

  if (err && !st) return html`<div class="page-msg error">${err}<p><a href="#/">返回审阅列表</a></p></div>`;
  if (!st) return html`<div class="page-msg">读取中…</div>`;
  if (!st.plan || !st.map) return html`<${NotReviewed} st=${st} />`;

  const ctx = {
    st, M, sel, select, send, say, gate, stageFilter, colorStages, openFile: setFileView, flash, view, setView, focus, setFocus,
  };
  return html`
    <div class="review ${colorStages ? "colored" : ""} ${focus ? "focus" : ""}">
      <${TopBar} ctx=${ctx} tab=${tab} setTab=${setTab} />
      <aside class="pane map-pane">
        <${MapPane} ctx=${ctx} setStageFilter=${setStageFilter}
          colorStages=${colorStages} setColorStages=${(v) => { setColorStages(v); store("color-stages", v); }} />
      </aside>
      <main class="pane doc-pane" ref=${docRef}>
        <${Orientation} ctx=${ctx} />
        ${view === "baseline" && M.baseline
          ? M.baseline.map((f) => html`<${PlainDoc} file=${f} ctx=${ctx} key=${"b:" + f.path} />`)
          : M.files.map((f) => html`<${DocFile} file=${f} ctx=${ctx} key=${f.path} />`)}
        <${Minimap} ctx=${ctx} docRef=${docRef} />
      </main>
      <aside class="pane side-pane" ref=${sideRef}>
        ${tab === "queue" && html`<${Queue} ctx=${ctx} />`}
        ${tab === "signoff" && html`<${Signoff} ctx=${ctx} />`}
        ${tab === "cert" && html`<${Process} ctx=${ctx} />`}
        ${tab === "talk" && html`<${Talk} ctx=${ctx} />`}
      </aside>
      ${fileView && html`<${FileViewer} sid=${sid} ev=${fileView} st=${st} onClose=${() => setFileView(null)} />`}
      ${toast && html`<div class="toast ${toast.bad ? "bad" : ""}" role="status">${toast.msg}</div>`}
    </div>`;
}

function NotReviewed({ st }) {
  const p = st.plan;
  return html`
    <div class="page-msg">
      <p><a href="#/">← 审阅列表</a></p>
      <h2>${p ? p.artifact.title : st.id}</h2>
      <p>${!p ? "这次审阅还没有计划。" : p.status === "draft"
        ? "审阅计划尚未确认，请在 agent 对话中确认（review-start）。"
        : "计划已确认，尚无审阅结果。请在 agent 中运行 review-run。"}</p>
      <p class="muted">结果写入后，页面会自动更新。</p>
    </div>`;
}

// ---------------------------------------------------------------- timer

function useHumanTimer(sid, stage) {
  const lastAct = useRef(Date.now());
  const stageRef = useRef(stage);
  stageRef.current = stage;
  useEffect(() => {
    const act = () => { lastAct.current = Date.now(); };
    const evs = ["mousemove", "keydown", "scroll", "click", "wheel"];
    evs.forEach((e) => addEventListener(e, act, { passive: true, capture: true }));
    const TICK = 30000;
    const t = setInterval(() => {
      const active = document.visibilityState === "visible" && Date.now() - lastAct.current < 120000;
      if (!active) return;
      api(`/api/s/${encodeURIComponent(sid)}/time`, { minutes: TICK / 60000, stage: stageRef.current || null })
        .catch(() => {});
    }, TICK);
    return () => { clearInterval(t); evs.forEach((e) => removeEventListener(e, act, { capture: true })); };
  }, [sid]);
}

// ---------------------------------------------------------------- top bar

function CountLine({ M }) {
  const { S1, S2, S3 } = M.counts;
  if (!S1 && !S2) {
    return html`<span class="count ok">没有待处理的重大问题${S3 ? html`<span class="muted">（表述问题 ${S3} 条）</span>` : ""}</span>`;
  }
  const merged = M.counts.roots < S1 + S2;
  return html`<span class="count">
    ${S1 ? html`<span class="sev s1">重大问题 ${S1}</span>` : ""}
    ${S2 ? html`<span class="sev s2">一般问题 ${S2}</span>` : ""}
    ${merged ? html`<span class="muted">按根源合并为 ${M.counts.roots} 个</span>` : ""}
    ${S3 ? html`<span class="muted">· 表述 ${S3}</span>` : ""}
  </span>`;
}

function TopBar({ ctx, tab, setTab }) {
  const { st, M } = ctx;
  const m = st.metrics;
  const rounds = m ? m.ai.rounds : [];
  const lastRound = rounds[rounds.length - 1];
  const nTodo = M.queue.predict.length + M.queue.decide.length + M.queue.major.length + M.queue.premises.length;
  return html`
    <header class="topbar">
      <a href="#/" class="back" title="审阅列表">←</a>
      <div class="tb-title">
        <div class="tb-name">${st.plan.artifact.title}</div>
        <div class="tb-meta muted">
          ${lastRound ? `AI 审阅 ${lastRound.round} 轮` : "审阅尚未开始"}
          ${M.changes.length ? ` · 修改 ${M.changes.length} 处` : ""}
          ${m && m.human && m.human.minutes ? ` · 你已阅读 ${Math.round(m.human.minutes)} 分钟` : ""}
          ${st.status && st.status.round_open ? html` · <span class="chip warn">第 ${st.status.round_open} 轮进行中</span>` : ""}
          ${st.status && st.status.loop === "revise" ? html` · <span class="chip warn">AI 审阅进行中</span>` : ""}
        </div>
      </div>
      <${CountLine} M=${M} />
      <nav class="tabs" role="tablist">
        <button role="tab" class=${tab === "queue" ? "on" : ""} onClick=${() => setTab("queue")}>
          审阅意见${nTodo ? html` <b>${nTodo}</b>` : ""}</button>
        <button role="tab" class=${tab === "talk" ? "on" : ""} onClick=${() => setTab("talk")}>
          讨论${M.pending.length ? html` <b>${M.pending.length}</b>` : ""}</button>
        <button role="tab" class=${tab === "signoff" ? "on" : ""} onClick=${() => setTab("signoff")}>完成审阅</button>
        <button role="tab" class=${tab === "cert" ? "on" : ""} onClick=${() => setTab("cert")}>审阅过程</button>
      </nav>
    </header>`;
}

// ---------------------------------------------------------------- map

function MapPane({ ctx, setStageFilter, colorStages, setColorStages }) {
  const { M, sel, select, stageFilter, gate } = ctx;
  return html`
    <div class="map">
      <div class="pane-head">
        <h2>审阅框架</h2>
        <label class="toggle"><input type="checkbox" checked=${colorStages}
          onChange=${(e) => setColorStages(e.target.checked)} /> 按环节着色</label>
      </div>
      <ol class="stage-list">
        ${orderedStages(M.plan).map(({ s, asp }) => {
          const fs = M.openFindings.filter((f) => f.stage === s.id);
          const ds = M.decisions.filter((d) => d.stage === s.id);
          const cs = M.claims.filter((c) => c.stage === s.id);
          const on = stageFilter === s.id;
          return html`
            ${asp && html`<li class="aspect-head" title=${[asp.ask, asp.covers ? "范围：" + asp.covers : "", ...(asp.criteria || []).map((k) => "· " + k)].filter(Boolean).join("\n")}>
              <span class="anum">${asp.n}</span>${asp.name}
              ${!gate && M.judged[asp.id] && html`<span class="rating ${M.judged[asp.id].rating}">${RATING_ZH[M.judged[asp.id].rating]}</span>`}</li>
              ${!gate && M.judged[asp.id] && html`<li class="aspect-verdict" title=${M.judged[asp.id].reasons.map((r) => "· " + r).join("\n")}>${M.judged[asp.id].verdict}</li>`}`}
            <li class="stage ${s.included ? "" : "excluded"} ${on ? "on" : ""}" style=${`--stage:${M.stageColor[s.id]}`}>
              <button class="stage-head" onClick=${() => setStageFilter(on ? null : s.id)}
                title=${s.included ? "只显示这一环节" : s.excluded_reason || "本次未纳入"}>
                <span class="dot"></span>
                <span class="stage-name">${s.name}</span>
                <span class="vj" title=${VJ_TITLE[s.verifiability]}>${VJ_ZH[s.verifiability]}</span>
                ${fs.some((f) => f.severity === "S1") ? html`<span class="sev s1 mini">重大</span>` : ""}
              </button>
              ${!s.included && html`<div class="stage-note muted">未纳入：${s.excluded_reason || ""}</div>`}
              ${s.included && (ds.length || cs.length) ? html`
                <ul class="nodes">
                  ${ds.map((d) => html`
                    <li><button class="node ${sel === d.id ? "sel" : ""} ${M.needs.has(d.id) ? "needs" : ""}"
                      onClick=${() => select(d.id)}>
                      <span class="nid">${d.id}</span> ${d.title}
                      <span class="nstat">${M.needs.has(d.id) ? ACTION_ZH[d.human_action] : DSTATUS_ZH[d.status]}</span>
                    </button></li>`)}
                  ${cs.length ? html`<li class="claim-tally muted">${claimTally(cs, gate)}</li>` : ""}
                </ul>` : ""}
            </li>`;
        })}
      </ol>
    </div>`;
}

const RATING_ZH = { strong: "扎实", adequate: "基本成立", weak: "存在明显问题",
  clear: "想清楚了", partial: "有方向，未想透", open: "尚未涉及" };
const VJ_ZH = { V: "可核实", J: "需判断", "J+V": "核实与判断" };
const VJ_TITLE = {
  V: "有可对照的依据（代码、数据、结果、原文），AI 已核实",
  J: "没有标准答案，AI 给出意见和理由，供你判断",
  "J+V": "部分可核实，部分需要判断",
};

// stages grouped under their aspects (in the aspects' order); the first stage of each group carries its header
function orderedStages(plan) {
  const as = plan.aspects || [];
  if (!as.length) return plan.stages.map((s) => ({ s, asp: null }));
  const out = [];
  as.forEach((a, k) => plan.stages.filter((s) => s.aspect === a.id)
    .forEach((s, i) => out.push({ s, asp: i === 0 ? { ...a, n: k + 1 } : null })));
  const known = new Set(as.map((a) => a.id));
  plan.stages.filter((s) => !known.has(s.aspect)).forEach((s) => out.push({ s, asp: null }));
  return out;
}

function claimTally(cs, gate) {
  const n = (k) => cs.filter((c) => c.check_status === k).length;
  if (gate) return `论断 ${cs.length} 条`;
  const parts = [["verified", "已核实"], ["has_finding", "存疑"], ["unchecked", "未核实"]]
    .filter(([k]) => n(k)).map(([k, w]) => `${w} ${n(k)}`);
  return `关键论断 ${cs.length} 条：${parts.join("，")}`;
}

// ---------------------------------------------------------------- orientation

function Orientation({ ctx }) {
  const { M, st, gate } = ctx;
  const p = M.plan;
  const [open, setOpen] = useState(false);
  return html`
    <section class="orient">
      ${M.mp.overview ? html`<${Overview} ov=${M.mp.overview} ctx=${ctx} />`
        : html`<p class="orient-lead"><span class="muted">主要结论：</span>${M.mp.summary}</p>`}
      ${!gate && M.zones.length ? html`<div class="zonebar">
        ${M.changes.length ? html`<span class="zchip edit">${noteLabel("edit")} ${M.changes.length}</span>` : ""}
        ${["suggest", "verify", "explore", "talk"].map((k) => {
          const n = M.zones.filter((z) => z.kind === k).length;
          return n ? html`<span class="zchip ${k}">${noteLabel(k)} ${n}</span>` : "";
        })}
        <label class="toggle"><input type="checkbox" checked=${ctx.focus} onChange=${(e) => ctx.setFocus(e.target.checked)} /> 只看这些段落</label>
      </div>` : ""}
      ${M.baseline && html`<div class="viewtoggle" role="radiogroup" aria-label="版本">
        <button class=${ctx.view === "current" ? "on" : ""} onClick=${() => ctx.setView("current")}>修改稿</button>
        <button class=${ctx.view === "baseline" ? "on" : ""} onClick=${() => ctx.setView("baseline")}>原稿</button>
        <span class="muted small">绿色标出的 ${M.changes.length} 处为 AI 的修改，点击可查看修改前后</span></div>`}
      ${p.direction_check && !M.mp.overview && html`<p class="muted small">研究问题：${p.direction_check.question}
        ${p.direction_check.answer ? html` — <b>${p.direction_check.answer}</b>` : ""}</p>`}
      ${p.not_checked.length ? html`
        <div class="notchecked">
          <button class="linkish" onClick=${() => setOpen(!open)} aria-expanded=${open}>
            本次未核查 ${p.not_checked.length} 项 ${open ? "▴" : "▾"}</button>
          ${open && html`<ul>${p.not_checked.map((n) => html`<li><b>${n.what}</b>：${n.why}</li>`)}</ul>`}
        </div>` : ""}
    </section>`;
}

// the review's top layer: the paper's architecture, and the questions that decide whether it stands
function Overview({ ov, ctx }) {
  const [open, setOpen] = useState(true);
  const refs = (ids) => (ids || []).filter((id) => ctx.M.items[id]).map((id) => {
    const k = noteKind(ctx.M.items[id]);
    return html`<button class="ref ${k || ""}" onClick=${() => { track("ref", id); ctx.select(id); }} title=${k ? NOTE[k].name : ""}>${id}</button>`;
  });
  return html`<div class="overview">
    <button class="ov-head" aria-expanded=${open} onClick=${() => setOpen(!open)}>
      <span>概览</span><span class="muted small">${open ? "收起" : "展开"}</span></button>
    ${open && html`
      <dl class="ov-parts">${ov.parts.map((pt) => html`
        <dt>${pt.name}</dt><dd>${pt.text} ${refs(pt.refs)}</dd>`)}</dl>
      ${ov.path ? html`<h3 class="ov-q-head">建议的路径</h3>
        <p class="ov-path-text">${ov.path.text}</p>
        <ol class="ov-q ov-path">${ov.path.steps.map((s, i) => html`<li key=${i}>
          <b>${s.title}</b> ${s.why} ${refs(s.refs)}</li>`)}</ol>` : ""}
      ${ov.key_questions.length ? html`<h3 class="ov-q-head">关键问题</h3>
        <ol class="ov-q">${ov.key_questions.map((q) => html`<li key=${q.id}>
          <b>${q.title}</b> ${q.body} ${refs(q.refs)}</li>`)}</ol>` : ""}
      <${DevilInvite} ctx=${ctx} />`}
  </div>`;
}

// devil mode is a separate exercise; say so where the key questions are, so nobody has to discover it
const DEVIL_ROLE = {
  "research-report": "答辩委员会中最严格的教授", "academic-writing": "期刊的第二审稿人", "research-plan": "基金评审委员",
  "analysis-writeup": "要依据这份分析做决定的负责人", "product-spec": "合伙人会议上的投资人", "technical-design": "设计评审会上的首席工程师",
  "course-design": "课程委员会的资深教师", "blog-post": "持怀疑态度的内行读者", "personal-writing": "招生或招聘委员会的评委",
  "creative-writing": "文学编辑", "meeting-report": "没有参会、要据此行动的负责人",
};
function DevilInvite({ ctx }) {
  const [busy, setBusy] = useState(false);
  const [sent, setSent] = useState(false);
  const ti = ctx.st.type_info || {};
  const role = (ti.devil && ti.devil.role) || DEVIL_ROLE[ctx.M.plan.classification.types[0]] || "最严格的内行提问者";
  const go = async () => {
    setBusy(true);
    const ok = await ctx.say({ text: `[答辩模式] 请扮演${role}，开始答辩。` });
    setBusy(false);
    if (ok) setSent(true);
  };
  return html`<div class="devil">
    <div><b>答辩模式</b>　关键问题经不经得起追问？AI 扮演${role}，一次问一个决定成败的问题，根据你的回答追问，最后指出哪些回答站得住。在 agent 对话中进行。</div>
    <button disabled=${busy || sent} onClick=${go}>${sent ? "已请求，请回到对话" : "开始答辩"}</button>
  </div>`;
}

// every note on the page is one of these kinds, with the same colour and icon everywhere
const NOTE = {
  edit: { icon: "✎", name: "修改" },
  suggest: { icon: "💡", name: "建议" },
  explore: { icon: "？", name: "值得追问" },
  verify: { icon: "⚠", name: "待核实" },
  talk: { icon: "💬", name: "讨论中" },
};
const noteLabel = (k, sub) => `${NOTE[k].icon} ${NOTE[k].name}${sub ? " · " + sub : ""}`;
function noteKind(it) {
  if (!it) return null;
  if (it.kind === "change") return "edit";
  if (it.kind === "lead") return "explore";
  if (it.kind === "finding") return it.obj.note === "verify" ? "verify" : "suggest";
  if (it.kind === "claim") return it.obj.source_type === "unsourced" ? "verify" : null;
  return "suggest";
}
const LEAD_ZH = {
  anomaly: "异常结果", untested_implication: "未检验的推论", rival_explanation: "未排除的竞争性解释",
  model_for_design: "以模型代替设计", incoherence: "前后不一致",
  untested_assumption: "未经检验的假设", open_choice: "尚未作出的选择", missing_piece: "尚未考虑的部分",
};
// a plan (course, product idea, technical design) is read for how far it has been thought through
const isPlan = (ctx) => (ctx.st.type_info || {}).work === "plan";
const ownPlan = (ctx) => isPlan(ctx) && (ctx.M.plan.user_role || "author") === "author";
const LEAD_NOTE = {
  finished: "这些不是需要修改的错误，而是主要结论可能站不住的地方：异常结果、未检验的推论、未排除的竞争性解释。供你参考，可以记下想法或与 AI 讨论。",
  plan: "这些不是需要修改的错误，而是方案还没有想透的地方：依赖的假设尚未检验、有待作出的选择、尚未考虑的部分。供你参考，可以记下想法或与 AI 讨论。",
};

// ---------------------------------------------------------------- document

function markClass(M, id, gate, stageFilter, sel) {
  const it = M.items[id];
  if (!it) return null;
  const o = it.obj;
  let c;
  if (it.kind === "finding") {
    if (gate) return null;
    c = `hl f ${o.severity.toLowerCase()} n-${noteKind(it)} ${M.findingOpen(o) ? "open" : "done"}`;
  } else if (it.kind === "decision") {
    c = M.needs.has(id) ? "hl d n-suggest needs-d" : "hl d";
  } else if (it.kind === "lead") {
    if (gate) return null;
    c = M.needs.has(id) ? "hl lead n-explore open" : "hl lead n-explore";
  } else if (it.kind === "change") {
    if (gate) return null;
    const r = M.last[id] && M.last[id].confirm;
    c = `hl chg n-edit${r ? (r.value === "wrong" ? " undo" : " kept") : ""}`;
  } else {
    if (gate) return null;
    c = o.source_type === "unsourced" ? "hl c n-verify" : "hl c";
  }
  if (stageFilter && o.stage !== stageFilter) c += " dim";
  if (sel === id) c += " sel";
  return c;
}

function DocFile({ file, ctx }) {
  const { M, gate } = ctx;
  const marks = useMemo(() => Object.entries(M.ranges)
    .filter(([, r]) => r.path === file.path)
    .map(([id, r]) => ({ id, start: r.start, end: r.end })), [M, file.path]);
  return html`
    <article class="doc ${file.markdown ? "md" : "plain"}">
      ${M.files.length > 1 && html`<div class="doc-file muted">${file.path}</div>`}
      ${file.blocks.map((b, bi) => {
        const bm = marks.filter((m) => m.end > b.start && m.start < b.end);
        const ds = M.decisions.filter((d) => {
          const r = M.ranges[d.id];
          return r && r.path === file.path && r.end > b.start && r.end <= b.end + 1;
        });
        const stage = blockStage(M, bm);
        const zs = gate ? [] : M.zones.filter((z) => z.path === file.path && z.end > b.start && z.start < b.end);
        const starts = zs.filter((z) => z.start >= b.start || bi === 0 || !(z.start < file.blocks[bi - 1].end));
        const zk = zs.length ? zoneRank(zs)[0].kind : "";
        return html`
          ${starts.map((z) => html`<${ZoneCallout} z=${z} ctx=${ctx} key=${"z:" + z.id} />`)}
          <div class="blk ${zs.length ? "zone z-" + zk : b.type === "h" ? "" : "nozone"}" key=${b.start} style=${stage ? `--stage:${M.stageColor[stage]}` : ""} data-stage=${stage || ""}>
            ${renderBlock(file, b, bm, ctx)}
            ${ds.filter((d) => !zs.some((z) => z.id === d.id)).map((d) => html`<${DecisionInline} d=${d} ctx=${ctx} key=${d.id} />`)}
          </div>`;
      })}
    </article>`;
}

const ZONE_ORDER = { suggest: 0, verify: 1, explore: 2, talk: 3 };
function zoneRank(zs) { return zs.slice().sort((a, b) => ZONE_ORDER[a.kind] - ZONE_ORDER[b.kind]); }

// the question a zone asks, written above the stretch of text it covers
function ZoneCallout({ z, ctx }) {
  const sel = ctx.sel === z.id;
  return html`<button class="zcall ${z.kind} ${sel ? "sel" : ""}" data-ids=${z.id} onClick=${() => { track("zone", z.id); ctx.select(z.id, "doc"); }}>
    <span class="zlabel">${z.label}</span>
    <span class="zq">${z.question}</span>
    ${z.extra ? html`<span class="zx muted">${z.extra}</span>` : ""}
    <span class="zgo">${z.kind === "talk" ? "查看讨论" : "查看"} →</span>
  </button>`;
}

function blockStage(M, bm) {
  const order = { decision: 0, finding: 1, change: 2, claim: 3 };
  const its = bm.map((m) => M.items[m.id]).filter(Boolean).sort((a, b) => order[a.kind] - order[b.kind]);
  return its.length ? its[0].obj.stage : null;
}

function renderRun(file, run, marks, ctx, inline = true) {
  const { M, gate, stageFilter, sel, select } = ctx;
  const pieces = runPieces(file.text, run, marks, inline);
  const out = [];
  for (const p of pieces) {
    let node = p.text;
    for (const s of p.styles) {
      node = s === "code" ? html`<code>${node}</code>` : s === "strong" ? html`<strong>${node}</strong>`
        : s === "em" ? html`<em>${node}</em>` : html`<span class="lnk">${node}</span>`;
    }
    const ids = p.marks.filter((id) => markClass(M, id, gate, stageFilter, sel));
    if (ids.length) {
      // innermost (shortest) range wins the click
      const len = (id) => M.ranges[id].end - M.ranges[id].start;
      const sorted = ids.slice().sort((a, b) => len(a) - len(b));
      const cls = sorted.map((id) => markClass(M, id, gate, stageFilter, sel)).join(" ");
      const top = sorted.find((id) => M.items[id].kind !== "claim") || sorted[0];
      node = html`<mark class=${cls + (sorted.some((id) => M.needs.has(id)) ? " needs" : "")}
        data-ids=${sorted.join(" ")} onClick=${(e) => { e.stopPropagation(); select(top, "doc"); }}>${node}</mark>`;
    }
    out.push(node);
    for (const id of p.ends) {
      const it = M.items[id];
      if (it && it.kind === "claim" && !gate) {
        const c = it.obj;
        const resp = M.last[id] && M.last[id].confirm;
        out.push(html`<button class="src ${c.check_status} ${c.source_type === "unsourced" ? "unsourced" : ""} ${resp ? "confirmed-" + resp.value : ""}"
          data-ids=${id} title=${`${CHECK_ZH[c.check_status]} · ${SOURCE_ZH[c.source_type]}`}
          onClick=${(e) => { e.stopPropagation(); select(id, "doc"); }}>
          ${c.check_status === "verified" ? "✓" : c.check_status === "has_finding" ? "!" : "?"}</button>`);
      }
    }
  }
  return out;
}

function renderBlock(file, b, bm, ctx) {
  const r = (run) => renderRun(file, run, bm, ctx);
  switch (b.type) {
    case "h": {
      const lv = Math.min(b.level + 1, 6);
      return html`<${"h" + lv} class="dh">${r(b.runs[0])}<//>`;
    }
    case "hr": return html`<hr />`;
    case "code": return html`<pre class="code">${renderRun(file, b.runs[0], bm, ctx, false)}</pre>`;
    case "pre": return html`<p class="plain-p">${renderRun(file, b.runs[0], bm, ctx, false)}</p>`;
    case "quote": return html`<blockquote>${b.runs.map((x) => html`<p>${r(x)}</p>`)}</blockquote>`;
    case "list": {
      const items = b.items.map((it) => html`<li style=${it.indent ? `margin-left:${it.indent * 0.6}em` : ""}>
        ${it.runs.map((x, i) => html`${i ? " " : ""}${r(x)}`)}</li>`);
      return b.ordered ? html`<ol>${items}</ol>` : html`<ul>${items}</ul>`;
    }
    case "table": return html`<div class="tbl-wrap"><table>
      <thead><tr>${b.rows[0].map((c) => html`<th>${r(c)}</th>`)}</tr></thead>
      <tbody>${b.rows.slice(1).map((row) => html`<tr>${row.map((c) => html`<td>${r(c)}</td>`)}</tr>`)}</tbody>
    </table></div>`;
    default: return html`<p>${r(b.runs[0])}</p>`;
  }
}

function DecisionInline({ d, ctx }) {
  const { M, sel, select } = ctx;
  const chosen = d.options.find((o) => o.chosen);
  const needs = M.needs.has(d.id);
  if (d.human_action === "predict") return null;
  return html`
    <button class="dblock ${sel === d.id ? "sel" : ""} ${needs ? "needs" : ""}" data-ids=${d.id}
      onClick=${() => select(d.id, "doc")}>
      <span class="dblock-k">研究决策 · ${d.title}</span>
      <span class="dblock-v">当前：${chosen ? chosen.label : "—"}${d.options.length > 1 ? `（另有 ${d.options.length - 1} 个备选）` : ""}</span>
      ${needs ? html`<span class="chip suggest">${ACTION_ZH[d.human_action]}</span>` : html`<span class="muted small">${DSTATUS_ZH[d.status]}</span>`}
    </button>`;
}

function Minimap({ ctx, docRef }) {
  const [ticks, setTicks] = useState([]);
  const { M, select, gate } = ctx;
  useLayoutEffect(() => {
    const el = docRef.current;
    if (!el) return;
    const measure = () => {
      const H = el.scrollHeight || 1;
      const seen = new Set();
      const out = [];
      el.querySelectorAll(".zcall, mark.needs, .dblock.needs").forEach((m) => {
        const ids = (m.dataset.ids || "gate").split(" ");
        const id = ids.find((x) => M.needs.has(x)) || ids[0];
        if (seen.has(id)) return;
        seen.add(id);
        const it = M.items[id];
        const kind = m.classList.contains("zcall") ? m.classList[1] : noteKind(it) || "suggest";
        out.push({ id, top: (m.offsetTop / H) * 100, kind });
      });
      setTicks(out);
    };
    measure();
    const ro = new ResizeObserver(measure);
    ro.observe(el);
    return () => ro.disconnect();
  }, [M, gate]);
  return html`<div class="minimap" aria-hidden="true">
    ${ticks.map((t) => html`<button class="tick ${t.kind}" style=${`top:${t.top}%`} tabindex="-1"
      onClick=${() => t.kind !== "gate" && select(t.id)} title=${t.id}></button>`)}
  </div>`;
}

// ---------------------------------------------------------------- queue

function Queue({ ctx }) {
  const { M, gate } = ctx;
  const q = M.queue;
  const [showMinor, setShowMinor] = useState(false);
  const [showDone, setShowDone] = useState(false);
  const [showChanges, setShowChanges] = useState(false);
  const selChange = ctx.sel && M.items[ctx.sel] && M.items[ctx.sel].kind === "change";
  useEffect(() => { if (selChange) setShowChanges(true); }, [ctx.sel]);
  const empty = !q.decide.length && !q.major.length && !q.premises.length && !q.predict.length && !q.leads.length;
  const fixedByAi = M.findings.filter((f) => f.status === "fixed" && M.changes.some((c) => (c.finding_ids || []).includes(f.id)));
  return html`<div class="queue">
    ${q.decide.length ? html`<${Section} title=${isPlan(ctx) ? "需要你作的选择" : "研究决策"} note=${isPlan(ctx) ? "方案中尚未定下、又会影响其余部分的选择。" : "按研究流程排列；上游决策一旦更改，下游的判断需要重新考虑。"}>
      ${q.decide.map((d) => html`<${DecisionCard} d=${d} ctx=${ctx} key=${d.id} />`)}</${Section}>` : ""}
    ${q.leads.length ? html`<${Section} title="值得追问之处" note=${LEAD_NOTE[isPlan(ctx) ? "plan" : "finished"]}>
      ${q.leads.map((l) => html`<${LeadCard} l=${l} ctx=${ctx} key=${l.id} />`)}</${Section}>` : ""}
    ${q.major.length ? html`<${Section} title="具体问题" note="按严重程度和研究环节排列；同一根源引起的问题归在根源之下。">
      ${q.major.map((f) => html`<${FindingCard} f=${f} ctx=${ctx} key=${f.id} />`)}</${Section}>` : ""}
    ${q.premises.length ? html`<${Section} title="缺少出处的论断" note="相关材料不在项目中，AI 无法核实。">
      ${q.premises.map((c) => html`<${ClaimCard} c=${c} ctx=${ctx} key=${c.id} />`)}</${Section}>` : ""}
    ${q.changes.length ? html`<${Fold} title=${`AI 的修改 ${q.changes.length} 处（默认保留，可逐处撤销）`} open=${showChanges} setOpen=${setShowChanges}>
      ${q.changes.map((c) => html`<${ChangeCard} c=${c} ctx=${ctx} key=${c.id} />`)}</${Fold}>` : ""}
    ${empty ? html`<div class="empty">没有待处理的问题。</div>` : ""}
    ${fixedByAi.length ? html`<p class="muted small">另有 ${fixedByAi.length} 个问题已由 AI 修改并复核，见「已处理」。</p>` : ""}
    ${q.minor.length ? html`<${Fold} title=${`表述问题 ${q.minor.length} 条（只列出，未修改）`} open=${showMinor} setOpen=${setShowMinor}>
      ${q.minor.map((f) => html`<${FindingCard} f=${f} ctx=${ctx} key=${f.id} />`)}</${Fold}>` : ""}
    ${q.done.length + q.changesDone.length ? html`<${Fold} title=${`已处理 ${q.done.length + q.changesDone.length} 项`} open=${showDone} setOpen=${setShowDone}>
      ${q.changesDone.map((c) => html`<${ChangeCard} c=${c} ctx=${ctx} key=${c.id} />`)}
      ${q.done.map((o) => M.items[o.id].kind === "finding" ? html`<${FindingCard} f=${o} ctx=${ctx} key=${o.id} />`
        : M.items[o.id].kind === "decision" ? html`<${DecisionCard} d=${o} ctx=${ctx} key=${o.id} />`
          : M.items[o.id].kind === "lead" ? html`<${LeadCard} l=${o} ctx=${ctx} key=${o.id} />`
            : html`<${ClaimCard} c=${o} ctx=${ctx} key=${o.id} />`)}</${Fold}>` : ""}
    ${selectedClaim(ctx)}
  </div>`;
}

// A verified claim clicked in the document has no queue slot; show it at the bottom.
function selectedClaim(ctx) {
  const { M, sel } = ctx;
  const it = sel && M.items[sel];
  if (!it || it.kind !== "claim") return "";
  const c = it.obj;
  if (M.queue.premises.includes(c) || M.queue.done.includes(c)) return "";
  return html`<${Section} title="所选论断"><${ClaimCard} c=${c} ctx=${ctx} /></${Section}>`;
}

function Section({ title, note, children }) {
  return html`<section class="qsec"><h3>${title}</h3>${note && html`<p class="muted small">${note}</p>`}${children}</section>`;
}

function Fold({ title, open, setOpen, children }) {
  return html`<section class="qsec fold">
    <button class="fold-head" aria-expanded=${open} onClick=${() => setOpen(!open)}>${open ? "▾" : "▸"} ${title}</button>
    ${open && children}
  </section>`;
}

function CardShell({ id, ctx, cls, head, children, forceOpen }) {
  const open = forceOpen || ctx.sel === id;
  return html`<div class="card ${cls} ${open ? "open" : ""}" data-card=${id}>
    <button class="card-head" onClick=${() => ctx.select(open ? null : id, "side")} aria-expanded=${open}>${head}</button>
    ${open && html`<div class="card-body">${children}</div>`}
  </div>`;
}

function StaleNote({ ctx, id }) {
  return ctx.M.ranges[id] ? "" : html`<p class="warnbox small">当前版本中找不到这段原文（可能已改写）。</p>`;
}

function FindingCard({ f, ctx }) {
  const { M, st } = ctx;
  const resp = M.last[f.id] && M.last[f.id].reaction;
  const kids = M.children[f.id] || [];
  const head = html`
    <span class="dtag ${noteKind({ kind: "finding", obj: f })}">${noteLabel(noteKind({ kind: "finding", obj: f }), f.severity === "S1" ? "重大" : f.severity === "S3" ? "表述" : "")}</span>
    <span class="card-title">${f.title}</span>
    <span class="card-meta muted">${f.id} · ${M.stageName[f.stage] || f.stage}${kids.length ? ` · 连带 ${kids.length} 条` : ""}
      ${resp ? html` · <span class="chip ok">${REACTION_ZH[resp.reaction]}${resp.fix_by === "human" ? "（作者修改）" : ""}</span>` : ""}
      ${f.status !== "open" ? html` · <span class="chip">${f.status === "fixed" ? "已修正" : "已关闭"}</span>` : ""}</span>`;
  return html`<${CardShell} id=${f.id} ctx=${ctx} cls="finding" head=${head}>
    <blockquote class="anchor-q">${f.anchor.exact}</blockquote>
    <${StaleNote} ctx=${ctx} id=${f.id} />
    <p class="detail">${f.detail}</p>
    ${f.handoff_reason && f.status === "open" ? html`<p class="muted small">${f.handoff_reason}</p>` : ""}
    ${f.suggested_fix && !f.handoff_reason && html`<h4>修改建议</h4>
      <p>${f.suggested_fix.text}</p>
      ${f.suggested_fix.replacement && html`<div class="diff"><del>${f.anchor.exact}</del><ins>${f.suggested_fix.replacement}</ins></div>`}`}
    <details class="ev-fold"><summary>依据（${f.evidence.length} 条）</summary>
      ${f.evidence.map((ev, i) => html`<${Evidence} ev=${ev} ctx=${ctx} key=${i} />`)}
      <p class="muted small">${SEV_ZH[f.severity]} · ${ORIGIN_ZH[f.origin]}${f.caused_by ? ` · 由 ${f.caused_by} 引起` : ""}</p>
    </details>
    ${kids.length ? html`<h4>关联问题（根源修正后通常随之解决）</h4>
      ${kids.map((c) => html`<${FindingCard} f=${c} ctx=${ctx} key=${c.id} />`)}` : ""}
    ${fixes(ctx, f.id)}
    ${f.status === "open" ? html`<${Reactions} target=${f.id} resp=${resp} ctx=${ctx} st=${st} />` : ""}
    <${Discuss} ctx=${ctx} target=${f.id} />
  </${CardShell}>`;
}

function Reactions({ target, resp, ctx }) {
  const [note, setNote] = useState("");
  const [busy, setBusy] = useState(false);
  const go = async (r) => {
    setBusy(true);
    const ok = await ctx.send({ kind: "reaction", target, reaction: r.reaction, fix_by: r.fix_by, note });
    setBusy(false);
    if (ok) setNote("");
  };
  return html`<div class="actions">
    <div class="btns">
      ${REACTIONS.map((r) => html`<button disabled=${busy} title=${r.hint || ""}
        class=${resp && resp.reaction === r.reaction && (resp.fix_by || null) === (r.fix_by || null) ? "picked" : ""}
        onClick=${() => go(r)}>${r.label}</button>`)}
    </div>
    <input class="note" placeholder="备注（可选）" value=${note} onInput=${(e) => setNote(e.target.value)} />
  </div>`;
}

function DecisionCard({ d, ctx }) {
  const { M } = ctx;
  const l = M.last[d.id] || {};
  const needs = M.needs.has(d.id);
  const head = html`
    <span class="dtag suggest">${noteLabel("suggest", "研究决策")}</span>
    <span class="card-title">${d.title}</span>
    <span class="card-meta muted">${d.id} · ${M.stageName[d.stage] || d.stage} · 重要性 ${d.load_bearing.score}/3
      ${needs ? html` · <span class="chip suggest">${ACTION_ZH[d.human_action]}</span>` : html` · ${DSTATUS_ZH[d.status]}`}</span>`;
  return html`<${CardShell} id=${d.id} ctx=${ctx} cls="decision" head=${head}>
    <${DecisionBody} d=${d} ctx=${ctx} l=${l} />
  </${CardShell}>`;
}

function DecisionBody({ d, ctx, l }) {
  const chosen = d.options.find((o) => o.chosen);
  const [pick, setPick] = useState(l.choice ? l.choice.option_id : chosen && chosen.id);
  const [text, setText] = useState("");
  const [busy, setBusy] = useState(false);
  const act = d.human_action === "predict" ? "answer" : d.human_action;
  const go = async (body) => { setBusy(true); await ctx.send({ target: d.id, ...body }); setBusy(false); };
  return html`
    <p class="question">${d.question}</p>
    ${d.prompt_to_human && html`<p class="prompt">${d.prompt_to_human}</p>`}
    <${StaleNote} ctx=${ctx} id=${d.id} />
    <div class="options" role="radiogroup">
      ${d.options.map((o) => html`
        <label class="opt ${pick === o.id ? "picked" : ""}">
          <input type="radio" name=${"opt-" + d.id} checked=${pick === o.id} onChange=${() => setPick(o.id)} />
          <span class="opt-label">${o.label}${o.chosen ? html` <span class="chip">原文的做法</span>` : ""}</span>
          ${o.sensitivity && html`<span class="opt-sens ${o.sensitivity.computed ? "computed" : ""}">
            ${o.sensitivity.computed ? "重新计算：" : "推断："}${o.sensitivity.effect}</span>`}
          ${o.sensitivity && o.sensitivity.evidence && o.sensitivity.evidence.length ? html`
            <details class="ev-fold"><summary>依据</summary>
              ${o.sensitivity.evidence.map((ev, i) => html`<${Evidence} ev=${ev} ctx=${ctx} key=${i} />`)}</details>` : ""}
        </label>`)}
    </div>
    ${d.criteria && d.criteria.length ? html`<h4>判断标准</h4><ul class="criteria">
      ${d.criteria.map((k) => html`<li>${k.text}${k.stated_in_artifact ? "" : html` <span class="muted small">（AI 推断，原文未写明）</span>`}</li>`)}</ul>` : ""}
    ${d.challenge && html`<div class="challenge"><b>不同看法</b> ${d.challenge}</div>`}
    ${d.evidence && d.evidence.length ? html`<details class="ev-fold"><summary>证据 ${d.evidence.length} 条</summary>
      ${d.evidence.map((ev, i) => html`<${Evidence} ev=${ev} ctx=${ctx} key=${i} />`)}</details>` : ""}
    ${(d.upstream.length || d.downstream.length) ? html`<p class="muted small">
      ${d.upstream.length ? `上游：${d.upstream.join("、")}` : ""}${d.upstream.length && d.downstream.length ? " · " : ""}
      ${d.downstream.length ? `下游：${d.downstream.join("、")}（更改后需要重新考虑）` : ""}</p>` : ""}
    <div class="actions">
      ${act === "answer" && html`
        <textarea rows="3" placeholder="你的看法（可选）" value=${text} onInput=${(e) => setText(e.target.value)}></textarea>
        ${l.answer && html`<p class="muted small">此前的看法：${l.answer.text}</p>`}`}
      <div class="btns">
        ${act === "answer"
          ? html`<button class="primary" disabled=${busy || !text.trim()} onClick=${() => go({ kind: "answer", text })}>记下</button>`
          : html`<button class="primary" disabled=${busy || !pick}
              onClick=${() => go({ kind: "choice", option: pick })}>${pick === (chosen && chosen.id) ? "维持原做法" : "改用这一方案"}</button>`}
        ${act === "confirm" && html`
          <button disabled=${busy} onClick=${() => go({ kind: "confirm", value: "correct" })}>确认无误</button>
          <button disabled=${busy} onClick=${() => go({ kind: "confirm", value: "wrong" })}>存疑</button>`}
      </div>
      ${l.choice && html`<p class="muted small">已选：${(d.options.find((o) => o.id === l.choice.option_id) || {}).label}（${fmtTime(l.choice.created_at)}）</p>`}
      ${l.confirm && html`<p class="muted small">已${l.confirm.value === "correct" ? "确认无误" : "标记存疑"}</p>`}
    </div>
    <${Discuss} ctx=${ctx} target=${d.id} />`;
}

function ClaimCard({ c, ctx }) {
  const { M } = ctx;
  const resp = M.last[c.id] && M.last[c.id].confirm;
  const [busy, setBusy] = useState(false);
  const head = html`
    <span class="src ${c.check_status} ${c.source_type === "unsourced" ? "unsourced" : ""}">${c.check_status === "verified" ? "✓" : c.check_status === "has_finding" ? "!" : "?"}</span>
    <span class="card-title">${c.text}</span>
    <span class="card-meta muted">${c.id} · ${M.stageName[c.stage] || c.stage} · ${SOURCE_ZH[c.source_type]} · ${CHECK_ZH[c.check_status]}
      ${resp ? html` · <span class="chip ok">${resp.value === "correct" ? "已确认无误" : "已标记存疑"}</span>` : ""}</span>`;
  const go = async (value) => { setBusy(true); await ctx.send({ kind: "confirm", target: c.id, value }); setBusy(false); };
  return html`<${CardShell} id=${c.id} ctx=${ctx} cls="claim" head=${head}>
    <${StaleNote} ctx=${ctx} id=${c.id} />
    ${c.sources && c.sources.length ? html`<h4>来源</h4>${c.sources.map((ev, i) => html`<${Evidence} ev=${ev} ctx=${ctx} key=${i} />`)}`
      : html`<p class="muted small">没有出处。${c.source_type === "unsourced" ? "相关材料不在项目中。" : ""}</p>`}
    <div class="actions"><div class="btns">
      <button disabled=${busy} class=${resp && resp.value === "correct" ? "picked" : ""} onClick=${() => go("correct")}>确认无误</button>
      <button disabled=${busy} class=${resp && resp.value === "wrong" ? "picked" : ""} onClick=${() => go("wrong")}>标记存疑</button>
    </div></div>
    <${Discuss} ctx=${ctx} target=${c.id} />
  </${CardShell}>`;
}

// a detective lead: the AI asks, the person thinks; nothing here is fixed automatically
function LeadCard({ l, ctx }) {
  const { M } = ctx;
  const ans = M.last[l.id] && M.last[l.id].answer;
  const [text, setText] = useState("");
  const [busy, setBusy] = useState(false);
  const head = html`
    <span class="dtag explore">${noteLabel("explore", LEAD_ZH[l.kind])}</span>
    <span class="card-title">${l.title}</span>
    <span class="card-meta muted">${l.id}${ans ? html` · <span class="chip ok">已记下想法</span>` : ""}</span>`;
  const go = async () => {
    setBusy(true);
    const ok = await ctx.send({ kind: "answer", target: l.id, text });
    setBusy(false);
    if (ok) setText("");
  };
  return html`<${CardShell} id=${l.id} ctx=${ctx} cls="lead" head=${head}>
    <h4>观察</h4><p>${l.observation}</p>
    <h4>${isPlan(ctx) ? "对方案的影响" : "对主要结论的影响"}</h4><p>${l.threat}</p>
    <h4>可以追问的问题</h4><ol class="lead-q">${l.questions.map((q, i) => html`<li key=${i}>${q}</li>`)}</ol>
    ${l.evidence && l.evidence.length ? html`<h4>依据</h4>${l.evidence.map((ev, i) => html`<${Evidence} ev=${ev} ctx=${ctx} key=${i} />`)}` : ""}
    ${ans ? html`<p class="muted small">你的想法：${ans.text}</p>` : ""}
    <div class="actions">
      <textarea rows="3" placeholder="你的想法、需要核查的材料，或认为无关紧要的理由（可选）" value=${text}
        onInput=${(e) => setText(e.target.value)}></textarea>
      <div class="btns"><button class="primary" disabled=${busy || !text.trim()} onClick=${go}>${ans ? "补充" : "记下"}</button></div>
    </div>
    <${Discuss} ctx=${ctx} target=${l.id} />
  </${CardShell}>`;
}

// the AI's own edits that answer a finding
function fixes(ctx, fid) {
  const cs = ctx.M.changes.filter((c) => (c.finding_ids || []).includes(fid));
  if (!cs.length) return "";
  return html`<h4>AI 的相关修改</h4><ul class="ack">${cs.map((c) => html`<li>
    <button class="linkish" onClick=${() => ctx.select(c.id)}>${c.id} · ${c.summary}</button></li>`)}</ul>`;
}

function ChangeCard({ c, ctx }) {
  const { M } = ctx;
  const resp = M.last[c.id] && M.last[c.id].confirm;
  const [note, setNote] = useState("");
  const [busy, setBusy] = useState(false);
  const go = async (value) => { setBusy(true); const ok = await ctx.send({ kind: "confirm", target: c.id, value, note }); setBusy(false); if (ok) setNote(""); };
  const head = html`
    <span class="dtag edit">${noteLabel("edit")}</span>
    <span class="card-title">${c.summary}</span>
    <span class="card-meta muted">${c.id} · 第 ${c.round} 轮后${(c.finding_ids || []).length ? ` · 针对 ${c.finding_ids.join("、")}` : ""}
      ${resp ? html` · <span class="chip ${resp.value === "correct" ? "ok" : "warn"}">${resp.value === "correct" ? "已保留" : "已撤销"}</span>` : ""}</span>`;
  return html`<${CardShell} id=${c.id} ctx=${ctx} cls="change" head=${head}>
    <p class="detail">${c.why}</p>
    <div class="diff">${c.before ? html`<del>${c.before}</del>` : ""}${c.after ? html`<ins>${c.after}</ins>` : html`<span class="muted small">（删除）</span>`}</div>
    <${StaleNote} ctx=${ctx} id=${c.id} />
    ${(c.finding_ids || []).length ? html`<p class="muted small">针对的问题：${c.finding_ids.map((id, i) => html`${i ? "、" : ""}<button class="linkish" onClick=${() => ctx.select(id)}>${id}</button>`)}</p>` : ""}
    ${c.evidence && c.evidence.length ? html`<h4>新增内容的依据</h4>${c.evidence.map((ev, i) => html`<${Evidence} ev=${ev} ctx=${ctx} key=${i} />`)}` : ""}
    ${c.project_files && c.project_files.length ? html`<p class="muted small">相关的代码或数据副本：${c.project_files.map((x) => shortPath(x, ctx.st)).join("、")}（原项目未改动）</p>` : ""}
    <div class="actions">
      <div class="btns">
        <button disabled=${busy} class=${resp && resp.value === "correct" ? "picked" : ""} onClick=${() => go("correct")}>保留</button>
        <button disabled=${busy} class=${resp && resp.value === "wrong" ? "picked" : ""} onClick=${() => go("wrong")}>撤销</button>
      </div>
      <input class="note" placeholder="备注（可选）" value=${note} onInput=${(e) => setNote(e.target.value)} />
    </div>
    <${Discuss} ctx=${ctx} target=${c.id} />
  </${CardShell}>`;
}

function Messages({ ctx, msgs }) {
  return html`<ol class="msgs">${msgs.map((m) => html`<li class="msg ${m.author}">
    <span class="who">${m.author === "ai" ? "AI" : "你"} · ${fmtTime(m.created_at)}</span>
    <div class="msg-text">${m.text}</div>
    ${m.author === "human" && !ctx.M.replied.has(m.id) ? html`<span class="muted small">等待 AI 回复（需保持 agent 对话开启）</span>` : ""}
    ${(m.change_ids || []).length ? html`<span class="small">据此修改：${m.change_ids.map((id, i) => html`${i ? "、" : ""}<button class="linkish" onClick=${() => ctx.select(id)}>${id}</button>`)}</span>` : ""}
  </li>`)}</ol>`;
}

function Discuss({ ctx, target, open: startOpen }) {
  const msgs = ctx.M.threads[target || ""] || [];
  const [open, setOpen] = useState(!!startOpen || msgs.length > 0);
  const [text, setText] = useState("");
  const [busy, setBusy] = useState(false);
  const go = async () => {
    setBusy(true);
    const ok = await ctx.say({ text, target: target || null });
    setBusy(false);
    if (ok) setText("");
  };
  if (!open) return html`<button class="linkish small discuss-open" onClick=${() => setOpen(true)}>与 AI 讨论</button>`;
  return html`<div class="discuss">
    ${target ? html`<h4>讨论</h4>` : ""}
    <${Messages} ctx=${ctx} msgs=${msgs} />
    <textarea rows="2" placeholder=${target ? "提问，或写下你的看法" : "关于全文，想与 AI 讨论什么？"}
      value=${text} onInput=${(e) => setText(e.target.value)}
      onKeyDown=${(e) => { if (e.key === "Enter" && (e.metaKey || e.ctrlKey) && text.trim()) go(); }}></textarea>
    <div class="btns"><button disabled=${busy || !text.trim()} onClick=${go}>发送</button>
      <span class="muted small">AI AI 会在这里回复；如需修改正文，会在副本中修改并记录。</span></div>
  </div>`;
}

const SCOPE = [
  { id: "this_type", label: "适用于同类文本" },
  { id: "general", label: "适用于所有审阅" },
  { id: "this_review", label: "仅限本次" },
];

function Talk({ ctx }) {
  const { M } = ctx;
  const [text, setText] = useState("");
  const [scope, setScope] = useState("this_type");
  const [busy, setBusy] = useState(false);
  const others = Object.entries(M.threads).filter(([k]) => k);
  const suggestions = ctx.st.feedback.filter((r) => r.kind === "suggestion");
  const go = async () => {
    setBusy(true);
    const ok = await ctx.send({ kind: "suggestion", text, scope });
    setBusy(false);
    if (ok) setText("");
  };
  return html`<div class="talk">
    <section class="qsec"><h3>关于全文的讨论</h3>
      <${Discuss} ctx=${ctx} target=${null} open=${true} /></section>
    ${others.length ? html`<section class="qsec"><h3>各条目的讨论</h3><ul class="ack">
      ${others.map(([id, ms]) => {
        const waiting = ms.some((m) => m.author === "human" && !M.replied.has(m.id));
        return html`<li><button class="linkish" onClick=${() => ctx.select(id)}>${id}</button>
          <span class="muted small"> ${ms.length} 条${waiting ? " · 等 AI 回复" : ""} · ${ms[ms.length - 1].text.slice(0, 40)}</span></li>`;
      })}</ul></section>` : ""}
    <section class="qsec"><h3>对审阅的建议</h3>
      <p class="muted small">针对审阅本身，而非这篇文本：AI 应多关注什么、少报告什么、怎样表述更清楚。整理成规则后，经你同意生效。</p>
      <textarea rows="3" placeholder="例如：审阅学术报告时，先核对标题和摘要提出的问题是否就是数据能够回答的问题" value=${text}
        onInput=${(e) => setText(e.target.value)}></textarea>
      <div class="scope" role="radiogroup">${SCOPE.map((s) => html`<label>
        <input type="radio" name="scope" checked=${scope === s.id} onChange=${() => setScope(s.id)} /> ${s.label}</label>`)}</div>
      <div class="btns"><button class="primary" disabled=${busy || !text.trim()} onClick=${go}>记录建议</button></div>
      ${suggestions.length ? html`<h4>已记录的建议</h4><ul class="ack">${suggestions.map((r) => html`<li>${r.text}
        <span class="muted small">（${(SCOPE.find((s) => s.id === r.scope) || {}).label}）</span></li>`)}</ul>` : ""}
    </section>
    <${Missed} ctx=${ctx} />
  </div>`;
}

// issues the person found that the review should have raised: the strongest signal for improving reviews
function Missed({ ctx }) {
  const [text, setText] = useState("");
  const [where, setWhere] = useState("");
  const [busy, setBusy] = useState(false);
  const missed = ctx.st.feedback.filter((r) => r.kind === "missed");
  const go = async () => {
    setBusy(true);
    const ok = await ctx.send({ kind: "missed", text, note: where || undefined });
    setBusy(false);
    if (ok) { setText(""); setWhere(""); }
  };
  return html`<section class="qsec"><h3>AI 遗漏的问题</h3>
    <p class="muted small">你发现、但审阅没有提出的问题。这是改进审阅最重要的依据：下次同类审阅会据此增加相应的检查。</p>
    <textarea rows="3" placeholder="例如：没有检查空档简历与对照简历在格式上是否一致" value=${text}
      onInput=${(e) => setText(e.target.value)}></textarea>
    <input class="note" placeholder="位置（可选），例如 §2.3" value=${where} onInput=${(e) => setWhere(e.target.value)} />
    <div class="btns"><button class="primary" disabled=${busy || !text.trim()} onClick=${go}>记录</button></div>
    ${missed.length ? html`<h4>已记录（${missed.length}）</h4><ul class="ack">${missed.map((r) => html`<li>${r.text}
      ${r.note ? html`<span class="muted small">（${r.note}）</span>` : ""}</li>`)}</ul>` : ""}
  </section>`;
}

// a document without any review layer (the version before the AI's edits)
function PlainDoc({ file, ctx }) {
  const plain = { ...ctx, M: { ...ctx.M, ranges: {}, items: {}, needs: new Set() } };
  return html`<article class="doc ${file.markdown ? "md" : "plain"} baseline">
    <div class="doc-file muted">原稿（AI 修改前）${ctx.M.baseline.length > 1 ? ` · ${file.path}` : ""}</div>
    ${file.blocks.map((b) => html`<div class="blk" key=${b.start}>${renderBlock(file, b, [], plain)}</div>`)}
  </article>`;
}

function Evidence({ ev, ctx }) {
  const sp = (p) => shortPath(p, ctx.st);
  if (ev.kind === "file") {
    return html`<div class="ev">
      <button class="ev-head linkish" onClick=${() => ctx.openFile(ev)} title="打开文件">
        ${sp(ev.path)} · 第 ${ev.line_start}${ev.line_end && ev.line_end !== ev.line_start ? `–${ev.line_end}` : ""} 行</button>
      <pre>${ev.excerpt}</pre>${ev.note && html`<p class="muted small">${ev.note}</p>`}</div>`;
  }
  if (ev.kind === "data") {
    return html`<div class="ev">
      <button class="ev-head linkish" onClick=${() => ctx.openFile({ ...ev, search: ev.key })}>${sp(ev.path)} · ${ev.key}</button>
      <pre>${typeof ev.value === "string" ? ev.value : JSON.stringify(ev.value, null, 1)}</pre>
      ${ev.note && html`<p class="muted small">${ev.note}</p>`}</div>`;
  }
  if (ev.kind === "command") {
    return html`<div class="ev"><div class="ev-head mono">$ ${ev.command}</div>
      ${ev.cwd && html`<div class="muted small">在 ${sp(ev.cwd)}${ev.exit_code !== undefined ? ` · 退出码 ${ev.exit_code}` : ""}</div>`}
      <pre>${ev.output_excerpt}</pre>${ev.note && html`<p class="muted small">${ev.note}</p>`}</div>`;
  }
  return html`<div class="ev"><div class="ev-head">${sp(ev.source)}${ev.locator ? ` · ${ev.locator}` : ""}</div>
    <blockquote>${ev.text}</blockquote>${ev.note && html`<p class="muted small">${ev.note}</p>`}</div>`;
}

// ---------------------------------------------------------------- file viewer

function FileViewer({ sid, ev, st, onClose }) {
  const [data, setData] = useState(null);
  const [err, setErr] = useState(null);
  const box = useRef(null);
  useEffect(() => {
    api(`/api/s/${encodeURIComponent(sid)}/file?path=${encodeURIComponent(ev.path)}`).then(setData, (e) => setErr(e.message));
    const esc = (e) => e.key === "Escape" && onClose();
    addEventListener("keydown", esc);
    return () => removeEventListener("keydown", esc);
  }, [ev.path]);
  const lines = data ? data.text.split("\n") : [];
  let lo = ev.line_start || 0;
  let hi = ev.line_end || lo;
  if (!lo && ev.search && data) {
    const k = lines.findIndex((l) => l.includes(ev.search));
    if (k >= 0) { lo = k + 1; hi = k + 1; }
  }
  useLayoutEffect(() => {
    const el = box.current && box.current.querySelector(".ln.hit");
    if (el) el.scrollIntoView({ block: "center" });
  }, [data]);
  return html`<div class="modal-bg" onClick=${onClose}>
    <div class="modal" role="dialog" aria-label="源文件" onClick=${(e) => e.stopPropagation()}>
      <div class="modal-head"><span class="mono">${shortPath(ev.path, st)}</span>
        <button onClick=${onClose} aria-label="关闭">×</button></div>
      <div class="modal-body" ref=${box}>
        ${err && html`<p class="error">${err}</p>`}
        ${!data && !err && html`<p class="muted">读取中…</p>`}
        ${data && html`<pre class="file">${lines.map((l, i) => html`<div class="ln ${i + 1 >= lo && i + 1 <= hi ? "hit" : ""}"><span class="n">${i + 1}</span>${l || " "}</div>`)}</pre>`}
      </div>
    </div>
  </div>`;
}

// ---------------------------------------------------------------- signoff

function Signoff({ ctx }) {
  const { M, st, send } = ctx;
  const [quality, setQuality] = useState(0);
  const [note, setNote] = useState("");
  const [busy, setBusy] = useState(false);
  const s1 = M.openFindings.filter((f) => f.severity === "S1");
  const awaiting = M.queue.decide;
  const last = M.signoffs[M.signoffs.length - 1];
  const go = async (outcome) => {
    setBusy(true);
    const ok = await send({ kind: "signoff", outcome, quality: quality || undefined, note });
    // finishing is the AI's cue for its last step: write back, revise, and learn from this review
    if (ok) await ctx.say({ text: `[完成审阅] ${outcome === "deliver" ? "定稿" : "再审一轮"}` }, "已记录。AI 会在对话中继续");
    setBusy(false);
  };
  const List = ({ title, items, render: r }) => items.length ? html`<h4>${title}（${items.length}）</h4>
    <ul class="ack">${items.map((x) => html`<li><button class="linkish" onClick=${() => ctx.select(x.id)}>${r(x)}</button></li>`)}</ul>` : "";
  return html`<div class="signoff">
    <h3>完成审阅</h3>
    ${last && html`<p class="muted small">上次：${last.signoff.outcome === "deliver" ? "定稿" : "再审一轮"}（${fmtTime(last.created_at)}）
      ${last.artifact_hash === st.artifact_hash ? "，针对当前版本" : "，针对的是旧版本"}</p>`}
    ${s1.length ? html`<p class="muted small">仍有 ${s1.length} 个重大问题未处理。</p>` : ""}
    <${List} title="未处理的问题" items=${M.openFindings} render=${(f) => `${f.severity} ${f.id} · ${f.title}`} />
    <${List} title="待定的研究决策" items=${awaiting} render=${(d) => `${d.id} · ${d.title}`} />
    <${List} title="缺少出处的论断" items=${M.queue.premises} render=${(c) => `${c.id} · ${c.text}`} />
    <${List} title="沿用原做法的研究决策" items=${M.queue.aiDefault}
      render=${(d) => `${d.id} · ${d.title}：${(d.options.find((o) => o.chosen) || {}).label || ""}`} />
    ${M.plan.not_checked.length ? html`<h4>本次未核查</h4><ul class="ack">
      ${M.plan.not_checked.map((n) => html`<li>${n.what}<span class="muted">：${n.why}</span></li>`)}</ul>` : ""}
    <p class="muted small">以上各项会一并记入审阅记录。</p>
    <p class="small">完成之后，AI 会回顾你在这次审阅中的回应——哪些意见你认为不成立或无关紧要、撤销了哪些修改、记下了哪些遗漏——提出几条改进今后同类审阅的建议，请你逐条确认。</p>
    <h4>对这篇文本当前质量的评价（可选）</h4>
    <div class="stars" role="radiogroup">${[1, 2, 3, 4, 5].map((n) => html`
      <button class=${quality >= n ? "on" : ""} aria-label=${n + " 分"} onClick=${() => setQuality(quality === n ? 0 : n)}>${n}</button>`)}</div>
    <input class="note" placeholder="备注（可选）" value=${note} onInput=${(e) => setNote(e.target.value)} />
    <div class="btns">
      <button class="primary" disabled=${busy} onClick=${() => go("deliver")}>定稿</button>
      <button disabled=${busy} onClick=${() => go("another_round")}>再审一轮</button>
    </div>
    ${M.changes.length ? html`<p class="muted small">AI 的 ${M.changes.length} 处修改目前只在副本中。定稿后，可在 agent 对话中要求写回原文件；写回前会列出全部差异，经你同意才会修改。</p>` : ""}
    <p class="muted small">选择"再审一轮"后，可在 agent 中运行 review-apply，按你的意见修改。</p>
  </div>`;
}

// the review as a reviewer's process: verdict, rounds, what was checked
function Process({ ctx }) {
  const { st, M } = ctx;
  const pr = st.process;
  const rd = M.mp && M.mp.readiness;
  const [showCert, setShowCert] = useState(false);
  const ref = (id) => M.items[id]
    ? html`<button class="ref ${noteKind(M.items[id]) || ""}" onClick=${() => ctx.select(id)}>${id}</button>` : html`<span class="muted">${id}</span>`;
  return html`<div class="process">
    ${rd ? html`<section class="verdict ${rd.verdict}">
      <h3>${ownPlan(ctx)
        ? (rd.verdict === "ready" ? "可以着手实施" : "动手之前，先想清楚这几件事")
        : M.plan.user_role === "evaluator"
        ? (rd.verdict === "ready" ? "审稿结论：可以接受" : "审稿结论：需要作者修改")
        : (rd.verdict === "ready" ? "可以交给你审阅" : "建议先退回作者修改")}</h3>
      ${ownPlan(ctx) && M.mp.overview && M.mp.overview.path ? html`<p class="muted small">下一步见概览中的“建议的路径”。</p>` : ""}
      <p>${rd.text}</p>
      ${rd.tasks.length ? html`<ol class="tasks">${rd.tasks.map((t) => html`<li key=${t.id}>
        <b>${t.title}</b> ${t.why} ${(t.refs || []).map(ref)}</li>`)}</ol>` : ""}
      ${rd.sent_to ? html`<p class="muted small">已发给 ${rd.sent_to}（${fmtTime(rd.sent_at)}）</p>`
        : rd.tasks.length ? html`<p class="muted small">${M.plan.user_role === "evaluator" ? "给作者的意见在 handback.md。" : "任务清单在 handback.md；可在 agent 对话中要求发给作者。"}</p>` : ""}
    </section>` : ""}
    ${pr ? html`<section>
      <h3>AI 的审阅过程</h3>
      <ol class="rounds">${pr.rounds.map((r) => html`<li key=${r.round}>
        <div class="r-head">第 ${r.round} 轮${r.from_edits ? " · 复查上一轮的修改" : ""}</div>
        <p>审阅者提出 ${r.examined} 条意见，复核后保留 ${r.kept} 条${r.dropped ? `，其余 ${r.dropped} 条重复或不成立` : ""}。
          ${r.raised.S1 ? `其中重大问题 ${r.raised.S1} 个。` : ""}</p>
        ${Object.keys(r.raised.by_layer).length ? html`<p class="muted small">分布：${Object.entries(r.raised.by_layer).map(([k, v]) => `${k} ${v}`).join("，")}</p>` : ""}
        ${r.edits.total ? html`<details><summary>AI 随后修改 ${r.edits.total} 处</summary>
          <ul>${r.edits.by_layer.map((g) => html`<li><b>${g.layer}</b> ${g.n} 处：${g.examples.join("；")}${g.n > g.examples.length ? "……" : ""}</li>`)}</ul>
        </details>` : ""}
        ${r.confirmed_fixed ? html`<p class="muted small">下一轮确认其中 ${r.confirmed_fixed} 个问题已经解决。</p>` : ""}
      </li>`)}</ol>
      ${pr.merged ? html`<p class="muted small">交给你之前，又把 ${pr.merged} 条与其他意见或选择重复的合并，每件事只写一次。</p>` : ""}
      <p class="muted small">共修改 ${pr.edits_total} 处，解决 ${pr.fixed_total} 个问题${pr.fixed_after_rounds ? `（其中 ${pr.fixed_after_rounds} 个在三轮之后修正）` : ""}。</p>
      ${pr.handed.length ? html`<h4>留给作者的问题（${pr.handed.length}）</h4>
        <ul class="handed">${pr.handed.map((h) => html`<li>${ref(h.id)} ${h.title}</li>`)}</ul>` : ""}
    </section>` : html`<p class="muted">还没有审阅过程的记录。</p>`}
    <section>
      <button class="fold-head" aria-expanded=${showCert} onClick=${() => setShowCert(!showCert)}>${showCert ? "▾" : "▸"} 核查记录：查了什么、没有查什么</button>
      ${showCert && html`<${Certificate} ctx=${ctx} />`}
    </section>
  </div>`;
}

function Certificate({ ctx }) {
  const { st } = ctx;
  if (!st.certificate) return html`<p class="muted">还没有核查记录。</p>`;
  const file = { path: "certificate.md", text: st.certificate };
  const blocks = parseBlocks(st.certificate, true);
  const plain = { ...ctx, M: { ...ctx.M, ranges: {}, items: {}, needs: new Set() } };
  return html`<article class="doc md cert">${blocks.map((b) => html`<div class="blk" key=${b.start}>${renderBlock(file, b, [], plain)}</div>`)}</article>`;
}

render(html`<${App} />`, document.getElementById("app"));
