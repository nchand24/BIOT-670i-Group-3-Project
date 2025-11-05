# flask_app.py — BIOT 670i Dandelion Project
# Full Flask web app with authentication, uploads, EXIF metadata extraction, and random banner display.

import os
import sys
import uuid
import json
import sqlite3
import random
import hashlib
import datetime
import mimetypes
from flask import (
    Flask, request, render_template, render_template_string,
    redirect, url_for, send_from_directory, flash, session, g
)
from werkzeug.security import generate_password_hash, check_password_hash

# Try both EXIF libraries for safety
try:
    import exifread
except ImportError:
    exifread = None

try:
    from PIL import Image, ExifTags
except ImportError:
    Image = None
    ExifTags = None

# --------------------------------------------------------------------------------------
# EXIF extraction
# --------------------------------------------------------------------------------------

def extract_exif(file_path):
    """Try extracting EXIF metadata with Pillow, fallback to exifread if needed."""
    metadata = {}
    try:
        if Image:
            with Image.open(file_path) as img:
                info = img.getexif()
                for tag, value in info.items():
                    tag_name = ExifTags.TAGS.get(tag, tag)
                    metadata[tag_name] = str(value)
                if metadata:
                    return metadata
    except Exception as e:
        print("Pillow EXIF error:", e)

    # fallback to exifread
    if exifread:
        try:
            with open(file_path, "rb") as f:
                tags = exifread.process_file(f, details=False)
            for tag, val in tags.items():
                clean = str(tag).replace("EXIF ", "").replace("Image ", "")
                metadata[clean] = str(val)
        except Exception as e:
            print("Exifread error:", e)
    return metadata


# --------------------------------------------------------------------------------------
# Config
# --------------------------------------------------------------------------------------

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.path.join(BASE_DIR, "warehouse.db")

app = Flask(__name__)
app.config["SECRET_KEY"] = os.environ.get("SECRET_KEY", "dev-dandelion-key")
app.config["UPLOAD_ROOT"] = os.path.join(BASE_DIR, "uploads")
os.makedirs(app.config["UPLOAD_ROOT"], exist_ok=True)

ALLOW_GLOBAL_DOWNLOADS = True

# --------------------------------------------------------------------------------------
# Template helpers
# --------------------------------------------------------------------------------------

def _template_exists(name: str) -> bool:
    return os.path.exists(os.path.join(BASE_DIR, "templates", name))

def render(name: str, **ctx):
    if _template_exists(name):
        return render_template(name, **ctx)
    return render_template_string(f"<pre>Missing template: {name}</pre>")

# --------------------------------------------------------------------------------------
# Database setup
# --------------------------------------------------------------------------------------

