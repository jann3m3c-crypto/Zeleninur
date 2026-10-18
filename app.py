import os, sqlite3, secrets, uuid, json
from datetime import date, datetime
from functools import wraps
from werkzeug.security import generate_password_hash, check_password_hash
from werkzeug.utils import secure_filename
from flask import Flask, render_template, request, redirect, url_for, session, flash, g, abort

APP_DIR = os.path.dirname(os.path.abspath(__file__))
# Hosted deployments should point this directory to persistent storage (Railway volume: /data).
DATA_DIR = os.environ.get("ZELENINUR_DATA_DIR", "").strip() or None
DB_PATH = os.environ.get(
    "ZELENINUR_DB_PATH",
    os.path.join(DATA_DIR, "zeleninur.db") if DATA_DIR else os.path.join(APP_DIR, "zeleninur.db"),
)
os.makedirs(os.path.dirname(os.path.abspath(DB_PATH)), exist_ok=True)
UPLOAD_DIR = (
    os.path.join(DATA_DIR, "uploads") if DATA_DIR else os.path.join(APP_DIR, "static", "uploads")
)
os.makedirs(UPLOAD_DIR, exist_ok=True)
# Serve player-uploaded evidence through Flask's normal /static/uploads URL while storing
# those files on the persistent volume in hosted mode.
if DATA_DIR:
    _static_uploads = os.path.join(APP_DIR, "static", "uploads")
    if not os.path.lexists(_static_uploads):
        os.symlink(UPLOAD_DIR, _static_uploads, target_is_directory=True)
app = Flask(__name__)
app.config["MAX_CONTENT_LENGTH"] = 8 * 1024 * 1024
_secret_file = os.path.join(APP_DIR, ".zeleninur_secret")
if os.environ.get("ZELENINUR_SECRET"):
    app.secret_key = os.environ["ZELENINUR_SECRET"]
else:
    if not os.path.exists(_secret_file):
        with open(_secret_file, "w", encoding="utf-8") as _f:
            _f.write(secrets.token_hex(32))
    with open(_secret_file, encoding="utf-8") as _f:
        app.secret_key = _f.read().strip()

MAX_PLAYERS = 200
ALLOWED_IMAGE_EXTENSIONS = {"jpg", "jpeg", "png", "webp"}

# Mapování středů všech 29 políček na původní ilustraci (souřadnice v % obrázku).
# Obrázek se nikdy nepřekresluje; souřadnice jsou relativní, takže se škálují s ním.
# Pořadí cesty: spodní podzimní řada zprava doleva (1–8), zimní řada zleva doprava (9–16),
# zelená řada zprava doleva (17–24), horní červená řada zleva doprava (25–29).
SPACE_COORDS = [
    # 1–8: spodní podzimní řada — začíná vlevo dole a pokračuje zleva doprava
    (22, 89), (32, 89), (40, 89), (48, 89), (58, 89), (68, 89), (79, 89), (92, 89),
    # 9–16: zimní řada — začíná vpravo a pokračuje zprava doleva
    (88, 68), (76, 68), (65, 68), (54, 68), (43, 68), (32, 68), (20, 68), (9, 68),
    # 17–24: zelená řada — začíná vlevo a pokračuje zleva doprava
    (6, 45), (17, 45), (29, 45), (41, 45), (52, 45), (65, 45), (78, 45), (92, 45),
    # 25–29: horní červená řada — zleva doprava
    (37, 25), (54, 25), (65, 25), (73, 25), (80, 25),
]
# Funny epithet for each of the user's real 100 circular avatar portraits.
# Keep this JSON in the app root so avatar labels are easy to maintain without editing code.
AVATAR_EPITHETS_PATH = os.path.join(APP_DIR, "avatar_epithets.json")
try:
    with open(AVATAR_EPITHETS_PATH, encoding="utf-8") as _f:
        _epithet_rows = json.load(_f)
    AVATAR_EPITHETS = {
        f"avatar-{int(row['avatar_id']):03d}": str(row["name"]).strip()
        for row in _epithet_rows
        if isinstance(row, dict) and row.get("avatar_id") and row.get("name")
    }
except (OSError, ValueError, TypeError, KeyError):
    AVATAR_EPITHETS = {}

def build_avatar_catalog():
    # One flat selection of all 100 user-provided circular portrait artworks.
    # Each card label is the funny name assigned to that exact portrait file.
    return [
        {"id": f"avatar-{i:03d}",
         "label": AVATAR_EPITHETS.get(f"avatar-{i:03d}", f"Postavička {i:03d}"),
         "category": "Všechny postavičky", "src": f"avatars/portrait-{i:03d}.png"}
        for i in range(1, 101)
    ]
AVATARS = build_avatar_catalog()
AVATAR_BY_ID = {a["id"]: a for a in AVATARS}

def get_db():
    if "db" not in g:
        g.db = sqlite3.connect(DB_PATH, timeout=15)
        g.db.row_factory = sqlite3.Row
        g.db.execute("PRAGMA foreign_keys = ON")
        g.db.execute("PRAGMA busy_timeout = 15000")
    return g.db

@app.teardown_appcontext
def close_db(exception=None):
    db = g.pop("db", None)
    if db is not None:
        db.close()

def table_columns(db, table):
    return {row["name"] for row in db.execute(f"PRAGMA table_info({table})").fetchall()}

