import contextlib
import copy
import io
import json
import os
import sys
import tempfile
import unittest
import urllib.error
from pathlib import Path
from unittest.mock import patch
from symlink_fixture import create_symlink

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "skills/project-closeout/scripts"))
from project_closeout import core, providers
from project_closeout.cli import main


class ProjectTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name).resolve() / "项目 with spaces"
        self.root.mkdir()
        self.write("outputs/final.md", "# 最终结果\n![图](../assets/plot.svg)\n")
        self.write("assets/plot.svg", '<svg><image href="../data/table.csv"/></svg>')
        self.write("data/table.csv", "x,y\n1,2\n")
        self.write("work/previews/page-01.png", b"render-preview")
        self.write("work/previews/page-02.png", b"another-preview")
        self.write("draft.txt", "a draft of uncertain value")
        self.write("copy.txt", "a draft of uncertain value")
        self.write("work/script.py", "# expensive experimental code")
        self.write(".env.local", "OPENAI_API_KEY=never-send-this-value")

    def tearDown(self):
        self.tmp.cleanup()

    def write(self, rel, content):
        p = self.root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(content.encode() if isinstance(content, str) else content)
        return p

    def plan(self, tier="balanced"):
        return core.make_plan(self.root, tier, ["outputs/final.md"])

    def record(self, plan, path):
        return next(r for r in plan["records"] if r["path"] == path)

    def advice(self, plan, action="candidate", confidence=.99):
        return [{"path": r["path"], "action": action, "confidence": confidence,
                 "reason": "测试用途"} for r in plan["records"] if r["kind"] == "file"]

    def test_final_dependencies_sources_and_uncertain_duplicates(self):
        p = self.plan()
        for name in ("outputs/final.md", "assets/plot.svg", "data/table.csv", "work/script.py"):
            r = self.record(p, name)
            self.assertEqual(r["decision"], "keep")
            self.assertTrue(r["protected"])
        self.assertEqual(self.record(p, "draft.txt")["decision"], "review")
        self.assertEqual(self.record(p, "draft.txt")["duplicates"], ["copy.txt"])
        self.assertEqual(self.record(p, "work/previews/page-01.png")["decision"], "candidate")
        self.assertEqual(self.record(core.make_plan(self.root, "full"), "work/script.py")["decision"], "keep")

    def test_referenced_preview_is_kept_even_if_not_final(self):
        self.write("notes.md", "keep work/previews/page-01.png")
        self.assertTrue(self.record(self.plan(), "work/previews/page-01.png")["protected"])

    def test_python_output_target_is_not_input_dependency(self):
        self.write("source/render.py", 'from pathlib import Path\n# 生成预览\nPath("work/previews/page-01.png").write_bytes(b"x")\n')
        p = self.plan()
        r = self.record(p, "work/previews/page-01.png")
        self.assertEqual(r["decision"], "candidate")
        self.assertEqual(r["generated_by"], ["source/render.py"])
        self.assertEqual(r["referenced_by"], [])
        self.write("source/reader.py", 'open("work/previews/page-01.png", "rb").read()')
        self.assertTrue(self.record(self.plan(), "work/previews/page-01.png")["protected"])

    def test_chinese_punctuation_and_markdown_paths_with_spaces(self):
        self.write("raw export.csv", "x,y")
        self.write("outputs/final.md", "数据在 ../data/table.csv，图见 ../assets/plot.svg。\n[表](../raw export.csv)")
        p = self.plan()
        refs = self.record(p, "outputs/final.md")["references"]
        self.assertIn("data/table.csv", refs)
        self.assertIn("assets/plot.svg", refs)
        self.assertIn("raw export.csv", refs)
        self.assertTrue(self.record(p, "raw export.csv")["protected"])

    def test_unknown_binary_not_promoted_by_model(self):
        self.write("unknown.blob", b"\x00\x01\x02")
        p = self.plan()
        providers.merge_advice(p, self.advice(p))
        self.assertEqual(self.record(p, "unknown.blob")["decision"], "review")
        self.assertEqual(self.record(p, "work/script.py")["decision"], "keep")
        self.assertEqual(self.record(p, "draft.txt")["decision"], "candidate")
        p = self.plan("full")
        providers.merge_advice(p, self.advice(p))
        self.assertEqual(self.record(p, "draft.txt")["decision"], "review")

    def test_missing_final_and_truncated_input_stay_uncertain(self):
        p = core.make_plan(self.root)
        self.assertTrue(any("尚未指定" in x for x in p["warnings"]))
        self.write("large.log", "a" * (512 * 1024 + 10))
        p = self.plan()
        providers.merge_advice(p, self.advice(p))
        self.assertEqual(self.record(p, "large.log")["decision"], "review")

    def test_secret_files_never_read_or_sent(self):
        self.write("sample.txt", "api_key = 'abc-private-value'\n")
        p = self.plan()
        r = self.record(p, ".env.local")
        self.assertNotIn("sha256", r)
        self.assertNotIn("excerpt", r)
        self.assertNotIn("abc-private-value", self.record(p, "sample.txt")["excerpt"])
        for provider in providers.DEFAULTS:
            settings = providers.config(None, provider, "test-model")
            manifest = providers.prepare(p, settings)
            text = json.dumps(manifest)
            self.assertNotIn("never-send-this-value", text)
            self.assertNotIn("abc-private-value", text)
            self.assertNotIn(".env.local", text)

    def test_scan_does_not_follow_symlinks(self):
        outside = Path(self.tmp.name) / "outside"
        outside.mkdir()
        (outside / "secret.txt").write_text("must-not-be-read")
        create_symlink(self.root / "external", outside, target_is_directory=True)
        create_symlink(self.root / "alias.txt", outside / "secret.txt")
        p = self.plan()
        self.assertEqual(self.record(p, "external")["kind"], "protected-directory")
        self.assertNotIn("must-not-be-read", json.dumps(p))
        with self.assertRaises(core.CloseoutError):
            core.safe_path(self.root, "external/secret.txt")

    def test_apply_and_undo_preserve_exact_bytes(self):
        p = self.plan()
        before = (self.root / "work/previews/page-01.png").read_bytes()
        journal = core.apply_plan(p, self.root, ["work/previews/page-01.png"])
        self.assertFalse((self.root / "work/previews/page-01.png").exists())
        saved = self.root / core.STATE / "runs" / journal["id"] / "files/work/previews/page-01.png"
        self.assertEqual(saved.read_bytes(), before)
        restored = core.undo(self.root, journal["id"])
        self.assertEqual(restored["status"], "restored")
        self.assertEqual((self.root / "work/previews/page-01.png").read_bytes(), before)
        core.undo(self.root, journal["id"])

    def test_modified_or_new_file_invalidates_plan_without_moves(self):
        for mutation in (lambda: self.write("draft.txt", "changed"),
                         lambda: self.write("new.txt", "new")):
            p = self.plan()
            mutation()
            with self.assertRaises(core.CloseoutError):
                core.apply_plan(p, self.root, ["work/previews/page-01.png"])
            self.assertTrue((self.root / "work/previews/page-01.png").exists())

    def test_apply_rejects_protected_unknown_and_escape(self):
        p = self.plan()
        for selected in ("outputs/final.md", "work/script.py", ".env.local", "../outside", "missing"):
            with self.assertRaises(core.CloseoutError):
                core.apply_plan(p, self.root, [selected])
        bad = copy.deepcopy(p)
        bad["records"][0]["path"] = "../escape"
        with self.assertRaises(core.CloseoutError):
            core.validate_plan(bad, self.root)
        for relative in ("/etc/passwd", "../x", "a/../../x", "a\\b"):
            with self.assertRaises(core.CloseoutError):
                core.safe_path(self.root, relative)

    def test_undo_conflict_does_not_overwrite(self):
        journal = core.apply_plan(self.plan(), self.root, ["work/previews/page-01.png"])
        self.write("work/previews/page-01.png", b"new-important-content")
        with self.assertRaises(core.CloseoutError):
            core.undo(self.root, journal["id"])
        self.assertEqual((self.root / "work/previews/page-01.png").read_bytes(), b"new-important-content")

    def test_symlinked_state_refused(self):
        create_symlink(self.root / core.STATE, Path(self.tmp.name), target_is_directory=True)
        with self.assertRaises(core.CloseoutError):
            core.apply_plan(self.plan(), self.root, ["work/previews/page-01.png"])

    def test_interrupted_run_is_recoverable(self):
        original_move = core.move_no_replace
        calls = []
        def interrupted(src, dst):
            calls.append(src)
            if len(calls) == 2:
                raise OSError("simulated interruption")
            original_move(src, dst)
        with patch.object(core, "move_no_replace", interrupted):
            with self.assertRaises(OSError):
                core.apply_plan(self.plan(), self.root,
                                ["work/previews/page-01.png", "work/previews/page-02.png"])
        journal_path = next((self.root / core.STATE / "runs").glob("*/journal.json"))
        journal = core.read_json(journal_path)
        self.assertEqual(journal["status"], "interrupted")
        core.undo(self.root, journal["id"])
        self.assertTrue((self.root / "work/previews/page-01.png").exists())
        self.assertTrue((self.root / "work/previews/page-02.png").exists())

    def test_pending_journal_handles_rename_before_status_write(self):
        real_write = core.atomic_json
        def crash(path, data):
            if data.get("status") == "applying" and any(e["status"] == "moved" for e in data["entries"]):
                raise OSError("simulated journal interruption")
            real_write(path, data)
        with patch.object(core, "atomic_json", crash):
            with self.assertRaises(OSError):
                core.apply_plan(self.plan(), self.root, ["work/previews/page-01.png"])
        journal_path = next((self.root / core.STATE / "runs").glob("*/journal.json"))
        core.undo(self.root, core.read_json(journal_path)["id"])
        self.assertTrue((self.root / "work/previews/page-01.png").exists())

    def test_interruption_between_link_and_unlink_recovers(self):
        real_unlink = os.unlink
        def fail_original(path, *args, **kwargs):
            if Path(path) == self.root / "work/previews/page-01.png":
                raise OSError("simulated unlink interruption")
            return real_unlink(path, *args, **kwargs)
        with patch.object(core.os, "unlink", fail_original):
            with self.assertRaises(OSError):
                core.apply_plan(self.plan(), self.root, ["work/previews/page-01.png"])
        journal_path = next((self.root / core.STATE / "runs").glob("*/journal.json"))
        journal = core.read_json(journal_path)
        core.undo(self.root, journal["id"])
        self.assertEqual((self.root / "work/previews/page-01.png").read_bytes(), b"render-preview")

    def test_destination_created_during_move_is_never_overwritten(self):
        real_link = os.link
        def race(src, dst, *args, **kwargs):
            Path(dst).write_bytes(b"new-important-destination")
            return real_link(src, dst, *args, **kwargs)
        with patch.object(core.os, "link", race):
            with self.assertRaises(FileExistsError):
                core.apply_plan(self.plan(), self.root, ["work/previews/page-01.png"])
        self.assertTrue((self.root / "work/previews/page-01.png").exists())
        saved = next((self.root / core.STATE / "runs").glob("*/files/work/previews/page-01.png"))
        self.assertEqual(saved.read_bytes(), b"new-important-destination")

    def test_cli_plan_payload_analyze_preview_gate_and_review(self):
        out = io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(out):
            self.assertEqual(main(["plan", str(self.root), "--final", "outputs/final.md"]), 0)
            plan_path = next((self.root / core.STATE / "plans").glob("*.json"))
            args = ["--plan", str(plan_path), "--root", str(self.root),
                    "--provider", "deepseek", "--model", "test-model"]
            with patch.object(providers, "call") as mock_call:
                self.assertEqual(main(["payload"] + args), 0)
                self.assertEqual(main(["analyze"] + args + ["--send", "--payload-sha256", "wrong"]), 2)
                mock_call.assert_not_called()
            p = core.read_json(plan_path)
            advice_path = self.root / core.STATE / "advice.json"
            core.atomic_json(advice_path, {"decisions": self.advice(p, "keep")})
            self.assertEqual(main(["review", "--root", str(self.root), "--plan", str(plan_path),
                                   "--advice", str(advice_path)]), 0)

    def test_model_batch_cache_and_invalid_schema(self):
        p = self.plan()
        settings = providers.config(None, "deepseek", "test-model")
        manifest = providers.prepare(p, settings, 3)
        def fake_call(settings, spec, key):
            body = json.loads(spec["body"]["messages"][1]["content"])
            return json.dumps({"decisions": [{"path": r["path"], "action": "keep",
                                             "confidence": .95, "reason": "保留"} for r in body["files"]]})
        with patch.object(providers, "call", side_effect=fake_call) as mock_call:
            providers.analyze(p, settings, manifest, "not-a-real-key")
            self.assertEqual(mock_call.call_count, manifest["batch_count"])
            mock_call.reset_mock()
            providers.analyze(self.plan(), settings, manifest, "not-a-real-key")
            mock_call.assert_not_called()
        for data in ({"decisions": []},
                     {"decisions": [{"path": "../x", "action": "candidate", "confidence": .99, "reason": "x"}]},
                     {"decisions": [{"path": "draft.txt", "action": "candidate", "confidence": True, "reason": "x"}]},
                     {"decisions": [{"path": "draft.txt", "action": "delete", "confidence": .99, "reason": "x"}]}):
            with self.assertRaises(core.CloseoutError):
                providers.parse_advice(json.dumps(data), {"draft.txt"})

    def test_wizard_offline_and_cancel_send(self):
        with contextlib.redirect_stdout(io.StringIO()), patch("builtins.input", side_effect=[
                "1", "2", "outputs/final.md", "", "n"]), patch.object(providers, "call") as call:
            self.assertEqual(main(["wizard", "--projects-root", str(self.root)]), 0)
            call.assert_not_called()
        with contextlib.redirect_stdout(io.StringIO()), patch("builtins.input", side_effect=[
                "1", "2", "outputs/final.md", "", "y", "4", "test-model", "n"]), \
                patch.dict(os.environ, {}, clear=True), patch("getpass.getpass", return_value="not-a-real-key"), \
                patch.object(providers, "call") as call:
            self.assertEqual(main(["wizard", "--projects-root", str(self.root)]), 0)
            call.assert_not_called()
        state_text = "".join(p.read_text() for p in (self.root / core.STATE).rglob("*.json"))
        self.assertNotIn("not-a-real-key", state_text)

    def test_unicode_text_truncation_keeps_readable_prefix(self):
        self.write("large-cn.txt", "中" * 180000)
        p = self.plan()
        r = self.record(p, "large-cn.txt")
        self.assertTrue(r["truncated"])
        self.assertEqual(r["extraction"], "text")
        self.assertTrue(r["excerpt"].startswith("中"))

    def test_repeated_scan_does_not_accumulate_plan_reports(self):
        with contextlib.redirect_stdout(io.StringIO()):
            for _ in range(3):
                self.assertEqual(main(["plan", str(self.root)]), 0)
        files = list((self.root / core.STATE / "plans").iterdir())
        self.assertEqual(len(files), 2)


