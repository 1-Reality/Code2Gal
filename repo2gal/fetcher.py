"""GitHub 采集适配层（Chronicle 模式）。

GitHub 的认证、分页、限流、重试、GraphQL、Discussion、wiki 与增量备份
全部委托给成熟项目 ``josegonzalez/python-github-backup``。本模块只做两件事：

1. 以 subprocess 调用 ``github-backup``；
2. 把其落盘的 Git 仓库和 JSON 归一化成 RepoContext。

项目明确禁止在已有成熟开源实现时自造 GitHub API 客户端。
"""

from __future__ import annotations

import dataclasses
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable

import requests

from .errors import FetchError, UsageError


GITHUB_REST_API = "https://api.github.com"


@dataclass
class Comment:
    author: str
    body: str


@dataclass
class Thread:
    """一条 Issue、PR 或 Discussion，连同它的讨论。"""

    number: int
    title: str
    kind: str  # "issue" | "pr" | "discussion"
    state: str
    author: str
    created_at: str
    comment_count: int
    body: str
    comments: list[Comment] = field(default_factory=list)


@dataclass
class Release:
    tag: str
    name: str
    published_at: str
    body: str


@dataclass
class Contributor:
    login: str
    contributions: int


@dataclass
class SourceSnapshot:
    """送入 LLM 的仓库文本快照。

    role 可区分 repository-tree/current-source/current-text/license/history。
    """

    path: str
    role: str
    content: str


@dataclass
class RepoContext:
    """喂给 LLM 的结构化上下文。"""

    owner: str
    name: str
    description: str
    language: str
    stars: int
    created_at: str
    topics: list[str] = field(default_factory=list)
    readme_excerpt: str = ""
    wiki_excerpt: str = ""
    contributors: list[Contributor] = field(default_factory=list)
    releases: list[Release] = field(default_factory=list)
    threads: list[Thread] = field(default_factory=list)
    source_snapshots: list[SourceSnapshot] = field(default_factory=list)
    backup_dir: str = ""

    @property
    def full_name(self) -> str:
        return f"{self.owner}/{self.name}"

    def to_dict(self) -> dict[str, Any]:
        return dataclasses.asdict(self)


def parse_repo(url: str) -> tuple[str, str]:
    """从 URL 或 owner/repo 里解出 (owner, repo)。"""
    text = url.strip().removesuffix(".git")
    match = re.search(r"github\.com[/:]([^/]+)/([^/]+)", text)
    if match:
        return match.group(1), match.group(2)
    parts = text.split("/")
    if len(parts) == 2 and all(parts):
        return parts[0], parts[1]
    raise UsageError(f"无法解析仓库标识：{url!r}（期望 owner/repo 或 GitHub URL）")


# 不使用 --all：上游的 --all 会额外下载 Release 二进制并读取 hooks，可能需要
# 高权限 token，也可能意外拉取数十 GB。这里显式列出生成叙事真正需要的全量数据。
def _resolve_repo_backup_dir(backup_root: Path, repo: str) -> Path:
    """兼容 GitHub 仓库名大小写归一化。

    GitHub URL/REST 对仓库名大小写不敏感，但 Linux 文件系统敏感；
    python-github-backup 会按 GitHub 返回的 canonical name 落盘。
    """
    repositories = Path(backup_root).resolve() / "repositories"
    exact = repositories / repo
    if exact.exists() or not repositories.is_dir():
        return exact

    matches = [
        path
        for path in repositories.iterdir()
        if path.is_dir() and path.name.casefold() == repo.casefold()
    ]
    return matches[0] if len(matches) == 1 else exact


NARRATIVE_BACKUP_FLAGS = (
    "--repositories",
    "--issues",
    "--issue-comments",
    "--issue-events",
    "--issue-timeline",
    "--pulls",
    "--pull-comments",
    "--pull-reviews",
    "--pull-commits",
    "--pull-details",
    "--discussions",
    "--wikis",
    "--releases",
    "--labels",
    "--milestones",
    "--fork",
)


