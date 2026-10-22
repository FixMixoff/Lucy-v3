import hmac
import ntpath
import ipaddress
import os
import ctypes
import re
import secrets
import socket
import subprocess
import threading
import time
from datetime import timedelta

from flask import (Flask, abort, jsonify, redirect, render_template_string,
                   request, send_from_directory, session)
from werkzeug.serving import make_server

import matcher
from actions import HANDLERS

GROUPS = [
    ("Музыка", [("radio", "▶ Включить"), ("plpa", "⏯ Пауза"), ("prev", "⏮"), ("next", "⏭"),
                ("mute", "🔇 Звук"), ("now_playing", "Что играет")]),
    ("Компьютер", [("game_on", "🎮 Игровой режим"), ("game_off", "Обычный режим"),
                   ("desk", "Рабочий стол"), ("screenshot", "📷 Скриншот"),
                   ("mon_off", "Выкл. мониторы"), ("youtube", "YouTube"), ("discord", "Discord")]),
    ("Осторожно", [("empty_trash", "Очистить корзину"), ("clear_notes", "Удалить заметки"),
                   ("off", "⏻ Выключить ПК"), ("cancel_off", "Отменить выключение")]),
]
CONFIRMS = {"clear_notes": "Удалить все заметки?"}
ALLOWED = {name for _, buttons in GROUPS for name, _ in buttons} | {"search", "timer", "add_note", "ctime"}
RESERVED = {"CON", "PRN", "AUX", "NUL", *(f"COM{i}" for i in range(1, 10)),
            *(f"LPT{i}" for i in range(1, 10))}


def safe_name(name):
    name = os.path.basename((name or "").replace("\\", "/"))
    name = re.sub(r'[\x00-\x1f<>:"|?*]', "_", name).strip(" .")[:150]
    if not name:
        name = "file"
    if os.path.splitext(name)[0].upper() in RESERVED:
        name = "_" + name
    return name


def unique_path(folder, name):
    base, ext = os.path.splitext(name)
    path, n = os.path.join(folder, name), 1
    while os.path.exists(path):
        path = os.path.join(folder, f"{base} ({n}){ext}")
        n += 1
    return path


def lan_ip():
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("10.255.255.255", 1))
        ip = s.getsockname()[0]
        s.close()
        return ip
    except Exception:
        return "127.0.0.1"


def lan_ips():
    main = lan_ip()
    rows = []
    try:
        import psutil
        for iface, addrs in psutil.net_if_addrs().items():
            for a in addrs:
                if a.family == socket.AF_INET and not a.address.startswith(("127.", "169.254.")):
                    rows.append((iface, a.address))
    except Exception:
        try:
            for ip in socket.gethostbyname_ex(socket.gethostname())[2]:
                if not ip.startswith(("127.", "169.254.")):
                    rows.append(("?", ip))
        except Exception:
            pass
    rows.sort(key=lambda r: r[1] != main)
    if not rows and main != "127.0.0.1":
        rows = [("?", main)]
    return rows


def load_or_create(path, make):
    if os.path.exists(path):
        with open(path, encoding="utf-8") as f:
            value = f.read().strip()
        if value:
            return value, False
    value = make()
    with open(path, "w", encoding="utf-8") as f:
        f.write(value)
    return value, True


def list_dir(folder):
    rows = []
    if os.path.isdir(folder):
        for name in os.listdir(folder):
            p = os.path.join(folder, name)
            if os.path.isfile(p):
                st = os.stat(p)
                rows.append({"name": name, "size": st.st_size, "time": st.st_mtime})
    rows.sort(key=lambda r: r["time"], reverse=True)
    return rows[:100]


class WebCtx:

    def __init__(self, real, speak):
        self._real, self._speak, self.messages = real, speak, []

    def say(self, text, wait=False):
        self.messages.append(text)
        if self._speak:
            self._real.say(text, wait)

    def __getattr__(self, name):
        return getattr(self._real, name)


