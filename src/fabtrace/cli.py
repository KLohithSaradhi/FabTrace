from __future__ import annotations

import argparse
from pathlib import Path

from fabtrace.analytics import AnalyticsError, analyze_orders
from fabtrace.extraction import (
    EXTRACTION_VERSION,
    SUPPORTED_EXTRACTOR_VERSIONS,
    EvaluationError,
    ExtractionError,
    evaluate_extraction,
    extract_provider_updates,
)
from fabtrace.ingestion import IngestionError, ingest_provider_updates
from fabtrace.orchestration import PipelineExecutionError, execute_plan_pipeline
from fabtrace.planning import (
    PlanArtifactStore,
    PlanOrderRequest,
    PlanRegistrationError,
    PlanningError,
    plan_order,
    register_plan,
)
from fabtrace.products import ProductConfigStore
from fabtrace.reconciliation import (
    RECONCILIATION_VERSION,
    SUPPORTED_RECONCILIATION_VERSIONS,
    ReconciliationError,
    reconcile_order_state,
)
from fabtrace.simulation.config import SimulationConfigError
from fabtrace.simulation.runner import (
    SimulationRunError,
    run_plan_simulation,
    run_simulation,
)
from fabtrace.source.loader import SourceDataError, load_source_catalog
from fabtrace.source.validation import validate_source_catalog


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="fabtrace",
        description="FabTrace supply-chain data and simulation tools.",
    )
    commands = parser.add_subparsers(dest="command", required=True)

    data_parser = commands.add_parser("data", help="Work with source data.")
    data_commands = data_parser.add_subparsers(dest="data_command", required=True)

    validate_parser = data_commands.add_parser(
        "validate",
        help="Load and validate the immutable ETO source tables.",
    )
    validate_parser.add_argument(
        "--data-dir",
        type=Path,
        default=Path("Data"),
        help="Directory containing the five ETO CSV files (default: Data).",
    )

    simulate_parser = commands.add_parser(
        "simulate",
        help="Run a validated provider simulation scenario.",
    )
    simulate_parser.add_argument(
        "--scenario",
        type=Path,
        required=True,
        help="Path to a versioned scenario JSON file.",
    )
    simulate_parser.add_argument(
        "--data-dir",
        type=Path,
        default=Path("Data"),
        help="Directory containing the five ETO CSV files (default: Data).",
    )
    simulate_parser.add_argument(
        "--output-dir",
        type=Path,
        default=Path(".fabtrace/runs"),
        help="Parent directory for generated runs (default: .fabtrace/runs).",
    )

    simulate_plan_parser = commands.add_parser(
        "simulate-plan",
        help="Execute an immutable planner-v1 plan with provider actors.",
    )
    simulate_plan_parser.add_argument(
        "--plan",
        type=Path,
        required=True,
        help="Path to an immutable plan.json artifact.",
    )
    simulate_plan_parser.add_argument(
        "--scenario",
        type=Path,
        required=True,
        help="Path to a plan-execution scenario JSON file.",
    )
    simulate_plan_parser.add_argument(
        "--data-dir",
        type=Path,
        default=Path("Data"),
        help="Directory containing the five ETO CSV files (default: Data).",
    )
    simulate_plan_parser.add_argument(
        "--output-dir",
        type=Path,
        default=Path(".fabtrace/runs"),
        help="Parent directory for generated runs (default: .fabtrace/runs).",
    )

    ingest_parser = commands.add_parser(
        "ingest",
        help="Ingest provider messages into durable raw storage.",
    )
    ingest_commands = ingest_parser.add_subparsers(
        dest="ingest_command", required=True
    )
    provider_updates_parser = ingest_commands.add_parser(
        "provider-updates",
        help="Ingest a bounded provider_updates.jsonl batch.",
    )
    provider_updates_parser.add_argument(
        "--input",
        type=Path,
        required=True,
        help="Path to a provider_updates.jsonl file.",
    )
    provider_updates_parser.add_argument(
        "--database",
        type=Path,
        default=Path(".fabtrace/state/fabtrace.db"),
        help=(
            "SQLite database for raw retention "
            "(default: .fabtrace/state/fabtrace.db)."
        ),
    )

    extract_parser = commands.add_parser(
        "extract",
        help="Extract structured claims from retained provider messages.",
    )
    extract_commands = extract_parser.add_subparsers(
        dest="extract_command", required=True
    )
    extract_updates_parser = extract_commands.add_parser(
        "provider-updates",
        help="Run a deterministic versioned extractor for one scenario.",
    )
    extract_updates_parser.add_argument(
        "--database",
        type=Path,
        default=Path(".fabtrace/state/fabtrace.db"),
        help=(
            "SQLite database containing raw provider messages "
            "(default: .fabtrace/state/fabtrace.db)."
        ),
    )
    extract_updates_parser.add_argument(
        "--scenario-id",
        required=True,
        help="Exact scenario boundary to extract.",
    )
    extract_updates_parser.add_argument(
        "--extractor-version",
        choices=SUPPORTED_EXTRACTOR_VERSIONS,
        default=EXTRACTION_VERSION,
        help=f"Extractor contract to run (default: {EXTRACTION_VERSION}).",
    )

    evaluate_parser = commands.add_parser(
        "evaluate",
        help="Evaluate completed outputs through an isolated truth path.",
    )
    evaluate_commands = evaluate_parser.add_subparsers(
        dest="evaluate_command", required=True
    )
    evaluate_extraction_parser = evaluate_commands.add_parser(
        "extraction",
        help="Compare completed versioned claims with simulator ground truth.",
    )
    evaluate_extraction_parser.add_argument(
        "--database",
        type=Path,
        default=Path(".fabtrace/state/fabtrace.db"),
        help=(
            "SQLite database containing completed claims "
            "(default: .fabtrace/state/fabtrace.db)."
        ),
    )
    evaluate_extraction_parser.add_argument(
        "--ground-truth",
        type=Path,
        required=True,
        help="Private ground_truth.jsonl used only by this evaluator.",
    )
    evaluate_extraction_parser.add_argument(
        "--scenario-id",
        required=True,
        help="Exact scenario boundary to evaluate.",
    )
    evaluate_extraction_parser.add_argument(
        "--extractor-version",
        choices=SUPPORTED_EXTRACTOR_VERSIONS,
        default=EXTRACTION_VERSION,
        help=f"Extractor contract to evaluate (default: {EXTRACTION_VERSION}).",
    )

    reconcile_parser = commands.add_parser(
        "reconcile",
        help="Reconcile extracted claims into historical operational state.",
    )
    reconcile_commands = reconcile_parser.add_subparsers(
        dest="reconcile_command", required=True
    )
    order_state_parser = reconcile_commands.add_parser(
        "order-state",
        help="Build append-only versioned order-state snapshots for one scenario.",
    )
    order_state_parser.add_argument(
        "--database",
        type=Path,
        default=Path(".fabtrace/state/fabtrace.db"),
        help=(
            "SQLite database containing extracted claims "
            "(default: .fabtrace/state/fabtrace.db)."
        ),
    )
    order_state_parser.add_argument(
        "--scenario-id",
        required=True,
        help="Exact scenario boundary to reconcile.",
    )
    order_state_parser.add_argument(
        "--reconciliation-version",
        choices=SUPPORTED_RECONCILIATION_VERSIONS,
        default=RECONCILIATION_VERSION,
        help=(
            "Reconciliation contract to run "
            f"(default: {RECONCILIATION_VERSION})."
        ),
    )

    analyze_parser = commands.add_parser(
        "analyze",
        help="Compute reproducible operational metrics from reconciled state.",
    )
    analyze_commands = analyze_parser.add_subparsers(
        dest="analyze_command", required=True
    )
    analyze_orders_parser = analyze_commands.add_parser(
        "orders",
        help="Build operational-metrics-v1 snapshots for one scenario.",
    )
    analyze_orders_parser.add_argument(
        "--database",
        type=Path,
        default=Path(".fabtrace/state/fabtrace.db"),
        help=(
            "SQLite database containing reconciled order state "
            "(default: .fabtrace/state/fabtrace.db)."
        ),
    )
    analyze_orders_parser.add_argument(
        "--scenario-id",
        required=True,
        help="Exact scenario boundary to analyze.",
    )
    analyze_orders_parser.add_argument(
        "--as-of",
        required=True,
        help="Timezone-aware cutoff, such as 2026-01-16T00:00:00Z.",
    )

    register_parser = commands.add_parser(
        "register",
        help="Register immutable artifacts for operational joins.",
    )
    register_commands = register_parser.add_subparsers(
        dest="register_command", required=True
    )
    register_plan_parser = register_commands.add_parser(
        "plan",
        help="Register one immutable plan and its provider-order graph.",
    )
    register_plan_parser.add_argument(
        "--plan",
        type=Path,
        required=True,
        help="Path to an immutable plan.json or source_plan.json artifact.",
    )

    pipeline_parser = commands.add_parser(
        "pipeline",
        help="Run checkpointed local data pipelines.",
    )
    pipeline_commands = pipeline_parser.add_subparsers(
        dest="pipeline_command", required=True
    )
    execute_plan_parser = pipeline_commands.add_parser(
        "execute-plan",
        help="Register, simulate, ingest, extract, reconcile, and analyze a plan.",
    )
    execute_plan_parser.add_argument(
        "--plan", type=Path, required=True, help="Immutable plan artifact."
    )
    execute_plan_parser.add_argument(
        "--scenario", type=Path, required=True, help="Plan-execution scenario JSON."
    )
    execute_plan_parser.add_argument(
        "--data-dir", type=Path, default=Path("Data")
    )
    execute_plan_parser.add_argument(
        "--database", type=Path, default=Path(".fabtrace/state/fabtrace.db")
    )
    execute_plan_parser.add_argument(
        "--output-dir", type=Path, default=Path(".fabtrace/runs")
    )
    register_plan_parser.add_argument(
        "--database",
        type=Path,
        default=Path(".fabtrace/state/fabtrace.db"),
        help="FabTrace SQLite database (default: .fabtrace/state/fabtrace.db).",
    )

    plan_parser = commands.add_parser(
        "plan",
        help="Build immutable customer and provider order plans.",
    )
    plan_commands = plan_parser.add_subparsers(
        dest="plan_command", required=True
    )
    plan_order_parser = plan_commands.add_parser(
        "order",
        help="Expand a published product into a planner-v1 order plan.",
    )
    plan_order_parser.add_argument("--product-id", required=True)
    plan_order_parser.add_argument("--product-version", type=int, required=True)
    plan_order_parser.add_argument("--order-id", required=True)
    plan_order_parser.add_argument("--quantity", type=float, required=True)
    plan_order_parser.add_argument("--requested-at", required=True)
    plan_order_parser.add_argument(
        "--product-config-dir",
        type=Path,
        default=Path(".fabtrace/config/products"),
    )
    plan_order_parser.add_argument(
        "--plans-dir",
        type=Path,
        default=Path(".fabtrace/plans"),
    )

    serve_parser = commands.add_parser(
        "serve",
        help="Run the local dashboard, product designer, and planner.",
    )
    serve_parser.add_argument(
        "--database",
        type=Path,
        default=Path(".fabtrace/state/fabtrace.db"),
        help=(
            "SQLite database to open read-only "
            "(default: .fabtrace/state/fabtrace.db)."
        ),
    )
    serve_parser.add_argument(
        "--product-config-dir",
        type=Path,
        default=Path(".fabtrace/config/products"),
        help=(
            "Directory for immutable published products "
            "(default: .fabtrace/config/products)."
        ),
    )
    serve_parser.add_argument(
        "--product-draft-dir",
        type=Path,
        default=Path(".fabtrace/drafts/products"),
        help=(
            "Directory for mutable product drafts "
            "(default: .fabtrace/drafts/products)."
        ),
    )
    serve_parser.add_argument(
        "--plans-dir",
        type=Path,
        default=Path(".fabtrace/plans"),
        help="Directory for immutable order plans (default: .fabtrace/plans).",
    )
    serve_parser.add_argument(
        "--data-dir",
        type=Path,
        default=Path("Data"),
        help="Directory containing the immutable ETO CSV files (default: Data).",
    )
    serve_parser.add_argument(
        "--host",
        default="127.0.0.1",
        help="Interface to bind (default: 127.0.0.1).",
    )
    serve_parser.add_argument(
        "--port",
        type=int,
        default=8000,
        help="TCP port to bind (default: 8000).",
    )
    return parser


