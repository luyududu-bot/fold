import contextlib
import ast
import hashlib
import json
import os
import posixpath
import re
import stat
import subprocess
import tempfile
import uuid
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath, PureWindowsPath

STATE = ".project-closeout"
WINDOWS_RECYCLE = os.name == "nt"
SKIP_DIRS = {".git", ".hg", ".svn", "node_modules", ".venv", "venv", STATE}
TEXT_EXT = {".md", ".txt", ".py", ".js", ".ts", ".tsx", ".jsx", ".css", ".html",
            ".json", ".csv", ".tsv", ".yaml", ".yml", ".toml", ".xml", ".svg",
            ".sh", ".r", ".tex", ".bib", ".ipynb", ".log", ".rst", ".sql", ".swift"}
SOURCE_EXT = {".py", ".js", ".ts", ".tsx", ".jsx", ".css", ".html", ".sh", ".r",
              ".tex", ".bib", ".ipynb", ".sql", ".drawio", ".fig", ".psd", ".ai",
              ".blend", ".sketch", ".docx", ".pptx", ".xlsx", ".svg", ".swift",
              ".pcraft", ".vectorcraft"}
OFFICE_EXT = {".pdf", ".docx", ".pptx", ".xlsx", ".xls"}
SECRET_RE = re.compile(r"(?i)(?:sk-[a-z0-9_-]{12,}|AIza[a-z0-9_-]{20,}|"
                       r"(?:api[_-]?key|secret|password|authorization|access[_-]?token)"
                       r"\s*[\"']?\s*[:=]\s*[\"']?[^\s\"',;]+)")


class CloseoutError(Exception):
    pass


def now():
    return datetime.now(timezone.utc).isoformat()


def redact(text):
    return SECRET_RE.sub("[REDACTED]", text)


def sensitive(path):
    p = PurePosixPath(path)
    n = p.name.lower()
    return (n.startswith(".env") or n in {".npmrc", ".pypirc", "id_rsa", "id_ed25519"}
            or p.suffix.lower() in {".key", ".pem", ".p12", ".pfx", ".keystore"}
            or any(x in n for x in ("credential", "secret", "password"))
            or any(x.lower() in {".ssh", ".aws", ".gnupg"} for x in p.parts))


def safe_path(root, relative):
    """Lexically confined path; refuse symlinks in every existing component."""
    if not isinstance(relative, str) or "\\" in relative:
        raise CloseoutError("路径必须是项目内的相对路径")
    p = PurePosixPath(relative)
    if p.is_absolute() or not p.parts or any(x in {"..", "."} for x in p.parts):
        raise CloseoutError("拒绝越界路径：" + relative)
    current = Path(root)
    for part in p.parts:
        if os.name == "nt" and (":" in part or part.endswith((" ", ".")) or PureWindowsPath(part).is_reserved()):
            raise CloseoutError("无效的 Windows 文件名：" + relative)
        current = current / part
        if current.is_symlink():
            raise CloseoutError("拒绝符号链接路径：" + relative)
        if os.name == "nt":
            try:
                attributes = current.lstat().st_file_attributes
            except FileNotFoundError:
                continue
            if attributes & stat.FILE_ATTRIBUTE_REPARSE_POINT:
                raise CloseoutError("拒绝重解析点路径：" + relative)
    return current


def root_path(path):
    p = Path(path).expanduser().absolute()
    if not p.is_dir() or p.is_symlink():
        raise CloseoutError("项目根目录必须是普通目录")
    # Resolve parent aliases once; nested aliases are never followed.
    return p.resolve()


def sha_file(path):
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    h = hashlib.sha256()
    fd = os.open(str(path), flags)
    with os.fdopen(fd, "rb") as stream:
        if not stat.S_ISREG(os.fstat(stream.fileno()).st_mode):
            raise CloseoutError("仅处理普通文件")
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            h.update(block)
    return h.hexdigest()


