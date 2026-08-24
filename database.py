"""MongoDB persistence for the Discord bot."""

from datetime import datetime, timedelta, timezone
import os
from typing import Any

from pymongo import ASCENDING, DESCENDING, MongoClient
from pymongo.collection import Collection


MONGODB_URI = os.getenv("MONGODB_URI")
MONGODB_DATABASE = os.getenv("MONGODB_DATABASE", "payment_bot")

_client: MongoClient[Any] | None = None
_db: Any = None


def setup_database() -> None:
    global _client, _db
    if not MONGODB_URI:
        raise RuntimeError("MONGODB_URI is missing. Add your MongoDB Atlas connection URL to .env.")
    _client = MongoClient(MONGODB_URI, serverSelectionTimeoutMS=10000)
    _client.admin.command("ping")
    _db = _client[MONGODB_DATABASE]
    _db.payment_settings.create_index("user_id", unique=True)
    _db.app_users.create_index("user_id", unique=True)
    _db.auto_responses.create_index(
        [("user_id", ASCENDING), ("response_name", ASCENDING)], unique=True
    )
    _db.ltc_invoices.create_index("invoice_id", unique=True)
    _db.ltc_invoices.create_index([("status", ASCENDING), ("created_at", DESCENDING)])


def _collection(name: str) -> Collection[Any]:
    if _db is None:
        raise RuntimeError("MongoDB is not initialized.")
    return _db[name]


def save_ltc_address(user_id: int, address: str) -> None:
    _collection("payment_settings").update_one(
        {"user_id": user_id}, {"$set": {"ltc_address": address}}, upsert=True
    )


def get_ltc_address(user_id: int) -> str | None:
    row = _collection("payment_settings").find_one({"user_id": user_id}, {"ltc_address": 1})
    return str(row["ltc_address"]) if row and row.get("ltc_address") else None


def save_upi_id(user_id: int, upi_id: str) -> None:
    _collection("payment_settings").update_one(
        {"user_id": user_id}, {"$set": {"upi_id": upi_id}}, upsert=True
    )


def get_upi_id(user_id: int) -> str | None:
    row = _collection("payment_settings").find_one({"user_id": user_id}, {"upi_id": 1})
    return str(row["upi_id"]) if row and row.get("upi_id") else None


def save_qr_image(user_id: int, image_data: bytes, filename: str) -> None:
    _collection("payment_settings").update_one(
        {"user_id": user_id},
        {"$set": {"qr_image": image_data, "qr_filename": filename}},
        upsert=True,
    )


def get_qr_image(user_id: int) -> tuple[bytes, str] | None:
    row = _collection("payment_settings").find_one({"user_id": user_id})
    if not row or row.get("qr_image") is None:
        return None
    return bytes(row["qr_image"]), str(row.get("qr_filename") or "upi-qr.png")


def save_qr2_image(user_id: int, image_data: bytes, filename: str) -> None:
    _collection("payment_settings").update_one(
        {"user_id": user_id},
        {"$set": {"qr2_image": image_data, "qr2_filename": filename}},
        upsert=True,
    )


def get_qr2_image(user_id: int) -> tuple[bytes, str] | None:
    row = _collection("payment_settings").find_one({"user_id": user_id})
    if not row or row.get("qr2_image") is None:
        return None
    return bytes(row["qr2_image"]), str(row.get("qr2_filename") or "upi-qr2.png")


def delete_qr_image(user_id: int) -> bool:
    result = _collection("payment_settings").update_one(
        {"user_id": user_id, "qr_image": {"$exists": True, "$ne": None}},
        {"$unset": {"qr_image": "", "qr_filename": ""}},
    )
    return result.modified_count > 0


def get_bot_stats() -> tuple[int, int, int, int, int]:
    payment = _collection("payment_settings")
    responses = _collection("auto_responses")
    return (
        _collection("app_users").count_documents({}),
        payment.count_documents({"ltc_address": {"$exists": True, "$nin": [None, ""]}}),
        payment.count_documents({"upi_id": {"$exists": True, "$nin": [None, ""]}}),
        responses.count_documents({}),
        len(responses.distinct("user_id")),
    )


def save_auto_response(user_id: int, response_name: str, response_text: str) -> None:
    _collection("auto_responses").update_one(
        {"user_id": user_id, "response_name": response_name},
        {"$set": {"response_text": response_text}},
        upsert=True,
    )


def get_auto_response(user_id: int, response_name: str) -> str | None:
    row = _collection("auto_responses").find_one(
        {"user_id": user_id, "response_name": response_name}, {"response_text": 1}
    )
    return str(row["response_text"]) if row else None


def get_auto_response_names(user_id: int) -> list[str]:
    rows = _collection("auto_responses").find(
        {"user_id": user_id}, {"response_name": 1, "_id": 0}
    ).sort("response_name", ASCENDING)
    return [str(row["response_name"]) for row in rows]


def delete_auto_response(user_id: int, response_name: str) -> bool:
    result = _collection("auto_responses").delete_one(
        {"user_id": user_id, "response_name": response_name}
    )
    return result.deleted_count > 0


