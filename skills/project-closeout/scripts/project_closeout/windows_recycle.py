"""Windows 系统回收站中的整批移出与恢复。"""
import ctypes
import os
import subprocess
from pathlib import Path

from .core import CloseoutError, atomic_json, safe_path, sha_file, trash_label


def recycle_folder(stage):
    from ctypes import wintypes

    class FileOperation(ctypes.Structure):
        if ctypes.sizeof(ctypes.c_void_p) == 4:
            _pack_ = 1
        _fields_ = [("hwnd", wintypes.HWND), ("operation", wintypes.UINT),
                    ("source", wintypes.LPCWSTR), ("target", wintypes.LPCWSTR),
                    ("flags", wintypes.WORD), ("aborted", wintypes.BOOL),
                    ("mappings", wintypes.HANDLE), ("title", wintypes.LPCWSTR)]

    # FO_DELETE，允许撤销、无界面。即使系统拒绝回收，本地仍有完整文件。
    source = ctypes.create_unicode_buffer(str(stage) + "\0")
    op = FileOperation(operation=3, source=ctypes.cast(source, wintypes.LPCWSTR),
                       flags=0x40 | 0x04 | 0x10 | 0x400)
    operation = ctypes.WinDLL("shell32").SHFileOperationW
    operation.argtypes = [ctypes.POINTER(FileOperation)]
    operation.restype = ctypes.c_int
    if operation(ctypes.byref(op)) or op.aborted:
        raise CloseoutError("Windows 未完成回收。可以在 Fold 撤销本批。")


_SHELL_QUERY = r"""
$ErrorActionPreference = 'Stop'
[Console]::OutputEncoding = New-Object System.Text.UTF8Encoding($false)
$batchPath = $env:FOLD_RECYCLE_STAGE
$name = [IO.Path]::GetFileName($batchPath)
$parent = [IO.Path]::GetDirectoryName($batchPath)
$shell = New-Object -ComObject Shell.Application
$folder = $shell.NameSpace(10)
$items = $folder.Items()
$matchesForBatch = @($items | Where-Object {
    $_.Name -eq $name -and $_.ExtendedProperty('System.Recycle.DeletedFrom') -eq $parent
})
if ($matchesForBatch.Count -eq 0) { exit 0 }
if ($matchesForBatch.Count -ne 1) { throw 'Ambiguous batch' }
$item = $matchesForBatch[0]
if ($env:FOLD_RECYCLE_ACTION -eq 'restore') {
    if ([IO.Directory]::Exists($batchPath) -or [IO.File]::Exists($batchPath)) { throw 'Restore conflict' }
    $item.InvokeVerb('undelete')
    for ($i = 0; $i -lt 100 -and ![IO.Directory]::Exists($batchPath); $i++) {
        Start-Sleep -Milliseconds 100
    }
    if (![IO.Directory]::Exists($batchPath)) { throw 'Restore unconfirmed' }
    [Console]::WriteLine($batchPath)
} else { [Console]::WriteLine($item.Path) }
"""


def shell_batch(stage, action):
    """按完整原目录和唯一批次名查找，只还原这一项。"""
    exe = Path(os.environ["SystemRoot"]) / "System32/WindowsPowerShell/v1.0/powershell.exe"
    try:
        result = subprocess.run(
            [str(exe), "-NoProfile", "-NonInteractive", "-STA", "-Command", _SHELL_QUERY],
            env=dict(os.environ, FOLD_RECYCLE_STAGE=str(stage), FOLD_RECYCLE_ACTION=action),
            capture_output=True, timeout=60, creationflags=subprocess.CREATE_NO_WINDOW,
        )
    except subprocess.TimeoutExpired as error:
        raise CloseoutError("Windows 回收站响应超时，请稍后再次核对本批。") from error
    if result.returncode:
        raise CloseoutError("Windows 未确认本批回收站操作，请检查批次记录。")
    path = result.stdout.decode("utf-8-sig").strip()
    return Path(path) if path else None


def recorded_stage(root, journal_path, journal):
    """从批次号和已知根目录推导中转位置，兼容此前的短路径批次。"""
    name = "Fold-" + journal["id"]
    if journal.get("windows_short_stage") is True:
        local = Path(os.environ["LOCALAPPDATA"]).resolve()
        return safe_path(local, "Fold/Recovery/" + name)
    if "windows_parent_depth" in journal:
        depth = journal["windows_parent_depth"]
        parents = (root, *root.parents)
        if type(depth) is not int or not 0 <= depth < len(parents):
            raise CloseoutError("无效的中转目录记录")
        return safe_path(parents[depth], name)
    return safe_path(journal_path.parent, trash_label(root, journal["id"]))


def shell_units(value):
    # Windows Shell 的传统路径上限以 UTF-16 单元计数，留出目录操作余量。
    return len(str(value).encode("utf-16-le")) // 2


