from __future__ import annotations

import json
import subprocess
from unittest.mock import patch

from autoloop.cli import build_parser


def test_jev_report_subcommand_registered():
    parser = build_parser()
    args = parser.parse_args(["jev-report"])
    assert args.command == "jev-report"


def test_jev_report_publish_flag():
    parser = build_parser()
    args = parser.parse_args(["jev-report", "--publish"])
    assert args.publish is True
    assert args.pr is False


def test_jev_report_publish_and_pr_flags():
    parser = build_parser()
    args = parser.parse_args(["jev-report", "--publish", "--pr"])
    assert args.publish is True
    assert args.pr is True


def test_jev_report_no_flags_default():
    parser = build_parser()
    args = parser.parse_args(["jev-report"])
    assert args.publish is False
    assert args.pr is False


def test_jev_report_pr_flag_without_publish():
    parser = build_parser()
    args = parser.parse_args(["jev-report", "--pr"])
    assert args.pr is True
    assert args.publish is False


def test_jev_report_dispatch_calls_main(tmp_path):
    called = {}

    def fake_main(*, ctx, publish, pr, gate_low, gate_high):
        called["ctx"] = ctx
        called["publish"] = publish
        called["pr"] = pr

    with (
        patch("autoloop.jev_report.main", fake_main),
        patch("autoloop.config.RepoContext") as mock_ctx_cls,
    ):
        mock_ctx_cls.return_value = "fake_ctx"

        from autoloop.cli import main

        with patch("sys.argv", ["autoloop", "jev-report"]):
            main()

    assert called["publish"] is False
    assert called["pr"] is False


def test_jev_report_dispatch_with_publish(tmp_path):
    called = {}

    def fake_main(*, ctx, publish, pr, gate_low, gate_high):
        called["publish"] = publish
        called["pr"] = pr

    with (
        patch("autoloop.jev_report.main", fake_main),
        patch("autoloop.config.RepoContext") as mock_ctx_cls,
    ):
        mock_ctx_cls.return_value = "fake_ctx"

        from autoloop.cli import main

        with patch("sys.argv", ["autoloop", "jev-report", "--publish"]):
            main()

    assert called["publish"] is True
    assert called["pr"] is False


def test_jev_report_dispatch_with_publish_pr(tmp_path):
    called = {}

    def fake_main(*, ctx, publish, pr, gate_low, gate_high):
        called["publish"] = publish
        called["pr"] = pr

    with (
        patch("autoloop.jev_report.main", fake_main),
        patch("autoloop.config.RepoContext") as mock_ctx_cls,
    ):
        mock_ctx_cls.return_value = "fake_ctx"

        from autoloop.cli import main

        with patch("sys.argv", ["autoloop", "jev-report", "--publish", "--pr"]):
            main()

    assert called["publish"] is True
    assert called["pr"] is True


def test_jev_report_dispatch_passes_configured_band(tmp_path):
    from autoloop.config import AutoLoopConfig

    called = {}

    def fake_main(*, ctx, publish, pr, gate_low, gate_high):
        called["gate_low"] = gate_low
        called["gate_high"] = gate_high

    cfg = AutoLoopConfig(jev_gate_low=0.2, jev_gate_high=0.8)
    with (
        patch("autoloop.jev_report.main", fake_main),
        patch("autoloop.config.RepoContext") as mock_ctx_cls,
        patch("autoloop.config.load_config", return_value=cfg),
    ):
        mock_ctx_cls.return_value = "fake_ctx"

        from autoloop.cli import main

        with patch("sys.argv", ["autoloop", "jev-report"]):
            main()

    assert called == {"gate_low": 0.2, "gate_high": 0.8}


