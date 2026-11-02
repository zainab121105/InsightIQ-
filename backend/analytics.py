
from database import get_db_connection
from auth import AuthenticatedBusinessContext


def _business_id(context: AuthenticatedBusinessContext) -> int:
    if not isinstance(context, AuthenticatedBusinessContext) or context.business_id <= 0:
        raise ValueError("An authenticated business context is required.")
    return context.business_id


def _validate_region(cursor, region: str, business_id: int) -> str:
    if not isinstance(region, str) or not region.strip():
        raise ValueError("Region must be a non-empty string.")

    normalized_region = region.strip()
    cursor.execute(
        """
        SELECT 1
        FROM sales_transactions
        WHERE business_id = %s AND region = %s
        LIMIT 1
        """,
        (business_id, normalized_region),
    )
    if cursor.fetchone() is None:
        raise ValueError(f"Unknown region: {normalized_region}")

    return normalized_region


def _validate_products(cursor, products: list[str], business_id: int) -> list[str]:
    if not products:
        raise ValueError("Products must contain at least one product name.")
    if any(not isinstance(product, str) or not product.strip() for product in products):
        raise ValueError("Each product must be a non-empty string.")

    normalized_products = list(dict.fromkeys(product.strip() for product in products))
    placeholders = ", ".join(["%s"] * len(normalized_products))
    cursor.execute(
        f"""
        SELECT product
        FROM sales_transactions
        WHERE business_id = %s AND product IN ({placeholders})
        GROUP BY product
        """,
        (business_id, *normalized_products),
    )
    known_products = {row["product"] for row in cursor.fetchall()}
    unknown_products = [
        product for product in normalized_products if product not in known_products
    ]
    if unknown_products:
        raise ValueError(f"Unknown product(s): {', '.join(unknown_products)}")

    return normalized_products


# 1. Get overall sales summary
def get_sales_summary(context: AuthenticatedBusinessContext):

    business_id = _business_id(context)
    connection = get_db_connection()
    cursor = connection.cursor(dictionary=True)

    query = """
        SELECT
            COUNT(*) AS total_transactions,
            SUM(quantity) AS total_quantity,
            SUM(revenue) AS total_revenue,
            AVG(revenue) AS average_revenue
        FROM sales_transactions
        WHERE business_id = %s
    """

    cursor.execute(query, (business_id,))
    result = cursor.fetchone()

    cursor.close()
    connection.close()

    # Convert Decimal values into JSON-compatible numbers
    for key, value in result.items():
        if value is not None:
            result[key] = float(value)

    result["total_transactions"] = int(
        result["total_transactions"]
    )

    return result


def get_order_date_range(context: AuthenticatedBusinessContext):
    """Return the earliest and latest order dates for this business."""
    business_id = _business_id(context)
    connection = get_db_connection()
    cursor = connection.cursor(dictionary=True)

    query = """
        SELECT
            MIN(order_date) AS min_order_date,
            MAX(order_date) AS max_order_date
        FROM sales_transactions
        WHERE business_id = %s
    """

    cursor.execute(query, (business_id,))
    result = cursor.fetchone()

    cursor.close()
    connection.close()

    # Convert date objects to ISO strings for JSON serialization
    for key in ("min_order_date", "max_order_date"):
        value = result.get(key)
        if value is not None and hasattr(value, "isoformat"):
            result[key] = value.isoformat()
        elif value is not None:
            result[key] = str(value)

    return result

# 2. Get product-wise revenue
def get_product_revenue(context: AuthenticatedBusinessContext):

    business_id = _business_id(context)
    connection = get_db_connection()
    cursor = connection.cursor(dictionary=True)

    query = """
        SELECT
            product,
            SUM(revenue) AS total_revenue
        FROM sales_transactions
        WHERE business_id = %s
        GROUP BY product
        ORDER BY total_revenue DESC
    """

    cursor.execute(query, (business_id,))
    results = cursor.fetchall()

    cursor.close()
    connection.close()

    for row in results:
        row["total_revenue"] = float(row["total_revenue"])

    return results


# 3. Get region-wise revenue
def get_region_revenue(context: AuthenticatedBusinessContext):

    business_id = _business_id(context)
    connection = get_db_connection()
    cursor = connection.cursor(dictionary=True)

    query = """
        SELECT
            region,
            SUM(revenue) AS total_revenue
        FROM sales_transactions
        WHERE business_id = %s
        GROUP BY region
        ORDER BY total_revenue DESC
    """

    cursor.execute(query, (business_id,))
    results = cursor.fetchall()

    cursor.close()
    connection.close()

    for row in results:
        row["total_revenue"] = float(row["total_revenue"])

    return results


# 4. Get monthly revenue
def get_monthly_revenue(context: AuthenticatedBusinessContext):

    business_id = _business_id(context)
    connection = get_db_connection()
    cursor = connection.cursor(dictionary=True)

    query = """
        SELECT
            order_month,
            SUM(revenue) AS total_revenue
        FROM sales_transactions
        WHERE business_id = %s
        GROUP BY order_month
        ORDER BY order_month
    """

    cursor.execute(query, (business_id,))
    results = cursor.fetchall()

    cursor.close()
    connection.close()

    for row in results:
        row["total_revenue"] = float(row["total_revenue"])

    return results


