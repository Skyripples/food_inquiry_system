#!/usr/bin/env python3
"""Collect TFDA food traceability data into an atomic gzip JSON archive."""

from __future__ import annotations

import argparse
import codecs
import gzip
import json
import os
import re
import sys
import tempfile
import time
import zipfile
from collections.abc import Iterator
from datetime import datetime, timezone
from pathlib import Path
from typing import TextIO
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen


DATASET_URL = "https://data.gov.tw/dataset/33575"
DOWNLOAD_URL = "https://data.fda.gov.tw/data/opendata/export/188/json"
DEFAULT_OUTPUT = (
    Path(__file__).resolve().parents[1]
    / "data"
    / "raw"
    / "tfda_traceability.json.gz"
)
DEFAULT_USER_AGENT = (
    "food-inquiry-system TFDA traceability collector/2.5 "
    "(+https://github.com/Skyripples/food_inquiry_system)"
)

FIELD_COMPANY = "\u516c\u53f8\u540d\u7a31"
FIELD_PRODUCT = "\u7522\u54c1\u540d\u7a31"
FIELD_PACKAGE = "\u5305\u88dd\u898f\u683c"
FIELD_TRACEABILITY_CODE = "\u7522\u54c1\u8ffd\u6eaf\u7cfb\u7d71\u4e32\u63a5\u78bc"
FIELD_SERVING = "\u6bcf\u4e00\u4efd\u91cf"
FIELD_CONTENT_LABEL = "\u5167\u5bb9\u7269\u6a19\u793a"

NUTRITION_FIELDS = {
    "calories": "\u6bcf\u4efd\u71b1\u91cf",
    "protein": "\u6bcf\u4efd\u86cb\u767d\u8cea",
    "fat": "\u6bcf\u4efd\u8102\u80aa",
    "saturatedFat": "\u6bcf\u4efd\u98fd\u548c\u8102\u80aa",
    "transFat": "\u6bcf\u4efd\u53cd\u5f0f\u8102\u80aa",
    "carbohydrates": "\u6bcf\u4efd\u78b3\u6c34\u5316\u5408\u7269",
    "sugar": "\u6bcf\u4efd\u7cd6",
    "sodium": "\u6bcf\u4efd\u9209",
}
REQUIRED_SOURCE_FIELDS = (
    FIELD_COMPANY,
    FIELD_PRODUCT,
    FIELD_PACKAGE,
    FIELD_TRACEABILITY_CODE,
    FIELD_SERVING,
    FIELD_CONTENT_LABEL,
    *NUTRITION_FIELDS.values(),
)

NUMBER = r"(?:0|[1-9]\d*)(?:\.\d+)?"
QUANTITY_PATTERN = re.compile(
    rf"^\s*(?P<value>{NUMBER})\s*(?P<unit>\u516c\u514b|\u514b|g|\u6beb\u5347|ml)\s*$",
    re.IGNORECASE,
)
PACKAGE_ZERO_PATTERN = re.compile(
    r"^\s*0(?:\.0+)?\s*(?:\u516c\u514b|\u514b|g|\u6beb\u5347|ml)(?:\s*/|\s*$)",
    re.IGNORECASE,
)
NUTRIENT_PATTERN = re.compile(
    rf"^\s*(?P<value>{NUMBER})\s*(?P<unit>\u5927\u5361|\u5343\u5361|kcal|\u516c\u514b|\u514b|g|\u6beb\u514b|mg)\s*$",
    re.IGNORECASE,
)
UNIT_MAP = {
    "\u5927\u5361": "kcal",
    "\u5343\u5361": "kcal",
    "kcal": "kcal",
    "\u516c\u514b": "g",
    "\u514b": "g",
    "g": "g",
    "\u6beb\u514b": "mg",
    "mg": "mg",
    "\u6beb\u5347": "ml",
    "ml": "ml",
}


class CollectionError(RuntimeError):
    """Raised when the source or generated archive is invalid."""


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def temporary_path(directory: Path, prefix: str, suffix: str) -> Path:
    descriptor, name = tempfile.mkstemp(dir=directory, prefix=prefix, suffix=suffix)
    os.close(descriptor)
    return Path(name)


