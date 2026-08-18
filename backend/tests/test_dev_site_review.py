"""Tests for the site review — the dev-run's only way to see what it built.

pytest/npm say nothing about whether a static site renders, so a run could
publish a blank page, a page throwing JS errors or one that scrolls sideways on
a phone and still declare itself done. These cover the translation from the
sidecar's measurements into a verdict, and the completion gate that verdict
feeds.
"""

import json

import pytest

from backend import autonomy, control_plane, database, dev_runs, dev_sandbox, tools


@pytest.fixture()
def site_env(tmp_path, monkeypatch):
    db_path = str(tmp_path / "runs.db")
    for module in (database, control_plane, dev_runs, autonomy):
        monkeypatch.setattr(module, "DB_PATH", db_path)
    database.init_db()
    previews = tmp_path / "dev-previews"
    monkeypatch.setattr(dev_sandbox, "PREVIEWS_ROOT", previews)
    monkeypatch.setattr(dev_sandbox, "RUNS_ROOT", tmp_path / "dev-repo-runs")
    return previews


def _publish(previews, run_id, html="<html><body><h1>Hi</h1></body></html>"):
    (previews / run_id).mkdir(parents=True, exist_ok=True)
    (previews / run_id / "index.html").write_text(html)


def _audit_page(**overrides):
    page = {
        "viewport": "mobile", "size": "375x812", "title": "Landing", "text_length": 400,
        "headings": 2, "broken_images": [], "horizontal_overflow_px": 0,
        "links": [], "console_errors": [], "failed_requests": [],
    }
    page.update(overrides)
    return page


def _stub_audit(monkeypatch, payload):
    monkeypatch.setattr(tools, "_browser_runner_audit", lambda run_id, timeout=180.0: payload)


# ── Translating measurements into a verdict ──────────────────────────────────

def test_a_clean_site_passes(site_env, monkeypatch):
    _publish(site_env, "run-000000000001")
    _stub_audit(monkeypatch, {"pages": [_audit_page(), _audit_page(viewport="desktop")]})
    review = tools.review_published_demo("run-000000000001")
    assert review["verdict"] == "pass"
    assert review["problems"] == []


@pytest.mark.parametrize("page_overrides, expected", [
    ({"console_errors": ["error: Uncaught TypeError: x is not a function"]}, "JavaScript error"),
    ({"failed_requests": ["HTTP 404 http://127.0.0.1/app.css"]}, "request failed"),
    ({"broken_images": ["/img/hero.png"]}, "image does not render"),
    ({"horizontal_overflow_px": 140}, "wider than the screen"),
    ({"text_length": 3}, "essentially empty"),
    ({"load_error": "TimeoutError: navigation timeout"}, "did not load"),
])
def test_each_kind_of_breakage_fails_the_review(site_env, monkeypatch, page_overrides, expected):
    _publish(site_env, "run-000000000001")
    _stub_audit(monkeypatch, {"pages": [_audit_page(**page_overrides)]})
    review = tools.review_published_demo("run-000000000001")
    assert review["verdict"] == "fail"
    assert any(expected in problem for problem in review["problems"]), review["problems"]


def test_warnings_do_not_fail_the_review(site_env, monkeypatch):
    """A console warning or a missing <title> is worth telling the agent about,
    but refusing to finish a working site over it would be obstruction."""
    _publish(site_env, "run-000000000001")
    _stub_audit(monkeypatch, {"pages": [_audit_page(
        title="", headings=0, console_errors=["warning: deprecated API"])]})
    review = tools.review_published_demo("run-000000000001")
    assert review["verdict"] == "pass"
    assert any("no <title>" in w for w in review["warnings"])
    assert any("console warning" in w for w in review["warnings"])


def test_dead_internal_links_are_caught_against_the_published_files(site_env, monkeypatch):
    previews = site_env
    _publish(previews, "run-000000000001")
    (previews / "run-000000000001" / "about.html").write_text("<html>about</html>")
    _stub_audit(monkeypatch, {"pages": [_audit_page(links=[
        "about.html",            # published
        "/pricing.html",         # never published
        "https://example.com",   # not ours to guarantee
        "#features", "mailto:a@b.c",
        "../../etc/passwd",      # escapes the snapshot
    ])]})
    review = tools.review_published_demo("run-000000000001")
    dead = [p for p in review["problems"] if "[links]" in p]
    assert len(dead) == 1
    assert "/pricing.html" in dead[0]


def test_screenshots_land_beside_the_snapshot_not_inside_it(site_env, monkeypatch):
    """Inside the snapshot they would be wiped by the next publish and counted
    against the publish size limits."""
    import base64

    previews = site_env
    _publish(previews, "run-000000000001")
    blob = base64.b64encode(b"\xff\xd8\xff-jpeg-ish").decode()
    _stub_audit(monkeypatch, {"pages": [_audit_page(screenshot_b64=blob)]})

    review = tools.review_published_demo("run-000000000001")
    assert review["screenshots"]["mobile"] == "/demo/_audits/run-000000000001/mobile.jpg"
    assert (previews / "_audits" / "run-000000000001" / "mobile.jpg").exists()
    assert not (previews / "run-000000000001" / "mobile.jpg").exists()
    # The blob itself never travels back to the model.
    assert "screenshot_b64" not in json.dumps(review)


