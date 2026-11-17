from __future__ import annotations

from climateledger.intake import load_batch, write_manifest
from climateledger.registry import ContractError, LEGACY_REGION, Source
from climateledger.service import LedgerService
from support import LedgerCase


def regional_rows(*values, region, indicator="co2_ppm"):
    return [{"indicator": indicator, "period": f"2026-{n:02d}", "value": str(value),
             "unit": "ppm", "region": region}
            for n, value in enumerate(values, 1)]


class RegionContractTests(LedgerCase):
    def setUp(self):
        super().setUp()
        self.service.register_source({"key": "national", "title": "Synthetic national service",
                                      "license": "Local synthetic fixture",
                                      "allowed_indicators": ["co2_ppm"],
                                      "allowed_regions": ["national"]})
        self.service.register_source({"key": "province", "title": "Synthetic provincial service",
                                      "license": "Local synthetic fixture",
                                      "allowed_indicators": ["co2_ppm"],
                                      "allowed_regions": ["province_anhui", "coastal_east"]})

    def test_source_region_scope_is_parsed_and_encoded(self):
        source = self.service.registry.source("national")
        self.assertEqual(source.allowed_regions, ("national",))
        self.assertIn("allowed_regions", source.encode())

    def test_unrestricted_source_has_no_region_list(self):
        self.assertIsNone(self.service.registry.source("lab_a").allowed_regions)

    def test_explicit_region_scope_must_be_nonempty_and_unique(self):
        with self.assertRaisesRegex(ContractError, "region_scope"):
            Source.from_dict({"key": "xx", "title": "X", "license": "L",
                              "allowed_indicators": ["co2_ppm"], "allowed_regions": []})
        with self.assertRaisesRegex(ContractError, "region_scope"):
            Source.from_dict({"key": "xx", "title": "X", "license": "L",
                              "allowed_indicators": ["co2_ppm"],
                              "allowed_regions": ["national", "national"]})

    def test_reserved_region_cannot_be_licensed_or_submitted(self):
        with self.assertRaisesRegex(ContractError, "reserved_region"):
            Source.from_dict({"key": "xx", "title": "X", "license": "L",
                              "allowed_indicators": ["co2_ppm"],
                              "allowed_regions": [LEGACY_REGION]})
        batch, _ = self.ingest("reserved", regional_rows(428, region=LEGACY_REGION),
                               source="province")
        self.assertEqual(batch["status"], "rejected")
        self.assertIn("reserved_region", {item["code"] for item in batch["findings"]})

    def test_invalid_region_identifier_is_row_rejection(self):
        batch, _ = self.ingest("bad_region", regional_rows(428, region="Province Anhui"),
                               source="province")
        self.assertEqual(batch["status"], "rejected")
        self.assertIn("invalid_region", {item["code"] for item in batch["findings"]})

    def test_unauthorized_row_region_is_rejected_with_evidence(self):
        batch, _ = self.ingest("wrong_region", regional_rows(428, region="national"),
                               source="province")
        self.assertEqual(batch["status"], "rejected")
        self.assertIn("region_scope_violation", {item["code"] for item in batch["findings"]})
        self.assertEqual(self.service.batch(batch["id"])["measurements"], [])

    def test_unauthorized_manifest_region_blocks_before_rows(self):
        rows = [{"indicator": "co2_ppm", "period": "2026-01", "value": "428", "unit": "ppm"}]
        path = write_manifest(self.root / "wrong_manifest", source="province",
                              batch_id="wrong_manifest", rows=rows, region="national")
        with self.assertRaisesRegex(ContractError, "region_scope_violation"):
            self.service.ingest(path)
        self.assertEqual(self.service.batches(), [])

    def test_manifest_region_and_region_column_cannot_be_combined(self):
        rows = regional_rows(428, region="national")
        with self.assertRaisesRegex(ContractError, "region_conflict"):
            write_manifest(self.root / "conflict", source="national", batch_id="conflict",
                           rows=rows, region="national")