def init_db():
    db = sqlite3.connect(DB_PATH, timeout=15)
    db.row_factory = sqlite3.Row
    db.execute("PRAGMA foreign_keys = ON")
    db.executescript("""
    CREATE TABLE IF NOT EXISTS users (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        username TEXT UNIQUE NOT NULL,
        password_hash TEXT NOT NULL,
        display_name TEXT NOT NULL,
        role TEXT NOT NULL DEFAULT 'player',
        position INTEGER NOT NULL DEFAULT 1,
        coins INTEGER NOT NULL DEFAULT 0,
        account_status TEXT NOT NULL DEFAULT 'active',
        created_at TEXT,
        avatar_id TEXT NOT NULL DEFAULT 'avatar-001',
        session_version INTEGER NOT NULL DEFAULT 1
    );
    CREATE TABLE IF NOT EXISTS tasks (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        title TEXT NOT NULL,
        description TEXT NOT NULL,
        active INTEGER NOT NULL DEFAULT 1,
        created_at TEXT,
        task_type TEXT NOT NULL DEFAULT 'text',
        options_text TEXT,
        space_number INTEGER NOT NULL DEFAULT 1
    );
    CREATE TABLE IF NOT EXISTS submissions (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id INTEGER NOT NULL REFERENCES users(id),
        task_id INTEGER NOT NULL REFERENCES tasks(id),
        answer TEXT NOT NULL DEFAULT '',
        status TEXT NOT NULL DEFAULT 'pending',
        submitted_at TEXT,
        reviewed_at TEXT,
        review_note TEXT,
        approved_week TEXT,
        attachment_path TEXT,
        auto_result TEXT
    );
    CREATE TABLE IF NOT EXISTS player_messages (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id INTEGER NOT NULL REFERENCES users(id),
        message TEXT NOT NULL,
        active INTEGER NOT NULL DEFAULT 1,
        created_at TEXT
    );
    CREATE TABLE IF NOT EXISTS space_settings (
        space_number INTEGER PRIMARY KEY,
        skip_cost INTEGER NOT NULL DEFAULT 0
    );
    CREATE TABLE IF NOT EXISTS player_advances (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id INTEGER NOT NULL REFERENCES users(id),
        week_key TEXT NOT NULL,
        from_position INTEGER NOT NULL,
        to_position INTEGER NOT NULL,
        source TEXT NOT NULL,
        cost INTEGER NOT NULL DEFAULT 0,
        created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
        UNIQUE(user_id, week_key)
    );
    CREATE TABLE IF NOT EXISTS task_advances (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id INTEGER NOT NULL REFERENCES users(id),
        task_id INTEGER NOT NULL REFERENCES tasks(id),
        week_number INTEGER NOT NULL,
        from_position INTEGER NOT NULL,
        to_position INTEGER NOT NULL,
        source TEXT NOT NULL,
        cost INTEGER NOT NULL DEFAULT 0,
        created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
        UNIQUE(user_id, task_id)
    );
    CREATE TABLE IF NOT EXISTS work_offers (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        title TEXT NOT NULL,
        description TEXT NOT NULL DEFAULT '',
        reward INTEGER NOT NULL CHECK(reward > 0),
        active INTEGER NOT NULL DEFAULT 1,
        created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
    );
    CREATE TABLE IF NOT EXISTS work_claims (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id INTEGER NOT NULL REFERENCES users(id),
        offer_id INTEGER NOT NULL REFERENCES work_offers(id),
        note TEXT NOT NULL DEFAULT '',
        status TEXT NOT NULL DEFAULT 'pending',
        submitted_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
        reviewed_at TEXT,
        UNIQUE(user_id, offer_id, submitted_at)
    );
    CREATE TABLE IF NOT EXISTS app_meta (
        key TEXT PRIMARY KEY,
        value TEXT NOT NULL
    );
    CREATE TABLE IF NOT EXISTS minigame_scores (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
        round_number INTEGER NOT NULL,
        best_score INTEGER NOT NULL DEFAULT 0 CHECK(best_score >= 0),
        best_level INTEGER NOT NULL DEFAULT 1 CHECK(best_level >= 1),
        updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
        UNIQUE(user_id, round_number)
    );
    """)
    migrations = {
        "users": [
            ("account_status", "TEXT NOT NULL DEFAULT 'active'"),
            ("created_at", "TEXT"),
            ("avatar_id", "TEXT NOT NULL DEFAULT 'avatar-001'"),
            ("session_version", "INTEGER NOT NULL DEFAULT 1")
        ],
        "tasks": [
            ("created_at", "TEXT"),
            ("task_type", "TEXT NOT NULL DEFAULT 'text'"),
            ("options_text", "TEXT"),
            ("space_number", "INTEGER NOT NULL DEFAULT 1"),
            ("week_number", "INTEGER"),
            ("month_title", "TEXT"),
            ("date_range", "TEXT"),
            ("story_intro", "TEXT"),
            ("correct_answer", "TEXT"),
            ("explanation", "TEXT"),
            ("failure_consequence", "TEXT"),
            ("published", "INTEGER NOT NULL DEFAULT 0"),
            ("published_at", "TEXT")
        ],
        "submissions": [
            ("answer", "TEXT NOT NULL DEFAULT ''"),
            ("status", "TEXT NOT NULL DEFAULT 'pending'"),
            ("submitted_at", "TEXT"),
            ("reviewed_at", "TEXT"),
            ("review_note", "TEXT"),
            ("approved_week", "TEXT"),
            ("attachment_path", "TEXT"),
            ("auto_result", "TEXT")
        ],
        "player_messages": [
            ("active", "INTEGER NOT NULL DEFAULT 1"),
            ("created_at", "TEXT")
        ]
    }
    for table, defs in migrations.items():
        cols = table_columns(db, table)
        for col, definition in defs:
            if col not in cols:
                db.execute(f"ALTER TABLE {table} ADD COLUMN {col} {definition}")
    db.execute("UPDATE users SET created_at=CURRENT_TIMESTAMP WHERE created_at IS NULL")
    db.execute("UPDATE users SET avatar_id='avatar-001' WHERE avatar_id IS NULL OR avatar_id=''")
    db.execute("UPDATE tasks SET created_at=CURRENT_TIMESTAMP WHERE created_at IS NULL")
    db.execute("UPDATE submissions SET submitted_at=CURRENT_TIMESTAMP WHERE submitted_at IS NULL")
    db.execute("UPDATE player_messages SET created_at=CURRENT_TIMESTAMP WHERE created_at IS NULL")
    for n in range(1, 30):
        db.execute("INSERT OR IGNORE INTO space_settings (space_number,skip_cost) VALUES (?,0)", (n,))
    # Local development keeps the convenient demo accounts. Hosted/public instances disable
    # them by default and must create the initial admin from private environment variables.
    hosted_instance = any(os.environ.get(k) for k in ("RAILWAY_ENVIRONMENT", "RENDER", "FLY_APP_NAME")) or bool(os.environ.get("PORT"))
    allow_demo_users = os.environ.get(
        "ZELENINUR_ALLOW_DEMO_USERS", "false" if hosted_instance else "true"
    ).strip().lower() in ("1", "true", "yes", "on")
    configured_admin = os.environ.get("ZELENINUR_ADMIN_USERNAME", "").strip()
    configured_password = os.environ.get("ZELENINUR_ADMIN_PASSWORD", "")
    if configured_admin and configured_password and not db.execute(
        "SELECT id FROM users WHERE username=?", (configured_admin,)
    ).fetchone():
        db.execute(
            "INSERT INTO users (username,password_hash,display_name,role,position,coins,account_status,created_at,avatar_id) VALUES (?,?,?,?,?,?,?,CURRENT_TIMESTAMP,?)",
            (configured_admin, generate_password_hash(configured_password), "Správce", "admin", 1, 0, "active", "avatar-001")
        )
    if allow_demo_users:
        if not db.execute("SELECT id FROM users WHERE username='admin'").fetchone():
            db.execute(
                "INSERT INTO users (username,password_hash,display_name,role,position,coins,account_status,created_at,avatar_id) VALUES (?,?,?,?,?,?,?,CURRENT_TIMESTAMP,?)",
                ("admin", generate_password_hash("admin123"), "Správce", "admin", 1, 0, "active", "avatar-001")
            )
        if not db.execute("SELECT id FROM users WHERE username='hrac'").fetchone():
            db.execute(
                "INSERT INTO users (username,password_hash,display_name,role,position,coins,account_status,created_at,avatar_id) VALUES (?,?,?,?,?,?,?,CURRENT_TIMESTAMP,?)",
                ("hrac", generate_password_hash("hrac123"), "Ukázkový hráč", "player", 1, 0, "active", "avatar-001")
            )
        # Only apply demo-password migration in local demo mode; never reset a hosted admin.
        demo_credential_migration = "demo_credentials_admin123_hrac123_v1"
        if not db.execute("SELECT 1 FROM app_meta WHERE key=?", (demo_credential_migration,)).fetchone():
            db.execute(
                "UPDATE users SET password_hash=?, role='admin', account_status='active' WHERE username='admin'",
                (generate_password_hash("admin123"),)
            )
            db.execute(
                "UPDATE users SET password_hash=?, role='player', account_status='active' WHERE username='hrac'",
                (generate_password_hash("hrac123"),)
            )
            db.execute("INSERT INTO app_meta (key,value) VALUES (?,?)", (demo_credential_migration, "completed"))
    elif hosted_instance and not db.execute("SELECT id FROM users WHERE role='admin'").fetchone():
        db.close()
        raise RuntimeError(
            "No admin account exists. Set ZELENINUR_ADMIN_USERNAME and ZELENINUR_ADMIN_PASSWORD as private deployment variables, then restart."
        )
    if not db.execute("SELECT id FROM tasks LIMIT 1").fetchone():
        db.execute(
            "INSERT INTO tasks (title,description,active,created_at,task_type,options_text,space_number) VALUES (?,?,1,CURRENT_TIMESTAMP,'text',NULL,1)",
            ("První týdenní úkol", "Toto je zkušební úkol. Napiš, co bys tento týden udělal pro svou zahrádku. Odpověď se odešle správci ke kontrole.")
        )
    # Synchronize the canonical 29-task catalog once.
    # Existing published tasks are immutable; unpublished drafts are aligned to the supplied
    # source wording once, and missing weeks are added. The metadata key prevents a later
    # app restart from overwriting edits made by the administrator.
    weekly_catalog_migration = "weekly_task_catalog_29_source_v1"
    catalog_done = db.execute("SELECT 1 FROM app_meta WHERE key=?", (weekly_catalog_migration,)).fetchone()
    seed_path = os.path.join(APP_DIR, "weekly_tasks_seed.json")
    if not catalog_done and os.path.exists(seed_path):
        try:
            with open(seed_path, encoding="utf-8") as _seed_file:
                _weekly_seed = json.load(_seed_file)
            for _t in _weekly_seed:
                _week_no = int(_t["week_number"])
                _existing = db.execute(
                    "SELECT * FROM tasks WHERE week_number=? ORDER BY id LIMIT 1", (_week_no,)
                ).fetchone()
                if _existing:
                    # Keep the published version frozen; use the supplied list for unstarted drafts.
                    _has_submissions = db.execute(
                        "SELECT COUNT(*) AS n FROM submissions WHERE task_id=?", (_existing["id"],)
                    ).fetchone()["n"]
                    if not _existing["published"] and not _has_submissions:
                        db.execute("""UPDATE tasks SET
                            title=?, description=?, active=0, task_type=?, options_text=?, space_number=?,
                            month_title=?, date_range=?, story_intro=?, correct_answer=?, explanation=?,
                            failure_consequence=?, published=0, published_at=NULL
                            WHERE id=?""",
                            (_t["title"], _t["description"], _t.get("task_type","text"),
                             _t.get("options_text") or None, _week_no,
                             _t.get("month_title",""), _t.get("date_range",""), _t.get("story_intro",""),
                             _t.get("correct_answer",""), _t.get("explanation",""),
                             _t.get("failure_consequence",""), _existing["id"]))
                else:
                    db.execute("""INSERT INTO tasks
                        (title,description,active,created_at,task_type,options_text,space_number,
                         week_number,month_title,date_range,story_intro,correct_answer,explanation,
                         failure_consequence,published,published_at)
                        VALUES (?, ?, 0, CURRENT_TIMESTAMP, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0, NULL)""",
                        (_t["title"], _t["description"], _t.get("task_type","text"),
                         _t.get("options_text") or None, _week_no, _week_no,
                         _t.get("month_title",""), _t.get("date_range",""), _t.get("story_intro",""),
                         _t.get("correct_answer",""), _t.get("explanation",""),
                         _t.get("failure_consequence","")))
            db.execute("INSERT INTO app_meta (key,value) VALUES (?,?)",
                       (weekly_catalog_migration, "completed"))
        except (OSError, ValueError, KeyError, sqlite3.Error) as _seed_error:
            print(f"Upozornění: katalog 29 týdenních úkolů se nepodařilo synchronizovat: {_seed_error}")

    # One-time migration: self-registration no longer requires administrator approval.
    # Activate existing pending player registrations too, so early testers are not locked out.
    auto_activate_key = "auto_activate_pending_player_accounts_v1"
    if not db.execute("SELECT 1 FROM app_meta WHERE key=?", (auto_activate_key,)).fetchone():
        db.execute("UPDATE users SET account_status='active' WHERE role='player' AND account_status='pending'")
        db.execute("INSERT INTO app_meta (key,value) VALUES (?,?)", (auto_activate_key, "completed"))

    # One-time requested restart of the weekly-task journey.
    # Preserve player accounts, passwords, avatars, gold coins, and minigame scores;
    # clear only task answers/progress and restart player markers at square 1.
    progress_reset_key = "weekly_task_progress_reset_start_over_v1"
    if not db.execute("SELECT 1 FROM app_meta WHERE key=?", (progress_reset_key,)).fetchone():
        db.execute("DELETE FROM submissions WHERE user_id IN (SELECT id FROM users WHERE role='player')")
        db.execute("DELETE FROM task_advances WHERE user_id IN (SELECT id FROM users WHERE role='player')")
        db.execute("DELETE FROM player_advances WHERE user_id IN (SELECT id FROM users WHERE role='player')")
        db.execute("UPDATE users SET position=1 WHERE role='player'")
        db.execute("INSERT INTO app_meta (key,value) VALUES (?,?)", (progress_reset_key, "completed"))

    # Backfill weekly movement locks from previously approved tasks. This preserves existing progress.
    approvals = db.execute("""SELECT id,user_id,approved_week,reviewed_at FROM submissions
        WHERE status='approved' AND (approved_week IS NOT NULL OR auto_result IS NULL)""").fetchall()
    for a in approvals:
        key = a["approved_week"]
        if not key and a["reviewed_at"]:
            try:
                key = datetime.strptime(a["reviewed_at"][:19], "%Y-%m-%d %H:%M:%S").date().strftime("%G-W%V")
                db.execute("UPDATE submissions SET approved_week=? WHERE id=?", (key, a["id"]))
            except (ValueError, TypeError):
                key = None
        if key:
            u = db.execute("SELECT position FROM users WHERE id=?", (a["user_id"],)).fetchone()
            pos = int(u["position"]) if u else 1
            db.execute(
                "INSERT OR IGNORE INTO player_advances (user_id,week_key,from_position,to_position,source,cost,created_at) VALUES (?,?,?,?,?,0,COALESCE(?,CURRENT_TIMESTAMP))",
                (a["user_id"], key, max(1,pos-1), pos, "task", a["reviewed_at"])
            )
    # One-time migration: tasks approved/skipped under the old engine already counted as completed.
    # Preserve that history without moving players again; all new progress is tracked per task.
    migration_key = "task_advances_backfilled_v1"
    if not db.execute("SELECT 1 FROM app_meta WHERE key=?", (migration_key,)).fetchone():
        old_done = db.execute("""SELECT s.user_id,s.task_id,t.week_number,s.status,s.reviewed_at
            FROM submissions s JOIN tasks t ON t.id=s.task_id
            WHERE t.week_number IS NOT NULL AND s.status IN ('approved','skipped')""").fetchall()
        for row in old_done:
            u = db.execute("SELECT position FROM users WHERE id=?", (row["user_id"],)).fetchone()
            pos = int(u["position"]) if u else 1
            db.execute("""INSERT OR IGNORE INTO task_advances
                (user_id,task_id,week_number,from_position,to_position,source,cost,created_at)
                VALUES (?,?,?,?,?,'migration',0,COALESCE(?,CURRENT_TIMESTAMP))""",
                (row["user_id"], row["task_id"], int(row["week_number"] or 0), max(1,pos-1), pos, row["reviewed_at"]))
        db.execute("INSERT INTO app_meta (key,value) VALUES (?,?)", (migration_key, "completed"))
    db.commit()
    db.close()

