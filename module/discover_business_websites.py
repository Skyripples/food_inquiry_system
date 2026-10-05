#!/usr/bin/env python3
"""Discover unverified website candidates for registered food manufacturers."""

from __future__ import annotations

import argparse
import gzip
import html
import io
import json
import os
import re
import sys
import tempfile
import time
import unicodedata
from datetime import datetime, timezone
from html.parser import HTMLParser
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qs, quote_plus, unquote, urlsplit, urlunsplit
from urllib.request import Request, urlopen


PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_INPUT = PROJECT_ROOT / "data" / "processed" / "food_manufacturers.json.gz"
DEFAULT_OUTPUT = PROJECT_ROOT / "data" / "processed" / "food_business_websites.json.gz"
DEFAULT_CHECKPOINT = PROJECT_ROOT / "data" / "processed" / ".food_business_websites.progress.jsonl"
SEARCH_ENDPOINT = "https://lite.duckduckgo.com/lite/"
SEARCH_SOURCE_NAME = "DuckDuckGo Lite"
BRAVE_SEARCH_ENDPOINT = "https://api.search.brave.com/res/v1/web/search"
BRAVE_SEARCH_SOURCE_NAME = "Brave Search API"
BRAVE_API_KEY_ENV = "BRAVE_SEARCH_API_KEY"
DEFAULT_USER_AGENT = (
    "Mozilla/5.0 (compatible; food-inquiry-system/2.4; "
    "+https://github.com/Skyripples/food_inquiry_system)"
)
URL_FIELD_PATTERN = re.compile(r"(?:網站|網址|官網|website|url|homepage)", re.IGNORECASE)
URL_VALUE_PATTERN = re.compile(r"https?://[^\s<>\"']+", re.IGNORECASE)
EXCLUDED_DOMAINS = frozenset({
    # Social and user-generated platforms.
    "facebook.com", "instagram.com", "youtube.com", "youtu.be", "linkedin.com",
    "threads.net", "tiktok.com", "x.com", "twitter.com", "line.me",
    # Shopping, marketplace, delivery, and reservation platforms.
    "shopee.tw", "momo.com.tw", "pchome.com.tw", "rakuten.com.tw", "ruten.com.tw",
    "books.com.tw", "foodpanda.com.tw", "ubereats.com", "inline.app",
    # Business directories, maps, reviews, and government/open-data mirrors.
    "104.com.tw", "1111.com.tw", "518.com.tw", "findcompany.com.tw", "twincn.com",
    "opengovtw.com", "companys.com.tw", "iyp.com.tw", "yellowpages.com.tw",
    "google.com", "google.com.tw", "maps.google.com", "tripadvisor.com.tw",
    "foursquare.com", "data.gov.tw", "data.fda.gov.tw",
    # Encyclopedias, dictionaries, and broad Q&A/content sites are not business sites.
    "wikipedia.org", "wiktionary.org", "baidu.com", "zhihu.com", "newton.com.tw",
    "messenger.com",
})
ORGANIZATION_SUFFIX_PATTERN = re.compile(
    r"(?:股份有限公司|有限公司|有限合夥)$"
)


class DiscoveryError(RuntimeError):
    pass


class BraveSearchError(DiscoveryError):
    pass


class QueryLimitReached(BraveSearchError):
    pass


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def normalized(value: object) -> str:
    return value.strip() if isinstance(value, str) else ""


def compact_match_text(value: str) -> str:
    normalized_value = unicodedata.normalize("NFKC", unquote(value)).casefold()
    return re.sub(r"[^0-9a-z\u3400-\u9fff]+", "", normalized_value)


def candidate_match_evidence(url: str, title: str, business_name: str, unified_number: str) -> list[str]:
    haystack_title = compact_match_text(title)
    haystack_url = compact_match_text(url)
    full_name = compact_match_text(business_name)
    core_name = compact_match_text(ORGANIZATION_SUFFIX_PATTERN.sub("", business_name))
    evidence: list[str] = []
    if full_name and (full_name in haystack_title or full_name in haystack_url):
        evidence.append("full_name")
    elif len(core_name) >= 3 and (core_name in haystack_title or core_name in haystack_url):
        evidence.append("core_name")
    if unified_number and (unified_number in title or unified_number in unquote(url)):
        evidence.append("unified_number")
    return evidence


