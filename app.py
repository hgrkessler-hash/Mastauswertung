import os
import io
import json
from flask import Flask, request, jsonify, send_from_directory, session
from werkzeug.security import generate_password_hash, check_password_hash
from functools import wraps
import psycopg2
from datetime import datetime
from reportlab.platypus import SimpleDocTemplate, Paragraph, Spacer, Table
from reportlab.lib import colors
from reportlab.lib.styles import getSampleStyleSheet
import pandas as pd
import dropbox  # Für Dropbox-Backup

app = Flask(__name__, static_folder="static")
app.secret_key = os.environ.get("SECRET_KEY", "CHANGE_ME")
DATABASE_URL = os.environ.get("DATABASE_URL")
DROPBOX_TOKEN = os.environ.get("DROPBOX_TOKEN")

def get_conn():
    return psycopg2.connect(DATABASE_URL)

def login_required(role=None):
    def wrapper(f):
        @wraps(f)
        def decorated(*args, **kwargs):
            if "user" not in session:
                return jsonify({"error": "Unauthorized"}), 401
            if role and session.get("role") != role:
                return jsonify({"error": "Forbidden"}), 403
            return f(*args, **kwargs)
        return decorated
    return wrapper

@app.route("/")
def index():
    return send_from_directory("static", "index.html")

# ---------- DB INIT ----------
def init_db():
    conn = get_conn()
    cur = conn.cursor()
    # Separate Executes für bessere Kompatibilität
    cur.execute("""
        CREATE TABLE IF NOT EXISTS users (
            id SERIAL PRIMARY KEY,
            username TEXT UNIQUE NOT NULL,
            password TEXT NOT NULL,
            role TEXT NOT NULL
        )
    """)
    cur.execute("""
        CREATE TABLE IF NOT EXISTS groups (
            id SERIAL PRIMARY KEY,
            name TEXT,
            revenue FLOAT,
            cost FLOAT
        )
    """)
    cur.execute("""
        CREATE TABLE IF NOT EXISTS audit_log (
            id SERIAL PRIMARY KEY,
            username TEXT,
            action TEXT,
            timestamp TEXT
        )
    """)
    conn.commit()
    cur.close()
    conn.close()

# ---------- AUTH ----------
@app.route("/api/login", methods=["POST"])
def login():
    data = request.json
    try:
        username = data["username"]
        password = data["password"]
    except KeyError:
        return jsonify({"error": "Missing credentials"}), 400
    conn = get_conn()
    cur = conn.cursor()
    cur.execute("SELECT password, role FROM users WHERE username=%s", (username,))
    row = cur.fetchone()
    cur.close()
    conn.close()
    if row and check_password_hash(row[0], password):
        session["user"] = username
        session["role"] = row[1]
        return jsonify({"status": "ok"})
    return jsonify({"error": "Invalid"}), 401

# ---------- DATA ----------
@app.route("/api/groups", methods=["GET"])
@login_required()
def get_groups():
    conn = get_conn()
    cur = conn.cursor()
    cur.execute("SELECT id, name, revenue, cost FROM groups")
    rows = cur.fetchall()
    cur.close()
    conn.close()
    return jsonify([{"id": r[0], "name": r[1], "revenue": r[2], "cost": r[3]} for r in rows])

@app.route("/api/groups", methods=["POST"])
@login_required()
def save_groups():
    data = request.json
    conn = get_conn()
    cur = conn.cursor()
    cur.execute("DELETE FROM groups")
    for g in data:
        cur.execute("INSERT INTO groups (name, revenue, cost) VALUES (%s, %s, %s)",
                    (g.get("name"), g.get("revenue", 0), g.get("cost", 0)))
    conn.commit()
    cur.execute("INSERT INTO audit_log (username, action, timestamp) VALUES (%s, %s, %s)",
                (session["user"], "Updated groups", datetime.utcnow().isoformat()))
    conn.commit()
    cur.close()
    conn.close()
    # Auto-Backup zu Dropbox
    upload_to_dropbox()
    return jsonify({"status": "saved"})

# ---------- CSV IMPORT ----------
@app.route("/api/import-csv", methods=["POST"])
@login_required(role="admin")
def import_csv():
    if "file" not in request.files:
        return jsonify({"error": "No file"}), 400
    file = request.files["file"]
    try:
        df = pd.read_csv(file)
        required_cols = ["name", "revenue", "cost"]
        if not all(col in df.columns for col in required_cols):
            return jsonify({"error": "Missing columns"}), 400
    except Exception as e:
        return jsonify({"error": str(e)}), 400
    conn = get_conn()
    cur = conn.cursor()
    cur.execute("DELETE FROM groups")
    for _, row in df.iterrows():
        cur.execute("INSERT INTO groups (name, revenue, cost) VALUES (%s, %s, %s)",
                    (row["name"], row["revenue"], row["cost"]))
    conn.commit()
    cur.close()
    conn.close()
    return jsonify({"status": "imported"})

# ---------- PDF EXPORT ----------
@app.route("/api/export-pdf")
@login_required()
def export_pdf():
    buffer = io.BytesIO()
    doc = SimpleDocTemplate(buffer)
    elements = []
    styles = getSampleStyleSheet()
    conn = get_conn()
    cur = conn.cursor()
    cur.execute("SELECT name, revenue, cost FROM groups")
    rows = cur.fetchall()
    cur.close()
    conn.close()
    data = [["Name", "Umsatz", "Kosten", "DB"]]
    for r in rows:
        data.append([r[0], r[1], r[2], r[1] - r[2] if r[1] and r[2] else 0])
    table = Table(data)
    table.setStyle([("GRID", (0, 0), (-1, -1), 1, colors.black)])
    elements.append(Paragraph("Mast KPI Report", styles["Heading1"]))
    elements.append(Spacer(1, 12))
    elements.append(table)
    doc.build(elements)
    buffer.seek(0)
    return buffer.read(), 200, {
        "Content-Type": "application/pdf",
        "Content-Disposition": "attachment; filename=report.pdf"
    }

# ---------- AUDIT ----------
@app.route("/api/audit")
@login_required(role="admin")
def audit():
    conn = get_conn()
    cur = conn.cursor()
    cur.execute("SELECT username, action, timestamp FROM audit_log ORDER BY id DESC LIMIT 50")
    rows = cur.fetchall()
    cur.close()
    conn.close()
    return jsonify([{"username": r[0], "action": r[1], "timestamp": r[2]} for r in rows])

# ---------- DROPBOX BACKUP ----------
def upload_to_dropbox():
    if not DROPBOX_TOKEN:
        print("No Dropbox token set – skipping backup")
        return
    try:
        dbx = dropbox.Dropbox(DROPBOX_TOKEN)
        conn = get_conn()
        cur = conn.cursor()
        cur.execute("SELECT * FROM groups")
        rows = cur.fetchall()
        data = json.dumps({"groups": [{"id": r[0], "name": r[1], "revenue": r[2], "cost": r[3]} for r in rows]})
        file_name = f"/mast_backup_{datetime.utcnow().isoformat()}.json"
        dbx.files_upload(data.encode('utf-8'), file_name)
        cur.close()
        conn.close()
        print("Backup uploaded to Dropbox")
    except Exception as e:
        print(f"Dropbox backup failed: {str(e)}")

@app.route("/api/manual-backup")
@login_required(role="admin")
def manual_backup():
    upload_to_dropbox()
    return jsonify({"status": "backup done"})

# Initialisierung beim Start
with app.app_context():
    init_db()

if __name__ == "__main__":
    app.run(debug=True)
