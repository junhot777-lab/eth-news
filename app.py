# app.py  ── ETH 뉴스 수집 뷰어 (최신순, 무한 스크롤, 로그인/로그아웃 OK, 경량)
import os
import sqlite3
import time
from datetime import datetime, timezone
from urllib.parse import urlparse

import requests
from flask import Flask, jsonify, request, make_response, redirect, Response

# ─────────────────────────── 기본 설정 ───────────────────────────
app = Flask(__name__)

ADMIN_PW = "Philia12"
ADMIN_COOKIE = "admin"           # 값 "1"이면 관리자
DB_PATH = os.path.join("/tmp", "ethnews.db")  # Render free 한도 고려해서 /tmp 사용

# ─────────────────────────── DB 유틸 ───────────────────────────
def get_db():
    conn = sqlite3.connect(DB_PATH, timeout=5, isolation_level=None)
    conn.row_factory = sqlite3.Row
    return conn

def init_db():
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    conn = get_db()
    conn.execute("""
        CREATE TABLE IF NOT EXISTS articles(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            title TEXT NOT NULL,
            link  TEXT NOT NULL UNIQUE,
            source TEXT,
            pub_ts INTEGER NOT NULL
        );
    """)
    conn.execute("CREATE INDEX IF NOT EXISTS idx_articles_pub ON articles(pub_ts DESC);")
    conn.close()

init_db()

# ─────────────────────────── 수집 소스 ───────────────────────────
# 가볍게: RSS/Atom류 위주. (HTML 본문 긁지 않음)
SOURCES = [
    # CoinDesk ETH 태그
    "https://www.coindesk.com/arc/outboundfeeds/rss/?outputType=xml&tag=Ethereum",
    # Decrypt ETH
    "https://decrypt.co/feed",
    # Cointelegraph ETH
    "https://cointelegraph.com/rss",
]

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Render/eth-news; +https://eth-news.onrender.com)"
}

def as_epoch(dt_str):
    # RSS 날짜 문자열을 epoch로 best-effort 변환
    try:
        from email.utils import parsedate_to_datetime
        return int(parsedate_to_datetime(dt_str).astimezone(timezone.utc).timestamp())
    except Exception:
        return int(time.time())

def normalize_source(link:str, fallback:str=""):
    try:
        host = urlparse(link).hostname or ""
        return fallback or host.replace("www.", "")
    except Exception:
        return fallback or "unknown"

# ─────────────────────────── 관리자: 수집 트리거 ───────────────────────────
@app.get("/admin/fetch")
def admin_fetch():
    # 쿼리스트링 pw 검사
    pw = request.args.get("pw", "")
    if pw != ADMIN_PW and request.cookies.get(ADMIN_COOKIE) != "1":
        return jsonify({"error": "locked"}), 401

    added = 0
    conn = get_db()
    try:
        for feed in SOURCES:
            try:
                r = requests.get(feed, headers=HEADERS, timeout=8)
                if r.status_code != 200 or not r.text:
                    continue
                text = r.text

                # 매우 경량 파서: title/link/pubDate만 뽑는다
                # 진짜 파서는 feedparser가 편하지만 의존 줄여 메모리/속도 절약
                # item 단위로 잘라서 최소 파싱
                parts = text.split("<item")
                if len(parts) == 1:
                    parts = text.split("<entry")
                for raw in parts[1:]:
                    seg = raw.split("</item>", 1)[0] if "</item>" in raw else raw.split("</entry>",1)[0]
                    # title
                    t1 = seg.split("<title",1)
                    title = ""
                    if len(t1) > 1:
                        t2 = t1[1].split(">",1)
                        if len(t2) > 1:
                            title = t2[1].split("</title>",1)[0].strip()
                    # link
                    link = ""
                    if "<link>" in seg:
                        link = seg.split("<link>",1)[1].split("</link>",1)[0].strip()
                    elif "href=" in seg:  # atom 형식
                        lk = seg.split("href=",1)[1].split('"',2)
                        if len(lk) >= 2:
                            link = lk[1].strip()
                    # pubDate / updated
                    pub_ts = int(time.time())
                    if "<pubDate>" in seg:
                        pub_ts = as_epoch(seg.split("<pubDate>",1)[1].split("</pubDate>",1)[0].strip())
                    elif "<updated>" in seg:
                        pub_ts = as_epoch(seg.split("<updated>",1)[1].split("</updated>",1)[0].strip())

                    if not title or not link:
                        continue

                    source = normalize_source(link)
                    try:
                        conn.execute(
                            "INSERT INTO articles(title, link, source, pub_ts) VALUES(?,?,?,?)",
                            (title, link, source, pub_ts)
                        )
                        added += 1
                    except sqlite3.IntegrityError:
                        # 이미 있는 링크는 스킵
                        pass
            except Exception:
                # 개별 소스 실패는 전체 실패로 안 번지게 무시
                pass
    finally:
        conn.close()

    return jsonify({"ok": True, "added": added, "total": count_articles()})

