import os
import re
import time
import html
import hmac
import hashlib
import secrets
import smtplib
import socket
import random
from contextlib import contextmanager
from datetime import date, datetime, timedelta, timezone
from email.message import EmailMessage
from functools import wraps

import jwt
import psycopg2
from psycopg2.extras import RealDictCursor
from flask import Flask, jsonify, request, make_response, g
from werkzeug.security import generate_password_hash, check_password_hash

app = Flask(__name__)

# ---------------- configuration ----------------
DB_CONFIG = dict(
    host=os.getenv("DB_HOST", "db"),
    port=int(os.getenv("DB_PORT", "5432")),
    dbname=os.getenv("DB_NAME", "expenses"),
    user=os.getenv("DB_USER", "appuser"),
    password=os.getenv("DB_PASSWORD", "apppass"),
    connect_timeout=5,
)

JWT_SECRET = os.getenv("JWT_SECRET", "dev-secret-change-me")
JWT_HOURS = int(os.getenv("JWT_EXP_HOURS", "8"))
COOKIE_SECURE = os.getenv("COOKIE_SECURE", "false").lower() == "true"
COOKIE_NAME = "spendsight_token"

# --- email verification ---
EMAIL_BACKEND = os.getenv("EMAIL_BACKEND", "console")   # console | smtp | ses
EMAIL_FROM = os.getenv("EMAIL_FROM", "SpendSight <no-reply@example.com>")
SMTP_HOST = os.getenv("SMTP_HOST", "mailpit")
SMTP_PORT = int(os.getenv("SMTP_PORT", "1025"))
SMTP_USER = os.getenv("SMTP_USER", "")
SMTP_PASSWORD = os.getenv("SMTP_PASSWORD", "")
SMTP_STARTTLS = os.getenv("SMTP_STARTTLS", "false").lower() == "true"
SES_REGION = os.getenv("SES_REGION", "")
CODE_TTL_MIN = int(os.getenv("CODE_TTL_MINUTES", "10"))
CODE_MAX_ATTEMPTS = 5
RESEND_COOLDOWN_SEC = 60

if JWT_SECRET == "dev-secret-change-me":
    print("WARNING: using the default JWT_SECRET. Set a long random JWT_SECRET for real use.", flush=True)
print(f"Email backend: {EMAIL_BACKEND}", flush=True)

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

EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
DUMMY_HASH = generate_password_hash("not-a-real-password")


# ---------------- database helpers ----------------
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


def seed_demo(cur, user_id, rows=35):
    """Insert sample expenses for one user (different data per user)."""
    rng = random.Random(user_id)
    today = date.today()
    for _ in range(rows):
        cat = rng.choice(CATEGORIES)
        lo, hi = SEED_RANGE[cat]
        cur.execute(
            "INSERT INTO expenses (user_id, title, amount, category, spent_on) VALUES (%s,%s,%s,%s,%s)",
            (user_id, rng.choice(SEED_TITLES[cat]), round(rng.uniform(lo, hi), 2), cat,
             today - timedelta(days=rng.randint(0, 150))),
        )


