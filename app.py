import os
import sqlite3
import time
from datetime import datetime, timezone
from urllib.parse import urlparse

import feedparser
from flask import (
    Flask, request, jsonify, render_template_string,
    redirect, url_for, session, abort, Response
)

# ---------------------- 기본 설정 ----------------------
DB_PATH = os.path.join("/tmp", "news.db")
os.makedirs("/tmp", exist_ok=True)

app = Flask(__name__)
app.secret_key = os.environ.get("FLASK_SECRET", "eth-news-secret")

SITE_PASSWORD = os.environ.get("SITE_PASSWORD", "Philia12")           # 로그인 비번
ADMIN_PASSWORD = os.environ.get("ADMIN_PASSWORD", SITE_PASSWORD)       # /admin/fetch, /cron 보호
OPENAI_API_KEY = os.environ.get("OPENAI_API_KEY", "").strip()          # 선택: 있으면 LLM 번역 사용

FEEDS = [
    "https://www.coindesk.com/arc/outboundfeeds/rss/?outputType=xml",
    "https://www.cointelegraph.com/rss",
    "https://decrypt.co/feed",
    "https://www.theblock.co/rss",
]
ETH_KEYWORDS = [
    "ethereum"," ether "," eth ","이더리움","vitalik","rollup","layer 2","layer2","staking",
    "beacon","eip-","l2","arbitrum","optimism","base","etf"
]

