import json
import os
import re
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
import promote_source

REPO = "owner/repo"
IMAGE = "demoacr.azurecr.io/safe-demo@sha256:" + "b" * 64


def source_run(**changes):
    value = {"id": 42, "repository": {"full_name": REPO}, "path": ".github/workflows/release.yml",
             "event": "workflow_dispatch", "head_branch": "main", "status": "completed",
             "conclusion": "failure", "head_sha": "sourcesha"}
    value.update(changes)
    return value


def job(name, conclusion="success"):
    return {"name": name, "status": "completed", "conclusion": conclusion}


def reader(run=None, jobs=None):
    jobs = [job("korea / rollout"), job("southeastasia / rollout"),
            job("westus / rollout", "failure")] if jobs is None else jobs
    calls = []

    def read(path):
        calls.append(path)
        if path == f"/repos/{REPO}/actions/runs/42":
            return run or source_run()
        if path == f"/repos/{REPO}/actions/runs/42/jobs?filter=latest&per_page=100":
            return {"total_count": len(jobs), "jobs": jobs}
        raise AssertionError(path)
    read.calls = calls
    return read


def source_release(**changes):
    value = {"image": IMAGE, "run_id": "42", "repository": REPO, "commit": "sourcesha"}
    value.update(changes)
    return value


