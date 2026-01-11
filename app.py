import io
import os
import re
import sqlite3
import time
import zipfile
from functools import wraps
from pathlib import Path

from flask import Flask, flash, redirect, render_template, request, send_file, send_from_directory, url_for
from flask_login import LoginManager, UserMixin, current_user, login_required, login_user, logout_user
from werkzeug.security import check_password_hash, generate_password_hash
from werkzeug.utils import secure_filename

# ================== CONFIG ==================
BASE_DIR = Path(__file__).resolve().parent
INSTANCE_DIR = BASE_DIR / "instance"
DB_PATH = INSTANCE_DIR / "app.db"

# Carpeta base donde se guardan TODOS los proyectos (cada proyecto será subcarpeta)
UPLOADS_ROOT = BASE_DIR / "uploads"
ALLOWED_EXTENSIONS = {"png", "jpg", "jpeg", "webp"}


def slugify(name: str) -> str:
    name = name.strip().lower()
    name = re.sub(r"\s+", "_", name)
    name = re.sub(r"[^a-z0-9_\-]", "", name)
    return name[:60] if name else "proyecto"


def create_app():
    app = Flask(__name__)
    app.secret_key = os.environ.get("SECRET_KEY", "dev_secret_change_me")

    INSTANCE_DIR.mkdir(exist_ok=True)
    UPLOADS_ROOT.mkdir(exist_ok=True)

    # ========== LOGIN MANAGER ==========
    login_manager = LoginManager()
    login_manager.login_view = "login"
    login_manager.init_app(app)

    # ========== DB HELPERS ==========
    def get_db():
        conn = sqlite3.connect(DB_PATH)
        conn.row_factory = sqlite3.Row
        return conn

    def table_has_column(conn, table: str, column: str) -> bool:
        cols = conn.execute(f"PRAGMA table_info({table})").fetchall()
        return any(c["name"] == column for c in cols)

    def ensure_table_users(conn):
        conn.execute("""
            CREATE TABLE IF NOT EXISTS users (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                username TEXT UNIQUE NOT NULL,
                password_hash TEXT NOT NULL,
                is_admin INTEGER NOT NULL DEFAULT 0
            )
        """)

    def ensure_table_projects(conn):
        conn.execute("""
            CREATE TABLE IF NOT EXISTS projects (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT UNIQUE NOT NULL,
                slug TEXT UNIQUE NOT NULL,
                created_at TEXT NOT NULL DEFAULT (datetime('now','localtime')),
                description TEXT,
                status TEXT NOT NULL DEFAULT 'pendiente'
            )
        """)

        # migración suave si venías de versión sin description/status
        if not table_has_column(conn, "projects", "description"):
            try:
                conn.execute("ALTER TABLE projects ADD COLUMN description TEXT")
            except sqlite3.OperationalError:
                pass

        if not table_has_column(conn, "projects", "status"):
            try:
                conn.execute("ALTER TABLE projects ADD COLUMN status TEXT NOT NULL DEFAULT 'pendiente'")
            except sqlite3.OperationalError:
                pass

    def ensure_table_photos(conn):
        conn.execute("""
            CREATE TABLE IF NOT EXISTS photos (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                filepath TEXT,
                filename TEXT,
                display_name TEXT NOT NULL,
                description TEXT,
                uploaded_at TEXT NOT NULL DEFAULT (datetime('now','localtime')),
                uploaded_by INTEGER,
                project_id INTEGER,
                FOREIGN KEY (uploaded_by) REFERENCES users(id),
                FOREIGN KEY (project_id) REFERENCES projects(id)
            )
        """)

        # migración suave
        if not table_has_column(conn, "photos", "project_id"):
            try:
                conn.execute("ALTER TABLE photos ADD COLUMN project_id INTEGER")
            except sqlite3.OperationalError:
                pass

        if not table_has_column(conn, "photos", "filepath"):
            try:
                conn.execute("ALTER TABLE photos ADD COLUMN filepath TEXT")
            except sqlite3.OperationalError:
                pass

        if not table_has_column(conn, "photos", "filename"):
            try:
                conn.execute("ALTER TABLE photos ADD COLUMN filename TEXT")
            except sqlite3.OperationalError:
                pass

    def init_db():
        conn = get_db()
        ensure_table_users(conn)
        ensure_table_projects(conn)
        ensure_table_photos(conn)
        conn.commit()
        conn.close()

    init_db()

    # ========== USER MODEL ==========
    class User(UserMixin):
        def __init__(self, row):
            self.id = int(row["id"])
            self.username = row["username"]
            self.password_hash = row["password_hash"]
            self.is_admin = bool(row["is_admin"])

    @login_manager.user_loader
    def load_user(user_id):
        conn = get_db()
        row = conn.execute("SELECT * FROM users WHERE id = ?", (user_id,)).fetchone()
        conn.close()
        return User(row) if row else None

    def ensure_admin_and_default_project():
        conn = get_db()

        # admin
        existing_admin = conn.execute("SELECT id FROM users WHERE is_admin = 1 LIMIT 1").fetchone()
        if not existing_admin:
            conn.execute(
                "INSERT INTO users (username, password_hash, is_admin) VALUES (?, ?, 1)",
                ("admin", generate_password_hash("admin123")),
            )

        # default project
        default = conn.execute("SELECT id FROM projects WHERE slug = ?", ("general",)).fetchone()
        if not default:
            conn.execute(
                "INSERT INTO projects (name, slug, description, status) VALUES (?, ?, ?, ?)",
                ("General", "general", "Proyecto por defecto", "pendiente"),
            )

        conn.commit()

        # backfill para fotos antiguas
        general_id = conn.execute("SELECT id FROM projects WHERE slug = ?", ("general",)).fetchone()["id"]

        conn.execute("UPDATE photos SET project_id = ? WHERE project_id IS NULL", (general_id,))
        conn.execute("""
            UPDATE photos
            SET filepath = ('general/' || filename)
            WHERE (filepath IS NULL OR filepath = '') AND filename IS NOT NULL
        """)

        conn.commit()
        conn.close()

        (UPLOADS_ROOT / "general").mkdir(parents=True, exist_ok=True)

    ensure_admin_and_default_project()

    # ========== PERMISOS ==========
    def admin_required(view_func):
        @wraps(view_func)
        @login_required
        def wrapper(*args, **kwargs):
            if not getattr(current_user, "is_admin", False):
                flash("Acceso solo para administradores.", "error")
                return redirect(url_for("dashboard"))
            return view_func(*args, **kwargs)
        return wrapper

    def upload_required(view_func):
        @wraps(view_func)
        @login_required
        def wrapper(*args, **kwargs):
            if not getattr(current_user, "is_admin", False):
                flash("No tienes permisos para subir imágenes.", "error")
                return redirect(url_for("dashboard"))
            return view_func(*args, **kwargs)
        return wrapper

    # ========== UTILS ==========
    def allowed_file(filename: str) -> bool:
        return "." in filename and filename.rsplit(".", 1)[1].lower() in ALLOWED_EXTENSIONS

    def get_photo_filepath(row) -> str:
        fp = (row["filepath"] or "").strip() if "filepath" in row.keys() else ""
        fn = (row["filename"] or "").strip() if "filename" in row.keys() else ""
        if fp:
            return fp
        if fn:
            return f"general/{fn}"
        return ""

    # ========== ROUTES ==========
    @app.route("/")
    def home():
        return redirect(url_for("dashboard")) if current_user.is_authenticated else redirect(url_for("login"))

    # ---- AUTH ----
    @app.route("/login", methods=["GET", "POST"])
    def login():
        if request.method == "POST":
            username = request.form.get("username", "").strip()
            password = request.form.get("password", "")

            conn = get_db()
            row = conn.execute("SELECT * FROM users WHERE username = ?", (username,)).fetchone()
            conn.close()

            if not row or not check_password_hash(row["password_hash"], password):
                flash("Usuario o contraseña incorrectos.", "error")
                return redirect(url_for("login"))

            login_user(User(row))
            return redirect(url_for("dashboard"))

        return render_template("login.html")

    @app.route("/logout")
    @login_required
    def logout():
        logout_user()
        return redirect(url_for("login"))

    @app.route("/account/password", methods=["GET", "POST"])
    @login_required
    def change_password():
        if request.method == "POST":
            current_pw = request.form.get("current_password", "")
            new_pw = request.form.get("new_password", "")
            new_pw2 = request.form.get("new_password2", "")

            if new_pw != new_pw2:
                flash("Las contraseñas nuevas no coinciden.", "error")
                return redirect(url_for("change_password"))

            conn = get_db()
            row = conn.execute("SELECT * FROM users WHERE id = ?", (int(current_user.id),)).fetchone()
            if not row or not check_password_hash(row["password_hash"], current_pw):
                conn.close()
                flash("Tu contraseña actual es incorrecta.", "error")
                return redirect(url_for("change_password"))

            conn.execute(
                "UPDATE users SET password_hash = ? WHERE id = ?",
                (generate_password_hash(new_pw), int(current_user.id)),
            )
            conn.commit()
            conn.close()

            flash("Contraseña actualizada.", "success")
            return redirect(url_for("dashboard"))

        return render_template("change_password.html")

    # ---- ADMIN (Usuarios + Proyectos + Backup) ----
    @app.route("/admin")
    @admin_required
    def admin_panel():
        conn = get_db()
        users = conn.execute("SELECT id, username, is_admin FROM users ORDER BY id DESC").fetchall()
        projects = conn.execute(
            "SELECT id, name, slug, created_at, description, status FROM projects ORDER BY id DESC"
        ).fetchall()
        conn.close()
        return render_template("admin.html", users=users, projects=projects)

    @app.route("/admin/users/create", methods=["POST"])
    @admin_required
    def admin_create_user():
        username = request.form.get("username", "").strip()
        password = request.form.get("password", "")
        is_admin = 1 if request.form.get("is_admin") == "on" else 0

        if not username or not password:
            flash("Usuario y contraseña son obligatorios.", "error")
            return redirect(url_for("admin_panel"))

        conn = get_db()
        try:
            conn.execute(
                "INSERT INTO users (username, password_hash, is_admin) VALUES (?, ?, ?)",
                (username, generate_password_hash(password), is_admin),
            )
            conn.commit()
            flash("Usuario creado.", "success")
        except sqlite3.IntegrityError:
            flash("Ese usuario ya existe.", "error")
        finally:
            conn.close()

        return redirect(url_for("admin_panel"))

    @app.route("/admin/users/<int:user_id>/delete", methods=["POST"])
    @admin_required
    def admin_delete_user(user_id):
        if int(current_user.id) == int(user_id):
            flash("No puedes eliminar tu propio usuario.", "error")
            return redirect(url_for("admin_panel"))

        conn = get_db()
        target = conn.execute(
            "SELECT id, username, is_admin FROM users WHERE id = ?",
            (user_id,)
        ).fetchone()

        if not target:
            conn.close()
            flash("Usuario no encontrado.", "error")
            return redirect(url_for("admin_panel"))

        if int(target["is_admin"]) == 1:
            admins_count = conn.execute(
                "SELECT COUNT(*) AS c FROM users WHERE is_admin = 1"
            ).fetchone()["c"]
            if admins_count <= 1:
                conn.close()
                flash("No puedes eliminar el último administrador.", "error")
                return redirect(url_for("admin_panel"))

        conn.execute("UPDATE photos SET uploaded_by = NULL WHERE uploaded_by = ?", (user_id,))
        conn.execute("DELETE FROM users WHERE id = ?", (user_id,))
        conn.commit()
        conn.close()

        flash(f"Usuario '{target['username']}' eliminado.", "success")
        return redirect(url_for("admin_panel"))

    @app.route("/admin/projects/create", methods=["POST"])
    @admin_required
    def admin_create_project():
        name = request.form.get("project_name", "").strip()
        description = request.form.get("project_description", "").strip()
        status = request.form.get("project_status", "pendiente").strip()

        if not name:
            flash("El nombre del proyecto es obligatorio.", "error")
            return redirect(url_for("admin_panel"))

        slug = slugify(name)

        conn = get_db()
        try:
            base = slug
            i = 2
            while conn.execute("SELECT 1 FROM projects WHERE slug = ?", (slug,)).fetchone():
                slug = f"{base}_{i}"
                i += 1

            conn.execute(
                "INSERT INTO projects (name, slug, description, status) VALUES (?, ?, ?, ?)",
                (name, slug, description, status),
            )
            conn.commit()

            (UPLOADS_ROOT / slug).mkdir(parents=True, exist_ok=True)

            flash("Proyecto creado.", "success")
        except sqlite3.IntegrityError:
            flash("Ese proyecto ya existe.", "error")
        finally:
            conn.close()

        return redirect(url_for("admin_panel"))

    @app.route("/admin/projects/<int:project_id>/edit", methods=["GET", "POST"])
    @admin_required
    def admin_edit_project(project_id):
        conn = get_db()
        project = conn.execute("SELECT * FROM projects WHERE id = ?", (project_id,)).fetchone()
        if not project:
            conn.close()
            flash("Proyecto no encontrado.", "error")
            return redirect(url_for("admin_panel"))

        if request.method == "POST":
            name = request.form.get("name", "").strip()
            description = request.form.get("description", "").strip()
            status = request.form.get("status", "pendiente").strip()

            if not name:
                conn.close()
                flash("El nombre es obligatorio.", "error")
                return redirect(url_for("admin_edit_project", project_id=project_id))

            try:
                conn.execute("""
                    UPDATE projects SET name = ?, description = ?, status = ?
                    WHERE id = ?
                """, (name, description, status, project_id))
                conn.commit()
                flash("Proyecto actualizado.", "success")
            except sqlite3.IntegrityError:
                flash("Ya existe un proyecto con ese nombre.", "error")
            finally:
                conn.close()

            return redirect(url_for("admin_panel"))

        conn.close()
        return render_template("edit_project.html", project=project)

    @app.route("/admin/projects/<int:project_id>/delete", methods=["POST"])
    @admin_required
    def admin_delete_project(project_id):
        conn = get_db()

        proj = conn.execute("SELECT id, slug FROM projects WHERE id = ?", (project_id,)).fetchone()
        if not proj:
            conn.close()
            flash("Proyecto no encontrado.", "error")
            return redirect(url_for("admin_panel"))

        if proj["slug"] == "general":
            conn.close()
            flash("No se puede eliminar el proyecto General.", "error")
            return redirect(url_for("admin_panel"))

        general = conn.execute("SELECT id, slug FROM projects WHERE slug = 'general'").fetchone()
        if not general:
            conn.close()
            flash("No existe el proyecto General.", "error")
            return redirect(url_for("admin_panel"))

        photos = conn.execute(
            "SELECT id, filepath, filename FROM photos WHERE project_id = ?",
            (project_id,)
        ).fetchall()

        (UPLOADS_ROOT / "general").mkdir(parents=True, exist_ok=True)

        for ph in photos:
            old_fp = (ph["filepath"] or "").strip() if ph["filepath"] else ""
            if not old_fp and ph["filename"]:
                old_fp = f"{proj['slug']}/{ph['filename']}"

            old_path = UPLOADS_ROOT / old_fp if old_fp else None
            old_name = os.path.basename(old_fp) if old_fp else (ph["filename"] or f"{int(time.time())}.jpg")

            new_fp = f"general/{old_name}"
            new_path = UPLOADS_ROOT / new_fp

            try:
                if old_path and old_path.exists():
                    if new_path.exists():
                        new_fp = f"general/{int(time.time())}_{old_name}"
                        new_path = UPLOADS_ROOT / new_fp
                    old_path.replace(new_path)
            except Exception:
                pass

            conn.execute(
                "UPDATE photos SET project_id = ?, filepath = ?, filename = ? WHERE id = ?",
                (general["id"], new_fp, os.path.basename(new_fp), int(ph["id"]))
            )

        conn.execute("DELETE FROM projects WHERE id = ?", (project_id,))
        conn.commit()
        conn.close()

        flash("Proyecto eliminado. Las fotos se movieron a General.", "success")
        return redirect(url_for("admin_panel"))

    @app.route("/admin/backup")
    @admin_required
    def download_backup():
        mem = io.BytesIO()
        with zipfile.ZipFile(mem, mode="w", compression=zipfile.ZIP_DEFLATED) as z:
            if DB_PATH.exists():
                z.write(DB_PATH, arcname="backup/app.db")
            if UPLOADS_ROOT.exists():
                for p in UPLOADS_ROOT.rglob("*"):
                    if p.is_file():
                        rel = p.relative_to(UPLOADS_ROOT).as_posix()
                        z.write(p, arcname=f"backup/uploads/{rel}")

        mem.seek(0)
        return send_file(mem, as_attachment=True, download_name="backup_fotos.zip", mimetype="application/zip")

    # ---- DASHBOARD (BÚSQUEDA) ----
    @app.route("/dashboard")
    @login_required
    def dashboard():
        q = request.args.get("q", "").strip()
        project_id = request.args.get("project_id", "").strip()
        date_from = request.args.get("date_from", "").strip()
        date_to = request.args.get("date_to", "").strip()

        conn = get_db()
        projects = conn.execute("SELECT id, name FROM projects ORDER BY name ASC").fetchall()

        where = []
        params = []

        if q:
            where.append("(p.display_name LIKE ? OR p.description LIKE ?)")
            params.extend([f"%{q}%", f"%{q}%"])

        if project_id:
            where.append("p.project_id = ?")
            params.append(project_id)

        if date_from:
            where.append("date(p.uploaded_at) >= date(?)")
            params.append(date_from)

        if date_to:
            where.append("date(p.uploaded_at) <= date(?)")
            params.append(date_to)

        where_sql = ("WHERE " + " AND ".join(where)) if where else ""

        photos = conn.execute(f"""
            SELECT p.*, pr.name AS project_name
            FROM photos p
            LEFT JOIN projects pr ON pr.id = p.project_id
            {where_sql}
            ORDER BY p.id DESC
        """, params).fetchall()

        conn.close()

        return render_template(
            "dashboard.html",
            photos=photos,
            projects=projects,
            q=q,
            project_id=project_id,
            date_from=date_from,
            date_to=date_to,
            user=current_user,
        )

    # ---- UPLOAD ----
    @app.route("/upload", methods=["GET", "POST"])
    @upload_required
    def upload():
        conn = get_db()
        projects = conn.execute("SELECT id, name, slug FROM projects ORDER BY name ASC").fetchall()

        if request.method == "POST":
            file = request.files.get("photo")
            display_name = request.form.get("display_name", "").strip()
            description = request.form.get("description", "").strip()
            project_id = request.form.get("project_id", "").strip()

            if not project_id:
                conn.close()
                flash("Selecciona un proyecto.", "error")
                return redirect(url_for("upload"))

            proj = conn.execute("SELECT id, slug FROM projects WHERE id = ?", (project_id,)).fetchone()
            if not proj:
                conn.close()
                flash("Proyecto inválido.", "error")
                return redirect(url_for("upload"))

            if not file or file.filename == "":
                conn.close()
                flash("Selecciona una imagen.", "error")
                return redirect(url_for("upload"))

            if not display_name:
                conn.close()
                flash("El nombre/código es obligatorio.", "error")
                return redirect(url_for("upload"))

            original_name = file.filename or ""
            ext = Path(original_name).suffix.lower().lstrip(".")

            if ext not in ALLOWED_EXTENSIONS:
                conn.close()
                flash("Formato no permitido. Usa JPG/PNG/WEBP.", "error")
                return redirect(url_for("upload"))

            base = secure_filename(display_name).strip("_").lower()
            if not base:
                base = f"foto_{int(time.time())}"

            final_name = f"{base}_{int(time.time())}.{ext}"

            project_slug = proj["slug"]
            target_dir = UPLOADS_ROOT / project_slug
            target_dir.mkdir(parents=True, exist_ok=True)

            save_path = target_dir / final_name
            file.save(save_path)

            filepath = f"{project_slug}/{final_name}"

            conn.execute("""
                INSERT INTO photos (filepath, filename, display_name, description, uploaded_by, project_id)
                VALUES (?, ?, ?, ?, ?, ?)
            """, (filepath, final_name, display_name, description, int(current_user.id), int(project_id)))

            conn.commit()
            conn.close()

            flash("Imagen subida correctamente.", "success")
            return redirect(url_for("dashboard"))

        conn.close()
        return render_template("upload.html", projects=projects)

    # ---- EDIT PHOTO (renombra archivo si cambia display_name + mueve si cambia proyecto) ----
    @app.route("/photos/<int:photo_id>/edit", methods=["GET", "POST"])
    @admin_required
    def edit_photo(photo_id):
        conn = get_db()

        photo = conn.execute("""
            SELECT p.*, pr.name AS project_name, pr.slug AS project_slug
            FROM photos p
            LEFT JOIN projects pr ON pr.id = p.project_id
            WHERE p.id = ?
        """, (photo_id,)).fetchone()

        if not photo:
            conn.close()
            flash("Foto no encontrada.", "error")
            return redirect(url_for("dashboard"))

        projects = conn.execute("SELECT id, name, slug FROM projects ORDER BY name ASC").fetchall()

        if request.method == "POST":
            new_display_name = request.form.get("display_name", "").strip()
            new_description = request.form.get("description", "").strip()
            new_project_id = request.form.get("project_id", "").strip()

            if not new_display_name:
                conn.close()
                flash("El nombre/código es obligatorio.", "error")
                return redirect(url_for("edit_photo", photo_id=photo_id))

            if not new_project_id:
                conn.close()
                flash("Selecciona un proyecto.", "error")
                return redirect(url_for("edit_photo", photo_id=photo_id))

            new_proj = conn.execute("SELECT id, slug FROM projects WHERE id = ?", (new_project_id,)).fetchone()
            if not new_proj:
                conn.close()
                flash("Proyecto inválido.", "error")
                return redirect(url_for("edit_photo", photo_id=photo_id))

            old_fp = get_photo_filepath(photo)
            old_project_id = str(photo["project_id"] or "")
            old_display_name = (photo["display_name"] or "").strip()

            old_path = UPLOADS_ROOT / old_fp if old_fp else None
            old_filename = os.path.basename(old_fp) if old_fp else (photo["filename"] or "")

            ext = Path(old_filename).suffix.lower().lstrip(".")
            if ext not in ALLOWED_EXTENSIONS:
                ext = "jpg"

            project_changed = (old_project_id != str(new_project_id))
            name_changed = (old_display_name.lower() != new_display_name.lower())

            base = secure_filename(new_display_name).strip("_").lower()
            if not base:
                base = f"foto_{int(time.time())}"

            if name_changed:
                new_filename = f"{base}_{int(time.time())}.{ext}"
            else:
                new_filename = old_filename

            new_fp = f"{new_proj['slug']}/{new_filename}"
            new_path = UPLOADS_ROOT / new_fp

            (UPLOADS_ROOT / new_proj["slug"]).mkdir(parents=True, exist_ok=True)

            if project_changed or name_changed:
                try:
                    if old_path and old_path.exists():
                        if new_path.exists():
                            new_filename = f"{base}_{int(time.time())}.{ext}"
                            new_fp = f"{new_proj['slug']}/{new_filename}"
                            new_path = UPLOADS_ROOT / new_fp
                        old_path.replace(new_path)
                    else:
                        flash("Aviso: no se encontró el archivo físico para mover/renombrar.", "error")
                        new_fp = old_fp
                        new_filename = old_filename
                except Exception as e:
                    conn.close()
                    flash(f"No se pudo mover/renombrar el archivo: {e}", "error")
                    return redirect(url_for("edit_photo", photo_id=photo_id))

            conn.execute("""
                UPDATE photos
                SET display_name = ?, description = ?, project_id = ?, filepath = ?, filename = ?
                WHERE id = ?
            """, (new_display_name, new_description, int(new_project_id), new_fp, new_filename, photo_id))

            conn.commit()
            conn.close()

            flash("Foto actualizada.", "success")
            return redirect(url_for("dashboard"))

        conn.close()
        return render_template("edit_photo.html", photo=photo, projects=projects)

    @app.route("/photos/<int:photo_id>/delete", methods=["POST"])
    @admin_required
    def delete_photo(photo_id):
        conn = get_db()
        photo = conn.execute("SELECT * FROM photos WHERE id = ?", (photo_id,)).fetchone()
        if not photo:
            conn.close()
            flash("Foto no encontrada.", "error")
            return redirect(url_for("dashboard"))

        try:
            fp = get_photo_filepath(photo)
            if fp:
                file_path = UPLOADS_ROOT / fp
                if file_path.exists():
                    file_path.unlink()
        except Exception:
            pass

        conn.execute("DELETE FROM photos WHERE id = ?", (photo_id,))
        conn.commit()
        conn.close()

        flash("Foto eliminada.", "success")
        return redirect(url_for("dashboard"))

    # ---- SERVIR IMÁGENES ----
    @app.route("/uploads/<path:filepath>")
    @login_required
    def uploaded_file(filepath):
        return send_from_directory(UPLOADS_ROOT, filepath)

    return app


app = create_app()

if __name__ == "__main__":
    app.run(debug=True)