# ---------------------- DB ----------------------
def get_db():
    conn = sqlite3.connect(DB_PATH, timeout=10, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    return conn

def init_db():
    conn = get_db()
    c = conn.cursor()
    c.execute("""
      CREATE TABLE IF NOT EXISTS articles(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        title TEXT NOT NULL,
        link  TEXT NOT NULL UNIQUE,
        source TEXT,
        published_at TEXT,
        summary TEXT
      )
    """)
    c.execute("CREATE INDEX IF NOT EXISTS idx_pub ON articles(published_at DESC)")
    conn.commit()
    conn.close()

init_db()

# ---------------------- 유틸 ----------------------
def host_of(url: str) -> str:
    try:
        h = urlparse(url).hostname or ""
        return h.replace("www.", "")
    except Exception:
        return ""

def is_eth_related(title: str, desc: str) -> bool:
    blob = f"{(title or '').lower()} {(desc or '').lower()}"
    return any(k in blob for k in ETH_KEYWORDS)

def parse_time(entry) -> str:
    # feedparser가 주는 parsed가 있으면 사용, 없으면 now
    try:
        if getattr(entry, "published_parsed", None):
            return datetime.fromtimestamp(time.mktime(entry.published_parsed), tz=timezone.utc).isoformat()
        if getattr(entry, "updated_parsed", None):
            return datetime.fromtimestamp(time.mktime(entry.updated_parsed), tz=timezone.utc).isoformat()
    except Exception:
        pass
    return datetime.now(tz=timezone.utc).isoformat()

def three_line_summary_en(title: str, desc: str) -> str:
    """외부 API 없이 초슬림 3줄 영문 요약 (제목+요약에서 문장 잘라 3줄)"""
    def clean(t: str) -> str:
        return " ".join((t or "").replace("\n"," ").split())
    t = clean(title)[:140]
    d = clean(desc)
    parts = [p.strip() for p in d.replace("•",". ").split(".") if p.strip()]
    line2 = parts[0][:180] if parts else ""
    line3 = (parts[1][:180] if len(parts) > 1 else "")
    lines = [f"• {t}"]
    if line2: lines.append(f"• {line2}")
    if line3: lines.append(f"• {line3}")
    return "\n".join(lines[:3])

# --- 제목 한국어 번역 (오프라인 사전 + 선택적 LLM) ---
_GLOSS = [
    ("ethereum","이더리움"), ("layer 2","레이어 2"), ("rollup","롤업"),
    ("staking","스테이킹"), ("validator","밸리데이터"), ("upgrade","업그레이드"),
    ("merge","머지"), ("etf","ETF"), ("sec","SEC"), ("price","가격"),
    ("surge","급등"), ("drop","하락"), ("network","네트워크"), ("fees","수수료"),
    ("mainnet","메인넷"), ("testnet","테스트넷"), ("airdrop","에어드롭"),
    ("foundation","재단"), ("proposal","제안"), ("governance","거버넌스")
]

def translate_title_ko_offline(title: str) -> str:
    s = title or ""
    low = s.lower()
    # 긴 단어부터 치환
    for en, ko in sorted(_GLOSS, key=lambda x: -len(x[0])):
        low = low.replace(en, ko)
    # 대충 첫 글자 대문자였던 건 유지 불가 → 그냥 결과 반환
    return low

def translate_title_ko(title: str, desc: str) -> str:
    """OPENAI_API_KEY가 있으면 LLM으로 깔끔 번역, 없으면 오프라인 사전 번역."""
    if not OPENAI_API_KEY:
        return translate_title_ko_offline(title)
    try:
        import json, urllib.request
        prompt = (
            "Translate the following crypto news title into natural Korean. "
            "Keep it concise and factual, no embellishment.\n\n"
            f"Title: {title}\n"
        )
        req = urllib.request.Request(
            url="https://api.openai.com/v1/chat/completions",
            data=json.dumps({
                "model":"gpt-4o-mini",
                "messages":[{"role":"user","content":prompt}],
                "temperature":0.2,
                "max_tokens":80
            }).encode("utf-8"),
            headers={"Authorization":f"Bearer {OPENAI_API_KEY}","Content-Type":"application/json"},
            method="POST"
        )
        with urllib.request.urlopen(req, timeout=5) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        text = (data["choices"][0]["message"]["content"] or "").strip()
        return text or translate_title_ko_offline(title)
    except Exception:
        return translate_title_ko_offline(title)

# ---------------------- 수집 ----------------------
def fetch_once(max_per_feed=25) -> dict:
    scanned, added = 0, 0
    conn = get_db()
    c = conn.cursor()
    for feed_url in FEEDS:
        try:
            d = feedparser.parse(feed_url)
        except Exception:
            continue
        src = (getattr(d, "feed", {}) or {}).get("title") or host_of(feed_url)
        cnt = 0
        for e in d.entries:
            if cnt >= max_per_feed:
                break
            scanned += 1
            title = (getattr(e, "title", "") or "").strip()
            link  = (getattr(e, "link", "") or "").strip()
            if not title or not link:
                continue
            desc = getattr(e, "summary", "") or getattr(e, "description", "") or ""
            if not is_eth_related(title, desc):
                continue
            pub = parse_time(e)
            summ = three_line_summary_en(title, desc)
            try:
                c.execute(
                    "INSERT OR IGNORE INTO articles(title,link,source,published_at,summary) VALUES(?,?,?,?,?)",
                    (title, link, src, pub, summ)
                )
                if c.rowcount > 0:
                    added += 1
                    cnt += 1
            except Exception:
                pass
    conn.commit()
    conn.close()
    return {"scanned": scanned, "added": added}

def count_rows():
    conn = get_db()
    n = conn.execute("SELECT COUNT(*) FROM articles").fetchone()[0]
    conn.close()
    return n

# ---------------------- 템플릿 (ETH 배경 + 제목_번역 + 최신순) ----------------------
PAGE = r"""<!doctype html>
<html lang="ko">
<head>
<meta charset="utf-8"/><meta name="viewport" content="width=device-width,initial-scale=1"/>
<title>이더리움 실시간 뉴스 집계</title>
<style>
  :root{--bg1:#0b0f17;--bg2:#101826;--card:#121b2a;--muted:#8aa0bf;--accent:#66d9ff;--border:#213047;--text:#e6f0ff}
  html,body{height:100%}
  body{
    margin:0;color:var(--text);font-family:system-ui,-apple-system,Segoe UI,Roboto,Apple SD Gothic Neo,Noto Sans KR,sans-serif;
    background:
      radial-gradient(1200px 600px at 80% -10%, rgba(102,217,255,.12), transparent 60%),
      radial-gradient(900px 500px at -10% 20%, rgba(95,158,255,.10), transparent 55%),
      linear-gradient(160deg, var(--bg1), var(--bg2));
  }
  body::before{
    content:"";position:fixed;inset:0;pointer-events:none;opacity:.05;
    background-image:url('data:image/svg+xml;utf8,<svg xmlns="http://www.w3.org/2000/svg" width="120" height="120" viewBox="0 0 120 120"><g fill="none" stroke="%23b5e3ff" stroke-width="1.2"><path d="M60 10 25 60l35-16 35 16z"/><path d="M60 10v94"/><path d="M60 104 25 60l35 16 35-16z"/></g></svg>');
    background-size:220px 220px;
  }
  .wrap{max-width:960px;margin:32px auto;padding:0 16px}
  h1{margin:0 0 6px;font-size:28px}
  .hint{color:var(--muted);font-size:14px;margin-bottom:14px}
  .bar{display:flex;gap:8px;margin:10px 0 16px}
  input[type="text"]{flex:1;padding:12px 14px;border-radius:12px;border:1px solid var(--border);background:#0f1624;color:var(--text);outline:none}
  .btn{padding:10px 14px;border-radius:12px;border:1px solid var(--border);background:#0f1624;color:var(--text);cursor:pointer}
  .card{
    background:rgba(18,27,42,.76);border:1px solid var(--border);border-radius:16px;padding:18px;margin:14px 0;
    box-shadow:0 6px 20px rgba(0,0,0,.28);backdrop-filter: blur(3px); position:relative;
  }
  .ttl{font-size:22px;line-height:1.35;color:#a9ceff;text-decoration:none;position:relative;z-index:2;pointer-events:auto}
  .ttl:hover{text-decoration:underline}
  .ko{margin-top:6px;color:#dfeaff;opacity:.95}
  .meta{color:var(--muted);font-size:13px;margin:6px 0 6px}
  .sum{white-space:pre-wrap;line-height:1.55;color:#dce9ff}
  .empt{padding:28px;text-align:center;color:var(--muted)}
  .login{max-width:400px;margin:90px auto}
  .center{display:flex;gap:8px;align-items:center;justify-content:center}
</style>
</head>
<body>
<div class="wrap">
  {% if not session.get('ok') %}
    <div class="login card">
      <h1>접속 비밀번호</h1>
      <p class="hint">허용된 사용자만 열람합니다.</p>
      <form method="post" action="{{ url_for('login') }}" class="center">
        <input type="password" name="pw" placeholder="Password" />
        <button class="btn" type="submit">입장</button>
      </form>
      {% if error %}<p class="hint" style="color:#ffb3b3">비밀번호가 틀렸습니다.</p>{% endif %}
    </div>
  {% else %}
    <h1>이더리움 실시간 뉴스 집계</h1>
    <div class="hint">최신순 · 자동 집계(5분마다) · 제목 한국어 번역 표시</div>

    <div class="bar">
      <input id="q" type="text" placeholder="제목/매체로 검색 (Enter)">
      <button class="btn" onclick="logout()">로그아웃</button>
    </div>

    <div id="list"></div>
    <div class="center" style="margin:16px 0;">
      <button id="more" class="btn" onclick="loadMore()">더 보기</button>
    </div>
  {% endif %}
</div>

{% if session.get('ok') %}
<script>
let page = 1, q = "", autoload = true;

function esc(s){return s.replace(/[&<>\"']/g,m=>({"&":"&amp;","<":"&lt;",">":"&gt;","\"":"&quot;","'":"&#39;"}[m]))}
function fmt(d){try{return new Date(d).toLocaleString('ko-KR')}catch(_){return d}}
function row(a){
  let host=""; try{ host=new URL(a.link).hostname.replace(/^www\./,""); }catch(_){ host=a.source||""; }
  const sums=(a.summary||"").trim();
  const titleKo=a.title_ko?`<div class="ko">🛈 ${esc(a.title_ko)}</div>`:"";
  return `
    <div class="card">
      <a class="ttl" href="${a.link}" target="_blank" rel="noopener noreferrer">${esc(a.title)}</a>
      ${titleKo}
      <div class="meta">${esc(host)} | ${fmt(a.published_at)}</div>
      ${sums?`<div class="sum">${esc(sums)}</div>`:""}
    </div>`;
}

async function fetchPage(reset=false){
  const params = new URLSearchParams({ page:String(page), q:q, limit:"20" });
  const r = await fetch('/api/articles?'+params.toString(), {cache:'no-store'});
  if(!r.ok) return;
  const data = await r.json();
  const list = document.getElementById('list');
  if(reset) list.innerHTML = "";
  const items = data.articles||[];
  if(items.length===0 && page===1){ list.innerHTML = `<div class="card empt">표시할 기사가 없습니다.</div>`; document.getElementById('more').style.display='none'; return; }
  items.forEach(a=> list.insertAdjacentHTML('beforeend', row(a)));
  document.getElementById('more').style.display = items.length<20 ? 'none':'inline-block';
}

function loadMore(){ page += 1; fetchPage(false); }

document.getElementById('q').addEventListener('keydown', e=>{
  if(e.key==='Enter'){ q = e.target.value.trim(); page=1; fetchPage(true); }
});

// --- 자동 집계: 로그인 세션에서 5분마다 수집 + 새로고침 ---
async function pulse(){ try{ await fetch('/pulse', {method:'POST'}); }catch(_){} }
setInterval(()=>{ pulse(); if(autoload){ page=1; fetchPage(true);} }, 300000); // 5분
fetchPage(true); pulse();
</script>
{% endif %}
</body>
</html>
"""

# ---------------------- 라우트 ----------------------
@app.get("/")
def index():
    err = request.args.get("err")
    return render_template_string(PAGE, error=bool(err))

@app.post("/login")
def login():
    pw = request.form.get("pw","")
    if pw == SITE_PASSWORD:
        session["ok"] = True
        return redirect(url_for("index"))
    return redirect(url_for("index", err=1))

@app.post("/logout")
def logout():
    session.clear()
    return ("", 204)

@app.get("/api/articles")
def api_articles():
    if not session.get("ok"):
        return jsonify({"error":"locked"}), 401
    limit = max(1, min(50, int(request.args.get("limit", "20"))))
    page  = max(1, int(request.args.get("page", "1")))
    q     = (request.args.get("q") or "").strip()

    offset = (page - 1) * limit
    conn = get_db()
    c = conn.cursor()
    if q:
        c.execute("""
          SELECT title, link, source, published_at, summary
          FROM articles
          WHERE title LIKE ? OR source LIKE ?
          ORDER BY datetime(published_at) DESC, id DESC
          LIMIT ? OFFSET ?
        """, (f"%{q}%", f"%{q}%", limit, offset))
    else:
        c.execute("""
          SELECT title, link, source, published_at, summary
          FROM articles
          ORDER BY datetime(published_at) DESC, id DESC
          LIMIT ? OFFSET ?
        """, (limit, offset))
    rows = [dict(r) for r in c.fetchall()]
    conn.close()

    # 제목 한국어 번역을 응답에 동적으로 포함
    out = []
    for r in rows:
        r["title_ko"] = translate_title_ko(r["title"], r.get("summary") or "")
        out.append(r)
    return jsonify({"articles": out})

@app.get("/admin/fetch")
def admin_fetch():
    pw = request.args.get("pw","")
    if pw != ADMIN_PASSWORD:
        return jsonify({"error":"locked"}), 401
    info = fetch_once()
    total = count_rows()
    return jsonify({"ok": True, **info, "total": total})

@app.post("/pulse")
def pulse():
    """로그인된 세션에서만 호출 가능. 5분마다 짧게 수집해서 누적."""
    if not session.get("ok"):
        return jsonify({"error":"locked"}), 401
    info = fetch_once(max_per_feed=10)
    return jsonify({"ok": True, **info})

@app.get("/cron")
def cron():
    """외부 크론(예: Render Jobs/uptime cron)에서 호출: ?token=비번"""
    token = request.args.get("token","")
    if token != ADMIN_PASSWORD:
        return jsonify({"error":"locked"}), 401
    info = fetch_once()
    return jsonify({"ok": True, **info, "total": count_rows()})

# ---------------------- 실행 ----------------------
if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5000)))
