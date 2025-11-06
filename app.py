import os, sqlite3, time
from datetime import datetime, timezone
from flask import Flask, request, jsonify, render_template_string, redirect, url_for, session, abort
from apscheduler.schedulers.background import BackgroundScheduler
import feedparser
import requests
from bs4 import BeautifulSoup

# -------------------- 기본 설정 --------------------
APP_PASSWORD = "Philia12"                     # 요구사항대로 하드코딩(진짜 서비스에선 ENV 써라)
DB_DIR = os.environ.get("DB_DIR", "/tmp/data")
DB_PATH = os.path.join(DB_DIR, "ethnews.db")

FETCH_INTERVAL_MIN = int(os.environ.get("FETCH_INTERVAL_MIN", "30"))  # 30분마다 수집(무료 인스턴스 배려)
USER_AGENT = "Mozilla/5.0 (compatible; EthNewsFetcher/1.0)"
TIMEOUT = (5, 10)  # (connect, read)

app = Flask(__name__)
app.secret_key = os.environ.get("SECRET_KEY", "not-secret-but-whatever")  # 세션용

scheduler = BackgroundScheduler(daemon=True)

KEYWORDS = [
    "ethereum","ether","eth",
    "비트코인","이더리움","암호화폐","가상화폐","블록체인","코인"
]

RSS_SOURCES = [
    # 영어권 대표
    ("Cointelegraph", "https://cointelegraph.com/rss"),
    ("Decrypt",        "https://decrypt.co/feed"),
    ("CoinDesk",       "https://www.coindesk.com/arc/outboundfeeds/rss/"),
    # 한국(조선일보 경제 RSS) — 키워드 필터로 코인 관련만 선별
    ("조선일보",       "https://www.chosun.com/arc/outboundfeeds/rss/category/economy/?outputType=xml"),
]

# -------------------- DB 유틸 --------------------
def ensure_db():
    os.makedirs(DB_DIR, exist_ok=True)
    with sqlite3.connect(DB_PATH) as conn:
        c = conn.cursor()
        c.execute("""
        CREATE TABLE IF NOT EXISTS articles (
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          title TEXT NOT NULL,
          link TEXT UNIQUE,
          source TEXT,
          published INTEGER,         -- epoch seconds (UTC)
          summary TEXT
        )
        """)
        c.execute("CREATE INDEX IF NOT EXISTS idx_published_desc ON articles(published DESC)")
        c.execute("CREATE INDEX IF NOT EXISTS idx_source ON articles(source)")
        c.execute("CREATE INDEX IF NOT EXISTS idx_title ON articles(title)")
        conn.commit()

def db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn

def utc_now_s():
    return int(time.time())

# -------------------- 수집 로직 --------------------
def match_keywords(text: str) -> bool:
    t = (text or "").lower()
    return any(k in t for k in KEYWORDS)

def strip_html_keep_text(html: str) -> str:
    if not html:
        return ""
    soup = BeautifulSoup(html, "html5lib")
    # 이미지/스크립트 제거
    for tag in soup(["script", "style", "img", "video", "source"]):
        tag.decompose()
    txt = " ".join(soup.get_text(separator=" ").split())
    return txt[:800]  # 요약 과열 방지

def fetch_feed(source_name: str, url: str) -> int:
    # feedparser 자체가 내부 요청하므로, 여기선 requests로 받아서 text를 파싱해
    try:
        resp = requests.get(url, headers={"User-Agent": USER_AGENT}, timeout=TIMEOUT)
        resp.raise_for_status()
        feed = feedparser.parse(resp.text)
    except Exception:
        return 0

    added = 0
    with db() as conn:
        c = conn.cursor()
        for e in feed.entries[:80]:  # 과한 페치 방지
            title = e.get("title", "").strip()
            link  = e.get("link", "").strip()
            summ  = e.get("summary", "") or e.get("description", "") or ""
            if not title or not link:
                continue

            blob = f"{title} {summ}"
            if not match_keywords(blob):
                continue

            # 발행 시각
            if "published_parsed" in e and e.published_parsed:
                ts = int(time.mktime(e.published_parsed))
            elif "updated_parsed" in e and e.updated_parsed:
                ts = int(time.mktime(e.updated_parsed))
            else:
                ts = utc_now_s()

            summary = strip_html_keep_text(summ)
            try:
                c.execute("""
                  INSERT OR IGNORE INTO articles(title, link, source, published, summary)
                  VALUES (?, ?, ?, ?, ?)
                """, (title, link, source_name, ts, summary))
                if c.rowcount > 0:
                    added += 1
            except sqlite3.Error:
                # 어떤 깨진 행이 있어도 전체는 안죽게
                pass
        conn.commit()
    return added

