import os
import pandas as pd
import bcrypt
from flask import Flask, render_template, request, redirect, session, send_file, url_for
from database import get_connection, create_tables
from dotenv import load_dotenv
import matplotlib
matplotlib.use('Agg')  # Use non-GUI backend for server
import matplotlib.pyplot as plt
from io import BytesIO
import base64
from datetime import datetime
import random
import string


app = Flask(__name__)
load_dotenv()

# Secret key
app.secret_key = os.getenv("SECRET_KEY") or "supersecretkey123"

# Upload folder
UPLOAD_FOLDER = "uploads"
os.makedirs(UPLOAD_FOLDER, exist_ok=True)

# Initialize database tables
create_tables()

# ---------------- LOGIN ---------------- #
@app.route("/")
def home():
    return render_template("login.html")

@app.route("/login", methods=["POST"])
def login():
    email = request.form["email"]
    password = request.form["password"]
    role = request.form["role"]

    conn = get_connection()
    cur = conn.cursor()
    cur.execute("SELECT id,name,password,role FROM users WHERE email=%s", (email,))
    user = cur.fetchone()
    cur.close()
    conn.close()

    if user and bcrypt.checkpw(password.encode(), user[2].encode()) and user[3] == role:
        session["user_id"] = user[0]
        session["role"] = user[3]
        session["name"] = user[1]

        if role == "analyst":
            return redirect("/analyst")
        else:
            return redirect("/supervisor")

    return render_template("login.html", error="Invalid credentials")

@app.route("/logout")
def logout():
    session.clear()
    return redirect("/")

# ---------------- FORGOT PASSWORD ---------------- #
@app.route("/forgot_password", methods=["GET", "POST"])
def forgot_password():
    if request.method == "POST":
        # Step 1: User enters email
        if "new_password" not in request.form:
            email = request.form["email"]

            conn = get_connection()
            cur = conn.cursor()
            cur.execute("SELECT id FROM users WHERE email=%s", (email,))
            user = cur.fetchone()
            cur.close()
            conn.close()

            if not user:
                return render_template("forgot_password.html", error="Email not found")

            # Generate temporary password
            temp_password = ''.join(random.choices(string.ascii_letters + string.digits, k=8))

            return render_template("forgot_password.html",
                                   step="set_new",
                                   email=email,
                                   temp_password=temp_password)

        # Step 2: User sets new password
        else:
            email = request.form["email"]
            temp_password = request.form["temp_password"]
            new_password = request.form["new_password"]
            confirm_password = request.form["confirm_password"]

            if new_password != confirm_password:
                return render_template("forgot_password.html",
                                       step="set_new",
                                       email=email,
                                       temp_password=temp_password,
                                       error="Passwords do not match")

            hashed_pw = bcrypt.hashpw(new_password.encode(), bcrypt.gensalt()).decode()

            conn = get_connection()
            cur = conn.cursor()
            cur.execute("UPDATE users SET password=%s WHERE email=%s", (hashed_pw, email))
            conn.commit()
            cur.close()
            conn.close()

            return render_template("forgot_password.html", success="Password updated successfully")

    return render_template("forgot_password.html")

# ---------------- ANALYST DASHBOARD ---------------- #
@app.route("/analyst")
def analyst_dashboard():
    if session.get("role") != "analyst":
        return redirect("/")
    return render_template("analyst_dashboard.html")

