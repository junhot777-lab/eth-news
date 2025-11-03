#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Ethereum news mini-aggregator for Render
- Uses Google News RSS for "이더리움"
- Stores to SQLite at /data/articles.db
- Flask app served by gunicorn
- APScheduler runs in-process (single worker)
"""

import os
import sqlite3
from contextlib import closing
from datetime import datetime, timezone
from urllib.parse import quote

import feedparser
from dateutil import parser as dtparser
from flask import Flask, jsonify, render_template_string, request, session
from apscheduler.schedulers.background import BackgroundScheduler

# ---------- Config ----------
DB_PATH = os.environ.get("DB_PATH", "/data/articles.db")
PORT = int(os.environ.get("PORT", "8000"))  # Render injects this
FETCH_INTERVAL_SEC = int(os.environ.get("FETCH_INTERVAL_SEC", "60"))
SECRET_KEY = os.environ.get("SECRET_KEY", "change-me")
ADMIN_PASSWORD = os.environ.get("ADMIN_PASSWORD", "Philia12")

GOOGLE_NEWS_RSS = (
    "https://news.google.com/rss/search?q=" + quote("이더리움") +
    "&hl=ko&gl=KR&ceid=KR:ko"
)
FEEDS = [GOOGLE_NEWS_RSS]

# ---------- App ----------
app = Flask(__name__)
app.secret_key = SECRET_KEY

HTML_TEMPLATE = """<!doctype html>
<html lang="ko"><head>
<meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>이더리움 실시간 뉴스 집계</title>
<style>
body{font-family:system-ui,Segoe UI,Noto Sans KR,Arial,sans-serif;margin:0;background:#0b0d10;color:#e6e9ee}
header{position:sticky;top:0;background:#0f1216;border-bottom:1px solid #1c2128;padding:16px}
h1{font-size:20px;margin:0}.meta{opacity:.7;font-size:12px}.wrap{max-width:920px;margin:0 auto;padding:16px}
.card{background:#11151a;border:1px solid #1f2630;border-radius:14px;padding:16px;margin:12px 0}
.card a{color:#9ecbff;text-decoration:none}.card a:hover{text-decoration:underline}
.time{font-size:12px;opacity:.8}.controls{display:flex;gap:8px;align-items:center;margin-top:8px}
input[type="search"]{flex:1;padding:8px 10px;border-radius:10px;border:1px solid #2a3240;background:#0b0f14;color:#e6e9ee}
.badge{display:inline-block;padding:2px 8px;border:1px solid #2a3240;border-radius:999px;font-size:12px;opacity:.8}
.overlay{position:fixed;inset:0;background:rgba(3,5,7,.85);display:flex;align-items:center;justify-content:center;z-index:9999}
.lockbox{background:#0b1220;padding:28px;border-radius:12px;border:1px solid #22303a;min-width:320px}
.lockbox h2{margin:0 0 10px 0;font-size:18px}.lockbox p{margin:0 0 12px 0;opacity:.8;font-size:13px}
.lockbox input{width:100%;padding:10px;border-radius:8px;border:1px solid #21303a;background:#081018;color:#e6eef8}
.lockbox button{margin-top:10px;padding:10px;border-radius:8px;border:none;cursor:pointer}
.err{color:#ff8b8b;margin-top:8px;font-size:13px}
</style>
<script>
async function refreshList(){
  const q=new URLSearchParams(window.location.search).get('q')||'';
  const r=await fetch('/api/articles?q='+encodeURIComponent(q));
  const data=await r.json();
  const root=document.getElementById('list'); root.innerHTML='';
  (data.articles||[]).forEach(x=>{
    const d=document.createElement('div'); d.className='card';
    d.innerHTML=`<div style="display:flex;justify-content:space-between;gap:12px;align-items:center">
      <a href="${x.link}" target="_blank" rel="noopener">${x.title}</a>
      <span class="badge">${x.source||''}</span></div>
      <div class="time">${x.published_at_local}</div>`;
    root.appendChild(d);
  });
  document.getElementById('count').textContent=(data.articles||[]).length;
  document.getElementById('updated').textContent=new Date().toLocaleString();
}
function applySearch(ev){
  ev&&ev.preventDefault(); const q=document.getElementById('q').value.trim();
  const url=new URL(window.location.href);
  if(q) url.searchParams.set('q',q); else url.searchParams.delete('q');
  history.replaceState({},'',url); refreshList();
}
async function tryUnlock(e){
  e&&e.preventDefault();
  const pw=document.getElementById('pw').value;
  const res=await fetch('/unlock',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({password:pw})});
  const j=await res.json();
  if(j.success){document.getElementById('overlay').style.display='none';refreshList();}
  else{document.getElementById('err').textContent=j.error||'비밀번호가 틀렸습니다.';}
}
window.addEventListener('DOMContentLoaded',()=>{
  fetch('/api/unlocked').then(r=>r.json()).then(j=>{
    if(j.unlocked){document.getElementById('overlay').style.display='none';refreshList();}
  });
  setInterval(refreshList,30000);
});
</script>
</head><body>
<div id="overlay" class="overlay"><div class="lockbox">
  <h2>화면 잠금</h2><p>관리자 비밀번호를 입력하세요.</p>
  <form onsubmit="tryUnlock(event)">
    <input id="pw" type="password" placeholder="비밀번호"><button type="submit">잠금 해제</button>
    <div id="err" class="err"></div>
  </form></div></div>
<header><div class="wrap">
  <h1>이더리움 실시간 뉴스 집계</h1>
  <div class="meta">30초마다 자동 새로고침 • <span id="updated">-</span></div>
  <form class="controls" onsubmit="applySearch(event)">
    <input id="q" type="search" placeholder="제목 키워드 필터 (예: ETF, 업그레이드)">
  </form>
  <div class="meta"><span id="count">0</span>건 표시 중</div>
</div></header>
<main class="wrap"><div id="list"></div></main>
</body></html>"""

# ---------- DB ----------
def init_db():
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True) if "/" in DB_PATH else None
    with closing(sqlite3.connect(DB_PATH)) as conn:
        conn.execute("""
        CREATE TABLE IF NOT EXISTS articles(
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          title TEXT NOT NULL,
          link TEXT NOT NULL UNIQUE,
          source TEXT,
          published_at_utc TEXT NOT NULL,
          created_at_utc TEXT NOT NULL
        )""")
        conn.commit()

def insert_article(title, link, source, published_at_utc):
    try:
        with closing(sqlite3.connect(DB_PATH)) as conn:
            conn.execute(
                """INSERT OR IGNORE INTO articles
                   (title, link, source, published_at_utc, created_at_utc)
                   VALUES (?, ?, ?, ?, ?)""",
                (title, link, source, published_at_utc, datetime.now(timezone.utc).isoformat()),
            )
            conn.commit()
    except sqlite3.Error as e:
        print("[DB] insert error:", e)

def query_articles(q=None, limit=200):
    sql = "SELECT title, link, source, published_at_utc FROM articles"
    params = []
    if q:
        sql += " WHERE title LIKE ?"
        params.append(f"%{q}%")
    sql += " ORDER BY published_at_utc DESC, id DESC LIMIT ?"
    params.append(limit)
    with closing(sqlite3.connect(DB_PATH)) as conn:
        return conn.execute(sql, params).fetchall()

# ---------- Fetcher ----------
def parse_time(entry):
    for k in ("published", "updated", "pubDate"):
        if getattr(entry, k, None):
            try:
                return dtparser.parse(getattr(entry, k)).astimezone(timezone.utc)
            except Exception:
                pass
    return datetime.now(timezone.utc)

def fetch_once():
    total_new = 0
    for url in FEEDS:
        try:
            feed = feedparser.parse(url)
            src_title = feed.feed.get("title", "Google News") if hasattr(feed, "feed") else "RSS"
            for e in feed.entries:
                title = e.get("title", "(no title)")
                link = e.get("link")
                if not link:
                    continue
                published = parse_time(e)
                insert_article(title, link, src_title, published.isoformat())
                total_new += 1
        except Exception as ex:
            print("[FETCH] error:", ex)
    print(f"[FETCH] cycle done. seen={total_new} at {datetime.now().isoformat()}")

# ---------- Bootstrapping (single-run) ----------
_initialized = False
_sched = None
def ensure_started():
    global _initialized, _sched
    if _initialized:
        return
    init_db()
    fetch_once()
    _sched = BackgroundScheduler(daemon=True)
    _sched.add_job(fetch_once, "interval", seconds=FETCH_INTERVAL_SEC)
    _sched.start()
    _initialized = True
    print("[BOOT] app initialized")

# call at import (gunicorn path)
ensure_started()

# ---------- Web ----------
@app.get("/")
def home():
    return render_template_string(HTML_TEMPLATE)

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

@app.get("/api/articles")
def api_articles():
    if not session.get("unlocked"):
        return jsonify({"error": "locked"}), 401
    q = request.args.get("q", "").strip()
    rows = query_articles(q if q else None)
    def to_local(iso_str):
        try:
            dt = dtparser.parse(iso_str)
            return dt.astimezone().strftime("%Y-%m-%d %H:%M:%S")
        except Exception:
            return iso_str
    return jsonify({"articles": [
        {"title": r[0], "link": r[1], "source": r[2] or "", "published_at_local": to_local(r[3])}
        for r in rows
    ]})

# local debug only
if __name__ == "__main__":
    from werkzeug.serving import run_simple
    run_simple("0.0.0.0", PORT, app, use_reloader=False, use_debugger=True)
