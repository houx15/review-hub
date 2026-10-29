"""Tests for scripts/rh.py.

Run: uv run --with pytest --with jsonschema --with pyyaml --with rfc3339-validator pytest tests
"""
import importlib.util
import io
import json
import shutil
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
FIXTURE = ROOT / "tests" / "fixtures" / "salary-audit"
EXAMPLES = ROOT / "schemas" / "examples"

spec = importlib.util.spec_from_file_location("rh", ROOT / "scripts" / "rh.py")
rh = importlib.util.module_from_spec(spec)
spec.loader.exec_module(rh)


def run(ws, *args, stdin=None):
    """Run rh in-process; return (exit code, stdout+stderr)."""
    out, err = io.StringIO(), io.StringIO()
    old = sys.stdout, sys.stderr, sys.stdin
    sys.stdout, sys.stderr = out, err
    if stdin is not None:
        sys.stdin = io.StringIO(stdin)
    code = 0
    try:
        rh.main(["--workspace", str(ws), *args])
    except SystemExit as e:
        code = e.code or 0
    finally:
        sys.stdout, sys.stderr, sys.stdin = old
    return code, out.getvalue() + err.getvalue()


@pytest.fixture
def session(tmp_path):
    """A workspace with one session whose plan is confirmed and map is written."""
    proj = tmp_path / "proj"
    shutil.copytree(FIXTURE, proj)
    ws = tmp_path / "ws"
    assert run(ws, "init", str(ws))[0] == 0
    code, out = run(ws, "new-session", "--slug", "test", str(proj / "report.md"))
    assert code == 0, out
    sess = next((ws / "sessions").iterdir())
    sid = sess.name
    h = rh.artifact_hash(sess)

    plan = json.loads((EXAMPLES / "plan.json").read_text())
    plan.update(session_id=sid, status="confirmed")
    plan["artifact"]["hash"] = h
    plan["artifact"]["snapshot_paths"] = ["artifact/report.md"]
    plan["stages"].append({"id": "analysis", "name": "统计分析与稳健性", "aspect": "implementation",
                           "verifiability": "V", "included": True,
                           "ai_checks": [], "human": {"involved": False, "action": "none"}})
    rh.write_json(sess / "plan.json", plan)

    anchor = {"path": "artifact/report.md", "exact": "拒绝作答的记录视为缺失"}
    mp = json.loads((EXAMPLES / "map.json").read_text())
    mp.update(session_id=sid, artifact_hash=h)
    mp["decisions"][0]["anchor"] = anchor
    mp["decisions"][1]["anchor"] = {"path": "artifact/report.md", "exact": "配对固定效应"}
    mp["claims"] = [{"id": "C01", "stage": "interpretation", "text": "低 28.6%",
                     "anchor": {"path": "artifact/report.md", "exact": "平均低 28.6%"},
                     "source_type": "computed", "sources": [], "check_status": "unchecked"}]
    rh.write_json(sess / "map.json", mp)
    return ws, sess, proj


def candidate(proj, **over):
    c = {
        "stage": "measurement", "severity": "S1", "title": "拒答被记成了数值",
        "detail": "解析代码取第一个数字，拒答里的年份被当成起薪。",
        "evidence": [{"kind": "file", "path": str(proj / "src" / "parse_salary.py"), "line_start": 7,
                      "excerpt": 'm = re.search(r"\\d[\\d,]*", text)'}],
        "anchor": {"path": "artifact/report.md", "exact": "拒绝作答的记录视为缺失",
                   "prefix": "薪资结果取模型回复中给出的起薪数字。", "suffix": "，不进入分析"},
        "origin": "source_material", "decision_ids": ["D01"], "problem_kind": "refusal_parsed_as_value",
        "suggested_fix": {"text": "先识别拒答"},
    }
    c.update(over)
    return json.dumps(c, ensure_ascii=False)


def add(ws, sess, cand, rnd="1"):
    return run(ws, "add-finding", str(sess), "--round", rnd, "--finder", "f", "--verifier", "v",
               "--method", "m", stdin=cand)


def test_schema_examples_validate():
    pairs = [("plan.json", "plan.schema.json"), ("map.json", "map.schema.json"),
             ("metrics.json", "metrics.schema.json")]
    for f, s in pairs:
        assert rh.schema_errors(s, json.loads((EXAMPLES / f).read_text())) == [], f
    for f, s in [("findings.jsonl", "finding.schema.json"), ("feedback.jsonl", "feedback.schema.json"),
                 ("incidents.jsonl", "incident.schema.json")]:
        for rec in rh.read_jsonl(EXAMPLES / f):
            assert rh.schema_errors(s, rec) == [], (f, rec["id"])


def test_init_is_idempotent(tmp_path):
    ws = tmp_path / "ws"
    run(ws, "init", str(ws))
    (ws / "types" / "research-report" / "rules.md").write_text("my rules")
    code, out = run(ws, "init", str(ws))
    assert code == 0 and "无，全部已存在" in out
    assert (ws / "types" / "research-report" / "rules.md").read_text() == "my rules"


def test_hash_changes_with_content(session):
    ws, sess, proj = session
    h1 = rh.artifact_hash(sess)
    (proj / "report.md").write_text((proj / "report.md").read_text() + "\n补充一句。\n")
    code, out = run(ws, "snapshot", str(sess))
    assert code == 0 and "补充一句" in out
    assert rh.artifact_hash(sess) != h1


