#!/usr/bin/env python3
"""Filter factory/manufacturing registrations from the food-business registry."""

from __future__ import annotations

import argparse
import gzip
import io
import json
import os
import re
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterator


PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_INPUT = PROJECT_ROOT / "data" / "raw" / "food_business_registry.json.gz"
DEFAULT_OUTPUT = PROJECT_ROOT / "data" / "processed" / "food_manufacturers.json.gz"
MANUFACTURING_ITEM = "工廠/製造場所"
MANUFACTURING_ITEM_ALIASES = frozenset({MANUFACTURING_ITEM, "工廠／製造場所"})
UNIFIED_NUMBER_PATTERN = re.compile(r"^\d{8}$")
RECORDS_START_PATTERN = re.compile(r'"records"\s*:\s*\[')


class FilterError(RuntimeError):
    pass


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def normalized(value: object) -> str:
    return value.strip() if isinstance(value, str) else ""


def is_manufacturing_record(record: dict[str, object]) -> bool:
    return normalized(record.get("登錄項目")) in MANUFACTURING_ITEM_ALIASES


def iter_registry_records(path: Path, chunk_size: int = 1024 * 1024) -> Iterator[dict[str, object]]:
    """Stream objects from the top-level records array without loading the registry."""
    decoder = json.JSONDecoder()
    with gzip.open(path, "rt", encoding="utf-8", errors="strict") as source:
        buffer = ""
        while True:
            chunk = source.read(chunk_size)
            if not chunk:
                raise FilterError("input JSON does not contain a records array")
            buffer += chunk
            match = RECORDS_START_PATTERN.search(buffer)
            if match:
                buffer = buffer[match.end():]
                break
            # The key token is short; retaining a small suffix avoids unbounded
            # memory use if metadata before records becomes unexpectedly large.
            buffer = buffer[-256:]

        position = 0
        reached_end = False
        while not reached_end:
            while True:
                while position < len(buffer) and (buffer[position].isspace() or buffer[position] == ","):
                    position += 1
                if position < len(buffer):
                    break
                chunk = source.read(chunk_size)
                if not chunk:
                    raise FilterError("records array ended unexpectedly")
                buffer = chunk
                position = 0

            if buffer[position] == "]":
                reached_end = True
                continue

            while True:
                try:
                    record, end = decoder.raw_decode(buffer, position)
                    break
                except json.JSONDecodeError:
                    chunk = source.read(chunk_size)
                    if not chunk:
                        raise FilterError("invalid JSON object in records array")
                    buffer = buffer[position:] + chunk
                    position = 0

            if not isinstance(record, dict):
                raise FilterError("records array contains a non-object value")
            yield record
            position = end
            if position > chunk_size:
                buffer = buffer[position:]
                position = 0


def group_manufacturers(records: Iterator[dict[str, object]]) -> tuple[list[dict[str, object]], dict[str, int]]:
    grouped: dict[str, dict[str, object]] = {}
    manufacturers: list[dict[str, object]] = []
    scanned_count = 0
    manufacturing_record_count = 0
    valid_unified_number_count = 0
    invalid_unified_number_count = 0
    missing_unified_number_count = 0

    for record in records:
        scanned_count += 1
        if not is_manufacturing_record(record):
            continue
        manufacturing_record_count += 1
        unified_number = normalized(record.get("公司統一編號"))
        if not unified_number:
            missing_unified_number_count += 1
            # Every missing-number record remains independent, even if its name
            # or address resembles another record.
            manufacturers.append({
                "unified_number": None,
                "unified_number_status": "missing",
                "records": [record],
            })
            continue

        valid = UNIFIED_NUMBER_PATTERN.fullmatch(unified_number) is not None
        if valid:
            valid_unified_number_count += 1
        else:
            invalid_unified_number_count += 1
        business = grouped.get(unified_number)
        if business is None:
            business = {
                "unified_number": unified_number,
                "unified_number_status": "valid" if valid else "invalid",
                "records": [],
            }
            grouped[unified_number] = business
            manufacturers.append(business)
        business["records"].append(record)

    stats = {
        "scanned_record_count": scanned_count,
        "manufacturing_record_count": manufacturing_record_count,
        "manufacturer_group_count": len(manufacturers),
        "records_with_valid_unified_number": valid_unified_number_count,
        "records_with_invalid_unified_number": invalid_unified_number_count,
        "records_without_unified_number": missing_unified_number_count,
    }
    return manufacturers, stats


