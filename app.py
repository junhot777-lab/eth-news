from flask import Flask, render_template_string, request, jsonify, redirect, make_response
import feedparser
import sqlite3
from datetime import datetime

app = Flask(__name__)

DB_PATH = "articles.db"
ADMIN_PW = "Philia12"
COOKIE = "admin"

HTML = """
<!DOCTYPE html>
<html lang="ko">
<head>
<meta charset="UTF-8">
<title>이더리움 실시간 뉴스 집계</title>
<style>
body {
  background: url('https://ethereum.org/static/28214bb49c1db20696a8b02b76f8df38/31987/hero.webp') no-repeat center center fixed;
  background-size: cover;
  font-family: Pretendard, sans-serif;
  color: #fff;
  text-align: center;
  margin: 0;
  padding: 0;
}
.topbar {
  padding: 20px;
  background: rgba(0,0,0,0.6);
  display: flex;
  justify-content: center;
  align-items: center;
  position: sticky;
  top: 0;
  z-index: 10;
}
#search {
  width: 60%;
  padding: 10px;
  border-radius: 8px;
  border: none;
  outline: none;
}
.logout {
  margin-left: 10px;
  padding: 8px 12px;
  background: #333;
  border: 1px solid #aaa;
  color: #fff;
  border-radius: 8px;
  cursor: pointer;
}
.card {
  background: rgba(0,0,0,0.6);
  margin: 12px auto;
  padding: 14px;
  width: 80%;
  border-radius: 12px;
  text-align: left;
}
.card a { color: #66b3ff; text-decoration: none; font-size: 18px; font-weight: bold; }
.card small { color: #ccc; }
</style>
</head>
<body>
<div class="topbar">
  <input id="search" placeholder="제목/매체로 검색 (Enter)">
  <button id="logoutBtn" class="logout">로그아웃</button>
</div>
<h2>이더리움 실시간 뉴스 집계</h2>
<p>최신순 • 자동 집계(매일) • 제목 한국어 번역 제외</p>
<div id="news"></div>

<script>
async function fetchArticles() {
  const res = await fetch("/api/articles");
  const data = await res.json();
  const box = document.getElementById("news");
  box.innerHTML = "";
  for (const a of data.articles) {
    const div = document.createElement("div");
    div.className = "card";
    div.innerHTML = `
      <a href="${a.link}" target="_blank">${a.title}</a><br>
      <small>${a.source} | ${a.date}</small>
      <p>${a.summary}</p>
    `;
    box.appendChild(div);
  }
}

document.getElementById("search").addEventListener("keypress", e => {
  if (e.key === "Enter") {
    const q = e.target.value.toLowerCase();
    document.querySelectorAll(".card").forEach(c => {
      c.style.display = c.textContent.toLowerCase().includes(q) ? "" : "none";
    });
  }
});

document.getElementById("logoutBtn").addEventListener("click", async () => {
  await fetch("/admin/logout", {method: "POST"});
  location.replace("/");
});

fetchArticles();
setInterval(fetchArticles, 600000);
</script>
</body>
</html>
"""

def init_db():
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute("""CREATE TABLE IF NOT EXISTS articles (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        title TEXT, link TEXT UNIQUE, date TEXT, summary TEXT, source TEXT
    )""")
    conn.commit()
    conn.close()

@app.route("/")
def home():
    return render_template_string(HTML)

@app.route("/api/articles")
def api_articles():
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute("SELECT title, link, date, summary, source FROM articles ORDER BY id DESC")
    rows = cur.fetchall()
    conn.close()
    return jsonify({"articles": [
        {"title": r[0], "link": r[1], "date": r[2], "summary": r[3], "source": r[4]} for r in rows
    ]})

@app.route("/admin/fetch")
def fetch_feeds():
    pw = request.args.get("pw", "")
    if pw != ADMIN_PW:
        return jsonify({"error": "locked"}), 403
    FEEDS = [
        "https://decrypt.co/feed",
        "https://cointelegraph.com/rss",
        "https://www.coindesk.com/arc/outboundfeeds/rss/"
    ]
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    added = 0
    for url in FEEDS:
        feed = feedparser.parse(url)
        for e in feed.entries[:15]:
            title = e.get("title", "").strip()
            link = e.get("link", "")
            date = e.get("published", datetime.utcnow().isoformat())
            summary = e.get("summary", "")[:250]
            source = feed.feed.get("title", "unknown")
            try:
                cur.execute("INSERT INTO articles (title, link, date, summary, source) VALUES (?, ?, ?, ?, ?)",
                            (title, link, date, summary, source))
                added += 1
            except sqlite3.IntegrityError:
                pass
    conn.commit()
    conn.close()
    return jsonify({"ok": True, "added": added})

@app.post("/admin/logout")
def logout():
    resp = make_response(redirect("/"))
    resp.delete_cookie(COOKIE)
    return resp

if __name__ == "__main__":
    init_db()
    app.run(host="0.0.0.0", port=10000)