class ResumeSourceTests(unittest.TestCase):
    def verify(self, read=None, start="usw", run_id="42"):
        return promote_source.verify_source(read or reader(), REPO, run_id, start, current_run_id="99")

    def rebind(self, release=None):
        return promote_source.rebind(release or source_release(), repository=REPO, source_run_id="42",
                                     source_sha="sourcesha", run_id="99", commit="newsha",
                                     registry="demoacr")

    def test_source_release_is_rebound_to_this_run_with_same_image(self):
        read = reader()
        self.assertEqual(self.verify(read), "sourcesha")
        self.assertIn(f"/repos/{REPO}/actions/runs/42/jobs?filter=latest&per_page=100", read.calls)
        self.assertEqual(self.rebind(), {"image": IMAGE, "run_id": "99", "repository": REPO,
                                         "commit": "newsha", "source_run_id": "42"})

    def test_source_must_be_completed_main_dispatch_of_full_release_in_this_repo(self):
        cases = {"repository": {"repository": {"full_name": "other/repo"}},
                 "release.yml": {"path": ".github/workflows/release-kr.yml"},
                 "dispatch": {"event": "push"},
                 "main": {"head_branch": "feature"},
                 "completed": {"status": "in_progress"}}
        for message, change in cases.items():
            with self.subTest(message), self.assertRaisesRegex(RuntimeError, message):
                self.verify(reader(source_run(**change)))

    def test_source_run_id_must_be_numeric_and_another_run(self):
        for run_id in ("42/../1", "", "99"):
            with self.subTest(run_id), self.assertRaises(RuntimeError):
                self.verify(run_id=run_id)

    def test_every_region_before_start_must_have_succeeded_in_latest_attempt(self):
        korea, sea = job("korea / rollout"), job("southeastasia / rollout")
        failing = {"sea": [job("korea / rollout", "failure")],
                   "usw": [korea, job("southeastasia / rollout", "cancelled")]}
        missing = {"sea": [], "usw": [korea]}
        for start in ("sea", "usw"):
            for jobs in (failing[start], missing[start], [korea, korea, sea]):
                with self.subTest(start=start, jobs=jobs), self.assertRaisesRegex(RuntimeError, "region"):
                    self.verify(reader(jobs=jobs), start=start)
        self.assertEqual(self.verify(reader(jobs=[korea]), start="sea"), "sourcesha")
        self.assertEqual(self.verify(reader(jobs=[]), start="kr"), "sourcesha")
        with self.assertRaisesRegex(RuntimeError, "region"):
            self.verify(start="eu")

    def test_incomplete_job_listing_fails_closed(self):
        read = reader()

        def truncated(path):
            value = read(path)
            return dict(value, total_count=101) if "jobs" in path else value
        with self.assertRaisesRegex(RuntimeError, "jobs"):
            self.verify(truncated)

    def test_source_record_must_be_immutable_registry_digest_from_source_run(self):
        cases = {"digest": {"image": "demoacr.azurecr.io/safe-demo:run-42"},
                 "registry": {"image": "otheracr.azurecr.io/safe-demo@sha256:" + "b" * 64},
                 "origin": {"run_id": "41"}}
        cases["commit"] = {"commit": "othersha"}
        cases["repository"] = {"repository": "other/repo"}
        for message, change in cases.items():
            with self.subTest(message), self.assertRaisesRegex(RuntimeError, message):
                self.rebind(source_release(**change))

    def test_cli_verifies_then_writes_rebound_release_record(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            (tmp / "source").mkdir()
            (tmp / "source" / "release.json").write_text(json.dumps(source_release()))
            env = {"GITHUB_REPOSITORY": REPO, "SOURCE_RUN_ID": "42", "START_REGION": "usw",
                   "GITHUB_RUN_ID": "99", "GITHUB_SHA": "newsha", "GITHUB_OUTPUT": str(tmp / "out"),
                   "SOURCE_SHA": "sourcesha", "ACR_NAME": "demoacr"}
            promote_source.main(["verify"], env=env, read=reader())
            self.assertEqual((tmp / "out").read_text(), "sha=sourcesha\n")
            cwd = os.getcwd()
            os.chdir(tmp)
            try:
                promote_source.main(["rebind", "source/release.json"], env=env)
            finally:
                os.chdir(cwd)
            self.assertEqual(json.loads((tmp / "release.json").read_text())["source_run_id"], "42")


WORKFLOWS = ROOT / ".github" / "workflows"


def release_jobs():
    text = (WORKFLOWS / "release.yml").read_text()
    jobs, current, lines = {}, None, text.split("jobs:\n", 1)[1].splitlines()
    for index, line in enumerate(lines):
        header = re.fullmatch(r"  ([a-z]+):", line)
        if header:
            current = jobs.setdefault(header.group(1), {"needs": [], "if": None, "body": ""})
            continue
        current["body"] += line + "\n"
        match = re.fullmatch(r"    (needs|if): (.*)", line)
        if not match:
            continue
        key, value = match.groups()
        if key == "needs":
            current["needs"] = [v.strip() for v in value.strip("[]").split(",")]
        elif value == ">-":
            continuation = []
            for follow in lines[index + 1:]:
                if not follow.startswith("      "):
                    break
                continuation.append(follow.strip())
            current["if"] = " ".join(continuation)
        else:
            current["if"] = value
    return jobs


def simulate(inputs, ref="refs/heads/main", failures=()):
    """Evaluate job `if`s like GitHub: no status function means an implicit success()."""
    inputs = {"profile": "production", "resume_from_run_id": "", "start_region": "kr", **inputs}
    jobs, results = release_jobs(), {}
    for name, job in jobs.items():
        needs = {n: SimpleNamespace(result=results[n]) for n in job["needs"]}
        success = all(n.result == "success" for n in needs.values())
        condition = (job["if"] or "success()").strip()
        if condition.startswith("${{"):
            condition = condition[3:-2]
        if not re.search(r"\b(success|failure|cancelled|always)\(", condition):
            condition = f"success() && ({condition})"
        python = re.sub(r"!(?!=)", " not ", condition.replace("&&", " and ").replace("||", " or "))
        scope = {"inputs": SimpleNamespace(**inputs), "needs": SimpleNamespace(**needs),
                 "github": SimpleNamespace(ref=ref), "success": lambda: success,
                 "cancelled": lambda: False, "always": lambda: True,
                 "failure": lambda: any(n.result == "failure" for n in needs.values())}
        ran = eval(python, {"__builtins__": {}}, scope)
        results[name] = ("failure" if name in failures else "success") if ran else "skipped"
    return {name for name, result in results.items() if result != "skipped"}


class ReleaseWorkflowTests(unittest.TestCase):
    REGIONS = ["korea", "southeastasia", "westus"]

    def test_normal_release_is_unchanged_and_ignores_start_region(self):
        for start in ("kr", "sea", "usw"):
            self.assertEqual(simulate({"start_region": start}), {"build", *self.REGIONS})
        self.assertEqual(simulate({}, failures={"build"}), {"build"})
        self.assertEqual(simulate({}, failures={"korea"}), {"build", "korea"})
        self.assertEqual(simulate({}, failures={"southeastasia"}), {"build", "korea", "southeastasia"})
        self.assertEqual(simulate({}, ref="refs/heads/feature"), set())

    def test_resume_promotes_source_and_starts_at_start_region(self):
        resume = {"resume_from_run_id": "42"}
        self.assertEqual(simulate({**resume, "start_region": "kr"}), {"promote", *self.REGIONS})
        self.assertEqual(simulate({**resume, "start_region": "sea"}), {"promote", "southeastasia", "westus"})
        self.assertEqual(simulate({**resume, "start_region": "usw"}), {"promote", "westus"})
        self.assertEqual(simulate({**resume, "start_region": "sea"}, failures={"promote"}), {"promote"})
        self.assertEqual(simulate({**resume, "start_region": "sea"}, failures={"southeastasia"}),
                         {"promote", "southeastasia"})
        self.assertEqual(simulate({**resume, "start_region": "kr"}, failures={"korea"}), {"promote", "korea"})
        self.assertEqual(simulate(resume, ref="refs/heads/feature"), set())

    def test_promote_job_is_protected_guarded_and_reuses_source_artifact(self):
        text = (WORKFLOWS / "release.yml").read_text()
        self.assertIn("      resume_from_run_id:\n", text)
        self.assertIn("        default: ''\n", text)
        self.assertIn("      start_region:\n        description:", text)
        self.assertIn("        options: [kr, sea, usw]\n        default: kr\n", text)
        promote = release_jobs()["promote"]["body"]
        self.assertIn("    environment: build\n", promote)
        self.assertIn('test "$RUN_ATTEMPT" = "1"', promote)
        self.assertLess(promote.index("promote_source.py verify"), promote.index("actions/download-artifact@v4"))
        self.assertLess(promote.index("actions/download-artifact@v4"), promote.index("promote_source.py rebind"))
        self.assertIn("name: release-${{ inputs.resume_from_run_id }}", promote)
        self.assertIn("run-id: ${{ inputs.resume_from_run_id }}", promote)
        self.assertIn("github-token: ${{ github.token }}", promote)
        self.assertIn("name: release-${{ github.run_id }}", promote)
        self.assertNotIn("run: python scripts/promote_source.py verify ${{", promote)
        self.assertIn("group: safe-aca-production\n  cancel-in-progress: false\n", text)

    def test_rerun_guards_remain_in_build_snapshot_and_rollout(self):
        self.assertIn('test "$RUN_ATTEMPT" = "1"', (WORKFLOWS / "build.yml").read_text())
        for script in ("snapshot.py", "rollout.py"):
            self.assertIn('os.environ["GITHUB_RUN_ATTEMPT"] == "1"',
                          (ROOT / "scripts" / script).read_text())


if __name__ == "__main__":
    unittest.main()
