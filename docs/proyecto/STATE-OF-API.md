# La Goyniela — Mapa conceptual de la API

**Fecha**: 30 de septiembre de 2026
**Estado**: v0.2.x (users + grupos + auth + jornada schema)
**Tests**: 90 pasando
**Próximo milestone**: persistencia de escrutinio (T-data-4)

---

## 1. Resumen ejecutivo

La API de La Goyniela es un servicio FastAPI (Python 3.11) conectado a PostgreSQL 16 que expone una API REST con autenticación JWT, gestión de equipos/competiciones y ciclo de vida de quinielas. Los datos externos (Quiniwin, Losilla, API-Football, The Odds API, Football-Data) llegan por adapters HTTP que aún no están automatizados.

| Capa | Estado | Lo que hay | Lo que falta |
|---|---|---|---|
| HTTP framework | ✅ | FastAPI + Uvicorn + OpenAPI auto-generated | nada |
| Auth | ✅ | local email/password + Google + X | nada |
| Users + Groups | ✅ | 9 endpoints de grupos con roles admin/member | invitaciones por link (sólo por código) |
| Catalog | ✅ | 4 endpoints, 9 competiciones, 423 equipos | nada |
| Losilla (live) | ✅ | 3 endpoints leen upstream con cache 5min | persistencia en DB |
| Jornada schema | ✅ | 5 tablas nuevas + endpoints read-only | endpoints de voto (MVP-02) |
| Scheduler | ❌ | solo documentación en `scheduler.md` | implementación APScheduler |
| Quiniwin client | ❌ | solo docs | cliente HTTP + parse + tests |
| Adapters Quiniwin/FootballData/Odds | ❌ | solo scripts `scripts/` | clientes HTTP + endpoints |
| Admin endpoints | ❌ | ninguno | futuros MVP-03 |

---

## 2. Stack tecnológico

**Runtime**: Python 3.11+, FastAPI 0.110+, Uvicorn (ASGI), Pydantic v2, Pydantic-settings, pyjwt, passlib[bcrypt], google-auth, httpx, structlog, psycopg[binary], SQLAlchemy 2.0 (async), Alembic 1.13+

**Gestor**: `uv` (Astral, ~50x más rápido que pip). Lockfile en `uv.lock`.

**DB**: PostgreSQL 16-alpine en Docker, puerto 5433 (5432 ocupado por otro proyecto).

**Tests**: pytest + pytest-asyncio + pytest-httpx + httpx.AsyncClient + ASGITransport (in-process, no necesita servidor corriendo).

**Auth providers**:
- local: bcrypt cost 12
- Google: id_token verify vía `google-auth`
- X (Twitter): `/2/users/me` con Bearer access_token vía httpx

---

## 3. Inventario de endpoints

**24 rutas funcionales** + docs interactivos en `/docs` (Swagger UI) y `/redoc`.

### Auth (`tag: 'api'`)

| Método | Ruta | Descripción |
|---|---|---|
| `POST` | `/auth/register` | Crea user con bcrypt, persiste en DB, devuelve JWT |
| `POST` | `/auth/login` | Valida email+password con bcrypt, devuelve JWT |
| `POST` | `/auth/google` | Verifica `id_token` de Google con `google-auth`, crea/linkea user por email |
| `POST` | `/auth/x` | Llama `/2/users/me` de X, crea/linkea user por `x_id` |
| `GET` | `/auth/me` | Devuelve user actual desde Bearer token |

### Grupos (`tag: 'groups'`)

| Método | Ruta | Auth | Descripción |
|---|---|---|---|
| `POST` | `/groups` | Bearer | Crea grupo. **Body**: `{name, description?}` → 201 + group con join_code de 6 chars alfanum |
| `GET` | `/groups` | Bearer | Lista grupos del usuario actual |
| `POST` | `/groups/join` | Bearer | Une a un grupo vía `join_code`. **Body**: `{join_code}` → 200 + group |
| `GET` | `/groups/{id}` | Bearer | Detalle + lista de miembros (requiere membresía) |
| `POST` | `/groups/{id}/leave` | Bearer | Salir del grupo. **El creador NO puede** (debe eliminar el grupo en su lugar) → 204 |
| `DELETE` | `/groups/{id}` | Bearer (creator) | Eliminar grupo + cascada de miembros |
| `POST` | `/groups/{id}/members/{uid}/promote` | Bearer (admin) | member → admin |
| `POST` | `/groups/{id}/members/{uid}/demote` | Bearer (admin) | admin → member (excepto al creator) |
| `DELETE` | `/groups/{id}/members/{uid}` | Bearer (admin) | Quitar miembro del grupo |

