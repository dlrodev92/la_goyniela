# La Goyniela — API

Backend de **La Goyniela**, una app para organizar peñas de La Quiniela española entre amigos. Cubre:

- Gestión de grupos (peñas), miembros y roles
- Creación de porras en 4 modos (`multiple`, `sencilla`, `reducida`, `intuicion`)
- Voto de miembros con N columnas simultáneas
- Propuesta automática por columna con media exponencial
- Boleto oficial N-columnas (validación por modo)
- Panel de superadmin: ingesta Quiniwin + Losilla encadenada, CRUD de equipos, edición de jornadas
- Descarga del boleto como PDF

Stack: **FastAPI** + **SQLAlchemy 2 (async)** + **Postgres** + **Alembic** + **APScheduler**.

---

## 1. Quickstart (dev local)

```bash
# 1) Clonar y entrar
cd /home/davld/projects/la_goyniela

# 2) Levantar Postgres (docker compose incluido)
docker compose up -d

# 3) Copiar el template de env y rellenar secretos
cp .env.example .env
# editar .env — ver sección §4 abajo

# 4) Instalar deps + migrar
uv sync
uv run alembic upgrade head

# 5) Sembrar el catálogo de equipos (una vez)
uv run python -m app.scripts.load_team_catalog

# 6) Arrancar la API
uv run uvicorn app.main:app --host 0.0.0.0 --port 8000 --reload
```

Abrí `http://localhost:8000/docs` para el Swagger interactivo.

### Tests

```bash
uv run pytest -q                    # toda la suite (405 tests)
uv run pytest tests/test_X.py -v    # un archivo
```

Los tests usan una **DB de test separada** (`la_goyniela_test` en el mismo Postgres). Configurá `TEST_DATABASE_URL` o usá la convención por defecto del `conftest.py`.

---

## 2. Estructura del repo

```
.
├── app/                          # código de la aplicación
│   ├── api/v1/                   # routers (un archivo por recurso)
│   │   ├── auth.py               #   /auth/{register,login,google,x,me}
│   │   ├── groups.py             #   /groups/{...} CRUD
│   │   ├── porras.py             #   /groups/{g}/porras + /boleto + /picks + /propuesta + /boleto.pdf
│   │   ├── jornadas.py           #   /jornadas
│   │   ├── losilla.py            #   /api/v1/losilla/{jornada}/escrutinio,comentarios
│   │   ├── teams.py              #   /teams
│   │   ├── admin.py              #   require_superadmin helper
│   │   ├── admin_jornadas.py     #   /api/v1/admin/jornadas/...
│   │   ├── admin_teams.py        #   /api/v1/admin/teams/...
│   │   ├── admin_users.py        #   /api/v1/admin/users/...
│   │   ├── admin_uploads.py      #   /api/v1/admin/teams/{id}/logo
│   │   ├── admin_composicion.py  #   /api/v1/admin/jornadas/{id}/composicion
│   │   └── admin_ingest.py       #   /api/v1/admin/ingest/quiniwin (manual Quiniwin + Losilla)
│   ├── clients/                  # HTTP clients a Quiniwin + Losilla
│   ├── core/                     # security (JWT)
│   ├── db/                       # modelos + session + migrations
│   ├── middleware/               # rate limit (admin endpoints, 300 req/min)
│   ├── schemas/                  # Pydantic DTOs (request/response shapes)
│   ├── scripts/                  # CLIs one-shot (load_team_catalog, build_jornada, ...)
│   └── services/                 # business logic puro (jornada_manager, propuesta, boleto_pdf, reducciones)
├── db/migrations/versions/       # Alembic
├── data/                         # team-catalog + team-assets (logo, manifest)
├── tests/                        # pytest suite (405 tests, 1:1 con módulos)
├── docs/                         # docs internas (CLIENT.md, API.md, ...)
├── pyproject.toml                # dependencias + tool config
├── uv.lock                       # lockfile
├── alembic.ini
├── docker-compose.yml            # Postgres local (5433:5432)
├── Dockerfile                    # imagen de producción (multi-stage, uv, non-root)
├── Procfile                      # Heroku/Railway start
├── railway.json                  # config Railway (releaseCommand + healthcheck)
├── .dockerignore                 # excluye dev artifacts del build context
├── .env.example                  # template de variables de entorno
└── readme.md                     # este archivo
```

---

## 3. Variables de entorno

`cp .env.example .env` y rellená:

