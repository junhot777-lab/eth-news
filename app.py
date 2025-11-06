import os
import sqlite3
import threading
import time
from datetime import datetime, timezone
from html import escape

import feedparser
import requests
from bs4 import BeautifulSoup
from flask import Flask, jsonify, request, session, redirect, url_for, make_response

# -----------------------------
# Config
# -----------------------------
PASSWORD = "Philia12"
DB_PATH = "/tmp/news.db"  # Render 무료 플랜에서 안전한 쓰기 경로
FETCH_INTERVAL_SEC = 60 * 60  # 1시간마다 백그라운드 수집
MAX_ROWS = 5000  # 오래된 기사 자동 정리 상한
USER_AGENT = "Mozilla/5.0 (compatible; EthNewsBot/1.0; +https://eth-news.onrender.com)"

# RSS 소스: 글로벌 + 한국(조선일보는 구글뉴스 RSS로 안전 접근)
RSS_SOURCES = [
    # 글로벌
    ("CoinDesk", "https://www.coindesk.com/arc/outboundfeeds/rss/"),
    ("Decrypt", "https://decrypt.co/feed"),
    ("Cointelegraph", "https://cointelegraph.com/rss"),
    # 한국(이더리움/암호화폐/가상화폐/블록체인 키워드)
    ("Chosun", "https://news.google.com/rss/search?q=site:chosun.com+(%EC%9D%B4%EB%8D%94%EB%A6%AC%EC%9B%80+OR+%EC%95%94%ED%98%B8%ED%99%94%ED%8F%90+OR+%EA%B0%80%EC%83%81%ED%99%94%ED%8F%90+OR+%EB%B8%94%EB%A1%9D%EC%B2%B4%EC%9D%B8)&hl=ko&gl=KR&ceid=KR:ko"),
]

KEYWORDS = [
    "ethereum", "eth", "vitalik",
    "이더리움", "ETH", "암호화폐", "가상화폐", "블록체인", "디파이", "레어룬"
]

app = Flask(__name__)
app.secret_key = os.environ.get("FLASK_SECRET", os.urandom(16))

# -----------------------------
# DB helpers
# -----------------------------
def get_conn():
    return sqlite3.connect(DB_PATH, timeout=10, isolation_level=None)

