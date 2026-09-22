# CM Maids — Trabalhe conosco

Formulário de candidatura para helpers (EN/PT/ES) com pontuação, banco SQLite, e-mail via Resend e Meta Pixel (Purchase).

Rotas: `/work-with-us` · `/trabalhe-conosco` · `/trabaja-con-nosotros` · `/admin` (Basic Auth) · `/admin/export.csv`

## Variáveis de ambiente

| Var | O quê |
|---|---|
| `ADMIN_USER` / `ADMIN_PASS` | login do `/admin` (sem `ADMIN_PASS` o admin fica fechado) |
| `RESEND_API_KEY` | chave da Resend |
| `NOTIFY_TO` | e-mails que recebem cada candidatura (separados por vírgula) |
| `NOTIFY_FROM` | remetente (domínio verificado na Resend) |
| `META_PIXEL_ID` | pixel (default 1656588805885650) |
| `META_CAPI_TOKEN` | opcional — token da Conversions API pra mandar o Purchase também pelo servidor (dedup por `event_id`) |
| `PUBLIC_URL` | `https://cmmaids.com` |
| `DB_PATH` | `/data/applications.db` (volume) |
| `SECRETS_FILE` | `/data/secrets.env` — arquivo `CHAVE=valor` lido no boot (é onde ficam `RESEND_API_KEY` e `ADMIN_PASS` em produção, fora do painel e do repo) |

## Rodar local

```
pip install -r requirements.txt
DB_PATH=./dev.db ADMIN_PASS=x uvicorn app:app --reload
```
