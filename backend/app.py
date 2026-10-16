import os
import time
import socket
import random
from contextlib import contextmanager
from datetime import date, timedelta

import psycopg2
from psycopg2.extras import RealDictCursor
from flask import Flask, jsonify, request

app = Flask(__name__)

DB_CONFIG = dict(
    host=os.getenv("DB_HOST", "db"),
    port=int(os.getenv("DB_PORT", "5432")),
    dbname=os.getenv("DB_NAME", "expenses"),
    user=os.getenv("DB_USER", "appuser"),
    password=os.getenv("DB_PASSWORD", "apppass"),
    connect_timeout=5,
)

CATEGORIES = ["Food", "Transport", "Shopping", "Bills", "Entertainment", "Health", "Travel"]

SEED_TITLES = {
    "Food": ["Groceries", "Restaurant dinner", "Coffee", "Lunch with team"],
    "Transport": ["Cab ride", "Fuel", "Metro card recharge"],
    "Shopping": ["New headphones", "Clothes", "Books"],
    "Bills": ["Electricity bill", "Internet bill", "Phone recharge"],
    "Entertainment": ["Movie tickets", "Streaming subscription", "Concert"],
    "Health": ["Pharmacy", "Gym membership", "Doctor visit"],
    "Travel": ["Train tickets", "Hotel booking", "Flight"],
}
SEED_RANGE = {
    "Food": (150, 1800), "Transport": (80, 900), "Shopping": (500, 6000),
    "Bills": (400, 3500), "Entertainment": (200, 2500),
    "Health": (300, 3000), "Travel": (1500, 12000),
}


@contextmanager
def db_cursor():
    """Open a connection, yield a dict cursor, commit, always close."""
    conn = psycopg2.connect(**DB_CONFIG)
    try:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            yield cur
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def init_db(retries=30):
    """Create table + seed demo data. Retries while the DB is starting up."""
    for attempt in range(1, retries + 1):
        try:
            with db_cursor() as cur:
                # advisory lock: if several backend tasks start together, only one seeds
                cur.execute("SELECT pg_advisory_xact_lock(42)")
                cur.execute(
                    """
                    CREATE TABLE IF NOT EXISTS expenses (
                        id        SERIAL PRIMARY KEY,
                        title     VARCHAR(120) NOT NULL,
                        amount    NUMERIC(10,2) NOT NULL CHECK (amount > 0),
                        category  VARCHAR(40) NOT NULL,
                        spent_on  DATE NOT NULL DEFAULT CURRENT_DATE
                    )
                    """
                )
                cur.execute("SELECT COUNT(*) AS c FROM expenses")
                if cur.fetchone()["c"] == 0:
                    random.seed(7)
                    today = date.today()
                    for _ in range(40):
                        cat = random.choice(CATEGORIES)
                        lo, hi = SEED_RANGE[cat]
                        cur.execute(
                            "INSERT INTO expenses (title, amount, category, spent_on) VALUES (%s,%s,%s,%s)",
                            (
                                random.choice(SEED_TITLES[cat]),
                                round(random.uniform(lo, hi), 2),
                                cat,
                                today - timedelta(days=random.randint(0, 150)),
                            ),
                        )
                    print("Seeded demo data", flush=True)
            print("Database ready", flush=True)
            return
        except psycopg2.OperationalError as exc:
            print(f"DB not ready (attempt {attempt}/{retries}): {exc}", flush=True)
            time.sleep(2)
    raise RuntimeError("Could not connect to database")


init_db()


def row_to_json(r):
    return {
        "id": r["id"],
        "title": r["title"],
        "amount": float(r["amount"]),
        "category": r["category"],
        "spent_on": r["spent_on"].isoformat(),
    }


@app.route("/api/health")
def health():
    try:
        with db_cursor() as cur:
            cur.execute("SELECT 1")
        return jsonify(status="ok", db="connected")
    except Exception as exc:
        return jsonify(status="degraded", db="down", error=str(exc)), 503


@app.route("/api/info")
def info():
    return jsonify(backend_container=socket.gethostname(), categories=CATEGORIES)


@app.route("/api/expenses", methods=["GET"])
def list_expenses():
    with db_cursor() as cur:
        cur.execute("SELECT * FROM expenses ORDER BY spent_on DESC, id DESC LIMIT 15")
        return jsonify([row_to_json(r) for r in cur.fetchall()])


@app.route("/api/expenses", methods=["POST"])
def add_expense():
    data = request.get_json(silent=True) or {}
    title = (data.get("title") or "").strip()
    category = data.get("category")
    try:
        amount = float(data.get("amount"))
        spent_on = date.fromisoformat(data.get("spent_on") or date.today().isoformat())
    except (TypeError, ValueError):
        return jsonify(error="Invalid amount or date"), 400
    if not title or len(title) > 120:
        return jsonify(error="Title is required (max 120 chars)"), 400
    if amount <= 0:
        return jsonify(error="Amount must be greater than 0"), 400
    if category not in CATEGORIES:
        return jsonify(error="Unknown category"), 400

    with db_cursor() as cur:
        cur.execute(
            "INSERT INTO expenses (title, amount, category, spent_on) VALUES (%s,%s,%s,%s) RETURNING *",
            (title, amount, category, spent_on),
        )
        return jsonify(row_to_json(cur.fetchone())), 201


@app.route("/api/expenses/<int:expense_id>", methods=["DELETE"])
def delete_expense(expense_id):
    with db_cursor() as cur:
        cur.execute("DELETE FROM expenses WHERE id = %s", (expense_id,))
        if cur.rowcount == 0:
            return jsonify(error="Not found"), 404
    return "", 204


@app.route("/api/summary")
def summary():
    with db_cursor() as cur:
        cur.execute("SELECT COALESCE(SUM(amount),0) AS total, COUNT(*) AS cnt FROM expenses")
        totals = cur.fetchone()

        cur.execute(
            "SELECT COALESCE(SUM(amount),0) AS s FROM expenses "
            "WHERE spent_on >= date_trunc('month', CURRENT_DATE)"
        )
        this_month = cur.fetchone()["s"]

        cur.execute(
            "SELECT category, SUM(amount) AS total FROM expenses "
            "GROUP BY category ORDER BY total DESC"
        )
        by_category = [{"category": r["category"], "total": float(r["total"])} for r in cur.fetchall()]

        cur.execute(
            "SELECT date_trunc('month', spent_on) AS m, SUM(amount) AS total FROM expenses "
            "WHERE spent_on >= date_trunc('month', CURRENT_DATE) - INTERVAL '5 months' "
            "GROUP BY m ORDER BY m"
        )
        by_month = [{"month": r["m"].strftime("%b %y"), "total": float(r["total"])} for r in cur.fetchall()]

    return jsonify(
        total=float(totals["total"]),
        count=totals["cnt"],
        this_month=float(this_month),
        top_category=by_category[0]["category"] if by_category else None,
        by_category=by_category,
        by_month=by_month,
    )


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000)