def fetch_once() -> dict:
    total_added = 0
    total_seen  = 0
    for name, url in RSS_SOURCES:
        added = fetch_feed(name, url)
        total_added += added
        total_seen  += 1
    return {"ok": True, "sources": total_seen, "added": total_added, "time": utc_now_s()}

def schedule_jobs():
    # 무료 512MB 환경 방어: 너무 자주 돌리지 말고, 실패해도 조용히
    if scheduler.get_jobs():
        return
    scheduler.add_job(fetch_once, "interval", minutes=FETCH_INTERVAL_MIN, id="fetch_job", max_instances=1, coalesce=True, misfire_grace_time=120)
    scheduler.start()

# -------------------- 인증/세션 --------------------
def logged_in() -> bool:
    return session.get("ok") is True

def require_login():
    if not logged_in():
        abort(401)

@app.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "POST":
        pw = request.form.get("pw", "")
        if pw == APP_PASSWORD:
            session["ok"] = True
            return redirect(url_for("index"))
        return render_template_string(LOGIN_HTML, error="비밀번호가 틀렸습니다.")
    return render_template_string(LOGIN_HTML, error=None)

@app.route("/logout", methods=["POST"])
def logout():
    session.clear()
    return redirect(url_for("login"))

# -------------------- API --------------------
@app.route("/api/articles")
def api_articles():
    require_login()
    q = (request.args.get("q") or "").strip()
    source = (request.args.get("source") or "").strip()
    limit = min(int(request.args.get("limit", "20")), 50)
    offset = max(int(request.args.get("offset", "0")), 0)

    sql = "SELECT title, link, source, published, summary FROM articles"
    cond, params = [], []
    if q:
        cond.append("(title LIKE ? OR summary LIKE ?)")
        like = f"%{q}%"
        params.extend([like, like])
    if source:
        cond.append("source = ?")
        params.append(source)
    if cond:
        sql += " WHERE " + " AND ".join(cond)
    sql += " ORDER BY published DESC LIMIT ? OFFSET ?"
    params.extend([limit, offset])

    with db() as conn:
        rows = conn.execute(sql, params).fetchall()
    data = [{
        "title": r["title"],
        "link": r["link"],
        "source": r["source"],
        "published": r["published"],
        "summary": r["summary"]
    } for r in rows]
    return jsonify({"articles": data})

@app.route("/admin/fetch")
def admin_fetch():
    # 수동 주입 엔드포인트. pw=Philia12 필요
    pw = request.args.get("pw", "")
    if pw != APP_PASSWORD:
        return jsonify({"error": "locked"}), 403
    ensure_db()
    info = fetch_once()
    return jsonify(info)

# -------------------- UI --------------------
@app.route("/")
def index():
    if not logged_in():
        return redirect(url_for("login"))
    return render_template_string(INDEX_HTML)

# Flask 3.x: before_first_request 제거됨 → before_serving 사용
@app.before_serving
def boot_fetch():
    ensure_db()
    try:
        fetch_once()  # 첫 구동시 한 번 채워두기
    except Exception:
        pass
    schedule_jobs()

