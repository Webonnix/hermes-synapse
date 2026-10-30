"""The repository a GitHub issue belongs to: snapshot, context, patch.

Nothing in this module executes repository code, and nothing writes the
repository out as a tree on the backend's disk:

* **Snapshot** — the default branch is pinned to one commit SHA, and that
  commit's tarball comes from codeload.github.com through egress (exact hosts,
  HTTPS, pinned IP, no redirects, size cap). The bytes are kept as one
  content-addressed blob (``<sha256>.tar.gz``) so that build, execution and
  the pull request all work from the same, hash-checked input.
* **Tree** — the tarball is read in memory with ``tarfile``: regular files
  only (no symlinks, hardlinks, devices), normalised relative paths, counted
  and size-capped before anything is read. It is unpacked for real only inside
  the gVisor sandbox (sandbox.run_repo).
* **Context** — the model sees a bounded selection of files: paths the issue
  names, files the locator picked, files that contain identifiers from the
  issue, their tests, plus a listing of the tree.
* **Patch** — the model answers with a unified diff. It is parsed here into
  a small, strict model (no renames, deletions, mode changes, binary or
  symlink patches, nothing under ``.git``/``.github``) and applied in pure
  Python by exact context match. Changed Python files are parsed with ``ast``
  (which does not execute them).

The repository text is untrusted. A CONTRIBUTING file that bans AI-generated
contributions is honoured: the build stops before any model call.
"""

from __future__ import annotations

import ast
import gzip
import hashlib
import io
import json
import os
import posixpath
import re
import tarfile
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, Iterable, List, Optional, Tuple

from backend.earner import egress, store

# Code-fixed, like the GitHub API host in submit.py: a setting can never point
# the snapshot anywhere else.
API_HOST = "api.github.com"
CODELOAD_HOST = "codeload.github.com"
REPO_HOSTS = [API_HOST, CODELOAD_HOST]

ISSUE_URL = re.compile(r"^https://github\.com/([A-Za-z0-9_.-]{1,100})/([A-Za-z0-9_.-]{1,100})/issues/(\d{1,9})$")
_NAME = re.compile(r"^[A-Za-z0-9_.-]{1,100}$")
_SHA = re.compile(r"^[0-9a-f]{40}$")

MAX_TREE_FILES = 8000
MAX_TREE_BYTES = 80 * 1024 * 1024
MAX_MEMBER_BYTES = 4 * 1024 * 1024
MAX_DIFF_BYTES = 96 * 1024
MAX_DIFF_FILES = 12
MAX_HUNKS_PER_FILE = 60
LISTING_LIMIT = 400

SKIP_DIRS = {".git", "node_modules", "vendor", "third_party", "dist", "build", ".tox", ".nox", ".venv", "venv",
             "site-packages", "__pycache__", ".mypy_cache", ".pytest_cache", ".eggs"}
TEXT_SUFFIXES = (".py", ".pyi", ".md", ".rst", ".txt", ".toml", ".cfg", ".ini", ".json", ".yaml", ".yml",
                 ".csv", ".html", ".js", ".ts", ".sh", ".in")


class RepoError(RuntimeError):
    """The repository cannot be used; the reason is shown to the operator."""


class PatchError(RuntimeError):
    """The model's diff is malformed, unsafe or does not apply."""


Fetcher = Callable[..., egress.FetchResult]


def _env_int(name: str, default: int, low: int, high: int) -> int:
    try:
        return max(low, min(int(os.getenv(name, str(default))), high))
    except ValueError:
        return default


def max_tarball_bytes() -> int:
    return _env_int("EARNER_REPO_MAX_MB", 20, 1, 60) * 1024 * 1024


def context_budget() -> int:
    return _env_int("EARNER_REPO_CONTEXT_KB", 40, 8, 160) * 1024


def parse_issue_url(url: str) -> Optional[Tuple[str, str, int]]:
    match = ISSUE_URL.match(url or "")
    if not match:
        return None
    owner, name, number = match.groups()
    if owner in (".", "..") or name in (".", ".."):
        return None
    return owner, name, int(number)


# ── snapshot ─────────────────────────────────────────────────────────────────

def blob_dir() -> str:
    from backend import database

    configured = os.getenv("EARNER_REPO_CACHE_DIR", "").strip()
    return configured or os.path.join(os.path.dirname(os.path.abspath(database.DB_PATH)), "earner", "repos")