def has_distinctive_name_prefix(url: str, title: str, business_name: str) -> bool:
    core_name = compact_match_text(ORGANIZATION_SUFFIX_PATTERN.sub("", business_name))
    if len(core_name) < 4:
        return False
    haystack = compact_match_text(f"{title} {url}")
    return core_name[:2] in haystack


def page_company_name_evidence(page: str, business_name: str) -> list[str]:
    page_text = compact_match_text(page)
    full_name = compact_match_text(business_name)
    core_name = compact_match_text(ORGANIZATION_SUFFIX_PATTERN.sub("", business_name))
    if full_name and full_name in page_text:
        return ["full_name_in_page"]
    if len(core_name) >= 3 and core_name in page_text:
        return ["core_name_in_page"]
    return []


def hostname_is_excluded(hostname: str) -> bool:
    hostname = hostname.lower().removeprefix("www.")
    return any(hostname == domain or hostname.endswith(f".{domain}") for domain in EXCLUDED_DOMAINS)


def normalize_candidate_url(value: str) -> str | None:
    try:
        parsed = urlsplit(html.unescape(value.strip()))
    except ValueError:
        return None
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        return None
    if hostname_is_excluded(parsed.hostname) or parsed.hostname.endswith("duckduckgo.com"):
        return None
    path = parsed.path or "/"
    return urlunsplit((parsed.scheme.lower(), parsed.netloc.lower(), path, parsed.query, ""))


def candidate_url_exclusion_reason(value: str) -> str | None:
    try:
        parsed = urlsplit(html.unescape(value.strip()))
    except ValueError:
        return "invalid_url"
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        return "invalid_url"
    if hostname_is_excluded(parsed.hostname):
        return f"excluded_domain:{parsed.hostname.lower()}"
    if parsed.hostname.endswith("duckduckgo.com"):
        return "unresolved_search_redirect"
    return None


def decode_search_redirect(value: str) -> str:
    parsed = urlsplit(html.unescape(value))
    if not parsed.hostname or not parsed.hostname.endswith("duckduckgo.com"):
        return value
    return unquote(parse_qs(parsed.query).get("uddg", [value])[0])


class SearchResultParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.capture = False
        self.current_url = ""
        self.current_text: list[str] = []
        self.results: list[tuple[str, str]] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        attributes = dict(attrs)
        classes = (attributes.get("class") or "").split()
        if tag.lower() == "a" and "result-link" in classes and attributes.get("href"):
            self.capture = True
            self.current_url = attributes["href"] or ""
            self.current_text = []

    def handle_data(self, data: str) -> None:
        if self.capture:
            self.current_text.append(data)

    def handle_endtag(self, tag: str) -> None:
        tag = tag.lower()
        if tag == "a" and self.capture:
            title = " ".join("".join(self.current_text).split())
            if self.current_url and title:
                self.results.append((self.current_url, title))
            self.capture = False
            self.current_url = ""
            self.current_text = []