### Catálogo (`tag: 'competitions'`, `'teams'`)

| Método | Ruta | Descripción |
|---|---|---|
| `GET` | `/api/v1/competitions` | Lista las 9 competiciones. `?tier=1|2|3`, `?country=Spain` |
| `GET` | `/api/v1/competitions/{competition_id}` | Una competición o 404 |
| `GET` | `/api/v1/teams` | Lista equipos. `?competition_id=X`, `?gender=men|women`, `?country=ESP`, `?q=substring` |
| `GET` | `/api/v1/teams/{team_id}` | Un equipo por id numérico, o 404 |

### Losilla (read-only, upstream) (`tag: 'losilla'`)

| Método | Ruta | Descripción |
|---|---|---|
| `GET` | `/api/v1/losilla/jornada/{jornada}` | Full payload (live + escrutinio + comentarios) |
| `GET` | `/api/v1/losilla/jornada/{jornada}/escrutinio` | Solo escrutinio (404 → fallback upstream) |
| `GET` | `/api/v1/losilla/jornada/{jornada}/comentarios` | 14 comentarios editoriales (404 → fallback upstream) |

### Jornadas (`tag: 'jornadas'`)

| Método | Ruta | Descripción |
|---|---|---|
| `GET` | `/jornadas` | Lista jornadas. `?season=YYYY`, `?status=draft|active|closed|completed` |
| `GET` | `/jornadas/{jornada_id}` | Detalle de una jornada con sus 15 partidos |

### Health

| Método | Ruta | Descripción |
|---|---|---|
| `GET` | `/health` | Liveness probe |
| `GET` | `/health/db` | DB readiness (SELECT 1 contra Postgres) |

### Generados por FastAPI

| Ruta | Descripción |
|---|---|
| `/docs` | Swagger UI (interactivo) |
| `/redoc` | ReDoc (readable) |
| `/openapi.json` | Spec OpenAPI 3.0 |

---

## 4. Schema de base de datos (13 tablas)

### Diagrama de relaciones

