import os
import sqlite3
from datetime import datetime
from urllib.parse import urlparse

from flask import Flask, request, jsonify, render_template_string, redirect, url_for, session, abort

# -----------------------------------------------------------------------------
# 기본 설정
# -----------------------------------------------------------------------------
DB_PATH = os.path.join("/tmp", "news.db")  # Render 무료 인스턴스 안전지대
os.makedirs("/tmp", exist_ok=True)

app = Flask(__name__)
app.secret_key = os.environ.get("FLASK_SECRET", "change-this")

SITE_PASSWORD = os.environ.get("SITE_PASSWORD", "Philia12")  # 요청한 기본값

# -----------------------------------------------------------------------------
# DB 유틸
# -----------------------------------------------------------------------------
def get_db():
    conn = sqlite3.connect(DB_PATH, timeout=10, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    return conn

def init_db():
    conn = get_db()
    c = conn.cursor()
    c.execute(
        """
        CREATE TABLE IF NOT EXISTS articles(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            title TEXT NOT NULL,
            link  TEXT NOT NULL UNIQUE,
            source TEXT,
            published_at TEXT,
            summary TEXT
        )
        """
    )
    c.execute("CREATE INDEX IF NOT EXISTS idx_articles_published ON articles(published_at DESC)")
    conn.commit()
    conn.close()

init_db()

# -----------------------------------------------------------------------------
# 템플릿 (이더리움 배경 + 이미지 깨짐 방지)
# - 외부 이미지 <img> 삽입하지 않음
# - 요약은 텍스트만, 링크는 새 창
# -----------------------------------------------------------------------------
PAGE = r"""
<!doctype html>
<html lang="ko">
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width,initial-scale=1" />
  <title>이더리움 실시간 뉴스 집계</title>
  <style>
    :root{
      --bg1:#0b0f17; --bg2:#101826; --card:#121b2a; --muted:#8aa0bf; --accent:#66d9ff;
      --border:#213047; --text:#e6f0ff;
    }
    html,body{height:100%}
    body{
      margin:0; color:var(--text); font-family:system-ui,-apple-system,Segoe UI,Roboto,Apple SD Gothic Neo,Noto Sans KR,sans-serif;
      background:
        radial-gradient(1200px 600px at 80% -10%, rgba(102,217,255,.12), transparent 60%),
        radial-gradient(900px 500px at -10% 20%, rgba(95,158,255,.10), transparent 55%),
        linear-gradient(160deg, var(--bg1), var(--bg2));
    }
    /* 이더리움 로고 패턴 (작은 SVG를 data URI로 반복) */
    body::before{
      content:"";
      position:fixed; inset:0; pointer-events:none; opacity:.05;
      background-image:url('data:image/svg+xml;utf8,<svg xmlns="http://www.w3.org/2000/svg" width="120" height="120" viewBox="0 0 120 120"><g fill="none" stroke="%23b5e3ff" stroke-width="1.2"><path d="M60 10 25 60l35-16 35 16z"/><path d="M60 10v94"/><path d="M60 104 25 60l35 16 35-16z"/></g></svg>');
      background-size:220px 220px;
    }
    .wrap{max-width:960px; margin:32px auto; padding:0 16px}
    h1{margin:0 0 6px; font-size:28px}
    .hint{color:var(--muted); font-size:14px; margin-bottom:14px}
    .bar{display:flex; gap:8px; margin:10px 0 16px}
    input[type="text"]{
      flex:1; padding:12px 14px; border-radius:12px; border:1px solid var(--border);
      background:#0f1624; color:var(--text); outline:none;
    }
    .btn{padding:10px 14px; border-radius:12px; border:1px solid var(--border); background:#0f1624; color:var(--text); cursor:pointer}
    .card{
      background:rgba(18,27,42,.76);
      border:1px solid var(--border);
      border-radius:16px; padding:18px; margin:14px 0;
      box-shadow:0 6px 20px rgba(0,0,0,.28);
      backdrop-filter: blur(3px);
    }
    .ttl{font-size:22px; line-height:1.35; color:#a9ceff; text-decoration:none}
    .meta{color:var(--muted); font-size:13px; margin:6px 0 10px}
    .sum{white-space:pre-wrap; line-height:1.6; color:#dce9ff}
    .pill{display:inline-block; padding:2px 8px; border:1px solid var(--border); border-radius:999px; color:#cfe6ff; font-size:12px}
    .empt{padding:28px; text-align:center; color:var(--muted)}
    .login{max-width:400px; margin:90px auto}
    .center{display:flex; gap:8px; align-items:center; justify-content:center}
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
      <div class="hint">최신순 · 아래 ‘더 보기’로 과거 기사 로드 · 요약은 영어 3줄</div>

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

<script>
let page = 1, q = "";
const list = document.getElementById('list');
const moreBtn = document.getElementById('more');
const qEl = document.getElementById('q');

function fmt(d){
  try{ return new Date(d).toLocaleString('ko-KR'); }catch(_){ return d; }
}

function row(a){
  const host = (()=>{ try{ return new URL(a.link).hostname.replace(/^www\./,""); }catch(_){ return a.source||"" }})();
  const sums = (a.summary||"").trim();
  return `
    <div class="card">
      <a class="ttl" href="${a.link}" target="_blank" rel="noopener noreferrer">${a.title}</a>
      <div class="meta">${host} | ${fmt(a.published_at)}</div>
      ${sums ? `<div class="sum">${sums}</div>` : ``}
    </div>`;
}

async function fetchPage(reset=false){
  const params = new URLSearchParams({ page, q, limit: 20 });
  const res = await fetch('/api/articles?'+params.toString());
  const data = await res.json();
  if(reset){ list.innerHTML = "" }
  const items = data.articles || [];
  if(items.length === 0 && page === 1){
    list.innerHTML = `<div class="card empt">표시할 기사가 없습니다.</div>`;
    moreBtn.style.display = 'none';
    return;
  }
  items.forEach(a => list.insertAdjacentHTML('beforeend', row(a)));
  moreBtn.style.display = items.length < 20 ? 'none' : 'inline-block';
}

function loadMore(){ page += 1; fetchPage(); }

qEl?.addEventListener('keydown', e=>{
  if(e.key === 'Enter'){
    q = qEl.value.trim();
    page = 1;
    fetchPage(true);
  }
});

async function logout(){
  await fetch('/logout', {method:'POST'});
  location.reload();
}

fetchPage();
</script>
</body>
</html>
"""

# -----------------------------------------------------------------------------
# 라우트
# -----------------------------------------------------------------------------
@app.route("/", methods=["GET"])
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
    page = max(1, int(request.args.get("page", "1")))
    q = (request.args.get("q") or "").strip()

    offset = (page - 1) * limit

    conn = get_db()
    c = conn.cursor()
    if q:
        c.execute(
            """
            SELECT title, link, source, published_at, summary
            FROM articles
            WHERE title LIKE ? OR source LIKE ?
            ORDER BY datetime(published_at) DESC, id DESC
            LIMIT ? OFFSET ?
            """,
            (f"%{q}%", f"%{q}%", limit, offset),
        )
    else:
        c.execute(
            """
            SELECT title, link, source, published_at, summary
            FROM articles
            ORDER BY datetime(published_at) DESC, id DESC
            LIMIT ? OFFSET ?
            """,
            (limit, offset),
        )
    rows = [dict(r) for r in c.fetchall()]
    conn.close()
    return jsonify({"articles": rows})

# -----------------------------------------------------------------------------
# 관리용 수동 수집 엔드포인트 (비번 쿼리)
# 참고: 크롤러/파서 부분은 기존 로직 유지한다고 가정
# -----------------------------------------------------------------------------
@app.get("/admin/fetch")
def admin_fetch():
    pw = request.args.get("pw","")
    if pw != SITE_PASSWORD:
        abort(403)

    # 여기선 데모용으로 articles 테이블이 비었으면 더미 1~2개를 채워
    # UI만 확인 가능하도록 한다. 실제 수집 로직은 기존 코드 사용.
    conn = get_db()
    c = conn.cursor()
    c.execute("SELECT COUNT(*) FROM articles")
    n = c.fetchone()[0]

    added = 0
    if n == 0:
        demo = [
            {
                "title":"Ethereum ecosystem outlook improves as ETH tests key ranges",
                "link":"https://example.org/eth-outlook",
                "source":"example.org",
                "published_at": datetime.utcnow().isoformat(timespec="seconds"),
                "summary":"• Market eyes catalysts around L2 activity\n• Stakers monitor yields vs risk\n• Dev roadmap stays the main narrative"
            },
            {
                "title":"Rollups post new TPS highs; fees drift lower",
                "link":"https://example.org/l2-fees",
                "source":"example.org",
                "published_at": datetime.utcnow().isoformat(timespec="seconds"),
                "summary":"• Throughput rose on major rollups\n• Fee pressure keeps trending down\n• Users migrate where UX is cheaper"
            },
        ]
        for a in demo:
            try:
                c.execute(
                    "INSERT OR IGNORE INTO articles(title,link,source,published_at,summary) VALUES(?,?,?,?,?)",
                    (a["title"], a["link"], a["source"], a["published_at"], a["summary"]),
                )
                added += c.rowcount
            except Exception:
                pass
        conn.commit()
    conn.close()
    return jsonify({"ok": True, "added": added})
# -----------------------------------------------------------------------------

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5000)))