def get_product_revenue_by_region(
    region: str,
    context: AuthenticatedBusinessContext,
):
    """Return product performance metrics for one valid region."""
    business_id = _business_id(context)
    connection = get_db_connection()
    cursor = connection.cursor(dictionary=True)

    try:
        normalized_region = _validate_region(cursor, region, business_id)
        cursor.execute(
            """
            SELECT
                product,
                SUM(revenue) AS total_revenue,
                SUM(quantity) AS total_quantity,
                COUNT(*) AS total_transactions
            FROM sales_transactions
            WHERE business_id = %s AND region = %s
            GROUP BY product
            ORDER BY total_revenue DESC
            """,
            (business_id, normalized_region),
        )
        results = cursor.fetchall()
        for row in results:
            row["total_revenue"] = float(row["total_revenue"])
            row["total_quantity"] = int(row["total_quantity"])
            row["total_transactions"] = int(row["total_transactions"])
        return results
    finally:
        cursor.close()
        connection.close()


def compare_products_by_region(
    region: str,
    context: AuthenticatedBusinessContext,
    products: list[str] | None = None,
):
    """Compare selected products, or all products, within one valid region."""
    business_id = _business_id(context)
    connection = get_db_connection()
    cursor = connection.cursor(dictionary=True)

    try:
        normalized_region = _validate_region(cursor, region, business_id)
        normalized_products = (
            _validate_products(cursor, products, business_id)
            if products is not None
            else None
        )

        product_filter = ""
        parameters: list[str | int] = [business_id, normalized_region]
        if normalized_products is not None:
            placeholders = ", ".join(["%s"] * len(normalized_products))
            product_filter = f"AND product IN ({placeholders})"
            parameters.extend(normalized_products)

        cursor.execute(
            f"""
            SELECT
                product,
                SUM(revenue) AS total_revenue,
                SUM(quantity) AS total_quantity,
                COUNT(*) AS total_transactions
            FROM sales_transactions
            WHERE business_id = %s AND region = %s
            {product_filter}
            GROUP BY product
            ORDER BY total_revenue DESC
            """,
            tuple(parameters),
        )
        results = cursor.fetchall()
        for row in results:
            row["total_revenue"] = float(row["total_revenue"])
            row["total_quantity"] = int(row["total_quantity"])
            row["total_transactions"] = int(row["total_transactions"])
        return results
    finally:
        cursor.close()
        connection.close()


def get_region_product_performance(
    region: str,
    context: AuthenticatedBusinessContext,
):
    """Return a region and its products ordered by revenue."""
    return {
        "region": region.strip() if isinstance(region, str) else region,
        "products": get_product_revenue_by_region(region, context),
    }


def get_channel_revenue_by_region(
    region: str,
    context: AuthenticatedBusinessContext,
):
    """Return revenue, quantity and transaction count per sales channel in one region."""
    business_id = _business_id(context)
    connection = get_db_connection()
    cursor = connection.cursor(dictionary=True)

    try:
        normalized_region = _validate_region(cursor, region, business_id)
        cursor.execute(
            """
            SELECT
                sales_channel,
                SUM(revenue)       AS total_revenue,
                SUM(quantity)      AS total_quantity,
                COUNT(*)           AS total_transactions
            FROM sales_transactions
            WHERE business_id = %s AND region = %s
            GROUP BY sales_channel
            ORDER BY total_revenue DESC
            """,
            (business_id, normalized_region),
        )
        results = cursor.fetchall()
        for row in results:
            row["total_revenue"] = float(row["total_revenue"])
            row["total_quantity"] = int(row["total_quantity"])
            row["total_transactions"] = int(row["total_transactions"])
        return {"region": normalized_region, "channels": results}
    finally:
        cursor.close()
        connection.close()


def get_regional_channel_summary(
    regions: list[str],
    context: AuthenticatedBusinessContext,
):
    """Return per-channel revenue, quantity and transaction counts for multiple regions.

    All requested regions are validated before any data is fetched.  The result
    is a dict keyed by region name, each value being the list of channel rows
    returned by get_channel_revenue_by_region.
    """
    if not regions:
        raise ValueError("At least one region must be supplied.")

    business_id = _business_id(context)
    connection = get_db_connection()
    cursor = connection.cursor(dictionary=True)

    try:
        # Validate every region up-front so the caller gets a clear error
        # before any data is fetched.
        normalized_regions = [
            _validate_region(cursor, r, business_id) for r in regions
        ]
    finally:
        cursor.close()
        connection.close()

    summary: dict[str, list[dict]] = {}
    for region in normalized_regions:
        result = get_channel_revenue_by_region(region, context)
        summary[region] = result["channels"]
    return summary
