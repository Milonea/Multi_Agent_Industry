from __future__ import annotations

import json
from pathlib import Path
import unittest

from portfolio_control_plane import (
    ContractError,
    EvidenceReport,
    ValidationPolicyRegistry,
    default_capability_registry,
    default_validation_policy_registry,
    execute_plan,
    make_run_result,
    approve_plan,
    build_cross_domain_plan,
)


ROOT = Path(__file__).resolve().parents[1]


def run_envelope() -> dict:
    return make_run_result(
        run={
            "id": "run-sample-001",
            "project": "example-owner/example-project",
            "capability": "task.contract.validate",
            "request_summary": "validate the local contract",
            "base_sha": "a" * 40,
            "head_sha": "b" * 40,
        },
        result={
            "status": "PASS",
            "summary": "contract validated",
            "changed_paths": [],
            "external_writes": {"attempted": False, "count": 0, "targets": []},
            "tests": [{"name": "contract", "status": "PASS"}],
            "risks": [],
            "rollback": "revert the scoped commit",
        },
        validation={"validator": "validator", "cycle": 0, "findings": [], "next_action": "review"},
    )


class PortfolioContractTests(unittest.TestCase):
    def test_run_result_is_validated_and_strict(self) -> None:
        envelope = run_envelope()
        self.assertEqual(envelope["result"]["status"], "PASS")
        envelope["result"]["external_writes"] = {"attempted": False, "count": 1, "targets": []}
        with self.assertRaises(ContractError):
            run_envelope_from(envelope)

    def test_default_capability_registry_is_generic_and_read_only(self) -> None:
        capabilities = default_capability_registry()
        self.assertTrue(
            capabilities.resolve(
                "task.contract.validate", action="validate", requested_tools=("read:contracts",)
            ).allowed
        )
        self.assertFalse(
            capabilities.resolve(
                "task.contract.validate", action="write", requested_tools=("write:repository",)
            ).allowed
        )
        self.assertFalse(capabilities.resolve("unknown.external.write", action="write").allowed)
        policies = default_validation_policy_registry()
        self.assertEqual(policies.get("framework", "core").max_cycles, 3)

    def test_workflow_plan_requires_approval_and_never_calls_an_adapter(self) -> None:
        plan = build_cross_domain_plan(
            run_id="plan-sample-001",
            stages=[
                {
                    "name": "validate-contract",
                    "capability": "task.contract.validate",
                    "action": "validate",
                    "status": "verified",
                    "tools": ["read:contracts"],
                    "provenance": ["fixture://contract"],
                }
            ],
        )
        self.assertEqual(plan["status"], "approval_required")
        self.assertEqual(plan["risk"]["external_writes"], [])
        self.assertEqual(execute_plan(plan)["status"], "HOLD_MANUAL")
        approved = approve_plan(plan, approved_by="reviewer", scope="one local test run")
        execution = execute_plan(approved)
        self.assertEqual(execution["status"], "NEEDS_MANUAL")
        self.assertFalse(execution["executed"])
        self.assertEqual(execution["external_writes"], [])

    def test_unknown_capability_blocks_plan(self) -> None:
        plan = build_cross_domain_plan(
            stages=[{"name": "unknown", "capability": "unregistered.write", "action": "write"}]
        )
        self.assertEqual(plan["status"], "HOLD_MANUAL")
        self.assertTrue(plan["risk"]["manual_gates"])

    def test_evidence_report_redacts_url_parameters_and_free_text_secrets(self) -> None:
        report = EvidenceReport.from_envelope(run_envelope(), agent="agent Bearer synthetic-placeholder")
        self.assertNotIn("synthetic-placeholder", json.dumps(report.to_dict()))
        self.assertNotIn("raw_conversation", json.dumps(report.to_dict()))
        self.assertIn("Run run-sample-001", report.render("custom.integration"))
        with self.assertRaises(ValueError):
            report.render("Custom Integration")

    def test_all_schema_files_are_valid_json(self) -> None:
        schemas = list((ROOT / ".agents").rglob("*.schema.json"))
        schemas += list((ROOT / "agents").rglob("*.schema.json"))
        schemas += list((ROOT / "portfolio_control_plane").rglob("*.schema.json"))
        self.assertGreaterEqual(len(schemas), 12)
        for path in schemas:
            with self.subTest(path=path.relative_to(ROOT)):
                schema = json.loads(path.read_text(encoding="utf-8"))
                self.assertIn("$schema", schema)
                self.assertIn("$id", schema)


def run_envelope_from(envelope: dict) -> dict:
    from portfolio_control_plane.contracts import validate_run_result

    return validate_run_result(envelope)


if __name__ == "__main__":
    unittest.main()
