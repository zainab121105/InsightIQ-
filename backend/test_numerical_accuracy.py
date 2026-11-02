"""
Tests for numerical accuracy improvements: INR formatting, pre-formatted context,
partial month awareness, backend-as-source-of-truth, and tenant-specific analytics.

Run with:
    python -m pytest test_numerical_accuracy.py -v
"""

import datetime
import json
import os
import sys
import unittest
from decimal import Decimal
from unittest.mock import MagicMock, patch

sys.path.insert(0, os.path.dirname(__file__))
from auth import AuthenticatedBusinessContext

BUSINESS_CONTEXT = AuthenticatedBusinessContext(
    user_id=1,
    business_id=101,
    role="owner",
    session_id="00000000-0000-4000-8000-000000000001",
)

BUSINESS_B_CONTEXT = AuthenticatedBusinessContext(
    user_id=2,
    business_id=202,
    role="member",
    session_id="00000000-0000-4000-8000-000000000002",
)

PATCH_CONN = "analytics.get_db_connection"


# ============================================================
# 1. INR Formatting Tests
# ============================================================

class TestINRCurrencyFormatting(unittest.TestCase):
    """Verify _format_inr_currency produces correct Indian digit grouping."""

    def setUp(self):
        import chat
        self.format = chat._format_inr_currency

    def test_correct_formatting_1479650(self):
        """The Legacy baseline: 1479650 must format to ₹14,79,650."""
        result = self.format(1479650)
        self.assertEqual(result, "₹14,79,650")

    def test_prevents_incorrect_14796650(self):
        """14796650 (the wrong value) formats to ₹1,47,96,650 which is the reported bug."""
        result = self.format(14796650)
        self.assertEqual(result, "₹1,47,96,650")
        # Confirm 1479650 and 14796650 produce different results
        correct = self.format(1479650)
        incorrect = self.format(14796650)
        self.assertNotEqual(correct, incorrect)
        self.assertEqual(correct, "₹14,79,650")

    def test_small_amounts(self):
        self.assertEqual(self.format(100), "₹100")
        self.assertEqual(self.format(999), "₹999")

    def test_thousands(self):
        self.assertEqual(self.format(1000), "₹1,000")
        self.assertEqual(self.format(9999), "₹9,999")

    def test_lakhs(self):
        self.assertEqual(self.format(100000), "₹1,00,000")
        self.assertEqual(self.format(189000), "₹1,89,000")
        self.assertEqual(self.format(411200), "₹4,11,200")

    def test_crores(self):
        self.assertEqual(self.format(10000000), "₹1,00,00,000")
        self.assertEqual(self.format(15063000), "₹1,50,63,000")

    def test_zero(self):
        self.assertEqual(self.format(0), "₹0")

    def test_float_rounding(self):
        self.assertEqual(self.format(1479650.0), "₹14,79,650")
        self.assertEqual(self.format(1479650.49), "₹14,79,650")
        # Python's round() uses banker's rounding; 1479650.5 rounds to 1479650
        self.assertEqual(self.format(1479650.5), "₹14,79,650")
        self.assertEqual(self.format(1479650.6), "₹14,79,651")

    def test_decimal_input(self):
        """Ensure Decimal values work correctly."""
        self.assertEqual(self.format(Decimal("1479650")), "₹14,79,650")
        self.assertEqual(self.format(Decimal("1479650.00")), "₹14,79,650")

    def test_decimal_values_are_preformatted_in_context(self):
        import chat
        result = chat._preformat_context_values({
            "get_sales_summary": {
                "total_revenue": Decimal("1479650.00"),
                "average_revenue": Decimal("29593.00"),
            }
        })
        self.assertEqual(result["formatted_total_revenue"], "₹14,79,650")


# ============================================================
# 2. Pre-formatted Context Tests
# ============================================================