def reserve_stage(root, files, stage, journal):
    longest = max(shell_units(entry["path"]) for entry in journal["entries"])
    if shell_units(stage) + 1 + longest <= 247:
        stage.mkdir()
        return stage

    name = "Fold-" + journal["id"]
    candidates = []
    appdata = os.environ.get("LOCALAPPDATA")
    device = files.stat().st_dev
    if appdata:
        local = Path(appdata).resolve()
        if local.is_dir() and local.stat().st_dev == device:
            candidates.append((safe_path(local, "Fold/Recovery/" + name),
                               {"windows_short_stage": True}))
    # 其他卷优先使用项目目录，再尝试同卷的较短父目录。硬链接始终在同卷内。
    for depth, parent in enumerate((root, *root.parents)):
        if parent.stat().st_dev != device:
            break
        candidates.append((safe_path(parent, name), {"windows_parent_depth": depth}))
    candidates.append((stage, {}))
    # 优先保留目录层次；最短位置仍超限时，按清单序号还原原始路径。
    for compact in (False, True):
        for candidate, fields in candidates:
            if shell_units(candidate) + 1 + (12 if compact else longest) > 247:
                continue
            try:
                candidate.parent.mkdir(parents=True, exist_ok=True)
                candidate.mkdir()
            except (PermissionError, FileExistsError):
                continue
            journal.update(fields)
            if compact:
                journal["windows_compact"] = True
            return candidate
    raise CloseoutError("同一磁盘上没有可用的短中转目录，本批仍可从项目内撤销。")


def batch_members(journal):
    for index, entry in enumerate(journal["entries"]):
        name = f"F{index:08d}" if journal.get("windows_compact") else entry["path"]
        yield entry, name


def send_batch(files, stage, journal_path, journal):
    # 仅向系统交出硬链接。收到真实回收站条目并核对全部文件后才释放本地链接。
    stage = reserve_stage(Path(journal["root"]), files, stage, journal)
    journal.update(destination="windows-recycle-bin", status="trashing")
    try:
        atomic_json(journal_path, journal)
    except OSError:
        stage.rmdir()
        raise
    for entry, name in batch_members(journal):
        target = safe_path(stage, name)
        target.parent.mkdir(exist_ok=True, parents=True)
        os.link(safe_path(files, entry["path"]), target)
    recycle_folder(stage)
    bucket = shell_batch(stage, "find")
    if stage.exists() or not bucket or not bucket.is_dir():
        raise CloseoutError("未能在 Windows 回收站确认本批，文件仍在项目内暂存。")
    for entry, name in batch_members(journal):
        local = safe_path(files, entry["path"])
        recycled = safe_path(bucket, name)
        if not local.samefile(recycled) or sha_file(recycled) != entry["sha256"]:
            raise CloseoutError("回收站内容核对失败，项目内暂存已保留。")
    for entry, name in batch_members(journal):
        local = safe_path(files, entry["path"])
        if not local.samefile(safe_path(bucket, name)):
            raise CloseoutError("回收站内容已变化，已停止释放暂存。")
        local.unlink()
    journal["status"] = "trashed"
    atomic_json(journal_path, journal)
    return journal


def prepare_undo(root, journal_path, journal):
    files = safe_path(journal_path.parent, "files")
    stage = recorded_stage(root, journal_path, journal)
    stage.parent.mkdir(parents=True, exist_ok=True)
    entries = journal["entries"]
    for entry in entries:
        original = safe_path(root, entry["path"])
        if original.exists():
            raise CloseoutError("原路径已有文件，未覆盖：" + entry["path"])
    if not stage.exists():
        shell_batch(stage, "restore")
    # 覆盖系统回收失败、手动还原批次，以及释放暂存中途退出三种状态。
    for entry, name in batch_members(journal):
        local = safe_path(files, entry["path"])
        source = safe_path(stage, name)
        if source.exists():
            if sha_file(source) != entry["sha256"]:
                raise CloseoutError("回收站文件内容已变化：" + entry["path"])
            local.parent.mkdir(parents=True, exist_ok=True)
            if not local.exists():
                os.link(source, local)
            if not local.samefile(source):
                raise CloseoutError("恢复暂存中出现同名文件：" + entry["path"])
            source.unlink()
        elif not local.is_file() or sha_file(local) != entry["sha256"]:
            raise CloseoutError("批次文件缺失或已变化，请核对回收站：" + entry["path"])
    if stage.is_dir():
        parents = {stage}
        for _, name in batch_members(journal):
            parent = safe_path(stage, name).parent
            while parent != stage:
                parents.add(parent)
                parent = parent.parent
        for parent in sorted(parents, key=lambda p: len(p.parts), reverse=True):
            if parent.is_dir():
                parent.rmdir()
    for field in ("destination", "windows_compact", "windows_parent_depth", "windows_short_stage"):
        journal.pop(field, None)
    journal["status"] = "applied"
    atomic_json(journal_path, journal)
