"""Join exact local paths to names exported from the Codex desktop connector."""
import hashlib
import json
import os
from pathlib import Path

from .discovery import default_roots, discover_projects


def default_catalog():
    explicit = os.environ.get("PROJECT_CLOSEOUT_CATALOG")
    if explicit:
        return Path(explicit).expanduser()
    installed = Path(__file__).resolve().parents[2] / "project-index.local.json"
    if installed.is_file():
        return installed
    # Source checkout. A copied skill can explicitly receive its own index path.
    return Path(__file__).resolve().parents[4] / "project-index.local.json"


def load_catalog(path=None):
    path = Path(path) if path else default_catalog()
    if not path.is_file() or path.is_symlink() or path.stat().st_size > 2 * 1024 * 1024:
        return {"projects": [], "threads": []}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        if data.get("schema") != 1:
            return {"projects": [], "threads": []}
        return {"projects": [p for p in data.get("projects", []) if isinstance(p, dict)
                             and isinstance(p.get("label"), str) and isinstance(p.get("path"), str)
                             and Path(p["path"]).is_absolute()],
                "threads": [t for t in data.get("threads", []) if isinstance(t, dict)
                            and isinstance(t.get("title"), str) and isinstance(t.get("cwd"), str)
                            and Path(t["cwd"]).is_absolute()]}
    except (ValueError, OSError, TypeError, AttributeError):
        return {"projects": [], "threads": []}


def project_catalog(roots=None, catalog_path=None):
    metadata = load_catalog(catalog_path)
    project_names = {str(Path(p["path"]).resolve()): p["label"] for p in metadata["projects"]}
    threads = {}
    for t in sorted(metadata["threads"], key=lambda x: x.get("updatedAt", 0), reverse=True):
        path = str(Path(t["cwd"]).resolve())
        threads.setdefault(path, []).append(t)
    if roots is None:
        roots = list(default_roots())
        # Include real app projects outside generated Documents/Codex folders.
        roots += [Path(p["path"]) for p in metadata["projects"]]
        roots += [Path(t["cwd"]) for t in metadata["threads"]]
    result = discover_projects(roots)
    by_path = {p["path"]: p for p in result["projects"]}
    # A registered project can be an empty folder, or have no code markers.
    for raw in roots:
        path = Path(raw).expanduser()
        canonical = str(path.resolve())
        if canonical not in by_path and (canonical in project_names or canonical in threads) and path.is_dir() and not path.is_symlink():
            project = {"name": path.name, "path": canonical, "relative_path": path.name,
                       "search_root": canonical, "date": "", "directory_modified": path.stat().st_mtime,
                       "signals": ["Codex 名称索引"], "output_examples": []}
            result["projects"].append(project)
            by_path[canonical] = project
    for p in result["projects"]:
        entries = threads.get(p["path"], [])
        p["id"] = hashlib.sha256(p["path"].encode()).hexdigest()[:24]
        p["threads"] = [{k: t.get(k) for k in ("id", "title", "updatedAt", "archived")} for t in entries]
        p["last_activity"] = max([t.get("updatedAt", 0) for t in entries] or [p["directory_modified"]])
        if p["path"] in project_names:
            p["display_name"], p["display_kind"] = project_names[p["path"]], "project"
        elif entries:
            # Shared user-named folders group their chats; generated chat folders
            # show the source-provided title exactly rather than inventing a name.
            if len(entries) > 1 and not p["date"]:
                p["display_name"], p["display_kind"] = p["name"], "shared"
            else:
                p["display_name"], p["display_kind"] = entries[0]["title"], "chat"
        else:
            p["display_name"], p["display_kind"] = p["name"], "folder"
    order = {"project": 0, "shared": 1, "chat": 2, "folder": 3}
    result["projects"].sort(key=lambda p: (order[p["display_kind"]], -p["last_activity"], p["path"]))
    result["name_notice"] = "项目名与聊天名来自本机 Codex 索引；未匹配的文件夹单独显示，不猜测名称。"
    return result
