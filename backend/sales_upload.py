"""Sales data ingestion, validation, and staging module for InsightIQ.

Provides:
- In-memory parsing of CSV and XLSX files (up to 10MB).
- XLSX ZIP metadata inspection before parsing (expansion ratio, entry count).
- Maximum row count enforcement for both CSV and XLSX.
- Strict validation rules mirroring preprocessing.ipynb.
- Text field maximum length enforcement (no silent truncation).
- Recalculation of derived revenue and order_month.
- In-file and MySQL duplicate transaction ID detection.
- Short-lived in-memory staging for preview tokens (15-minute expiration).
- Single-transaction atomic commit for import confirmation with rollback on failure.
"""

from __future__ import annotations

import io
import math
import re
import secrets
import threading
import time
import zipfile
from datetime import datetime, date
from typing import Any

import pandas as pd
from database import get_db_connection

# Maximum file size allowed for ingestion (10 MB).
# Enforced by streaming read in the FastAPI route before this module is called.
MAX_FILE_SIZE_BYTES = 10 * 1024 * 1024

# Maximum number of data rows allowed per upload (headers excluded).
MAX_ROWS = 5_000

# XLSX ZIP safety limits
# Maximum total uncompressed size across all ZIP entries (50 MB).
MAX_XLSX_UNCOMPRESSED_BYTES = 50 * 1024 * 1024
# Maximum number of ZIP entries (prevents crafted archives with thousands of tiny files).
MAX_XLSX_ZIP_ENTRIES = 1_024
# Maximum expansion ratio (uncompressed / compressed).  Normal XLSX files sit
# well below 20:1; a ratio above 100 is a strong indicator of a zip bomb.
MAX_XLSX_EXPANSION_RATIO = 100

# Staging preview TTL (15 minutes in seconds)
PREVIEW_EXPIRATION_SECONDS = 15 * 60

# Required input columns (case-insensitive mapping target)
REQUIRED_COLUMNS = [
    "transaction_id",
    "order_date",
    "product",
    "category",
    "region",
    "sales_channel",
    "quantity",
    "unit_price",
    "discount_amount",
    "status",
]

# Maximum character lengths for text fields.
# Each limit matches the actual MySQL column width exactly so that oversized
# values are rejected at the validation layer before reaching the database.
TEXT_FIELD_MAX_LENGTHS: dict[str, int] = {
    "transaction_id": 20,   # matches MySQL VARCHAR(20)
    "product": 100,         # matches MySQL VARCHAR(100)
    "category": 100,        # matches MySQL VARCHAR(100)
    "region": 50,           # matches MySQL VARCHAR(50)
    "sales_channel": 30,    # matches MySQL VARCHAR(30)
    "status": 30,           # matches MySQL VARCHAR(30)
}

# Thread-safe in-memory staging store
_STAGING_LOCK = threading.Lock()
_STAGING_STORE: dict[str, dict[str, Any]] = {}


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _cleanup_expired_previews() -> None:
    """Remove expired preview sessions from memory."""
    now = time.time()
    with _STAGING_LOCK:
        expired_keys = [
            token
            for token, data in _STAGING_STORE.items()
            if data.get("expires_at", 0) <= now
        ]
        for token in expired_keys:
            _STAGING_STORE.pop(token, None)