def test_session_validates(session):
    ws, sess, _ = session
    code, out = run(ws, "validate", str(sess))
    assert code == 0, out


def test_validate_catches_bad_anchor_and_links(session):
    ws, sess, _ = session
    mp = json.loads((sess / "map.json").read_text())
    mp["claims"][0]["anchor"]["exact"] = "报告里没有这句话"
    mp["decisions"][1]["upstream"] = []  # D01 still lists D02 downstream
    rh.write_json(sess / "map.json", mp)
    code, out = run(ws, "validate", str(sess))
    assert code == 3
    assert "锚点原文在快照中找不到" in out and "upstream 里没有" in out


def test_add_finding_happy_path_and_dedupe(session):
    ws, sess, proj = session
    code, out = add(ws, sess, candidate(proj))
    assert code == 0 and out.strip() == "F001"
    code, out = add(ws, sess, candidate(proj, title="换个标题"))
    assert code == 5 and "F001" in out
    assert run(ws, "validate", str(sess))[0] == 0


def test_add_finding_rejects_no_evidence(session):
    ws, sess, proj = session
    code, _ = add(ws, sess, candidate(proj, evidence=[]))
    assert code == 2


def test_add_finding_rejects_invented_anchor(session):
    ws, sess, proj = session
    bad = {"path": "artifact/report.md", "exact": "模型显然有偏见"}
    code, out = add(ws, sess, candidate(proj, anchor=bad))
    assert code == 3 and "锚点" in out


def test_add_finding_rejects_invented_excerpt(session):
    ws, sess, proj = session
    ev = [{"kind": "file", "path": str(proj / "src" / "parse_salary.py"), "line_start": 7,
           "excerpt": "return float(text)"}]
    code, out = add(ws, sess, candidate(proj, evidence=ev))
    assert code == 3 and "找不到" in out


def test_add_finding_accepts_elided_excerpt(session):
    ws, sess, proj = session
    ev = [{"kind": "file", "path": str(proj / "src" / "parse_salary.py"), "line_start": 5, "line_end": 8,
           "excerpt": "def parse_salary(text):\n...\n    return int(m.group().replace(\",\", \"\")) if m else None"}]
    code, out = add(ws, sess, candidate(proj, evidence=ev))
    assert code == 0, out


def test_add_finding_rejects_wrong_line(session):
    ws, sess, proj = session
    ev = [{"kind": "file", "path": str(proj / "src" / "parse_salary.py"), "line_start": 1,
           "excerpt": 'm = re.search(r"\\d[\\d,]*", text)'}]
    code, out = add(ws, sess, candidate(proj, evidence=ev))
    assert code == 3 and "行号" in out


def test_s3_cap(session):
    ws, sess, proj = session
    for i in range(rh.S3_CAP):
        code, out = add(ws, sess, candidate(proj, severity="S3", problem_kind=f"wording_{i}"))
        assert code == 0, out
    code, _ = add(ws, sess, candidate(proj, severity="S3", problem_kind="wording_x"))
    assert code == 6
    last = rh.latest_by_id(rh.read_jsonl(sess / "findings.jsonl"))["F001"]
    assert last["suggested_fix"]["apply_by_default"] is False


def test_feedback_copies_anchor_and_signoff_is_never_blocked(session):
    ws, sess, proj = session
    add(ws, sess, candidate(proj))
    # an open S1 is recorded as seen, not a lock
    code, out = run(ws, "add-feedback", str(sess), "--kind", "signoff", "--outcome", "another_round")
    assert code == 0, out
    assert "F001" in rh.read_jsonl(sess / "feedback.jsonl")[-1]["signoff"]["acknowledged"]
    code, out = run(ws, "add-feedback", str(sess), "--kind", "reaction", "--target", "F001",
                    "--reaction", "fixed", "--fix-by", "ai")
    assert code == 0 and out.strip() == "FB0002"
    rec = rh.read_jsonl(sess / "feedback.jsonl")[1]
    assert rec["anchor"]["exact"] == "拒绝作答的记录视为缺失"
    code, out = run(ws, "add-feedback", str(sess), "--kind", "signoff", "--outcome", "deliver",
                    "--quality", "4")
    assert code == 0, out
    assert run(ws, "validate", str(sess))[0] == 0


def test_choice_on_unknown_option_fails_validation(session):
    ws, sess, _ = session
    code, out = run(ws, "add-feedback", str(sess), "--kind", "choice", "--target", "D02", "--option", "O9")
    assert code == 0
    code, out = run(ws, "validate", str(sess))
    assert code == 3 and "O9" in out


def test_rounds_metrics_and_summary(session):
    ws, sess, proj = session
    assert run(ws, "round-start", str(sess))[0] == 0
    assert run(ws, "round-start", str(sess))[0] != 0  # one round at a time
    add(ws, sess, candidate(proj), rnd="1")
    code, out = run(ws, "round-end", str(sess), "--agents", "3", "--candidates", "4",
                    "--dropped-no-evidence", "2", "--dropped-by-verifier", "1")
    assert code == 0, out
    m = json.loads((sess / "metrics.json").read_text())
    assert m["ai"]["rounds"][0]["kept"] == 1
    assert m["findings"]["by_severity"]["S1"] == 1
    run(ws, "add-feedback", str(sess), "--kind", "reaction", "--target", "F001", "--reaction", "ai_wrong")
    m = json.loads((sess / "metrics.json").read_text())
    assert m["findings"]["precision"] == 0.0
    code, out = run(ws, "summary", str(sess), "--print")
    assert code == 0
    assert out.startswith("**没有待处理的问题**")  # F001 has been responded to
    assert run(ws, "validate", str(sess))[0] == 0


