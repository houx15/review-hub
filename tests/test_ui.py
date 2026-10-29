"""Tests for scripts/rh_ui.py: the JSON API, and (when Playwright and Chrome are
available) the page itself driven in a real browser.

Run: uv run --no-project --with pytest --with jsonschema --with pyyaml --with rfc3339-validator \
       --with playwright pytest tests
The browser tests use the installed Google Chrome (channel="chrome"); they are
skipped when Playwright or Chrome is missing.
"""
import http.client
import importlib.util
import json
import threading
from pathlib import Path

import pytest

from test_rh import ROOT, add, candidate, rh, run, session  # noqa: F401  (session is a fixture)

spec = importlib.util.spec_from_file_location("rh_ui", ROOT / "scripts" / "rh_ui.py")
rh_ui = importlib.util.module_from_spec(spec)
spec.loader.exec_module(rh_ui)


@pytest.fixture
def server(session):
    ws, sess, proj = session
    code, out = add(ws, sess, candidate(proj))
    assert code == 0, out
    srv = rh_ui.make_server(ws, 0)
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    yield srv.server_address[1], ws, sess, proj
    srv.shutdown()
    srv.server_close()


def call(port, method, path, body=None, headers=None):
    c = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
    h = {"Content-Type": "application/json", **(headers or {})}
    c.request(method, path, body=json.dumps(body) if body is not None else None, headers=h)
    r = c.getresponse()
    data = r.read()
    try:
        data = json.loads(data)
    except ValueError:
        pass
    return r.status, data


def test_sessions_and_state(server):
    port, ws, sess, _ = server
    st, data = call(port, "GET", "/api/sessions")
    assert st == 200 and data["sessions"][0]["id"] == sess.name
    assert data["sessions"][0]["open"]["S1"] == 1
    st, data = call(port, "GET", f"/api/s/{sess.name}/state")
    assert st == 200
    assert data["artifact"][0]["path"] == "artifact/report.md"
    assert data["findings"][0]["id"] == "F001"
    assert data["status"]["unresolved_findings"] == ["F001"]
    assert data["prediction_mask"] == []  # nothing on the page is held back
    st, v = call(port, "GET", f"/api/s/{sess.name}/version")
    assert v["version"] == data["version"]


def test_feedback_written_with_ui_source_and_anchor(server):
    port, ws, sess, _ = server
    st, data = call(port, "POST", f"/api/s/{sess.name}/feedback",
                    {"kind": "reaction", "target": "F001", "reaction": "fixed", "fix_by": "ai", "note": "好"})
    assert st == 200, data
    rec = rh.read_jsonl(sess / "feedback.jsonl")[-1]
    assert rec["source"] == "ui" and rec["reaction"] == "fixed" and rec["anchor"]["exact"] == "拒绝作答的记录视为缺失"
    st, data = call(port, "POST", f"/api/s/{sess.name}/feedback", {"kind": "choice", "target": "D02", "option": "O3"})
    assert st == 200, data
    st, data = call(port, "POST", f"/api/s/{sess.name}/feedback", {"kind": "plan_confirm"})
    assert st == 400  # the UI cannot confirm plans


def test_signoff_with_open_s1_is_recorded_not_blocked(server):
    port, ws, sess, _ = server
    st, data = call(port, "POST", f"/api/s/{sess.name}/feedback", {"kind": "signoff", "outcome": "another_round"})
    assert st == 200 and "F001" in data["record"]["signoff"]["acknowledged"]
    call(port, "POST", f"/api/s/{sess.name}/feedback", {"kind": "reaction", "target": "F001", "reaction": "ai_wrong"})
    st, data = call(port, "POST", f"/api/s/{sess.name}/feedback",
                    {"kind": "signoff", "outcome": "deliver", "quality": 4})
    assert st == 200, data
    assert data["record"]["signoff"]["quality_score"] == 4


def test_bad_feedback_is_rejected(server):
    port, ws, sess, _ = server
    st, data = call(port, "POST", f"/api/s/{sess.name}/feedback", {"kind": "reaction", "target": "F999", "reaction": "fixed"})
    assert st == 400 and "F999" in data["error"]
    st, data = call(port, "POST", f"/api/s/{sess.name}/feedback", {"kind": "reaction", "target": "F001"})
    assert st == 400  # schema: reaction required
    assert len(rh.read_jsonl(sess / "feedback.jsonl")) == 0


