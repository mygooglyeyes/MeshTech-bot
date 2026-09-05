"""Tests for core/version.py - the dashboard build stamp.

Resolution order: $MESHTECH_COMMIT -> <root>/.git-commit -> <root>/.git
read by hand -> unknown.  Tests pass an explicit ``root`` so they never
touch the real repository's .git.
"""
from __future__ import annotations

from pathlib import Path

from core.version import __version__, version_stamp


def _write(root: Path, *parts: str, text: str) -> None:
    p = root.joinpath(*parts)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text, encoding="utf-8")


def _git_repo(root: Path, sha: str = "a" * 40, branch: str = "main") -> None:
    _write(root, ".git", "HEAD", text=f"ref: refs/heads/{branch}\n")
    _write(root, ".git", "refs", "heads", branch, text=sha + "\n")


def test_unknown_with_no_sources(tmp_path):
    stamp = version_stamp(tmp_path)
    assert stamp == {"source": "unknown"}


def test_env_var_wins(monkeypatch, tmp_path):
    monkeypatch.setenv("MESHTECH_COMMIT", "0123456789abcdef")
    _git_repo(tmp_path, sha="f" * 40)
    stamp = version_stamp(tmp_path)
    assert stamp["commit"] == "0123456"      # shortened to 7
    assert stamp["branch"] == ""
    assert stamp["source"] == "env"


def test_baked_file_beats_git(monkeypatch, tmp_path):
    monkeypatch.delenv("MESHTECH_COMMIT", raising=False)
    _write(tmp_path, ".git-commit", text="cafebabe1234\n")
    _git_repo(tmp_path, sha="f" * 40)
    stamp = version_stamp(tmp_path)
    assert stamp["commit"] == "cafebab"
    assert stamp["source"] == "file"


def test_git_loose_ref(tmp_path, monkeypatch):
    monkeypatch.delenv("MESHTECH_COMMIT", raising=False)
    sha = "abcdef0123456789"
    _git_repo(tmp_path, sha=sha)
    stamp = version_stamp(tmp_path)
    assert stamp["commit"] == sha[:7]
    assert stamp["branch"] == "main"
    assert stamp["source"] == "git"
    assert stamp["version"] == __version__


def test_unknown_has_no_version_key(tmp_path, monkeypatch):
    monkeypatch.delenv("MESHTECH_COMMIT", raising=False)
    stamp = version_stamp(tmp_path)      # no .git, no baked file
    assert "version" not in stamp        # unknown builds carry no number


def test_git_packed_refs_fallback(tmp_path, monkeypatch):
    monkeypatch.delenv("MESHTECH_COMMIT", raising=False)
    _write(tmp_path, ".git", "HEAD", text="ref: refs/heads/main\n")
    _write(tmp_path, ".git", "packed-refs",
           text="# pack-refs with: peeled fully-peeled sorted\n"
                "1234567890abcdef1234567890abcdef12345678 refs/heads/other\n"
                "feedface1234567890abcdef1234567890abcdef refs/heads/main\n")
    stamp = version_stamp(tmp_path)
    assert stamp["commit"] == "feedfac"
    assert stamp["branch"] == "main"


def test_git_detached_head(tmp_path, monkeypatch):
    monkeypatch.delenv("MESHTECH_COMMIT", raising=False)
    sha = "deadbeefcafe1234567890abcdef1234567890ab"
    _write(tmp_path, ".git", "HEAD", text=sha + "\n")
    stamp = version_stamp(tmp_path)
    assert stamp["commit"] == sha[:7]
    assert stamp["branch"] == ""


def test_gitdir_file_indirection(tmp_path, monkeypatch):
    monkeypatch.delenv("MESHTECH_COMMIT", raising=False)
    real = tmp_path / "actual-git"
    _write(real, "HEAD", text="ref: refs/heads/work\n")
    _write(real, "refs", "heads", "work", text="0badc0de" * 5 + "\n")
    _write(tmp_path, ".git", text="gitdir: actual-git\n")
    stamp = version_stamp(tmp_path)
    assert stamp["commit"] == "0badc0d"
    assert stamp["branch"] == "work"
    assert stamp["source"] == "git"


def test_absolute_gitdir_path(tmp_path, monkeypatch):
    monkeypatch.delenv("MESHTECH_COMMIT", raising=False)
    real = tmp_path / "elsewhere" / "git"
    _write(real, "HEAD", text="ref: refs/heads/main\n")
    _write(real, "refs", "heads", "main", text="1111222233334444555566667777888899990000\n")
    _write(tmp_path, ".git", text=f"gitdir: {real}\n")
    stamp = version_stamp(tmp_path)
    assert stamp["commit"] == "1111222"
    assert stamp["source"] == "git"