LOGIN_HTML = """<!doctype html><html lang="ru"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1"><title>Lucy · вход</title>
<style>
*{box-sizing:border-box}body{margin:0;min-height:100vh;display:grid;place-items:center;background:#12141a;
color:#e8eaf0;font:16px system-ui,sans-serif}
form{background:#1c1f29;border:1px solid #2a2e3b;border-radius:16px;padding:28px;width:min(92vw,340px)}
h1{margin:0 0 18px;font-size:22px}input,button{width:100%;padding:14px;border-radius:10px;font-size:16px;border:1px solid #2a2e3b}
input{background:#12141a;color:#e8eaf0;margin-bottom:12px}button{background:#6c8cff;color:#fff;border:0;font-weight:600}
.err{color:#ff7a80;margin:0 0 12px;font-size:14px}
</style></head><body><form method="post">
<h1>Lucy</h1>{% if error %}<p class="err">{{ error }}</p>{% endif %}
<input type="password" name="password" placeholder="Пароль" autofocus autocomplete="current-password">
<button>Войти</button></form></body></html>"""

PANEL_HTML = """<!doctype html><html lang="ru"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1"><title>Lucy</title>
<style>
:root{--bg:#12141a;--card:#1c1f29;--line:#2a2e3b;--text:#e8eaf0;--mut:#8b90a0;--acc:#6c8cff;--bad:#e5484d}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--text);font:16px/1.4 system-ui,sans-serif;padding:16px 16px 90px;
max-width:720px;margin-inline:auto}
header{display:flex;justify-content:space-between;align-items:center;margin-bottom:12px}
h1{margin:0;font-size:24px}header a{color:var(--mut);font-size:14px}
.status{display:flex;gap:8px;flex-wrap:wrap;margin-bottom:14px}
.pill{background:var(--card);border:1px solid var(--line);border-radius:999px;padding:6px 12px;font-size:14px;color:var(--mut)}
.pill.on{color:#7ee787;border-color:#2c6b3a}
section{background:var(--card);border:1px solid var(--line);border-radius:16px;padding:14px;margin-bottom:14px}
h2{margin:0 0 10px;font-size:15px;color:var(--mut);font-weight:600;text-transform:uppercase;letter-spacing:.04em}
.grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(130px,1fr));gap:8px}
button,.btn{background:#262a38;color:var(--text);border:1px solid var(--line);border-radius:12px;padding:14px 10px;
font-size:15px;cursor:pointer;text-align:center;text-decoration:none;display:block}
button:active{background:#323850}
button.danger{border-color:#5a2a2d;color:#ff9aa0}
button.main{background:var(--acc);border:0;color:#fff;font-weight:600}
.row{display:flex;gap:8px;margin-bottom:8px}.row input,textarea{flex:1;min-width:0}
input[type=text],textarea{background:var(--bg);color:var(--text);border:1px solid var(--line);border-radius:12px;
padding:12px;font-size:16px;font-family:inherit}
textarea{width:100%;min-height:80px;margin-bottom:8px}
.file{display:flex;justify-content:space-between;gap:8px;align-items:center;padding:8px 0;border-top:1px solid var(--line);font-size:14px}
.file a{color:var(--acc);word-break:break-all}.file small{color:var(--mut)}
.file button{padding:6px 10px;font-size:13px}
.bar{height:6px;background:var(--line);border-radius:3px;margin:8px 0;overflow:hidden}.bar i{display:block;height:100%;width:0;background:var(--acc)}
.vol{display:flex;align-items:center;gap:10px;margin:6px 0}.vol span{width:90px;color:var(--mut);font-size:14px}
.vol input{flex:1;accent-color:var(--acc);height:28px}.vol b{width:64px;text-align:right;font-size:14px;font-weight:600}
pre{white-space:pre-wrap;margin:0;font-family:inherit;font-size:14px;color:var(--mut)}
img#shot{display:none;width:100%;border-radius:12px;margin-top:10px}
#toast{position:fixed;left:50%;bottom:24px;transform:translateX(-50%) translateY(80px);background:#2f3550;color:#fff;
padding:12px 18px;border-radius:12px;max-width:90vw;transition:transform .25s;z-index:5}
#toast.show{transform:translateX(-50%) translateY(0)}
</style></head><body>
<header><h1>Lucy</h1><a href="/logout">Выйти</a></header>
<div class="status"><span class="pill" id="st-music">Музыка: —</span><span class="pill" id="st-game">Обычный режим</span></div>

{% for title, buttons in groups %}
<section><h2>{{ title }}</h2><div class="grid">
{% for name, label in buttons %}
<button data-run="{{ name }}" data-confirm="{{ confirms.get(name, '') }}"{% if title == 'Осторожно' %} class="danger"{% endif %}>{{ label }}</button>
{% endfor %}</div>{% if title == 'Компьютер' %}<img id="shot" alt="Скриншот">{% endif %}</section>
{% endfor %}

<section><h2>Громкость</h2>
<label class="vol"><span>Компьютер</span><input type="range" id="v-master" min="0" max="100" oninput="vol('master',this.value)"><b id="v-master-n">—</b></label>
<label class="vol"><span>Музыка</span><input type="range" id="v-music" min="0" max="100" oninput="vol('music',this.value)" disabled><b id="v-music-n">нет звука</b></label>
</section>

<section><h2>Быстро</h2>
<div class="row"><input type="text" id="q-search" placeholder="Найти в интернете"><button class="main" data-form="search" data-input="q-search">Найти</button></div>
<div class="row"><input type="text" id="q-timer" placeholder="Таймер: 10 минут"><button class="main" data-form="timer" data-input="q-timer">Пуск</button></div>
<div class="row"><input type="text" id="q-note" placeholder="Новая заметка"><button class="main" data-form="add_note" data-input="q-note">Записать</button></div>
</section>

<section><h2>Текст на ПК</h2>
<textarea id="clip" placeholder="Вставь сюда текст или ссылку, он окажется в буфере обмена ПК"></textarea>
<button class="main" onclick="sendClip()">В буфер ПК</button></section>

<section><h2>Файлы на ПК</h2>
<div class="row"><input type="file" id="file" multiple style="flex:1;color:var(--mut)"><button class="main" onclick="upload()">Отправить</button></div>
<div class="bar"><i id="bar"></i></div>
<div id="inbox"></div></section>

<section><h2>Файлы с ПК на телефон</h2><div id="outbox"></div></section>

<section><h2>Заметки</h2><pre id="notes">Загрузка…</pre></section>
<div id="toast"></div>
<script>
const $=s=>document.querySelector(s);
function toast(t){const e=$('#toast');e.textContent=t;e.classList.add('show');clearTimeout(window._t);window._t=setTimeout(()=>e.classList.remove('show'),3500)}
async function post(url,data){const r=await fetch(url,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(data)});
 if(r.status===401){location.href='/login';return {}}let j={};try{j=await r.json()}catch(e){}return Object.assign({ok:r.ok},j)}
async function run(name,param,ask){
 if(ask&&!confirm(ask))return;
 const r=await post('/api/run',{name:name,param:param||'',confirm:!!ask});
 toast(r.messages&&r.messages.length?r.messages.join(' · '):(r.error||(r.ok?'Готово':'Ошибка')));
 if(r.image){const i=$('#shot');i.src=r.image;i.style.display='block'}
 if(name==='add_note')notes();
 status();
}
document.addEventListener('click',e=>{
 const b=e.target.closest('button');if(!b)return;
 if(b.dataset.run!==undefined)run(b.dataset.run,'',b.dataset.confirm);
 if(b.dataset.form){const i=$('#'+b.dataset.input);if(!i.value.trim())return;run(b.dataset.form,i.value.trim(),'');i.value=''}
});
async function sendClip(){const t=$('#clip').value;if(!t.trim())return;const r=await post('/api/clipboard',{text:t});toast(r.ok?'Текст в буфере ПК':(r.error||'Ошибка'));if(r.ok)$('#clip').value=''}
function size(n){return n>1048576?(n/1048576).toFixed(1)+' МБ':n>1024?Math.round(n/1024)+' КБ':n+' Б'}
function fillList(el,rows,kind,canDelete){
 el.replaceChildren();if(!rows.length){el.innerHTML='<small style="color:var(--mut)">Пока пусто</small>';return}
 for(const f of rows){const d=document.createElement('div');d.className='file';
  const a=document.createElement('a');a.href='/files/'+kind+'/'+encodeURIComponent(f.name);a.textContent=f.name;
  const s=document.createElement('small');s.textContent=size(f.size);const l=document.createElement('div');l.append(a,document.createElement('br'),s);d.append(l);
  if(canDelete){const x=document.createElement('button');x.textContent='Удалить';x.onclick=async()=>{if(confirm('Удалить '+f.name+'?')){await post('/api/files/delete',{kind:kind,name:f.name});files()}};d.append(x)}
  el.append(d)}}
async function files(){const r=await (await fetch('/api/files')).json();fillList($('#inbox'),r.inbox,'in',true);fillList($('#outbox'),r.outbox,'out',true)}
function upload(){
 const fs=$('#file').files;if(!fs.length)return;const fd=new FormData();for(const f of fs)fd.append('files',f);
 const x=new XMLHttpRequest();x.open('POST','/api/upload');
 x.upload.onprogress=e=>{if(e.lengthComputable)$('#bar').style.width=(100*e.loaded/e.total)+'%'};
 x.onload=()=>{$('#bar').style.width='0';let j={};try{j=JSON.parse(x.responseText)}catch(e){}
  toast(x.status===200?'Загружено файлов: '+j.saved.length:(j.error||'Ошибка загрузки'));$('#file').value='';files()};
 x.onerror=()=>toast('Нет связи с ПК');x.send(fd)}
const volTimers={};
function vol(t,v){$('#v-'+t+'-n').textContent=v+'%';clearTimeout(volTimers[t]);
 volTimers[t]=setTimeout(async()=>{const r=await post('/api/volume',{target:t,level:+v});if(!r.ok)toast(r.error||'Не удалось изменить громкость')},120)}
async function loadVol(){
 if(document.hidden||(document.activeElement&&document.activeElement.type==='range'))return;
 try{const r=await (await fetch('/api/volume')).json();
  for(const t of ['master','music']){const s=$('#v-'+t),n=$('#v-'+t+'-n');
   if(r[t]===null||r[t]===undefined){s.disabled=true;n.textContent=t==='music'?'нет звука':'—'}
   else{s.disabled=false;s.value=r[t];n.textContent=r[t]+'%'}}}catch(e){}}
async function notes(){const r=await (await fetch('/api/notes')).json();$('#notes').textContent=r.text||'Заметок пока нет'}
async function status(){if(document.hidden)return;try{const r=await (await fetch('/api/status')).json();
 $('#st-music').textContent=r.track?((r.playing?'▶ ':'⏸ ')+r.track):'Музыка: —';
 const g=$('#st-game');g.textContent=r.light?'Лёгкий режим':'Обычный режим';g.classList.toggle('on',r.light)}catch(e){}}
files();notes();status();loadVol();setInterval(status,6000);setInterval(loadVol,6000);
</script></body></html>"""


