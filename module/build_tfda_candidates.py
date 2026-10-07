#!/usr/bin/env python3
"""Build review-only candidates from complete TFDA traceability products."""

from __future__ import annotations

import argparse
import codecs
import gzip
import hashlib
import json
import os
import re
import sys
import tempfile
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

try:
    from module.filter_tfda_products import open_record_stream
except ModuleNotFoundError:  # Direct execution places the module directory on sys.path.
    from filter_tfda_products import open_record_stream


PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_INPUT = PROJECT_ROOT / "data" / "processed" / "tfda_products_usable.json.gz"
DEFAULT_OUTPUT = PROJECT_ROOT / "data" / "candidates" / "tfda_candidates.json.gz"
TARGET_CLASSIFICATION = "complete_nutrition_and_ingredients"
SOURCE_ID = "tfda-traceability"
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
OPENING_BRACKETS = {
    "(": ")",
    "（": "）",
    "[": "]",
    "【": "】",
    "〔": "〕",
    "﹝": "﹞",
    "{": "}",
}
CLOSING_BRACKETS = set(OPENING_BRACKETS.values())
INGREDIENT_SEPARATORS = {"、", ",", "，", ";", "；", "\n", "\r"}


class CandidateError(RuntimeError):
    """Raised when candidate input or output is invalid."""


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def temporary_path(directory: Path, prefix: str, suffix: str) -> Path:
    descriptor, name = tempfile.mkstemp(dir=directory, prefix=prefix, suffix=suffix)
    os.close(descriptor)
    return Path(name)


def clean_text(value: object) -> str:
    return value.strip() if isinstance(value, str) else ""


def split_ingredients(raw: object) -> tuple[list[str], str | None]:
    """Split only top-level delimiters, preserving source order and compounds."""
    text = raw if isinstance(raw, str) else ""
    if not text.strip():
        return [], "empty_content_label"

    parts: list[str] = []
    current: list[str] = []
    stack: list[str] = []
    last_top_level_separator: str | None = None
    for character in text:
        if character in OPENING_BRACKETS:
            stack.append(OPENING_BRACKETS[character])
            current.append(character)
            last_top_level_separator = None
        elif character in CLOSING_BRACKETS:
            if not stack or character != stack[-1]:
                return [], "unbalanced_ingredient_brackets"
            stack.pop()
            current.append(character)
            last_top_level_separator = None
        elif character in INGREDIENT_SEPARATORS and not stack:
            ingredient = "".join(current).strip()
            if ingredient:
                parts.append(ingredient)
            elif not (
                character == "、"
                and last_top_level_separator == "、"
                and parts
            ):
                return [], "empty_ingredient_between_separators"
            current = []
            last_top_level_separator = character
        else:
            current.append(character)
            if not character.isspace():
                last_top_level_separator = None

    if stack:
        return [], "unbalanced_ingredient_brackets"
    ingredient = "".join(current).strip()
    if ingredient:
        parts.append(ingredient)
    elif parts and last_top_level_separator != "、":
        return [], "trailing_ingredient_separator"
    if not parts:
        return [], "ingredients_not_parsed"
    return parts, None


def nutrition_is_valid(value: object) -> bool:
    if not isinstance(value, dict) or set(NUTRIENT_NAMES) - set(value):
        return False
    for name in NUTRIENT_NAMES:
        nutrient = value.get(name)
        if not isinstance(nutrient, dict):
            return False
        amount = nutrient.get("value")
        unit = nutrient.get("unit")
        if isinstance(amount, bool) or not isinstance(amount, (int, float)):
            return False
        if not isinstance(unit, str) or not unit.strip():
            return False
    return True


def serving_is_missing(value: object) -> bool:
    if not isinstance(value, dict):
        return True
    amount = value.get("value")
    unit = value.get("unit")
    return (
        isinstance(amount, bool)
        or not isinstance(amount, (int, float))
        or not isinstance(unit, str)
        or not unit.strip()
    )


def identity_key(normalized: dict[str, object]) -> tuple[str, str, str]:
    return (
        clean_text(normalized.get("companyName")),
        clean_text(normalized.get("productName")),
        clean_text(normalized.get("packageSpecification")),
    )


def candidate_id_base(
    traceability_code: str, identity: tuple[str, str, str]
) -> str:
    if traceability_code:
        safe_code = re.sub(r"[^A-Za-z0-9._-]+", "-", traceability_code).strip("-")
        if safe_code:
            return f"tfda-traceability:{safe_code}"
    digest = hashlib.sha256("\x1f".join(identity).encode("utf-8")).hexdigest()[:20]
    return f"tfda-traceability:missing:{digest}"


def source_metadata(upstream: object, fetched_at: object) -> dict[str, object]:
    source = upstream if isinstance(upstream, dict) else {}
    return {
        "id": SOURCE_ID,
        "name": source.get("name", "TFDA 食品追溯追蹤資料集"),
        "provider": source.get("provider"),
        "dataset_url": source.get("dataset_url"),
        "download_url": source.get("download_url"),
        "fetched_at": fetched_at,
    }


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


