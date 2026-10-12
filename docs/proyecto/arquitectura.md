# La Goyniela — Arquitectura + endpoints (plano v1)

**Fecha**: 27 de septiembre de 2026
**Estado**: plano (sin implementación todavía)
**Complemento de**: `docs/proyecto/modelo-datos.md`

Este documento define:

1. Stack técnico concreto (librerías, versiones)
2. Estructura de directorios del monorepo
3. Endpoints HTTP REST (rutas, payloads, status codes)
4. DTOs (Pydantic) — qué sale de la API
5. Configuración (env vars, secrets)
6. Tests
7. Deployment (mínimo viable)

## 1. Stack técnico

**Lenguaje y runtime**:
- Python **3.11+** (mínimo para `tomllib`, asyncio mejorado, type hints finos)
- `pip` + `pyproject.toml` (PEP 621) o Poetry — **usaremos `uv`** por velocidad (Astral).

**HTTP framework**:
- FastAPI **0.110+** — schema-first con OpenAPI auto-generado.
- Uvicorn (ASGI server) con worker único para v1.

**ORM y DB**:
- SQLAlchemy **2.0+** (estilo async + declarative moderno).
- Alembic **1.13+** para migraciones (con soporte async).
- psycopg3 (`psycopg[binary]` 3.1+) — driver PG con tipos nativos de Postgres (JSONB, UUID, ENUM).

**DTO / validación**:
- Pydantic **v2** (más rápido, mejor discriminated unions).

**HTTP client** (consumir las 5 APIs externas):
- httpx **0.27+** (cliente async con HTTP/2 opcional).

**Scheduler**:
- APScheduler **3.x** (in-process, ligero, suficiente para v1).
- Alternativa para futuro: cron externo llamando a un CLI.

**Logging**:
- structlog (JSON-friendly) → consola; en futuro a un agregador.

**Tests**:
- pytest + pytest-asyncio
- pytest-httpx (mockear las APIs externas)
- testcontainers-python (Postgres efímero en Docker para integration)
- factory-boy (construir fixtures de DB)

**Otras**:
- pydantic-settings — carga de env vars tipada
- tenacity — reintentos idempotentes con backoff
- python-dateutil — parsing de fechas heterogéneas
- pyjwt — tokens cuando llegue MVP-02 (no ahora)

Todo en `pyproject.toml`. No usamos Flask, Django ni starlette directo.

## 2. Estructura de directorios

Monorepo, aplicación única con dos entry points (`app.py` para HTTP,
`scheduler.py` para ingesta) y un paquete compartido.

```
la_goyniela/
├── pyproject.toml             # uv + dependencias + scripts
├── uv.lock
├── README.md
├── .env.example               # documentación de env vars
├── .gitignore                 # .env, __pycache__, .pytest_cache, *.db
├── docs/                      # ya existe
│   └── proyecto/
│       ├── estado-exploracion.md
│       ├── modelo-datos.md
│       ├── arquitectura.md    (este archivo)
│       └── scheduler.md       (próximo)
├── alembic.ini                # config Alembic
├── migrations/                # Alembic
│   ├── env.py
│   └── versions/
│       ├── 0001_initial.py    # crea tablas del modelo de mercado + seed
│       └── ...
├── scripts/                   # ya existe
│   ├── probe_sources.py
│   ├── mock_quiniela.py
│   ├── build_team_ids.py
│   ├── team_ids_api_football.py
│   └── ...
├── app/                       # nueva carpeta
│   ├── __init__.py
│   ├── main.py                # FastAPI instance + lifespan + routers
│   ├── config.py              # pydantic-settings
│   ├── db.py                  # SQLAlchemy engine + session factory
│   ├── models/                # ORM models (mapeo a tablas)
│   │   ├── __init__.py
│   │   ├── source.py
│   │   ├── team_identity.py
│   │   ├── jornada.py
│   │   ├── composicion.py
│   │   ├── estimacion.py
│   │   ├── pleno.py
│   │   ├── historico.py
│   │   ├── h2h.py
│   │   └── fetch_session.py
│   ├── schemas/               # DTOs Pydantic
│   │   ├── __init__.py
│   │   ├── jornada.py
│   │   ├── partido.py
│   │   ├── estimacion.py
│   │   └── admin.py
│   ├── routers/               # FastAPI routers
│   │   ├── __init__.py
│   │   ├── jornada.py
│   │   ├── partido.py
│   │   ├── admin.py
│   │   └── salud.py
│   ├── adapters/              # adapters de las 5 fuentes
│   │   ├── __init__.py
│   │   ├── losilla.py
│   │   ├── quiniwin.py
│   │   ├── football_data.py
│   │   ├── odds_api.py
│   │   └── api_football.py
│   ├── pipeline/              # orquesta: adapter → normalizer → DB
│   │   ├── __init__.py
│   │   ├── normalize.py       # mapea respuesta → row de DB
│   │   ├── upsert.py          # INSERT … ON CONFLICT
│   │   └── jornada_runner.py  # orquesta fetch+ingesta de una jornada
│   └── scheduler/
│       ├── __init__.py
│       └── runner.py          # entrypoint `python -m app.scheduler`
├── tests/                     # nueva carpeta
│   ├── conftest.py            # fixtures globales
│   ├── test_adapters/
│   │   ├── test_losilla.py
│   │   ├── test_odds_api.py
│   │   └── ...
│   ├── test_pipeline/
│   └── test_api/
│       ├── test_jornada.py
│       └── ...
└── docker-compose.yml         # Postgres local dev (opcional)
```

