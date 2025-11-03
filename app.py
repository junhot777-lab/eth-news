import os, sqlite3, time, feedparser
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
    "https://www.coindeskkorea.com/rss/allArticle.xml",
]
REQUEST_HEADERS = {"User-Agent": "Mozilla/5.0 (Render-eth-news)"}

# ---------- DB ----------
def ensure_db():
    os.makedirs(DB_DIR, exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("""
    CREATE TABLE IF NOT EXISTS articles(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        title TEXT, link TEXT UNIQUE, source TEXT, date TEXT
    )""")
    conn.commit(); conn.close()

def insert_article(title, link, source, date):
    conn = sqlite3.connect(DB_PATH); c = conn.cursor()
    c.execute("INSERT OR IGNORE INTO articles(title,link,source,date) VALUES (?,?,?,?)",
              (title, link, source, date))
    conn.commit(); conn.close()

def query(q=None):
    conn = sqlite3.connect(DB_PATH); c = conn.cursor()
    if q:
        c.execute("SELECT title,link,source,date FROM articles WHERE title LIKE ? ORDER BY id DESC",
                  (f"%{q}%",))
    else:
        c.execute("SELECT title,link,source,date FROM articles ORDER BY id DESC")
    rows = c.fetchall(); conn.close(); return rows

# ---------- Filter ----------
KEYWORDS = [
    "이더리움","이더","ethereum"," ether "," eth ","(eth)","[eth]","以太坊"
]
def match(text: str) -> bool:
    t = f" {text.lower()} "
    return any(k.lower() in t for k in KEYWORDS)

# ---------- Fetch ----------
def fetch_articles(max_per_feed=30):
    ensure_db()
    # 이미 본 링크 메모
    seen = set()
    try:
        conn = sqlite3.connect(DB_PATH); c = conn.cursor()
        c.execute("SELECT link FROM articles"); seen = {r[0] for r in c.fetchall()}
        conn.close()
    except Exception as e:
        print("[FETCH] read-seen error:", e)

    added = 0; scanned = 0
    for url in FEEDS:
        try:
            fp = feedparser.parse(url, request_headers=REQUEST_HEADERS)
            src = fp.feed.get("title", url)
            entries = fp.entries[:max_per_feed]
            for e in entries:
                scanned += 1
                title = e.get("title","")
                link  = e.get("link")
                summary = e.get("summary","")
                # 제목 + 요약을 함께 검사
                text_for_filter = f"{title} {summary}"
                if not link or not title: 
                    continue
                if match(text_for_filter) and link not in seen:
                    insert_article(title, link, src, e.get("published",""))
                    added += 1
        except Exception as e:
            print(f"[FETCH] feed error {url} ->", e)

    print(f"[FETCH] scanned={scanned} added={added} at {time.strftime('%H:%M:%S')}")
    # 완전 빈 DB면 보기용으로 최근 몇 개라도 채워 넣기(필터 무시, 사용자 경험용)
    try:
        if len(query()) == 0:
            for url in FEEDS:
                fp = feedparser.parse(url, request_headers=REQUEST_HEADERS)
                src = fp.feed.get("title", url)
                for e in fp.entries[:8]:
                    title = e.get("title",""); link = e.get("link")
                    if link and title:
                        insert_article(title, link, src, e.get("published",""))
            print("[FETCH] fallback seeded some articles")
    except Exception as e:
        print("[FETCH] fallback error:", e)

# ---------- Flask ----------
@app.before_request
def _ensure():
    try: ensure_db()
    except Exception as e: print("[DB]", e)

@app.get("/")
def home():
    return render_template_string(HTML)

@app.post("/unlock")
def unlock():
    j = request.get_json(silent=True) or {}
    if j.get("password") == ADMIN_PASSWORD:
        session["unlocked"] = True
        return jsonify({"success": True})
    return jsonify({"success": False, "error":"비밀번호가 올바르지 않습니다."}), 401

@app.get("/api/unlocked")
def api_unlocked():
    return jsonify({"unlocked": bool(session.get("unlocked"))})

@app.get("/api/articles")
def api_articles():
    if not session.get("unlocked"):
        return jsonify({"error":"locked"}), 401
    q = request.args.get("q","").strip() or None
    rows = query(q)
    def to_local(s):
        try:
            from dateutil import parser as dtp
            return dtp.parse(s).astimezone().strftime("%Y-%m-%d %H:%M:%S")
        except Exception:
            return s
    return jsonify({"articles":[
        {"title":r[0], "link":r[1], "source":r[2] or "", "published_at_local": to_local(r[3])}
        for r in rows
    ]})

# ---- Admin: 강제 수집 & 상태 확인 ----
@app.get("/admin/fetch")
def admin_fetch():
    if request.args.get("pw") != ADMIN_PASSWORD:
        return jsonify({"ok":False, "error":"nope"}), 401
    before = len(query())
    fetch_articles()
    after = len(query())
    return jsonify({"ok":True, "added": after - before, "total": after})

@app.get("/admin/ping")
def admin_ping():
    return jsonify({"ok": True, "count": len(query())})

# ---------- Boot ----------
print("[BOOT] init & first fetch…")
ensure_db()
fetch_articles()  # 시작 즉시 1회
sched = BackgroundScheduler()
sched.add_job(fetch_articles, "interval", minutes=30)
sched.start()

HTML = """
<!doctype html><html lang="ko"><head><meta charset="utf-8">
<title>이더리움 뉴스 집계기</title>
<style>
body{font-family:Arial;background:#111;color:#eee;text-align:center}
input,button{padding:8px;margin:5px;border-radius:6px;border:none}
.card{background:#222;margin:10px auto;padding:10px;border-radius:10px;width:90%;max-width:720px}
a{color:#00bcd4;text-decoration:none}.small{color:#aaa;font-size:12px}#msg{color:#ffb74d}
</style></head><body>
<h2>이더리움 뉴스 집계기</h2>
<div id="lock" style="display:none">
  <p>관리자 비밀번호 입력:</p>
  <input type="password" id="pw"><button onclick="unlock()">잠금 해제</button>
  <p id="msg"></p>
</div>
<div id="content" style="display:none">
  <input id="search" placeholder="검색어 입력" oninput="load()">
  <div id="hint" class="small"></div>
  <div id="articles"></div>
</div>
<script>
async function check(){let r=await fetch('/api/unlocked');let j=await r.json();
  if(j.unlocked){document.getElementById('content').style.display='block';load();}
  else document.getElementById('lock').style.display='block';}
async function unlock(){
  let pw=document.getElementById('pw').value;
  let r=await fetch('/unlock',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({password:pw})});
  let j=await r.json();
  if(j.success){document.getElementById('lock').style.display='none';document.getElementById('content').style.display='block';load();}
  else document.getElementById('msg').innerText=j.error||'실패';}
async function load(){
  let q=document.getElementById('search').value, hint=document.getElementById('hint');
  let r=await fetch('/api/articles?q='+encodeURIComponent(q));
  if(r.status===401){document.getElementById('lock').style.display='block';document.getElementById('content').style.display='none';return;}
  let j=await r.json(); let box=document.getElementById('articles'); box.innerHTML='';
  if(!j.articles || j.articles.length===0){hint.innerText='표시할 기사가 없습니다. 잠시만 기다리거나 상단의 /admin/fetch로 강제 수집하세요.'; return;}
  hint.innerText='';
  j.articles.forEach(a=>{
    let d=document.createElement('div'); d.className='card';
    d.innerHTML=`<a href="${a.link}" target="_blank">${a.title}</a><br><div class="small">${a.source} | ${a.published_at_local||''}</div>`;
    box.appendChild(d);
  });
}
window.addEventListener('DOMContentLoaded', check);
</script></body></html>
"""

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=8000)