def run_backup(
    owner: str,
    repo: str,
    backup_root: Path,
    *,
    token: str | None = None,
    organization: bool = False,
    incremental: bool = True,
    log=lambda _msg: None,
    progress=lambda _msg: None,
) -> Path:
    """调用 python-github-backup，返回该仓库的备份目录。

    Token 通过权限为 0600 的临时文件传递，避免出现在进程列表或 shell history。
    Discussion 使用 GraphQL，完整采集必须有 token，因此本适配层直接要求认证。
    """
    if not token:
        raise FetchError(
            "python-github-backup 需要 GitHub Token；请设置 GITHUB_TOKEN，"
            "或用 --reuse-backup 读取已有备份"
        )

    sibling = Path(sys.executable).with_name("github-backup")
    executable = str(sibling) if sibling.is_file() else shutil.which("github-backup")
    if not executable:
        raise FetchError("找不到 github-backup，请执行 pip install github-backup")

    backup_root = Path(backup_root).resolve()
    backup_root.mkdir(parents=True, exist_ok=True)
    repo_dir = _resolve_repo_backup_dir(backup_root, repo)

    command = [
        executable,
        owner,
        "--output-directory",
        str(backup_root),
        "--repository",
        repo,
        *NARRATIVE_BACKUP_FLAGS,
    ]
    if organization:
        command.append("--organization")
    command.append("--private")
    if incremental and repo_dir.exists():
        command.append("--incremental")

    token_file: str | None = None
    try:
        with tempfile.NamedTemporaryFile("w", delete=False, encoding="utf-8") as handle:
            handle.write(token)
            token_file = handle.name
        os.chmod(token_file, 0o600)
        # GitHub Actions' built-in GITHUB_TOKEN is a GitHub App installation
        # token (normally ghs_*), not a user PAT. python-github-backup otherwise
        # probes GET /user and receives 401, so explicitly use its app-auth mode.
        if token.startswith("ghs_"):
            command.append("--as-app")
            option = "--token"
        else:
            option = "--token-fine" if token.startswith("github_pat_") else "--token"
        command.extend((option, Path(token_file).as_uri()))

        log(f"调用 python-github-backup 采集 {owner}/{repo}")
        process = subprocess.Popen(
            command,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            bufsize=1,
        )
        output: list[str] = []
        if process.stdout:
            for raw_line in process.stdout:
                line = raw_line.strip()
                if line:
                    output.append(line)
                    progress(line)
        returncode = process.wait()
        if returncode:
            message = output[-1] if output else "无错误详情"
            raise FetchError(f"github-backup 退出码 {returncode}：{message}")
    finally:
        if token_file:
            Path(token_file).unlink(missing_ok=True)

    repo_dir = _resolve_repo_backup_dir(backup_root, repo)
    if not repo_dir.exists():
        raise FetchError(f"github-backup 未产出预期目录：{repo_dir}")
    log(f"原始备份已保存：{repo_dir}")
    return repo_dir


def fetch_repository_metadata(
    owner: str,
    repo: str,
    token: str,
    *,
    log=lambda _msg: None,
) -> dict[str, Any]:
    """通过官方 GitHub REST API 补齐上游不落盘的仓库概览。

    这是仓库数据采集模块唯一允许的直接网络补充。URL 固定为
    ``api.github.com/repos/{owner}/{repo}``；不得改成抓取 GitHub HTML 页面。
    失败时保留 github-backup 主流程，不把非关键元数据升级为致命错误。
    """
    log("通过官方 GitHub REST API 获取仓库概览")
    try:
        response = requests.get(
            f"{GITHUB_REST_API}/repos/{owner}/{repo}",
            headers={
                "Accept": "application/vnd.github+json",
                "Authorization": f"Bearer {token}",
                "X-GitHub-Api-Version": "2022-11-28",
                "User-Agent": "Repo2Gal",
            },
            timeout=30,
        )
    except requests.RequestException as exc:
        log(f"仓库概览获取失败（{exc}），继续使用备份数据")
        return {}
    if not response.ok:
        log(f"仓库概览获取失败（HTTP {response.status_code}），继续使用备份数据")
        return {}
    try:
        data = response.json()
    except ValueError:
        log("仓库概览响应不是合法 JSON，继续使用备份数据")
        return {}
    return data if isinstance(data, dict) else {}


def _metadata_path(repo_backup_dir: Path) -> Path:
    return repo_backup_dir / "repo2gal-repository.json"


def _read_metadata(repo_backup_dir: Path) -> dict[str, Any]:
    path = _metadata_path(repo_backup_dir)
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return {}
    return data if isinstance(data, dict) else {}


def _clean_body(text: str | None, limit: int) -> str:
    """压掉 Markdown 噪声并限制单段长度。"""
    if not text:
        return ""
    text = re.sub(r"```.*?```", "[代码块]", text, flags=re.S)
    text = re.sub(r"<!--.*?-->", "", text, flags=re.S)
    text = re.sub(r"<[^>]+>", "", text)
    text = re.sub(r"!\[[^\]]*\]\([^)]*\)", "", text)
    text = re.sub(r"\[([^\]]*)\]\([^)]*\)", r"\1", text)
    text = re.sub(r"https?://\S+", "", text)
    text = re.sub(r"[ \t]{2,}", " ", text)
    text = re.sub(r"\n{3,}", "\n\n", text).strip()
    return text[:limit] + "…" if len(text) > limit else text


