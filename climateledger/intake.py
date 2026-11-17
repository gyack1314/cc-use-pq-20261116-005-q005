from __future__ import annotations

from dataclasses import dataclass
from hashlib import sha256
from io import StringIO
from pathlib import Path
from typing import Any
import csv
import json

from .registry import ContractError, LEGACY_REGION, explicit_region, identifier


REGION_ORIGIN_MANIFEST = "manifest"
REGION_ORIGIN_ROWS = "rows"
REGION_ORIGIN_LEGACY = "legacy_default"


@dataclass(frozen=True)
class BatchEnvelope:
    source: str
    batch_id: str
    format: str
    rows: tuple[dict[str, Any], ...]
    payload_hash: str
    declared_count: int
    metadata: dict[str, Any]
    region: str | None = None
    region_default: str = LEGACY_REGION
    region_origin: str = REGION_ORIGIN_LEGACY

    def region_interpretation(self) -> dict[str, Any]:
        """Deterministic, replayable account of how rows obtained a region."""
        if self.region_origin == REGION_ORIGIN_MANIFEST:
            explanation = f"all rows use manifest region {self.region}"
        elif self.region_origin == REGION_ORIGIN_ROWS:
            explanation = "each row carries an explicit region column"
        else:
            explanation = (f"no region declared; rows are stored in reserved region "
                           f"{LEGACY_REGION}, never inferred as a province or country")
        return {"origin": self.region_origin, "batch_region": self.region,
                "row_default": self.region_default, "explanation": explanation}


def canonical_bytes(value: Any) -> bytes:
    return json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":"), allow_nan=False).encode("utf-8")


def file_bytes(path: Path, maximum: int = 8_000_000) -> bytes:
    if not path.is_file() or path.is_symlink():
        raise ContractError("missing_file", f"local file not found: {path}")
    size = path.stat().st_size
    if size > maximum:
        raise ContractError("file_too_large", f"file exceeds {maximum} bytes")
    content = path.read_bytes()
    if len(content) != size:
        raise ContractError("file_changed", "file changed while reading")
    return content


def manifest_value(path: Path) -> dict[str, Any]:
    raw = file_bytes(path, 100_000)
    try:
        value = json.loads(raw.decode("utf-8-sig"))
    except (UnicodeError, json.JSONDecodeError):
        raise ContractError("manifest_json", "manifest must be UTF-8 JSON") from None
    if not isinstance(value, dict):
        raise ContractError("manifest_shape", "manifest must be a JSON object")
    return value


def valid_mapping(mapping: Any) -> dict[str, str]:
    required = frozenset(("indicator", "period", "value", "unit"))
    if not isinstance(mapping, dict) or not required <= set(mapping) <= required | {"region"}:
        raise ContractError("column_mapping", "columns must map indicator, period, value and unit, "
                                              "with optional region")
    for key, column in mapping.items():
        if not isinstance(column, str) or not column.strip():
            raise ContractError("column_mapping", f"empty mapping for {key}")
    if len(set(mapping.values())) != len(mapping):
        raise ContractError("column_mapping", "one column cannot serve two fields")
    return mapping


def parse_json_rows(data: bytes) -> list[dict[str, Any]]:
    try:
        value = json.loads(data.decode("utf-8-sig"))
    except (UnicodeError, json.JSONDecodeError):
        raise ContractError("payload_json", "payload must be UTF-8 JSON") from None
    if not isinstance(value, list):
        raise ContractError("payload_shape", "JSON payload must be a list")
    if any(not isinstance(row, dict) for row in value):
        raise ContractError("row_shape", "each JSON row must be an object")
    return value


def parse_csv_rows(data: bytes) -> list[dict[str, Any]]:
    try:
        content = data.decode("utf-8-sig")
    except UnicodeError:
        raise ContractError("payload_utf8", "CSV payload must be UTF-8") from None
    if "\x00" in content:
        raise ContractError("payload_nul", "CSV cannot contain NUL")
    reader = csv.DictReader(StringIO(content, newline=""), strict=True)
    if not reader.fieldnames or len(set(reader.fieldnames)) != len(reader.fieldnames):
        raise ContractError("csv_header", "CSV header missing or duplicated")
    try:
        rows = list(reader)
    except csv.Error as exc:
        raise ContractError("csv_parse", str(exc)) from None
    if any(None in row for row in rows):
        raise ContractError("csv_columns", "CSV row contains extra fields")
    return rows


