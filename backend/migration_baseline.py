"""Read-only baseline report for the InsightIQ sales_transactions table.

Run from the repository root:

    python backend/migration_baseline.py
    python backend/migration_baseline.py --json backend/migrations/baseline.json

The module only executes SELECT statements. It does not create, alter, update,
delete, or insert database objects or records.
"""

from __future__ import annotations

import argparse
import json
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any
from collections.abc import Mapping

from database import get_db_connection


QUERIES = {
    "schema": """
        SELECT column_name, data_type, column_type, is_nullable, column_key
        FROM information_schema.columns
        WHERE table_schema = DATABASE()
          AND table_name = 'sales_transactions'
        ORDER BY ordinal_position
    """,
    "summary": """
        SELECT
            COUNT(*) AS total_transactions,
            COALESCE(SUM(revenue), 0) AS total_revenue,
            COALESCE(SUM(quantity), 0) AS total_quantity,
            COALESCE(AVG(revenue), 0) AS average_revenue_per_transaction
        FROM sales_transactions
    """,
    "transaction_bounds": """
        SELECT
            MIN(transaction_id) AS minimum_transaction_id,
            MAX(transaction_id) AS maximum_transaction_id,
            MIN(order_date) AS minimum_order_date,
            MAX(order_date) AS maximum_order_date
        FROM sales_transactions
    """,
    "revenue_by_product": """
        SELECT product, SUM(revenue) AS total_revenue
        FROM sales_transactions
        GROUP BY product
        ORDER BY total_revenue DESC, product
    """,
    "revenue_by_region": """
        SELECT region, SUM(revenue) AS total_revenue
        FROM sales_transactions
        GROUP BY region
        ORDER BY total_revenue DESC, region
    """,
    "revenue_by_month": """
        SELECT order_month, SUM(revenue) AS total_revenue
        FROM sales_transactions
        GROUP BY order_month
        ORDER BY order_month
    """,
    "revenue_by_sales_channel": """
        SELECT sales_channel, SUM(revenue) AS total_revenue
        FROM sales_transactions
        GROUP BY sales_channel
        ORDER BY total_revenue DESC, sales_channel
    """,
}


def _json_value(value: Any) -> Any:
    """Convert MySQL numeric/date values to JSON-compatible values."""
    if isinstance(value, Decimal):
        return format(value, "f")
    if isinstance(value, (date, datetime)):
        return value.isoformat()
    return value


def _normalize_rows(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [
        {key: _json_value(value) for key, value in row.items()}
        for row in rows
    ]


def _normalize_schema_rows(rows: list[Mapping[str, Any]]) -> list[dict[str, Any]]:
    """Canonicalize dictionary-cursor schema keys regardless of key casing."""
    normalized_rows = []
    for row in rows:
        values = {
            str(key).casefold(): _json_value(value)
            for key, value in row.items()
        }
        normalized_rows.append(
            {
                "column_name": values.get("column_name"),
                "data_type": values.get("data_type"),
                "column_type": values.get("column_type"),
                "is_nullable": values.get("is_nullable"),
                "column_key": values.get("column_key"),
            }
        )
    return normalized_rows


def collect_baseline(connection: Any) -> dict[str, Any]:
    """Collect all baseline values using SELECT-only queries."""
    cursor = connection.cursor(dictionary=True)
    try:
        baseline: dict[str, Any] = {}
        for name, query in QUERIES.items():
            cursor.execute(query)
            if name == "schema":
                baseline[name] = _normalize_schema_rows(cursor.fetchall())
            elif name in {"revenue_by_product", "revenue_by_region",
                          "revenue_by_month", "revenue_by_sales_channel"}:
                baseline[name] = _normalize_rows(cursor.fetchall())
            else:
                row = cursor.fetchone() or {}
                baseline[name] = {
                    key: _json_value(value) for key, value in row.items()
                }
        return baseline
    finally:
        cursor.close()


def format_baseline(baseline: dict[str, Any]) -> str:
    """Render a baseline in a readable, stable text format."""
    summary = baseline["summary"]
    bounds = baseline["transaction_bounds"]
    lines = [
        "InsightIQ migration baseline",
        "============================",
        f"Total transactions: {summary.get('total_transactions', 0)}",
        f"Total revenue: {summary.get('total_revenue', 0)}",
        f"Total quantity: {summary.get('total_quantity', 0)}",
        f"Average revenue per transaction: "
        f"{summary.get('average_revenue_per_transaction', 0)}",
        f"Minimum transaction ID: {bounds.get('minimum_transaction_id')}",
        f"Maximum transaction ID: {bounds.get('maximum_transaction_id')}",
        f"Minimum order date: {bounds.get('minimum_order_date')}",
        f"Maximum order date: {bounds.get('maximum_order_date')}",
    ]

    if baseline.get("schema"):
        lines.extend(["", "Schema columns:", "---------------"])
        for column in baseline["schema"]:
            lines.append(
                f"{column.get('column_name')}: {column.get('column_type')} "
                f"(nullable={column.get('is_nullable')}, key={column.get('column_key')})"
            )

    for title, key, label in (
        ("Revenue by product", "revenue_by_product", "product"),
        ("Revenue by region", "revenue_by_region", "region"),
        ("Revenue by month", "revenue_by_month", "order_month"),
        ("Revenue by sales channel", "revenue_by_sales_channel", "sales_channel"),
    ):
        lines.extend(["", f"{title}:", "-" * len(title)])
        for row in baseline[key]:
            lines.append(f"{row.get(label)}: {row.get('total_revenue')}")

    return "\n".join(lines)


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--json",
        dest="json_path",
        type=Path,
        help="Also save the JSON baseline to this path.",
    )
    return parser.parse_args()


def main() -> int:
    args = _parse_args()
    connection = get_db_connection()
    try:
        baseline = collect_baseline(connection)
    finally:
        connection.close()

    print(format_baseline(baseline))
    if args.json_path:
        args.json_path.parent.mkdir(parents=True, exist_ok=True)
        args.json_path.write_text(
            json.dumps(baseline, indent=2, ensure_ascii=False) + "\n",
            encoding="utf-8",
        )
        print(f"\nJSON baseline saved to: {args.json_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