def _load_json_files(directory: Path) -> Iterable[dict[str, Any]]:
    if not directory.is_dir():
        return []
    values: list[dict[str, Any]] = []
    for path in sorted(directory.glob("*.json")):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError):
            continue
        if isinstance(data, dict):
            values.append(data)
    return values


def _login(actor: Any) -> str:
    if isinstance(actor, dict):
        return actor.get("login") or actor.get("name") or "unknown"
    return "unknown"


def _comments(items: Iterable[dict[str, Any]], limit: int = 20) -> list[Comment]:
    result: list[Comment] = []
    for item in items:
        body = _clean_body(item.get("body") or item.get("bodyText"), 500)
        if body:
            result.append(Comment(author=_login(item.get("user") or item.get("author")), body=body))
        for reply in item.get("reply_data") or []:
            reply_body = _clean_body(reply.get("body") or reply.get("bodyText"), 500)
            if reply_body:
                result.append(Comment(author=_login(reply.get("author")), body=reply_body))
        if len(result) >= limit:
            break
    return result[:limit]


def _thread_from_issue(data: dict[str, Any]) -> Thread:
    comments = _comments(data.get("comment_data") or [])
    return Thread(
        number=data.get("number", 0),
        title=data.get("title") or "（无标题）",
        kind="issue",
        state=data.get("state") or "unknown",
        author=_login(data.get("user")),
        created_at=(data.get("created_at") or "")[:10],
        comment_count=max(data.get("comments", 0), len(comments)),
        body=_clean_body(data.get("body"), 800),
        comments=comments,
    )


def _thread_from_pull(data: dict[str, Any]) -> Thread:
    raw_comments = [
        *(data.get("comment_regular_data") or []),
        *(data.get("comment_data") or []),
        *(data.get("review_data") or []),
    ]
    comments = _comments(raw_comments)
    count = data.get("comments", 0) + data.get("review_comments", 0)
    return Thread(
        number=data.get("number", 0),
        title=data.get("title") or "（无标题）",
        kind="pr",
        state=data.get("state") or "unknown",
        author=_login(data.get("user")),
        created_at=(data.get("created_at") or "")[:10],
        comment_count=max(count, len(comments)),
        body=_clean_body(data.get("body"), 800),
        comments=comments,
    )


def _thread_from_discussion(data: dict[str, Any]) -> Thread:
    comments = _comments(data.get("comment_data") or [])
    return Thread(
        number=data.get("number", 0),
        title=data.get("title") or "（无标题）",
        kind="discussion",
        state="closed" if data.get("closed") else "open",
        author=_login(data.get("author")),
        created_at=(data.get("createdAt") or "")[:10],
        comment_count=max(data.get("comment_count", 0), len(comments)),
        body=_clean_body(data.get("body") or data.get("bodyText"), 800),
        comments=comments,
    )


def _read_text_candidates(directory: Path, names: tuple[str, ...], limit: int) -> str:
    if not directory.is_dir():
        return ""
    lower_names = {name.lower() for name in names}
    reference, files = _git_files(directory)
    for name in files:
        if "/" not in name and name.lower() in lower_names:
            text = _git_output(directory, "show", f"{reference}:{name}")
            if text:
                return _clean_body(text, limit)
    for path in directory.iterdir():
        if path.is_file() and path.name.lower() in lower_names:
            try:
                return _clean_body(path.read_text(encoding="utf-8"), limit)
            except (OSError, UnicodeDecodeError):
                pass
    return ""


def _read_wiki(directory: Path, limit: int = 3000) -> str:
    if not directory.is_dir():
        return ""
    chunks: list[str] = []
    reference, git_paths = _git_files(directory)
    paths = git_paths or [
        str(path.relative_to(directory))
        for path in sorted(directory.rglob("*.md"))
        if ".git" not in path.parts
    ]
    for relative in paths:
        if not relative.lower().endswith(".md"):
            continue
        if reference:
            raw = _git_output(directory, "show", f"{reference}:{relative}")
        else:
            try:
                raw = (directory / relative).read_text(encoding="utf-8")
            except (OSError, UnicodeDecodeError):
                continue
        text = _clean_body(raw, 1000)
        if text:
            chunks.append(f"### {Path(relative).stem}\n{text}")
        if sum(map(len, chunks)) >= limit:
            break
    return _clean_body("\n\n".join(chunks), limit)


def _git_output(repo_dir: Path, *args: str) -> str:
    if not (repo_dir / ".git").exists():
        return ""
    result = subprocess.run(
        ["git", "-C", str(repo_dir), *args], text=True, capture_output=True, check=False
    )
    return result.stdout.strip() if result.returncode == 0 else ""


