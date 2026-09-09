# -*- coding: utf-8 -*-
"""قاعدة البيانات (SQLite) والمصادقة.

نموذج الدخول:
  • المعلّم: بريد + كلمة مرور مُشفَّرة (PBKDF2-HMAC-SHA256، لا تُخزَّن كنص).
  • الطالب: رقم دخول من ٤ أرقام تنشئه له المعلّمة في قائمة الصف.
    الرقم يُخزَّن كنص صريح عمدًا — لأن المعلّمة يجب أن تراه لتعطيه للطالب،
    فهو أشبه برقم الخزانة المدرسية لا بكلمة مرور. لا تُحفظ في التطبيق أي
    بيانات حساسة تتجاوز اسم الطالب وتقدّمه الإملائي.
"""

import hashlib
import hmac
import json
import os
import secrets
import sqlite3
import time

from content import CATEGORY_KEYS

DB_PATH = os.environ.get("IMLA_DB") or os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "imla.db")

PBKDF2_ROUNDS = 200_000
SESSION_TTL = 60 * 60 * 24 * 180     # ١٨٠ يومًا — الطالب لا يعيد الدخول كل حصة
RECENT_WINDOW = 5
CODE_LENGTH = 4


def connect():
    conn = sqlite3.connect(DB_PATH, timeout=15)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")     # يحتمل عدة طلاب في وقت واحد
    return conn


SCHEMA = """
CREATE TABLE IF NOT EXISTS teachers (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    name          TEXT    NOT NULL,
    email         TEXT    NOT NULL UNIQUE,
    password_hash TEXT    NOT NULL,
    created_at    INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS students (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    name            TEXT    NOT NULL,
    username        TEXT    NOT NULL UNIQUE,   -- رقم المستخدم (تعطيه المعلّمة)
    secret          TEXT    NOT NULL,          -- الرقم السري (يظهر للمعلّمة فقط)
    teacher_id      INTEGER NOT NULL REFERENCES teachers(id) ON DELETE CASCADE,
    diagnostic_done INTEGER NOT NULL DEFAULT 0,
    created_at      INTEGER NOT NULL,
    last_seen       INTEGER
);

CREATE TABLE IF NOT EXISTS category_stats (
    student_id   INTEGER NOT NULL REFERENCES students(id) ON DELETE CASCADE,
    category_key TEXT    NOT NULL,
    attempts     INTEGER NOT NULL DEFAULT 0,
    correct      INTEGER NOT NULL DEFAULT 0,
    recent_json  TEXT    NOT NULL DEFAULT '[]',
    PRIMARY KEY (student_id, category_key)
);

CREATE TABLE IF NOT EXISTS sessions (
    token      TEXT    PRIMARY KEY,
    role       TEXT    NOT NULL,
    subject_id INTEGER NOT NULL,
    expires_at INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS attempts (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    student_id   INTEGER NOT NULL REFERENCES students(id) ON DELETE CASCADE,
    category_key TEXT    NOT NULL,
    word         TEXT    NOT NULL,
    answer       TEXT    NOT NULL,
    is_correct   INTEGER NOT NULL,
    mode         TEXT    NOT NULL,
    created_at   INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS practice_sessions (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    student_id  INTEGER NOT NULL REFERENCES students(id) ON DELETE CASCADE,
    kind        TEXT    NOT NULL,      -- 'adaptive' | 'paragraph'
    score       INTEGER NOT NULL,
    total       INTEGER NOT NULL,
    finished_at INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_sessions_student ON practice_sessions(student_id);
CREATE INDEX IF NOT EXISTS idx_attempts_student ON attempts(student_id);
CREATE INDEX IF NOT EXISTS idx_students_teacher ON students(teacher_id);
"""


def init_db():
    conn = connect()
    with conn:
        conn.executescript(SCHEMA)
    conn.close()


# ------------------------------------------------------------ كلمات السر

def hash_secret(secret):
    salt = secrets.token_bytes(16)
    digest = hashlib.pbkdf2_hmac("sha256", secret.encode("utf-8"), salt, PBKDF2_ROUNDS)
    return "pbkdf2_sha256${}${}${}".format(PBKDF2_ROUNDS, salt.hex(), digest.hex())