class SearchClient:
    def __init__(self, *, timeout: float, retries: int, delay: float, user_agent: str) -> None:
        self.source_name = SEARCH_SOURCE_NAME
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

    def fetch_page(self, url: str) -> str:
        last_error: Exception | None = None
        for attempt in range(self.retries + 1):
            self._wait()
            request = Request(
                url,
                headers={
                    "User-Agent": self.user_agent,
                    "Accept": "text/html,application/xhtml+xml;q=0.9,*/*;q=0.5",
                    "Accept-Language": "zh-TW,zh;q=0.9,en;q=0.5",
                },
            )
            try:
                with urlopen(request, timeout=self.timeout) as response:
                    self.last_request_at = time.monotonic()
                    content_type = response.headers.get_content_type()
                    if content_type not in {"text/html", "application/xhtml+xml", "text/plain"}:
                        raise DiscoveryError(f"unsupported page content type: {content_type}")
                    charset = response.headers.get_content_charset() or "utf-8"
                    return response.read(2 * 1024 * 1024).decode(charset, errors="replace")
            except (HTTPError, URLError, TimeoutError, OSError, UnicodeError, DiscoveryError) as error:
                self.last_request_at = time.monotonic()
                last_error = error
                if isinstance(error, HTTPError) and 400 <= error.code < 500 and error.code != 429:
                    break
                if attempt < self.retries:
                    time.sleep(min(2**attempt, 8))
        raise DiscoveryError(f"candidate page fetch failed: {last_error}")

    def search(
        self,
        query: str,
        max_candidates: int,
        *,
        business_name: str,
        unified_number: str,
    ) -> tuple[str, list[dict[str, object]], list[dict[str, object]]]:
        search_url = f"{SEARCH_ENDPOINT}?q={quote_plus(query)}"
        last_error: Exception | None = None
        for attempt in range(self.retries + 1):
            self._wait()
            request = Request(
                search_url,
                headers={
                    "User-Agent": self.user_agent,
                    "Accept": "text/html,application/xhtml+xml;q=0.9,*/*;q=0.5",
                    "Accept-Language": "zh-TW,zh;q=0.9,en;q=0.5",
                },
            )
            try:
                with urlopen(request, timeout=self.timeout) as response:
                    self.last_request_at = time.monotonic()
                    charset = response.headers.get_content_charset() or "utf-8"
                    page = response.read().decode(charset, errors="replace")
                parser = SearchResultParser()
                parser.feed(page)
                if not parser.results:
                    raise DiscoveryError(
                        f"search source returned no parseable results (HTTP {getattr(response, 'status', 'unknown')}); "
                        "possible rate limiting or challenge page"
                    )
                candidates, diagnostics = evaluate_search_results(
                    self,
                    parser.results,
                    search_url=search_url,
                    source_name=self.source_name,
                    max_candidates=max_candidates,
                    business_name=business_name,
                    unified_number=unified_number,
                    decode_redirects=True,
                )
                return search_url, candidates, diagnostics
            except (HTTPError, URLError, TimeoutError, OSError, UnicodeError) as error:
                self.last_request_at = time.monotonic()
                last_error = error
                if isinstance(error, HTTPError) and 400 <= error.code < 500 and error.code != 429:
                    break
                if attempt < self.retries:
                    time.sleep(min(2**attempt, 8))
        raise DiscoveryError(f"search failed after {self.retries + 1} attempt(s): {last_error}")


def evaluate_search_results(
    client: SearchClient,
    results: list[tuple[str, str] | tuple[str, str, str]],
    *,
    search_url: str,
    source_name: str,
    max_candidates: int,
    business_name: str,
    unified_number: str,
    decode_redirects: bool,
) -> tuple[list[dict[str, object]], list[dict[str, object]]]:
    candidates: list[dict[str, object]] = []
    diagnostics: list[dict[str, object]] = []
    seen: set[str] = set()
    for result in results:
        raw_url, title = result[0], result[1]
        description = result[2] if len(result) > 2 else ""
        decoded_url = decode_search_redirect(raw_url) if decode_redirects else raw_url
        url = normalize_candidate_url(decoded_url)
        diagnostic: dict[str, object] = {
            "raw_url": raw_url,
            "decoded_url": decoded_url,
            "url": url,
            "title": title,
            "description": description or None,
        }
        reason = candidate_url_exclusion_reason(decoded_url)
        if reason:
            diagnostic.update({"decision": "excluded", "reason": reason})
            diagnostics.append(diagnostic)
            continue
        if not url:
            diagnostic.update({"decision": "excluded", "reason": "invalid_url"})
            diagnostics.append(diagnostic)
            continue
        if url in seen:
            diagnostic.update({"decision": "excluded", "reason": "duplicate_url"})
            diagnostics.append(diagnostic)
            continue
        searchable_text = f"{title} {description}".strip()
        evidence = candidate_match_evidence(url, searchable_text, business_name, unified_number)
        if not evidence and has_distinctive_name_prefix(url, searchable_text, business_name):
            try:
                evidence = page_company_name_evidence(client.fetch_page(url), business_name)
            except DiscoveryError as page_error:
                diagnostic.update({
                    "decision": "excluded",
                    "reason": "candidate_page_fetch_failed",
                    "detail": str(page_error),
                })
                diagnostics.append(diagnostic)
                continue
            if not evidence:
                diagnostic.update({
                    "decision": "excluded",
                    "reason": "candidate_page_missing_company_name",
                })
                diagnostics.append(diagnostic)
                continue
        if not evidence:
            diagnostic.update({"decision": "excluded", "reason": "insufficient_business_match"})
            diagnostics.append(diagnostic)
            continue
        seen.add(url)
        diagnostic.update({
            "decision": "accepted",
            "reason": "business_name_or_number_match",
            "match_evidence": evidence,
        })
        diagnostics.append(diagnostic)
        candidates.append({
            "url": url,
            "title": title,
            "description": description or None,
            "source": {"name": source_name, "url": search_url},
            "discovery_method": "search",
            "match_evidence": evidence,
            "verification_status": "unverified_candidate",
        })
        if len(candidates) >= max_candidates:
            break
    return candidates, diagnostics


