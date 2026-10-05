#!/usr/bin/env python3
"""Download and stream Taiwan's official food-business registry into gzip JSON."""

from __future__ import annotations

import argparse
import csv
import gzip
import io
import json
import os
import re
import sys
import tempfile
import time
import zipfile
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen


DATASET_PAGE_URL = "https://data.gov.tw/dataset/8938"
DOWNLOAD_URL = "https://data.fda.gov.tw/data/opendata/export/97/csv"
DEFAULT_OUTPUT = (
    Path(__file__).resolve().parents[1]
    / "data"
    / "raw"
    / "food_business_registry.json.gz"
)
DEFAULT_USER_AGENT = (
    "food-inquiry-system food-business-registry collector/2.4 "
    "(+https://github.com/Skyripples/food_inquiry_system)"
)
PRIMARY_FIELDS = (
    "公司或商業登記名稱",
    "公司統一編號",
    "業者地址",
    "食品業者登錄字號",
    "登錄項目",
)
UNIFIED_NUMBER_PATTERN = re.compile(r"^\d{8}$")


class CollectionError(RuntimeError):
    pass


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def create_temporary_path(directory: Path, prefix: str, suffix: str) -> Path:
    descriptor, name = tempfile.mkstemp(dir=directory, prefix=prefix, suffix=suffix)
    os.close(descriptor)
    return Path(name)


def download_with_retry(
    url: str,
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
                url,
                headers={
                    "User-Agent": user_agent,
                    "Accept": "application/zip,application/octet-stream;q=0.9,*/*;q=0.5",
                },
            )
            with urlopen(request, timeout=timeout) as response, destination.open("wb") as output:
                if getattr(response, "status", 200) != 200:
                    raise CollectionError(f"HTTP status {response.status}")
                while True:
                    chunk = response.read(1024 * 1024)
                    if not chunk:
                        break
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
    raise CollectionError(f"download failed after {retries + 1} attempt(s): {last_error}")


def choose_csv_member(archive: zipfile.ZipFile) -> zipfile.ZipInfo:
    members = [
        member for member in archive.infolist()
        if not member.is_dir() and member.filename.lower().endswith(".csv")
    ]
    if len(members) != 1:
        names = ", ".join(member.filename for member in members) or "none"
        raise CollectionError(f"expected exactly one CSV in ZIP; found: {names}")
    return members[0]


def detect_encoding(archive: zipfile.ZipFile, member: zipfile.ZipInfo) -> str:
    with archive.open(member) as source:
        sample = source.read(64 * 1024)
    if sample.startswith(b"\xef\xbb\xbf"):
        return "utf-8-sig"
    for encoding in ("utf-8", "cp950"):
        try:
            sample.decode(encoding)
            return encoding
        except UnicodeDecodeError:
            continue
    raise CollectionError("CSV encoding is neither UTF-8 nor CP950")


def normalized(value: object) -> str:
    return value.strip() if isinstance(value, str) else ""


def business_identity(record: dict[str, str]) -> tuple[str, ...]:
    unified_number = normalized(record.get("公司統一編號"))
    if unified_number:
        return ("unified_number", unified_number)
    # Missing unified numbers remain in records. This fallback is used only for
    # the aggregate unique-business statistic and never mutates source data.
    return (
        "name_address",
        normalized(record.get("公司或商業登記名稱")),
        normalized(record.get("業者地址")),
    )


class GzipJsonWriter:
    def __init__(self, path: Path) -> None:
        self.path = path
        self.raw = path.open("w+b")
        self.compressed = gzip.GzipFile(
            fileobj=self.raw,
            mode="wb",
            compresslevel=9,
            mtime=0,
        )
        self.uncompressed_size = 0

    def write(self, value: str) -> None:
        encoded = value.encode("utf-8")
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


def validate_gzip_stream(path: Path) -> int:
    """Fully decompress and UTF-8 decode, validating gzip CRC without buffering."""
    decoded_characters = 0
    with gzip.open(path, "rt", encoding="utf-8", errors="strict") as source:
        while True:
            chunk = source.read(1024 * 1024)
            if not chunk:
                break
            decoded_characters += len(chunk)
    if decoded_characters == 0:
        raise CollectionError("generated gzip JSON is empty")
    return decoded_characters


