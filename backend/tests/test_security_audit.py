"""DevSecOps audits: findings, secret hygiene of the reports, workspace bounds,
and the tool policy that keeps them read-only and off public channels."""

import json
import os

import pytest

from backend import security_audit


SECRET = "Zr8Kq2Lm9Xw4Vb7Nc1Pd6Tf3Hj5Gs0Ya"


@pytest.fixture
def workspace(tmp_path, monkeypatch):
    monkeypatch.setenv("PROJECT_WORKSPACE_ROOT", str(tmp_path))
    return tmp_path


def _write(root, relative, text):
    path = root / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


# ─── Images ───────────────────────────────────────────────────────────────────

def test_dockerfile_rules():
    findings = security_audit.audit_dockerfile(
        "FROM python:3.11-slim AS build\n"
        "RUN curl -fsSL https://get.example.sh | sh\n"
        "FROM build\n"
        "ADD https://example.com/tool.tgz /opt/\n",
        "Dockerfile",
    )
    rules = {f["rule"] for f in findings}
    assert {"unpinned-image", "pipe-to-shell", "remote-add", "runs-as-root"} <= rules
    # A stage alias is not an image pull.
    assert all(f.get("image") != "build" for f in findings)


def test_pinned_non_root_dockerfile_is_clean():
    findings = security_audit.audit_dockerfile(
        "FROM python:3.11-slim@sha256:" + "a" * 64 + "\nUSER app\n", "Dockerfile"
    )
    assert findings == []


def test_compose_rules_and_no_secret_values_in_output():
    text = (
        "services:\n"
        "  proxy:\n"
        "    image: tecnativa/docker-socket-proxy\n"
        "    privileged: true\n"
        "    network_mode: host\n"
        "    volumes:\n"
        "      - /var/run/docker.sock:/var/run/docker.sock:ro\n"
        "    environment:\n"
        "      - SECRETS=0\n"
        f"      - DB_PASSWORD={SECRET}\n"
        "      - API_TOKEN=${API_TOKEN}\n"
    )
    findings = security_audit.audit_compose(text, "docker-compose.yml")
    rules = [f["rule"] for f in findings]
    assert {"privileged", "host-network_mode", "docker-socket", "latest-tag", "unpinned-image"} <= set(rules)
    literal = [f for f in findings if f["rule"] == "literal-secret-in-compose"]
    assert len(literal) == 1 and "DB_PASSWORD" in literal[0]["detail"]
    assert SECRET not in json.dumps(findings)


# ─── Secrets ──────────────────────────────────────────────────────────────────

def test_secret_scan_reports_location_and_fingerprint_never_the_value(workspace):
    _write(workspace, "backend/settings.py", f'API_KEY = "{SECRET}"\n')
    _write(workspace, "backend/keys.py", "token = 'ghp_" + "A1b2C3d4" * 4 + "abcd'\n")
    _write(workspace, "backend/names.py", 'TOKEN_SETTING = "CONTENT_FACTORY_TOKEN"\npassword = "changeme-please"\n')
    report = security_audit.scan_secrets()
    by_file = {f["file"]: f for f in report["findings"]}
    assert set(by_file) == {"backend/settings.py", "backend/keys.py"}
    assert by_file["backend/keys.py"]["rule"] == "github-token"
    assert by_file["backend/settings.py"]["line"] == 1 and len(by_file["backend/settings.py"]["fingerprint"]) == 8
    dumped = json.dumps(report)
    assert SECRET not in dumped and "A1b2C3d4" not in dumped


def test_secret_scan_checks_env_file_mode_not_contents(workspace):
    env = _write(workspace, ".env", f"OPENROUTER_API_KEY={SECRET}\n")
    os.chmod(env, 0o644)
    report = security_audit.scan_secrets()
    assert [f["rule"] for f in report["findings"]] == ["world-readable-env-file"]
    os.chmod(env, 0o600)
    assert security_audit.scan_secrets()["findings"] == []


def test_secret_findings_in_tests_are_downgraded(workspace):
    _write(workspace, "backend/tests/test_x.py", f'API_KEY = "{SECRET}"\n')
    finding = security_audit.scan_secrets()["findings"][0]
    assert finding["in_tests"] is True and finding["severity"] == "low"


def test_scan_cannot_leave_the_workspace(workspace, tmp_path_factory):
    outside = tmp_path_factory.mktemp("outside")
    _write(outside, "leak.py", f'API_KEY = "{SECRET}"\n')
    result = json.loads(security_audit.run_tool("security_scan_secrets", {"path": "../" + outside.name}))
    assert result["status"] == "error"


def test_symlinks_are_not_followed(workspace, tmp_path_factory):
    outside = tmp_path_factory.mktemp("outside")
    _write(outside, "leak.py", f'API_KEY = "{SECRET}"\n')
    os.symlink(outside / "leak.py", workspace / "link.py")
    assert security_audit.scan_secrets()["findings"] == []


# ─── CI ───────────────────────────────────────────────────────────────────────

