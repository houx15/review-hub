#!/usr/bin/env python3
# /// script
# requires-python = ">=3.9"
# dependencies = ["jsonschema>=4.18", "pyyaml>=6", "rfc3339-validator"]
# ///
"""rh_ui: the local review UI server.

Serves ui/ and a small JSON API over the workspace's session files. It never
calls a model and never talks to the network: it only reads session files,
appends to feedback.jsonl and updates the human-time fields of metrics.json,
using the same functions as rh.py. Listens on 127.0.0.1 only.

    uv run scripts/rh_ui.py [--workspace <ws>] [<session>] [--port 8765] [--open]
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import subprocess
import sys
import threading
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlparse

sys.path.insert(0, str(Path(__file__).resolve().parent))
import rh  # noqa: E402

UI_DIR = rh.ENGINE_ROOT / "ui"
MIME = {".html": "text/html", ".js": "text/javascript", ".css": "text/css", ".svg": "image/svg+xml",
        ".json": "application/json"}
MAX_FILE_BYTES = 2_000_000
MAX_MINUTES_PER_TICK = 2.0
WRITE_LOCK = threading.Lock()


# ---------------------------------------------------------------- session data

def list_sessions(ws: Path) -> list:
    out = []
    root = ws / "sessions"
    if not root.is_dir():
        return out
    for sess in sorted(root.iterdir(), reverse=True):
        if not (sess / "sources.json").is_file():
            continue
        item = {"id": sess.name, "title": sess.name, "status": "no_plan"}
        try:
            if (sess / "plan.json").exists():
                plan = rh.read_json(sess / "plan.json")
                item["title"] = plan["artifact"]["title"]
                item["tier"] = plan["tier"]
                item["status"] = plan["status"]
            if (sess / "map.json").exists():
                item["status"] = "reviewed"
                u = rh.unresolved(sess)
                item["open"] = {s: sum(f["severity"] == s for f in u["findings"]) for s in rh.SEVERITIES}
                item["awaiting"] = len(u["awaiting"])
            fb = rh.read_jsonl(sess / "feedback.jsonl")
            so = [r for r in fb if r["kind"] == "signoff"]
            if so:
                item["signoff"] = so[-1]["signoff"]["outcome"]
                item["signed_current"] = so[-1]["artifact_hash"] == rh.artifact_hash(sess)
            item["updated"] = max(p.stat().st_mtime for p in sess.iterdir())
        except (rh.RHError, OSError, KeyError, ValueError) as e:
            item["error"] = str(e)
        out.append(item)
    return out


def session_version(sess: Path) -> str:
    """Changes whenever any file the UI shows changes."""
    h = hashlib.sha256()
    paths = [sess / n for n in ("plan.json", "map.json", "findings.jsonl", "feedback.jsonl",
                                "certificate.md", "metrics.json", "changes.jsonl", "threads.jsonl")]
    paths += rh.artifact_files(sess)
    for p in paths:
        if p.exists():
            st = p.stat()
            h.update(f"{p.name}:{st.st_mtime_ns}:{st.st_size};".encode())
    return h.hexdigest()[:16]


def read_opt_json(p: Path):
    return rh.read_json(p) if p.exists() else None


def session_state(sess: Path) -> dict:
    findings = rh.read_jsonl(sess / "findings.jsonl")
    latest = rh.latest_by_id(findings)
    fb = rh.read_jsonl(sess / "feedback.jsonl")
    state = {
        "id": sess.name,
        "path": str(sess),
        "version": session_version(sess),
        "artifact_hash": rh.artifact_hash(sess),
        "artifact": [{"path": p.relative_to(sess).as_posix(),
                      "text": p.read_text(encoding="utf-8", errors="replace")}
                     for p in rh.artifact_files(sess)],
        "sources": read_opt_json(sess / "sources.json"),
        "plan": read_opt_json(sess / "plan.json"),
        "map": read_opt_json(sess / "map.json"),
        "findings": list(latest.values()),
        "feedback": fb,
        "certificate": (sess / "certificate.md").read_text(encoding="utf-8")
        if (sess / "certificate.md").exists() else None,
        "metrics": read_opt_json(sess / "metrics.json"),
        "changes": list(rh.changes(sess).values()),
        "threads": rh.threads(sess),
        "baseline": [{"path": f["snapshot"], "text": rh.baseline_path(sess, f["snapshot"]).read_text(
                         encoding="utf-8", errors="replace")}
                     for f in (read_opt_json(sess / "sources.json") or {"files": []})["files"]
                     if rh.baseline_path(sess, f["snapshot"]).exists()],
        "project_root": str(rh.project_root(sess) or ""),
        "home": str(Path.home()),
    }
    if state["plan"]:
        t = rh.load_type(state["plan"]["classification"]["types"][0], sess.parent.parent)
        state["prediction_mask"] = (t or {}).get("prediction_mask", [])
        state["type_info"] = {"name": (t or {}).get("name"), "overview": (t or {}).get("overview", []),
                              "devil": (t or {}).get("devil"), "work": rh.work_kind(t)}
    if state["metrics"] and state["metrics"]["ai"]["rounds"] and state["plan"]:
        state["process"] = rh.review_process(sess)
    if state["map"]:
        u = rh.unresolved(sess)
        st = rh.round_stats(sess)
        state["status"] = {
            "unresolved_findings": [f["id"] for f in u["findings"]],
            "ai_default": [d["id"] for d in u["ai_default"]],
            "awaiting": [d["id"] for d in u["awaiting"]],
            "unsourced": [c["id"] for c in u["unsourced"]],
            "rounds": st,
            "convergence": rh.convergence(st),
            "round_open": rh.current_round(sess),
            "loop": rh.loop_next(sess),
            "inbox": [m["id"] for m in rh.inbox(sess)],
        }
    return state


def allowed_roots(sess: Path) -> list:
    roots = [sess.resolve()]
    pr = rh.project_root(sess)
    if pr:
        roots.append(pr.resolve())
    plan = read_opt_json(sess / "plan.json") or {}
    for g in plan.get("ground_truth", []):
        if g.get("available") and g.get("location"):
            roots.append(Path(g["location"]).expanduser().resolve())
    return roots


def read_source_file(sess: Path, path: str) -> dict:
    p = Path(path).expanduser()
    if not p.is_absolute():
        p = sess / p
    p = p.resolve()
    if not any(p == r or r in p.parents for r in allowed_roots(sess)):
        raise rh.RHError("只能查看本次审阅、原文所在项目和计划中登记的材料", 403)
    if not p.is_file():
        raise rh.RHError(f"文件不存在：{path}", 404)
    if p.stat().st_size > MAX_FILE_BYTES:
        raise rh.RHError("文件过大，无法在页面中显示", 413)
    raw = p.read_bytes()
    if b"\0" in raw[:4096]:
        raise rh.RHError("二进制文件，无法在页面中显示", 415)
    return {"path": str(p), "text": raw.decode("utf-8", errors="replace")}


# ---------------------------------------------------------------- http

class Handler(BaseHTTPRequestHandler):
    server_version = "review-hub"
    ws: Path = Path()
    port: int = 0

    def log_message(self, fmt, *args):  # keep the agent's terminal quiet
        pass

    # -- helpers
    def send_json(self, obj, status=200):
        body = json.dumps(obj, ensure_ascii=False).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def send_error_json(self, msg, status=400, code=1):
        self.send_json({"error": msg, "code": code}, status)

    def host_ok(self) -> bool:
        host = (self.headers.get("Host") or "").split(":")[0]
        return host in ("127.0.0.1", "localhost")

    def origin_ok(self) -> bool:
        origin = self.headers.get("Origin")
        if origin is None:
            return True
        return urlparse(origin).hostname in ("127.0.0.1", "localhost")

    def session(self, sid: str) -> Path:
        if not sid or "/" in sid or sid.startswith("."):
            raise rh.RHError("审阅编号不合法", 400)
        sess = self.ws / "sessions" / sid
        if not (sess / "sources.json").is_file():
            raise rh.RHError(f"找不到这次审阅：{sid}", 404)
        return sess

    def read_body(self) -> dict:
        n = int(self.headers.get("Content-Length") or 0)
        if n > 1_000_000:
            raise rh.RHError("请求太大", 413)
        try:
            body = json.loads(self.rfile.read(n) or b"{}")
        except json.JSONDecodeError:
            raise rh.RHError("请求不是合法 JSON", 400)
        if not isinstance(body, dict):
            raise rh.RHError("请求必须是 JSON 对象", 400)
        return body

    def route(self):
        u = urlparse(self.path)
        parts = [unquote(x) for x in u.path.split("/") if x]
        return u, parts, parse_qs(u.query)

    # -- verbs
    def do_GET(self):
        if not self.host_ok():
            return self.send_error_json("Host 不被允许", 403)
        u, parts, q = self.route()
        try:
            if parts[:1] != ["api"]:
                return self.serve_static(u.path)
            if parts == ["api", "sessions"]:
                return self.send_json({"workspace": str(self.ws), "sessions": list_sessions(self.ws)})
            if len(parts) == 4 and parts[1] == "s":
                sess = self.session(parts[2])
                if parts[3] == "version":
                    return self.send_json({"version": session_version(sess)})
                if parts[3] == "state":
                    return self.send_json(session_state(sess))
                if parts[3] == "file":
                    return self.send_json(read_source_file(sess, (q.get("path") or [""])[0]))
            self.send_error_json("没有这个接口", 404)
        except rh.RHError as e:
            self.send_error_json(str(e), e.code if e.code >= 400 else 400, e.code)

    def do_POST(self):
        if not self.host_ok() or not self.origin_ok():
            return self.send_error_json("来源不被允许", 403)
        u, parts, q = self.route()
        try:
            if len(parts) != 4 or parts[:2] != ["api", "s"]:
                return self.send_error_json("没有这个接口", 404)
            sess = self.session(parts[2])
            body = self.read_body()
            with WRITE_LOCK:
                if parts[3] == "feedback":
                    return self.send_json(post_feedback(sess, body))
                if parts[3] == "time":
                    return self.send_json(post_time(sess, body))
                if parts[3] == "message":
                    return self.send_json(post_message(sess, body))
                if parts[3] == "activity":
                    return self.send_json(post_activity(sess, body))
            self.send_error_json("没有这个接口", 404)
        except rh.RHError as e:
            self.send_error_json(str(e), e.code if e.code >= 400 else 400, e.code)

    def serve_static(self, path: str):
        rel = "index.html" if path in ("", "/") else path.lstrip("/")
        p = (UI_DIR / rel).resolve()
        if UI_DIR.resolve() not in p.parents or not p.is_file():
            return self.send_error_json("没有这个文件", 404)
        body = p.read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", MIME.get(p.suffix, "application/octet-stream") + "; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


FEEDBACK_FIELDS = ("reaction", "fix_by", "value", "option", "text", "outcome", "quality", "note", "scope")


def post_feedback(sess: Path, body: dict) -> dict:
    kind = body.get("kind")
    if kind not in ("reaction", "confirm", "choice", "answer", "prediction", "signoff", "suggestion", "missed"):
        raise rh.RHError("页面不能记录这类意见", 400)
    kw = {k: body[k] for k in FEEDBACK_FIELDS if body.get(k) not in (None, "")}
    if "quality" in kw:
        kw["quality"] = int(kw["quality"])
    rec = rh.make_feedback(sess, kind, body.get("target"), source="ui", **kw)
    return {"record": rec}


def post_message(sess: Path, body: dict) -> dict:
    text = (body.get("text") or "").strip()
    if not text:
        raise rh.RHError("消息不能为空", 400)
    if len(text) > 4000:
        raise rh.RHError("消息太长", 400)
    rec = rh.add_message(sess, "human", text, target=body.get("target") or None, source="ui")
    return {"record": rec}


ACTIVITY_KINDS = ("open", "close", "ref", "zone")


def post_activity(sess: Path, body: dict) -> dict:
    """What the person opened and for how long: kept locally, read only by review-learn. Never shown or sent."""
    events = body.get("events")
    if not isinstance(events, list) or len(events) > 200:
        raise rh.RHError("events 必须是不超过 200 条的列表", 400)
    out = []
    for e in events:
        if not isinstance(e, dict) or e.get("kind") not in ACTIVITY_KINDS:
            raise rh.RHError("事件类型不对", 400)
        if not isinstance(e.get("id"), str) or not re.fullmatch(r"[A-Z]\d{1,5}", e["id"]):
            raise rh.RHError("事件的 id 不对", 400)
        t = e.get("t")
        if not isinstance(t, (int, float)):
            raise rh.RHError("事件缺少时间", 400)
        out.append({"t": int(t), "kind": e["kind"], "id": e["id"]})
    with (sess / "activity.jsonl").open("a", encoding="utf-8") as f:
        for e in out:
            f.write(json.dumps(e, separators=(",", ":")) + "\n")
    return {"saved": len(out)}


def post_time(sess: Path, body: dict) -> dict:
    try:
        minutes = float(body.get("minutes", 0))
    except (TypeError, ValueError):
        raise rh.RHError("minutes 必须是数字", 400)
    if not 0 < minutes <= MAX_MINUTES_PER_TICK:
        raise rh.RHError(f"每次最多记 {MAX_MINUTES_PER_TICK} 分钟", 400)
    stage = body.get("stage") or None
    if stage is not None and not isinstance(stage, str):
        raise rh.RHError("stage 必须是字符串", 400)
    if not (sess / "plan.json").exists():
        return {"skipped": "no plan"}
    m = rh.add_human_time(sess, minutes, stage)
    return {"minutes": m["human"]["minutes"]}


def make_server(ws: Path, port: int) -> ThreadingHTTPServer:
    Handler.ws = ws
    last = None
    for p in range(port, port + 20):
        try:
            srv = ThreadingHTTPServer(("127.0.0.1", p), Handler)
            srv.daemon_threads = True
            return srv
        except OSError as e:
            last = e
    raise SystemExit(f"错误：端口 {port}–{port + 19} 都被占用：{last}")


def main(argv=None) -> None:
    ap = argparse.ArgumentParser(prog="rh_ui", description="Review Engine 本地审核界面")
    ap.add_argument("--workspace", help="工作区路径（默认 $REVIEW_WORKSPACE 或 ~/review-workspace）")
    ap.add_argument("session", nargs="?", help="直接打开的会话（文件夹路径或 session_id）")
    ap.add_argument("--port", type=int, default=8765)
    ap.add_argument("--open", action="store_true", help="启动后用默认浏览器打开")
    a = ap.parse_args(argv)
    ws = rh.workspace_root(a.workspace)
    sid = None
    if a.session:
        sess = rh.resolve_session(a.session, a.workspace)
        if sess.parent != ws / "sessions":
            ws = sess.parent.parent  # a session path outside $REVIEW_WORKSPACE: serve its workspace
        sid = sess.name
    if not (ws / "sessions").is_dir():
        raise SystemExit(f"错误：工作区 {ws} 未初始化，先运行 review-setup")
    srv = make_server(ws, a.port)
    url = f"http://127.0.0.1:{srv.server_address[1]}/" + (f"#/s/{sid}" if sid else "")
    print(f"审阅页面：{url}", flush=True)
    print(f"工作区：{ws}（按 Ctrl+C 停止）", flush=True)
    if a.open and sys.platform == "darwin":
        subprocess.run(["open", url], check=False)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