def verify_secret(secret, stored):
    try:
        algo, rounds, salt_hex, digest_hex = stored.split("$")
        if algo != "pbkdf2_sha256":
            return False
        expected = hashlib.pbkdf2_hmac(
            "sha256", secret.encode("utf-8"), bytes.fromhex(salt_hex), int(rounds))
        return hmac.compare_digest(expected.hex(), digest_hex)
    except (ValueError, AttributeError):
        return False


# --------------------------------------------------------------- الجلسات

def create_session(conn, role, subject_id):
    token = secrets.token_urlsafe(32)
    with conn:
        conn.execute(
            "INSERT INTO sessions (token, role, subject_id, expires_at) VALUES (?,?,?,?)",
            (token, role, subject_id, int(time.time()) + SESSION_TTL))
    return token


def resolve_session(conn, token):
    if not token:
        return None
    row = conn.execute(
        "SELECT role, subject_id, expires_at FROM sessions WHERE token = ?",
        (token,)).fetchone()
    if not row:
        return None
    if row["expires_at"] < int(time.time()):
        destroy_session(conn, token)
        return None
    return row["role"], row["subject_id"]


def destroy_session(conn, token):
    with conn:
        conn.execute("DELETE FROM sessions WHERE token = ?", (token,))


def purge_expired_sessions(conn):
    with conn:
        conn.execute("DELETE FROM sessions WHERE expires_at < ?", (int(time.time()),))


# --------------------------------------------------------------- المعلّمون

def create_teacher(conn, name, email, password):
    email = email.strip().lower()
    if conn.execute("SELECT 1 FROM teachers WHERE email = ?", (email,)).fetchone():
        return None
    with conn:
        cur = conn.execute(
            "INSERT INTO teachers (name, email, password_hash, created_at) VALUES (?,?,?,?)",
            (name.strip(), email, hash_secret(password), int(time.time())))
    return get_teacher(conn, cur.lastrowid)


def authenticate_teacher(conn, email, password):
    row = conn.execute("SELECT * FROM teachers WHERE email = ?",
                       (email.strip().lower(),)).fetchone()
    if not row or not verify_secret(password, row["password_hash"]):
        return None
    return _teacher_public(row)


def get_teacher(conn, teacher_id):
    row = conn.execute("SELECT * FROM teachers WHERE id = ?", (teacher_id,)).fetchone()
    return _teacher_public(row) if row else None


def _teacher_public(row):
    return {"id": row["id"], "name": row["name"], "email": row["email"]}


# ----------------------------------------------------- قائمة الطلاب (الصف)

def username_taken(conn, username, exclude_id=None):
    if exclude_id:
        return conn.execute(
            "SELECT 1 FROM students WHERE username = ? AND id != ?",
            (username, exclude_id)).fetchone() is not None
    return conn.execute("SELECT 1 FROM students WHERE username = ?",
                        (username,)).fetchone() is not None


def _random_digits(n=CODE_LENGTH):
    return "".join(secrets.choice("0123456789") for _ in range(n))


def generate_username(conn):
    """رقم مستخدم من ٤ خانات غير مستخدَم."""
    for _ in range(400):
        u = _random_digits()
        if not username_taken(conn, u):
            return u
    return None


