import argparse
import json
import os
import sys
from pathlib import Path

from . import __version__
from .core import (CloseoutError, apply_plan, atomic_json, make_plan, now, read_json,
                   report, root_path, scan, state_path, undo, validate_plan)
from . import providers
from .discovery import choose_project, discover_projects, print_projects


def save_plan(plan, label):
    root = Path(plan["root"])
    dest = state_path(root, "plans/latest-" + label + ".json")
    atomic_json(dest, plan)
    # The state directory was checked above; report cannot overwrite user content.
    markdown = dest.with_suffix(".md")
    if markdown.is_symlink():
        raise CloseoutError("报告路径不能是符号链接")
    markdown.write_text(report(plan), encoding="utf-8")
    print("方案：" + str(dest))
    print("报告：" + str(markdown))
    counts = {}
    for r in plan["records"]:
        counts[r["decision"]] = counts.get(r["decision"], 0) + 1
    print(json.dumps(counts, ensure_ascii=False))
    return dest


def parser():
    p = argparse.ArgumentParser(description="项目收尾：先预览，选定文件后隔离，可恢复。")
    p.add_argument("--version", action="version", version=__version__)
    sub = p.add_subparsers(dest="command", required=True)
    wizard_cmd = sub.add_parser("wizard", help="自动发现项目，选择后进入本地盘点或模型分析")
    wizard_cmd.add_argument("--projects-root", action="append", help="扫描的项目总目录，可重复；默认 Documents/Codex")
    projects = sub.add_parser("projects", help="只列出识别到的项目，不读取项目内容")
    projects.add_argument("--root", action="append", help="扫描的项目总目录，可重复")
    projects.add_argument("--json", action="store_true", help="输出 JSON")
    web = sub.add_parser("web", help="打开按真实项目名称显示的本地可视化界面")
    web.add_argument("--port", type=int, default=0)
    web.add_argument("--catalog", default=None, help="Codex 项目与聊天名称索引")
    web.add_argument("--no-open", action="store_true", help="仅启动，不自动打开浏览器")
    plan = sub.add_parser("plan", help="本地扫描，生成 JSON 方案与 Markdown 报告，不移动文件")
    plan.add_argument("root")
    plan.add_argument("--tier", choices=["full", "balanced", "minimal"], default="balanced")
    plan.add_argument("--final", action="append", default=[], help="最终成果的相对文件/目录，可重复")
    plan.add_argument("--note", default="", help="项目目标或聊天摘要；本地脱敏后保存，可发送给模型")
    plan.add_argument("--markitdown", action="store_true", help="使用已安装的 MarkItDown 提取办公文档")
    for command in ("payload", "analyze"):
        a = sub.add_parser(command, help="预览精确外发正文" if command == "payload" else "显式发送并获取模型建议")
        a.add_argument("--plan", required=True)
        a.add_argument("--root", required=True)
        a.add_argument("--provider", required=True, choices=list(providers.DEFAULTS))
        a.add_argument("--config", default=None)
        a.add_argument("--model", default=None)
        a.add_argument("--batch-size", type=int, default=12)
        a.add_argument("--max-batches", type=int, default=100)
        if command == "analyze":
            a.add_argument("--env-file", default=None)
            a.add_argument("--send", action="store_true", help="确认向所选 endpoint 发送预览内容，可能计费")
            a.add_argument("--payload-sha256", required=True, help="必须与已预览 payload 的摘要一致")
            a.add_argument("--refresh", action="store_true", help="忽略本地建议缓存，再次调用 API")
    review = sub.add_parser("review", help="导入 Skill/人工生成的 JSON 建议，不调用 API")
    review.add_argument("--plan", required=True)
    review.add_argument("--root", required=True)
    review.add_argument("--advice", required=True)
    apply = sub.add_parser("apply", help="将明确选中的候选移入项目内隔离区，不永久删除")
    apply.add_argument("--plan", required=True)
    apply.add_argument("--root", required=True)
    apply.add_argument("--select", action="append", required=True, help="明确选择的相对路径，可重复")
    apply.add_argument("--yes", action="store_true", help="确认执行列出的文件")
    restore = sub.add_parser("undo", help="恢复一次归档，遇到冲突停止，不覆盖原文件")
    restore.add_argument("--root", required=True)
    restore.add_argument("--run", required=True)
    restore.add_argument("--yes", action="store_true")
    return p