def build_document(input_path: Path) -> dict[str, object]:
    manufacturers, stats = group_manufacturers(iter_registry_records(input_path))
    return {
        "source": {
            "name": "食品業者登錄資料集－工廠／製造場所篩選結果",
            "input": str(input_path),
            "registration_item_filter": MANUFACTURING_ITEM,
            "filtered_at": utc_now(),
        },
        "manufacturers": manufacturers,
        "stats": stats,
    }


def validate_document(document: object) -> None:
    if not isinstance(document, dict) or not isinstance(document.get("manufacturers"), list):
        raise FilterError("output root or manufacturers is invalid")
    stats = document.get("stats")
    if not isinstance(stats, dict):
        raise FilterError("output stats is invalid")
    record_count = 0
    for index, manufacturer in enumerate(document["manufacturers"]):
        if not isinstance(manufacturer, dict):
            raise FilterError(f"manufacturer {index} is not an object")
        status = manufacturer.get("unified_number_status")
        unified_number = manufacturer.get("unified_number")
        records = manufacturer.get("records")
        if status not in {"valid", "invalid", "missing"} or not isinstance(records, list) or not records:
            raise FilterError(f"manufacturer {index} has invalid grouping metadata")
        if status == "valid" and (
            not isinstance(unified_number, str)
            or UNIFIED_NUMBER_PATTERN.fullmatch(unified_number) is None
        ):
            raise FilterError(f"manufacturer {index} has an invalid valid-number marker")
        if status == "invalid" and (not isinstance(unified_number, str) or not unified_number):
            raise FilterError(f"manufacturer {index} lost its abnormal unified number")
        if status == "missing" and (unified_number is not None or len(records) != 1):
            raise FilterError(f"manufacturer {index} merged a missing-number record")
        for record in records:
            if not isinstance(record, dict) or not is_manufacturing_record(record):
                raise FilterError(f"manufacturer {index} contains a non-manufacturing record")
        record_count += len(records)
    if record_count != stats.get("manufacturing_record_count"):
        raise FilterError("output manufacturing record count does not match stats")
    if len(document["manufacturers"]) != stats.get("manufacturer_group_count"):
        raise FilterError("output manufacturer group count does not match stats")


def atomic_write_gzip_json(output: Path, document: dict[str, object]) -> tuple[int, int]:
    output.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        dir=output.parent,
        prefix=f".{output.name}.",
        suffix=".tmp",
    )
    os.close(descriptor)
    temporary = Path(temporary_name)
    try:
        with temporary.open("w+b") as raw:
            with gzip.GzipFile(fileobj=raw, mode="wb", compresslevel=9, mtime=0) as compressed:
                with io.TextIOWrapper(compressed, encoding="utf-8", newline="\n") as text:
                    json.dump(document, text, ensure_ascii=False, separators=(",", ":"))
            raw.flush()
            os.fsync(raw.fileno())

        with gzip.open(temporary, "rt", encoding="utf-8") as source:
            verified = json.load(source)
        validate_document(verified)
        uncompressed_size = len(
            json.dumps(document, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        )
        compressed_size = temporary.stat().st_size
        os.replace(temporary, output)
        return uncompressed_size, compressed_size
    except BaseException:
        temporary.unlink(missing_ok=True)
        raise


def run(input_path: Path, output_path: Path) -> dict[str, object]:
    input_path = input_path.resolve()
    output_path = output_path.resolve()
    if not input_path.is_file():
        raise FilterError(f"input file does not exist: {input_path}")
    document = build_document(input_path)
    validate_document(document)
    uncompressed_size, compressed_size = atomic_write_gzip_json(output_path, document)
    result = {
        **document["stats"],
        "uncompressed_json_size": uncompressed_size,
        "compressed_json_size": compressed_size,
        "output": str(output_path),
    }
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return result


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", type=Path, default=DEFAULT_INPUT)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    try:
        run(args.input, args.output)
    except Exception as error:
        print(f"食品製造業者篩選失敗：{error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
