"""CM Maids — formulário "Trabalhe conosco" (EN/PT/ES) com pontuação, SQLite, e-mail (Resend) e Meta Pixel/CAPI."""
import csv
import hashlib
import io
import json
import os
import re
import secrets
import sqlite3
import threading
import time
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

from fastapi import BackgroundTasks, Depends, FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from fastapi.security import HTTPBasic, HTTPBasicCredentials
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, field_validator

BASE = Path(__file__).parent
# segredos ficam num arquivo dentro do volume (fora do repo e fora do painel); env do painel tem prioridade
_secrets = Path(os.environ.get("SECRETS_FILE", "/data/secrets.env"))
if _secrets.is_file():
    for _line in _secrets.read_text(encoding="utf-8").splitlines():
        if "=" in _line and not _line.lstrip().startswith("#"):
            _k, _v = _line.split("=", 1)
            os.environ.setdefault(_k.strip(), _v.strip())
DB_PATH = os.environ.get("DB_PATH", "/data/applications.db")
RESEND_API_KEY = os.environ.get("RESEND_API_KEY", "")
NOTIFY_TO = [e.strip() for e in os.environ.get("NOTIFY_TO", "").split(",") if e.strip()]
NOTIFY_FROM = os.environ.get("NOTIFY_FROM", "CM Maids <vagas@cmdigitalbr.com>")
REPLY_TO = os.environ.get("REPLY_TO", "contact@cmmaids.com")
ADMIN_USER = os.environ.get("ADMIN_USER", "admin")
ADMIN_PASS = os.environ.get("ADMIN_PASS", "")
META_PIXEL_ID = os.environ.get("META_PIXEL_ID", "1656588805885650")
META_CAPI_TOKEN = os.environ.get("META_CAPI_TOKEN", "")  # opcional: Conversions API (server-side Purchase)
PUBLIC_URL = os.environ.get("PUBLIC_URL", "https://cmmaids.com").rstrip("/")

SLUGS = {"pt": "trabalhe-conosco", "en": "work-with-us", "es": "trabaja-con-nosotros"}
DAYS = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]
SHIFT = (8 * 60 + 30, 16 * 60)  # turno 8:30–16:00


def _min(hhmm: str) -> int:
    h, m = hhmm.split(":")
    return int(h) * 60 + int(m)


def availability_of(days: list[str], hours_from: str, hours_to: str) -> str:
    """full = seg–sex inteiros cobrindo 8:30–16:00; partial = algum dia com sobreposição; no = nada."""
    f, t = _min(hours_from), _min(hours_to)
    if t <= f or not days or t <= SHIFT[0] or f >= SHIFT[1]:
        return "no"
    if all(d in days for d in DAYS[:5]) and f <= SHIFT[0] and t >= SHIFT[1]:
        return "full"
    return "partial"

# ---- pontuação (fonte única: servidor) ----
POINTS = {
    "experience": {"none": 0, "lt1": 1, "1to3": 2, "gt3": 3},
    "has_car": {"yes": 3, "no": 0},
    "availability": {"full": 3, "partial": 1, "no": 0},
    "document": {"ssn": 3, "itin": 3, "none": 0},
    "supplies": {"yes": 1, "no": 0},
    "time_in_us": {"lt6m": 0, "6to12m": 1, "1to3y": 2, "gt3y": 3},
}
MAX_SCORE = sum(max(v.values()) for v in POINTS.values())  # 16


def score_and_bucket(a: dict) -> tuple[int, str]:
    score = sum(POINTS[k][a[k]] for k in POINTS)
    if a["document"] == "none":
        bucket = "nao_qualificado"  # sem SSN/ITIN vai pro outro lugar
    elif a["has_car"] == "yes" and a["availability"] == "full" and score >= 10:
        bucket = "qualificado"
    else:
        bucket = "potencial"
    return score, bucket


# ---- banco ----
_db_lock = threading.Lock()


def db() -> sqlite3.Connection:
    Path(DB_PATH).parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(DB_PATH, check_same_thread=False)
    con.row_factory = sqlite3.Row
    return con