class TestPreformattedContextValues(unittest.TestCase):
    """Verify _preformat_context_values adds correctly formatted currency strings."""

    def setUp(self):
        import chat
        self.preformat = chat._preformat_context_values
        self.format = chat._format_inr_currency

    def test_formats_summary_revenue(self):
        context = {
            "get_sales_summary": {
                "total_transactions": 50,
                "total_quantity": 598,
                "total_revenue": 1479650.0,
                "average_revenue": 29593.0,
            }
        }
        result = self.preformat(context)
        self.assertEqual(result["formatted_total_revenue"], "₹14,79,650")
        self.assertEqual(result["formatted_average_revenue"], "₹29,593")
        self.assertEqual(result["formatted_total_transactions"], "50")
        self.assertEqual(result["formatted_total_quantity"], "598")

    def test_formats_product_revenue(self):
        context = {
            "get_product_revenue": [
                {"product": "Monitor", "total_revenue": 517800.0},
                {"product": "Desk", "total_revenue": 222500.0},
            ]
        }
        result = self.preformat(context)
        self.assertEqual(len(result["formatted_product_revenue"]), 2)
        self.assertEqual(
            result["formatted_product_revenue"][0]["formatted_revenue"],
            "₹5,17,800",
        )
        self.assertEqual(
            result["formatted_product_revenue"][1]["formatted_revenue"],
            "₹2,22,500",
        )

    def test_formats_region_revenue(self):
        context = {
            "get_region_revenue": [
                {"region": "South", "total_revenue": 478200.0},
                {"region": "North", "total_revenue": 417150.0},
            ]
        }
        result = self.preformat(context)
        self.assertEqual(len(result["formatted_region_revenue"]), 2)
        self.assertEqual(
            result["formatted_region_revenue"][0]["formatted_revenue"],
            "₹4,78,200",
        )

    def test_formats_monthly_revenue(self):
        context = {
            "get_monthly_revenue": [
                {"order_month": "2026-01", "total_revenue": 210250.0},
                {"order_month": "2026-07", "total_revenue": 45000.0},
            ]
        }
        result = self.preformat(context)
        self.assertEqual(len(result["formatted_monthly_revenue"]), 2)
        self.assertEqual(
            result["formatted_monthly_revenue"][0]["formatted_revenue"],
            "₹2,10,250",
        )

    def test_handles_missing_data_gracefully(self):
        """No crash when keys are missing."""
        result = self.preformat({})
        self.assertNotIn("formatted_total_revenue", result)
        self.assertNotIn("formatted_product_revenue", result)

    def test_handles_none_values(self):
        """No crash when summary has None values."""
        context = {
            "get_sales_summary": {
                "total_transactions": 0,
                "total_quantity": None,
                "total_revenue": None,
                "average_revenue": None,
            }
        }
        result = self.preformat(context)
        self.assertNotIn("formatted_total_revenue", result)

    def test_preserves_original_numeric_values(self):
        """Pre-formatting must not alter the original numeric values."""
        context = {
            "get_sales_summary": {
                "total_transactions": 50,
                "total_quantity": 598,
                "total_revenue": 1479650.0,
                "average_revenue": 29593.0,
            }
        }
        result = self.preformat(context)
        # The original summary must still be present with numeric values
        summary = result["get_sales_summary"]
        self.assertEqual(summary["total_revenue"], 1479650.0)
        self.assertEqual(summary["total_transactions"], 50)

    def test_product_aggregate_consistency(self):
        """Sum of pre-formatted product revenues should match total revenue."""
        product_rows = [
            {"product": "Monitor", "total_revenue": 517800.0},
            {"product": "Office Chair", "total_revenue": 179400.0},
            {"product": "Desk", "total_revenue": 222500.0},
            {"product": "USB-C Hub", "total_revenue": 163800.0},
            {"product": "Wireless Mouse", "total_revenue": 147500.0},
            {"product": "Keyboard", "total_revenue": 124200.0},
            {"product": "Laptop Stand", "total_revenue": 84600.0},
        ]
        total = sum(r["total_revenue"] for r in product_rows)
        context = {
            "get_sales_summary": {
                "total_transactions": 50,
                "total_quantity": 598,
                "total_revenue": total,
                "average_revenue": total / 50,
            },
            "get_product_revenue": product_rows,
        }
        result = self.preformat(context)
        # Verify the formatted total matches the sum
        self.assertEqual(
            result["formatted_total_revenue"],
            self.format(total),
        )
        # Verify each product formatted value
        for i, row in enumerate(product_rows):
            self.assertEqual(
                result["formatted_product_revenue"][i]["formatted_revenue"],
                self.format(row["total_revenue"]),
            )

    def test_regional_aggregate_consistency(self):
        """Sum of region revenues should be verifiable against the total."""
        region_rows = [
            {"region": "North", "total_revenue": 417150.0},
            {"region": "South", "total_revenue": 478200.0},
            {"region": "East", "total_revenue": 332200.0},
            {"region": "West", "total_revenue": 278750.0},
        ]
        context = {"get_region_revenue": region_rows}
        result = self.preformat(context)
        # Each region formatted value should be correct
        for i, row in enumerate(region_rows):
            self.assertEqual(
                result["formatted_region_revenue"][i]["formatted_revenue"],
                self.format(row["total_revenue"]),
            )