def init_db(retries=30):
    """Create / upgrade tables. Retries while the DB is starting up."""
    for attempt in range(1, retries + 1):
        try:
            with db_cursor() as cur:
                # advisory lock: if several backend tasks start together, only one migrates
                cur.execute("SELECT pg_advisory_xact_lock(42)")
                cur.execute(
                    """
                    CREATE TABLE IF NOT EXISTS users (
                        id            SERIAL PRIMARY KEY,
                        email         VARCHAR(254) UNIQUE NOT NULL,
                        name          VARCHAR(80)  NOT NULL,
                        password_hash TEXT         NOT NULL,
                        created_at    TIMESTAMPTZ  NOT NULL DEFAULT NOW()
                    )
                    """
                )
                # Existing users (created before verification existed) are treated as verified;
                # users created from now on default to NOT verified.
                cur.execute("ALTER TABLE users ADD COLUMN IF NOT EXISTS email_verified BOOLEAN NOT NULL DEFAULT TRUE")
                cur.execute("ALTER TABLE users ALTER COLUMN email_verified SET DEFAULT FALSE")
                cur.execute(
                    """
                    CREATE TABLE IF NOT EXISTS email_codes (
                        user_id      INTEGER PRIMARY KEY REFERENCES users(id) ON DELETE CASCADE,
                        code_hash    TEXT        NOT NULL,
                        expires_at   TIMESTAMPTZ NOT NULL,
                        attempts     INTEGER     NOT NULL DEFAULT 0,
                        last_sent_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
                    )
                    """
                )
                cur.execute(
                    """
                    CREATE TABLE IF NOT EXISTS expenses (
                        id        SERIAL PRIMARY KEY,
                        title     VARCHAR(120)  NOT NULL,
                        amount    NUMERIC(10,2) NOT NULL CHECK (amount > 0),
                        category  VARCHAR(40)   NOT NULL,
                        spent_on  DATE          NOT NULL DEFAULT CURRENT_DATE
                    )
                    """
                )
                cur.execute(
                    "ALTER TABLE expenses ADD COLUMN IF NOT EXISTS "
                    "user_id INTEGER REFERENCES users(id) ON DELETE CASCADE"
                )
                cur.execute(
                    "CREATE INDEX IF NOT EXISTS idx_expenses_user_date ON expenses (user_id, spent_on)"
                )
            print("Database ready", flush=True)
            return
        except psycopg2.OperationalError as exc:
            print(f"DB not ready (attempt {attempt}/{retries}): {exc}", flush=True)
            time.sleep(2)
    raise RuntimeError("Could not connect to database")


init_db()


# ---------------- email sending ----------------
_ses_client = None


def send_email(to, subject, text_body, html_body):
    """Send one email using the configured backend. Returns True on success."""
    try:
        if EMAIL_BACKEND == "console":
            # Learning/dev only: the message (including the code) is written to the container log.
            print(f"[EMAIL console] to={to} subject={subject}\n{text_body}", flush=True)
        elif EMAIL_BACKEND == "smtp":
            msg = EmailMessage()
            msg["From"], msg["To"], msg["Subject"] = EMAIL_FROM, to, subject
            msg.set_content(text_body)
            msg.add_alternative(html_body, subtype="html")
            with smtplib.SMTP(SMTP_HOST, SMTP_PORT, timeout=10) as smtp:
                if SMTP_STARTTLS:
                    smtp.starttls()
                if SMTP_USER:
                    smtp.login(SMTP_USER, SMTP_PASSWORD)
                smtp.send_message(msg)
        elif EMAIL_BACKEND == "ses":
            global _ses_client
            if _ses_client is None:
                import boto3
                _ses_client = boto3.client("sesv2", region_name=SES_REGION or None)
            _ses_client.send_email(
                FromEmailAddress=EMAIL_FROM,
                Destination={"ToAddresses": [to]},
                Content={"Simple": {
                    "Subject": {"Data": subject, "Charset": "UTF-8"},
                    "Body": {"Text": {"Data": text_body, "Charset": "UTF-8"},
                             "Html": {"Data": html_body, "Charset": "UTF-8"}},
                }},
            )
        else:
            print(f"Unknown EMAIL_BACKEND '{EMAIL_BACKEND}'", flush=True)
            return False
        return True
    except Exception as exc:  # never crash the request because email failed
        print(f"Email send failed ({EMAIL_BACKEND}): {exc}", flush=True)
        return False


