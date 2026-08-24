"""One-time migration of the existing bot_data.sqlite3 into MongoDB."""

import os
import sqlite3
from datetime import datetime, timezone

from dotenv import load_dotenv
from pymongo import MongoClient

load_dotenv()

sqlite_path = os.getenv("SQLITE_PATH", "bot_data.sqlite3")
mongodb_uri = os.getenv("MONGODB_URI")
database_name = os.getenv("MONGODB_DATABASE", "payment_bot")

if not mongodb_uri:
    raise RuntimeError("MONGODB_URI is missing in .env")

client = MongoClient(mongodb_uri)
db = client[database_name]
connection = sqlite3.connect(sqlite_path)
connection.row_factory = sqlite3.Row


def rows(table: str) -> list[dict]:
    return [dict(row) for row in connection.execute(f"SELECT * FROM {table}").fetchall()]


def replace(collection: str, documents: list[dict]) -> None:
    if not documents:
        return
    db[collection].delete_many({})
    db[collection].insert_many(documents)


payment_documents = rows("payment_settings")
for document in payment_documents:
    document.pop("_id", None)
replace("payment_settings", payment_documents)

user_documents = rows("app_users")
for document in user_documents:
    document["first_seen_at"] = datetime.now(timezone.utc)
replace("app_users", user_documents)

response_documents = rows("auto_responses")
replace("auto_responses", response_documents)

invoice_documents = rows("ltc_invoices")
for document in invoice_documents:
    for field in ("created_at", "paid_at", "detected_at"):
        value = document.get(field)
        if isinstance(value, str):
            try:
                document[field] = datetime.fromisoformat(value.replace(" ", "T")).replace(tzinfo=timezone.utc)
            except ValueError:
                document[field] = None
replace("ltc_invoices", invoice_documents)

print(f"Migrated {len(payment_documents)} payment settings")
print(f"Migrated {len(user_documents)} app users")
print(f"Migrated {len(response_documents)} auto-responses")
print(f"Migrated {len(invoice_documents)} invoices")
connection.close()
client.close()
