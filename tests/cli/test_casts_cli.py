# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

"""Tests for `li casts` CLI command."""

from __future__ import annotations

from lionagi.cli.main import main as cli_main


class TestCastsCLIList:
    def test_list_roles_default(self, capsys):
        code = cli_main(["casts"])
        assert code == 0
        out = capsys.readouterr().out
        assert "analyst" in out
        assert "critic" in out
        assert "implementer" in out

    def test_list_modes_flag(self, capsys):
        code = cli_main(["casts", "--modes"])
        assert code == 0
        out = capsys.readouterr().out
        assert "adversarial" in out
        assert "evidential" in out

    def test_list_has_header(self, capsys):
        code = cli_main(["casts"])
        assert code == 0
        out = capsys.readouterr().out
        assert "NAME" in out
        assert "DESCRIPTION" in out


class TestCastsCLIDetail:
    def test_role_detail(self, capsys):
        code = cli_main(["casts", "analyst"])
        assert code == 0
        out = capsys.readouterr().out
        assert "analyst" in out.lower()
        assert "AnalysisResult" in out

    def test_critic_role_detail(self, capsys):
        code = cli_main(["casts", "critic"])
        assert code == 0
        out = capsys.readouterr().out
        assert "Verdict" in out

    def test_mode_detail(self, capsys):
        code = cli_main(["casts", "adversarial"])
        assert code == 0
        out = capsys.readouterr().out
        assert "adversarial" in out.lower()

    def test_unknown_name_returns_error(self, capsys):
        code = cli_main(["casts", "nonexistent-role-xyz"])
        assert code == 1

    def test_postmortem_lead_role_resolves(self, capsys):
        code = cli_main(["casts", "postmortem-lead"])
        assert code == 0

    def test_evidential_mode_detail(self, capsys):
        code = cli_main(["casts", "evidential"])
        assert code == 0
        out = capsys.readouterr().out
        assert "evidential" in out.lower()