## 3. Endpoints HTTP

**Prefijo**: `/api/v1`
**Formato**: JSON en request y response.
**Errores**: RFC 7807 problem-details.
**Versionado**: el segmento `/v1/` ya está. Cuando rompamos contrato, duplicamos a `/v2/`.

### `GET /api/v1/salud`

Healthcheck.

```
200 OK
{
  "status": "ok",
  "db": "reachable",
  "scheduler_running": true
}
```

### `GET /api/v1/jornadas/current`

Última jornada detectada (la que aún no cerró). Si varias están abiertas, la
del partido más cercano al kickoff.

```
200 OK JornadaListDto {
  temporada: 2027,
  numero: 9,
  temporada_str: "2026/27",
  partidos: 15,
  latest_kickoff_at: "2026-09-27T19:02:00Z",
  active_lae: true,
  links: {
    "self": "/api/v1/jornadas/2027/9"
  }
}
```

### `GET /api/v1/jornadas/{temporada}/{numero}`

Detalle de una jornada.

```
200 OK JornadaDetailDto {
  temporada, numero, temporada_str,
  partidos: [
    PartidoResumenDto:
      id, posicion, local_canonical, visitante_canonical,
      competition_type, kickoff_at, competition_code,
      sources_available: ["losilla_real", "odds_api_uk", ...],
      has_odds_uk: true,
      has_h2h: false,
      has_historico_local: true,
      has_historico_visitante: false
    ... 15 items
  ]
}
```

Si la jornada no existe en DB, **404** con problem-details.

### `GET /api/v1/partidos/{id}`

Detalle completo de un partido.

```
200 OK PartidoDetalleDto {
  id, jornada_id, posicion,
  local_raw, local_canonical,
  visitante_raw, visitante_canonical,
  competition_type, kickoff_at,
  estimaciones: [
    EstimacionFuenteDto:
      source: {code, name},
      prob_1, prob_x, prob_2,
      implied_1_raw, implied_x_raw, implied_2_raw,
      houses_count, overround_pct,
      captured_at
    ...
  ],
  h2h: H2HDto | null,
  historico_local: PartidoHistoricoDto[] | [],
  historico_visitante: PartidoHistoricoDto[] | [],
  captions: ["competition_type=liga_f: sin cuotas UK", ...]
}
```

### `GET /api/v1/partidos/{id}/estimaciones`

Alias para solo las estimaciones de un partido (subset del anterior).

### `GET /api/v1/partidos/{id}/h2h`

Solo el H2H.

```
200 OK H2HDto {
  fixture_count,
  matches: [
    {fecha, competition, home, away, score} ...
  ],
  source: {code, name},
  note: "unknown_team_ids" | "international" | "ok"
}
404 Not Found: si no hay H2H para este partido.
```

### `GET /api/v1/partidos/{id}/historico?limit=5`

Últimos N partidos del local y visitante (default 5, max 20).

### `GET /api/v1/admin/doors`

Estado de las 5 puertas G1–G5 (feature futura — define qué tan "saludable"
es nuestra data).

```
200 OK DoorsDto {
  doors: [
    {id: "G1", label: "Losilla autorización", status: "open|closed|unknown"},
    ...
  ]
}
```

### `GET /api/v1/equipos/{canonical}`

Info de un equipo por nombre canónico. Útil para debugging de identidad.

```
200 OK EquipoDto {
  canonical_team_name,
  aliases: [{source, raw_name}, ...],
  api_football_id: 541,
  competition_type_seen: ["sp1"]
}
```

