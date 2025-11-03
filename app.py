#!/usr/bin/env python3
# -*- coding: utf-8 -*-
import os, sqlite3
from contextlib import closing
from datetime import datetime, timezone
from urllib.parse import quote

import feedparser
from dateutil import parser as dtparser
from flask import Flask, jsonify, render_template_string, request, session
from apscheduler.schedulers.background import BackgroundScheduler

# ---------- Config ----------
DB_PATH = os.environ.get("DB_PATH", "/tmp/data/articles.db")
FETCH_INTERVAL_SEC = int(os.environ.get("FETCH_INTERVAL_SEC", "60"))
ADMIN_PASSWORD = os.environ.get("ADMIN_PASSWORD", "Philia12")
SECRET_KEY = os.environ.get("SECRET_KEY", "change-me")

GOOGLE_NEWS_RSS = (
    "https://news.google.com/rss/search?q=" + quote("이더리움") +
    "&hl=ko&gl=KR&ceid=KR:ko"
)
FEEDS = [GOOGLE_NEWS_RSS]

# ---------- App ----------
app = Flask(__name__)
app.secret_key = SECRET_KEY

HTML = """<!doctype html><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>이더리움 실시간 뉴스</title>
<style>body{font-family:system-ui,Noto Sans KR,Arial;background:#0b0d10;color:#e6e9ee;margin:0}
header{position:sticky;top:0;background:#0f1216;border-bottom:1px solid #1c2128;padding:16px}
.wrap{max-width:900px;margin:0 auto;padding:16px}.card{background:#11151a;border:1px solid #1f2630;border-radius:14px;padding:16px;margin:12px 0}
a{color:#9ecbff;text-decoration:none}.badge{border:1px solid #2a3240;border-radius:999px;padding:2px 8px;font-size:12px;opacity:.8}
.overlay{position:fixed;inset:0;background:rgba(3,5,7,.85);display:flex;align-items:center;justify-content:center;z-index:9999}
.lock{background:#0b1220;padding:24px;border-radius:12px;border:1px solid #22303a;min-width:320px}
.lock input{width:100%;padding:10px;border-radius:8px;border:1px solid #21303a;background:#081018;color:#e6eef8}
.lock button{margin-top:10px;padding:10px;border-radius:8px;border:none;cursor:pointer}
.err{color:#ff8b8b;margin-top:8px;font-size:13px}
.meta{opacity:.75;font-size:12px}</style>
<div id="overlay" class="overlay"><div class="lock">
  <h3>화면 잠금</h3><p class="meta">관리자 비밀번호를 입력하세요.</p>
  <input id="pw" type="password" placeholder="비밀번호"><button onclick="unlock()">잠금 해제</button>
  <div id="err" class="err"></div></div></div>
<header><div class="wrap">
  <h1 style="margin:0;font-size:20px">이더리움 실시간 뉴스</h1>
  <div class="meta">30초마다 자동 새로고침 • <span id="updated">-</span></div>
  <input id="q" type="search" placeholder="제목 키워드 필터 (예: ETF, 업그레이드)" oninput="apply()"
         style="margin-top:8px;width:100%;padding:8px;border-radius:10px;border:1px solid #2a3240;background:#0b0f14;color:#e6e9ee">
  <div class="meta"><span id="count">0</span>건 표시</div>
</div></header>
<main class="wrap"><div id="list"></div></main>
<script>
async function refresh(){
  const q=new URLSearchParams(location.search).get('q')||'';
  const r=await fetch('/api/articles?q='+encodeURIComponent(q));
  const j=await r.json();
  if(j.error){document.getElementById('overlay').style.display='flex';return;}
  const root=document.getElementById('list'); root.innerHTML='';
  (j.articles||[]).forEach(x=>{
    const d=document.createElement('div'); d.className='card';
    d.innerHTML=`<div style="display:flex;justify-content:space-between;gap:12px;align-items:center">
      <a target="_blank" rel="noopener" href="${x.link}">${x.title}</a>
      <span class="badge">${x.source||''}</span></div>
      <div class="meta">${x.published_at_local}</div>`;
    root.appendChild(d);
  });
  document.getElementById('count').textContent=(j.articles||[]).length;
  document.getElementById('updated').textContent=new Date().toLocaleString();
}
async function unlock(){
  const pw=document.getElementById('pw').value;
  const r=await fetch('/unlock',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({password:pw})});
  const j=await r.json();
  if(j.success){document.getElementById('overlay').style.display='none'; refresh();}
  else{document.getElementById('err').textContent=j.error||'비밀번호가 올바르지 않습니다.';}
}
function apply(){
  const q=document.getElementById('q').value.trim(); const u=new URL(location.href);
  if(q)u.searchParams.set('q',q); else u.searchParams.delete('q'); history.replaceState({},'',u); refresh();
}
addEventListener('DOMContentLoaded',()=>{
  fetch('/api/unlocked').then(r=>r.json()).then(j=>{ if(j.unlocked){document.getElementById('overlay').style.display='none'; refresh();} });
  setInterval(refresh,30000);
});
</script>"""

