import mysql.connector
import os
from pathlib import Path
from dotenv import load_dotenv

load_dotenv(Path(__file__).resolve().parents[1] / ".env")

def get_db_connection():

    connection = mysql.connector.connect(
        host=os.getenv("DB_HOST"),
        port=int(os.getenv("DB_PORT", 3306)),
        user=os.getenv("DB_USER"),
        password=os.getenv("DB_PASSWORD"),
        database=os.getenv("DB_NAME"),
        connection_timeout=10
    )

    return connection


if __name__ == "__main__":

    try:
        connection = get_db_connection()

        if connection.is_connected():
            print("MySQL connected successfully!")

        connection.close()

    except mysql.connector.Error as error:
        print("Database connection failed:", error)