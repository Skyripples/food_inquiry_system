#!/usr/bin/env python3
"""Analyze whether TFDA candidates can become schema-valid formal products."""

from __future__ import annotations

import argparse
import gzip
import json
import os
import sys
import tempfile
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

try:
    from module.promote_candidate import SchemaValidationError, validate_document
except ModuleNotFoundError:  # Direct execution places the module directory on sys.path.
    from promote_candidate import SchemaValidationError, validate_document


PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_INPUT = PROJECT_ROOT / "data" / "candidates" / "tfda_candidates.json.gz"
DEFAULT_SCHEMA = PROJECT_ROOT / "data" / "product-schema.json"
DEFAULT_OUTPUT = (
    PROJECT_ROOT / "data" / "processed" / "tfda_promotion_analysis.json.gz"
)
NUTRIENT_NAMES = (
    "calories",
    "protein",
    "fat",
    "saturatedFat",
    "transFat",
    "carbohydrates",
    "sugar",
    "sodium",
)
ISSUE_MISSING_BRAND = "missing_brand"
ISSUE_MISSING_BARCODE = "missing_barcode"
ISSUE_MISSING_SERVING = "missing_serving"
ISSUE_INGREDIENTS_PARSE_ERROR = "ingredients_parse_error"
ISSUE_NUTRITION_STRUCTURE_ERROR = "nutrition_structure_error"


class AnalysisError(RuntimeError):
    """Raised when analysis input or generated output is invalid."""


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def load_json(path: Path) -> object:
    with path.open("r", encoding="utf-8") as source:
        return json.load(source)


def load_gzip_json(path: Path) -> object:
    with gzip.open(path, "rt", encoding="utf-8") as source:
        return json.load(source)


def non_empty_string(value: object) -> bool:
    return isinstance(value, str) and bool(value.strip())


def valid_number(value: object) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool) and value >= 0


def serving_is_valid(value: object) -> bool:
    return (
        isinstance(value, dict)
        and valid_number(value.get("value"))
        and non_empty_string(value.get("unit"))
    )


def nutrition_is_valid(value: object) -> bool:
    if not isinstance(value, dict):
        return False
    for name in NUTRIENT_NAMES:
        nutrient = value.get(name)
        if not isinstance(nutrient, dict):
            return False
        if not valid_number(nutrient.get("value")):
            return False
        if not non_empty_string(nutrient.get("unit")):
            return False
    return True


def collect_issues(candidate: dict[str, object]) -> list[str]:
    issues: list[str] = []
    if not non_empty_string(candidate.get("brand")):
        issues.append(ISSUE_MISSING_BRAND)
    if not non_empty_string(candidate.get("barcode")):
        issues.append(ISSUE_MISSING_BARCODE)
    if not serving_is_valid(candidate.get("serving")):
        issues.append(ISSUE_MISSING_SERVING)
    if candidate.get("ingredientsParseError") is not None:
        issues.append(ISSUE_INGREDIENTS_PARSE_ERROR)
    if not nutrition_is_valid(candidate.get("nutrition")):
        issues.append(ISSUE_NUTRITION_STRUCTURE_ERROR)
    return issues


def issue_category(issues: list[str]) -> str:
    if not issues:
        return "ready"
    if len(issues) == 1:
        return issues[0]
    return "multiple_issues"


def normalized_source(candidate: dict[str, object]) -> dict[str, str] | None:
    source = candidate.get("source")
    if not isinstance(source, dict):
        return None
    source_id = source.get("id")
    name = source.get("name")
    url = source.get("dataset_url")
    if not all(non_empty_string(value) for value in (source_id, name, url)):
        return None
    return {
        "id": str(source_id).strip(),
        "name": str(name).strip(),
        "url": str(url).strip(),
    }


def build_schema_product(candidate: dict[str, object]) -> dict[str, object]:
    """Map only known values; omit optional fields that would require guessing."""
    product: dict[str, object] = {
        "id": candidate.get("candidateId"),
        "name": candidate.get("productName"),
    }

    if non_empty_string(candidate.get("brand")):
        product["brand"] = str(candidate["brand"]).strip()
    if non_empty_string(candidate.get("barcode")):
        product["barcode"] = str(candidate["barcode"]).strip()

    serving = candidate.get("serving")
    if serving_is_valid(serving):
        product["serving"] = {
            "amount": serving["value"],
            "unit": str(serving["unit"]).strip(),
        }

    nutrition = candidate.get("nutrition")
    if nutrition_is_valid(nutrition):
        product["nutrition"] = {
            name: {
                "value": nutrition[name]["value"],
                "unit": str(nutrition[name]["unit"]).strip(),
            }
            for name in NUTRIENT_NAMES
        }

    # A parse failure must never be promoted as parsed ingredients. The field is
    # optional in the formal Schema, so omit it instead of guessing boundaries.
    if candidate.get("ingredientsParseError") is None:
        ingredients = candidate.get("ingredients")
        if isinstance(ingredients, list):
            product["ingredients"] = ingredients

    source = normalized_source(candidate)
    if source is not None:
        product["sources"] = [source]
        if "nutrition" in product:
            product["nutritionSource"] = source["id"]
    return product


def schema_required_fields(schema: dict[str, object]) -> dict[str, list[str]]:
    product = schema.get("$defs", {}).get("product", {})
    source = schema.get("$defs", {}).get("source", {})
    ingredient = schema.get("$defs", {}).get("ingredient", {})
    return {
        "document": list(schema.get("required", [])),
        "product": list(product.get("required", [])),
        "source_when_present": list(source.get("required", [])),
        "ingredient_when_present": list(ingredient.get("required", [])),
    }