def send_code_email(user, code):
    subject = "Your SpendSight verification code"
    text_body = (
        f"Hi {user['name']},\n\n"
        f"Your verification code is: {code}\n\n"
        f"It expires in {CODE_TTL_MIN} minutes. If you did not create a SpendSight account, "
        f"you can ignore this email.\n"
    )
    html_body = (
        '<div style="font-family:Arial,sans-serif;max-width:420px;margin:auto;padding:24px;'
        'border:1px solid #e5e7eb;border-radius:12px">'
        '<h2 style="margin:0 0 8px">Verify your email</h2>'
        f"<p>Hi {html.escape(user['name'])}, use this code to finish creating your SpendSight account:</p>"
        f'<p style="font-size:32px;letter-spacing:8px;font-weight:700;margin:18px 0">{code}</p>'
        f'<p style="color:#6b7280;font-size:13px">The code expires in {CODE_TTL_MIN} minutes. '
        "If you did not sign up, ignore this email.</p></div>"
    )
    return send_email(user["email"], subject, text_body, html_body)


# ---------------- verification codes ----------------
def hash_code(user_id, code):
    return hmac.new(JWT_SECRET.encode(), f"{user_id}:{code}".encode(), hashlib.sha256).hexdigest()


def create_code(cur, user_id):
    """Store a fresh 6-digit code (only its hash). Returns the code, or None while the resend
    cool-down is active (the previous code stays valid)."""
    code = f"{secrets.randbelow(10 ** 6):06d}"
    cur.execute(
        """
        INSERT INTO email_codes (user_id, code_hash, expires_at, attempts, last_sent_at)
        VALUES (%s, %s, NOW() + make_interval(mins => %s), 0, NOW())
        ON CONFLICT (user_id) DO UPDATE
           SET code_hash = EXCLUDED.code_hash, expires_at = EXCLUDED.expires_at,
               attempts = 0, last_sent_at = NOW()
         WHERE email_codes.last_sent_at < NOW() - make_interval(secs => %s)
        RETURNING user_id
        """,
        (user_id, hash_code(user_id, code), CODE_TTL_MIN, RESEND_COOLDOWN_SEC),
    )
    return code if cur.fetchone() else None


# ---------------- auth helpers ----------------
def make_token(user):
    now = datetime.now(timezone.utc)
    payload = {
        "sub": str(user["id"]),
        "name": user["name"],
        "email": user["email"],
        "iat": now,
        "exp": now + timedelta(hours=JWT_HOURS),
    }
    return jwt.encode(payload, JWT_SECRET, algorithm="HS256")


def auth_response(user, status=200):
    resp = make_response(jsonify(id=user["id"], name=user["name"], email=user["email"]), status)
    resp.set_cookie(
        COOKIE_NAME, make_token(user),
        max_age=JWT_HOURS * 3600, httponly=True, samesite="Lax",
        secure=COOKIE_SECURE, path="/",
    )
    return resp


def login_required(fn):
    @wraps(fn)
    def wrapper(*args, **kwargs):
        token = request.cookies.get(COOKIE_NAME)
        if not token:
            return jsonify(error="Not signed in"), 401
        try:
            payload = jwt.decode(token, JWT_SECRET, algorithms=["HS256"])
            g.user = {
                "id": int(payload["sub"]),
                "name": payload.get("name", ""),
                "email": payload.get("email", ""),
            }
        except (jwt.PyJWTError, KeyError, ValueError):
            resp = make_response(jsonify(error="Session expired, please sign in again"), 401)
            resp.delete_cookie(COOKIE_NAME, path="/")
            return resp
        return fn(*args, **kwargs)
    return wrapper


