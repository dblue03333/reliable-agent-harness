"""The interactive demo requires a decision after showing the exact proposal."""

import argparse
import json
from pathlib import Path

import pytest

from agent_harness.demo import run_demo


@pytest.mark.parametrize(
    ("choice", "status", "count", "code"),
    [
        ("approve", "completed", 1, 0),
        ("reject", "completed", 0, 0),
        ("yes", "waiting_approval", 0, 3),
        (None, "waiting_approval", 0, 3),
    ],
)
async def test_cli_waits_for_explicit_reviewed_decision(
    monkeypatch, capsys, choice, status, count, code
):
    def answer():
        displayed = capsys.readouterr().err
        assert '"pending_action"' in displayed
        assert '"severity": "high"' in displayed
        assert '"title": "Investigate checkout timeouts"' in displayed
        if choice is None:
            raise EOFError
        return choice

    monkeypatch.setattr("builtins.input", answer)
    args = argparse.Namespace(
        scenario="approval",
        objective="Investigate checkout",
        data_dir=Path(__file__).resolve().parents[1] / "mock_data",
    )
    assert await run_demo(args) == code
    result = json.loads(capsys.readouterr().out)
    assert result["execution"]["status"] == status
    assert result["incident_count"] == count
