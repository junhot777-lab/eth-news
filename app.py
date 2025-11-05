import os
import sqlite3
import time
from datetime import datetime
from dateutil import parser as dtparse

from flask import Flask, jsonify, request, Response

import feedparser

# -------------------------------
# Paths & DB
# -------------------------------
DATA_DIR = "/tmp/data"
DB_PATH = os.path.join(DATA_DIR, "news.db")

os.makedirs(DATA_DIR, exist_ok=True)

def get_conn():
    conn = sqlite3.connect(DB_PATH, check_same_thread=False)
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS articles (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            title TEXT NOT NULL,
            link TEXT NOT NULL UNIQUE,
            source TEXT,
            published_at INTEGER,          -- epoch seconds
            summary TEXT                   -- light 3-line English summary
        )
        """
    )
    conn.execute("CREATE INDEX IF NOT EXISTS idx_articles_time ON articles(published_at DESC)")
    conn.commit()
    return conn

CONN = get_conn()

# -------------------------------
# Feeds to crawl (lightweight)
# -------------------------------
FEEDS = [
    # 코인/이더 관련 대표 피드 몇 개
    "https://www.coindesk.com/arc/outboundfeeds/rss/",
    "https://decrypt.co/feed",
    "https://cointelegraph.com/rss",
]

# -------------------------------
# Minimal 3-line English summary
# (no external APIs)
# -------------------------------
def lite_summary(title: str, desc: str) -> str:
    """
    Super-cheap summary: 3 short bullet lines.
    1) title trimmed
    2) first sentence from description
    3) another short fragment if available
    """
    def clean(t: str) -> str:
        return " ".join((t or "").replace("\n", " ").split())

    t = clean(title)[:140]

    # try to get description from feed
    d = clean(desc)
    # naive sentence split
    parts = [p.strip() for p in d.replace("•", ". ").split(".") if p.strip()]
    line2 = parts[0][:180] if parts else ""
    line3 = (parts[1][:180] if len(parts) > 1 else "")

    lines = [f"• {t}"]
    if line2:
        lines.append(f"• {line2}")
    if line3:
        lines.append(f"• {line3}")

    return "\n".join(lines[:3])

# -------------------------------
# Fetch logic
# -------------------------------
def parse_time(entry):
    # Try entry.published / updated / dc:date; fallback to now
    for key in ("published", "updated", "created"):
        val = getattr(entry, key, None)
        if val:
            try:
                return int(dtparse.parse(val).timestamp())
            except Exception:
                pass
    return int(time.time())

def save_article(title, link, source, published_at, summary):
    try:
        with CONN:
            CONN.execute(
                "INSERT OR IGNORE INTO articles (title, link, source, published_at, summary) VALUES (?, ?, ?, ?, ?)",
                (title, link, source, published_at, summary),
            )
    except Exception:
        # ignore row errors to keep fetch cheap
        pass

def fetch_once(max_per_feed: int = 15):
    for url in FEEDS:
        try:
            feed = feedparser.parse(url)
        except Exception:
            continue

        source = (feed.feed.get("title") or "Unknown").strip()

        count = 0
        for e in feed.entries:
            if count >= max_per_feed:
                break

            title = (getattr(e, "title", "") or "").strip()
            link = (getattr(e, "link", "") or "").strip()
            if not title or not link:
                continue

            desc = getattr(e, "summary", "") or getattr(e, "description", "") or ""
            pub = parse_time(e)
            summ = lite_summary(title, desc)

            save_article(title, link, source, pub, summ)
            count += 1

# -------------------------------
# Flask app
# -------------------------------
app = Flask(__name__)

@app.route("/")
def index():
    # Simple HTML (raw string). Keep it inside r""" ... """ to avoid syntax issues.
    html = r"""<!doctype html>