## 4. DTOs (Pydantic v2)

Definidos en `app/schemas/`. Ejemplo:

```python
# app/schemas/jornada.py
from pydantic import BaseModel, Field
from datetime import datetime

class PartidoResumenDto(BaseModel):
    id: int
    posicion: int = Field(ge=1, le=15)
    local_canonical: str | None
    visitante_canonical: str | None
    competition_type: str
    kickoff_at: datetime | None
    competition_code: str | None
    sources_available: list[str]
    has_odds_uk: bool
    has_h2h: bool
    has_historico_local: bool
    has_historico_visitante: bool


class JornadaDetailDto(BaseModel):
    temporada: int
    numero: int
    temporada_str: str
    partidos: list[PartidoResumenDto]


class JornadaListDto(BaseModel):
    temporada: int
    numero: int
    temporada_str: str
    partidos: int = Field(ge=15, le=15)
    latest_kickoff_at: datetime | None
    active_lae: bool
    links: dict[str, str]
```

Pydantic v2 con `model_config = ConfigDict(from_attributes=True)` para
serializar desde ORM. Esto le da validación auto al request y conversión al
cliente.

## 5. Configuración

### Env vars (cargadas con `pydantic-settings`)

```
# Database
DATABASE_URL=postgresql+psycopg://user:pass@host:5432/dbname
DATABASE_ECHO=false           # SQL log (dev only)
DATABASE_POOL_MIN=2
DATABASE_POOL_MAX=10

# API keys externos
ODDS_API_KEY=...             # NO COMMITEAR
API_FOOTBALL_KEY=...         # NO COMMITEAR

# Logging
LOG_LEVEL=info               # debug|info|warning|error
LOG_JSON=false               # true para producción

# Comportamiento scheduler
SCHEDULER_ENABLED=true
SCHEDULER_CRON_DAILY=0 8,20 * * *   # 8am + 8pm UTC
SCHEDULER_TZ=UTC

# Server
APP_HOST=0.0.0.0
APP_PORT=8000
APP_VERSION=0.1.0
```

### Ejemplo `.env.example`

```
DATABASE_URL=postgresql+psycopg://goyniela:dev@localhost:5432/goyniela_dev
DATABASE_ECHO=false

ODDS_API_KEY=
API_FOOTBALL_KEY=

LOG_LEVEL=info
SCHEDULER_ENABLED=true
APP_HOST=0.0.0.0
APP_PORT=8000
```

En `.gitignore` (al menos):
```
.env
*.db
__pycache__/
.pytest_cache/
.venv/
.coverage
node_modules/
/tmp/
/cache/
```

## 6. Tests

Tres niveles:

1. **Unit (rápidos, sin I/O)**: parsers, normalizers, validators. Sin HTTP.
   `pytest app/tests/unit/...`

2. **Adapter (con HTTP mockeado)**: cada adapter de las 5 fuentes tiene
   un test que inyecta respuestas grabadas (fixtures en
   `tests/fixtures/{source}/`) usando `pytest-httpx`. Verifica que
   extraemos lo esperado. `pytest app/tests/adapter/...`

3. **Integration (con DB efímera)**: cada endpoint se levanta contra un
   Postgres efímero (testcontainers-python o un `docker run` arrancado por
   conftest), se seedea con `migrations/0001_initial.py`, se llama el
   endpoint y se verifica el response. `pytest app/tests/integration/...`

**Cobertura objetivo**: >70% global, >85% en `app/pipeline/`,
`app/adapters/parsers/`. Menos crítico en routers (mejor probar integration).

**Naming**: `tests/test_<module>.py` o `tests/test_<module>/test_<aspect>.py`.

### Ejemplo de test de adapter

```python
# tests/test_adapters/test_odds_api.py
import pytest
from httpx import MockTransport, AsyncClient

@pytest.mark.asyncio
async def test_odds_api_extracts_h2h_quoted():
    fixture = json.loads(Path("tests/fixtures/odds_api/sample.json").read_text())
    with MockTransport(lambda req: httpx.Response(200, json=fixture)):
        async with OddsApiAdapter("KEY") as adapter:
            events = await adapter.fetch_soccer_spain_la_liga()
            pct = adapter.extract_h2h_consensus(events[0])
            assert abs(pct["1"] + pct["X"] + pct["2"] - 100) < 0.1
```

## 7. Deployment (mínimo viable)

Para correr en una máquina sola (VPS, homelab), el patrón es:

