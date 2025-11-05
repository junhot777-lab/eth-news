import os
import sqlite3
import time
from datetime import datetime, timezone
from typing import List, Dict

import requests
import feedparser
from flask import Flask, request, jsonify, Response

# -----------------------
# Config
# -----------------------
APP_TITLE = "이더리움 실시간 뉴스 집계기"
DB_DIR = os.environ.get("DB_DIR", "/tmp/data")
DB_PATH = os.path.join(DB_DIR, "news.db")

ETH_KEYWORDS = [
    "ethereum", "ether", "eth", "vitalik", "eip-", "rollup", "l2",
    "optimism", "arbitrum", "zkSync", "base chain", "beacon chain",
    "staking", "withdrawal", "validator", "sepoli", "holesky"
]

FEEDS = [
    "https://www.coindesk.com/arc/outboundfeeds/rss/?outputType=xml",
    "https://decrypt.co/feed",
    "https://cointelegraph.com/rss",
    "https://www.theblock.co/rss",
]

USER_AGENT = "eth-news (Render) - requests/2.x"

OPENAI_API_KEY = os.environ.get("OPENAI_API_KEY", "").strip()
DISABLE_SUMMARY = os.environ.get("DISABLE_SUMMARY", "").strip() == "1"

# -----------------------
# App + DB
# -----------------------
app = Flask(__name__)

def ensure_db():
    os.makedirs(DB_DIR, exist_ok=True)
    with sqlite3.connect(DB_PATH) as conn:
        c = conn.cursor()
        c.execute("""
        CREATE TABLE IF NOT EXISTS articles (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            title TEXT NOT NULL,
            link TEXT UNIQUE NOT NULL,
            source TEXT,
            published_at TEXT,
            summary_en TEXT DEFAULT ''
        )
        """)
        c.execute("CREATE INDEX IF NOT EXISTS idx_published ON articles(published_at DESC)")
        conn.commit()

ensure_db()

# -----------------------
# Utils
# -----------------------
def is_eth_related(text: str) -> bool:
    if not text:
        return False
    t = text.lower()
    return any(k in t for k in ETH_KEYWORDS)

def http_get(url: str, timeout: float = 6.0) -> requests.Response:
    return requests.get(url, headers={"User-Agent": USER_AGENT}, timeout=timeout)

def parse_time(entry) -> str:
    # RFC822 -> ISO8601. Fallback to now().
    try:
        if hasattr(entry, "published_parsed") and entry.published_parsed:
            ts = time.mktime(entry.published_parsed)
            return datetime.fromtimestamp(ts, tz=timezone.utc).isoformat()
    except Exception:
        pass
    return datetime.now(tz=timezone.utc).isoformat()

# -----------------------
# Summarizer (English 3 lines)
# -----------------------
def summarize_english_3_lines(title: str, desc: str) -> str:
    if DISABLE_SUMMARY or not OPENAI_API_KEY:
        return ""
    # Keep prompts and tokens tiny to avoid OOM/timeouts on free tier
    content = (desc or "").strip()
    content = content[:700]  # hard cap context
    prompt = (
        "You are a concise crypto news assistant. "
        "Summarize the following news in exactly 3 short English bullet lines. "
        "Each bullet must be under 18 words, factual, and non-redundant.\n\n"
        f"Title: {title}\n"
        f"Body: {content}\n\n"
        "Output format:\n"
        "- line 1\n- line 2\n- line 3"
    )
    try:
        # Minimal client without bring-in heavy libs
        import json
        import urllib.request

        req = urllib.request.Request(
            url="https://api.openai.com/v1/chat/completions",
            data=json.dumps({
                "model": "gpt-4o-mini",
                "messages": [{"role": "user", "content": prompt}],
                "temperature": 0.2,
                "max_tokens": 180,
                "timeout": 3000
            }).encode("utf-8"),
            headers={
                "Authorization": f"Bearer {OPENAI_API_KEY}",
                "Content-Type": "application/json"
            },
            method="POST"
        )
        with urllib.request.urlopen(req, timeout=5) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        text = data["choices"][0]["message"]["content"].strip()
        # Safety: keep only first 3 lines
        lines = [ln.strip() for ln in text.splitlines() if ln.strip().startswith("-")]
        return "\n".join(lines[:3])
    except Exception:
        # Fail silently to keep page responsive
        return ""

# -----------------------
# Fetch & Store
# -----------------------
def upsert_article(title: str, link: str, source: str, published_at: str, summary: str):
    with sqlite3.connect(DB_PATH) as conn:
        c = conn.cursor()
        try:
            c.execute("""
                INSERT OR IGNORE INTO articles(title, link, source, published_at, summary_en)
                VALUES(?,?,?,?,?)
            """, (title, link, source, published_at, summary))
            # If already exists but summary empty, try to fill it once
            if c.rowcount == 0 and summary:
                c.execute("""
                    UPDATE articles
                    SET summary_en = CASE WHEN IFNULL(summary_en,'')='' THEN ? ELSE summary_en END
                    WHERE link = ?
                """, (summary, link))
        finally:
            conn.commit()

def fetch_once() -> Dict[str, int]:
    scanned = 0
    added = 0
    for feed_url in FEEDS:
        try:
            resp = http_get(feed_url, timeout=8)
            resp.raise_for_status()
            parsed = feedparser.parse(resp.content)
        except Exception:
            continue

        for e in parsed.entries[:50]:  # gentle cap
            scanned += 1
            title = getattr(e, "title", "") or ""
            link = getattr(e, "link", "") or ""
            source = parsed.feed.get("title", "") if getattr(parsed, "feed", None) else ""
            if not title or not link:
                continue

            desc = ""
            if hasattr(e, "summary"):
                desc = e.summary
            elif hasattr(e, "description"):
                desc = e.description

            if not (is_eth_related(title) or is_eth_related(desc)):
                continue

            published_at = parse_time(e)
            summary = summarize_english_3_lines(title, desc)
            before_add = count_total()
            upsert_article(title.strip(), link.strip(), source.strip(), published_at, summary)
            after_add = count_total()
            if after_add > before_add:
                added += 1
    return {"scanned": scanned, "added": added, "total": count_total()}

