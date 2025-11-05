# app.py — ETH 뉴스 집계 (최신순 + 누적 로드 + 잠금 + 한국어 3줄요약)
import os, sqlite3, time, re, html, feedparser
from flask import Flask, jsonify, render_template_string, request, session
from apscheduler.schedulers.background import BackgroundScheduler

# --------- 기본 설정 ---------
ADMIN_PASSWORD = "Philia12"
SECRET_KEY = "Philia12-key"
DB_DIR = "/tmp/data"            # Render 무료 플랜: /tmp만 쓰기 가능
DB_PATH = os.path.join(DB_DIR, "articles.db")

FEEDS = [
    "https://cointelegraph.com/rss",
    "https://decrypt.co/feed",
    "https://www.coindesk.com/arc/outboundfeeds/rss/",
    "https://www.coindeskkorea.com/rss/allArticle.xml",  # KR
]
REQUEST_HEADERS = {"User-Agent": "Mozilla/5.0 (eth-news-render)"}

KEYWORDS = ["이더리움", "이더", "ethereum", " ether ", " eth ", "(eth)", "[eth]", "以太坊"]

app = Flask(__name__)
app.secret_key = SECRET_KEY

# --------- DB 유틸 ---------
def ensure_db():
    os.makedirs(DB_DIR, exist_ok=True)
    with sqlite3.connect(DB_PATH) as conn:
        c = conn.cursor()
        c.execute("""
        CREATE TABLE IF NOT EXISTS articles (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            title TEXT,
            link TEXT UNIQUE,
            source TEXT,
            published INTEGER,
            created INTEGER
        )
        """)
        conn.commit()

def query_sql(sql, params=()):
    with sqlite3.connect(DB_PATH) as conn:
        return conn.execute(sql, params).fetchall()

def insert_article(title, link, source, published_epoch):
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute("""
            INSERT OR IGNORE INTO articles (title, link, source, published, created)
            VALUES (?, ?, ?, ?, ?)
        """, (title, link, source, int(published_epoch), int(time.time())))
        conn.commit()

# --------- 요약기(한국어 3줄) ---------
_sentence_splitter = re.compile(r"[\.!\?。！？]|(?<=다)\.|(?<=요)\.|…")
_space = re.compile(r"\s+")

def _clean_text(t: str) -> str:
    if not t: return ""
    t = html.unescape(t)
    # RSS summary에 HTML 태그 섞여있으면 삭제
    t = re.sub(r"<[^>]+>", " ", t)
    t = _space.sub(" ", t).strip()
    return t

def summarize_ko(title: str, summary: str = "") -> list[str]:
    """
    외부 API 없이 가벼운 추출식 요약:
    - 제목 + 요약 텍스트 합쳐서 문장 분리
    - 너무 짧은/긴 문장 필터
    - 키워드 가중치로 상위 3개 선택
    """
    title = _clean_text(title)
    body = _clean_text(summary)
    base = (title + " . " + body).strip()

    if not base:
        return []

    # 문장 분리
    parts = [s.strip() for s in _sentence_splitter.split(base) if s and s.strip()]
    # 문장 전처리 & 길이 필터
    cand = [p for p in parts if 8 <= len(p) <= 140]

    if not cand:
        # 최소한 제목이라도 1줄
        return [title[:140]]

    # 스코어링: 키워드 매칭 + 길이 적당 가중치
    kws = [k.lower().strip() for k in KEYWORDS]
    scored = []
    for s in cand:
        ls = s.lower()
        score = 0
        for k in kws:
            if k and k in ls:
                score += 3
        # 숫자/단위 많이 포함되면(가격/비율) 가중치 살짝
        if re.search(r"\d", s): score += 1
        # 너무 긴 건 감점, 적당(20~100) 보너스
        L = len(s)
        if 20 <= L <= 100: score += 1
        elif L > 120: score -= 1
        scored.append((score, s))

    # 점수 내림차순 → 상위 3개
    scored.sort(key=lambda x: x[0], reverse=True)
    top = [s for _, s in scored[:3]]

    # 중복/유사 제거(앞부분 20자 기준)
    seen = set()
    dedup = []
    for s in top:
        key = s[:20]
        if key not in seen:
            dedup.append(s)
            seen.add(key)

    return dedup[:3]

# --------- 날짜 파싱 ---------
def to_epoch(entry) -> int:
    for key in ("published_parsed", "updated_parsed"):
        st = entry.get(key)
        if st:
            try:
                return int(time.mktime(st))
            except Exception:
                pass
    return int(time.time())

