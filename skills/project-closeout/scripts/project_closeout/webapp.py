"""Loopback-only, token-protected local interface for the existing core."""
import copy
import json
import os
import secrets
import stat
import threading
import uuid
import webbrowser
from collections import Counter
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlsplit

from . import providers
from .catalog import project_catalog
from .preferences import Preferences
from .profiles import build_profiles, overview, closure
from .core import CloseoutError, apply_plan, atomic_json, make_plan, report, root_path, scan, sensitive, state_path, trash_run, undo


class App:
    def __init__(self, catalog_path=None, preferences=None):
        self.catalog_path = catalog_path
        self.token = secrets.token_urlsafe(32)
        self.projects = {}
        self.jobs = {}
        self.runs = {}
        self.prepared = {}
        self.preferences = preferences or Preferences()
        self.mutex = threading.Lock()
        self.refresh()

    def refresh(self):
        result = project_catalog(catalog_path=self.catalog_path)
        self.projects = {p["id"]: p for p in result["projects"]}
        return result

    def start_job(self, task):
        key = uuid.uuid4().hex
        self.jobs[key] = {"status": "running", "progress": "正在读取文件，请稍候…"}
        def run():
            try:
                result = task(key)
                self.jobs[key].update(status="done", **result)
            except CloseoutError as error:
                self.jobs[key].update(status="error", error=str(error))
            except Exception:
                self.jobs[key].update(status="error", error="操作未完成，请检查项目文件权限与配置。")
        threading.Thread(target=run, daemon=True).start()
        return {"job": key}

    def get_plan(self, job):
        item = self.jobs.get(job, {})
        if item.get("status") != "done" or "plan" not in item:
            raise CloseoutError("请先完成一次项目盘点")
        return item["plan"]

    def save(self, plan, label):
        path = state_path(Path(plan["root"]), "plans/latest-" + label + ".json")
        atomic_json(path, plan)
        md = path.with_suffix(".md")
        if md.is_symlink():
            raise CloseoutError("报告路径不能是符号链接")
        md.write_text(report(plan), encoding="utf-8")
        return str(md)

    def public_job(self, key):
        item = self.jobs.get(key)
        if not item:
            raise CloseoutError("找不到这次盘点，请重新选择项目")
        result = {k: v for k, v in item.items() if k not in {"plan", "plans", "manifest","overrides"}}
        if "plan" in item and "plans" not in item:
            plan = item["plan"]
            result["summary"] = dict(Counter(r["decision"] for r in plan["records"]))
            result["warnings"] = plan["warnings"]
            result["records"] = [{k: r.get(k) for k in ("path", "size", "decision", "reason", "model_advice","role","kind")}
                                 for r in plan["records"]]
        if "plans" in item:
            result["profiles"] = item["profiles"]
        return result

    def review(self, job, tier):
        item = self.jobs.get(job, {})
        if item.get("status") != "done" or tier not in item.get("plans", {}):
            raise CloseoutError("这份方案尚未准备好")
        plan = item["plans"][tier]
        return {"records": [{k: r.get(k) for k in ("path", "size", "decision", "reason", "model_advice","role","kind")}
                            for r in plan["records"]], "warnings": plan["warnings"]}

    def details(self, key):
        project = self.projects.get(key)
        if not project:
            raise CloseoutError("找不到这个项目")
        root = root_path(project["path"])
        output = root / "outputs"
        base = output if output.is_dir() and not output.is_symlink() else root
        finals = [{"label": "保留整个成果目录", "path": "outputs"}] if base == output else []
        for directory, dirs, files in os.walk(base, followlinks=False):
            depth = len(Path(directory).relative_to(base).parts)
            dirs[:] = sorted(d for d in dirs if depth < 2 and not d.startswith(".")
                             and not sensitive(d) and not (Path(directory)/d).is_symlink()
                             and d not in {"node_modules", "work", "venv", "__pycache__"})
            for name in sorted(files):
                path = Path(directory) / name
                if not name.startswith(".") and not sensitive(name) and not path.is_symlink() and path.is_file():
                    rel = path.relative_to(root).as_posix()
                    finals.append({"label": rel, "path": rel})
                if len(finals) >= 100:
                    break
            if len(finals) >= 100:
                break
        return {"finals": finals}

    def preview(self, key):
        """A bounded local image cover; callers choose a project, never a path."""
        project = self.projects.get(key)
        if not project:
            raise CloseoutError("找不到这个项目")
        root = root_path(project["path"])
        formats = {".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".webp": "image/webp"}
        for directory in (root / "outputs", root):
            if directory.is_symlink() or not directory.is_dir():
                continue
            try:
                candidates = []
                with os.scandir(directory) as entries:
                    for index, entry in enumerate(entries):
                        if index >= 128:
                            break
                        if (not entry.name.startswith(".") and not sensitive(entry.name)
                                and Path(entry.name).suffix.lower() in formats
                                and not entry.is_symlink() and entry.is_file(follow_symlinks=False)):
                            candidates.append(Path(entry.path))
                for path in sorted(candidates):
                    # O_NOFOLLOW also rejects a file swapped for a symlink after discovery.
                    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
                    with os.fdopen(fd, "rb") as source:
                        info = os.fstat(source.fileno())
                        if not stat.S_ISREG(info.st_mode) or not 0 < info.st_size <= 8 * 1024 * 1024:
                            continue
                        data = source.read(8 * 1024 * 1024 + 1)
                    if len(data) > 8 * 1024 * 1024:
                        continue
                    suffix = path.suffix.lower()
                    valid = ((suffix == ".png" and data.startswith(b"\x89PNG\r\n\x1a\n"))
                             or (suffix in {".jpg", ".jpeg"} and data.startswith(b"\xff\xd8\xff"))
                             or (suffix == ".webp" and data[:4] == b"RIFF" and data[8:12] == b"WEBP"))
                    if valid:
                        return data, formats[suffix]
            except OSError:
                continue
        return b"", "image/png"

    def action(self, route, data):
        if route == "/api/settings":
            return self.preferences.save(data)
        if route == "/api/refresh":
            return self.refresh()
        if route == "/api/start":
            project = self.projects.get(data.get("project"))
            if not project:
                raise CloseoutError("请从列表中选择项目")
            # Capture the saved choice before starting; a later settings change
            # cannot change the provider for an analysis already in progress.
            connection, connection_error = None, None
            try:
                connection = self.preferences.connection()
            except CloseoutError as error:
                connection_error = str(error)
            def task(job):
                self.jobs[job]["progress"] = "正在识别成果、源文件和过程文件…"
                # Bound each log independently so early, large logs cannot
                # consume the text budget before later project files are read.
                plan = make_plan(project["path"], "balanced", text_limit=64 * 1024)
                mode, message = "local", "本地规则分析；尚未启用 AI 内容判断"
                manifest = None
                files = [r for r in plan["records"] if r["kind"] == "file"]
                coverage = {"status": "local", "file_count": len(files),
                            "text_count": sum(r.get("extraction") in {"text", "markitdown"} for r in files),
                            "unread_count": sum(r.get("extraction") not in {"text", "markitdown"} for r in files),
                            "truncated_count": sum(bool(r.get("truncated")) for r in files),
                            "budget_skipped_count": sum(r.get("extraction") == "budget-skipped" for r in files),
                            "ai_file_count": 0, "batch_count": 0, "completed_batches": 0}
                self.jobs[job]["analysis"] = coverage
                if connection:
                    settings, api_key = connection
                    self.jobs[job]["progress"] = "正在分析文件价值与保留建议…"
                    try:
                        manifest = providers.prepare(plan, settings, reviewable_only=True, adaptive=True)
                        coverage.update(status="running", batch_count=manifest["batch_count"])
                        if manifest["file_count"]:
                            atomic_json(state_path(Path(plan["root"]), "payloads/latest-auto.json"), manifest)
                            def progress(i, n, source):
                                coverage["completed_batches"] = i
                                self.jobs[job]["progress"] = "正在分析文本片段 · " + str(i) + "/" + str(n) + " 批"
                            providers.analyze(plan, settings, manifest, api_key, progress=progress)
                            coverage.update(status="complete", ai_file_count=manifest["file_count"])
                            mode, message = "ai", settings["provider"] + " · " + settings["model"] + " 文本片段分析完成；未读文件可在清单中手动归类。"
                        else:
                            coverage["status"] = "not-needed"
                            message = "没有可供 AI 判断的完整可提取文本。本次未调用 AI；请核对本地归类。"
                    except CloseoutError as error:
                        coverage["status"] = "failed"
                        message = "AI 分析未完成；以下仅为本地盘点，不能据此认定其他文件都有价值。" + str(error)
                elif connection_error:
                    coverage["status"] = "failed"
                    message = connection_error
                plan["analysis"] = dict(coverage)
                if coverage["status"] == "failed":
                    plan["warnings"].append(message)
                self.jobs[job]["progress"] = "正在生成三种保留方案…"
                plans, summaries = build_profiles(plan)
                if coverage["status"] == "failed":
                    for summary in summaries:
                        summary["recommended"] = False
                return {"plan": plan, "plans": plans, "profiles": summaries,
                        "overview": overview(plans["milestones"], summaries), "scope":"", "scopes":sorted({str(parent) for r in files for parent in Path(r["path"]).parents if str(parent)!="."}), "mode": mode, "mode_message": message,
                        "analysis": coverage,
                        "has_transmission": bool(manifest and manifest["file_count"]),
                        "manifest": manifest, "report_path": self.save(plans["milestones"], "analysis")}
            return self.start_job(task)
        if route == "/api/task":
            job=data.get("job");base=self.get_plan(job);item=self.jobs[job]
            scope=data.get("scope","")
            if scope and scope not in item.get("scopes",[]):raise CloseoutError("未知任务目录")
            overrides=dict(item.get("overrides",{}))
            path=data.get("path")
            if path is not None:
                role=data.get("role")
                if role not in {"result","milestone","process","auto"} or not any(
                        r["path"]==path and r["kind"]=="file" for r in base["records"]):
                    raise CloseoutError("无效的文件归类")
                if role=="auto":overrides.pop(path,None)
                else:overrides[path]=role
            plans,profiles=build_profiles(base,scope,overrides)
            item.update(scope=scope,overrides=overrides,plans=plans,profiles=profiles,
                        overview=overview(plans["milestones"],profiles),version=item.get("version",0)+1)
            return self.public_job(job)
        if route == "/api/scan":
            project = self.projects.get(data.get("project"))
            if not project:
                raise CloseoutError("请从列表中选择项目")
            tier = data.get("tier", "balanced")
            if tier not in {"full", "balanced", "minimal"}:
                raise CloseoutError("请选择整理程度")
            finals = data.get("finals", [])
            if not isinstance(finals, list) or not all(isinstance(v, str) for v in finals):
                raise CloseoutError("最终成果格式无效")
            note = data.get("note", "")
            if not isinstance(note, str) or len(note) > 10000:
                raise CloseoutError("项目说明过长")
            def task(key):
                plan = make_plan(project["path"], tier, finals, note)
                return {"plan": plan, "report_path": self.save(plan, "local")}
            return self.start_job(task)
        if route == "/api/payload":
            plan = self.get_plan(data.get("job"))
            settings = providers.config(None, data.get("provider"), data.get("model"))
            manifest = providers.prepare(plan, settings)
            digest = manifest["payload_sha256"]
            self.prepared[digest] = (data["job"], settings, manifest)
            return {"digest": digest, "batches": manifest["batch_count"], "endpoint": settings["base_url"],
                    "preview": manifest}
        if route == "/api/analyze":
            digest = data.get("digest")
            if digest not in self.prepared or self.prepared[digest][0] != data.get("job"):
                raise CloseoutError("请先预览将发送的内容")
            _, settings, manifest = self.prepared[digest]
            key = data.get("key", "")
            if not isinstance(key, str) or not key.strip() or "\n" in key or "\r" in key:
                raise CloseoutError("请在本地填写 API Key")
            plan = copy.deepcopy(self.get_plan(data["job"]))
            def task(job):
                if scan(Path(plan["root"]))["fingerprint"] != plan["fingerprint"]:
                    raise CloseoutError("项目文件已变化，请重新盘点")
                providers.analyze(plan, settings, manifest, key,
                                  progress=lambda i, n, mode: self.jobs[job].update(progress="已分析 " + str(i) + "/" + str(n) + " 批"))
                return {"plan": plan, "report_path": self.save(plan, "ai")}
            return self.start_job(task)
        if route == "/api/apply":
            plan = self.get_plan(data.get("job"))
            if "tier" in data:
                plans = self.jobs[data["job"]].get("plans", {})
                tier = data.get("tier")
                if tier not in plans:
                    raise CloseoutError("请先选择分析完成后的保留方案")
                plan = plans[tier]
                item=self.jobs[data["job"]]
                profile=next(p for p in item["profiles"] if p["tier"]==tier)
                if data.get("version",0)!=item.get("version",0):raise CloseoutError("文件清单已更新，请重看后执行")
                if profile["blocked"]:raise CloseoutError("请在文件清单中指定需要留下的结果或重要节点")
                if tier=="clear" and data.get("confirm_clear") is not True:raise CloseoutError("请确认全部清理当前任务")
                exclusions = data.get("keep_extra", [])
                candidates = {r["path"] for r in plan["records"] if r["decision"] == "candidate"}
                if not isinstance(exclusions, list) or not all(isinstance(v, str) and v in candidates for v in exclusions):
                    raise CloseoutError("额外保留的文件不在这份方案内")
                selected = sorted(candidates - set(exclusions))
                by={r["path"]:r for r in plan["records"]}
                if set(selected) & closure(exclusions,by):
                    raise CloseoutError("额外保留的文件引用了待移出文件，请把它标为结果后重新预览")
                if not selected:
                    return {"run": None, "count": 0}
                receipt = copy.deepcopy(plan)
                for record in receipt["records"]:
                    if record["path"] in exclusions:
                        record.update(decision="keep", reason="你要求额外保留")
                self.save(receipt, "selected")
            else:
                selected = data.get("selected")
            if not isinstance(selected, list) or not all(isinstance(v, str) for v in selected):
                raise CloseoutError("请明确选择文件")
            result = apply_plan(plan, Path(plan["root"]), selected, reviewed_task="tier" in data)
            self.runs[result["id"]] = plan["root"]
            try:
                trash_run(Path(plan["root"]), result["id"])
            except (CloseoutError,OSError):
                # Keep the receipt and undo button available when macOS denies the move.
                return {"run":result["id"],"count":len(result["entries"]),"destination":"unconfirmed"}
            return {"run": result["id"], "count": len(result["entries"]), "destination":"trash"}
        if route == "/api/undo":
            run = data.get("run")
            if run not in self.runs:
                raise CloseoutError("找不到本次隔离记录")
            undo(Path(self.runs[run]), run)
            return {"restored": True}
        raise CloseoutError("未知操作")


def handler_for(app):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass  # Never log request bodies, keys, or personal paths.

        def reply(self, code, content, mime="application/json; charset=utf-8"):
            body = content.encode("utf-8") if isinstance(content, str) else content
            self.send_response(code)
            self.send_header("Content-Type", mime)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Content-Security-Policy", "default-src 'self'; img-src 'self' blob:; style-src 'self'; script-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'none'")
            self.end_headers()
            self.wfile.write(body)

        def valid_host(self):
            return self.headers.get("Host") == "127.0.0.1:" + str(self.server.server_port)

        def authorized(self):
            origin = self.headers.get("Origin")
            expected = "http://127.0.0.1:" + str(self.server.server_port)
            return (self.valid_host() and (not origin or origin == expected)
                    and secrets.compare_digest(self.headers.get("X-Closeout-Token", ""), app.token))

        def json_reply(self, value, code=200):
            self.reply(code, json.dumps(value, ensure_ascii=False))

        def do_GET(self):
            route = urlsplit(self.path).path
            assets = {"/": ("index.html", "text/html; charset=utf-8"),
                      "/fold-logo.svg": ("fold-logo.svg", "image/svg+xml"),
                      "/fold-icon.svg": ("fold-icon.svg", "image/svg+xml"),
                      "/ui.js": ("ui.js", "text/javascript; charset=utf-8"),
                      "/ui.css": ("ui.css", "text/css; charset=utf-8")}
            for name in ("source-tokens.css", "source-material.css", "source-theme.css", "reference-theme.css", "reference.html", "glass-ui-LICENSE.txt"):
                assets["/" + name] = (name, "text/html; charset=utf-8" if name.endswith(".html") else "text/plain; charset=utf-8" if name.endswith(".txt") else "text/css; charset=utf-8")
            if not self.valid_host():
                self.json_reply({"error": "无效的本地访问地址"}, 403)
                return
            if route in assets:
                name, mime = assets[route]
                self.reply(200, (Path(__file__).parent / "ui" / name).read_bytes(), mime)
                return
            if not self.authorized():
                self.json_reply({"error": "请使用启动入口重新打开页面"}, 403)
                return
            try:
                if route == "/api/projects":
                    self.json_reply({"projects": list(app.projects.values())})
                elif route.startswith("/api/preview/"):
                    data, mime = app.preview(route.rsplit("/", 1)[-1])
                    self.reply(200 if data else 204, data, mime)
                elif route == "/api/settings":
                    self.json_reply(app.preferences.public())
                elif route.startswith("/api/transmission/"):
                    item = app.jobs.get(route.rsplit("/", 1)[-1], {})
                    self.json_reply({"manifest": item.get("manifest")})
                elif route.startswith("/api/records/"):
                    parts = route.split("/")
                    if len(parts) != 5:
                        raise CloseoutError("无效的方案请求")
                    self.json_reply(app.review(parts[3], parts[4]))
                elif route.startswith("/api/details/"):
                    self.json_reply(app.details(route.rsplit("/", 1)[-1]))
                elif route.startswith("/api/jobs/"):
                    self.json_reply(app.public_job(route.rsplit("/", 1)[-1]))
                else:
                    self.json_reply({"error": "页面不存在"}, 404)
            except CloseoutError as error:
                self.json_reply({"error": str(error)}, 400)

        def do_POST(self):
            if not self.authorized():
                self.json_reply({"error": "本地访问校验失败"}, 403)
                return
            try:
                length = int(self.headers.get("Content-Length", "0"))
                if not 0 < length <= 256 * 1024:
                    raise CloseoutError("请求内容过大或为空")
                data = json.loads(self.rfile.read(length))
                if not isinstance(data, dict):
                    raise CloseoutError("请求格式无效")
                # Serialize state-changing routes; scans/API jobs run in background.
                with app.mutex:
                    result = app.action(urlsplit(self.path).path, data)
                self.json_reply(result)
            except CloseoutError as error:
                self.json_reply({"error": str(error)}, 400)
            except (ValueError, TypeError, OSError, KeyError):
                self.json_reply({"error": "操作未完成，请检查文件权限与输入。"}, 400)
    return Handler


def serve(port=0, catalog_path=None, open_browser=True):
    app = App(catalog_path)
    server = ThreadingHTTPServer(("127.0.0.1", port), handler_for(app))
    url = "http://127.0.0.1:" + str(server.server_port) + "/#" + app.token
    print("项目收尾已启动。请在打开的页面中点击项目名称。", flush=True)
    print("本地页面：" + url, flush=True)
    print("保持此窗口打开；关闭窗口或按 Ctrl+C 可退出。", flush=True)
    if open_browser:
        webbrowser.open(url)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
