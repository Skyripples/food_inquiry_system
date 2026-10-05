#!/usr/bin/env python3
"""Classify collected TFDA traceability records by usable product data."""

from __future__ import annotations

import argparse
import codecs
import gzip
import json
import os
import re
import sys
import tempfile
from collections import Counter
from collections.abc import Iterator
from datetime import datetime, timezone
from pathlib import Path
from typing import TextIO


PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_INPUT = PROJECT_ROOT / "data" / "raw" / "tfda_traceability.json.gz"
DEFAULT_OUTPUT = PROJECT_ROOT / "data" / "processed" / "tfda_products_usable.json.gz"

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
CLASS_COMPLETE_NUTRITION = "complete_nutrition"
CLASS_HAS_INGREDIENTS = "has_ingredients"
CLASS_BOTH = "complete_nutrition_and_ingredients"
CLASS_INCOMPLETE = "incomplete"
CLASSIFICATIONS = (
    CLASS_COMPLETE_NUTRITION,
    CLASS_HAS_INGREDIENTS,
    CLASS_BOTH,
    CLASS_INCOMPLETE,
)
RECORDS_PATTERN = re.compile(r'"records"\s*:\s*\[')


class FilterError(RuntimeError):
    """Raised when the input or generated output is invalid."""


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def temporary_path(directory: Path, prefix: str, suffix: str) -> Path:
    descriptor, name = tempfile.mkstemp(dir=directory, prefix=prefix, suffix=suffix)
    os.close(descriptor)
    return Path(name)


def open_record_stream(
    source: TextIO, *, chunk_size: int = 1024 * 1024
) -> tuple[dict[str, object], Iterator[object]]:
    """Read the envelope and return a streaming iterator over its records."""
    buffer = ""
    match: re.Match[str] | None = None
    while match is None:
        chunk = source.read(chunk_size)
        if not chunk:
            raise FilterError('input JSON has no "records" array')
        buffer += chunk
        match = RECORDS_PATTERN.search(buffer)
        if len(buffer) > 16 * 1024 * 1024:
            raise FilterError('input JSON envelope is unexpectedly large')

    header_text = buffer[: match.start()] + '"records":[]}'
    try:
        metadata = json.loads(header_text)
    except json.JSONDecodeError as error:
        raise FilterError(f"input JSON envelope is invalid: {error}") from error
    if not isinstance(metadata, dict):
        raise FilterError("input JSON envelope is not an object")

    initial = buffer[match.end() :]

    def records() -> Iterator[object]:
        decoder = json.JSONDecoder()
        pending = initial
        position = 0
        finished = False
        while not finished:
            chunk = source.read(chunk_size)
            if chunk:
                pending += chunk

            while True:
                while position < len(pending) and (
                    pending[position].isspace() or pending[position] == ","
                ):
                    position += 1
                if position < len(pending) and pending[position] == "]":
                    finished = True
                    position += 1
                    break
                if position >= len(pending):
                    break
                try:
                    value, end = decoder.raw_decode(pending, position)
                except json.JSONDecodeError:
                    if not chunk:
                        raise FilterError("input records array ended inside a value")
                    break
                yield value
                position = end

            if position:
                pending = pending[position:]
                position = 0
            if not chunk and not finished:
                raise FilterError("input records array has no closing bracket")

        # Read to EOF so gzip validates its CRC and truncated input is rejected.
        while source.read(chunk_size):
            pass

    return metadata, records()


def is_number(value: object) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def has_complete_nutrition(record: dict[str, object]) -> bool:
    normalized = record.get("normalized")
    if not isinstance(normalized, dict):
        return False
    nutrition = normalized.get("nutrition")
    if not isinstance(nutrition, dict):
        return False
    for nutrient_name in NUTRIENT_NAMES:
        nutrient = nutrition.get(nutrient_name)
        if not isinstance(nutrient, dict):
            return False
        if not is_number(nutrient.get("value")):
            return False
        if not isinstance(nutrient.get("unit"), str) or not nutrient["unit"].strip():
            return False
    return True


def has_ingredients(record: dict[str, object]) -> bool:
    normalized = record.get("normalized")
    if not isinstance(normalized, dict):
        return False
    content_label = normalized.get("contentLabel")
    return isinstance(content_label, str) and bool(content_label.strip())


def classify_record(record: dict[str, object]) -> str:
    complete_nutrition = has_complete_nutrition(record)
    ingredients = has_ingredients(record)
    if complete_nutrition and ingredients:
        return CLASS_BOTH
    if complete_nutrition:
        return CLASS_COMPLETE_NUTRITION
    if ingredients:
        return CLASS_HAS_INGREDIENTS
    return CLASS_INCOMPLETE


