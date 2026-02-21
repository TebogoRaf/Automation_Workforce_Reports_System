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
        email = request.form["email"]
        conn = get_connection()
        cur = conn.cursor()
        cur.execute("SELECT id FROM users WHERE email=%s", (email,))
        user = cur.fetchone()

        if not user:
            cur.close()
            conn.close()
            return render_template("forgot_password.html", error="Email not found")

        # Generate a temporary password
        temp_password = ''.join(random.choices(string.ascii_letters + string.digits, k=8))
        hashed_temp = bcrypt.hashpw(temp_password.encode(), bcrypt.gensalt()).decode()

        # Update user password in database
        cur.execute("UPDATE users SET password=%s WHERE email=%s", (hashed_temp, email))
        conn.commit()
        cur.close()
        conn.close()

        return render_template("forgot_password.html", success=f"Temporary password: {temp_password}")

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

    file = request.files["file"]
    filepath = os.path.join(UPLOAD_FOLDER, file.filename)
    file.save(filepath)

    df = pd.read_excel(filepath)
    df.columns = df.columns.str.strip().str.lower()

    # Calculations
    df["aht"] = df["wait"] + df["duration"]
    dispositions = df.groupby("workstream").size().reset_index(name="count")
    disconnections = df["disconnection"].value_counts().reset_index()
    disconnections.columns = ["disconnection", "count"]
    aht_table = df.groupby("handled by")["aht"].mean().reset_index()
    trends = df.groupby("date").size().reset_index(name="calls")

    # ---------------- Graphs ---------------- #
    # Line chart for call trends
    plt.figure(figsize=(6,4))
    plt.plot(trends["date"], trends["calls"], marker='o')
    plt.title("Call Trends")
    plt.xlabel("Date")
    plt.ylabel("Number of Calls")
    plt.tight_layout()
    buf1 = BytesIO()
    plt.savefig(buf1, format="png")
    buf1.seek(0)
    call_trends_graph = base64.b64encode(buf1.getvalue()).decode('utf-8')
    plt.close()

    # Pie chart for dispositions
    plt.figure(figsize=(6,6))
    plt.pie(dispositions["count"], labels=dispositions["workstream"], autopct='%1.1f%%')
    plt.title("Dispositions")
    buf2 = BytesIO()
    plt.savefig(buf2, format="png")
    buf2.seek(0)
    dispositions_graph = base64.b64encode(buf2.getvalue()).decode('utf-8')
    plt.close()

    # Bar chart for AHT
    plt.figure(figsize=(6,4))
    plt.bar(aht_table["handled by"], aht_table["aht"], color='skyblue')
    plt.title("AHT Tracking")
    plt.xlabel("Agent")
    plt.ylabel("Average Handle Time")
    plt.tight_layout()
    buf3 = BytesIO()
    plt.savefig(buf3, format="png")
    buf3.seek(0)
    aht_graph = base64.b64encode(buf3.getvalue()).decode('utf-8')
    plt.close()

    # Save report in database
    report_excel_path = os.path.join(UPLOAD_FOLDER, f"report_{file.filename}")
    with pd.ExcelWriter(report_excel_path) as writer:
        dispositions.to_excel(writer, sheet_name="Dispositions", index=False)
        disconnections.to_excel(writer, sheet_name="Disconnections", index=False)
        aht_table.to_excel(writer, sheet_name="AHT Tracking", index=False)
        trends.to_excel(writer, sheet_name="Call Trends", index=False)

    report_pdf_path = report_excel_path.replace(".xlsx", ".pdf")
    # Optional: generate PDF if needed

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
        aht_table=aht_table.to_dict(orient="records"),
        trends=trends.to_dict(orient="records"),
        call_trends_graph=call_trends_graph,
        dispositions_graph=dispositions_graph,
        aht_graph=aht_graph
    )

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