def current_user():
    user_id = session.get("user_id")
    if not user_id:
        return None
    user = get_db().execute("SELECT * FROM users WHERE id=?", (user_id,)).fetchone()
    if not user:
        session.clear()
        return None
    # A password reset increments session_version so existing player sessions expire.
    stored_version = int(user["session_version"] or 1)
    session_version = session.get("auth_version")
    if session_version is None:
        # Smoothly migrate browser sessions created before this feature was installed.
        session["auth_version"] = stored_version
    elif int(session_version) != stored_version:
        session.clear()
        return None
    return user

def login_required(fn):
    @wraps(fn)
    def wrapper(*args, **kwargs):
        user = current_user()
        if not user or user["account_status"] != "active":
            session.clear()
            return redirect(url_for("login"))
        return fn(*args, **kwargs)
    return wrapper

def admin_required(fn):
    @wraps(fn)
    def wrapper(*args, **kwargs):
        user = current_user()
        if not user or user["account_status"] != "active" or user["role"] != "admin":
            flash("Tato část je přístupná pouze správci.", "error")
            return redirect(url_for("home") if user and user["account_status"] == "active" else url_for("login"))
        return fn(*args, **kwargs)
    return wrapper

def current_week_key():
    return date.today().strftime("%G-W%V")

def player_has_advanced(db, user_id, week_key=None):
    """Legacy calendar-week marker only; the weekly-task engine no longer uses it to limit movement."""
    return bool(db.execute(
        "SELECT id FROM player_advances WHERE user_id=? AND week_key=?",
        (user_id, week_key or current_week_key())
    ).fetchone())


def latest_published_week(db):
    row = db.execute("SELECT MAX(week_number) AS n FROM tasks WHERE week_number IS NOT NULL AND published=1").fetchone()
    return int(row["n"] or 0)


def normalize_answer(value):
    import unicodedata, re
    value = unicodedata.normalize("NFKD", str(value or "").casefold())
    value = "".join(ch for ch in value if not unicodedata.combining(ch))
    value = re.sub(r"[^a-z0-9]+", "", value)
    return value


def answer_is_correct(task, answer):
    """Auto-check multiple-choice and short exact-answer tasks; leave open/photo tasks for review."""
    task_type = (task["task_type"] or "text").lower()
    expected = (task["correct_answer"] or "").strip()
    answer = (answer or "").strip()
    if task_type == "choice":
        import re
        expected_letter = re.match(r"\s*\(([A-C])\)", expected, re.I)
        chosen_letter = re.match(r"\s*\(([A-C])\)", answer, re.I)
        if expected_letter and chosen_letter:
            return expected_letter.group(1).upper() == chosen_letter.group(1).upper()
        return bool(expected) and normalize_answer(answer) == normalize_answer(expected)
    if task_type != "text" or not expected or not answer:
        return None
    # Parenthetical notes in the answer key often describe an accepted alternative.
    # Accept both the full key and its main answer before the explanatory parenthesis.
    candidates = [expected]
    if "(" in expected:
        candidates.append(expected.split("(", 1)[0].strip().rstrip(".,;:"))
        import re
        for parenthetical in re.findall(r"\(([^)]*)\)", expected):
            alt = re.sub(r"^(případně|například|např\.)\s*", "", parenthetical, flags=re.I).strip().rstrip(".,;:")
            if alt:
                candidates.append(alt)
    for chunk in expected.split(";"):
        if chunk.strip():
            candidates.append(chunk.strip())
    actual = normalize_answer(answer)
    return any(actual == normalize_answer(candidate) for candidate in candidates if normalize_answer(candidate))


def record_task_advance(db, user_id, task, source, cost=0):
    """Record exactly one move per player/task and update coins/position atomically."""
    already = db.execute("SELECT id FROM task_advances WHERE user_id=? AND task_id=?", (user_id, task["id"])).fetchone()
    if already:
        return False
    player = db.execute("SELECT position,coins FROM users WHERE id=? AND role='player' AND account_status='active'", (user_id,)).fetchone()
    if not player:
        return False
    from_pos = max(1, min(29, int(player["position"] or 1)))
    cost = int(cost or 0)
    if cost < 0 or int(player["coins"] or 0) < cost:
        return False
    if from_pos >= 29 and cost > 0:
        return False
    to_pos = min(29, from_pos + 1)
    db.execute("""INSERT INTO task_advances
        (user_id,task_id,week_number,from_position,to_position,source,cost,created_at)
        VALUES (?,?,?,?,?,?,?,CURRENT_TIMESTAMP)""",
        (user_id, task["id"], int(task["week_number"] or 0), from_pos, to_pos, source, cost))
    updated = db.execute("UPDATE users SET coins=coins-?,position=MIN(position+1,29) WHERE id=? AND position=? AND coins>=?",
                         (cost, user_id, from_pos, cost))
    if updated.rowcount != 1:
        raise sqlite3.IntegrityError("Zůstatek nebo pozice hráče se změnily")
    return True


def get_weekly_task_progress(db, user_id):
    """Return published tasks and progress. Position advances, not just correct answers, unlock next task."""
    current_week = latest_published_week(db)
    if current_week <= 0:
        return [], None, 0
    rows = db.execute(
        "SELECT * FROM tasks WHERE week_number IS NOT NULL AND published=1 AND week_number<=? ORDER BY week_number ASC",
        (current_week,)
    ).fetchall()
    player = db.execute("SELECT position,coins FROM users WHERE id=?", (user_id,)).fetchone()
    from_pos = int(player["position"] or 1) if player else 1
    setting = db.execute("SELECT skip_cost FROM space_settings WHERE space_number=?", (from_pos,)).fetchone()
    current_cost = int(setting["skip_cost"] or 0) if setting else 0
    items = []
    next_id = None
    for row in rows:
        task = dict(row)
        subrow = db.execute(
            "SELECT * FROM submissions WHERE user_id=? AND task_id=? ORDER BY id DESC LIMIT 1",
            (user_id, task["id"])
        ).fetchone()
        submission = dict(subrow) if subrow else None
        advance = db.execute("SELECT id,source,cost FROM task_advances WHERE user_id=? AND task_id=?", (user_id, task["id"])).fetchone()
        status = submission["status"] if submission else "not_started"
        complete = bool(advance)
        eligible = not complete and next_id is None
        if eligible:
            next_id = task["id"]
        can_submit = eligible and status in ("not_started", "needs_changes")
        buy_cost = 1 if status == "rejected" else current_cost
        can_buy = bool(eligible and submission and status in ("approved", "rejected")
                       and from_pos < 29 and (status == "rejected" or current_cost > 0))
        solution_visible = bool(submission and (status in ("approved", "rejected", "skipped")
                                or submission.get("auto_result")))
        items.append({
            "task": task, "submission": submission, "status": status,
            "complete": complete, "eligible": eligible,
            "can_submit": can_submit, "can_buy": can_buy,
            "is_historical": int(task.get("week_number") or 0) < current_week,
            "is_current_week": int(task.get("week_number") or 0) == current_week,
            "solution_visible": solution_visible,
            "advance": dict(advance) if advance else None,
            "skip_cost": current_cost,
            "buy_cost": buy_cost,
        })
    return items, next_id, current_week


def get_next_weekly_task(db, user_id):
    items, next_id, current_week = get_weekly_task_progress(db, user_id)
    for item in items:
        if item["task"]["id"] == next_id:
            return item, current_week
    return None, current_week

def get_avatar_src(avatar_id):
    return AVATAR_BY_ID.get(avatar_id, AVATARS[0])["src"]