def test_jev_report_main_uses_configured_band(tmp_path):
    from autoloop.jev_report import main as jev_report_main

    ctx = _make_ctx(tmp_path)
    (ctx.data_dir / "jev_decisions.jsonl").write_text(
        json.dumps(
            {
                "timestamp": "2026-01-05T00:00:00",
                "issue": 1,
                "point": "auto-merge",
                "jev_call": {"meets_acceptance_criteria": 0.7},
                "incumbent_call": {"approved": True},
            }
        )
        + "\n"
    )
    jev_report_main(ctx=ctx, publish=False, pr=False, gate_low=0.15, gate_high=0.85)
    md = (tmp_path / "JEV.md").read_text()
    assert "**Band:** 0.15–0.85 (from config)" in md
    assert '"Pipeline (0.15-0.85)"' in md
    # The Calibration section bins at fixed edges (0.35, 0.65 among them), so check
    # only that the old hardcoded band doesn't leak in.
    assert "0.35–0.65" not in md
    assert "0.35-0.65" not in md


def _make_ctx(tmp_path):
    data_dir = tmp_path / "data"
    data_dir.mkdir(exist_ok=True)
    (data_dir / "jev_decisions.jsonl").write_text("")

    class FakeCtx:
        pass

    ctx = FakeCtx()
    ctx.repo_dir = tmp_path
    ctx.data_dir = data_dir
    return ctx


def test_jev_report_main_writes_jev_md_locally(tmp_path):
    from autoloop.jev_report import main as jev_report_main

    ctx = _make_ctx(tmp_path)
    jev_report_main(ctx=ctx, publish=False, pr=False)

    jev_path = tmp_path / "JEV.md"
    assert jev_path.exists()
    content = jev_path.read_text()
    assert "# JEV Report" in content


def test_jev_report_main_no_git_ops_without_publish(tmp_path):
    from autoloop.jev_report import main as jev_report_main

    ctx = _make_ctx(tmp_path)
    calls = []
    original_run = subprocess.run

    def spy_run(cmd, **kw):
        calls.append(cmd)
        return original_run(cmd, **kw)

    with patch("autoloop.jev_report.subprocess.run", spy_run):
        jev_report_main(ctx=ctx, publish=False, pr=False)

    git_calls = [c for c in calls if c[0] in ("git", "gh")]
    assert git_calls == []


def test_jev_report_publish_requires_main_branch(tmp_path, capsys):
    from autoloop.jev_report import main as jev_report_main

    ctx = _make_ctx(tmp_path)

    def fake_run(cmd, **kw):
        if cmd[:3] == ["git", "rev-parse", "--abbrev-ref"]:
            return subprocess.CompletedProcess(cmd, 0, stdout="feature-branch\n", stderr="")
        return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")

    with patch("autoloop.jev_report.subprocess.run", fake_run):
        jev_report_main(ctx=ctx, publish=True, pr=False)

    captured = capsys.readouterr()
    assert "--publish must be run from the main branch" in captured.out


def _fake_subprocess_all_ok(calls=None):
    if calls is None:
        calls = []

    def _fake(cmd, **kw):
        calls.append(cmd)
        stdout = ""
        if cmd[:2] == ["gh", "pr"]:
            stdout = "https://github.com/owner/repo/pull/99\n"
        elif cmd[:3] == ["git", "rev-parse", "--abbrev-ref"]:
            stdout = "main\n"
        return subprocess.CompletedProcess(cmd, 0, stdout=stdout, stderr="")

    return _fake


def test_jev_report_publish_pr_creates_branch_and_pr(tmp_path, capsys):
    from autoloop.jev_report import main as jev_report_main

    ctx = _make_ctx(tmp_path)
    calls = []
    with patch("autoloop.jev_report.subprocess.run", _fake_subprocess_all_ok(calls)):
        jev_report_main(ctx=ctx, publish=True, pr=True)

    captured = capsys.readouterr()
    assert "PR created:" in captured.out
    cmd_strs = [" ".join(str(a) for a in c) for c in calls]
    assert any("checkout -b" in s for s in cmd_strs)
    assert any("gh pr create" in s for s in cmd_strs)


def _render_empty_jev_md(generated_at):
    from autoloop.jev_report import ParsedDecisions, build_report

    return build_report(ParsedDecisions(backfilled=[], live=[]), generated_at)


def _fake_subprocess_main_has(published, calls):
    ok = _fake_subprocess_all_ok(calls)

    def _fake(cmd, **kw):
        if cmd[:2] == ["git", "show"]:
            calls.append(cmd)
            return subprocess.CompletedProcess(cmd, 0, stdout=published, stderr="")
        return ok(cmd, **kw)

    return _fake