def create_app(ctx):
    cfg = ctx.cfg
    base = cfg.web_files_dir
    inbox, outbox = os.path.join(base, "inbox"), os.path.join(base, "outbox")
    os.makedirs(inbox, exist_ok=True)
    os.makedirs(outbox, exist_ok=True)

    password = cfg.web_password
    if not password:
        password, created = load_or_create(os.path.join(ctx.base, "web_password.txt"),
                                           lambda: secrets.token_urlsafe(6))
        if created:
            print(f"» [Сайт] Пароль для входа: {password}  (он сохранён в web_password.txt)")
    secret, _ = load_or_create(os.path.join(ctx.base, "web_secret.key"),
                               lambda: secrets.token_hex(32))

    app = Flask(__name__)
    app.secret_key = secret
    app.config.update(MAX_CONTENT_LENGTH=cfg.web_max_mb * 1024 * 1024,
                      SESSION_COOKIE_HTTPONLY=True, SESSION_COOKIE_SAMESITE="Strict",
                      PERMANENT_SESSION_LIFETIME=timedelta(days=30))
    fails = {}
    status_cache = {"t": 0, "data": {}}
    lock = threading.Lock()

    @app.before_request
    def guard():
        try:
            ip = ipaddress.ip_address(request.remote_addr or "")
        except ValueError:
            abort(403)
        if not (ip.is_private or ip.is_loopback or cfg.web_allow_public):
            abort(403)
        if request.endpoint in ("login", "logout") or session.get("ok"):
            return None
        if request.path.startswith("/api/"):
            return jsonify(error="Нужен вход"), 401
        return redirect("/login")

    @app.after_request
    def headers(resp):
        resp.headers["Cache-Control"] = "no-store"
        resp.headers["X-Content-Type-Options"] = "nosniff"
        resp.headers["Referrer-Policy"] = "no-referrer"
        return resp

    @app.route("/login", methods=["GET", "POST"])
    def login():
        ip = request.remote_addr
        if request.method == "POST":
            count, until = fails.get(ip, [0, 0])
            if time.time() < until:
                return render_template_string(LOGIN_HTML, error="Слишком много попыток. Подождите 5 минут."), 429
            if hmac.compare_digest(request.form.get("password", "").encode(), password.encode()):
                fails.pop(ip, None)
                session.clear()
                session["ok"] = True
                session.permanent = True
                return redirect("/")
            count += 1
            fails[ip] = [count, time.time() + 300 if count >= 5 else 0]
            if count >= 5:
                fails[ip][0] = 0
            return render_template_string(LOGIN_HTML, error="Неверный пароль"), 401
        return render_template_string(LOGIN_HTML, error=None)

    @app.route("/logout")
    def logout():
        session.clear()
        return redirect("/login")

    @app.route("/")
    def panel():
        confirms = {n: c.confirm for c in matcher.COMMANDS for n in [c.name] if c.confirm}
        confirms.update(CONFIRMS)
        return render_template_string(PANEL_HTML, groups=GROUPS, confirms=confirms)

    @app.post("/api/run")
    def api_run():
        data = request.get_json(silent=True) or {}
        name, param = data.get("name", ""), str(data.get("param", "")).strip()
        if name not in ALLOWED or name not in HANDLERS:
            return jsonify(error="Неизвестная команда"), 400
        cmd = next((c for c in matcher.COMMANDS if c.name == name), None)
        ask = CONFIRMS.get(name) or (cmd.confirm if cmd else "")
        if ask and not data.get("confirm"):
            return jsonify(error="Нужно подтверждение"), 409
        if name == "search" and not param:
            return jsonify(error="Что искать?"), 400
        if name == "add_note" and not param:
            return jsonify(error="Что записать?"), 400
        if name == "timer" and matcher.parse_duration(param) <= 0:
            return jsonify(error="Не понял время, например: 10 минут"), 400
        wctx = WebCtx(ctx, cfg.web_speak)
        errors = []

        def work():
            try:
                HANDLERS[name](wctx, param)
            except Exception as e:
                errors.append(str(e))
        t = threading.Thread(target=work, daemon=True)
        t.start()
        t.join(timeout=3)
        if errors:
            return jsonify(error=f"Не получилось: {errors[0]}"), 500
        out = {"messages": wctx.messages, "done": not t.is_alive()}
        if name == "screenshot":
            out["image"] = f"/shot/latest?t={int(time.time())}"
        return jsonify(out)

    @app.get("/api/status")
    def api_status():
        with lock:
            if time.time() - status_cache["t"] > 5:
                data = {"light": ctx.light, "track": "", "playing": False}
                try:
                    import media
                    info = media.now_playing(cfg.media_match)
                    if info and info[1]:
                        data["track"] = f"{info[0]} — {info[1]}" if info[0] else info[1]
                        data["playing"] = bool(info[2])
                except Exception:
                    pass
                status_cache.update(t=time.time(), data=data)
            return jsonify(status_cache["data"])

    @app.get("/shot/latest")
    def shot_latest():
        folder = os.path.join(ctx.base, "screenshots")
        rows = [r for r in list_dir(folder) if r["name"].lower().endswith(".png")]
        if not rows:
            abort(404)
        return send_from_directory(folder, rows[0]["name"])

    music_exe = ntpath.basename(cfg.yandex_music) if cfg.yandex_music else "ya_m.exe"

    @app.get("/api/volume")
    def api_volume_get():
        import volume
        out = {}
        for key, getter in (("master", volume.get_master), ("music", lambda: volume.get_app(music_exe))):
            try:
                out[key] = getter()
            except Exception:
                out[key] = None
        return jsonify(out)

    @app.post("/api/volume")
    def api_volume_set():
        import volume
        data = request.get_json(silent=True) or {}
        try:
            level = int(data.get("level"))
        except (TypeError, ValueError):
            return jsonify(error="Нужно число от 0 до 100"), 400
        if not 0 <= level <= 100:
            return jsonify(error="Нужно число от 0 до 100"), 400
        try:
            if data.get("target") == "master":
                volume.set_master(level)
            elif data.get("target") == "music":
                if not volume.set_app(music_exe, level):
                    return jsonify(error="Музыка сейчас не играет, регулировать нечего"), 409
            else:
                return jsonify(error="Неизвестная цель"), 400
        except Exception as e:
            return jsonify(error=f"Не удалось: {e}"), 500
        return jsonify(ok=True)

    @app.get("/api/notes")
    def api_notes():
        path = os.path.join(ctx.base, "notes.txt")
        text = ""
        if os.path.exists(path):
            with open(path, encoding="utf-8") as f:
                text = f.read().strip()
        return jsonify(text=text)

    @app.post("/api/clipboard")
    def api_clipboard():
        text = str((request.get_json(silent=True) or {}).get("text", ""))
        if not text.strip():
            return jsonify(error="Пустой текст"), 400
        if len(text) > 100000:
            return jsonify(error="Слишком длинный текст"), 400
        try:
            import pyperclip
            pyperclip.copy(text)
        except Exception as e:
            return jsonify(error=f"Не удалось: {e}"), 500
        return jsonify(ok=True)

    def folder_for(kind):
        return {"in": inbox, "out": outbox}.get(kind)

    @app.get("/api/files")
    def api_files():
        return jsonify(inbox=list_dir(inbox), outbox=list_dir(outbox))

    @app.post("/api/upload")
    def api_upload():
        saved = []
        for f in request.files.getlist("files"):
            if not f.filename:
                continue
            path = unique_path(inbox, safe_name(f.filename))
            f.save(path)
            saved.append(os.path.basename(path))
        if not saved:
            return jsonify(error="Файлы не выбраны"), 400
        print(f"» [Сайт] Получено файлов: {len(saved)} (папка {inbox})")
        return jsonify(saved=saved)

    @app.get("/files/<kind>/<path:name>")
    def files_get(kind, name):
        folder = folder_for(kind)
        if not folder or name != os.path.basename(name):
            abort(404)
        return send_from_directory(folder, name, as_attachment=True)

    @app.post("/api/files/delete")
    def files_delete():
        data = request.get_json(silent=True) or {}
        folder, name = folder_for(data.get("kind")), str(data.get("name", ""))
        if not folder or name != os.path.basename(name):
            return jsonify(error="Нельзя"), 400
        path = os.path.join(folder, name)
        if os.path.isfile(path):
            os.remove(path)
        return jsonify(ok=True)

    @app.errorhandler(413)
    def too_big(_):
        return jsonify(error=f"Файл больше {cfg.web_max_mb} МБ"), 413

    return app