def test_summary_leads_with_counts(session):
    ws, sess, proj = session
    add(ws, sess, candidate(proj))
    add(ws, sess, candidate(proj, severity="S2", problem_kind="other"))
    code, out = run(ws, "summary", str(sess), "--print")
    assert out.splitlines()[0] == "**重大问题 1 项，一般问题 1 项**"
    # findings are labelled with their aspect, and the summary has one row per aspect
    assert "环节：实现与测量 · 测量怎么做的" in out
    assert "| 实现与测量 | 尚未评价 | 测量和方法做得对不对、好不好、稳不稳？ | 1 | 1 | 2 |" in out
    assert "| 结果呈现 | 本次未审阅 | 结果讲清楚了吗，重点突出了吗？ | — | — | — |" in out


def test_convergence():
    r = lambda n, s1, s2: {"round": n, "S1": s1, "S2": s2, "S3": 0}
    assert rh.convergence([r(1, 1, 0), r(2, 0, 0), r(3, 0, 0)]) == "stop"
    assert rh.convergence([r(1, 1, 0), r(2, 1, 0), r(3, 2, 0)]) == "step_back"
    assert rh.convergence([r(1, 1, 0), r(2, 0, 1)]) == "continue"


def test_summary_nests_caused_findings_and_shortens_paths(session):
    ws, sess, proj = session
    add(ws, sess, candidate(proj))
    code, out = run(ws, "add-finding", str(sess), "--round", "1", "--finder", "f", "--verifier", "v",
                    "--method", "m", "--caused-by", "F001",
                    stdin=candidate(proj, severity="S2", problem_kind="n_mismatch", title="样本量写错"))
    assert code == 0, out
    code, out = run(ws, "summary", str(sess), "--print")
    assert out.splitlines()[0] == "**重大问题 1 项，一般问题 1 项**（按根源合并为 1 个问题）"
    assert "关联问题" in out and "- F002（S2）样本量写错" in out
    assert "### F002" not in out
    assert "`src/parse_salary.py:7`" in out and str(proj) not in out.split("---")[0]
    assert run(ws, "link-finding", str(sess), "F001", "--caused-by", "F002")[0] != 0  # no cycles


def test_anchor_helper(session):
    ws, sess, _ = session
    code, out = run(ws, "anchor", str(sess), "平均低 28.6%")
    assert code == 0, out
    a = json.loads(out)
    assert a["path"] == "artifact/report.md" and a["suffix"].startswith("，差异") and a["line_hint"] == 21
    assert rh.anchor_found(sess, a)
    assert run(ws, "anchor", str(sess), "不存在的句子")[0] != 0
    assert run(ws, "anchor", str(sess), "简历")[0] != 0  # not unique


def test_init_plan_draft(tmp_path):
    proj = tmp_path / "proj"
    shutil.copytree(FIXTURE, proj)
    ws = tmp_path / "ws"
    run(ws, "init", str(ws))
    run(ws, "new-session", "--slug", "t", str(proj / "report.md"))
    sess = next((ws / "sessions").iterdir())
    code, out = run(ws, "init-plan", str(sess), "--type", "research-report", "--tier", "light")
    assert code == 0, out
    plan = json.loads((sess / "plan.json").read_text())
    inc = [s["id"] for s in plan["stages"] if s["included"]]
    assert inc == ["framing", "design", "interpretation", "conclusion"]
    # the expert questions travel with each aspect into the plan
    assert all(a.get("expert_questions") and a.get("red_flags") for a in plan["aspects"])
    # stages carry the type's aspects (the seven aspects of an academic report)
    assert [a["name"] for a in plan["aspects"]] == ["研究问题与定位", "文献基础", "研究设计", "结果", "分析", "解读"]
    assert {s["aspect"] for s in plan["stages"]} == {a["id"] for a in plan["aspects"]}
    assert plan["attention_budget"]["open_questions_max"] == 0
    code, out = run(ws, "validate", str(sess))
    assert code == 3 and "TODO" in out  # placeholders must be filled before the plan is usable
    assert run(ws, "init-plan", str(sess), "--type", "research-report", "--tier", "light")[0] != 0


def test_add_finding_strips_nulls_and_stray_origin_decision(session):
    ws, sess, proj = session
    code, out = add(ws, sess, candidate(proj, origin="introduced", origin_decision="D02", rule_ref=None))
    assert code == 0, out
    f = rh.read_jsonl(sess / "findings.jsonl")[0]
    assert "origin_decision" not in f and "D02" in f["decision_ids"] and "rule_ref" not in f


