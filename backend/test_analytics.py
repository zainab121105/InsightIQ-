"""
Mocked unit tests for analytics.py and the deterministic regional comparison
evidence builder in chat.py.

All MySQL connections are patched so no live database or Groq API call is made.
Run with:
    python -m pytest test_analytics.py -v
or:
    python test_analytics.py
"""

import json
import sys
import unittest
from decimal import Decimal
from unittest.mock import MagicMock, patch

# ---------------------------------------------------------------------------
# Helper factories
# ---------------------------------------------------------------------------

def _make_cursor_mock(rows):
    cursor = MagicMock()
    if isinstance(rows, dict):
        cursor.fetchone.return_value = rows
    else:
        cursor.fetchall.return_value = rows
    return cursor


def _conn_factory(rows):
    cursor = _make_cursor_mock(rows)
    conn = MagicMock()
    conn.cursor.return_value = cursor
    return conn, cursor


# ============================================================
# Shared fixture data (mirrors the cleaned_sales.csv dataset)
# ============================================================

SOUTH_PRODUCT_ROWS = [
    {"product": "Monitor",        "total_revenue": Decimal("122500"), "total_quantity": 13, "total_transactions": 3},
    {"product": "Wireless Mouse", "total_revenue": Decimal("54600"),  "total_quantity": 84, "total_transactions": 4},
    {"product": "Desk",           "total_revenue": Decimal("87500"),  "total_quantity":  7, "total_transactions": 2},
    {"product": "Office Chair",   "total_revenue": Decimal("117000"), "total_quantity": 15, "total_transactions": 3},
    {"product": "Keyboard",       "total_revenue": Decimal("21600"),  "total_quantity": 24, "total_transactions": 1},
    {"product": "USB-C Hub",      "total_revenue": Decimal("55800"),  "total_quantity": 31, "total_transactions": 2},
    {"product": "Laptop Stand",   "total_revenue": Decimal("19200"),  "total_quantity": 16, "total_transactions": 1},
]

NORTH_PRODUCT_ROWS = [
    {"product": "Monitor",        "total_revenue": Decimal("189300"), "total_quantity": 20, "total_transactions": 4},
    {"product": "Wireless Mouse", "total_revenue": Decimal("37050"),  "total_quantity": 57, "total_transactions": 2},
    {"product": "Desk",           "total_revenue": Decimal("37500"),  "total_quantity":  3, "total_transactions": 1},
    {"product": "Office Chair",   "total_revenue": Decimal("23400"),  "total_quantity":  3, "total_transactions": 1},
    {"product": "Keyboard",       "total_revenue": Decimal("67500"),  "total_quantity": 55, "total_transactions": 3},
    {"product": "USB-C Hub",      "total_revenue": Decimal("18000"),  "total_quantity": 10, "total_transactions": 1},
    {"product": "Laptop Stand",   "total_revenue": Decimal("45000"),  "total_quantity": 25, "total_transactions": 2},
]

SOUTH_CHANNEL_ROWS = [
    {"sales_channel": "Retail", "total_revenue": Decimal("249600"), "total_quantity": 48, "total_transactions": 5},
    {"sales_channel": "Online", "total_revenue": Decimal("228600"), "total_quantity": 87, "total_transactions": 8},
]

NORTH_CHANNEL_ROWS = [
    {"sales_channel": "Online", "total_revenue": Decimal("233700"), "total_quantity": 93, "total_transactions": 7},
    {"sales_channel": "Retail", "total_revenue": Decimal("183450"), "total_quantity": 80, "total_transactions": 6},
]

REGION_ROWS = [
    {"region": "North", "total_revenue": Decimal("417150")},
    {"region": "South", "total_revenue": Decimal("478200")},
    {"region": "East",  "total_revenue": Decimal("332200")},
    {"region": "West",  "total_revenue": Decimal("278750")},
]

SUMMARY_ROW = {
    "total_transactions": 49,
    "total_quantity": 598,
    "total_revenue": Decimal("1506300"),
    "average_revenue": Decimal("30740.8163"),
}

PRODUCT_ROWS = [
    {"product": "Monitor",        "total_revenue": Decimal("517800")},
    {"product": "Office Chair",   "total_revenue": Decimal("179400")},
    {"product": "Desk",           "total_revenue": Decimal("222500")},
    {"product": "USB-C Hub",      "total_revenue": Decimal("163800")},
    {"product": "Wireless Mouse", "total_revenue": Decimal("147500")},
    {"product": "Keyboard",       "total_revenue": Decimal("124200")},
    {"product": "Laptop Stand",   "total_revenue": Decimal("84600")},
]

