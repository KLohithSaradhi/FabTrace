from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from fabtrace.cli import main
from fabtrace.orchestration import PipelineExecutionError, execute_plan_pipeline
from fabtrace.web import create_app
from test_plan_simulation import DATA_DIR, _write_plan, _write_scenario
from test_web import ASGITestClient


def test_execute_plan_pipeline_completes_and_resumes_checkpoints(
    tmp_path: Path,
) -> None:
    plan_path = _write_plan(tmp_path)
    scenario_path = _write_scenario(tmp_path)
    database_path = tmp_path / "state" / "fabtrace.db"
    runs_dir = tmp_path / "runs"

    first = execute_plan_pipeline(
        plan_path=plan_path,
        scenario_path=scenario_path,
        data_dir=DATA_DIR,
        database_path=database_path,
        output_dir=runs_dir,
    )
    second = execute_plan_pipeline(
        plan_path=plan_path,
        scenario_path=scenario_path,
        data_dir=DATA_DIR,
        database_path=database_path,
        output_dir=runs_dir,
    )

    assert first.status == "COMPLETED"
    assert [stage.stage_name for stage in first.stages] == [
        "register_plan",
        "simulate",
        "ingest",
        "extract",
        "reconcile",
        "analyze",
    ]
    assert not any(stage.checkpoint_reused for stage in first.stages)
    assert second.pipeline_run_id == first.pipeline_run_id
    assert all(stage.checkpoint_reused for stage in second.stages)
    with sqlite3.connect(database_path) as connection:
        assert connection.execute("PRAGMA user_version").fetchone()[0] == 6
        assert connection.execute("SELECT COUNT(*) FROM pipeline_runs").fetchone()[0] == 1
        assert connection.execute(
            "SELECT COUNT(*) FROM pipeline_stage_attempts"
        ).fetchone()[0] == 6
        assert connection.execute(
            "SELECT COUNT(*) FROM pipeline_run_events"
        ).fetchone()[0] == 4
        analytics_as_of = connection.execute(
            "SELECT as_of_time FROM analytics_runs"
        ).fetchone()[0]
        latest_received = connection.execute(
            "SELECT MAX(received_at) FROM raw_provider_messages"
        ).fetchone()[0]
        assert analytics_as_of == latest_received


def test_pipeline_cli_and_customer_detail_show_stage_status(
    tmp_path: Path,
    capsys,
) -> None:
    plan_path = _write_plan(tmp_path)
    scenario_path = _write_scenario(tmp_path)
    database_path = tmp_path / "fabtrace.db"
    runs_dir = tmp_path / "runs"

    assert main(
        [
            "pipeline",
            "execute-plan",
            "--plan",
            str(plan_path),
            "--scenario",
            str(scenario_path),
            "--data-dir",
            str(DATA_DIR),
            "--database",
            str(database_path),
            "--output-dir",
            str(runs_dir),
        ]
    ) == 0
    output = capsys.readouterr().out
    assert "Plan pipeline: PASS" in output
    assert "- analyze: COMPLETED" in output

    client = ASGITestClient(create_app(database_path))
    api = client.get(
        "/api/v1/customer-orders/CHAIN-001",
        params={"scenario_id": "chain-execution"},
    )
    page = client.get(
        "/customer-orders/CHAIN-001",
        params={"scenario_id": "chain-execution"},
    )
    assert api.status_code == 200
    assert api.json()["pipeline_runs"][0]["status"] == "COMPLETED"
    assert len(api.json()["pipeline_runs"][0]["stages"]) == 6
    assert page.status_code == 200
    assert "Pipeline status" in page.text
    assert "register plan" in page.text


def test_failed_stage_resumes_without_reexecuting_completed_stages(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    plan_path = _write_plan(tmp_path)
    scenario_path = _write_scenario(tmp_path)
    database_path = tmp_path / "fabtrace.db"
    runs_dir = tmp_path / "runs"
    import fabtrace.orchestration.pipeline as pipeline_module

    real_analyze = pipeline_module.analyze_orders

    def fail_analytics(*args, **kwargs):
        raise RuntimeError("injected analytics failure")

    monkeypatch.setattr(pipeline_module, "analyze_orders", fail_analytics)
    with pytest.raises(PipelineExecutionError, match="injected analytics failure"):
        execute_plan_pipeline(
            plan_path=plan_path,
            scenario_path=scenario_path,
            data_dir=DATA_DIR,
            database_path=database_path,
            output_dir=runs_dir,
        )

    monkeypatch.setattr(pipeline_module, "analyze_orders", real_analyze)
    resumed = execute_plan_pipeline(
        plan_path=plan_path,
        scenario_path=scenario_path,
        data_dir=DATA_DIR,
        database_path=database_path,
        output_dir=runs_dir,
    )

    assert [stage.checkpoint_reused for stage in resumed.stages] == [
        True,
        True,
        True,
        True,
        True,
        False,
    ]
    assert resumed.stages[-1].attempt_number == 2
    with sqlite3.connect(database_path) as connection:
        failed = connection.execute(
            "SELECT COUNT(*) FROM pipeline_stage_events WHERE status = 'FAILED'"
        ).fetchone()[0]
        assert failed == 1