def download_with_retry(
    destination: Path,
    *,
    timeout: float,
    retries: int,
    user_agent: str,
) -> int:
    last_error: Exception | None = None
    for attempt in range(retries + 1):
        try:
            request = Request(
                DOWNLOAD_URL,
                headers={
                    "User-Agent": user_agent,
                    "Accept": "application/zip,application/octet-stream;q=0.9,*/*;q=0.5",
                },
            )
            with urlopen(request, timeout=timeout) as response, destination.open("wb") as output:
                if getattr(response, "status", 200) != 200:
                    raise CollectionError(f"HTTP status {response.status}")
                while chunk := response.read(1024 * 1024):
                    output.write(chunk)
                output.flush()
                os.fsync(output.fileno())
            if not zipfile.is_zipfile(destination):
                raise CollectionError("downloaded resource is not a ZIP archive")
            return destination.stat().st_size
        except (HTTPError, URLError, TimeoutError, OSError, CollectionError) as error:
            last_error = error
            destination.unlink(missing_ok=True)
            if attempt < retries:
                time.sleep(min(2**attempt, 8))
    raise CollectionError(
        f"download failed after {retries + 1} attempt(s): {last_error}"
    )


def choose_json_member(archive: zipfile.ZipFile) -> zipfile.ZipInfo:
    members = [
        item
        for item in archive.infolist()
        if not item.is_dir() and item.filename.lower().endswith(".json")
    ]
    if len(members) != 1:
        names = ", ".join(item.filename for item in members) or "none"
        raise CollectionError(f"expected exactly one JSON member; found: {names}")
    return members[0]


def iter_json_array(source: TextIO, *, chunk_size: int = 1024 * 1024) -> Iterator[object]:
    """Incrementally decode one top-level JSON array without loading it all."""
    decoder = json.JSONDecoder()
    buffer = ""
    position = 0
    started = False
    finished = False

    while not finished:
        chunk = source.read(chunk_size)
        if chunk:
            buffer += chunk
        elif not buffer[position:].strip():
            break

        while True:
            while position < len(buffer) and buffer[position].isspace():
                position += 1
            if not started:
                if position >= len(buffer):
                    break
                if buffer[position] != "[":
                    raise CollectionError("source JSON is not a top-level array")
                started = True
                position += 1
                continue

            while position < len(buffer) and (
                buffer[position].isspace() or buffer[position] == ","
            ):
                position += 1
            if position < len(buffer) and buffer[position] == "]":
                finished = True
                position += 1
                break
            if position >= len(buffer):
                break
            try:
                value, end = decoder.raw_decode(buffer, position)
            except json.JSONDecodeError:
                if not chunk:
                    raise CollectionError("source JSON ended inside a value")
                break
            yield value
            position = end

        if position:
            buffer = buffer[position:]
            position = 0
        if not chunk and not finished:
            raise CollectionError("source JSON array has no closing bracket")

    if not started or not finished:
        raise CollectionError("source JSON array is incomplete")
    if buffer[position:].strip():
        raise CollectionError("unexpected data after source JSON array")


def clean_text(value: object) -> str:
    return value.strip() if isinstance(value, str) else ""


def numeric_value(text: str) -> int | float:
    number = float(text)
    return int(number) if number.is_integer() else number


def parse_quantity(raw: object) -> dict[str, object]:
    text = raw if isinstance(raw, str) else ""
    parsed: dict[str, object] = {"raw": text, "value": None, "unit": None}
    match = QUANTITY_PATTERN.fullmatch(text)
    if match:
        unit = match.group("unit")
        parsed["value"] = numeric_value(match.group("value"))
        parsed["unit"] = UNIT_MAP.get(unit.lower(), UNIT_MAP.get(unit))
    return parsed


def parse_nutrient(raw: object, expected_unit: str) -> dict[str, object]:
    text = raw if isinstance(raw, str) else ""
    parsed: dict[str, object] = {"raw": text, "value": None, "unit": None}
    match = NUTRIENT_PATTERN.fullmatch(text)
    if not match:
        return parsed
    source_unit = match.group("unit")
    unit = UNIT_MAP.get(source_unit.lower(), UNIT_MAP.get(source_unit))
    if unit != expected_unit:
        return parsed
    parsed["value"] = numeric_value(match.group("value"))
    parsed["unit"] = unit
    return parsed


