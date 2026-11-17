from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, asdict
from decimal import Decimal
from typing import Any

from .normalize import period_index, period_from_index
from .registry import Registry, region_identifier


@dataclass(frozen=True)
class CoverageRow:
    source: str
    indicator: str
    region: str
    first: str
    last: str
    observed: int
    expected: int
    missing: list[str]

    def document(self) -> dict[str, Any]:
        return asdict(self)


class CoverageReporter:
    def __init__(self, store, registry: Registry):
        self.store = store
        self.registry = registry

    def report(self, version: int, indicator_key: str | None = None,
               source_key: str | None = None, region: str | None = None) -> dict[str, Any]:
        self.store.release(version)
        if indicator_key is not None:
            self.registry.indicator(indicator_key)
        if source_key is not None:
            self.registry.source(source_key)
        if region is not None:
            region_identifier(region, "region")
        grouped: dict[tuple[str, str, str], set[str]] = defaultdict(set)
        for row in self.store.release_entries(version):
            if indicator_key is not None and row["indicator"] != indicator_key:
                continue
            if source_key is not None and row["source"] != source_key:
                continue
            # Coverage is assessed inside one region; a global series cannot
            # fill a province's gaps and vice versa.
            if region is not None and row["region"] != region:
                continue
            grouped[(row["source"], row["indicator"], row["region"])].add(row["period"])
        results = []
        for (source, indicator_key, series_region), periods in sorted(grouped.items()):
            indicator = self.registry.indicator(indicator_key)
            indexes = {period_index(period, indicator.cadence) for period in periods}
            start, end = min(indexes), max(indexes)
            missing = [period_from_index(value, indicator.cadence)
                       for value in range(start, end + 1) if value not in indexes]
            results.append(CoverageRow(source, indicator_key, series_region,
                                       period_from_index(start, indicator.cadence),
                                       period_from_index(end, indicator.cadence), len(indexes),
                                       end - start + 1, missing))
        return {"version": version, "series_count": len(results),
                "observed": sum(row.observed for row in results),
                "missing": sum(len(row.missing) for row in results),
                "regions": sorted({row.region for row in results}),
                "series": [row.document() for row in results]}

    def source_matrix(self, version: int, indicator_key: str, *,
                      region: str | None = None) -> dict[str, Any]:
        indicator = self.registry.indicator(indicator_key)
        self.store.release(version)
        if region is not None:
            region_identifier(region, "region")
        rows = [row for row in self.store.release_entries(version)
                if row["indicator"] == indicator_key and (region is None or row["region"] == region)]
        # Each region is its own comparison universe so a national figure is
        # never placed in the same cell as a province figure.
        by_region: dict[str, list[dict[str, Any]]] = defaultdict(list)
        for row in rows:
            by_region[row["region"]].append(row)
        regions = []
        for series_region in sorted(by_region):
            series_rows = by_region[series_region]
            periods = sorted({row["period"] for row in series_rows})
            sources = sorted({row["source"] for row in series_rows})
            lookup = {(row["source"], row["period"]): row for row in series_rows}
            matrix = []
            for period in periods:
                cells = {}
                for source in sources:
                    row = lookup.get((source, period))
                    cells[source] = {"value": row["value"], "batch_id": row["batch_id"]} if row else None
                matrix.append({"period": period, "sources": cells})
            regions.append({"region": series_region, "sources": sources, "periods": matrix})
        return {"version": version, "indicator": indicator_key,
                "unit": indicator.canonical_unit, "regions": regions}

    def discrepancy_report(self, version: int, indicator_key: str, *,
                           region: str | None = None) -> dict[str, Any]:
        matrix = self.source_matrix(version, indicator_key, region=region)
        disagreements = []
        compared = 0
        for series_region in matrix["regions"]:
            for row in series_region["periods"]:
                present = {key: Decimal(value["value"]) for key, value in row["sources"].items()
                           if value is not None}
                if len(present) < 2:
                    continue
                compared += 1
                minimum, maximum = min(present.values()), max(present.values())
                if minimum != maximum:
                    disagreements.append({"region": series_region["region"], "period": row["period"],
                                          "minimum": str(minimum), "maximum": str(maximum),
                                          "spread": str(maximum - minimum),
                                          "sources": {key: str(value) for key, value in present.items()}})
        return {"version": version, "indicator": indicator_key,
                "regions": [item["region"] for item in matrix["regions"]],
                "compared_periods": compared, "disagreements": disagreements}


class BatchCoverage:
    def __init__(self, store, registry: Registry):
        self.store = store
        self.registry = registry

    def report(self, batch_id: int) -> dict[str, Any]:
        batch = self.store.batch(batch_id)
        grouped: dict[tuple[str, str], list[str]] = defaultdict(list)
        for row in self.store.measurements(batch_id):
            grouped[(row["indicator"], row["region"])].append(row["period"])
        series = []
        for (key, region), periods in sorted(grouped.items()):
            indicator = self.registry.indicator(key)
            indexes = {period_index(period, indicator.cadence) for period in periods}
            start, end = min(indexes), max(indexes)
            missing = [period_from_index(index, indicator.cadence)
                       for index in range(start, end + 1) if index not in indexes]
            series.append({"indicator": key, "region": region, "cadence": indicator.cadence,
                           "first": period_from_index(start, indicator.cadence),
                           "last": period_from_index(end, indicator.cadence),
                           "rows": len(periods), "distinct_periods": len(indexes),
                           "missing": missing})
        return {"batch_id": batch_id, "source": batch["source"],
                "region": batch["region"], "region_origin": batch["region_origin"],
                "declared_rows": batch["row_count"], "accepted_rows": sum(row["rows"] for row in series),
                "series": series, "status": batch["status"]}