# ---------- DB helpers ----------
def ensure_dirs():
    d = os.path.dirname(DB_PATH) or "/tmp"
    os.makedirs(d, exist_ok=True)

def init_db():
    ensure_dirs()
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
    with closing(sqlite3.connect(DB_PATH)) as conn:
        conn.execute(
            """INSERT OR IGNORE INTO articles
               (title, link, source, published_at_utc, created_at_utc)
               VALUES (?, ?, ?, ?, ?)""",
            (title, link, source, published_at_utc, datetime.now(timezone.utc).isoformat()),
        )
        conn.commit()

def query_articles(q=None, limit=200):
    sql = "SELECT title, link, source, published_at_utc FROM articles"
    params = []
    if q:
        sql += " WHERE title LIKE ?"; params.append(f"%{q}%")
    sql += " ORDER BY published_at_utc DESC, id DESC LIMIT ?"; params.append(limit)
    with closing(sqlite3.connect(DB_PATH)) as conn:
        return conn.execute(sql, params).fetchall()

# ---------- Fetcher ----------
def parse_time(entry):
    for k in ("published","updated","pubDate"):
        v = getattr(entry, k, None)
        if v:
            try: return dtparser.parse(v).astimezone(timezone.utc)
            except: pass
    return datetime.now(timezone.utc)

def fetch_once():
    total=0
    for url in FEEDS:
        try:
            feed = feedparser.parse(url)
            src = getattr(feed, "feed", {}).get("title","RSS")
            for e in feed.entries:
                title = e.get("title","(no title)")
                link  = e.get("link");  if not link: continue
                insert_article(title, link, src, parse_time(e).isoformat()); total+=1
        except Exception as ex:
            print("[FETCH] error:", ex)
    print(f"[FETCH] done seen={total} @ {datetime.now().isoformat()}")

# ---------- Boot sequence (always on import) ----------
_initialized=False
_scheduler=None
def start_once():
    global _initialized,_scheduler
    if _initialized: return
    init_db()
    fetch_once()
    _scheduler = BackgroundScheduler(daemon=True)
    _scheduler.add_job(fetch_once, "interval", seconds=FETCH_INTERVAL_SEC)
    _scheduler.start()
    _initialized=True
    print("[BOOT] started; DB:", DB_PATH)

start_once()  # 중요: gunicorn import 시에도 실행

# ---------- Routes ----------
@app.get("/")
def home():
    return render_template_string(HTML)

@app.post("/unlock")
def unlock():
    j = request.get_json(silent=True) or {}
    if j.get("password")==ADMIN_PASSWORD:
        session["unlocked"]=True
        return jsonify({"success":True})
    return jsonify({"success":False,"error":"비밀번호가 올바르지 않습니다."}), 401

@app.get("/api/unlocked")
def api_unlocked():
    return jsonify({"unlocked": bool(session.get("unlocked"))})

@app.get("/api/articles")
def api_articles():
    if not session.get("unlocked"):
        return jsonify({"error":"locked"}), 401
    q = request.args.get("q","").strip() or None
    rows = query_articles(q)
    def to_local(s):
        try: return dtparser.parse(s).astimezone().strftime("%Y-%m-%d %H:%M:%S")
        except: return s
    return jsonify({"articles":[
        {"title":r[0],"link":r[1],"source":r[2] or "", "published_at_local":to_local(r[3])}
        for r in rows
    ]})

# Local debug
if __name__=="__main__":
    from werkzeug.serving import run_simple
    run_simple("0.0.0.0", 8000, app, use_reloader=False, use_debugger=True)
