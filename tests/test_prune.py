"""Tests for autoloop prune: stale worktree and branch cleanup."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from autoloop import prune


def _cp(cmd, returncode=0, stdout="", stderr=""):
    return subprocess.CompletedProcess(cmd, returncode, stdout=stdout, stderr=stderr)


class FakeRepo:
    """Stand-in for git and gh: worktrees, branches, PR states, dirty trees, unique commits."""

    def __init__(self, tmp_path: Path):
        self.repo_dir = tmp_path / "repo"
        self.worktree_dir = tmp_path / "data" / "worktrees"
        self.ctx = SimpleNamespace(repo_dir=self.repo_dir, worktree_dir=self.worktree_dir)
        self.cfg = SimpleNamespace(repo="test-owner/test-repo")
        self.worktrees: list[tuple[Path, str | None]] = [(self.repo_dir, "main")]
        self.branches: list[str] = []
        self.pr_states: dict[str, object] = {}
        self.dirty: set[Path] = set()
        self.unique_commits: dict[str, int] = {}
        self.calls: list[list[str]] = []

    def add_worktree(self, branch: str, pr_state=None, dirty=False, unique=0) -> Path:
        path = self.worktree_dir / branch
        self.worktrees.append((path, branch))
        self.add_branch(branch, pr_state, unique)
        if dirty:
            self.dirty.add(path)
        return path

    def add_branch(self, branch: str, pr_state=None, unique=0):
        self.branches.append(branch)
        if pr_state is not None:
            self.pr_states[branch] = pr_state
        self.unique_commits[branch] = unique

    def run(self, cmd, **kwargs):
        self.calls.append(list(cmd))
        cwd = Path(kwargs.get("cwd") or self.repo_dir)
        if cmd[0] == "gh":
            branch = cmd[cmd.index("--head") + 1]
            state = self.pr_states.get(branch)
            if state == "raise":
                raise FileNotFoundError("gh")
            if state == "error":
                return _cp(cmd, 1, stderr="HTTP 401: Bad credentials")
            prs = [] if state is None else [{"state": s} for s in state.split(",")]
            return _cp(cmd, stdout=json.dumps(prs))
        sub = cmd[1:]
        if sub[:3] == ["worktree", "list", "--porcelain"]:
            blocks = []
            for path, branch in self.worktrees:
                ref = f"branch refs/heads/{branch}" if branch else "detached"
                blocks.append(f"worktree {path}\nHEAD abc123\n{ref}\n")
            return _cp(cmd, stdout="\n".join(blocks))
        if sub[0] == "for-each-ref":
            return _cp(cmd, stdout="".join(f"{b}\n" for b in self.branches))
        if sub[0] == "status":
            return _cp(cmd, stdout=" M src/app.py\n" if cwd in self.dirty else "")
        if sub[0] == "rev-list":
            branch = sub[-1].split("..refs/heads/")[1]
            return _cp(cmd, stdout=f"{self.unique_commits.get(branch, 0)}\n")
        if sub[:3] == ["worktree", "remove", "--force"]:
            path = Path(sub[3])
            self.worktrees = [w for w in self.worktrees if w[0] != path]
            return _cp(cmd)
        if sub[:2] == ["branch", "-D"]:
            self.branches.remove(sub[2])
            return _cp(cmd)
        raise AssertionError(f"unexpected command: {cmd}")

    def mutating_calls(self):
        return [
            c
            for c in self.calls
            if c[:3] == ["git", "worktree", "remove"] or c[:3] == ["git", "branch", "-D"]
        ]

    def prune(self, **kwargs):
        with patch("autoloop.prune.subprocess.run", side_effect=self.run):
            return prune.prune(self.ctx, self.cfg, **kwargs)

    def main(self):
        with patch("autoloop.prune.subprocess.run", side_effect=self.run):
            return prune.main(self.ctx, self.cfg)


@pytest.fixture()
def repo(tmp_path):
    return FakeRepo(tmp_path)


def _item(result, name):
    return next(i for i in result.items if i.name == name)


class TestMergedWorktree:
    def test_clean_worktree_removed_before_branch_deleted(self, repo, capsys):
        path = repo.add_worktree("autoloop/1-done", "MERGED")

        assert repo.main() == 0

        assert repo.mutating_calls() == [
            ["git", "worktree", "remove", "--force", str(path)],
            ["git", "branch", "-D", "autoloop/1-done"],
        ]
        out = capsys.readouterr().out
        assert "worktree autoloop/1-done: removed" in out
        assert "autoloop/1-done" not in repo.branches

    def test_merged_pr_deletes_branch_even_with_unique_commits(self, repo):
        repo.add_worktree("autoloop/2-squashed", "MERGED", unique=3)

        result = repo.prune()

        assert ["git", "branch", "-D", "autoloop/2-squashed"] in repo.calls
        assert _item(result, "autoloop/2-squashed").removed


class TestDirtyWorktree:
    def test_dirty_worktree_kept_even_when_merged(self, repo, capsys):
        repo.add_worktree("autoloop/3-dirty", "MERGED", dirty=True)

        assert repo.main() == 0

        assert repo.mutating_calls() == []
        assert "autoloop/3-dirty" in repo.branches
        out = capsys.readouterr().out
        line = next(ln for ln in out.splitlines() if "autoloop/3-dirty" in ln)
        assert "kept (dirty)" in line
        assert "uncommitted tracked changes" in line

    def test_status_ignores_untracked_files(self, repo):
        path = repo.add_worktree("autoloop/4-x", "MERGED")

        repo.prune()

        assert [
            "git",
            "status",
            "--porcelain",
            "--untracked-files=no",
        ] in repo.calls
        assert ["git", "worktree", "remove", "--force", str(path)] in repo.calls


class TestOpenPr:
    def test_open_pr_worktree_and_branch_kept(self, repo, capsys):
        repo.add_worktree("autoloop/5-open", "OPEN")

        result = repo.prune()

        assert repo.mutating_calls() == []
        assert "autoloop/5-open" in repo.branches
        assert _item(result, "autoloop/5-open").outcome == "kept (open PR)"

    def test_open_pr_wins_over_older_merged_pr(self, repo):
        repo.add_worktree("autoloop/6-reopened", "MERGED,OPEN")

        repo.prune()

        assert repo.mutating_calls() == []


class TestNeedsReview:
    def test_branch_without_merged_pr_and_unique_commits_kept(self, repo, capsys):
        repo.add_branch("autoloop/7-orphan", None, unique=2)

        assert repo.main() == 0

        assert repo.mutating_calls() == []
        assert "autoloop/7-orphan" in repo.branches
        out = capsys.readouterr().out
        heading = out.index("Needs human review:")
        assert "autoloop/7-orphan" in out[heading:]
        assert "1 needing review" in out

    def test_needs_review_count(self, repo):
        repo.add_branch("autoloop/8-orphan", None, unique=2)
        repo.add_branch("autoloop/9-closed", "CLOSED", unique=1)

        result = repo.prune()

        assert result.needs_review == 2

    def test_closed_pr_with_unique_commits_removes_worktree_keeps_branch(self, repo, capsys):
        path = repo.add_worktree("autoloop/10-abandoned", "CLOSED", unique=4)

        assert repo.main() == 0

        assert repo.mutating_calls() == [["git", "worktree", "remove", "--force", str(path)]]
        assert "autoloop/10-abandoned" in repo.branches
        out = capsys.readouterr().out
        assert "worktree removed, branch kept" in out
        assert "autoloop/10-abandoned" in out[out.index("Needs human review:") :]

    def test_closed_pr_with_merged_commits_deletes_branch(self, repo):
        repo.add_worktree("autoloop/11-closed-merged", "CLOSED", unique=0)

        repo.prune()

        assert ["git", "branch", "-D", "autoloop/11-closed-merged"] in repo.calls


class TestBranchWithoutWorktree:
    def test_merged_pr_branch_deleted(self, repo, capsys):
        repo.add_branch("autoloop/12-merged", "MERGED", unique=5)

        repo.main()

        assert repo.mutating_calls() == [["git", "branch", "-D", "autoloop/12-merged"]]
        assert "branch autoloop/12-merged: removed" in capsys.readouterr().out

    def test_no_pr_fully_merged_branch_deleted(self, repo):
        repo.add_branch("autoloop/13-landed", None, unique=0)

        result = repo.prune()

        assert repo.mutating_calls() == [["git", "branch", "-D", "autoloop/13-landed"]]
        assert _item(result, "autoloop/13-landed").removed

    def test_open_pr_branch_kept(self, repo):
        repo.add_branch("autoloop/14-open", "OPEN")

        repo.prune()

        assert repo.mutating_calls() == []

    def test_branch_checked_out_outside_worktree_dir_kept(self, repo):
        repo.add_branch("autoloop/15-here", "MERGED")
        repo.worktrees[0] = (repo.repo_dir, "autoloop/15-here")

        result = repo.prune()

        assert repo.mutating_calls() == []
        assert _item(result, "autoloop/15-here").outcome == "kept (checked out)"

    def test_worktree_branch_not_processed_twice(self, repo):
        repo.add_worktree("autoloop/16-once", "OPEN")

        result = repo.prune()

        assert [i.name for i in result.items] == ["autoloop/16-once"]


class TestUnknownPrState:
    @pytest.mark.parametrize("failure", ["error", "raise"])
    def test_gh_failure_keeps_item_and_continues(self, repo, capsys, failure):
        repo.add_worktree("autoloop/17-unknown", failure)
        repo.add_branch("autoloop/18-unknown-branch", failure)
        done = repo.add_worktree("autoloop/19-done", "MERGED")

        assert repo.main() == 0

        assert repo.mutating_calls() == [
            ["git", "worktree", "remove", "--force", str(done)],
            ["git", "branch", "-D", "autoloop/19-done"],
        ]
        assert "autoloop/17-unknown" in repo.branches
        assert "autoloop/18-unknown-branch" in repo.branches
        out = capsys.readouterr().out
        for name in ("autoloop/17-unknown", "autoloop/18-unknown-branch"):
            line = next(ln for ln in out.splitlines() if f" {name}:" in ln)
            assert "unknown PR state" in line
        assert "worktree autoloop/19-done: removed" in out


class TestSummary:
    def test_summary_counts_and_exit_code(self, repo, capsys):
        repo.add_worktree("autoloop/20-done", "MERGED")
        repo.add_branch("autoloop/21-done", "MERGED")
        repo.add_worktree("autoloop/22-open", "OPEN")
        repo.add_worktree("autoloop/23-dirty", "MERGED", dirty=True)
        repo.add_branch("autoloop/24-orphan", None, unique=1)

        rc = repo.main()

        assert rc == 0
        out = capsys.readouterr().out
        assert out.rstrip().splitlines()[-1] == ("Summary: 2 removed, 3 kept, 1 needing review")

    def test_empty_repo_reports_nothing(self, repo, capsys):
        assert repo.main() == 0
        out = capsys.readouterr().out
        assert "Nothing to prune." in out
        assert "Summary: 0 removed, 0 kept, 0 needing review" in out


class TestStaleWorktreeCount:
    def test_counts_merged_and_closed_worktrees(self, repo):
        repo.add_worktree("autoloop/25-merged", "MERGED")
        repo.add_worktree("autoloop/26-closed", "CLOSED", unique=2)
        repo.add_worktree("autoloop/27-dirty", "MERGED", dirty=True)
        repo.add_worktree("autoloop/28-open", "OPEN")
        repo.add_worktree("autoloop/29-nopr", None)
        repo.add_branch("autoloop/30-branch-only", "MERGED")

        result = repo.prune()

        assert result.stale_worktrees == 3

    def test_dry_run_counts_without_removing(self, repo):
        repo.add_worktree("autoloop/31-merged", "MERGED")
        repo.add_branch("autoloop/32-merged", "MERGED")

        result = repo.prune(dry_run=True)

        assert result.stale_worktrees == 1
        assert repo.mutating_calls() == []
        assert result.removed == 0


class TestGetPrState:
    def _state(self, stdout="", returncode=0):
        cfg = SimpleNamespace(repo="test-owner/test-repo")
        with patch(
            "autoloop.prune.subprocess.run", return_value=_cp([], returncode, stdout=stdout)
        ) as mock_run:
            state = prune.get_pr_state("autoloop/1-x", cfg)
        return state, mock_run.call_args[0][0]

    def test_uses_gh_pr_list_with_head_and_all_states(self):
        _, cmd = self._state("[]")
        assert cmd[:3] == ["gh", "pr", "list"]
        assert cmd[cmd.index("--head") + 1] == "autoloop/1-x"
        assert cmd[cmd.index("--state") + 1] == "all"
        assert cmd[cmd.index("--json") + 1] == "state"
        assert cmd[cmd.index("--repo") + 1] == "test-owner/test-repo"

    def test_no_prs_returns_none(self):
        assert self._state("[]")[0] is None

    def test_merged_beats_closed(self):
        assert self._state('[{"state": "CLOSED"}, {"state": "MERGED"}]')[0] == "MERGED"

    def test_bad_json_is_unknown(self):
        assert self._state("not json")[0] == prune.UNKNOWN

    def test_nonzero_exit_is_unknown(self):
        assert self._state("", returncode=1)[0] == prune.UNKNOWN


class TestHelpers:
    def test_list_worktrees_parses_detached(self, tmp_path):
        ctx = SimpleNamespace(repo_dir=tmp_path)
        out = "worktree /a\nHEAD 1\nbranch refs/heads/main\n\nworktree /b\nHEAD 2\ndetached\n"
        with patch("autoloop.prune.subprocess.run", return_value=_cp([], stdout=out)):
            assert prune.list_worktrees(ctx) == [(Path("/a"), "main"), (Path("/b"), None)]

    def test_list_worktrees_git_failure_returns_empty(self, tmp_path):
        ctx = SimpleNamespace(repo_dir=tmp_path)
        with patch("autoloop.prune.subprocess.run", return_value=_cp([], 128)):
            assert prune.list_worktrees(ctx) == []

    def test_list_autoloop_branches(self, tmp_path):
        ctx = SimpleNamespace(repo_dir=tmp_path)
        out = "autoloop/1-a\nautoloop/2-b\n"
        with patch("autoloop.prune.subprocess.run", return_value=_cp([], stdout=out)) as run:
            assert prune.list_autoloop_branches(ctx) == ["autoloop/1-a", "autoloop/2-b"]
        assert run.call_args[0][0][-1] == "refs/heads/autoloop/"

    def test_is_dirty_when_git_status_fails(self, tmp_path):
        with patch("autoloop.prune.subprocess.run", return_value=_cp([], 128)):
            assert prune.is_dirty(tmp_path) is True

    def test_is_fully_merged_false_when_rev_list_fails(self, tmp_path):
        ctx = SimpleNamespace(repo_dir=tmp_path)
        with patch("autoloop.prune.subprocess.run", return_value=_cp([], 128)):
            assert prune.is_fully_merged("autoloop/1-a", ctx) is False

    def test_is_fully_merged_checks_against_origin_main(self, tmp_path):
        ctx = SimpleNamespace(repo_dir=tmp_path)
        with patch("autoloop.prune.subprocess.run", return_value=_cp([], stdout="0\n")) as run:
            assert prune.is_fully_merged("autoloop/1-a", ctx) is True
        assert run.call_args[0][0] == [
            "git",
            "rev-list",
            "--count",
            "origin/main..refs/heads/autoloop/1-a",
        ]

    def test_detached_worktree_kept(self, repo):
        path = repo.worktree_dir / "autoloop" / "33-detached"
        repo.worktrees.append((path, None))

        result = repo.prune()

        assert repo.mutating_calls() == []
        assert _item(result, "autoloop/33-detached").outcome == "kept (detached)"

    def test_worktree_remove_failure_keeps_branch(self, repo):
        repo.add_worktree("autoloop/34-locked", "MERGED")
        original = repo.run

        def run(cmd, **kwargs):
            if cmd[:3] == ["git", "worktree", "remove"]:
                repo.calls.append(list(cmd))
                return _cp(cmd, 1, stderr="locked")
            return original(cmd, **kwargs)

        with patch("autoloop.prune.subprocess.run", side_effect=run):
            result = prune.prune(repo.ctx, repo.cfg)

        assert ["git", "branch", "-D", "autoloop/34-locked"] not in repo.calls
        assert _item(result, "autoloop/34-locked").outcome == "kept (remove failed)"
