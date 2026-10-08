#!/usr/bin/env python3
"""Dry-run validation for promoting high-completeness TFDA candidates.

This tool never writes products.json. It converts eligible candidates in memory,
validates each result against the current formal product Schema, and reports all
identity conflicts that would make a future batch import unsafe.
"""

from __future__ import annotations

import argparse
import gzip
import json
import sys
from collections import Counter
from pathlib import Path

try:
    from module.promote_candidate import SchemaValidationError, validate_document
except ModuleNotFoundError:  # Direct execution places the module directory on sys.path.
    from promote_candidate import SchemaValidationError, validate_document


PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_CANDIDATES = PROJECT_ROOT / "data" / "candidates" / "tfda_candidates.json.gz"
DEFAULT_PRODUCTS = PROJECT_ROOT / "data" / "products.json"
DEFAULT_SCHEMA = PROJECT_ROOT / "data" / "product-schema.json"
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


class PromotionDryRunError(RuntimeError):
    """Raised when the dry-run input or generated product is structurally invalid."""


def load_json(path: Path) -> object:
    with path.open("r", encoding="utf-8") as source:
        return json.load(source)


def load_gzip_json(path: Path) -> object:
    with gzip.open(path, "rt", encoding="utf-8") as source:
        return json.load(source)


def non_empty_string(value: object) -> bool:
    return isinstance(value, str) and bool(value.strip())


def valid_number(value: object) -> bool:
    return (
        isinstance(value, (int, float))
        and not isinstance(value, bool)
        and value >= 0
    )


def serving_is_valid(value: object) -> bool:
    return (
        isinstance(value, dict)
        and valid_number(value.get("value"))
        and non_empty_string(value.get("unit"))
    )


def nutrition_is_complete(value: object) -> bool:
    if not isinstance(value, dict):
        return False
    for nutrient_name in NUTRIENT_NAMES:
        nutrient = value.get(nutrient_name)
        if not isinstance(nutrient, dict):
            return False
        if not valid_number(nutrient.get("value")):
            return False
        if not non_empty_string(nutrient.get("unit")):
            return False
    return True


def ingredients_are_parsed(candidate: dict[str, object]) -> bool:
    ingredients = candidate.get("ingredients")
    return (
        candidate.get("ingredientsParseError") is None
        and isinstance(ingredients, list)
        and bool(ingredients)
        and all(
            isinstance(ingredient, dict)
            and non_empty_string(ingredient.get("name"))
            for ingredient in ingredients
        )
    )


def is_eligible(candidate: dict[str, object]) -> bool:
    return (
        serving_is_valid(candidate.get("serving"))
        and nutrition_is_complete(candidate.get("nutrition"))
        and ingredients_are_parsed(candidate)
    )


def build_source(candidate: dict[str, object]) -> dict[str, str]:
    source = candidate.get("source")
    if not isinstance(source, dict):
        raise PromotionDryRunError("candidate source is not an object")
    source_id = source.get("id")
    source_name = source.get("name")
    source_url = source.get("dataset_url")
    if not all(non_empty_string(value) for value in (source_id, source_name, source_url)):
        raise PromotionDryRunError(
            "candidate source requires non-empty id, name, and dataset_url"
        )
    return {
        "id": str(source_id).strip(),
        "name": str(source_name).strip(),
        "url": str(source_url).strip(),
    }


def build_product(candidate: dict[str, object]) -> dict[str, object]:
    """Map a single eligible candidate without inferring brand or barcode."""
    if not is_eligible(candidate):
        raise PromotionDryRunError("candidate does not meet high-completeness rules")

    source = build_source(candidate)
    serving = candidate["serving"]
    nutrition = candidate["nutrition"]
    ingredients = candidate["ingredients"]
    product: dict[str, object] = {
        "id": candidate.get("candidateId"),
        "name": candidate.get("productName"),
        "manufacturer": candidate.get("companyName"),
        "traceabilityCode": candidate.get("traceabilityCode"),
        "serving": {
            "amount": serving["value"],
            "unit": str(serving["unit"]).strip(),
        },
        "nutrition": {
            nutrient_name: {
                "value": nutrition[nutrient_name]["value"],
                "unit": str(nutrition[nutrient_name]["unit"]).strip(),
            }
            for nutrient_name in NUTRIENT_NAMES
        },
        "ingredients": [
            {
                "name": str(ingredient["name"]).strip(),
                "sourceId": source["id"],
            }
            for ingredient in ingredients
        ],
        "sources": [source],
        "nutritionSource": source["id"],
    }
    return product