def count_total() -> int:
    with sqlite3.connect(DB_PATH) as conn:
        c = conn.cursor()
        c.execute("SELECT COUNT(*) FROM articles")
        return int(c.fetchone()[0])

# -----------------------
# Routes
# -----------------------
@app.route("/healthz")
def health():
    return "ok"

@app.route("/api/articles")
def api_articles():
    try:
        limit = max(1, min(int(request.args.get("limit", "50")), 200))
    except Exception:
        limit = 50
    q = (request.args.get("q") or "").strip().lower()

    with sqlite3.connect(DB_PATH) as conn:
        c = conn.cursor()
        if q:
            c.execute("""
                SELECT title, link, source, published_at, summary_en
                FROM articles
                WHERE lower(title) LIKE ? OR lower(source) LIKE ?
                ORDER BY datetime(published_at) DESC
                LIMIT ?
            """, (f"%{q}%", f"%{q}%", limit))
        else:
            c.execute("""
                SELECT title, link, source, published_at, summary_en
                FROM articles
                ORDER BY datetime(published_at) DESC
                LIMIT ?
            """, (limit,))
        rows = c.fetchall()

    articles = []
    for title, link, source, published_at, summary_en in rows:
        articles.append({
            "title": title, "link": link, "source": source,
            "published_at": published_at, "summary_en": summary_en
        })
    return jsonify({"articles": articles})

@app.route("/admin/fetch")
def admin_fetch():
    # Simple guard to avoid randoms hitting it
    pw = request.args.get("pw", "")
    if pw != os.environ.get("ADMIN_PW", "Philia12"):
        return jsonify({"error": "locked"}), 401
    t0 = time.time()
    result = fetch_once()
    result["took_ms"] = int((time.time() - t0) * 1000)
    return jsonify(result)

@app.route("/")
def index():
    # Ultra-light HTML to avoid Jinja and templates
    html = f"""<!doctype html>
<html lang="ko">
<head>
<meta charset="utf-8"/>
<meta name="viewport" content="width=device-width,initial-scale=1"/>
<title>{APP_TITLE}</title>
<style>
  body {{ background:#0f1115; color:#d7e1ec; font-family:system-ui,-apple-system,Segoe UI,Roboto,Helvetica,Arial,sans-serif; }}
  .wrap {{ max-width:920px; margin:28px auto; padding:0 16px; }}
  h1 {{ font-size:28px; margin:0 0 6px; }}
  .hint {{ opacity:.7; font-size:13px; margin-bottom:16px; }}
  #q {{ width:100%; padding:10px 12px; border-radius:8px; border:1px solid #2a2f3a; background:#12151c; color:#d7e1ec; }}
  .card {{ border:1px solid #2a2f3a; background:#12151c; border-radius:12px; padding:14px 16px; margin:14px 0; }}
  .title a {{ color:#98c7ff; text-decoration:none; }}
  .meta {{ font-size:12px; opacity:.75; margin:6px 0 8px; }}
  .sum {{ white-space:pre-wrap; line-height:1.35; }}
  .badge {{ float:right; opacity:.8; font-size:12px; border:1px solid #2a2f3a; padding:2px 8px; border-radius:999px; }}
  .more {{ display:block; text-align:center; padding:10px; border-radius:10px; border:1px solid #2a2f3a; color:#d7e1ec; text-decoration:none; }}
</style>
</head>
<body>
<div class="wrap">
  <h1>이더리움 실시간 뉴스 집계</h1>
  <div class="hint">최신순 • 요약은 영어 3줄 • 아래 검색 가능</div>
  <input id="q" placeholder="제목/매체로 검색 (Enter)" />
  <div id="list"></div>
  <a id="more" href="#" class="more">더 보기</a>
</div>
<script>
  let next = 0, page = 20, q = "";
  const list = document.getElementById("list");
  const more = document.getElementById("more");
  const qi = document.getElementById("q");

  async function load(reset=false){
    const limit = reset ? Math.max(page, next || page) : page;
    const url = "/api/articles?limit=" + limit + (q ? "&q=" + encodeURIComponent(q) : "");
    const r = await fetch(url, {headers: {"Cache-Control":"no-cache"}});
    const data = await r.json();
    const items = data.articles || [];
    if(reset){ list.innerHTML=""; next = 0; }
    const slice = items.slice(next, next + page);
    slice.forEach(a => {{
      const el = document.createElement("div");
      el.className="card";
      const time = (a.published_at||"").replace("T"," ").slice(0,19).replace("+00:00","");
      el.innerHTML = `
        <div class="title"><a href="${{a.link}}" target="_blank" rel="noopener">${{a.title}}</a>
          <span class="badge">${{a.source||""}}</span>
        </div>
        <div class="meta">${{time}}</div>
        ${{
          a.summary_en ? `<div class="sum">${{a.summary_en}}</div>` : ``
        }}
      `;
      list.appendChild(el);
    }});
    next += slice.length;
    more.style.display = (next < items.length) ? "block" : "none";
  }
  more.addEventListener("click", e => {{ e.preventDefault(); load(false); }});
  qi.addEventListener("keydown", e => {{ if(e.key === "Enter") {{ q = qi.value.trim(); next = 0; load(true); }} }});
  load(true);
</script>
</body>
</html>"""
    return Response(html, mimetype="text/html")