def _git_files(repo_dir: Path) -> tuple[str, list[str]]:
    """返回最新远端引用及其文件列表，避免读取增量备份中的陈旧工作树。"""
    if not (repo_dir / ".git").exists():
        return "", []
    reference = _git_output(
        repo_dir, "symbolic-ref", "--quiet", "--short", "refs/remotes/origin/HEAD"
    )
    if not reference:
        remotes = _git_output(
            repo_dir, "for-each-ref", "--format=%(refname:short)", "refs/remotes/origin"
        ).splitlines()
        reference = next(
            (item for preferred in ("origin/main", "origin/master") for item in remotes if item == preferred),
            remotes[0] if remotes else "HEAD",
        )
    files = _git_output(repo_dir, "ls-tree", "-r", "--name-only", reference).splitlines()
    return reference, files


def _detect_language(repo_dir: Path) -> str:
    _, git_paths = _git_files(repo_dir)
    if git_paths:
        extensions = Counter(Path(path).suffix.lower() for path in git_paths)
    else:
        extensions = Counter(
            path.suffix.lower()
            for path in repo_dir.rglob("*")
            if path.is_file() and ".git" not in path.parts
        )
    mapping = {
        ".py": "Python",
        ".ts": "TypeScript",
        ".tsx": "TypeScript",
        ".js": "JavaScript",
        ".jsx": "JavaScript",
        ".rs": "Rust",
        ".go": "Go",
        ".java": "Java",
        ".kt": "Kotlin",
        ".cpp": "C++",
        ".c": "C",
        ".rb": "Ruby",
        ".php": "PHP",
    }
    known: Counter[str] = Counter()
    for extension, count in extensions.items():
        if extension in mapping:
            known[mapping[extension]] += count
    return known.most_common(1)[0][0] if known else "未知"


SOURCE_EXTENSIONS = {
    ".c", ".cc", ".cpp", ".cs", ".dart", ".ex", ".exs", ".fs", ".fsx", ".go", ".h", ".hpp",
    ".java", ".js", ".jsx", ".kt", ".kts", ".lua", ".mjs", ".cjs", ".php", ".ps1", ".py",
    ".r", ".rb", ".rs", ".scala", ".sh", ".sol", ".svelte", ".swift", ".ts", ".tsx", ".vb",
    ".vue", ".zig",
}

# 这些目录通常是依赖、构建产物或大体积资源，不属于“仓库作者写给人/机器读的文本”。
# .github 故意不跳过，Workflow/Issue 模板本身也是项目结构的一部分。
TEXT_SKIP_DIRS = {
    ".git", "assets", "build", "coverage", "dist", "node_modules", "target", "vendor",
    "__pycache__", ".cache",
}

# 机器生成、噪声极高的文本快照。文件名仍会出现在 repository-tree 中。
TEXT_SKIP_NAMES = {
    "package-lock.json", "pnpm-lock.yaml", "yarn.lock", "bun.lock", "bun.lockb",
    "cargo.lock", "pubspec.lock", "poetry.lock", "pipfile.lock", "composer.lock",
    "uv.lock", "go.sum",
}

BINARY_EXTENSIONS = {
    ".7z", ".a", ".aab", ".apk", ".avi", ".bin", ".bmp", ".class", ".dat", ".db", ".dll",
    ".dylib", ".exe", ".flac", ".gif", ".gz", ".ico", ".ipa", ".jar", ".jpeg", ".jpg",
    ".m4a", ".mkv", ".mov", ".mp3", ".mp4", ".o", ".obj", ".ogg", ".otf", ".pdf", ".png",
    ".rar", ".so", ".sqlite", ".sqlite3", ".tar", ".ttf", ".wav", ".webm", ".webp", ".woff",
    ".woff2", ".xz", ".zip",
}

REPOSITORY_TREE_LIMIT = 40_000
CURRENT_TEXT_TOTAL_LIMIT = 480_000
CURRENT_SOURCE_FILE_LIMIT = 140_000
CURRENT_TEXT_FILE_LIMIT = 120_000
HISTORY_SOURCE_LIMIT = 80_000