| Variable | Descripción | Ejemplo |
|---|---|---|
| `ENV` | `dev` \| `staging` \| `prod` | `dev` |
| `DATABASE_URL` | URL async de Postgres | `postgresql+psycopg://user:pass@host:5432/la_goyniela` |
| `LOG_LEVEL` | `DEBUG` \| `INFO` \| `WARNING` \| `ERROR` | `INFO` |
| `CORS_ORIGINS` | CSV de origins permitidos | `http://localhost:3000,https://la-goyniela.vercel.app` |
| `JWT_SECRET` | Secreto para firmar JWTs | (un string random de 32+ chars) |
| `JWT_ALGORITHM` | Algoritmo JWT | `HS256` |
| `JWT_EXPIRATION_MINUTES` | TTL del access token | `1440` (24h) |
| `GOOGLE_CLIENT_ID` | OAuth Google | (de Google Cloud Console) |
| `X_CLIENT_ID` / `X_CLIENT_SECRET` | OAuth X (Twitter) | (de X Developer Portal) |
| `STORAGE_PUBLIC_BASE_URL` | Base URL de logos en S3/R2 | `https://cdn.example.com` |
| `STORAGE_BACKEND` | `local` \| `s3` | `s3` en prod |
| `SCHEDULER_ENABLED` | `true` \| `false` — activa el cron in-process | `true` |
| `API_FOOTBALL_KEY` | (opcional) si se usa API-Football | (de api-football.com) |

**IMPORTANTE**: nunca commitear `.env` con secretos reales. Está en `.gitignore`.

---

## 4. Deploy a Railway

Railway detecta Python automáticamente vía `pyproject.toml` y usa Nixpacks.

### Setup

1. **Crear proyecto en Railway** apuntando al repo de GitHub.
2. **Agregar un Postgres plugin** (o apuntar a uno externo) — Railway inyecta `DATABASE_URL` automáticamente.
3. **Setear las env vars** (ver §3). El `PORT` lo inyecta Railway.
4. **El primer deploy corre `releaseCommand` (`alembic upgrade head`)** antes de levantar el web process.

### Lo que hace Railway al deploy

```
release:  uv run alembic upgrade head
web:      uv run uvicorn app.main:app --host 0.0.0.0 --port $PORT --workers 2
```

`railway.json` configura el healthcheck en `/health` y el restart policy.

### Dockerfile alternativo

Si preferís build por Docker (recomendado para reproducibilidad), Railway lo detecta y usa `Dockerfile` en vez de Nixpacks. El `Dockerfile` ya está escrito:
- `python:3.12-slim` base
- `uv` instalado vía imagen oficial
- Healthcheck vía `curl /health`
- Usuario no-root

---

## 5. Deploy manual con Docker

```bash
docker build -t la-goyniela-api .
docker run --rm -p 8000:8000 --env-file .env la-goyniela-api
```

El entrypoint del container corre `alembic upgrade head` antes de `uvicorn`, así que no hace falta ejecutar migraciones a mano.

---

## 6. Documentación adicional

- [`docs/API.md`](docs/API.md) — referencia completa de endpoints
- [`docs/CLIENT.md`](docs/CLIENT.md) — guía de integración para el cliente
- [`docs/CLIENT-BOLETO-MODES.md`](docs/CLIENT-BOLETO-MODES.md) — modos de boleto (sencilla/multiple/reducida/intuicion) + PDF
- [`docs/HANDOFF-NEXT-SESSION.md`](docs/HANDOFF-NEXT-SESSION.md) — pendientes y fixes abiertos
- [`docs/producto/gestor-jornadas.md`](docs/producto/gestor-jornadas.md) — diseño del gestor de jornadas

Para el dev del cliente Next.js, el doc canónico está en el repo hermano: `la_goyniela_client/FRONTEND-IMPLEMENTATION.md`.

---

## 7. Convenciones

- **Schema-first**: los schemas Pydantic en `app/schemas/` son la fuente de verdad del contrato API.
- **Service-layer pura**: `app/services/*` no tocan DB ni HTTP, son funciones puras testeables sin fixtures.
- **Endpoints thin**: cada router es una capa fina sobre el service layer — auth, validación, marshalling.
- **Soft delete**: `Jornada` y `Team` tienen `deleted_at`. Endpoints públicos filtran; admin puede ver y restaurar.
- **Multi-column picks**: la `pick` table tiene `columna_idx` (1..N). Pleno se persiste una vez por (member, porra, partido=15, columna_idx), todos con el mismo valor. La propuesta agrega por columna.
- **Rate limit**: 300 req/min por usuario en `/api/v1/admin/*` (in-memory sliding window). Subir/bajar el límite requiere reinicio.

---

## 8. Licencia

Uso personal. No redistribuir.
