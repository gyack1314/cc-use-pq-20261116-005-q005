from __future__ import annotations

from pathlib import Path
from typing import Any

from .intake import REGION_ORIGIN_ROWS, load_batch
from .normalize import Normalizer
from .quality import QualityGate
from .registry import ContractError, Registry


class ReplayInspector:
    def __init__(self, store, registry: Registry):
        self.store = store
        self.registry = registry
        self.normalizer = Normalizer(registry)
        self.quality = QualityGate(registry, store)

    def compare_manifest(self, manifest_path: str | Path, batch_id: int) -> dict[str, Any]:
        envelope = load_batch(manifest_path)
        batch = self.store.batch(batch_id)
        identity = envelope.source == batch["source"] and envelope.batch_id == batch["external_id"]
        bytes_match = envelope.payload_hash == batch["payload_hash"]
        if not identity:
            raise ContractError("replay_identity", "manifest source or external id differs from batch")
        source = self.registry.source(envelope.source)
        # A row-scoped file must carry a region on every row; only manifest
        # and legacy files have a batch-level default to fall back on.
        fallback = None if envelope.region_origin == REGION_ORIGIN_ROWS else envelope.region_default
        measurements, rejected = self.normalizer.batch(source, envelope.rows, fallback)
        expected = self.store.measurements(batch_id)
        comparison = []
        for item in measurements:
            old = next((row for row in expected if row["line"] == item.line), None)
            stored_region = old["region"] if old else None
            # "origin" is how the original row obtained its scope (manifest,
            # column or legacy default); "interpretation" reports that choice
            # separately so a legacy file's reading is visible and auditable.
            matches = (old is not None and old["indicator"] == item.indicator and
                       old["period"] == item.period and old["value"] == item.value and
                       old["unit"] == item.unit and stored_region == item.region)
            comparison.append({"line": item.line, "matches": matches,
                               "stored_value": old["value"] if old else None,
                               "replayed_value": item.value,
                               "stored_region": stored_region, "replayed_region": item.region,
                               "region_origin": envelope.region_origin})
        return {"batch_id": batch_id, "identity_match": identity, "payload_hash_match": bytes_match,
                "region_interpretation": envelope.region_interpretation(),
                "region_interpretation_match": batch["region_origin"] == envelope.region_origin,
                "stored_region": batch["region"], "stored_region_origin": batch["region_origin"],
                "replayed_regions": sorted({item.region for item in measurements}),
                "stored_rows": len(expected), "replayed_rows": len(measurements),
                "rejected_rows": rejected, "row_comparison": comparison,
                "reproducible": bytes_match and len(expected) == len(measurements) and
                                batch["region_origin"] == envelope.region_origin and
                                all(row["matches"] for row in comparison)}

    def manifest_receipt(self, manifest_path: str | Path) -> dict[str, Any]:
        envelope = load_batch(manifest_path)
        existing = self.store.latest_batch(envelope.source, envelope.batch_id)
        return {"source": envelope.source, "external_id": envelope.batch_id,
                "payload_hash": envelope.payload_hash, "declared_count": envelope.declared_count,
                "latest_batch": existing["id"] if existing else None,
                "same_payload": existing["payload_hash"] == envelope.payload_hash if existing else None,
                "next_revision": (existing["revision"] + 1 if existing else 1)}
