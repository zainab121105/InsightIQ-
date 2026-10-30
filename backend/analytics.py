
from database import get_db_connection


def _validate_region(cursor, region: str) -> str:
    if not isinstance(region, str) or not region.strip():
        raise ValueError("Region must be a non-empty string.")

    normalized_region = region.strip()
    cursor.execute(
        """
        SELECT 1
        FROM sales_transactions
        WHERE region = %s
        LIMIT 1
        """,
        (normalized_region,),
    )
    if cursor.fetchone() is None:
        raise ValueError(f"Unknown region: {normalized_region}")

    return normalized_region


def _validate_products(cursor, products: list[str]) -> list[str]:
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
        WHERE product IN ({placeholders})
        GROUP BY product
        """,
        tuple(normalized_products),
    )
    known_products = {row["product"] for row in cursor.fetchall()}
    unknown_products = [
        product for product in normalized_products if product not in known_products
    ]
    if unknown_products:
        raise ValueError(f"Unknown product(s): {', '.join(unknown_products)}")

    return normalized_products


# 1. Get overall sales summary
def get_sales_summary():

    connection = get_db_connection()
    cursor = connection.cursor(dictionary=True)

    query = """
        SELECT
            COUNT(*) AS total_transactions,
            SUM(quantity) AS total_quantity,
            SUM(revenue) AS total_revenue,
            AVG(revenue) AS average_revenue
        FROM sales_transactions
    """

    cursor.execute(query)
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


# 2. Get product-wise revenue
def get_product_revenue():

    connection = get_db_connection()
    cursor = connection.cursor(dictionary=True)

    query = """
        SELECT
            product,
            SUM(revenue) AS total_revenue
        FROM sales_transactions
        GROUP BY product
        ORDER BY total_revenue DESC
    """

    cursor.execute(query)
    results = cursor.fetchall()

    cursor.close()
    connection.close()

    for row in results:
        row["total_revenue"] = float(row["total_revenue"])

    return results


# 3. Get region-wise revenue
def get_region_revenue():

    connection = get_db_connection()
    cursor = connection.cursor(dictionary=True)

    query = """
        SELECT
            region,
            SUM(revenue) AS total_revenue
        FROM sales_transactions
        GROUP BY region
        ORDER BY total_revenue DESC
    """

    cursor.execute(query)
    results = cursor.fetchall()

    cursor.close()
    connection.close()

    for row in results:
        row["total_revenue"] = float(row["total_revenue"])

    return results


# 4. Get monthly revenue
def get_monthly_revenue():

    connection = get_db_connection()
    cursor = connection.cursor(dictionary=True)

    query = """
        SELECT
            order_month,
            SUM(revenue) AS total_revenue
        FROM sales_transactions
        GROUP BY order_month
        ORDER BY order_month
    """

    cursor.execute(query)
    results = cursor.fetchall()

    cursor.close()
    connection.close()

    for row in results:
        row["total_revenue"] = float(row["total_revenue"])

    return results


def get_product_revenue_by_region(region: str):
    """Return product performance metrics for one valid region."""
    connection = get_db_connection()
    cursor = connection.cursor(dictionary=True)

    try:
        normalized_region = _validate_region(cursor, region)
        cursor.execute(
            """
            SELECT
                product,
                SUM(revenue) AS total_revenue,
                SUM(quantity) AS total_quantity,
                COUNT(*) AS total_transactions
            FROM sales_transactions
            WHERE region = %s
            GROUP BY product
            ORDER BY total_revenue DESC
            """,
            (normalized_region,),
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
    region: str, products: list[str] | None = None
):
    """Compare selected products, or all products, within one valid region."""
    connection = get_db_connection()
    cursor = connection.cursor(dictionary=True)

    try:
        normalized_region = _validate_region(cursor, region)
        normalized_products = (
            _validate_products(cursor, products) if products is not None else None
        )

        product_filter = ""
        parameters: list[str] = [normalized_region]
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
            WHERE region = %s
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


def get_region_product_performance(region: str):
    """Return a region and its products ordered by revenue."""
    return {
        "region": region.strip() if isinstance(region, str) else region,
        "products": get_product_revenue_by_region(region),
    }


def get_channel_revenue_by_region(region: str):
    """Return revenue, quantity and transaction count per sales channel in one region."""
    connection = get_db_connection()
    cursor = connection.cursor(dictionary=True)

    try:
        normalized_region = _validate_region(cursor, region)
        cursor.execute(
            """
            SELECT
                sales_channel,
                SUM(revenue)       AS total_revenue,
                SUM(quantity)      AS total_quantity,
                COUNT(*)           AS total_transactions
            FROM sales_transactions
            WHERE region = %s
            GROUP BY sales_channel
            ORDER BY total_revenue DESC
            """,
            (normalized_region,),
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


def get_regional_channel_summary(regions: list[str]):
    """Return per-channel revenue, quantity and transaction counts for multiple regions.

    All requested regions are validated before any data is fetched.  The result
    is a dict keyed by region name, each value being the list of channel rows
    returned by get_channel_revenue_by_region.
    """
    if not regions:
        raise ValueError("At least one region must be supplied.")

    connection = get_db_connection()
    cursor = connection.cursor(dictionary=True)

    try:
        # Validate every region up-front so the caller gets a clear error
        # before any data is fetched.
        normalized_regions = [
            _validate_region(cursor, r) for r in regions
        ]
    finally:
        cursor.close()
        connection.close()

    summary: dict[str, list[dict]] = {}
    for region in normalized_regions:
        result = get_channel_revenue_by_region(region)
        summary[region] = result["channels"]
    return summary