```
              ┌─────────────────┐
              │   alembic/      │  (tracking Alembic, 1 row)
              │   version       │
              └─────────────────┘

  ┌─────────────────────────────────────────┐
  │                  user                  │  ← 0+ rows dinámicos
  │  id PK, email UNIQUE, password_hash,   │
  │  google_id UNIQUE, x_id UNIQUE,         │
  │  display_name, avatar_url, is_active,   │
  │  created_at                             │
  └─────────────────────────────────────────┘
                       │
                       │ 1:N
                       ▼
  ┌─────────────────────────────────────────┐
  │              group_member               │  ← 0+ rows dinámicos
  │  id PK, group_id FK→group,              │
  │  user_id FK→user,                      │
  │  role ENUM(admin, member),             │
  │  joined_at                             │
  └─────────────────────────────────────────┘
                       ▲
                       │ N:1
  ┌─────────────────────────────────────────┐
  │                  group                  │  ← 0+ rows dinámicos
  │  id PK, name, description,              │
  │  created_by_user_id FK→user,           │
  │  join_code UNIQUE, max_members,         │
  │  created_at                             │
  └─────────────────────────────────────────┘

  ┌─────────────────────────────────────────┐
  │              competition                │  ← 9 rows (seed)
  │  id PK (string), name, country,        │
  │  gender, tier, season,                  │
  │  official_source_url                    │
  └─────────────────────────────────────────┘
                       ▲
                       │ N:1 (FK competition_id)
                       │
  ┌─────────────────────────────────────────┐
  │            team_identity_map            │  ← 423 rows (seed) + N stubs
  │  id PK, entity_id UNIQUE,              │
  │  competition_id FK→competition,        │
  │  canonical_name, short_name,            │
  │  gender, country, logo_url, created_at │
  │  UNIQUE INDEX (competition_id,          │
  │    lower(unaccent(canonical_name)))     │  ← dedup constraint
  └─────────────────────────────────────────┘
                       ▲
                       │ N:1 (FK team_id)
                       │
  ┌─────────────────────────────────────────┐
  │              composicion                │  ← 0+ rows dinámicos (T-data-1)
  │  id PK, jornada_id FK→jornada,         │
  │  posicion (1..15) CHECK,               │
  │  local_team_id FK→team_identity_map,    │
  │  visitante_team_id FK→team_identity_map,│
  │  local_raw, visitante_raw,             │
  │  kickoff_at, competicion_raw,          │
  │  fetched_at                             │
  │  UNIQUE (jornada_id, posicion)         │
  └─────────────────────────────────────────┘
                       ▲
                       │ N:1 (FK jornada_id, ON DELETE CASCADE)
                       │
  ┌─────────────────────────────────────────┐
  │                 jornada                 │  ← 0+ rows dinámicos (T-data-1)
  │  id PK, temporada, temporada_str,      │
  │  numero UNIQUE(temporada, numero),        │
  │  status ENUM(draft,active,closed,completed),│
  │  sale_opens_at, voting_closes_at,      │
  │  kickoff_first_match_at, fetched_at    │
  │  INDEX (status, kickoff_first_match_at)│
  └─────────────────────────────────────────┘

  ┌─────────────────────────────────────────┐
  │            jornada_escrutinio           │  ← 0+ rows (post-jornada)
  │  id PK, jornada_id UNIQUE FK→jornada,  │
  │  source_id FK→source,                 │
  │  fetched_at, fetch_session_id UUID,    │
  │  payload JSONB                         │
  └─────────────────────────────────────────┘

  ┌─────────────────────────────────────────┐
  │          jornada_comentario             │  ← 14 rows por jornada (post)
  │  id PK, jornada_id FK→jornada,         │
  │  source_id FK→source,                 │
  │  partido_orden (1..14, no 15),         │
  │  local_raw, visitante_raw, comentario, │
  │  fetched_at, fetch_session_id UUID,    │
  │  UNIQUE (jornada_id, partido_orden)    │
  └─────────────────────────────────────────┘

  ┌─────────────────────────────────────────┐
  │                 source                  │  ← 5 rows (seed)
  │  id PK, code UNIQUE, name,             │
  │  kind (api|xml|embed), base_url        │
  └─────────────────────────────────────────┘
                       ▲
                       │ 1:N
                       │
  ┌─────────────────────────────────────────┐
  │            fetch_session               │  ← 0+ rows dinámicos (T-data-3)
  │  id PK (UUID), source_id FK→source,    │
  │  started_at, finished_at,             │
  │  source_stats JSONB,                  │
  └─────────────────────────────────────────┘
                       ▲
                       │ 1:1 (referenciado por jornada_escrutinio,
                       │ jornada_comentario, composicion, estimacion_snapshot, etc.)
                       │
                       │  FK→team_identity_map (N:1)
                       │
  ┌─────────────────────────────────────────┐
  │         team_id_api_football           │  ← 60 rows (seed)
  │  id PK, source_id FK→source,           │
  │  team_identity_id FK→team_identity_map,│
  │  api_football_id, season_seen,         │
  │  UNIQUE(team_identity_id, season_seen) │
  └─────────────────────────────────────────┘
```

### Catalog rows (8+)

| Tabla | Filas actuales | Cómo se popula |
|---|---|---|
| `competition` | 9 | Seed (script manual) |
| `source` | 5 | Seed (script manual) |
| `team_identity_map` | 423 | Seed (script manual) + stubs auto en runtime |
| `team_id_api_football` | 60 | Seed (script manual) |
| `user` | dinámico | `POST /auth/register` |
| `group` | dinámico | `POST /groups` |
| `group_member` | dinámico | `POST /groups/join` |
| `competition` (ficticio `'unknown'`) | 1 | T8 setup (default for team stubs) |

