import sqlite3
import re
import json
from collections import Counter
from datetime import datetime, timedelta
from difflib import SequenceMatcher

from flask import Flask, render_template, request, redirect, url_for, flash

app = Flask(__name__)
app.secret_key = "daa_project_secret_key"
DB_NAME = "reviews.db"


# -----------------------------
# DATABASE FUNCTIONS
# -----------------------------
def get_db_connection():
    conn = sqlite3.connect(DB_NAME)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    conn = get_db_connection()
    cur = conn.cursor()

    cur.execute("""
        CREATE TABLE IF NOT EXISTS feedbacks (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id TEXT NOT NULL,
            product_id TEXT NOT NULL,
            original_text TEXT NOT NULL,
            cleaned_text TEXT NOT NULL,
            score INTEGER NOT NULL,
            classification TEXT NOT NULL,
            signal_strength TEXT NOT NULL,
            duplicate_flag INTEGER DEFAULT 0,
            similarity_flag INTEGER DEFAULT 0,
            repetition_flag INTEGER DEFAULT 0,
            behavior_flag INTEGER DEFAULT 0,
            similarity_value REAL DEFAULT 0,
            repetition_ratio REAL DEFAULT 0,
            issues_json TEXT,
            created_at TEXT NOT NULL
        )
    """)

    conn.commit()
    conn.close()


# -----------------------------
# TEXT PROCESSING
# -----------------------------
def preprocess_text(text):
    text = text.lower()
    text = re.sub(r"[^a-z0-9\s]", " ", text)
    text = re.sub(r"\s+", " ", text).strip()
    return text


def extract_word_frequency(cleaned_text):
    words = cleaned_text.split()
    freq = Counter(words)
    return words, freq


def jaccard_similarity(text1, text2):
    set1 = set(text1.split())
    set2 = set(text2.split())

    if not set1 and not set2:
        return 1.0
    if not set1 or not set2:
        return 0.0

    return len(set1 & set2) / len(set1 | set2)


def combined_similarity(text1, text2):
    seq_score = SequenceMatcher(None, text1, text2).ratio()
    jac_score = jaccard_similarity(text1, text2)
    return (seq_score * 0.6) + (jac_score * 0.4)


