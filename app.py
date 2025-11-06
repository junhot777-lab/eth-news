import os, sqlite3, json, time
from datetime import datetime, timezone, timedelta
from flask import Flask, request, jsonify, g, render_template_string, redirect, url_for, session, abort
import requests
import feedparser
from apscheduler.schedulers.background import BackgroundScheduler

# ------------------ Config ------------------
ADMIN_PW = "Philia12"                          # 관리자 주입용 비번
LOGIN_PW = "Philia12"                          # 화면 접근용 비번
SECRET_KEY = os.environ.get("SECRET_KEY", "eth-news-secret-keep-it")
REGION_TZ = timezone(timedelta(hours=9))       # KST 표기
DB_DIR = "/tmp/data"
DB_PATH = os.path.join(DB_DIR, "news.db")
FETCH_INTERVAL_MIN = 30                        # 자동 수집 주기(분). free 인스턴스 안전 범위

# RSS/피드 목록 (가볍고 안정적인 것만)
FEEDS = [
    # 영어권 크립토
    ("Cointelegraph", "https://news.google.com/rss/search?q=site:cointelegraph.com+Ethereum+OR+ETH&hl=en"),
    ("Decrypt",        "https://news.google.com/rss/search?q=site:decrypt.co+Ethereum+OR+ETH&hl=en"),
    ("CoinDesk",       "https://news.google.com/rss/search?q=site:coindesk.com+Ethereum+OR+ETH&hl=en"),
    # 한국(조선일보) 가상화폐/이더리움 관련
    ("조선일보",       "https://news.google.com/rss/search?q=site:chosun.com+%EA%B0%80%EC%83%81%ED%99%94%ED%8F%90+OR+%EC%9D%B4%EB%8D%94%EB%A6%AC%EC%9B%80&hl=ko"),
]

# ------------------ App ------------------
app = Flask(__name__)
app.secret_key = SECRET_KEY

# ------------------ DB ------------------
def ensure_db():
    os.makedirs(DB_DIR, exist_ok=True)
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute("""
        CREATE TABLE IF NOT EXISTS articles(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            title TEXT NOT NULL,
            source TEXT,
            link TEXT UNIQUE,
            published_at INTEGER,   -- epoch seconds
            created_at INTEGER DEFAULT (strftime('%s','now'))
        )""")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_published ON articles(published_at DESC)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_source ON articles(source)")
        conn.commit()

def get_db():
    if "db" not in g:
        g.db = sqlite3.connect(DB_PATH, timeout=10, check_same_thread=False)
    return g.db

@app.teardown_appcontext
def close_db(_e=None):
    db = g.pop("db", None)
    if db:
        db.close()

# ------------------ Fetcher ------------------
def normalize_ts(entry):
    # feedparser가 주는 published_parsed 우선
    ts = None
    if getattr(entry, "published_parsed", None):
        try:
            ts = int(time.mktime(entry.published_parsed))
        except Exception:
            ts = None
    if ts is None:
        ts = int(time.time())
    return ts

def fetch_once():
    """모든 FEEDS에서 기사 수집. 중복은 UNIQUE(link)로 자동 스킵."""
    ensure_db()
    added = 0
    for source, url in FEEDS:
        try:
            fp = feedparser.parse(url)
            for e in fp.entries[:50]:  # 각 소스 당 상위 50건만, 과도한 메모리 방지
                link = getattr(e, "link", None)
                title = (getattr(e, "title", "") or "").strip()
                if not link or not title:
                    continue
                ts = normalize_ts(e)
                try:
                    get_db().execute(
                        "INSERT OR IGNORE INTO articles(title, source, link, published_at) VALUES(?,?,?,?)",
                        (title, source, link, ts),
                    )
                    added += get_db().total_changes
                except sqlite3.Error:
                    pass
        except Exception:
            # 개별 피드 실패해도 전체 멈추지 않음
            continue
    try:
        get_db().commit()
    except Exception:
        pass
    return added

# ------------------ Scheduler (가벼운 주기 수집) ------------------
scheduler = BackgroundScheduler(daemon=True, timezone="UTC", job_defaults={"coalesce": True, "max_instances": 1})
@scheduler.scheduled_job("interval", minutes=FETCH_INTERVAL_MIN)
def scheduled_fetch():
    try:
        fetch_once()
    except Exception:
        pass

# Render free 인스턴스는 부팅 직후 한 번만
@app.before_first_request
def boot_fetch():
    ensure_db()
    try:
        fetch_once()
    except Exception:
        pass
    if not scheduler.running:
        scheduler.start()

# ------------------ Auth guard ------------------
WHITELIST = {"/login", "/healthz"}
def allowed_path(path):
    if path in WHITELIST: 
        return True
    if path.startswith("/static/"):
        return True
    # 관리자 주입은 별도 쿼리 비번
    return False