### Runtime-only tables (no seed)

| Tabla | Filas actuales | Cómo se popula |
|---|---|---|
| `jornada` | 0 | T-data-1 (Quiniwin client + scheduler) |
| `composicion` | 0 | T-data-1 |
| `jornada_escrutinio` | 0 | T-data-4 (post-jornada scheduler) |
| `jornada_comentario` | 0 | T-data-4 |
| `fetch_session` | 0 | Cada batch del scheduler |

---

## 5. Diagrama de flujo conceptual

```
┌──────────────────────────────────────────────────────────────────┐
│                          CLIENTE                                 │
│  (SPA en Firebase / static site / mobile)                         │
│                                                                  │
│  - Login form                                                │
│  - Google Sign-In SDK → id_token                          │
│  - X OAuth 2.0 + PKCE → access_token                       │
│  - POST /auth/{google,x} con token                              │
│  - Guarda token JWT nuestro para requests subsiguientes     │
│  - Lee /jornadas/{n}/partidos para mostrar boleto           │
└─────────────────────────────┬────────────────────────────────────┘
                              │
                              │ HTTPS + Bearer JWT
                              ▼
┌──────────────────────────────────────────────────────────────────┐
│                      FASTAPI (uvicorn :8000)                       │
│                                                                  │
│  ┌─────────────────────────────────────────────────────────────┐ │
│  │  AUTH (5 endpoints)                                          │ │
│  │  - register/login (bcrypt)                                   │ │
│  │  - google (verify id_token)                                  │ │
│  │  - x (call /2/users/me)                                       │ │
│  │  - me (decode JWT)                                            │ │
│  └─────────────────────────────────────────────────────────────┘ │
│                                                                  │
│  ┌─────────────────────────────────────────────────────────────┐ │
│  │  GROUPS (9 endpoints)                                         │ │
│  │  - crear/listar/detalle/salir/unirse                         │ │
│  │  - admin: promote/demote/remove member + delete group        │ │
│  └─────────────────────────────────────────────────────────────┘ │
│                                                                  │
│  ┌─────────────────────────────────────────────────────────────┐ │
│  │  CATALOG (4 endpoints)                                       │ │
│  │  - competitions list/detail                                  │ │
│  │  - teams list/detail                                          │ │
│  └─────────────────────────────────────────────────────────────┘ │
│                                                                  │
│  ┌─────────────────────────────────────────────────────────────┐ │
│  │  JORNADAS (2 endpoints)                                       │ │
│  │  - list, detail                                              │ │
│  └─────────────────────────────────────────────────────────────┘ │
│                                                                  │
│  ┌─────────────────────────────────────────────────────────────┐ │
│  │  LOSILLA (3 endpoints)                                        │ │
│  │  - full, escrutinio, comentarios (live, con cache 5min)        │ │
│  └─────────────────────────────────────────────────────────────┘ │
│                                                                  │
│  ┌─────────────────────────────────────────────────────────────┐ │
│  │  app/clients/                              │  (HTTP clients)  │ │
│  │  - eduardolosilla.py                       │ ──────────────► │ │
│  │  - quiniwin.py [FUTURO]                                       │ │
│  └─────────────────────────────────────────────────────────────┘ │
└─────────────────────────────────────┬────────────────────────────┘
                                      │
                                      │ SQLAlchemy async (psycopg3)
                                      ▼
┌──────────────────────────────────────────────────────────────────┐
│              PostgreSQL 16 (la-goyniela-postgres :5433)          │
│                                                                  │
│  13 tablas:                                                     │
│   - catalog: competition, source, team_identity_map,            │
│              team_id_api_football                            │
│   - users: user, group, group_member                        │
│   - jornada: jornada, composicion, jornada_escrutinio,       │
│              jornada_comentario, fetch_session                │
└──────────────────────────────────────────────────────────────────┘

         ┌─────── 5 EXTERNAL SOURCES  ───────┐
         │                                  │
   ┌─────▼─────┐  ┌─────────┐  ┌───────▼──────┐
   │ Quiniwin  │  │  Losilla │  │ API-Football │
   │  (XML    │  │  (HTML    │  │  (REST)    │
   │  free)   │  │  free)    │  │  key-based)  │
   └──────────┘  └──────────┘  └──────────────┘
                          │
                  ┌───────▼──────┐
                  │  The Odds   │
                  │   API (REST) │
                  │  free tier    │
                  └──────────────┘
                          │
                  ┌───────▼─────────────┐
                  │  Football-Data   │
                  │   (CSV download)  │
                  │   free            │
                  └──────────────────┘
```