def init_db():
    with db() as con:
        con.execute(
            """CREATE TABLE IF NOT EXISTS applications (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            created_at TEXT NOT NULL,
            lang TEXT, name TEXT, phone TEXT, email TEXT, instagram TEXT, facebook TEXT,
            city TEXT, zip TEXT, experience TEXT, has_car TEXT, availability TEXT,
            days TEXT, hours_from TEXT, hours_to TEXT,
            document TEXT, supplies TEXT, time_in_us TEXT,
            score INTEGER, bucket TEXT, event_id TEXT,
            ip TEXT, user_agent TEXT, fbp TEXT, fbc TEXT, query TEXT)"""
        )
        have = {r[1] for r in con.execute("PRAGMA table_info(applications)")}
        for col in ("days", "hours_from", "hours_to"):
            if col not in have:
                con.execute(f"ALTER TABLE applications ADD COLUMN {col} TEXT")


init_db()

# ---- app ----
app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)
app.mount("/static", StaticFiles(directory=BASE / "static"), name="static")

PAGE_META = {
    "en": ("Work with us — CM Maids", "Join the CM Maids team. Apply in 2 minutes."),
    "pt": ("Trabalhe conosco — CM Maids", "Faça parte do time CM Maids. Candidate-se em 2 minutos."),
    "es": ("Trabaja con nosotros — CM Maids", "Únete al equipo CM Maids. Postúlate en 2 minutos."),
}


def render_form(lang: str) -> HTMLResponse:
    title, desc = PAGE_META[lang]
    html = (
        (BASE / "form.html").read_text(encoding="utf-8").replace("{{LANG}}", lang)
        .replace("{{TITLE}}", title)
        .replace("{{DESC}}", desc)
        .replace("{{PIXEL_ID}}", META_PIXEL_ID)
        .replace("{{PUBLIC_URL}}", PUBLIC_URL)
    )
    return HTMLResponse(html, headers={"Cache-Control": "no-cache"})


@app.middleware("http")
async def limit_body(request: Request, call_next):
    if request.method == "POST" and int(request.headers.get("content-length") or 0) > 64_000:
        return Response("payload too large", status_code=413)
    return await call_next(request)


@app.get("/")
def root(request: Request):
    accept = request.headers.get("accept-language", "").lower()
    lang = "pt" if accept.startswith("pt") else "es" if accept.startswith("es") else "en"
    q = request.url.query
    return RedirectResponse(f"/{SLUGS[lang]}" + (f"?{q}" if q else ""), status_code=302)


for _lang, _slug in SLUGS.items():
    app.add_api_route(f"/{_slug}", (lambda lang: (lambda: render_form(lang)))(_lang), methods=["GET"])


@app.get("/health")
def health():
    return {"ok": True}


# ---- candidatura ----
class Application(BaseModel):
    lang: str
    name: str
    phone: str
    email: str = ""
    instagram: str = ""
    facebook: str = ""
    city: str
    zip: str = ""
    experience: str
    has_car: str
    days: list[str]
    hours_from: str
    hours_to: str
    document: str
    supplies: str
    time_in_us: str
    website: str = ""  # honeypot: humano deixa vazio
    fbp: str = ""
    fbc: str = ""
    query: str = ""

    @field_validator("lang")
    @classmethod
    def _lang(cls, v):
        if v not in SLUGS:
            raise ValueError("lang")
        return v

    @field_validator("name", "city")
    @classmethod
    def _text(cls, v):
        v = " ".join(v.split())
        if not 2 <= len(v) <= 100:
            raise ValueError("length")
        return v

    @field_validator("phone")
    @classmethod
    def _phone(cls, v):
        digits = re.sub(r"\D", "", v).lstrip("0")
        if len(digits) == 10:
            digits = "1" + digits  # número dos EUA sem o código do país
        if not 8 <= len(digits) <= 15:
            raise ValueError("phone")
        return digits

    @field_validator("email")
    @classmethod
    def _email(cls, v):
        v = v.strip().lower()
        if v and not re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", v):
            raise ValueError("email")
        return v[:120]

    @field_validator("zip")
    @classmethod
    def _zip(cls, v):
        v = re.sub(r"\D", "", v)
        if v and len(v) != 5:
            raise ValueError("zip")
        return v

    @field_validator("instagram")
    @classmethod
    def _ig(cls, v):
        v = re.sub(r"^(https?://)?(www\.)?instagram\.com/", "", v.strip(), flags=re.I).split("?")[0].strip("/@ ")
        return v[:60]

    @field_validator("facebook", "fbp", "fbc", "query", "website")
    @classmethod
    def _opt(cls, v):
        return v.strip()[:300]

    @field_validator("days")
    @classmethod
    def _days(cls, v):
        v = [d for d in DAYS if d in v]
        if not v:
            raise ValueError("days")
        return v

    @field_validator("hours_from", "hours_to")
    @classmethod
    def _hhmm(cls, v):
        if not re.fullmatch(r"([01]\d|2[0-3]):[0-5]\d", v):
            raise ValueError("time")
        return v

    @field_validator("experience", "has_car", "document", "supplies", "time_in_us")
    @classmethod
    def _enum(cls, v, info):
        if v not in POINTS[info.field_name]:
            raise ValueError(info.field_name)
        return v


