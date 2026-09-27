#!/usr/bin/env python3
"""Collect review-only food candidates for products in the PECOS raw catalog."""

from __future__ import annotations

import argparse
import gzip
import json
import os
import re
import ssl
import tempfile
import time
from datetime import datetime, timezone
from difflib import SequenceMatcher
from html.parser import HTMLParser
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlencode
from urllib.request import Request, urlopen


PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_CATALOG = PROJECT_ROOT / "data" / "raw" / "pecos_catalog.json.gz"
DEFAULT_OUTPUT = PROJECT_ROOT / "data" / "candidates" / "pecos_candidates.json"
PECOS_API_URL = "https://www.pecos.com.tw/api.html"
PXGO_SEARCH_URL = "https://shop.pxgo.com.tw/hourArrive/search/result?q={}"
USER_AGENT = (
    "food-inquiry-system candidate collector/2.2 "
    "(+https://github.com/Skyripples/food_inquiry_system)"
)


def normalize_name(value: str) -> str:
    return re.sub(r"[^0-9a-z\u3400-\u9fff]+", "", value.casefold())


class ProductIdParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.products: list[tuple[str, str]] = []
        self.seen: set[tuple[str, str]] = set()

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag != "a":
            return
        values = {key: value or "" for key, value in attrs}
        product_id = values.get("data-rel", "").strip()
        name = " ".join(values.get("data-name", "").split())
        key = (product_id, name)
        if product_id.isdigit() and name and key not in self.seen:
            self.seen.add(key)
            self.products.append(key)


class ProductDetailParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.in_heading = False
        self.in_serving = False
        self.name_parts: list[str] = []
        self.serving_parts: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        values = {key: value or "" for key, value in attrs}
        if tag == "h1":
            self.in_heading = True
        elif self.in_heading and tag == "span" and "gray" in values.get("class", "").split():
            self.in_serving = True

    def handle_endtag(self, tag: str) -> None:
        if tag == "span" and self.in_serving:
            self.in_serving = False
        elif tag == "h1":
            self.in_heading = False
            self.in_serving = False

    def handle_data(self, data: str) -> None:
        if not self.in_heading:
            return
        if self.in_serving:
            self.serving_parts.append(data)
        else:
            self.name_parts.append(data)

    @property
    def name(self) -> str:
        return " ".join(" ".join(self.name_parts).split())

    @property
    def serving_text(self) -> str:
        return " ".join(" ".join(self.serving_parts).split())


class JsonLdParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.in_json_ld = False
        self.current: list[str] = []
        self.documents: list[dict[str, object]] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        values = {key: value or "" for key, value in attrs}
        if tag == "script" and values.get("type") == "application/ld+json":
            self.in_json_ld = True
            self.current = []

    def handle_endtag(self, tag: str) -> None:
        if tag != "script" or not self.in_json_ld:
            return
        self.in_json_ld = False
        try:
            value = json.loads("".join(self.current))
            if isinstance(value, dict):
                self.documents.append(value)
        except json.JSONDecodeError:
            pass

    def handle_data(self, data: str) -> None:
        if self.in_json_ld:
            self.current.append(data)


class NuxtDataParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.in_data = False
        self.parts: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        values = {key: value or "" for key, value in attrs}
        if tag == "script" and values.get("id") == "__NUXT_DATA__":
            self.in_data = True

    def handle_endtag(self, tag: str) -> None:
        if tag == "script":
            self.in_data = False

    def handle_data(self, data: str) -> None:
        if self.in_data:
            self.parts.append(data)


class DetailTableParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.in_cell = False
        self.cell_parts: list[str] = []
        self.row: list[str] = []
        self.rows: list[list[str]] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag == "td":
            self.in_cell = True
            self.cell_parts = []
        elif tag == "br" and self.in_cell:
            self.cell_parts.append("\n")

    def handle_endtag(self, tag: str) -> None:
        if tag == "td" and self.in_cell:
            self.in_cell = False
            self.row.append("".join(self.cell_parts).strip())
        elif tag == "tr":
            if self.row:
                self.rows.append(self.row)
            self.row = []

    def handle_data(self, data: str) -> None:
        if self.in_cell:
            self.cell_parts.append(data)