---

## 6. Tasks completadas (T1-T8)

### T1 — Bootstrap
- `pyproject.toml` con uv-managed deps, 13 runtime + 4 dev
- Docker Compose con Postgres 16-alpine en puerto 5433
- `app/main.py` con FastAPI app + health endpoint
- `app/core/config.py` con pydantic-settings

### T2 — DB session
- `app/db/session.py` con async engine + `get_session()` dependency
- `app/db/models.py` con `HealthCheck` placeholder (luego reemplazado)
- `GET /health/db` que hace `SELECT 1`

### T3 — Alembic
- `db/migrations/env.py` configurado para SQLAlchemy 2.0 async
- `db/migrations/versions/33954b4cfbab_initial_health_check_table.py` (luego superseded en T4)

### T4 — Catalog models
- `Competition`, `Source`, `TeamIdentityMap`, `TeamIdApiFootball`
- Migration `605f324ada7e_catalog_competition_source_team_` — drop health_check, add 4 catalog tables

### T5 — Initial catalog load
- `app/scripts/load_team_catalog.py` — idempotente, lee `data/team-catalog.json` y UPSERTs
- Cargado: 9 competiciones, 5 sources, 423 teams, 60 IDs API-Football

### T6 — Read-only catalog endpoints
- `app/api/v1/competitions.py` + `app/api/v1/teams.py`
- Schemas con `from_attributes=True`
- Filtros: `?tier`, `?country`, `?competition_id`, `?gender`, `?q`

### T6.5 — Refactor a inglés
- Renombrado `competiciones.py` → `competitions.py`, `equipos.py` → `teams.py`
- Endpoints `/api/v1/competiciones` → `/api/v1/competitions`, etc.
- Comentarios y docstrings traducidos

### T-users-1 — Users/groups schema
- 3 tablas nuevas: `user`, `group`, `group_member` con `role` ENUM
- Migration `33906bd8fdea_auth_user_group_group_member`

### T-users-2 — Auth
- `app/core/security.py` con bcrypt + JWT + Google id_token + X token verify
- `app/api/v1/auth.py` con 5 endpoints: register, login, google, x, me
- `app/schemas/auth.py` con Pydantic DTOs
- Auto-link por email (Google/X crean user si no existe, linkean si existe)
- `unaccent` extension instalada
- `bcrypt>=4.0,<4.1` pin (passlib 1.7 no funciona con bcrypt 5)

### T-users-3 — Groups
- `app/api/v1/groups.py` con 9 endpoints (5 normales + 4 admin)
- `app/api/v1/_group_helpers.py` con `get_group_member_or_404` y `get_admin_or_403`
- `app/schemas/group.py`
- Join_code de 6 chars sin ambigüedad (sin 0/O/1/I/l)
- Creator inmutable (no puede ser kicked ni demoteado)

### T8 — Jornada schema + endpoints
- 5 tablas nuevas: `jornada`, `composicion`, `jornada_escrutinio`, `jornada_comentario`, `fetch_session`
- `unaccent` extension + functional UNIQUE INDEX para dedup de equipos
- `JornadaStatus` enum (draft, active, closed, completed)
- 2 endpoints read-only: `GET /jornadas`, `GET /jornadas/{id}`
- `competition_id='unknown'` placeholder para stubs de equipo
- 16 nuevos tests (jornada + dedup + constraints)

### Total: 90 tests pasando

---

## 7. Tasks pendientes

### T-data-1 — Quiniwin client + matching + bootstrap (4-6h)

