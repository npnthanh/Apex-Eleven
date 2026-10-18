"""Account sessions and isolated save files for the single-player game."""

import os
import re
import secrets
import sqlite3
import time
import uuid
from contextlib import closing
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import urlsplit

import click
from flask import (
    Blueprint, abort, current_app, flash, g, redirect, render_template,
    request, session, url_for,
)
from werkzeug.security import check_password_hash, generate_password_hash


auth = Blueprint("auth", __name__)
USERNAME_PATTERN = re.compile(r"[a-z0-9_.-]{3,32}\Z")
LOGIN_WINDOW = 15 * 60
# Check an actual hash even when the account is absent.
DUMMY_PASSWORD_HASH = generate_password_hash(secrets.token_urlsafe(32))


def get_auth_db():
    if "auth_db" not in g:
        root = Path(current_app.config["DATA_DIR"])
        root.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(root / "accounts.sqlite3", timeout=30)
        conn.row_factory = sqlite3.Row
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS user_account (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                username TEXT NOT NULL UNIQUE COLLATE NOCASE,
                password_hash TEXT NOT NULL,
                game_key TEXT NOT NULL UNIQUE,
                created_at INTEGER NOT NULL
            );
            CREATE TABLE IF NOT EXISTS login_failure (
                address TEXT NOT NULL,
                username TEXT NOT NULL,
                created_at INTEGER NOT NULL
            );
            CREATE INDEX IF NOT EXISTS login_failure_address_time
                ON login_failure(address, created_at);
        """)
        g.auth_db = conn
    return g.auth_db


def game_db_path():
    """Resolve ownership only from the account loaded on the server."""
    if g.get("user") is None:
        raise RuntimeError("A signed-in account is required to open a game save.")
    root = Path(current_app.config["DATA_DIR"]) / "games"
    root.mkdir(parents=True, exist_ok=True)
    return root / (g.user["game_key"] + ".sqlite3")


def csrf_token():
    if "csrf_token" not in session:
        session["csrf_token"] = secrets.token_urlsafe(32)
    return session["csrf_token"]


def safe_next():
    target = request.args.get("next", "")
    try:
        parts = urlsplit(target)
    except ValueError:
        return url_for("home")
    if (target.startswith("/") and not target.startswith("//")
            and not parts.scheme and not parts.netloc
            and "\\" not in target and not any(ord(c) < 32 for c in target)):
        return target
    return url_for("home")


def start_session(user_id):
    session.clear()
    session["user_id"] = user_id
    session.permanent = True


@auth.route("/register", methods=["GET", "POST"])
def register():
    if g.user is not None:
        return redirect(url_for("home"))
    errors = []
    username = request.form.get("username", "").strip().lower()
    club_name = " ".join(request.form.get("club_name", "").split())
    if request.method == "POST":
        password = request.form.get("password", "")
        if not USERNAME_PATTERN.fullmatch(username):
            errors.append("Tên đăng nhập cần 3–32 ký tự: chữ không dấu, số, dấu chấm, gạch dưới hoặc gạch ngang.")
        if not 8 <= len(password) <= 128:
            errors.append("Mật khẩu cần từ 8 đến 128 ký tự.")
        if password != request.form.get("confirm_password", ""):
            errors.append("Mật khẩu xác nhận chưa khớp.")
        if not 2 <= len(club_name) <= 28 or any(ord(c) < 32 for c in club_name):
            errors.append("Tên đội bóng cần từ 2 đến 28 ký tự.")
        if not errors:
            password_hash = generate_password_hash(password)
            conn = get_auth_db()
            save_path = None
            try:
                # Do not expose an account until its initial save is ready.
                conn.execute("BEGIN IMMEDIATE")
                cur = conn.execute(
                    "INSERT INTO user_account (username, password_hash, game_key, created_at) VALUES (?, ?, ?, ?)",
                    (username, password_hash, uuid.uuid4().hex, int(time.time())),
                )
                user_id = cur.lastrowid
                g.user = conn.execute("SELECT * FROM user_account WHERE id=?", (user_id,)).fetchone()
                save_path = game_db_path()
                current_app.extensions["initialize_game"](club_name=club_name)
                conn.commit()
            except Exception as error:
                conn.rollback()
                if save_path is not None:
                    for game_conn in g.pop("game_connections", []):
                        game_conn.close()
                    save_path.unlink(missing_ok=True)
                g.user = None
                if isinstance(error, sqlite3.IntegrityError) and "user_account.username" in str(error):
                    errors.append("Tên đăng nhập này đã được sử dụng.")
                else:
                    raise
            else:
                start_session(user_id)
                flash("Tạo tài khoản thành công. Đội bóng của bạn đã sẵn sàng!")
                return redirect(url_for("home"))
    return render_template("auth.html", mode="register", errors=errors,
                           username=username, club_name=club_name), (400 if errors else 200)


@auth.route("/login", methods=["GET", "POST"])
def login():
    if g.user is not None:
        return redirect(url_for("home"))
    errors = []
    status = 200
    username = request.form.get("username", "").strip().lower()
    if request.method == "POST":
        conn = get_auth_db()
        address = request.remote_addr or "unknown"
        now = int(time.time())
        conn.execute("DELETE FROM login_failure WHERE created_at < ?", (now - LOGIN_WINDOW,))
        failures = conn.execute(
            "SELECT COUNT(*) FROM login_failure WHERE address=?", (address,),
        ).fetchone()[0]
        user_failures = conn.execute(
            "SELECT COUNT(*) FROM login_failure WHERE address=? AND username=?", (address, username[:32]),
        ).fetchone()[0]
        conn.commit()
        if failures >= 30 or user_failures >= 5:
            errors.append("Bạn đã đăng nhập sai quá nhiều lần. Vui lòng thử lại sau 15 phút.")
            status = 429
        else:
            user = conn.execute("SELECT * FROM user_account WHERE username=?", (username,)).fetchone()
            password = request.form.get("password", "")
            valid = check_password_hash(user["password_hash"] if user else DUMMY_PASSWORD_HASH, password[:129])
            if user is not None and valid and len(password) <= 128:
                conn.execute("DELETE FROM login_failure WHERE address=? AND username=?", (address, username))
                conn.commit()
                start_session(user["id"])
                flash("Đăng nhập thành công. Chào mừng bạn trở lại!")
                return redirect(safe_next())
            conn.execute("INSERT INTO login_failure VALUES (?, ?, ?)", (address, username[:32], now))
            conn.commit()
            errors.append("Tên đăng nhập hoặc mật khẩu không đúng.")
            status = 400
    return render_template("auth.html", mode="login", errors=errors, username=username), status


@auth.post("/logout")
def logout():
    session.clear()
    flash("Bạn đã đăng xuất.")
    return redirect(url_for("auth.login"))


@auth.post("/change_password")
def change_password():
    if g.user is None:
        return redirect(url_for("auth.login"))

    current_pwd = request.form.get("current_password", "")
    new_pwd = request.form.get("new_password", "")
    confirm_pwd = request.form.get("confirm_password", "")

    fallback = request.referrer or url_for("home")

    if not check_password_hash(g.user["password_hash"], current_pwd):
        flash("Mật khẩu hiện tại không chính xác.")
        return redirect(fallback)

    if not 8 <= len(new_pwd) <= 128:
        flash("Mật khẩu mới phải từ 8 đến 128 ký tự.")
        return redirect(fallback)

    if new_pwd != confirm_pwd:
        flash("Mật khẩu xác nhận không khớp.")
        return redirect(fallback)

    conn = get_auth_db()
    conn.execute(
        "UPDATE user_account SET password_hash=? WHERE id=?",
        (generate_password_hash(new_pwd), g.user["id"]),
    )
    conn.commit()
    flash("Đổi mật khẩu thành công!")
    return redirect(fallback)


def init_auth(app, initialize_game):
    root = Path(os.environ.get("TOUCHLINE_DATA_DIR", app.instance_path)).resolve()
    root.mkdir(parents=True, exist_ok=True)
    secret = os.environ.get("SECRET_KEY")
    if not secret:
        secret_file = root / "session.key"
        try:
            fd = os.open(secret_file, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        except FileExistsError:
            secret = secret_file.read_text(encoding="ascii").strip()
        else:
            secret = secrets.token_hex(32)
            with os.fdopen(fd, "w", encoding="ascii") as handle:
                handle.write(secret)
    if not secret:
        raise RuntimeError("The session key must not be empty.")
    app.config.update(
        SECRET_KEY=secret,
        DATA_DIR=str(root),
        SESSION_COOKIE_HTTPONLY=True,
        SESSION_COOKIE_SAMESITE="Lax",
        SESSION_COOKIE_SECURE=os.environ.get("SESSION_COOKIE_SECURE", "0") == "1",
        PERMANENT_SESSION_LIFETIME=timedelta(days=7),
        MAX_CONTENT_LENGTH=64 * 1024,
    )
    app.extensions["initialize_game"] = initialize_game
    app.register_blueprint(auth)
    app.jinja_env.globals["csrf_token"] = csrf_token

    @app.cli.command("import-legacy")
    @click.option("--username", required=True, help="Account that will own the old save.")
    @click.option("--source", type=click.Path(exists=True, dir_okay=False, path_type=Path),
                  default=Path(app.root_path) / "game.db", show_default=True)
    def import_legacy(username, source):
        """Import a pre-account save. Stop the game server before running this command."""
        g.user = get_auth_db().execute(
            "SELECT * FROM user_account WHERE username=?", (username.strip().lower(),),
        ).fetchone()
        if g.user is None:
            raise click.ClickException("Account not found. Register it first.")
        target = game_db_path()
        if source.resolve() == target.resolve():
            raise click.ClickException("Source and destination must be different files.")
        temporary = target.with_suffix(".import-" + uuid.uuid4().hex)
        try:
            with closing(sqlite3.connect(source.resolve().as_uri() + "?mode=ro", uri=True)) as original:
                tables = {row[0] for row in original.execute("SELECT name FROM sqlite_master WHERE type='table'")}
                required = {"club", "facility", "squad_player", "market_listing", "team", "fixture"}
                if not required <= tables or original.execute("SELECT COUNT(*) FROM club WHERE id=1").fetchone()[0] != 1:
                    raise click.ClickException("This file is not a game save.")
                if original.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
                    raise click.ClickException("The source database is damaged.")
                destination = sqlite3.connect(temporary)
                try:
                    original.backup(destination)
                finally:
                    destination.close()
            if target.exists():
                backup = target.with_suffix(".backup-" + str(time.time_ns()) + ".sqlite3")
                previous = sqlite3.connect(target)
                saved = sqlite3.connect(backup)
                try:
                    previous.backup(saved)
                finally:
                    saved.close()
                    previous.close()
                click.echo(f"Previous account save backed up to: {backup}")
            os.replace(temporary, target)
        except sqlite3.Error as error:
            raise click.ClickException(f"Could not import save: {error}") from error
        finally:
            temporary.unlink(missing_ok=True)
        click.echo(f"Imported the save for {g.user['username']}. The source file was preserved.")

    @app.before_request
    def authenticate_request():
        g.user = None
        if request.endpoint == "static":
            return
        user_id = session.get("user_id")
        if isinstance(user_id, int):
            g.user = get_auth_db().execute(
                "SELECT * FROM user_account WHERE id=?", (user_id,),
            ).fetchone()
        if user_id is not None and g.user is None:
            session.clear()
        if request.endpoint is None:
            return
        if g.user is None and request.endpoint not in {"auth.login", "auth.register"}:
            target = request.full_path.rstrip("?") if request.method == "GET" else url_for("home")
            return redirect(url_for("auth.login", next=target))
        if request.method in {"POST", "PUT", "PATCH", "DELETE"}:
            payload = request.get_json(silent=True) if request.is_json else None
            if payload is not None and not isinstance(payload, dict):
                abort(400)
            json_csrf = (payload or {}).get("csrf_token", "")
            supplied = (
                request.form.get("csrf_token", "")
                or request.headers.get("X-CSRFToken", "")
                or json_csrf
            )
            expected = session.get("csrf_token", "")
            if not expected or not isinstance(supplied, str) or not secrets.compare_digest(expected.encode(), supplied.encode()):
                abort(400, description="Phiên biểu mẫu đã hết hạn. Vui lòng tải lại trang và thử lại.")

    @app.teardown_appcontext
    def close_auth_db(error=None):
        conn = g.pop("auth_db", None)
        if conn is not None:
            conn.close()

    @app.context_processor
    def account_context():
        user = g.get("user")
        joined = datetime.fromtimestamp(user["created_at"], timezone(timedelta(hours=7))).strftime("%d/%m/%Y %H:%M") if user else "—"
        return {"current_user": user, "account_created_at": joined}

    @app.after_request
    def private_response(response):
        if request.endpoint != "static":
            response.headers["Cache-Control"] = "no-store"
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["X-Frame-Options"] = "SAMEORIGIN"
        return response
