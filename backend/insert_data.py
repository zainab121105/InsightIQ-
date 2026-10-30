
import pandas as pd
from database import get_db_connection

# 1. Load cleaned dataset
df = pd.read_csv("backend/data/cleaned_sales.csv")

# 2. Connect to MySQL
connection = get_db_connection()
cursor = connection.cursor()

# 3. SQL INSERT query
query = """
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

# 4. Prepare records
records = []

for _, row in df.iterrows():
    records.append((
        row["Transaction_Id"],
        row["Order_date"],
        row["Product"],
        row["Category"],
        row["Region"],
        row["Sales_channel"],
        int(row["Quantity"]),
        float(row["Unit_price"]),
        float(row["Discount_amount"]),
        row["Status"],
        float(row["Revenue"]),
        row["Order_Month"]
    ))

# 5. Insert records
try:
    cursor.executemany(query, records)
    connection.commit()

    print("Records inserted:", cursor.rowcount)

except Exception as error:
    connection.rollback()
    print("Insertion failed:", error)

finally:
    cursor.close()
    connection.close()