**Lo que falta**:
- `app/clients/quiniwin.py` con httpx async + parse XML
- `app/scripts/ingest_quiniwin.py` CLI one-shot para bootstrap J1-J10
- Helper `find_or_create_team(session, raw_name, competition_id)` con:
  - Lookup normalizado via `(competition_id, lower(unaccent(canonical_name)))`
  - INSERT ... ON CONFLICT (la constraint UNIQUE ya está) 
  - Stub con `entity_id = "auto-{slug}-{hash}"`, `competition_id="unknown"`, `gender="men"|"women"`
  - Logging cuando crea stub
- Tests (15-20): bootstrap de J1-J10, dedup (mismo equipo 2 veces → 1 row), `(f)` suffix detection, edge cases

**Bloquea**: T9, T10, T11, y la persistencia de escrutinio (T-data-4).

### T-data-2 — Scheduler
- `Jornada` rows escritas
- Esperado: ingest recurrente + persistencia de escrutinio/comentarios

**Lo que falta**:
- `app/scheduler/feed_runner.py` con `AsyncIOScheduler` y 2 jobs:
  - Diario 09:00 Madrid: poll Quiniwin, ingest nuevas jornadas
  - Lunes 09:00 Madrid: post-jornada, fetch Losilla escrutinio/comentarios
- Ingest pipeline `app/pipeline/jornada_runner.py`:
  - fetch + parse + normalize + UPSERT
  - Per-source stats → `fetch_session`
  - Idempotencia y manejo de errores (cada fuente falla independiente)
- Configuración en `app/scheduler/config.py` con env vars
- Tests (5-8): scheduler arranca, jobs disparan, idempotencia funciona

### T-data-4 — Losilla persistence (3-4h)

**Lo que falta**:
- `app/scripts/losilla_ingest.py` (CLI one-shot): persiste escrutinio + comentarios de una jornada cerrada
- Refactor del router `app/api/v1/losilla.py`:
  - `/escrutinio`: primero DB, fallback upstream
  - `/comentarios`: primero DB, fallback upstream
  - `/jornada/{n}`: queda igual (live)

### T7 — Adaptar `build_jornada.py` (1-2h, opcional)

**Lo que falta**:
- Que `build_jornada.py` consuma la API en vez de JSON estático
- Sustituir `mock_quiniela.py` por llamadas HTTP a `/jornadas/{n}/partidos`
- Solo si querés conectar el HTML del prototipo a la API

### Admin endpoints (futuro, MVP-03+)

**Lo que falta**:
- `PATCH /api/v1/teams/{entity_id}` — fix stubs (logo, competition_id, gender, country)
- `GET /api/v1/teams?competition=unknown` — lista stubs pendientes
- `GET /admin/fetch-sessions` — ver historial de batches
- `POST /admin/jornadas/{id}/ingest?force=true` — forzar un batch manual
- Auth admin (JWT claim `is_admin` o role separada)

### User-facing features (MVP-02)

**Lo que falta**:
- Endpoint `POST /groups/{id}/picks` para votar
- Endpoint de tally de votos de grupo
- Algoritmo de scoring
- Endpoint para exportar jugada final

---

## 8. Configuración

### Variables de entorno (.env)

```
# DB
DATABASE_URL=postgresql+psycopg://la_goyniela:la_goyniela@localhost:5433/la_goyniela

# Server
ENV=dev
LOG_LEVEL=INFO
CORS_ORIGINS=["http://localhost:3000"]

# Auth
JWT_SECRET=<random-32+-chars>            # generar con secrets.token_urlsafe(48)
JWT_ALGORITHM=HS256
JWT_EXPIRATION_MINUTES=1440               # 24h

# OAuth providers
GOOGLE_CLIENT_ID=<.apps.googleusercontent.com>
X_CLIENT_ID=<...>
X_CLIENT_SECRET=<...>
```

### Variables por tareas (futuro)