def test_jev_report_publish_pr_skips_when_unchanged_from_main(tmp_path, capsys):
    from autoloop.jev_report import main as jev_report_main

    ctx = _make_ctx(tmp_path)
    published = _render_empty_jev_md("2026-11-07 07:00 UTC")
    calls = []
    with patch("autoloop.jev_report.subprocess.run", _fake_subprocess_main_has(published, calls)):
        jev_report_main(ctx=ctx, publish=True, pr=True)

    captured = capsys.readouterr()
    assert "unchanged from main, skipping" in captured.out
    cmd_strs = [" ".join(str(a) for a in c) for c in calls]
    assert any("git show main:JEV.md" in s for s in cmd_strs)
    assert not any("checkout -b" in s for s in cmd_strs)
    assert not any("gh pr create" in s for s in cmd_strs)
    assert not (tmp_path / "JEV.md").exists()


def test_jev_report_publish_skips_direct_commit_when_unchanged(tmp_path, capsys):
    from autoloop.jev_report import main as jev_report_main

    ctx = _make_ctx(tmp_path)
    published = _render_empty_jev_md("2026-11-07 07:00 UTC")
    calls = []
    with patch("autoloop.jev_report.subprocess.run", _fake_subprocess_main_has(published, calls)):
        jev_report_main(ctx=ctx, publish=True, pr=False)

    cmd_strs = [" ".join(str(a) for a in c) for c in calls]
    assert not any("git commit" in s for s in cmd_strs)
    assert not any("git push" in s for s in cmd_strs)


def test_jev_report_publish_pr_proceeds_when_body_differs(tmp_path, capsys):
    from autoloop.jev_report import main as jev_report_main

    ctx = _make_ctx(tmp_path)
    published = _render_empty_jev_md("2026-11-07 07:00 UTC").replace("n=0", "n=3")
    calls = []
    with patch("autoloop.jev_report.subprocess.run", _fake_subprocess_main_has(published, calls)):
        jev_report_main(ctx=ctx, publish=True, pr=True)

    captured = capsys.readouterr()
    assert "PR created:" in captured.out
    assert (tmp_path / "JEV.md").exists()


def test_published_jev_md_none_when_missing_on_main(tmp_path):
    from autoloop.jev_report import _published_jev_md

    ctx = _make_ctx(tmp_path)

    def fake_run(cmd, **kw):
        return subprocess.CompletedProcess(cmd, 128, stdout="", stderr="fatal: path not found")

    with patch("autoloop.jev_report.subprocess.run", fake_run):
        assert _published_jev_md(ctx) is None


def test_published_jev_md_none_without_git(tmp_path):
    from autoloop.jev_report import _published_jev_md

    ctx = _make_ctx(tmp_path)

    def fake_run(cmd, **kw):
        raise FileNotFoundError("git")

    with patch("autoloop.jev_report.subprocess.run", fake_run):
        assert _published_jev_md(ctx) is None


def test_published_jev_md_returns_main_content(tmp_path):
    from autoloop.jev_report import _published_jev_md

    ctx = _make_ctx(tmp_path)

    def fake_run(cmd, **kw):
        assert cmd == ["git", "show", "main:JEV.md"]
        return subprocess.CompletedProcess(cmd, 0, stdout="# JEV Report\n", stderr="")

    with patch("autoloop.jev_report.subprocess.run", fake_run):
        assert _published_jev_md(ctx) == "# JEV Report\n"


def test_jev_report_publish_only_commits_directly(tmp_path, capsys):
    from autoloop.jev_report import main as jev_report_main

    ctx = _make_ctx(tmp_path)
    calls = []
    with patch("autoloop.jev_report.subprocess.run", _fake_subprocess_all_ok(calls)):
        jev_report_main(ctx=ctx, publish=True, pr=False)

    cmd_strs = [" ".join(str(a) for a in c) for c in calls]
    assert any("git commit" in s for s in cmd_strs)
    assert any("git push" in s for s in cmd_strs)
    assert not any("gh pr create" in s for s in cmd_strs)