# ponytail: rate limit em memória por IP (1 worker); se escalar workers, mover pra sqlite
_hits: dict[str, list[float]] = {}


def rate_limited(ip: str, limit=8, window=600) -> bool:
    now = time.time()
    hits = [t for t in _hits.get(ip, []) if now - t < window]
    _hits[ip] = hits + [now]
    return len(hits) >= limit


def client_ip(request: Request) -> str:
    fwd = request.headers.get("x-forwarded-for", "")
    return fwd.split(",")[0].strip() if fwd else (request.client.host if request.client else "")


@app.post("/api/apply")
def apply(a: Application, request: Request, bg: BackgroundTasks):
    if a.website:  # bot preencheu o honeypot: finge sucesso
        return {"ok": True, "score": 0, "bucket": "potencial", "event_id": secrets.token_hex(8)}
    ip = client_ip(request)
    if rate_limited(ip):
        raise HTTPException(429, "too many requests")
    data = a.model_dump(exclude={"website"})
    data["availability"] = availability_of(data["days"], data["hours_from"], data["hours_to"])
    data["days"] = ",".join(data["days"])
    data["score"], data["bucket"] = score_and_bucket(data)
    data["event_id"] = secrets.token_hex(8)
    data["created_at"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
    data["ip"] = ip
    data["user_agent"] = request.headers.get("user-agent", "")[:300]
    cols = ", ".join(data)
    with _db_lock, db() as con:
        cur = con.execute(f"INSERT INTO applications ({cols}) VALUES ({', '.join('?' for _ in data)})", list(data.values()))
        data["id"] = cur.lastrowid
    bg.add_task(notify_email, data)
    bg.add_task(send_capi, data)
    return {"ok": True, "score": data["score"], "bucket": data["bucket"], "availability": data["availability"], "event_id": data["event_id"], "max": MAX_SCORE}


# ---- rótulos (PT, pro time) ----
LABELS = {
    "experience": {"none": "Nenhuma", "lt1": "Menos de 1 ano", "1to3": "1 a 3 anos", "gt3": "Mais de 3 anos"},
    "has_car": {"yes": "Sim", "no": "Não"},
    "availability": {"full": "Total (8:30–4pm)", "partial": "Parcial", "no": "Não"},
    "days": {"mon": "Seg", "tue": "Ter", "wed": "Qua", "thu": "Qui", "fri": "Sex", "sat": "Sáb", "sun": "Dom"},
    "document": {"ssn": "SSN", "itin": "ITIN", "none": "Nenhum"},
    "supplies": {"yes": "Sim", "no": "Não"},
    "time_in_us": {"lt6m": "Menos de 6 meses", "6to12m": "6 meses a 1 ano", "1to3y": "1 a 3 anos", "gt3y": "Mais de 3 anos"},
    "bucket": {"qualificado": "✅ Qualificada", "potencial": "🟡 Potencial", "nao_qualificado": "🔴 Não qualificada"},
    "lang": {"pt": "Português", "en": "English", "es": "Español"},
}


def label(field: str, value: str) -> str:
    if field == "days":
        return "/".join(LABELS["days"][d] for d in value.split(",") if d in LABELS["days"]) if value else "—"
    return LABELS.get(field, {}).get(value, value or "—")


def ampm(v: str) -> str:
    h, m = _min(v) // 60, _min(v) % 60
    return f"{h % 12 or 12}:{m:02d} {'AM' if h < 12 else 'PM'}"


def fmt_hours(d: dict) -> str:
    return f'{label("days", d["days"])} {ampm(d["hours_from"])}–{ampm(d["hours_to"])}'


def _post_json(url: str, payload: dict, headers: dict, timeout=15) -> str:
    req = urllib.request.Request(url, data=json.dumps(payload).encode(), headers={"Content-Type": "application/json", "User-Agent": "cmmaids-form/1.0", **headers})  # Resend bloqueia o UA padrão do urllib (403)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read().decode()


def notify_email(d: dict):
    if not RESEND_API_KEY or not NOTIFY_TO:
        return
    esc = lambda s: (s or "—").replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
    phone = d["phone"]
    ig = d["instagram"]
    fb = d["facebook"]
    fb_href = fb if fb.startswith("http") else f"https://www.facebook.com/search/top?q={urllib.request.quote(fb)}"
    rows = [
        ("Nome", esc(d["name"])),
        ("Telefone", f'<a href="tel:+{phone}">+{esc(phone)}</a> &nbsp;·&nbsp; <a href="https://wa.me/{phone}">WhatsApp</a>'),
        ("E-mail", esc(d["email"])),
        ("Cidade / ZIP", esc(f'{d["city"]} {d["zip"]}'.strip())),
        ("Experiência", label("experience", d["experience"])),
        ("Tem carro", label("has_car", d["has_car"])),
        ("Disponível 8:30–4pm", f'{label("availability", d["availability"])} — {esc(fmt_hours(d))}'),
        ("SSN / ITIN", label("document", d["document"])),
        ("Material de limpeza", label("supplies", d["supplies"])),
        ("Tempo nos EUA", label("time_in_us", d["time_in_us"])),
        ("Instagram", f'<a href="https://instagram.com/{urllib.request.quote(ig)}">@{esc(ig)}</a>' if ig else "—"),
        ("Facebook", f'<a href="{esc(fb_href)}">{esc(fb)}</a>' if fb else "—"),
        ("Idioma do formulário", label("lang", d["lang"])),
        ("Origem (UTM)", esc(d["query"])),
    ]
    table = "".join(
        f'<tr><td style="padding:6px 10px;color:#6b7280;white-space:nowrap">{k}</td><td style="padding:6px 10px;font-weight:600">{v}</td></tr>'
        for k, v in rows
    )
    color = {"qualificado": "#16a34a", "potencial": "#d97706", "nao_qualificado": "#dc2626"}[d["bucket"]]
    html = f"""<div style="font-family:Arial,sans-serif;max-width:560px;margin:auto;color:#111">
    <h2 style="margin:0 0 4px">Nova candidatura — CM Maids</h2>
    <p style="margin:0 0 14px"><span style="background:{color};color:#fff;padding:4px 10px;border-radius:999px;font-weight:700">{label("bucket", d["bucket"])}</span>
    &nbsp; Pontuação: <b>{d["score"]}/{MAX_SCORE}</b></p>
    <table style="border-collapse:collapse;width:100%;font-size:14px">{table}</table>
    <p style="margin-top:16px;font-size:13px;color:#6b7280">Todas as candidatas: <a href="{PUBLIC_URL}/admin">{PUBLIC_URL}/admin</a></p></div>"""
    try:
        _post_json(
            "https://api.resend.com/emails",
            {"from": NOTIFY_FROM, "to": NOTIFY_TO, "reply_to": REPLY_TO, "subject": f'{label("bucket", d["bucket"])} {d["name"]} — {d["score"]}/{MAX_SCORE} — CM Maids', "html": html},
            {"Authorization": f"Bearer {RESEND_API_KEY}"},
        )
    except Exception as e:  # e-mail é notificação; a candidatura já está salva
        print("resend error:", e, flush=True)


def _sha(v: str) -> list[str]:
    v = v.strip().lower()
    return [hashlib.sha256(v.encode()).hexdigest()] if v else []


def send_capi(d: dict):
    """Purchase via Conversions API (dedup com o pixel pelo event_id). Só roda se META_CAPI_TOKEN estiver setado."""
    if not META_CAPI_TOKEN:
        return
    parts = d["name"].split()
    user = {
        "em": _sha(d["email"]), "ph": _sha(d["phone"]), "fn": _sha(parts[0]), "ln": _sha(parts[-1] if len(parts) > 1 else ""),
        "ct": _sha(re.sub(r"[^a-z]", "", d["city"].lower())), "zp": _sha(d["zip"]), "country": _sha("us"),
        "client_ip_address": d["ip"], "client_user_agent": d["user_agent"],
        **({"fbp": d["fbp"]} if d["fbp"] else {}), **({"fbc": d["fbc"]} if d["fbc"] else {}),
    }
    payload = {"data": [{
        "event_name": "Purchase", "event_time": int(time.time()), "event_id": d["event_id"], "action_source": "website",
        "event_source_url": f"{PUBLIC_URL}/{SLUGS[d['lang']]}",
        "user_data": {k: v for k, v in user.items() if v},
        "custom_data": {"currency": "USD", "value": d["score"], "content_name": "helper-application", "content_category": d["bucket"]},
    }]}
    try:
        _post_json(f"https://graph.facebook.com/v21.0/{META_PIXEL_ID}/events?access_token={META_CAPI_TOKEN}", payload, {})
    except Exception as e:
        print("capi error:", e, flush=True)


# ---- admin ----
security = HTTPBasic()


def admin_auth(creds: HTTPBasicCredentials = Depends(security)):
    ok = ADMIN_PASS and secrets.compare_digest(creds.username, ADMIN_USER) and secrets.compare_digest(creds.password, ADMIN_PASS)
    if not ok:
        raise HTTPException(401, "Unauthorized", headers={"WWW-Authenticate": "Basic"})


def all_rows() -> list[dict]:
    with db() as con:
        return [dict(r) for r in con.execute("SELECT * FROM applications ORDER BY id DESC")]


@app.get("/admin", dependencies=[Depends(admin_auth)])
def admin():
    rows = all_rows()
    for r in rows:
        r.pop("ip", None); r.pop("user_agent", None); r.pop("fbp", None); r.pop("fbc", None)
    data = json.dumps(rows).replace("<", "\\u003c").replace(">", "\\u003e").replace("&", "\\u0026")  # nada de HTML dentro do <script>
    html = (BASE / "admin.html").read_text(encoding="utf-8").replace("{{DATA}}", data).replace("{{MAX}}", str(MAX_SCORE))
    return HTMLResponse(html, headers={"Cache-Control": "no-store"})


@app.get("/admin/export.csv", dependencies=[Depends(admin_auth)])
def export_csv():
    rows = all_rows()
    buf = io.StringIO()
    fields = ["id", "created_at", "bucket", "score", "name", "phone", "email", "city", "zip", "experience", "has_car",
              "availability", "days", "hours_from", "hours_to", "document", "supplies", "time_in_us", "instagram", "facebook", "lang", "query"]
    w = csv.writer(buf)
    w.writerow(fields)
    safe = lambda v: "'" + v if isinstance(v, str) and v[:1] in "=+-@\t\r" else v
    for r in rows:
        w.writerow([safe(label(f, r[f]) if f in LABELS else ampm(r[f]) if f.startswith("hours_") and r[f] else r[f]) for f in fields])
    return Response("﻿" + buf.getvalue(), media_type="text/csv; charset=utf-8",
                    headers={"Content-Disposition": "attachment; filename=candidatas-cmmaids.csv"})


@app.delete("/admin/api/applications/{app_id}", dependencies=[Depends(admin_auth)])
def delete_application(app_id: int):
    with _db_lock, db() as con:
        con.execute("DELETE FROM applications WHERE id = ?", (app_id,))
    return {"ok": True}
