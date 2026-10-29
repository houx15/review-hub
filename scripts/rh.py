#!/usr/bin/env python3
# /// script
# requires-python = ">=3.9"
# dependencies = ["jsonschema>=4.18", "pyyaml>=6", "rfc3339-validator"]
# ///
"""rh: deterministic helpers for the Review Engine skills.

Skills do the judgement; this script does everything that must be exact:
session folders, artifact hashing, ids, schema validation, append-only
writes, metrics and the summary. Run with `uv run scripts/rh.py <command>`.
"""
from __future__ import annotations

import argparse
import datetime as dt
import difflib
import hashlib
import json
import os
import re
import shutil
import sys
import time
from pathlib import Path

import yaml
from jsonschema import Draft202012Validator, FormatChecker
from referencing import Registry, Resource

ENGINE_ROOT = Path(__file__).resolve().parent.parent
SCHEMA_DIR = ENGINE_ROOT / "schemas"
SCHEMA_VERSION = "0.1"
SESSION_FILES = ["findings.jsonl", "feedback.jsonl", "changes.jsonl", "threads.jsonl"]
SEVERITIES = ["S1", "S2", "S3"]
S3_CAP = 5
REACTIONS = ["fixed", "ai_wrong", "intentional", "doesnt_matter", "class_of_problem"]
REACTION_ZH = {
    "fixed": "已修正", "ai_wrong": "不成立", "intentional": "有意为之",
    "doesnt_matter": "无关紧要", "class_of_problem": "同类问题",
}
ORIGIN_ZH = {"introduced": "文本本身的问题", "source_material": "源自材料或数据", "inherited": "源自上游的研究决策"}
CANDIDATE_FIELDS = {
    "stage", "severity", "title", "detail", "evidence", "anchor", "origin", "origin_decision",
    "decision_ids", "claim_ids", "suggested_fix", "rule_ref", "class_of", "problem_kind", "note",
}


class RHError(Exception):
    def __init__(self, msg: str, code: int = 1):
        super().__init__(msg)
        self.code = code


# ---------------------------------------------------------------- basics

def now() -> str:
    return dt.datetime.now().astimezone().isoformat(timespec="seconds")


def die(msg: str, code: int = 1):
    print(f"错误：{msg}", file=sys.stderr)
    sys.exit(code)


def read_json(p: Path):
    return json.loads(p.read_text(encoding="utf-8"))


def write_json(p: Path, data) -> None:
    p.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def read_jsonl(p: Path) -> list:
    if not p.exists():
        return []
    out = []
    for i, line in enumerate(p.read_text(encoding="utf-8").splitlines(), 1):
        if line.strip():
            try:
                out.append(json.loads(line))
            except json.JSONDecodeError as e:
                raise RHError(f"{p.name} 第 {i} 行不是合法 JSON：{e}")
    return out


def append_jsonl(p: Path, rec: dict) -> None:
    with p.open("a", encoding="utf-8") as f:
        f.write(json.dumps(rec, ensure_ascii=False, separators=(",", ":")) + "\n")


def latest_by_id(records: list) -> dict:
    """findings.jsonl is append-only; the last line per id wins."""
    out = {}
    for r in records:
        out[r["id"]] = r
    return out


def norm_ws(s: str) -> str:
    return re.sub(r"\s+", " ", s).strip()


def workspace_root(arg: str | None) -> Path:
    p = arg or os.environ.get("REVIEW_WORKSPACE") or "~/review-workspace"
    return Path(p).expanduser().resolve()


def resolve_session(arg: str, ws: str | None) -> Path:
    p = Path(arg).expanduser()
    if p.is_dir():
        return p.resolve()
    cand = workspace_root(ws) / "sessions" / arg
    if cand.is_dir():
        return cand
    die(f"找不到会话：{arg}")


def session_id(sess: Path) -> str:
    return sess.name


# ---------------------------------------------------------------- schemas

_validators: dict = {}
COMMON_ID = "https://review-hub.invalid/schemas/common.schema.json"


def validator(name: str) -> Draft202012Validator:
    if not _validators:
        schemas = {p.name: read_json(p) for p in SCHEMA_DIR.glob("*.schema.json")}
        reg = Registry().with_resources((s["$id"], Resource.from_contents(s)) for s in schemas.values())
        for n, s in schemas.items():
            _validators[n] = Draft202012Validator(s, registry=reg, format_checker=FormatChecker())
        _validators["evidence"] = Draft202012Validator({"$ref": COMMON_ID + "#/$defs/evidence"},
                                                       registry=reg, format_checker=FormatChecker())
    return _validators[name]


def schema_errors(name: str, doc) -> list:
    errs = sorted(validator(name).iter_errors(doc), key=lambda e: list(e.absolute_path))
    return [f"{'/'.join(map(str, e.absolute_path)) or '(根)'}: {e.message[:300]}" for e in errs]


def check_or_die(name: str, doc, what: str) -> None:
    errs = schema_errors(name, doc)
    if errs:
        raise RHError(f"{what} 不符合 {name}：\n  " + "\n  ".join(errs), 3)


# ---------------------------------------------------------------- artifact

def artifact_files(sess: Path) -> list:
    root = sess / "artifact"
    return sorted(p for p in root.rglob("*") if p.is_file())


def artifact_hash(sess: Path) -> str:
    """sha256 over sorted lines '<path relative to session>\\t<sha256 of file>\\n'."""
    lines = []
    for p in artifact_files(sess):
        rel = p.relative_to(sess).as_posix()
        lines.append(f"{rel}\t{hashlib.sha256(p.read_bytes()).hexdigest()}\n")
    return "sha256:" + hashlib.sha256("".join(sorted(lines)).encode()).hexdigest()


def anchor_found(sess: Path, anchor: dict) -> bool:
    p = sess / anchor["path"]
    if not p.is_file():
        return False
    text = p.read_text(encoding="utf-8", errors="replace")
    return anchor["exact"] in text or norm_ws(anchor["exact"]) in norm_ws(text)


ELISION = re.compile(r"(\.\.\.|…+|⋯+)")


def check_file_evidence(ev: dict, sess: Path) -> str | None:
    """Return an error if a file excerpt cannot be found where it claims to be."""
    p = Path(ev["path"]).expanduser()
    if not p.is_absolute():
        p = sess / p
    if not p.is_file():
        print(f"提示：证据文件无法访问，未核对摘录：{ev['path']}（请用绝对路径或会话内路径）", file=sys.stderr)
        return None
    text = p.read_text(encoding="utf-8", errors="replace")
    excerpt = norm_ws(ev["excerpt"])
    if excerpt not in norm_ws(text):
        # allow an excerpt of non-contiguous lines, with '...' marking elided lines
        parts = [l for l in ev["excerpt"].splitlines() if l.strip() and not ELISION.fullmatch(l.strip())]
        if not parts or not all(norm_ws(l) in norm_ws(text) for l in parts):
            return f"摘录在 {ev['path']} 中找不到：{ev['excerpt'][:80]!r}"
    lines = text.splitlines()
    lo = max(ev["line_start"] - 3, 0)
    hi = (ev.get("line_end") or ev["line_start"]) + 2
    window = norm_ws("\n".join(lines[lo:hi]))
    first = norm_ws(ev["excerpt"].splitlines()[0]) if ev["excerpt"].strip() else ""
    if first and first not in window:
        return f"{ev['path']} 第 {ev['line_start']} 行附近没有这段摘录（行号不对？）"
    return None


# ---------------------------------------------------------------- types

def load_type(type_id: str, ws: Path | None) -> dict | None:
    for base in ([ws / "types"] if ws else []) + [ENGINE_ROOT / "types"]:
        p = base / type_id / "TYPE.md"
        if p.is_file():
            m = re.match(r"^---\n(.*?)\n---\n", p.read_text(encoding="utf-8"), re.S)
            if m:
                return yaml.safe_load(m.group(1))
    return None


# A plan (a course, a product idea, a technical design) is judged on how far it has been thought
# through; finished work on whether it holds. Each has its own rating scale and kinds of leads.
RATINGS = {"finished": ["strong", "adequate", "weak"], "plan": ["clear", "partial", "open"]}
WORK_ZH = {"finished": "已完成的作品", "plan": "方案"}
LEAD_KINDS = {"finished": ["anomaly", "untested_implication", "rival_explanation", "model_for_design", "incoherence"],
              "plan": ["untested_assumption", "open_choice", "missing_piece", "incoherence"]}


def work_kind(td: dict | None) -> str:
    return (td or {}).get("work", "finished")


def lead_kinds(td: dict | None) -> list:
    return (td or {}).get("lead_kinds") or LEAD_KINDS[work_kind(td)]


# ---------------------------------------------------------------- commands: setup

CONFIG_TEMPLATE = """# Review Engine 工作区配置
engine_root: {engine}
default_tier: standard
enabled_types: [{types}]
agents: [claude-code]
"""


def cmd_init(a) -> None:
    ws = Path(a.path).expanduser().resolve()
    created = []

    def mk(p: Path, content: str | None = None):
        if p.exists():
            return
        if content is None:
            p.mkdir(parents=True)
        else:
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(content, encoding="utf-8")
        created.append(p.relative_to(ws).as_posix() or ".")

    mk(ws)
    types = sorted(p.parent.name for p in (ENGINE_ROOT / "types").glob("*/TYPE.md"))
    mk(ws / "config.yaml", CONFIG_TEMPLATE.format(engine=ENGINE_ROOT, types=", ".join(types)))
    mk(ws / "personal" / "blind-spots.md", "# 个人盲点\n\n（由 review-learn 根据你的反馈提议，你批准后写入。）\n")
    mk(ws / "personal" / "preferences.md", "# 表述与格式偏好\n\n（只影响怎么说，不影响查不查。）\n")
    for t in types:
        src = ENGINE_ROOT / "types" / t / "TYPE.md"
        mk(ws / "types" / t / "TYPE.md", src.read_text(encoding="utf-8"))
        mk(ws / "types" / t / "rules.md", "# 审核规则\n\n（每条规则：编号、内容、来源反馈、命中次数。）\n")
        mk(ws / "types" / t / "checks")
    for d in ["sessions", "ledger/eval"]:
        mk(ws / d)
    for f in ["ledger/incidents.jsonl", "ledger/rule-proposals.jsonl"]:
        mk(ws / f, "")
    print(f"工作区：{ws}")
    print("新建：" + (", ".join(created) if created else "（无，全部已存在）"))


def cmd_new_session(a) -> None:
    ws = workspace_root(a.workspace)
    if not (ws / "sessions").is_dir():
        die(f"工作区 {ws} 未初始化，先运行 review-setup（rh init）")
    if not re.fullmatch(r"[a-z0-9]+(-[a-z0-9]+)*", a.slug):
        die("slug 只能用小写英文、数字和连字符")
    sid = f"{dt.date.today().isoformat()}-{a.slug}"
    sess = ws / "sessions" / sid
    n = 2
    while sess.exists():
        sid = f"{dt.date.today().isoformat()}-{a.slug}-{n}"
        sess = ws / "sessions" / sid
        n += 1
    srcs = [Path(f).expanduser().resolve() for f in a.files]
    for s in srcs:
        if not s.is_file():
            die(f"不是文件：{s}")
    names = [s.name for s in srcs]
    if len(set(names)) != len(names):
        die("产出文件重名，请分别建会话或先改名")
    (sess / "artifact").mkdir(parents=True)
    (sess / "work").mkdir()
    (sess / "baseline").mkdir()
    for s in srcs:
        shutil.copy2(s, sess / "artifact" / s.name)
        shutil.copy2(s, sess / "baseline" / s.name)
    write_json(sess / "sources.json", {
        "files": [{"snapshot": f"artifact/{s.name}", "original": str(s)} for s in srcs],
    })
    for f in SESSION_FILES:
        (sess / f).touch()
    print(f"会话：{sess}")
    print(f"session_id：{sid}")
    print(f"artifact_hash：{artifact_hash(sess)}")
    print("快照：" + ", ".join(f"artifact/{n}" for n in names))


def cmd_snapshot(a) -> None:
    sess = resolve_session(a.session, a.workspace)
    old = artifact_hash(sess)
    src = read_json(sess / "sources.json")["files"]
    pending = [c["id"] for c in applied_changes(sess)]
    if pending and not a.force:
        die(f"快照里有 AI 自改、还没写回原文件的改动（{', '.join(pending)}），重新复制会把它们覆盖。"
            "先 writeback，或确认要丢弃时加 --force", 3)
    for f in src:
        orig, snap = Path(f["original"]), sess / f["snapshot"]
        if not orig.is_file():
            die(f"原文件不存在：{orig}")
        before = snap.read_text(encoding="utf-8", errors="replace").splitlines()
        after = orig.read_text(encoding="utf-8", errors="replace").splitlines()
        diff = list(difflib.unified_diff(before, after, f["snapshot"], str(orig), n=0, lineterm=""))
        if diff:
            print("\n".join(diff))
        shutil.copy2(orig, snap)
        base = sess / "baseline" / Path(f["snapshot"]).name
        base.parent.mkdir(exist_ok=True)
        shutil.copy2(orig, base)
    if pending:
        for c in applied_changes(sess):
            set_change_status(sess, c["id"], "reverted")
    new = artifact_hash(sess)
    print(f"artifact_hash：{old} -> {new}" if new != old else f"没有改动：{new}")


def make_anchor(sess: Path, exact: str, path: str | None = None, ctx: int = 32) -> dict:
    files = [sess / path] if path else artifact_files(sess)
    hits = []
    for f in files:
        text = f.read_text(encoding="utf-8", errors="replace")
        start = text.find(exact)
        while start != -1:
            hits.append((f, text, start))
            start = text.find(exact, start + 1)
    if not hits:
        raise RHError(f"快照中找不到：{exact[:60]!r}")
    if len(hits) > 1:
        raise RHError(f"快照中出现 {len(hits)} 次，请加长原文使其唯一：{exact[:60]!r}")
    f, text, i = hits[0]
    return {
        "path": f.relative_to(sess).as_posix(), "exact": exact,
        "prefix": text[max(0, i - ctx):i], "suffix": text[i + len(exact):i + len(exact) + ctx],
        "line_hint": text.count("\n", 0, i) + 1,
    }