def main(argv=None):
    args = parser().parse_args(argv)
    try:
        if args.command == "web":
            from .webapp import serve
            serve(args.port, args.catalog, not args.no_open)
            return 0
        if args.command == "wizard":
            return wizard(args.projects_root)
        if args.command == "projects":
            from .catalog import project_catalog
            result = project_catalog(args.root)
            if args.json:
                print(json.dumps(result, ensure_ascii=False, indent=2))
            else:
                print(result["notice"])
                print_projects(result, page_size=len(result["projects"]), show_paths=True)
                for warning in result["warnings"]:
                    print("提示：" + warning)
            return 0
        if args.command == "plan":
            save_plan(make_plan(args.root, args.tier, args.final, args.note, args.markitdown), "local")
            return 0
        root = root_path(args.root)
        if args.command == "undo":
            if not args.yes:
                raise CloseoutError("恢复需要 --yes，遇到冲突不会覆盖文件")
            result = undo(root, args.run)
            print("已恢复：" + result["id"])
            return 0
        plan = read_json(args.plan)
        validate_plan(plan, root)
        if args.command == "apply":
            if not args.yes:
                raise CloseoutError("执行需要 --yes；请先检查报告和 --select 文件清单")
            result = apply_plan(plan, root, args.select)
            print("已隔离：" + result["id"])
            print("隔离不释放磁盘空间；文件仍保存在项目内的 .project-closeout/runs/。")
            print("恢复：undo --root " + json.dumps(str(root), ensure_ascii=False) + " --run " + result["id"] + " --yes")
            return 0
        if scan(root)["fingerprint"] != plan["fingerprint"]:
            raise CloseoutError("项目文件已变化，请重新生成方案")
        if args.command == "review":
            allowed = {r["path"] for r in plan["records"] if r["kind"] == "file"}
            advice = providers.parse_advice(Path(args.advice).read_text(encoding="utf-8"), allowed)
            providers.merge_advice(plan, advice)
            plan["model"] = {"provider": "skill-or-human", "analyzed_at": now(), "api_calls": 0}
            save_plan(plan, "reviewed")
            return 0
        settings = providers.config(args.config, args.provider, args.model)
        manifest = providers.prepare(plan, settings, args.batch_size, args.max_batches)
        dest = state_path(root, "payloads/" + manifest["payload_sha256"] + ".json")
        atomic_json(dest, manifest)
        print("外发预览：" + str(dest))
        print("批次数：" + str(manifest["batch_count"]))
        print("payload-sha256：" + manifest["payload_sha256"])
        if args.command == "payload":
            print("尚未联网。检查上述文件后，analyze 使用相同参数与此摘要。")
            return 0
        if not args.send:
            raise CloseoutError("尚未发送；调用模型需要 --send 和预览摘要")
        if args.payload_sha256 != manifest["payload_sha256"]:
            raise CloseoutError("请求与预览摘要不一致，没有发送")
        key = providers.load_key(settings, args.env_file)
        plan = providers.analyze(plan, settings, manifest, key, args.refresh,
                                 lambda i, n, kind: print("分析 " + str(i) + "/" + str(n) + " [" + kind + "]", flush=True))
        save_plan(plan, "ai")
        return 0
    except (EOFError, KeyboardInterrupt):
        print("\n已取消", file=sys.stderr)
        return 130
    except (CloseoutError, OSError, ValueError, KeyError, TypeError) as error:
        if isinstance(error, CloseoutError):
            message = str(error)
        else:
            # Parsing/config/library errors must not echo raw values or secrets.
            message = "无法完成操作（" + type(error).__name__ + "）；请检查文件格式、权限和路径"
        print("错误：" + message, file=sys.stderr)
        return 2


def wizard(projects_roots=None):
    """Small terminal entry; keys are entered locally and kept in memory only."""
    import getpass
    print("项目收尾 · 本地终端版")
    print("扫描不移动文件。可以先不配置 API，只查看本地建议。\n")
    root = choose_project(projects_roots)
    if root is None:
        print("已取消")
        return 0
    print("1 完整保留过程 / 2 精简归档 / 3 最小交付")
    choice = input("档位 [2]：").strip() or "2"
    if choice not in {"1", "2", "3"}:
        raise CloseoutError("请选择 1、2 或 3")
    tier = {"1": "full", "2": "balanced", "3": "minimal"}[choice]
    final = input("最终成果的项目相对路径（可为目录；暂不确定则留空）：").strip()
    note = input("项目目标或特别想保留的内容（可留空）：").strip()
    plan = make_plan(root, tier, [final] if final else [], note)
    save_plan(plan, "local")
    print("\n可打开上面的 .md 报告查看文件去留及理由。")
    if input("是否进一步调用你自己的 API 分析？[y/N]：").strip().lower() != "y":
        return 0
    names = list(providers.DEFAULTS)
    print(" / ".join(str(i + 1) + " " + name for i, name in enumerate(names)))
    selected = input("服务商 [1]：").strip() or "1"
    if selected not in {"1", "2", "3", "4"}:
        raise CloseoutError("请选择 1 至 4")
    provider = names[int(selected) - 1]
    model = input("模型 ID（按你的 API 账户填写）：").strip()
    settings = providers.config(None, provider, model)
    key = os.environ.get(settings["api_key_env"], "")
    if not key:
        key = getpass.getpass("API Key（只在本地输入，不显示、不保存）：").strip()
    if not key:
        print("未输入密钥，已保留本地报告，未联网")
        return 0
    manifest = providers.prepare(plan, settings)
    dest = state_path(root, "payloads/" + manifest["payload_sha256"] + ".json")
    atomic_json(dest, manifest)
    print("\n将发送到：" + settings["base_url"])
    print("批次数：" + str(manifest["batch_count"]))
    print("请打开并检查实际外发内容：" + str(dest))
    if input("已检查，确认发送并承担 API 费用？[y/N]：").strip().lower() != "y":
        print("已取消发送；密钥没有保存")
        return 0
    if scan(root)["fingerprint"] != plan["fingerprint"]:
        raise CloseoutError("项目文件已变化，请重新盘点")
    providers.analyze(plan, settings, manifest, key,
                      progress=lambda i, n, kind: print("分析 " + str(i) + "/" + str(n) + " [" + kind + "]", flush=True))
    save_plan(plan, "ai")
    print("分析完成，没有移动项目文件。按报告选定具体候选后，可用 apply 执行。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