# -------------------- 템플릿 --------------------
LOGIN_HTML = r"""
<!doctype html>
<html lang="ko">
<head>
  <meta charset="utf-8">
  <title>로그인 • ETH 뉴스</title>
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <style>
    html,body{height:100%;margin:0;background:#0f172a;color:#e2e8f0;font-family:system-ui, -apple-system, Segoe UI, Roboto}
    .bg{
      position:fixed; inset:0; opacity:.12; pointer-events:none;
      background-image:
        radial-gradient(1200px 600px at 10% 10%, #6ee7b7 0%, rgba(0,0,0,0) 60%),
        radial-gradient(1000px 500px at 90% 0%,  #60a5fa 0%, rgba(0,0,0,0) 60%),
        radial-gradient(1000px 500px at 50% 100%, #a78bfa 0%, rgba(0,0,0,0) 60%),
        url('https://upload.wikimedia.org/wikipedia/commons/0/05/Ethereum_logo_2014.svg');
      background-repeat:no-repeat;
      background-size:cover, cover, cover, 38rem;
      background-position:center center, center center, center center, center 30%;
      filter:contrast(120%) saturate(120%);
    }
    .card{max-width:420px;margin:12vh auto;padding:28px;background:rgba(2,6,23,.7);border:1px solid rgba(148,163,184,.2);border-radius:16px;backdrop-filter:blur(6px)}
    input{width:100%;padding:12px 14px;border-radius:10px;border:1px solid rgba(148,163,184,.3);background:#0b1220;color:#e2e8f0}
    button{width:100%;margin-top:12px;padding:12px 14px;border-radius:10px;border:0;background:#22d3ee;color:#001016;font-weight:700;cursor:pointer}
    .err{color:#fda4af;margin-bottom:8px}
    h1{font-size:22px;margin:0 0 14px 0}
  </style>
</head>
<body>
  <div class="bg"></div>
  <div class="card">
    <h1>이더리움 실시간 뉴스 • 로그인</h1>
    {% if error %}<div class="err">{{error}}</div>{% endif %}
    <form method="post">
      <input name="pw" type="password" placeholder="비밀번호 입력 (Philia12)" autofocus>
      <button type="submit">입장</button>
    </form>
  </div>
</body>
</html>
"""

