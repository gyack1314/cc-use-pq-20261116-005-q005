from __future__ import annotations

from hashlib import sha256
from typing import Any

from .intake import canonical_bytes
from .registry import ContractError


LEGACY_ENTRY_FIELDS = ("source", "indicator", "period", "batch_id", "line", "value", "unit")
REGION_ENTRY_FIELDS = ("source", "indicator", "region", "period", "batch_id", "line",
                       "value", "unit")


def entry_fields(manifest: dict[str, Any]) -> tuple[str, ...]:
    return REGION_ENTRY_FIELDS if manifest.get("schema_version", 1) >= 2 else LEGACY_ENTRY_FIELDS


class ReleaseBuilder:
    def __init__(self, store):
        self.store = store

    def preview(self, batch_id: int, expected_revision: int) -> dict[str, Any]:
        batch = self.store.batch(batch_id)
        if batch["revision"] != expected_revision:
            raise ContractError("stale_revision", "expected revision differs")
        if batch["status"] not in {"accepted", "approved", "published"}:
            raise ContractError("quality_gate", "batch has unresolved quality findings")
        latest = self.store.latest_batch(batch["source"], batch["external_id"])
        if latest["id"] != batch_id and batch["status"] != "published":
            raise ContractError("stale_revision", "newer revision supersedes this batch")
        if batch["published_version"]:
            return self.store.release(batch["published_version"])["manifest"]
        prior = self.store.latest_release()
        version = prior["version"] + 1 if prior else 1
        entries = self._entries(batch_id, prior["version"] if prior else None)
        return self._manifest(batch, version, entries, prior)

    def _entries(self, batch_id: int, prior_version: int | None) -> list[dict[str, Any]]:
        batch = self.store.batch(batch_id)
        entries: dict[tuple[str, str, str, str], dict[str, Any]] = {}
        fields = ("source", "indicator", "region", "period", "batch_id", "line", "value", "unit")
        if prior_version:
            for item in self.store.release_entries(prior_version):
                key = item["source"], item["indicator"], item["region"], item["period"]
                entries[key] = {field: item[field] for field in fields}
        for item in self.store.measurements(batch_id):
            key = batch["source"], item["indicator"], item["region"], item["period"]
            entries[key] = {"source": batch["source"], "indicator": item["indicator"],
                            "region": item["region"], "period": item["period"],
                            "batch_id": batch_id, "line": item["line"],
                            "value": item["value"], "unit": item["unit"]}
        return [entries[key] for key in sorted(entries)]

    def _manifest(self, batch: dict[str, Any], version: int,
                  entries: list[dict[str, Any]], prior: dict[str, Any] | None) -> dict[str, Any]:
        data_hash = sha256(canonical_bytes(entries)).hexdigest()
        regions = sorted({entry["region"] for entry in entries})
        return {"schema_version": 2, "version": version,
                "parent_digest": prior["digest"] if prior else None,
                "batch": {"id": batch["id"], "source": batch["source"], "external_id": batch["external_id"],
                          "revision": batch["revision"], "payload_hash": batch["payload_hash"],
                          "row_count": batch["row_count"],
                          # The batch's region provenance travels with the snapshot.
                          "region": batch["region"], "region_origin": batch["region_origin"],
                          "region_detail": batch["region_detail"]},
                "entry_count": len(entries), "entries_sha256": data_hash,
                "regions": regions,
                "quality_findings": batch["findings"]}

    def publish(self, batch_id: int, expected_revision: int) -> dict[str, Any]:
        batch = self.store.batch(batch_id)
        if batch["published_version"]:
            if batch["revision"] != expected_revision:
                raise ContractError("stale_revision", "expected revision differs")
            return self.store.release(batch["published_version"])
        manifest = self.preview(batch_id, expected_revision)
        prior = self.store.latest_release()
        entries = self._entries(batch_id, prior["version"] if prior else None)
        digest = sha256(canonical_bytes(manifest)).hexdigest()
        return self.store.publish(batch_id, expected_revision, manifest, entries, digest)

    def export(self, version: int) -> dict[str, Any]:
        release = self.store.release(version)
        entries = self.store.release_entries(version)
        manifest = release["manifest"]
        # Historical schema-1 snapshots digest only the original fields; their
        # stored data is unscoped, and adding region to the digest would
        # retroactively declare them corrupt.
        fields = entry_fields(manifest)
        comparable = [{key: item[key] for key in fields} for item in entries]
        actual = sha256(canonical_bytes(comparable)).hexdigest()
        if actual != manifest["entries_sha256"]:
            raise ContractError("release_corrupt", "release entries digest differs from manifest")
        if fields is REGION_ENTRY_FIELDS and (
                sorted({entry["region"] for entry in comparable}) != manifest.get("regions")):
            raise ContractError("release_corrupt", "release region inventory differs from manifest")
        manifest_digest = sha256(canonical_bytes(manifest)).hexdigest()
        if manifest_digest != release["digest"]:
            raise ContractError("release_corrupt", "manifest digest differs from stored digest")
        if manifest["entry_count"] != len(entries):
            raise ContractError("release_corrupt", "release entry count differs")
        return {"manifest": manifest, "digest": release["digest"], "entries": comparable}

    @staticmethod
    def verify(bundle: dict[str, Any]) -> bool:
        if not isinstance(bundle, dict) or not isinstance(bundle.get("manifest"), dict):
            return False
        if not isinstance(bundle.get("entries"), list) or not isinstance(bundle.get("digest"), str):
            return False
        manifest = bundle["manifest"]
        if manifest.get("entry_count") != len(bundle["entries"]):
            return False
        if entry_fields(manifest) is REGION_ENTRY_FIELDS:
            # A region-less bundle cannot pass as a region-aware release, and
            # the declared region inventory must match the entries it ships.
            regions = {entry.get("region") for entry in bundle["entries"] if isinstance(entry, dict)}
            if None in regions or sorted(regions) != manifest.get("regions"):
                return False
        try:
            entries_hash = sha256(canonical_bytes(bundle["entries"])).hexdigest()
            manifest_hash = sha256(canonical_bytes(manifest)).hexdigest()
        except (TypeError, ValueError):
            return False
        return entries_hash == manifest.get("entries_sha256") and manifest_hash == bundle["digest"]

    def lineage(self, version: int) -> list[dict[str, Any]]:
        seen = set()
        result = []
        current = self.store.release(version)
        while current:
            if current["digest"] in seen:
                raise ContractError("release_cycle", "digest chain repeats")
            seen.add(current["digest"])
            result.append({"version": current["version"], "digest": current["digest"],
                           "batch": current["manifest"]["batch"]})
            if current["version"] == 1:
                break
            previous = self.store.release(current["version"] - 1)
            if current["manifest"]["parent_digest"] != previous["digest"]:
                raise ContractError("release_corrupt", "parent digest chain differs")
            current = previous
        return result
