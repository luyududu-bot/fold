import contextlib
import io
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from symlink_fixture import create_symlink

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "skills/project-closeout/scripts"))
from project_closeout.discovery import choose_project, discover_projects
from project_closeout.cli import main
from project_closeout.catalog import project_catalog


class DiscoveryTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name).resolve()
        self.workspace = self.root / "Codex"
        self.project = self.workspace / "2026-11-16" / "论文 project"
        (self.project / "work" / "nested-project" / ".git").mkdir(parents=True)
        (self.project / "outputs").mkdir()
        (self.project / "outputs" / "论文终稿.pdf").write_bytes(b"content-must-not-be-read")
        self.second = self.workspace / "2026-11-17" / "设计项目"
        (self.second / "outputs").mkdir(parents=True)
        (self.second / "work").mkdir()

    def tearDown(self):
        self.temp.cleanup()

    def test_date_projects_are_boundaries_not_output_subprojects(self):
        before = sorted(str(p) for p in self.workspace.rglob("*"))
        # Discovery must not read file contents (including files named manifests).
        with patch.object(Path, "read_text", side_effect=AssertionError("content read")), \
             patch.object(Path, "read_bytes", side_effect=AssertionError("content read")):
            result = discover_projects([self.workspace])
        self.assertEqual([p["path"] for p in result["projects"]], [str(self.second), str(self.project)])
        self.assertIn("论文终稿.pdf", result["projects"][1]["output_examples"])
        self.assertEqual(before, sorted(str(p) for p in self.workspace.rglob("*")))

    def test_non_codex_projects_and_excluded_folders(self):
        repo = self.workspace / "repositories" / "example"
        repo.mkdir(parents=True)
        (repo / "package.json").write_text("{}")
        assets = self.workspace / "assets" / "not-a-project"
        assets.mkdir(parents=True)
        (assets / "pyproject.toml").write_text("")
        empty = self.workspace / "2026-11-17" / "new-empty-project"
        empty.mkdir()
        result = discover_projects([self.workspace])
        paths = {p["path"] for p in result["projects"]}
        self.assertIn(str(repo), paths)
        self.assertIn(str(empty), paths)
        self.assertNotIn(str(assets), paths)

    def test_symlinks_skipped_and_overlapping_roots_deduplicated(self):
        external = self.root / "outside"
        (external / ".git").mkdir(parents=True)
        create_symlink(self.workspace / "external-link", external, target_is_directory=True)
        result = discover_projects([self.workspace, self.project])
        self.assertEqual(len(result["projects"]), 2)
        self.assertNotIn(str(external), {p["path"] for p in result["projects"]})

    def test_search_then_select_and_invalid_number_retry(self):
        out = io.StringIO()
        with contextlib.redirect_stdout(out), patch("builtins.input", side_effect=["999", "/论文", "1"]):
            selected = choose_project([self.workspace])
        self.assertEqual(selected, self.project)
        self.assertIn("成果目录：论文终稿.pdf", out.getvalue())
        self.assertIn("编号不在列表中", out.getvalue())

    def test_pagination_has_stable_global_numbers(self):
        for i in range(12):
            (self.workspace / "2026-11-15" / ("item-" + str(i))).mkdir(parents=True)
        expected = project_catalog([self.workspace])["projects"][8]["path"]
        with contextlib.redirect_stdout(io.StringIO()), patch("builtins.input", side_effect=["n", "9"]):
            selected = choose_project([self.workspace])
        self.assertEqual(str(selected), expected)

    def test_manual_dragged_path_and_quit(self):
        escaped = str(self.project).replace(" ", "\\ ")
        with contextlib.redirect_stdout(io.StringIO()), patch("builtins.input", side_effect=["0", escaped]):
            self.assertEqual(choose_project([self.workspace]), self.project)
        with contextlib.redirect_stdout(io.StringIO()), patch("builtins.input", return_value="q"):
            self.assertIsNone(choose_project([self.workspace]))

    def test_custom_search_directory_and_no_match(self):
        with contextlib.redirect_stdout(io.StringIO()), patch("builtins.input", side_effect=["f", str(self.workspace), "1"]):
            self.assertEqual(choose_project([]), self.second)
        with contextlib.redirect_stdout(io.StringIO()), patch("builtins.input", side_effect=["/不存在", "q"]):
            self.assertIsNone(choose_project([self.workspace]))

    def test_cli_listing_and_wizard_select_only_one_project(self):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            self.assertEqual(main(["projects", "--root", str(self.workspace), "--json"]), 0)
        self.assertEqual(len(json.loads(out.getvalue())["projects"]), 2)
        with contextlib.redirect_stdout(io.StringIO()), patch("builtins.input", side_effect=["1", "2", "", "", "n"]):
            self.assertEqual(main(["wizard", "--projects-root", str(self.workspace)]), 0)
        self.assertTrue((self.second / ".project-closeout/plans/latest-local.json").is_file())
        self.assertFalse((self.project / ".project-closeout").exists())


if __name__ == "__main__":
    unittest.main()