def clear_auto_responses(user_id: int) -> int:
    return _collection("auto_responses").delete_many({"user_id": user_id}).deleted_count


def mark_user_seen(user_id: int) -> bool:
    result = _collection("app_users").update_one(
        {"user_id": user_id},
        {"$setOnInsert": {"user_id": user_id, "first_seen_at": datetime.now(timezone.utc)}},
        upsert=True,
    )
    return result.upserted_id is not None


def create_invoice(
    invoice_id: str,
    user_id: int,
    address: str,
    usd_amount: float,
    ltc_amount: float,
    baseline_received: int,
) -> None:
    now = datetime.now(timezone.utc)
    active = _collection("ltc_invoices").find_one(
        {"ltc_address": address, "status": {"$in": ["pending", "detected"]},
         "created_at": {"$gte": now - timedelta(minutes=30)}},
        {"invoice_id": 1},
    )
    if active:
        raise ValueError("an active invoice already exists for this address")
    _collection("ltc_invoices").insert_one(
        {"invoice_id": invoice_id, "user_id": user_id, "ltc_address": address,
         "usd_amount": usd_amount, "ltc_amount": ltc_amount,
         "required_litoshis": round(ltc_amount * 100_000_000),
         "baseline_received": baseline_received, "status": "pending", "transaction_id": None,
         "created_at": now, "paid_at": None, "detected_at": None,
         "message_channel_id": None, "message_id": None}
    )


def save_invoice_message(invoice_id: str, channel_id: int, message_id: int) -> None:
    _collection("ltc_invoices").update_one(
        {"invoice_id": invoice_id}, {"$set": {"message_channel_id": channel_id, "message_id": message_id}}
    )


def get_invoice_message(invoice_id: str) -> tuple[int, int] | None:
    row = _collection("ltc_invoices").find_one({"invoice_id": invoice_id})
    if not row or row.get("message_channel_id") is None or row.get("message_id") is None:
        return None
    return int(row["message_channel_id"]), int(row["message_id"])


def get_invoice_summary() -> dict[str, float | int]:
    invoices = _collection("ltc_invoices")
    rows = list(invoices.find({}, {"status": 1, "usd_amount": 1, "ltc_amount": 1, "created_at": 1}))
    day_ago = datetime.now(timezone.utc) - timedelta(hours=24)
    return {
        "total": len(rows),
        "paid": sum(row.get("status") == "paid" for row in rows),
        "pending": sum(row.get("status") == "pending" and row.get("created_at", datetime.min.replace(tzinfo=timezone.utc)) >= day_ago for row in rows),
        "detected": sum(row.get("status") == "detected" for row in rows),
        "paid_usd": float(sum(row.get("usd_amount", 0) for row in rows if row.get("status") == "paid")),
        "paid_ltc": float(sum(row.get("ltc_amount", 0) for row in rows if row.get("status") == "paid")),
    }


def get_pending_invoices() -> list[tuple[Any, ...]]:
    now = datetime.now(timezone.utc)
    rows = _collection("ltc_invoices").find(
        {"status": {"$in": ["pending", "detected"]}, "created_at": {"$gte": now - timedelta(minutes=30)}}
    )
    return [(row["invoice_id"], row["user_id"], row["ltc_address"], row["usd_amount"], row["required_litoshis"], row["baseline_received"], row["status"]) for row in rows]


def expire_old_invoices() -> list[tuple[str, float]]:
    cutoff = datetime.now(timezone.utc) - timedelta(minutes=30)
    collection = _collection("ltc_invoices")
    rows = list(collection.find({"status": {"$in": ["pending", "detected"]}, "created_at": {"$lt": cutoff}}, {"invoice_id": 1, "usd_amount": 1}))
    collection.update_many({"status": {"$in": ["pending", "detected"]}, "created_at": {"$lt": cutoff}}, {"$set": {"status": "expired"}})
    return [(str(row["invoice_id"]), float(row["usd_amount"])) for row in rows]


def mark_invoice_detected(invoice_id: str, transaction_id: str) -> int | None:
    collection = _collection("ltc_invoices")
    if collection.find_one({"transaction_id": transaction_id, "status": {"$in": ["detected", "paid"]}}):
        return None
    row = collection.find_one_and_update(
        {"invoice_id": invoice_id, "status": "pending"},
        {"$set": {"status": "detected", "transaction_id": transaction_id, "detected_at": datetime.now(timezone.utc)}},
    )
    return int(row["user_id"]) if row else None


def mark_invoice_paid(invoice_id: str, transaction_id: str) -> int | None:
    collection = _collection("ltc_invoices")
    if collection.find_one({"transaction_id": transaction_id, "status": {"$in": ["detected", "paid"]}, "invoice_id": {"$ne": invoice_id}}):
        return None
    row = collection.find_one_and_update(
        {"invoice_id": invoice_id, "status": {"$in": ["pending", "detected"]}},
        {"$set": {"status": "paid", "transaction_id": transaction_id, "paid_at": datetime.now(timezone.utc)}},
    )
    return int(row["user_id"]) if row else None