def get_avatar_name(avatar_id):
    """Return the humorous epithet attached to the user's selected portrait."""
    return AVATAR_BY_ID.get(avatar_id, AVATARS[0])["label"]

def save_upload(file_storage):
    filename = secure_filename(file_storage.filename or "")
    if not filename or "." not in filename:
        return None
    ext = filename.rsplit(".", 1)[1].lower()
    if ext not in ALLOWED_IMAGE_EXTENSIONS:
        return None
    stored = f"{uuid.uuid4().hex}.{ext}"
    file_storage.save(os.path.join(UPLOAD_DIR, stored))
    return f"uploads/{stored}"

@app.route("/")
@login_required
def home():
    db = get_db()
    user = current_user()
    if user["role"] == "admin":
        return redirect(url_for("admin"))

    weekly_task_items, next_task_id, published_week_number = get_weekly_task_progress(db, user["id"])
    featured_item = next((item for item in weekly_task_items if item["task"]["id"] == next_task_id), None)
    if featured_item is None and weekly_task_items:
        featured_item = weekly_task_items[-1]
    task = featured_item["task"] if featured_item else None
    submission = featured_item["submission"] if featured_item else None

    user_message_row = db.execute(
        "SELECT message FROM player_messages WHERE user_id=? AND active=1 ORDER BY id DESC LIMIT 1",
        (user["id"],)
    ).fetchone()
    user_message = user_message_row["message"] if user_message_row else ""
    players = db.execute(
        "SELECT id,username,display_name,position,coins,avatar_id FROM users WHERE role='player' AND account_status='active' ORDER BY display_name"
    ).fetchall()
    by_position = {n: [] for n in range(1,30)}
    for p in players:
        pos = max(1, min(29, int(p["position"] or 1)))
        avatar_id = p["avatar_id"] if p["avatar_id"] in AVATAR_BY_ID else "avatar-001"
        av = get_avatar_src(avatar_id)
        by_position[pos].append({
            "id": p["id"], "username": p["username"], "name": p["display_name"],
            "coins": p["coins"], "avatar_id": avatar_id, "avatar_name": get_avatar_name(avatar_id),
            "avatar_src": url_for("static", filename=av, v="avatars-woodrim-v34"), "current": p["id"] == user["id"]
        })
    groups = []
    for n, (x, y) in enumerate(SPACE_COORDS, start=1):
        groups.append({"position": n, "x": x, "y": y, "players": by_position[n]})
    player_cards = []
    for p in players:
        avatar_id = p["avatar_id"] if p["avatar_id"] in AVATAR_BY_ID else "avatar-001"
        player_cards.append({
            "id": p["id"], "name": p["display_name"],
            "position": max(1, min(29, int(p["position"] or 1))),
            "coins": int(p["coins"] or 0), "avatar_id": avatar_id,
            "avatar_name": get_avatar_name(avatar_id),
            "avatar_src": url_for("static", filename=AVATAR_BY_ID[avatar_id]["src"], v="avatars-woodrim-v34"),
            "current": p["id"] == user["id"]
        })
    player_cards.sort(key=lambda p: (not p["current"], p["name"].casefold()))
    player_cards_visible = player_cards[:3]
    player_cards_more = max(0, len(player_cards) - len(player_cards_visible))
    spaces_data = {str(n): by_position[n] for n in range(1,30)}
    work_offers = db.execute(
        "SELECT o.*, (SELECT c.status FROM work_claims c WHERE c.offer_id=o.id AND c.user_id=? ORDER BY c.id DESC LIMIT 1) AS my_status FROM work_offers o WHERE o.active=1 ORDER BY o.id DESC",
        (user["id"],)
    ).fetchall()
    settings = db.execute("SELECT skip_cost FROM space_settings WHERE space_number=?", (user["position"],)).fetchone()
    skip_cost = int(settings["skip_cost"]) if settings else 0
    # Calendar-week movement limits are intentionally removed. Each task has its own single-use progress record.
    has_advanced = False
    can_buy = bool(featured_item and featured_item.get("can_buy"))
    for item in weekly_task_items:
        item["answer_url"] = url_for("static", filename=item["submission"]["attachment_path"]) if item["submission"] and item["submission"].get("attachment_path") else None
    # Preserve submissions from the old per-space task version in a separate readable archive.
    legacy_history = db.execute(
        """SELECT s.*,t.title AS task_title,t.space_number FROM submissions s JOIN tasks t ON t.id=s.task_id
           WHERE s.user_id=? AND t.week_number IS NULL ORDER BY COALESCE(s.reviewed_at,s.submitted_at) DESC""",
        (user["id"],)
    ).fetchall()
    avatars = []
    current_avatar = user["avatar_id"] if user["avatar_id"] in AVATAR_BY_ID else "avatar-001"
    current_avatar_name = get_avatar_name(current_avatar)
    for av in AVATARS:
        item = dict(av)
        item["url"] = url_for("static", filename=av["src"], v="avatars-woodrim-v34")
        avatars.append(item)
    minigame_unlocked = bool(user["role"] == "player" and minigame_round_unlocked(db, user["id"], 1))
    minigame_best_row = db.execute("SELECT best_score,best_level FROM minigame_scores WHERE user_id=? AND round_number=1", (user["id"],)).fetchone()
    minigame2_unlocked = bool(user["role"] == "player" and minigame_round_unlocked(db, user["id"], 2))
    minigame2_best_row = db.execute("SELECT best_score,best_level FROM minigame_scores WHERE user_id=? AND round_number=2", (user["id"],)).fetchone()
    return render_template(
        "home.html", user=user, task=task, submission=submission, groups=groups,
        spaces_data=spaces_data, user_message=user_message, avatars=avatars,
        current_avatar=current_avatar, current_avatar_name=current_avatar_name,
        current_avatar_url=url_for("static", filename=get_avatar_src(current_avatar), v="avatars-woodrim-v34"),
        player_cards=player_cards_visible, player_cards_more=player_cards_more,
        week_key=current_week_key(), skip_cost=skip_cost, has_advanced=has_advanced, can_buy=can_buy,
        player_count=len(players), player_capacity=MAX_PLAYERS, work_offers=work_offers,
        weekly_task_items=weekly_task_items, published_week_number=published_week_number,
        legacy_history=legacy_history, featured_item=featured_item,
        minigame_unlocked=minigame_unlocked,
        minigame_best_score=int(minigame_best_row["best_score"]) if minigame_best_row else None,
        minigame_best_level=int(minigame_best_row["best_level"]) if minigame_best_row else None,
        minigame2_unlocked=minigame2_unlocked,
        minigame2_best_score=int(minigame2_best_row["best_score"]) if minigame2_best_row else None,
        minigame2_best_level=int(minigame2_best_row["best_level"]) if minigame2_best_row else None
    )

def minigame_round_unlocked(db, user_id, round_number):
    """A minigame opens after a player starts its matching published weekly task, regardless of answer correctness or review status."""
    if round_number not in (1, 2):
        return False
    task = db.execute("SELECT id FROM tasks WHERE week_number=? AND published=1 ORDER BY id LIMIT 1", (round_number,)).fetchone()
    if not task:
        return False
    submission = db.execute(
        "SELECT id FROM submissions WHERE user_id=? AND task_id=? ORDER BY id DESC LIMIT 1",
        (user_id, task["id"])
    ).fetchone()
    return submission is not None


def get_minigame_leaderboard(db, round_number, limit=200):
    rows = db.execute("""SELECT s.best_score,s.best_level,s.updated_at,u.display_name,u.username,u.avatar_id
        FROM minigame_scores s JOIN users u ON u.id=s.user_id
        WHERE s.round_number=? AND u.role='player' AND u.account_status='active'
        ORDER BY s.best_score DESC,s.best_level DESC,s.updated_at ASC,u.display_name COLLATE NOCASE ASC
        LIMIT ?""", (round_number, limit)).fetchall()
    result=[]
    for i,row in enumerate(rows, start=1):
        score=int(row["best_score"] or 0)
        if round_number == 2:
            if score >= 25: status="Postrach slimáků 🛡️"
            elif score >= 16: status="Zahradní ochránce 🌿"
            elif score >= 8: status="Lovec škůdců 🐌"
            else: status="Nováček hlídky 🍃"
        else:
            if score >= 20: status="Elitní skener 👁️"
            elif score >= 12: status="Zkušený hledač 🔍"
            elif score >= 7: status="Vidí skrz mlhu 🌫️"
            else: status="Začínající farmář 🌱"
        avatar_id = row["avatar_id"] if row["avatar_id"] in AVATAR_BY_ID else "avatar-001"
        result.append({"rank":i,"name":row["display_name"],"username":row["username"],
                       "avatar_name":get_avatar_name(avatar_id),
                       "score":score,"best_level":int(row["best_level"] or 1),"status":status,
                       "avatar_src":url_for("static",filename=get_avatar_src(avatar_id),v="avatars-woodrim-v34")})
    return result


@app.route("/minihra/<int:round_number>")
@login_required
def minigame(round_number):
    user=current_user()
    if user["role"] == "admin":
        return redirect(url_for("admin"))
    db=get_db()
    if round_number not in (1, 2) or not minigame_round_unlocked(db, user["id"], round_number):
        flash(f"Minihra se otevře po odeslání týdenního úkolu {round_number}; správnost odpovědi nerozhoduje.", "error")
        return redirect(url_for("home"))
    best=db.execute("SELECT best_score,best_level FROM minigame_scores WHERE user_id=? AND round_number=?",
                    (user["id"],round_number)).fetchone()
    avatar_id = user["avatar_id"] if user["avatar_id"] in AVATAR_BY_ID else "avatar-001"
    template = "minigame.html" if round_number == 1 else "minigame2.html"
    return render_template(template, user=user, round_number=round_number,
                           current_avatar_name=get_avatar_name(avatar_id),
                           best_score=int(best["best_score"]) if best else None,
                           best_level=int(best["best_level"]) if best else None,
                           leaderboard=get_minigame_leaderboard(db,round_number))