def test_host_and_origin_checks(server):
    port, ws, sess, _ = server
    st, _ = call(port, "GET", "/api/sessions", headers={"Host": "evil.example"})
    assert st == 403
    st, _ = call(port, "POST", f"/api/s/{sess.name}/feedback",
                 {"kind": "reaction", "target": "F001", "reaction": "fixed"}, headers={"Origin": "http://evil.example"})
    assert st == 403
    assert rh.read_jsonl(sess / "feedback.jsonl") == []


def test_file_reading_is_confined(server):
    port, ws, sess, proj = server
    p = proj / "src" / "parse_salary.py"
    st, data = call(port, "GET", f"/api/s/{sess.name}/file?path={p}")
    assert st == 200 and "re.search" in data["text"]
    st, _ = call(port, "GET", f"/api/s/{sess.name}/file?path=/etc/passwd")
    assert st == 403
    st, _ = call(port, "GET", f"/api/s/{sess.name}/file?path={proj}/../../../../etc/passwd")
    assert st == 403
    st, _ = call(port, "GET", "/../scripts/rh.py")
    assert st == 404
    st, _ = call(port, "GET", "/api/s/..%2F..%2Fetc/state")
    assert st in (400, 404)


def test_time_accumulates_in_metrics(server):
    port, ws, sess, _ = server
    for _ in range(3):
        st, data = call(port, "POST", f"/api/s/{sess.name}/time", {"minutes": 0.5, "stage": "measurement"})
        assert st == 200, data
    m = rh.read_json(sess / "metrics.json")
    assert m["human"] == {"measured_by": "ui_timer", "minutes": 1.5, "by_stage_minutes": {"measurement": 1.5}}
    st, _ = call(port, "POST", f"/api/s/{sess.name}/time", {"minutes": 30})
    assert st == 400


def test_static_files(server):
    port, *_ = server
    st, body = call(port, "GET", "/")
    assert st == 200 and b"app.js" in body
    st, _ = call(port, "GET", "/vendor/preact-htm.module.js")
    assert st == 200


# ---------------------------------------------------------------- browser

def _browser():
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        pytest.skip("playwright 未安装")
    pw = sync_playwright().start()
    try:
        b = pw.chromium.launch(channel="chrome")
    except Exception as e:  # Chrome not installed
        pw.stop()
        pytest.skip(f"没有可用的 Chrome：{e}")
    return pw, b


@pytest.fixture
def page(server):
    port, ws, sess, proj = server
    # add a prediction decision anchored on the conclusion, to exercise the gate
    mp = rh.read_json(sess / "map.json")
    mp["decisions"].append({
        "id": "D03", "stage": "conclusion", "title": "主结果", "question": "模型是否压低女性起薪？",
        "options": [{"id": "O1", "label": "低 28.6%，显著", "chosen": True}],
        "criteria": [], "evidence": [], "upstream": [], "downstream": [], "verifiability": "J+V",
        "load_bearing": {"score": 3, "basis": "judgement", "rationale": "主结论"}, "status": "awaiting_human",
        "human_action": "predict", "prompt_to_human": "你预期模型给女性的起薪比男性低多少？",
        "anchor": rh.make_anchor(sess, "大模型在起薪建议中普遍存在对女性的系统性压低"),
    })
    rh.write_json(sess / "map.json", mp)
    code, out = run(ws, "validate", str(sess))
    assert code == 0, out
    pw, b = _browser()
    pg = b.new_page(viewport={"width": 1400, "height": 900})
    errors = []
    pg.on("pageerror", lambda e: errors.append(str(e)))
    pg.goto(f"http://127.0.0.1:{port}/#/s/{sess.name}")
    pg.wait_for_selector(".review")
    yield pg, sess, errors
    b.close()
    pw.stop()