class RegionIntakeTests(LedgerCase):
    def test_manifest_region_applies_to_every_row(self):
        path = write_manifest(self.root / "national", source="lab_a", batch_id="national_batch",
                              rows=self.rows(428, 429), region="national")
        envelope = load_batch(path)
        self.assertEqual({row["region"] for row in envelope.rows}, {"national"})
        self.assertEqual(envelope.region_origin, "manifest")

    def test_row_region_is_read_from_column(self):
        rows = [{"indicator": "co2_ppm", "period": "2026-01", "value": "428",
                 "unit": "ppm", "region": "national"}]
        path = write_manifest(self.root / "row_region", source="lab_a", batch_id="row_batch",
                              rows=rows)
        envelope = load_batch(path)
        self.assertEqual(envelope.region_origin, "rows")
        self.assertIsNone(envelope.region)
        self.assertEqual(envelope.rows[0]["region"], "national")

    def test_csv_carries_region_column(self):
        rows = [{"indicator": "co2_ppm", "period": "2026-01", "value": "428",
                 "unit": "ppm", "region": "coastal_east"}]
        path = write_manifest(self.root / "csv_region", source="lab_a", batch_id="csv_batch",
                              rows=rows, format_name="csv")
        envelope = load_batch(path)
        self.assertEqual(envelope.rows[0]["region"], "coastal_east")

    def test_legacy_file_gets_reserved_scope_with_explanation(self):
        path = self.manifest("legacy_shape", self.rows(428))
        envelope = load_batch(path)
        self.assertEqual(envelope.region_origin, "legacy_default")
        self.assertEqual(envelope.region_default, LEGACY_REGION)
        interpretation = envelope.region_interpretation()
        self.assertEqual(interpretation["row_default"], LEGACY_REGION)
        self.assertIn("never inferred", interpretation["explanation"])

    def test_row_scoped_file_cannot_omit_the_region_column(self):
        # A missing mapped column fails intake as a whole, exactly like an
        # absent indicator or period; it is never filled from another row.
        rows = [{"indicator": "co2_ppm", "period": "2026-01", "value": "428",
                 "unit": "ppm", "region": "national"},
                {"indicator": "co2_ppm", "period": "2026-02", "value": "429", "unit": "ppm"}]
        path = write_manifest(self.root / "missing_cell", source="lab_a",
                              batch_id="missing_cell", rows=rows)
        with self.assertRaisesRegex(ContractError, "missing_column"):
            self.service.ingest(path)
        self.assertEqual(self.service.batches(), [])

    def test_blank_cell_in_region_column_is_rejected_not_defaulted(self):
        rows = [{"indicator": "co2_ppm", "period": "2026-01", "value": "428",
                 "unit": "ppm", "region": ""}]
        path = write_manifest(self.root / "blank_region", source="lab_a", batch_id="blank_batch",
                              rows=rows)
        batch = self.service.ingest(path)["batch"]
        self.assertEqual(batch["status"], "rejected")
        self.assertIn("invalid_region", {item["code"] for item in batch["findings"]})
        self.assertEqual(self.service.batch(batch["id"])["measurements"], [])


class RegionQualityTests(LedgerCase):
    def test_same_period_in_two_regions_is_not_a_duplicate(self):
        rows = regional_rows(428, region="national") + regional_rows(420, region="province_anhui")
        batch, _ = self.ingest("two_regions", rows, source="lab_a")
        self.assertEqual(batch["status"], "accepted")
        self.assertEqual(batch["findings"], [])
        self.assertEqual(len(self.service.batch(batch["id"])["measurements"]), 2)

    def test_duplicate_is_judged_inside_the_same_region(self):
        rows = regional_rows(428, region="national") + regional_rows(428, region="national")
        batch, _ = self.ingest("same_region_dup", rows, source="lab_a")
        self.assertEqual(batch["status"], "rejected")
        finding = next(item for item in batch["findings"] if item["code"] == "duplicate_period")
        self.assertEqual(finding["region"], "national")

    def test_conflict_is_judged_inside_the_same_region(self):
        rows = regional_rows(428, region="national") + regional_rows(429, region="national")
        batch, _ = self.ingest("same_region_conflict", rows, source="lab_a")
        self.assertIn("conflicting_period", {item["code"] for item in batch["findings"]})

    def test_gap_is_assessed_per_region(self):
        rows = [{"indicator": "co2_ppm", "period": "2026-01", "value": "428",
                 "unit": "ppm", "region": "national"},
                {"indicator": "co2_ppm", "period": "2026-01", "value": "420",
                 "unit": "ppm", "region": "province_anhui"},
                {"indicator": "co2_ppm", "period": "2026-03", "value": "422",
                 "unit": "ppm", "region": "province_anhui"}]
        batch, _ = self.ingest("region_gap", rows, source="lab_a")
        gaps = [item for item in batch["findings"] if item["code"] == "missing_period"]
        self.assertEqual([(item["region"], item["period"]) for item in gaps],
                         [("province_anhui", "2026-02")])

    def test_revision_is_scoped_to_region(self):
        self.release_with_region("national", 428)
        batch, _ = self.ingest("region_revision",
                               regional_rows(430, region="province_anhui"), source="lab_a")
        self.assertEqual(batch["status"], "accepted")
        self.assertNotIn("revised_value", {item["code"] for item in batch["findings"]})

    def release_with_region(self, region, value):
        batch, _ = self.ingest(f"release_{region}",
                               regional_rows(value, region=region), source="lab_a")
        self.assertEqual(batch["status"], "accepted")
        return self.service.publish(batch["id"], batch["revision"])