MONTHLY_ROWS = [
    {"order_month": "2026-01", "total_revenue": Decimal("210250")},
    {"order_month": "2026-02", "total_revenue": Decimal("233600")},
    {"order_month": "2026-03", "total_revenue": Decimal("255900")},
    {"order_month": "2026-04", "total_revenue": Decimal("240800")},
    {"order_month": "2026-05", "total_revenue": Decimal("307700")},
    {"order_month": "2026-06", "total_revenue": Decimal("258050")},
]

PATCH_CONN = "analytics.get_db_connection"


# ============================================================
# Tests: original 7 analytics functions
# ============================================================

class TestGetSalesSummary(unittest.TestCase):
    @patch(PATCH_CONN)
    def test_returns_floats_and_int(self, mock_db):
        import analytics
        conn, _ = _conn_factory(SUMMARY_ROW.copy())
        mock_db.return_value = conn
        result = analytics.get_sales_summary()
        self.assertIsInstance(result["total_revenue"], float)
        self.assertIsInstance(result["average_revenue"], float)
        self.assertIsInstance(result["total_transactions"], int)
        self.assertAlmostEqual(result["total_revenue"], 1506300.0)
        self.assertEqual(result["total_transactions"], 49)

    @patch(PATCH_CONN)
    def test_closes_connection_on_success(self, mock_db):
        import analytics
        conn, _ = _conn_factory(SUMMARY_ROW.copy())
        mock_db.return_value = conn
        analytics.get_sales_summary()
        conn.close.assert_called_once()


class TestGetProductRevenue(unittest.TestCase):
    @patch(PATCH_CONN)
    def test_all_revenues_are_float(self, mock_db):
        import analytics
        conn, _ = _conn_factory([r.copy() for r in PRODUCT_ROWS])
        mock_db.return_value = conn
        rows = analytics.get_product_revenue()
        for row in rows:
            self.assertIsInstance(row["total_revenue"], float)

    @patch(PATCH_CONN)
    def test_returns_all_rows(self, mock_db):
        import analytics
        conn, _ = _conn_factory([r.copy() for r in PRODUCT_ROWS])
        mock_db.return_value = conn
        rows = analytics.get_product_revenue()
        self.assertEqual(len(rows), len(PRODUCT_ROWS))


class TestGetRegionRevenue(unittest.TestCase):
    @patch(PATCH_CONN)
    def test_all_revenues_are_float(self, mock_db):
        import analytics
        conn, _ = _conn_factory([r.copy() for r in REGION_ROWS])
        mock_db.return_value = conn
        rows = analytics.get_region_revenue()
        for row in rows:
            self.assertIsInstance(row["total_revenue"], float)

    @patch(PATCH_CONN)
    def test_returns_correct_regions(self, mock_db):
        import analytics
        conn, _ = _conn_factory([r.copy() for r in REGION_ROWS])
        mock_db.return_value = conn
        rows = analytics.get_region_revenue()
        regions = {r["region"] for r in rows}
        self.assertEqual(regions, {"North", "South", "East", "West"})


class TestGetMonthlyRevenue(unittest.TestCase):
    @patch(PATCH_CONN)
    def test_all_revenues_are_float(self, mock_db):
        import analytics
        conn, _ = _conn_factory([r.copy() for r in MONTHLY_ROWS])
        mock_db.return_value = conn
        rows = analytics.get_monthly_revenue()
        for row in rows:
            self.assertIsInstance(row["total_revenue"], float)

    @patch(PATCH_CONN)
    def test_returns_six_months(self, mock_db):
        import analytics
        conn, _ = _conn_factory([r.copy() for r in MONTHLY_ROWS])
        mock_db.return_value = conn
        rows = analytics.get_monthly_revenue()
        self.assertEqual(len(rows), 6)