def init_db():
    os.makedirs("/tmp", exist_ok=True)
    with get_conn() as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS articles (
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              title TEXT NOT NULL,
              link TEXT NOT NULL UNIQUE,
              source TEXT,
              pub_ts INTEGER,
              created_ts INTEGER DEFAULT (strftime('%s','now')),
              summary TEXT
            );
        """)
        conn.execute("CREATE INDEX IF NOT EXISTS idx_pub_ts ON articles(pub_ts DESC);")

def prune_old(conn):
    # MAX_ROWS 초과분 삭제
    cur = conn.execute("SELECT COUNT(*) FROM articles;")
    total = cur.fetchone()[0] or 0
    if total > MAX_ROWS:
        to_delete = total - MAX_ROWS
        conn.execute(
            "DELETE FROM articles WHERE id IN (SELECT id FROM articles ORDER BY pub_ts ASC, id ASC LIMIT ?);",
            (to_delete,)
        )

def _clean_text(txt: str) -> str:
    if not txt:
        return ""
    # 간단 태그 제거
    soup = BeautifulSoup(txt, "html.parser")
    return " ".join(soup.get_text(" ").split())

def _has_keyword(text: str) -> bool:
    t = text.lower()
    return any(k.lower() in t for k in KEYWORDS)

def fetch_once() -> dict:
    """RSS에서 기사 수집하고 DB에 upsert. 안전하고 가벼운 구현."""
    added = 0
    total_seen = 0
    headers = {"User-Agent": USER_AGENT, "Accept": "text/html,application/xhtml+xml"}

    with get_conn() as conn:
        for source, url in RSS_SOURCES:
            try:
                # feedparser는 내부적으로 요청을 하므로 여기서는 그대로 사용
                feed = feedparser.parse(url)
                for e in feed.entries[:100]:  # 각 피드에서 최신 100개만 본다
                    total_seen += 1
                    title = _clean_text(getattr(e, "title", ""))
                    link = getattr(e, "link", "")
                    summary = _clean_text(getattr(e, "summary", getattr(e, "description", "")))

                    if not title or not link:
                        continue

                    # 조선 등 일부 링크는 원문이 리다이렉트/추가 파라미터 있을 수 있으니 정리
                    link = link.strip()

                    # 키워드 필터(타이틀+요약 기준)
                    if not _has_keyword(f"{title} {summary}"):
                        continue

                    # 발행 시각 파싱(없으면 지금)
                    pub_ts = int(time.time())
                    if hasattr(e, "published_parsed") and e.published_parsed:
                        pub_ts = int(time.mktime(e.published_parsed))

                    try:
                        conn.execute(
                            "INSERT OR IGNORE INTO articles(title, link, source, pub_ts, summary) VALUES(?,?,?,?,?)",
                            (title, link, source, pub_ts, summary[:400])
                        )
                        added += conn.total_changes  # IGNORE 된 경우 0 증가
                    except sqlite3.Error:
                        # 유니크 충돌 등은 무시
                        pass
            except Exception:
                # 소스 하나가 망가져도 전체 중단하지 않음
                continue

        prune_old(conn)

    return {"ok": True, "added": added, "total_seen": total_seen}

# -----------------------------
# Background scheduler (lightweight)
# -----------------------------
def schedule_loop():
    # 부팅 직후 한 번
    try:
        fetch_once()
    except Exception:
        pass

    # 이후 주기적
    while True:
        time.sleep(FETCH_INTERVAL_SEC)
        try:
            fetch_once()
        except Exception:
            # 실패해도 다음 턴에 다시 시도
            pass

# Render 무료 인스턴스는 재부팅이 잦으므로 데몬 스레드에 올려둔다
def start_scheduler_once():
    t = threading.Thread(target=schedule_loop, daemon=True)
    t.start()

# -----------------------------
# Auth
# -----------------------------
def logged_in() -> bool:
    return session.get("authed") is True

@app.route("/admin/login", methods=["GET", "POST"])
def admin_login():
    if request.method == "POST":
        pw = request.form.get("pw", "")
        if pw == PASSWORD:
            session["authed"] = True
            # 로그인 후 즉시 한 번 수집
            try:
                fetch_once()
            except Exception:
                pass
            return redirect(url_for("index"))
        else:
            return make_response("""
                <meta charset="utf-8"><body style="background:#111;color:#eee;font-family:system-ui">
                <h3>Wrong password</h3>
                <a href="/admin/login" style="color:#7dd3fc">Try again</a>
                </body>
            """, 401)

    return """
    <meta charset="utf-8" />
    <body style="background:#0b0f12;color:#e5e7eb;font-family:system-ui;display:grid;place-items:center;height:100vh">
      <form method="post" style="background:#10151a;padding:24px 28px;border-radius:12px;width:320px;box-shadow:0 8px 24px rgba(0,0,0,.4)">
        <h2 style="margin:0 0 16px 0">Admin Login</h2>
        <label style="font-size:14px;opacity:.85">Password</label>
        <input type="password" name="pw" autofocus style="width:100%;margin-top:6px;padding:10px 12px;border-radius:8px;border:1px solid #334155;background:#0b0f12;color:#e5e7eb"/>
        <button style="margin-top:14px;width:100%;padding:10px 12px;border:0;border-radius:8px;background:#3b82f6;color:white">Login</button>
      </form>
    </body>
    """

@app.route("/admin/logout")
def admin_logout():
    session.clear()
    return redirect(url_for("index"))

@app.route("/admin/fetch")
def admin_fetch():
    if not logged_in():
        return redirect(url_for("admin_login"))
    res = fetch_once()
    return jsonify(res)

# -----------------------------
# API
# -----------------------------
@app.route("/api/articles")
def api_articles():
    try:
        limit = max(1, min(50, int(request.args.get("limit", 20))))
    except ValueError:
        limit = 20
    try:
        offset = max(0, int(request.args.get("offset", 0)))
    except ValueError:
        offset = 0

    q = request.args.get("q", "").strip()

    with get_conn() as conn:
        if q:
            qlike = f"%{q}%"
            cur = conn.execute(
                """
                SELECT title, link, source, pub_ts, summary
                FROM articles
                WHERE (title LIKE ? OR source LIKE ?)
                ORDER BY pub_ts DESC, id DESC
                LIMIT ? OFFSET ?;
                """,
                (qlike, qlike, limit, offset)
            )
        else:
            cur = conn.execute(
                """
                SELECT title, link, source, pub_ts, summary
                FROM articles
                ORDER BY pub_ts DESC, id DESC
                LIMIT ? OFFSET ?;
                """,
                (limit, offset)
            )
        rows = cur.fetchall()

    articles = []
    for title, link, source, pub_ts, summary in rows:
        dt = datetime.fromtimestamp(pub_ts, tz=timezone.utc).astimezone()
        articles.append({
            "title": title,
            "link": link,
            "source": source or "",
            "published": dt.strftime("%Y-%m-%d %H:%M:%S"),
            "summary": summary or ""
        })
    return jsonify({"articles": articles})

# -----------------------------
# UI (초경량, 이더 배경, 무한 스크롤 디바운스, 로그아웃 버튼)
# -----------------------------
@app.route("/")
def index():
    authed = logged_in()
    logout_html = ("""
      <a href="/admin/logout"
         style="padding:8px 10px;border:1px solid #334155;border-radius:8px;color:#e5e7eb;text-decoration:none;background:rgba(17,24,39,.4)">
        로그아웃
      </a>
    """) if authed else ("""
      <a href="/admin/login"
         style="padding:8px 10px;border:1px solid #334155;border-radius:8px;color:#e5e7eb;text-decoration:none;background:rgba(17,24,39,.4)">
        관리자 로그인
      </a>
    """)

    return f"""