# ---------------- auth routes ----------------
@app.route("/api/auth/register", methods=["POST"])
def register():
    data = request.get_json(silent=True) or {}
    name = str(data.get("name") or "").strip()
    email = str(data.get("email") or "").strip().lower()
    password = data.get("password")

    if not name or len(name) > 80:
        return jsonify(error="Please enter your name (max 80 characters)"), 400
    if len(email) > 254 or not EMAIL_RE.match(email):
        return jsonify(error="Please enter a valid email address"), 400
    if not isinstance(password, str) or not (8 <= len(password) <= 128):
        return jsonify(error="Password must be 8 to 128 characters"), 400

    code = None
    with db_cursor() as cur:
        # A VERIFIED email cannot be re-registered. An UNVERIFIED one can: nobody has proven they
        # own it yet, so the latest sign-up replaces the pending one and gets a new code.
        cur.execute(
            """
            INSERT INTO users (email, name, password_hash) VALUES (%s, %s, %s)
            ON CONFLICT (email) DO UPDATE
               SET name = EXCLUDED.name, password_hash = EXCLUDED.password_hash
             WHERE users.email_verified = FALSE
            RETURNING id, email, name
            """,
            (email, name, generate_password_hash(password)),
        )
        user = cur.fetchone()
        if user:
            code = create_code(cur, user["id"])
    if not user:
        return jsonify(error="An account with this email already exists"), 409

    email_sent = send_code_email(user, code) if code else True   # cool-down: earlier code still valid
    return jsonify(status="verification_required", email=email, email_sent=email_sent), 201


@app.route("/api/auth/verify", methods=["POST"])
def verify_email():
    data = request.get_json(silent=True) or {}
    email = str(data.get("email") or "").strip().lower()
    code = str(data.get("code") or "").strip()
    if not email or not re.fullmatch(r"\d{6}", code):
        return jsonify(error="Enter the 6-digit code"), 400

    with db_cursor() as cur:
        cur.execute(
            """
            SELECT u.id, u.email, u.name, u.email_verified,
                   c.code_hash, c.attempts, (c.expires_at > NOW()) AS fresh
              FROM users u LEFT JOIN email_codes c ON c.user_id = u.id
             WHERE u.email = %s
            """,
            (email,),
        )
        row = cur.fetchone()
        if not row:
            return jsonify(error="Invalid or expired code"), 400
        if row["email_verified"]:
            return jsonify(error="This email is already verified. Please sign in."), 400
        if not row["code_hash"] or not row["fresh"]:
            return jsonify(error="That code has expired. Request a new one."), 400
        if row["attempts"] >= CODE_MAX_ATTEMPTS:
            cur.execute("DELETE FROM email_codes WHERE user_id = %s", (row["id"],))
            return jsonify(error="Too many wrong attempts. Request a new code."), 429

        if not hmac.compare_digest(row["code_hash"], hash_code(row["id"], code)):
            left = CODE_MAX_ATTEMPTS - row["attempts"] - 1
            if left <= 0:
                cur.execute("DELETE FROM email_codes WHERE user_id = %s", (row["id"],))
                return jsonify(error="Too many wrong attempts. Request a new code."), 429
            cur.execute("UPDATE email_codes SET attempts = attempts + 1 WHERE user_id = %s", (row["id"],))
            return jsonify(error=f"Incorrect code. {left} attempt(s) left."), 400

        cur.execute("UPDATE users SET email_verified = TRUE WHERE id = %s", (row["id"],))
        cur.execute("DELETE FROM email_codes WHERE user_id = %s", (row["id"],))
        user = {"id": row["id"], "email": row["email"], "name": row["name"]}
    return auth_response(user)


@app.route("/api/auth/resend", methods=["POST"])
def resend_code():
    data = request.get_json(silent=True) or {}
    email = str(data.get("email") or "").strip().lower()
    user, code = None, None
    if email:
        with db_cursor() as cur:
            cur.execute("SELECT id, email, name, email_verified FROM users WHERE email = %s", (email,))
            user = cur.fetchone()
            if user and not user["email_verified"]:
                code = create_code(cur, user["id"])
    if code:
        send_code_email(user, code)
    # same answer whether or not the account exists, so this cannot be used to probe emails
    return jsonify(status="sent")


