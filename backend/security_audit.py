"""Read-only security audits for the DevSecOps agent.

Four checks over the project workspace (the read-only /workspace mount in the
backend container, see autonomy.resolve_workspace):

* ``audit_lockfiles`` — every package pinned in uv.lock / package-lock.json
  checked against OSV.dev;
* ``audit_images`` — Dockerfiles and compose files: base images without a
  digest, ``latest``, root user, remote ADD, pipe-to-shell, privileged
  containers, docker.sock, host namespaces, literal secrets in environment;
* ``scan_secrets`` — credential-shaped strings in the tree;
* ``audit_ci`` — GitHub/Gitea workflows: actions not pinned to a commit SHA,
  ``pull_request_target``, event data interpolated into ``run:``, broad
  permissions.

Contracts (docs/CONTRACTS.md, «DevSecOps»):

* Nothing here writes, executes or changes anything. The only network call is
  to a fixed host (api.osv.dev) with package names and versions — never file
  contents.
* A secret is reported by location, rule and an 8-character fingerprint of its
  hash, never by value: the report goes to a model, to chat, and to Obsidian.
* Paths in reports are workspace-relative; the caller can narrow the scan to a
  subdirectory but never leave the workspace (resolved path, not text check).
* Output is bounded (MAX_FINDINGS) so a large tree cannot flood a turn.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import stat
import tomllib
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional

logger = logging.getLogger("hermes.security_audit")

MAX_FINDINGS = 200
MAX_SCAN_FILES = 6000
MAX_SCAN_BYTES = 1_000_000
OSV_API = "https://api.osv.dev/v1"
OSV_BATCH = 500
OSV_DETAIL_LIMIT = 40

_SKIP_DIRS = {
    ".git", "node_modules", ".venv", "venv", "__pycache__", ".pytest_cache", "dist",
    "build", "coverage", ".mypy_cache", ".ruff_cache", "site-packages",
}
_BINARY_SUFFIXES = {
    ".png", ".jpg", ".jpeg", ".gif", ".webp", ".ico", ".pdf", ".zip", ".gz", ".tgz",
    ".mp3", ".mp4", ".wav", ".ogg", ".onnx", ".bin", ".so", ".pyc", ".woff", ".woff2",
    ".ttf", ".db", ".sqlite", ".gguf", ".safetensors",
}


# ─── Workspace ────────────────────────────────────────────────────────────────

def _workspace(subpath: Optional[str] = None) -> tuple[Path, Path]:
    from backend.autonomy import resolve_workspace

    root = resolve_workspace()
    if not subpath:
        return root, root
    target = (root / subpath).resolve()
    try:
        target.relative_to(root)
    except ValueError as exc:
        raise ValueError("path must stay inside the workspace") from exc
    if not target.exists():
        raise ValueError("path does not exist in the workspace")
    return root, target


def _walk(base: Path) -> Iterable[Path]:
    count = 0
    for dirpath, dirnames, filenames in os.walk(base, followlinks=False):
        dirnames[:] = [name for name in dirnames if name not in _SKIP_DIRS]
        for name in filenames:
            path = Path(dirpath) / name
            if path.is_symlink():
                continue
            count += 1
            if count > MAX_SCAN_FILES:
                return
            yield path


def _rel(root: Path, path: Path) -> str:
    try:
        return path.relative_to(root).as_posix()
    except ValueError:
        return path.name


def _report(tool: str, root: Path, base: Path, findings: List[Dict[str, Any]], **extra: Any) -> Dict[str, Any]:
    order = {"critical": 0, "high": 1, "medium": 2, "low": 3, "info": 4}
    findings.sort(key=lambda item: (order.get(item.get("severity", "info"), 5), item.get("file", "")))
    summary: Dict[str, int] = {}
    for item in findings:
        summary[item.get("severity", "info")] = summary.get(item.get("severity", "info"), 0) + 1
    return {
        "tool": tool,
        "scope": _rel(root, base) if base != root else ".",
        "summary": summary,
        "total": len(findings),
        "truncated": len(findings) > MAX_FINDINGS,
        "findings": findings[:MAX_FINDINGS],
        **extra,
    }


# ─── Lockfiles → OSV ──────────────────────────────────────────────────────────

def parse_uv_lock(text: str) -> List[Dict[str, str]]:
    data = tomllib.loads(text)
    packages = []
    for package in data.get("package", []):
        source = package.get("source") or {}
        # editable/virtual/path entries are the project itself, not a dependency
        if not package.get("version") or not ("registry" in source or not source):
            continue
        packages.append({"name": package["name"], "version": package["version"], "ecosystem": "PyPI"})
    return packages


def parse_package_lock(text: str) -> List[Dict[str, str]]:
    data = json.loads(text)
    packages = []
    for key, meta in (data.get("packages") or {}).items():
        if not key or not isinstance(meta, dict) or meta.get("link") or not meta.get("version"):
            continue
        name = meta.get("name") or key.rsplit("node_modules/", 1)[-1]
        packages.append({"name": name, "version": meta["version"], "ecosystem": "npm"})
    if not packages:  # lockfileVersion 1
        for name, meta in (data.get("dependencies") or {}).items():
            if isinstance(meta, dict) and meta.get("version"):
                packages.append({"name": name, "version": meta["version"], "ecosystem": "npm"})
    unique = {(p["ecosystem"], p["name"], p["version"]): p for p in packages}
    return list(unique.values())


def _osv_post(path: str, payload: Dict[str, Any]) -> Dict[str, Any]:
    import httpx

    response = httpx.post(f"{OSV_API}{path}", json=payload, timeout=30.0, follow_redirects=False)
    response.raise_for_status()
    return response.json()


def _osv_get(path: str) -> Dict[str, Any]:
    import httpx

    response = httpx.get(f"{OSV_API}{path}", timeout=20.0, follow_redirects=False)
    response.raise_for_status()
    return response.json()


def _severity_from_osv(detail: Dict[str, Any]) -> str:
    label = str((detail.get("database_specific") or {}).get("severity") or "").lower()
    if label in ("critical", "high", "medium", "low"):
        return label
    if label == "moderate":
        return "medium"
    return "medium"


def _fixed_versions(detail: Dict[str, Any], name: str) -> List[str]:
    fixed = []
    for affected in detail.get("affected") or []:
        if (affected.get("package") or {}).get("name", "").lower() != name.lower():
            continue
        for range_ in affected.get("ranges") or []:
            for event in range_.get("events") or []:
                if event.get("fixed"):
                    fixed.append(event["fixed"])
    return sorted(set(fixed))


def audit_lockfiles(subpath: Optional[str] = None) -> Dict[str, Any]:
    root, base = _workspace(subpath)
    targets = []
    for path in _walk(base):
        if path.name == "uv.lock":
            targets.append((path, parse_uv_lock))
        elif path.name == "package-lock.json":
            targets.append((path, parse_package_lock))

    entries: List[Dict[str, str]] = []
    errors: List[str] = []
    for path, parser in targets:
        try:
            for package in parser(path.read_text(encoding="utf-8")):
                entries.append({**package, "lockfile": _rel(root, path)})
        except Exception as exc:
            errors.append(f"{_rel(root, path)}: {type(exc).__name__}")

    hits: List[tuple[Dict[str, str], Dict[str, Any]]] = []
    try:
        for start in range(0, len(entries), OSV_BATCH):
            batch = entries[start:start + OSV_BATCH]
            result = _osv_post("/querybatch", {"queries": [
                {"package": {"name": e["name"], "ecosystem": e["ecosystem"]}, "version": e["version"]}
                for e in batch
            ]})
            for entry, row in zip(batch, result.get("results") or []):
                for vuln in (row or {}).get("vulns") or []:
                    hits.append((entry, vuln))
    except Exception as exc:
        # Fail closed and say so: "no findings" from a scan that never reached
        # the database would read as a clean bill of health.
        return _report("audit_lockfiles", root, base, [], status="error",
                       error=f"OSV unavailable: {type(exc).__name__}",
                       lockfiles=[_rel(root, p) for p, _ in targets], packages=len(entries))

    details: Dict[str, Dict[str, Any]] = {}
    findings = []
    for entry, vuln in hits:
        vuln_id = vuln.get("id", "")
        if vuln_id not in details and len(details) < OSV_DETAIL_LIMIT:
            try:
                details[vuln_id] = _osv_get(f"/vulns/{vuln_id}")
            except Exception:
                details[vuln_id] = {}
        detail = details.get(vuln_id) or {}
        findings.append({
            "rule": "vulnerable-dependency",
            "severity": _severity_from_osv(detail) if detail else "medium",
            "id": vuln_id,
            "aliases": (detail.get("aliases") or [])[:5],
            "package": entry["name"],
            "version": entry["version"],
            "ecosystem": entry["ecosystem"],
            "lockfile": entry["lockfile"],
            "file": entry["lockfile"],
            "summary": str(detail.get("summary") or "")[:300],
            "fixed": _fixed_versions(detail, entry["name"]) if detail else [],
            "modified": vuln.get("modified") or detail.get("modified") or "",
        })
    return _report("audit_lockfiles", root, base, findings, status="ok",
                   lockfiles=[_rel(root, p) for p, _ in targets], packages=len(entries), errors=errors)


# ─── Images: Dockerfiles and compose ──────────────────────────────────────────

_FROM = re.compile(r"^\s*FROM\s+(?:--platform=\S+\s+)?(\S+)(?:\s+AS\s+(\S+))?", re.I)
_PIPE_SHELL = re.compile(r"\b(curl|wget)\b[^|\n]*\|\s*(sudo\s+)?(ba|z|da)?sh\b")
_SECRETISH_KEY = re.compile(r"(password|passwd|secret|token|api[_-]?key|private[_-]?key)", re.I)


def _image_findings(image: str, file: str, line: int, stages: set[str]) -> List[Dict[str, Any]]:
    if image.lower() == "scratch" or image in stages or "$" in image:
        return []
    findings = []
    if "@sha256:" not in image:
        findings.append({"rule": "unpinned-image", "severity": "medium", "file": file, "line": line,
                         "image": image, "detail": "no @sha256 digest — the tag can be repointed"})
    name = image.split("@", 1)[0]
    tag = name.rsplit(":", 1)[1] if ":" in name.rsplit("/", 1)[-1] else ""
    if tag in ("", "latest"):
        findings.append({"rule": "latest-tag", "severity": "medium", "file": file, "line": line,
                         "image": image, "detail": "implicit or explicit :latest"})
    return findings


def audit_dockerfile(text: str, file: str) -> List[Dict[str, Any]]:
    findings: List[Dict[str, Any]] = []
    stages: set[str] = set()
    final_user = None
    final_stage_line = 0
    for number, line in enumerate(text.splitlines(), start=1):
        stripped = line.strip()
        match = _FROM.match(stripped)
        if match:
            findings += _image_findings(match.group(1), file, number, stages)
            if match.group(2):
                stages.add(match.group(2))
            final_user = None
            final_stage_line = number
            continue
        upper = stripped.upper()
        if upper.startswith("USER "):
            final_user = stripped.split(None, 1)[1].strip()
        elif upper.startswith("ADD ") and re.search(r"\bhttps?://", stripped):
            findings.append({"rule": "remote-add", "severity": "medium", "file": file, "line": number,
                             "detail": "ADD from a URL is unverified — download with a checksum instead"})
        if _PIPE_SHELL.search(stripped):
            findings.append({"rule": "pipe-to-shell", "severity": "high", "file": file, "line": number,
                             "detail": "remote script piped into a shell during build"})
    if final_stage_line and final_user in (None, "root", "0", "0:0"):
        findings.append({"rule": "runs-as-root", "severity": "low", "file": file, "line": final_stage_line,
                         "detail": "final stage has no non-root USER"})
    return findings


def audit_compose(text: str, file: str) -> List[Dict[str, Any]]:
    import yaml

    try:
        data = yaml.safe_load(text) or {}
    except yaml.YAMLError:
        return [{"rule": "unparseable", "severity": "info", "file": file, "detail": "YAML parse error"}]
    findings: List[Dict[str, Any]] = []
    services = data.get("services") if isinstance(data, dict) else None
    for name, service in (services or {}).items():
        if not isinstance(service, dict):
            continue
        where = {"file": file, "service": name}
        if service.get("image"):
            for finding in _image_findings(str(service["image"]), file, 0, set()):
                findings.append({**finding, "service": name})
        if service.get("privileged"):
            findings.append({**where, "rule": "privileged", "severity": "high", "detail": "privileged: true"})
        for key in ("network_mode", "pid", "ipc"):
            if str(service.get(key) or "") == "host":
                findings.append({**where, "rule": f"host-{key}", "severity": "medium", "detail": f"{key}: host"})
        if service.get("cap_add"):
            findings.append({**where, "rule": "cap-add", "severity": "medium",
                             "detail": f"cap_add: {', '.join(map(str, service['cap_add']))}"})
        for volume in service.get("volumes") or []:
            source = volume.get("source", "") if isinstance(volume, dict) else str(volume).split(":", 1)[0]
            if "docker.sock" in source:
                findings.append({**where, "rule": "docker-socket", "severity": "high",
                                 "detail": "docker.sock mounted — root on the host for anything in the container"})
        environment = service.get("environment") or {}
        if isinstance(environment, list):
            environment = dict(item.split("=", 1) if "=" in item else (item, None) for item in environment)
        for key, value in environment.items():
            if value and _SECRETISH_KEY.search(str(key)) and _looks_like_literal_secret(str(value)):
                findings.append({**where, "rule": "literal-secret-in-compose", "severity": "high",
                                 "detail": f"environment {key} has a literal value (value not shown)"})
    return findings


def audit_images(subpath: Optional[str] = None) -> Dict[str, Any]:
    root, base = _workspace(subpath)
    findings: List[Dict[str, Any]] = []
    scanned = []
    for path in _walk(base):
        name = path.name.lower()
        try:
            if name == "dockerfile" or name.startswith("dockerfile.") or name.endswith(".dockerfile"):
                findings += audit_dockerfile(path.read_text(encoding="utf-8", errors="replace"), _rel(root, path))
                scanned.append(_rel(root, path))
            elif re.fullmatch(r"(docker-)?compose(\.[\w-]+)*\.ya?ml", name):
                findings += audit_compose(path.read_text(encoding="utf-8", errors="replace"), _rel(root, path))
                scanned.append(_rel(root, path))
        except OSError:
            continue
    return _report("audit_images", root, base, findings, files=scanned)


# ─── Secrets ──────────────────────────────────────────────────────────────────

SECRET_RULES = [
    ("private-key", "critical", re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH |DSA |PGP )?PRIVATE KEY-----")),
    ("aws-access-key", "critical", re.compile(r"\b(AKIA|ASIA)[0-9A-Z]{16}\b")),
    ("github-token", "critical", re.compile(r"\b(ghp|gho|ghu|ghs|ghr)_[A-Za-z0-9]{36}\b|\bgithub_pat_[A-Za-z0-9_]{60,}\b")),
    ("gitea-or-hex-token", "low", re.compile(r"(?i)\b(token|api[_-]?key)\b\s*[=:]\s*['\"]?[0-9a-f]{40}\b")),
    ("slack-token", "critical", re.compile(r"\bxox[abpors]-[A-Za-z0-9-]{10,}\b")),
    ("anthropic-key", "critical", re.compile(r"\bsk-ant-[A-Za-z0-9_-]{20,}\b")),
    ("openai-style-key", "high", re.compile(r"\bsk-(?:proj-|or-v1-)?[A-Za-z0-9_-]{32,}\b")),
    ("telegram-bot-token", "high", re.compile(r"\b\d{8,10}:AA[A-Za-z0-9_-]{33}\b")),
    ("google-api-key", "high", re.compile(r"\bAIza[0-9A-Za-z_-]{35}\b")),
    ("generic-assignment", "medium", re.compile(
        r"(?i)\b[\w.-]*(password|passwd|secret|api[_-]?key|access[_-]?token|auth[_-]?token)\b\s*[=:]\s*['\"]([^'\"\s]{12,})['\"]")),
]
_PLACEHOLDER = re.compile(
    r"(?i)(example|placeholder|changeme|your[_-]|dummy|mock|fake|test|sample|xxx|\*\*\*|<|\$\{|\{\{|redacted|0{8,})")


# A single-case word with underscores is a name (an env var, a storage key),
# not a credential: "CONTENT_FACTORY_TOKEN" / "vexa_auth_token".
_IDENTIFIER = re.compile(r"^(?:[a-z][a-z_.-]*|[A-Z][A-Z0-9_.-]*)$")


def _looks_like_literal_secret(value: str) -> bool:
    value = value.strip()
    if len(value) < 8 or _IDENTIFIER.fullmatch(value) or _PLACEHOLDER.search(value):
        return False
    return value.lower() not in {"true", "false", "none", "null"}


def _fingerprint(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8", errors="replace")).hexdigest()[:8]


def scan_text_for_secrets(text: str, file: str) -> List[Dict[str, Any]]:
    findings = []
    for number, line in enumerate(text.splitlines(), start=1):
        if len(line) > 4000:
            continue
        for rule, severity, pattern in SECRET_RULES:
            match = pattern.search(line)
            if not match:
                continue
            value = match.group(match.lastindex) if rule == "generic-assignment" else match.group(0)
            if rule == "generic-assignment" and not _looks_like_literal_secret(value):
                continue
            if _PLACEHOLDER.search(value):
                continue
            findings.append({"rule": rule, "severity": severity, "file": file, "line": number,
                             "fingerprint": _fingerprint(value)})
            break
    return findings


def scan_secrets(subpath: Optional[str] = None) -> Dict[str, Any]:
    root, base = _workspace(subpath)
    findings: List[Dict[str, Any]] = []
    scanned = 0
    for path in _walk(base):
        rel = _rel(root, path)
        name = path.name
        if name == ".env" or (name.startswith(".env.") and name not in (".env.example", ".env.sample")):
            # Real configuration is expected to hold secrets; what matters is
            # who can read it, so report its mode instead of its contents.
            try:
                mode = path.stat().st_mode
            except OSError:
                continue
            if mode & (stat.S_IROTH | stat.S_IWOTH):
                findings.append({"rule": "world-readable-env-file", "severity": "high", "file": rel,
                                 "detail": f"mode {oct(mode & 0o777)} — should be 0600"})
            continue
        if path.suffix.lower() in _BINARY_SUFFIXES or name.endswith((".lock", "-lock.json")):
            continue
        try:
            if path.stat().st_size > MAX_SCAN_BYTES:
                continue
            raw = path.read_bytes()
        except OSError:
            continue
        if b"\x00" in raw[:4096]:
            continue
        scanned += 1
        for finding in scan_text_for_secrets(raw.decode("utf-8", errors="replace"), rel):
            if "/tests/" in f"/{rel}" or name.startswith("test_"):
                finding["in_tests"] = True
                finding["severity"] = "low"
            findings.append(finding)
    return _report("scan_secrets", root, base, findings, files_scanned=scanned)


# ─── CI workflows ─────────────────────────────────────────────────────────────

_USES = re.compile(r"^\s*-?\s*uses:\s*['\"]?([^'\"\s#]+)")
_SHA = re.compile(r"@[0-9a-f]{40}$")
_EVENT_IN_RUN = re.compile(
    r"\$\{\{\s*github\.(event\.(issue|pull_request|comment|review|head_commit|commits)|head_ref)[^}]*\}\}")


def audit_workflow(text: str, file: str) -> List[Dict[str, Any]]:
    import yaml

    findings: List[Dict[str, Any]] = []
    for number, line in enumerate(text.splitlines(), start=1):
        match = _USES.match(line)
        if match:
            ref = match.group(1)
            if not ref.startswith(("./", "docker://")) and not _SHA.search(ref):
                findings.append({"rule": "unpinned-action", "severity": "medium", "file": file, "line": number,
                                 "action": ref, "detail": "pin third-party actions to a full commit SHA"})
    try:
        data = yaml.safe_load(text) or {}
    except yaml.YAMLError:
        return findings + [{"rule": "unparseable", "severity": "info", "file": file}]
    if not isinstance(data, dict):
        return findings
    # PyYAML reads the bare key `on` as boolean True.
    triggers = data.get("on", data.get(True))
    trigger_names = set(triggers) if isinstance(triggers, (dict, list)) else {str(triggers)}
    if "pull_request_target" in trigger_names:
        findings.append({"rule": "pull-request-target", "severity": "high", "file": file,
                         "detail": "pull_request_target runs with secrets on forked code if it checks out the PR"})
    permissions = data.get("permissions")
    if permissions == "write-all":
        findings.append({"rule": "write-all-permissions", "severity": "high", "file": file,
                         "detail": "permissions: write-all"})
    elif permissions is None:
        findings.append({"rule": "default-token-permissions", "severity": "low", "file": file,
                         "detail": "no top-level permissions block — token gets the repository default"})
    for job_name, job in (data.get("jobs") or {}).items():
        for step in (job or {}).get("steps") or []:
            run = step.get("run") if isinstance(step, dict) else None
            if run and _EVENT_IN_RUN.search(str(run)):
                findings.append({"rule": "script-injection", "severity": "high", "file": file, "job": job_name,
                                 "detail": "untrusted event data interpolated into run: — pass it via env"})
    return findings


def audit_ci(subpath: Optional[str] = None) -> Dict[str, Any]:
    root, base = _workspace(subpath)
    findings: List[Dict[str, Any]] = []
    workflows = []
    for path in _walk(base):
        rel = _rel(root, path)
        if re.search(r"(^|/)\.(github|gitea|forgejo)/workflows/[^/]+\.ya?ml$", rel):
            try:
                findings += audit_workflow(path.read_text(encoding="utf-8", errors="replace"), rel)
                workflows.append(rel)
            except OSError:
                continue
    return _report("audit_ci", root, base, findings, workflows=workflows)


AUDITS = {
    "security_audit_lockfiles": audit_lockfiles,
    "security_audit_images": audit_images,
    "security_scan_secrets": scan_secrets,
    "security_audit_ci": audit_ci,
}


def run_tool(name: str, arguments: Dict[str, Any]) -> str:
    try:
        report = AUDITS[name](arguments.get("path") or None)
    except ValueError as exc:
        return json.dumps({"status": "error", "error": str(exc)}, ensure_ascii=False)
    except Exception as exc:
        logger.exception("Security audit %s failed", name)
        return json.dumps({"status": "error", "error": type(exc).__name__}, ensure_ascii=False)
    return json.dumps(report, ensure_ascii=False)