class BraveSearchClient(SearchClient):
    def __init__(
        self,
        *,
        api_key: str,
        max_queries: int,
        timeout: float,
        retries: int,
        delay: float,
        user_agent: str,
        opener=urlopen,
    ) -> None:
        super().__init__(timeout=timeout, retries=retries, delay=delay, user_agent=user_agent)
        if not api_key.strip():
            raise BraveSearchError(f"missing API key in environment variable {BRAVE_API_KEY_ENV}")
        if max_queries <= 0:
            raise BraveSearchError("--max-api-queries must be greater than 0")
        self.source_name = BRAVE_SEARCH_SOURCE_NAME
        self.api_key = api_key.strip()
        self.max_queries = max_queries
        self.queries_used = 0
        self.opener = opener

    def search(
        self,
        query: str,
        max_candidates: int,
        *,
        business_name: str,
        unified_number: str,
    ) -> tuple[str, list[dict[str, object]], list[dict[str, object]]]:
        params = f"q={quote_plus(query)}&count={min(20, max(1, max_candidates * 3))}&country=TW&search_lang=zh-hant&ui_lang=zh-TW"
        search_url = f"{BRAVE_SEARCH_ENDPOINT}?{params}"
        last_error: Exception | None = None
        for attempt in range(self.retries + 1):
            if self.queries_used >= self.max_queries:
                raise QueryLimitReached(
                    f"Brave API query limit reached ({self.queries_used}/{self.max_queries})"
                )
            self._wait()
            request = Request(
                search_url,
                headers={
                    "Accept": "application/json",
                    "X-Subscription-Token": self.api_key,
                    "User-Agent": self.user_agent,
                },
            )
            self.queries_used += 1
            try:
                with self.opener(request, timeout=self.timeout) as response:
                    self.last_request_at = time.monotonic()
                    payload = json.loads(response.read().decode("utf-8"))
                web = payload.get("web") if isinstance(payload, dict) else None
                raw_results = web.get("results") if isinstance(web, dict) else None
                if not isinstance(raw_results, list):
                    raise BraveSearchError("Brave API response is missing web.results")
                parsed_results = [
                    (
                        str(item.get("url", "")),
                        str(item.get("title", "")),
                        str(item.get("description", "")),
                    )
                    for item in raw_results if isinstance(item, dict)
                ]
                candidates, diagnostics = evaluate_search_results(
                    self,
                    parsed_results,
                    search_url=search_url,
                    source_name=self.source_name,
                    max_candidates=max_candidates,
                    business_name=business_name,
                    unified_number=unified_number,
                    decode_redirects=False,
                )
                return search_url, candidates, diagnostics
            except (HTTPError, URLError, TimeoutError, OSError, UnicodeError, json.JSONDecodeError) as error:
                self.last_request_at = time.monotonic()
                last_error = error
                if isinstance(error, HTTPError) and 400 <= error.code < 500 and error.code != 429:
                    break
                if attempt < self.retries:
                    time.sleep(min(2**attempt, 8))
            except BraveSearchError:
                raise
        raise BraveSearchError(
            f"Brave API failed after {attempt + 1} request(s); "
            f"queries used {self.queries_used}/{self.max_queries}: {last_error}"
        )