class ProviderTest(unittest.TestCase):
    def test_four_http_protocols_and_responses(self):
        responses = {
            "deepseek": {"choices": [{"finish_reason": "stop", "message": {"content": "{}"}}]},
            "openai": {"status": "completed", "output": [{"type": "message", "content": [{"type": "output_text", "text": "{}"}]}]},
            "anthropic": {"stop_reason": "end_turn", "content": [{"type": "text", "text": "{}"}]},
            "gemini": {"candidates": [{"finishReason": "STOP", "content": {"parts": [{"text": "{}"}]}}]},
        }
        for provider in providers.DEFAULTS:
            settings = providers.config(None, provider, "test-model")
            spec = providers.request_spec(settings, "file data")
            captured = []
            class Reply:
                def __enter__(self): return self
                def __exit__(self, *args): pass
                def read(self, limit): return json.dumps(responses[provider]).encode()
            class Opener:
                def open(self, request, timeout):
                    captured.append(request)
                    return Reply()
            with patch.object(providers.urllib.request, "build_opener", return_value=Opener()):
                self.assertEqual(providers.call(settings, spec, "fake-key"), "{}")
            request = captured[0]
            header = "X-goog-api-key" if provider == "gemini" else "X-api-key" if provider == "anthropic" else "Authorization"
            self.assertIn("fake-key", request.get_header(header))
            self.assertNotIn("fake-key", request.full_url)
            self.assertNotIn("fake-key", json.dumps(spec))
            if provider == "openai":
                self.assertFalse(spec["body"]["store"])

    def test_errors_do_not_echo_secrets_and_no_redirect(self):
        settings = providers.config(None, "deepseek", "test-model")
        error = urllib.error.HTTPError("https://host", 401, "fake-secret-key", {}, io.BytesIO(b"secret-response"))
        class Opener:
            def open(self, request, timeout): raise error
        with patch.object(providers.urllib.request, "build_opener", return_value=Opener()):
            with self.assertRaises(core.CloseoutError) as ctx:
                providers.call(settings, providers.request_spec(settings, "x"), "fake-secret-key")
        self.assertNotIn("secret", str(ctx.exception))
        self.assertIsNone(providers.NoRedirect().redirect_request(None, None, 302, "", {}, "https://elsewhere"))

    def test_key_file_is_not_executed_and_missing_configuration_is_clear(self):
        settings = providers.config(None, "gemini", "test-model")
        with tempfile.TemporaryDirectory() as folder, patch.dict(os.environ, {}, clear=True):
            path = Path(folder) / ".env.local"
            path.write_text("GEMINI_API_KEY='literal-$(never-execute)'\n")
            self.assertEqual(providers.load_key(settings, path), "literal-$(never-execute)")
            with self.assertRaises(core.CloseoutError): providers.load_key(settings)
        with self.assertRaises(core.CloseoutError): providers.config(None, "gemini")


if __name__ == "__main__":
    unittest.main()