def test_workflow_rules():
    text = (
        "on:\n  pull_request_target:\n"
        "permissions: write-all\n"
        "jobs:\n  build:\n    runs-on: ubuntu-latest\n    steps:\n"
        "      - uses: actions/checkout@v4\n"
        "      - uses: actions/setup-python@" + "0" * 40 + "\n"
        "      - uses: ./local-action\n"
        "      - run: echo \"${{ github.event.pull_request.title }}\"\n"
    )
    findings = security_audit.audit_workflow(text, ".github/workflows/ci.yml")
    rules = [f["rule"] for f in findings]
    assert rules.count("unpinned-action") == 1
    assert {"pull-request-target", "write-all-permissions", "script-injection"} <= set(rules)


def test_audit_ci_finds_gitea_and_github_workflows(workspace):
    _write(workspace, ".gitea/workflows/ci.yml", "on: push\njobs: {}\n")
    _write(workspace, ".github/workflows/t.yml", "on: push\npermissions: {}\njobs: {}\n")
    report = security_audit.audit_ci()
    assert sorted(report["workflows"]) == [".gitea/workflows/ci.yml", ".github/workflows/t.yml"]
    assert [f["rule"] for f in report["findings"]] == ["default-token-permissions"]


# ─── Lockfiles ────────────────────────────────────────────────────────────────

UV_LOCK = """
version = 1

[[package]]
name = "hermes-backend"
version = "0.1.0"
source = { editable = "." }

[[package]]
name = "jinja2"
version = "3.1.2"
source = { registry = "https://pypi.org/simple" }
"""

PACKAGE_LOCK = json.dumps({
    "lockfileVersion": 3,
    "packages": {
        "": {"name": "frontend", "version": "1.0.0"},
        "node_modules/lodash": {"version": "4.17.20"},
        "node_modules/a/node_modules/lodash": {"version": "4.17.20"},
    },
})


def test_lockfile_parsers():
    assert security_audit.parse_uv_lock(UV_LOCK) == [{"name": "jinja2", "version": "3.1.2", "ecosystem": "PyPI"}]
    assert security_audit.parse_package_lock(PACKAGE_LOCK) == [
        {"name": "lodash", "version": "4.17.20", "ecosystem": "npm"}
    ]


def test_audit_lockfiles_against_osv(workspace, monkeypatch):
    _write(workspace, "backend/uv.lock", UV_LOCK)
    _write(workspace, "frontend/package-lock.json", PACKAGE_LOCK)
    sent = []

    def osv_post(path, payload):
        sent.append(payload)
        return {"results": [
            {"vulns": [{"id": "GHSA-jinja", "modified": "2026-01-01"}]} if q["package"]["name"] == "jinja2" else {}
            for q in payload["queries"]
        ]}

    def osv_get(path):
        return {"summary": "Sandbox escape", "aliases": ["CVE-2024-0001"],
                "database_specific": {"severity": "HIGH"},
                "affected": [{"package": {"name": "jinja2"}, "ranges": [{"events": [{"introduced": "0"}, {"fixed": "3.1.3"}]}]}]}

    monkeypatch.setattr(security_audit, "_osv_post", osv_post)
    monkeypatch.setattr(security_audit, "_osv_get", osv_get)
    report = security_audit.audit_lockfiles()
    assert report["status"] == "ok" and report["packages"] == 2
    finding = report["findings"][0]
    assert finding["package"] == "jinja2" and finding["severity"] == "high" and finding["fixed"] == ["3.1.3"]
    assert finding["lockfile"] == "backend/uv.lock"
    # Only names and versions leave the machine.
    assert set(sent[0]["queries"][0]) == {"package", "version"}


def test_audit_lockfiles_fails_closed_when_osv_is_unreachable(workspace, monkeypatch):
    _write(workspace, "backend/uv.lock", UV_LOCK)

    def down(path, payload):
        raise ConnectionError("no network")

    monkeypatch.setattr(security_audit, "_osv_post", down)
    report = security_audit.audit_lockfiles()
    assert report["status"] == "error" and "OSV unavailable" in report["error"]


# ─── Policy wiring ────────────────────────────────────────────────────────────

AUDIT_TOOLS = ["security_audit_lockfiles", "security_audit_images", "security_scan_secrets", "security_audit_ci"]


def test_audit_tools_are_subagent_only_and_low_risk():
    from backend import control_plane, tool_permissions

    for name in AUDIT_TOOLS:
        assert tool_permissions.is_tool_allowed(tool_permissions.SUBAGENT, name)
        assert not tool_permissions.is_tool_allowed(tool_permissions.PUBLIC_CHANNEL, name)
        assert control_plane.classify_tool_risk(name) in ("R0", "R1")


def test_audit_tools_are_in_the_schema_and_dispatch(workspace):
    from backend import tools

    names = {tool["function"]["name"] for tool in tools.TOOLS_SCHEMA}
    assert set(AUDIT_TOOLS) <= names
    result = json.loads(tools.execute_tool("security_audit_ci", {}))
    assert result["tool"] == "audit_ci"


def test_devsecops_discipline_routes_security_work():
    from backend import disciplines

    assert disciplines.detect_discipline("проверь уязвимости в зависимостях") == "security"
    assert disciplines.detect_discipline("найди секреты в репозитории") == "security"
    assert disciplines.detect_discipline("настрой деплой docker на сервер") == "devops"
