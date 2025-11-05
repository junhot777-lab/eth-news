from flask import Flask, render_template_string, request, redirect, url_for, session, jsonify
import sqlite3
import feedparser
import os
import requests
from datetime import datetime

app = Flask(__name__)
app.secret_key = "secure-secret"
DB_PATH = "/tmp/articles.db"
PASSWORD = "Philia12"

OPENAI_API_KEY = os.getenv("OPENAI_API_KEY", "").strip()

# -----------------------------------------
# DB 초기화
# -----------------------------------------
def init_db():
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("""
        CREATE TABLE IF NOT EXISTS articles (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            title TEXT,
            title_ko TEXT,
            link TEXT,
            date TEXT,
            source TEXT,
            summary1 TEXT,
            summary2 TEXT,
            summary3 TEXT
        )
    """)
    conn.commit()
    conn.close()

# -----------------------------------------
# 번역 함수 (제목만)
# -----------------------------------------
def translate_title_ko(text: str) -> str:
    if not OPENAI_API_KEY:
        return text
    try:
        url = "https://api.openai.com/v1/responses"
        headers = {
            "Authorization": f"Bearer {OPENAI_API_KEY}",
            "Content-Type": "application/json",
        }
        body = {
            "model": "gpt-4.1-mini",
            "input": f"Translate the following crypto news headline into natural Korean (1 line):\n{text}",
            "max_output_tokens": 80,
        }
        r = requests.post(url, headers=headers, json=body, timeout=12)
        r.raise_for_status()
        data = r.json()
        return data.get("output_text", "").strip() or text
    except Exception:
        return text

# -----------------------------------------
# 기사 수집
# -----------------------------------------
def fetch_feeds():
    FEEDS = [
        "https://cointelegraph.com/rss",
        "https://decrypt.co/feed",
        "https://www.coindesk.com/arc/outboundfeeds/rss/"
    ]

    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    added = 0

    for url in FEEDS:
        feed = feedparser.parse(url)
        for entry in feed.entries[:20]:
            title = entry.get("title", "").strip()
            link = entry.get("link", "").strip()
            date = entry.get("published", datetime.utcnow().isoformat())
            source = feed.feed.get("title", "unknown")
            title_ko = translate_title_ko(title)
            s1 = s2 = s3 = ""

            c.execute("""
                INSERT OR IGNORE INTO articles(title, title_ko, link, date, source, summary1, summary2, summary3)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """, (title, title_ko, link, date, source, s1, s2, s3))
            added += c.rowcount

    conn.commit()
    conn.close()
    return added

# -----------------------------------------
# 관리자 페이지 (기사 수집 수동)
# -----------------------------------------
@app.route("/admin/fetch")
def admin_fetch():
    pw = request.args.get("pw", "")
    if pw != PASSWORD:
        return jsonify({"error": "locked"})
    added = fetch_feeds()
    return jsonify({"ok": True, "added": added})

# -----------------------------------------
# 기사 API
# -----------------------------------------
@app.route("/api/articles")
def api_articles():
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("SELECT title, title_ko, link, date, source FROM articles ORDER BY id DESC")
    rows = [
        {"title": r[0], "title_ko": r[1], "link": r[2], "date": r[3], "source": r[4]}
        for r in c.fetchall()
    ]
    conn.close()
    return jsonify({"articles": rows})

# -----------------------------------------
# 메인 페이지
# -----------------------------------------
@app.route("/", methods=["GET", "POST"])
def index():
    if "logged_in" not in session:
        if request.method == "POST":
            if request.form.get("password") == PASSWORD:
                session["logged_in"] = True
                return redirect(url_for("index"))
        return render_template_string(LOGIN_PAGE)

    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("SELECT title, title_ko, link, date, source FROM articles ORDER BY id DESC")
    rows = c.fetchall()
    conn.close()
    return render_template_string(HTML_PAGE, rows=rows)

@app.route("/logout")
def logout():
    session.pop("logged_in", None)
    return redirect(url_for("index"))

# -----------------------------------------
# HTML 템플릿
# -----------------------------------------
LOGIN_PAGE = """
<!DOCTYPE html>
<html>
<head>
<meta charset="utf-8">
<title>이더리움 뉴스 로그인</title>
<style>
body {
  background-color: #0d0d0d;
  color: white;
  font-family: sans-serif;
  text-align: center;
  padding-top: 150px;
}
input {
  padding: 10px;
  border-radius: 8px;
  border: none;
}
button {
  padding: 10px 20px;
  border: none;
  border-radius: 8px;
  background: #1e90ff;
  color: white;
}
</style>
</head>
<body>
<h1>이더리움 뉴스 접속</h1>
<form method="post">
  <input type="password" name="password" placeholder="비밀번호 입력">
  <button type="submit">접속</button>
</form>
</body>
</html>
"""

HTML_PAGE = """
<!DOCTYPE html>
<html>
<head>
<meta charset="utf-8">
<title>이더리움 실시간 뉴스 집계</title>
<style>
body {
  background-image: url('https://upload.wikimedia.org/wikipedia/commons/6/6f/Ethereum-icon-purple.svg');
  background-repeat: repeat;
  background-size: 200px;
  background-color: #0d0d0d;
  color: white;
  font-family: sans-serif;
  padding: 20px;
}
.container {
  max-width: 850px;
  margin: auto;
}
.article {
  background: rgba(30,30,30,0.9);
  margin: 15px 0;
  padding: 15px;
  border-radius: 10px;
}
a {
  color: #66ccff;
  text-decoration: none;
  font-size: 1.2em;
}
.source {
  font-size: 0.9em;
  color: #ccc;
}
.title-ko {
  color: #ffd700;
  margin-top: 5px;
  font-size: 0.95em;
}
.logout {
  position: absolute;
  right: 20px;
  top: 20px;
}
</style>
</head>
<body>
<a href="{{ url_for('logout') }}" class="logout">로그아웃</a>
<div class="container">
<h1>이더리움 실시간 뉴스 집계</h1>
<p>최신순 · 자동 집계(매 fetch마다) · 제목 한국어 번역 표시</p>
{% for r in rows %}
  <div class="article">
    <a href="{{ r[2] }}" target="_blank">{{ r[0] }}</a><br>
    {% if r[1] and r[1] != r[0] %}
      <div class="title-ko">• {{ r[1] }}</div>
    {% endif %}
    <div class="source">{{ r[4] }} | {{ r[3] }}</div>
  </div>
{% else %}
  <p>표시할 뉴스가 없습니다.</p>
{% endfor %}
</div>
</body>
</html>
"""

# -----------------------------------------
# 시작
# -----------------------------------------
if __name__ == "__main__":
    init_db()
    fetch_feeds()
    app.run(host="0.0.0.0", port=10000)