@app.route("/api/minihra/<int:round_number>/leaderboard", methods=["GET"])
@login_required
def minigame_leaderboard_api(round_number):
    user=current_user(); db=get_db()
    if user["role"] == "admin" or round_number not in (1, 2) or not minigame_round_unlocked(db,user["id"],round_number):
        return {"error":"Minihra není odemčená."}, 403
    return {"leaderboard":get_minigame_leaderboard(db,round_number)}


@app.route("/api/minihra/<int:round_number>/score", methods=["POST"])
@login_required
def minigame_score_api(round_number):
    db=get_db(); user=current_user()
    if user["role"] == "admin" or round_number not in (1, 2) or not minigame_round_unlocked(db,user["id"],round_number):
        return {"error":"Minihra není odemčená."}, 403
    data=request.get_json(silent=True) or {}
    score=data.get("skore",data.get("score")); level=data.get("uroven",data.get("level",1))
    try: score=int(score); level=int(level)
    except (TypeError,ValueError): return {"error":"Neplatný výsledek."}, 400
    if score < 0 or score > 100000 or level < 1 or level > 10000:
        return {"error":"Výsledek je mimo povolený rozsah."}, 400
    db.execute("BEGIN IMMEDIATE")
    old=db.execute("SELECT best_score,best_level FROM minigame_scores WHERE user_id=? AND round_number=?",
                   (user["id"],round_number)).fetchone()
    new_record=old is None or score > int(old["best_score"])
    if old is None:
        db.execute("INSERT INTO minigame_scores (user_id,round_number,best_score,best_level,updated_at) VALUES (?,?,?,?,CURRENT_TIMESTAMP)",
                   (user["id"],round_number,score,level))
    elif new_record:
        db.execute("UPDATE minigame_scores SET best_score=?,best_level=?,updated_at=CURRENT_TIMESTAMP WHERE user_id=? AND round_number=?",
                   (score,level,user["id"],round_number))
    db.commit()
    best=db.execute("SELECT best_score,best_level FROM minigame_scores WHERE user_id=? AND round_number=?",
                    (user["id"],round_number)).fetchone()
    return {"ok":True,"new_record":new_record,"best_score":int(best["best_score"]),
            "best_level":int(best["best_level"]),"leaderboard":get_minigame_leaderboard(db,round_number)}


@app.route("/register", methods=["GET","POST"])
def register():
    if request.method == "POST":
        username = request.form.get("username","").strip()
        display_name = request.form.get("display_name","").strip()
        password = request.form.get("password","")
        confirm = request.form.get("confirm_password","")
        db = get_db()
        count = db.execute("SELECT COUNT(*) AS n FROM users WHERE role='player' AND account_status IN ('active','pending')").fetchone()["n"]
        if count >= MAX_PLAYERS:
            flash(f"Hra už dosáhla kapacity {MAX_PLAYERS} hráčů. Registrace je dočasně uzavřena.", "error")
            return render_template("register.html")
        if len(username)<3 or len(username)>30 or not username.replace("_","").replace("-","").isalnum():
            flash("Uživatelské jméno musí mít 3–30 znaků; použij písmena, čísla, pomlčku nebo podtržítko.", "error")
            return render_template("register.html")
        if len(display_name)<2 or len(display_name)>50:
            flash("Zobrazované jméno musí mít 2–50 znaků.", "error"); return render_template("register.html")
        if len(password)<6:
            flash("Heslo musí mít alespoň 6 znaků.", "error"); return render_template("register.html")
        if password != confirm:
            flash("Hesla se neshodují.", "error"); return render_template("register.html")
        try:
            cursor = db.execute(
                "INSERT INTO users (username,password_hash,display_name,role,position,coins,account_status,created_at,avatar_id) VALUES (?,?,?,?,?,?,?,CURRENT_TIMESTAMP,?)",
                (username,generate_password_hash(password),display_name,"player",1,0,"active","avatar-001")
            )
            db.commit()
        except sqlite3.IntegrityError:
            flash("Toto uživatelské jméno už někdo používá. Vyber jiné.", "error")
            return render_template("register.html")
        # Self-registration now creates an active player account and signs the player in immediately.
        session.clear()
        session["user_id"] = cursor.lastrowid
        flash(f"Vítej ve hře, {display_name}! Účet je aktivní.", "success")
        return redirect(url_for("home"))
    return render_template("register.html")

@app.route("/login", methods=["GET","POST"])
def login():
    if request.method=="POST":
        username=request.form.get("username","").strip()
        password=request.form.get("password","")
        user=get_db().execute("SELECT * FROM users WHERE username=?",(username,)).fetchone()
        if user and check_password_hash(user["password_hash"],password):
            if user["account_status"]=="pending":
                flash("Registraci jsme přijali. Počkej, až správce účet schválí.","error")
                return render_template("login.html")
            if user["account_status"]!="active":
                flash("Tento účet není aktivní. Obrať se na správce hry.","error")
                return render_template("login.html")
            session.clear(); session["user_id"]=user["id"]
            return redirect(url_for("home"))
        flash("Nesprávné přihlašovací jméno nebo heslo.","error")
    return render_template("login.html")

@app.route("/logout")
def logout():
    session.clear()
    return redirect(url_for("login"))

@app.route("/set-avatar", methods=["POST"])
@login_required
def set_avatar():
    user=current_user()
    if user["role"]=="admin":
        return redirect(url_for("admin"))
    avatar_id=request.form.get("avatar_id","")
    if avatar_id not in AVATAR_BY_ID:
        flash("Tento avatar neexistuje.","error")
    else:
        get_db().execute("UPDATE users SET avatar_id=? WHERE id=? AND role='player'",(avatar_id,user["id"]))
        get_db().commit()
        flash("Avatar byl změněn.","success")
    return redirect(url_for("home"))

@app.route("/submit", methods=["POST"])
@login_required
def submit():
    db=get_db(); user=current_user()
    if user["role"]=="admin": return redirect(url_for("admin"))
    task_id=request.form.get("task_id",type=int)
    answer=request.form.get("answer","").strip()
    upload=request.files.get("attachment")
    attachment_path=None
    if upload and upload.filename:
        attachment_path=save_upload(upload)
        if not attachment_path:
            flash("Povoleny jsou pouze obrázky JPG, PNG nebo WEBP.","error"); return redirect(url_for("home"))
    try:
        db.execute("BEGIN IMMEDIATE")
        progress, next_id, published_week = get_weekly_task_progress(db, user["id"])
        if not published_week:
            db.rollback(); flash("Správce zatím nezveřejnil první úkol.","error"); return redirect(url_for("home"))
        if task_id != next_id:
            db.rollback(); flash("Úkoly se plní postupně. Nejdřív dokonči nejstarší nevyřízený úkol.","error"); return redirect(url_for("home"))
        task=db.execute("SELECT * FROM tasks WHERE id=? AND week_number IS NOT NULL AND published=1 AND week_number<=?",(task_id,published_week)).fetchone()
        if not task:
            db.rollback(); flash("Tento úkol není zveřejněný.","error"); return redirect(url_for("home"))
        task_type=(task["task_type"] or "text").lower()
        existing=db.execute("SELECT * FROM submissions WHERE user_id=? AND task_id=? ORDER BY id DESC LIMIT 1",(user["id"],task_id)).fetchone()
        if task_type=="photo" and not (attachment_path or (existing and existing["status"]=="needs_changes" and existing["attachment_path"])):
            db.rollback(); flash("K tomuto úkolu musíš přiložit fotografii.","error"); return redirect(url_for("home"))
        if task_type=="choice" and not answer:
            db.rollback(); flash("Vyber jednu z nabízených odpovědí.","error"); return redirect(url_for("home"))
        if task_type=="text" and not answer:
            db.rollback(); flash("Vyplň odpověď před odesláním.","error"); return redirect(url_for("home"))
        if task_type=="challenge" and not answer and not attachment_path:
            db.rollback(); flash("Napiš odpověď nebo přilož fotografii.","error"); return redirect(url_for("home"))
        if existing and existing["status"] in ("pending","approved","rejected","skipped"):
            db.rollback(); flash("Tento úkol už máš odevzdaný. Podívej se na vyhodnocení nebo použij zlaťák k postupu.","error")
            return redirect(url_for("home"))

        auto_result=answer_is_correct(task, answer)
        is_auto=auto_result is not None
        is_current=int(task["week_number"] or 0)==published_week
        if is_auto:
            status="approved" if auto_result else "rejected"
            note="Automatická kontrola: správná odpověď." if auto_result else "Automatická kontrola: odpověď není správná."
            reviewed_at_sql="CURRENT_TIMESTAMP"
        else:
            status="pending"
            note=None
            reviewed_at_sql="NULL"
        if existing and existing["status"]=="needs_changes":
            old_attachment = attachment_path or existing["attachment_path"]
            db.execute("""UPDATE submissions SET answer=?,attachment_path=?,status=?,submitted_at=CURRENT_TIMESTAMP,
                reviewed_at=""" + reviewed_at_sql + ",review_note=?,auto_result=? WHERE id=?",
                (answer,old_attachment,status,note,"correct" if auto_result is True else "incorrect" if auto_result is False else "manual",existing["id"]))
            submission_id=existing["id"]
        else:
            db.execute("""INSERT INTO submissions (user_id,task_id,answer,status,submitted_at,reviewed_at,review_note,attachment_path,auto_result)
                VALUES (?,?,?, ?,CURRENT_TIMESTAMP,""" + reviewed_at_sql + ",?,?,?)",
                (user["id"],task_id,answer,status,note,attachment_path,"correct" if auto_result is True else "incorrect" if auto_result is False else "manual"))
            submission_id=db.execute("SELECT last_insert_rowid()").fetchone()[0]
        moved=False
        if is_auto and auto_result is True and is_current:
            moved=record_task_advance(db,user["id"],task,"auto_correct",0)
        db.commit()
        if is_auto and auto_result is True:
            if moved:
                flash("Správně! Řešení a vysvětlení najdeš u úkolu. Automaticky jsi postoupil/a o jedno políčko.","success")
            else:
                flash("Správná odpověď! Historický úkol je zaznamenán; k postupu na další políčko použij zlaťák.","success")
        elif is_auto and auto_result is False:
            flash("Odpověď není správná. U úkolu najdeš správné řešení a můžeš použít zlaťák a postoupit.","error")
        else:
            flash("Odpověď byla odeslána správci ke kontrole. Po schválení se u aktuálního týdne postoupí automaticky; u historického úkolu se postupuje za zlaťák.","success")
    except sqlite3.IntegrityError:
        db.rollback(); flash("Úkol se právě změnil nebo už byl zaznamenán. Obnov stránku a zkus to znovu.","error")
    except Exception:
        db.rollback(); raise
    return redirect(url_for("home"))


