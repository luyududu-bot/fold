"""Bounded, metadata-only project discovery; no contents or chat logs are read."""
import os
import re
import shlex
from datetime import datetime
from pathlib import Path

from .core import CloseoutError, SKIP_DIRS, root_path, sensitive

EXCLUDED = SKIP_DIRS | {"outputs", "work", "inputs", "assets", "data", "raw", "src", "source",
                        "sources", "tests", "test", "cache", "__pycache__", "build", "dist",
                        "vendor", "target", "coverage", "logs", "tmp", "temp", "scratch"}
MANIFESTS = {"package.json", "pyproject.toml", "Cargo.toml", "go.mod", "pom.xml", "build.gradle",
             "CMakeLists.txt", "Project.toml", "Gemfile", "composer.json"}
PROJECT_EXTENSIONS = {".docx", ".pptx", ".xlsx", ".pdf", ".drawio", ".psd", ".blend", ".sketch",
                      ".py", ".ipynb", ".tex", ".fig", ".ai", ".svg"}
DATE = re.compile(r"\d{4}-\d{2}-\d{2}\Z")


def clean_label(value, limit=90):
    value = "".join(c if c.isprintable() else " " for c in str(value))
    return value if len(value) <= limit else value[:limit - 1] + "…"


def input_directory(raw):
    raw = raw.strip()
    if not raw:
        raise CloseoutError("未输入文件夹")
    if not Path(raw).expanduser().is_dir():
        if os.name == "nt":
            windows_path = raw.strip('"').replace("\\ ", " ")
            if Path(windows_path).expanduser().is_dir():
                return root_path(windows_path)
        try:
            values = shlex.split(raw)
        except ValueError:
            values = []
        if len(values) == 1:
            raw = values[0]
    return root_path(raw)


def default_roots():
    configured = os.environ.get("PROJECT_CLOSEOUT_ROOTS", "")
    if configured:
        return [Path(x).expanduser() for x in configured.split(os.pathsep) if x]
    codex = Path.home() / "Documents" / "Codex"
    return [codex] if codex.is_dir() else []


def discover_projects(roots=None, max_depth=3, max_projects=500, max_directories=4000):
    """Stop at each project boundary, so its outputs/work aren't separate projects."""
    roots = default_roots() if roots is None else roots
    projects, warnings, seen, visited = [], [], set(), 0
    def visit(path, base, depth, date_hint=""):
        nonlocal visited
        if len(projects) >= max_projects or visited >= max_directories:
            return
        try:
            if path.is_symlink() or not path.is_dir():
                return
            canonical = path.resolve()
            if canonical in seen:
                return
            seen.add(canonical)
            visited += 1
            entries = []
            with os.scandir(path) as iterator:
                for entry in iterator:
                    if len(entries) >= 10000:
                        warnings.append("目录条目过多，已跳过：" + clean_label(path))
                        return
                    entries.append(entry)
            entries.sort(key=lambda e: e.name)
            names = {e.name for e in entries if not e.is_symlink()}
            directories = {e.name for e in entries if not e.is_symlink() and e.is_dir(follow_symlinks=False)}
            files = [e.name for e in entries if not e.is_symlink() and e.is_file(follow_symlinks=False)
                     and not e.name.startswith(".") and not sensitive(e.name)]
            signals = []
            if {"work", "outputs"} <= directories:
                signals.append("Codex 工作区")
            if ".git" in names:
                signals.append("Git 项目")
            manifests = sorted(set(files) & MANIFESTS)
            if manifests:
                signals.append("代码项目（" + manifests[0] + "）")
            if {"inputs", "outputs"} <= directories:
                signals.append("素材与成果目录")
            if any(n.endswith((".xcodeproj", ".xcworkspace")) for n in names):
                signals.append("Xcode 项目")
            # A dated container is not itself a project, even if it has notes.
            is_date = bool(DATE.fullmatch(path.name))
            if is_date:
                date_hint = path.name
            is_dated_child = bool(DATE.fullmatch(path.parent.name))
            if not signals and is_dated_child:
                signals.append("日期目录中的项目文件夹")
            if not signals and depth > 0 and any(Path(f).suffix.lower() in PROJECT_EXTENSIONS for f in files):
                signals.append("文档或创作文件集合")
            if signals and not is_date:
                samples = []
                output_dir = path / "outputs"
                if "outputs" in directories:
                    with os.scandir(output_dir) as iterator:
                        for entry in iterator:
                            if not entry.name.startswith(".") and not sensitive(entry.name) and not entry.is_symlink():
                                samples.append(entry.name)
                            if len(samples) >= 3:
                                break
                modified = path.stat().st_mtime
                projects.append({"name": path.name, "path": str(canonical), "search_root": str(base),
                                 "relative_path": path.relative_to(base).as_posix() if path != base else path.name,
                                 "date": date_hint, "directory_modified": modified,
                                 "signals": signals, "output_examples": sorted(samples)})
                return
            if depth >= max_depth:
                return
            for entry in entries:
                if (entry.name.startswith(".") or entry.name in EXCLUDED or sensitive(entry.name)
                        or entry.is_symlink() or not entry.is_dir(follow_symlinks=False)
                        or entry.name.endswith((".app", ".xcodeproj", ".xcworkspace"))):
                    continue
                visit(path / entry.name, base, depth + 1, date_hint)
        except OSError:
            warnings.append("无法读取目录，已跳过：" + clean_label(path))
    for raw in roots:
        path = Path(raw).expanduser().absolute()
        if not path.is_dir() or path.is_symlink():
            warnings.append("扫描目录不存在或是符号链接：" + clean_label(path))
            continue
        base = path.resolve()
        visit(base, base, 0)
    if len(projects) >= max_projects or visited >= max_directories:
        warnings.append("已达到发现上限，请选择更小的扫描目录。")
    projects.sort(key=lambda p: (p["date"] or datetime.fromtimestamp(p["directory_modified"]).strftime("%Y-%m-%d"),
                                 p["directory_modified"], p["path"]), reverse=True)
    return {"projects": projects, "warnings": warnings, "visited_directories": visited,
            "notice": "按目录结构识别项目候选；只读取名称与元数据，未读取内容或判断项目是否结束。"}