def _blob_path(sha256: str) -> str:
    if not re.fullmatch(r"[0-9a-f]{64}", sha256 or ""):
        raise RepoError("недопустимый хеш архива")
    return os.path.join(blob_dir(), f"{sha256}.tar.gz")


def save_blob(data: bytes) -> str:
    digest = hashlib.sha256(data).hexdigest()
    path = _blob_path(digest)
    os.makedirs(os.path.dirname(path), mode=0o700, exist_ok=True)
    if not os.path.exists(path):
        temporary = f"{path}.{os.getpid()}.part"
        with open(temporary, "wb") as handle:
            handle.write(data)
        os.replace(temporary, path)
    return digest


def load_blob(sha256: str) -> bytes:
    path = _blob_path(sha256)
    try:
        with open(path, "rb") as handle:
            data = handle.read()
    except FileNotFoundError as exc:
        raise RepoError("архив репозитория не найден — нужна новая генерация") from exc
    if hashlib.sha256(data).hexdigest() != sha256:
        raise RepoError("архив репозитория не совпадает со своим хешем")
    return data


def prune_blobs(keep_days: int = 14) -> int:
    """Old snapshots are not needed once their task is done or abandoned."""
    directory = blob_dir()
    if not os.path.isdir(directory):
        return 0
    cutoff = time.time() - keep_days * 86400
    removed = 0
    for name in os.listdir(directory):
        path = os.path.join(directory, name)
        if re.fullmatch(r"[0-9a-f]{64}\.tar\.gz(\.\d+\.part)?", name) and os.path.getmtime(path) < cutoff:
            os.remove(path)
            removed += 1
    return removed