```
# T-data-1/2/3
SCHEDULER_ENABLED=true
SCHEDULER_TIMEZONE=Europe/Madrid
SCHEDULER_INGEST_INTERVAL_HOURS=12
SCHEDULER_INGEST_JITTER_SECONDS=300
SCHEDULER_MAX_RETRIES=3
SCHEDULER_SNAPSHOT_LEAD_DAYS=4
SCHEDULER_FETCH_TIMEOUT_SEC=10
SCHEDULER_KEEP_FETCH_SESSIONS_DAYS=30
```

### Secrets management
- `.env` está en `.gitignore`, NO se commitea
- `.env.example` contiene solo placeholders, SÍ se commitea
- En dev: `.env` con credenciales reales
- En prod (futuro): secrets via variable de entorno del orquestador (Docker/K8s)

---

## 9. Limitaciones conocidas / TODOs

### Ya acordadas
- **Auth sin refresh tokens**: usuario debe re-loguearse cada 24h
- **Sin invitaciones por link**: solo join_code (compartir el código manualmente)
- **Sin email verification**: emails no se validan, el login es directo
- **Sin password reset**: si olvidás la contraseña, no podés recuperarla (futuro MVP-03)
- **Sofascore widget descartado**: NO usamos iframes de Sofascore (404s). Para data de partidos, dependemos de API-Football (gratis hasta 2024)

### Datos / freshness
- **Free plan API-Football**: máximo 2024/25 (no hay datos de 2026/27). H2H y forma en `jornada_escrutinio` y `team_id_api_football` están basados en 2024.
- **team_id_api_football: 60/71** del legacy: 11 equipos del 2024/25 ya no están en 2026/27 (relegados/rebranded)
- **4 partidos de J11 sin H2H**: son filiales y Liga F sin canónico en mock_quiniela

### Decisiones técnicas tomadas hoy
- **`unaccent` is STABLE not IMMUTABLE** → se usa wrapper `immutable_unaccent(text)` para el índice único de dedup. Si cambia Postgres en el futuro, re-evaluar.
- **`/jornadas` sin prefijo `/api/v1`**: divergencia con `/api/v1/competitions` y `/api/v1/teams`. Worth a future consistency pass.
- **`JornadaStatus` como TEXT con CHECK** (no Postgres ENUM type) para que Alembic autogenerate funcione smooth

### Lo que necesitarías en DB-backed

- **Tabla `country`** referencia con código de país (3-letter ISO) para equipos. Schema actual tiene `country: VARCHAR(8)` que guarda ISO codes para clubs (e.g., `'ESP'`) y NULL para national teams. Mostrar nombres completos requiere join contra tabla de países.
- **Tabla `competition_extra`** (futuro): tier, official URL, etc. La actual tabla `competition` tiene esos campos pero no incluye las "extras" como relinking URLs de UEFA.

---

## 10. Cómo seguir

### Orden recomendado

```
1. T-data-1 (Quiniwin client + matching + bootstrap)
   ↓ porque sin esto no podemos ingestar nada
2. T-data-2 (scheduler recurrente)
   ↓ porque automatiza la detección de nuevas jornadas
3. T-data-4 (Losilla persistence)
   ↓ porque cierra el ciclo "live + closed"
4. T7 (build_jadaptar build_jornada.py)
   ↓ opcional: conecta el prototipo HTML a la API
5. Admin endpoints (MVP-03)
6. Votation endpoints (MVP-02)
```

### Estimación total

- T-data-1: 4-6h
- T-data-2: 3-4h
- T-data-4: 3-4h
- T7: 1-2h (opcional)
- MVP-02: 1-2 semanas (más scope)
- MVP-03: 1 semana (admin)

### Cuándo estás listo para DB-backed

Si tienes la **shape de la DB** pensada (como mencionaste en una sesión anterior: "una tabla con todos los equipos que pueden aparecer en la quiniela con su id correspondiente"), lo que queda es:
1. Crear esa tabla (migración Alembic nueva)
2. Migrar los datos del legacy `scripts/team_ids_api_football.py` y `data/team-catalog.json`
3. Adaptar `app/clients/eduardolosilla.py` para usarla (lookup + creación de stubs)
4. Re-aprovechar T-data-1 / T-data-2 / T-data-4 que ya tenemos diseñados