def print_projects(result, start=0, page_size=8, show_paths=False):
    projects = result["projects"]
    for i, project in enumerate(projects[start:start + page_size], start + 1):
        print(str(i) + ". " + clean_label(project.get("display_name", project["relative_path"])))
        examples = "、".join(clean_label(n, 32) for n in project["output_examples"])
        if examples:
            print("   成果目录：" + examples)
        if show_paths:
            print("   " + clean_label(project["path"], 200))


def choose_project(roots=None):
    from .catalog import project_catalog
    use_default = roots is None
    roots = default_roots() if use_default else list(roots)
    result = project_catalog(None if use_default else roots)
    page, query, page_size = 0, "", 8
    while True:
        filtered = [p for p in result["projects"]
                    if query.casefold() in (p.get("display_name", p["name"]) + " " + p["path"] + " " + " ".join(p["output_examples"])).casefold()]
        pages = max(1, (len(filtered) + page_size - 1) // page_size)
        page = min(page, pages - 1)
        print("\n选择要整理的项目")
        print("扫描位置：" + ("、".join(clean_label(p, 140) for p in roots) or "未设置；可按 f 选择"))
        print("发现 " + str(len(result["projects"])) + " 个项目候选" +
              ("，搜索“" + clean_label(query) + "”后 " + str(len(filtered)) + " 个" if query else "") +
              "；第 " + str(page + 1) + "/" + str(pages) + " 页")
        print_projects({"projects": filtered}, page * page_size, page_size)
        if not filtered:
            print("没有匹配项目。可用 f 扫描其他目录，或 0 手动指定项目。")
        for warning in result["warnings"]:
            print("提示：" + warning)
        print("输入项目编号；n 下一页 / p 上一页 / /关键词 搜索 / / 清除搜索")
        print("f 扫描其他目录 / r 刷新 / 0 手动指定项目 / q 退出")
        value = input("请选择：").strip()
        if value.lower() == "q":
            return None
        if value.isdecimal() and value != "0":
            index = int(value) - 1
            if 0 <= index < len(filtered):
                project = filtered[index]
                selected = root_path(project["path"])
                print("\n已选择：" + clean_label(project.get("display_name", project["name"])))
                print("项目路径：" + str(selected))
                return selected
            print("编号不在列表中，请重新选择。")
        elif value == "0":
            raw = input("项目文件夹（可从 Finder 拖入；回车返回列表）：").strip()
            if raw:
                try:
                    return input_directory(raw)
                except CloseoutError as error:
                    print(str(error))
        elif value.lower() == "n":
            page = min(page + 1, pages - 1)
        elif value.lower() == "p":
            page = max(page - 1, 0)
        elif value.startswith("/"):
            query, page = value[1:].strip(), 0
        elif value.lower() == "r":
            result = project_catalog(None if use_default else roots)
        elif value.lower() == "f":
            raw = input("要扫描的项目总目录（回车返回列表）：").strip()
            if raw:
                try:
                    roots = [input_directory(raw)]
                    use_default = False
                    result = project_catalog(roots)
                    page, query = 0, ""
                except CloseoutError as error:
                    print(str(error))
        elif value:
            # Preserve the original paste/drag workflow for experienced users.
            try:
                return input_directory(value)
            except CloseoutError:
                print("请输入列表编号或菜单选项。")