# --------- 수집 루틴 ---------
def match_keyword(title: str, summary: str = "") -> bool:
    t = f" {str(title).lower()} {str(summary).lower()} "
    return any(k.lower() in t for k in KEYWORDS)

def fetch_articles(max_per_feed=40):
    ensure_db()
    seen = set(r[0] for r in query_sql("SELECT link FROM articles"))
    scanned, added = 0, 0

    for url in FEEDS:
        try:
            fp = feedparser.parse(url, request_headers=REQUEST_HEADERS)
            src = fp.feed.get("title", url)
            for e in fp.entries[:max_per_feed]:
                scanned += 1
                title = e.get("title", "")
                link  = e.get("link")
                summary = e.get("summary", "")
                if not title or not link:
                    continue
                if match_keyword(title, summary) and link not in seen:
                    insert_article(title, link, src, to_epoch(e))
                    added += 1
        except Exception as ex:
            print(f"[FETCH] feed error {url} ->", ex)

    print(f"[FETCH] scanned={scanned} added={added} {time.strftime('%Y-%m-%d %H:%M:%S')}")

# --------- 잠금/인증 ---------
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

# --------- 최신순 + 누적 로드 API (요약 포함) ---------
@app.get("/api/articles")
def api_articles():
    if not session.get("unlocked"):
        return jsonify({"error": "locked"}), 403

    q = (request.args.get("q") or "").strip()
    before = request.args.get("before")    # 커서: 이 epoch보다 과거
    limit = int(request.args.get("limit") or 30)

    sql = "SELECT title, link, source, published FROM articles"
    conds, params = [], []

    if q:
        conds.append("(title LIKE ? OR source LIKE ?)")
        like = f"%{q}%"
        params += [like, like]

    if before:
        try:
            conds.append("published < ?")
            params.append(int(before))
        except Exception:
            pass

    if conds:
        sql += " WHERE " + " AND ".join(conds)

    sql += " ORDER BY published DESC, id DESC LIMIT ?"
    params.append(limit)

    rows = query_sql(sql, tuple(params))

    # API에서 바로 3줄 요약 생성
    items = []
    for title, link, source, published in rows:
        # RSS에서 다시 요약 얻기 위해 간단한 캐시/리패치 대신 제목 기반 요약 생성
        # (요약 품질을 올리고 싶으면 fetch 때 summary를 함께 DB에 저장하도록 확장 가능)
        summary_lines = summarize_ko(title)  # title만으로도 1~3줄
        items.append({
            "title": title,
            "link": link,
            "source": source or "",
            "published": int(published),
            "summary_ko": summary_lines,
        })
    return jsonify({"articles": items})

# --------- 강제 수집/상태 ---------
@app.get("/admin/fetch")
def admin_fetch():
    if request.args.get("pw") != ADMIN_PASSWORD:
        return jsonify({"ok": False, "error": "nope"}), 401
    before = query_sql("SELECT COUNT(*) FROM articles")[0][0]
    fetch_articles()
    after = query_sql("SELECT COUNT(*) FROM articles")[0][0]
    return jsonify({"ok": True, "added": after - before, "total": after})

@app.get("/admin/ping")
def admin_ping():
    total = query_sql("SELECT COUNT(*) FROM articles")[0][0]
    return jsonify({"ok": True, "total": total})

# --------- 페이지 ---------
@app.before_request
def ensure_on_each_request():
    try:
        ensure_db()
    except Exception as e:
        print("[DB INIT ERROR]", e)

@app.get("/")
def home():
    return render_template_string(HTML)

# --------- 부팅 시 1회 수집 + 주기 수집 ---------
print("[BOOT] init & first fetch…")
ensure_db()
fetch_articles()  # 시작 즉시 1회
sched = BackgroundScheduler()
sched.add_job(fetch_articles, "interval", minutes=30)
sched.start()