class HttpClient:
    def __init__(self, timeout: float, retries: int, delay: float) -> None:
        self.timeout = timeout
        self.retries = retries
        self.delay = delay
        self.last_request_at: float | None = None
        self.ssl_context = ssl.create_default_context()
        if hasattr(ssl, "VERIFY_X509_STRICT"):
            self.ssl_context.verify_flags &= ~ssl.VERIFY_X509_STRICT

    def _wait(self) -> None:
        if self.last_request_at is None:
            return
        remaining = self.delay - (time.monotonic() - self.last_request_at)
        if remaining > 0:
            time.sleep(remaining)

    def request(self, url: str, form: dict[str, str] | None = None) -> bytes:
        data = urlencode(form).encode("utf-8") if form else None
        last_error: Exception | None = None
        for attempt in range(self.retries + 1):
            self._wait()
            request = Request(
                url,
                data=data,
                headers={
                    "User-Agent": USER_AGENT,
                    "Accept": "application/json,text/html;q=0.9,*/*;q=0.8",
                    "Accept-Language": "zh-TW,zh;q=0.9",
                },
            )
            try:
                with urlopen(request, timeout=self.timeout, context=self.ssl_context) as response:
                    self.last_request_at = time.monotonic()
                    return response.read()
            except (HTTPError, URLError, TimeoutError, OSError) as error:
                self.last_request_at = time.monotonic()
                last_error = error
                if isinstance(error, HTTPError) and 400 <= error.code < 500 and error.code != 429:
                    break
                if attempt < self.retries:
                    time.sleep(min(2 ** attempt, 8))
        raise RuntimeError(f"request failed after {self.retries + 1} attempt(s): {last_error}")

    def get_text(self, url: str) -> str:
        return self.request(url).decode("utf-8", errors="replace")

    def post_json(self, url: str, form: dict[str, str]) -> dict[str, object]:
        return json.loads(self.request(url, form).decode("utf-8"))


def load_targets(catalog_path: Path, limit: int) -> list[dict[str, str]]:
    with gzip.open(catalog_path, "rt", encoding="utf-8") as source:
        catalog = json.load(source)
    targets: list[dict[str, str]] = []
    for brand in catalog.get("brands", []):
        for product in brand.get("products", []):
            targets.append({
                "product_name": product["name"],
                "brand": brand["name"],
                "catalog_source": brand["url"],
            })
            if len(targets) == limit:
                return targets
    return targets


def parse_serving(text: str) -> dict[str, object]:
    if not text:
        return {}
    single = re.fullmatch(r"\s*(\d+(?:\.\d+)?)\s*([a-zA-Z]+)\s*", text)
    if single:
        amount = float(single.group(1))
        return {
            "amount": int(amount) if amount.is_integer() else amount,
            "unit": single.group(2),
            "text": text,
        }
    return {"text": text}


def dereference_field(nodes: list[object], record: dict[str, object], field: str) -> object:
    value = record.get(field)
    if isinstance(value, int) and 0 <= value < len(nodes):
        return nodes[value]
    return value


def extract_pxgo_record(html: str) -> dict[str, object]:
    parser = NuxtDataParser()
    parser.feed(html)
    nodes = json.loads("".join(parser.parts))
    if not isinstance(nodes, list):
        raise ValueError("PXGo Nuxt data is not an array")
    record = next(
        (
            node for node in nodes
            if isinstance(node, dict) and {"goodsBarcode", "goodsShowName", "goodsSpec", "details"} <= node.keys()
        ),
        None,
    )
    if record is None:
        raise ValueError("PXGo product record not found")
    return {
        field: dereference_field(nodes, record, field)
        for field in ("goodsBarcode", "goodsShowName", "goodsSpec", "details")
    }


def split_ingredients(text: str) -> list[str]:
    values: list[str] = []
    current: list[str] = []
    depth = 0
    for character in text:
        if character in "（(":
            depth += 1
        elif character in "）)" and depth > 0:
            depth -= 1
        if character in "、，," and depth == 0:
            value = "".join(current).strip()
            if value:
                values.append(value)
            current = []
        else:
            current.append(character)
    value = "".join(current).strip()
    if value:
        values.append(value)
    return values