def analyze(
    candidates_document: dict[str, object],
    schema: dict[str, object],
    *,
    generated_at: str,
    sample_limit: int | None = None,
) -> dict[str, object]:
    candidates = candidates_document.get("candidates")
    if not isinstance(candidates, list):
        raise AnalysisError("candidate document has no candidates array")
    selected = candidates if sample_limit is None else candidates[:sample_limit]
    if not selected:
        raise AnalysisError("candidate document contains no candidates")

    issue_counts: Counter[str] = Counter()
    category_counts: Counter[str] = Counter()
    schema_ready = 0
    schema_blocked = 0
    minimal_complete_ready = 0
    records: list[dict[str, object]] = []

    for candidate in selected:
        if not isinstance(candidate, dict):
            raise AnalysisError("candidate array contains a non-object value")
        issues = collect_issues(candidate)
        issue_counts.update(issues)
        category = issue_category(issues)
        category_counts[category] += 1
        product = build_schema_product(candidate)
        validation_error: str | None = None
        try:
            validate_document(
                {"schemaVersion": "1.0", "products": [product]}, schema
            )
            schema_ready += 1
        except SchemaValidationError as error:
            schema_blocked += 1
            validation_error = str(error)

        if not any(
            issue in issues
            for issue in (
                ISSUE_MISSING_SERVING,
                ISSUE_INGREDIENTS_PARSE_ERROR,
                ISSUE_NUTRITION_STRUCTURE_ERROR,
            )
        ):
            minimal_complete_ready += 1

        records.append({
            "candidateId": candidate.get("candidateId"),
            "traceabilityCode": candidate.get("traceabilityCode"),
            "schemaStatus": "ready" if validation_error is None else "blocked",
            "issueCategory": category,
            "issues": issues,
            "schemaValidationError": validation_error,
        })

    stats: dict[str, object] = {
        "candidate_count": len(selected),
        "schema_ready_count": schema_ready,
        "schema_blocked_count": schema_blocked,
        "issue_counts": dict(sorted(issue_counts.items())),
        "issue_category_counts": dict(sorted(category_counts.items())),
        "multiple_issues_count": category_counts["multiple_issues"],
        "ready_with_complete_nutrition_ingredients_serving_count": minimal_complete_ready,
        "safe_if_optional_brand_and_barcode_are_omitted_count": minimal_complete_ready,
        "safe_under_actual_schema_with_invalid_optional_fields_omitted_count": schema_ready,
        "sample_limited": sample_limit is not None,
    }
    return {
        "source": {
            "name": "TFDA formal-product promotion eligibility analysis",
            "generated_at": generated_at,
            "input_source": candidates_document.get("source"),
            "schema": "data/product-schema.json",
        },
        "schemaRequirements": schema_required_fields(schema),
        "analysisPolicy": {
            "companyNameIsBrand": False,
            "traceabilityCodeIsBarcode": False,
            "optionalInvalidFieldsAreOmitted": True,
            "ingredientsWithParseErrorAreOmitted": True,
        },
        "records": records,
        "stats": stats,
    }


def atomic_write_gzip_json(output: Path, value: dict[str, object]) -> tuple[int, int]:
    output.parent.mkdir(parents=True, exist_ok=True)
    encoded = json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    descriptor, name = tempfile.mkstemp(
        dir=output.parent, prefix=f".{output.name}.", suffix=".tmp"
    )
    os.close(descriptor)
    temporary = Path(name)
    try:
        with temporary.open("wb") as raw:
            with gzip.GzipFile(
                filename="", mode="wb", fileobj=raw, compresslevel=9, mtime=0
            ) as compressed:
                compressed.write(encoded)
            raw.flush()
            os.fsync(raw.fileno())
        with gzip.open(temporary, "rt", encoding="utf-8") as source:
            validated = json.load(source)
        if validated.get("stats") != value.get("stats"):
            raise AnalysisError("generated gzip JSON failed validation")
        os.replace(temporary, output)
        return len(encoded), output.stat().st_size
    finally:
        temporary.unlink(missing_ok=True)


def run(args: argparse.Namespace) -> dict[str, object]:
    input_path = args.input.resolve()
    schema_path = args.schema.resolve()
    output_path = args.output.resolve()
    candidates = load_gzip_json(input_path)
    schema = load_json(schema_path)
    if not isinstance(candidates, dict) or not isinstance(schema, dict):
        raise AnalysisError("input and Schema must both be JSON objects")
    result = analyze(
        candidates, schema, generated_at=utc_now(), sample_limit=args.sample_limit
    )
    uncompressed_size, compressed_size = atomic_write_gzip_json(output_path, result)
    result["stats"]["uncompressed_output_size"] = uncompressed_size
    result["stats"]["compressed_output_size"] = compressed_size
    print(json.dumps(result["stats"], ensure_ascii=False, indent=2))
    print(f"Output: {output_path}")
    return result


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", type=Path, default=DEFAULT_INPUT)
    parser.add_argument("--schema", type=Path, default=DEFAULT_SCHEMA)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--sample-limit", type=int)
    args = parser.parse_args()
    if args.sample_limit is not None and args.sample_limit <= 0:
        parser.error("--sample-limit must be greater than 0")
    return args


def main() -> int:
    try:
        run(parse_args())
    except Exception as error:
        print(f"TFDA promotion analysis failed: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