def collect_registry(
    archive_path: Path,
    output_temporary: Path,
    *,
    fetched_at: str,
) -> dict[str, object]:
    total_records = 0
    with_unified_number = 0
    without_unified_number = 0
    malformed_unified_numbers = 0
    malformed_rows = 0
    empty_rows = 0
    missing_fields: Counter[str] = Counter()
    registration_items: Counter[str] = Counter()
    unique_businesses: set[tuple[str, ...]] = set()

    writer = GzipJsonWriter(output_temporary)
    try:
        source_metadata = {
            "name": "政府資料開放平臺－食品業者登錄資料集",
            "dataset_url": DATASET_PAGE_URL,
            "download_url": DOWNLOAD_URL,
            "provider": "衛生福利部食品藥物管理署",
            "fetched_at": fetched_at,
        }
        writer.write("{")
        writer.write('"source":')
        writer.write(json.dumps(source_metadata, ensure_ascii=False, separators=(",", ":")))
        writer.write(',"fetched_at":')
        writer.write(json.dumps(fetched_at))

        with zipfile.ZipFile(archive_path) as archive:
            member = choose_csv_member(archive)
            encoding = detect_encoding(archive, member)
            writer.write(',"archive":')
            writer.write(json.dumps({
                "member": member.filename,
                "encoding": encoding,
                "uncompressed_csv_size": member.file_size,
            }, ensure_ascii=False, separators=(",", ":")))
            writer.write(',"records":[')

            with archive.open(member) as binary_source:
                text_source = io.TextIOWrapper(binary_source, encoding=encoding, newline="")
                reader = csv.DictReader(text_source)
                fields = list(reader.fieldnames or [])
                if not fields:
                    raise CollectionError("CSV has no header")
                missing_headers = [field for field in PRIMARY_FIELDS if field not in fields]
                if missing_headers:
                    raise CollectionError(f"CSV missing required headers: {', '.join(missing_headers)}")

                first_record = True
                for raw_record in reader:
                    overflow = raw_record.pop(None, None)
                    if overflow:
                        malformed_rows += 1
                    record = {
                        str(key): value if isinstance(value, str) else ""
                        for key, value in raw_record.items()
                    }
                    if not any(normalized(value) for value in record.values()):
                        empty_rows += 1
                        continue

                    total_records += 1
                    unified_number = normalized(record.get("公司統一編號"))
                    if unified_number:
                        with_unified_number += 1
                        if UNIFIED_NUMBER_PATTERN.fullmatch(unified_number) is None:
                            malformed_unified_numbers += 1
                    else:
                        without_unified_number += 1

                    for field in PRIMARY_FIELDS:
                        if not normalized(record.get(field)):
                            missing_fields[field] += 1
                    registration_item = normalized(record.get("登錄項目")) or "（空白）"
                    registration_items[registration_item] += 1
                    unique_businesses.add(business_identity(record))

                    if not first_record:
                        writer.write(",")
                    writer.write(json.dumps(record, ensure_ascii=False, separators=(",", ":")))
                    first_record = False

        stats: dict[str, object] = {
            "total_registration_records": total_records,
            "records_with_unified_number": with_unified_number,
            "records_without_unified_number": without_unified_number,
            "unique_business_count": len(unique_businesses),
            "unique_business_method": (
                "公司統一編號；缺少統一編號時以公司或商業登記名稱與業者地址組合"
            ),
            "registration_item_counts": dict(sorted(registration_items.items())),
            "missing_primary_field_counts": {
                field: missing_fields[field] for field in PRIMARY_FIELDS
            },
            "malformed_unified_number_count": malformed_unified_numbers,
            "malformed_csv_row_count": malformed_rows,
            "empty_csv_row_count": empty_rows,
        }
        writer.write('],"stats":')
        writer.write(json.dumps(stats, ensure_ascii=False, separators=(",", ":")))
        writer.write("}")
        writer.close()
        stats["uncompressed_json_size"] = writer.uncompressed_size
        stats["compressed_json_size"] = output_temporary.stat().st_size
        return stats
    except BaseException:
        writer.abort()
        raise


def run(args: argparse.Namespace) -> dict[str, object]:
    output = args.output.resolve()
    output.parent.mkdir(parents=True, exist_ok=True)
    download_temporary = create_temporary_path(
        output.parent, f".{output.name}.download.", ".zip.tmp"
    )
    output_temporary = create_temporary_path(
        output.parent, f".{output.name}.", ".tmp"
    )
    try:
        fetched_at = utc_now()
        print(f"下載官方 CSV ZIP：{DOWNLOAD_URL}")
        downloaded_size = download_with_retry(
            DOWNLOAD_URL,
            download_temporary,
            timeout=args.timeout,
            retries=args.retries,
            user_agent=args.user_agent,
        )
        print(f"下載大小：{downloaded_size} bytes")
        stats = collect_registry(
            download_temporary,
            output_temporary,
            fetched_at=fetched_at,
        )
        validate_gzip_stream(output_temporary)
        os.replace(output_temporary, output)
        stats["downloaded_zip_size"] = downloaded_size
        print("完成")
        print(json.dumps(stats, ensure_ascii=False, indent=2))
        print(f"輸出：{output}")
        return stats
    finally:
        download_temporary.unlink(missing_ok=True)
        output_temporary.unlink(missing_ok=True)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--timeout", type=float, default=60.0)
    parser.add_argument("--retries", type=int, default=2)
    parser.add_argument("--user-agent", default=DEFAULT_USER_AGENT)
    args = parser.parse_args()
    if args.timeout <= 0:
        parser.error("--timeout must be greater than 0")
    if args.retries < 0:
        parser.error("--retries must be 0 or greater")
    return args


def main() -> int:
    args = parse_args()
    try:
        run(args)
    except Exception as error:
        print(f"食品業者母清單蒐集失敗：{error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