def atomic_json(path, data):
    path = Path(path)
    if path.is_symlink():
        raise CloseoutError("拒绝写入符号链接")
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=".closeout-")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump(data, stream, ensure_ascii=False, indent=2, allow_nan=False)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)


def read_json(path):
    with Path(path).open(encoding="utf-8") as stream:
        return json.load(stream)


def extract(path, use_markitdown=False, max_bytes=512 * 1024):
    suffix = path.suffix.lower()
    if suffix in TEXT_EXT or path.name.lower() in {"readme", "makefile", "dockerfile"}:
        with path.open("rb") as stream:
            data = stream.read(max_bytes + 1)
        if b"\x00" in data[:8192]:
            return "", "binary", False
        try:
            import codecs
            truncated = len(data) > max_bytes
            decoder = codecs.getincrementaldecoder("utf-8")()
            return decoder.decode(data[:max_bytes], final=not truncated), "text", truncated
        except UnicodeDecodeError:
            return "", "encoding-unreadable", False
    if use_markitdown and suffix in OFFICE_EXT:
        # Conversion runs in a child with no provider credential environment.
        import subprocess
        import sys
        env = {k: v for k, v in os.environ.items()
               if not any(x in k.upper() for x in ("KEY", "TOKEN", "SECRET", "PASSWORD"))}
        code = ("from markitdown import MarkItDown; import sys; "
                "t=MarkItDown(enable_plugins=False).convert(sys.argv[1]).text_content; "
                "sys.stdout.write(t[:524289])")
        try:
            result = subprocess.run([sys.executable, "-c", code, str(path)],
                                    capture_output=True, timeout=30, env=env)
            if result.returncode == 0:
                text = result.stdout.decode("utf-8", errors="replace")
                return text[:512 * 1024], "markitdown", len(text) > 512 * 1024
            return "", "conversion-unavailable", False
        except subprocess.TimeoutExpired:
            return "", "conversion-timeout", False
    return "", "not-extracted", False


def fingerprint(records):
    snapshot = [{k: r.get(k) for k in ("path", "kind", "sha256", "size", "mtime_ns")}
                for r in records]
    return hashlib.sha256(json.dumps(snapshot, sort_keys=True,
                                    ensure_ascii=False).encode()).hexdigest()


def separate_python_outputs(text):
    """Recognize literal write targets only; unknown dataflow remains conservative."""
    try:
        tree = ast.parse(text)
    except (SyntaxError, ValueError, RecursionError):
        return text, []
    targets, outputs = [], []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        target = None
        if isinstance(node.func, ast.Name) and node.func.id == "open" and node.args:
            mode = node.args[1] if len(node.args) > 1 else next(
                (kw.value for kw in node.keywords if kw.arg == "mode"), None)
            # w/x are pure output; append or '+' may read/retain previous data.
            if isinstance(mode, ast.Constant) and mode.value in {"w", "wb", "wt", "x", "xb", "xt"}:
                target = node.args[0]
        elif isinstance(node.func, ast.Attribute):
            if node.func.attr == "savefig" and node.args:
                target = node.args[0]
            elif node.func.attr in {"write_text", "write_bytes"}:
                value = node.func.value
                if isinstance(value, ast.Call) and isinstance(value.func, ast.Name) and value.func.id == "Path" and value.args:
                    target = value.args[0]
        if isinstance(target, ast.Constant) and isinstance(target.value, str):
            targets.append(target)
            outputs.append(target.value)
    # AST columns are byte offsets; patch bytes rather than Unicode characters.
    lines = text.encode("utf-8").splitlines(keepends=True)
    starts = [0]
    for line in lines:
        starts.append(starts[-1] + len(line))
    data = bytearray(text.encode("utf-8"))
    for target in targets:
        start = starts[target.lineno - 1] + target.col_offset
        end = starts[target.end_lineno - 1] + target.end_col_offset
        data[start:end] = b" " * (end - start)
    return data.decode("utf-8"), outputs