INDEX_HTML = r"""
<!doctype html>
<html lang="ko">
<head>
  <meta charset="utf-8">
  <title>이더리움 실시간 뉴스 집계</title>
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <style>
    :root{--ink:#e5f4ff;--muted:#9fb3c8;--card:#0b1220;--chip:#152033;--accent:#22d3ee}
    html,body{height:100%;margin:0;background:#0f172a;color:var(--ink);font-family:system-ui,-apple-system,Segoe UI,Roboto}
    .bg{
      position:fixed; inset:0; opacity:.10; pointer-events:none;
      background-image:
        radial-gradient(1200px 600px at 10% 10%, #6ee7b7 0%, rgba(0,0,0,0) 60%),
        radial-gradient(1000px 500px at 90% 0%,  #60a5fa 0%, rgba(0,0,0,0) 60%),
        radial-gradient(1000px 500px at 50% 100%, #a78bfa 0%, rgba(0,0,0,0) 60%),
        url('https://upload.wikimedia.org/wikipedia/commons/0/05/Ethereum_logo_2014.svg');
      background-repeat:no-repeat;
      background-size:cover, cover, cover, 36rem;
      background-position:center center, center center, center center, right 10% top 20%;
      filter:contrast(120%) saturate(120%);
    }
    header{position:sticky;top:0;backdrop-filter:blur(4px);background:rgba(2,6,23,.6);border-bottom:1px solid rgba(148,163,184,.12)}
    .wrap{max-width:980px;margin:0 auto;padding:18px}
    h1{margin:6px 0 14px 0;font-size:26px}
    .row{display:flex;gap:10px;align-items:center}
    .row form{margin-left:auto}
    input[type=text]{flex:1;padding:12px 14px;border-radius:12px;border:1px solid rgba(148,163,184,.25);background:#0b1220;color:var(--ink)}
    .btn{padding:10px 14px;border-radius:10px;border:1px solid rgba(148,163,184,.25);background:var(--chip);color:var(--ink);cursor:pointer}
    .btn.acc{background:var(--accent);color:#001016;border:0;font-weight:700}
    .list{max-width:980px;margin:18px auto;padding:0 18px}
    .card{background:var(--card);border:1px solid rgba(148,163,184,.18);border-radius:16px;padding:16px 18px;margin:14px 0}
    .title{font-size:20px;margin:0 0 6px 0}
    .meta{color:var(--muted);font-size:12px;margin-bottom:10px}
    .source{display:inline-block;padding:3px 8px;background:var(--chip);border-radius:999px;margin-left:6px}
    a{color:#8dd1ff;text-decoration:none}
    a:hover{text-decoration:underline}
    .more{display:block;margin:16px auto 32px auto}
    .logout{margin-left:10px}
  </style>
</head>
<body>
  <div class="bg"></div>
  <header>
    <div class="wrap">
      <h1>이더리움 실시간 뉴스 집계</h1>
      <div class="row">
        <input id="q" type="text" placeholder="제목/매체로 검색 (Enter)">
        <button class="btn" id="srcAll">전체</button>
        <button class="btn" data-src="Cointelegraph">Cointelegraph</button>
        <button class="btn" data-src="Decrypt">Decrypt</button>
        <button class="btn" data-src="CoinDesk">CoinDesk</button>
        <button class="btn" data-src="조선일보">조선일보</button>
        <form method="post" action="/logout"><button class="btn logout" type="submit">로그아웃</button></form>
      </div>
    </div>
  </header>

  <div class="list" id="list"></div>
  <button id="more" class="btn more">더 보기</button>

<script>
let offset = 0, limit = 20, busy = false, q = "", src = "";
const list = document.getElementById("list");
const more = document.getElementById("more");
const qbox = document.getElementById("q");

function fmt(ts){
  const d = new Date(ts*1000);
  return d.getFullYear()+"."+String(d.getMonth()+1).padStart(2,"0")+"."+String(d.getDate()).padStart(2,"0")+" "+
         String(d.getHours()).padStart(2,"0")+":"+String(d.getMinutes()).padStart(2,"0")+":"+String(d.getSeconds()).padStart(2,"0");
}

function card(a){
  const div = document.createElement("div");
  div.className = "card";
  div.innerHTML = `
    <div class="title"><a href="${a.link}" target="_blank" rel="noopener">${a.title}</a></div>
    <div class="meta">${a.source ? a.source : ""} <span class="source">${a.source||""}</span> | ${fmt(a.published)}</div>
    ${a.summary ? `<div class="sum">${a.summary.replace(/</g,"&lt;")}</div>` : ``}
  `;
  return div;
}

async function load(reset=false){
  if (busy) return;
  busy = true;
  if (reset){ offset = 0; list.innerHTML = ""; }
  const url = new URL(location.origin + "/api/articles");
  url.searchParams.set("limit", limit);
  url.searchParams.set("offset", offset);
  if (q)   url.searchParams.set("q", q);
  if (src) url.searchParams.set("source", src);
  const r = await fetch(url);
  if (!r.ok){ busy=false; return; }
  const js = await r.json();
  const arr = js.articles || [];
  arr.forEach(a => list.appendChild(card(a)));
  offset += arr.length;
  more.style.display = arr.length < limit ? "none" : "block";
  busy = false;
}

more.addEventListener("click", ()=> load(false));
qbox.addEventListener("keydown", (e)=>{ if(e.key==="Enter"){ q = qbox.value.trim(); load(true);} });
document.querySelectorAll("button[data-src]").forEach(b=>{
  b.addEventListener("click", ()=>{ src = b.getAttribute("data-src"); load(true); });
});
document.getElementById("srcAll").addEventListener("click", ()=>{ src=""; load(true); });

load(true);
</script>
</body>
</html>
"""

# -------------------- WSGI --------------------
# gunicorn 에서 'app:app' 으로 참조
if __name__ == "__main__":
    # 로컬 테스트용
    ensure_db()
    app.run(host="0.0.0.0", port=5000, debug=True)