def test_check_candidates(session, tmp_path):
    ws, sess, proj = session
    good = json.loads(candidate(proj))
    good["cid"] = "data-1"
    bad = json.loads(candidate(proj, evidence=[{"path": "x", "line": 3}],
                               anchor={"path": "artifact/report.md", "exact": "没有这句"}))
    bad["cid"] = "numbers-1"
    f = tmp_path / "cands.jsonl"
    f.write_text(json.dumps(good, ensure_ascii=False) + "\n" + json.dumps(bad, ensure_ascii=False) + "\n")
    code, out = run(ws, "check-candidates", str(sess), str(f))
    assert code == 0
    assert "data-1" not in out and "numbers-1" in out and "格式不对" in out and "锚点" in out
    # the skill gives the path relative to the session folder
    (sess / "work" / "round-1").mkdir(parents=True, exist_ok=True)
    shutil.copy(f, sess / "work" / "round-1" / "candidates-all.jsonl")
    code, out = run(ws, "check-candidates", str(sess), "work/round-1/candidates-all.jsonl")
    assert code == 0 and "numbers-1" in out


def test_reviewer_count():
    assert rh.reviewer_count("light", 4) == 2
    assert rh.reviewer_count("standard", 9) == 4
    assert rh.reviewer_count("deep", 11) == 6


def test_apply_plan(session):
    ws, sess, proj = session
    assert add(ws, sess, candidate(proj))[0] == 0
    assert add(ws, sess, candidate(proj, severity="S2", title="另一处", problem_kind="other",
                                    anchor={"path": "artifact/report.md", "exact": "配对固定效应"}))[0] == 0
    run(ws, "add-feedback", str(sess), "--kind", "reaction", "--target", "F001", "--reaction", "fixed", "--fix-by", "ai")
    run(ws, "add-feedback", str(sess), "--kind", "reaction", "--target", "F002", "--reaction", "doesnt_matter")
    mp = rh.read_json(sess / "map.json")
    mp["decisions"][0]["downstream"] = ["D02"]
    mp["decisions"][1]["upstream"] = ["D01"]
    rh.write_json(sess / "map.json", mp)
    d1_other = next(o["id"] for o in mp["decisions"][0]["options"] if not o["chosen"])
    run(ws, "add-feedback", str(sess), "--kind", "choice", "--target", "D01", "--option", d1_other)
    code, out = run(ws, "apply-plan", str(sess), "--write")
    assert code == 0, out
    todo = rh.read_json(sess / "work" / "apply-0" / "todo.json")
    assert [x["id"] for x in todo["fix"]] == ["F001"]
    assert [x["id"] for x in todo["close"]] == ["F002"]
    assert [x["id"] for x in todo["decisions"]] == ["D01"] and todo["invalidate"] == ["D02"]
    fl = rh.latest_by_id(rh.read_jsonl(sess / "findings.jsonl"))
    assert fl["F002"]["status"] == "closed" and fl["F001"]["status"] == "open"
    st = {d["id"]: d["status"] for d in rh.read_json(sess / "map.json")["decisions"]}
    assert st == {"D01": "changed", "D02": "invalidated"}
    assert run(ws, "validate", str(sess))[0] == 0


# ---------------------------------------------------------------- AI revise loop and discussion

def finish_round(ws, sess, proj, **cand):
    assert run(ws, "round-start", str(sess))[0] == 0
    if cand is not None:
        code, out = add(ws, sess, candidate(proj, **cand))
        assert code == 0, out
    code, out = run(ws, "round-end", str(sess), "--agents", "2", "--candidates", "1")
    assert code == 0, out


def change(**over):
    c = {"summary": "拒答单独标出，不再当成起薪", "why": "F001：拒答里的年份被当成了起薪",
         "path": "artifact/report.md", "before": "拒绝作答的记录视为缺失",
         "after": "拒绝作答的记录单独标出并剔除", "finding_ids": ["F001"]}
    c.update(over)
    return json.dumps(c, ensure_ascii=False)


def edit(sess, old, new):
    p = sess / "artifact" / "report.md"
    p.write_text(p.read_text().replace(old, new))


def test_revise_loop_records_every_edit(session):
    ws, sess, proj = session
    assert (sess / "baseline" / "report.md").read_text() == (proj / "report.md").read_text()
    finish_round(ws, sess, proj)
    assert rh.loop_next(sess) == "revise"  # an open S1 after round 1
    assert run(ws, "revise-begin", str(sess))[0] == 0
    assert rh.loop_next(sess) == "busy"
    edit(sess, "拒绝作答的记录视为缺失", "拒绝作答的记录单独标出并剔除")
    edit(sess, "都是完整配对", "都是完整的配对")  # an edit nobody recorded
    code, out = run(ws, "revise-end", str(sess))
    assert code == 3 and "完整的配对" in out
    code, out = run(ws, "add-change", str(sess), stdin=change())
    assert code == 0 and out.strip() == "R001", out
    code, out = run(ws, "add-change", str(sess), stdin=change(summary="措辞", why="更清楚", before="都是完整配对",
                                                               after="都是完整的配对", finding_ids=[]))
    assert code == 0, out
    code, out = run(ws, "revise-end", str(sess))
    assert code == 0, out
    # F001's text is gone from the new version; the next round must mark it fixed or re-anchor it
    assert "F001" in out
    r = rh.read_jsonl(sess / "changes.jsonl")[0]
    assert r["anchor"]["exact"] == "拒绝作答的记录单独标出并剔除" and r["round"] == 1
    code, out = run(ws, "diff", str(sess))
    assert "-薪资结果取模型回复中给出的起薪数字。拒绝作答的记录视为缺失" in out
    assert run(ws, "validate", str(sess))[0] == 0
    # round 2 confirms the fix; no S1/S2 left, so the loop hands over to the person
    assert run(ws, "round-start", str(sess))[0] == 0
    assert run(ws, "set-finding-status", str(sess), "F001", "fixed")[0] == 0
    assert run(ws, "round-end", str(sess), "--agents", "2", "--candidates", "0")[0] == 0
    assert rh.loop_next(sess) == "handoff"
    code, out = run(ws, "status", str(sess))
    assert "loop=handoff" in out and "AI 自改 2 处" in out


