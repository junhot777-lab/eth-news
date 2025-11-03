from flask import Flask, render_template, request, redirect, url_for
import os
import sqlite3

app = Flask(__name__)

# ✅ Render 환경에서는 /tmp 디렉토리만 쓰기가 가능
DB_DIR = "/tmp/data"
DB_PATH = os.path.join(DB_DIR, "articles.db")


def init_db():
    os.makedirs(DB_DIR, exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute('''CREATE TABLE IF NOT EXISTS articles
                 (id INTEGER PRIMARY KEY AUTOINCREMENT,
                  title TEXT NOT NULL,
                  link TEXT NOT NULL,
                  date TEXT)''')
    conn.commit()
    conn.close()


@app.route('/')
def index():
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("SELECT * FROM articles ORDER BY id DESC")
    articles = c.fetchall()
    conn.close()
    return render_template('index.html', articles=articles)


@app.route('/add', methods=['POST'])
def add_article():
    title = request.form['title']
    link = request.form['link']
    date = request.form['date']
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("INSERT INTO articles (title, link, date) VALUES (?, ?, ?)", (title, link, date))
    conn.commit()
    conn.close()
    return redirect(url_for('index'))


if __name__ == '__main__':
    init_db()
    app.run(host='0.0.0.0', port=10000)