```
+------------------------------+
|  tu máquina o VPS            |
|                              |
|  ┌─────────────────────┐    |
|  │  FastAPI (uvicorn)  │    |
|  │   :8000             │ ◀────── curl / browser / app móvil
|  └─────────────────────┘    |
|             │              |
|             ▼              |
|  ┌─────────────────────┐    |
|  │  Postgres 16        │    |
|  │  (Docker o nativo)  │    |
|  └─────────────────────┘    |
|                              |
|  ┌─────────────────────┐    |
|  │  Scheduler (proceso) │    |   ← llama a las 5 APIs externas
|  │  APScheduler         │    |     cada X horas (cron-style)
|  └─────────────────────┘    |
+------------------------------+
```

**Para v1 (personal+amigos)**:
- 1 VPS barato (Hetzner CX22 ~€4/mes, Fly.io con free tier, Railway hobby).
- Postgres administrado (Neon, Supabase) o contenedor en la misma máquina.
- Supervisor + `python -m app.main` para FastAPI.
- `python -m app.scheduler.runner` para el scheduler.

**Los dos procesos comparten la misma DB y los mismos adapters** (no se
importan FastAPI en el scheduler).

**Schema migrations** se aplican con:
```
alembic upgrade head
```

Al arrancar el contenedor se corre `alembic upgrade head` antes de `uvicorn`.

## 8. Decisiones tomadas con respecto a la arquitectura

1. **Monolito, dos procesos**: app FastAPI + scheduler. Comparten DB. No es
   serverless. Esta es la estructura mínima viable.
2. **Sync o async**: en los endpoints el IO es lo único async (DB y HTTP
   externo). El pipeline de scheduler usa async también. Compatible con
   FastAPI.
3. **No Pydantic v1**: usaremos Pydantic v2. Razón: 5-50x más rápido y
   mejor discriminated unions (lo necesitamos en `EstimacionFuenteDto`
   que tiene variantes por fuente).
4. **No usamos Repository pattern** explícito. SQLAlchemy 2.0 ya provee
   su propia capa. Una capa de repos abstrayendo SQLAlchemy es puro
   boilerplate para este tamaño.
5. **Cada adapter = una clase con métodos async** (`fetch_x`,
   `extract_y`). El pipeline las orquesta.
6. **Idempotencia garantizada por UPSERT**: la PK de cada tabla permite
   `INSERT … ON CONFLICT DO UPDATE`. El scheduler corre con la misma
   frecuencia y nunca duplica.

## 9. Lo que NO está en este plano (diferido)

- **Multi-usuario y auth** (MVP-02, fuera de esta fase).
- **Mensajería, notificaciones, chat** (fuera del MVP).
- **Rate limiting por IP** (si la API se expone públicamente).
- **CORS** (asumimos mismo dominio al inicio; si la app tiene front
  separado, se agrega `allow_origins=[...]`).
- **Servidor de tareas distribuidas** (Celery, RQ). APScheduler in-process
  es suficiente para v1 de un solo servidor.
- **OpenAPI documentation client-side** (Swagger/Redoc). FastAPI ya lo da
  en `/docs` y `/redoc` automáticamente.
- **Prometheus / OpenTelemetry metrics**. Diferido a v2 si crece.
- **Background job retry** con DLQ. Un job que falla se loguea y se
  intenta en el próximo tick.

## Diagrama de la arquitectura

```
                 ┌──────────────────────────────────┐
                 │  Cliente (curl / app / browser)  │
                 └─────────────┬────────────────────┘
                               │ HTTP / JSON
                               ▼
       ┌────────────────────────────────────────────┐
       │   uvicorn (FastAPI)                        │
       │   ┌──────┐ ┌──────┐ ┌──────┐ ┌──────┐    │
       │   │ roster│ │partido│ │admin │ │salud │   │
       │   └──────┘ └──────┘ └──────┘ └──────┘    │
       │   schemas (Pydantic)                       │
       └──────┬─────────────────────────────────┬─┘
              │ SQLAlchemy 2.0 async             │ httpx async
              ▼                                  ▼
   ┌──────────────────┐                ┌─────────────────┐
   │  Postgres 16     │                │  Adapters        │
   │  (Alembic migr.) │                │  Losilla        │
   │  9 tablas mercado│                │  Quiniwin       │
   └────────▲─────────┘                │  Football-Data  │
            │                          │  Odds API       │
            │                          │  API-Football   │
            └───── APScheduler ──────┘ └─────────────────┘
                   (mismo proceso,
                    o separado,
                    comparten DB)
```
