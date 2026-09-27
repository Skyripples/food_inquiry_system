#!/usr/bin/env python3
"""Collect the public PECOS brand and product catalog into compressed JSON."""

from __future__ import annotations

import argparse
import gzip
import json
import os
import re
import sys
import tempfile
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from html.parser import HTMLParser
from pathlib import Path
from typing import Iterator
from urllib.error import HTTPError, URLError
from urllib.parse import quote, unquote, urljoin, urlsplit, urlunsplit
from urllib.request import Request, urlopen


BASE_URL = "https://www.pecos.com.tw/"
BRANDS_URL = urljoin(BASE_URL, "brands.html")
SITEMAP_URL = urljoin(BASE_URL, "sitemap.html")
DEFAULT_OUTPUT = Path(__file__).resolve().parents[1] / "data" / "raw" / "pecos_catalog.json.gz"
DEFAULT_USER_AGENT = (
    "food-inquiry-system PECOS catalog collector/2.1 "
    "(+https://github.com/Skyripples/food_inquiry_system)"
)
VOID_TAGS = {
    "area", "base", "br", "col", "embed", "hr", "img", "input", "link",
    "meta", "param", "source", "track", "wbr",
}


@dataclass
class Node:
    tag: str
    attrs: dict[str, str]
    parent: Node | None = None
    children: list[Node | str] = field(default_factory=list)

    @property
    def classes(self) -> set[str]:
        return set(self.attrs.get("class", "").split())

    def descendants(self, *, include_self: bool = False) -> Iterator[Node]:
        if include_self:
            yield self
        for child in self.children:
            if isinstance(child, Node):
                yield child
                yield from child.descendants()


class DocumentParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.root = Node("document", {})
        self.stack = [self.root]

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        node = Node(tag.lower(), {key.lower(): value or "" for key, value in attrs}, self.stack[-1])
        self.stack[-1].children.append(node)
        if tag.lower() not in VOID_TAGS:
            self.stack.append(node)

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self.handle_starttag(tag, attrs)
        if tag.lower() not in VOID_TAGS:
            self.handle_endtag(tag)

    def handle_endtag(self, tag: str) -> None:
        tag = tag.lower()
        for index in range(len(self.stack) - 1, 0, -1):
            if self.stack[index].tag == tag:
                del self.stack[index:]
                return

    def handle_data(self, data: str) -> None:
        self.stack[-1].children.append(data)


def parse_document(html: str) -> Node:
    parser = DocumentParser()
    parser.feed(html)
    parser.close()
    return parser.root


def normalize_text(value: str) -> str:
    return " ".join(value.split())


def node_text(node: Node) -> str:
    parts: list[str] = []

    def collect(current: Node) -> None:
        for child in current.children:
            if isinstance(child, str):
                parts.append(child)
            elif child.tag not in {"script", "style"}:
                collect(child)

    collect(node)
    return normalize_text(" ".join(parts))


def first_descendant(node: Node, *, tag: str | None = None, class_name: str | None = None) -> Node | None:
    for candidate in node.descendants():
        if tag is not None and candidate.tag != tag:
            continue
        if class_name is not None and class_name not in candidate.classes:
            continue
        return candidate
    return None


def absolute_url(url: str, base_url: str = BASE_URL) -> str:
    joined = urljoin(base_url, url.strip())
    parts = urlsplit(joined)
    path = quote(parts.path, safe="/%:@!$&'()*+,;=-._~")
    query = quote(parts.query, safe="=&?/%:@!$'()*+,;~-._")
    return urlunsplit((parts.scheme.lower(), parts.netloc.lower(), path, query, ""))


def is_http_url(url: str) -> bool:
    return urlsplit(url).scheme in {"http", "https"}