def port_is_free(port):
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        s.bind(("0.0.0.0", port))
        return True
    except OSError:
        return False
    finally:
        s.close()


class WebServer:
    def __init__(self, ctx):
        self.ctx = ctx

    def start(self):
        cfg = self.ctx.cfg
        if not port_is_free(cfg.web_port):
            print(f"» [Сайт] Порт {cfg.web_port} занят другой программой. "
                  "Поменяйте PORT в разделе [WEB] файла config.txt.")
            return False
        try:
            app = create_app(self.ctx)
            self.server = make_server("0.0.0.0", cfg.web_port, app, threaded=True)
        except (OSError, SystemExit) as e:
            print(f"» [Сайт] Не удалось запустить на порту {cfg.web_port}: {e}")
            return False
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        print("» [Сайт] Откройте на телефоне (нужен адрес вашей Wi-Fi сети, обычно 192.168.x.x):")
        for iface, ip in lan_ips() or [("?", "127.0.0.1")]:
            print(f"      http://{ip}:{cfg.web_port}    ({iface})")
        print("   Не открывается? Выполните: python lucy.py --web-check")
        return True


def _ps(script, timeout=40):
    r = subprocess.run(["powershell", "-NoProfile", "-Command",
                        "[Console]::OutputEncoding = [System.Text.Encoding]::UTF8; " + script],
                       capture_output=True, text=True, encoding="utf-8", errors="replace",
                       timeout=timeout, creationflags=0x08000000)
    return [ln.strip().lstrip("\ufeff") for ln in r.stdout.splitlines() if ln.strip()]