def normalize_record(raw: dict[str, object]) -> tuple[dict[str, object], list[str]]:
    anomalies: list[str] = []
    package = clean_text(raw.get(FIELD_PACKAGE))
    if PACKAGE_ZERO_PATTERN.search(package):
        anomalies.append("zero_package_quantity")

    nutrition: dict[str, dict[str, object]] = {}
    for name, source_field in NUTRITION_FIELDS.items():
        expected_unit = "kcal" if name == "calories" else "mg" if name == "sodium" else "g"
        value = parse_nutrient(raw.get(source_field), expected_unit)
        nutrition[name] = value
        if value["raw"] and value["value"] is None:
            anomalies.append(f"unparsed_nutrition:{name}")

    normalized: dict[str, object] = {
        "companyName": clean_text(raw.get(FIELD_COMPANY)),
        "productName": clean_text(raw.get(FIELD_PRODUCT)),
        "packageSpecification": package,
        "traceabilityCode": clean_text(raw.get(FIELD_TRACEABILITY_CODE)),
        # TFDA's traceability connection code is not a GTIN/EAN barcode.
        "barcode": None,
        "serving": parse_quantity(raw.get(FIELD_SERVING)),
        "nutrition": nutrition,
        "contentLabel": raw.get(FIELD_CONTENT_LABEL)
        if isinstance(raw.get(FIELD_CONTENT_LABEL), str)
        else "",
    }
    return normalized, anomalies


class GzipJsonWriter:
    def __init__(self, path: Path) -> None:
        self.raw = path.open("w+b")
        self.compressed = gzip.GzipFile(
            fileobj=self.raw, mode="wb", compresslevel=9, mtime=0
        )
        self.uncompressed_size = 0

    def write(self, text: str) -> None:
        encoded = text.encode("utf-8")
        self.compressed.write(encoded)
        self.uncompressed_size += len(encoded)

    def close(self) -> None:
        self.compressed.close()
        self.raw.flush()
        os.fsync(self.raw.fileno())
        self.raw.close()

    def abort(self) -> None:
        try:
            self.compressed.close()
        finally:
            self.raw.close()


def transform_archive(
    archive_path: Path,
    output_path: Path,
    *,
    fetched_at: str,
    sample_limit: int | None = None,
) -> dict[str, object]:
    writer = GzipJsonWriter(output_path)
    total = 0
    complete_nutrition = 0
    missing_content_label = 0
    anomaly_count = 0
    try:
        source = {
            "name": "TFDA \u98df\u54c1\u8ffd\u6eaf\u8ffd\u8e64\u7cfb\u7d71\u6d88\u8cbb\u8005\u67e5\u8a62\u8cc7\u6599\u96c6",
            "provider": "\u885b\u751f\u798f\u5229\u90e8\u98df\u54c1\u85e5\u7269\u7ba1\u7406\u7f72",
            "dataset_url": DATASET_URL,
            "download_url": DOWNLOAD_URL,
            "fetched_at": fetched_at,
        }
        writer.write("{")
        writer.write('"source":' + json.dumps(source, ensure_ascii=False, separators=(",", ":")))
        writer.write(',"fetched_at":' + json.dumps(fetched_at))

        with zipfile.ZipFile(archive_path) as archive:
            member = choose_json_member(archive)
            writer.write(',"archive":' + json.dumps(
                {
                    "member": member.filename,
                    "uncompressed_json_size": member.file_size,
                    "sample_limited": sample_limit is not None,
                },
                ensure_ascii=False,
                separators=(",", ":"),
            ))
            writer.write(',"records":[')
            first = True
            with archive.open(member) as binary:
                text = codecs.getreader("utf-8-sig")(binary, errors="strict")
                for item in iter_json_array(text):
                    if not isinstance(item, dict):
                        raise CollectionError("source array contains a non-object record")
                    if total == 0:
                        missing = [field for field in REQUIRED_SOURCE_FIELDS if field not in item]
                        if missing:
                            raise CollectionError(
                                "source JSON is missing expected fields: " + ", ".join(missing)
                            )
                    normalized, anomalies = normalize_record(item)
                    record = {
                        "raw": item,
                        "normalized": normalized,
                        "anomalies": anomalies,
                    }
                    if not first:
                        writer.write(",")
                    writer.write(json.dumps(record, ensure_ascii=False, separators=(",", ":")))
                    first = False
                    total += 1
                    values = normalized["nutrition"].values()
                    if all(value["value"] is not None for value in values):
                        complete_nutrition += 1
                    if not normalized["contentLabel"]:
                        missing_content_label += 1
                    anomaly_count += len(anomalies)
                    if sample_limit is not None and total >= sample_limit:
                        break

        if total == 0:
            raise CollectionError("source JSON contains no records")
        stats: dict[str, object] = {
            "record_count": total,
            "complete_per_serving_nutrition_count": complete_nutrition,
            "missing_content_label_count": missing_content_label,
            "anomaly_count": anomaly_count,
            "sample_limited": sample_limit is not None,
        }
        writer.write('],"stats":' + json.dumps(stats, separators=(",", ":")) + "}")
        writer.close()
        stats["uncompressed_output_size"] = writer.uncompressed_size
        stats["compressed_output_size"] = output_path.stat().st_size
        return stats
    except BaseException:
        writer.abort()
        raise