# GitHub/ChooseALicense 常见协议，以及实践中经常出现在 GitHub LICENSE 文件开头的标题。
# 命中后只把协议名送进 LLM，避免把成千上万字的标准法律模板当成项目叙事素材。
# 顺序很重要：AGPL/LGPL 必须先于 GPL。
KNOWN_LICENSE_TITLES: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("GNU Affero General Public License v3.0", (
        r"\bGNU AFFERO GENERAL PUBLIC LICENSE\b.{0,80}\bVERSION 3\b",
    )),
    ("GNU Lesser General Public License v3.0", (
        r"\bGNU LESSER GENERAL PUBLIC LICENSE\b.{0,80}\bVERSION 3\b",
    )),
    ("GNU Lesser General Public License v2.1", (
        r"\bGNU LESSER GENERAL PUBLIC LICENSE\b.{0,80}\bVERSION 2\.1\b",
    )),
    ("GNU General Public License v3.0", (
        r"\bGNU GENERAL PUBLIC LICENSE\b.{0,80}\bVERSION 3\b",
    )),
    ("GNU General Public License v2.0", (
        r"\bGNU GENERAL PUBLIC LICENSE\b.{0,80}\bVERSION 2\b",
    )),
    ("Apache License 2.0", (
        r"\bAPACHE LICENSE\b.{0,80}\bVERSION 2\.0\b",
        r"\bAPACHE LICENSE,?\s+VERSION 2\.0\b",
    )),
    ("Mozilla Public License 2.0", (
        r"\bMOZILLA PUBLIC LICENSE\b.{0,80}\bVERSION 2\.0\b",
    )),
    ("Eclipse Public License 2.0", (
        r"\bECLIPSE PUBLIC LICENSE\b.{0,80}\b(?:V|VERSION)\s*2\.0\b",
    )),
    ("Boost Software License 1.0", (
        r"\bBOOST SOFTWARE LICENSE\b.{0,80}\bVERSION 1\.0\b",
    )),
    ("BSD 3-Clause License", (
        r"\bBSD 3[- ]CLAUSE\b.*\bLICENSE\b",
        r"\bBSD THREE[- ]CLAUSE\b.*\bLICENSE\b",
    )),
    ("BSD 2-Clause License", (
        r"\bBSD 2[- ]CLAUSE\b.*\bLICENSE\b",
        r"\bBSD TWO[- ]CLAUSE\b.*\bLICENSE\b",
    )),
    ("MIT License", (
        r"(?m)^\s*MIT LICENSE\s*$",
    )),
    ("ISC License", (
        r"(?m)^\s*ISC LICENSE\s*$",
    )),
    ("The Unlicense", (
        r"(?m)^\s*(?:THE )?UNLICENSE\s*$",
        r"\bTHIS IS FREE AND UNENCUMBERED SOFTWARE RELEASED INTO THE PUBLIC DOMAIN\b",
    )),
    ("Creative Commons Zero v1.0 Universal", (
        r"\bCREATIVE COMMONS ZERO\b.{0,80}\b1\.0\b",
        r"\bCC0 1\.0 UNIVERSAL\b",
    )),
    ("Creative Commons Attribution 4.0 International", (
        r"\bCREATIVE COMMONS ATTRIBUTION 4\.0 INTERNATIONAL\b",
        r"\bCC BY 4\.0\b",
    )),
    ("Creative Commons Attribution-ShareAlike 4.0 International", (
        r"\bCREATIVE COMMONS ATTRIBUTION[- ]SHAREALIKE 4\.0 INTERNATIONAL\b",
        r"\bCC BY-SA 4\.0\b",
    )),
    ("zlib License", (
        r"(?m)^\s*ZLIB LICENSE\s*$",
    )),
    ("Artistic License 2.0", (
        r"\bARTISTIC LICENSE\b.{0,80}\b2\.0\b",
    )),
    ("Academic Free License 3.0", (
        r"\bACADEMIC FREE LICENSE\b.{0,80}\b3\.0\b",
    )),
    ("Open Software License 3.0", (
        r"\bOPEN SOFTWARE LICENSE\b.{0,80}\b3\.0\b",
    )),
    ("Educational Community License 2.0", (
        r"\bEDUCATIONAL COMMUNITY LICENSE\b.{0,80}\b2\.0\b",
    )),
    ("Microsoft Public License", (
        r"\bMICROSOFT PUBLIC LICENSE\b",
        r"\bMS-PL\b",
    )),
    ("Microsoft Reciprocal License", (
        r"\bMICROSOFT RECIPROCAL LICENSE\b",
        r"\bMS-RL\b",
    )),
    ("NCSA Open Source License", (
        r"\bUNIVERSITY OF ILLINOIS/NCSA OPEN SOURCE LICENSE\b",
        r"\bNCSA OPEN SOURCE LICENSE\b",
    )),
    ("PostgreSQL License", (
        r"(?m)^\s*POSTGRESQL LICENSE\s*$",
    )),
    ("Python Software Foundation License 2.0", (
        r"\bPYTHON SOFTWARE FOUNDATION LICENSE\b.{0,80}\bVERSION 2\b",
    )),
    ("Universal Permissive License 1.0", (
        r"\bUNIVERSAL PERMISSIVE LICENSE\b.{0,80}\b1\.0\b",
    )),
    ("Common Development and Distribution License 1.0", (
        r"\bCOMMON DEVELOPMENT AND DISTRIBUTION LICENSE\b.{0,80}\b1\.0\b",
        r"\bCDDL\b.{0,40}\b1\.0\b",
    )),
    ("WTFPL", (
        r"\bDO WHAT THE FUCK YOU WANT TO PUBLIC LICENSE\b",
        r"(?m)^\s*WTFPL\s*$",
    )),
)