def add_student(conn, teacher_id, name, username=None, secret=None):
    """تضيف المعلّمة طالبًا لقائمتها. يعيد (student, error)."""
    name = " ".join((name or "").split())
    if len(name) < 2:
        return None, "اكتبي اسم الطالب."
    if conn.execute("SELECT 1 FROM students WHERE teacher_id = ? AND name = ?",
                    (teacher_id, name)).fetchone():
        return None, "هذا الاسم موجود في قائمتك بالفعل."
    if username:
        username = username.strip()
        if not (username.isdigit() and len(username) == CODE_LENGTH):
            return None, "رقم المستخدم يجب أن يكون ٤ أرقام."
        if username_taken(conn, username):
            return None, "رقم المستخدم هذا مأخوذ — اختاري غيره."
    else:
        username = generate_username(conn)
        if not username:
            return None, "تعذّر توليد رقم مستخدم جديد."
    if secret:
        secret = secret.strip()
        if not (secret.isdigit() and len(secret) == CODE_LENGTH):
            return None, "الرقم السري يجب أن يكون ٤ أرقام."
    else:
        secret = _random_digits()
    with conn:
        cur = conn.execute(
            "INSERT INTO students (name, username, secret, teacher_id, diagnostic_done, created_at)"
            " VALUES (?,?,?,?,0,?)", (name, username, secret, teacher_id, int(time.time())))
        for key in CATEGORY_KEYS:
            conn.execute(
                "INSERT INTO category_stats (student_id, category_key) VALUES (?,?)",
                (cur.lastrowid, key))
    return get_student(conn, cur.lastrowid), None


def rename_student(conn, student_id, name):
    name = " ".join((name or "").split())
    if len(name) < 2:
        return "اكتبي اسم الطالب."
    with conn:
        conn.execute("UPDATE students SET name = ? WHERE id = ?", (name, student_id))
    return None


def set_student_credentials(conn, student_id, username=None, secret=None):
    """تغيّر المعلّمة رقم المستخدم و/أو الرقم السري."""
    if username:
        username = username.strip()
        if not (username.isdigit() and len(username) == CODE_LENGTH):
            return "رقم المستخدم يجب أن يكون ٤ أرقام."
        if username_taken(conn, username, exclude_id=student_id):
            return "رقم المستخدم هذا مأخوذ."
    if secret:
        secret = secret.strip()
        if not (secret.isdigit() and len(secret) == CODE_LENGTH):
            return "الرقم السري يجب أن يكون ٤ أرقام."
    with conn:
        if username:
            conn.execute("UPDATE students SET username = ? WHERE id = ?",
                         (username, student_id))
        if secret:
            conn.execute("UPDATE students SET secret = ? WHERE id = ?",
                         (secret, student_id))
    return None


def delete_student(conn, student_id):
    with conn:
        conn.execute("DELETE FROM sessions WHERE role = 'student' AND subject_id = ?",
                     (student_id,))
        conn.execute("DELETE FROM students WHERE id = ?", (student_id,))


def authenticate_student(conn, username, secret):
    """دخول الطالب برقم المستخدم والرقم السري. يعيد student_id أو None."""
    username = (username or "").strip()
    secret = (secret or "").strip()
    if not (username.isdigit() and len(username) == CODE_LENGTH):
        return None
    row = conn.execute("SELECT id, secret FROM students WHERE username = ?",
                       (username,)).fetchone()
    if not row or not hmac.compare_digest(row["secret"], secret):
        return None
    with conn:
        conn.execute("UPDATE students SET last_seen = ? WHERE id = ?",
                     (int(time.time()), row["id"]))
    return row["id"]


def touch_student(conn, student_id):
    with conn:
        conn.execute("UPDATE students SET last_seen = ? WHERE id = ?",
                     (int(time.time()), student_id))


def get_student(conn, student_id):
    row = conn.execute("SELECT * FROM students WHERE id = ?", (student_id,)).fetchone()
    if not row:
        return None
    return {"id": row["id"], "name": row["name"],
            "username": row["username"], "secret": row["secret"],
            "teacherId": row["teacher_id"],
            "diagnosticDone": bool(row["diagnostic_done"]),
            "lastSeen": row["last_seen"]}


def set_diagnostic_done(conn, student_id, done=True):
    with conn:
        conn.execute("UPDATE students SET diagnostic_done = ? WHERE id = ?",
                     (1 if done else 0, student_id))


def reset_stats(conn, student_id):
    with conn:
        conn.execute("UPDATE category_stats SET attempts = 0, correct = 0,"
                     " recent_json = '[]' WHERE student_id = ?", (student_id,))
        conn.execute("DELETE FROM attempts WHERE student_id = ?", (student_id,))
        conn.execute("DELETE FROM practice_sessions WHERE student_id = ?", (student_id,))
        conn.execute("UPDATE students SET diagnostic_done = 0 WHERE id = ?", (student_id,))


