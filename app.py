import os
import pandas as pd
import bcrypt
from flask import Flask, render_template, request, redirect, session, send_file, url_for
from database import get_connection, create_tables
from reportlab.platypus import SimpleDocTemplate, Paragraph, Spacer, Table
from reportlab.lib import colors
from reportlab.lib.styles import getSampleStyleSheet
import matplotlib.pyplot as plt
from dotenv import load_dotenv

app = Flask(__name__)

# Load environment variables
load_dotenv()
app.secret_key = os.getenv("SECRET_KEY") or "supersecretkey123"

UPLOAD_FOLDER = "uploads"
os.makedirs(UPLOAD_FOLDER, exist_ok=True)

# Ensure tables exist
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


# ---------------- ANALYST DASHBOARD ---------------- #
@app.route("/analyst")
def analyst_dashboard():
    if session.get("role") != "analyst":
        return redirect("/")

    # Show empty initially
    return render_template("analyst_dashboard.html", tables=False)


@app.route("/upload", methods=["POST"])
def upload():
    if session.get("role") != "analyst":
        return redirect("/")

    file = request.files["file"]
    filepath = os.path.join(UPLOAD_FOLDER, file.filename)
    file.save(filepath)

    # Read Excel
    df = pd.read_excel(filepath)
    df.columns = df.columns.str.strip().str.lower()

    # AHT calculation
    df["aht"] = df["wait"] + df["duration"]

    # Dispositions
    dispositions = df.groupby("workstream").size().reset_index(name="count")

    # Disconnections
    disconnections = df["disconnection"].value_counts().reset_index()
    disconnections.columns = ["disconnection", "count"]

    # AHT Tracking
    aht_table = df.groupby("handled by")["aht"].mean().reset_index()

    # Call Trends
    trends = df.groupby("date").size().reset_index(name="calls")

    # ------------------ Generate Graphs ------------------ #
    # AHT Line Chart
    plt.figure(figsize=(6,4))
    plt.plot(aht_table['handled by'], aht_table['aht'], marker='o', color='blue')
    plt.title('Average Handling Time (AHT) by Agent')
    plt.xlabel('Agent')
    plt.ylabel('AHT')
    plt.grid(True)
    plt.tight_layout()
    aht_chart_path = os.path.join(UPLOAD_FOLDER, f"aht_{file.filename}.png")
    plt.savefig(aht_chart_path)
    plt.close()

    # Dispositions Pie Chart
    plt.figure(figsize=(6,6))
    plt.pie(dispositions['count'], labels=dispositions['workstream'], autopct='%1.1f%%', startangle=140)
    plt.title('Dispositions Distribution')
    dispositions_chart_path = os.path.join(UPLOAD_FOLDER, f"disp_{file.filename}.png")
    plt.savefig(dispositions_chart_path)
    plt.close()

    # Call Trends Line Chart
    plt.figure(figsize=(6,4))
    plt.plot(trends['date'], trends['calls'], marker='o', color='green')
    plt.title('Call Trends Over Days')
    plt.xlabel('Date')
    plt.ylabel('Number of Calls')
    plt.grid(True)
    plt.tight_layout()
    trends_chart_path = os.path.join(UPLOAD_FOLDER, f"trends_{file.filename}.png")
    plt.savefig(trends_chart_path)
    plt.close()

    # ------------------ Save Excel & PDF ------------------ #
    report_excel_path = os.path.join(UPLOAD_FOLDER, f"report_{file.filename}")
    with pd.ExcelWriter(report_excel_path) as writer:
        dispositions.to_excel(writer, sheet_name="Dispositions", index=False)
        disconnections.to_excel(writer, sheet_name="Disconnections", index=False)
        aht_table.to_excel(writer, sheet_name="AHT Tracking", index=False)
        trends.to_excel(writer, sheet_name="Call Trends", index=False)

    report_pdf_path = report_excel_path.replace(".xlsx", ".pdf")
    doc = SimpleDocTemplate(report_pdf_path)
    elements = []
    styles = getSampleStyleSheet()
    elements.append(Paragraph("Automation Workforce Report", styles["Title"]))
    elements.append(Spacer(1,12))
    for table_data in [dispositions.values.tolist(), disconnections.values.tolist(),
                       aht_table.values.tolist(), trends.values.tolist()]:
        t = Table(table_data)
        t.setStyle([('BACKGROUND',(0,0),(-1,0),colors.grey), ('GRID',(0,0),(-1,-1),1,colors.black)])
        elements.append(t)
        elements.append(Spacer(1,20))
    doc.build(elements)

    # ------------------ Save Report Record ------------------ #
    conn = get_connection()
    cur = conn.cursor()
    cur.execute("""
        INSERT INTO reports (uploaded_by, original_filename, generated_excel, generated_pdf)
        VALUES (%s,%s,%s,%s)
    """, (session["user_id"], file.filename, report_excel_path, report_pdf_path))
    conn.commit()
    cur.close()
    conn.close()

    # ------------------ Render Dashboard with tables + charts ------------------ #
    return render_template(
        "analyst_dashboard.html",
        tables=True,
        dispositions_table=dispositions.to_html(index=False, classes="table table-striped"),
        disconnections_table=disconnections.to_html(index=False, classes="table table-striped"),
        aht_table=aht_table.to_html(index=False, classes="table table-striped"),
        trends_table=trends.to_html(index=False, classes="table table-striped"),
        aht_chart=url_for('static', filename=os.path.basename(aht_chart_path)),
        dispositions_chart=url_for('static', filename=os.path.basename(dispositions_chart_path)),
        trends_chart=url_for('static', filename=os.path.basename(trends_chart_path))
    )


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
