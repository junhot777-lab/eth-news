import os
import sqlite3
import threading
import time
import feedparser
from flask import Flask, jsonify, render_template_string, request, session
from apscheduler.schedulers.background import BackgroundScheduler

app = Flask(__name__)
app.secret_key = "Philia12-key"

ADMIN_PASSWORD = "Philia12"
DB_DIR = "/tmp/data"
DB_PATH = os.path.join(DB_DIR, "articles.db")

FEEDS = [
    "https://cointelegraph.com/rss",
    "https://decrypt.co/feed",
    "https://www.coindesk.com/arc/outboundfeeds/rss/",
    "https://www.coindeskkorea.com/rss/allArticle.xml",  # KR 소스 추가
]

# ---------------- DB ----------------
def ensure_dirs():
    os.makedirs(DB_DIR, exist_ok=True)

def init_db():
    ensure_dirs()
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("""
        CREATE TABLE IF NOT EXISTS articles (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            title TEXT,
            link TEXT UNIQUE,
            source TEXT,
            date TEXT
        )
    """)
    conn.commit()
    conn.close()

def insert_article(title, link, source, date):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute(
        "INSERT OR IGNORE INTO articles (title, link, source, date) VALUES (?, ?, ?, ?)",
        (title, link, source, date),
    )
    conn.commit()
    conn.close()

def query_articles(q=None):
    init_db()
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    if q:
        c.execute(
            "SELECT title, link, source, date FROM articles WHERE title LIKE ? ORDER BY id DESC",
            (f"%{q}%",),
        )
    else:
        c.execute("SELECT title, link, source, date FROM articles ORDER BY id DESC")
    rows = c.fetchall()
    conn.close()
    return rows

# -------------- Fetch ---------------
KEYWORDS = ("이더리움", "ethereum", " eth ", "[eth]", "(eth)")
def match_keyword(title: str) -> bool:
    t = f" {title.lower()} "
    return any(k in t for k in [k if k.islower() else k.lower() for k in KEYWORDS])

def fetch_articles():
    init_db()
    seen = set()
    try:
        conn = sqlite3.connect(DB_PATH)
        c = conn.cursor()
        c.execute("SELECT link FROM articles")
        seen = {row[0] for row in c.fetchall()}
        conn.close()
    except Exception:
        pass

    added = 0
    for feed_url in FEEDS:
        feed = feedparser.parse(feed_url)
        src = feed.feed.get("title", feed_url)
        for e in feed.entries[:20]:
            link = e.get("link")
            title = e.get("title", "")
            if not link or not title:
                continue
            if match_keyword(title) and link not in seen:
                insert_article(title, link, src, e.get("published", ""))
                added += 1
    print(f"[FETCH] cycle done. added={added} at {time.strftime('%Y-%m-%d %H:%M:%S')}")

# -------------- Routes --------------
@app.before_request
def ensure_db_before_request():
    try:
        init_db()
    except Exception as e:
        print("[DB INIT ERROR]", e)

@app.get("/")
def home():
    return render_template_string(HTML)

@app.post("/unlock")
def unlock():
    j = request.get_json(silent=True) or {}
    if j.get("password") == ADMIN_PASSWORD:
        session["unlocked"] = True
        return jsonify({"success": True})
    return jsonify({"success": False, "error": "비밀번호가 올바르지 않습니다."}), 401

@app.get("/api/unlocked")
def api_unlocked():
    return jsonify({"unlocked": bool(session.get("unlocked"))})

@app.get("/api/articles")
def api_articles():
    if not session.get("unlocked"):
        return jsonify({"error": "locked"}), 401
    q = request.args.get("q", "").strip() or None
    rows = query_articles(q)
    def to_local(s):
        try:
            from dateutil import parser as dtparser
            return dtparser.parse(s).astimezone().strftime("%Y-%m-%d %H:%M:%S")
        except Exception:
            return s
    return jsonify({
        "articles": [
            {"title": r[0], "link": r[1], "source": r[2] or "", "published_at_local": to_local(r[3])}
            for r in rows
        ]
    })

# -------------- Boot ---------------
print("[BOOT] ensuring DB...")
try:
    init_db()
    print("[BOOT] DB ready")
except Exception as e:
    print("[BOOT ERROR]", e)

scheduler = BackgroundScheduler()
scheduler.add_job(fetch_articles, "interval", minutes=30)
scheduler.start()
threading.Thread(target=fetch_articles, daemon=True).start()

# -------------- HTML ---------------
HTML = """
<!DOCTYPE html>
<html lang="ko">
<head>
<meta charset="UTF-8">
<title>이더리움 뉴스 집계기</title>
<style>
body { font-family: Arial; background: #111; color: #eee; text-align: center; }
input, button { padding: 8px; margin: 5px; border-radius: 5px; border: none; }
.card { background: #222; margin: 10px auto; padding: 10px; border-radius: 10px; width: 90%; max-width: 720px; }
a { color: #00bcd4; text-decoration: none; }
.small { color: #aaa; font-size: 12px; }
#msg { color:#ffb74d; }
</style>
</head>
<body>
<h2>이더리움 뉴스 집계기</h2>

<div id="lockScreen" style="display:none;">
  <p>관리자 비밀번호 입력:</p>
  <input type="password" id="pw"><button onclick="unlock()">잠금 해제</button>
  <p id="msg"></p>
</div>

<div id="content" style="display:none;">
  <input type="text" id="search" placeholder="검색어 입력" oninput="load()">
  <div id="hint" class="small"></div>
  <div id="articles"></div>
</div>

<script>
async function checkUnlocked() {
  try {
    const r = await fetch('/api/unlocked');
    const j = await r.json();
    if (j.unlocked) {
      document.getElementById('lockScreen').style.display='none';
      document.getElementById('content').style.display='block';
      load();
    } else {
      document.getElementById('lockScreen').style.display='block';
      document.getElementById('content').style.display='none';
    }
  } catch(e){
    document.getElementById('msg').innerText='서버 연결 실패';
    document.getElementById('lockScreen').style.display='block';
  }
}

async function unlock(){
  let pw=document.getElementById('pw').value;
  let r=await fetch('/unlock',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({password:pw})});
  let j=await r.json();
  if(j.success){
    document.getElementById('lockScreen').style.display='none';
    document.getElementById('content').style.display='block';
    load();
  }else{
    document.getElementById('msg').innerText=j.error||'잠금 해제 실패';
  }
}

async function load(){
  let q=document.getElementById('search').value;
  const hint=document.getElementById('hint');
  hint.innerText='';
  try{
    let r=await fetch('/api/articles?q='+encodeURIComponent(q));
    if(r.status===401){
      document.getElementById('lockScreen').style.display='block';
      document.getElementById('content').style.display='none';
      document.getElementById('msg').innerText='비밀번호로 잠금 해제해 주세요.';
      return;
    }
    let j=await r.json();
    let box=document.getElementById('articles'); box.innerHTML='';
    if(!j.articles || j.articles.length===0){
      hint.innerText='표시할 기사가 없습니다. 잠시 후 자동 수집되거나 검색어를 바꿔보세요.';
      return;
    }
    j.articles.forEach(a=>{
      let d=document.createElement('div');d.className='card';
      d.innerHTML=`<a href="${a.link}" target="_blank">${a.title}</a><br><div class="small">${a.source} | ${a.published_at_local||''}</div>`;
      box.appendChild(d);
    });
  }catch(e){
    hint.innerText='불러오기에 실패했습니다.';
  }
}

window.addEventListener('DOMContentLoaded', checkUnlocked);
</script>
</body>
</html>
"""

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=8000)