def test_browser_nothing_is_held_back(page):
    pg, sess, errors = page
    # results and problems are visible from the start; a "predict" decision is just an open question
    assert pg.locator(".masked").count() == 0
    assert "28.6%" in pg.locator(".doc").inner_text()
    pg.wait_for_selector("mark.hl.f.s1")
    pg.click('.node:has-text("D03")')
    pg.wait_for_selector('[data-card="D03"].open')
    pg.fill('[data-card="D03"] textarea', "我原以为差距更小")
    pg.click('[data-card="D03"] >> text=记下')
    pg.wait_for_function("() => document.querySelector('.toast') && document.querySelector('.toast').textContent.includes('已记录')")
    fb = rh.read_jsonl(sess / "feedback.jsonl")
    assert fb[-1]["kind"] == "answer" and fb[-1]["source"] == "ui" and fb[-1]["target"]["id"] == "D03"

    # clicking the highlight opens the finding card; react to it
    pg.click("mark.hl.f.s1")
    pg.wait_for_selector('[data-card="F001"].open')
    pg.click('[data-card="F001"] >> text=不成立')
    pg.wait_for_function("() => !document.querySelector('mark.hl.f.s1.open')")
    assert rh.read_jsonl(sess / "feedback.jsonl")[-1]["reaction"] == "ai_wrong"

    # choose another option on D02
    pg.click('.node:has-text("D02")')
    pg.wait_for_selector('[data-card="D02"].open')
    pg.locator('[data-card="D02"] .opt').nth(2).click()
    pg.click('[data-card="D02"] >> text=改用这一方案')
    pg.wait_for_function("() => document.querySelector('.toast') && document.querySelector('.toast').textContent.includes('已记录')")
    rec = rh.read_jsonl(sess / "feedback.jsonl")[-1]
    assert rec["kind"] == "choice" and rec["option_id"] == "O3"

    # finish the review
    pg.click("button[role=tab]:has-text('完成审阅')")
    pg.click(".stars button >> nth=3")
    pg.click("text=定稿")
    pg.wait_for_function("() => document.querySelector('.toast') && document.querySelector('.toast').textContent.includes('已记录')")
    rec = rh.read_jsonl(sess / "feedback.jsonl")[-1]
    assert rec["kind"] == "signoff" and rec["signoff"]["outcome"] == "deliver" and rec["signoff"]["quality_score"] == 4
    # finishing tells the conversation, where the AI takes its last step (write back, then learn from this review)
    pg.wait_for_function("() => document.querySelector('.toast') && document.querySelector('.toast').textContent.includes('对话中继续')")
    assert rh.read_jsonl(sess / "threads.jsonl")[-1]["text"] == "[完成审阅] 定稿"
    assert errors == []


def test_browser_finishing_is_never_blocked_and_file_viewer(page):
    pg, sess, errors = page
    pg.wait_for_selector("mark.hl.f.s1")
    pg.click("button[role=tab]:has-text('完成审阅')")
    assert not pg.locator("button:has-text('定稿')").is_disabled()  # an open S1 is listed, not a lock
    assert "重大问题未处理" in pg.locator(".signoff").inner_text()
    pg.click("button[role=tab]:has-text('审阅意见')")
    pg.click('[data-card="F001"] .card-head')
    pg.click('[data-card="F001"] .ev-fold summary')
    pg.click('[data-card="F001"] .ev-head')
    pg.wait_for_selector(".modal .ln.hit")
    assert "re.search" in pg.locator(".modal .ln.hit").first.inner_text()
    assert errors == []


def test_browser_reloads_when_files_change(page):
    pg, sess, errors = page
    pg.wait_for_selector("mark.hl.f.s1")
    # an agent writes feedback from the chat; the page picks it up by polling
    code, out = run(sess.parent.parent, "add-feedback", str(sess), "--kind", "reaction", "--target", "F001",
                    "--reaction", "doesnt_matter")
    assert code == 0, out
    pg.wait_for_function("() => !document.querySelector('mark.hl.f.s1.open')", timeout=8000)
    assert errors == []


# ---------------------------------------------------------------- AI edits, discussion, suggestions

def revised(ws, sess, proj):
    """Round 1 found F001; the AI edited its copy (R001) and round 2 confirmed the fix."""
    from test_rh import change, edit
    assert run(ws, "round-start", str(sess))[0] == 0
    assert run(ws, "round-end", str(sess), "--agents", "2", "--candidates", "1")[0] == 0
    assert run(ws, "revise-begin", str(sess))[0] == 0
    edit(sess, "拒绝作答的记录视为缺失", "拒绝作答的记录单独标出并剔除")
    code, out = run(ws, "add-change", str(sess), stdin=change())
    assert code == 0, out
    assert run(ws, "revise-end", str(sess))[0] == 0
    assert run(ws, "set-finding-status", str(sess), "F001", "fixed")[0] == 0