def _validate_data(data_dir: Path) -> int:
    try:
        catalog = load_source_catalog(data_dir)
    except SourceDataError as exc:
        print(f"Validation: FAIL\n{exc}")
        return 2

    report = validate_source_catalog(catalog)
    print("FabTrace source data validation")
    print(f"Data directory: {data_dir.resolve()}")
    print(f"Stages: {len(catalog.stages)}")
    print(f"Inputs: {len(catalog.inputs)}")
    print(f"Providers: {len(catalog.providers)} logical IDs")
    print(f"Provider source rows: {catalog.provider_row_count}")
    print(f"Provision relationships: {len(catalog.provisions)}")
    print(f"Sequence relationships: {len(catalog.sequence)}")

    if report.errors:
        print(f"Validation: FAIL ({len(report.errors)} errors)")
        for error in report.errors:
            print(f"- {error}")
        return 1

    print("Validation: PASS (0 errors)")
    return 0


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    if args.command == "data" and args.data_command == "validate":
        return _validate_data(args.data_dir)
    if args.command == "simulate":
        command = (
            "fabtrace",
            "simulate",
            "--scenario",
            str(args.scenario),
            "--data-dir",
            str(args.data_dir),
            "--output-dir",
            str(args.output_dir),
        )
        try:
            result = run_simulation(
                scenario_path=args.scenario,
                data_dir=args.data_dir,
                runs_dir=args.output_dir,
                command=command,
            )
        except (SourceDataError, SimulationConfigError, SimulationRunError) as exc:
            print(f"Simulation: FAIL\n{exc}")
            return 2
        print("Simulation: PASS")
        print(f"Run directory: {result.run_directory}")
        print(f"Ground-truth events: {result.event_count}")
        print(f"Provider messages: {result.message_count}")
        return 0
    if args.command == "simulate-plan":
        command = (
            "fabtrace",
            "simulate-plan",
            "--plan",
            str(args.plan),
            "--scenario",
            str(args.scenario),
            "--data-dir",
            str(args.data_dir),
            "--output-dir",
            str(args.output_dir),
        )
        try:
            result = run_plan_simulation(
                plan_path=args.plan,
                scenario_path=args.scenario,
                data_dir=args.data_dir,
                runs_dir=args.output_dir,
                command=command,
            )
        except (SourceDataError, SimulationConfigError, SimulationRunError) as exc:
            print(f"Plan simulation: FAIL\n{exc}")
            return 2
        print("Plan simulation: PASS")
        print(f"Run directory: {result.run_directory}")
        print(f"Ground-truth events: {result.event_count}")
        print(f"Provider messages: {result.message_count}")
        return 0
    if args.command == "ingest" and args.ingest_command == "provider-updates":
        try:
            result = ingest_provider_updates(args.input, args.database)
        except IngestionError as exc:
            print(f"Ingestion: FAIL\n{exc}")
            return 2
        print("Ingestion: PASS")
        print(f"Batch ID: {result.batch_id}")
        print(f"Database: {result.database_path}")
        print(f"Input lines: {result.total_lines}")
        print(f"Accepted: {result.accepted_count}")
        print(f"Duplicates: {result.duplicate_count}")
        print(f"Quarantined: {result.quarantined_count}")
        return 0
    if args.command == "extract" and args.extract_command == "provider-updates":
        try:
            result = extract_provider_updates(
                args.database,
                args.scenario_id,
                args.extractor_version,
            )
        except ExtractionError as exc:
            print(f"Extraction: FAIL\n{exc}")
            return 2
        print("Extraction: PASS")
        print(f"Extraction run ID: {result.extraction_run_id}")
        print(f"Extractor: {result.extractor_version}")
        print(f"Scenario: {result.scenario_id}")
        print(f"Messages selected: {result.selected_count}")
        print(f"Extracted: {result.extracted_count}")
        print(f"Abstained: {result.abstained_count}")
        print(f"Review required: {result.review_count}")
        print(f"Failed: {result.failed_count}")
        print(f"Already processed: {result.skipped_count}")
        print(f"Claims emitted: {result.claim_count}")
        return 0
    if args.command == "evaluate" and args.evaluate_command == "extraction":
        try:
            result = evaluate_extraction(
                args.database,
                args.ground_truth,
                args.scenario_id,
                args.extractor_version,
            )
        except EvaluationError as exc:
            print(f"Evaluation: FAIL\n{exc}")
            return 2
        print("Evaluation: PASS")
        print(f"Extractor: {result.extractor_version}")
        print(f"Scenario: {result.scenario_id}")
        print(f"Messages evaluated: {result.message_count}")
        print(f"Abstained: {result.abstained_count}")
        print(f"Review required: {result.review_count}")
        print("Field metrics:")
        for metric in result.metrics:
            print(
                f"- {metric.claim_type}: correct={metric.correct_count}/"
                f"{metric.expected_count}, precision={metric.precision:.3f}, "
                f"recall={metric.recall:.3f}, missing={metric.missing_count}, "
                f"incorrect={metric.incorrect_count}, "
                f"unexpected={metric.unexpected_count}"
            )
        print(
            f"Overall exact: {result.correct_count}/{result.expected_count}"
        )
        return 0
    if args.command == "reconcile" and args.reconcile_command == "order-state":
        try:
            result = reconcile_order_state(
                args.database,
                args.scenario_id,
                args.reconciliation_version,
            )
        except ReconciliationError as exc:
            print(f"Reconciliation: FAIL\n{exc}")
            return 2
        print("Reconciliation: PASS")
        print(f"Reconciliation run ID: {result.reconciliation_run_id}")
        print(f"Version: {result.reconciliation_version}")
        print(f"Scenario: {result.scenario_id}")
        print(f"Messages selected: {result.selected_count}")
        print(f"Applied: {result.applied_count}")
        print(f"Stale: {result.stale_count}")
        print(f"Conflicts: {result.conflict_count}")
        print(f"Already processed: {result.skipped_count}")
        print(f"Snapshots emitted: {result.snapshot_count}")
        return 0
    if args.command == "analyze" and args.analyze_command == "orders":
        try:
            result = analyze_orders(
                args.database,
                args.scenario_id,
                args.as_of,
            )
        except AnalyticsError as exc:
            print(f"Analytics: FAIL\n{exc}")
            return 2
        print("Analytics: PASS")
        print(f"Analytics run ID: {result.analytics_run_id}")
        print(f"Metric version: {result.metric_version}")
        print(f"Reconciliation version: {result.reconciliation_version}")
        print(f"Scenario: {result.scenario_id}")
        print(f"As of: {result.as_of_time}")
        print(f"Orders selected: {result.selected_order_count}")
        print(f"Computed: {result.computed_count}")
        print(f"Already computed: {result.skipped_count}")
        return 0
    if args.command == "register" and args.register_command == "plan":
        try:
            result = register_plan(args.plan, args.database)
        except PlanRegistrationError as exc:
            print(f"Plan registration: FAIL\n{exc}")
            return 2
        print("Plan registration: PASS")
        print(f"Order: {result.order_id}")
        print(f"Database: {result.database_path}")
        print(f"Plan SHA-256: {result.plan_sha256}")
        print(f"Provider orders: {result.provider_order_count}")
        print(f"Dependencies: {result.dependency_count}")
        print("Result: registered" if result.registered else "Result: replay")
        return 0
    if args.command == "pipeline" and args.pipeline_command == "execute-plan":
        command = (
            "fabtrace",
            "pipeline",
            "execute-plan",
            "--plan",
            str(args.plan),
            "--scenario",
            str(args.scenario),
            "--data-dir",
            str(args.data_dir),
            "--database",
            str(args.database),
            "--output-dir",
            str(args.output_dir),
        )
        try:
            result = execute_plan_pipeline(
                plan_path=args.plan,
                scenario_path=args.scenario,
                data_dir=args.data_dir,
                database_path=args.database,
                output_dir=args.output_dir,
                command=command,
            )
        except (
            AnalyticsError,
            ExtractionError,
            IngestionError,
            PipelineExecutionError,
            PlanRegistrationError,
            ReconciliationError,
            SimulationConfigError,
            SimulationRunError,
            SourceDataError,
        ) as exc:
            print(f"Plan pipeline: FAIL\n{exc}")
            return 2
        print("Plan pipeline: PASS")
        print(f"Pipeline run ID: {result.pipeline_run_id}")
        print(f"Customer order: {result.customer_order_id}")
        print(f"Scenario: {result.scenario_id}")
        for stage in result.stages:
            source = "checkpoint" if stage.checkpoint_reused else "executed"
            print(
                f"- {stage.stage_name}: {stage.status} "
                f"(attempt {stage.attempt_number}, {source})"
            )
        return 0
    if args.command == "plan" and args.plan_command == "order":
        try:
            request = PlanOrderRequest(
                product_id=args.product_id,
                product_version=args.product_version,
                order_id=args.order_id,
                quantity=args.quantity,
                requested_at=args.requested_at,
            )
            result = plan_order(
                ProductConfigStore(
                    Path(".fabtrace/drafts/products"),
                    args.product_config_dir,
                ),
                PlanArtifactStore(args.plans_dir),
                request,
            )
        except (ValueError, PlanningError) as exc:
            print(f"Planning: FAIL\n{exc}")
            return 2
        print("Planning: PASS")
        print(f"Order: {result.plan.order_id}")
        print(
            f"Product: {result.plan.product_id} "
            f"v{result.plan.product_version}"
        )
        print(f"Expected delivery: {result.plan.expected_delivery_at.isoformat()}")
        print(f"Provider orders: {len(result.plan.provider_orders)}")
        print(
            "Critical path: "
            + " -> ".join(result.plan.critical_path_node_ids)
        )
        print(f"Plan artifact: {result.plan_path}")
        return 0
    if args.command == "serve":
        import uvicorn

        from fabtrace.web import create_app

        uvicorn.run(
            create_app(
                args.database,
                args.product_config_dir,
                args.product_draft_dir,
                args.plans_dir,
                args.data_dir,
            ),
            host=args.host,
            port=args.port,
        )
        return 0
    raise AssertionError("argparse accepted an unsupported command")
