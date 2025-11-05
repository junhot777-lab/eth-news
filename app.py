import os, sqlite3, time, re, html
from datetime import datetime, timezone
from urllib.parse import urlparse
import feedparser
import httpx
import trafilatura
import tldextract

from flask import Flask, request, jsonify, Response

# ---------- 설정 ----------
DB_PATH = os.getenv("DB_PATH", "/tmp/ethnews.db")
KEYWORD = os.getenv("KEYWORD", "ethereum|ether|이더리움")
ADMIN_PW = os.getenv("ADMIN_PW", "Philia12")
FETCH_INTERVAL = int(os.getenv("FETCH_INTERVAL", "900"))  # 초
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")  # 있으면 한국어 요약 활성화

# RSS 후보들 (가볍게 늘려도 됨)
FEEDS = [
    "https://www.coindesk.com/arc/outboundfeeds/rss/?outputType=xml",
    "https://decrypt.co/feed",
    "https://cointelegraph.com/rss",
    "https://www.theblock.co/rss",
]

app = Flask(__name__)

# ---------- DB ----------
def init_db():
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    with sqlite3.connect(DB_PATH) as con:
        c = con.cursor()
        c.execute("""
        CREATE TABLE IF NOT EXISTS articles(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            title TEXT NOT NULL,
            link TEXT UNIQUE NOT NULL,
            source TEXT,
            published_ts INTEGER,
            summary_ko TEXT DEFAULT NULL
        )""")
        c.execute("CREATE INDEX IF NOT EXISTS idx_pub ON articles(published_ts DESC)")
        con.commit()

init_db()

# ---------- 유틸 ----------
def domain_name(url:str) -> str:
    try:
        ext = tldextract.extract(url)
        if ext.domain:
            return ext.domain.capitalize()
    except Exception:
        pass
    return urlparse(url).netloc or ""

_kw = re.compile(KEYWORD, re.I)

def is_eth_related(title:str, summary:str=""):
    text = f"{title} {summary}".lower()
    return bool(_kw.search(text))

def parse_time(entry):
    # feedparser의 published_parsed 우선
    if getattr(entry, "published_parsed", None):
        return int(time.mktime(entry.published_parsed))
    if getattr(entry, "updated_parsed", None):
        return int(time.mktime(entry.updated_parsed))
    return int(time.time())

# ---------- 본문 추출 ----------
def fetch_article_text(url:str, timeout=12.0) -> str:
    try:
        with httpx.Client(follow_redirects=True, timeout=timeout, headers={
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0 Safari/537.36"
        }) as cli:
            r = cli.get(url)
            r.raise_for_status()
            downloaded = trafilatura.extract(r.text, include_comments=False, include_tables=False)
            return downloaded or ""
    except Exception:
        return ""

# ---------- 기본 추출 요약(영문) ----------
_SENT_SPLIT = re.compile(r'(?<=[\.\?\!])\s+')
def keyword_score(sent:str) -> float:
    s = sent.lower()
    score = 0.0
    # 가중 키워드
    for w, wgt in [
        ("ethereum", 3), ("ether", 2.5), ("eth", 2),
        ("price", 1.2), ("upgrade", 1.1), ("etf", 1.1),
        ("defi", 1.0), ("layer 2", 1.0), ("staking", 1.0),
        ("merge", 1.0), ("fund", 0.9), ("market", 0.8),
    ]:
        if w in s: score += wgt
    # 길이 페널티
    words = len(sent.split())
    if words > 5: score += min(2.0, words/40)
    return score

def extract_3lines(text:str) -> list:
    sents = [s.strip() for s in _SENT_SPLIT.split(text) if len(s.strip())>30]
    if not sents:
        return []
    scored = sorted(sents, key=keyword_score, reverse=True)
    pick = []
    seen = set()
    for s in scored:
        sig = s[:50]
        if sig in seen: 
            continue
        seen.add(sig)
        pick.append(s)
        if len(pick) == 3: break
    return pick

# ---------- 한국어 요약 (선택적: OPENAI_API_KEY 필요) ----------
def summarize_ko(title:str, body_text:str) -> list:
    # 키가 없으면 기본 추출 영문을 반환하고, 렌더링 시 한국어 접두사만 붙인다.
    base = extract_3lines(body_text or title)
    if not OPENAI_API_KEY:
        return base  # 영문 3줄 반환
    try:
        import httpx
        sys_prompt = (
            "다음 기사의 핵심만 한국어로 정확하고 간결하게 3줄 bullet로 요약해줘. "
            "과장 금지, 수치/주체를 명확히, 각 줄은 18~40자."
        )
        user = f"제목: {title}\n본문:\n{body_text[:4000]}"
        payload = {
            "model": "gpt-4o-mini",
            "messages": [
                {"role":"system","content":sys_prompt},
                {"role":"user","content":user}
            ],
            "temperature": 0.2,
        }
        headers = {"Authorization": f"Bearer {OPENAI_API_KEY}", "Content-Type":"application/json"}
        with httpx.Client(timeout=20) as cli:
            r = cli.post("https://api.openai.com/v1/chat/completions", json=payload, headers=headers)
            r.raise_for_status()
            text = r.json()["choices"][0]["message"]["content"].strip()
        lines = [re.sub(r'^[\-\•\*]\s*','',ln).strip() for ln in text.splitlines() if ln.strip()]
        return [ln for ln in lines if ln][:3] or base
    except Exception:
        return base