def test_api_messages_suggestions_and_change_feedback(server):
    port, ws, sess, proj = server
    revised(ws, sess, proj)
    st, data = call(port, "GET", f"/api/s/{sess.name}/state")
    assert [c["id"] for c in data["changes"]] == ["R001"]
    assert "视为缺失" in data["baseline"][0]["text"] and "单独标出" in data["artifact"][0]["text"]
    st, data = call(port, "POST", f"/api/s/{sess.name}/message", {"text": "为什么要剔除？", "target": "R001"})
    assert st == 200 and data["record"]["author"] == "human" and data["record"]["source"] == "ui"
    st, _ = call(port, "POST", f"/api/s/{sess.name}/message", {"text": "  "})
    assert st == 400
    st, data = call(port, "GET", f"/api/s/{sess.name}/state")
    assert data["status"]["inbox"] == ["M0001"]
    st, data = call(port, "POST", f"/api/s/{sess.name}/feedback", {"kind": "confirm", "target": "R001", "value": "wrong"})
    assert st == 200, data
    st, data = call(port, "POST", f"/api/s/{sess.name}/feedback",
                    {"kind": "suggestion", "text": "先看标题", "scope": "general"})
    assert st == 200 and data["record"]["scope"] == "general"
    # the page can't post AI messages
    call(port, "POST", f"/api/s/{sess.name}/message", {"text": "我是 AI", "author": "ai"})
    assert all(m["author"] == "human" for m in rh.read_jsonl(sess / "threads.jsonl"))


def test_browser_changes_discussion_and_suggestion(server):
    port, ws, sess, proj = server
    revised(ws, sess, proj)
    pw, b = _browser()
    pg = b.new_page(viewport={"width": 1400, "height": 900})
    errors = []
    pg.on("pageerror", lambda e: errors.append(str(e)))
    pg.goto(f"http://127.0.0.1:{port}/#/s/{sess.name}")
    pg.wait_for_selector(".review")
    try:
        # areas that need the person are marked, with the question written above them
        zc = pg.locator('.zcall.suggest[data-ids="D02"]')
        assert "职业固定效应" in zc.inner_text() or "固定效应" in zc.inner_text()
        zc.click()
        pg.wait_for_selector('[data-card="D02"].open')
        pg.click(".zonebar input")
        pg.wait_for_selector(".review.focus .blk.nozone")
        pg.click(".zonebar input")
        # the AI's edit is marked in the text; clicking it opens the change card
        pg.click("mark.hl.chg")
        pg.wait_for_selector('[data-card="R001"].open')
        assert "视为缺失" in pg.locator('[data-card="R001"] .diff del').inner_text()
        # discuss it; the message lands in threads.jsonl and waits for the agent
        pg.click('[data-card="R001"] >> text=与 AI 讨论')
        pg.fill('[data-card="R001"] .discuss textarea', "剔除会不会改变样本量？")
        pg.click('[data-card="R001"] .discuss >> text=发送')
        pg.wait_for_selector('[data-card="R001"] .msg.human')
        assert rh.read_jsonl(sess / "threads.jsonl")[-1]["target"] == {"type": "change", "id": "R001"}
        # the agent replies from the chat; the page picks it up by polling
        assert run(ws, "reply", str(sess), "--to", "M0001", "--text", "会，样本从 60 对变成 52 对。")[0] == 0
        pg.wait_for_selector('[data-card="R001"] .msg.ai', timeout=8000)
        # undo it
        pg.click('[data-card="R001"] >> text=撤销')
        pg.wait_for_selector("mark.hl.chg.undo")
        assert rh.read_jsonl(sess / "feedback.jsonl")[-1]["value"] == "wrong"
        # the original version is one click away
        pg.click(".viewtoggle >> text=原稿")
        assert "视为缺失" in pg.locator(".doc.baseline").inner_text()
        pg.click(".viewtoggle >> text=修改稿")
        # a suggestion for future reviews
        pg.click("button[role=tab]:has-text('讨论')")
        pg.fill('.talk .qsec:has(h3:has-text("对审阅的建议")) textarea', "审学术报告时先看标题和数据回答的是否一致")
        pg.click("text=记录建议")
        pg.wait_for_selector(".talk >> text=已记录的建议")
        rec = rh.read_jsonl(sess / "feedback.jsonl")[-1]
        assert rec["kind"] == "suggestion" and rec["scope"] == "this_type"
        assert errors == []
    finally:
        b.close()
        pw.stop()