class HttpClient:
    def __init__(self, *, timeout: float, retries: int, delay: float, user_agent: str) -> None:
        self.timeout = timeout
        self.retries = retries
        self.delay = delay
        self.user_agent = user_agent
        self.last_request_at: float | None = None

    def _wait(self) -> None:
        if self.last_request_at is None:
            return
        remaining = self.delay - (time.monotonic() - self.last_request_at)
        if remaining > 0:
            time.sleep(remaining)

    def get_text(self, url: str) -> str:
        request_url = absolute_url(url)
        last_error: Exception | None = None
        for attempt in range(self.retries + 1):
            self._wait()
            request = Request(
                request_url,
                headers={
                    "User-Agent": self.user_agent,
                    "Accept": "text/html,application/xhtml+xml;q=0.9,*/*;q=0.8",
                    "Accept-Language": "zh-TW,zh;q=0.9,en;q=0.5",
                },
            )
            try:
                with urlopen(request, timeout=self.timeout) as response:
                    self.last_request_at = time.monotonic()
                    charset = response.headers.get_content_charset() or "utf-8"
                    return response.read().decode(charset, errors="replace")
            except (HTTPError, URLError, TimeoutError, OSError) as error:
                self.last_request_at = time.monotonic()
                last_error = error
                if isinstance(error, HTTPError) and 400 <= error.code < 500 and error.code != 429:
                    break
                if attempt < self.retries:
                    time.sleep(min(2 ** attempt, 8))
        raise RuntimeError(f"GET {request_url} failed after {self.retries + 1} attempt(s): {last_error}")


def discover_categories(root: Node) -> dict[str, str]:
    categories: dict[str, str] = {}
    for anchor in root.descendants():
        relation = anchor.attrs.get("data-rel", "")
        if anchor.tag == "a" and relation.startswith("cate-"):
            name = node_text(anchor)
            if name:
                categories.setdefault(relation, name)
    return categories


def brand_name_from_item(item: Node, anchor: Node) -> str:
    image = first_descendant(anchor, tag="img")
    return normalize_text((image.attrs.get("alt", "") if image else "") or node_text(anchor))


def discover_from_brand_list(root: Node) -> list[dict[str, str]]:
    categories = discover_categories(root)
    discovered: list[dict[str, str]] = []
    for item in root.descendants():
        if item.tag != "div" or "item" not in item.classes:
            continue
        category_id = next((name for name in item.classes if name.startswith("cate-")), "")
        anchor = first_descendant(item, tag="a")
        href = anchor.attrs.get("href", "") if anchor else ""
        if not re.match(r"^/?brands-(?!\d+\.html)[^?#]+\.html$", href, re.IGNORECASE):
            continue
        name = brand_name_from_item(item, anchor)
        if name:
            discovered.append({
                "name": name,
                "url": absolute_url(href),
                "category": categories.get(category_id, ""),
            })
    return discovered


def discover_from_sitemap(root: Node) -> list[dict[str, str]]:
    discovered: list[dict[str, str]] = []
    for anchor in root.descendants():
        if anchor.tag != "a":
            continue
        href = anchor.attrs.get("href", "")
        if not re.match(r"^/?brands-(?!\d+\.html)[^?#]+\.html$", href, re.IGNORECASE):
            continue
        name = node_text(anchor)
        if name:
            discovered.append({"name": name, "url": absolute_url(href), "category": ""})
    return discovered


def discover_brands(brand_html: str, sitemap_html: str) -> list[dict[str, str]]:
    combined = discover_from_brand_list(parse_document(brand_html))
    combined.extend(discover_from_sitemap(parse_document(sitemap_html)))
    unique: list[dict[str, str]] = []
    by_url: dict[str, dict[str, str]] = {}
    for brand in combined:
        # The two PECOS indexes sometimes express the same path as Unicode,
        # percent-encoding, or safe punctuation (for example ' versus %27).
        canonical_key = unquote(brand["url"]).casefold()
        existing = by_url.get(canonical_key)
        if existing:
            if not existing["category"] and brand["category"]:
                existing["category"] = brand["category"]
            continue
        copy = dict(brand)
        by_url[canonical_key] = copy
        unique.append(copy)
    return unique