def _inspect_xlsx_zip(content: bytes) -> None:
    """Inspect XLSX ZIP metadata for signs of a zip bomb or suspicious archive.

    Checks three independent limits *before* handing the bytes to openpyxl:
    1. Number of ZIP entries — rejects archives with more than MAX_XLSX_ZIP_ENTRIES entries.
    2. Total uncompressed size — rejects archives whose members would expand beyond
       MAX_XLSX_UNCOMPRESSED_BYTES when decompressed.
    3. Expansion ratio — rejects archives where uncompressed/compressed > MAX_XLSX_EXPANSION_RATIO.

    Raises:
        ValueError: If any limit is exceeded or if the ZIP is structurally invalid.
    """
    try:
        with zipfile.ZipFile(io.BytesIO(content)) as zf:
            entries = zf.infolist()
    except zipfile.BadZipFile as exc:
        raise ValueError("File content is not a valid Excel (.xlsx) file.") from exc

    if len(entries) > MAX_XLSX_ZIP_ENTRIES:
        raise ValueError(
            f"XLSX archive contains {len(entries)} entries, which exceeds the "
            f"maximum allowed limit of {MAX_XLSX_ZIP_ENTRIES}."
        )

    total_compressed = sum(e.compress_size for e in entries)
    total_uncompressed = sum(e.file_size for e in entries)

    if total_uncompressed > MAX_XLSX_UNCOMPRESSED_BYTES:
        uncompressed_mb = total_uncompressed / (1024 * 1024)
        raise ValueError(
            f"XLSX archive would expand to {uncompressed_mb:.1f} MB uncompressed, "
            f"exceeding the {MAX_XLSX_UNCOMPRESSED_BYTES // (1024 * 1024)} MB limit."
        )

    if total_compressed > 0:
        ratio = total_uncompressed / total_compressed
        if ratio > MAX_XLSX_EXPANSION_RATIO:
            raise ValueError(
                f"XLSX archive has a suspicious compression ratio of {ratio:.0f}:1. "
                f"Upload rejected for safety."
            )


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def parse_and_validate_file(
    content: bytes,
    filename: str,
) -> dict[str, Any]:
    """Parse CSV or Excel bytes, apply validation, check duplicates, and stage valid records.

    Raises:
        ValueError: If file is oversized, format is unsupported, malformed,
                    missing required headers, or contains too many rows.
    """
    if len(content) > MAX_FILE_SIZE_BYTES:
        raise ValueError(
            f"File size exceeds maximum allowed limit of {MAX_FILE_SIZE_BYTES // (1024 * 1024)} MB."
        )

    if not content:
        raise ValueError("Uploaded file is empty.")

    lower_name = (filename or "").lower().strip()
    is_csv = lower_name.endswith(".csv")
    is_xlsx = lower_name.endswith(".xlsx")

    if not (is_csv or is_xlsx):
        raise ValueError("Unsupported file format. Please upload a .csv or .xlsx file.")

    df: pd.DataFrame
    try:
        if is_csv:
            try:
                decoded_str = content.decode("utf-8-sig")
            except UnicodeDecodeError:
                decoded_str = content.decode("latin-1")
            df = pd.read_csv(io.StringIO(decoded_str), dtype=str)
        else:
            # Validate magic bytes first
            if not content.startswith(b"PK\x03\x04"):
                raise ValueError("File content is not a valid Excel (.xlsx) file.")
            # Inspect ZIP metadata for zip-bomb indicators before handing to openpyxl
            _inspect_xlsx_zip(content)
            df = pd.read_excel(io.BytesIO(content), dtype=str, engine="openpyxl")
    except ValueError:
        raise
    except Exception as error:
        raise ValueError(f"Malformed or unreadable file: {str(error)}") from error

    if df.empty:
        raise ValueError("The uploaded sheet contains no data rows.")

    # Enforce maximum row count (headers are not counted by pandas)
    if len(df) > MAX_ROWS:
        raise ValueError(
            f"Uploaded file contains {len(df):,} data rows, which exceeds the "
            f"maximum allowed limit of {MAX_ROWS:,} rows per upload."
        )

    # Normalize column names: strip spaces, lowercase
    raw_columns = list(df.columns)
    column_mapping: dict[str, str] = {}
    for col in raw_columns:
        norm = str(col).strip().lower()
        column_mapping[col] = norm
    df = df.rename(columns=column_mapping)

    # Check for missing required columns
    missing_cols = [col for col in REQUIRED_COLUMNS if col not in df.columns]
    if missing_cols:
        missing_display = ", ".join(sorted(missing_cols))
        raise ValueError(f"Uploaded file is missing required column(s): {missing_display}")

    # Text sanitation: replace NaN/None/empty with None; keep raw strings intact
    for col in REQUIRED_COLUMNS:
        df[col] = df[col].apply(
            lambda v: None if pd.isna(v) or str(v).strip().lower() in ("nan", "none", "", "null") else str(v).strip()
        )

    # Fetch existing transaction IDs from MySQL to detect database collisions
    existing_db_ids: set[str] = set()
    raw_id_list = [
        str(v).strip()
        for v in df["transaction_id"].dropna().tolist()
        if str(v).strip()
    ]
    if raw_id_list:
        connection = get_db_connection()
        cursor = connection.cursor(dictionary=True)
        try:
            # Query in batches of 500
            for i in range(0, len(raw_id_list), 500):
                batch = raw_id_list[i : i + 500]
                placeholders = ", ".join(["%s"] * len(batch))
                cursor.execute(
                    f"SELECT transaction_id FROM sales_transactions WHERE transaction_id IN ({placeholders})",
                    tuple(batch),
                )
                for row in cursor.fetchall():
                    existing_db_ids.add(row["transaction_id"])
        finally:
            cursor.close()
            connection.close()

    # Track seen transaction IDs in file for in-file duplicate detection
    seen_file_ids: set[str] = set()
    in_file_duplicate_ids: set[str] = set()

    total_rows = len(df)
    valid_records: list[dict[str, Any]] = []
    rejected_records: list[dict[str, Any]] = []

    for idx, row in df.iterrows():
        row_num = int(idx) + 1  # 1-indexed row number
        rejection_reasons: list[str] = []

        # 1. Missing required fields check
        missing_fields = [
            col for col in REQUIRED_COLUMNS
            if row.get(col) is None or pd.isna(row.get(col)) or str(row.get(col)).strip() == ""
        ]
        if missing_fields:
            rejection_reasons.append(f"Missing required field(s): {', '.join(missing_fields)}")

        raw_id = row.get("transaction_id")

        # 2. Text field length validation (applied per-field with specific messages)
        for field, max_len in TEXT_FIELD_MAX_LENGTHS.items():
            field_val = row.get(field)
            if field_val is not None and len(str(field_val)) > max_len:
                rejection_reasons.append(
                    f"Field '{field}' exceeds maximum length of {max_len} characters "
                    f"(got {len(str(field_val))})"
                )

        if raw_id is not None:
            # 3. Database collision check
            if raw_id in existing_db_ids:
                rejection_reasons.append("Transaction ID already exists in database")

            # 4. In-file duplicate check
            if raw_id in seen_file_ids:
                in_file_duplicate_ids.add(raw_id)
                rejection_reasons.append("Duplicate Transaction ID within uploaded file")
            else:
                seen_file_ids.add(raw_id)

        # 5. Order date parsing (supports ISO YYYY-MM-DD or DD-MM-YYYY)
        raw_date = row.get("order_date")
        parsed_date: date | None = None
        order_month: str | None = None
        if raw_date is not None:
            parsed_date = _parse_order_date(raw_date)
            if parsed_date is None:
                rejection_reasons.append("Invalid order date (expected YYYY-MM-DD or DD-MM-YYYY)")
            else:
                order_month = parsed_date.strftime("%Y-%m")

        # 6. Quantity validation (positive integer)
        raw_qty = row.get("quantity")
        parsed_qty: int | None = None
        if raw_qty is not None:
            try:
                qty_float = float(raw_qty)
                if not qty_float.is_integer() or qty_float <= 0:
                    rejection_reasons.append("Quantity must be a positive integer")
                else:
                    parsed_qty = int(qty_float)
            except (ValueError, TypeError):
                rejection_reasons.append("Quantity must be a positive integer")

        # 7. Unit price validation (greater than zero)
        raw_price = row.get("unit_price")
        parsed_price: float | None = None
        if raw_price is not None:
            try:
                price_val = float(raw_price)
                if math.isnan(price_val) or price_val <= 0:
                    rejection_reasons.append("Unit price must be greater than zero")
                else:
                    parsed_price = round(price_val, 2)
            except (ValueError, TypeError):
                rejection_reasons.append("Unit price must be a valid number greater than zero")

        # 8. Discount amount validation (non-negative, <= quantity * unit_price)
        raw_discount = row.get("discount_amount")
        parsed_discount: float | None = None
        if raw_discount is not None:
            try:
                discount_val = float(raw_discount)
                if math.isnan(discount_val) or discount_val < 0:
                    rejection_reasons.append("Discount amount cannot be negative")
                else:
                    parsed_discount = round(discount_val, 2)
            except (ValueError, TypeError):
                rejection_reasons.append("Discount amount must be a valid non-negative number")

        # Gross amount comparison if qty and price are valid
        if parsed_qty is not None and parsed_price is not None and parsed_discount is not None:
            gross = round(parsed_qty * parsed_price, 2)
            if parsed_discount > gross:
                rejection_reasons.append("Discount amount exceeds gross amount (quantity * unit price)")

        # 9. Status check: Only 'Completed' transactions are accepted
        raw_status = row.get("status")
        if raw_status is not None:
            if raw_status.strip().lower() != "completed":
                rejection_reasons.append("Transaction not completed")

        # If any validation failed, record as rejected
        if rejection_reasons:
            safe_id = str(raw_id) if raw_id is not None and not pd.isna(raw_id) else "UNKNOWN"
            rejected_records.append({
                "row_number": row_num,
                "transaction_id": safe_id,
                "rejection_reasons": rejection_reasons,
            })
        else:
            # Server-side calculation of revenue
            calc_revenue = round((parsed_qty * parsed_price) - parsed_discount, 2)

            valid_records.append({
                "transaction_id": raw_id,
                "order_date": parsed_date.strftime("%Y-%m-%d"),
                "product": row["product"],
                "category": row["category"],
                "region": row["region"],
                "sales_channel": row["sales_channel"],
                "quantity": parsed_qty,
                "unit_price": parsed_price,
                "discount_amount": parsed_discount,
                "status": "Completed",
                "revenue": calc_revenue,
                "order_month": order_month,
            })

    # Summary calculations
    valid_count = len(valid_records)
    invalid_count = len(rejected_records)
    projected_revenue = round(sum(r["revenue"] for r in valid_records), 2)
    projected_quantity = sum(r["quantity"] for r in valid_records)

    # Stage valid records
    _cleanup_expired_previews()
    preview_id = secrets.token_urlsafe(32)
    expires_at = time.time() + PREVIEW_EXPIRATION_SECONDS

    with _STAGING_LOCK:
        _STAGING_STORE[preview_id] = {
            "created_at": time.time(),
            "expires_at": expires_at,
            "valid_records": valid_records,
            "filename": filename,
            "total_rows": total_rows,
            "valid_count": valid_count,
            "projected_revenue": projected_revenue,
            "projected_quantity": projected_quantity,
        }

    return {
        "preview_id": preview_id,
        "expires_in_seconds": PREVIEW_EXPIRATION_SECONDS,
        "summary": {
            "total_rows": total_rows,
            "valid_rows_count": valid_count,
            "invalid_rows_count": invalid_count,
            "existing_db_duplicates_count": len(
                [
                    r
                    for r in rejected_records
                    if "Transaction ID already exists in database" in r["rejection_reasons"]
                ]
            ),
            "in_file_duplicates_count": len(in_file_duplicate_ids),
            "projected_revenue": projected_revenue,
            "projected_quantity": projected_quantity,
        },
        "valid_records_preview": valid_records[:10],
        "rejected_records": rejected_records,
    }


