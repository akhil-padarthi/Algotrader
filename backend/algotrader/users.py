"""User accounts: profiles, passwords, log-in sessions, each person's Alpaca keys and purchases.

Everything lives in the SQLite file ``backend/data/users.db``, or in PostgreSQL (for example Supabase)
when DATABASE_URL is set (see db.py):

- ``users``           name, email, password hash, admin flag, the person's Alpaca paper keys (encrypted)
- ``sessions``        who is logged in on which browser (only a hash of each session token is stored)
- ``login_attempts``  recent log-in and sign-up attempts, to slow down password guessing
- ``trades``          every buy and sell a person placed from the dashboard

Passwords are stored as scrypt hashes (werkzeug), never as text. Alpaca keys are encrypted with
Fernet (AES + HMAC, from the ``cryptography`` package) using a key kept outside the database in
``backend/data/secret.key`` (or the SECRET_KEY setting), so a copy of the database alone doesn't
expose anyone's keys.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import os
import re
import secrets
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path

from werkzeug.security import check_password_hash, generate_password_hash

from .db import Database

SESSION_COOKIE = "algotrader_session"
LOGIN_WINDOW = timedelta(minutes=15)
MAX_FAILED_PER_EMAIL = 8          # wrong passwords for one account within LOGIN_WINDOW
MAX_FAILED_PER_IP = 30            # wrong passwords from one address within LOGIN_WINDOW
MAX_SIGNUPS_PER_IP = 10           # new accounts from one address per hour
MIN_PASSWORD, MAX_PASSWORD = 8, 200
EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s.]+$")
COMMON_PASSWORDS = {
    "password", "password1", "password123", "passw0rd", "12345678", "123456789", "1234567890", "11111111",
    "00000000", "87654321", "qwerty123", "qwertyuiop", "iloveyou", "abc12345", "abcd1234", "letmein1",
    "welcome1", "admin123", "trustno1", "sunshine", "football", "baseball", "superman", "princess",
    "starwars", "whatever", "trading123", "stocks123", "algotrader", "changeme",
}

TABLES = ["users", "sessions", "login_attempts", "trades"]
SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    email TEXT NOT NULL UNIQUE COLLATE NOCASE,
    name TEXT NOT NULL,
    password_hash TEXT NOT NULL,
    is_admin INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    last_login_at TEXT,
    alpaca_key_enc TEXT,
    alpaca_secret_enc TEXT,
    alpaca_key_fp TEXT,
    alpaca_url TEXT,
    alpaca_account TEXT,
    alpaca_linked_at TEXT
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_users_alpaca_fp ON users(alpaca_key_fp) WHERE alpaca_key_fp IS NOT NULL;
CREATE TABLE IF NOT EXISTS sessions (
    token_hash TEXT PRIMARY KEY,
    user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    created_at TEXT NOT NULL,
    expires_at TEXT NOT NULL,
    last_seen_at TEXT NOT NULL,
    user_agent TEXT
);
CREATE INDEX IF NOT EXISTS idx_sessions_user ON sessions(user_id);
CREATE TABLE IF NOT EXISTS login_attempts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    kind TEXT NOT NULL,
    email TEXT,
    ip TEXT,
    ok INTEGER NOT NULL,
    at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_attempts_at ON login_attempts(at);
CREATE TABLE IF NOT EXISTS trades (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    created_at TEXT NOT NULL,
    symbol TEXT NOT NULL,
    side TEXT NOT NULL,
    amount REAL,
    qty REAL,
    price REAL,
    alpaca_order_id TEXT,
    status TEXT,
    message TEXT
);
CREATE INDEX IF NOT EXISTS idx_trades_user ON trades(user_id, id);
"""


class AccountError(ValueError):
    """A problem the person can fix, with the HTTP status the API should answer with."""

    def __init__(self, message: str, status: int = 400):
        super().__init__(message)
        self.message = message
        self.status = status


def _now() -> datetime:
    return datetime.now(timezone.utc).replace(microsecond=0)


def _iso(dt: datetime) -> str:
    return dt.isoformat()