# -----------------------------
# ANALYSIS LOGIC
# -----------------------------
def analyze_feedback(user_id, product_id, feedback_text):
    cleaned_text = preprocess_text(feedback_text)
    words, freq = extract_word_frequency(cleaned_text)

    conn = get_db_connection()
    cur = conn.cursor()

    score = 100
    issues = []

    duplicate_flag = 0
    similarity_flag = 0
    repetition_flag = 0
    behavior_flag = 0

    similarity_value = 0.0
    repetition_ratio = 0.0

    # 1) Duplicate Detection
    cur.execute("""
        SELECT * FROM feedbacks
        WHERE cleaned_text = ?
    """, (cleaned_text,))
    duplicate_rows = cur.fetchall()

    if duplicate_rows:
        duplicate_flag = 1
        score -= 40
        issues.append("Duplicate feedback detected")

    # 2) Similarity Check
    cur.execute("""
        SELECT cleaned_text FROM feedbacks
        WHERE product_id = ?
    """, (product_id,))
    old_reviews = cur.fetchall()

    max_similarity = 0.0
    for row in old_reviews:
        old_text = row["cleaned_text"]
        sim = combined_similarity(cleaned_text, old_text)
        if sim > max_similarity:
            max_similarity = sim

    similarity_value = round(max_similarity, 2)

    if max_similarity >= 0.80:
        similarity_flag = 1
        score -= 25
        issues.append(f"High similarity detected ({similarity_value})")
    elif max_similarity >= 0.65:
        similarity_flag = 1
        score -= 12
        issues.append(f"Moderate similarity detected ({similarity_value})")

    # 3) Repetition Check
    if len(words) > 0:
        most_common_word_count = freq.most_common(1)[0][1]
        repetition_ratio = round(most_common_word_count / len(words), 2)

        if len(words) >= 5 and repetition_ratio >= 0.35:
            repetition_flag = 1
            score -= 15
            issues.append(f"High word repetition found ({repetition_ratio})")
        elif len(words) >= 5 and repetition_ratio >= 0.25:
            repetition_flag = 1
            score -= 8
            issues.append(f"Moderate word repetition found ({repetition_ratio})")

    # 4) User Behavior Check
    now = datetime.now()
    one_day_ago = (now - timedelta(days=1)).strftime("%Y-%m-%d %H:%M:%S")

    # total reviews by same user
    cur.execute("""
        SELECT COUNT(*) as cnt FROM feedbacks
        WHERE user_id = ?
    """, (user_id,))
    total_by_user = cur.fetchone()["cnt"]

    # same user reviewing same product multiple times
    cur.execute("""
        SELECT COUNT(*) as cnt FROM feedbacks
        WHERE user_id = ? AND product_id = ?
    """, (user_id, product_id))
    same_product_count = cur.fetchone()["cnt"]

    # many reviews in last 24 hrs by same user
    cur.execute("""
        SELECT COUNT(*) as cnt FROM feedbacks
        WHERE user_id = ? AND created_at >= ?
    """, (user_id, one_day_ago))
    recent_count = cur.fetchone()["cnt"]

    if same_product_count >= 1:
        behavior_flag = 1
        score -= 15
        issues.append("User already reviewed the same product")
    elif recent_count >= 5:
        behavior_flag = 1
        score -= 12
        issues.append("User posted too many reviews in short time")
    elif total_by_user >= 10:
        behavior_flag = 1
        score -= 8
        issues.append("High review activity from user")

    # score limits
    score = max(0, min(100, score))

    # signal strength
    issue_count = duplicate_flag + similarity_flag + repetition_flag + behavior_flag
    if issue_count >= 3 or score < 40:
        signal_strength = "High"
    elif issue_count == 2 or score < 75:
        signal_strength = "Medium"
    elif issue_count == 1:
        signal_strength = "Low"
    else:
        signal_strength = "None"

    # classification
    if score >= 75:
        classification = "Genuine"
    elif 40 <= score <= 74:
        classification = "Suspicious"
    else:
        classification = "Fake"

    conn.close()

    return {
        "user_id": user_id,
        "product_id": product_id,
        "original_text": feedback_text,
        "cleaned_text": cleaned_text,
        "score": score,
        "classification": classification,
        "signal_strength": signal_strength,
        "duplicate_flag": duplicate_flag,
        "similarity_flag": similarity_flag,
        "repetition_flag": repetition_flag,
        "behavior_flag": behavior_flag,
        "similarity_value": similarity_value,
        "repetition_ratio": repetition_ratio,
        "issues": issues
    }


def save_feedback(result):
    conn = get_db_connection()
    cur = conn.cursor()

    cur.execute("""
        INSERT INTO feedbacks (
            user_id, product_id, original_text, cleaned_text,
            score, classification, signal_strength,
            duplicate_flag, similarity_flag, repetition_flag, behavior_flag,
            similarity_value, repetition_ratio, issues_json, created_at
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    """, (
        result["user_id"],
        result["product_id"],
        result["original_text"],
        result["cleaned_text"],
        result["score"],
        result["classification"],
        result["signal_strength"],
        result["duplicate_flag"],
        result["similarity_flag"],
        result["repetition_flag"],
        result["behavior_flag"],
        result["similarity_value"],
        result["repetition_ratio"],
        json.dumps(result["issues"]),
        datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    ))

    conn.commit()
    conn.close()