# ============================================================
# 3. Partial Month Awareness Tests
# ============================================================

class TestPartialMonthAwareness(unittest.TestCase):
    """Verify _identify_partial_months correctly flags incomplete data periods."""

    def setUp(self):
        import chat
        self.identify = chat._identify_partial_months

    def test_july_5_flagged_as_partial(self):
        """July 2026 with max date July 5 must be flagged as partial."""
        date_range = {
            "min_order_date": "2026-01-03",
            "max_order_date": "2026-07-05",
        }
        monthly_rows = [
            {"order_month": "2026-01", "total_revenue": 210250.0},
            {"order_month": "2026-02", "total_revenue": 233600.0},
            {"order_month": "2026-07", "total_revenue": 45000.0},
        ]
        result = self.identify(date_range, monthly_rows)
        partial_months = [p["month"] for p in result["partial_months"]]
        self.assertIn("2026-07", partial_months)
        # Check the note mentions incomplete data
        self.assertIn("incomplete data", result["note"])

    def test_complete_months_identified(self):
        """Months that are not partial should be listed as complete."""
        date_range = {
            "min_order_date": "2026-01-01",
            "max_order_date": "2026-07-05",
        }
        monthly_rows = [
            {"order_month": "2026-01", "total_revenue": 210250.0},
            {"order_month": "2026-02", "total_revenue": 233600.0},
            {"order_month": "2026-03", "total_revenue": 255900.0},
            {"order_month": "2026-07", "total_revenue": 45000.0},
        ]
        result = self.identify(date_range, monthly_rows)
        # Jan starts on the 1st so should not be partial from first-month check
        # Feb and Mar should be complete
        self.assertIn("2026-02", result["complete_months"])
        self.assertIn("2026-03", result["complete_months"])

    def test_first_month_partial_when_not_starting_on_day_1(self):
        """If earliest order is Jan 3, January should be flagged partial."""
        date_range = {
            "min_order_date": "2026-01-03",
            "max_order_date": "2026-06-30",
        }
        monthly_rows = [
            {"order_month": "2026-01", "total_revenue": 210250.0},
        ]
        result = self.identify(date_range, monthly_rows)
        partial_months = [p["month"] for p in result["partial_months"]]
        self.assertIn("2026-01", partial_months)

    def test_none_date_range_handled(self):
        """None date range should not crash."""
        result = self.identify(None, [])
        self.assertEqual(result["partial_months"], [])
        self.assertIsNone(result["note"])

    def test_empty_date_range_handled(self):
        """Empty dict should not crash."""
        result = self.identify({"min_order_date": None, "max_order_date": None}, [])
        self.assertEqual(result["partial_months"], [])

    def test_note_mentions_do_not_describe_as_decline(self):
        """The note should warn against interpreting partial months as declines."""
        date_range = {
            "min_order_date": "2026-01-03",
            "max_order_date": "2026-07-05",
        }
        monthly_rows = [
            {"order_month": "2026-07", "total_revenue": 45000.0},
        ]
        result = self.identify(date_range, monthly_rows)
        self.assertIn("decline", result["note"].lower())

    def test_february_28_is_complete_in_a_non_leap_year(self):
        result = self.identify(
            {
                "min_order_date": "2026-01-01",
                "max_order_date": "2026-02-28",
            },
            [{"order_month": "2026-02", "total_revenue": 100.0}],
        )
        self.assertEqual(result["partial_months"], [])
        self.assertIn("2026-02", result["complete_months"])