class TestGetProductRevenueByRegion(unittest.TestCase):
    def _conn_for_region(self, product_rows):
        cursor = MagicMock()
        cursor.fetchone.return_value = {"1": 1}
        cursor.fetchall.return_value = [r.copy() for r in product_rows]
        conn = MagicMock()
        conn.cursor.return_value = cursor
        return conn

    @patch(PATCH_CONN)
    def test_south_returns_correct_product_count(self, mock_db):
        import analytics
        mock_db.return_value = self._conn_for_region(SOUTH_PRODUCT_ROWS)
        rows = analytics.get_product_revenue_by_region("South")
        self.assertEqual(len(rows), len(SOUTH_PRODUCT_ROWS))

    @patch(PATCH_CONN)
    def test_float_int_conversion(self, mock_db):
        import analytics
        mock_db.return_value = self._conn_for_region(SOUTH_PRODUCT_ROWS)
        rows = analytics.get_product_revenue_by_region("South")
        for row in rows:
            self.assertIsInstance(row["total_revenue"], float)
            self.assertIsInstance(row["total_quantity"], int)
            self.assertIsInstance(row["total_transactions"], int)

    @patch(PATCH_CONN)
    def test_unknown_region_raises(self, mock_db):
        import analytics
        cursor = MagicMock()
        cursor.fetchone.return_value = None
        conn = MagicMock()
        conn.cursor.return_value = cursor
        mock_db.return_value = conn
        with self.assertRaises(ValueError):
            analytics.get_product_revenue_by_region("Atlantis")


class TestCompareProductsByRegion(unittest.TestCase):
    def _conn_for_comparison(self, product_rows=None):
        cursor = MagicMock()
        cursor.fetchone.return_value = {"1": 1}
        rows = product_rows or SOUTH_PRODUCT_ROWS
        cursor.fetchall.side_effect = [
            [{"product": r["product"]} for r in rows],
            [r.copy() for r in rows],
        ]
        conn = MagicMock()
        conn.cursor.return_value = cursor
        return conn

    @patch(PATCH_CONN)
    def test_compare_two_products(self, mock_db):
        import analytics
        mock_db.return_value = self._conn_for_comparison()
        rows = analytics.compare_products_by_region("South", ["Monitor", "Keyboard"])
        self.assertIsInstance(rows, list)

    @patch(PATCH_CONN)
    def test_unknown_region_raises(self, mock_db):
        import analytics
        cursor = MagicMock()
        cursor.fetchone.return_value = None
        conn = MagicMock()
        conn.cursor.return_value = cursor
        mock_db.return_value = conn
        with self.assertRaises(ValueError):
            analytics.compare_products_by_region("Atlantis", ["Monitor"])


class TestGetRegionProductPerformance(unittest.TestCase):
    def _conn_for_region(self, product_rows):
        cursor = MagicMock()
        cursor.fetchone.return_value = {"1": 1}
        cursor.fetchall.return_value = [r.copy() for r in product_rows]
        conn = MagicMock()
        conn.cursor.return_value = cursor
        return conn

    @patch(PATCH_CONN)
    def test_returns_region_and_products_keys(self, mock_db):
        import analytics
        mock_db.return_value = self._conn_for_region(SOUTH_PRODUCT_ROWS)
        result = analytics.get_region_product_performance("South")
        self.assertIn("region", result)
        self.assertIn("products", result)
        self.assertEqual(result["region"], "South")

    @patch(PATCH_CONN)
    def test_products_is_list(self, mock_db):
        import analytics
        mock_db.return_value = self._conn_for_region(SOUTH_PRODUCT_ROWS)
        result = analytics.get_region_product_performance("South")
        self.assertIsInstance(result["products"], list)


# ============================================================
# Tests: new channel analytics functions
# ============================================================

