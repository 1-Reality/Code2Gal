from __future__ import annotations

import subprocess
from pathlib import Path

from repo2gal.fetcher import (
    _collect_source_snapshots,
    _known_license_title,
)


def _git(repo: Path, *args: str) -> None:
    subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True)


def _make_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init")
    _git(repo, "config", "user.email", "test@example.com")
    _git(repo, "config", "user.name", "Repo2Gal Test")
    return repo


def test_known_license_titles_are_compacted() -> None:
    assert _known_license_title("MIT License\n\nCopyright 2026 Example") == "MIT License"
    assert (
        _known_license_title(
            "Apache License\nVersion 2.0, January 2004\nhttp://www.apache.org/licenses/"
        )
        == "Apache License 2.0"
    )
    assert (
        _known_license_title(
            "GNU GENERAL PUBLIC LICENSE\nVersion 3, 29 June 2007"
        )
        == "GNU General Public License v3.0"
    )
    assert _known_license_title("哥哥科技专有软件最终用户许可协议\n第一条 自定义内容") is None


def test_repository_wide_text_context_and_custom_license(tmp_path: Path) -> None:
    repo = _make_repo(tmp_path)
    (repo / "src").mkdir()
    (repo / ".github" / "workflows").mkdir(parents=True)

    (repo / "README.md").write_text("# Demo\n完整 README 文本\n", encoding="utf-8")
    (repo / "src" / "a.py").write_text("VALUE = 1\n", encoding="utf-8")
    (repo / "config.yaml").write_text("enabled: true\n", encoding="utf-8")
    (repo / ".github" / "workflows" / "ci.yml").write_text("name: CI\n", encoding="utf-8")
    (repo / "LICENSE").write_text("MIT License\n\nCopyright 2026 Example\n", encoding="utf-8")
    (repo / "LICENSE-custom.md").write_text(
        "哥哥科技专有软件最终用户许可协议\n第一条 自定义内容\n",
        encoding="utf-8",
    )
    (repo / "package-lock.json").write_text('{"lockfileVersion": 3}\n', encoding="utf-8")
    (repo / "program.exe").write_bytes(b"MZ\x00\x01binary")

    _git(repo, "add", ".")
    _git(repo, "commit", "-m", "fixture")

    snapshots = _collect_source_snapshots(repo, history_count=0)
    by_path = {item.path: item for item in snapshots}

    tree = by_path["(repository tree)"].content
    assert "README.md" in tree
    assert "program.exe" in tree
    assert "package-lock.json" in tree

    assert by_path["README.md"].content == "# Demo\n完整 README 文本\n"
    assert by_path["src/a.py"].role == "current-source"
    assert by_path["config.yaml"].role == "current-text"
    assert by_path[".github/workflows/ci.yml"].role == "current-text"

    assert by_path["LICENSE"].role == "license"
    assert by_path["LICENSE"].content == "MIT License"

    assert by_path["LICENSE-custom.md"].role == "license-custom"
    assert "第一条 自定义内容" in by_path["LICENSE-custom.md"].content

    assert "package-lock.json" not in by_path
    assert "program.exe" not in by_path