def count_articles():
    conn = get_db()
    try:
        cur = conn.execute("SELECT COUNT(*) AS c FROM articles")
        return int(cur.fetchone()["c"])
    finally:
        conn.close()

# ─────────────────────────── API: 무한 스크롤 ───────────────────────────
@app.get("/api/articles")
def api_articles():
    try:
        limit = max(1, min(50, int(request.args.get("limit", "20"))))
    except Exception:
        limit = 20
    try:
        offset = max(0, int(request.args.get("offset", "0")))
    except Exception:
        offset = 0

    q = request.args.get("q", "").strip()

    conn = get_db()
    try:
        if q:
            rows = conn.execute(
                "SELECT title, link, source, pub_ts FROM articles "
                "WHERE title LIKE ? ORDER BY pub_ts DESC LIMIT ? OFFSET ?",
                (f"%{q}%", limit, offset)
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT title, link, source, pub_ts FROM articles "
                "ORDER BY pub_ts DESC LIMIT ? OFFSET ?",
                (limit, offset)
            ).fetchall()

        data = []
        for r in rows:
            data.append({
                "title": r["title"],
                "link": r["link"],
                "source": r["source"],
                "pub_ts": r["pub_ts"],
                "pub_iso": datetime.fromtimestamp(r["pub_ts"], tz=timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
            })
        return jsonify({"articles": data, "next_offset": offset + len(data)})
    finally:
        conn.close()

# ─────────────────────────── 로그인/로그아웃 ───────────────────────────
@app.post("/admin/login")
def admin_login():
    pw = (request.json or {}).get("pw", "")
    if pw == ADMIN_PW:
        resp = make_response(jsonify({"ok": True}))
        resp.set_cookie(ADMIN_COOKIE, "1", httponly=True, samesite="Lax", max_age=60*60*6)
        return resp
    return jsonify({"ok": False}), 401

@app.route("/admin/logout", methods=["GET", "POST"])
def admin_logout():
    resp = make_response(redirect("/"))
    resp.delete_cookie(ADMIN_COOKIE)
    return resp

# ─────────────────────────── 프런트 (최신순 무한 쌓기 + 로그아웃 버튼) ───────────────────────────
@app.get("/")
def index():
    # 간단한 정적 HTML. 링크는 target=_blank로 확실히 클릭됨.
    html = f"""<!doctype html>
<html lang="ko">
<head>
<meta charset="utf-8" />
<meta name="viewport" content="width=device-width, initial-scale=1" />
<title>이더리움 실시간 뉴스 집계</title>
<style>
  :root {{
    --bg: #0d0f12;
    --card: #1a1f29;
    --text: #dfe6ef;
    --muted: #9fb0c3;
    --accent: #7cc4ff;
  }}
  * {{ box-sizing: border-box; }}
  body {{
    margin: 0;
    background: var(--bg);
    color: var(--text);
    font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, Pretendard, Apple SD Gothic Neo, 'Noto Sans KR', sans-serif;
  }}

  .top {{
    position: sticky;
    top: 0; z-index: 9999;
    background: linear-gradient(180deg, rgba(13,15,18,.95), rgba(13,15,18,.85));
    backdrop-filter: blur(6px);
    padding: 14px 12px;
    display: flex; gap: 8px; align-items: center; justify-content: space-between;
    border-bottom: 1px solid rgba(255,255,255,.05);
  }}
  h1 {{ font-size: 20px; margin: 0; }}
  .muted {{ color: var(--muted); font-size: 12px; }}
  #q {{
    flex: 1; max-width: 640px;
    height: 36px; padding: 0 12px; border-radius: 9px;
    background: #0f141a; color: var(--text);
    outline: none; border: 1px solid rgba(255,255,255,.08);
  }}
  #logoutBtn {{
    height: 36px; padding: 0 12px; border-radius: 9px;
    border: 1px solid rgba(255,255,255,.15); color: #fff;
    background: #2b3340; cursor: pointer; z-index: 10000;
  }}

  .wrap {{ max-width: 980px; margin: 0 auto; padding: 12px; }}
  .card {{
    background: var(--card);
    border: 1px solid rgba(255,255,255,.06);
    border-radius: 14px;
    padding: 14px;
    margin: 12px 0;
  }}
  .title {{
    font-size: 20px; margin: 0 0 8px 0;
  }}
  .title a {{ color: #b9d8ff; text-decoration: none; }}
  .meta {{ font-size: 12px; color: var(--muted); margin-bottom: 8px; }}
  .loader {{ text-align:center; padding: 18px; color: var(--muted); }}
</style>
</head>
<body>
  <div class="top">
    <div>
      <h1>이더리움 실시간 뉴스 집계</h1>
      <div class="muted">최신순 · 아래 '더 보기'로 과거 기사 계속 누적</div>
    </div>
    <input id="q" placeholder="제목/매체로 검색 (Enter)" />
    <button id="logoutBtn" type="button">로그아웃</button>
  </div>

  <div class="wrap">
    <div id="list"></div>
    <div id="more" class="loader">불러오는 중...</div>
  </div>

<script>
let offset = 0;
let done = false;
let loading = false;
let query = "";

const list = document.getElementById('list');
const more = document.getElementById('more');
const q = document.getElementById('q');

function card(item){{
  const d = new Date(item.pub_ts * 1000);
  const when = d.toISOString().replace('T',' ').slice(0,19) + " UTC";
  return `
    <div class="card">
      <h2 class="title"><a href="${{item.link}}" target="_blank" rel="noopener">${{item.title}}</a></h2>
      <div class="meta">${{item.source}} | ${{when}}</div>
    </div>
  `;
}}

async function load() {{
  if (done || loading) return;
  loading = true;
  more.textContent = "불러오는 중...";
  try {{
    const url = `/api/articles?limit=20&offset=${{offset}}` + (query ? `&q=${{encodeURIComponent(query)}}` : "");
    const res = await fetch(url);
    const js = await res.json();
    if (js.articles && js.articles.length) {{
      list.insertAdjacentHTML('beforeend', js.articles.map(card).join(''));
      offset = js.next_offset ?? (offset + js.articles.length);
      more.textContent = "아래로 스크롤하면 더 보기";
    }} else {{
      done = true;
      more.textContent = "더 이상 항목 없음";
    }}
  }} catch (e) {{
    more.textContent = "불러오기 실패";
  }} finally {{
    loading = false;
  }}
}}

function nearBottom(){{
  return window.innerHeight + window.scrollY >= document.body.offsetHeight - 600;
}}

window.addEventListener('scroll', ()=>{{ if(nearBottom()) load(); }});
window.addEventListener('load', load);

q.addEventListener('keydown', (ev)=>{{
  if(ev.key === 'Enter'){{
    query = q.value.trim();
    list.innerHTML = "";
    offset = 0; done = false;
    load();
  }}
}});

// 로그아웃 눌리게 확실히
document.getElementById('logoutBtn').addEventListener('click', async (e) => {{
  e.preventDefault(); e.stopPropagation();
  try {{
    const r = await fetch('/admin/logout', {{method:'POST'}});
    if(!r.ok) await fetch('/admin/logout', {{method:'GET'}});
  }} catch(_){{
  }}
  location.replace('/');
}});
</script>
</body>
</html>
"""
    return Response(html, mimetype="text/html")

# ─────────────────────────── WSGI 엔트리 ───────────────────────────
if __name__ == "__main__":
    # 로컬 테스트용
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", "8000")))