def scan(root, use_markitdown=False, max_files=20000, text_limit=512 * 1024):
    records, texts, warnings = [], {}, []
    extracted_bytes = 0
    def walk_error(error):
        raise CloseoutError("无法完整扫描；已停止：" + str(error.filename))
    for directory, dirs, files in os.walk(root, followlinks=False, onerror=walk_error):
        dirs.sort()
        files.sort()
        retained = []
        for name in dirs:
            p = Path(directory) / name
            rel = p.relative_to(root).as_posix()
            if name == STATE:
                if p.is_symlink():
                    raise CloseoutError("状态目录不能是符号链接")
                continue
            if p.is_symlink() or name in SKIP_DIRS or sensitive(rel):
                records.append({"path": rel, "kind": "protected-directory", "size": 0,
                                "mtime_ns": p.lstat().st_mtime_ns, "protected": True,
                                "reason": "符号链接、私密目录或依赖/版本管理目录，不展开"})
            else:
                retained.append(name)
        dirs[:] = retained
        for name in files:
            p = Path(directory) / name
            rel = p.relative_to(root).as_posix()
            if len(records) >= max_files:
                raise CloseoutError("文件数量超过扫描上限；没有生成不完整方案")
            s = p.lstat()
            r = {"path": rel, "size": s.st_size, "mtime_ns": s.st_mtime_ns}
            if not stat.S_ISREG(s.st_mode) or p.is_symlink() or sensitive(rel):
                r.update(kind="protected-file", protected=True,
                         reason="私密文件、符号链接或非普通文件，不读取")
            else:
                digest = sha_file(p)
                if extracted_bytes >= 32 * 1024 * 1024:
                    text, mode, truncated = "", "budget-skipped", False
                else:
                    text, mode, truncated = extract(p, use_markitdown, min(text_limit, 32 * 1024 * 1024 - extracted_bytes))
                    extracted_bytes += len(text.encode("utf-8"))
                after = p.stat()
                if (after.st_size, after.st_mtime_ns) != (s.st_size, s.st_mtime_ns):
                    raise CloseoutError("扫描时文件发生变化，请重试：" + rel)
                r.update(kind="file", sha256=digest, extraction=mode, truncated=truncated,
                         excerpt=redact(text[:2400]), protected=False)
                if text:
                    texts[rel] = text
            records.append(r)
    records.sort(key=lambda x: x["path"])
    # Text reference discovery is evidence, not a complete dependency graph.
    paths = {r["path"] for r in records if r["kind"] == "file"}
    names = defaultdict(list)
    for path in paths:
        names[PurePosixPath(path).name].append(path)
    def resolve_token(origin, token):
        found = set()
        token = token.rstrip(".:!?，。；：！？“”‘’").replace("\\", "/")
        if token in paths:
            found.add(token)
        normalized = posixpath.normpath(str(PurePosixPath(origin).parent / token))
        if normalized in paths:
            found.add(normalized)
        if token in names:
            found.update(names[token])
        return found
    generated = defaultdict(list)
    for r in records:
        refs = set()
        text = texts.get(r["path"], "")
        if PurePosixPath(r["path"]).suffix.lower() == ".py":
            text, outputs = separate_python_outputs(text)
            for output in outputs:
                for target in resolve_token(r["path"], output):
                    generated[target].append(r["path"])
        tokens = re.findall(r"[^\s\"'`()<>\[\]{},;=，。；：！？“”‘’]+", text)
        # Markdown link targets may contain spaces.
        tokens += re.findall(r"\]\(([^)\n]+)\)", text)
        for token in tokens:
            refs.update(resolve_token(r["path"], token))
        refs.discard(r["path"])
        r["references"] = sorted(refs)
    for r in records:
        r["generated_by"] = sorted(set(generated[r["path"]]))
    if any(r.get("truncated") for r in records):
        warnings.append("部分文本只读取前 " + str(text_limit // 1024) + " KiB，引用检查不完整。")
    if any(r.get("extraction") == "budget-skipped" for r in records):
        warnings.append("已到 32 MiB 文本提取预算，剩余文件只盘点；请按子项目分别分析。")
    warnings.append("引用检查只覆盖可提取文本中的文件路径；仅区分少数 Python 字面量写入目标，未验证代码 import、动态加载及二进制内部依赖。")
    return {"records": records, "fingerprint": fingerprint(records), "warnings": warnings}


def source_or_record(path):
    p = PurePosixPath(path)
    n = p.name.lower()
    parts = {part.lower() for part in p.parts[:-1]}
    return (p.suffix.lower() in SOURCE_EXT or n.startswith(("readme", "license", "requirements"))
            or n in {"agents.md", "makefile", "dockerfile", "package.json", "package-lock.json",
                     "pyproject.toml", "uv.lock", "poetry.lock", "cargo.lock", "cargo.toml"}
            or bool(parts & {"src", "source", "sources", "assets", "data", "raw", "experiments",
                             "results", "records", "decisions", "tests"}))


def temp_evidence(path):
    p = PurePosixPath(path)
    parts = {x.lower() for x in p.parts}
    n = p.name.lower()
    if "__pycache__" in parts or n == ".ds_store" or p.suffix.lower() == ".pyc":
        return "解释器缓存或系统目录元数据"
    if parts & {"work", "tmp", "temp", "scratch"}:
        if parts & {"preview", "previews", "rendered", "renders", "thumbnails"}:
            return "临时工作区中的渲染/预览产物；仍需人工确认可再生成"
        if n.startswith(("preview", "thumbnail", "render-page", "page-")) and p.suffix.lower() in {".png", ".jpg", ".webp"}:
            return "临时工作区中的页面预览命名；仍需人工确认可再生成"
    return None


def make_plan(root, tier="balanced", finals=(), note="", use_markitdown=False, text_limit=512 * 1024):
    root = root_path(root)
    inventory = scan(root, use_markitdown, text_limit=text_limit)
    records = inventory["records"]
    by_path = {r["path"]: r for r in records}
    final_paths = set()
    for value in finals:
        p = safe_path(root, value)
        rel = p.relative_to(root).as_posix()
        matched = {x for x in by_path if x == rel or x.startswith(rel + "/")}
        if not matched:
            raise CloseoutError("找不到最终成果：" + value)
        final_paths.update(matched)
    protected = set(final_paths)
    queue = list(final_paths)
    while queue:
        for ref in by_path[queue.pop()].get("references", []):
            if ref not in protected:
                protected.add(ref)
                queue.append(ref)
    incoming = defaultdict(list)
    duplicates = defaultdict(list)
    for r in records:
        for ref in r.get("references", []):
            incoming[ref].append(r["path"])
        if r.get("sha256"):
            duplicates[r["sha256"]].append(r["path"])
    for r in records:
        path = r["path"]
        r["referenced_by"] = incoming[path]
        r["duplicates"] = [x for x in duplicates.get(r.get("sha256"), []) if x != path]
        r["origin"] = "local"
        if r.get("protected"):
            r["decision"] = "protected"
        elif path in final_paths:
            r.update(decision="keep", reason="你指定的最终成果", protected=True)
        elif path in protected:
            r.update(decision="keep", reason="最终成果引用的必要文件", protected=True)
        elif incoming[path]:
            r.update(decision="keep", reason="被其他项目文件引用，保留原路径", protected=True)
        elif source_or_record(path):
            r.update(decision="keep", reason="源文件、数据、配置或重要记录，默认保留", protected=True)
        elif temp_evidence(path):
            r.update(decision="candidate", reason=temp_evidence(path))
        elif r["duplicates"]:
            r.update(decision="review", reason="内容重复，但位置可能有用途；需确认保留哪一份")
        elif tier == "full":
            r.update(decision="keep", reason="完整档保留未确认的过程文件")
        else:
            r.update(decision="review", reason="缺少充分的用途/可再生成证据，待确认")
    warnings = inventory["warnings"]
    if not finals:
        warnings.append("尚未指定最终成果，不能确认哪个版本被采用；请用 --final 指定文件或目录。")
    if tier == "minimal":
        warnings.append("最小档仅提高精简建议倾向，源文件/数据仍保留；不把未知文件直接当成垃圾。")
    return {"schema": 1, "id": uuid.uuid4().hex, "created_at": now(), "root": str(root),
            "tier": tier, "finals": sorted(final_paths), "note": redact(note),
            "fingerprint": inventory["fingerprint"], "records": records, "warnings": warnings,
            "model": None}


def report(plan):
    labels = {"keep": "保留", "protected": "保护", "review": "待确认", "candidate": "清理候选"}
    counts = Counter(r["decision"] for r in plan["records"])
    def cell(value):
        return str(value).replace("|", "\\|").replace("\n", " ").replace("\r", " ")
    lines = ["# 项目收尾预览", "", "项目：`" + plan["root"] + "`", "",
             "档位：" + plan["tier"] + "；" + " / ".join(labels[k] + " " + str(counts[k]) for k in labels), "",
             "候选文件仅为建议，生成报告不会移动文件。", ""]
    lines.extend("- " + x for x in plan["warnings"])
    lines += ["", "| 文件 | 大小 | 建议 | 依据 |", "|---|---:|---|---|"]
    for r in plan["records"]:
        reason = r.get("reason", "")
        if r.get("model_advice"):
            reason += "；模型：" + r["model_advice"]["reason"]
        lines.append("| " + cell(r["path"]) + " | " + str(r["size"]) + " | " + labels[r["decision"]]
                     + " | " + cell(reason) + " |")
    return "\n".join(lines) + "\n"


def state_path(root, suffix):
    return safe_path(root, STATE + "/" + suffix)


@contextlib.contextmanager
def locked(root):
    lock = state_path(root, "operation.lock")
    lock.parent.mkdir(exist_ok=True)
    try:
        fd = os.open(str(lock), os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    except FileExistsError:
        raise CloseoutError("已有归档操作锁；确认没有其他操作后再处理 operation.lock")
    os.close(fd)
    try:
        yield
    finally:
        lock.unlink()


def validate_plan(plan, root):
    if plan.get("schema") != 1 or plan.get("root") != str(root):
        raise CloseoutError("方案版本或项目根目录不匹配")
    if not isinstance(plan.get("records"), list):
        raise CloseoutError("方案文件格式无效")
    seen = set()
    for r in plan["records"]:
        p = r.get("path")
        # A protected link is listed but never selected or traversed.
        if r.get("kind") in {"protected-file","protected-directory"}:
            if not isinstance(p,str) or not p or PurePosixPath(p).is_absolute() or ".." in PurePosixPath(p).parts:
                raise CloseoutError("无效的受保护路径")
        else:
            safe_path(root, p)
        if p in seen or PurePosixPath(p).parts[0] == STATE:
            raise CloseoutError("方案包含重复或状态目录路径")
        seen.add(p)


def move_no_replace(src, dst):
    """Atomic no-clobber destination creation; journal recovers link/unlink gaps."""
    os.link(src, dst, follow_symlinks=False)
    os.unlink(src)


def apply_plan(plan, root, selected, *, reviewed_task=False):
    root = root_path(root)
    validate_plan(plan, root)
    by_path = {r["path"]: r for r in plan["records"]}
    if not selected or len(set(selected)) != len(selected):
        raise CloseoutError("请明确选择不重复的候选文件")
    for path in selected:
        r = by_path.get(path)
        if (not r or r.get("decision") != "candidate" or sensitive(path)
                or (not reviewed_task and (r.get("protected") or source_or_record(path)
                    or r.get("referenced_by") or path in plan.get("finals", [])))
                or r.get("kind") != "file"):
            raise CloseoutError("只能归档未受保护的清理候选：" + path)
    with locked(root):
        if scan(root)["fingerprint"] != plan["fingerprint"]:
            raise CloseoutError("项目文件已变化，请重新生成方案；没有移动文件")
        run_id = uuid.uuid4().hex
        journal_path = state_path(root, "runs/" + run_id + "/journal.json")
        entries = [{"path": p, "sha256": by_path[p]["sha256"], "status": "pending"} for p in selected]
        journal = {"schema": 1, "id": run_id, "root": str(root), "created_at": now(),
                   "status": "applying", "plan_id": plan["id"], "tier": plan["tier"],
                   "entries": entries}
        # Durable intent journal first, so interrupted moves can be recovered.
        atomic_json(journal_path, journal)
        try:
            for entry in entries:
                src = safe_path(root, entry["path"])
                dst = state_path(root, "runs/" + run_id + "/files/" + entry["path"])
                if not src.is_file() or sha_file(src) != entry["sha256"] or dst.exists():
                    raise CloseoutError("文件变化或目标冲突，已停止：" + entry["path"])
                dst.parent.mkdir(parents=True, exist_ok=True)
                move_no_replace(src, dst)
                entry["status"] = "moved"
                atomic_json(journal_path, journal)
            journal["status"] = "applied"
        except BaseException:
            journal["status"] = "interrupted"
            atomic_json(journal_path, journal)
            raise
        atomic_json(journal_path, journal)
        return journal


def trash_directory(root):
    home_bin = Path.home() / ".Trash"
    if root.stat().st_dev == Path.home().stat().st_dev:
        return home_bin
    mount = root
    while mount.parent != mount and mount.parent.stat().st_dev == mount.stat().st_dev:
        mount = mount.parent
    return mount / ".Trashes" / str(os.getuid())


def trash_label(root, run_id):
    return "项目整理-" + (root.name[:28] or "任务") + "-" + run_id


def system_trash(folder):
    tool = "/usr/bin/trash"
    if not os.path.isfile(tool):
        raise CloseoutError("本机没有系统废纸篓命令；本批文件仍可从项目内恢复")
    status = subprocess.run([tool, str(folder)], capture_output=True)
    if status.returncode:
        raise CloseoutError("系统未确认移到废纸篓；请检查权限，本批文件未被永久删除")


def trash_run(root, run_id):
    """将核验过的一批文件送入系统回收站。"""
    root = root_path(root)
    if not re.fullmatch(r"[a-f0-9]{32}", run_id):
        raise CloseoutError("无效批次")
    with locked(root):
        journal_path = state_path(root, "runs/" + run_id + "/journal.json")
        j = read_json(journal_path)
        if (j.get("root") != str(root) or j.get("id") != run_id or
                j.get("schema") != 1 or j.get("status") != "applied"):
            raise CloseoutError("只可转移一份已完成且未恢复的整理记录")
        files = state_path(root, "runs/" + run_id + "/files")
        expected = {e["path"]:e["sha256"] for e in j["entries"]}
        seen = set()
        for f in files.rglob("*"):
            if f.is_symlink():raise CloseoutError("批次中发现未预期的链接")
            if f.is_dir():continue
            rel = f.relative_to(files).as_posix()
            if rel not in expected or not f.is_file() or sha_file(f)!=expected[rel]:
                raise CloseoutError("收纳文件与本次清单不符，未送废纸篓")
            seen.add(rel)
        if not expected or seen != set(expected):raise CloseoutError("本次清单不完整")
        label = trash_label(root, run_id)
        stage = files.with_name(label)
        if WINDOWS_RECYCLE:
            from .windows_recycle import send_batch
            return send_batch(files, stage, journal_path, j)
        dest = trash_directory(root) / label
        if stage.exists() or dest.is_symlink() or dest.exists():
            raise CloseoutError("目标文件夹已存在，请先核对")
        j["destination"]="trash";j["status"]="trashing"
        atomic_json(journal_path,j)
        files.rename(stage)
        try:
            system_trash(stage)
        except (OSError, CloseoutError):
            if stage.exists():
                stage.rename(files);j["status"]="applied";j.pop("destination",None)
                atomic_json(journal_path,j)
            raise
        j["status"]="trashed";atomic_json(journal_path,j)
        return j


def undo(root, run_id):
    root = root_path(root)
    if not re.fullmatch(r"[a-f0-9]{32}", run_id):
        raise CloseoutError("无效的归档批次 ID")
    with locked(root):
        journal_path = state_path(root, "runs/" + run_id + "/journal.json")
        journal = read_json(journal_path)
        if journal.get("schema") != 1 or journal.get("id") != run_id or journal.get("root") != str(root):
            raise CloseoutError("归档记录不匹配")
        if journal.get("destination") == "windows-recycle-bin":
            from .windows_recycle import prepare_undo
            prepare_undo(root, journal_path, journal)
        def saved_path(path):
            if journal.get("destination")=="trash":
                label=trash_label(root,run_id)
                # Interrupted transfer: resolve the filesystem state, not just the last journal write.
                for name in ("files",label):
                    stage=state_path(root,"runs/"+run_id+"/"+name)
                    if stage.is_dir():return safe_path(stage,path)
                saved=trash_directory(root)
                if saved.is_symlink():raise CloseoutError("废纸篓路径异常")
                return safe_path(saved,label+"/"+path)
            return state_path(root, "runs/" + run_id + "/files/" + path)
        checks, seen = [], set()
        for entry in journal["entries"]:
            path = entry["path"]
            if path in seen or PurePosixPath(path).parts[0] == STATE:
                raise CloseoutError("归档记录路径无效")
            seen.add(path)
            original = safe_path(root, path)
            saved = saved_path(path)
            if saved.exists():
                if not saved.is_file() or sha_file(saved) != entry["sha256"]:
                    raise CloseoutError("恢复冲突或隔离文件变化，没有覆盖文件：" + path)
                if original.exists():
                    if original.is_file() and os.path.samefile(original, saved):
                        checks.append((entry, saved, original, "unlink-copy"))
                    else:
                        raise CloseoutError("恢复冲突，没有覆盖文件：" + path)
                else:
                    checks.append((entry, saved, original, "move"))
            elif original.is_file() and sha_file(original) == entry["sha256"]:
                # Already restored, or interruption happened before rename.
                continue
            else:
                raise CloseoutError("找不到可验证的文件；如果已清空废纸篓则无法撤销：" + path)
        journal["status"] = "restoring"
        atomic_json(journal_path, journal)
        for entry, saved, original, operation in checks:
            # Repeat conflict check immediately before moving.
            original = safe_path(root, entry["path"])
            saved = saved_path(entry["path"])
            if sha_file(saved) != entry["sha256"]:
                raise CloseoutError("恢复时文件发生变化，已停止")
            if operation == "unlink-copy":
                if not original.exists() or not os.path.samefile(saved, original):
                    raise CloseoutError("恢复时原路径变化，已停止")
                saved.unlink()
            else:
                if original.exists():
                    raise CloseoutError("恢复时原路径变化，已停止")
                original.parent.mkdir(parents=True, exist_ok=True)
                move_no_replace(saved, original)
            entry["status"] = "restored"
            atomic_json(journal_path, journal)
        if journal.get("destination")=="trash":
            bucket=trash_directory(root)/trash_label(root,run_id)
            if bucket.is_dir() and not bucket.is_symlink():
                # Remove only empty directories in our own batch, never other Trash items.
                for directory, _, _ in os.walk(bucket,topdown=False,followlinks=False):
                    with contextlib.suppress(OSError):Path(directory).rmdir()
        journal["status"] = "restored"
        atomic_json(journal_path, journal)
        return journal