class TestGetChannelRevenueByRegion(unittest.TestCase):
    def _conn_for_channel(self, channel_rows):
        cursor = MagicMock()
        cursor.fetchone.return_value = {"1": 1}
        cursor.fetchall.return_value = [r.copy() for r in channel_rows]
        conn = MagicMock()
        conn.cursor.return_value = cursor
        return conn

    @patch(PATCH_CONN)
    def test_south_structure(self, mock_db):
        import analytics
        mock_db.return_value = self._conn_for_channel(SOUTH_CHANNEL_ROWS)
        result = analytics.get_channel_revenue_by_region("South")
        self.assertEqual(result["region"], "South")
        self.assertIsInstance(result["channels"], list)
        self.assertEqual(len(result["channels"]), 2)

    @patch(PATCH_CONN)
    def test_type_conversion(self, mock_db):
        import analytics
        mock_db.return_value = self._conn_for_channel(SOUTH_CHANNEL_ROWS)
        result = analytics.get_channel_revenue_by_region("South")
        for row in result["channels"]:
            self.assertIsInstance(row["total_revenue"], float)
            self.assertIsInstance(row["total_quantity"], int)
            self.assertIsInstance(row["total_transactions"], int)

    @patch(PATCH_CONN)
    def test_channel_names_present(self, mock_db):
        import analytics
        mock_db.return_value = self._conn_for_channel(SOUTH_CHANNEL_ROWS)
        result = analytics.get_channel_revenue_by_region("South")
        channels = {row["sales_channel"] for row in result["channels"]}
        self.assertEqual(channels, {"Retail", "Online"})

    @patch(PATCH_CONN)
    def test_unknown_region_raises(self, mock_db):
        import analytics
        cursor = MagicMock()
        cursor.fetchone.return_value = None
        conn = MagicMock()
        conn.cursor.return_value = cursor
        mock_db.return_value = conn
        with self.assertRaises(ValueError):
            analytics.get_channel_revenue_by_region("Narnia")

    @patch(PATCH_CONN)
    def test_revenue_values(self, mock_db):
        import analytics
        mock_db.return_value = self._conn_for_channel(SOUTH_CHANNEL_ROWS)
        result = analytics.get_channel_revenue_by_region("South")
        revenues = {r["sales_channel"]: r["total_revenue"] for r in result["channels"]}
        self.assertAlmostEqual(revenues["Retail"], 249600.0)
        self.assertAlmostEqual(revenues["Online"], 228600.0)


class TestGetRegionalChannelSummary(unittest.TestCase):
    """Tests for get_regional_channel_summary with two regions."""

    def _build_multi_region_side_effects(self):
        # Call 1: validation connection
        val_cursor = MagicMock()
        val_cursor.fetchone.return_value = {"1": 1}
        val_conn = MagicMock()
        val_conn.cursor.return_value = val_cursor

        # Call 2: South channel connection
        south_cursor = MagicMock()
        south_cursor.fetchone.return_value = {"1": 1}
        south_cursor.fetchall.return_value = [r.copy() for r in SOUTH_CHANNEL_ROWS]
        south_conn = MagicMock()
        south_conn.cursor.return_value = south_cursor

        # Call 3: North channel connection
        north_cursor = MagicMock()
        north_cursor.fetchone.return_value = {"1": 1}
        north_cursor.fetchall.return_value = [r.copy() for r in NORTH_CHANNEL_ROWS]
        north_conn = MagicMock()
        north_conn.cursor.return_value = north_cursor

        return [val_conn, south_conn, north_conn]

    @patch(PATCH_CONN)
    def test_returns_both_regions(self, mock_db):
        import analytics
        mock_db.side_effect = self._build_multi_region_side_effects()
        result = analytics.get_regional_channel_summary(["South", "North"])
        self.assertIn("South", result)
        self.assertIn("North", result)

    @patch(PATCH_CONN)
    def test_each_region_has_two_channels(self, mock_db):
        import analytics
        mock_db.side_effect = self._build_multi_region_side_effects()
        result = analytics.get_regional_channel_summary(["South", "North"])
        self.assertEqual(len(result["South"]), 2)
        self.assertEqual(len(result["North"]), 2)

    @patch(PATCH_CONN)
    def test_values_are_correct_types(self, mock_db):
        import analytics
        mock_db.side_effect = self._build_multi_region_side_effects()
        result = analytics.get_regional_channel_summary(["South", "North"])
        for region_rows in result.values():
            for row in region_rows:
                self.assertIsInstance(row["total_revenue"], float)
                self.assertIsInstance(row["total_quantity"], int)
                self.assertIsInstance(row["total_transactions"], int)

    @patch(PATCH_CONN)
    def test_empty_list_raises(self, mock_db):
        import analytics
        with self.assertRaises(ValueError):
            analytics.get_regional_channel_summary([])

    @patch(PATCH_CONN)
    def test_south_retail_revenue(self, mock_db):
        import analytics
        mock_db.side_effect = self._build_multi_region_side_effects()
        result = analytics.get_regional_channel_summary(["South", "North"])
        south_map = {r["sales_channel"]: r["total_revenue"] for r in result["South"]}
        self.assertAlmostEqual(south_map["Retail"], 249600.0)


# ============================================================
# Tests: deterministic regional comparison evidence builder
# ============================================================