def _truncate_text(text: str, limit: int, marker: str) -> str:
    if len(text) <= limit:
        return text
    if limit <= len(marker) + 40:
        return text[:limit]
    available = limit - len(marker)
    head = int(available * 0.75)
    tail = available - head
    return text[:head] + marker + text[-tail:]


def _git_blob_bytes(repo_dir: Path, reference: str, relative: str) -> bytes:
    if not (repo_dir / ".git").exists():
        return b""
    result = subprocess.run(
        ["git", "-C", str(repo_dir), "show", f"{reference}:{relative}"],
        capture_output=True,
        check=False,
    )
    return result.stdout if result.returncode == 0 else b""


def _decode_text_blob(data: bytes) -> str:
    """保守识别 Git blob 是否为普通文本，不用扩展名猜 UTF-8 文档。"""
    if not data:
        return ""
    if data.startswith((b"\xff\xfe", b"\xfe\xff")):
        try:
            return data.decode("utf-16")
        except UnicodeDecodeError:
            return ""
    if b"\x00" in data[:16_384]:
        return ""
    try:
        text = data.decode("utf-8-sig")
    except UnicodeDecodeError:
        return ""
    sample = text[:16_384]
    if sample:
        controls = sum(
            1 for ch in sample
            if ord(ch) < 32 and ch not in "\n\r\t\f\b"
        )
        if controls / len(sample) > 0.01:
            return ""
    return text


def _looks_like_license_path(relative: str) -> bool:
    path = Path(relative)
    stem = path.stem.casefold()
    name = path.name.casefold()
    if any(part.casefold() in {"license", "licenses", "licence", "licences"} for part in path.parts[:-1]):
        return True
    return (
        stem.startswith(("license", "licence", "copying"))
        or name in {"unlicense", "copyright"}
    )


def _known_license_title(text: str) -> str | None:
    # 法律模板的协议名一定在开头附近；只扫描前 12 KiB，避免正文里的引用误命中。
    head = text[:12_000]
    compact = re.sub(r"[ \t]+", " ", head).upper()
    for title, patterns in KNOWN_LICENSE_TITLES:
        if any(re.search(pattern, compact, flags=re.S) for pattern in patterns):
            return title
    return None


def _text_priority(relative: str) -> tuple[int, str]:
    path = Path(relative)
    low = relative.casefold()
    name = path.name.casefold()
    suffix = path.suffix.casefold()
    score = 0

    if len(path.parts) == 1:
        score += 4_000
    if name.startswith("readme"):
        score += 40_000
    if suffix in SOURCE_EXTENSIONS:
        score += 30_000
    if name.startswith(("contributing", "security", "changelog", "history", "architecture", "design")):
        score += 18_000
    if low.startswith(".github/"):
        score += 12_000
    if low.startswith(("docs/", "doc/")):
        score += 10_000
    if _looks_like_license_path(relative):
        score += 8_000
    if suffix in {".toml", ".yaml", ".yml", ".json", ".jsonc", ".ini", ".cfg", ".conf", ".xml"}:
        score += 7_000
    return score, relative


def _read_source_snapshot(path: Path, *, limit: int) -> str:
    """兼容历史目录的文件系统读取；超限时保留头尾。"""
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return ""
    return _truncate_text(
        text,
        limit,
        "\n\n/* … Repo2Gal source-context: middle truncated … */\n\n",
    )


def _is_source_file(path: Path) -> bool:
    return path.is_file() and path.suffix.lower() in SOURCE_EXTENSIONS


def _source_rank(path: Path, source_dir: Path, *, history: bool) -> tuple[int, int, str]:
    """历史代表文件仍沿用单文件实验分支的确定性排序。"""
    rel = path.relative_to(source_dir).as_posix()
    low = rel.lower()
    name = path.name.lower()
    score = 0

    if name == "new.user.js":
        score += 20_000
    if "最新" in rel or "latest" in low:
        score += 10_000
    if not history and path.parent == source_dir:
        score += 2_000
    if history and ("历史大版本" in rel or "major" in low):
        score += 2_000
    if "/temp/" in f"/{low}/" or "/tmp/" in f"/{low}/":
        score -= 20_000
    return score, min(path.stat().st_size, 2_000_000), rel