# ============================================================
# 4. Backend Metrics in AI Context Tests
# ============================================================

class TestBackendMetricsInAIContext(unittest.TestCase):
    """Verify that _compact_final_answer_context includes backend-provided
    pre-formatted values, formatting rules, and date range."""

    def _make_business_data(self):
        return {
            "get_sales_summary": {
                "total_transactions": 50,
                "total_quantity": 598,
                "total_revenue": 1479650.0,
                "average_revenue": 29593.0,
            },
            "get_product_revenue": [
                {"product": "Monitor", "total_revenue": 517800.0},
                {"product": "Desk", "total_revenue": 222500.0},
            ],
            "get_region_revenue": [
                {"region": "South", "total_revenue": 478200.0},
                {"region": "North", "total_revenue": 417150.0},
            ],
            "get_monthly_revenue": [
                {"order_month": "2026-01", "total_revenue": 210250.0},
                {"order_month": "2026-07", "total_revenue": 45000.0},
            ],
        }

    def test_context_contains_formatted_total_revenue(self):
        import chat
        context_token = chat._active_business_context.set(BUSINESS_CONTEXT)
        try:
            with patch.object(chat, "_call_analytics", side_effect=RuntimeError("skip")):
                result = chat._compact_final_answer_context(
                    "What is total revenue?",
                    self._make_business_data(),
                    None,
                )
        finally:
            chat._active_business_context.reset(context_token)
        self.assertEqual(result["formatted_total_revenue"], "₹14,79,650")

    def test_context_contains_formatting_rules(self):
        import chat
        context_token = chat._active_business_context.set(BUSINESS_CONTEXT)
        try:
            with patch.object(chat, "_call_analytics", side_effect=RuntimeError("skip")):
                result = chat._compact_final_answer_context(
                    "Show revenue summary",
                    self._make_business_data(),
                    None,
                )
        finally:
            chat._active_business_context.reset(context_token)
        self.assertIn("formatting_rules", result)
        rules = result["formatting_rules"]
        self.assertTrue(rules["use_preformatted_values"])
        self.assertIn("exactly as provided", rules["instruction"].lower())

    def test_context_includes_date_range_when_available(self):
        import chat
        mock_date_range = {
            "min_order_date": "2026-01-03",
            "max_order_date": "2026-07-05",
        }
        context_token = chat._active_business_context.set(BUSINESS_CONTEXT)
        try:
            with patch.object(
                chat,
                "_call_analytics",
                return_value=mock_date_range,
            ):
                result = chat._compact_final_answer_context(
                    "Show monthly trends",
                    self._make_business_data(),
                    None,
                )
        finally:
            chat._active_business_context.reset(context_token)
        self.assertIn("dataset_date_range", result)
        self.assertEqual(result["dataset_date_range"]["max_order_date"], "2026-07-05")
        self.assertIn("partial_month_analysis", result)

    def test_context_has_answer_sections(self):
        import chat
        context_token = chat._active_business_context.set(BUSINESS_CONTEXT)
        try:
            with patch.object(chat, "_call_analytics", side_effect=RuntimeError("skip")):
                result = chat._compact_final_answer_context(
                    "Revenue analysis",
                    self._make_business_data(),
                    None,
                )
        finally:
            chat._active_business_context.reset(context_token)
        sections = result["scope_rules"]["answer_sections"]
        self.assertEqual(
            sections,
            ["Observed Facts", "Calculated Insights", "Business Hypotheses", "Recommendations"],
        )

    def test_context_contains_verified_percentage_metrics(self):
        import chat
        business_data = {
            "get_sales_summary": {"total_revenue": 559200.0},
            "get_product_revenue": [
                {"product": "Monitor", "total_revenue": 318500.0},
                {"product": "Desk", "total_revenue": 240700.0},
            ],
            "get_region_revenue": [
                {"region": "North", "total_revenue": 559200.0},
            ],
            "get_monthly_revenue": [
                {"order_month": "2026-04", "total_revenue": 240700.0},
                {"order_month": "2026-05", "total_revenue": 318500.0},
            ],
        }
        context_token = chat._active_business_context.set(BUSINESS_CONTEXT)
        try:
            with patch.object(
                chat,
                "_call_analytics",
                return_value={
                    "min_order_date": "2026-04-01",
                    "max_order_date": "2026-05-31",
                },
            ):
                result = chat._compact_final_answer_context(
                    "Show monthly growth and revenue shares.",
                    business_data,
                    None,
                )
        finally:
            chat._active_business_context.reset(context_token)
        changes = result["verified_percentage_metrics"]["month_over_month_changes"]
        self.assertEqual(changes[0]["percentage_change"], 32.32)
        self.assertEqual(changes[0]["direction"], "growth")
        self.assertIn("verified_percentage_metrics", json.dumps(result))