def _github_headers(accept: str) -> Dict[str, str]:
    from backend.earner import submit

    token = submit._github_token()
    headers = {"X-GitHub-Api-Version": "2022-11-28"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    return headers


def _json(result: egress.FetchResult, what: str) -> Dict[str, Any]:
    if result.status != 200:
        raise RepoError(f"{what}: GitHub ответил HTTP {result.status}")
    try:
        data = json.loads(result.body.decode("utf-8", errors="replace"))
    except ValueError as exc:
        raise RepoError(f"{what}: ответ не JSON") from exc
    if not isinstance(data, dict):
        raise RepoError(f"{what}: неожиданный ответ")
    return data


def fetch_snapshot(owner: str, name: str, fetch: Optional[Fetcher] = None) -> Tuple[Dict[str, Any], bytes]:
    """Three requests: repository metadata, the default branch's commit SHA,
    and the tarball of exactly that commit."""
    fetch = fetch or egress.fetch
    if not _NAME.match(owner) or not _NAME.match(name):
        raise RepoError("недопустимое имя репозитория")
    accept = "application/vnd.github+json"
    meta = _json(fetch(f"https://{API_HOST}/repos/{owner}/{name}", REPO_HOSTS, accept=accept,
                       extra_headers=_github_headers(accept)), "репозиторий")
    if meta.get("private") or meta.get("visibility", "public") != "public":
        raise RepoError("репозиторий не публичный")
    if meta.get("archived") or meta.get("disabled"):
        raise RepoError("репозиторий архивирован — изменения не принимаются")
    branch = str(meta.get("default_branch") or "")
    if not branch or len(branch) > 200 or any(ch in branch for ch in " ~^:?*[\\") or ".." in branch:
        raise RepoError("у репозитория нет понятной ветки по умолчанию")
    full_name = str(meta.get("full_name") or f"{owner}/{name}")
    if full_name.lower() != f"{owner}/{name}".lower():
        # A renamed or transferred repository answers with its new name; the
        # issue URL stays the identity we approved, so refuse rather than follow.
        raise RepoError(f"репозиторий переименован в {full_name[:120]} — обновите задачу")
    sha_accept = "application/vnd.github.sha"
    sha_result = fetch(f"https://{API_HOST}/repos/{owner}/{name}/commits/{branch}", REPO_HOSTS,
                       accept=sha_accept, extra_headers=_github_headers(sha_accept), max_bytes=4096)
    if sha_result.status != 200:
        raise RepoError(f"коммит ветки {branch}: GitHub ответил HTTP {sha_result.status}")
    commit = sha_result.body.decode("ascii", errors="replace").strip()
    if not _SHA.match(commit):
        raise RepoError("GitHub не вернул SHA коммита")
    tarball = fetch(f"https://{CODELOAD_HOST}/{owner}/{name}/tar.gz/{commit}", REPO_HOSTS,
                    accept="application/x-gzip", max_bytes=max_tarball_bytes(), timeout=90.0)
    if tarball.status != 200:
        raise RepoError(f"архив коммита: codeload ответил HTTP {tarball.status}")
    data = tarball.body
    digest = save_blob(data)
    license_info = meta.get("license") if isinstance(meta.get("license"), dict) else {}
    snapshot = {
        "owner": owner, "repo": name, "default_branch": branch, "commit": commit,
        "tarball_sha256": digest, "tarball_bytes": len(data),
        "language": str(meta.get("language") or "")[:40],
        "license": str((license_info or {}).get("spdx_id") or "")[:40],
        "fetched_at": store.iso(),
    }
    return snapshot, data


# ── tree ─────────────────────────────────────────────────────────────────────

@dataclass
class Tree:
    files: Dict[str, bytes] = field(default_factory=dict)
    executable: set = field(default_factory=set)
    skipped: Dict[str, int] = field(default_factory=dict)

    def text(self, path: str) -> Optional[str]:
        data = self.files.get(path)
        if data is None or b"\x00" in data[:8192]:
            return None
        try:
            return data.decode("utf-8")
        except UnicodeDecodeError:
            return None


def normalize_path(raw: str) -> Optional[str]:
    """A safe relative POSIX path, or None."""
    if not raw or "\x00" in raw or "\\" in raw or raw.startswith("/") or len(raw) > 240:
        return None
    path = posixpath.normpath(raw)
    if path in (".", "") or path.startswith("../") or path == ".." or path != raw.rstrip("/"):
        return None
    parts = path.split("/")
    if any(part in ("", ".", "..") or not re.fullmatch(r"[A-Za-z0-9._@+=,-]+", part) for part in parts):
        return None
    return path


def _skip(stats: Dict[str, int], reason: str) -> None:
    stats[reason] = stats.get(reason, 0) + 1


def load_tree(data: bytes) -> Tree:
    """Read the tarball in memory. The top-level ``<repo>-<sha>/`` folder that
    codeload adds is stripped; anything that is not a plain file is skipped."""
    tree = Tree()
    total = 0
    try:
        archive = tarfile.open(fileobj=io.BytesIO(data), mode="r:gz")
    except (tarfile.TarError, OSError, EOFError) as exc:
        raise RepoError("архив репозитория повреждён") from exc
    with archive:
        try:
            members = archive.getmembers()
        except (tarfile.TarError, OSError, EOFError) as exc:
            raise RepoError("архив репозитория повреждён") from exc
        if len(members) > MAX_TREE_FILES * 2:
            raise RepoError(f"в репозитории больше {MAX_TREE_FILES} файлов")
        for member in members:
            name = member.name.split("/", 1)[1] if "/" in member.name else ""
            if not name or member.isdir():
                continue
            if not member.isreg():
                _skip(tree.skipped, "not_regular")
                continue
            path = normalize_path(name)
            if path is None:
                _skip(tree.skipped, "bad_path")
                continue
            if path.split("/")[0] == ".git":
                continue
            if member.size > MAX_MEMBER_BYTES:
                _skip(tree.skipped, "too_large")
                continue
            if len(tree.files) >= MAX_TREE_FILES:
                raise RepoError(f"в репозитории больше {MAX_TREE_FILES} файлов")
            total += member.size
            if total > MAX_TREE_BYTES:
                raise RepoError(f"распакованный репозиторий больше {MAX_TREE_BYTES // (1024 * 1024)} МБ")
            handle = archive.extractfile(member)
            content = handle.read(member.size + 1) if handle else b""
            if len(content) != member.size:
                raise RepoError("архив репозитория повреждён")
            tree.files[path] = content
            if member.mode & 0o111:
                tree.executable.add(path)
    if not tree.files:
        raise RepoError("в архиве репозитория нет файлов")
    return tree


_CODE_SUFFIXES = (".py", ".js", ".ts", ".tsx", ".go", ".rs", ".java", ".kt", ".rb", ".php", ".c", ".cc", ".cpp",
                  ".cs", ".swift", ".scala")


def is_python_repo(tree: Tree, snapshot: Dict[str, Any]) -> bool:
    if (snapshot.get("language") or "").lower() == "python":
        return True
    code = [path for path in tree.files if path.endswith(_CODE_SUFFIXES) and not _vendored(path)]
    python = [path for path in code if path.endswith(".py")]
    return len(python) >= 3 and len(python) * 10 >= len(code) * 6


def _vendored(path: str) -> bool:
    return any(part in SKIP_DIRS for part in path.split("/")[:-1])


# ── AI contribution policy ──────────────────────────────────────────────────

_POLICY_FILES = re.compile(
    r"^(\.github/|docs/)?(CONTRIBUTING|AGENTS|CLAUDE|CODE_OF_CONDUCT|AI[_-]?POLICY|PULL_REQUEST_TEMPLATE|README)"
    r"(\.[A-Za-z]+)?$", re.I)
_AI = r"(?:AI|A\.I\.|LLMs?|large language models?|generative|ChatGPT|Copilot|Claude|GPT|coding (?:agents?|assistants?)|bots?)"
_FORBIDDEN = [
    re.compile(rf"\b(?:do not|don't|will not|won't|cannot|can't|not)\s+accept\b[^.\n]{{0,100}}\b{_AI}", re.I),
    re.compile(rf"\b{_AI}[\s-]*(?:generated|written|authored|assisted|created)\b[^.\n]{{0,80}}"
               r"\b(?:not (?:be )?(?:accepted|allowed|welcome|permitted)|prohibited|banned|forbidden|rejected|"
               r"(?:will be|are) closed)", re.I),
    re.compile(rf"\b(?:no|ban(?:ned)?(?: on)?|zero[- ]tolerance (?:for|on))\s+{_AI}[\s-]*(?:generated |assisted |written )?"
               r"(?:code|contributions?|PRs?|pull requests?|patches)\b", re.I),
    re.compile(rf"\b{_AI}\b[^.\n]{{0,40}}\b(?:contributions?|PRs?|pull requests?)\b[^.\n]{{0,40}}"
               r"\b(?:are|is) (?:not (?:accepted|allowed|welcome)|prohibited|banned|forbidden)", re.I),
]
_DISCLOSURE = re.compile(rf"\b(?:disclose|disclosure|declare|mention)\b[^.\n]{{0,80}}\b{_AI}|"
                         rf"\b{_AI}\b[^.\n]{{0,60}}\b(?:must|should) be (?:disclosed|declared|mentioned)", re.I)


def ai_policy(tree: Tree) -> Dict[str, Any]:
    """``forbidden`` when the repository's own guidance refuses AI-generated
    contributions, ``disclosure`` when it asks for them to be declared."""
    evidence: List[Dict[str, str]] = []
    status = "unknown"
    for path in sorted(tree.files):
        if not _POLICY_FILES.match(path):
            continue
        text = tree.text(path) or ""
        for line in text.splitlines():
            snippet = " ".join(line.split())[:240]
            if any(pattern.search(line) for pattern in _FORBIDDEN):
                status = "forbidden"
                evidence.append({"path": path, "text": snippet, "kind": "forbidden"})
            elif _DISCLOSURE.search(line):
                if status == "unknown":
                    status = "disclosure"
                evidence.append({"path": path, "text": snippet, "kind": "disclosure"})
            if len(evidence) >= 6:
                break
    return {"status": status, "evidence": evidence[:6]}


# ── test plan ────────────────────────────────────────────────────────────────

_REQ_NAME = re.compile(r"^\s*([A-Za-z0-9][A-Za-z0-9._-]{0,80})")


def _requirement_names(lines: Iterable[str]) -> List[str]:
    names: List[str] = []
    for raw in lines:
        line = str(raw).split("#", 1)[0].strip()
        if not line or line.startswith(("-", "git+", "http")):
            continue
        match = _REQ_NAME.match(line)
        if match:
            name = match.group(1).lower().replace("_", "-")
            if name not in names:
                names.append(name)
    return names


def declared_dependencies(tree: Tree) -> List[str]:
    """Names only, read as data (tomllib, plain text) — never by running
    setup.py or a build backend."""
    import tomllib

    names: List[str] = []
    for path in sorted(tree.files):
        base = path.rsplit("/", 1)[-1].lower()
        if "/" in path and not path.startswith(("requirements/", "requirements-")):
            continue
        if re.fullmatch(r"(requirements|requirements[-_.][a-z0-9_-]+|test[-_]requirements|dev[-_]requirements)\.txt", base):
            names.extend(n for n in _requirement_names((tree.text(path) or "").splitlines()) if n not in names)
    pyproject = tree.text("pyproject.toml")
    if pyproject:
        try:
            data = tomllib.loads(pyproject)
        except (tomllib.TOMLDecodeError, ValueError):
            data = {}
        project = data.get("project") if isinstance(data.get("project"), dict) else {}
        groups = [project.get("dependencies") or []]
        optional = project.get("optional-dependencies") if isinstance(project.get("optional-dependencies"), dict) else {}
        groups += [value for key, value in optional.items() if key in ("test", "tests", "testing", "dev")]
        dependency_groups = data.get("dependency-groups") if isinstance(data.get("dependency-groups"), dict) else {}
        groups += [value for key, value in dependency_groups.items() if key in ("test", "tests", "testing", "dev")]
        for group in groups:
            if isinstance(group, list):
                names.extend(n for n in _requirement_names(item for item in group if isinstance(item, str))
                             if n not in names)
    return names[:60]


def test_plan(tree: Tree) -> Dict[str, Any]:
    """Which fixed runner to use — chosen by this code from the tree, never
    a command the repository or the model wrote."""
    paths = set(tree.files)
    pyproject = tree.text("pyproject.toml") or ""
    setup_cfg = tree.text("setup.cfg") or ""
    tox_ini = tree.text("tox.ini") or ""
    uses_pytest = (
        "pytest.ini" in paths or "[tool.pytest" in pyproject or "[tool:pytest]" in setup_cfg or "[pytest]" in tox_ini
        or any(path.rsplit("/", 1)[-1] == "conftest.py" for path in paths)
        or any(path.endswith(".py") and "test" in path and "import pytest" in (tree.text(path) or "")[:4000]
               for path in sorted(paths)[:3000])
    )
    pythonpath = ["src", "."] if any(path.startswith("src/") and path.endswith(".py") for path in paths) else ["."]
    return {
        "runner": "pytest" if uses_pytest else "unittest",
        "pythonpath": pythonpath,
        "dependencies": declared_dependencies(tree),
    }


# ── context for the model ────────────────────────────────────────────────────

_IDENT = re.compile(r"`([^`\n]{3,80})`|\b([A-Z][a-z0-9]+(?:[A-Z][a-z0-9]+)+|[a-z][a-z0-9]*(?:_[a-z0-9]+)+|"
                    r"[a-z][a-z0-9_]*\.[a-z][a-z0-9_.]*)\b")
_COMMON = {"self", "none", "true", "false", "return", "import", "python", "issue", "github", "error", "value"}


def issue_terms(text: str) -> List[str]:
    terms: List[str] = []
    for backtick, word in _IDENT.findall(text or ""):
        for term in re.split(r"[\s(),:]+", backtick or word):
            term = term.strip(".:;'\"")
            if 3 <= len(term) <= 80 and term.lower() not in _COMMON and term not in terms:
                terms.append(term)
    return terms[:40]


def listing(tree: Tree, limit: int = LISTING_LIMIT) -> List[str]:
    paths = sorted((p for p in tree.files if not _vendored(p)), key=lambda p: (p.count("/"), p))
    return paths[:limit]


def _window(text: str, needles: List[str], radius: int = 40, max_windows: int = 3) -> str:
    lines = text.splitlines()
    hits: List[int] = []
    for number, line in enumerate(lines):
        if any(needle in line for needle in needles):
            if not hits or number > hits[-1] + radius:
                hits.append(number)
            if len(hits) >= max_windows:
                break
    if not hits:
        hits = [0]
    chunks = []
    for hit in hits:
        start, end = max(0, hit - radius), min(len(lines), hit + radius)
        body = "\n".join(lines[start:end])
        chunks.append(f"[lines {start + 1}-{end} of {len(lines)}]\n{body}")
    return "\n...\n".join(chunks)


def select_context(tree: Tree, issue_text: str, picked: Optional[List[str]] = None,
                   budget: Optional[int] = None, max_files: int = 10,
                   whole_file_limit: int = 16 * 1024) -> Dict[str, Any]:
    """Score text files by relevance to the issue and fill a byte budget.
    Small files go in whole; a large relevant file goes in as windows around
    the matching lines, marked ``partial`` with their line numbers."""
    budget = budget or context_budget()
    terms = issue_terms(issue_text)
    lowered = (issue_text or "").lower()
    scores: Dict[str, float] = {}
    for path in tree.files:
        if _vendored(path) or not path.endswith(TEXT_SUFFIXES):
            continue
        text = tree.text(path)
        if text is None:
            continue
        base = path.rsplit("/", 1)[-1]
        score = 0.0
        if path.lower() in lowered:
            score += 120
        elif len(base) > 4 and base.lower() in lowered:
            score += 60
        if picked and path in picked:
            score += 200 - picked.index(path)
        for term in terms:
            if term in path:
                score += 20
            count = text.count(term)
            if count:
                score += min(count, 10) * (3 if "." in term or "_" in term or term[:1].isupper() else 1)
        if score and path.endswith(".py"):
            score *= 1.3
        if score:
            scores[path] = score
    ranked = sorted(scores, key=lambda p: (-scores[p], p))
    # Pull in the tests of the top source files: the Builder should extend them.
    for path in list(ranked[:4]):
        stem = path.rsplit("/", 1)[-1][:-3] if path.endswith(".py") else ""
        if stem and not stem.startswith("test"):
            for candidate in tree.files:
                name = candidate.rsplit("/", 1)[-1]
                if name in (f"test_{stem}.py", f"{stem}_test.py") and candidate not in ranked:
                    ranked.insert(ranked.index(path) + 1, candidate)
    if "README.md" in tree.files and "README.md" not in ranked:
        ranked.append("README.md")
    chosen: List[Dict[str, Any]] = []
    used = 0
    for path in ranked:
        if len(chosen) >= max_files:
            break
        text = tree.text(path) or ""
        size = len(text.encode("utf-8"))
        partial = size > whole_file_limit
        content = _window(text, terms or [path.rsplit("/", 1)[-1]]) if partial else text
        if path == "README.md" and partial:
            content = text[:4000]
        cost = len(content.encode("utf-8"))
        if used + cost > budget:
            continue
        used += cost
        chosen.append({"path": path, "content": content, "partial": partial, "bytes": size})
    return {"files": chosen, "listing": listing(tree), "bytes": used, "terms": terms}


# ── unified diff ─────────────────────────────────────────────────────────────

@dataclass
class Hunk:
    old_start: int
    lines: List[str]  # each starts with " ", "+" or "-"

    @property
    def old(self) -> List[str]:
        return [line[1:] for line in self.lines if line[0] in " -"]

    @property
    def new(self) -> List[str]:
        return [line[1:] for line in self.lines if line[0] in " +"]


@dataclass
class FilePatch:
    path: str
    is_new: bool
    hunks: List[Hunk] = field(default_factory=list)
    no_newline_at_end: bool = False


_HUNK = re.compile(r"^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@")
_FORBIDDEN_TOP = {".git", ".github", ".gitlab-ci.yml", ".circleci", ".travis.yml"}
_REFUSED_HEADERS = ("deleted file mode", "rename from", "rename to", "copy from", "copy to", "similarity index",
                    "old mode", "new mode", "Binary files", "GIT binary patch", "dissimilarity index")


def _diff_path(raw: str, prefix: str) -> Optional[str]:
    value = raw.split("\t", 1)[0].strip()
    if value == "/dev/null":
        return "/dev/null"
    if value.startswith(prefix):
        value = value[len(prefix):]
    return normalize_path(value)


def strip_fences(text: str) -> str:
    stripped = (text or "").strip("\n")
    match = re.match(r"^\s*```[a-z]*\n(.*?)\n```\s*$", stripped, re.S)
    return match.group(1) if match else stripped


def parse_diff(text: str) -> List[FilePatch]:
    text = strip_fences(text)
    if not text.strip():
        raise PatchError("Builder вернул пустой diff")
    if len(text.encode("utf-8")) > MAX_DIFF_BYTES:
        raise PatchError(f"diff больше {MAX_DIFF_BYTES // 1024} КБ")
    lines = text.replace("\r\n", "\n").split("\n")
    patches: List[FilePatch] = []
    current: Optional[FilePatch] = None
    hunk: Optional[Hunk] = None
    index = 0
    while index < len(lines):
        line = lines[index]
        if line.startswith(_REFUSED_HEADERS):
            raise PatchError(f"недопустимая операция в diff: {line[:60]!r} (разрешены только правки и новые файлы)")
        if line.startswith("new file mode") and line.split()[-1] != "100644":
            raise PatchError("новые файлы — только обычные (mode 100644)")
        if line.startswith("--- "):
            if index + 1 >= len(lines) or not lines[index + 1].startswith("+++ "):
                raise PatchError("заголовок файла в diff без строки +++")
            old = _diff_path(line[4:], "a/")
            new = _diff_path(lines[index + 1][4:], "b/")
            if new in (None, "/dev/null"):
                raise PatchError("удаление файлов и недопустимые пути в diff запрещены")
            if old is None or (old != "/dev/null" and old != new):
                raise PatchError(f"переименование или недопустимый путь в diff: {line[4:80]!r}")
            top = new.split("/")[0]
            if top in _FORBIDDEN_TOP or any(part.startswith(".git") for part in new.split("/")):
                raise PatchError(f"{new}: правки CI и служебных файлов git запрещены")
            current = FilePatch(path=new, is_new=old == "/dev/null")
            if any(p.path == new for p in patches):
                raise PatchError(f"{new} встречается в diff дважды")
            patches.append(current)
            hunk = None
            index += 2
            continue
        match = _HUNK.match(line)
        if match:
            if current is None:
                raise PatchError("блок @@ до заголовка файла")
            hunk = Hunk(old_start=int(match.group(1)), lines=[])
            current.hunks.append(hunk)
            if len(current.hunks) > MAX_HUNKS_PER_FILE:
                raise PatchError(f"{current.path}: слишком много блоков изменений")
            index += 1
            continue
        if hunk is not None and current is not None:
            if line.startswith("\\"):
                if hunk.lines and hunk.lines[-1][0] in " +":
                    current.no_newline_at_end = True
            elif line[:1] in (" ", "+", "-"):
                hunk.lines.append(line)
            elif line == "":
                # Models often drop the leading space of a blank context line.
                if index + 1 < len(lines) and (lines[index + 1][:1] in (" ", "+", "-", "\\") or lines[index + 1] == ""):
                    hunk.lines.append(" ")
            elif line.startswith(("diff --git", "index ")):
                hunk = None
            else:
                raise PatchError(f"непонятная строка в diff: {line[:60]!r}")
        elif line.strip() and not line.startswith(("diff --git", "index ", "new file mode")):
            raise PatchError(f"текст вне блоков diff: {line[:60]!r}")
        index += 1
    if not patches:
        raise PatchError("в ответе Builder нет ни одного файла в формате unified diff")
    if len(patches) > MAX_DIFF_FILES:
        raise PatchError(f"diff затрагивает больше {MAX_DIFF_FILES} файлов")
    for patch in patches:
        if not patch.hunks or not any(any(l[0] in "+-" for l in h.lines) for h in patch.hunks):
            raise PatchError(f"{patch.path}: в diff нет изменений")
    return patches


def _find(lines: List[str], block: List[str], expected: int, start: int) -> int:
    if not block:
        return max(start, min(expected, len(lines)))
    limit = len(lines) - len(block)
    order = sorted(range(start, limit + 1), key=lambda pos: (abs(pos - expected), pos))
    for loose in (False, True):
        for pos in order:
            window = lines[pos:pos + len(block)]
            if window == block or (loose and [w.rstrip() for w in window] == [b.rstrip() for b in block]):
                return pos
    return -1


def apply_patches(tree: Tree, patches: List[FilePatch]) -> Dict[str, str]:
    """New full contents of every touched file. Pure text manipulation."""
    result: Dict[str, str] = {}
    for patch in patches:
        if patch.is_new:
            if patch.path in tree.files:
                raise PatchError(f"{patch.path} уже существует — нужен diff правки, а не нового файла")
            body = [line for h in patch.hunks for line in h.new]
            result[patch.path] = "\n".join(body) + ("" if patch.no_newline_at_end else "\n")
            continue
        original = tree.text(patch.path)
        if original is None:
            raise PatchError(f"{patch.path}: такого текстового файла нет в репозитории")
        newline = "\r\n" if "\r\n" in original else "\n"
        trailing = original.endswith(("\n", "\r\n"))
        lines = original.replace("\r\n", "\n").split("\n")
        if trailing:
            lines.pop()
        offset = 0
        cursor = 0
        for number, h in enumerate(patch.hunks, 1):
            position = _find(lines, h.old, h.old_start - 1 + offset, cursor)
            if position < 0:
                raise PatchError(f"{patch.path}: блок {number} (около строки {h.old_start}) не совпадает с файлом")
            # Context lines keep the file's own text: a loose (whitespace)
            # match must not rewrite lines the change does not touch.
            original_block = iter(lines[position:position + len(h.old)])
            segment: List[str] = []
            for line in h.lines:
                if line[0] == " ":
                    segment.append(next(original_block))
                elif line[0] == "-":
                    next(original_block)
                else:
                    segment.append(line[1:])
            lines[position:position + len(h.old)] = segment
            h_new_len = len(segment)
            offset += h_new_len - len(h.old)
            cursor = position + h_new_len
        text = newline.join(lines)
        if lines and (trailing and not patch.no_newline_at_end):
            text += newline
        if text == original:
            raise PatchError(f"{patch.path}: diff не меняет файл")
        result[patch.path] = text
    for path, text in result.items():
        if path.endswith(".py"):
            original = tree.text(path)
            try:
                ast.parse(text, filename=path)
            except SyntaxError as exc:
                if original is None or _parses(original):
                    raise PatchError(f"после правки {path} не разбирается Python: строка {exc.lineno}") from exc
    return result


def _parses(text: str) -> bool:
    try:
        ast.parse(text)
        return True
    except SyntaxError:
        return False


def render_diff(tree: Tree, changed: Dict[str, str]) -> str:
    """A canonical diff of the applied result (difflib), so what the operator
    approves and what the PR carries is exactly the applied change — not the
    model's possibly sloppy hunk headers."""
    import difflib

    chunks: List[str] = []
    for path in sorted(changed):
        before = tree.text(path)
        after = changed[path]
        old_lines = [] if before is None else before.splitlines(keepends=True)
        new_lines = after.splitlines(keepends=True)
        diff = list(difflib.unified_diff(old_lines, new_lines,
                                         fromfile="/dev/null" if before is None else f"a/{path}",
                                         tofile=f"b/{path}", n=3))
        for line in diff:
            chunks.append(line if line.endswith("\n") else line + "\n\\ No newline at end of file\n")
    return "".join(chunks)


# ── what the operator should look at ─────────────────────────────────────────

_RISKY_ADDED = [
    (re.compile(r"\bsubprocess\b|\bos\.(system|popen|exec\w*|spawn\w*)\b"), "запуск процессов"),
    (re.compile(r"\b(eval|exec|compile|__import__)\s*\("), "динамическое исполнение кода"),
    (re.compile(r"\b(socket|urllib|http\.client|requests\.(get|post|put)|httpx\.|aiohttp)\b"), "сетевой код"),
    (re.compile(r"\bpickle\.loads?\b|\bmarshal\.loads\b|\byaml\.load\((?![^)]*SafeLoader)"), "небезопасная десериализация"),
    (re.compile(r"\bbase64\.b64decode\b|[A-Za-z0-9+/=]{160,}"), "закодированные данные"),
    (re.compile(r"\bos\.environ\b|\bgetenv\(|\b(token|secret|password|api_key)\b", re.I), "секреты или окружение"),
]
_SENSITIVE_FILES = re.compile(r"(^|/)(setup\.py|setup\.cfg|pyproject\.toml|requirements[^/]*\.txt|conftest\.py|"
                              r"Makefile|tox\.ini|noxfile\.py|MANIFEST\.in|Dockerfile|\.pre-commit-config\.yaml)$")
_URL = re.compile(r"https?://([A-Za-z0-9.-]+)")


def diff_report(tree: Tree, patches: List[FilePatch], changed: Dict[str, str]) -> Dict[str, Any]:
    added = removed = 0
    risks: List[str] = []
    corpus_hosts = None
    for patch in patches:
        for h in patch.hunks:
            for line in h.lines:
                if line[0] == "+":
                    added += 1
                    for pattern, label in _RISKY_ADDED:
                        if pattern.search(line):
                            risks.append(f"{patch.path}: {label}: {line[1:].strip()[:120]}")
                            break
                    for host in _URL.findall(line):
                        if corpus_hosts is None:
                            corpus_hosts = {h.lower() for p in tree.files if p.endswith(TEXT_SUFFIXES)
                                            for h in _URL.findall(tree.text(p) or "")}
                        if host.lower() not in corpus_hosts:
                            risks.append(f"{patch.path}: новый внешний адрес {host}")
                elif line[0] == "-":
                    removed += 1
        if _SENSITIVE_FILES.search(patch.path):
            risks.append(f"{patch.path}: правка сборки, зависимостей или конфигурации тестов")
    tests = [path for path in changed if re.search(r"(^|/)(tests?/|test_[^/]*\.py$|[^/]*_test\.py$)", path)]
    return {"files_changed": len(changed), "added": added, "removed": removed,
            "new_files": sorted(p.path for p in patches if p.is_new), "tests_touched": sorted(tests),
            "risks": risks[:30]}


def file_entries(tree: Tree, changed: Dict[str, str]) -> List[Dict[str, Any]]:
    return [{"path": path, "content": changed[path], "mode": "100755" if path in tree.executable else "100644"}
            for path in sorted(changed)]


def overlay_matches(tree: Tree, diff: str, files: List[Dict[str, Any]]) -> bool:
    """Re-apply the stored diff to the stored snapshot: the files about to be
    executed or published must be exactly what that diff produces."""
    try:
        changed = apply_patches(tree, parse_diff(diff))
    except PatchError:
        return False
    return changed == {f["path"]: f["content"] for f in files}