def duplicate_values(values: list[str]) -> set[str]:
    return {value for value, count in Counter(values).items() if count > 1}


def identity_key(candidate: dict[str, object]) -> tuple[str, str, str]:
    return tuple(
        str(candidate.get(field, "")).strip()
        for field in ("companyName", "productName", "packageSpecification")
    )


def analyze_dry_run(
    candidate_document: dict[str, object],
    products_document: dict[str, object],
    schema: dict[str, object],
) -> dict[str, object]:
    candidates = candidate_document.get("candidates")
    existing_products = products_document.get("products")
    if not isinstance(candidates, list):
        raise PromotionDryRunError("candidate document has no candidates array")
    if not isinstance(existing_products, list):
        raise PromotionDryRunError("products document has no products array")

    invalid_serving_count = sum(
        not serving_is_valid(candidate.get("serving"))
        for candidate in candidates
        if isinstance(candidate, dict)
    )
    incomplete_nutrition_count = sum(
        not nutrition_is_complete(candidate.get("nutrition"))
        for candidate in candidates
        if isinstance(candidate, dict)
    )
    ingredients_parse_failure_count = sum(
        not ingredients_are_parsed(candidate)
        for candidate in candidates
        if isinstance(candidate, dict)
    )
    eligible = [
        candidate
        for candidate in candidates
        if isinstance(candidate, dict) and is_eligible(candidate)
    ]
    products: list[dict[str, object]] = []
    conversion_failures: list[dict[str, object]] = []
    for candidate in eligible:
        try:
            products.append(build_product(candidate))
        except PromotionDryRunError as error:
            conversion_failures.append({
                "candidateId": candidate.get("candidateId"),
                "reason": str(error),
            })

    ids = [
        str(product["id"]).strip()
        for product in products
        if non_empty_string(product.get("id"))
    ]
    traceability_codes = [
        str(product["traceabilityCode"]).strip()
        for product in products
        if non_empty_string(product.get("traceabilityCode"))
    ]
    duplicate_ids = duplicate_values(ids)
    duplicate_traceability_codes = duplicate_values(traceability_codes)
    identity_counts = Counter(identity_key(candidate) for candidate in eligible)
    duplicate_identity_groups = {
        identity for identity, count in identity_counts.items() if count > 1
    }

    existing_ids = {
        str(product["id"]).strip()
        for product in existing_products
        if isinstance(product, dict) and non_empty_string(product.get("id"))
    }
    existing_traceability_codes = {
        str(product["traceabilityCode"]).strip()
        for product in existing_products
        if isinstance(product, dict)
        and non_empty_string(product.get("traceabilityCode"))
    }

    schema_failures: list[dict[str, object]] = []
    schema_pass_ids: set[str] = set()
    for product in products:
        try:
            validate_document(
                {"schemaVersion": products_document.get("schemaVersion"), "products": [product]},
                schema,
            )
            if non_empty_string(product.get("id")):
                schema_pass_ids.add(str(product["id"]).strip())
        except SchemaValidationError as error:
            schema_failures.append({
                "id": product.get("id"),
                "traceabilityCode": product.get("traceabilityCode"),
                "reason": str(error),
            })

    id_conflict_records = sum(product.get("id") in duplicate_ids for product in products)
    traceability_conflict_records = sum(
        product.get("traceabilityCode") in duplicate_traceability_codes
        for product in products
    )
    existing_id_conflicts = {
        str(product["id"]).strip()
        for product in products
        if non_empty_string(product.get("id"))
        and str(product["id"]).strip() in existing_ids
    }
    existing_traceability_conflicts = {
        str(product["traceabilityCode"]).strip()
        for product in products
        if non_empty_string(product.get("traceabilityCode"))
        and str(product["traceabilityCode"]).strip()
        in existing_traceability_codes
    }
    existing_conflict_records = sum(
        (
            non_empty_string(product.get("id"))
            and str(product["id"]).strip() in existing_ids
        )
        or (
            non_empty_string(product.get("traceabilityCode"))
            and str(product["traceabilityCode"]).strip()
            in existing_traceability_codes
        )
        for product in products
    )

    safe_products = [
        product
        for product in products
        if non_empty_string(product.get("id"))
        and str(product["id"]).strip() in schema_pass_ids
        and product.get("id") not in duplicate_ids
        and product.get("traceabilityCode") not in duplicate_traceability_codes
        and product.get("id") not in existing_ids
        and product.get("traceabilityCode") not in existing_traceability_codes
    ]

    # Validate the exact future document shape as a final dry-run assertion.
    batch_schema_error: str | None = None
    try:
        validate_document(
            {
                "schemaVersion": products_document.get("schemaVersion"),
                "products": [*existing_products, *safe_products],
            },
            schema,
        )
    except SchemaValidationError as error:
        batch_schema_error = str(error)

    return {
        "mode": "dry-run",
        "products_json_modified": False,
        "candidate_count": len(candidates),
        "convertible_candidate_count": len(eligible),
        "ineligible_candidate_count": len(candidates) - len(eligible),
        "invalid_serving_count": invalid_serving_count,
        "incomplete_nutrition_count": incomplete_nutrition_count,
        "ingredients_parse_failure_count": ingredients_parse_failure_count,
        "conversion_success_count": len(products),
        "conversion_failure_count": len(conversion_failures),
        "schema_pass_count": len(products) - len(schema_failures),
        "schema_failure_count": len(schema_failures),
        "duplicate_id_value_count": len(duplicate_ids),
        "duplicate_id_record_count": id_conflict_records,
        "duplicate_traceability_code_value_count": len(
            duplicate_traceability_codes
        ),
        "duplicate_traceability_code_record_count": traceability_conflict_records,
        "existing_product_id_conflict_count": len(existing_id_conflicts),
        "existing_product_traceability_code_conflict_count": len(
            existing_traceability_conflicts
        ),
        "existing_formal_product_conflict_count": existing_conflict_records,
        "duplicate_company_product_specification_group_count": len(
            duplicate_identity_groups
        ),
        "duplicate_company_product_specification_record_count": sum(
            identity_counts[identity] for identity in duplicate_identity_groups
        ),
        "duplicate_company_product_specification_records_merged": 0,
        "final_safe_import_count": len(safe_products),
        "batch_schema_validation": "pass" if batch_schema_error is None else "fail",
        "batch_schema_error": batch_schema_error,
        "details": {
            "conversion_failures": conversion_failures,
            "schema_failures": schema_failures,
            "duplicate_ids": sorted(duplicate_ids),
            "duplicate_traceability_codes": sorted(duplicate_traceability_codes),
            "existing_id_conflicts": sorted(existing_id_conflicts),
            "existing_traceability_code_conflicts": sorted(
                existing_traceability_conflicts
            ),
        },
    }


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--candidates", type=Path, default=DEFAULT_CANDIDATES)
    parser.add_argument("--products", type=Path, default=DEFAULT_PRODUCTS)
    parser.add_argument("--schema", type=Path, default=DEFAULT_SCHEMA)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    try:
        candidates = load_gzip_json(args.candidates.resolve())
        products = load_json(args.products.resolve())
        schema = load_json(args.schema.resolve())
        if not all(isinstance(value, dict) for value in (candidates, products, schema)):
            raise PromotionDryRunError("all input document roots must be objects")
        result = analyze_dry_run(candidates, products, schema)
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0 if result["batch_schema_validation"] == "pass" else 1
    except (OSError, json.JSONDecodeError, PromotionDryRunError) as error:
        print(f"dry-run failed: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