def test_loop_stops_after_three_rounds(session):
    ws, sess, proj = session
    for n in range(3):
        finish_round(ws, sess, proj, severity="S2", problem_kind=f"k{n}", title=f"问题 {n}")
    assert rh.loop_next(sess) == "handoff"


def test_snapshot_and_writeback_protect_both_sides(session):
    ws, sess, proj = session
    finish_round(ws, sess, proj)
    run(ws, "revise-begin", str(sess))
    edit(sess, "拒绝作答的记录视为缺失", "拒绝作答的记录单独标出并剔除")
    run(ws, "add-change", str(sess), stdin=change())
    assert run(ws, "revise-end", str(sess))[0] == 0
    # re-copying from the original would silently drop the AI's edit
    code, out = run(ws, "snapshot", str(sess))
    assert code == 3 and "R001" in out
    # writing back needs explicit consent
    code, out = run(ws, "writeback", str(sess))
    assert code == 3 and "--yes" in out
    # and refuses when someone changed the original meanwhile
    orig = proj / "report.md"
    orig.write_text(orig.read_text() + "\n作者新加的一段。\n")
    code, out = run(ws, "writeback", str(sess), "--yes")
    assert code == 3 and "被别人改过" in out
    orig.write_text((sess / "baseline" / "report.md").read_text())
    code, out = run(ws, "writeback", str(sess), "--yes")
    assert code == 0, out
    assert "单独标出并剔除" in orig.read_text()
    assert run(ws, "diff", str(sess))[1].strip() == "和原版没有差别"


def test_undo_change_from_feedback(session):
    ws, sess, proj = session
    finish_round(ws, sess, proj)
    run(ws, "revise-begin", str(sess))
    edit(sess, "拒绝作答的记录视为缺失", "拒绝作答的记录单独标出并剔除")
    run(ws, "add-change", str(sess), stdin=change())
    run(ws, "revise-end", str(sess))
    run(ws, "set-finding-status", str(sess), "F001", "fixed")
    code, out = run(ws, "add-feedback", str(sess), "--kind", "confirm", "--target", "R001", "--value", "wrong",
                    "--note", "保留原来的说法")
    assert code == 0, out
    assert rh.read_jsonl(sess / "feedback.jsonl")[-1]["anchor"]["exact"] == "拒绝作答的记录单独标出并剔除"
    code, out = run(ws, "apply-plan", str(sess))
    assert code == 0 and "撤销 AI 自改" in out and "R001" in out
    assert run(ws, "set-change-status", str(sess), "R001", "reverted")[0] == 3  # text still there
    edit(sess, "拒绝作答的记录单独标出并剔除", "拒绝作答的记录视为缺失")
    assert run(ws, "set-change-status", str(sess), "R001", "reverted")[0] == 0
    assert rh.applied_changes(sess) == []


def test_discussion_inbox_and_reply(session):
    ws, sess, proj = session
    add(ws, sess, candidate(proj))
    m = rh.add_message(sess, "human", "这里为什么算 S1？", target="F001", source="ui")
    assert m["id"] == "M0001" and m["target"] == {"type": "finding", "id": "F001"}
    rh.add_message(sess, "human", "整体上结论太强了吗？", source="ui")
    code, out = run(ws, "inbox", str(sess))
    assert code == 0 and "M0001" in out and "整份产出" in out
    code, out = run(ws, "reply", str(sess), "--to", "M0001", "--text", "因为它改变主结论的符号。")
    assert code == 0, out
    reply = rh.read_jsonl(sess / "threads.jsonl")[-1]
    assert reply["author"] == "ai" and reply["target"]["id"] == "F001"  # inherits the thread's target
    assert [x["id"] for x in rh.inbox(sess)] == ["M0002"]
    assert run(ws, "reply", str(sess), "--to", "M0009", "--text", "x")[0] != 0
    assert run(ws, "validate", str(sess))[0] == 0


def test_suggestion_feedback(session):
    ws, sess, proj = session
    code, out = run(ws, "add-feedback", str(sess), "--kind", "suggestion", "--text", "以后先看摘要和标题是否一致",
                    "--scope", "this_type")
    assert code == 0, out
    rec = rh.read_jsonl(sess / "feedback.jsonl")[-1]
    assert rec["target"]["type"] == "session" and rec["scope"] == "this_type"
    assert run(ws, "add-feedback", str(sess), "--kind", "suggestion")[0] != 0  # needs text