def cmd_anchor(a) -> None:
    sess = resolve_session(a.session, a.workspace)
    print(json.dumps(make_anchor(sess, a.exact, a.path), ensure_ascii=False))


def cmd_hash(a) -> None:
    print(artifact_hash(resolve_session(a.session, a.workspace)))


BUDGETS = {"light": (0, 3), "standard": (3, 10), "deep": (5, 10)}
TODO = "TODO"


def reviewer_count(tier: str, n_stages: int) -> int:
    """Finders (about three stages each) plus one verifier; deep adds a falsifier."""
    if tier == "light":
        return 2
    finders = max(1, -(-n_stages // 3))
    return finders + 1 + (1 if tier == "deep" else 0)


def cmd_init_plan(a) -> None:
    sess = resolve_session(a.session, a.workspace)
    if (sess / "plan.json").exists() and not a.force:
        die("plan.json 已存在（--force 覆盖）")
    td = load_type(a.type, workspace_root(a.workspace))
    if td is None:
        die(f"找不到类型 {a.type} 的 TYPE.md")
    in_tier = td.get("tiers", {}).get(a.tier, "all")
    stages, not_checked = [], []
    for st in td["stages"]:
        inc = in_tier == "all" or st["id"] in in_tier
        d = {"id": st["id"], "name": st["name"], "verifiability": st["v"], "included": inc,
             "ai_checks": [], "human": {"involved": False, "action": "none"}}
        if st.get("aspect"):
            d["aspect"] = st["aspect"]
        if not inc:
            d["excluded_reason"] = f"{a.tier} 档不包含"
            not_checked.append({"what": st["name"], "why": d["excluded_reason"]})
        stages.append(d)
    src = read_json(sess / "sources.json")["files"]
    oq, ch = BUDGETS[a.tier]
    plan = {
        "schema_version": SCHEMA_VERSION, "id": "plan", "created_at": now(), "session_id": session_id(sess),
        "revision": 1, "status": "draft",
        "artifact": {"title": TODO, "snapshot_paths": [f["snapshot"] for f in src], "hash": artifact_hash(sess),
                     "original_location": src[0]["original"] if len(src) == 1 else str(Path(src[0]["original"]).parent)},
        "classification": {"types": [a.type], "rationale": TODO, "confirmed_by_human": False},
        "tier": a.tier, "focus": [],
        "direction_check": {"question": TODO},
        **({"aspects": td["aspects"]} if td.get("aspects") else {}),
        "user_role": "author",
        "revision_policy": td.get("revision", "full"),
        "stages": stages, "ground_truth": [],
        "attention_budget": {"open_questions_max": oq, "choices_max": ch},
        "estimate": {"human_minutes": {"light": 5, "standard": 20, "deep": 45}[a.tier],
                     "reviewer_agents": reviewer_count(a.tier, sum(st["included"] for st in stages)), "max_rounds": 3},
        "not_checked": not_checked,
    }
    check_or_die("plan.schema.json", plan, "计划草稿")
    write_json(sess / "plan.json", plan)
    print(f"已写入计划草稿：{sess / 'plan.json'}")
    print("还需要填：artifact.title、classification.rationale、direction_check.question、"
          "各环节的 ai_checks 和 human、ground_truth；把没有真值支撑的环节改为 included: false 并写进 not_checked。")


def plan_todos(sess: Path) -> list:
    text = (sess / "plan.json").read_text(encoding="utf-8") if (sess / "plan.json").exists() else ""
    return [f"plan.json 里还有 {text.count(chr(34) + TODO + chr(34))} 处 TODO"] if f'"{TODO}"' in text else []


# ---------------------------------------------------------------- validation

def validate_session(sess: Path, ws: Path | None) -> tuple:
    errs, notes = [], []
    sid = session_id(sess)
    errs += plan_todos(sess)
    cur_hash = artifact_hash(sess)

    plan = read_json(sess / "plan.json") if (sess / "plan.json").exists() else None
    mp = read_json(sess / "map.json") if (sess / "map.json").exists() else None
    metrics = read_json(sess / "metrics.json") if (sess / "metrics.json").exists() else None

    for name, doc, schema in [("plan.json", plan, "plan.schema.json"), ("map.json", mp, "map.schema.json"),
                              ("metrics.json", metrics, "metrics.schema.json")]:
        if doc is None:
            notes.append(f"{name} 还不存在")
            continue
        errs += [f"{name}: {e}" for e in schema_errors(schema, doc)]
        if doc.get("session_id") != sid:
            errs.append(f"{name}: session_id 应为 {sid}")

    try:
        findings = read_jsonl(sess / "findings.jsonl")
        feedback = read_jsonl(sess / "feedback.jsonl")
        chg = read_jsonl(sess / "changes.jsonl") if (sess / "changes.jsonl").exists() else []
        msgs = read_jsonl(sess / "threads.jsonl") if (sess / "threads.jsonl").exists() else []
    except RHError as e:
        return [str(e)], notes
    for fname, recs, schema in [("findings.jsonl", findings, "finding.schema.json"),
                                ("feedback.jsonl", feedback, "feedback.schema.json"),
                                ("changes.jsonl", chg, "change.schema.json"),
                                ("threads.jsonl", msgs, "message.schema.json")]:
        for i, r in enumerate(recs, 1):
            errs += [f"{fname} 第 {i} 行: {e}" for e in schema_errors(schema, r)]
            if r.get("session_id") != sid:
                errs.append(f"{fname} 第 {i} 行: session_id 应为 {sid}")

    # stage ids must come from TYPE.md, and map/finding stages from the plan
    plan_stages = set()
    if plan and plan["artifact"]["hash"] != cur_hash:
        notes.append("快照在计划之后改过（plan.artifact.hash 与当前快照不同），这是再审时的正常情况")
    if plan:
        type_stages = set()
        for t in plan["classification"]["types"]:
            td = load_type(t, ws)
            if td is None:
                notes.append(f"找不到类型 {t} 的 TYPE.md，跳过环节 id 检查")
                type_stages = None
                break
            type_stages |= {s["id"] for s in td["stages"]}
        for s in plan["stages"]:
            plan_stages.add(s["id"])
            if s.get("custom"):
                # designed for this artifact: it has no TYPE.md section, so it must say what to look for
                if not s.get("criteria"):
                    errs.append(f"plan.json: 自定义环节 {s['id']} 要写 criteria（失败的样子、查什么）")
            elif type_stages is not None and s["id"] not in type_stages:
                errs.append(f"plan.json: 环节 {s['id']} 不在 TYPE.md 中（为这份产出新设计的环节要标 custom: true）")
        aspect_ids = {x["id"] for x in plan.get("aspects", [])}
        for s in plan["stages"]:
            if s.get("aspect") and s["aspect"] not in aspect_ids:
                errs.append(f"plan.json: 环节 {s['id']} 的 aspect {s['aspect']} 不在 plan.aspects 中")

    dec_ids, claim_ids, lead_ids = set(), set(), set()
    if mp:
        lead_ids = {x["id"] for x in mp.get("leads", [])}
        if len(lead_ids) != len(mp.get("leads", [])):
            errs.append("map.json: 线索 id 重复")
        plan_aspects = {x["id"] for x in plan.get("aspects", [])} if plan else set()
        for x in mp.get("leads", []):
            if x.get("aspect") and x["aspect"] not in plan_aspects:
                errs.append(f"map.json {x['id']}: 方面 {x['aspect']} 不在 plan.aspects 中")
            if mp["artifact_hash"] == artifact_hash(sess) and not anchor_found(sess, x["anchor"]):
                errs.append(f"map.json {x['id']}: 锚点在快照中找不到")
        decs = {d["id"]: d for d in mp["decisions"]}
        dec_ids = set(decs)
        claim_ids = {c["id"] for c in mp["claims"]}
        if len(decs) != len(mp["decisions"]):
            errs.append("map.json: 决策点 id 重复")
        if len(claim_ids) != len(mp["claims"]):
            errs.append("map.json: 主张 id 重复")
        for d in mp["decisions"]:
            chosen = sum(o["chosen"] for o in d["options"])
            if chosen != 1:
                errs.append(f"map.json {d['id']}: 应恰好一个已选项，现在 {chosen} 个")
            if len({o["id"] for o in d["options"]}) != len(d["options"]):
                errs.append(f"map.json {d['id']}: 选项 id 重复")
            for u in d["upstream"]:
                if u not in decs:
                    errs.append(f"map.json {d['id']}: 上游 {u} 不存在")
                elif d["id"] not in decs[u]["downstream"]:
                    errs.append(f"map.json {d['id']}: 上游 {u} 的 downstream 里没有 {d['id']}")
            for w in d["downstream"]:
                if w not in decs:
                    errs.append(f"map.json {d['id']}: 下游 {w} 不存在")
                elif d["id"] not in decs[w]["upstream"]:
                    errs.append(f"map.json {d['id']}: 下游 {w} 的 upstream 里没有 {d['id']}")
            for o in d["options"]:
                sens = o.get("sensitivity")
                if sens and sens["computed"] and not sens.get("evidence"):
                    errs.append(f"map.json {d['id']}/{o['id']}: computed 的敏感度必须附证据（重跑的命令，或已选项的结果文件）")
            if d["human_action"] in ("answer", "predict") and not d.get("prompt_to_human"):
                errs.append(f"map.json {d['id']}: {d['human_action']} 需要 prompt_to_human")
        for c in mp["claims"]:
            for x in c.get("decision_ids", []):
                if x not in decs:
                    errs.append(f"map.json {c['id']}: 决策点 {x} 不存在")
        if plan_stages:
            for obj in mp["decisions"] + mp["claims"]:
                if obj["stage"] not in plan_stages:
                    errs.append(f"map.json {obj['id']}: 环节 {obj['stage']} 不在计划中")
        if mp["artifact_hash"] == cur_hash:
            for obj in mp["decisions"] + mp["claims"]:
                if not anchor_found(sess, obj["anchor"]):
                    errs.append(f"map.json {obj['id']}: 锚点原文在快照中找不到：{obj['anchor']['exact'][:40]!r}")

    fl = latest_by_id(findings)
    revs: dict = {}
    for r in findings:
        if r["id"] in revs and r["rev"] <= revs[r["id"]]:
            errs.append(f"findings.jsonl {r['id']}: rev 没有递增")
        revs[r["id"]] = r["rev"]
    map_hash = mp["artifact_hash"] if mp else None
    if mp and mp.get("overview"):
        known_ids = set(fl) | dec_ids | claim_ids | lead_ids | {c["id"] for c in read_jsonl(sess / "changes.jsonl")}
        ov = mp["overview"]
        for part in ov["parts"]:
            for x in part.get("refs", []):
                if x not in known_ids:
                    errs.append(f"map.json 概览「{part['name']}」：引用的 {x} 不存在")
        for q in ov["key_questions"]:
            for x in q.get("refs", []):
                if x not in known_ids:
                    errs.append(f"map.json 关键问题 {q['id']}：引用的 {x} 不存在")
    if mp and mp.get("readiness"):
        known_ids = set(fl) | dec_ids | claim_ids | lead_ids | {c["id"] for c in read_jsonl(sess / "changes.jsonl")}
        for t in mp["readiness"]["tasks"]:
            for x in t.get("refs", []):
                if x not in known_ids:
                    errs.append(f"map.json 退回任务 {t['id']}：引用的 {x} 不存在")
        own_plan = bool(plan) and work_kind(load_type(plan["classification"]["types"][0], ws)) == "plan" and plan.get("user_role", "author") == "author"
        if own_plan and mp["readiness"]["tasks"]:
            # the author's next steps are the suggested path; a second list would repeat it
            errs.append("map.json：作者自己的方案不列退回任务，下一步写在建议的路径（overview.path）里")
        elif mp["readiness"]["verdict"] == "revise_first" and not mp["readiness"]["tasks"] and not own_plan:
            errs.append("map.json：判断需要先修改，却没有列出任务")
    # plans and finished work are judged on different scales and asked different kinds of questions
    td0 = load_type(plan["classification"]["types"][0], ws) if plan else None
    if mp and td0:
        work = work_kind(td0)
        for a in mp.get("assessments", []):
            if a["rating"] not in RATINGS[work]:
                errs.append(f"map.json 评价 {a['aspect']}：评级 {a['rating']} 不适用于{WORK_ZH[work]}（可用：{'、'.join(RATINGS[work])}）")
        kinds = lead_kinds(td0)
        for x in mp.get("leads", []):
            if x["kind"] not in kinds:
                errs.append(f"map.json 线索 {x['id']}：类型 {x['kind']} 不适用于这类作品（可用：{'、'.join(kinds)}）")
        ov = mp.get("overview")
        if ov and work == "plan" and not ov.get("path"):
            errs.append("map.json 概览：方案类作品要写建议的路径（overview.path）")
        if ov and ov.get("path"):
            if work != "plan":
                errs.append("map.json 概览：建议的路径只用于方案类作品")
            known_ids = set(fl) | dec_ids | claim_ids | lead_ids | {c["id"] for c in read_jsonl(sess / "changes.jsonl")}
            for i, st in enumerate(ov["path"]["steps"], 1):
                for x in st.get("refs", []):
                    if x not in known_ids:
                        errs.append(f"map.json 建议的路径第 {i} 步：引用的 {x} 不存在")
    if mp and mp.get("assessments"):
        aspect_ids = {x["id"] for x in plan.get("aspects", [])} if plan else set()
        seen = set()
        for a in mp["assessments"]:
            if a["aspect"] not in aspect_ids:
                errs.append(f"map.json 评价：方面 {a['aspect']} 不在 plan.aspects 中")
            if a["aspect"] in seen:
                errs.append(f"map.json 评价：方面 {a['aspect']} 评价了两次")
            seen.add(a["aspect"])
            for x in a.get("finding_ids", []):
                if x not in fl:
                    errs.append(f"map.json 评价 {a['aspect']}：发现 {x} 不存在")
    for f in fl.values():
        # ids refer to the map of the version the finding was raised on; older maps live in work/round-N/
        if f["artifact_hash"] == map_hash:
            for x in f.get("decision_ids", []) + ([f["origin_decision"]] if f.get("origin_decision") else []):
                if x not in dec_ids:
                    errs.append(f"findings.jsonl {f['id']}: 决策点 {x} 不存在")
            for x in f.get("claim_ids", []):
                if x not in claim_ids:
                    errs.append(f"findings.jsonl {f['id']}: 主张 {x} 不存在")
        if f.get("caused_by") and (f["caused_by"] not in fl or f["caused_by"] == f["id"]):
            errs.append(f"findings.jsonl {f['id']}: caused_by {f['caused_by']} 不存在或指向自己")
        if f["status"] == "open":
            # an open finding is shown now, so what it links to must be in the current map
            for x in f.get("decision_ids", []):
                if mp and x not in dec_ids:
                    errs.append(f"findings.jsonl {f['id']}: 决策点 {x} 已不在 map.json 中（用 edit-finding 改 decision_ids）")
        if f.get("superseded_by") and f["superseded_by"] not in fl and f["superseded_by"] not in dec_ids:
            errs.append(f"findings.jsonl {f['id']}: superseded_by {f['superseded_by']} 不存在")
        if plan_stages and f["stage"] not in plan_stages:
            errs.append(f"findings.jsonl {f['id']}: 环节 {f['stage']} 不在计划中")
        if f["artifact_hash"] == cur_hash and f["status"] == "open" and not anchor_found(sess, f["anchor"]):
            errs.append(f"findings.jsonl {f['id']}: 锚点原文在快照中找不到")
    if mp:
        for c in mp["claims"]:
            for x in c.get("finding_ids", []):
                if x not in fl:
                    errs.append(f"map.json {c['id']}: 发现 {x} 不存在")

    cl = latest_by_id(chg)
    for c in cl.values():
        for x in c.get("finding_ids", []):
            if x not in fl:
                errs.append(f"changes.jsonl {c['id']}: 发现 {x} 不存在")
    mids = {m["id"] for m in msgs}
    if len(mids) != len(msgs):
        errs.append("threads.jsonl: 消息 id 重复")
    for m in msgs:
        if m.get("reply_to") and m["reply_to"] not in mids:
            errs.append(f"threads.jsonl {m['id']}: reply_to {m['reply_to']} 不存在")
        for x in m.get("change_ids", []):
            if x not in cl:
                errs.append(f"threads.jsonl {m['id']}: 改动 {x} 不存在")

    fb_ids = set()
    for r in feedback:
        if r["id"] in fb_ids:
            errs.append(f"feedback.jsonl: id {r['id']} 重复")
        fb_ids.add(r["id"])
        t = r["target"]
        known = {"finding": fl.keys(), "decision": dec_ids, "claim": claim_ids, "change": cl.keys(),
                 "lead": lead_ids, "plan": {"plan"}, "session": {sid}}[t["type"]]
        stale = t["type"] in ("decision", "claim", "lead") and r["artifact_hash"] != map_hash
        if t["id"] not in known and not stale:
            errs.append(f"feedback.jsonl {r['id']}: 目标 {t['type']} {t['id']} 不存在")
        if r["kind"] == "choice" and mp and t["id"] in dec_ids and not stale:
            opts = {o["id"] for d in mp["decisions"] if d["id"] == t["id"] for o in d["options"]}
            if r["option_id"] not in opts:
                errs.append(f"feedback.jsonl {r['id']}: 选项 {r['option_id']} 不存在")
    for f in fl.values():
        if f.get("class_of") and f["class_of"] not in fb_ids:
            errs.append(f"findings.jsonl {f['id']}: class_of {f['class_of']} 不存在")
    return errs, notes


def cmd_validate(a) -> None:
    sess = resolve_session(a.session, a.workspace)
    errs, notes = validate_session(sess, workspace_root(a.workspace))
    for n in notes:
        print(f"提示：{n}")
    if errs:
        print(f"{len(errs)} 处问题：")
        for e in errs:
            print(f"  - {e}")
        sys.exit(3)
    print("校验通过")


def cmd_check_plan(a) -> None:
    sess = resolve_session(a.session, a.workspace)
    p = sess / "plan.json"
    if not p.exists():
        die("还没有 plan.json，先用 review-start", 4)
    plan = read_json(p)
    check_or_die("plan.schema.json", plan, "plan.json")
    if plan["status"] != "confirmed":
        die("计划还没有确认，先请用户确认", 4)
    print(f"计划已确认（第 {plan['revision']} 版，{plan['tier']}）")


# ---------------------------------------------------------------- rounds & metrics

def load_metrics(sess: Path) -> dict:
    p = sess / "metrics.json"
    if p.exists():
        return read_json(p)
    plan = read_json(sess / "plan.json")
    t = now()
    return {
        "schema_version": SCHEMA_VERSION, "id": "metrics", "created_at": t, "updated_at": t,
        "session_id": session_id(sess), "type": plan["classification"]["types"][0], "tier": plan["tier"],
        "ai": {"rounds": []},
        "findings": {"by_severity": {s: 0 for s in SEVERITIES}, "by_reaction": {}, "precision": None},
        "feedback_count": 0,
    }


def latest_reactions(feedback: list) -> dict:
    out = {}
    for r in feedback:
        if r["kind"] == "reaction" and r["target"]["type"] == "finding":
            out[r["target"]["id"]] = r["reaction"]
    return out


def recompute_metrics(sess: Path) -> dict:
    m = load_metrics(sess)
    fl = latest_by_id(read_jsonl(sess / "findings.jsonl"))
    fb = read_jsonl(sess / "feedback.jsonl")
    live = [f for f in fl.values() if f["status"] not in ("superseded", "invalidated")]
    m["findings"]["by_severity"] = {s: sum(f["severity"] == s for f in live) for s in SEVERITIES}
    rx = latest_reactions(fb)
    by = {k: 0 for k in REACTIONS}
    for f in live:
        if f["id"] in rx:
            by[rx[f["id"]]] += 1
    by["no_response"] = sum(f["id"] not in rx for f in live)
    m["findings"]["by_reaction"] = {k: v for k, v in by.items() if v}
    responded = sum(v for k, v in by.items() if k != "no_response")
    m["findings"]["precision"] = round(1 - by["ai_wrong"] / responded, 3) if responded else None
    m["feedback_count"] = len(fb)
    so = [r for r in fb if r["kind"] == "signoff"]
    if so and so[-1]["signoff"].get("quality_score"):
        m.setdefault("quality", {})["at_signoff"] = so[-1]["signoff"]["quality_score"]
    m["updated_at"] = now()
    check_or_die("metrics.schema.json", m, "metrics.json")
    write_json(sess / "metrics.json", m)
    return m


def cmd_round_start(a) -> None:
    sess = resolve_session(a.session, a.workspace)
    plan = read_json(sess / "plan.json") if (sess / "plan.json").exists() else None
    if not plan or plan.get("status") != "confirmed":
        die("计划还没有确认，先用 review-start 并请用户确认", 4)
    m = load_metrics(sess)
    if current_round(sess) is not None:
        die(f"第 {current_round(sess)} 轮还没有结束（先 round-end）")
    n = len(m["ai"]["rounds"]) + 1
    (sess / "work" / f"round-{n}").mkdir(parents=True, exist_ok=True)
    if n > 1 and (sess / "map.json").exists():
        # keep the previous round's map: older findings and feedback refer to its ids
        shutil.copy2(sess / "map.json", sess / "work" / f"round-{n - 1}" / "map.json")
    state = sess / "work" / "round-open.json"
    write_json(state, {"round": n, "started_at": now()})
    m["updated_at"] = now()
    write_json(sess / "metrics.json", m)
    print(f"第 {n} 轮开始。草稿目录：work/round-{n}/")


def current_round(sess: Path) -> int | None:
    st = sess / "work" / "round-open.json"
    return read_json(st)["round"] if st.exists() else None


def cmd_round_end(a) -> None:
    sess = resolve_session(a.session, a.workspace)
    st = sess / "work" / "round-open.json"
    if not st.exists():
        die("没有进行中的一轮（先 round-start）")
    o = read_json(st)
    fl = latest_by_id(read_jsonl(sess / "findings.jsonl"))
    kept = sum(1 for f in fl.values() if f["round"] == o["round"])
    m = load_metrics(sess)
    rec = {
        "round": o["round"], "started_at": o["started_at"], "ended_at": now(),
        "reviewer_agents": a.agents, "candidates": a.candidates, "kept": kept,
        "dropped_no_evidence": a.dropped_no_evidence, "dropped_by_verifier": a.dropped_by_verifier,
        "dropped_duplicate": a.dropped_duplicate,
    }
    accounted = kept + a.dropped_no_evidence + a.dropped_by_verifier + a.dropped_duplicate
    if accounted != a.candidates:
        print(f"提示：候选 {a.candidates} 条，但保留+丢弃 = {accounted}（S3 超额等未计入）", file=sys.stderr)
    m["ai"]["rounds"].append(rec)
    write_json(sess / "metrics.json", m)
    st.unlink()
    recompute_metrics(sess)
    print(f"第 {o['round']} 轮结束：候选 {a.candidates}，保留 {kept}")


# ---------------------------------------------------------------- findings

def fingerprint(stage: str, anchor: dict, kind: str) -> str:
    return f"{stage}|{norm_ws(anchor['exact'])}|{kind}"


def next_id(prefix: str, existing, width: int) -> str:
    nums = [int(x[len(prefix):]) for x in existing if re.fullmatch(prefix + r"\d+", x)]
    return f"{prefix}{(max(nums) + 1) if nums else 1:0{width}d}"


def cmd_add_finding(a) -> None:
    sess = resolve_session(a.session, a.workspace)
    rnd = a.round or current_round(sess)
    if rnd is None:
        die("没有进行中的一轮，用 --round 指定")
    try:
        cand = json.loads(sys.stdin.read())
    except json.JSONDecodeError as e:
        die(f"标准输入不是合法 JSON：{e}")
    cand = {k: v for k, v in cand.items() if v is not None}
    if cand.get("origin") != "inherited" and "origin_decision" in cand:
        print("提示：origin 不是 inherited，忽略 origin_decision（相关决策点请放在 decision_ids）", file=sys.stderr)
        cand.setdefault("decision_ids", [])
        if cand["origin_decision"] not in cand["decision_ids"]:
            cand["decision_ids"].append(cand["origin_decision"])
        del cand["origin_decision"]
    extra = set(cand) - CANDIDATE_FIELDS
    if extra:
        print(f"提示：忽略字段 {sorted(extra)}", file=sys.stderr)
    if not cand.get("evidence"):
        die("没有证据，丢弃（计入 dropped_no_evidence）", 2)
    for k in ("stage", "anchor", "problem_kind"):
        if not cand.get(k):
            die(f"缺少 {k}", 3)
    if not anchor_found(sess, cand["anchor"]):
        die(f"锚点原文在快照中找不到：{cand['anchor']['exact'][:60]!r}", 3)
    for ev in cand["evidence"]:
        if ev.get("kind") == "file" and "excerpt" in ev and "line_start" in ev:
            err = check_file_evidence(ev, sess)
            if err:
                die(f"证据核对失败：{err}", 3)

    path = sess / "findings.jsonl"
    fl = latest_by_id(read_jsonl(path))
    fp = fingerprint(cand["stage"], cand["anchor"], cand["problem_kind"])
    for f in fl.values():
        if f["fingerprint"] == fp:
            print(f"duplicate：与 {f['id']} 相同（{f['status']}），不写入")
            sys.exit(5)
    if cand.get("severity") == "S3":
        n_s3 = sum(1 for f in fl.values() if f["severity"] == "S3" and f["round"] == rnd)
        if n_s3 >= S3_CAP:
            print(f"S3 本轮已有 {S3_CAP} 条，不写入")
            sys.exit(6)

    rec = {
        "schema_version": SCHEMA_VERSION,
        "id": next_id("F", fl.keys(), 3),
        "created_at": now(),
        "session_id": session_id(sess),
        "rev": 1,
        "round": rnd,
        "artifact_hash": artifact_hash(sess),
    }
    for k in ("severity", "stage", "title", "detail", "evidence", "anchor", "origin", "origin_decision",
              "decision_ids", "claim_ids", "rule_ref", "class_of", "suggested_fix", "note"):
        if k in cand:
            rec[k] = cand[k]
    if a.caused_by:
        if a.caused_by not in fl:
            die(f"caused_by {a.caused_by} 不存在", 3)
        rec["caused_by"] = a.caused_by
    rec["fingerprint"] = fp
    rec["verification"] = {"finder": a.finder, "verifier": a.verifier, "method": a.method}
    rec["status"] = "open"
    if rec.get("severity") == "S3":
        rec.setdefault("suggested_fix", {"text": "（表述问题，只列不修）"})
        rec["suggested_fix"].setdefault("apply_by_default", False)
    check_or_die("finding.schema.json", rec, "发现")
    append_jsonl(path, rec)
    print(rec["id"])


def cmd_check_candidates(a) -> None:
    """Lint a merged candidates file before it goes to the verifier."""
    sess = resolve_session(a.session, a.workspace)
    v = validator("evidence")
    bad = 0
    f = Path(a.file).expanduser()
    if not f.is_absolute() and not f.exists():
        f = sess / f  # the skills give paths relative to the session
    for i, line in enumerate(f.read_text(encoding="utf-8").splitlines(), 1):
        if not line.strip():
            continue
        c = json.loads(line)
        cid = c.get("cid", f"第 {i} 行")
        issues = []
        evs = c.get("evidence") or []
        if not evs:
            issues.append("没有证据")
        for j, ev in enumerate(evs):
            errs = list(v.iter_errors(ev))
            if errs:
                issues.append(f"evidence[{j}] 格式不对：{errs[0].message[:120]}")
            elif ev["kind"] == "file":
                e = check_file_evidence(ev, sess)
                if e:
                    issues.append(f"evidence[{j}]：{e}")
        if not c.get("anchor") or not anchor_found(sess, c["anchor"]):
            issues.append("锚点原文在快照中找不到")
        for k in ("stage", "severity", "title", "problem_kind"):
            if not c.get(k):
                issues.append(f"缺少 {k}")
        if issues:
            bad += 1
            print(f"{cid}: " + "；".join(issues))
    print(f"检查完毕：{bad} 条有问题" if bad else "全部候选格式正确")


def cmd_set_finding_status(a) -> None:
    sess = resolve_session(a.session, a.workspace)
    path = sess / "findings.jsonl"
    fl = latest_by_id(read_jsonl(path))
    if a.finding not in fl:
        die(f"没有发现 {a.finding}")
    rec = dict(fl[a.finding])
    rec["rev"] += 1
    rec["created_at"] = now()
    rec["status"] = a.status
    if a.superseded_by:
        rec["superseded_by"] = a.superseded_by
    check_or_die("finding.schema.json", rec, "发现")
    append_jsonl(path, rec)
    if (sess / "metrics.json").exists():
        recompute_metrics(sess)
    print(f"{a.finding} → {a.status}（rev {rec['rev']}）")


def cmd_link_finding(a) -> None:
    sess = resolve_session(a.session, a.workspace)
    path = sess / "findings.jsonl"
    fl = latest_by_id(read_jsonl(path))
    for x in (a.finding, a.caused_by):
        if x not in fl:
            die(f"没有发现 {x}")
    if a.finding == a.caused_by or fl[a.caused_by].get("caused_by") == a.finding:
        die("不能形成循环")
    rec = dict(fl[a.finding], rev=fl[a.finding]["rev"] + 1, created_at=now(), caused_by=a.caused_by)
    check_or_die("finding.schema.json", rec, "发现")
    append_jsonl(path, rec)
    print(f"{a.finding} ← 根源 {a.caused_by}")


# ---------------------------------------------------------------- feedback

def unresolved(sess: Path) -> dict:
    fl = latest_by_id(read_jsonl(sess / "findings.jsonl"))
    rx = latest_reactions(read_jsonl(sess / "feedback.jsonl"))
    open_f = [f for f in fl.values() if f["status"] == "open" and f["id"] not in rx]
    mp = read_json(sess / "map.json") if (sess / "map.json").exists() else {"decisions": [], "claims": []}
    return {
        "findings": sorted(open_f, key=lambda f: (f["severity"], f["id"])),
        "ai_default": [d for d in mp["decisions"] if d["status"] == "ai_default"],
        "awaiting": [d for d in mp["decisions"] if d["status"] in ("awaiting_human", "invalidated")],
        "unsourced": [c for c in mp["claims"] if c["source_type"] == "unsourced"],
    }


def make_feedback(sess: Path, kind: str, target: str | None = None, *, reaction=None, fix_by=None,
                  value=None, option=None, text=None, outcome=None, quality=None, source="chat",
                  note=None, scope=None) -> dict:
    """Build, check and append one feedback record. Shared by the CLI and the UI server."""
    sid = session_id(sess)
    path = sess / "feedback.jsonl"
    fb = read_jsonl(path)
    rec = {
        "schema_version": SCHEMA_VERSION, "id": next_id("FB", [r["id"] for r in fb], 4),
        "created_at": now(), "session_id": sid, "artifact_hash": artifact_hash(sess),
        "kind": kind,
    }
    tgt = target
    if kind == "signoff" or (kind in ("suggestion", "missed") and not target):
        tgt = sid
    elif kind == "plan_confirm":
        tgt = "plan"
    if not tgt:
        raise RHError("需要 --target")

    if tgt == sid:
        rec["target"] = {"type": "session", "id": sid}
    elif tgt == "plan":
        rec["target"] = {"type": "plan", "id": "plan"}
    elif tgt.startswith("F") and not tgt.startswith("FB"):
        fl = latest_by_id(read_jsonl(sess / "findings.jsonl"))
        if tgt not in fl:
            raise RHError(f"没有发现 {tgt}")
        rec["target"] = {"type": "finding", "id": tgt}
        rec["anchor"] = fl[tgt]["anchor"]
    elif tgt.startswith("R"):
        cs = latest_by_id(read_jsonl(sess / "changes.jsonl"))
        if tgt not in cs:
            raise RHError(f"没有改动 {tgt}")
        rec["target"] = {"type": "change", "id": tgt}
        rec["anchor"] = cs[tgt]["anchor"]
    elif tgt[0] in "DCL":
        mp = read_json(sess / "map.json")
        objs = {o["id"]: o for o in mp["decisions"] + mp["claims"] + mp.get("leads", [])}
        if tgt not in objs:
            raise RHError(f"map.json 里没有 {tgt}")
        rec["target"] = {"type": {"D": "decision", "C": "claim", "L": "lead"}[tgt[0]], "id": tgt}
        rec["anchor"] = objs[tgt]["anchor"]
    else:
        raise RHError(f"无法识别的目标：{tgt}")

    for k, v in (("reaction", reaction), ("fix_by", fix_by), ("value", value), ("text", text)):
        if v:
            rec[k] = v
    if option:
        rec["option_id"] = option
    if kind == "suggestion":
        rec["scope"] = scope or "this_type"
    if kind == "plan_confirm":
        rec["plan_revision"] = read_json(sess / "plan.json")["revision"]
    if kind == "signoff":
        if not outcome:
            raise RHError("签收需要 --outcome deliver|another_round")
        u = unresolved(sess)
        rec["signoff"] = {
            "outcome": outcome,
            "acknowledged": [f["id"] for f in u["findings"]] + [d["id"] for d in u["ai_default"]]
                            + [c["id"] for c in u["unsourced"]],
        }
        if quality:
            rec["signoff"]["quality_score"] = quality
    rec["source"] = source
    if note:
        rec["note"] = note
    errs = schema_errors("feedback.schema.json", rec)
    if errs:
        raise RHError("反馈不符合 schema：" + "；".join(errs[:5]))
    append_jsonl(path, rec)
    if (sess / "metrics.json").exists():
        recompute_metrics(sess)
    return rec


def cmd_add_feedback(a) -> None:
    sess = resolve_session(a.session, a.workspace)
    rec = make_feedback(sess, a.kind, a.target, reaction=a.reaction, fix_by=a.fix_by, value=a.value,
                        option=a.option, text=a.text, outcome=a.outcome, quality=a.quality,
                        source=a.source, note=a.note, scope=a.scope)
    print(rec["id"])


def add_human_time(sess: Path, minutes: float, stage: str | None = None) -> dict:
    """Accumulate review time measured by the UI into metrics.json."""
    m = load_metrics(sess)
    h = m.get("human") or {}
    if h.get("measured_by") not in (None, "ui_timer"):
        h = {}
    h["measured_by"] = "ui_timer"
    h["minutes"] = round(h.get("minutes", 0) + minutes, 2)
    if stage:
        bs = h.setdefault("by_stage_minutes", {})
        bs[stage] = round(bs.get(stage, 0) + minutes, 2)
    m["human"] = h
    m["updated_at"] = now()
    check_or_die("metrics.schema.json", m, "metrics.json")
    write_json(sess / "metrics.json", m)
    return m


# ---------------------------------------------------------------- apply

CLOSING = ("ai_wrong", "intentional", "doesnt_matter")


def latest_feedback(sess: Path, map_hash: str | None) -> dict:
    """Latest feedback per (target id, kind); decision/claim feedback only for the current map."""
    out: dict = {}
    for r in read_jsonl(sess / "feedback.jsonl"):
        t = r.get("target", {})
        if t.get("type") in ("decision", "claim") and r["artifact_hash"] != map_hash:
            continue
        out.setdefault(t.get("id"), {})[r["kind"]] = r
    return out


def build_apply_plan(sess: Path) -> dict:
    mp = read_json(sess / "map.json")
    fb = latest_feedback(sess, mp["artifact_hash"])
    fl = latest_by_id(read_jsonl(sess / "findings.jsonl"))
    u = unresolved(sess)
    plan = {"fix": [], "human_fix": [], "close": [], "untouched_s3": [],
            "decisions": [], "invalidate": [], "answers": [], "wrong": [], "revert": [], "discuss": []}
    for c in applied_changes(sess):
        r = fb.get(c["id"], {}).get("confirm")
        if r and r["value"] == "wrong":
            plan["revert"].append({"id": c["id"], "summary": c["summary"], "path": c["path"],
                                   "before": c["before"], "after": c["after"], "note": r.get("note")})
    plan["discuss"] = [{"id": m["id"], "text": m["text"], "target": m.get("target")} for m in inbox(sess)]
    for f in sorted(fl.values(), key=lambda x: x["id"]):
        if f["status"] != "open":
            continue
        r = fb.get(f["id"], {}).get("reaction")
        if not r:
            if f["severity"] == "S3":
                plan["untouched_s3"].append(f["id"])
            continue
        item = {"id": f["id"], "severity": f["severity"], "title": f["title"], "anchor": f["anchor"],
                "suggested_fix": f.get("suggested_fix"), "note": r.get("note")}
        if r["reaction"] in CLOSING:
            plan["close"].append({**item, "reaction": r["reaction"]})
        elif r["reaction"] == "fixed" and r.get("fix_by") == "human":
            plan["human_fix"].append(item)
        else:
            if r["reaction"] == "class_of_problem":
                item["instances"] = [x["id"] for x in fl.values() if x.get("class_of") == f["id"] and x["status"] == "open"]
            plan["fix"].append(item)
    by_id = {d["id"]: d for d in mp["decisions"]}

    def downstream(did, seen):
        for x in by_id[did]["downstream"]:
            if x not in seen:
                seen.add(x)
                downstream(x, seen)
        return seen

    changed = set()
    for d in mp["decisions"]:
        l = fb.get(d["id"], {})
        chosen = next((o["id"] for o in d["options"] if o["chosen"]), None)
        new = None
        if "choice" in l:
            opt = l["choice"]["option_id"]
            if opt != chosen:
                label = next(o["label"] for o in d["options"] if o["id"] == opt)
                plan["decisions"].append({"id": d["id"], "title": d["title"], "from": chosen, "to": opt,
                                          "to_label": label, "anchor": d["anchor"], "note": l["choice"].get("note")})
                new = "changed"
                changed.add(d["id"])
            else:
                new = "confirmed"
        if "answer" in l:
            plan["answers"].append({"id": d["id"], "question": d.get("prompt_to_human") or d["question"],
                                    "answer": l["answer"]["text"], "anchor": d["anchor"]})
            new = new or "confirmed"
        if "prediction" in l:
            new = new or "confirmed"
        if "confirm" in l:
            if l["confirm"]["value"] == "wrong":
                plan["wrong"].append({"id": d["id"], "text": d["title"], "anchor": d["anchor"], "note": l["confirm"].get("note")})
            new = new or "confirmed"
        if new and d["status"] in ("awaiting_human", "ai_default", "invalidated"):
            d["_new"] = new
    for did in sorted(changed):
        for x in sorted(downstream(did, set())):
            if x not in changed and x not in plan["invalidate"]:
                plan["invalidate"].append(x)
    for c in mp["claims"]:
        l = fb.get(c["id"], {})
        if "confirm" in l and l["confirm"]["value"] == "wrong":
            plan["wrong"].append({"id": c["id"], "text": c["text"], "anchor": c["anchor"], "note": l["confirm"].get("note")})
    plan["_map"] = mp
    return plan


def render_apply_plan(plan: dict) -> str:
    L = []
    def sec(title, rows):
        if rows:
            L.append(f"## {title}")
            L.extend(rows)
            L.append("")
    sec("撤销 AI 自改（把 after 换回 before，然后 RH set-change-status <会话> <R 编号> reverted）",
        [f"- {x['id']} {x['summary']}" + (f"\n  用户补充：{x['note']}" if x.get("note") else "") for x in plan["revert"]])
    sec("还没回复的讨论（先用 RH reply 回复）",
        [f"- {x['id']}" + (f"（{x['target']['id']}）" if x.get("target") else "") + f"：{x['text']}" for x in plan["discuss"]])
    sec("要改（AI 改）", [f"- {x['id']} [{x['severity']}] {x['title']}"
                         + (f"（连同同类：{', '.join(x['instances'])}）" if x.get("instances") else "")
                         + (f"\n  建议：{x['suggested_fix']['text']}" if x.get("suggested_fix") else "")
                         + (f"\n  用户补充：{x['note']}" if x.get("note") else "") for x in plan["fix"]])
    sec("改选的决策点（修改相关段落）", [f"- {x['id']} {x['title']}：{x['from']} → {x['to']}「{x['to_label']}」"
                                 + (f"\n  用户补充：{x['note']}" if x.get("note") else "") for x in plan["decisions"]])
    sec("用户的回答（按回答修改）", [f"- {x['id']} 问：{x['question']}\n  答：{x['answer']}" for x in plan["answers"]])
    sec("用户标为有问题", [f"- {x['id']} {x['text']}" + (f"\n  用户补充：{x['note']}" if x.get("note") else "") for x in plan["wrong"]])
    sec("下游失效，再审时重看", [f"- {x}" for x in plan["invalidate"]])
    sec("用户自己改（不动）", [f"- {x['id']} {x['title']}" for x in plan["human_fix"]])
    sec("不改，关闭", [f"- {x['id']} {x['title']}（{REACTION_ZH[x['reaction']]}）" for x in plan["close"]])
    sec("S3 没有回应，只列不修", [f"- {x}" for x in plan["untouched_s3"]])
    if not L:
        L.append("没有需要修改的内容。")
    return "\n".join(L).rstrip() + "\n"


def cmd_apply_plan(a) -> None:
    sess = resolve_session(a.session, a.workspace)
    if current_round(sess) is not None:
        raise RHError(f"第 {current_round(sess)} 轮还没有结束")
    plan = build_apply_plan(sess)
    mp = plan.pop("_map")
    n = len(load_metrics(sess)["ai"]["rounds"]) if (sess / "plan.json").exists() else 0
    out_dir = sess / "work" / f"apply-{n}"
    out_dir.mkdir(parents=True, exist_ok=True)
    write_json(out_dir / "todo.json", plan)
    text = render_apply_plan(plan)
    (out_dir / "todo.md").write_text(text, encoding="utf-8")
    print(text, end="")
    print(f"\n清单：{out_dir.relative_to(sess)}/todo.json")
    if not a.write:
        return
    path = sess / "findings.jsonl"
    fl = latest_by_id(read_jsonl(path))
    for x in plan["close"]:
        rec = dict(fl[x["id"]], rev=fl[x["id"]]["rev"] + 1, created_at=now(), status="closed")
        check_or_die("finding.schema.json", rec, "发现")
        append_jsonl(path, rec)
    inval = set(plan["invalidate"])
    for d in mp["decisions"]:
        new = d.pop("_new", None)
        if d["id"] in inval:
            d["status"] = "invalidated"
        elif new:
            d["status"] = new
    check_or_die("map.schema.json", mp, "map.json")
    write_json(sess / "map.json", mp)
    if (sess / "metrics.json").exists():
        recompute_metrics(sess)
    print(f"已关闭 {len(plan['close'])} 条发现；决策点状态已更新"
          + (f"，{len(inval)} 个下游决策点标为 invalidated" if inval else "") + "。")


# ---------------------------------------------------------------- AI revise loop

MAX_REVIEW_ROUNDS = 3


def baseline_path(sess: Path, snapshot: str) -> Path:
    return sess / "baseline" / snapshot.split("/", 1)[1]


def ensure_baseline(sess: Path) -> None:
    """Sessions made before the revise loop existed have no baseline/: the snapshot is the baseline."""
    for f in read_json(sess / "sources.json")["files"]:
        b = baseline_path(sess, f["snapshot"])
        if not b.exists():
            b.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(sess / f["snapshot"], b)


def changes(sess: Path) -> dict:
    return latest_by_id(read_jsonl(sess / "changes.jsonl")) if (sess / "changes.jsonl").exists() else {}


def applied_changes(sess: Path) -> list:
    return [c for c in changes(sess).values() if c.get("status", "applied") == "applied"]


def set_change_status(sess: Path, cid: str, status: str) -> dict:
    cs = changes(sess)
    if cid not in cs:
        raise RHError(f"没有改动 {cid}")
    rec = dict(cs[cid], status=status, created_at=now())
    check_or_die("change.schema.json", rec, "改动")
    append_jsonl(sess / "changes.jsonl", rec)
    return rec


def cmd_set_change_status(a) -> None:
    sess = resolve_session(a.session, a.workspace)
    c = changes(sess).get(a.change)
    if not c:
        die(f"没有改动 {a.change}")
    if a.status == "reverted":
        text = (sess / c["path"]).read_text(encoding="utf-8")
        if c["after"] and c["after"] in text:
            die(f"{a.change} 改后的文字还在快照里，先把它换回改前的文字再标 reverted", 3)
    set_change_status(sess, a.change, a.status)
    print(f"{a.change} → {a.status}")


def finished_rounds(sess: Path) -> list:
    m = read_json(sess / "metrics.json") if (sess / "metrics.json").exists() else {"ai": {"rounds": []}}
    return [r["round"] for r in m["ai"]["rounds"]]


def open_serious(sess: Path) -> list:
    """Open findings the AI should still fix itself: S1/S2, and anything raised after round 1 at any severity,
    since later rounds only look at the AI's own edits and an AI-introduced error must not reach the person."""
    return [f for f in unresolved(sess)["findings"]
            if (f["severity"] in ("S1", "S2") or f["round"] > 1) and not f.get("handoff_reason")]


def loop_next(sess: Path) -> str:
    """After a review round: 'revise' (AI fixes its own findings, then reviews again) or 'handoff' (to the person)."""
    done = finished_rounds(sess)
    if current_round(sess) or (sess / "work" / "revise-open.json").exists() or not done:
        return "busy"
    if len(done) >= MAX_REVIEW_ROUNDS or not open_serious(sess):
        return "handoff"
    plan = read_json(sess / "plan.json")
    if plan.get("user_role") == "evaluator" or plan.get("revision_policy") == "none":
        return "handoff"  # someone else's finished work, or creative work: the AI reviews it, it does not rewrite it
    return "revise"


def cmd_revise_begin(a) -> None:
    sess = resolve_session(a.session, a.workspace)
    state = sess / "work" / "revise-open.json"
    if state.exists():
        die("上一次自改还没有结束（先 revise-end）")
    if current_round(sess):
        die("还有一轮自审没有结束（先 round-end）")
    done = finished_rounds(sess)
    if not done:
        die("还没有完成任何一轮自审")
    ensure_baseline(sess)
    n = done[-1]
    before = sess / "work" / f"round-{n}" / "before-revise"
    if before.exists():
        shutil.rmtree(before)
    shutil.copytree(sess / "artifact", before)
    write_json(state, {"round": n, "started_at": now(), "hash": artifact_hash(sess)})
    todo = unresolved(sess)["findings"]
    print(f"开始自改（针对第 {n} 轮的发现）。直接编辑 {sess / 'artifact'} 下的文件，每处改动用 add-change 记录。")
    print(f"待处理的发现 {len(todo)} 条：" + "、".join(f"{f['id']}({f['severity']})" for f in todo))


def cmd_add_change(a) -> None:
    sess = resolve_session(a.session, a.workspace)
    state = sess / "work" / "revise-open.json"
    try:
        c = json.loads(sys.stdin.read())
    except json.JSONDecodeError as e:
        die(f"标准输入不是 JSON：{e}", 3)
    if state.exists():
        rnd = read_json(state)["round"]
    elif c.get("message_ids"):
        done = finished_rounds(sess)
        rnd = done[-1] if done else 1
    else:
        die("没有进行中的自改（先 revise-begin）；讨论中做的改动要带 message_ids", 3)
    for k in ("summary", "why", "path"):
        if not c.get(k):
            die(f"缺少 {k}", 3)
    after, before = c.get("after", ""), c.get("before", "")
    if not after and not before:
        die("before 和 after 不能都为空", 3)
    exact = after or c.get("anchor_exact")
    if not exact:
        die("删除类改动要给 anchor_exact：删除位置前面的一段原文", 3)
    anchor = make_anchor(sess, exact, c["path"])
    fl = latest_by_id(read_jsonl(sess / "findings.jsonl"))
    for fid in c.get("finding_ids", []):
        if fid not in fl:
            die(f"没有发现 {fid}", 3)
    cs = changes(sess)
    rec = {"schema_version": SCHEMA_VERSION, "id": next_id("R", cs.keys(), 3), "created_at": now(),
           "session_id": session_id(sess), "round": rnd, "summary": c["summary"], "why": c["why"],
           "path": c["path"], "before": before, "after": after, "anchor": anchor, "status": "applied"}
    for k in ("finding_ids", "decision_ids", "message_ids", "evidence", "project_files"):
        if c.get(k):
            rec[k] = c[k]
    check_or_die("change.schema.json", rec, "改动")
    append_jsonl(sess / "changes.jsonl", rec)
    print(rec["id"])


def locate_edit(text: str, before: str, prefix: str) -> int:
    """Where an edit happened: when `before` occurs more than once, the occurrence the recorded context precedes."""
    hits = [m.start() for m in re.finditer(re.escape(before), text)]
    if len(hits) > 1 and prefix:
        tail = norm_ws(prefix)[-24:]
        for i in hits:
            if norm_ws(text[max(0, i - 200):i]).endswith(tail):
                return i
    return hits[0]


def unrecorded_edits(sess: Path, old_dir: Path, recs: list) -> list:
    """Lines that differ between artifact/ and old_dir once the recorded edits are replayed on old_dir."""
    out = []
    for f in artifact_files(sess):
        rel = f.relative_to(sess / "artifact").as_posix()
        old_f = old_dir / rel
        old_text = old_f.read_text(encoding="utf-8", errors="replace") if old_f.exists() else ""
        new_text = f.read_text(encoding="utf-8", errors="replace")
        mine = [c for c in recs if c["path"] == f"artifact/{rel}" and c.get("status") != "reverted"]
        replayed = old_text
        for c in mine:
            if c["before"] and c["before"] in replayed:
                i = locate_edit(replayed, c["before"], (c.get("anchor") or {}).get("prefix", ""))
                replayed = replayed[:i] + c["after"] + replayed[i + len(c["before"]):]
        for line in difflib.unified_diff(replayed.splitlines(), new_text.splitlines(), n=0, lineterm=""):
            if line.startswith(("+++", "---", "@@")) or not line[1:].strip():
                continue
            out.append(f"artifact/{rel}: {line[:100]}")
    return out


def stale_anchors(sess: Path) -> list:
    return [f["id"] for f in unresolved(sess)["findings"] if not anchor_found(sess, f["anchor"])]


def cmd_revise_end(a) -> None:
    sess = resolve_session(a.session, a.workspace)
    state = sess / "work" / "revise-open.json"
    if not state.exists():
        die("没有进行中的自改")
    o = read_json(state)
    recs = [c for c in applied_changes(sess) if c["round"] == o["round"]]
    missing = unrecorded_edits(sess, sess / "work" / f"round-{o['round']}" / "before-revise", recs)
    if missing:
        die("这些改动没有记录（每处改动都要 add-change）：\n  " + "\n  ".join(missing[:20]), 3)
    state.unlink()
    print(f"自改结束：{len(recs)} 处改动，artifact_hash {o['hash']} -> {artifact_hash(sess)}")
    st = stale_anchors(sess)
    if st:
        print("这些未处理发现的锚点在新版本里找不到了，下一轮核实时修好的标 fixed，没修好的用 reanchor 更新：" + "、".join(st))


def cmd_handoff(a) -> None:
    sess = resolve_session(a.session, a.workspace)
    path = sess / "findings.jsonl"
    fl = latest_by_id(read_jsonl(path))
    if a.finding not in fl:
        die(f"没有发现 {a.finding}")
    rec = dict(fl[a.finding], rev=fl[a.finding]["rev"] + 1, created_at=now(), handoff_reason=a.reason)
    if a.note:
        rec["note"] = a.note
    check_or_die("finding.schema.json", rec, "发现")
    append_jsonl(path, rec)
    print(f"{a.finding} 交给作者：{a.reason}")


EDITABLE = ("title", "detail", "suggested_fix", "note", "handoff_reason", "severity", "decision_ids")


def cmd_edit_finding(a) -> None:
    """Rewrite how a finding reads (wording, note type, severity, linked decisions); a new revision, the history stays."""
    sess = resolve_session(a.session, a.workspace)
    path = sess / "findings.jsonl"
    fl = latest_by_id(read_jsonl(path))
    if a.finding not in fl:
        die(f"没有发现 {a.finding}")
    try:
        patch = json.loads(sys.stdin.read())
    except json.JSONDecodeError as e:
        die(f"标准输入不是合法 JSON：{e}")
    bad = set(patch) - set(EDITABLE)
    if bad:
        die(f"只能改这些字段：{'、'.join(EDITABLE)}；不能改：{'、'.join(sorted(bad))}")
    rec = dict(fl[a.finding], rev=fl[a.finding]["rev"] + 1, created_at=now(), **patch)
    rec = {k: v for k, v in rec.items() if v is not None}
    check_or_die("finding.schema.json", rec, "发现")
    append_jsonl(path, rec)
    print(f"{a.finding} 第 {rec['rev']} 版")


def cmd_reanchor(a) -> None:
    sess = resolve_session(a.session, a.workspace)
    path = sess / "findings.jsonl"
    fl = latest_by_id(read_jsonl(path))
    if a.finding not in fl:
        die(f"没有发现 {a.finding}")
    rec = dict(fl[a.finding], rev=fl[a.finding]["rev"] + 1, created_at=now(),
               anchor=make_anchor(sess, a.exact))
    check_or_die("finding.schema.json", rec, "发现")
    append_jsonl(path, rec)
    print(f"{a.finding} 锚点已更新（rev {rec['rev']}）")


def artifact_diff(sess: Path) -> str:
    ensure_baseline(sess)
    out = []
    for f in read_json(sess / "sources.json")["files"]:
        old = baseline_path(sess, f["snapshot"]).read_text(encoding="utf-8", errors="replace").splitlines()
        new = (sess / f["snapshot"]).read_text(encoding="utf-8", errors="replace").splitlines()
        out += difflib.unified_diff(old, new, f["original"], f["snapshot"], lineterm="")
    return "\n".join(out)


def cmd_diff(a) -> None:
    sess = resolve_session(a.session, a.workspace)
    d = artifact_diff(sess)
    print(d or "和原版没有差别")


def cmd_writeback(a) -> None:
    sess = resolve_session(a.session, a.workspace)
    ensure_baseline(sess)
    files = read_json(sess / "sources.json")["files"]
    conflicts = [f["original"] for f in files if Path(f["original"]).is_file()
                 and Path(f["original"]).read_bytes() != baseline_path(sess, f["snapshot"]).read_bytes()]
    if conflicts:
        die("原文件在会话开始后被别人改过，直接写回会覆盖那些改动：" + "、".join(conflicts)
            + "。先和用户商量（可以把差异手工合并）。", 3)
    if not a.yes:
        die("写回会覆盖原文件。必须先在对话里得到用户的明确同意，然后加 --yes", 3)
    for f in files:
        shutil.copy2(sess / f["snapshot"], f["original"])
        shutil.copy2(sess / f["snapshot"], baseline_path(sess, f["snapshot"]))
    print("已写回：" + "、".join(f["original"] for f in files))


# ---------------------------------------------------------------- discussion

def threads(sess: Path) -> list:
    return read_jsonl(sess / "threads.jsonl") if (sess / "threads.jsonl").exists() else []


def target_ref(sess: Path, tgt: str) -> dict:
    if tgt.startswith("R"):
        if tgt not in changes(sess):
            raise RHError(f"没有改动 {tgt}")
        return {"type": "change", "id": tgt}
    if tgt.startswith("F"):
        if tgt not in latest_by_id(read_jsonl(sess / "findings.jsonl")):
            raise RHError(f"没有发现 {tgt}")
        return {"type": "finding", "id": tgt}
    if tgt[:1] in ("D", "C", "L"):
        mp = read_json(sess / "map.json") if (sess / "map.json").exists() else {"decisions": [], "claims": []}
        if tgt not in {o["id"] for o in mp["decisions"] + mp["claims"] + mp.get("leads", [])}:
            raise RHError(f"map.json 里没有 {tgt}")
        return {"type": {"D": "decision", "C": "claim", "L": "lead"}[tgt[0]], "id": tgt}
    raise RHError(f"无法识别的目标：{tgt}")


def add_message(sess: Path, author: str, text: str, target: str | None = None, reply_to: str | None = None,
                source: str = "chat", change_ids: list | None = None) -> dict:
    ms = threads(sess)
    rec = {"schema_version": SCHEMA_VERSION, "id": next_id("M", [m["id"] for m in ms], 4), "created_at": now(),
           "session_id": session_id(sess), "author": author, "text": text, "source": source,
           "artifact_hash": artifact_hash(sess)}
    if reply_to:
        q = {m["id"]: m for m in ms}.get(reply_to)
        if not q:
            raise RHError(f"没有消息 {reply_to}")
        if "target" in q and not target:
            rec["target"] = q["target"]
        rec["reply_to"] = reply_to
    if target:
        rec["target"] = target_ref(sess, target)
    if change_ids:
        rec["change_ids"] = change_ids
    errs = schema_errors("message.schema.json", rec)
    if errs:
        raise RHError("消息不符合 schema：" + "；".join(errs[:5]))
    append_jsonl(sess / "threads.jsonl", rec)
    return rec


def inbox(sess: Path) -> list:
    ms = threads(sess)
    answered = {m.get("reply_to") for m in ms if m["author"] == "ai"}
    return [m for m in ms if m["author"] == "human" and m["id"] not in answered]


def cmd_inbox(a) -> None:
    sess = resolve_session(a.session, a.workspace)
    deadline = time.time() + (a.wait or 0)
    while True:
        new = inbox(sess)
        if new or time.time() >= deadline:
            break
        time.sleep(2)
    for m in new:
        t = m.get("target")
        print(f"{m['id']} · {(t['type'] + ' ' + t['id']) if t else '整份产出'} · {m['created_at']}\n  {m['text']}")
    if not new:
        print("没有待回复的消息")
        sys.exit(2)


def cmd_reply(a) -> None:
    sess = resolve_session(a.session, a.workspace)
    rec = add_message(sess, "ai", a.text, target=a.target, reply_to=a.to,
                      change_ids=a.changes.split(",") if a.changes else None)
    print(rec["id"])


# ---------------------------------------------------------------- status & summary

def round_stats(sess: Path) -> list:
    fl = latest_by_id(read_jsonl(sess / "findings.jsonl"))
    rounds = sorted({f["round"] for f in fl.values()})
    m = read_json(sess / "metrics.json") if (sess / "metrics.json").exists() else {"ai": {"rounds": []}}
    done = [r["round"] for r in m["ai"]["rounds"]]
    out = []
    for n in sorted(set(rounds) | set(done)):
        new = [f for f in fl.values() if f["round"] == n]
        out.append({"round": n, **{s: sum(f["severity"] == s for f in new) for s in SEVERITIES}})
    return out


def convergence(stats: list) -> str:
    if len(stats) >= 3 and all(r["S1"] > 0 for r in stats[-3:]):
        return "step_back"
    if len(stats) >= 2 and all(r["S1"] == 0 and r["S2"] == 0 for r in stats[-2:]):
        return "stop"
    return "continue"


def cmd_status(a) -> None:
    sess = resolve_session(a.session, a.workspace)
    st = round_stats(sess)
    for r in st:
        print(f"第 {r['round']} 轮新发现：S1 {r['S1']}，S2 {r['S2']}，S3 {r['S3']}")
    c = convergence(st)
    print({"stop": "收敛：连续两轮没有新的 S1/S2，可以停止。",
           "step_back": "连续三轮仍有新 S1：建议退一步，回到上游重新考虑设计。",
           "continue": "未收敛。"}[c])
    u = unresolved(sess)
    s1 = [f["id"] for f in u["findings"] if f["severity"] == "S1"]
    print(f"未回应的发现：{len(u['findings'])}（S1：{', '.join(s1) or '无'}）")
    nxt = loop_next(sess)
    print({"revise": f"AI 自改：还有未处理的 S1/S2，且自审不到 {MAX_REVIEW_ROUNDS} 轮，先自己改再审一轮（revise-begin）。",
           "handoff": "交给用户：自审已满轮数或没有 S1/S2 了。",
           "busy": "自审或自改正在进行中。"}[nxt])
    ac = applied_changes(sess)
    if ac:
        print(f"AI 自改 {len(ac)} 处，还没有写回原文件（diff 查看）")
    q = inbox(sess)
    if q:
        print(f"待回复的讨论消息：{', '.join(m['id'] for m in q)}")
    print(f"loop={nxt}")
    if s1:
        print(f"未处理的重大问题：{', '.join(s1)}（只是提示，不妨碍完成审阅）")
    print(f"convergence={c}")


MAX_EVIDENCE = 2
MAX_EXCERPT_LINES = 6


def project_root(sess: Path) -> Path | None:
    """Folder holding the original artifact; evidence paths are shown relative to it."""
    try:
        files = read_json(sess / "sources.json")["files"]
    except (OSError, KeyError, json.JSONDecodeError):
        return None
    parents = [Path(f["original"]).parent for f in files]
    return Path(os.path.commonpath(parents)) if parents else None


def short_path(path: str, root: Path | None, sess: Path | None = None) -> str:
    p = Path(path).expanduser()
    for base in (sess, root):
        if base and p.is_absolute():
            try:
                return p.relative_to(base).as_posix()
            except ValueError:
                pass
    home = str(Path.home())
    return path.replace(home, "~", 1) if path.startswith(home) else path


def short_block(text: str) -> str:
    lines = text.splitlines() or [""]
    if len(lines) > MAX_EXCERPT_LINES:
        lines = lines[:MAX_EXCERPT_LINES] + [f"…（另 {len(lines) - MAX_EXCERPT_LINES} 行）"]
    return "\n  ".join(lines)


def fmt_evidence(ev: dict, root: Path | None = None, sess: Path | None = None) -> str:
    k = ev["kind"]
    if k == "file":
        rng = f"{ev['line_start']}" + (f"-{ev['line_end']}" if ev.get("line_end") and ev["line_end"] != ev["line_start"] else "")
        s = f"`{short_path(ev['path'], root, sess)}:{rng}`\n\n  ```\n  {short_block(ev['excerpt'])}\n  ```"
    elif k == "data":
        s = f"`{short_path(ev['path'], root, sess)}` [{ev['key']}] = `{json.dumps(ev['value'], ensure_ascii=False)}`"
    elif k == "command":
        cmd = ev["command"]
        if sess:
            cmd = cmd.replace(str(sess), "<会话>")
        if root:
            cmd = cmd.replace(str(root) + "/", "")
        where = f"（在 `{short_path(ev['cwd'], root, sess)}` 下）" if ev.get("cwd") else ""
        s = f"运行 `{cmd}`{where}\n\n  ```\n  {short_block(ev['output_excerpt'])}\n  ```"
    else:
        loc = f" {ev['locator'].strip('（）()')}" if ev.get("locator") else ""
        s = f"{short_path(ev['source'], root, sess)}{loc}：「{ev['text']}」"
    if ev.get("note"):
        s += f"  \n  {ev['note']}"
    return s


def stage_labels(plan: dict) -> dict:
    """Stage id -> readable label, prefixed with its aspect when the type groups stages."""
    an = {x["id"]: x["name"] for x in plan.get("aspects", [])}
    return {s["id"]: (f"{an[s['aspect']]} · " if s.get("aspect") in an else "") + s["name"] for s in plan["stages"]}


RATING_ZH = {"strong": "扎实", "adequate": "基本成立", "weak": "存在明显问题",
             "clear": "想清楚了", "partial": "有方向，未想透", "open": "尚未涉及"}


def aspect_table(plan: dict, u: dict, mp: dict | None = None) -> list:
    """One row per aspect: the reviewer's judgement, then what is left for the human."""
    stage_aspect = {s["id"]: s.get("aspect") for s in plan["stages"]}
    judged = {a["aspect"]: a for a in (mp or {}).get("assessments", [])}
    L = ["| 方面 | 评价 | 概要 | 重大问题 | 一般问题 | 待定决策 |", "| --- | --- | --- | --- | --- | --- |"]
    for a in plan["aspects"]:
        fs = [f for f in u["findings"] if stage_aspect.get(f["stage"]) == a["id"]]
        ds = [d for d in u["awaiting"] if stage_aspect.get(d["stage"]) == a["id"]]
        j = judged.get(a["id"])
        checked = any(s["included"] for s in plan["stages"] if s.get("aspect") == a["id"])
        n = lambda sev: sum(f["severity"] == sev for f in fs) or "—"
        L.append(f"| {a['name']} | {RATING_ZH[j['rating']] if j else ('尚未评价' if checked else '本次未审阅')} | {j['verdict'] if j else a.get('ask', '')} "
                 f"| {n('S1')} | {n('S2')} | {len(ds) or '—'} |")
    L.append("")
    if judged:
        L.append("## 各方面的评价")
        L.append("")
        names = {a["id"]: a["name"] for a in plan["aspects"]}
        for a in plan["aspects"]:
            j = judged.get(a["id"])
            if not j:
                continue
            L.append(f"### {names[a['id']]} · {RATING_ZH[j['rating']]}")
            L.append("")
            L.append(j["verdict"])
            L.append("")
            L.extend(f"- {r}" for r in j["reasons"])
            if j.get("finding_ids"):
                L.append(f"- 相关问题：{'、'.join(j['finding_ids'])}")
            L.append("")
        qs = [(names.get(j["aspect"], j["aspect"]), q) for j in judged.values() for q in j.get("questions", [])]
        if qs:
            L.append("## 可以进一步思考的问题")
            L.append("")
            L.extend(f"{i}. {q}（{n}）" for i, (n, q) in enumerate(qs, 1))
            L.append("")
    return L


def review_process(sess: Path) -> dict:
    """The review told as a reviewer's process: what each round raised, what the AI changed, what was confirmed."""
    plan = read_json(sess / "plan.json")
    m = read_json(sess / "metrics.json") if (sess / "metrics.json").exists() else {"ai": {"rounds": []}}
    rounds = m["ai"]["rounds"]
    recs = read_jsonl(sess / "findings.jsonl")
    latest = latest_by_id(recs)
    stage_aspect = {st["id"]: st.get("aspect") for st in plan["stages"]}
    aspect_name = {a["id"]: a["name"] for a in plan.get("aspects", [])}
    layer = lambda stage: aspect_name.get(stage_aspect.get(stage), "其他")
    chg = [c for c in latest_by_id(read_jsonl(sess / "changes.jsonl")).values()]
    first_fixed = {}
    for r in recs:
        if r["status"] == "fixed" and r["id"] not in first_fixed:
            first_fixed[r["id"]] = r["created_at"]

    def in_round(ts, rd):
        return rd["started_at"] <= ts <= rd["ended_at"]

    out = []
    for i, rd in enumerate(rounds):
        n = rd["round"]
        raised = [f for f in latest.values() if f["round"] == n]
        by_layer: dict = {}
        for f in raised:
            by_layer[layer(f["stage"])] = by_layer.get(layer(f["stage"]), 0) + 1
        edits = [c for c in chg if c["round"] == n]
        groups: dict = {}
        for c in edits:
            stages = {latest[x]["stage"] for x in c.get("finding_ids", []) if x in latest}
            key = layer(sorted(stages)[0]) if stages else "其他"
            groups.setdefault(key, []).append(c["summary"])
        nxt = rounds[i + 1] if i + 1 < len(rounds) else None
        confirmed = [fid for fid, ts in first_fixed.items() if nxt and in_round(ts, nxt)]
        out.append({
            "round": n, "examined": rd["candidates"], "kept": rd["kept"],
            "dropped": rd["dropped_by_verifier"] + rd["dropped_duplicate"] + rd["dropped_no_evidence"],
            "raised": {"total": len(raised), "S1": sum(f["severity"] == "S1" for f in raised),
                       "S2": sum(f["severity"] == "S2" for f in raised), "S3": sum(f["severity"] == "S3" for f in raised),
                       "by_layer": by_layer},
            "from_edits": n > 1,
            "edits": {"total": len(edits), "by_layer": [{"layer": k, "n": len(v), "examples": v[:3]} for k, v in groups.items()]},
            "confirmed_fixed": len(confirmed),
        })
    last_end = rounds[-1]["ended_at"] if rounds else ""
    fixed_after = [fid for fid, ts in first_fixed.items() if ts > last_end]
    handed = [{"id": f["id"], "title": f["title"], "note": f.get("note", "suggest")}
              for f in latest.values() if f["status"] == "open" and f.get("handoff_reason")]
    return {"rounds": out, "fixed_after_rounds": len(fixed_after),
            "fixed_total": sum(1 for f in latest.values() if f["status"] == "fixed"),
            "edits_total": len(chg), "handed": handed,
            # merged while writing the overview, so each point is stated once; the round counts above predate it
            "merged": sum(1 for f in latest.values() if f["status"] == "superseded")}


def verdict_label(plan: dict, td: dict | None, verdict: str) -> str:
    """How the verdict reads depends on who the person is: the plan's own author, a reviewer, or the
    person receiving another agent's work."""
    if work_kind(td) == "plan" and plan.get("user_role", "author") == "author":
        return "可以着手实施" if verdict == "ready" else "动手之前，先想清楚这几件事"
    if plan.get("user_role") == "evaluator":
        return "审稿结论：可以接受" if verdict == "ready" else "审稿结论：需要作者修改"
    return "可以交给你审阅" if verdict == "ready" else "建议先退回作者修改"


def process_section(sess: Path, mp: dict | None) -> list:
    pr = review_process(sess)
    plan = read_json(sess / "plan.json")
    td = load_type(plan["classification"]["types"][0], sess.parent.parent)
    L = ["## 审阅过程", ""]
    rd = (mp or {}).get("readiness")
    if rd:
        L.append(f"**{verdict_label(plan, td, rd['verdict'])}**　{rd['text']}")
        L.append("")
        for t in rd["tasks"]:
            L.append(f"- {t['id']} {t['title']}　{t['why']}" + (f"（见 {'、'.join(t['refs'])}）" if t.get("refs") else ""))
        if rd["tasks"]:
            L.append("")
    for r in pr["rounds"]:
        layers = "、".join(f"{k} {v}" for k, v in r["raised"]["by_layer"].items())
        what = "复查上一轮的修改，" if r["from_edits"] else ""
        L.append(f"**第 {r['round']} 轮**　{what}审阅者提出 {r['examined']} 条意见，复核后保留 {r['kept']} 条"
                 + (f"（{layers}）" if layers else "") + f"，其余 {r['dropped']} 条重复或不成立。"
                 + (f"随后 AI 在副本中修改 {r['edits']['total']} 处。" if r["edits"]["total"] else "")
                 + (f"下一轮确认其中 {r['confirmed_fixed']} 个问题已解决。" if r["confirmed_fixed"] else ""))
        L.append("")
    if pr["merged"]:
        L.append(f"交给你之前，又把 {pr['merged']} 条与其他意见或选择重复的合并，每件事只写一次。")
        L.append("")
    return L


def overview_section(ov: dict) -> list:
    """The top layer: the paper's architecture and the questions that decide it."""
    L = ["## 概览", ""]
    for part in ov["parts"]:
        refs = part.get("refs", [])
        L.append(f"**{part['name']}**　{part['text']}" + (f"（见 {'、'.join(refs)}）" if refs else ""))
        L.append("")
    if ov.get("path"):
        L.append("## 建议的路径")
        L.append("")
        L.append(ov["path"]["text"])
        L.append("")
        for i, st in enumerate(ov["path"]["steps"], 1):
            refs = st.get("refs", [])
            L.append(f"{i}. **{st['title']}**　{st['why']}" + (f"（见 {'、'.join(refs)}）" if refs else ""))
        L.append("")
    if ov["key_questions"]:
        L.append("## 关键问题")
        L.append("")
        for q in ov["key_questions"]:
            refs = q.get("refs", [])
            L.append(f"**{q['id']} {q['title']}**　{q['body']}" + (f"（见 {'、'.join(refs)}）" if refs else ""))
            L.append("")
    return L


LEAD_ZH = {"anomaly": "异常结果", "untested_implication": "未检验的推论", "rival_explanation": "未排除的竞争性解释",
           "model_for_design": "以模型代替设计", "incoherence": "前后不一致",
           "untested_assumption": "未经检验的假设", "open_choice": "尚未作出的选择", "missing_piece": "尚未考虑的部分"}
LEAD_INTRO = {
    "finished": "以下不是需要修改的错误，而是主要结论可能站不住的地方：异常结果、未检验的推论、未排除的竞争性解释。供参考，可在页面上记下想法或与 AI 讨论。",
    "plan": "以下不是需要修改的错误，而是方案还没有想透的地方：依赖的假设尚未检验、有待作出的选择、尚未考虑的部分。供参考，可在页面上记下想法或与 AI 讨论。"}


def lead_section(sess: Path, mp: dict) -> list:
    """Detective leads: questions for the person, not problems for the AI to fix."""
    answered = {r["target"]["id"] for r in read_jsonl(sess / "feedback.jsonl")
                if r["kind"] == "answer" and r["target"]["type"] == "lead"}
    plan = read_json(sess / "plan.json")
    work = work_kind(load_type(plan["classification"]["types"][0], sess.parent.parent))
    L = ["## 值得追问之处", "", LEAD_INTRO[work], ""]
    for x in mp["leads"]:
        L.append(f"### {x['id']} · {LEAD_ZH[x['kind']]}：{x['title']}" + ("（已记下想法）" if x["id"] in answered else ""))
        L.append("")
        L.append(f"> {x['anchor']['exact']}")
        L.append("")
        L.append(f"- **观察**：{x['observation']}")
        L.append(f"- **{'对方案的影响' if work == 'plan' else '对主要结论的影响'}**：{x['threat']}")
        L.extend(f"- **可以追问**：{q}" for q in x["questions"])
        L.append("")
    return L


def render_summary(sess: Path) -> str:
    plan = read_json(sess / "plan.json")
    mp = read_json(sess / "map.json") if (sess / "map.json").exists() else None
    m = read_json(sess / "metrics.json") if (sess / "metrics.json").exists() else None
    root = project_root(sess)
    u = unresolved(sess)
    open_ids = {f["id"] for f in u["findings"]}
    # a finding nests under its root cause when the root is itself still open
    children: dict = {}
    roots = []
    for f in u["findings"]:
        if f.get("caused_by") in open_ids:
            children.setdefault(f["caused_by"], []).append(f)
        else:
            roots.append(f)
    by = {s: [f for f in u["findings"] if f["severity"] == s] for s in SEVERITIES}
    label = stage_labels(plan)
    L = []
    if by["S1"] or by["S2"]:
        head = f"**重大问题 {len(by['S1'])} 项，一般问题 {len(by['S2'])} 项**"
        n_roots = sum(1 for f in roots if f["severity"] in ("S1", "S2"))
        if n_roots < len(by["S1"]) + len(by["S2"]):
            head += f"（按根源合并为 {n_roots} 个问题）"
        if by["S3"]:
            head += f"，另有表述问题 {len(by['S3'])} 条"
        L.append(head)
    else:
        L.append("**没有待处理的问题**" + (f"（另有表述问题 {len(by['S3'])} 条）" if by["S3"] else ""))
    L.append("")
    L.append(f"# 审阅摘要：{plan['artifact']['title']}")
    L.append("")
    if m and m["ai"]["rounds"]:
        r = m["ai"]["rounds"][-1]
        n_changes = len(applied_changes(sess))
        L.append(f"AI 审阅 {len(m['ai']['rounds'])} 轮" + (f"，在副本中修改 {n_changes} 处" if n_changes else "") + "。")
        L.append("")

    if mp and mp.get("overview"):
        L.extend(overview_section(mp["overview"]))
    if (sess / "metrics.json").exists() and read_json(sess / "metrics.json")["ai"]["rounds"]:
        L.extend(process_section(sess, mp))
    elif mp:
        L.append(f"**主要结论**：{mp['summary']}")
        L.append("")
    if plan.get("aspects"):
        L.extend(aspect_table(plan, u, mp))
    if mp and mp.get("leads"):
        L.extend(lead_section(sess, mp))

    for sev, title in [("S1", "S1 · 影响主要结论或研究设计的有效性"), ("S2", "S2 · 影响某一具体论断")]:
        rs = [f for f in roots if f["severity"] == sev]
        if not rs:
            continue
        L.append(f"## {title}")
        L.append("")
        for f in rs:
            L.append(f"### {f['id']} · {f['title']}")
            L.append("")
            L.append(f"环节：{label.get(f['stage'], f['stage'])} · {ORIGIN_ZH[f['origin']]}"
                     + (f"（{f['origin_decision']}）" if f["origin"] == "inherited" and f.get("origin_decision") else ""))
            L.append("")
            L.append(f"> {f['anchor']['exact']}")
            L.append("")
            L.append(f["detail"])
            L.append("")
            if f.get("handoff_reason"):
                L.append(f"**有待作者处理**：{f['handoff_reason']}")
                L.append("")
            L.append("证据：")
            L.append("")
            for ev in f["evidence"][:MAX_EVIDENCE]:
                L.append(f"- {fmt_evidence(ev, root, sess)}")
            if len(f["evidence"]) > MAX_EVIDENCE:
                L.append(f"- （另有 {len(f['evidence']) - MAX_EVIDENCE} 条证据，见 findings.jsonl）")
            L.append("")
            if f.get("suggested_fix"):
                L.append(f"修改建议：{f['suggested_fix']['text']}")
                L.append("")
            kids = children.get(f["id"], [])
            if kids:
                L.append("关联问题（根源修正后通常随之解决）：")
                L.append("")
                for c in kids:
                    fix = f" → 改为「{c['suggested_fix']['replacement']}」" if c.get("suggested_fix", {}).get("replacement") else ""
                    L.append(f"- {c['id']}（{c['severity']}）{c['title']}：「{c['anchor']['exact']}」{fix}")
                L.append("")
    ac = applied_changes(sess)
    if ac:
        rounds = finished_rounds(sess)
        L.append(f"## AI 的修改：{len(ac)} 处（审阅 {len(rounds)} 轮；只在副本中，尚未写回原文件）")
        L.append("")
        for c in ac:
            fx = f"（针对 {'、'.join(c['finding_ids'])}）" if c.get("finding_ids") else ""
            L.append(f"- {c['id']} · {c['summary']}{fx}：{c['why']}")
        L.append("")
        L.append("可在页面上逐处撤销，或在对话中说明（如「撤销 R001」）。全部差异：`RH diff <会话>`。")
        L.append("")
    s3_roots = [f for f in roots if f["severity"] == "S3"]
    if s3_roots:
        L.append("## 表述问题（只列出，未修改）")
        L.append("")
        for f in s3_roots:
            L.append(f"- {f['id']} · {f['title']}：「{f['anchor']['exact']}」")
        L.append("")

    if u["awaiting"]:
        L.append("## 待定的研究决策")
        L.append("")
        for d in sorted(u["awaiting"], key=lambda d: -d["load_bearing"]["score"]):
            L.append(f"### {d['id']} · {d['title']}（重要性 {d['load_bearing']['score']}/3）")
            L.append("")
            L.append(d["question"])
            L.append("")
            if d["status"] == "invalidated":
                L.append("（上游决策已更改，需重新考虑）")
                L.append("")
            if d["human_action"] == "predict":
                L.append("")
                L.append("")
            if d["human_action"] == "answer" and d.get("prompt_to_human"):
                L.append(f"**开放问题**：{d['prompt_to_human']}")
                L.append("")
            for o in d["options"]:
                mark = "●" if o["chosen"] else "○"
                sens = ""
                if o.get("sensitivity"):
                    sens = f" —— {o['sensitivity']['effect']}" + ("" if o["sensitivity"]["computed"] else "（推理）")
                L.append(f"- {mark} {o['id']} {o['label']}{'（当前）' if o['chosen'] else ''}{sens}")
            L.append("")
            if d.get("challenge"):
                L.append(f"*不同看法*：{d['challenge']}")
                L.append("")

    if u["unsourced"]:
        L.append("## 缺少出处的论断")
        L.append("")
        for c in u["unsourced"]:
            L.append(f"- {c['id']}：「{c['text']}」")
        L.append("")

    cert = sess / "certificate.md"
    L.append("---")
    L.append("")
    L.append(cert.read_text(encoding="utf-8").strip() if cert.exists() else "# 核查记录\n\n（certificate.md 尚未生成。）")
    L.append("")
    L.append("---")
    L.append("")
    L.append("**回应方式**：可在页面上处理，也可在对话中说明，例如「F001 已修正」「F002 不成立：……」「D02 改用 O2」。"
             "可选的回应：" + " / ".join(REACTION_ZH.values()) + "。")
    return "\n".join(L) + "\n"


def cmd_summary(a) -> None:
    sess = resolve_session(a.session, a.workspace)
    text = render_summary(sess)
    (sess / "summary.md").write_text(text, encoding="utf-8")
    print(text if a.print else f"已写入 {sess / 'summary.md'}")


def render_handback(sess: Path) -> str:
    """The task list for the work's author, when the reviewer judges it not ready for the person."""
    plan = read_json(sess / "plan.json")
    mp = read_json(sess / "map.json")
    rd = mp.get("readiness")
    if not rd or not rd["tasks"]:
        raise RHError("map.json 里没有给作者的任务")
    fl = latest_by_id(read_jsonl(sess / "findings.jsonl"))
    leads = {x["id"]: x for x in mp.get("leads", [])}
    # evidence points into the review's read-only copy (truth/); the author needs the real project paths
    orig = Path(plan["artifact"]["original_location"]).expanduser()
    repo = next((d for d in [orig, *orig.parents] if (d / ".git").exists()), None)
    copy = str(sess / "truth") + "/"

    def where(path: str) -> str:
        return str(repo / path[len(copy):]) if repo and path.startswith(copy) else path

    L = [f"# 审阅意见：{plan['artifact']['title']}", "",
         f"原文：`{plan['artifact']['original_location']}`", "", rd["text"], "", "## 请先完成", ""]
    for t in rd["tasks"]:
        L.append(f"### {t['id']} {t['title']}")
        L.append("")
        L.append(t["why"])
        L.append("")
        for x in t.get("refs", []):
            if x in fl:
                f = fl[x]
                L.append(f"- **{f['title']}**　{f['detail']}")
                for ev in f["evidence"][:3]:
                    if ev.get("path"):
                        L.append(f"  - 依据：`{where(ev['path'])}`" + (f" 第 {ev['line_start']} 行" if ev.get("line_start") else ""))
            elif x in leads:
                L.append(f"- **{leads[x]['title']}**　{leads[x]['observation']}")
        L.append("")
    if (sess / "truth").exists():
        L.append("依据中的行号对应审阅时导出的项目版本；此后项目若有修改，行号可能偏移。")
        L.append("")
    L.append("完成后请说明改了什么、重新运行了哪些分析，审阅会在新版本上继续。")
    return "\n".join(L) + "\n"


def cmd_handback(a) -> None:
    sess = resolve_session(a.session, a.workspace)
    text = render_handback(sess)
    (sess / "handback.md").write_text(text, encoding="utf-8")
    print(text if a.print else f"已写入 {sess / 'handback.md'}")


def collect_signals(ws: Path, only: str | None = None) -> dict:
    """What the person's responses say about the reviews themselves, across all sessions (or one), by type."""
    out: dict = {}
    for sess in sorted((ws / "sessions").glob("*")):
        if only and sess.name != only:
            continue
        if not (sess / "plan.json").exists() or not (sess / "map.json").exists():
            continue  # a review that has not run yet says nothing about the review
        plan = read_json(sess / "plan.json")
        t = plan["classification"]["types"][0]
        fl = latest_by_id(read_jsonl(sess / "findings.jsonl"))
        mp = read_json(sess / "map.json") if (sess / "map.json").exists() else {}
        fb = read_jsonl(sess / "feedback.jsonl")
        last: dict = {}
        for r in fb:
            last.setdefault(r["target"]["id"], {})[r["kind"]] = r
        stage_name = {st["id"]: st["name"] for st in plan["stages"]}
        rec = out.setdefault(t, {"sessions": [], "notes": 0, "responded": 0, "accepted": 0, "rejected": 0,
                                 "low_value": 0, "edits": 0, "edits_undone": 0, "leads": 0, "leads_answered": 0,
                                 "minutes": 0, "rejected_items": [], "low_value_items": [], "undone_items": [],
                                 "lead_answers": [], "suggestions": [], "missed": [],
                                 "opened": 0, "never_opened_items": [], "long_look_no_response": [],
                                 "discussed_items": []})
        rec["sessions"].append(sess.name)
        rec["notes"] += len(fl) + len(mp.get("leads", []))
        for f in fl.values():
            r = last.get(f["id"], {}).get("reaction")
            if not r:
                continue
            rec["responded"] += 1
            item = {"session": sess.name, "id": f["id"], "check": stage_name.get(f["stage"], f["stage"]),
                    "title": f["title"], "note": r.get("note", "")}
            if r["reaction"] in ("fixed", "class_of_problem"):
                rec["accepted"] += 1
            elif r["reaction"] == "ai_wrong":
                rec["rejected"] += 1
                rec["rejected_items"].append(item)
            elif r["reaction"] == "doesnt_matter":
                rec["low_value"] += 1
                rec["low_value_items"].append(item)
        for c in latest_by_id(read_jsonl(sess / "changes.jsonl")).values():
            rec["edits"] += 1
            r = last.get(c["id"], {}).get("confirm")
            if r and r["value"] == "wrong":
                rec["edits_undone"] += 1
                rec["undone_items"].append({"session": sess.name, "id": c["id"], "summary": c["summary"],
                                            "note": r.get("note", "")})
        for x in mp.get("leads", []):
            rec["leads"] += 1
            r = last.get(x["id"], {}).get("answer")
            if r:
                rec["leads_answered"] += 1
                rec["lead_answers"].append({"session": sess.name, "id": x["id"], "title": x["title"], "answer": r["text"]})
        for r in fb:
            if r["kind"] == "suggestion":
                rec["suggestions"].append({"session": sess.name, "id": r["id"], "scope": r.get("scope"), "text": r["text"]})
            if r["kind"] == "missed":
                rec["missed"].append({"session": sess.name, "id": r["id"], "text": r["text"], "note": r.get("note", "")})
        m = read_json(sess / "metrics.json") if (sess / "metrics.json").exists() else {}
        rec["minutes"] += round((m.get("human") or {}).get("minutes") or 0)
        # what the person looked at, from the page: notes never opened, notes read at length without a response
        notes = {f["id"]: f["title"] for f in fl.values() if f["status"] == "open"} | \
            {x["id"]: x["title"] for x in mp.get("leads", [])}
        seconds: dict = {}
        opened_at: dict = {}
        for e in read_jsonl(sess / "activity.jsonl"):
            if e["kind"] == "open":
                opened_at[e["id"]] = e["t"]
                seconds.setdefault(e["id"], 0)
            elif e["kind"] == "close" and e["id"] in opened_at:
                seconds[e["id"]] += min(600, (e["t"] - opened_at.pop(e["id"])) / 1000)
        if (sess / "activity.jsonl").exists():
            rec["opened"] += sum(1 for x in notes if x in seconds)
            for x, title in notes.items():
                responded = any(k in last.get(x, {}) for k in ("reaction", "answer"))
                if x not in seconds and not responded:
                    rec["never_opened_items"].append({"session": sess.name, "id": x, "title": title})
                elif seconds.get(x, 0) >= 60 and not responded:
                    rec["long_look_no_response"].append({"session": sess.name, "id": x, "title": title,
                                                         "seconds": round(seconds[x])})
        # what the person discussed with the AI: often a note that was unclear or contested
        for x, title in notes.items():
            msgs = [mm["text"] for mm in threads(sess) if mm["author"] == "human" and (mm.get("target") or {}).get("id") == x]
            if msgs:
                rec["discussed_items"].append({"session": sess.name, "id": x, "title": title, "messages": msgs})
    return out


def cmd_signals(a) -> None:
    ws = workspace_root(a.workspace)
    only = resolve_session(a.session, a.workspace).name if a.session else None
    sig = collect_signals(ws, only)
    (ws / "ledger").mkdir(exist_ok=True)
    write_json(ws / "ledger" / ("signals.json" if not only else f"signals-{only}.json"), sig)
    pct = lambda n, d: f"{round(100 * n / d)}%" if d else "—"
    for t, r in sig.items():
        print(f"## {t}（{len(r['sessions'])} 次审阅）")
        print(f"- 批注 {r['notes']} 条，你回应了 {r['responded']} 条：同意 {pct(r['accepted'], r['responded'])}，"
              f"不成立 {pct(r['rejected'], r['responded'])}，无关紧要 {pct(r['low_value'], r['responded'])}")
        print(f"- AI 修改 {r['edits']} 处，撤销 {r['edits_undone']} 处；值得追问 {r['leads']} 条，记下想法 {r['leads_answered']} 条")
        print(f"- AI 漏掉的问题 {len(r['missed'])} 条；对审阅的建议 {len(r['suggestions'])} 条；你用时 {r['minutes']} 分钟")
        if r["opened"] or r["never_opened_items"]:
            print(f"- 页面上打开过 {r['opened']} 条批注；{len(r['never_opened_items'])} 条从未打开，"
                  f"{len(r['long_look_no_response'])} 条看了一分钟以上却没有回应；与 AI 讨论过 {len(r['discussed_items'])} 条")
        print()
    print(f"详细记录：{ws / 'ledger' / ('signals.json' if not only else f'signals-{only}.json')}")


def cmd_metrics(a) -> None:
    sess = resolve_session(a.session, a.workspace)
    print(json.dumps(recompute_metrics(sess), ensure_ascii=False, indent=2))


# ---------------------------------------------------------------- cli

def main(argv=None) -> None:
    ap = argparse.ArgumentParser(prog="rh", description="Review Engine 辅助脚本")
    ap.add_argument("--workspace", help="工作区路径（默认 $REVIEW_WORKSPACE 或 ~/review-workspace）")
    sub = ap.add_subparsers(dest="cmd", required=True)

    def sp(name, fn, help_, session=True):
        p = sub.add_parser(name, help=help_)
        p.set_defaults(fn=fn)
        if session:
            p.add_argument("session", help="会话文件夹路径或 session_id")
        return p

    p = sp("init", cmd_init, "初始化工作区", session=False)
    p.add_argument("path")
    p = sp("new-session", cmd_new_session, "建会话并复制产出快照", session=False)
    p.add_argument("--slug", required=True)
    p.add_argument("files", nargs="+")
    p = sp("init-plan", cmd_init_plan, "按 TYPE.md 和档位生成计划草稿")
    p.add_argument("--type", required=True)
    p.add_argument("--tier", required=True, choices=["light", "standard", "deep"])
    p.add_argument("--force", action="store_true")
    p = sp("snapshot", cmd_snapshot, "从原位置重新复制快照并显示改动")
    p.add_argument("--force", action="store_true", help="丢弃还没写回的 AI 自改")
    sp("hash", cmd_hash, "打印当前快照的 artifact_hash")
    p = sp("anchor", cmd_anchor, "由一段原文生成文本锚点 JSON（原文须在快照中唯一）")
    p.add_argument("exact")
    p.add_argument("--path", help="快照中的文件，如 artifact/report.md")
    sp("validate", cmd_validate, "按 schema 和交叉规则校验会话")
    sp("check-plan", cmd_check_plan, "确认计划已确认")
    sp("round-start", cmd_round_start, "开始一轮自审")
    p = sp("round-end", cmd_round_end, "结束一轮自审并记录度量")
    for k in ("agents", "candidates", "dropped-no-evidence", "dropped-by-verifier", "dropped-duplicate"):
        p.add_argument(f"--{k}", type=int, required=k in ("agents", "candidates"), default=0)
    p = sp("add-finding", cmd_add_finding, "写入一条已验证的发现（JSON 从标准输入）")
    p.add_argument("--round", type=int)
    p.add_argument("--finder", required=True)
    p.add_argument("--verifier", required=True)
    p.add_argument("--method", required=True)
    p.add_argument("--caused-by")
    p = sp("check-candidates", cmd_check_candidates, "检查合并后的候选文件（证据格式、摘录、锚点）")
    p.add_argument("file")
    p = sp("set-finding-status", cmd_set_finding_status, "更新发现的 AI 一侧状态")
    p.add_argument("finding")
    p.add_argument("status", choices=["open", "fixed", "closed", "superseded", "invalidated"])
    p.add_argument("--superseded-by", help="合并进的发现（F…），或方案中对应的选择（D…）")
    p = sp("link-finding", cmd_link_finding, "标明一条发现由另一条（根源）引起")
    p.add_argument("finding")
    p.add_argument("--caused-by", required=True)
    p = sp("add-feedback", cmd_add_feedback, "写入一条用户反馈")
    p.add_argument("--kind", required=True,
                   choices=["reaction", "confirm", "choice", "answer", "prediction", "plan_confirm", "signoff",
                            "suggestion", "missed"])
    p.add_argument("--target")
    p.add_argument("--reaction", choices=REACTIONS)
    p.add_argument("--fix-by", dest="fix_by", choices=["ai", "human"])
    p.add_argument("--value", choices=["correct", "wrong"])
    p.add_argument("--option")
    p.add_argument("--text")
    p.add_argument("--outcome", choices=["deliver", "another_round"])
    p.add_argument("--quality", type=int, choices=range(1, 6))
    p.add_argument("--source", default="chat", choices=["ui", "chat", "voice"])
    p.add_argument("--note")
    p.add_argument("--scope", choices=["this_review", "this_type", "general"], help="kind=suggestion：建议适用的范围")
    p = sp("revise-begin", cmd_revise_begin, "开始 AI 自改：保存改前版本，列出待处理的发现")
    p = sp("add-change", cmd_add_change, "记录一处 AI 自改（JSON 从标准输入传入）")
    p = sp("revise-end", cmd_revise_end, "结束自改：检查每处改动都有记录")
    p = sp("handoff", cmd_handoff, "AI 自己改不了的发现：写明事实和判断，交给作者，自改循环不再算它")
    p.add_argument("finding")
    p.add_argument("--reason", required=True)
    p.add_argument("--note", choices=["suggest", "verify"], help="在页面上显示为建议还是待核实")
    p = sp("edit-finding", cmd_edit_finding, "改写一条发现的措辞、批注类型或严重度（JSON 从标准输入），保留历史版本")
    p.add_argument("finding")
    p = sp("reanchor", cmd_reanchor, "改动后给未处理的发现更新锚点")
    p.add_argument("finding")
    p.add_argument("exact")
    p = sp("set-change-status", cmd_set_change_status, "撤销 AI 自改后标记 reverted")
    p.add_argument("change")
    p.add_argument("status", choices=["applied", "reverted"])
    sp("diff", cmd_diff, "原版（baseline）与当前快照的差异")
    p = sp("writeback", cmd_writeback, "把快照写回原文件（需用户明确同意后加 --yes）")
    p.add_argument("--yes", action="store_true")
    p = sp("inbox", cmd_inbox, "列出用户在界面上发来、还没回复的讨论消息")
    p.add_argument("--wait", type=int, default=0, help="最多等这么多秒，直到有新消息")
    p = sp("reply", cmd_reply, "回复一条讨论消息")
    p.add_argument("--to", help="回复的消息编号")
    p.add_argument("--target", help="不回复具体消息时：讨论的对象（F/D/C/R 编号）")
    p.add_argument("--text", required=True)
    p.add_argument("--changes", help="因讨论做的改动编号，逗号分隔")
    p = sp("apply-plan", cmd_apply_plan, "把反馈整理成修改清单（--write 同时关闭不改的发现、更新决策点状态）")
    p.add_argument("--write", action="store_true")
    sp("status", cmd_status, "轮次、收敛判断和能否签收")
    p = sp("summary", cmd_summary, "生成 summary.md")
    p.add_argument("--print", action="store_true")
    sp("metrics", cmd_metrics, "重算 metrics.json 中的计数")
    p = sp("signals", cmd_signals, "汇总你在审阅中的回应，作为改进审阅的依据（写入 ledger/）", session=False)
    p.add_argument("--session", help="只看这一次审阅（审阅完成后的回顾用）")
    p = sp("handback", cmd_handback, "按“先退回修改”的判断，写出给作者的任务清单 handback.md")
    p.add_argument("--print", action="store_true")

    a = ap.parse_args(argv)
    try:
        a.fn(a)
    except RHError as e:
        die(str(e), e.code)


if __name__ == "__main__":
    main()