def init_db():
    conn = sqlite3.connect(DB_PATH)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS users (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            email TEXT UNIQUE,
            full_name TEXT,
            password_hash TEXT,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS uploads (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER,
            title TEXT,
            notes TEXT,
            original_name TEXT,
            stored_name TEXT,
            mime_type TEXT,
            size_bytes INTEGER,
            md5 TEXT,
            exif_json TEXT,
            created_at TEXT,
            FOREIGN KEY (user_id) REFERENCES users(id)
        )
    """)
    conn.commit()
    conn.close()


# --------------------------------------------------------------------------------------
# Load current user
# --------------------------------------------------------------------------------------

@app.before_request
def load_logged_in_user():
    user_id = session.get("user_id")
    if user_id is None:
        g.user = None
    else:
        conn = sqlite3.connect(DB_PATH)
        conn.row_factory = sqlite3.Row
        g.user = conn.execute("SELECT * FROM users WHERE id=?", (user_id,)).fetchone()
        conn.close()


# --------------------------------------------------------------------------------------
# Routes
# --------------------------------------------------------------------------------------

@app.get("/")
def home():
    if g.user:
        return redirect(url_for("files"))
    return redirect(url_for("login"))


# ---------------- Registration ----------------

@app.get("/register")
def register():
    return render("register.html")

@app.post("/register")
def register_post():
    email = (request.form.get("email") or "").strip().lower()
    full_name = (request.form.get("full_name") or "").strip()
    password = request.form.get("password") or ""

    if not email or not password:
        flash("Email and password required.")
        return redirect(url_for("register"))

    pw_hash = generate_password_hash(password)
    conn = sqlite3.connect(DB_PATH)
    try:
        conn.execute(
            "INSERT INTO users (email, full_name, password_hash) VALUES (?, ?, ?)",
            (email, full_name, pw_hash)
        )
        conn.commit()
    except sqlite3.IntegrityError:
        flash("That email is already registered.")
        return redirect(url_for("register"))
    finally:
        conn.close()

    flash("Account created successfully.")
    return redirect(url_for("login"))


# ---------------- Login ----------------

@app.get("/login")
def login():
    return render("login.html")

@app.post("/login")
def login_post():
    email = (request.form.get("email") or "").strip().lower()
    password = request.form.get("password") or ""

    conn = sqlite3.connect(DB_PATH)
    row = conn.execute("SELECT id, password_hash FROM users WHERE email=?", (email,)).fetchone()
    conn.close()

    if not row or not check_password_hash(row[1], password):
        flash("Invalid email or password.")
        return redirect(url_for("login"))

    session["user_id"] = row[0]
    return redirect(url_for("files"))


@app.get("/logout")
def logout():
    session.clear()
    flash("Logged out.")
    return redirect(url_for("login"))


# ---------------- Upload ----------------

@app.get("/upload")
def upload():
    if not g.user:
        return redirect(url_for("login"))
    return render("upload.html")


@app.post("/upload")
def do_upload():
    if not g.user:
        return redirect(url_for("login"))

    f = request.files.get("file")
    title = (request.form.get("title") or "").strip()
    notes = (request.form.get("notes") or "").strip()

    if not f or not f.filename:
        flash("Please select a file.")
        return redirect(url_for("upload"))

    original = f.filename
    ext = os.path.splitext(original)[1].lower()
    stored = f"{uuid.uuid4()}{ext}"

    user_id = g.user["id"]
    user_dir = os.path.join(app.config["UPLOAD_ROOT"], str(user_id))
    os.makedirs(user_dir, exist_ok=True)
    full_path = os.path.join(user_dir, stored)
    f.save(full_path)

    # EXIF extraction
    exif_data = extract_exif(full_path)
    exif_json = json.dumps(exif_data, ensure_ascii=False) if exif_data else "{}"

    size_bytes = os.path.getsize(full_path)
    mime_type = mimetypes.guess_type(full_path)[0] or "application/octet-stream"

    with open(full_path, "rb") as fh:
        md5sum = hashlib.md5(fh.read()).hexdigest()

    created_at = datetime.datetime.now().isoformat(timespec="seconds")

    conn = sqlite3.connect(DB_PATH)
    conn.execute("""
        INSERT INTO uploads (user_id, title, notes, original_name, stored_name, mime_type,
                             size_bytes, md5, exif_json, created_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    """, (user_id, title, notes, original, stored, mime_type, size_bytes, md5sum, exif_json, created_at))
    conn.commit()
    conn.close()

    flash("File uploaded successfully.")
    return redirect(url_for("files"))


# ---------------- File listing ----------------

@app.get("/files")
def files():
    if not g.user:
        return redirect(url_for("login"))

    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    rows = conn.execute("""
        SELECT id, title, original_name, stored_name, mime_type, size_bytes, created_at, exif_json
        FROM uploads
        WHERE user_id = ?
        ORDER BY id DESC
    """, (g.user["id"],)).fetchall()
    conn.close()

    # Random dandelion banner
    dandelions = [
        "https://loremflickr.com/1200/400/dandelion",
        "https://source.unsplash.com/random/1200x400/?dandelion",
        "https://loremflickr.com/1200/400/flower,dandelion",
        "https://picsum.photos/1200/400?blur=2&random=12"
    ]
    banner_url = random.choice(dandelions)

    return render("files.html", files=rows, banner=banner_url)


# ---------------- File download ----------------

@app.get("/download/<int:file_id>")
def download(file_id):
    if not g.user:
        return redirect(url_for("login"))

    conn = sqlite3.connect(DB_PATH)
    row = conn.execute("SELECT stored_name, original_name, user_id FROM uploads WHERE id=?", (file_id,)).fetchone()
    conn.close()

    if not row:
        flash("File not found.")
        return redirect(url_for("files"))

    stored_name, original_name, owner_id = row
    if not ALLOW_GLOBAL_DOWNLOADS and owner_id != g.user["id"]:
        flash("Unauthorized download attempt.")
        return redirect(url_for("files"))

    owner_dir = os.path.join(app.config["UPLOAD_ROOT"], str(owner_id))
    return send_from_directory(owner_dir, stored_name, as_attachment=True, download_name=original_name)


# --------------------------------------------------------------------------------------
# Init
# --------------------------------------------------------------------------------------

init_db()
application = app