def test_custom_stage_needs_criteria(session):
    ws, sess, proj = session
    plan = json.loads((sess / "plan.json").read_text())
    plan["stages"].append({"id": "ethics", "name": "伦理审查", "verifiability": "J", "included": True,
                           "custom": True, "ai_checks": [], "human": {"involved": False, "action": "none"}})
    rh.write_json(sess / "plan.json", plan)
    code, out = run(ws, "validate", str(sess))
    assert code == 3 and "criteria" in out
    plan["stages"][-1]["criteria"] = ["涉及人类被试却没有伦理审批"]
    rh.write_json(sess / "plan.json", plan)
    assert run(ws, "validate", str(sess))[0] == 0


@pytest.mark.parametrize("type_id", sorted(p.parent.name for p in (ROOT / "types").glob("*/TYPE.md")))
def test_every_type_makes_a_valid_plan(tmp_path, type_id):
    """The scenario library: every type has aspects with criteria, and init-plan turns it into a valid draft."""
    proj = tmp_path / "proj"
    shutil.copytree(FIXTURE, proj)
    ws = tmp_path / "ws"
    run(ws, "init", str(ws))
    run(ws, "new-session", "--slug", "t", str(proj / "report.md"))
    sess = next((ws / "sessions").iterdir())
    code, out = run(ws, "init-plan", str(sess), "--type", type_id, "--tier", "standard")
    assert code == 0, out
    plan = json.loads((sess / "plan.json").read_text())
    assert plan["aspects"] and all(a.get("criteria") for a in plan["aspects"])
    assert all(s["included"] and s["aspect"] for s in plan["stages"])
    # every type says how its overview is organised and who asks the questions in devil mode
    td = rh.load_type(type_id, ws)
    assert 2 <= len(td["overview"]) <= 6 and td["devil"]["role"] and td["devil"]["cares"]
    assert all(a.get("expert_questions") and a.get("red_flags") for a in td["aspects"])
    errs = [e for e in rh.validate_session(sess, ws)[0] if "TODO" not in e]
    assert errs == []


def test_handoff_stops_the_loop_counting_a_finding(session):
    ws, sess, proj = session
    finish_round(ws, sess, proj)
    assert rh.loop_next(sess) == "revise"
    code, out = run(ws, "handoff", str(sess), "F001", "--reason", "需要原始回复才能重跑，项目里没有")
    assert code == 0, out
    assert rh.loop_next(sess) == "handoff"
    assert "有待作者处理**：需要原始回复" in run(ws, "summary", str(sess), "--print")[1]


def test_aspect_assessments_lead_the_summary(session):
    ws, sess, proj = session
    mp = json.loads((sess / "map.json").read_text())
    mp["assessments"] = [{"aspect": "implementation", "rating": "weak", "verdict": "拒答被当成了起薪，主结论站不住。",
                          "reasons": ["解析只取第一个数字"], "questions": ["拒答本身是否也有性别差异？"]}]
    rh.write_json(sess / "map.json", mp)
    assert run(ws, "validate", sess.name)[0] == 0
    code, out = run(ws, "summary", sess.name, "--print")
    assert "| 实现与测量 | 存在明显问题 | 拒答被当成了起薪，主结论站不住。 |" in out
    assert "## 可以进一步思考的问题" in out and "1. 拒答本身是否也有性别差异？（实现与测量）" in out
    # an assessment must name an aspect of the plan and real findings
    mp["assessments"].append({"aspect": "nope", "rating": "strong", "verdict": "x", "reasons": ["y"], "finding_ids": ["F099"]})
    rh.write_json(sess / "map.json", mp)
    code, out = run(ws, "validate", sess.name)
    assert code != 0 and "方面 nope" in out and "F099" in out


def test_leads_are_questions_for_the_person(session):
    ws, sess, proj = session
    mp = json.loads((sess / "map.json").read_text())
    mp["leads"] = [{"id": "L01", "aspect": "implementation", "kind": "anomaly", "title": "拒答集中在一个模型",
                    "observation": "一个模型的拒答是其他模型的五倍。", "threat": "差异可能来自拒答而不是性别。",
                    "questions": ["这个模型为什么拒答更多？"], "evidence": [],
                    "anchor": {"path": "artifact/report.md", "exact": "平均低 28.6%"}}]
    rh.write_json(sess / "map.json", mp)
    assert run(ws, "validate", sess.name)[0] == 0
    code, out = run(ws, "summary", sess.name, "--print")
    assert "## 值得追问之处" in out and "L01 · 异常结果：拒答集中在一个模型" in out and "这个模型为什么拒答更多？" in out
    # the person answers it, or discusses it
    code, out = run(ws, "add-feedback", sess.name, "--kind", "answer", "--target", "L01", "--text", "是解析的问题")
    assert code == 0, out
    assert rh.read_jsonl(sess / "feedback.jsonl")[-1]["target"] == {"type": "lead", "id": "L01"}
    assert run(ws, "summary", sess.name, "--print")[1].count("（已记下想法）") == 1
    rh.add_message(sess, "human", "我觉得是解析", target="L01")
    assert rh.read_jsonl(sess / "threads.jsonl")[-1]["target"] == {"type": "lead", "id": "L01"}
    assert run(ws, "validate", sess.name)[0] == 0
    # a lead must point at text that exists
    mp["leads"][0]["anchor"]["exact"] = "没有这句话"
    rh.write_json(sess / "map.json", mp)
    code, out = run(ws, "validate", sess.name)
    assert code != 0 and "L01" in out