# ---------- 수집 ----------
def fetch_once():
    added = 0
    with sqlite3.connect(DB_PATH) as con:
        c = con.cursor()
        for url in FEEDS:
            try:
                feed = feedparser.parse(url)
                for e in getattr(feed, "entries", []):
                    title = html.unescape(getattr(e, "title", "")).strip()
                    link = getattr(e, "link", "").strip()
                    summary = html.unescape(getattr(e, "summary", "")).strip()
                    if not title or not link:
                        continue
                    if not is_eth_related(title, summary):
                        continue
                    ts = parse_time(e)
                    src = domain_name(link)

                    try:
                        c.execute("INSERT OR IGNORE INTO articles(title, link, source, published_ts) VALUES(?,?,?,?)",
                                  (title, link, src, ts))
                        if c.rowcount:
                            # 새로 들어온 기사만 본문 요약 시도
                            body = fetch_article_text(link)
                            ko3 = summarize_ko(title, body)
                            # 저장은 줄바꿈으로
                            c.execute("UPDATE articles SET summary_ko=? WHERE link=?", ("\n".join(ko3) if ko3 else None, link))
                            added += 1
                    except Exception:
                        pass
            except Exception:
                continue
        con.commit()
    return added

# ---------- 라우트 ----------
@app.get("/")
def index():
    # 간단 템플릿(최신순 상단)
    return Response(f"""
<!doctype html><html lang="ko"><meta charset="utf-8">
<title>이더리움 실시간 뉴스 집계</title>
<meta name=viewport content="width=device-width,initial-scale=1">
<style>
body{{background:#0e0f12;color:#e5e7eb;font-family:system-ui,Segoe UI,Apple SD Gothic Neo,sans-serif;margin:0}}
.wrap{{max-width:860px;margin:32px auto;padding:0 16px}}
h1{{font-size:28px;margin:0 0 8px}}
.sub{{color:#9ca3af;font-size:13px;margin-bottom:16px}}
.search{{width:100%;padding:14px 16px;border-radius:10px;border:1px solid #374151;background:#0b0c0f;color:#e5e7eb}}
.card{{background:#17181b;border:1px solid #2a2d33;border-radius:14px;padding:16px;margin:14px 0}}
.title{{font-size:22px;color:#93c5fd;text-decoration:none}}
.meta{{color:#9ca3af;font-size:13px;margin-top:6px}}
.badge{{background:#222;border:1px solid #333;color:#cbd5e1;border-radius:999px;padding:3px 10px;margin-left:6px;font-size:12px}}
.summary{{margin-top:10px;line-height:1.35}}
.more{{display:block;width:100%;padding:12px 14px;margin:20px 0;background:#0b0c0f;border:1px solid #374151;color:#e5e7eb;border-radius:10px}}
</style>
<div class=wrap>
  <h1>이더리움 실시간 뉴스 집계</h1>
  <div class=sub>최신순 · 아래 ‘더 보기’로 과거 기사 누적 · 요약 3줄 표시</div>
  <input class=search id=q placeholder="제목/매체로 검색 (Enter)">
  <div id=list></div>
  <button class=more id=more>더 보기</button>
</div>
<script>
let page=0, q="";
async function load(reset=false){{
  const r=await fetch(`/api/articles?page=${{page}}&q=${{encodeURIComponent(q)}}`);
  const j=await r.json();
  const box=document.querySelector("#list");
  if(reset) box.innerHTML="";
  j.articles.forEach(a=>{{
    const div=document.createElement("div"); div.className="card";
    const sum = a.summary_ko ? a.summary_ko.split("\\n").map(s=>"• "+s).join("<br>") : "";
    div.innerHTML = `
      <a class="title" href="${{a.link}}" target="_blank" rel="noopener">${{a.title}}</a>
      <div class="meta">${{a.date}}<span class="badge">${{a.source}}</span></div>
      <div class="summary">${{sum}}</div>
    `;
    box.appendChild(div);
  }});
  if(j.articles.length===0) document.querySelector("#more").disabled=true;
}}
document.querySelector("#more").onclick=()=>{{page++;load(false)}};
document.querySelector("#q").addEventListener("keydown",e=>{{
  if(e.key==="Enter"){{ q=e.target.value.trim(); page=0; document.querySelector("#more").disabled=false; load(true); }}
}});
load(true);
</script>
""","text/html")

@app.get("/api/articles")
def api_articles():
    page = max(int(request.args.get("page", "0")), 0)
    q = (request.args.get("q") or "").strip()
    with sqlite3.connect(DB_PATH) as con:
        c = con.cursor()
        sql = "SELECT title,link,source,published_ts,COALESCE(summary_ko,'') FROM articles "
        params = []
        if q:
            sql += "WHERE title LIKE ? OR source LIKE ? "
            params += [f"%{q}%", f"%{q}%"]
        sql += "ORDER BY published_ts DESC LIMIT 20 OFFSET ?"
        params.append(page*20)
        rows = c.execute(sql, params).fetchall()
    items = []
    for title, link, source, ts, sumko in rows:
        dt = datetime.fromtimestamp(ts, tz=timezone.utc).astimezone().strftime("%Y. %m. %d. %p %I:%M:%S")
        items.append({
            "title": title,
            "link": link,
            "source": source,
            "date": dt,
            "summary_ko": sumko
        })
    return jsonify({"articles": items})

@app.get("/admin/fetch")
def admin_fetch():
    if request.args.get("pw") != ADMIN_PW:
        return jsonify({"error":"locked"})
    added = fetch_once()
    return jsonify({"ok": True, "added": added, "total": count_total()})

def count_total():
    with sqlite3.connect(DB_PATH) as con:
        c = con.cursor()
        n = c.execute("SELECT COUNT(*) FROM articles").fetchone()[0]
        return n

# 시작 시 한 번 당겨두기
try:
    fetch_once()
except Exception:
    pass

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=8000)