@app.route("/buy-advance", methods=["POST"])
@login_required
def buy_advance():
    db=get_db(); user=current_user()
    if user["role"]=="admin": return redirect(url_for("admin"))
    submitted_task_id=request.form.get("task_id",type=int)
    try:
        db.execute("BEGIN IMMEDIATE")
        fresh=db.execute("SELECT * FROM users WHERE id=? AND role='player' AND account_status='active'",(user["id"],)).fetchone()
        if not fresh:
            db.rollback(); flash("Hráčský účet nebyl nalezen.","error"); return redirect(url_for("home"))
        eligible_item, published_week=get_next_weekly_task(db,fresh["id"])
        if not eligible_item or (submitted_task_id and eligible_item["task"]["id"]!=submitted_task_id):
            db.rollback(); flash("Nejdřív obnov stránku. K postupu je připraven jiný úkol.","error"); return redirect(url_for("home"))
        task=eligible_item["task"]; sub=eligible_item["submission"]
        if not sub or sub["status"] not in ("approved","rejected"):
            db.rollback(); flash("Nejdřív odpověz na úkol nebo počkej na jeho vyhodnocení.","error"); return redirect(url_for("home"))
        if db.execute("SELECT 1 FROM task_advances WHERE user_id=? AND task_id=?",(fresh["id"],task["id"])).fetchone():
            db.rollback(); flash("Tento úkol už máš započítaný v postupu.","error"); return redirect(url_for("home"))
        from_pos=int(fresh["position"])
        if from_pos>=29:
            db.rollback(); flash("Už jsi na posledním políčku.","error"); return redirect(url_for("home"))
        setting=db.execute("SELECT skip_cost FROM space_settings WHERE space_number=?",(from_pos,)).fetchone()
        configured_cost=int(setting["skip_cost"] or 0) if setting else 0
        # A rejected answer can always be bypassed for exactly one gold coin.
        # Approved answers keep the administrator-configured field price.
        cost=1 if sub["status"]=="rejected" else configured_cost
        if cost<=0:
            db.rollback(); flash("Správce zatím nenastavil cenu postupu z tohoto políčka.","error"); return redirect(url_for("home"))
        if int(fresh["coins"] or 0)<cost:
            db.rollback(); flash(f"Nemáš dost zlaťáků. Potřebuješ {cost}, máš {fresh['coins']}.","error"); return redirect(url_for("home"))
        moved=record_task_advance(db,fresh["id"],task,"coins",cost)
        if not moved:
            raise sqlite3.IntegrityError("Postup už existuje nebo se změnil zůstatek hráče")
        db.commit()
        flash(f"Použil/a jsi {cost} zlaťáků a postoupil/a na políčko {from_pos+1}. Úkol i jeho vyhodnocení zůstávají v historii.","success")
    except sqlite3.IntegrityError:
        db.rollback(); flash("Postup už mohl být zaznamenán. Obnov stránku a zkontroluj pozici a zlaťáky.","error")
    except Exception:
        db.rollback(); raise
    return redirect(url_for("home"))

@app.route("/work-claim", methods=["POST"])
@login_required
def work_claim():
    db=get_db(); user=current_user()
    if user["role"] == "admin": return redirect(url_for("admin"))
    offer_id=request.form.get("offer_id", type=int)
    note=request.form.get("note", "").strip()[:500]
    offer=db.execute("SELECT id FROM work_offers WHERE id=? AND active=1", (offer_id,)).fetchone()
    if not offer:
        flash("Tato pracovní odměna už není dostupná.", "error")
    else:
        pending=db.execute("SELECT id FROM work_claims WHERE user_id=? AND offer_id=? AND status='pending'",(user["id"],offer_id)).fetchone()
        if pending:
            flash("Tuto práci už jsi odeslal/a a čeká na kontrolu.", "error")
        else:
            db.execute("INSERT INTO work_claims (user_id,offer_id,note,status) VALUES (?,?,?,'pending')",(user["id"],offer_id,note))
            db.commit(); flash("Práce odeslána správci ke kontrole. Zlaťáky se připíšou po schválení.","success")
    return redirect(url_for("home"))

