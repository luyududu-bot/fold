import os
import json
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "skills/project-closeout/scripts"))
from project_closeout import core


class WindowsBatchTest(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory(prefix="fold-recycle-test-")
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name).resolve()
        self.relative = "work/previews/中文 sample.png"
        self.original = self.root / self.relative
        self.original.parent.mkdir(parents=True)
        self.original.write_bytes(b"fold-recycle-fixture")
        plan = core.make_plan(self.root, "balanced", [], "")
        self.run = core.apply_plan(plan, self.root, [self.relative])["id"]
        self.batch = self.root / core.STATE / "runs" / self.run
        self.stage = self.batch / core.trash_label(self.root, self.run)

    @unittest.skipUnless(os.name == "nt" and os.getenv("FOLD_TEST_RECYCLE_BIN") == "1",
                         "真实回收站测试需要显式启用")
    def test_round_trip(self):
        journal = core.trash_run(self.root, self.run)
        self.assertEqual(journal["status"], "trashed")
        self.assertFalse(self.stage.exists())
        self.assertFalse((self.batch / "files" / self.relative).exists())
        from project_closeout.windows_recycle import shell_batch
        self.assertTrue(shell_batch(self.stage, "find").is_dir())
        self.original.write_bytes(b"new file")
        with self.assertRaises(core.CloseoutError):
            core.undo(self.root, self.run)
        self.assertEqual(self.original.read_bytes(), b"new file")
        self.original.unlink()
        cli = Path(__file__).resolve().parents[1] / "closeout.py"
        subprocess.run([sys.executable, "-B", str(cli), "undo", "--root", str(self.root),
                        "--run", self.run, "--yes"], check=True, capture_output=True)
        self.assertEqual(self.original.read_bytes(), b"fold-recycle-fixture")
        self.assertIsNone(shell_batch(self.stage, "find"))
        core.undo(self.root, self.run)

    def test_interrupted_release_and_manual_shell_restore(self):
        from project_closeout import windows_recycle
        bucket = self.root / "mock-recycle"
        def recycle(stage):
            stage.rename(bucket)
        with patch("project_closeout.core.WINDOWS_RECYCLE", True), patch.object(
            windows_recycle, "recycle_folder", recycle
        ), patch.object(windows_recycle, "shell_batch", return_value=bucket):
            core.trash_run(self.root, self.run)
        bucket.rename(self.stage)
        os.link(self.stage / self.relative, self.batch / "files" / self.relative)
        with patch.object(windows_recycle, "shell_batch", side_effect=AssertionError):
            core.undo(self.root, self.run)
        self.assertEqual(self.original.read_bytes(), b"fold-recycle-fixture")

    def test_unconfirmed_recycle_still_allows_undo(self):
        with patch("project_closeout.core.WINDOWS_RECYCLE", True), patch(
            "project_closeout.windows_recycle.recycle_folder", side_effect=OSError("模拟系统拒绝")
        ), patch("project_closeout.windows_recycle.shell_batch", return_value=None):
            with self.assertRaises(OSError):
                core.trash_run(self.root, self.run)
            core.undo(self.root, self.run)
        self.assertEqual(self.original.read_bytes(), b"fold-recycle-fixture")

    @unittest.skipUnless(os.name == "nt" and os.getenv("FOLD_TEST_RECYCLE_BIN") == "1",
                         "真实回收站测试需要显式启用")
    def test_web_apply_and_undo(self):
        from project_closeout.webapp import App
        from project_closeout.windows_recycle import shell_batch
        from project_closeout.preferences import Preferences
        core.undo(self.root, self.run)
        catalog = self.batch / "test-catalog.json"
        catalog.write_text(json.dumps({"schema": 1, "threads": [], "projects": [
            {"path": str(self.root), "label": "临时测试项目"}]}), encoding="utf-8")
        app = App(catalog, Preferences(self.batch / "test-settings.json"))
        project = next(p for p in app.projects.values() if p["path"] == str(self.root))
        job = app.action("/api/scan", {"project": project["id"], "tier": "balanced",
                                      "finals": []})["job"]
        deadline = time.monotonic() + 10
        while app.public_job(job)["status"] == "running" and time.monotonic() < deadline:
            time.sleep(0.02)
        self.assertEqual(app.public_job(job)["status"], "done")
        reply = app.action("/api/apply", {"job": job, "selected": [self.relative]})
        self.assertEqual(reply["destination"], "trash")
        stage = self.batch.parent / reply["run"] / core.trash_label(self.root, reply["run"])
        self.assertTrue(shell_batch(stage, "find").is_dir())
        app.action("/api/undo", {"run": reply["run"]})
        self.assertEqual(self.original.read_bytes(), b"fold-recycle-fixture")

    @unittest.skipUnless(os.name == "nt" and os.getenv("FOLD_TEST_RECYCLE_BIN") == "1",
                         "真实回收站测试需要显式启用")
    def test_long_paths_from_cli_after_restart(self):
        from project_closeout import windows_recycle
        cli = Path(__file__).resolve().parents[1] / "closeout.py"
        for depth, compact in [(3, False), (12, True)]:
            with self.subTest(depth=depth):
                project = self.root / ("长路径项目" * 9) / str(depth)
                folder = project.joinpath("work", "previews", *(["很深的子目录0123456789"] * depth))
                files = [folder / name / "同名样张.png" for name in ["甲", "乙"]]
                for file in files:
                    file.parent.mkdir(parents=True, exist_ok=True)
                    file.write_bytes(file.parent.name.encode())
                rels = [file.relative_to(project).as_posix() for file in files]
                plan = core.make_plan(project, "balanced", [], "")
                plan_path = core.state_path(project, "plans/test.json")
                core.atomic_json(plan_path, plan)
                cmd = [sys.executable, "-B", str(cli), "apply", "--root", str(project),
                       "--plan", str(plan_path), "--recycle", "--yes"]
                for rel in rels:
                    cmd.extend(["--select", rel])
                completed = subprocess.run(cmd, capture_output=True)
                self.assertEqual(completed.returncode, 0, completed.stderr)
                journal_path, = (project / core.STATE / "runs").glob("*/journal.json")
                journal = core.read_json(journal_path)
                self.assertEqual(journal["status"], "trashed")
                self.assertEqual(journal.get("windows_compact", False), compact)
                stage = windows_recycle.recorded_stage(project, journal_path, journal)
                bucket = windows_recycle.shell_batch(stage, "find")
                self.assertIsNotNone(bucket)
                actual = sorted(p.read_bytes() for p in bucket.rglob("*") if p.is_file())
                self.assertEqual(actual, sorted(file.parent.name.encode() for file in files))
                subprocess.run([sys.executable, "-B", str(cli), "undo", "--root", str(project),
                                "--run", journal["id"], "--yes"], check=True, capture_output=True)
                self.assertEqual([file.read_bytes() for file in files],
                                 [file.parent.name.encode() for file in files])

    def test_legacy_short_stage_receipt(self):
        from project_closeout import windows_recycle
        journal_path = self.batch / "journal.json"
        journal = core.read_json(journal_path)
        journal.update(status="trashed", destination="windows-recycle-bin",
                       windows_short_stage=True)
        core.atomic_json(journal_path, journal)
        appdata = self.root / "local-appdata"
        stage = appdata / "Fold/Recovery" / ("Fold-" + self.run)
        stage.parent.mkdir(parents=True)
        (self.batch / "files").rename(stage)
        with patch.dict(os.environ, {"LOCALAPPDATA": str(appdata)}):
            core.undo(self.root, self.run)
        self.assertEqual(self.original.read_bytes(), b"fold-recycle-fixture")