def build_candidates(
    input_path: Path,
    temporary_output: Path,
    *,
    generated_at: str,
    sample_limit: int | None = None,
) -> dict[str, object]:
    total_input_scanned = 0
    selected = 0
    anomaly_candidates = 0
    missing_serving = 0
    ingredient_failures = 0
    nutrition_anomalies = 0
    trace_counts: Counter[str] = Counter()
    identity_counts: Counter[tuple[str, str, str]] = Counter()
    candidate_id_counts: Counter[str] = Counter()
    writer = GzipJsonWriter(temporary_output)

    try:
        with gzip.open(input_path, "rb") as binary_source:
            text_source = codecs.getreader("utf-8")(binary_source, errors="strict")
            envelope, records = open_record_stream(text_source)
            processed_source = envelope.get("source")
            processed_source = processed_source if isinstance(processed_source, dict) else {}
            fetched_at = processed_source.get("upstream_fetched_at")
            upstream = processed_source.get("upstream")
            source = source_metadata(upstream, fetched_at)

            writer.write("{")
            writer.write('"source":' + json.dumps(
                {
                    "name": "TFDA complete product candidate builder",
                    "input": str(input_path),
                    "target_classification": TARGET_CLASSIFICATION,
                    "generated_at": generated_at,
                    "upstream": source,
                },
                ensure_ascii=False,
                separators=(",", ":"),
            ))
            writer.write(',"candidates":[')
            first = True

            for wrapper in records:
                total_input_scanned += 1
                if not isinstance(wrapper, dict):
                    raise CandidateError("processed records contain a non-object value")
                if wrapper.get("classification") != TARGET_CLASSIFICATION:
                    continue
                record = wrapper.get("record")
                if not isinstance(record, dict):
                    raise CandidateError("selected wrapper has no record object")
                normalized = record.get("normalized")
                if not isinstance(normalized, dict):
                    raise CandidateError("selected record has no normalized object")

                identity = identity_key(normalized)
                traceability_code = clean_text(normalized.get("traceabilityCode"))
                trace_counts[traceability_code] += 1
                identity_counts[identity] += 1

                base_id = candidate_id_base(traceability_code, identity)
                candidate_id_counts[base_id] += 1
                occurrence = candidate_id_counts[base_id]
                candidate_id = base_id if occurrence == 1 else f"{base_id}:duplicate-{occurrence}"

                ingredients_raw = normalized.get("contentLabel")
                ingredient_names, ingredient_error = split_ingredients(ingredients_raw)
                if ingredient_error:
                    ingredient_failures += 1
                ingredients = [
                    {"name": name, "sourceId": SOURCE_ID} for name in ingredient_names
                ]

                serving = normalized.get("serving")
                if serving_is_missing(serving):
                    missing_serving += 1
                nutrition = normalized.get("nutrition")
                if not nutrition_is_valid(nutrition):
                    nutrition_anomalies += 1
                anomalies = record.get("anomalies")
                anomalies = list(anomalies) if isinstance(anomalies, list) else []
                anomaly_candidates += int(bool(anomalies))

                candidate = {
                    "candidateId": candidate_id,
                    "companyName": clean_text(normalized.get("companyName")),
                    "brand": None,
                    "productName": clean_text(normalized.get("productName")),
                    "packageSpecification": clean_text(
                        normalized.get("packageSpecification")
                    ),
                    "traceabilityCode": traceability_code,
                    "barcode": None,
                    "serving": serving,
                    "nutrition": nutrition,
                    "ingredients": ingredients,
                    "ingredientsRaw": ingredients_raw
                    if isinstance(ingredients_raw, str)
                    else "",
                    "ingredientsParseError": ingredient_error,
                    "source": source,
                    "fetched_at": fetched_at,
                    "anomalies": anomalies,
                }
                if not first:
                    writer.write(",")
                writer.write(json.dumps(candidate, ensure_ascii=False, separators=(",", ":")))
                first = False
                selected += 1
                if sample_limit is not None and selected >= sample_limit:
                    break

        if selected == 0:
            raise CandidateError(f"no {TARGET_CLASSIFICATION} records found")

        duplicate_trace_values = sum(
            1 for code, count in trace_counts.items() if code and count > 1
        )
        duplicate_trace_records = sum(
            count - 1 for code, count in trace_counts.items() if code and count > 1
        )
        duplicate_identity_values = sum(1 for count in identity_counts.values() if count > 1)
        duplicate_identity_records = sum(
            count - 1 for count in identity_counts.values() if count > 1
        )
        stats: dict[str, object] = {
            "input_records_scanned": total_input_scanned,
            "candidate_count": selected,
            "duplicate_traceability_code_values": duplicate_trace_values,
            "duplicate_traceability_code_records": duplicate_trace_records,
            "duplicate_company_product_specification_values": duplicate_identity_values,
            "duplicate_company_product_specification_records": duplicate_identity_records,
            "missing_serving_count": missing_serving,
            "ingredients_parse_failure_count": ingredient_failures,
            "nutrition_structure_anomaly_count": nutrition_anomalies,
            "anomaly_candidate_count": anomaly_candidates,
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


def validate_generated_archive(path: Path, expected_candidates: int) -> None:
    decoder = codecs.getincrementaldecoder("utf-8")("strict")
    first_non_space = ""
    tail = ""
    marker = f'"candidate_count":{expected_candidates}'
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
        raise CandidateError("generated gzip JSON failed envelope validation")


def run(args: argparse.Namespace) -> dict[str, object]:
    input_path = args.input.resolve()
    output_path = args.output.resolve()
    if not input_path.is_file():
        raise CandidateError(f"input does not exist: {input_path}")
    output_path.parent.mkdir(parents=True, exist_ok=True)
    temporary = temporary_path(output_path.parent, f".{output_path.name}.", ".tmp")
    try:
        stats = build_candidates(
            input_path,
            temporary,
            generated_at=utc_now(),
            sample_limit=args.sample_limit,
        )
        validate_generated_archive(temporary, int(stats["candidate_count"]))
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
        help="Build only the first N matching candidates (test use only).",
    )
    args = parser.parse_args()
    if args.sample_limit is not None and args.sample_limit <= 0:
        parser.error("--sample-limit must be greater than 0")
    return args


def main() -> int:
    try:
        run(parse_args())
    except Exception as error:
        print(f"TFDA candidate build failed: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