class RegionReleaseTests(LedgerCase):
    def publish_rows(self, batch_id, rows):
        batch = self.service.ingest(write_manifest(
            self.root / batch_id, source="lab_a", batch_id=batch_id, rows=rows))["batch"]
        if batch["status"] == "review":
            batch = self.service.approve(batch["id"], batch["revision"])
        if batch["status"] not in {"accepted", "approved"}:
            self.fail(f"batch {batch_id} was {batch['status']}: {batch['findings']}")
        return batch, self.service.publish(batch["id"], batch["revision"])

    def test_snapshot_keeps_same_month_values_for_each_region(self):
        self.publish_rows("national", regional_rows(420, region="national"))
        _, release = self.publish_rows("province",
                                       regional_rows(415, region="province_anhui"))
        entries = self.service.export(release["version"])["entries"]
        self.assertEqual([(row["region"], row["value"]) for row in entries],
                         [("national", "420.00"), ("province_anhui", "415.00")])

    def test_manifest_records_region_provenance_per_batch(self):
        _, release = self.publish_rows("national", regional_rows(420, region="national"))
        batch = release["manifest"]["batch"]
        self.assertEqual(batch["region"], "national")
        self.assertEqual(batch["region_origin"], "rows")
        self.assertEqual(release["manifest"]["regions"], ["national"])

    def test_mixed_row_regions_are_reported_as_mixed(self):
        rows = (regional_rows(420, region="national") +
                regional_rows(415, region="province_anhui"))
        batch, _ = self.publish_rows("mixed", rows)
        stored = self.service.batch(batch["id"])
        self.assertEqual(stored["region"], "mixed")
        self.assertEqual(stored["region_origin"], "rows")

    def test_legacy_snapshot_is_published_as_unscoped(self):
        batch, release = self.publish_rows("legacy", self.rows(428))
        self.assertEqual(release["manifest"]["batch"]["region"], LEGACY_REGION)
        self.assertEqual(release["manifest"]["batch"]["region_origin"], "legacy_default")
        self.assertEqual(release["manifest"]["batch"]["region_detail"]["row_default"], LEGACY_REGION)
        entries = self.service.export(release["version"])["entries"]
        self.assertEqual({row["region"] for row in entries}, {LEGACY_REGION})

    def test_region_query_never_falls_back_to_national(self):
        self.publish_rows("national", regional_rows(420, 421, region="national"))
        _, release = self.publish_rows("province", regional_rows(415, region="province_anhui"))
        self.assertEqual([row["value"] for row in
                          self.service.series("co2_ppm", release["version"],
                                              region="province_anhui")], ["415.00"])
        self.assertEqual([row["value"] for row in
                          self.service.series("co2_ppm", release["version"], region="national")],
                         ["420.00", "421.00"])
        stats = self.service.statistics("co2_ppm", release["version"], region="province_anhui")
        self.assertEqual(stats["count"], 1)
        self.assertEqual(stats["regions"], ["province_anhui"])

    def test_coverage_and_discrepancies_stay_inside_region(self):
        self.publish_rows("national_a", regional_rows(420, 421, region="national"))
        _, release = self.publish_rows(
            "province_a", regional_rows(415, 416, region="province_anhui") +
            regional_rows(430, 431, region="coastal_east"))
        report = self.service.coverage(release["version"], "co2_ppm")
        self.assertEqual(report["regions"], ["coastal_east", "national", "province_anhui"])
        self.assertTrue(all("region" in row for row in report["series"]))
        # A source that only reports nationally is not compared against a
        # province, so there are no cross-region disagreements to report.
        other = self.service.ingest(write_manifest(
            self.root / "province_b", source="lab_b", batch_id="province_b",
            rows=regional_rows(421, 422, region="province_anhui")))["batch"]
        self.service.publish(other["id"], other["revision"])
        report = self.service.discrepancies(release["version"] + 1, "co2_ppm")
        self.assertTrue(all(item["region"] == "province_anhui"
                            for item in report["disagreements"]))
        self.assertTrue(all(item["region"] == "province_anhui"
                            for item in self.service.discrepancies(
                                release["version"] + 1, "co2_ppm",
                                region="province_anhui")["disagreements"]))

    def test_compare_attributes_changes_to_a_region(self):
        self.publish_rows("first", regional_rows(420, 421, region="national") +
                          regional_rows(415, region="province_anhui"))
        self.publish_rows("second", regional_rows(420, 421, region="national") +
                          regional_rows(419, region="province_anhui"))
        diff = self.service.compare(1, 2)
        changes = [item for item in diff["changes"] if item["change"] == "revised"]
        self.assertEqual([(item["region"], item["period"]) for item in changes],
                         [("province_anhui", "2026-01")])
        self.assertEqual(diff["summary"]["by_region"]["national"], 2)

    def test_verify_rejects_region_tampering(self):
        _, release = self.publish_rows("national", regional_rows(420, region="national"))
        bundle = self.service.export(release["version"])
        self.assertTrue(self.service.verify(bundle))
        bundle["entries"][0]["region"] = "province_anhui"
        self.assertFalse(self.service.verify(bundle))

    def test_replay_reports_the_recorded_region_interpretation(self):
        batch, manifest = self.ingest("legacy_replay", self.rows(428))
        result = self.service.replay(manifest, batch["id"])
        self.assertTrue(result["reproducible"])
        self.assertEqual(result["region_interpretation"]["origin"], "legacy_default")
        self.assertEqual(result["region_interpretation_match"], True)
        self.assertEqual({row["stored_region"] for row in result["row_comparison"]},
                         {LEGACY_REGION})

    def test_audit_covers_region_provenance(self):
        self.publish_rows("national", regional_rows(420, region="national"))
        report = self.service.audit.release_trace(1)
        self.assertTrue(report["verified"])
        self.assertEqual(report["regions"], ["national"])