def extract_description(introduction: Node) -> str:
    body = first_descendant(introduction, tag="div", class_name="bd")
    if body is None:
        return ""
    parts: list[str] = []
    for child in body.children:
        if isinstance(child, Node) and "box" in child.classes:
            break
        if isinstance(child, str):
            parts.append(child)
        elif child.tag not in {"script", "style"}:
            parts.append(node_text(child))
    return normalize_text(" ".join(parts))


def extract_external_links(introduction: Node, page_url: str) -> list[dict[str, str]]:
    link_box = first_descendant(introduction, tag="div", class_name="qlink")
    if link_box is None:
        return []
    links: list[dict[str, str]] = []
    seen: set[str] = set()
    for anchor in link_box.descendants():
        if anchor.tag != "a" or not anchor.attrs.get("href"):
            continue
        url = absolute_url(anchor.attrs["href"], page_url)
        if not is_http_url(url) or url in seen:
            continue
        seen.add(url)
        links.append({"name": node_text(anchor), "url": url})
    return links


def extract_products(introduction: Node, page_url: str) -> list[dict[str, str]]:
    products: list[dict[str, str]] = []
    seen: set[str] = set()
    for box in introduction.descendants():
        if box.tag != "div" or "product-box" not in box.classes:
            continue
        named = next(
            (node for node in box.descendants() if node.attrs.get("data-name")),
            None,
        )
        name = normalize_text(named.attrs.get("data-name", "") if named else "")
        if not name:
            description = first_descendant(box, tag="div", class_name="description")
            name = node_text(description) if description else ""
        key = name.casefold()
        if not name or key in seen:
            continue
        seen.add(key)
        image = first_descendant(box, tag="img")
        image_url = ""
        if image and image.attrs.get("src"):
            candidate = absolute_url(image.attrs["src"], page_url)
            if is_http_url(candidate):
                image_url = candidate
        products.append({"name": name, "image_url": image_url})
    return products


def parse_brand_page(html: str, discovered: dict[str, str]) -> dict[str, object]:
    root = parse_document(html)
    introduction = first_descendant(root, tag="div", class_name="product-introduction")
    if introduction is None:
        raise ValueError("brand introduction section not found")
    heading = first_descendant(introduction, tag="h2")
    name = node_text(heading) if heading else discovered["name"]
    if not name:
        raise ValueError("brand name not found")
    return {
        "name": name,
        "url": discovered["url"],
        "category": discovered["category"],
        "description": extract_description(introduction),
        "products": extract_products(introduction, discovered["url"]),
        "external_links": extract_external_links(introduction, discovered["url"]),
    }


def name_key(name: str) -> str:
    without_markup = re.sub(r"<[^>]+>", "", name)
    return re.sub(r"\s+", "", without_markup).casefold()


def merge_duplicate_brands(brands: list[dict[str, object]]) -> list[dict[str, object]]:
    """Merge PECOS category pages that represent the same displayed brand."""
    merged: list[dict[str, object]] = []
    by_name: dict[str, dict[str, object]] = {}
    product_keys: dict[str, set[str]] = {}
    link_urls: dict[str, set[str]] = {}

    for brand in brands:
        key = name_key(str(brand["name"]))
        existing = by_name.get(key)
        if existing is None:
            copy = {
                **brand,
                "products": [dict(product) for product in brand["products"]],
                "external_links": [dict(link) for link in brand["external_links"]],
            }
            by_name[key] = copy
            product_keys[key] = {name_key(str(product["name"])) for product in copy["products"]}
            link_urls[key] = {str(link["url"]) for link in copy["external_links"]}
            merged.append(copy)
            continue

        if not existing["description"] and brand["description"]:
            existing["description"] = brand["description"]
        for product in brand["products"]:
            item_key = name_key(str(product["name"]))
            if item_key and item_key not in product_keys[key]:
                product_keys[key].add(item_key)
                existing["products"].append(dict(product))
        for link in brand["external_links"]:
            url = str(link["url"])
            if url not in link_urls[key]:
                link_urls[key].add(url)
                existing["external_links"].append(dict(link))

    return merged