def test_overview_leads_the_summary(session):
    ws, sess, proj = session
    mp = json.loads((sess / "map.json").read_text())
    part = lambda name, t, refs=(): {"name": name, "text": t, "refs": list(refs)}
    mp["overview"] = {"parts": [part("研究叙事", "作者认为模型压低女性起薪。"), part("测量", "起薪取自模型回复。", ["D01"]),
                                part("分析", "配对比较。"), part("结果与解读", "结论取决于拒答怎样处理。")],
                      "key_questions": [{"id": "Q1", "title": "拒答怎样处理", "body": "拒答若被记成数值，差距可能是假象。",
                                         "refs": ["D01"]}]}
    rh.write_json(sess / "map.json", mp)
    assert run(ws, "validate", sess.name)[0] == 0
    out = run(ws, "summary", sess.name, "--print")[1]
    assert out.index("## 概览") < out.index("## 关键问题") < out.index("| 方面 |")
    assert "**测量**　起薪取自模型回复。（见 D01）" in out
    mp["overview"]["key_questions"][0]["refs"] = ["F099"]
    rh.write_json(sess / "map.json", mp)
    code, out = run(ws, "validate", sess.name)
    assert code != 0 and "Q1" in out and "F099" in out


def test_edit_finding_rewrites_wording_and_keeps_history(session):
    ws, sess, proj = session
    add(ws, sess, candidate(proj))
    code, out = run(ws, "edit-finding", sess.name, "F001",
                    stdin=json.dumps({"title": "拒答被记成了起薪", "note": "verify"}, ensure_ascii=False))
    assert code == 0, out
    recs = [r for r in rh.read_jsonl(sess / "findings.jsonl") if r["id"] == "F001"]
    assert [r["rev"] for r in recs] == [1, 2] and recs[-1]["note"] == "verify" and recs[-1]["title"] == "拒答被记成了起薪"
    code, out = run(ws, "edit-finding", sess.name, "F001", stdin=json.dumps({"evidence": []}))
    assert code != 0 and "evidence" in out
    assert run(ws, "validate", sess.name)[0] == 0


def test_readiness_handback_and_process(session):
    ws, sess, proj = session
    finish_round(ws, sess, proj)
    mp = json.loads((sess / "map.json").read_text())
    mp["readiness"] = {"verdict": "revise_first", "text": "拒答的编码决定主结论，需要作者先修正。",
                       "tasks": [{"id": "T1", "title": "修正拒答的编码", "why": "拒答被记成了起薪。", "refs": ["F001"]}]}
    rh.write_json(sess / "map.json", mp)
    assert run(ws, "validate", sess.name)[0] == 0
    code, out = run(ws, "handback", sess.name, "--print")
    assert code == 0 and "### T1 修正拒答的编码" in out and "拒答被记成了起薪" in out
    out = run(ws, "summary", sess.name, "--print")[1]
    assert "## 审阅过程" in out and "建议先退回作者修改" in out and "**第 1 轮**" in out
    pr = rh.review_process(sess)
    assert pr["rounds"][0]["kept"] >= 1 and pr["rounds"][0]["raised"]["total"] >= 1
    # a verdict to send back needs tasks, and tasks must point at real notes
    mp["readiness"]["tasks"][0]["refs"] = ["F099"]
    rh.write_json(sess / "map.json", mp)
    code, out = run(ws, "validate", sess.name)
    assert code != 0 and "F099" in out


def test_signals_gather_responses_across_reviews(session):
    ws, sess, proj = session
    finish_round(ws, sess, proj)
    run(ws, "add-feedback", sess.name, "--kind", "reaction", "--target", "F001", "--reaction", "ai_wrong", "--note", "拒答本来就单列了")
    code, out = run(ws, "add-feedback", sess.name, "--kind", "missed", "--text", "没有检查名字是否暗示种族", "--note", "§2")
    assert code == 0, out
    run(ws, "add-feedback", sess.name, "--kind", "suggestion", "--text", "先看样本量", "--scope", "this_type")
    code, out = run(ws, "signals")
    assert code == 0 and "AI 漏掉的问题 1 条" in out and "不成立 100%" in out
    sig = json.loads((ws / "ledger" / "signals.json").read_text())
    r = next(iter(sig.values()))
    assert r["rejected_items"][0]["note"] == "拒答本来就单列了"
    assert r["missed"][0]["text"] == "没有检查名字是否暗示种族" and r["suggestions"][0]["text"] == "先看样本量"
    # the retrospective at the end of one review looks at that review only
    code, out = run(ws, "signals", "--session", sess.name)
    assert code == 0 and "（1 次审阅）" in out and (ws / "ledger" / f"signals-{sess.name}.json").exists()


def test_edit_of_a_repeated_phrase_is_placed_by_its_context(session):
    ws, sess, proj = session
    finish_round(ws, sess, proj)
    rep = sess / "artifact" / "report.md"
    rep.write_text(rep.read_text() + "\n附注：No\n\n结论：No\n", encoding="utf-8")
    run(ws, "snapshot", str(sess), "--force")
    run(ws, "round-start", str(sess)); run(ws, "round-end", str(sess), "--agents", "1", "--candidates", "0")
    assert run(ws, "revise-begin", str(sess))[0] == 0
    edit(sess, "结论：No", "结论：Yes")
    code, out = run(ws, "add-change", str(sess), stdin=change(summary="结论改为 Yes", before="No", after="结论：Yes",
                                                               finding_ids=[]))
    code, out = run(ws, "revise-end", str(sess))
    assert code == 0, out  # the second "No", not the first


