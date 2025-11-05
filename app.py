import os
import sqlite3
import requests
from flask import Flask, jsonify, render_template_string, request
from bs4 import BeautifulSoup
from datetime import datetime
from openai import OpenAI

app = Flask(__name__)

# DB 설정
DB_PATH = "/tmp/articles.db"
os.makedirs("/tmp", exist_ok=True)

# OpenAI 클라이언트
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
client = OpenAI(api_key=OPENAI_API_KEY) if OPENAI_API_KEY else None

# DB 초기화
def init_db():
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute('''CREATE TABLE IF NOT EXISTS articles
                 (id INTEGER PRIMARY KEY AUTOINCREMENT,
                  title TEXT,
                  link TEXT,
                  summary TEXT,
                  date TEXT,
                  source TEXT)''')
    conn.commit()
    conn.close()

init_db()

# 기사 가져오기
def fetch_articles():
    keywords = ["이더리움", "Ethereum"]
    urls = [
        "https://decrypt.co/",
        "https://cointelegraph.com/",
        "https://coindesk.com/"
    ]
    added = 0
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()

    for site in urls:
        try:
            html = requests.get(site, timeout=10, headers={"User-Agent": "Mozilla/5.0"}).text
            soup = BeautifulSoup(html, "html.parser")
            for link in soup.find_all("a", href=True):
                text = link.get_text(strip=True)
                if any(k in text for k in keywords):
                    url = link["href"]
                    if not url.startswith("http"):
                        url = site.rstrip("/") + "/" + url.lstrip("/")
                    c.execute("SELECT id FROM articles WHERE link=?", (url,))
                    if not c.fetchone():
                        summary = make_summary(text)
                        c.execute("INSERT INTO articles (title, link, summary, date, source) VALUES (?,?,?,?,?)",
                                  (text, url, summary, datetime.now().strftime("%Y-%m-%d %H:%M:%S"), site))
                        added += 1
        except Exception as e:
            print("Fetch error:", e)

    conn.commit()
    conn.close()
    return added

# OpenAI 요약 생성
def make_summary(text):
    if not client:
        return "(요약 생성 비활성화: API 키 없음)"
    try:
        prompt = f"다음 영어 문장을 한국어로 3줄로 간결하게 요약해줘:\n\n{text}"
        response = client.chat.completions.create(
            model="gpt-4o-mini",
            messages=[{"role": "user", "content": prompt}],
            max_tokens=100
        )
        return response.choices[0].message.content.strip()
    except Exception as e:
        return f"(요약 실패: {e})"

# 기본 페이지
@app.route("/")
def index():
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("SELECT title, link, summary, date, source FROM articles ORDER BY id DESC")
    articles = c.fetchall()
    conn.close()

    return render_template_string("""
    <html><head>
    <meta charset="utf-8">
    <title>이더리움 실시간 뉴스 집계</title>
    <style>
        body { background-color: #0a0a0a; color: #ddd; font-family: sans-serif; text-align: center; }
        .card { background:#111; margin:10px auto; padding:10px; width:90%; border-radius:10px; }
        a { color:#52b6ff; text-decoration:none; }
    </style>
    </head><body>
    <h2>이더리움 실시간 뉴스 집계</h2>
    <p>최신순 • 3줄 요약 자동 첨부</p>
    {% for t,l,s,d,src in articles %}
      <div class="card">
        <a href="{{l}}" target="_blank"><b>{{t}}</b></a><br>
        <small>{{src}} | {{d}}</small><br>
        <p>{{s}}</p>
      </div>
    {% endfor %}
    </body></html>
    """, articles=articles)

# 수동 업데이트
@app.route("/admin/fetch")
def admin_fetch():
    pw = request.args.get("pw")
    if pw != "Philia12":
        return jsonify({"error": "locked"})
    added = fetch_articles()
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("SELECT COUNT(*) FROM articles")
    total = c.fetchone()[0]
    conn.close()
    return jsonify({"ok": True, "added": added, "total": total})

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5000)))