def _parse_order_date(raw_date_str: str) -> date | None:
    """Parse date string supporting YYYY-MM-DD or DD-MM-YYYY."""
    s = raw_date_str.strip()
    # Try ISO YYYY-MM-DD
    try:
        return datetime.strptime(s, "%Y-%m-%d").date()
    except ValueError:
        pass

    # Try DD-MM-YYYY
    try:
        return datetime.strptime(s, "%d-%m-%Y").date()
    except ValueError:
        pass

    # Try timestamp string (e.g., from Excel read)
    try:
        return pd.to_datetime(s).date()
    except Exception:
        return None


def confirm_import(preview_id: str) -> dict[str, Any]:
    """Execute atomic batch insert for a valid staged preview.

    Raises:
        ValueError: If preview_id is unknown, expired, has no records, or collisions occurred.
        RuntimeError: If database insertion fails and rolls back.
    """
    _cleanup_expired_previews()

    with _STAGING_LOCK:
        staging_entry = _STAGING_STORE.pop(preview_id, None)

    if staging_entry is None:
        raise ValueError("Invalid, expired, or already confirmed preview identifier.")

    if staging_entry.get("expires_at", 0) <= time.time():
        raise ValueError("Preview session has expired. Please upload the file again.")

    valid_records = staging_entry.get("valid_records", [])
    if not valid_records:
        raise ValueError("Preview contains zero valid records to import.")

    # Atomic DB insertion with immediate collision check
    connection = get_db_connection()
    connection.autocommit = False
    cursor = connection.cursor(dictionary=True)

    try:
        # Re-check transaction IDs immediately within transaction
        candidate_ids = [r["transaction_id"] for r in valid_records]
        placeholders = ", ".join(["%s"] * len(candidate_ids))
        cursor.execute(
            f"SELECT transaction_id FROM sales_transactions WHERE transaction_id IN ({placeholders})",
            tuple(candidate_ids),
        )
        existing = {row["transaction_id"] for row in cursor.fetchall()}
        if existing:
            raise ValueError(
                f"Cannot import: {len(existing)} transaction ID(s) were recently committed to database: "
                f"{', '.join(sorted(existing)[:5])}"
            )

        insert_query = """
        INSERT INTO sales_transactions (
            transaction_id,
            order_date,
            product,
            category,
            region,
            sales_channel,
            quantity,
            unit_price,
            discount_amount,
            status,
            revenue,
            order_month
        )
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
        """

        records_tuples = [
            (
                r["transaction_id"],
                r["order_date"],
                r["product"],
                r["category"],
                r["region"],
                r["sales_channel"],
                r["quantity"],
                r["unit_price"],
                r["discount_amount"],
                r["status"],
                r["revenue"],
                r["order_month"],
            )
            for r in valid_records
        ]

        cursor.executemany(insert_query, records_tuples)
        connection.commit()

        imported_count = len(valid_records)
        imported_revenue = round(sum(r["revenue"] for r in valid_records), 2)
        imported_quantity = sum(r["quantity"] for r in valid_records)

        return {
            "status": "success",
            "imported_count": imported_count,
            "imported_revenue": imported_revenue,
            "imported_quantity": imported_quantity,
            "message": f"Successfully imported {imported_count} sales transactions.",
        }

    except Exception as error:
        connection.rollback()
        if isinstance(error, ValueError):
            raise
        raise RuntimeError(f"Database insertion failed and was rolled back: {error}") from error

    finally:
        cursor.close()
        connection.close()