def _collect_source_snapshots(
    source_dir: Path,
    *,
    history_count: int = 1,
) -> list[SourceSnapshot]:
    """读取当前仓库中尽可能多的真实文本，并保留少量历史源码代表。

    - repository-tree 总是先送入，模型至少知道完整文件布局；
    - 当前分支按 Git tree 读取，不依赖可能陈旧的工作树；
    - 常见标准 LICENSE 压缩成协议名，自编 LICENSE 保留全文；
    - 二进制、依赖/构建目录、lockfile 不灌入正文，但仍会出现在树里；
    - 当前文本总预算 480k，单文件另外限幅，避免一个巨型文件吞掉整个上下文。
    """
    source_dir = Path(source_dir)
    if not source_dir.is_dir():
        return []

    reference, git_paths = _git_files(source_dir)
    if not git_paths:
        return []

    tree_text = "\n".join(git_paths)
    tree_text = _truncate_text(
        tree_text,
        REPOSITORY_TREE_LIMIT,
        "\n…（repository tree 过长，已截断）\n",
    )
    snapshots: list[SourceSnapshot] = [
        SourceSnapshot(path="(repository tree)", role="repository-tree", content=tree_text)
    ]

    candidates: list[str] = []
    for relative in git_paths:
        path = Path(relative)
        parts = path.parts
        if any(part in TEXT_SKIP_DIRS for part in parts[:-1]):
            continue
        if parts and parts[0].casefold() == "history":
            continue
        if path.name.casefold() in TEXT_SKIP_NAMES:
            continue
        if path.suffix.casefold() in BINARY_EXTENSIONS:
            continue
        if path.suffix.casefold() == ".map" or ".min." in path.name.casefold():
            continue
        candidates.append(relative)

    candidates.sort(key=_text_priority, reverse=True)

    remaining = CURRENT_TEXT_TOTAL_LIMIT
    seen: set[str] = set()

    for relative in candidates:
        if remaining <= 0:
            break
        raw = _git_blob_bytes(source_dir, reference, relative)
        text = _decode_text_blob(raw)
        if not text:
            continue

        path = Path(relative)
        role = "current-source" if path.suffix.casefold() in SOURCE_EXTENSIONS else "current-text"
        if _looks_like_license_path(relative):
            title = _known_license_title(text)
            if title:
                text = title
                role = "license"
            else:
                role = "license-custom"

        file_limit = (
            CURRENT_SOURCE_FILE_LIMIT
            if role == "current-source"
            else CURRENT_TEXT_FILE_LIMIT
        )
        text = _truncate_text(
            text,
            min(file_limit, remaining),
            "\n\n…（该仓库文本过长，中段已截断）\n\n",
        )
        if not text or text in seen:
            continue
        seen.add(text)
        snapshots.append(
            SourceSnapshot(path=relative, role=role, content=text)
        )
        remaining -= len(text)

    # history 仍只取显式 history/ 目录中的少量源码代表，避免历史副本淹没当前仓库。
    history_candidates: list[Path] = []
    history_root = source_dir / "history"
    if history_root.is_dir():
        for path in history_root.rglob("*"):
            if not _is_source_file(path):
                continue
            rel_parts = path.relative_to(source_dir).parts
            if any(part.lower() in {"temp", "tmp"} for part in rel_parts[1:-1]):
                continue
            history_candidates.append(path)
    history_candidates.sort(
        key=lambda p: _source_rank(p, source_dir, history=True),
        reverse=True,
    )

    history_added = 0
    for path in history_candidates:
        if history_added >= max(0, history_count):
            break
        content = _read_source_snapshot(path, limit=HISTORY_SOURCE_LIMIT)
        if not content or content in seen:
            continue
        seen.add(content)
        snapshots.append(
            SourceSnapshot(
                path=path.relative_to(source_dir).as_posix(),
                role="history",
                content=content,
            )
        )
        history_added += 1

    return snapshots