<!doctype html>
<meta charset="utf-8">
<title>이더리움 실시간 뉴스 집계</title>
<meta name="viewport" content="width=device-width, initial-scale=1">
<style>
  :root {{
    --card: rgba(16,21,26,.85);
    --text: #e5e7eb;
    --muted: #94a3b8;
    --accent: #67e8f9;
  }}
  html,body {{height:100%;margin:0;font-family:system-ui, -apple-system, Segoe UI, Roboto, 'Noto Sans KR', sans-serif;background:#0b0f12;color:var(--text);}}
  /* 초경량 '이더 배경' 패턴 */
  body {{
    background:
      radial-gradient(ellipse at top, rgba(94,234,212,.06), transparent 60%),
      radial-gradient(ellipse at bottom, rgba(14,165,233,.06), transparent 60%),
      repeating-linear-gradient(45deg, rgba(148,163,184,.06) 0 2px, transparent 2px 10px),
      #0b0f12;
  }}
  .wrap {{max-width:960px;margin:24px auto;padding:0 16px}}
  h1 {{margin:0 0 4px;font-size:28px}}
  .hint {{color:var(--muted);font-size:13px;margin-bottom:12px}}
  .topbar {{display:flex;gap:10px;align-items:center;justify-content:space-between}}
  .search {{flex:1;padding:10px 12px;border-radius:10px;border:1px solid #334155;background:#0b0f12;color:var(--text)}}
  .btn-more {{display:block;width:100%;margin:18px 0;padding:10px 12px;border:1px solid #334155;border-radius:10px;background:#10151a;color:var(--text)}}
  .list {{display:flex;flex-direction:column;gap:14px;margin-top:14px}}
  .card {{background:var(--card);border:1px solid #1f2937;border-radius:14px;padding:14px 16px;box-shadow:0 8px 24px rgba(0,0,0,.25)}}
  .title a {{color:#93c5fd;text-decoration:none}}
  .meta {{color:var(--muted);font-size:12px;margin:4px 0 8px}}
  .dot {{display:inline-block;width:6px;height:6px;border-radius:3px;background:#22d3ee;margin-right:6px}}
</style>

<div class="wrap">
  <div class="topbar">
    <div>
      <h1>이더리움 실시간 뉴스 집계</h1>
      <div class="hint">최신순 · 자동 집계(부팅/매시간) · 관리자 페이지에서 수동 갱신 가능</div>
    </div>
    <div>{logout_html}</div>
  </div>

  <input id="q" class="search" placeholder="제목/매체로 검색 (Enter)" />

  <div id="list" class="list"></div>
  <button id="more" class="btn-more">더 보기</button>
</div>

<script>
  const list = document.getElementById('list');
  const more = document.getElementById('more');
  const q = document.getElementById('q');

  let offset = 0;
  const limit = 12;
  let loading = false;
  let ended = false;
  let lastQuery = "";

  function esc(s) {{
    return (s || "").replace(/[&<>"']/g, m => ({{'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}}[m]));
  }}

  function cardHTML(a) {{
    return `
      <div class="card">
        <div class="title"><a href="${{esc(a.link)}}" target="_blank" rel="noopener noreferrer">${{esc(a.title)}}</a></div>
        <div class="meta"><span class="dot"></span>${{esc(a.source || '')}} · ${{esc(a.published)}}</div>
        ${{
          a.summary ? `<div style="color:#cbd5e1;font-size:13px;line-height:1.4">${{esc(a.summary)}}</div>` : ''
        }}
      </div>`;
  }}

  async function load(reset=false) {{
    if (loading || ended) return;
    loading = true;
    if (reset) {{
      offset = 0; ended = false; list.innerHTML = "";
    }}
    const params = new URLSearchParams({{ limit, offset }});
    if (lastQuery) params.append('q', lastQuery);
    const res = await fetch('/api/articles?' + params.toString());
    const data = await res.json();
    const items = data.articles || [];
    if (items.length === 0) {{
      ended = true;
      more.style.display = 'none';
    }} else {{
      items.forEach(a => list.insertAdjacentHTML('beforeend', cardHTML(a)));
      offset += items.length;
      more.style.display = 'block';
    }}
    loading = false;
  }}

  more.addEventListener('click', () => load(false));

  q.addEventListener('keydown', (e) => {{
    if (e.key === 'Enter') {{
      lastQuery = q.value.trim();
      load(true);
    }}
  }});

  // 무한 스크롤 디바운스: 서버 폭격 방지
  let scrollTimer;
  window.addEventListener('scroll', () => {{
    clearTimeout(scrollTimer);
    scrollTimer = setTimeout(() => {{
      const nearBottom = window.innerHeight + window.scrollY >= document.body.offsetHeight - 200;
      if (nearBottom) load(false);
    }}, 500);
  }});

  load(true); // 초기 로드
</script>
"""

# -----------------------------
# App start
# -----------------------------
init_db()
start_scheduler_once()

# Render에서는 gunicorn이 app:app 형태로 실행
# 로컬 테스트용
if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5000)))