def atomic_write_gzip_json(output: Path, data: dict[str, object]) -> tuple[int, int]:
    output.parent.mkdir(parents=True, exist_ok=True)
    encoded = json.dumps(data, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    descriptor, temporary_name = tempfile.mkstemp(
        dir=output.parent,
        prefix=f".{output.name}.",
        suffix=".tmp",
    )
    os.close(descriptor)
    temporary = Path(temporary_name)
    try:
        with gzip.open(temporary, "wb", compresslevel=9) as compressed:
            compressed.write(encoded)
        # Windows requires a writable descriptor for fsync.
        with temporary.open("r+b") as handle:
            os.fsync(handle.fileno())
        compressed_size = temporary.stat().st_size
        os.replace(temporary, output)
        return len(encoded), compressed_size
    except BaseException:
        temporary.unlink(missing_ok=True)
        raise


def scrape_catalog(client: HttpClient, output: Path) -> dict[str, object]:
    print(f"讀取品牌列表：{BRANDS_URL}")
    brand_html = client.get_text(BRANDS_URL)
    print(f"讀取網站地圖：{SITEMAP_URL}")
    sitemap_html = client.get_text(SITEMAP_URL)
    discovered = discover_brands(brand_html, sitemap_html)
    print(f"發現 {len(discovered)} 個不重複品牌頁")

    brands: list[dict[str, object]] = []
    errors: list[dict[str, str]] = []
    for index, item in enumerate(discovered, start=1):
        print(f"[{index}/{len(discovered)}] {item['name']}")
        try:
            page = client.get_text(item["url"])
            brands.append(parse_brand_page(page, item))
        except Exception as error:  # A single brand must not abort the complete run.
            message = str(error)
            errors.append({"name": item["name"], "url": item["url"], "error": message})
            print(f"  失敗：{message}", file=sys.stderr)

    fetched_brand_page_count = len(brands)
    brands = merge_duplicate_brands(brands)
    product_count = sum(len(brand["products"]) for brand in brands)
    catalog: dict[str, object] = {
        "source": {
            "name": "統一企業集團 PECOS",
            "url": BASE_URL,
            "fetched_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        },
        "brands": brands,
        "errors": errors,
        "stats": {
            "brand_count": len(brands),
            "product_count": product_count,
            "successful_brand_count": len(brands),
            "failed_brand_count": len(errors),
            "fetched_brand_page_count": fetched_brand_page_count,
        },
    }
    uncompressed_size, compressed_size = atomic_write_gzip_json(output, catalog)
    print("完成")
    print(f"品牌數：{len(brands)}")
    print(f"系列產品總數：{product_count}")
    print(f"成功品牌數：{len(brands)}")
    print(f"失敗品牌數：{len(errors)}")
    print(f"壓縮前大小：{uncompressed_size} bytes")
    print(f"壓縮後大小：{compressed_size} bytes")
    print(f"輸出：{output}")
    return catalog


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--timeout", type=float, default=20.0)
    parser.add_argument("--retries", type=int, default=2)
    parser.add_argument("--delay", type=float, default=0.5)
    parser.add_argument("--user-agent", default=DEFAULT_USER_AGENT)
    args = parser.parse_args()
    if args.timeout <= 0:
        parser.error("--timeout must be greater than 0")
    if args.retries < 0:
        parser.error("--retries must be 0 or greater")
    if args.delay < 0:
        parser.error("--delay must be 0 or greater")
    return args


def main() -> int:
    args = parse_args()
    client = HttpClient(
        timeout=args.timeout,
        retries=args.retries,
        delay=args.delay,
        user_agent=args.user_agent,
    )
    try:
        scrape_catalog(client, args.output.resolve())
    except Exception as error:
        print(f"PECOS 型錄蒐集失敗：{error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