# ============================================================
# 5. Tenant-Specific Analytics Context Tests
# ============================================================

class TestTenantSpecificAnalyticsContext(unittest.TestCase):
    """Verify analytics context is scoped to the authenticated business."""

    def test_date_range_scoped_to_business(self):
        """get_order_date_range must use the business_id from context."""
        import analytics

        cursor = MagicMock()
        cursor.fetchone.return_value = {
            "min_order_date": datetime.date(2026, 1, 3),
            "max_order_date": datetime.date(2026, 7, 5),
        }
        conn = MagicMock()
        conn.cursor.return_value = cursor

        with patch(PATCH_CONN, return_value=conn):
            result = analytics.get_order_date_range(BUSINESS_CONTEXT)

        # Verify the query used the correct business_id
        call_args = cursor.execute.call_args
        self.assertEqual(call_args[0][1], (BUSINESS_CONTEXT.business_id,))
        self.assertEqual(result["min_order_date"], "2026-01-03")
        self.assertEqual(result["max_order_date"], "2026-07-05")
        conn.close.assert_called_once()

    def test_date_range_different_per_tenant(self):
        """Two tenants should get different date ranges."""
        import analytics

        def make_conn(min_d, max_d):
            cursor = MagicMock()
            cursor.fetchone.return_value = {
                "min_order_date": min_d,
                "max_order_date": max_d,
            }
            conn = MagicMock()
            conn.cursor.return_value = cursor
            return conn

        conn_a = make_conn(datetime.date(2026, 1, 1), datetime.date(2026, 6, 30))
        conn_b = make_conn(datetime.date(2026, 3, 1), datetime.date(2026, 9, 15))

        with patch(PATCH_CONN, side_effect=[conn_a, conn_b]):
            result_a = analytics.get_order_date_range(BUSINESS_CONTEXT)
            result_b = analytics.get_order_date_range(BUSINESS_B_CONTEXT)

        self.assertNotEqual(result_a, result_b)
        self.assertEqual(result_a["max_order_date"], "2026-06-30")
        self.assertEqual(result_b["max_order_date"], "2026-09-15")


# ============================================================
# 6. Missing/Inconsistent Analytics Values Tests
# ============================================================