_MOCKED_ANALYTICS_FUNCTIONS = {
    "get_sales_summary": lambda: {
        "total_transactions": 49,
        "total_quantity": 598,
        "total_revenue": 1506300.0,
        "average_revenue": 30740.82,
    },
    "get_product_revenue": lambda: [
        {"product": p["product"], "total_revenue": float(p["total_revenue"])}
        for p in PRODUCT_ROWS
    ],
    "get_region_revenue": lambda: [
        {"region": r["region"], "total_revenue": float(r["total_revenue"])}
        for r in REGION_ROWS
    ],
    "get_monthly_revenue": lambda: [
        {"order_month": r["order_month"], "total_revenue": float(r["total_revenue"])}
        for r in MONTHLY_ROWS
    ],
    "get_product_revenue_by_region": lambda region: [
        {
            "product": row["product"],
            "total_revenue": float(row["total_revenue"]),
            "total_quantity": int(row["total_quantity"]),
            "total_transactions": int(row["total_transactions"]),
        }
        for row in (NORTH_PRODUCT_ROWS if region == "North" else SOUTH_PRODUCT_ROWS)
    ],
    "compare_products_by_region": lambda region, products=None: [],
    "get_region_product_performance": lambda region: {"region": region, "products": []},
}

_MOCKED_CHANNEL_DATA = {
    "North": [
        {"sales_channel": c["sales_channel"],
         "total_revenue": float(c["total_revenue"]),
         "total_quantity": int(c["total_quantity"]),
         "total_transactions": int(c["total_transactions"])}
        for c in NORTH_CHANNEL_ROWS
    ],
    "South": [
        {"sales_channel": c["sales_channel"],
         "total_revenue": float(c["total_revenue"]),
         "total_quantity": int(c["total_quantity"]),
         "total_transactions": int(c["total_transactions"])}
        for c in SOUTH_CHANNEL_ROWS
    ],
}


def _run_evidence(higher="North", comparison="South"):
    import chat
    execution_cache: dict = {}
    with patch.dict("chat.ANALYTICS_FUNCTIONS", _MOCKED_ANALYTICS_FUNCTIONS):
        with patch("chat.get_regional_channel_summary",
                   return_value=_MOCKED_CHANNEL_DATA) as mock_ch:
            evidence, results = chat._deterministic_recommendation_evidence(
                execution_cache,
                regional_comparison=(higher, comparison),
            )
    return evidence, results, mock_ch