def has_anomaly(record: dict[str, object]) -> bool:
    anomalies = record.get("anomalies")
    return isinstance(anomalies, list) and bool(anomalies)


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


def filter_products(
    input_path: Path,
    temporary_output: Path,
    *,
    generated_at: str,
    sample_limit: int | None = None,
) -> dict[str, object]:
    counts: Counter[str] = Counter()
    anomaly_records = 0
    total = 0
    writer = GzipJsonWriter(temporary_output)
    try:
        with gzip.open(input_path, "rb") as binary_source:
            text_source = codecs.getreader("utf-8")(binary_source, errors="strict")
            upstream, records = open_record_stream(text_source)
            output_source = {
                "name": "TFDA traceability usable-product classification",
                "input": str(input_path),
                "generated_at": generated_at,
                "upstream": upstream.get("source"),
                "upstream_fetched_at": upstream.get("fetched_at"),
            }
            definitions = {
                CLASS_COMPLETE_NUTRITION: "all eight per-serving nutrients are parsed; content label is empty",
                CLASS_HAS_INGREDIENTS: "content label is present; nutrition is not complete",
                CLASS_BOTH: "all eight per-serving nutrients and content label are present",
                CLASS_INCOMPLETE: "neither complete per-serving nutrition nor content label is present",
            }
            writer.write("{")
            writer.write('"source":' + json.dumps(
                output_source, ensure_ascii=False, separators=(",", ":")
            ))
            writer.write(',"classificationDefinitions":' + json.dumps(
                definitions, ensure_ascii=False, separators=(",", ":")
            ))
            writer.write(',"records":[')

            first = True
            for item in records:
                if not isinstance(item, dict):
                    raise FilterError("input records array contains a non-object value")
                classification = classify_record(item)
                anomaly = has_anomaly(item)
                wrapped = {
                    "classification": classification,
                    "hasAnomaly": anomaly,
                    "record": item,
                }
                if not first:
                    writer.write(",")
                writer.write(json.dumps(wrapped, ensure_ascii=False, separators=(",", ":")))
                first = False
                total += 1
                counts[classification] += 1
                anomaly_records += int(anomaly)
                if sample_limit is not None and total >= sample_limit:
                    break

        if total == 0:
            raise FilterError("input contains no product records")
        stats: dict[str, object] = {
            "record_count": total,
            "classification_counts": {
                name: counts[name] for name in CLASSIFICATIONS
            },
            "anomaly_record_count": anomaly_records,
            "sample_limited": sample_limit is not None,
        }
        writer.write('],"stats":' + json.dumps(stats, separators=(",", ":")) + "}")
        writer.close()
        stats["uncompressed_output_size"] = writer.uncompressed_size
        stats["compressed_output_size"] = temporary_output.stat().st_size
        return stats
    except BaseException:
        writer.abort()
        raise


def validate_generated_archive(path: Path, expected_records: int) -> None:
    decoder = codecs.getincrementaldecoder("utf-8")("strict")
    first_non_space = ""
    tail = ""
    marker = f'"record_count":{expected_records}'
    with gzip.open(path, "rb") as source:
        while chunk := source.read(1024 * 1024):
            text = decoder.decode(chunk)
            if not first_non_space:
                stripped = text.lstrip()
                if stripped:
                    first_non_space = stripped[0]
            tail = (tail + text)[-4096:]
        tail += decoder.decode(b"", final=True)
    if first_non_space != "{" or not tail.rstrip().endswith("}") or marker not in tail:
        raise FilterError("generated gzip JSON failed envelope validation")


def run(args: argparse.Namespace) -> dict[str, object]:
    input_path = args.input.resolve()
    output_path = args.output.resolve()
    if not input_path.is_file():
        raise FilterError(f"input does not exist: {input_path}")
    output_path.parent.mkdir(parents=True, exist_ok=True)
    temporary = temporary_path(output_path.parent, f".{output_path.name}.", ".tmp")
    try:
        stats = filter_products(
            input_path,
            temporary,
            generated_at=utc_now(),
            sample_limit=args.sample_limit,
        )
        validate_generated_archive(temporary, int(stats["record_count"]))
        os.replace(temporary, output_path)
        print(json.dumps(stats, ensure_ascii=False, indent=2))
        print(f"Output: {output_path}")
        return stats
    finally:
        temporary.unlink(missing_ok=True)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", type=Path, default=DEFAULT_INPUT)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument(
        "--sample-limit",
        type=int,
        help="Process only the first N records (test use only).",
    )
    args = parser.parse_args()
    if args.sample_limit is not None and args.sample_limit <= 0:
        parser.error("--sample-limit must be greater than 0")
    return args


def main() -> int:
    try:
        run(parse_args())
    except Exception as error:
        print(f"TFDA product filtering failed: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