def parse_number(value: str) -> int | float:
    number = float(value)
    return int(number) if number.is_integer() else number


def nutrition_unit(value: str) -> str | None:
    return {"大卡": "kcal", "公克": "g", "毫克": "mg"}.get(value)


def parse_nutrition(text: str) -> tuple[dict[str, object], dict[str, object]]:
    serving: dict[str, object] = {}
    serving_match = re.search(r"每一份量\s*(\d+(?:\.\d+)?)\s*(毫升|公克)", text)
    if serving_match:
        serving = {
            "amount": parse_number(serving_match.group(1)),
            "unit": {"毫升": "ml", "公克": "g"}[serving_match.group(2)],
        }
    nutrient_names = {
        "熱量": "calories",
        "蛋白質": "protein",
        "脂肪": "fat",
        "飽和脂肪": "saturatedFat",
        "反式脂肪": "transFat",
        "碳水化合物": "carbohydrates",
        "糖": "sugar",
        "鈉": "sodium",
    }
    nutrition: dict[str, object] = {}
    for line in text.splitlines():
        normalized = line.strip()
        for label, key in nutrient_names.items():
            match = re.match(rf"^{label}\s*([0-9.]+)\s*(大卡|公克|毫克)", normalized)
            if match:
                unit = nutrition_unit(match.group(2))
                if unit:
                    nutrition[key] = {"value": parse_number(match.group(1)), "unit": unit}
                break
    return serving, nutrition


def parse_pxgo_details(details: object) -> tuple[dict[str, object], dict[str, object], list[str]]:
    if not isinstance(details, str):
        return {}, {}, []
    parser = DetailTableParser()
    parser.feed(details)
    table = {" ".join(row[0].split()): row[1] for row in parser.rows if len(row) >= 2}
    ingredients_text = next(
        (value for label, value in table.items() if "內容物" in label and "成分" in label),
        "",
    )
    nutrition_text = next(
        (value for label, value in table.items() if "營養標示" in label),
        "",
    )
    serving, nutrition = parse_nutrition(nutrition_text)
    return serving, nutrition, split_ingredients(ingredients_text)


def match_score(target_name: str, matched_name: str) -> float:
    target = normalize_name(target_name)
    matched = normalize_name(matched_name)
    if not target or not matched:
        return 0
    return round(SequenceMatcher(None, target, matched).ratio(), 4)


class PecosCandidateSource:
    source_name = "統一企業集團 PECOS 商品資料"

    def __init__(self, client: HttpClient) -> None:
        self.client = client
        self.product_ids_by_page: dict[str, dict[str, str]] = {}

    def _product_ids(self, page_url: str) -> dict[str, str]:
        if page_url not in self.product_ids_by_page:
            parser = ProductIdParser()
            parser.feed(self.client.get_text(page_url))
            self.product_ids_by_page[page_url] = {
                normalize_name(name): product_id for product_id, name in parser.products
            }
        return self.product_ids_by_page[page_url]

    def collect(self, target: dict[str, str]) -> list[dict[str, object]]:
        product_id = self._product_ids(target["catalog_source"]).get(
            normalize_name(target["product_name"]),
        )
        if not product_id:
            return []
        response = self.client.post_json(
            PECOS_API_URL,
            {"action": "GetProductData", "pdID": product_id},
        )
        if response.get("result") is not True or not isinstance(response.get("html"), str):
            return []
        parser = ProductDetailParser()
        parser.feed(response["html"])
        matched_name = parser.name
        if not matched_name:
            return []
        return [{
            "source_name": self.source_name,
            "source_url": target["catalog_source"],
            "barcode": None,
            "specification": parser.serving_text or None,
            "serving": parse_serving(parser.serving_text),
            "nutrition": {},
            "ingredients": [],
            "matched_name": matched_name,
            "match_score": match_score(target["product_name"], matched_name),
        }]


