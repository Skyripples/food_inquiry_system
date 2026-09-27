#!/usr/bin/env python3
"""Review and optionally promote one explicit candidate into products.json."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys
import tempfile
import unicodedata
from datetime import date
from pathlib import Path
from urllib.parse import urlsplit


PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_CANDIDATES = PROJECT_ROOT / "data" / "candidates" / "pecos_candidates.json"
DEFAULT_PRODUCTS = PROJECT_ROOT / "data" / "products.json"
DEFAULT_SCHEMA = PROJECT_ROOT / "data" / "product-schema.json"
NUTRIENT_KEYS = (
    "calories",
    "protein",
    "fat",
    "saturatedFat",
    "transFat",
    "carbohydrates",
    "sugar",
    "sodium",
)


class PromotionError(ValueError):
    pass


class SchemaValidationError(PromotionError):
    pass


def load_json(path: Path) -> object:
    with path.open("r", encoding="utf-8") as source:
        return json.load(source)


def slugify(value: str) -> str:
    normalized = unicodedata.normalize("NFKC", value).casefold()
    slug = re.sub(r"[^0-9a-z\u3400-\u9fff]+", "-", normalized).strip("-")
    return slug or "product"


def source_id_from_url(url: str) -> str:
    hostname = (urlsplit(url).hostname or "").lower()
    labels = hostname.split(".")
    if len(labels) >= 3 and labels[-2:] in (["com", "tw"], ["org", "tw"], ["net", "tw"]):
        value = labels[-3]
    elif len(labels) >= 2:
        value = labels[-2]
    elif labels:
        value = labels[0]
    else:
        value = "source"
    return slugify(value)


def select_candidate(
    document: dict[str, object],
    *,
    brand: str,
    product_name: str,
    source_name: str,
    barcode: str | None,
    specification: str | None,
) -> tuple[dict[str, object], dict[str, object]]:
    products = [
        item for item in document.get("results", [])
        if item.get("brand") == brand and item.get("product_name") == product_name
    ]
    if len(products) != 1:
        raise PromotionError(
            f"找不到唯一 PECOS 商品：brand={brand!r}, product_name={product_name!r}"
        )
    product = products[0]
    candidates = [
        item for item in product.get("candidates", [])
        if item.get("source_name") == source_name
        and (barcode is None or str(item.get("barcode") or "") == barcode)
        and (specification is None or item.get("specification") == specification)
    ]
    if len(candidates) != 1:
        raise PromotionError(
            f"候選條件必須只符合一筆，目前符合 {len(candidates)} 筆；請改用 barcode 明確指定"
        )
    return product, candidates[0]


def validate_complete_nutrition(nutrition: object) -> dict[str, object]:
    if not isinstance(nutrition, dict):
        raise PromotionError("候選 nutrition 不是物件")
    missing = [key for key in NUTRIENT_KEYS if key not in nutrition]
    if missing:
        raise PromotionError(f"候選缺少完整 8 項營養：{', '.join(missing)}")
    validated: dict[str, object] = {}
    for key in NUTRIENT_KEYS:
        item = nutrition[key]
        if (
            not isinstance(item, dict)
            or isinstance(item.get("value"), bool)
            or not isinstance(item.get("value"), (int, float))
            or item["value"] < 0
            or not isinstance(item.get("unit"), str)
            or not item["unit"].strip()
        ):
            raise PromotionError(f"候選 nutrition.{key} 不是有效數值與單位")
        validated[key] = {"value": item["value"], "unit": item["unit"].strip()}
    return validated


def build_product(
    candidate_document: dict[str, object],
    catalog_product: dict[str, object],
    candidate: dict[str, object],
) -> dict[str, object]:
    source_url = candidate.get("source_url")
    if not isinstance(source_url, str) or not source_url.strip():
        raise PromotionError("候選缺少 source_url")
    source_name = candidate.get("source_name")
    if not isinstance(source_name, str) or not source_name.strip():
        raise PromotionError("候選缺少 source_name")
    matched_name = candidate.get("matched_name")
    if not isinstance(matched_name, str) or not matched_name.strip():
        raise PromotionError("候選缺少 matched_name")
    brand = catalog_product.get("brand")
    if not isinstance(brand, str) or not brand.strip():
        raise PromotionError("PECOS 商品缺少 brand")
    barcode_value = candidate.get("barcode")
    barcode = str(barcode_value).strip() if barcode_value is not None else ""
    if not barcode or not barcode.isdigit():
        raise PromotionError("候選缺少有效的純數字 barcode")
    specification = candidate.get("specification")
    specification_text = specification.strip() if isinstance(specification, str) else ""
    serving = candidate.get("serving")
    if not isinstance(serving, dict):
        raise PromotionError("候選 serving 不是物件")
    amount = serving.get("amount")
    unit = serving.get("unit")
    if (
        isinstance(amount, bool)
        or not isinstance(amount, (int, float))
        or amount < 0
        or not isinstance(unit, str)
        or not unit.strip()
    ):
        raise PromotionError("候選缺少可轉換的 serving amount/unit")

    source_id = source_id_from_url(source_url)
    raw_ingredients = candidate.get("ingredients")
    if not isinstance(raw_ingredients, list):
        raise PromotionError("候選 ingredients 不是陣列")
    ingredients: list[dict[str, str]] = []
    for item in raw_ingredients:
        name = item.get("name") if isinstance(item, dict) else item
        if not isinstance(name, str) or not name.strip():
            raise PromotionError("候選 ingredients 含有無效名稱")
        ingredients.append({"name": name.strip(), "sourceId": source_id})

    generated_at = candidate_document.get("generated_at")
    verified_at = str(generated_at).split("T", 1)[0] if generated_at else ""
    try:
        date.fromisoformat(verified_at)
    except ValueError as error:
        raise PromotionError("候選 generated_at 無法轉換為 verified_at") from error

    identity = barcode or hashlib.sha256(source_url.encode("utf-8")).hexdigest()[:10]
    product_id = slugify("-".join(filter(None, [brand, catalog_product["product_name"], specification_text, identity])))
    return {
        "id": product_id,
        "name": matched_name.strip(),
        "brand": brand.strip(),
        "barcode": barcode,
        "serving": {"amount": amount, "unit": unit.strip()},
        "nutrition": validate_complete_nutrition(candidate.get("nutrition")),
        "ingredients": ingredients,
        "nutritionSource": source_id,
        "sources": [{
            "id": source_id,
            "name": source_name.strip(),
            "url": source_url.strip(),
            "verified_at": verified_at,
        }],
    }


def check_uniqueness(existing: list[dict[str, object]], product: dict[str, object]) -> None:
    barcode = product["barcode"]
    if any(item.get("barcode") == barcode for item in existing):
        raise PromotionError(f"barcode 已存在：{barcode}")
    product_id = product["id"]
    if any(item.get("id") == product_id for item in existing):
        raise PromotionError(f"id 已存在：{product_id}")


def resolve_reference(schema: dict[str, object], reference: str) -> dict[str, object]:
    if not reference.startswith("#/"):
        raise SchemaValidationError(f"不支援外部 Schema reference：{reference}")
    value: object = schema
    for part in reference[2:].split("/"):
        if not isinstance(value, dict) or part not in value:
            raise SchemaValidationError(f"找不到 Schema reference：{reference}")
        value = value[part]
    if not isinstance(value, dict):
        raise SchemaValidationError(f"Schema reference 不是物件：{reference}")
    return value


def validate_schema_node(
    instance: object,
    node: dict[str, object],
    root: dict[str, object],
    path: str = "$",
) -> None:
    if "$ref" in node:
        validate_schema_node(instance, resolve_reference(root, str(node["$ref"])), root, path)
        return
    expected = node.get("type")
    type_matches = {
        "object": isinstance(instance, dict),
        "array": isinstance(instance, list),
        "string": isinstance(instance, str),
        "number": isinstance(instance, (int, float)) and not isinstance(instance, bool),
    }
    if expected in type_matches and not type_matches[expected]:
        raise SchemaValidationError(f"{path} 應為 {expected}")
    if expected == "object":
        required = node.get("required", [])
        for key in required:
            if key not in instance:
                raise SchemaValidationError(f"{path} 缺少必要欄位 {key}")
        properties = node.get("properties", {})
        if node.get("additionalProperties") is False:
            extras = set(instance) - set(properties)
            if extras:
                raise SchemaValidationError(f"{path} 含有未定義欄位：{', '.join(sorted(extras))}")
        for key, value in instance.items():
            child_schema = properties.get(key)
            if isinstance(child_schema, dict):
                validate_schema_node(value, child_schema, root, f"{path}.{key}")
    elif expected == "array" and isinstance(node.get("items"), dict):
        for index, value in enumerate(instance):
            validate_schema_node(value, node["items"], root, f"{path}[{index}]")
    elif expected == "string":
        if "minLength" in node and len(instance) < node["minLength"]:
            raise SchemaValidationError(f"{path} 長度小於 {node['minLength']}")
        if "pattern" in node and re.search(str(node["pattern"]), instance) is None:
            raise SchemaValidationError(f"{path} 不符合 pattern")
        if node.get("format") == "date":
            try:
                date.fromisoformat(instance)
            except ValueError as error:
                raise SchemaValidationError(f"{path} 不是有效日期") from error
        if node.get("format") == "uri":
            parsed = urlsplit(instance)
            if not parsed.scheme or not parsed.netloc:
                raise SchemaValidationError(f"{path} 不是有效 URI")
    elif expected == "number" and "minimum" in node and instance < node["minimum"]:
        raise SchemaValidationError(f"{path} 小於 minimum {node['minimum']}")


def validate_document(document: dict[str, object], schema: dict[str, object]) -> None:
    validate_schema_node(document, schema, schema)


def atomic_write_json(path: Path, document: dict[str, object]) -> None:
    descriptor, temporary_name = tempfile.mkstemp(
        dir=path.parent,
        prefix=f".{path.name}.",
        suffix=".tmp",
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as output:
            json.dump(document, output, ensure_ascii=False, indent=2)
            output.write("\n")
            output.flush()
            os.fsync(output.fileno())
        os.replace(temporary, path)
    except BaseException:
        temporary.unlink(missing_ok=True)
        raise


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--brand", required=True, help="PECOS 品牌，需完全相符")
    parser.add_argument("--product-name", required=True, help="PECOS 原始商品名稱，需完全相符")
    parser.add_argument("--source-name", required=True, help="候選來源名稱，需完全相符")
    selector = parser.add_mutually_exclusive_group(required=True)
    selector.add_argument("--barcode", help="候選條碼")
    selector.add_argument("--specification", help="候選規格；若仍有多筆，請改用 barcode")
    parser.add_argument("--apply", action="store_true", help="驗證後原子寫入 products.json；預設為 dry-run")
    parser.add_argument("--candidates", type=Path, default=DEFAULT_CANDIDATES)
    parser.add_argument("--products", type=Path, default=DEFAULT_PRODUCTS)
    parser.add_argument("--schema", type=Path, default=DEFAULT_SCHEMA)
    return parser.parse_args()


def run(args: argparse.Namespace) -> dict[str, object]:
    candidates = load_json(args.candidates.resolve())
    products_document = load_json(args.products.resolve())
    schema = load_json(args.schema.resolve())
    if not isinstance(candidates, dict) or not isinstance(products_document, dict) or not isinstance(schema, dict):
        raise PromotionError("輸入 JSON 根節點必須是物件")
    catalog_product, candidate = select_candidate(
        candidates,
        brand=args.brand,
        product_name=args.product_name,
        source_name=args.source_name,
        barcode=args.barcode,
        specification=args.specification,
    )
    product = build_product(candidates, catalog_product, candidate)
    existing = products_document.get("products")
    if not isinstance(existing, list):
        raise PromotionError("products.json 的 products 不是陣列")
    check_uniqueness(existing, product)
    promoted_document = {**products_document, "products": [*existing, product]}
    validate_document(promoted_document, schema)

    print(json.dumps(product, ensure_ascii=False, indent=2))
    print("Schema 驗證：通過", file=sys.stderr)
    if args.apply:
        atomic_write_json(args.products.resolve(), promoted_document)
        print(f"已匯入：{args.products.resolve()}", file=sys.stderr)
    else:
        print("模式：dry-run（products.json 未修改）", file=sys.stderr)
    return product


def main() -> int:
    args = parse_args()
    try:
        run(args)
    except (OSError, json.JSONDecodeError, PromotionError) as error:
        print(f"候選轉換失敗：{error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