class TestDeterministicRecommendationEvidence(unittest.TestCase):

    # --- Channel breakdown ---

    def test_channel_breakdown_key_exists(self):
        evidence, _, _ = _run_evidence()
        self.assertIn("channel_breakdown", evidence["regional_comparison"])

    def test_channel_breakdown_contains_both_regions(self):
        evidence, _, _ = _run_evidence()
        cb = evidence["regional_comparison"]["channel_breakdown"]
        self.assertIn("North", cb)
        self.assertIn("South", cb)

    def test_channel_breakdown_available_flag_true(self):
        evidence, _, _ = _run_evidence()
        self.assertTrue(evidence["regional_comparison"]["channel_breakdown_available"])

    def test_channel_breakdown_has_correct_channels(self):
        evidence, _, _ = _run_evidence()
        cb = evidence["regional_comparison"]["channel_breakdown"]
        north_channels = {r["sales_channel"] for r in cb["North"]}
        south_channels = {r["sales_channel"] for r in cb["South"]}
        self.assertEqual(north_channels, {"Online", "Retail"})
        self.assertEqual(south_channels, {"Retail", "Online"})

    def test_get_regional_channel_summary_called_with_both_regions(self):
        evidence, _, mock_ch = _run_evidence()
        mock_ch.assert_called_once()
        called_regions = set(mock_ch.call_args[0][0])
        self.assertIn("North", called_regions)
        self.assertIn("South", called_regions)

    # --- Basket analysis note ---

    def test_basket_analysis_note_present(self):
        evidence, _, _ = _run_evidence()
        self.assertIn("basket_analysis_note", evidence["regional_comparison"])

    def test_basket_analysis_note_says_one_product(self):
        evidence, _, _ = _run_evidence()
        note = evidence["regional_comparison"]["basket_analysis_note"]
        self.assertIn("one product", note)
        self.assertIn("basket", note)

    # --- Available dataset fields ---

    def test_all_dataset_fields_listed(self):
        evidence, _, _ = _run_evidence()
        fields = evidence["regional_comparison"]["available_dataset_fields"]
        for expected in ("transaction_id", "product", "region", "sales_channel",
                         "quantity", "unit_price", "discount_amount", "revenue"):
            self.assertIn(expected, fields)

    # --- Grounding rules ---

    def test_grounding_rules_forbid_collecting_existing_fields(self):
        evidence, _, _ = _run_evidence()
        rules_text = " ".join(evidence["regional_comparison"]["grounding_rules"])
        self.assertIn("sales_channel", rules_text)
        self.assertIn("Do not recommend collecting fields", rules_text)

    def test_grounding_rules_forbid_basket_metrics(self):
        evidence, _, _ = _run_evidence()
        rules_text = " ".join(evidence["regional_comparison"]["grounding_rules"]).lower()
        self.assertIn("basket", rules_text)

    def test_grounding_rules_forbid_profitability_claims(self):
        evidence, _, _ = _run_evidence()
        rules_text = " ".join(evidence["regional_comparison"]["grounding_rules"])
        self.assertIn("Profitability", rules_text)

    def test_grounding_rules_allow_customer_segment_suggestion(self):
        evidence, _, _ = _run_evidence()
        rules_text = " ".join(evidence["regional_comparison"]["grounding_rules"])
        self.assertIn("Customer segment", rules_text)
        self.assertIn("suggested as", rules_text)

    # --- Recommendation limits ---

    def test_limits_forbid_channel_collection(self):
        evidence, _, _ = _run_evidence()
        limits_text = " ".join(evidence["recommendation_limits"])
        self.assertIn("sales-channel", limits_text)

    def test_limits_forbid_basket_claims(self):
        evidence, _, _ = _run_evidence()
        limits_text = " ".join(evidence["recommendation_limits"]).lower()
        self.assertIn("basket", limits_text)

    def test_limits_forbid_profitability_claims(self):
        evidence, _, _ = _run_evidence()
        limits_text = " ".join(evidence["recommendation_limits"]).lower()
        self.assertIn("margins", limits_text)

    # --- Existing regional comparison fields still intact ---

    def test_revenue_difference_computed_correctly(self):
        evidence, _, _ = _run_evidence("North", "South")
        diff = evidence["regional_comparison"]["revenue_difference"]
        north_rev = float(next(r["total_revenue"] for r in REGION_ROWS if r["region"] == "North"))
        south_rev = float(next(r["total_revenue"] for r in REGION_ROWS if r["region"] == "South"))
        self.assertAlmostEqual(diff, north_rev - south_rev, places=2)

    def test_product_revenue_rows_present(self):
        evidence, _, _ = _run_evidence()
        self.assertIn("product_revenue_rows", evidence["regional_comparison"])

    def test_regional_totals_present_for_both_regions(self):
        evidence, _, _ = _run_evidence()
        totals = evidence["regional_comparison"]["regional_totals"]
        self.assertIn("North", totals)
        self.assertIn("South", totals)
        for t in totals.values():
            self.assertIn("total_quantity", t)
            self.assertIn("total_transactions", t)

    def test_scope_is_regional_comparison(self):
        evidence, _, _ = _run_evidence()
        self.assertEqual(evidence["scope"], "regional_comparison")

    def test_monthly_data_scope_note(self):
        evidence, _, _ = _run_evidence()
        self.assertEqual(
            evidence["regional_comparison"]["monthly_data_scope"],
            "overall_only_not_region_specific",
        )

    def test_channel_breakdown_unavailable_adds_limitation(self):
        """When get_regional_channel_summary raises, a limitation is recorded."""
        import chat
        execution_cache: dict = {}
        with patch.dict("chat.ANALYTICS_FUNCTIONS", _MOCKED_ANALYTICS_FUNCTIONS):
            with patch("chat.get_regional_channel_summary",
                       side_effect=RuntimeError("DB down")):
                evidence, _, = chat._deterministic_recommendation_evidence(
                    execution_cache,
                    regional_comparison=("North", "South"),
                )[:2]
        limitations_text = " ".join(evidence["limitations"])
        self.assertIn("Sales-channel breakdown was unavailable", limitations_text)

    def test_channel_breakdown_empty_when_unavailable(self):
        """channel_breakdown_available is False when the call fails."""
        import chat
        execution_cache: dict = {}
        with patch.dict("chat.ANALYTICS_FUNCTIONS", _MOCKED_ANALYTICS_FUNCTIONS):
            with patch("chat.get_regional_channel_summary",
                       side_effect=RuntimeError("DB down")):
                evidence, _ = chat._deterministic_recommendation_evidence(
                    execution_cache,
                    regional_comparison=("North", "South"),
                )[:2]
        self.assertFalse(evidence["regional_comparison"]["channel_breakdown_available"])

    def test_channel_comparison_question_matches_intent(self):
        """Questions asking which channel generates more revenue in two regions match the intent."""
        import chat
        result = chat._regional_recommendation_comparison_intent(
            "Which sales channel generates more revenue in South and North?"
        )
        self.assertEqual(result, ("South", "North"))

        result_vs = chat._regional_recommendation_comparison_intent(
            "Which sales channel generates more revenue in South vs North?"
        )
        self.assertEqual(result_vs, ("South", "North"))

    def test_channel_revenue_advantage_percentage_calculation(self):
        """Verify percentage calculation formula uses comparison channel as denominator:
        South Retail advantage over Online: (212150 - 199050) / 199050 * 100 = 6.58%
        North Retail advantage over Online: (234450 - 150000) / 150000 * 100 = 56.30%
        """
        import chat
        execution_cache: dict = {}
        # Channel breakdown fixture with exact live numbers
        live_channel_mock = {
            "South": [
                {"sales_channel": "Retail", "total_revenue": 212150.0, "total_quantity": 95, "total_transactions": 6},
                {"sales_channel": "Online", "total_revenue": 199050.0, "total_quantity": 87, "total_transactions": 7},
            ],
            "North": [
                {"sales_channel": "Retail", "total_revenue": 234450.0, "total_quantity": 95, "total_transactions": 7},
                {"sales_channel": "Online", "total_revenue": 150000.0, "total_quantity": 78, "total_transactions": 7},
            ],
        }
        with patch.dict("chat.ANALYTICS_FUNCTIONS", _MOCKED_ANALYTICS_FUNCTIONS):
            with patch("chat.get_regional_channel_summary", return_value=live_channel_mock):
                evidence, _ = chat._deterministic_recommendation_evidence(
                    execution_cache,
                    regional_comparison=("South", "North"),
                )[:2]
        advantages = evidence["verified_calculations"]["channel_revenue_advantages"]
        self.assertIn("South", advantages)
        self.assertIn("North", advantages)
        self.assertEqual(advantages["South"]["comparison_channel_denominator"], "Online")
        self.assertEqual(advantages["South"]["retail_revenue_advantage_percentage"], 6.58)
        self.assertEqual(advantages["North"]["comparison_channel_denominator"], "Online")
        self.assertEqual(advantages["North"]["retail_revenue_advantage_percentage"], 56.3)

    def test_terminology_restrictions_in_rules_and_prompt(self):
        """Verify 'revenue difference' / 'revenue advantage' terminology and prohibition on 'profit margin'."""
        import chat
        evidence, _, _ = _run_evidence()
        rules_text = " ".join(evidence["regional_comparison"]["grounding_rules"])
        self.assertIn("revenue difference", rules_text)
        self.assertIn("revenue advantage", rules_text)
        self.assertIn("channel effectiveness", rules_text)
        self.assertIn("never use 'profit margin', 'profitability', or 'channel effectiveness'", rules_text)

        prompt = chat.ANSWER_SYSTEM_PROMPT
        self.assertIn("revenue difference", prompt)
        self.assertIn("revenue advantage", prompt)
        self.assertIn("6.58%", prompt)
        self.assertIn("56.30%", prompt)

    def test_cost_data_marked_unavailable(self):
        """COGS and cost data must be explicitly flagged as unavailable."""
        evidence, _, _ = _run_evidence()
        rc = evidence["regional_comparison"]
        self.assertFalse(rc.get("cost_of_goods_sold_available", True))
        self.assertFalse(rc.get("cost_data_available", True))
        rules_text = " ".join(rc["grounding_rules"])
        self.assertIn("Cost of goods sold (COGS)", rules_text)
        self.assertIn("unavailable", rules_text)

    def test_existing_data_fields_available_for_analysis(self):
        """Confirm unit_price, discount_amount, sales_channel, and region are present for recommendations."""
        evidence, _, _ = _run_evidence()
        dataset_fields = evidence["regional_comparison"]["available_dataset_fields"]
        for f in ("unit_price", "discount_amount", "sales_channel", "region"):
            self.assertIn(f, dataset_fields)
        rules_text = " ".join(evidence["regional_comparison"]["grounding_rules"])
        self.assertIn("average selling prices and discounts", rules_text)

    def test_recommendations_framing_promotions_as_experiments(self):
        """Confirm promotions are framed as measurable experiments rather than guaranteed outcomes."""
        import chat
        evidence, _, _ = _run_evidence()
        rules_text = " ".join(evidence["regional_comparison"]["grounding_rules"])
        self.assertIn("Distinguish evidence-backed observations from proposed experiments", rules_text)
        self.assertIn("experiments whose outcomes must be measured", rules_text)
        self.assertIn("experiments", chat.ANSWER_SYSTEM_PROMPT)