<html lang="ko">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>이더리움 실시간 뉴스 집계</title>
<style>
  :root { color-scheme: dark; }
  body { margin: 0; background:#0c0e11; color:#e6edf3; font-family: system-ui, -apple-system, Segoe UI, Roboto, sans-serif; }
  .wrap { max-width: 860px; margin: 32px auto; padding: 0 16px; }
  h1 { font-size: 24px; margin: 0 0 6px 0; }
  .hint { color:#9aa4ad; font-size: 13px; margin-bottom: 16px; }
  .search { width:100%; padding:12px 14px; border-radius:8px; border:1px solid #2a2f39; background:#0f1317; color:#e6edf3; outline:none; }
  .card { border:1px solid #2a2f39; background:#0f1317; border-radius:12px; padding:16px; margin:16px 0; }
  .title { font-size:20px; color:#9bd5ff; text-decoration:none; }
  .meta { margin-top:8px; color:#9aa4ad; font-size:12px; }
  .sum { white-space:pre-wrap; margin-top:12px; line-height:1.45; }
  .btn { display:block; width:100%; padding:12px; text-align:center; border:1px solid #2a2f39; background:#0f1317; color:#e6edf3;
         border-radius:10px; cursor:pointer; margin:18px 0; }
  .btn:hover { background:#131821; }
  .topbar { display:flex; gap:8px; align-items:center; margin:14px 0 10px; }
</style>
</head>
<body>
<div class="wrap">
  <h1>이더리움 실시간 뉴스 집계</h1>
  <div class="hint">최신순 • 아래 ‘더 보기’로 과거 기사 로드 • 요약은 영어 3줄</div>
  <div class="topbar">
    <input id="q" class="search" placeholder="제목/매체로 검색 (Enter)" />
  </div>
  <div id="list"></div>
  <button id="more" class="btn">더 보기</button>
</div>

<script>
let page = 0;
const PAGE_SIZE = 20;
let q = "";

const listEl = document.getElementById("list");
const btnMore = document.getElementById("more");
const qEl = document.getElementById("q");

qEl.addEventListener("keydown", (e) => {
  if (e.key === "Enter") {
    q = qEl.value.trim();
    page = 0;
    listEl.innerHTML = "";
    load(true);
  }
});

btnMore.addEventListener("click", () => load(false));

function fmt(ts) {
  const dt = new Date(ts * 1000);
  const y = dt.getFullYear();
  const m = String(dt.getMonth()+1).padStart(2,'0');
  const d = String(dt.getDate()).padStart(2,'0');
  const h = String(dt.getHours()).padStart(2,'0');
  const mi = String(dt.getMinutes()).padStart(2,'0');
  const s = String(dt.getSeconds()).padStart(2,'0');
  return `${y}.${m}.${d} ${h}:${mi}:${s}`;
}

async function load(reset) {
  const offset = page * PAGE_SIZE;
  const params = new URLSearchParams({ limit: PAGE_SIZE, offset: offset });
  if (q) params.set("q", q);

  const res = await fetch(`/api/articles?${params.toString()}`);
  const data = await res.json();

  const items = data.articles || [];
  if (reset) listEl.innerHTML = "";

  for (const a of items) {
    const card = document.createElement("div");
    card.className = "card";
    card.innerHTML = `
      <a class="title" href="${a.link}" target="_blank" rel="noopener noreferrer">${a.title}</a>
      <div class="meta">${a.source || ''} | ${fmt(a.published_at || Date.now()/1000)}</div>
      <div class="sum">${(a.summary || '').replaceAll('<','&lt;')}</div>
    `;
    listEl.appendChild(card);
  }

  if (items.length < PAGE_SIZE) {
    btnMore.disabled = true;
    btnMore.textContent = "더 이상 항목 없음";
  } else {
    btnMore.disabled = false;
    btnMore.textContent = "더 보기";
    page += 1;
  }
}

load(true);
</script>
</body>
</html>"""
    return Response(html, mimetype="text/html; charset=utf-8")

@app.route("/api/articles")
def api_articles():
    limit = max(1, min(int(request.args.get("limit", 20)), 100))
    offset = max(0, int(request.args.get("offset", 0)))
    q = (request.args.get("q") or "").strip()

    sql = "SELECT title, link, source, published_at, summary FROM articles"
    params = []
    if q:
        sql += " WHERE title LIKE ? OR source LIKE ?"
        like = f"%{q}%"
        params.extend([like, like])
    sql += " ORDER BY id DESC LIMIT ? OFFSET ?"
    params.extend([limit, offset])

    cur = CONN.execute(sql, params)
    rows = cur.fetchall()
    articles = []
    for t, l, s, p, sm in rows:
        articles.append({
            "title": t,
            "link": l,
            "source": s,
            "published_at": int(p) if p else None,
            "summary": sm or ""
        })
    return jsonify({"articles": articles})

# 수동 수집 엔드포인트 (비번 쿼리)
ADMIN_PW = os.environ.get("ADMIN_PW", "Philia12")

@app.route("/admin/fetch")
def admin_fetch():
    pw = request.args.get("pw", "")
    if pw != ADMIN_PW:
        return jsonify({"error": "locked"}), 403

    before = CONN.execute("SELECT COUNT(*) FROM articles").fetchone()[0]
    # 가벼운 수집: 각 피드 최대 15개
    fetch_once(max_per_feed=15)
    after = CONN.execute("SELECT COUNT(*) FROM articles").fetchone()[0]

    return jsonify({"ok": True, "added": max(0, after - before), "total": after})

# Render: gunicorn uses "app:app"
if __name__ == "__main__":
    # Local debug
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", "8000")))
