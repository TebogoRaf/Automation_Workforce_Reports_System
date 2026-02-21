import os
import pandas as pd
import bcrypt
from flask import Flask, render_template, request, redirect, session, send_file
from database import get_connection, create_tables
from reportlab.platypus import SimpleDocTemplate, Paragraph, Spacer, Table
from reportlab.lib import colors
from reportlab.lib.styles import getSampleStyleSheet
from dotenv import load_dotenv
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import io
import base64

# ---------------- FLASK SETUP ---------------- #
app = Flask(__name__)
load_dotenv()
app.secret_key = os.getenv("SECRET_KEY", "supersecretkey123")

UPLOAD_FOLDER = "uploads"
os.makedirs(UPLOAD_FOLDER, exist_ok=True)

create_tables()

# ---------------- LOGIN / LOGOUT ---------------- #
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
    cur.execute("SELECT id, name, password, role FROM users WHERE email=%s", (email,))
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
    df["aht"] = df["wait"] + df["duration"]

    # ----------------- TABLES ---------------- #
    dispositions = df.groupby("workstream").size().reset_index(name="count")
    disconnections = df["disconnection"].value_counts().reset_index()
    disconnections.columns = ["disconnection", "count"]
    aht_table = df.groupby("handled by")["aht"].mean().reset_index()
    trends = df.groupby("date").size().reset_index(name="calls")

    # total calls
    total_calls = trends["calls"].sum()

    # ----------------- SAVE EXCEL ---------------- #
    report_excel_path = os.path.join(UPLOAD_FOLDER, f"report_{file.filename}")
    with pd.ExcelWriter(report_excel_path) as writer:
        dispositions.to_excel(writer, sheet_name="Dispositions", index=False)
        disconnections.to_excel(writer, sheet_name="Disconnections", index=False)
        aht_table.to_excel(writer, sheet_name="AHT Tracking", index=False)
        trends.to_excel(writer, sheet_name="Call Trends", index=False)

    # ----------------- SAVE PDF ---------------- #
    report_pdf_path = report_excel_path.replace(".xlsx", ".pdf")
    doc = SimpleDocTemplate(report_pdf_path)
    elements = []
    styles = getSampleStyleSheet()
    elements.append(Paragraph("Automation Workforce Report", styles["Title"]))
    elements.append(Spacer(1, 12))

    for table_data in [dispositions.values.tolist(),
                       disconnections.values.tolist(),
                       aht_table.values.tolist(),
                       trends.values.tolist()]:
        t = Table(table_data)
        t.setStyle([
            ('BACKGROUND',(0,0),(-1,0),colors.grey),
            ('GRID',(0,0),(-1,-1),1,colors.black)
        ])
        elements.append(t)
        elements.append(Spacer(1, 20))
    doc.build(elements)

    # ----------------- SAVE TO DATABASE ---------------- #
    conn = get_connection()
    cur = conn.cursor()
    cur.execute("""
        INSERT INTO reports (uploaded_by, original_filename, generated_excel, generated_pdf)
        VALUES (%s,%s,%s,%s)
    """, (session["user_id"], file.filename, report_excel_path, report_pdf_path))
    conn.commit()
    cur.close()
    conn.close()

    return render_template("analyst_dashboard.html",
                           dispositions=dispositions.to_dict(orient='records'),
                           disconnections=disconnections.to_dict(orient='records'),
                           aht=aht_table.to_dict(orient='records'),
                           trends=trends.to_dict(orient='records'),
                           total_calls=total_calls)

# ---------------- SUPERVISOR DASHBOARD ---------------- #
@app.route("/supervisor")
def supervisor_dashboard():
    if session.get("role") != "supervisor":
        return redirect("/")

    conn = get_connection()
    cur = conn.cursor()
    cur.execute("""
        SELECT r.id,u.name,r.created_at,r.generated_excel,r.generated_pdf
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

# ---------------- VIEW / DOWNLOAD REPORT ---------------- #
@app.route("/report/<int:report_id>")
def view_report(report_id):
    if session.get("role") != "supervisor":
        return redirect("/")

    conn = get_connection()
    cur = conn.cursor()
    cur.execute("SELECT generated_excel, generated_pdf FROM reports WHERE id=%s", (report_id,))
    report = cur.fetchone()
    cur.close()
    conn.close()

    if not report:
        return "Report not found", 404

    # ----------------- READ EXCEL ---------------- #
    excel_path = report[0]
    xls = pd.ExcelFile(excel_path)
    tables = {}
    charts = {}

    for sheet_name in xls.sheet_names:
        df = pd.read_excel(xls, sheet_name=sheet_name)
        tables[sheet_name] = df.to_dict(orient='records')

        # Create charts
        fig, ax = plt.subplots(figsize=(6,4))
        if sheet_name.lower() in ["dispositions", "disconnections"]:
            ax.pie(df[df.columns[1]], labels=df[df.columns[0]], autopct='%1.1f%%', startangle=90)
            ax.set_title(sheet_name)
        else:
            ax.plot(df[df.columns[0]], df[df.columns[1]], marker='o')
            ax.set_title(sheet_name)
        buf = io.BytesIO()
        plt.savefig(buf, format='png')
        plt.close(fig)
        buf.seek(0)
        charts[sheet_name] = base64.b64encode(buf.read()).decode('utf-8')

    return render_template("report_preview.html", tables=tables, charts=charts, report_id=report_id)

@app.route("/report/download/<int:report_id>")
def download_report(report_id):
    if session.get("role") != "supervisor":
        return redirect("/")

    conn = get_connection()
    cur = conn.cursor()
    cur.execute("SELECT generated_excel FROM reports WHERE id=%s", (report_id,))
    report = cur.fetchone()
    cur.close()
    conn.close()

    if not report:
        return "Report not found", 404

    return send_file(report[0], as_attachment=True)

# ---------------- MAIN ---------------- #
if __name__ == "__main__":
    app.run(debug=True)