class TestZeroGroqDeterministicResponses(unittest.TestCase):
    """Verify that the 5 factual analytics questions and variations execute with ZERO Groq API calls."""

    def test_overall_summary_intent_variations(self):
        import chat
        queries = [
            "Give me an overall business summary.",
            "Give me the overall business summary.",
            "Show overall business summary.",
            "Summarize the business.",
            "What is the overall business performance?",
            "overall business summary",
        ]
        for q in queries:
            with self.subTest(query=q):
                self.assertTrue(chat._is_overall_summary_intent(q), f"Failed for {q}")

    def test_regional_comparison_intent_variations(self):
        import chat
        cases = [
            ("Compare South and North in terms of revenue.", ("South", "North")),
            ("Compare South and North.", ("South", "North")),
            ("Compare revenue between South and North.", ("South", "North")),
            ("How does South perform compared with North?", ("South", "North")),
            ("Which sales channel generates more revenue in South and North?", ("South", "North")),
            ("Which sales channel generates more revenue in South vs North?", ("South", "North")),
        ]
        for q, expected in cases:
            with self.subTest(query=q):
                res = chat._regional_recommendation_comparison_intent(q)
                self.assertEqual(res, expected, f"Failed for {q}")

    def test_five_factual_questions_call_zero_groq(self):
        import chat
        questions = [
            "Give me an overall business summary.",
            "Which product generates the highest revenue?",
            "Which region generates the highest revenue?",
            "Which sales channel generates more revenue in South and North?",
            "Compare South and North in terms of revenue.",
        ]
        for q in questions:
            with self.subTest(query=q):
                with patch("chat._chat_completion") as mock_completion:
                    answer, timings, data = chat.answer_question(q)
                    mock_completion.assert_not_called()
                    self.assertEqual(timings["planning_ms"], 0.0)
                    self.assertEqual(timings["response_generation_ms"], 0.0)
                    self.assertGreater(len(answer), 20)

    def test_unsupported_complex_question_still_calls_groq(self):
        import chat
        question = "What strategies would you recommend to improve seasonal sales patterns across quarters?"
        # Mocking chat completion to return a simulated response without consuming Groq quota
        mock_response = MagicMock()
        mock_response.choices = [
            MagicMock(message=MagicMock(content="Here are recommended seasonal strategies.", tool_calls=None))
        ]
        with patch("chat._chat_completion", return_value=mock_response) as mock_completion:
            answer, timings, data = chat.answer_question(question)
            self.assertTrue(mock_completion.called)
            self.assertEqual(answer, "Here are recommended seasonal strategies.")


# ============================================================
# Entry point
# ============================================================

if __name__ == "__main__":
    loader = unittest.TestLoader()
    suite = unittest.TestSuite()
    test_classes = [
        TestGetSalesSummary,
        TestGetProductRevenue,
        TestGetRegionRevenue,
        TestGetMonthlyRevenue,
        TestGetProductRevenueByRegion,
        TestCompareProductsByRegion,
        TestGetRegionProductPerformance,
        TestGetChannelRevenueByRegion,
        TestGetRegionalChannelSummary,
        TestDeterministicRecommendationEvidence,
        TestZeroGroqDeterministicResponses,
    ]
    for cls in test_classes:
        suite.addTests(loader.loadTestsFromTestCase(cls))
    runner = unittest.TextTestRunner(verbosity=2)
    result = runner.run(suite)
    sys.exit(0 if result.wasSuccessful() else 1)