def load_batch(manifest_path: str | Path) -> BatchEnvelope:
    path = Path(manifest_path)
    manifest = manifest_value(path)
    source = identifier(manifest.get("source"), "source")
    batch_id = identifier(manifest.get("batch_id"), "batch_id")
    format_name = manifest.get("format")
    if format_name not in {"csv", "json"}:
        raise ContractError("payload_format", "format must be csv or json")
    filename = manifest.get("payload")
    if not isinstance(filename, str) or not filename or Path(filename).is_absolute() or Path(filename).name != filename:
        raise ContractError("payload_path", "payload must be a sibling filename")
    if filename == path.name:
        raise ContractError("payload_path", "payload cannot be the manifest")
    mapping = valid_mapping(manifest.get("columns"))
    declared_count = manifest.get("row_count")
    if type(declared_count) is not int or declared_count < 1 or declared_count > 100_000:
        raise ContractError("row_count", "row_count must be 1-100000")
    metadata = manifest.get("metadata", {})
    if not isinstance(metadata, dict) or len(canonical_bytes(metadata)) > 8192:
        raise ContractError("metadata", "metadata must be a small object")
    raw_region = manifest.get("region")
    region = explicit_region(raw_region, "manifest region") if raw_region is not None else LEGACY_REGION
    if "region" in mapping and raw_region is not None:
        raise ContractError("region_conflict", "manifest region cannot be combined with a region column")
    if "region" in mapping:
        origin = REGION_ORIGIN_ROWS
    elif raw_region is not None:
        origin = REGION_ORIGIN_MANIFEST
    else:
        origin = REGION_ORIGIN_LEGACY
    payload = file_bytes(path.parent / filename)
    digest = sha256(payload).hexdigest()
    expected_hash = manifest.get("sha256")
    if not isinstance(expected_hash, str) or len(expected_hash) != 64 or expected_hash.lower() != digest:
        raise ContractError("payload_hash", "payload SHA-256 mismatch")
    raw_rows = parse_csv_rows(payload) if format_name == "csv" else parse_json_rows(payload)
    if len(raw_rows) != declared_count:
        raise ContractError("row_count", f"declared {declared_count}, found {len(raw_rows)}")
    rows = []
    for number, row in enumerate(raw_rows, 1):
        if any(column not in row for column in mapping.values()):
            raise ContractError("missing_column", f"row {number} missing mapped column")
        normalized = {key: row[column] for key, column in mapping.items()}
        if origin == REGION_ORIGIN_MANIFEST:
            normalized["region"] = region
        # When region comes from a column it is validated per row by the
        # normalizer, so a bad cell is one rejected row rather than a lost batch.
        normalized["line"] = number
        rows.append(normalized)
    return BatchEnvelope(source, batch_id, format_name, tuple(rows), digest, declared_count,
                         metadata, region if origin == REGION_ORIGIN_MANIFEST else None,
                         region, origin)


def write_manifest(directory: str | Path, *, source: str, batch_id: str,
                   rows: list[dict[str, Any]], format_name: str = "json",
                   metadata: dict[str, Any] | None = None,
                   region: str | None = None) -> Path:
    root = Path(directory)
    root.mkdir(parents=True, exist_ok=True)
    identifier(source, "source")
    identifier(batch_id, "batch_id")
    if format_name not in {"csv", "json"}:
        raise ContractError("payload_format", "format must be csv or json")
    if not rows:
        raise ContractError("empty_rows", "at least one row is required")
    columns = ["indicator", "period", "value", "unit"]
    if any("region" in row for row in rows):
        if region is not None:
            raise ContractError("region_conflict", "manifest region cannot be combined with a region column")
        columns.append("region")
        region = None
    else:
        region = explicit_region(region, "manifest region") if region is not None else None
    filename = f"{batch_id}.{format_name}"
    payload_path = root / filename
    if format_name == "json":
        payload = canonical_bytes(rows)
    else:
        stream = StringIO(newline="")
        writer = csv.DictWriter(stream, fieldnames=columns,
                                extrasaction="ignore", lineterminator="\n")
        writer.writeheader()
        writer.writerows(rows)
        payload = stream.getvalue().encode("utf-8")
    payload_path.write_bytes(payload)
    manifest = {"source": source, "batch_id": batch_id, "format": format_name,
                "payload": filename, "columns": {key: key for key in columns},
                "row_count": len(rows), "sha256": sha256(payload).hexdigest(), "metadata": metadata or {}}
    if region is not None:
        manifest["region"] = region
    manifest_path = root / f"{batch_id}.manifest.json"
    manifest_path.write_bytes(canonical_bytes(manifest))
    return manifest_path