def load_manufacturers(path: Path) -> tuple[dict[str, object], list[dict[str, object]]]:
    with gzip.open(path, "rt", encoding="utf-8") as source:
        document = json.load(source)
    manufacturers = document.get("manufacturers") if isinstance(document, dict) else None
    if not isinstance(manufacturers, list):
        raise DiscoveryError("input manufacturers is not an array")
    if not all(isinstance(item, dict) and isinstance(item.get("records"), list) for item in manufacturers):
        raise DiscoveryError("input contains an invalid manufacturer")
    return document, manufacturers


def registry_url_candidates(manufacturer: dict[str, object]) -> list[dict[str, object]]:
    candidates: list[dict[str, object]] = []
    seen: set[str] = set()
    for record in manufacturer.get("records", []):
        if not isinstance(record, dict):
            continue
        for field, value in record.items():
            if not URL_FIELD_PATTERN.search(str(field)) or not isinstance(value, str):
                continue
            for match in URL_VALUE_PATTERN.findall(value):
                url = normalize_candidate_url(match.rstrip(".,;，。；)）"))
                if not url or url in seen:
                    continue
                seen.add(url)
                candidates.append({
                    "url": url,
                    "title": None,
                    "source": {
                        "name": "食品業者登錄原始資料",
                        "field": str(field),
                    },
                    "discovery_method": "registry_field",
                    "verification_status": "unverified_candidate",
                })
    return candidates


def manufacturer_name(manufacturer: dict[str, object]) -> str:
    for record in manufacturer.get("records", []):
        if isinstance(record, dict):
            name = normalized(record.get("公司或商業登記名稱"))
            if name:
                return name
    return ""


def manufacturer_identity(manufacturer: dict[str, object], index: int) -> str:
    unified_number = normalized(manufacturer.get("unified_number"))
    if unified_number:
        return f"unified_number:{unified_number}"
    records = manufacturer.get("records", [])
    registration_number = ""
    if records and isinstance(records[0], dict):
        registration_number = normalized(records[0].get("食品業者登錄字號"))
    return f"missing:{index}:{registration_number}"


def input_signature(path: Path) -> dict[str, object]:
    stat = path.stat()
    return {"path": str(path.resolve()), "size": stat.st_size, "mtime_ns": stat.st_mtime_ns}


def load_checkpoint(path: Path, signature: dict[str, object]) -> list[dict[str, object]]:
    if not path.exists():
        return []
    entries: list[dict[str, object]] = []
    with path.open("r", encoding="utf-8") as source:
        first = source.readline()
        if not first:
            return []
        metadata = json.loads(first)
        if metadata.get("type") != "metadata" or metadata.get("input_signature") != signature:
            raise DiscoveryError("checkpoint belongs to a different input file; remove it or choose another --checkpoint")
        for line_number, line in enumerate(source, start=2):
            if not line.strip():
                continue
            try:
                entry = json.loads(line)
            except json.JSONDecodeError as error:
                raise DiscoveryError(f"checkpoint line {line_number} is invalid") from error
            if not isinstance(entry, dict) or entry.get("index") != len(entries):
                raise DiscoveryError(f"checkpoint line {line_number} is out of sequence")
            entries.append(entry)
    return entries