class TestMissingInconsistentAnalytics(unittest.TestCase):
    """Verify graceful handling when analytics data is missing or inconsistent."""

    def test_preformat_with_empty_product_list(self):
        import chat
        context = {
            "get_product_revenue": [],
        }
        result = chat._preformat_context_values(context)
        self.assertEqual(result.get("formatted_product_revenue"), [])

    def test_preformat_with_malformed_rows(self):
        import chat
        context = {
            "get_product_revenue": [
                {"product": "Monitor"},  # Missing total_revenue
                "not_a_dict",
            ],
        }
        result = chat._preformat_context_values(context)
        # Should skip all invalid rows (missing total_revenue or not a dict)
        self.assertEqual(len(result.get("formatted_product_revenue", [])), 0)

    def test_partial_months_with_invalid_dates(self):
        import chat
        result = chat._identify_partial_months(
            {"min_order_date": "not-a-date", "max_order_date": "also-not"},
            [],
        )
        # Should not crash, just return empty partials
        self.assertEqual(result["partial_months"], [])

    def test_offline_summary_without_date_range(self):
        """_format_offline_overall_summary should work even when date range is unavailable."""
        import chat
        context_token = chat._active_business_context.set(BUSINESS_CONTEXT)
        try:
            with patch.object(chat, "_call_analytics", side_effect=RuntimeError("DB down")):
                result = chat._format_offline_overall_summary({
                    "total_transactions": 50,
                    "total_quantity": 598,
                    "total_revenue": 1479650.0,
                    "average_revenue": 29593.0,
                })
        finally:
            chat._active_business_context.reset(context_token)
        self.assertIn("₹14,79,650", result)
        self.assertIn("Observed Facts", result)
        # Should NOT crash even without date range

    def test_consistent_breakdowns_are_verified(self):
        import chat
        result = chat._validate_analytics_context({
            "get_sales_summary": {
                "total_transactions": 2,
                "total_quantity": 3,
                "total_revenue": 300.0,
                "average_revenue": 150.0,
            },
            "get_product_revenue": [
                {"product": "A", "total_revenue": 100.0},
                {"product": "B", "total_revenue": 200.0},
            ],
            "get_region_revenue": [
                {"region": "North", "total_revenue": 300.0},
            ],
            "get_monthly_revenue": [
                {"order_month": "2026-01", "total_revenue": 300.0},
            ],
        })
        self.assertEqual(result["status"], "verified")
        self.assertEqual(result["issues"], [])

    def test_inconsistent_breakdown_is_unavailable_not_corrected(self):
        import chat
        result = chat._validate_analytics_context({
            "get_sales_summary": {
                "total_transactions": 2,
                "total_quantity": 3,
                "total_revenue": 300.0,
                "average_revenue": 150.0,
            },
            "get_product_revenue": [
                {"product": "A", "total_revenue": 100.0},
                {"product": "B", "total_revenue": 250.0},
            ],
        })
        self.assertEqual(result["status"], "inconsistent")
        self.assertIn("product revenue", result["unavailable_metrics"])
        self.assertEqual(result["issues"][0].startswith("product revenue"), True)

    def test_may_over_april_growth_is_32_32_percent(self):
        import chat
        result = chat._verified_percentage_metrics(
            {
                "get_sales_summary": {"total_revenue": 559200.0},
                "get_monthly_revenue": [
                    {"order_month": "2026-04", "total_revenue": 240700.0},
                    {"order_month": "2026-05", "total_revenue": 318500.0},
                ],
            }
        )
        self.assertEqual(
            result["month_over_month_changes"][0]["percentage_change"],
            32.32,
        )

    def test_revenue_shares_sum_to_100_percent(self):
        import chat
        result = chat._verified_percentage_metrics(
            {
                "get_sales_summary": {"total_revenue": 1000.0},
                "get_product_revenue": [
                    {"product": "A", "total_revenue": 333.33},
                    {"product": "B", "total_revenue": 666.67},
                ],
                "get_region_revenue": [
                    {"region": "North", "total_revenue": 400.0},
                    {"region": "South", "total_revenue": 600.0},
                ],
                "get_monthly_revenue": [
                    {"order_month": "2026-01", "total_revenue": 250.0},
                    {"order_month": "2026-02", "total_revenue": 750.0},
                ],
            }
        )
        for key in (
            "product_revenue_shares",
            "regional_revenue_shares",
            "monthly_revenue_shares",
        ):
            self.assertAlmostEqual(
                sum(item["percentage"] for item in result[key]),
                100.0,
                places=1,
            )

    def test_decline_calculation_is_signed_and_labelled(self):
        import chat
        result = chat._verified_percentage_metrics(
            {
                "get_monthly_revenue": [
                    {"order_month": "2026-04", "total_revenue": 500.0},
                    {"order_month": "2026-05", "total_revenue": 400.0},
                ]
            }
        )
        change = result["month_over_month_changes"][0]
        self.assertEqual(change["percentage_change"], -20.0)
        self.assertEqual(change["direction"], "decline")

    def test_zero_denominator_is_unavailable(self):
        import chat
        result = chat._verified_percentage_metrics(
            {
                "get_monthly_revenue": [
                    {"order_month": "2026-04", "total_revenue": 0.0},
                    {"order_month": "2026-05", "total_revenue": 400.0},
                ]
            }
        )
        change = result["month_over_month_changes"][0]
        self.assertIsNone(change["percentage_change"])
        self.assertEqual(change["status"], "unavailable_zero_denominator")

    def test_partial_month_comparison_is_not_full_month_growth(self):
        import chat
        result = chat._verified_percentage_metrics(
            {
                "get_monthly_revenue": [
                    {"order_month": "2026-06", "total_revenue": 500.0},
                    {"order_month": "2026-07", "total_revenue": 100.0},
                ]
            },
            {
                "partial_months": [
                    {"month": "2026-07", "latest_date": "2026-07-05"}
                ]
            },
        )
        change = result["month_over_month_changes"][0]
        self.assertIsNone(change["percentage_change"])
        self.assertEqual(change["status"], "unavailable_partial_month")

    def test_prompt_forbids_llm_percentage_recalculation(self):
        import chat
        self.assertIn(
            "Do not independently calculate",
            chat.ANSWER_SYSTEM_PROMPT,
        )
        self.assertIn("percentages", chat.ANSWER_SYSTEM_PROMPT)