def _token_hash(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def mask_account(number: str | None) -> str:
    number = str(number or "")
    return ("•••" + number[-4:]) if len(number) > 4 else number


# --------------------------------------------------------------------------- validation
def normalize_email(email: str) -> str:
    email = (email or "").strip().lower()
    if len(email) > 254 or not EMAIL_RE.match(email):
        raise AccountError("Enter a valid email address, like you@example.com.")
    return email


def clean_name(name: str) -> str:
    name = " ".join((name or "").split())
    if not name:
        raise AccountError("Enter your name.")
    if len(name) > 60:
        raise AccountError("Use a name of 60 characters or fewer.")
    if any(ord(ch) < 32 for ch in name):
        raise AccountError("Your name contains characters that aren't allowed.")
    return name


def check_password_rules(password: str, email: str = "", name: str = "") -> None:
    if not isinstance(password, str) or len(password) < MIN_PASSWORD:
        raise AccountError(f"Use at least {MIN_PASSWORD} characters for your password.")
    if len(password) > MAX_PASSWORD:
        raise AccountError(f"Use a password of {MAX_PASSWORD} characters or fewer.")
    low = password.lower()
    if low in COMMON_PASSWORDS or len(set(password)) < 3:
        raise AccountError("That password is too easy to guess. Choose something less common.")
    if email and low == email.lower():
        raise AccountError("Your password can't be your email address.")
    if name and low == name.lower():
        raise AccountError("Your password can't be your name.")


# --------------------------------------------------------------------------- encryption
class SecretBox:
    """Encrypts stored Alpaca keys. The key material comes from SECRET_KEY or backend/data/secret.key."""

    def __init__(self, material: str):
        try:
            from cryptography.fernet import Fernet
        except ImportError as exc:  # pragma: no cover - depends on the install
            raise RuntimeError("User accounts need the 'cryptography' package. In the backend folder run: "
                               "pip install -r requirements.txt") from exc
        digest = hashlib.sha256(("fernet:" + material).encode("utf-8")).digest()
        self._fernet = Fernet(base64.urlsafe_b64encode(digest))
        self._fp_key = hashlib.sha256(("fingerprint:" + material).encode("utf-8")).digest()

    @classmethod
    def from_settings(cls, settings) -> "SecretBox":
        if settings.secret_key:
            return cls(settings.secret_key)
        return cls(load_or_create_key_file(settings.secret_key_path))

    def encrypt(self, text: str) -> str:
        return self._fernet.encrypt(text.encode("utf-8")).decode("ascii")

    def decrypt(self, token: str) -> str:
        from cryptography.fernet import InvalidToken
        try:
            return self._fernet.decrypt(token.encode("ascii")).decode("utf-8")
        except (InvalidToken, ValueError) as exc:
            raise AccountError("Your saved Alpaca keys can't be read anymore (the server's secret key changed). "
                               "Connect your Alpaca paper account again.", 409) from exc

    def fingerprint(self, text: str) -> str:
        """Same keys -> same fingerprint, without storing the keys in a readable form."""
        return hmac.new(self._fp_key, text.encode("utf-8"), hashlib.sha256).hexdigest()


def load_or_create_key_file(path: Path) -> str:
    path = Path(path)
    if path.exists():
        material = path.read_text(encoding="utf-8").strip()
        if material:
            return material
    path.parent.mkdir(parents=True, exist_ok=True)
    material = secrets.token_urlsafe(48)
    try:
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError:   # another process created it a moment ago
        return path.read_text(encoding="utf-8").strip()
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        fh.write(material + "\n")
    return material


# --------------------------------------------------------------------------- the store
class UserStore:
    def __init__(self, target: Path | str, box: SecretBox, session_days: int = 30):
        """target: a SQLite file path, or a postgresql:// URL (DATABASE_URL)."""
        self.db = Database(target)
        self.box = box
        self.session_days = session_days
        self._lock = threading.Lock()
        # spend the same time on unknown emails as on real ones, so timing doesn't reveal who has an account
        self._dummy_hash = generate_password_hash(secrets.token_hex(8))
        self.db.create_tables(SCHEMA, TABLES)

    def _conn(self):
        return self.db.connect()

    # ------------------------------------------------------------------ views
    @staticmethod
    def public(row) -> dict:
        """What the dashboard may see about a person (never the password hash or keys)."""
        return {
            "id": row["id"], "email": row["email"], "name": row["name"], "is_admin": bool(row["is_admin"]),
            "created_at": row["created_at"], "last_login_at": row["last_login_at"],
            "broker": {
                "linked": bool(row["alpaca_key_enc"]),
                "account": row["alpaca_account"],
                "linked_at": row["alpaca_linked_at"],
            },
        }

    def _row(self, c, user_id: int):
        return c.execute("SELECT * FROM users WHERE id=?", (user_id,)).fetchone()

    def get(self, user_id: int) -> dict | None:
        with self._conn() as c:
            row = self._row(c, user_id)
            return self.public(row) if row else None

    def by_email(self, email: str) -> dict | None:
        with self._conn() as c:
            row = c.execute("SELECT * FROM users WHERE email=?", ((email or "").strip().lower(),)).fetchone()
            return self.public(row) if row else None

    def count(self) -> int:
        with self._conn() as c:
            return int(c.execute("SELECT COUNT(*) FROM users").fetchone()[0])

    def list_users(self) -> list[dict]:
        with self._conn() as c:
            return [self.public(r) for r in c.execute("SELECT * FROM users ORDER BY id")]

    # ------------------------------------------------------------ sign up / in
    def create_user(self, email: str, name: str, password: str, ip: str = "", is_admin: bool = False) -> dict:
        email, name = normalize_email(email), clean_name(name)
        check_password_rules(password, email, name)
        now = _now()
        with self._lock, self._conn() as c:
            if ip:
                recent = c.execute(
                    "SELECT COUNT(*) FROM login_attempts WHERE kind='signup' AND ip=? AND at>=?",
                    (ip, _iso(now - timedelta(hours=1)))).fetchone()[0]
                if recent >= MAX_SIGNUPS_PER_IP:
                    raise AccountError("Too many new accounts from this network. Try again in an hour.", 429)
            if c.execute("SELECT 1 FROM users WHERE email=?", (email,)).fetchone():
                raise AccountError("An account with this email already exists. Log in instead.", 409)
            try:
                new_id = c.insert(
                    "INSERT INTO users (email, name, password_hash, is_admin, created_at, updated_at) VALUES (?,?,?,?,?,?)",
                    (email, name, generate_password_hash(password), int(is_admin), _iso(now), _iso(now)))
            except self.db.integrity_errors as exc:    # someone signed up with it a moment ago
                raise AccountError("An account with this email already exists. Log in instead.", 409) from exc
            c.execute("INSERT INTO login_attempts (kind, email, ip, ok, at) VALUES ('signup', ?, ?, 1, ?)",
                      (email, ip, _iso(now)))
            return self.public(self._row(c, new_id))

    def authenticate(self, email: str, password: str, ip: str = "") -> dict:
        """The person's profile if the email and password match. Repeated failures are slowed down."""
        email = (email or "").strip().lower()
        if not isinstance(password, str) or len(password) > MAX_PASSWORD:
            password = ""          # never spend hashing time on absurdly long input
        now = _now()
        since = _iso(now - LOGIN_WINDOW)
        with self._conn() as c:
            by_email = c.execute("SELECT COUNT(*) FROM login_attempts WHERE kind='login' AND ok=0 AND email=? "
                                 "AND at>=?", (email, since)).fetchone()[0]
            by_ip = c.execute("SELECT COUNT(*) FROM login_attempts WHERE kind='login' AND ok=0 AND ip=? AND at>=?",
                              (ip, since)).fetchone()[0] if ip else 0
            if by_email >= MAX_FAILED_PER_EMAIL or by_ip >= MAX_FAILED_PER_IP:
                raise AccountError("Too many wrong passwords. Wait 15 minutes, then try again.", 429)
            row = c.execute("SELECT * FROM users WHERE email=?", (email,)).fetchone()
        ok = check_password_hash(row["password_hash"] if row else self._dummy_hash, password or "")
        ok = bool(row) and ok
        with self._lock, self._conn() as c:
            c.execute("INSERT INTO login_attempts (kind, email, ip, ok, at) VALUES ('login', ?, ?, ?, ?)",
                      (email, ip, int(ok), _iso(now)))
            c.execute("DELETE FROM login_attempts WHERE at<?", (_iso(now - timedelta(days=2)),))
            if ok:
                c.execute("UPDATE users SET last_login_at=? WHERE id=?", (_iso(now), row["id"]))
                user = self.public(self._row(c, row["id"]))
        if not ok:      # raised after the block, so the failed attempt is saved (an error inside would roll it back)
            raise AccountError("Email or password is incorrect.", 401)
        return user

    # ---------------------------------------------------------------- sessions
    def create_session(self, user_id: int, user_agent: str = "") -> str:
        token = secrets.token_urlsafe(32)
        now = _now()
        with self._lock, self._conn() as c:
            c.execute("DELETE FROM sessions WHERE expires_at<?", (_iso(now),))
            c.execute("INSERT INTO sessions (token_hash, user_id, created_at, expires_at, last_seen_at, user_agent) "
                      "VALUES (?,?,?,?,?,?)",
                      (_token_hash(token), user_id, _iso(now), _iso(now + timedelta(days=self.session_days)),
                       _iso(now), (user_agent or "")[:200]))
        return token

    def user_for_session(self, token: str | None) -> dict | None:
        if not token or len(token) > 200:
            return None
        now = _now()
        with self._conn() as c:
            row = c.execute(
                "SELECT u.*, s.expires_at AS s_expires, s.last_seen_at AS s_seen FROM sessions s "
                "JOIN users u ON u.id = s.user_id WHERE s.token_hash=?", (_token_hash(token),)).fetchone()
            if row is None:
                return None
            if row["s_expires"] < _iso(now):
                c.execute("DELETE FROM sessions WHERE token_hash=?", (_token_hash(token),))
                return None
            if row["s_seen"] < _iso(now - timedelta(minutes=5)):
                c.execute("UPDATE sessions SET last_seen_at=? WHERE token_hash=?", (_iso(now), _token_hash(token)))
            return self.public(row)

    def end_session(self, token: str | None) -> None:
        if token:
            with self._lock, self._conn() as c:
                c.execute("DELETE FROM sessions WHERE token_hash=?", (_token_hash(token),))

    def end_other_sessions(self, user_id: int, keep_token: str | None = None) -> None:
        with self._lock, self._conn() as c:
            c.execute("DELETE FROM sessions WHERE user_id=? AND token_hash!=?",
                      (user_id, _token_hash(keep_token) if keep_token else ""))

    # ----------------------------------------------------------------- profile
    def update_name(self, user_id: int, name: str) -> dict:
        name = clean_name(name)
        with self._lock, self._conn() as c:
            c.execute("UPDATE users SET name=?, updated_at=? WHERE id=?", (name, _iso(_now()), user_id))
            return self.public(self._row(c, user_id))

    def _check_password(self, user_id: int, password: str, wrong: str):
        with self._conn() as c:
            row = self._row(c, user_id)
        if row is None or not check_password_hash(row["password_hash"], password or ""):
            raise AccountError(wrong, 400)
        return row

    def change_password(self, user_id: int, current: str, new: str, keep_token: str | None = None) -> None:
        row = self._check_password(user_id, current, "Your current password is incorrect.")
        check_password_rules(new, row["email"], row["name"])
        with self._lock, self._conn() as c:
            c.execute("UPDATE users SET password_hash=?, updated_at=? WHERE id=?",
                      (generate_password_hash(new), _iso(_now()), user_id))
        self.end_other_sessions(user_id, keep_token)   # log out every other browser

    def delete_user(self, user_id: int, password: str | None = None) -> None:
        """Remove the profile, sessions, saved keys and purchase history. With a password, check it first."""
        if password is not None:
            self._check_password(user_id, password, "That password is incorrect.")
        with self._lock, self._conn() as c:
            c.execute("DELETE FROM users WHERE id=?", (user_id,))

    # ------------------------------------------------------------------- admin
    def set_admin(self, email: str, admin: bool = True) -> dict:
        with self._lock, self._conn() as c:
            row = c.execute("SELECT id FROM users WHERE email=?", ((email or "").strip().lower(),)).fetchone()
            if row is None:
                raise AccountError(f"No account with the email {email}.", 404)
            c.execute("UPDATE users SET is_admin=?, updated_at=? WHERE id=?", (int(admin), _iso(_now()), row["id"]))
            return self.public(self._row(c, row["id"]))

    def reset_password(self, email: str, new: str) -> dict:
        user = self.by_email(email)
        if user is None:
            raise AccountError(f"No account with the email {email}.", 404)
        check_password_rules(new, user["email"], user["name"])
        with self._lock, self._conn() as c:
            c.execute("UPDATE users SET password_hash=?, updated_at=? WHERE id=?",
                      (generate_password_hash(new), _iso(_now()), user["id"]))
            c.execute("DELETE FROM sessions WHERE user_id=?", (user["id"],))
        return user

    # ------------------------------------------------------------ Alpaca keys
    def link_alpaca(self, user_id: int, key_id: str, secret_key: str, url: str, account_number: str | None) -> dict:
        fp = self.box.fingerprint(key_id)
        now = _iso(_now())
        with self._lock, self._conn() as c:
            other = c.execute("SELECT id FROM users WHERE alpaca_key_fp=? AND id!=?", (fp, user_id)).fetchone()
            if other:
                raise AccountError("These Alpaca keys are already connected to another account on this site. "
                                   "Each person needs their own Alpaca paper account.", 409)
            try:
                c.execute(
                    "UPDATE users SET alpaca_key_enc=?, alpaca_secret_enc=?, alpaca_key_fp=?, alpaca_url=?, "
                    "alpaca_account=?, alpaca_linked_at=?, updated_at=? WHERE id=?",
                    (self.box.encrypt(key_id), self.box.encrypt(secret_key), fp, url, mask_account(account_number),
                     now, now, user_id))
            except self.db.integrity_errors as exc:
                raise AccountError("These Alpaca keys are already connected to another account on this site.", 409) from exc
            return self.public(self._row(c, user_id))

    def unlink_alpaca(self, user_id: int) -> dict:
        with self._lock, self._conn() as c:
            c.execute("UPDATE users SET alpaca_key_enc=NULL, alpaca_secret_enc=NULL, alpaca_key_fp=NULL, "
                      "alpaca_url=NULL, alpaca_account=NULL, alpaca_linked_at=NULL, updated_at=? WHERE id=?",
                      (_iso(_now()), user_id))
            return self.public(self._row(c, user_id))

    def alpaca_credentials(self, user_id: int) -> tuple[str, str, str] | None:
        """(key id, secret key, trading URL) for this person's own Alpaca account, decrypted."""
        with self._conn() as c:
            row = self._row(c, user_id)
        if row is None or not row["alpaca_key_enc"]:
            return None
        return self.box.decrypt(row["alpaca_key_enc"]), self.box.decrypt(row["alpaca_secret_enc"]), row["alpaca_url"]

    # ------------------------------------------------------------------ trades
    def record_trade(self, user_id: int, **fields) -> None:
        cols = ["symbol", "side", "amount", "qty", "price", "alpaca_order_id", "status", "message"]
        with self._lock, self._conn() as c:
            c.execute(f"INSERT INTO trades (user_id, created_at, {', '.join(cols)}) "
                      f"VALUES (?, ?, {', '.join('?' for _ in cols)})",
                      (user_id, _iso(_now()), *[fields.get(k) for k in cols]))

    def trades(self, user_id: int, limit: int = 50) -> list[dict]:
        with self._conn() as c:
            return [dict(r) for r in c.execute(
                "SELECT id, created_at, symbol, side, amount, qty, price, alpaca_order_id, status, message "
                "FROM trades WHERE user_id=? ORDER BY id DESC LIMIT ?", (user_id, limit))]