def validate_generated_archive(path: Path, expected_records: int) -> None:
    utf8 = codecs.getincrementaldecoder("utf-8")("strict")
    first_non_space = ""
    tail = ""
    marker = f'"record_count":{expected_records}'
    with gzip.open(path, "rb") as source:
        while chunk := source.read(1024 * 1024):
            text = utf8.decode(chunk)
            if not first_non_space:
                stripped = text.lstrip()
                if stripped:
                    first_non_space = stripped[0]
            tail = (tail + text)[-4096:]
        tail += utf8.decode(b"", final=True)
    if first_non_space != "{" or not tail.rstrip().endswith("}") or marker not in tail:
        raise CollectionError("generated gzip JSON failed envelope validation")


def run(args: argparse.Namespace) -> dict[str, object]:
    output = args.output.resolve()
    output.parent.mkdir(parents=True, exist_ok=True)
    output_temporary = temporary_path(output.parent, f".{output.name}.", ".tmp")
    downloaded_temporary: Path | None = None
    try:
        if args.archive is None:
            downloaded_temporary = temporary_path(
                output.parent, f".{output.name}.download.", ".zip.tmp"
            )
            print(f"Downloading official TFDA ZIP: {DOWNLOAD_URL}")
            downloaded_size = download_with_retry(
                downloaded_temporary,
                timeout=args.timeout,
                retries=args.retries,
                user_agent=args.user_agent,
            )
            archive_path = downloaded_temporary
        else:
            archive_path = args.archive.resolve()
            if not zipfile.is_zipfile(archive_path):
                raise CollectionError(f"--archive is not a ZIP file: {archive_path}")
            downloaded_size = archive_path.stat().st_size

        fetched_at = utc_now()
        stats = transform_archive(
            archive_path,
            output_temporary,
            fetched_at=fetched_at,
            sample_limit=args.sample_limit,
        )
        validate_generated_archive(output_temporary, int(stats["record_count"]))
        os.replace(output_temporary, output)
        stats["downloaded_zip_size"] = downloaded_size
        print(json.dumps(stats, ensure_ascii=False, indent=2))
        print(f"Output: {output}")
        return stats
    finally:
        output_temporary.unlink(missing_ok=True)
        if downloaded_temporary is not None:
            downloaded_temporary.unlink(missing_ok=True)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--timeout", type=float, default=60.0)
    parser.add_argument("--retries", type=int, default=2)
    parser.add_argument("--user-agent", default=DEFAULT_USER_AGENT)
    parser.add_argument(
        "--archive",
        type=Path,
        help="Process an existing TFDA ZIP instead of downloading (primarily for tests).",
    )
    parser.add_argument(
        "--sample-limit",
        type=int,
        help="Write only the first N records (test use only; download is still complete).",
    )
    args = parser.parse_args()
    if args.timeout <= 0:
        parser.error("--timeout must be greater than 0")
    if args.retries < 0:
        parser.error("--retries must be 0 or greater")
    if args.sample_limit is not None and args.sample_limit <= 0:
        parser.error("--sample-limit must be greater than 0")
    return args


def main() -> int:
    try:
        run(parse_args())
    except Exception as error:
        print(f"TFDA traceability collection failed: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