# ============================================================
# 7. Answer System Prompt Validation Tests
# ============================================================

class TestAnswerSystemPrompt(unittest.TestCase):
    """Verify ANSWER_SYSTEM_PROMPT contains critical numerical accuracy instructions."""

    def test_prompt_mentions_preformatted_values(self):
        import chat
        prompt = chat.ANSWER_SYSTEM_PROMPT
        self.assertIn("formatted_*", prompt)
        self.assertIn("verbatim", prompt.lower())

    def test_prompt_warns_against_digit_duplication(self):
        import chat
        prompt = chat.ANSWER_SYSTEM_PROMPT
        self.assertIn("duplicate digits", prompt.lower())

    def test_prompt_mentions_partial_months(self):
        import chat
        prompt = chat.ANSWER_SYSTEM_PROMPT
        self.assertIn("partial_month_analysis", prompt)
        self.assertIn("partial month", prompt.lower())

    def test_prompt_warns_against_decline_interpretation(self):
        import chat
        prompt = chat.ANSWER_SYSTEM_PROMPT
        self.assertIn("decline", prompt.lower())

    def test_prompt_has_four_answer_sections(self):
        import chat
        prompt = chat.ANSWER_SYSTEM_PROMPT
        for section in ["Observed Facts", "Calculated Insights", "Business Hypotheses", "Recommendations"]:
            self.assertIn(section, prompt)

    def test_final_answer_message_mentions_formatting(self):
        """_final_answer_messages result message should mention formatted values."""
        import chat
        context_token = chat._active_business_context.set(BUSINESS_CONTEXT)
        try:
            with patch.object(chat, "_call_analytics", side_effect=RuntimeError("skip")):
                messages = chat._final_answer_messages(
                    "What is the revenue?",
                    {
                        "get_sales_summary": {
                            "total_transactions": 50,
                            "total_quantity": 598,
                            "total_revenue": 1479650.0,
                            "average_revenue": 29593.0,
                        }
                    },
                )
        finally:
            chat._active_business_context.reset(context_token)
        # The result message should mention formatted values
        result_msg = messages[-1]["content"]
        self.assertIn("formatted_*", result_msg)
        self.assertIn("verbatim", result_msg.lower())


