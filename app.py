"""CM Maids — formulário "Trabalhe conosco" (EN/PT/ES) com pontuação, SQLite, e-mail (Resend) e Meta Pixel/CAPI."""
import hashlib
import html
import json
import os
import re
import secrets
import sqlite3
import threading
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

from fastapi import BackgroundTasks, FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response
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
META_PIXEL_ID = os.environ.get("META_PIXEL_ID", "1656588805885650")
META_CAPI_TOKEN = os.environ.get("META_CAPI_TOKEN", "")  # opcional: Conversions API (server-side Purchase)
PUBLIC_URL = os.environ.get("PUBLIC_URL", "https://cmmaids.com").rstrip("/")
# Plataforma operacional (operacionalcm.com): a candidata cai na aba Candidatas pra a
# Talita trabalhar. Entra por uma porta estreita — uma função que só sabe inserir
# candidata — e não pela chave-mestra do banco, que abre os outros apps do Erik.
PLATAFORMA_URL = os.environ.get("PLATAFORMA_URL", "https://wuvdbripwlkjpopwlzbm.supabase.co/functions/v1/cr-candidata")
PLATAFORMA_TOKEN = os.environ.get("PLATAFORMA_TOKEN", "")
PLATAFORMA_PAINEL = os.environ.get("PLATAFORMA_PAINEL", "https://operacionalcm.com")

SLUGS = {"pt": "trabalhe-conosco", "en": "work-with-us", "es": "trabaja-con-nosotros"}
DAYS = ["sun", "mon", "tue", "wed", "thu", "fri", "sat"]
WEEKDAYS = ["mon", "tue", "wed", "thu", "fri"]
SHIFT = (8 * 60 + 30, 16 * 60)  # turno 8:30–16:00


def _min(hhmm: str) -> int:
    h, m = hhmm.split(":")
    return int(h) * 60 + int(m)


def availability_of(days: list[str], hours_from: str, hours_to: str) -> str:
    """full = seg–sex inteiros cobrindo 8:30–16:00; partial = algum dia com sobreposição; no = nada."""
    f, t = _min(hours_from), _min(hours_to)
    if t <= f or not days or t <= SHIFT[0] or f >= SHIFT[1]:
        return "no"
    if all(d in days for d in WEEKDAYS) and f <= SHIFT[0] and t >= SHIFT[1]:
        return "full"
    return "partial"