def test_an_error_in_the_ais_own_edit_is_fixed_before_handover(session):
    ws, sess, proj = session
    finish_round(ws, sess, proj)                          # round 1: an S1
    assert run(ws, "set-finding-status", str(sess), "F001", "fixed")[0] == 0
    assert run(ws, "round-start", str(sess))[0] == 0      # round 2 looks at the AI's edits and finds a minor error
    code, out = add(ws, sess, candidate(proj, severity="S3", problem_kind="bad_revision", title="AI 改错了"), rnd="2")
    assert code == 0, out
    assert run(ws, "round-end", str(sess), "--agents", "2", "--candidates", "1")[0] == 0
    assert rh.loop_next(sess) == "revise"                # not handed over with the AI's mistake in it


def test_evaluating_someone_elses_work_is_not_rewritten(session):
    ws, sess, proj = session
    plan = json.loads((sess / "plan.json").read_text())
    plan["user_role"] = "evaluator"
    rh.write_json(sess / "plan.json", plan)
    finish_round(ws, sess, proj)                          # an open S1 after round 1
    assert rh.loop_next(sess) == "handoff"               # reviewed, not revised: the issue goes to the author
    assert run(ws, "validate", str(sess))[0] == 0


def as_plan(ws, sess):
    """Make the session's type a plan (TYPE.md `work: plan`), via a workspace copy of its TYPE.md."""
    t = json.loads((sess / "plan.json").read_text())["classification"]["types"][0]
    src = (ROOT / "types" / t / "TYPE.md").read_text(encoding="utf-8")
    (ws / "types" / t).mkdir(parents=True, exist_ok=True)
    (ws / "types" / t / "TYPE.md").write_text(src.replace("---\n", "---\nwork: plan\n", 1), encoding="utf-8")


def test_a_plan_is_judged_as_a_plan(session):
    ws, sess, proj = session
    finish_round(ws, sess, proj)
    as_plan(ws, sess)
    mp = json.loads((sess / "map.json").read_text())
    aspect = json.loads((sess / "plan.json").read_text())["aspects"][0]["id"]
    mp["assessments"] = [{"aspect": aspect, "rating": "weak", "verdict": "没有想清楚。", "reasons": ["只列了主题。"]}]
    mp["overview"] = {"parts": [{"name": "核心点", "text": "重心未定。"}, {"name": "最短路径", "text": "先做最难的。"}],
                      "key_questions": []}
    mp["readiness"] = {"verdict": "ready", "text": "可以开始，边做边定。",
                       "tasks": [{"id": "T1", "title": "先试十个人", "why": "检验最没把握的假设。"}]}
    rh.write_json(sess / "map.json", mp)
    code, out = run(ws, "validate", sess.name)
    # a plan is rated on how far it is thought through, carries a suggested path, and lists no hand-back tasks
    assert code != 0 and "weak" in out and "overview.path" in out and "不列退回任务" in out
    mp["assessments"][0]["rating"] = "partial"
    mp["overview"]["path"] = {"text": "先检验最没把握的一点，再做其余。",
                              "steps": [{"title": "手工给十个人推一周", "why": "看推荐是否有用。", "refs": ["F001"]}]}
    mp["readiness"]["tasks"] = []
    rh.write_json(sess / "map.json", mp)
    code, out = run(ws, "validate", sess.name)
    assert code == 0, out
    out = run(ws, "summary", sess.name, "--print")[1]
    assert "## 建议的路径" in out and "1. **手工给十个人推一周**" in out and "有方向，未想透" in out
    assert "可以着手实施" in out


def test_plan_leads_use_plan_kinds(session):
    ws, sess, proj = session
    as_plan(ws, sess)
    mp = json.loads((sess / "map.json").read_text())
    lead = {"id": "L01", "kind": "rival_explanation", "title": "需求只来自熟人", "observation": "只问了三位同学。",
            "threat": "需求可能被高估。", "questions": ["陌生的研究者会用吗？"], "evidence": [],
            "anchor": {"path": "artifact/report.md", "exact": "配对固定效应"}}
    mp["leads"] = [lead]
    rh.write_json(sess / "map.json", mp)
    code, out = run(ws, "validate", sess.name)
    assert code != 0 and "rival_explanation" in out
    lead["kind"] = "untested_assumption"
    rh.write_json(sess / "map.json", mp)
    assert run(ws, "validate", sess.name)[0] == 0
    out = run(ws, "summary", sess.name, "--print")[1]
    assert "未经检验的假设" in out and "方案还没有想透" in out and "对方案的影响" in out


def test_a_finding_merges_into_a_choice(session):
    ws, sess, proj = session
    add(ws, sess, candidate(proj))
    code, out = run(ws, "set-finding-status", sess.name, "F001", "superseded", "--superseded-by", "D01")
    assert code == 0, out
    assert run(ws, "validate", sess.name)[0] == 0
    code, out = run(ws, "set-finding-status", sess.name, "F001", "open")
    assert run(ws, "edit-finding", sess.name, "F001", stdin=json.dumps({"decision_ids": ["D09"]}))[0] == 0
    code, out = run(ws, "validate", sess.name)
    assert code != 0 and "D09" in out