class LegacyLedgerMigrationTests(LedgerCase):
    """A ledger written before regions must keep verifying after upgrade."""

    LEGACY_SCHEMA = """
    CREATE TABLE indicators (key TEXT PRIMARY KEY, contract TEXT NOT NULL);
    CREATE TABLE sources (key TEXT PRIMARY KEY, contract TEXT NOT NULL);
    CREATE TABLE unit_rules (indicator TEXT NOT NULL, source_unit TEXT NOT NULL,
        factor TEXT NOT NULL, offset TEXT NOT NULL, PRIMARY KEY(indicator, source_unit));
    CREATE TABLE batches (id INTEGER PRIMARY KEY, source TEXT NOT NULL, external_id TEXT NOT NULL,
        revision INTEGER NOT NULL, payload_hash TEXT NOT NULL, status TEXT NOT NULL,
        row_count INTEGER NOT NULL, metadata TEXT NOT NULL, findings TEXT NOT NULL,
        received_at TEXT NOT NULL, approved_at TEXT, published_version INTEGER,
        UNIQUE(source, external_id, revision));
    CREATE TABLE measurements (batch_id INTEGER NOT NULL, line INTEGER NOT NULL,
        indicator TEXT NOT NULL, period TEXT NOT NULL, value TEXT NOT NULL, unit TEXT NOT NULL,
        raw_value TEXT NOT NULL, raw_unit TEXT NOT NULL, factor TEXT NOT NULL, offset TEXT NOT NULL,
        PRIMARY KEY(batch_id, line));
    CREATE TABLE releases (version INTEGER PRIMARY KEY, created_at TEXT NOT NULL,
        manifest TEXT NOT NULL, digest TEXT NOT NULL UNIQUE);
    CREATE TABLE release_entries (version INTEGER NOT NULL, source TEXT NOT NULL,
        indicator TEXT NOT NULL, period TEXT NOT NULL, batch_id INTEGER NOT NULL,
        line INTEGER NOT NULL, value TEXT NOT NULL, unit TEXT NOT NULL,
        PRIMARY KEY(version, source, indicator, period));
    CREATE TABLE events (id INTEGER PRIMARY KEY, at TEXT NOT NULL, kind TEXT NOT NULL,
        subject TEXT NOT NULL, detail TEXT NOT NULL);
    """

    def setUp(self):
        # Start from the shared fixture, then replace it with a pre-region DB.
        import sqlite3
        from hashlib import sha256

        super().setUp()
        self.service.close()
        self.db.unlink()

        from climateledger.intake import canonical_bytes
        from climateledger.registry import Indicator, Source

        self.db = self.root / "legacy.sqlite3"
        connection = sqlite3.connect(str(self.db))
        connection.executescript(self.LEGACY_SCHEMA)
        indicator = Indicator.from_dict({"key": "co2_ppm", "title": "Synthetic carbon dioxide",
                                         "canonical_unit": "ppm", "cadence": "month",
                                         "precision": 2})
        source = Source.from_dict({"key": "lab_a", "title": "Synthetic lab A",
                                   "license": "Local synthetic fixture",
                                   "allowed_indicators": ["co2_ppm"]})
        connection.execute("INSERT INTO indicators VALUES(?,?)", (indicator.key, indicator.encode()))
        connection.execute("INSERT INTO sources VALUES(?,?)", (source.key, source.encode()))
        connection.execute(
            "INSERT INTO batches VALUES(1,'lab_a','old_batch',1,'hash','published',1,'{}','[]','2026-01-01T00:00:00+00:00',NULL,1)")
        connection.execute("INSERT INTO measurements VALUES(1,1,'co2_ppm','2026-01','428.00','ppm','428','ppm','1','0')")
        entries = [{"source": "lab_a", "indicator": "co2_ppm", "period": "2026-01",
                    "batch_id": 1, "line": 1, "value": "428.00", "unit": "ppm"}]
        manifest = {"schema_version": 1, "version": 1, "parent_digest": None,
                    "batch": {"id": 1, "source": "lab_a", "external_id": "old_batch",
                              "revision": 1, "payload_hash": "hash", "row_count": 1},
                    "entry_count": 1,
                    "entries_sha256": sha256(canonical_bytes(entries)).hexdigest(),
                    "quality_findings": []}
        digest = sha256(canonical_bytes(manifest)).hexdigest()
        connection.execute("INSERT INTO releases VALUES(1,'2026-01-01T00:00:00+00:00',?,?)",
                           (canonical_bytes(manifest).decode(), digest))
        connection.execute("INSERT INTO release_entries VALUES(1,'lab_a','co2_ppm','2026-01',1,1,'428.00','ppm')")
        connection.commit()
        connection.close()

        self.service = LedgerService(self.db)
        self.addCleanup(self.service.close)

    def test_old_snapshot_still_exports_and_verifies(self):
        bundle = self.service.export(1)
        self.assertTrue(self.service.verify(bundle))
        self.assertEqual(bundle["manifest"]["schema_version"], 1)

    def test_old_rows_get_the_reserved_scope_without_guessing(self):
        bundle = self.service.export(1)
        self.assertEqual({row["region"] for row in self.service.store.release_entries(1)},
                         {"unscoped"})
        # The schema-1 bundle keeps its historical entry shape; scope is
        # reported by the store, not silently appended to old digests.
        self.assertNotIn("region", bundle["entries"][0])
        trace = self.service.audit.release_trace(1)
        self.assertTrue(trace["verified"])
        self.assertEqual(trace["regions"], ["unscoped"])

    def test_new_batches_can_be_added_after_migration(self):
        self.service.register_source({"key": "lab_b", "title": "Synthetic lab B",
                                      "license": "Local synthetic fixture",
                                      "allowed_indicators": ["co2_ppm"]})
        batch, _ = self.ingest("after_upgrade", self.rows(429), source="lab_b")
        self.assertEqual(batch["status"], "accepted")
        release = self.service.publish(batch["id"], batch["revision"])
        self.assertEqual(release["version"], 2)
        self.assertTrue(self.service.verify(self.service.export(2)))