@app.route("/api/auth/login", methods=["POST"])
def login():
    data = request.get_json(silent=True) or {}
    email = str(data.get("email") or "").strip().lower()
    password = data.get("password")
    if not isinstance(password, str) or not email:
        return jsonify(error="Email and password are required"), 400

    code = None
    with db_cursor() as cur:
        cur.execute(
            "SELECT id, email, name, password_hash, email_verified FROM users WHERE email = %s", (email,)
        )
        user = cur.fetchone()
        password_ok = check_password_hash(user["password_hash"] if user else DUMMY_HASH, password)
        if user and password_ok and not user["email_verified"]:
            code = create_code(cur, user["id"])

    if not user or not password_ok:
        return jsonify(error="Invalid email or password"), 401
    if not user["email_verified"]:
        if code:
            send_code_email(user, code)
        return jsonify(error="Please verify your email to continue.", code="email_not_verified", email=email), 403
    return auth_response(user)


@app.route("/api/auth/logout", methods=["POST"])
def logout():
    resp = make_response("", 204)
    resp.delete_cookie(COOKIE_NAME, path="/")
    return resp


@app.route("/api/auth/me")
@login_required
def me():
    return jsonify(g.user)


# ---------------- public routes ----------------
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


# ---------------- protected routes (each user sees only their own data) ----------------
@app.route("/api/expenses", methods=["GET"])
@login_required
def list_expenses():
    with db_cursor() as cur:
        cur.execute(
            "SELECT * FROM expenses WHERE user_id = %s ORDER BY spent_on DESC, id DESC LIMIT 15",
            (g.user["id"],),
        )
        return jsonify([row_to_json(r) for r in cur.fetchall()])


@app.route("/api/expenses", methods=["POST"])
@login_required
def add_expense():
    data = request.get_json(silent=True) or {}
    title = str(data.get("title") or "").strip()
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
            "INSERT INTO expenses (user_id, title, amount, category, spent_on) "
            "VALUES (%s,%s,%s,%s,%s) RETURNING *",
            (g.user["id"], title, amount, category, spent_on),
        )
        return jsonify(row_to_json(cur.fetchone())), 201


@app.route("/api/expenses/<int:expense_id>", methods=["DELETE"])
@login_required
def delete_expense(expense_id):
    with db_cursor() as cur:
        cur.execute("DELETE FROM expenses WHERE id = %s AND user_id = %s", (expense_id, g.user["id"]))
        if cur.rowcount == 0:
            return jsonify(error="Not found"), 404
    return "", 204


@app.route("/api/demo-data", methods=["POST"])
@login_required
def demo_data():
    with db_cursor() as cur:
        cur.execute("SELECT COUNT(*) AS c FROM expenses WHERE user_id = %s", (g.user["id"],))
        if cur.fetchone()["c"] > 0:
            return jsonify(error="You already have expenses"), 409
        seed_demo(cur, g.user["id"])
    return jsonify(status="ok"), 201


@app.route("/api/summary")
@login_required
def summary():
    uid = g.user["id"]
    with db_cursor() as cur:
        cur.execute(
            "SELECT COALESCE(SUM(amount),0) AS total, COUNT(*) AS cnt FROM expenses WHERE user_id = %s",
            (uid,),
        )
        totals = cur.fetchone()

        cur.execute(
            "SELECT COALESCE(SUM(amount),0) AS s FROM expenses "
            "WHERE user_id = %s AND spent_on >= date_trunc('month', CURRENT_DATE)",
            (uid,),
        )
        this_month = cur.fetchone()["s"]

        cur.execute(
            "SELECT category, SUM(amount) AS total FROM expenses WHERE user_id = %s "
            "GROUP BY category ORDER BY total DESC",
            (uid,),
        )
        by_category = [{"category": r["category"], "total": float(r["total"])} for r in cur.fetchall()]

        cur.execute(
            "SELECT date_trunc('month', spent_on) AS m, SUM(amount) AS total FROM expenses "
            "WHERE user_id = %s AND spent_on >= date_trunc('month', CURRENT_DATE) - INTERVAL '5 months' "
            "GROUP BY m ORDER BY m",
            (uid,),
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