# -----------------------------
# ROUTES
# -----------------------------
@app.route("/")
def index():
    conn = get_db_connection()
    cur = conn.cursor()

    cur.execute("SELECT COUNT(*) as total FROM feedbacks")
    total_reviews = cur.fetchone()["total"]

    cur.execute("SELECT COUNT(*) as cnt FROM feedbacks WHERE classification = 'Genuine'")
    genuine_count = cur.fetchone()["cnt"]

    cur.execute("SELECT COUNT(*) as cnt FROM feedbacks WHERE classification = 'Suspicious'")
    suspicious_count = cur.fetchone()["cnt"]

    cur.execute("SELECT COUNT(*) as cnt FROM feedbacks WHERE classification = 'Fake'")
    fake_count = cur.fetchone()["cnt"]

    cur.execute("SELECT ROUND(AVG(score), 2) as avg_score FROM feedbacks")
    avg_score_row = cur.fetchone()
    avg_score = avg_score_row["avg_score"] if avg_score_row["avg_score"] is not None else 0

    conn.close()

    return render_template(
        "index.html",
        total_reviews=total_reviews,
        genuine_count=genuine_count,
        suspicious_count=suspicious_count,
        fake_count=fake_count,
        avg_score=avg_score
    )


@app.route("/submit", methods=["POST"])
def submit_feedback():
    user_id = request.form.get("user_id", "").strip()
    product_id = request.form.get("product_id", "").strip()
    feedback_text = request.form.get("feedback_text", "").strip()

    if not user_id or not product_id or not feedback_text:
        flash("All fields are required.", "danger")
        return redirect(url_for("index"))

    result = analyze_feedback(user_id, product_id, feedback_text)
    save_feedback(result)

    flash("Feedback analyzed and stored successfully.", "success")
    return redirect(url_for("view_feedbacks"))


@app.route("/feedbacks")
def view_feedbacks():
    conn = get_db_connection()
    cur = conn.cursor()

    cur.execute("""
        SELECT * FROM feedbacks
        ORDER BY score DESC, created_at DESC
    """)
    feedbacks = cur.fetchall()

    processed_feedbacks = []
    for fb in feedbacks:
        item = dict(fb)
        item["issues"] = json.loads(item["issues_json"]) if item["issues_json"] else []
        processed_feedbacks.append(item)

    conn.close()
    return render_template("feedbacks.html", feedbacks=processed_feedbacks)


@app.route("/products")
def product_analysis():
    conn = get_db_connection()
    cur = conn.cursor()

    cur.execute("""
        SELECT
            product_id,
            COUNT(*) as total_feedbacks,
            SUM(CASE WHEN classification = 'Genuine' THEN 1 ELSE 0 END) as genuine_feedbacks,
            SUM(CASE WHEN classification = 'Fake' THEN 1 ELSE 0 END) as fake_feedbacks,
            SUM(CASE WHEN classification = 'Suspicious' THEN 1 ELSE 0 END) as suspicious_feedbacks,
            ROUND(AVG(score), 2) as average_score
        FROM feedbacks
        GROUP BY product_id
        ORDER BY average_score DESC
    """)
    products = cur.fetchall()

    product_data = []
    for p in products:
        total = p["total_feedbacks"]
        genuine = p["genuine_feedbacks"]
        authenticity_percentage = round((genuine / total) * 100, 2) if total > 0 else 0

        product_data.append({
            "product_id": p["product_id"],
            "total_feedbacks": total,
            "genuine_feedbacks": genuine,
            "fake_feedbacks": p["fake_feedbacks"],
            "suspicious_feedbacks": p["suspicious_feedbacks"],
            "average_score": p["average_score"],
            "authenticity_percentage": authenticity_percentage
        })

    conn.close()
    return render_template("product_analysis.html", products=product_data)


@app.route("/delete/<int:feedback_id>", methods=["POST"])
def delete_feedback(feedback_id):
    conn = get_db_connection()
    cur = conn.cursor()
    cur.execute("DELETE FROM feedbacks WHERE id = ?", (feedback_id,))
    conn.commit()
    conn.close()

    flash("Feedback deleted successfully.", "warning")
    return redirect(url_for("view_feedbacks"))


@app.route("/reset", methods=["POST"])
def reset_database():
    conn = get_db_connection()
    cur = conn.cursor()
    cur.execute("DELETE FROM feedbacks")
    conn.commit()
    conn.close()

    flash("All feedback records deleted.", "warning")
    return redirect(url_for("index"))


if __name__ == "__main__":
    init_db()
    app.run(debug=True)