# ------------------------------------------------------------- الإحصاءات

def get_stats(conn, student_id):
    rows = conn.execute("SELECT * FROM category_stats WHERE student_id = ?",
                        (student_id,)).fetchall()
    stats = {}
    for row in rows:
        stats[row["category_key"]] = {"attempts": row["attempts"],
                                      "correct": row["correct"],
                                      "recent": json.loads(row["recent_json"])}
    for key in CATEGORY_KEYS:
        stats.setdefault(key, {"attempts": 0, "correct": 0, "recent": []})
    return stats


def record_attempt(conn, student_id, category, word, answer, correct, mode):
    with conn:
        row = conn.execute(
            "SELECT recent_json FROM category_stats WHERE student_id = ? AND category_key = ?",
            (student_id, category)).fetchone()
        recent = json.loads(row["recent_json"]) if row else []
        recent.append(bool(correct))
        recent = recent[-RECENT_WINDOW:]
        conn.execute(
            "INSERT INTO category_stats (student_id, category_key, attempts, correct, recent_json)"
            " VALUES (?, ?, 1, ?, ?)"
            " ON CONFLICT(student_id, category_key) DO UPDATE SET"
            "   attempts = attempts + 1,"
            "   correct  = correct + excluded.correct,"
            "   recent_json = excluded.recent_json",
            (student_id, category, 1 if correct else 0, json.dumps(recent)))
        conn.execute(
            "INSERT INTO attempts (student_id, category_key, word, answer, is_correct, mode, created_at)"
            " VALUES (?,?,?,?,?,?,?)",
            (student_id, category, word, answer, 1 if correct else 0, mode, int(time.time())))
        conn.execute("UPDATE students SET last_seen = ? WHERE id = ?",
                     (int(time.time()), student_id))


# ------------------------------------ التدريبات المكتملة (كؤوس ومهام)

def week_start_ts(now=None):
    """بداية الأسبوع الدراسي (الأحد ٠٠:٠٠ بتوقيت الجهاز)."""
    now = now or time.time()
    lt = time.localtime(now)
    midnight = time.mktime((lt.tm_year, lt.tm_mon, lt.tm_mday, 0, 0, 0, 0, 0, -1))
    # tm_wday: الاثنين=٠ … الأحد=٦ ← الأيام المنقضية منذ الأحد
    days_since_sunday = (lt.tm_wday + 1) % 7
    return int(midnight - days_since_sunday * 86400)


def record_practice(conn, student_id, kind, score, total):
    """يسجّل تدريبًا مكتملاً — وحدة العدّ للكؤوس والمهمة الأسبوعية."""
    with conn:
        conn.execute(
            "INSERT INTO practice_sessions (student_id, kind, score, total, finished_at)"
            " VALUES (?,?,?,?,?)",
            (student_id, kind, int(score), int(total), int(time.time())))


def count_practice(conn, student_id):
    row = conn.execute(
        "SELECT COUNT(*) n FROM practice_sessions WHERE student_id = ?",
        (student_id,)).fetchone()
    return row["n"] if row else 0


def count_practice_this_week(conn, student_id):
    row = conn.execute(
        "SELECT COUNT(*) n FROM practice_sessions WHERE student_id = ? AND finished_at >= ?",
        (student_id, week_start_ts())).fetchone()
    return row["n"] if row else 0


# --------------------------------------------------- استعلامات المعلّمة

def list_students_for_teacher(conn, teacher_id):
    return [dict(r) for r in conn.execute(
        "SELECT id, name, username, secret, diagnostic_done, last_seen FROM students"
        " WHERE teacher_id = ? ORDER BY name COLLATE NOCASE", (teacher_id,)).fetchall()]


def student_belongs_to_teacher(conn, student_id, teacher_id):
    return conn.execute("SELECT 1 FROM students WHERE id = ? AND teacher_id = ?",
                        (student_id, teacher_id)).fetchone() is not None