@app.route("/upload", methods=["POST"])
def upload():
    if session.get("role") != "analyst":
        return redirect("/")

    file = request.files.get("file")
    if not file:
        return render_template("analyst_dashboard.html", error="No file uploaded")

    filepath = os.path.join(UPLOAD_FOLDER, file.filename)
    file.save(filepath)

    try:
        df = pd.read_excel(filepath)
        df.columns = df.columns.str.strip().str.lower()

        # --- Column checks ---
        required_cols = ["wait", "duration", "workstream", "disconnection", "handled by", "date"]
        missing = [col for col in required_cols if col not in df.columns]
        if missing:
            return render_template("analyst_dashboard.html", error=f"Missing columns: {', '.join(missing)}")

        # --- Calculations ---
        df["aht"] = df["wait"] + df["duration"]

        aht_table = df.groupby("handled by")["aht"].mean().reset_index()
        # Convert Timedelta to minutes if needed
        if pd.api.types.is_timedelta64_dtype(aht_table["aht"]):
            aht_table["aht"] = aht_table["aht"].dt.total_seconds() / 60

        dispositions = df.groupby("workstream").size().reset_index(name="count")
        disconnections = df["disconnection"].value_counts().reset_index()
        disconnections.columns = ["disconnection", "count"]

        df["date"] = pd.to_datetime(df["date"], errors="coerce")
        trends = df.groupby("date").size().reset_index(name="calls")

        total_calls = int(trends["calls"].sum())
        total_answered = int(df["answered"].sum()) if "answered" in df.columns else 0
        total_dropped = int(df["dropped"].sum()) if "dropped" in df.columns else 0

        agent_names = aht_table["handled by"].tolist()
        agent_totals = aht_table["aht"].round(2).tolist()

        # --- Save report ---
        report_excel_path = os.path.join(UPLOAD_FOLDER, f"report_{file.filename}")
        with pd.ExcelWriter(report_excel_path) as writer:
            dispositions.to_excel(writer, sheet_name="Dispositions", index=False)
            disconnections.to_excel(writer, sheet_name="Disconnections", index=False)
            aht_table.to_excel(writer, sheet_name="AHT Tracking", index=False)
            trends.to_excel(writer, sheet_name="Call Trends", index=False)

        report_pdf_path = report_excel_path.replace(".xlsx", ".pdf")

        conn = get_connection()
        cur = conn.cursor()
        cur.execute("""
            INSERT INTO reports (uploaded_by, original_filename, generated_excel, generated_pdf)
            VALUES (%s,%s,%s,%s)
        """, (session["user_id"], file.filename, report_excel_path, report_pdf_path))
        conn.commit()
        cur.close()
        conn.close()

        return render_template(
            "analyst_dashboard.html",
            dispositions=dispositions.to_dict(orient="records"),
            disconnections=disconnections.to_dict(orient="records"),
            aht=aht_table.to_dict(orient="records"),
            trends=trends.to_dict(orient="records"),
            total_calls=total_calls,
            agent_names=agent_names,
            agent_totals=agent_totals,
            total_answered=total_answered,
            total_dropped=total_dropped
        )

    except Exception as e:
        # Show error in dashboard instead of 500
        return render_template("analyst_dashboard.html", error=f"Upload failed: {str(e)}")

# ---------------- SUPERVISOR DASHBOARD ---------------- #
@app.route("/supervisor")
def supervisor_dashboard():
    if session.get("role") != "supervisor":
        return redirect("/")

    conn = get_connection()
    cur = conn.cursor()
    cur.execute("""
        SELECT r.id,u.name,r.created_at,r.generated_excel
        FROM reports r
        JOIN users u ON r.uploaded_by=u.id
        ORDER BY r.created_at DESC
    """)
    reports = cur.fetchall()
    cur.close()
    conn.close()

    return render_template("supervisor_dashboard.html", reports=reports)

@app.route("/create_analyst", methods=["POST"])
def create_analyst():
    if session.get("role") != "supervisor":
        return redirect("/")

    name = request.form["name"]
    email = request.form["email"]
    password = bcrypt.hashpw(request.form["password"].encode(), bcrypt.gensalt()).decode()

    conn = get_connection()
    cur = conn.cursor()
    cur.execute("""
        INSERT INTO users (name,email,password,role)
        VALUES (%s,%s,%s,'analyst')
    """, (name,email,password))
    conn.commit()
    cur.close()
    conn.close()

    return redirect("/supervisor")

if __name__ == "__main__":
    app.run(debug=True)
