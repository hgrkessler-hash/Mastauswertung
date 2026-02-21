
import os, io, csv
from flask import Flask, request, jsonify, send_from_directory, session
from werkzeug.security import generate_password_hash, check_password_hash
from functools import wraps
import psycopg2
from datetime import datetime
from reportlab.platypus import SimpleDocTemplate, Paragraph, Spacer, Table
from reportlab.lib import colors
from reportlab.lib.styles import getSampleStyleSheet
import pandas as pd

app = Flask(__name__, static_folder="static")
app.secret_key = os.environ.get("SECRET_KEY", "CHANGE_ME")

DATABASE_URL = os.environ.get("DATABASE_URL")

def get_conn():
    return psycopg2.connect(DATABASE_URL)

def login_required(role=None):
    def wrapper(f):
        def decorated(*args, **kwargs):
            if "user" not in session:
                return jsonify({"error":"Unauthorized"}),401
            if role and session.get("role") != role:
                return jsonify({"error":"Forbidden"}),403
            return f(*args, **kwargs)
        decorated.__name__ = f.__name__
        return decorated
    return wrapper

@app.route("/")
def index():
    return send_from_directory("static","index.html")

# ---------- DB INIT ----------
@app.before_first_request
def init_db():
    conn = get_conn()
    cur = conn.cursor()
    cur.execute("""
        CREATE TABLE IF NOT EXISTS users(
            id SERIAL PRIMARY KEY,
            username TEXT UNIQUE,
            password TEXT,
            role TEXT
        );
        CREATE TABLE IF NOT EXISTS groups(
            id SERIAL PRIMARY KEY,
            name TEXT,
            revenue FLOAT,
            cost FLOAT
        );
        CREATE TABLE IF NOT EXISTS audit_log(
            id SERIAL PRIMARY KEY,
            username TEXT,
            action TEXT,
            timestamp TEXT
        );
    """)
    conn.commit()
    cur.close()
    conn.close()

# ---------- AUTH ----------
@app.route("/api/login", methods=["POST"])
def login():
    data = request.json
    conn = get_conn()
    cur = conn.cursor()
    cur.execute("SELECT password, role FROM users WHERE username=%s",(data["username"],))
    row = cur.fetchone()
    cur.close()
    conn.close()
    if row and check_password_hash(row[0], data["password"]):
        session["user"] = data["username"]
        session["role"] = row[1]
        return jsonify({"status":"ok"})
    return jsonify({"error":"Invalid"}),401

# ---------- DATA ----------
@app.route("/api/groups", methods=["GET"])
@login_required()
def get_groups():
    conn = get_conn()
    cur = conn.cursor()
    cur.execute("SELECT id,name,revenue,cost FROM groups")
    rows = cur.fetchall()
    cur.close()
    conn.close()
    return jsonify([{"id":r[0],"name":r[1],"revenue":r[2],"cost":r[3]} for r in rows])

@app.route("/api/groups", methods=["POST"])
@login_required()
def save_groups():
    data = request.json
    conn = get_conn()
    cur = conn.cursor()
    cur.execute("DELETE FROM groups")
    for g in data:
        cur.execute("INSERT INTO groups(name,revenue,cost) VALUES(%s,%s,%s)",
                    (g["name"],g["revenue"],g["cost"]))
    conn.commit()
    cur.execute("INSERT INTO audit_log(username,action,timestamp) VALUES(%s,%s,%s)",
                (session["user"],"Updated groups",datetime.utcnow().isoformat()))
    conn.commit()
    cur.close()
    conn.close()
    return jsonify({"status":"saved"})

# ---------- CSV IMPORT ----------
@app.route("/api/import-csv", methods=["POST"])
@login_required(role="admin")
def import_csv():
    file = request.files["file"]
    df = pd.read_csv(file)
    conn = get_conn()
    cur = conn.cursor()
    cur.execute("DELETE FROM groups")
    for _, row in df.iterrows():
        cur.execute("INSERT INTO groups(name,revenue,cost) VALUES(%s,%s,%s)",
                    (row["name"],row["revenue"],row["cost"]))
    conn.commit()
    cur.close()
    conn.close()
    return jsonify({"status":"imported"})

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
    cur.execute("SELECT name,revenue,cost FROM groups")
    rows = cur.fetchall()
    cur.close()
    conn.close()

    data = [["Name","Umsatz","Kosten","DB"]]
    for r in rows:
        data.append([r[0], r[1], r[2], r[1]-r[2]])

    table = Table(data)
    table.setStyle([("GRID",(0,0),(-1,-1),1,colors.black)])
    elements.append(Paragraph("Mast KPI Report", styles["Heading1"]))
    elements.append(Spacer(1,12))
    elements.append(table)
    doc.build(elements)

    buffer.seek(0)
    return (buffer.read(),200,{
        "Content-Type":"application/pdf",
        "Content-Disposition":"attachment;filename=report.pdf"
    })

# ---------- AUDIT ----------
@app.route("/api/audit")
@login_required(role="admin")
def audit():
    conn = get_conn()
    cur = conn.cursor()
    cur.execute("SELECT username,action,timestamp FROM audit_log ORDER BY id DESC LIMIT 50")
    rows = cur.fetchall()
    cur.close()
    conn.close()
    return jsonify(rows)

if __name__ == "__main__":
    app.run()