def web_check(port):
    print("1) Адреса этого ПК. Телефону нужен адрес из той же Wi-Fi сети (обычно 192.168.x.x):")
    for iface, ip in lan_ips() or [("?", "не найден")]:
        print(f"     http://{ip}:{port}    ({iface})")
    print("     Набирайте именно http:// (не https://).")

    print("\n2) Тип сети. Должен быть Private (Частная), в «Общедоступной» Windows закрывает входящие подключения:")
    try:
        rows = _ps("Get-NetConnectionProfile | ForEach-Object { $_.InterfaceAlias + ' | ' + $_.NetworkCategory }")
        for ln in rows or ["(не удалось определить)"]:
            print("     " + ln + ("   <-- ПРОБЛЕМА: сеть общедоступная" if "Public" in ln else ""))
        if any("Public" in ln for ln in rows):
            print("     Исправить: Параметры > Сеть и Интернет > Wi-Fi > свойства вашей сети >"
                  " Тип сетевого профиля: Частная.")
    except Exception as e:
        print("     не удалось проверить:", e)

    print(f"\n3) Разрешающее правило брандмауэра для порта {port}:")
    try:
        rows = _ps("(Get-NetFirewallRule -DisplayName 'Lucy web' -ErrorAction SilentlyContinue | Measure-Object).Count")
        if rows and rows[0].isdigit() and int(rows[0]) > 0:
            print("     есть (Lucy web)")
        else:
            print("     НЕТ. Создайте: python lucy.py --open-firewall")
    except Exception as e:
        print("     не удалось проверить:", e)

    print("\n4) Правила брандмауэра для Python (запрещающие входящие блокируют сайт, даже если есть разрешающее):")
    try:
        rows = _ps("Get-NetFirewallApplicationFilter | Where-Object { $_.Program -like '*python*' } | "
                   "ForEach-Object { $r = $_ | Get-NetFirewallRule; "
                   "$r.DisplayName + ' | ' + $r.Direction + ' | ' + $r.Action + ' | вкл: ' + $r.Enabled + ' | ' + $r.Profile }")
        bad = [ln for ln in rows if "Inbound" in ln and "Block" in ln and "True" in ln]
        for ln in rows or ["(правил для Python нет)"]:
            print("     " + ln + ("   <-- ЭТО БЛОКИРУЕТ" if ln in bad else ""))
        if bad:
            print("     Откройте «Брандмауэр Защитника Windows в режиме повышенной безопасности»"
                  " (wf.msc) > Правила для входящих подключений, найдите правило с таким названием"
                  " (действие «Блокировать») и отключите или удалите его.")
    except Exception as e:
        print("     не удалось проверить:", e)

    print("\n5) Если всё выше в порядке, а телефон не открывает:")
    print("     - телефон должен быть в той же Wi-Fi сети, не в «гостевой» (в ней устройства изолированы)")
    print("       и не в мобильном интернете; выключите VPN на телефоне;")
    print("     - в роутере может быть включена «изоляция клиентов» (AP isolation);")
    print("     - антивирус со своим сетевым экраном (Kaspersky и др.) тоже может блокировать.")


def open_firewall(port):
    cmd = ("Remove-NetFirewallRule -DisplayName 'Lucy web' -ErrorAction SilentlyContinue; "
           f"New-NetFirewallRule -DisplayName 'Lucy web' -Direction Inbound -Protocol TCP "
           f"-LocalPort {port} -Action Allow -Profile Private,Domain | Out-Null; "
           "Write-Host 'Готово: порт открыт для частных сетей'; Start-Sleep 3")
    print("Сейчас Windows спросит разрешение на запуск от имени администратора. Согласитесь.")
    ctypes.windll.shell32.ShellExecuteW(None, "runas", "powershell.exe",
                                        f'-NoProfile -Command "{cmd}"', None, 1)
    print("Правило создаётся в отдельном окне. Потом проверьте: python lucy.py --web-check")