@app.before_request
def guard():
    # API와 메인 화면을 세션 로그인으로 보호
    if allowed_path(request.path):
        return
    if request.path.startswith("/admin/fetch"):
        # /admin/fetch?pw=Philia12 로 별도 허용
        return
    if session.get("authed") is True:
        return
    return redirect(url_for("login", next=request.path))

# ------------------ UI ------------------
HTML = r"""
<!doctype html>
<html lang="ko">
<head>
  <meta charset="utf-8"/>
  <meta name="viewport" content="width=device-width,initial-scale=1"/>
  <title>이더리움 실시간 뉴스 집계</title>
  <style>
    :root { --fg:#e7eef7; --muted:#9fb0c3; --card:#111a23; --accent:#5ec1ff; }
    * { box-sizing:border-box; }
    body {
      margin:0; color:var(--fg); font-family:system-ui,-apple-system,Segoe UI,Roboto,Apple SD Gothic Neo,Noto Sans KR,sans-serif;
      background: #0a0f14;
      /* 이더 배경(가벼운 SVG 패턴) */
      background-image:
        radial-gradient(ellipse at 10% 0%, #0e1620 0 40%, transparent 41%),
        radial-gradient(ellipse at 90% 0%, #0e1620 0 40%, transparent 41%),
        repeating-linear-gradient(135deg, rgba(94,193,255,.08) 0 1px, transparent 1px 24px);
    }
    header { padding:28px 18px 6px; text-align:center; }
    h1 { margin:0 0 6px; font-size:28px; letter-spacing:.5px; }
    .sub { color:var(--muted); font-size:12px; }
    .wrap { max-width:960px; margin:16px auto 80px; padding:0 12px; }
    .toolbar { display:flex; gap:8px; align-items:center; }
    input[type=search]{
      width:100%; padding:12px 14px; border:1px solid #223142; border-radius:10px; background:#0c141c; color:var(--fg);
      outline:none;
    }
    .logout-btn {
      white-space:nowrap; padding:10px 14px; border-radius:10px; background:#162433; color:#dbe9f6; border:1px solid #28425c; cursor:pointer;
    }
    .card {
      background:var(--card); border:1px solid #1b2a3a; border-radius:16px; padding:16px 18px; margin:14px 0;
      box-shadow: 0 6px 18px rgba(0,0,0,.25);
    }
    .title { color:#cbe6ff; font-size:20px; margin:0 0 8px; line-height:1.35; }
    .title a { color:#8cd3ff; text-decoration:none; }
    .meta { color:var(--muted); font-size:12px; margin-bottom:8px; }
    .source-pill { display:inline-block; border:1px solid #24455f; color:#c3d7e9; border-radius:999px; padding:4px 10px; font-size:12px; margin-left:6px;}
    .loadmore { display:block; width:100%; padding:14px; margin:18px 0; border-radius:12px; border:1px solid #24455f; background:#102030; color:#cfe8fb; cursor:pointer;}
    .empty { text-align:center; color:#8ca3b9; padding:32px 4px;}
  </style>
</head>
<body>
  <header>
    <h1>이더리움 실시간 뉴스 집계</h1>
    <div class="sub">최신순 · 자동 집계(주기마다) · 검색(제목/매체)</div>
  </header>
  <div class="wrap">
    <div class="toolbar">
      <input id="q" type="search" placeholder="제목/매체로 검색 (Enter)"/>
      <form method="post" action="/logout"><button class="logout-btn">로그아웃</button></form>
    </div>
    <div id="list"></div>
    <button id="more" class="loadmore" style="display:none">더 보기</button>
    <div id="empty" class="empty" style="display:none">표시할 기사가 없습니다.</div>
  </div>

<script>
const list = document.getElementById('list');
const more = document.getElementById('more');
const emptyEl = document.getElementById('empty');
const q = document.getElementById('q');

let offset = 0, limit = 20, loading = false, ended = false, currentQ = "";

function fmt(ts){
  try{
    const d = new Date(ts*1000);
    const z = (n)=>String(n).padStart(2,'0');
    return `${d.getFullYear()}.${z(d.getMonth()+1)}.${z(d.getDate())} ${z(d.getHours())}:${z(d.getMinutes())}:${z(d.getSeconds())}`;
  }catch(e){ return ""; }
}

function card(item){
  const el = document.createElement('div');
  el.className = 'card';
  el.innerHTML = `
    <h3 class="title"><a target="_blank" rel="noopener" href="${item.link}">${item.title}</a></h3>
    <div class="meta">${item.source} · <span>${fmt(item.published_at)}</span> <span class="source-pill">${item.source}</span></div>
  `;
  return el;
}

async function load(){
  if(loading || ended) return;
  loading = true;
  more.style.display = 'none';
  const u = new URL(window.location.origin + '/api/articles');
  u.searchParams.set('limit', limit);
  u.searchParams.set('offset', offset);
  if(currentQ) u.searchParams.set('q', currentQ);
  const res = await fetch(u);
  if(!res.ok){ loading=false; return; }
  const data = await res.json();
  const arr = data.articles || [];
  if(offset===0){ list.innerHTML = ''; emptyEl.style.display = 'none'; }
  arr.forEach(a => list.appendChild(card(a)));
  offset += arr.length;
  loading = false;
  if(arr.length < limit){ ended = true; }
  more.style.display = ended ? 'none' : 'block';
  if(offset===0) emptyEl.style.display = 'block';
}

// 디바운스 스크롤(서버 폭격 방지)
let scrollTimer;
window.addEventListener('scroll', ()=>{
  clearTimeout(scrollTimer);
  scrollTimer = setTimeout(()=>{
    const nearBottom = window.innerHeight + window.scrollY >= document.body.offsetHeight - 200;
    if(nearBottom) load();
  }, 500);
});

more.addEventListener('click', load);

q.addEventListener('keydown', (e)=>{
  if(e.key === 'Enter'){
    currentQ = q.value.trim();
    offset = 0; ended = false;
    load();
  }
});

// 첫 로드
load();

// 5분마다 리스트 갱신(상단만 최신화)
setInterval(()=>{
  offset = 0; ended = false;
  load();
}, 300000);
</script>
</body>
</html>
"""