# ============================================================
# 8. get_order_date_range Function Tests
# ============================================================

class TestGetOrderDateRange(unittest.TestCase):
    """Test the new get_order_date_range analytics function."""

    def test_returns_iso_date_strings(self):
        import analytics
        cursor = MagicMock()
        cursor.fetchone.return_value = {
            "min_order_date": datetime.date(2026, 1, 3),
            "max_order_date": datetime.date(2026, 7, 5),
        }
        conn = MagicMock()
        conn.cursor.return_value = cursor

        with patch(PATCH_CONN, return_value=conn):
            result = analytics.get_order_date_range(BUSINESS_CONTEXT)

        self.assertEqual(result["min_order_date"], "2026-01-03")
        self.assertEqual(result["max_order_date"], "2026-07-05")

    def test_handles_none_dates(self):
        """Empty dataset returns None dates."""
        import analytics
        cursor = MagicMock()
        cursor.fetchone.return_value = {
            "min_order_date": None,
            "max_order_date": None,
        }
        conn = MagicMock()
        conn.cursor.return_value = cursor

        with patch(PATCH_CONN, return_value=conn):
            result = analytics.get_order_date_range(BUSINESS_CONTEXT)

        self.assertIsNone(result["min_order_date"])
        self.assertIsNone(result["max_order_date"])

    def test_handles_string_dates(self):
        """If MySQL returns strings, they should be passed through."""
        import analytics
        cursor = MagicMock()
        cursor.fetchone.return_value = {
            "min_order_date": "2026-01-03",
            "max_order_date": "2026-07-05",
        }
        conn = MagicMock()
        conn.cursor.return_value = cursor

        with patch(PATCH_CONN, return_value=conn):
            result = analytics.get_order_date_range(BUSINESS_CONTEXT)

        self.assertEqual(result["min_order_date"], "2026-01-03")
        self.assertEqual(result["max_order_date"], "2026-07-05")

    def test_closes_connection(self):
        import analytics
        cursor = MagicMock()
        cursor.fetchone.return_value = {
            "min_order_date": None,
            "max_order_date": None,
        }
        conn = MagicMock()
        conn.cursor.return_value = cursor

        with patch(PATCH_CONN, return_value=conn):
            analytics.get_order_date_range(BUSINESS_CONTEXT)

        cursor.close.assert_called_once()
        conn.close.assert_called_once()

    def test_rejects_missing_context(self):
        import analytics
        with self.assertRaises(ValueError):
            analytics.get_order_date_range(None)  # type: ignore[arg-type]


# ============================================================
# Entry point
# ============================================================

if __name__ == "__main__":
    loader = unittest.TestLoader()
    suite = unittest.TestSuite()
    test_classes = [
        TestINRCurrencyFormatting,
        TestPreformattedContextValues,
        TestPartialMonthAwareness,
        TestBackendMetricsInAIContext,
        TestTenantSpecificAnalyticsContext,
        TestMissingInconsistentAnalytics,
        TestAnswerSystemPrompt,
        TestGetOrderDateRange,
    ]
    for cls in test_classes:
        suite.addTests(loader.loadTestsFromTestCase(cls))
    runner = unittest.TextTestRunner(verbosity=2)
    result = runner.run(suite)
    sys.exit(0 if result.wasSuccessful() else 1)