def test_an_unreachable_reviewer_reports_unavailable_not_failure(site_env, monkeypatch):
    _publish(site_env, "run-000000000001")
    _stub_audit(monkeypatch, {"error": "Browser runner is unreachable (ConnectError)."})
    assert tools.review_published_demo("run-000000000001")["verdict"] == "unavailable"


def test_the_tool_refuses_before_anything_is_published(site_env):
    token = tools.CURRENT_DEV_RUN_ID.set("run-000000000001")
    try:
        assert "call dev_publish_demo first" in json.loads(tools.dev_review_demo())["error"]
    finally:
        tools.CURRENT_DEV_RUN_ID.reset(token)


# ── The completion gate ──────────────────────────────────────────────────────

@pytest.fixture()
def passing_tests(monkeypatch):
    """dev_run_tests always green, so only the site verdict is under test."""
    monkeypatch.setattr(dev_runs, "execute_governed_tool",
                        lambda *a, **kw: json.dumps({"exit_code": 0, "runner": "auto"}))


@pytest.mark.asyncio
async def test_a_site_that_does_not_render_cannot_be_declared_done(site_env, monkeypatch, passing_tests):
    run = dev_runs.create_run("build a landing page")
    _publish(site_env, run["id"])
    _stub_audit(monkeypatch, {"pages": [_audit_page(
        console_errors=["error: Uncaught ReferenceError: init is not defined"],
        horizontal_overflow_px=210)]})

    passed, updated = await dev_runs._verification_gate(dev_runs.get_run(run["id"]), trigger="completion")
    assert passed is False
    failure = [s for s in dev_runs.get_steps(run["id"]) if s["status"] == "failed"][-1]
    assert failure["tool"] == "dev_review_demo"
    # The specifics must reach the run context, or the next iteration is blind.
    assert "ReferenceError" in failure["summary"]
    assert "wider than the screen" in failure["summary"]
    assert updated["status"] != "done"


@pytest.mark.asyncio
async def test_a_rendering_site_passes_the_gate(site_env, monkeypatch, passing_tests):
    run = dev_runs.create_run("build a landing page")
    _publish(site_env, run["id"])
    _stub_audit(monkeypatch, {"pages": [_audit_page()]})
    passed, _ = await dev_runs._verification_gate(dev_runs.get_run(run["id"]), trigger="completion")
    assert passed is True


@pytest.mark.asyncio
async def test_a_run_that_published_nothing_is_not_asked_to_render(site_env, monkeypatch, passing_tests):
    """Most dev-runs are not websites; the gate must stay out of their way."""
    called = []
    monkeypatch.setattr(tools, "_browser_runner_audit",
                        lambda *a, **kw: called.append(1) or {"pages": []})
    run = dev_runs.create_run("refactor the billing module")
    passed, _ = await dev_runs._verification_gate(dev_runs.get_run(run["id"]), trigger="completion")
    assert passed is True and called == []


@pytest.mark.asyncio
async def test_a_downed_reviewer_never_traps_a_finished_run(site_env, monkeypatch, passing_tests):
    """Otherwise one dead sidecar makes every site run permanently unfinishable."""
    run = dev_runs.create_run("build a landing page")
    _publish(site_env, run["id"])
    _stub_audit(monkeypatch, {"error": "Browser runner is unreachable (ConnectError)."})
    passed, _ = await dev_runs._verification_gate(dev_runs.get_run(run["id"]), trigger="completion")
    assert passed is True
    note = [s for s in dev_runs.get_steps(run["id"]) if s["tool"] == "dev_review_demo"][-1]
    assert "not blocked" in note["summary"]


@pytest.mark.asyncio
async def test_a_push_is_not_held_up_by_rendering(site_env, monkeypatch, passing_tests):
    """Committing broken markup is how you fix it in the next iteration."""
    run = dev_runs.create_run("build a landing page")
    _publish(site_env, run["id"])
    _stub_audit(monkeypatch, {"pages": [_audit_page(text_length=0)]})
    passed, _ = await dev_runs._verification_gate(dev_runs.get_run(run["id"]), trigger="push")
    assert passed is True


def test_deleting_a_run_takes_its_audit_screenshots_with_it(site_env):
    run = dev_runs.create_run("build a landing page")
    _publish(site_env, run["id"])
    audits = site_env / "_audits" / run["id"]
    audits.mkdir(parents=True)
    (audits / "mobile.jpg").write_bytes(b"x")

    dev_runs.delete_run(run["id"])
    assert not audits.exists()
