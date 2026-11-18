import json
import os
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "skills/project-closeout/scripts"))
from project_closeout import core, providers
from project_closeout.preferences import Preferences
from project_closeout.profiles import build_profiles
from project_closeout.webapp import App


class MemoryVault:
    values = {}
    def exists(self, provider): return provider in self.values
    def get(self, provider): return self.values.get(provider, "")
    def put(self, provider, key): self.values[provider] = key


class SimplifiedFlowTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.base = Path(self.temp.name).resolve()
        self.root = self.base / "项目"
        files = {"outputs/report.md": "Final report", "source/edit.py": 'from pathlib import Path\nPath("work/previews/page-01.png").write_bytes(b"preview")\nPath("work/previews/page-02.png").write_bytes(b"preview-2")',
                 "work/previews/page-01.png": "generated-preview", "work/previews/page-02.png": "preview-2",
                 "work/old-process.txt": "Discardable general notes", "uncertain.bin": "unknown",
                 ".DS_Store": "system-cache"}
        for rel, content in files.items():
            path = self.root / rel; path.parent.mkdir(parents=True, exist_ok=True); path.write_text(content)
        self.settings = self.base / "analysis-settings.local.json"
        self.prefs = Preferences(self.settings, MemoryVault)
        self.catalog = self.base / "catalog.json"
        self.catalog.write_text(json.dumps({"schema":1,"projects":[{"path":str(self.root),"label":"我的项目"}],"threads":[]}))
        # Model the system Trash without modifying the developer's actual Trash.
        self.fake_trash = self.base / "test-trash"
        self.fake_trash.mkdir()
        for mock in (patch("project_closeout.core.trash_directory",return_value=self.fake_trash),
                     patch("project_closeout.core.system_trash",side_effect=lambda folder: folder.rename(self.fake_trash / folder.name))):
            mock.start();self.addCleanup(mock.stop)
        self.app = App(self.catalog, self.prefs)
        self.project = next(p for p in self.app.projects.values() if p["path"] == str(self.root))
        MemoryVault.values.clear()

    def tearDown(self): self.temp.cleanup()

    def wait(self, key):
        end = time.monotonic()+5
        while self.app.jobs[key]["status"] == "running" and time.monotonic() < end: time.sleep(.01)
        result = self.app.public_job(key)
        self.assertEqual(result["status"], "done", result)
        return result

    def start(self):
        return self.app.action("/api/start", {"project": self.project["id"]})["job"]

    def test_select_project_alone_produces_three_plans_without_moving_files(self):
        before={p.relative_to(self.root).as_posix():core.sha_file(p) for p in self.root.rglob("*") if p.is_file()}
        with patch("project_closeout.webapp.make_plan", wraps=core.make_plan) as make, patch.object(providers,"call") as call:
            job=self.start(); result=self.wait(job)
            self.assertEqual(make.call_count,1)
            call.assert_not_called()
        self.assertEqual([p["tier"] for p in result["profiles"]],["results","milestones","clear"])
        self.assertEqual(result["mode"],"local")
        self.assertNotIn("records_by_tier",result)
        self.assertEqual(before,{rel:core.sha_file(self.root/rel) for rel in before})
        plans=self.app.jobs[job]["plans"]
        moves={t:{r["path"] for r in p["records"] if r["decision"]=="candidate"} for t,p in plans.items()}
        self.assertLess(moves["milestones"],moves["results"])
        self.assertLess(moves["results"],moves["clear"])
        self.assertIn("uncertain.bin",moves["results"])
        self.assertNotIn("source/edit.py",moves["milestones"])
        self.assertIn("outputs/report.md",moves["clear"])

    def test_apply_whole_profile_with_optional_extra_keep_and_undo(self):
        job=self.start();self.wait(job)
        response=self.app.action("/api/apply",{"job":job,"tier":"milestones","keep_extra":["work/previews/page-01.png"]})
        self.assertTrue((self.root/"work/previews/page-01.png").exists())
        self.assertFalse((self.root/"work/previews/page-02.png").exists())
        self.app.action("/api/undo",{"run":response["run"]})
        self.assertEqual((self.root/"work/previews/page-02.png").read_text(),"preview-2")

    def test_preview_can_be_promoted_to_a_result(self):
        (self.root/"work/previews/page-original.png").write_text("possibly handmade work")
        job=self.start();self.wait(job)
        self.app.action("/api/task",{"job":job,"path":"work/previews/page-original.png","role":"result"})
        for tier,decision in (("results","keep"),("milestones","keep"),("clear","candidate")):
            record=next(r for r in self.app.review(job,tier)["records"] if r["path"]=="work/previews/page-original.png")
            self.assertEqual(record["decision"],decision)

    def test_shared_model_analysis_generates_evidence_based_stronger_profile(self):
        plan=core.make_plan(self.root)
        advice=[]
        for r in plan["records"]:
            if r["kind"]=="file":
                advice.append({"path":r["path"],"action":"candidate" if r["path"]=="work/old-process.txt" else "review",
                               "retention":"optional" if r["path"]=="work/old-process.txt" else "unknown",
                               "confidence":.96,"reason":"内容表明该笔记已被成果替代"})
        providers.merge_advice(plan,advice)
        plans, summaries=build_profiles(plan)
        def decision(tier,path): return next(r["decision"] for r in plans[tier]["records"] if r["path"]==path)
        self.assertEqual(decision("milestones","work/old-process.txt"),"candidate")
        self.assertEqual(decision("results","work/old-process.txt"),"candidate")
        self.assertEqual(decision("results","uncertain.bin"),"candidate")

    def test_settings_store_no_plaintext_key_and_reuse_vault_after_restart(self):
        self.prefs.save({"enabled":True,"provider":"deepseek","model":"test-model","key":"fake-secret-value","remember":True})
        self.assertNotIn("fake-secret-value",self.settings.read_text())
        public=self.prefs.public()
        self.assertTrue(public["has_key"])
        self.assertNotIn("fake-secret-value",json.dumps(public))
        with patch.dict(os.environ,{},clear=True):
            new=Preferences(self.settings,MemoryVault)
            self.assertEqual(new.connection()[1],"fake-secret-value")

    def test_session_key_is_not_written_to_disk(self):
        self.prefs.save({"enabled":True,"provider":"gemini","model":"test-model","key":"fake-session-key","remember":False})
        self.assertEqual(MemoryVault.values,{})
        self.assertNotIn("fake-session-key",self.settings.read_text())
        with patch.dict(os.environ,{},clear=True):
            self.assertEqual(self.prefs.connection()[1],"fake-session-key")
            with self.assertRaises(core.CloseoutError): Preferences(self.settings,MemoryVault).connection()

    def test_analysis_evidence_drives_roles_and_explanations_across_profiles(self):
        for name,text in [("notes.txt","此文为实际交付给评审的说明。"),("final-old.txt","本次试稿已经撤回。"),("final-unverified.txt","记录尚不完整。")]:
            (self.root/name).write_text(text)
        self.prefs.save({"enabled":True,"provider":"deepseek","model":"test-model","key":"fake-key","remember":False})
        def fake(settings,spec,key):
            payload=json.loads(spec["body"]["messages"][1]["content"])
            self.assertEqual(len(payload["strategies"]),3)
            decisions=[]
            for r in payload["files"]:
                role,quote={"notes.txt":("result","实际交付给评审"),"final-old.txt":("process","试稿已经撤回"),
                    "final-unverified.txt":("result","已经验收通过")}.get(r["path"],("unknown",""))
                decisions.append(dict(path=r["path"],role=role,evidence=quote,reason="根据提供片段判断",action="review" if role=="unknown" else "candidate" if role=="process" else "keep",confidence=.98))
            return json.dumps({"decisions":decisions})
        with patch.object(providers,"call",side_effect=fake) as call:
            job=self.start();result=self.wait(job);calls=call.call_count
            view={r["path"]:r for r in self.app.review(job,"results")["records"]}
            self.assertEqual(view["notes.txt"]["decision"],"keep")
            self.assertIn("实际交付给评审",view["notes.txt"]["reason"])
            self.assertEqual(view["final-old.txt"]["role"],"process")
            self.assertEqual(view["final-unverified.txt"]["role"],"unknown")
            self.assertNotIn("已经验收通过",view["final-unverified.txt"]["reason"])
            p=next(p for p in result["profiles"] if p["tier"]=="clear")
            self.assertFalse(p["explanation"]["keep"])
            self.assertGreater(p["explanation"]["uncertain"],0)
            self.assertIn("notes.txt",[r["path"] for r in p["explanation"]["move"]])
            self.app.action("/api/task",{"job":job,"path":"notes.txt","role":"milestone"})
            self.assertEqual(call.call_count,calls)

    def test_auto_api_uses_saved_choice_and_switching_profiles_does_not_recall(self):
        self.prefs.save({"enabled":True,"provider":"deepseek","model":"test-model","key":"fake-key","remember":False})
        def fake(settings,spec,key):
            files=json.loads(spec["body"]["messages"][1]["content"])["files"]
            return json.dumps({"decisions":[{"path":r["path"],"action":"keep","confidence":.99,
                                            "retention":"important","reason":"有价值的项目文件"} for r in files]})
        with patch.object(providers,"call",side_effect=fake) as call:
            job=self.start();result=self.wait(job)
            self.assertEqual(result["mode"],"ai")
            self.assertEqual(call.call_count,1)
            for tier in ("results","milestones","clear"):
                self.app.review(job,tier)
            self.assertEqual(call.call_count,1)
        self.assertNotIn("fake-key",json.dumps(self.app.public_job(job)))

    def test_api_failure_returns_local_suggestions_without_repeated_questions(self):
        self.prefs.save({"enabled":True,"provider":"deepseek","model":"test-model","key":"fake-key","remember":False})
        with patch.object(providers,"call",side_effect=core.CloseoutError("HTTP 429")):
            job=self.start();result=self.wait(job)
        self.assertEqual(result["mode"],"local")
        self.assertIn("未完成",result["mode_message"])
        self.assertEqual(len(result["profiles"]),3)
        self.assertEqual(result["analysis"]["status"],"failed")
        self.assertEqual(result["analysis"]["ai_file_count"],0)
        self.assertTrue(all(not p["recommended"] for p in result["profiles"]))

    def test_1704_readable_files_fit_auto_batch_limit_without_omission(self):
        plan=core.make_plan(self.root)
        template=next(r for r in plan["records"] if r["path"]=="work/old-process.txt")
        plan["records"]=[dict(template,path=f"notes/n-{i}.txt") for i in range(1704)]
        settings=providers.config(None,"deepseek","test-model")
        with self.assertRaises(core.CloseoutError): providers.prepare(plan,settings)
        manifest=providers.prepare(plan,settings,reviewable_only=True,adaptive=True)
        paths=[path for batch in manifest["batches"] for path in batch["paths"]]
        self.assertEqual(len(paths),1704)
        self.assertEqual(len(set(paths)),1704)
        self.assertLessEqual(manifest["batch_count"],100)
        self.assertGreater(manifest["batch_size"],12)
        self.assertTrue(all(len(json.dumps(b["request"],ensure_ascii=False).encode())<=160000 for b in manifest["batches"]))

    def test_auto_analysis_sends_readable_uncertain_files_only(self):
        plan=core.make_plan(self.root)
        settings=providers.config(None,"deepseek","test-model")
        manifest=providers.prepare(plan,settings,reviewable_only=True,adaptive=True)
        paths={path for batch in manifest["batches"] for path in batch["paths"]}
        self.assertEqual(paths,{"outputs/report.md","work/old-process.txt"})
        self.assertEqual(manifest["file_count"],2)
        self.assertNotIn(".DS_Store",paths)
        self.assertNotIn("source/edit.py",paths)
        self.assertNotIn("uncertain.bin",paths)
        self.assertNotIn("work/previews/page-01.png",paths)

    def test_no_readable_uncertain_files_does_not_claim_ai_completion(self):
        self.prefs.save({"enabled":True,"provider":"deepseek","model":"test-model","key":"fake-key","remember":False})
        plan=core.make_plan(self.root)
        for r in plan["records"]:
            if r["extraction"]=="text":r["protected"]=True
        with patch("project_closeout.webapp.make_plan",return_value=plan),patch.object(providers,"call") as call:
            result=self.wait(self.start())
            call.assert_not_called()
        self.assertEqual(result["mode"],"local")
        self.assertEqual(result["analysis"]["status"],"not-needed")

    def test_retry_reuses_successful_batches_without_partial_success(self):
        for i in range(24): (self.root/f"note-{i:02}.txt").write_text("Project notes")
        self.prefs.save({"enabled":True,"provider":"deepseek","model":"test-model","key":"fake-key","remember":False})
        requested=[]
        fail=True
        def fake(settings,spec,key):
            files=json.loads(spec["body"]["messages"][1]["content"])["files"]
            requested.append(tuple(r["path"] for r in files))
            if fail and len(requested)==2: raise core.CloseoutError("HTTP 429")
            return json.dumps({"decisions":[{"path":r["path"],"action":"keep","confidence":.99,
                                            "retention":"important","reason":"有价值的项目内容"} for r in files]})
        with patch.object(providers,"call",side_effect=fake):
            first=self.wait(self.start())
            self.assertEqual(first["analysis"]["status"],"failed")
            self.assertEqual(first["analysis"]["completed_batches"],1)
            self.assertEqual(first["analysis"]["ai_file_count"],0)
            first_batch=requested[0]
            fail=False
            second=self.wait(self.start())
        self.assertEqual(second["analysis"]["status"],"complete")
        self.assertEqual(second["analysis"]["completed_batches"],3)
        self.assertEqual(requested.count(first_batch),1)

    def test_scoped_clear_keeps_other_tasks_dependencies_and_restores(self):
        for rel,content in [("A/final.pdf","delivery"),("A/reusable.svg","vector"),("A/.env","private"),("B/manual.md","../A/reusable.svg")]:
            f=self.root/rel;f.parent.mkdir(exist_ok=True);f.write_text(content)
        (self.root/"A/link").symlink_to(self.root/"uncertain.bin")
        job=self.start();self.wait(job)
        d=self.app.action("/api/task",{"job":job,"scope":"A"})
        req={"job":job,"tier":"clear","version":d["version"]}
        with self.assertRaises(core.CloseoutError):self.app.action("/api/apply",req)
        receipt=self.app.action("/api/apply",dict(req,confirm_clear=True))
        self.assertEqual(receipt["destination"],"trash")
        bucket=self.fake_trash/core.trash_label(self.root,receipt["run"])
        self.assertEqual((bucket/"A/final.pdf").read_text(),"delivery")
        self.assertFalse((self.root/"A/final.pdf").exists())
        for rel in ("A/reusable.svg","A/.env","A/link","B/manual.md","uncertain.bin"): self.assertTrue((self.root/rel).exists())
        self.app.action("/api/undo",{"run":receipt["run"]})
        self.assertEqual((self.root/"A/final.pdf").read_text(),"delivery")

    def test_denied_trash_leaves_recoverable_batch(self):
        job=self.start();self.wait(job)
        with patch("project_closeout.core.system_trash",side_effect=core.CloseoutError("denied")):
            receipt=self.app.action("/api/apply",{"job":job,"tier":"milestones"})
        self.assertEqual(receipt["destination"],"unconfirmed")
        stage=self.root/core.STATE/"runs"/receipt["run"]/"files"
        self.assertTrue((stage/"work/previews/page-02.png").is_file())
        self.app.action("/api/undo",{"run":receipt["run"]})
        self.assertEqual((self.root/"work/previews/page-02.png").read_text(),"preview-2")

    def test_manual_final_retains_transitive_dependencies_not_unrelated_old_sources(self):
        for rel,content in [("selected.md","middle.txt"),("middle.txt","asset.svg"),("asset.svg","data"),("old.psd","old")]:
            (self.root/rel).write_text(content)
        job=self.start();self.wait(job)
        d=self.app.action("/api/task",{"job":job,"path":"selected.md","role":"result"})
        with self.assertRaises(core.CloseoutError):self.app.action("/api/apply",{"job":job,"tier":"results","version":0})
        receipt=self.app.action("/api/apply",{"job":job,"tier":"results","version":d["version"]})
        for n in ("selected.md","middle.txt","asset.svg"):self.assertTrue((self.root/n).exists())
        self.assertFalse((self.root/"old.psd").exists())
        self.app.action("/api/undo",{"run":receipt["run"]})
        self.assertEqual((self.root/"old.psd").read_text(),"old")

    def test_native_editable_formats_remain_protected(self):
        for path in ("edit.pcraft","edit.vectorcraft","View.swift"):
            (self.root/path).write_text("Editable project source")
        plan=core.make_plan(self.root,text_limit=64*1024)
        for path in ("edit.pcraft","edit.vectorcraft","View.swift"):
            record=next(r for r in plan["records"] if r["path"]==path)
            self.assertTrue(record["protected"])

    def test_auto_extraction_budget_does_not_starve_later_text(self):
        for i in range(66):
            (self.root/f"a-{i:03}.log").write_text("x"*(512*1024))
        (self.root/"z-later.txt").write_text("Later useful project text")
        plan=core.make_plan(self.root,text_limit=64*1024)
        later=next(r for r in plan["records"] if r["path"]=="z-later.txt")
        self.assertEqual(later["extraction"],"text")
        self.assertIn("Later useful",later["excerpt"])
        self.assertFalse(any(r.get("extraction")=="budget-skipped" for r in plan["records"]))


if __name__=="__main__": unittest.main()
