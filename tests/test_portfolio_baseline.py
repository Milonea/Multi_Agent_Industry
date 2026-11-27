from __future__ import annotations

import json
from pathlib import Path
import subprocess
import tempfile
import unittest

from portfolio_control_plane.baseline import BaselineError, UNKNOWN, audit_manifest


class PortfolioBaselineTests(unittest.TestCase):
    def _git_repository(self, root: Path) -> None:
        subprocess.run(["git", "init", "-q", str(root)], check=True)
        subprocess.run(["git", "-C", str(root), "config", "user.email", "test@example.invalid"], check=True)
        subprocess.run(["git", "-C", str(root), "config", "user.name", "Baseline Test"], check=True)

    def _manifest(self, local_path: str | None) -> dict:
        return {
            "schema_version": "1.0",
            "repositories": [
                {
                    "repo_id": "fixture-repo",
                    "repository": "example-org/fixture-repo",
                    "role": "core",
                    "canonical_owner": "fixture-owner",
                    "stable_url": "https://github.com/example-org/fixture-repo",
                    "canonical_status_url": "https://github.com/example-org/fixture-repo/issues",
                    "canonical_status_source": "fixture",
                    "local_path": local_path,
                    "availability": "checked_out" if local_path else "not_checked_out",
                    "evidence": ["fixture"],
                }
            ],
        }

    def test_read_only_audit_reports_static_metrics_and_unknown_runtime_metrics(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "src").mkdir()
            (root / ".github" / "workflows").mkdir(parents=True)
            (root / "src" / "main.py").write_text("print('fixture')\n", encoding="utf-8")
            (root / "package.json").write_text('{"private": true}\n', encoding="utf-8")
            (root / "package-lock.json").write_text('{}\n', encoding="utf-8")
            (root / ".github" / "workflows" / "ci.yml").write_text("name: ci\n", encoding="utf-8")
            (root / "build").mkdir()
            (root / "build" / "release.zip").write_bytes(b"artifact")
            self._git_repository(root)
            subprocess.run(["git", "-C", str(root), "add", "."], check=True)
            subprocess.run(["git", "-C", str(root), "commit", "-qm", "fixture"], check=True)

            report = audit_manifest(
                self._manifest("."),
                root,
                measured_at="2026-09-02T00:00:00+00:00",
                large_file_threshold_bytes=1,
            )
            row = report["repositories"][0]

            self.assertEqual(report["schema_version"], "1.0")
            self.assertIn(".tmp*", report["measurement_policy"]["excluded_directories"])
            self.assertEqual(row["availability"], "checked_out")
            self.assertIsInstance(row["source_bytes"], int)
            self.assertIsInstance(row["checkout_bytes"], int)
            self.assertIsInstance(row["git_object_bytes"], int)
            self.assertEqual(row["fresh_install"], UNKNOWN)
            self.assertEqual(row["ci_wall_clock"], UNKNOWN)
            self.assertGreater(row["artifact_bytes"], 0)
            self.assertEqual(row["dependency_manifests"], ["package.json"])
            self.assertEqual(row["dependency_locks"], ["package-lock.json"])
            self.assertEqual(row["ci_workflows"], [".github/workflows/ci.yml"])
            self.assertIn(
                ".github/workflows/ci.yml",
                [item["path"] for item in row["tracked_large_files"]],
            )
            self.assertEqual(row["release_artifacts"][0]["path"], "build/release.zip")
            self.assertNotIn(str(root), json.dumps(report))

    def test_missing_checkout_preserves_unknown_instead_of_zero(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            report = audit_manifest(
                self._manifest("missing-checkout-that-does-not-exist"),
                directory,
                measured_at="fixed",
            )
            row = report["repositories"][0]
            self.assertEqual(row["availability"], "not_checked_out")
            for field in ("source_bytes", "checkout_bytes", "git_object_bytes", "fresh_install", "ci_wall_clock", "artifact_bytes"):
                self.assertEqual(row[field], UNKNOWN)

    def test_retired_repository_remains_in_inventory_with_unknown_metrics(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            manifest = self._manifest(None)
            manifest["repositories"][0]["role"] = "retired"

            report = audit_manifest(manifest, directory, measured_at="fixed")

            row = report["repositories"][0]
            self.assertEqual(row["role"], "retired")
            self.assertEqual(row["availability"], "not_checked_out")
            self.assertEqual(row["source_bytes"], UNKNOWN)

    def test_external_reference_uses_report_schema_availability(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            manifest = self._manifest(None)
            manifest["repositories"][0]["availability"] = "external"
            manifest["repositories"][0]["baseline_scope"] = "external_reference"

            report = audit_manifest(manifest, directory, measured_at="fixed")

            row = report["repositories"][0]
            self.assertEqual(row["availability"], "outside_registered_scope")
            self.assertEqual(row["source_bytes"], UNKNOWN)
            self.assertIn(
                "local checkout is not in the registered baseline scope",
                row["warnings"],
            )

    def test_measurement_overrides_are_strict(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self._git_repository(root)
            with self.assertRaises(BaselineError):
                audit_manifest(
                    self._manifest("."),
                    root,
                    measurements={"fixture-repo": {"fresh_install": True}},
                )

    def test_manifest_repository_and_measurement_keys_are_strict(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            manifest = self._manifest(None)
            manifest["repositories"][0]["repository"] = "owner/repo/extra"
            with self.assertRaises(BaselineError):
                audit_manifest(manifest, directory)

            with self.assertRaises(BaselineError):
                audit_manifest(
                    self._manifest(None),
                    directory,
                    measurements={"unknown-repo": {"artifact_bytes": 0}},
                )


if __name__ == "__main__":
    unittest.main()