def test_browser_lead_is_a_highlighted_question(server):
    port, ws, sess, proj = server
    mp = rh.read_json(sess / "map.json")
    mp["leads"] = [{"id": "L01", "aspect": "implementation", "kind": "anomaly", "title": "拒答集中在一个模型",
                    "observation": "一个模型的拒答是其他模型的五倍。", "threat": "差异可能来自拒答而不是性别。",
                    "questions": ["这个模型为什么拒答更多？", "去掉它结论还在吗？"], "evidence": [],
                    "anchor": rh.make_anchor(sess, "平均低 28.6%")}]
    rh.write_json(sess / "map.json", mp)
    assert run(ws, "validate", str(sess))[0] == 0
    pw, b = _browser()
    pg = b.new_page(viewport={"width": 1400, "height": 900})
    errors = []
    pg.on("pageerror", lambda e: errors.append(str(e)))
    pg.goto(f"http://127.0.0.1:{port}/#/s/{sess.name}")
    pg.wait_for_selector(".review")
    try:
        # the passage is marked with the question above it, in the lead colour
        zc = pg.locator('.zcall.explore[data-ids="L01"]')
        assert "这个模型为什么拒答更多？" in zc.inner_text() and "异常结果" in zc.inner_text()
        assert pg.locator(".zonebar .zchip.explore").count() == 1
        zc.click()
        card = pg.locator('[data-card="L01"]')
        pg.wait_for_selector('[data-card="L01"].open')
        assert "去掉它结论还在吗？" in card.inner_text() and "对主要结论的影响" in card.inner_text()
        # the person answers; nothing is fixed for them
        card.locator("textarea").first.fill("是解析失败被当成了拒答，要回去看代码")
        card.locator("text=记下").click()
        # answered: the highlight clears and the lead moves to the done list
        pg.wait_for_selector('.zcall.explore[data-ids="L01"]', state="detached")
        rec = rh.read_jsonl(sess / "feedback.jsonl")[-1]
        assert rec["kind"] == "answer" and rec["target"] == {"type": "lead", "id": "L01"}
        assert errors == []
    finally:
        b.close()
        pw.stop()


def test_browser_overview_sits_above_the_text(server):
    port, ws, sess, proj = server
    mp = rh.read_json(sess / "map.json")
    part = lambda name, t, refs=(): {"name": name, "text": t, "refs": list(refs)}
    mp["overview"] = {"parts": [part("研究叙事", "作者认为模型压低女性起薪。"), part("测量", "起薪取自模型回复。", ["F001"]),
                                part("分析", "配对比较。"), part("结果与解读", "结论取决于拒答怎样处理。")],
                      "key_questions": [{"id": "Q1", "title": "拒答怎样处理。", "body": "拒答若被记成数值，差距可能是假象。",
                                         "refs": ["F001"]}]}
    rh.write_json(sess / "map.json", mp)
    assert run(ws, "validate", str(sess))[0] == 0
    pw, b = _browser()
    pg = b.new_page(viewport={"width": 1400, "height": 900})
    errors = []
    pg.on("pageerror", lambda e: errors.append(str(e)))
    pg.goto(f"http://127.0.0.1:{port}/#/s/{sess.name}")
    pg.wait_for_selector(".overview")
    try:
        ov = pg.locator(".overview").inner_text()
        for t in ("研究叙事", "测量", "分析", "结果与解读", "关键问题", "拒答若被记成数值"):
            assert t in ov
        # a reference opens the note it points at, in the note's colour
        pg.locator(".ov-q .ref.suggest").first.click()
        pg.wait_for_selector('[data-card="F001"].open')
        assert "💡 建议" in pg.locator('[data-card="F001"] .dtag').inner_text()
        # devil mode is offered right there, with the role that fits the genre
        assert "答辩委员会中最严格的教授" in pg.locator(".overview .devil").inner_text()
        pg.click(".overview .devil button")
        pg.wait_for_selector(".overview .devil button:has-text('已请求')")
        assert rh.read_jsonl(sess / "threads.jsonl")[-1]["text"].startswith("[答辩模式]")
        assert errors == []
    finally:
        b.close()
        pw.stop()


def test_reading_activity_is_saved_locally_and_used_for_learning(server):
    port, ws, sess, proj = server
    t0 = 1_800_000_000_000
    st, data = call(port, "POST", f"/api/s/{sess.name}/activity",
                    {"events": [{"kind": "open", "id": "F001", "t": t0}, {"kind": "close", "id": "F001", "t": t0 + 90_000},
                                {"kind": "ref", "id": "F001", "t": t0 + 91_000}]})
    assert st == 200 and data["saved"] == 3
    st, _ = call(port, "POST", f"/api/s/{sess.name}/activity", {"events": [{"kind": "open", "id": "../x", "t": 1}]})
    assert st == 400
    # it does not count as a change to the review, so the page does not reload over it
    v1 = call(port, "GET", f"/api/s/{sess.name}/version")[1]
    call(port, "POST", f"/api/s/{sess.name}/activity", {"events": [{"kind": "zone", "id": "F001", "t": t0}]})
    assert call(port, "GET", f"/api/s/{sess.name}/version")[1] == v1
    # F001 was read for a minute and a half without a response: a sign it may be unclear
    code, out = run(ws, "signals", "--session", sess.name)
    assert code == 0 and "1 条看了一分钟以上却没有回应" in out