class PxgoCandidateSource:
    source_name = "全聯小時達"

    def __init__(self, client: HttpClient) -> None:
        self.client = client

    def collect(self, target: dict[str, str]) -> list[dict[str, object]]:
        query = f"{target['brand']}{target['product_name']}"
        search_url = PXGO_SEARCH_URL.format(quote(query, safe=""))
        parser = JsonLdParser()
        parser.feed(self.client.get_text(search_url))
        brand_key = normalize_name(target["brand"])
        product_key = normalize_name(target["product_name"])
        matches: list[tuple[str, str]] = []
        seen_urls: set[str] = set()
        for document in parser.documents:
            if document.get("@type") != "Product":
                continue
            name = str(document.get("name") or "").strip()
            url = str(document.get("url") or "").strip()
            normalized = normalize_name(name)
            if not name or not url or brand_key not in normalized or product_key not in normalized:
                continue
            if url not in seen_urls:
                seen_urls.add(url)
                matches.append((name, url))

        candidates: list[dict[str, object]] = []
        for search_name, product_url in matches:
            record = extract_pxgo_record(self.client.get_text(product_url))
            matched_name = str(record.get("goodsShowName") or search_name).strip()
            specification = str(record.get("goodsSpec") or "").strip() or None
            serving, nutrition, ingredients = parse_pxgo_details(record.get("details"))
            barcode = str(record.get("goodsBarcode") or "").strip() or None
            candidates.append({
                "source_name": self.source_name,
                "source_url": product_url,
                "matched_name": matched_name,
                "barcode": barcode,
                "specification": specification,
                "serving": serving,
                "nutrition": nutrition,
                "ingredients": ingredients,
                "match_score": match_score(query, matched_name),
            })
        return candidates


def atomic_write_json(path: Path, data: dict[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        dir=path.parent,
        prefix=f".{path.name}.",
        suffix=".tmp",
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as output:
            json.dump(data, output, ensure_ascii=False, indent=2)
            output.write("\n")
            output.flush()
            os.fsync(output.fileno())
        os.replace(temporary, path)
    except BaseException:
        temporary.unlink(missing_ok=True)
        raise


def collect_candidates(
    catalog_path: Path,
    output_path: Path,
    limit: int,
    client: HttpClient,
) -> dict[str, object]:
    targets = load_targets(catalog_path, limit)
    sources = [PecosCandidateSource(client), PxgoCandidateSource(client)]
    results: list[dict[str, object]] = []
    errors: list[dict[str, str]] = []
    for index, target in enumerate(targets, start=1):
        print(f"[{index}/{len(targets)}] {target['brand']} - {target['product_name']}")
        candidates: list[dict[str, object]] = []
        for source in sources:
            try:
                candidates.extend(source.collect(target))
            except Exception as error:
                errors.append({
                    "product_name": target["product_name"],
                    "source_name": source.source_name,
                    "error": str(error),
                })
        candidates.sort(key=lambda item: item["match_score"], reverse=True)
        results.append({**target, "candidates": candidates})

    payload: dict[str, object] = {
        "generated_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        "catalog_file": catalog_path.name,
        "tested_product_count": len(results),
        "results": results,
        "errors": errors,
    }
    atomic_write_json(output_path, payload)
    return payload


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--catalog", type=Path, default=DEFAULT_CATALOG)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--limit", type=int, default=5)
    parser.add_argument("--timeout", type=float, default=20.0)
    parser.add_argument("--retries", type=int, default=2)
    parser.add_argument("--delay", type=float, default=0.5)
    args = parser.parse_args()
    if args.limit <= 0:
        parser.error("--limit must be greater than 0")
    if args.timeout <= 0:
        parser.error("--timeout must be greater than 0")
    if args.retries < 0 or args.delay < 0:
        parser.error("--retries and --delay must not be negative")
    return args


def main() -> int:
    args = parse_args()
    client = HttpClient(args.timeout, args.retries, args.delay)
    payload = collect_candidates(
        args.catalog.resolve(),
        args.output.resolve(),
        args.limit,
        client,
    )
    candidate_count = sum(len(item["candidates"]) for item in payload["results"])
    print(f"測試商品數：{len(payload['results'])}")
    print(f"候選總數：{candidate_count}")
    print(f"錯誤數：{len(payload['errors'])}")
    print(f"輸出：{args.output.resolve()}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