def context_from_backup(
    owner: str,
    repo: str,
    repo_backup_dir: Path,
    *,
    metadata: dict[str, Any] | None = None,
    top_threads: int = 12,
    source_context: bool = False,
    source_history: int = 1,
    log=lambda _msg: None,
) -> RepoContext:
    """把 python-github-backup 的落盘结果归一化成 RepoContext。"""
    repo_backup_dir = Path(repo_backup_dir)
    source_dir = repo_backup_dir / "repository"
    metadata = metadata if metadata is not None else _read_metadata(repo_backup_dir)

    threads = [
        *(_thread_from_issue(item) for item in _load_json_files(repo_backup_dir / "issues")),
        *(_thread_from_pull(item) for item in _load_json_files(repo_backup_dir / "pulls")),
        *(
            _thread_from_discussion(item)
            for item in _load_json_files(repo_backup_dir / "discussions")
        ),
    ]
    threads.sort(key=lambda item: (item.comment_count, item.created_at), reverse=True)
    threads = threads[:top_threads]

    activity: Counter[str] = Counter()
    for thread in threads:
        if thread.author != "unknown":
            activity[thread.author] += 1
        activity.update(comment.author for comment in thread.comments if comment.author != "unknown")

    releases = [
        Release(
            tag=item.get("tag_name") or "",
            name=item.get("name") or item.get("tag_name") or "",
            published_at=(item.get("published_at") or item.get("created_at") or "")[:10],
            body=_clean_body(item.get("body"), 500),
        )
        for item in _load_json_files(repo_backup_dir / "releases")
        if not item.get("draft")
    ]
    releases.sort(key=lambda item: item.published_at, reverse=True)

    readme = _read_text_candidates(
        source_dir, ("README.md", "README.rst", "README.txt", "README"), 3000
    )
    readme_description = next(
        (line.lstrip("#= ") for line in readme.splitlines() if line.strip()), ""
    )
    reference, _ = _git_files(source_dir)
    roots = _git_output(
        source_dir, "rev-list", "--max-parents=0", reference or "HEAD"
    ).splitlines()
    first_commit = _git_output(source_dir, "show", "-s", "--format=%cs", roots[0]) if roots else ""

    context = RepoContext(
        owner=(metadata.get("owner") or {}).get("login") or owner,
        name=metadata.get("name") or repo,
        description=metadata.get("description") or readme_description,
        language=metadata.get("language") or _detect_language(source_dir),
        stars=metadata.get("stargazers_count") or 0,
        created_at=(metadata.get("created_at") or first_commit)[:10],
        topics=metadata.get("topics") or [],
        readme_excerpt=readme,
        wiki_excerpt=_read_wiki(repo_backup_dir / "wiki"),
        contributors=[Contributor(login=name, contributions=count) for name, count in activity.most_common(8)],
        source_snapshots=(
            _collect_source_snapshots(source_dir, history_count=source_history)
            if source_context
            else []
        ),
        releases=releases[:10],
        threads=threads,
        backup_dir=str(repo_backup_dir),
    )
    log(
        f"上下文：{len(context.threads)} 条热门讨论（含 Discussion），"
        f"{len(context.releases)} 个 Release，wiki={'有' if context.wiki_excerpt else '无'}"
    )
    if context.source_snapshots:
        log(
            "源码上下文："
            + "，".join(
                f"{item.role}={item.path}（{len(item.content)} 字）"
                for item in context.source_snapshots
            )
        )
    if not context.threads and not context.readme_excerpt and not context.wiki_excerpt:
        raise FetchError("备份中没有 README、wiki 或社区讨论，素材不足以生成剧情")
    return context


def fetch_context(
    owner: str,
    repo: str,
    *,
    backup_root: Path,
    token: str | None = None,
    organization: bool = False,
    top_threads: int = 12,
    reuse_backup: bool = False,
    source_context: bool = False,
    source_history: int = 1,
    log=lambda _msg: None,
    progress=lambda _msg: None,
) -> RepoContext:
    """执行备份（或复用已有备份）并构建上下文。"""
    if not reuse_backup and not token:
        raise FetchError(
            "python-github-backup 需要 GitHub Token；请设置 GITHUB_TOKEN，"
            "或用 --reuse-backup 读取已有备份"
        )
    expected = _resolve_repo_backup_dir(Path(backup_root), repo)
    if reuse_backup:
        if not expected.exists():
            raise FetchError(f"--reuse-backup 指定的备份不存在：{expected}")
        repo_dir = expected
        log(f"复用原始备份：{repo_dir}")
        metadata = _read_metadata(repo_dir)
    else:
        metadata = fetch_repository_metadata(owner, repo, token or "", log=log)
        repo_dir = run_backup(
            owner,
            repo,
            Path(backup_root),
            token=token,
            organization=organization,
            log=log,
            progress=progress,
        )
        if metadata:
            _metadata_path(repo_dir).write_text(
                json.dumps(metadata, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
            )
    return context_from_backup(
        owner,
        repo,
        repo_dir,
        metadata=metadata,
        top_threads=top_threads,
        source_context=source_context,
        source_history=source_history,
        log=log,
    )