def initialize_checkpoint(path: Path, signature: dict[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="\n") as output:
        output.write(json.dumps({
            "type": "metadata",
            "input_signature": signature,
            "created_at": utc_now(),
        }, ensure_ascii=False, separators=(",", ":")) + "\n")
        output.flush()
        os.fsync(output.fileno())


def append_checkpoint(path: Path, entry: dict[str, object]) -> None:
    with path.open("a", encoding="utf-8", newline="\n") as output:
        output.write(json.dumps(entry, ensure_ascii=False, separators=(",", ":")) + "\n")
        output.flush()
        os.fsync(output.fileno())


def discover_one(
    manufacturer: dict[str, object],
    index: int,
    client: SearchClient,
    max_candidates: int,
) -> dict[str, object]:
    name = manufacturer_name(manufacturer)
    unified_number = normalized(manufacturer.get("unified_number"))
    candidates = registry_url_candidates(manufacturer)
    query: str | None = None
    search_url: str | None = None
    diagnostics: list[dict[str, object]] = []
    error: str | None = None
    if not candidates:
        query = f"{name} 官方網站" if name else unified_number
        if query:
            try:
                search_url, candidates, diagnostics = client.search(
                    query,
                    max_candidates,
                    business_name=name,
                    unified_number=unified_number,
                )
            except BraveSearchError:
                # Paid API failures stop before the current business is added to
                # the checkpoint. Previously completed businesses remain resumable.
                raise
            except DiscoveryError as caught:
                error = str(caught)

    if candidates:
        status = "candidates_found"
    elif error:
        status = "search_error"
    else:
        status = "not_found"
    return {
        "identity": manufacturer_identity(manufacturer, index),
        "unified_number": manufacturer.get("unified_number"),
        "unified_number_status": manufacturer.get("unified_number_status"),
        "records": manufacturer.get("records", []),
        "website_candidates": candidates,
        "search": {
            "query": query,
            "source_name": client.source_name if query else None,
            "source_url": search_url,
            "status": status,
            "error": error,
            "result_diagnostics": diagnostics,
        },
    }


def validate_output(document: object) -> None:
    if not isinstance(document, dict) or not isinstance(document.get("businesses"), list):
        raise DiscoveryError("output root or businesses is invalid")
    stats = document.get("stats")
    if not isinstance(stats, dict) or stats.get("tested_business_count") != len(document["businesses"]):
        raise DiscoveryError("output stats is invalid")
    candidate_count = 0
    found_count = 0
    for business in document["businesses"]:
        if not isinstance(business, dict) or not isinstance(business.get("records"), list):
            raise DiscoveryError("output contains an invalid business")
        candidates = business.get("website_candidates")
        if not isinstance(candidates, list):
            raise DiscoveryError("output candidates is invalid")
        if candidates:
            found_count += 1
        for candidate in candidates:
            if (
                not isinstance(candidate, dict)
                or normalize_candidate_url(str(candidate.get("url", ""))) is None
                or candidate.get("verification_status") != "unverified_candidate"
            ):
                raise DiscoveryError("output contains an invalid candidate URL")
        candidate_count += len(candidates)
    if stats.get("businesses_with_candidates") != found_count or stats.get("candidate_url_count") != candidate_count:
        raise DiscoveryError("output candidate statistics do not match")


def atomic_write_gzip_json(path: Path, document: dict[str, object]) -> tuple[int, int]:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, name = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.", suffix=".tmp")
    os.close(descriptor)
    temporary = Path(name)
    encoded = json.dumps(document, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    try:
        with temporary.open("w+b") as raw:
            with gzip.GzipFile(fileobj=raw, mode="wb", compresslevel=9, mtime=0) as compressed:
                compressed.write(encoded)
            raw.flush()
            os.fsync(raw.fileno())
        with gzip.open(temporary, "rt", encoding="utf-8") as source:
            verified = json.load(source)
        validate_output(verified)
        compressed_size = temporary.stat().st_size
        os.replace(temporary, path)
        return len(encoded), compressed_size
    except BaseException:
        temporary.unlink(missing_ok=True)
        raise


def run(args: argparse.Namespace) -> dict[str, object]:
    input_path = args.input.resolve()
    output_path = args.output.resolve()
    checkpoint_path = args.checkpoint.resolve()
    if not input_path.is_file():
        raise DiscoveryError(f"input file does not exist: {input_path}")
    input_document, manufacturers = load_manufacturers(input_path)
    selected = manufacturers[:args.limit] if args.limit is not None else manufacturers
    signature = {
        **input_signature(input_path),
        "search_provider": args.search_provider,
        "max_candidates": args.max_candidates,
        "selected_business_count": len(selected),
    }
    resumed = load_checkpoint(checkpoint_path, signature)
    if len(resumed) > len(selected):
        raise DiscoveryError("checkpoint contains more businesses than the selected run")
    for index, entry in enumerate(resumed):
        expected_identity = manufacturer_identity(selected[index], index)
        if entry.get("identity") != expected_identity:
            raise DiscoveryError(f"checkpoint business {index} does not match the current input order")
    if args.search_provider == "brave":
        api_key = os.environ.get(BRAVE_API_KEY_ENV, "")
        client: SearchClient = BraveSearchClient(
            api_key=api_key,
            max_queries=args.max_api_queries,
            timeout=args.timeout,
            retries=args.retries,
            delay=args.delay,
            user_agent=args.user_agent,
        )
    else:
        client = SearchClient(
            timeout=args.timeout,
            retries=args.retries,
            delay=args.delay,
            user_agent=args.user_agent,
        )
    if not checkpoint_path.exists():
        initialize_checkpoint(checkpoint_path, signature)

    businesses = [entry["result"] for entry in resumed]
    for index in range(len(businesses), len(selected)):
        manufacturer = selected[index]
        expected_identity = manufacturer_identity(manufacturer, index)
        result = discover_one(manufacturer, index, client, args.max_candidates)
        append_checkpoint(checkpoint_path, {
            "index": index,
            "identity": expected_identity,
            "result": result,
        })
        businesses.append(result)
        print(
            f"[{index + 1}/{len(selected)}] {manufacturer_name(manufacturer)}："
            f"{len(result['website_candidates'])} 個候選"
        )

    found_count = sum(bool(item["website_candidates"]) for item in businesses)
    candidate_count = sum(len(item["website_candidates"]) for item in businesses)
    error_count = sum(item["search"]["status"] == "search_error" for item in businesses)
    document: dict[str, object] = {
        "source": {
            "name": "食品製造業者官方網站候選蒐集",
            "input": str(input_path),
            "input_source": input_document.get("source"),
            "search_source": client.source_name,
            "generated_at": utc_now(),
            "candidate_notice": "候選網址未自動認定為官方網站，需人工確認。",
        },
        "businesses": businesses,
        "stats": {
            "tested_business_count": len(businesses),
            "businesses_with_candidates": found_count,
            "candidate_url_count": candidate_count,
            "businesses_without_candidates": len(businesses) - found_count,
            "search_error_count": error_count,
            "is_limited_run": args.limit is not None,
            "api_queries_used": getattr(client, "queries_used", 0),
            "api_query_limit": args.max_api_queries if args.search_provider == "brave" else None,
        },
    }
    validate_output(document)
    uncompressed_size, compressed_size = atomic_write_gzip_json(output_path, document)
    checkpoint_path.unlink(missing_ok=True)
    result_stats = {
        **document["stats"],
        "uncompressed_json_size": uncompressed_size,
        "compressed_json_size": compressed_size,
        "output": str(output_path),
    }
    print(json.dumps(result_stats, ensure_ascii=False, indent=2))
    return result_stats


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", type=Path, default=DEFAULT_INPUT)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--checkpoint", type=Path, default=DEFAULT_CHECKPOINT)
    parser.add_argument("--limit", type=int)
    parser.add_argument("--max-candidates", type=int, default=5)
    parser.add_argument("--timeout", type=float, default=25.0)
    parser.add_argument("--retries", type=int, default=2)
    parser.add_argument("--delay", type=float, default=2.0)
    parser.add_argument("--user-agent", default=DEFAULT_USER_AGENT)
    parser.add_argument(
        "--search-provider",
        choices=("duckduckgo", "brave"),
        default="duckduckgo",
    )
    parser.add_argument("--max-api-queries", type=int, default=100)
    args = parser.parse_args()
    if args.limit is not None and args.limit <= 0:
        parser.error("--limit must be greater than 0")
    if args.max_candidates <= 0:
        parser.error("--max-candidates must be greater than 0")
    if args.timeout <= 0 or args.retries < 0 or args.delay < 0:
        parser.error("timeout/delay must be non-negative and timeout must be positive")
    if args.max_api_queries <= 0:
        parser.error("--max-api-queries must be greater than 0")
    return args


def main() -> int:
    args = parse_args()
    try:
        run(args)
    except Exception as error:
        print(f"食品業者網站候選蒐集失敗：{error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