# ---- pontuação (fonte única: servidor) ----
POINTS = {
    "experience": {"none": 0, "lt1": 1, "1to3": 2, "gt3": 3},
    "has_car": {"yes": 3, "no": 0},
    "license": {"yes": 1, "no": 0, "na": 0},  # só pesa pra quem tem carro
    "availability": {"full": 3, "partial": 1, "no": 0},
    "document": {"ssn": 3, "itin": 3, "none": 0},
    "supplies": {"yes": 1, "no": 0},
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
            document TEXT, supplies TEXT, license TEXT,
            score INTEGER, bucket TEXT, event_id TEXT,
            ip TEXT, user_agent TEXT, fbp TEXT, fbc TEXT, query TEXT)"""
        )
        have = {r[1] for r in con.execute("PRAGMA table_info(applications)")}
        for col in ("days", "hours_from", "hours_to", "license"):
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
def root():
    """Site institucional. Os 3 formulários continuam nos slugs de sempre (anúncio aponta pra eles)."""
    html = (BASE / "site.html").read_text(encoding="utf-8").replace("{{PUBLIC_URL}}", PUBLIC_URL)
    return HTMLResponse(html, headers={"Cache-Control": "no-cache"})


def render_page(title: str, desc: str, content: str, *, status: int = 200, robots: str = "index,follow",
                extra_css: str = "", headers: dict | None = None) -> HTMLResponse:
    """Páginas simples (Privacy, Terms, invoice, 404) no molde page.html: mesmo topo e rodapé do site."""
    html = (
        (BASE / "page.html").read_text(encoding="utf-8")
        .replace("{{TITLE}}", title).replace("{{DESC}}", desc).replace("{{ROBOTS}}", robots)
        .replace("{{EXTRA_CSS}}", extra_css).replace("{{CONTENT}}", content)
    )
    return HTMLResponse(html, status_code=status, headers={"Cache-Control": "no-cache", **(headers or {})})


# Exigidas pelo registro de SMS (A2P 10DLC) da Twilio: precisam ficar públicas nestes endereços.
@app.get("/privacy")
def privacy():
    return render_page("Privacy Policy | CM Maids", "How CM Maids collects and uses your information, including text messages.",
                       (BASE / "legal" / "privacy.html").read_text(encoding="utf-8"))


@app.get("/terms")
def terms():
    return render_page("Terms | CM Maids", "Terms of the CM Maids text message program (billing notifications).",
                       (BASE / "legal" / "terms.html").read_text(encoding="utf-8"))


# ---------- página pública do invoice (leva 7): o [link] do SMS de lembrete ----------
# Os dados vêm da edge cr-invoice-publico (só o que o PDF imprime). Código ruim ou desconhecido dá o
# mesmo 404 do site, sem dizer se o invoice existe. noindex e no-store: não entra no Google nem em cache.
INVOICE_API = os.environ.get("INVOICE_API", "https://wuvdbripwlkjpopwlzbm.supabase.co/functions/v1/cr-invoice-publico")
_SEM_CACHE = {"X-Robots-Tag": "noindex, nofollow", "Cache-Control": "no-store"}

INVOICE_CSS = """
.iv-wrap{width:min(860px,100% - 32px);margin:28px auto 0}
.iv-acoes{display:flex;gap:10px;align-items:center;justify-content:space-between;flex-wrap:wrap;margin-bottom:14px}
.iv-selo{font:700 13px Inter,sans-serif;padding:7px 14px;border-radius:999px;letter-spacing:.02em}
.iv-selo.pago{background:#e3f4e9;color:#15803d}
.iv-selo.aberto{background:#fde8ee;color:#b42345}
.iv-sheet{background:#fff;border-radius:16px;overflow:hidden;box-shadow:0 10px 40px rgba(0,10,31,.12);color:#374151;font-family:Helvetica,Arial,sans-serif;-webkit-print-color-adjust:exact;print-color-adjust:exact}
.iv-band{background:#0B1F52;border-bottom:6px solid #E8B4C4;display:flex;align-items:center;justify-content:space-between;gap:16px;padding:22px 28px}
.iv-band img{width:76px;height:76px;border-radius:50%;box-shadow:0 0 0 2px rgba(232,180,196,.35)}
.iv-ttl{text-align:right;color:#fff}
.iv-ttl b{display:block;font-size:28px;letter-spacing:.01em;line-height:1}
.iv-ttl span{display:block;color:#E8B4C4;font-size:13px;margin-top:8px}
.iv-body{padding:26px 28px 22px}
.iv-meta{display:grid;grid-template-columns:1.4fr 1fr 1fr;gap:18px 22px}
.iv-lab{font-size:11px;font-weight:700;color:#6B7280;text-transform:uppercase;letter-spacing:.04em}
.iv-val{font-size:14px;margin-top:4px;white-space:pre-line;overflow-wrap:anywhere}
.iv-gap{margin-top:14px}
.iv-cliente{font-size:17px;font-weight:700;color:#0B1F52;margin-top:4px;overflow-wrap:anywhere}
.iv-big{font-size:19px;font-weight:700;color:#0B1F52;margin-top:4px}
table.iv{width:100%;border-collapse:collapse;margin-top:24px}
table.iv th{background:#0B1F52;color:#fff;font-size:11px;letter-spacing:.04em;padding:9px 10px;text-align:left}
table.iv td{padding:10px;border-bottom:1px solid #E6E1E8;vertical-align:top;font-size:14px}
table.iv tr:nth-child(even) td{background:#F6F2F5}
table.iv .r{text-align:right;white-space:nowrap}
table.iv .c{text-align:center}
table.iv td.r:last-child{font-weight:700;color:#0B1F52}
.iv-nm{font-weight:700;color:#0B1F52}
.iv-ds{font-size:12.5px;color:#6B7280;margin-top:3px;overflow-wrap:anywhere}
.iv-bonus{color:#0016bd;font-weight:700;font-size:12px;margin-left:5px}
.iv-tot{display:flex;justify-content:flex-end;margin-top:18px}
.iv-totbox{background:#0B1F52;color:#fff;display:flex;justify-content:space-between;align-items:center;gap:24px;padding:12px 16px;min-width:min(320px,100%)}
.iv-totbox b:last-child{font-size:19px}
.iv-pay{margin-top:22px}
.iv-note{font-size:12.5px;color:#6B7280;margin-top:8px;white-space:pre-line}
.iv-online{margin-top:12px}
.iv-foot{border-top:1px solid #E6E1E8;margin-top:24px;padding-top:10px;font-size:12px;color:#6B7280;display:flex;flex-wrap:wrap;justify-content:space-between;gap:6px}
.iv-foot b{color:#0B1F52}
.iv-ajuda{color:var(--muted);font-size:13.5px;text-align:center;margin-top:16px}
@media(max-width:640px){
  .iv-band{padding:18px 18px}.iv-band img{width:60px;height:60px}.iv-ttl b{font-size:23px}
  .iv-body{padding:20px 18px 18px}
  .iv-meta{grid-template-columns:1fr 1fr}.iv-meta .c1{grid-column:1/-1}
  table.iv .q,table.iv .p{display:none}
}
@media print{
  @page{size:letter;margin:.45in}
  header.top,footer.foot,.iv-acoes,.iv-ajuda{display:none !important}
  body{background:#fff}
  .iv-wrap{width:100%;margin:0}
  .iv-sheet{box-shadow:none;border-radius:0}
}
"""


def _dolar(v) -> str:
    try:
        return f"${float(v):,.2f}"
    except (TypeError, ValueError):
        return "$0.00"


def _html_invoice(d: dict) -> str:
    e = lambda s: html.escape(str(s or ""), quote=True)
    emp = d.get("empresa") or {}
    pago = bool(d.get("pago"))
    linhas = "".join(
        f"""<tr><td><span class="iv-nm">{e(it.get('nome'))}</span>{'<span class="iv-bonus">(Bonus)</span>' if it.get('bonus') else ''}"""
        f"""{f'<div class="iv-ds">{e(it.get("descricao"))}</div>' if it.get('descricao') else ''}</td>"""
        f"""<td class="c q">{e(it.get('qtd'))}</td><td class="r p">{_dolar(it.get('preco'))}</td><td class="r">{_dolar(it.get('valor'))}</td></tr>"""
        for it in (d.get("itens") or [])
    )
    link = str(d.get("link_pagamento") or "").strip()
    online = (f'<div class="iv-online"><a class="btn btn-navy" href="{e(link)}" target="_blank" rel="noopener nofollow">Pay online</a></div>'
              if re.match(r"^https?://", link) else "")
    email = f'<div class="iv-val">{e(d.get("email"))}</div>' if d.get("email") else ""
    return f"""
<main class="iv-wrap">
  <div class="iv-acoes">
    <span class="iv-selo {'pago' if pago else 'aberto'}">{'Paid · thank you!' if pago else 'Unpaid'}</span>
    <button class="btn btn-rose" type="button" onclick="window.print()">Download PDF</button>
  </div>
  <div class="iv-sheet">
    <div class="iv-band">
      <img src="/static/logo-redonda.png" alt="CM Maids" width="76" height="76">
      <div class="iv-ttl"><b>INVOICE</b><span>No. {e(d.get('numero'))}</span></div>
    </div>
    <div class="iv-body">
      <div class="iv-meta">
        <div class="c1">
          <div class="iv-lab">Billed to</div>
          <div class="iv-cliente">{e(d.get('cliente'))}</div>
          <div class="iv-val">{e(d.get('telefone'))}</div>{email}
          <div class="iv-lab iv-gap">Service period</div>
          <div class="iv-val">{e(d.get('periodo'))}</div>
        </div>
        <div>
          <div class="iv-lab">Issued</div><div class="iv-val">{e(d.get('emitido'))}</div>
          <div class="iv-lab iv-gap">Terms</div><div class="iv-val">{e(d.get('termos'))}</div>
        </div>
        <div>
          <div class="iv-lab">Due date</div><div class="iv-val">{e(d.get('vencimento'))}</div>
          <div class="iv-lab iv-gap">{'Amount paid' if pago else 'Amount due'}</div><div class="iv-big">{_dolar(d.get('total'))}</div>
        </div>
      </div>
      <table class="iv">
        <thead><tr><th>SERVICE</th><th class="c q">QTY</th><th class="r p">RATE</th><th class="r">AMOUNT</th></tr></thead>
        <tbody>{linhas}</tbody>
      </table>
      <div class="iv-tot"><div class="iv-totbox"><b>{'TOTAL PAID' if pago else 'TOTAL DUE'}</b><b>{_dolar(d.get('total'))}</b></div></div>
      <div class="iv-pay">
        <div class="iv-lab">Payment</div>
        <div class="iv-val">{e(d.get('pagamento'))}</div>
        {online}
        <div class="iv-note">{e(d.get('observacao'))}</div>
      </div>
      <div class="iv-foot">
        <span>{e(emp.get('email'))} &nbsp;|&nbsp; {e(emp.get('telefone'))} &nbsp;|&nbsp; {e(emp.get('site'))}</span>
        <span>Thank you for your business.</span>
        <span style="flex-basis:100%"><b>CM MAIDS</b> — {e(emp.get('frase'))}</span>
      </div>
    </div>
  </div>
  <p class="iv-ajuda">Questions about this invoice? Call or text <a href="tel:+19195254863">(919) 525-4863</a> or email <a href="mailto:contact@cmmaids.com">contact@cmmaids.com</a>.</p>
</main>"""


def pagina_404() -> HTMLResponse:
    return render_page("Page not found | CM Maids", "This page does not exist.",
                       '<main class="doc"><h1>Page not found</h1><p class="updated">This link is not valid or the page does not exist.</p>'
                       '<p><a class="btn btn-rose" href="/">Go to the home page</a></p></main>',
                       status=404, robots="noindex,nofollow", headers=_SEM_CACHE)


@app.get("/invoice/{token}")
def invoice_publico(token: str):
    if not re.fullmatch(r"[0-9a-f]{32}", token):
        return pagina_404()
    try:
        req = urllib.request.Request(f"{INVOICE_API}?t={token}", headers={"User-Agent": "cmmaids-site/1.0"})
        with urllib.request.urlopen(req, timeout=8) as r:
            d = json.loads(r.read().decode("utf-8"))
    except urllib.error.HTTPError as err:
        if err.code == 404:
            return pagina_404()
        d = None
    except Exception:
        d = None
    if not d or not d.get("numero"):
        return render_page("Invoice | CM Maids", "Invoice temporarily unavailable.",
                           '<main class="doc"><h1>Invoice unavailable</h1><p class="updated">We could not load this invoice right now. '
                           'Please try again in a few minutes, or call or text (919) 525-4863.</p></main>',
                           status=503, robots="noindex,nofollow", headers=_SEM_CACHE)
    return render_page(f"Invoice {html.escape(str(d['numero']))} | CM Maids", "Your CM Maids invoice.", _html_invoice(d),
                       robots="noindex,nofollow", extra_css=INVOICE_CSS, headers=_SEM_CACHE)


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
    license: str = "na"
    days: list[str]
    hours_from: str
    hours_to: str
    document: str
    supplies: str
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

    @field_validator("experience", "has_car", "license", "document", "supplies")
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
    if data["has_car"] != "yes":
        data["license"] = "na"
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
    bg.add_task(enviar_plataforma, data)
    return {"ok": True, "score": data["score"], "bucket": data["bucket"], "availability": data["availability"], "event_id": data["event_id"], "max": MAX_SCORE}


# ---- rótulos (PT, pro time) ----
LABELS = {
    "experience": {"none": "Nenhuma", "lt1": "Menos de 1 ano", "1to3": "1 a 3 anos", "gt3": "Mais de 3 anos"},
    "has_car": {"yes": "Sim", "no": "Não"},
    "license": {"yes": "Sim", "no": "Não", "na": "—"},
    "availability": {"full": "Total (8:30–4pm)", "partial": "Parcial", "no": "Não"},
    "days": {"sun": "Dom", "mon": "Seg", "tue": "Ter", "wed": "Qua", "thu": "Qui", "fri": "Sex", "sat": "Sáb"},
    "document": {"ssn": "SSN", "itin": "ITIN", "none": "Nenhum"},
    "supplies": {"yes": "Sim", "no": "Não"},
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
        ("Tem carro", label("has_car", d["has_car"]) + (" · habilitação: " + label("license", d["license"]) if d["has_car"] == "yes" else "")),
        ("Disponível 8:30–4pm", f'{label("availability", d["availability"])} — {esc(fmt_hours(d))}'),
        ("SSN / ITIN", label("document", d["document"])),
        ("Material de limpeza", label("supplies", d["supplies"])),
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
    <p style="margin-top:16px;font-size:13px;color:#6b7280">Todas as candidatas na plataforma: <a href="{PLATAFORMA_PAINEL}">{PLATAFORMA_PAINEL}</a></p></div>"""
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


def enviar_plataforma(d: dict):
    """Espelha a candidata na plataforma (operacionalcm.com). Nunca derruba o envio:
    o SQLite daqui continua sendo a fonte, isto é só a cópia operacional."""
    if not PLATAFORMA_TOKEN:
        return
    linha = {
        "source_id": d.get("id"), "name": d["name"], "phone": d["phone"], "email": d["email"],
        "instagram": d["instagram"] or None, "facebook": d["facebook"] or None,
        "city": d["city"], "zip": d["zip"], "days": d["days"],
        "hours_from": d["hours_from"], "hours_to": d["hours_to"], "availability": d["availability"],
        "has_car": d["has_car"], "experience_key": d["experience"], "document": d["document"],
        "supplies": d["supplies"], "license": d["license"],  # a pergunta "tempo nos EUA" saiu do formulário em 23/09
        "score": d["score"], "bucket": d["bucket"], "lang": d["lang"], "event_id": d["event_id"],
        "status": "nova",
    }
    try:
        _post_json(PLATAFORMA_URL, linha, {"x-cm-token": PLATAFORMA_TOKEN})
    except Exception as e:  # a candidata já está salva aqui; a plataforma reconcilia depois
        print("plataforma:", e, flush=True)


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