@app.route("/admin", methods=["GET","POST"])
@admin_required
def admin():
    db=get_db()
    if request.method=="POST":
        action=request.form.get("action")
        if action in ("approve_registration","reject_registration"):
            pid=request.form.get("player_id",type=int)
            p=db.execute("SELECT * FROM users WHERE id=? AND role='player'",(pid,)).fetchone()
            if p and p["account_status"]=="pending":
                status="active" if action=="approve_registration" else "rejected"
                db.execute("UPDATE users SET account_status=? WHERE id=?",(status,pid)); db.commit()
                flash("Registrace byla schválena." if status=="active" else "Registrace byla zamítnuta.","success")
        elif action in ("approve","return","reject"):
            sid=request.form.get("submission_id",type=int); note=request.form.get("note","").strip()
            sub=db.execute("SELECT * FROM submissions WHERE id=?",(sid,)).fetchone()
            if sub and sub["status"]=="pending":
                if action == "approve":
                    try:
                        db.execute("BEGIN IMMEDIATE")
                        player=db.execute("SELECT * FROM users WHERE id=?",(sub["user_id"],)).fetchone()
                        task_row=db.execute("SELECT * FROM tasks WHERE id=?",(sub["task_id"],)).fetchone()
                        active_week=latest_published_week(db); week=current_week_key(); moved=False
                        # The only free manual-review advance is for the globally current published week.
                        # Older tasks must be paid through one by one, even when the answer is approved.
                        if (player and player["role"]=="player" and task_row and task_row["week_number"] is not None
                                and int(task_row["week_number"])==active_week):
                            moved=record_task_advance(db,sub["user_id"],task_row,"manual_approved",0)
                        db.execute("UPDATE submissions SET status='approved',reviewed_at=CURRENT_TIMESTAMP,review_note=?,approved_week=? WHERE id=?",(note,week,sid))
                        db.commit()
                        if moved:
                            flash("Úkol schválen. Hráč automaticky postoupil o jedno políčko a může vidět řešení i tvoji poznámku.","success")
                        else:
                            flash("Úkol schválen a uložen do historie. Pokud jde o starší týden, hráč použije zlaťák a postoupí.","success")
                    except sqlite3.IntegrityError:
                        db.rollback(); flash("Úkol nebyl schválen kvůli souběžné změně. Obnov stránku a zkus to znovu.","error")
                else:
                    status="needs_changes" if action=="return" else "rejected"
                    db.execute("UPDATE submissions SET status=?,reviewed_at=CURRENT_TIMESTAMP,review_note=? WHERE id=?",(status,note,sid))
                    db.commit()
                    flash("Úkol byl vrácen k doplnění." if action=="return" else "Úkol byl označen jako nesplněný.","success")
        elif action=="mark_incomplete":
            pid=request.form.get("player_id",type=int)
            player=db.execute("SELECT * FROM users WHERE id=? AND role='player' AND account_status='active'",(pid,)).fetchone()
            if not player:
                flash("Hráč nenalezen.","error")
            else:
                eligible_item, active_week = get_next_weekly_task(db, pid)
                if not eligible_item:
                    flash("Pro tohoto hráče není žádný zveřejněný nevyřízený týdenní úkol.","error")
                else:
                    task=eligible_item["task"]
                    sub=eligible_item["submission"]
                    if sub and sub["status"]=="pending":
                        flash("Úkol už čeká na kontrolu. Nejprve ho vyhodnoť.","error")
                    elif sub and sub["status"]=="approved":
                        flash("Tento úkol už byl schválen.","error")
                    elif sub and sub["status"]=="skipped":
                        flash("Tento úkol už byl přeskočen.","error")
                    elif sub and sub["status"]=="rejected":
                        flash("Úkol už je označen jako nesplněný; hráč může využít placený postup, pokud má nastavenou cenu.","success")
                    elif sub and sub["status"]=="needs_changes":
                        db.execute("UPDATE submissions SET status='rejected',reviewed_at=CURRENT_TIMESTAMP,review_note=COALESCE(review_note,'') || ? WHERE id=?",(" Správce následně označil úkol jako nesplněný.",sub["id"]))
                        db.commit(); flash("Úkol byl označen jako nesplněný. Hráči se může nabídnout placený postup.","success")
                    else:
                        db.execute("INSERT INTO submissions (user_id,task_id,answer,status,submitted_at,reviewed_at,review_note) VALUES (?,?,?,'rejected',CURRENT_TIMESTAMP,CURRENT_TIMESTAMP,?)",
                                   (pid,task["id"],"","Správce označil týdenní úkol jako nesplněný. Můžeš využít placený postup, pokud je cena nastavena."))
                        db.commit(); flash(f"Týdenní úkol {task['week_number']} byl označen jako nesplněný.","success")
        elif action=="edit_user_account":
            uid=request.form.get("user_id",type=int)
            username=request.form.get("username","").strip()
            display_name=request.form.get("display_name","").strip()
            status=request.form.get("account_status","").strip()
            allowed_statuses={"active","pending","rejected","disabled"}
            target=db.execute("SELECT id,role,account_status FROM users WHERE id=?",(uid,)).fetchone() if uid else None
            valid_username=(3<=len(username)<=30 and username.replace("_","").replace("-","").isalnum())
            if not target:
                flash("Uživatelský účet nebyl nalezen.","error")
            elif not valid_username:
                flash("Přihlašovací jméno musí mít 3–30 znaků a může obsahovat písmena, čísla, pomlčku a podtržítko.","error")
            elif not (2<=len(display_name)<=50):
                flash("Zobrazované jméno musí mít 2 až 50 znaků.","error")
            elif status not in allowed_statuses:
                flash("Vyber platný stav účtu.","error")
            elif target["id"]==session.get("user_id") and status!="active":
                flash("Svůj vlastní administrátorský účet nemůžeš deaktivovat ani přesunout do jiného stavu.","error")
            else:
                try:
                    db.execute("UPDATE users SET username=?,display_name=?,account_status=? WHERE id=?",
                               (username,display_name,status,uid))
                    db.commit()
                    flash("Uživatelský účet byl upraven.","success")
                except sqlite3.IntegrityError:
                    db.rollback()
                    flash("Toto přihlašovací jméno už používá jiný účet.","error")
        elif action=="reset_user_password":
            uid=request.form.get("user_id",type=int)
            password=request.form.get("new_password","")
            confirm=request.form.get("confirm_password","")
            target=db.execute("SELECT id,username FROM users WHERE id=?",(uid,)).fetchone() if uid else None
            if not target:
                flash("Uživatelský účet nebyl nalezen.","error")
            elif len(password)<8 or len(password)>128:
                flash("Nové heslo musí mít 8 až 128 znaků.","error")
            elif password!=confirm:
                flash("Nové heslo a jeho potvrzení se neshodují.","error")
            else:
                db.execute("UPDATE users SET password_hash=?,session_version=COALESCE(session_version,1)+1 WHERE id=?",
                           (generate_password_hash(password),uid))
                db.commit()
                flash(f"Nové heslo pro účet @{target['username']} bylo nastaveno. Heslo se nikde nevypisuje — předej ho hráči bezpečným způsobem.","success")
        elif action=="add_player":
            username=request.form.get("username","").strip()
            display_name=request.form.get("display_name","").strip()
            password=request.form.get("password","")
            count=db.execute("SELECT COUNT(*) AS n FROM users WHERE role='player' AND account_status IN ('active','pending')").fetchone()["n"]
            if count>=MAX_PLAYERS:
                flash(f"Hra už dosáhla kapacity {MAX_PLAYERS} hráčů. Další účet nelze přidat.","error")
            elif not (3<=len(username)<=30 and username.replace("_","").replace("-","").isalnum()):
                flash("Přihlašovací jméno musí mít 3–30 znaků a může obsahovat písmena, čísla, pomlčku a podtržítko.","error")
            elif not (2<=len(display_name)<=50):
                flash("Zobrazované jméno musí mít 2 až 50 znaků.","error")
            elif len(password)<8:
                flash("Dočasné heslo musí mít alespoň 8 znaků.","error")
            else:
                try:
                    db.execute("INSERT INTO users (username,password_hash,display_name,role,position,coins,account_status,created_at,avatar_id) VALUES (?,?,?,?,?,?,?,CURRENT_TIMESTAMP,?)",
                               (username,generate_password_hash(password),display_name,"player",1,0,"active","avatar-001"))
                    db.commit()
                    flash(f"Účet @{username} byl vytvořen a je aktivní. Dočasné heslo předej hráči bezpečným způsobem.","success")
                except sqlite3.IntegrityError:
                    db.rollback()
                    flash("Toto přihlašovací jméno už používá jiný účet.","error")
        elif action=="edit_player":
            pid=request.form.get("player_id",type=int); name=request.form.get("display_name","").strip()
            if pid and 2<=len(name)<=50:
                db.execute("UPDATE users SET display_name=? WHERE id=? AND role='player'",(name,pid)); db.commit(); flash("Jméno hráče bylo upraveno.","success")
            else: flash("Zadej platné zobrazované jméno.","error")
        elif action=="set_position":
            pid=request.form.get("player_id",type=int); pos=request.form.get("position",type=int)
            player=db.execute("SELECT position FROM users WHERE id=? AND role='player' AND account_status='active'",(pid,)).fetchone()
            if player and pos and 1<=pos<=29:
                old_pos=int(player["position"])
                if old_pos != pos:
                    week=current_week_key()
                    db.execute("""INSERT INTO player_advances (user_id,week_key,from_position,to_position,source,cost,created_at)
                                  VALUES (?,?,?,?,?,0,CURRENT_TIMESTAMP)
                                  ON CONFLICT(user_id,week_key) DO UPDATE SET from_position=excluded.from_position,to_position=excluded.to_position,source='admin',cost=0,created_at=CURRENT_TIMESTAMP""",
                               (pid,week,old_pos,pos,"admin"))
                    db.execute("UPDATE users SET position=? WHERE id=?",(pos,pid))
                    db.commit(); flash("Pozice hráče byla ručně nastavena. Automatický postup je tento týden tímto zaznamenán jako využitý.","success")
                else: flash("Hráč už na tomto políčku je.","success")
            else: flash("Pozice musí být číslo 1 až 29.","error")
        elif action=="add_coins":
            pid=request.form.get("player_id",type=int); amount=request.form.get("amount",type=int)
            if pid and amount is not None:
                db.execute("UPDATE users SET coins=MAX(0,coins+?) WHERE id=? AND role='player'",(amount,pid)); db.commit(); flash("Zlaťáky byly upraveny.","success")
        elif action=="set_cost":
            pos=request.form.get("space_number",type=int); cost=request.form.get("skip_cost",type=int)
            if pos and 1<=pos<=29 and cost is not None and 0<=cost<=1000000:
                db.execute("INSERT INTO space_settings (space_number,skip_cost) VALUES (?,?) ON CONFLICT(space_number) DO UPDATE SET skip_cost=excluded.skip_cost",(pos,cost))
                db.commit(); flash(f"Cena postupu z políčka {pos} na políčko {min(29,pos+1)} byla uložena.","success")
            else: flash("Cena musí být celé číslo od 0 do 1 000 000.","error")
        elif action in ("save_week_task", "publish_week_task"):
            week_no=request.form.get("week_number",type=int)
            title=request.form.get("title","").strip()
            month_title=request.form.get("month_title","").strip()
            date_range=request.form.get("date_range","").strip()
            story_intro=request.form.get("story_intro","").strip()
            description=request.form.get("description","").strip()
            task_type=request.form.get("task_type","text")
            options_text=request.form.get("options_text","").strip()
            correct_answer=request.form.get("correct_answer","").strip()
            explanation=request.form.get("explanation","").strip()
            failure_consequence=request.form.get("failure_consequence","").strip()
            existing_task=db.execute("SELECT * FROM tasks WHERE week_number=?",(week_no,)).fetchone() if week_no else None
            if not existing_task:
                flash("Vybraný týden nebyl nalezen.","error")
            elif action=="publish_week_task" and existing_task["published"]:
                flash("Tento úkol už je zveřejněný. Pro opravu použij tlačítko Uložit opravu uzamčeného úkolu.","error")
            elif existing_task["published"] and request.form.get("edit_published_confirm") != "1":
                flash("Před úpravou zveřejněného úkolu zaškrtni potvrzení opravy uzamčeného úkolu.","error")
            elif not title or not description or not date_range or not month_title:
                flash("Vyplň název, termín, měsíc a zadání úkolu.","error")
            elif task_type not in ("text","choice","photo","challenge"):
                flash("Vyber platný typ úkolu.","error")
            elif task_type=="choice" and len([x for x in options_text.splitlines() if x.strip()])<2:
                flash("Úkol s výběrem musí mít alespoň dvě možnosti, každou na samostatném řádku.","error")
            else:
                db.execute("""UPDATE tasks SET title=?,month_title=?,date_range=?,story_intro=?,description=?,task_type=?,
                              options_text=?,correct_answer=?,explanation=?,failure_consequence=? WHERE id=?""",
                           (title,month_title,date_range,story_intro,description,task_type,
                            "\n".join(x.strip() for x in options_text.splitlines() if x.strip()) if task_type=="choice" else None,
                            correct_answer,explanation,failure_consequence,existing_task["id"]))
                if action=="publish_week_task":
                    not_published_before=db.execute("SELECT COUNT(*) AS n FROM tasks WHERE week_number<? AND (published IS NULL OR published=0)",(week_no,)).fetchone()["n"]
                    if not_published_before:
                        db.rollback()
                        flash("Týdny se zveřejňují postupně. Nejdřív aktivuj všechny předchozí týdny.","error")
                    else:
                        db.execute("UPDATE tasks SET published=1,active=1,published_at=CURRENT_TIMESTAMP WHERE id=?",(existing_task["id"],))
                        db.commit()
                        flash(f"Týden {week_no} je zveřejněný a uzamčený. Všichni hráči mají stejné zadání a mohou dohánět starší týdny.","success")
                else:
                    db.commit()
                    if existing_task["published"]:
                        flash(f"Oprava zveřejněného úkolu pro týden {week_no} byla uložena. Úkol zůstává zveřejněný; již uložená vyhodnocení odpovědí se zpětně nemění.","success")
                    else:
                        flash(f"Návrh úkolu pro týden {week_no} byl uložen. Do zveřejnění ho můžeš znovu upravit.","success")
        elif action=="set_task":
            pos=request.form.get("space_number",type=int)
            title=request.form.get("title","").strip(); description=request.form.get("description","").strip()
            task_type=request.form.get("task_type","text"); options_text=request.form.get("options_text","").strip()
            if task_type not in ("text","choice","photo","challenge"): task_type="text"
            options=[line.strip() for line in options_text.splitlines() if line.strip()]
            if pos is None or not 1<=pos<=29:
                flash("Vyber políčko od 1 do 29.","error")
            elif not title or not description:
                flash("Vyplň název i zadání úkolu.","error")
            elif task_type=="choice" and len(options)<2:
                flash("Úkol s výběrem musí mít alespoň dvě možnosti, každou na samostatném řádku.","error")
            else:
                db.execute("UPDATE tasks SET active=0 WHERE space_number=?",(pos,))
                db.execute("INSERT INTO tasks (title,description,active,created_at,task_type,options_text,space_number) VALUES (?,?,1,CURRENT_TIMESTAMP,?,?,?)",
                           (title,description,task_type,"\n".join(options) if task_type=="choice" else None,pos))
                db.commit(); flash(f"Aktivní úkol pro políčko {pos} byl uložen. Původní odevzdání a historie zůstaly zachované.","success")
        elif action=="add_work_offer":
            title=request.form.get("work_title", "").strip()
            description=request.form.get("work_description", "").strip()
            reward=request.form.get("work_reward", type=int)
            if not title or not description or reward is None or not 1<=reward<=10000:
                flash("Vyplň název, popis a odměnu 1 až 10 000 zlaťáků.","error")
            else:
                db.execute("INSERT INTO work_offers (title,description,reward,active) VALUES (?,?,?,1)",(title[:100],description[:1000],reward))
                db.commit(); flash("Pracovní odměna byla přidána.","success")
        elif action=="toggle_work_offer":
            oid=request.form.get("offer_id",type=int)
            db.execute("UPDATE work_offers SET active=CASE WHEN active=1 THEN 0 ELSE 1 END WHERE id=?",(oid,))
            db.commit(); flash("Dostupnost pracovní odměny byla změněna.","success")
        elif action in ("approve_work", "reject_work"):
            cid=request.form.get("claim_id",type=int)
            claim=db.execute("SELECT c.*,o.reward,o.title FROM work_claims c JOIN work_offers o ON o.id=c.offer_id WHERE c.id=? AND c.status='pending'",(cid,)).fetchone()
            if claim:
                db.execute("BEGIN IMMEDIATE")
                if action=="approve_work":
                    db.execute("UPDATE users SET coins=coins+? WHERE id=? AND role='player'",(claim["reward"],claim["user_id"]))
                    db.execute("UPDATE work_claims SET status='approved',reviewed_at=CURRENT_TIMESTAMP WHERE id=?",(cid,))
                    db.commit(); flash(f"Schváleno: připsáno {claim['reward']} zlaťáků.","success")
                else:
                    db.execute("UPDATE work_claims SET status='rejected',reviewed_at=CURRENT_TIMESTAMP WHERE id=?",(cid,))
                    db.commit(); flash("Pracovní požadavek byl zamítnut.","success")
        elif action=="add_message":
            pid=request.form.get("player_id",type=int); message=request.form.get("message","").strip()
            target=db.execute("SELECT id FROM users WHERE id=? AND role='player' AND account_status='active'",(pid,)).fetchone()
            if target and message:
                db.execute("UPDATE player_messages SET active=0 WHERE user_id=?",(pid,))
                db.execute("INSERT INTO player_messages (user_id,message,active,created_at) VALUES (?,?,1,CURRENT_TIMESTAMP)",(pid,message[:240]))
                db.commit(); flash("Soukromá zpráva se zobrazí pouze danému hráči v bublině u jeho políčka.","success")
            else: flash("Vyber hráče a napiš zprávu.","error")
        elif action=="clear_message":
            pid=request.form.get("player_id",type=int)
            if pid:
                db.execute("UPDATE player_messages SET active=0 WHERE user_id=?",(pid,)); db.commit(); flash("Bublina hráče byla odstraněna.","success")
        if action in ("edit_user_account", "reset_user_password", "add_player"):
            target_section = "players" if action == "add_player" else "userAccounts"
            return redirect(url_for("admin") + "#" + target_section)
        return redirect(url_for("admin"))

    work_offers=db.execute("SELECT * FROM work_offers ORDER BY id DESC").fetchall()
    work_claims=db.execute("SELECT c.*,u.display_name,u.username,o.title AS offer_title,o.reward FROM work_claims c JOIN users u ON u.id=c.user_id JOIN work_offers o ON o.id=c.offer_id WHERE c.status='pending' ORDER BY c.submitted_at").fetchall()
    registrations=[dict(r, avatar_name=get_avatar_name(r["avatar_id"])) for r in db.execute("SELECT * FROM users WHERE role='player' AND account_status='pending' ORDER BY created_at").fetchall()]
    user_accounts=db.execute("SELECT id,username,display_name,role,account_status,position,coins,created_at FROM users ORDER BY CASE role WHEN 'admin' THEN 0 ELSE 1 END, created_at, username").fetchall()
    pending=[dict(r, avatar_name=get_avatar_name(r["avatar_id"])) for r in db.execute("""SELECT s.*,u.display_name,u.username,u.position,u.avatar_id,t.title AS task_title,t.space_number,t.week_number,t.correct_answer,t.explanation,t.failure_consequence,t.date_range
      FROM submissions s JOIN users u ON u.id=s.user_id JOIN tasks t ON t.id=s.task_id WHERE s.status='pending' ORDER BY s.submitted_at""").fetchall()]
    players=[dict(r, avatar_name=get_avatar_name(r["avatar_id"])) for r in db.execute("""SELECT u.*,(SELECT message FROM player_messages pm WHERE pm.user_id=u.id AND pm.active=1 ORDER BY pm.id DESC LIMIT 1) AS active_message
      FROM users u WHERE u.role='player' AND u.account_status='active' ORDER BY u.display_name""").fetchall()]
    history=db.execute("""SELECT s.*,u.display_name,u.username,t.title AS task_title,t.space_number,t.week_number,t.date_range
      FROM submissions s JOIN users u ON u.id=s.user_id JOIN tasks t ON t.id=s.task_id
      WHERE s.status!='pending' ORDER BY COALESCE(s.reviewed_at,s.submitted_at) DESC LIMIT 100""").fetchall()
    legacy_advance_history=[dict(r) for r in db.execute("""SELECT a.*,u.display_name,u.username FROM player_advances a JOIN users u ON u.id=a.user_id ORDER BY a.created_at DESC LIMIT 100""").fetchall()]
    task_advance_history=[dict(r) for r in db.execute("""SELECT a.*,u.display_name,u.username,t.title AS task_title
        FROM task_advances a JOIN users u ON u.id=a.user_id JOIN tasks t ON t.id=a.task_id
        ORDER BY a.created_at DESC LIMIT 150""").fetchall()]
    for _advance in task_advance_history:
        _advance["week_key"] = f"Týden {_advance.get('week_number') or '—'}"
    advance_history=sorted(task_advance_history+legacy_advance_history,key=lambda r:r.get("created_at") or "",reverse=True)[:100]
    task_by_space={}
    for t in db.execute("SELECT * FROM tasks WHERE active=1 ORDER BY id DESC").fetchall():
        if t["space_number"] not in task_by_space: task_by_space[t["space_number"]]=t
    costs={r["space_number"]:r["skip_cost"] for r in db.execute("SELECT * FROM space_settings").fetchall()}
    space_rows=[]
    for n in range(1,30):
        t=task_by_space.get(n)
        space_rows.append({"number":n,"task_title":t["title"] if t else "Zatím bez úkolu","task_type":t["task_type"] if t else "—","has_task":bool(t),"cost":costs.get(n,0),"target":min(29,n+1)})
    active_tasks_data = {}
    for n, t in task_by_space.items():
        active_tasks_data[str(n)] = {
            "title": t["title"], "description": t["description"],
            "task_type": t["task_type"] or "text", "options_text": t["options_text"] or ""
        }
    weekly_task_rows=db.execute("SELECT * FROM tasks WHERE week_number IS NOT NULL ORDER BY week_number").fetchall()
    weekly_tasks=[]
    weekly_tasks_data={}
    for wt in weekly_task_rows:
        item=dict(wt)
        item["published"]=bool(item.get("published"))
        item["status_label"]="Zveřejněný · uzamčený" if item["published"] else "Návrh · lze upravit"
        item["submission_count"]=db.execute("SELECT COUNT(*) AS n FROM submissions WHERE task_id=?",(item["id"],)).fetchone()["n"]
        weekly_tasks.append(item)
        weekly_tasks_data[str(item["week_number"]) ]={k:item.get(k) for k in ["title","month_title","date_range","story_intro","description","task_type","options_text","correct_answer","explanation","failure_consequence","published"]}
    latest_week=latest_published_week(db)

    active_count=db.execute("SELECT COUNT(*) AS n FROM users WHERE role='player' AND account_status='active'").fetchone()["n"]
    all_count=db.execute("SELECT COUNT(*) AS n FROM users WHERE role='player' AND account_status IN ('active','pending')").fetchone()["n"]
    return render_template("admin.html",registrations=registrations,user_accounts=user_accounts,pending=pending,players=players,history=history,
                           current_admin_id=session.get("user_id"),advance_history=advance_history,space_rows=space_rows,active_count=active_count,
                           all_count=all_count,capacity=MAX_PLAYERS,avatars=AVATARS,week_key=current_week_key(),
                           work_offers=work_offers,work_claims=work_claims,active_tasks_data=active_tasks_data,
                           weekly_tasks=weekly_tasks,weekly_tasks_data=weekly_tasks_data,latest_week=latest_week)

# Gunicorn imports app:app without running the __main__ block, so database schema creation
# and safe migrations must happen on import as well as when running the app locally.
init_db()

if __name__ == "__main__":
    app.run(host="0.0.0.0" if os.environ.get("PORT") else "127.0.0.1",
            port=int(os.environ.get("PORT", "5000")), debug=False)