LOGIN_HTML = r"""
<!doctype html>
<html lang="ko">
<head>
  <meta charset="utf-8"/>
  <meta name="viewport" content="width=device-width,initial-scale=1"/>
  <title>로그인</title>
  <style>
    body{margin:0;display:grid;place-items:center;height:100vh;background:#0a0f14;color:#e7eef7;font-family:system-ui,-apple-system,Segoe UI,Roboto,Apple SD Gothic Neo,Noto Sans KR,sans-serif;}
    .box{width:min(92vw,420px);background:#101a24;border:1px solid #1b2a3a;border-radius:16px;padding:20px;box-shadow:0 10px 24px rgba(0,0,0,.35);}
    h2{margin:0 0 10px}
    input{width:100%;padding:12px 14px;border:1px solid #223142;border-radius:10px;background:#0c141c;color:#e7eef7;outline:none}
    button{margin-top:12px;width:100%;padding:12px;border-radius:10px;border:1px solid #28425c;background:#162433;color:#dbe9f6;cursor:pointer}
    .err{color:#ff8b8b;font-size:12px;height:16px;margin-top:6px}
  </style>
</head>
<body>
  <form class="box" method="post">
    <h2>접속 비밀번호</h2>
    <input type="password" name="pw" placeholder="비밀번호" autofocus/>
    <div class="err">{{ err or "" }}</div>
    <button>입장</button>
  </form>
</body>
</html>
"""

# ------------------ Routes ------------------
@app.route("/healthz")
def health():
    return "ok", 200

@app.route("/login", methods=["GET", "POST"])
def login():
    err = ""
    if request.method == "POST":
        if request.form.get("pw") == LOGIN_PW:
            session["authed"] = True
            nxt = request.args.get("next") or url_for("home")
            return redirect(nxt)
        else:
            err = "비밀번호가 틀렸습니다."
    return render_template_string(LOGIN_HTML, err=err)

@app.route("/logout", methods=["POST"])
def logout():
    session.clear()
    return redirect(url_for("login"))

@app.route("/")
def home():
    return render_template_string(HTML)

@app.route("/api/articles")
def api_articles():
    # 세션 보호됨
    q = (request.args.get("q") or "").strip()
    try:
        limit = max(1, min(50, int(request.args.get("limit", 20))))
    except Exception:
        limit = 20
    try:
        offset = max(0, int(request.args.get("offset", 0)))
    except Exception:
        offset = 0

    sql = "SELECT title, source, link, published_at FROM articles"
    params = []
    if q:
        sql += " WHERE title LIKE ? OR source LIKE ?"
        k = f"%{q}%"
        params += [k, k]
    sql += " ORDER BY published_at DESC LIMIT ? OFFSET ?"
    params += [limit, offset]

    cur = get_db().execute(sql, params)
    rows = cur.fetchall()
    arts = [{"title": r[0], "source": r[1] or "", "link": r[2], "published_at": int(r[3] or 0)} for r in rows]
    return jsonify({"articles": arts})

@app.route("/admin/fetch")
def admin_fetch():
    if request.args.get("pw") != ADMIN_PW:
        return jsonify({"error":"locked"}), 403
    added = fetch_once()
    total = get_db().execute("SELECT COUNT(*) FROM articles").fetchone()[0]
    return jsonify({"ok": True, "added": added, "total": total})

# ------------------ Gunicorn hook ------------------
# Render는 PORT를 환경변수로 넘김. (Procfile 없이 Start Command: `gunicorn app:app`)
# 여기는 로컬 테스트용으로만 사용.
if __name__ == "__main__":
    ensure_db()
    fetch_once()
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", "5000")))