# --------- 프런트(최신순 + 누적 '더 보기' + 3줄요약) ---------
HTML = """
<!doctype html>
<html lang="ko"><head>
<meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>이더리움 뉴스 집계</title>
<style>
body{font-family:system-ui,Segoe UI,Noto Sans KR,Arial;background:#0b0d10;color:#e6e9ee;margin:0}
header{position:sticky;top:0;background:#0f1216;border-bottom:1px solid #1c2128;padding:16px}
.wrap{max-width:920px;margin:0 auto;padding:16px}
h1{font-size:20px;margin:0}.meta{opacity:.7;font-size:12px}
#q{width:100%;padding:8px 10px;border-radius:10px;border:1px solid #2a3240;background:#0b0f14;color:#e6e9ee;margin-top:8px}
.card{background:#11151a;border:1px solid #1f2630;border-radius:14px;padding:16px;margin:12px 0}
.card a{color:#9ecbff;text-decoration:none}.card a:hover{text-decoration:underline}
.badge{display:inline-block;padding:2px 8px;border:1px solid #2a3240;border-radius:999px;font-size:12px;opacity:.8}
.summary{margin-top:8px;line-height:1.5}
.summary div{opacity:.92}
.muted{opacity:.7}
.center{display:flex;justify-content:center;margin:10px 0}
button{padding:10px 14px;border-radius:10px;border:none;cursor:pointer;background:#1b2838;color:#e6e9ee}
.overlay{position:fixed;inset:0;background:rgba(3,5,7,.88);display:flex;align-items:center;justify-content:center;z-index:9999}
.lock{background:#0b1220;padding:24px;border-radius:12px;border:1px solid #22303a;min-width:320px}
.lock input{width:100%;padding:10px;border-radius:8px;border:1px solid #21303a;background:#081018;color:#e6eef8}
.lock button{margin-top:10px}
.err{color:#ff8b8b;margin-top:8px;font-size:13px}
</style>
</head>
<body>
<div id="overlay" class="overlay" style="display:none">
  <div class="lock">
    <h3>화면 잠금</h3>
    <p class="muted">관리자 비밀번호를 입력하세요.</p>
    <input id="pw" type="password" placeholder="비밀번호">
    <button onclick="doUnlock()">잠금 해제</button>
    <div id="err" class="err"></div>
  </div>
</div>

<header><div class="wrap">
  <h1>이더리움 실시간 뉴스 집계</h1>
  <div class="meta">최신순 • 아래 '더 보기'로 과거 기사 계속 누적 • 요약 3줄 표시</div>
  <input id="q" type="search" placeholder="제목/매체로 검색 (Enter)" />
</div></header>

<main class="wrap">
  <div id="list"></div>
  <div class="center"><button id="more" style="display:none;">더 보기</button></div>
  <div id="hint" class="muted center"></div>
</main>

<script>
let loading=false, lastCursor=null, currentQuery="";
const list=document.getElementById("list");
const more=document.getElementById("more");
const q=document.getElementById("q");
const hint=document.getElementById("hint");

async function checkLock(){
  const r=await fetch('/api/unlocked'); const j=await r.json();
  document.getElementById('overlay').style.display = j.unlocked ? 'none':'flex';
  if(j.unlocked){ load({append:false}); }
}

async function doUnlock(){
  const pw=document.getElementById('pw').value;
  const r=await fetch('/unlock',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({password:pw})});
  const j=await r.json();
  if(j.success){ document.getElementById('overlay').style.display='none'; load({append:false}); }
  else{ document.getElementById('err').textContent=j.error||'비밀번호 오류'; }
}

function renderItems(arr, append){
  if(!append) list.innerHTML='';
  for(const e of arr){
    const d=document.createElement('div'); d.className='card';
    const lines = (e.summary_ko||[]).map(s => `<div>• ${s.replace(/</g,"&lt;")}</div>`).join("");
    d.innerHTML = `
      <div style="display:flex;justify-content:space-between;gap:12px;align-items:center">
        <a href="${e.link}" target="_blank" rel="noopener">${e.title.replace(/</g,"&lt;")}</a>
        <span class="badge">${e.source||''}</span>
      </div>
      <div class="muted">${new Date(e.published*1000).toLocaleString()}</div>
      ${lines ? `<div class="summary">${lines}</div>` : ""}
    `;
    list.appendChild(d);
  }
}

async function load({append=false}={}){
  if(loading) return; loading=true; hint.textContent='';
  const params = new URLSearchParams();
  if(currentQuery) params.set('q', currentQuery);
  if(append && lastCursor) params.set('before', String(lastCursor));
  params.set('limit', '30');

  const r=await fetch('/api/articles?'+params.toString());
  if(r.status===403){ document.getElementById('overlay').style.display='flex'; loading=false; return; }
  const j=await r.json(); const arr=j.articles||[];

  renderItems(arr, append);

  if(arr.length>0){
    lastCursor = arr[arr.length-1].published; // 다음 페이지 커서
    more.style.display='inline-block';
  }else{
    if(!append){ hint.textContent='표시할 기사가 없습니다.'; }
    more.style.display='none';
  }
  loading=false;
}

more.addEventListener('click',()=>load({append:true}));
q.addEventListener('keydown',(e)=>{ if(e.key==='Enter'){ currentQuery=q.value.trim(); lastCursor=null; load({append:false}); }});
window.addEventListener('DOMContentLoaded', checkLock);
</script>
</body></html>
"""

# 로컬 디버그용
if __name__ == "__main__":
    app.run(host="0.0.0.0", port=8000)
