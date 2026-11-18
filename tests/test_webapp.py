import copy
import json
import sys
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "skills/project-closeout/scripts"))
from project_closeout.catalog import project_catalog
from project_closeout.webapp import App, handler_for
from project_closeout.preferences import Preferences
from project_closeout.core import CloseoutError


class LocalUITest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.base = Path(self.tmp.name).resolve()
        self.root = self.base / "human project"
        (self.root / "outputs").mkdir(parents=True)
        (self.root / "work/previews").mkdir(parents=True)
        (self.root / "outputs/final.md").write_text("Final report")
        (self.root / "work/previews/page-01.png").write_bytes(b"preview")
        self.catalog = self.base / "index.json"
        self.catalog.write_text(json.dumps({"schema":1,"projects":[{"path":str(self.root),"label":"真正的项目名"}],
                                           "threads":[{"cwd":str(self.root),"title":"相关聊天原名","id":"test","updatedAt":100}]}))
        # Model the system Trash without modifying the developer's actual Trash.
        self.fake_trash = self.base / "test-trash"
        self.fake_trash.mkdir()
        for mock in (patch("project_closeout.core.trash_directory",return_value=self.fake_trash),
                     patch("project_closeout.core.system_trash",side_effect=lambda folder: folder.rename(self.fake_trash / folder.name))):
            mock.start();self.addCleanup(mock.stop)
        self.app = App(self.catalog, Preferences(self.base / "analysis-settings.local.json"))
        self.project = next(p for p in self.app.projects.values() if p["path"] == str(self.root))

    def tearDown(self):
        self.tmp.cleanup()

    def wait(self, job):
        deadline = time.monotonic() + 5
        while self.app.jobs[job]["status"] == "running" and time.monotonic() < deadline:
            time.sleep(.01)
        return self.app.public_job(job)

    def test_real_project_name_overrides_internal_slug_and_includes_outside_roots(self):
        p = self.project
        self.assertEqual(p["display_name"], "真正的项目名")
        self.assertEqual(p["display_kind"], "project")
        self.assertEqual(p["threads"][0]["title"], "相关聊天原名")
        self.assertFalse((self.root / ".project-closeout").exists())

    def test_chat_names_are_exact_and_unknown_folders_separate(self):
        chat = self.base / "generated" / "2026-11-17" / "ab-3"
        (chat / "outputs").mkdir(parents=True)
        (chat / "work").mkdir()
        other = self.base / "generated" / "2026-11-17" / "unknown"
        other.mkdir()
        self.catalog.write_text(json.dumps({"schema":1,"projects":[],"threads":[
            {"cwd":str(chat),"title":"用户设置的原始聊天标题","id":"thread","updatedAt":1}]}))
        result = project_catalog([self.base / "generated"], self.catalog)
        known = next(p for p in result["projects"] if p["path"] == str(chat))
        unknown = next(p for p in result["projects"] if p["path"] == str(other))
        self.assertEqual(known["display_name"], "用户设置的原始聊天标题")
        self.assertEqual(known["display_kind"], "chat")
        self.assertEqual(unknown["display_kind"], "folder")

    def test_scan_select_isolate_undo_without_api(self):
        finals = self.app.details(self.project["id"])["finals"]
        self.assertIn({"label":"outputs/final.md","path":"outputs/final.md"}, finals)
        with patch("project_closeout.providers.call") as api_call:
            job = self.app.action("/api/scan",{"project":self.project["id"],"tier":"balanced","finals":["outputs/final.md"]})["job"]
            result = self.wait(job)
            self.assertEqual(result["status"], "done")
            self.assertEqual(result["summary"]["candidate"], 1)
            self.assertNotIn("excerpt", json.dumps(result))
            moved = self.app.action("/api/apply",{"job":job,"selected":["work/previews/page-01.png"]})
            self.assertFalse((self.root / "work/previews/page-01.png").exists())
            self.app.action("/api/undo",{"run":moved["run"]})
            self.assertEqual((self.root / "work/previews/page-01.png").read_bytes(), b"preview")
            api_call.assert_not_called()

    def test_http_authorization_origin_and_static_route_confinement(self):
        server = ThreadingHTTPServer(("127.0.0.1",0),handler_for(self.app))
        thread = threading.Thread(target=server.serve_forever,daemon=True)
        thread.start()
        url = "http://127.0.0.1:"+str(server.server_port)
        try:
            with urllib.request.urlopen(url+"/") as response:
                self.assertIn('id="picker"',response.read().decode())
                self.assertIn("frame-ancestors 'none'",response.headers["Content-Security-Policy"])
            with self.assertRaises(urllib.error.HTTPError) as failure:
                urllib.request.urlopen(url+"/api/projects")
            self.assertEqual(failure.exception.code,403)
            with self.assertRaises(urllib.error.HTTPError) as failure:
                urllib.request.urlopen(url+"/api/preview/"+self.project["id"])
            self.assertEqual(failure.exception.code,403)
            req = urllib.request.Request(url+"/api/projects",headers={"X-Closeout-Token":self.app.token})
            with urllib.request.urlopen(req) as response:
                self.assertIn("真正的项目名",response.read().decode())
            req = urllib.request.Request(url+"/api/refresh",data=b"{}",headers={
                "X-Closeout-Token":self.app.token,"Origin":"https://unrelated.example"})
            with self.assertRaises(urllib.error.HTTPError) as failure:
                urllib.request.urlopen(req)
            self.assertEqual(failure.exception.code,403)
            with self.assertRaises(urllib.error.HTTPError):
                urllib.request.urlopen(url+"/../../index.json")
        finally:
            server.shutdown(); server.server_close(); thread.join(timeout=2)

    def test_local_image_preview_ignores_private_invalid_and_linked_files(self):
        import base64
        png = base64.b64decode('iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+j5u0AAAAASUVORK5CYII=')
        private = self.root / 'outputs/secret.png'
        private.write_bytes(png)
        outside = self.base / 'outside.png'
        outside.write_bytes(png)
        (self.root / 'outputs/linked.png').symlink_to(outside)
        (self.root / 'outputs/invalid.png').write_bytes(b'not an image')
        self.assertEqual(self.app.preview(self.project['id'])[0], b'')
        image = self.root / 'outputs/drawing.png'
        image.write_bytes(png)
        self.assertEqual(self.app.preview(self.project['id']), (png, 'image/png'))
        with image.open('wb') as source:
            source.write(png[:8]); source.truncate(8 * 1024 * 1024 + 1)
        self.assertEqual(self.app.preview(self.project['id'])[0], b'')
        with self.assertRaises(CloseoutError):
            self.app.preview('../../outside.png')

    def test_preview_does_not_follow_output_directory_link(self):
        import shutil
        shutil.rmtree(self.root / 'outputs')
        other = self.base / 'other'
        other.mkdir()
        (other / 'private.png').write_bytes(b'\x89PNG\r\n\x1a\n')
        (self.root / 'outputs').symlink_to(other, target_is_directory=True)
        self.assertEqual(self.app.preview(self.project['id'])[0], b'')


if __name__ == "__main__":
    unittest.main()
