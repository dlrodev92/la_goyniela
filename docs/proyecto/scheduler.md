# La Goyniela — Scheduler / Ingesta (plano v1)

**Fecha**: 27 de septiembre de 2026
**Estado**: plano (sin implementación todavía)
**Complemento de**: `modelo-datos.md`, `arquitectura.md`

Este documento define cómo, cuándo y con qué cadencia la app trae los datos
externos (las 5 fuentes) y los persiste en la base. Lo acordado:

- Postgres, UPSERT, scheduler como proceso separado del web.
- Snapshot a T-3/4 días antes del primer partido de la jornada (sin "freeze"
  explícito; el silencio del scheduler es la garantía).
- Trazabilidad por fila (`fetch_session_id`).

## 1. Forma física

Dos procesos, una base compartida:

```
+--------------+        +-----------------------+
| uvicorn app  |        | APScheduler in-process|
| FastAPI      |        | (mismo container o    |
| :8000        |        |  proceso separado)    |
|              |        |                       |
| Lee de Postgres| ◀────┤ Escribe a Postgres   |
+--------------+        +----------─▲───────────┘
                                      │
                                      │ fetch + upsert
                                      ▼
                            +-------------------+
                            |  5 APIs externas  |
                            |  (HTTP out)       |
                            +-------------------+
```

El scheduler puede correr:

- (a) **En el mismo proceso FastAPI** vía lifespan. Simple, una sola app
  que arranca. Adecuado para servidores single-container.
- (b) **Como proceso separado** que comparte la misma DB. Mejor para
  deploy donde querés escalar la web sin escalar el scheduler.

Para v1, **(a) es suficiente**. Un solo worker FastAPI levanta el
scheduler en arranque. Si después queremos (b), es mover
`app/scheduler/runner.py` a un contenedor separado y agregar un Docker
compose con 2 servicios.

## 2. Decisión de cadencia (cuándo fetchear)

La Quiniela alterna jornadas de clubes (semana) con jornadas de selección
(fines de semana) cada 1-2 semanas. La cadencia del scheduler es:

```
┌─────────────────────────────────────────────────────────────┐
│  Por jornada, ciclo semanal:                                  │
│                                                              │
│  L  M  W  J  V  S  D   ← día                                │
│  ───────────────────────────────────────────────              │
│  ▲        ▲        ▲     ▲     ▲                             │
│  │        │        │     │     │                             │
│  T-7d   T-5d    T-3/4d  T-1d   T+0 (kickoff del 1er partido)│
│                                                              │
│  fetch   fetch   ULTIMO   silencio: ya está el snapshot,    │
│  1       2       fetch    scheduler descansa                │
│           (cada 12h)  T-3/4d:↑                              │
│                          UPSERT  con último valor            │
└─────────────────────────────────────────────────────────────┘
```

Resumen en una tabla:

| Día vs kickoff | Acción | Por qué |
|---|---|---|
| T-7 a T-5 | Fetch diario de las 5 fuentes | Refresh temprano |
| T-5 a T-3/4 | Fetch cada 12 horas | Acercar a cierre |
| **T-3/4 días** | **Último fetch (snapshot final)** | El grupo va a usar esto |
| T-3/4 a T-1h | **Silencio** | No reescribir (es UPSERT pero confiamos en que no escribe) |
| T-1h | Cierre del **boleto** (no del snapshot) | Picks confirmados por admin |
| T+0 (kickoff) | Partido se juega | Datos "oficiales" empiezan a llegar por Odds y Football-Data |
| post-kickoff | Sin acción para esa jornada | Scheduler arranca con la siguiente |

Notas operacionales:

- **El "snapshot" no es una feature explícita** — es la última versión que
  quedó en `estimacion_snapshot` antes del silencio. Por eso la #2 dijo
  "UPSERT simple + cadencia".
- **El timeout por carrera**: si el scheduler arranca un batch 5min después
  del kickoff (schedulers a veces se atrasan), todavía podría escribir en
  la jornada vieja. Solución: el batch del scheduler compara `kickoff_at`
  con `NOW()` antes de hacer UPSERT — si el primer partido ya arrancó,
  el batch se aborta.
- **1 jornada a la vez por simplicidad**. Multi-jornada simultánea no se
  considera (la quiniela juega una jornada por semana).

## 3. Adapters de las 5 fuentes: qué fetchea cada uno

```
adapters/losilla.py:
  - fetch_real(jornada, temporada)        → Lista de 15 partido con porc_X
  - fetch_lae(jornada, temporada)          → Lista de 15 partido con porc_X (LAE)
  - parse(xml bytes)                       → (partido_attrs, summary)

adapters/quiniwin.py:
  - fetch(temporada_str, jornada)          → xml con 15 partidos ordenados

adapters/football_data.py:
  - fetch_season(temporada)                → CSV SP1
  - fetch_season(temporada)                → CSV SP2
  - fetch_fixtures()                       → CSV partidos próximos
  - parse_csv(bytes, division)             → rows

adapters/odds_api.py:
  - fetch_soccer_spain_la_liga()           → events de LaLiga
  - fetch_soccer_spain_segunda_division()  → events de Segunda
  - fetch_soccer_uefa_nations_league()     → events de UEFA
  - extract_h2h_consensus(event)           → {1, X, 2, overround_pct}

adapters/api_football.py:
  - fetch_h2h(team1_id, team2_id)          → fixtures H2H
  - fetch_team_season_league(league, year) → ids + nombres
  - fetch_fixture_date(team1, team2)       → kickoff_at
```

Cada adapter es **idempotente**: dado el mismo input + tiempo similar,
produce los mismos registros. Es responsabilidad del scheduler no romper
la idempotencia con cambios de timezone, headers, etc.

## 4. Pipeline de ingesta

Por cada (jornada, fuente), el pipeline hace:

```
1. fetch
   - Con cache: si hay cache fresco (<TTL) usar cache
   - Sin cache: GET HTTP, almacena respuesta cruda

2. parse + normalize
   - Bytes crudos → estructura Python (normalizada)
   - Resolve identidad: cada nombre crudo → canónico vía team_identity_map
   - Si no hay match: marca "unknown_team"

3. filtro
   - Solo partidos que están en composicion[jornada]
   - Otros partidos: log debug + descartar (no se guardan)

4. UPSERT
   - INSERT … ON CONFLICT DO UPDATE
   - Captura fetch_session_id del batch

5. audit
   - Log "fetched N rows from {fuente} for jornada {J}"
   - Si fail: log error y retry con backoff
```

Orquestador: `app/pipeline/jornada_runner.py`. Función principal:

```python
async def run_jornada_ingest(jornada_id: int, force: bool = False):
    fs = FetchSession(id=uuid4(), started_at=now())
    stats = {}
    try:
        stats["losilla_real"] = await losilla.fetch_and_persist(jornada_id, fs.id)
        stats["quiniwin"] = await quiniwin.fetch_and_persist(jornada_id, fs.id)
        stats["football_data"] = await football_data.fetch_and_persist(jornada_id, fs.id)
        if settings.has_odds_key():
            stats["odds_api_uk"] = await odds_api.fetch_and_persist(jornada_id, fs.id)
        if settings.has_api_football_key():
            stats["api_football"] = await api_football.fetch_and_persist(jornada_id, fs.id)
        fs.finished_at = now()
        fs.source_stats = stats
        fs.total_rows = sum(s.get("rows", 0) for s in stats.values())
    except Exception as exc:
        fs.notes = repr(exc)
    finally:
        await db.commit()
```

`force=True` se usa cuando la composición cambió (admin la re-confirmó);
sin force, el scheduler hace "diff vs última versión" y solo fetchea si
hubo cambios reales.

## 5. APScheduler config

```python
# app/scheduler/runner.py
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.interval import IntervalTrigger
from apscheduler.triggers.cron import CronTrigger

def main():
    scheduler = AsyncIOScheduler(timezone="UTC")

    # Fetch cada 12 horas (cubre T-7 a T-3/4)
    scheduler.add_job(
        run_jornada_ingest,
        IntervalTrigger(hours=12, jitter=300),    # ±5min jitter
        id="ingest_12h",
        coalesce=True,
        max_instances=1,
        kwargs={"force": False},
    )

    # Snap final (T-3/4d antes del primer partido de la próxima jornada)
    # Activado por composición que entra en ventana snapshot. Logica en runner.

    scheduler.start()
    # SIGTERM handler para apagado limpio
```

El trigger "snap final" no es fijo — depende de la jornada. Se programa
dinámicamente cuando `composicion.kickoff_at` se conoce.

## 6. Reintentos y manejo de fallos

Para cada adapter:

```
try:
    resp = httpx.get(url, headers=hdrs, timeout=10)
    resp.raise_for_status()
    return resp.content
except httpx.TimeoutException:
    log warn "timeout"
    raise AdapterTimeout(source)
except httpx.HTTPStatusError as e:
    if e.response.status_code in (403, 429, 500, 502, 503):
        log warn "status {code}"
        raise AdapterTransient(source)
    raise
```

Retry con tenacity en cada adapter:

```python
@retry(
    retry=retry_if_exception_type(AdapterTransient),
    wait=wait_exponential(min=4, max=60),
    stop=stop_after_attempt(3),
)
async def fetch(self):
    ...
```

| Falla | Acción |
|---|---|
| Timeout 1 vez | Log warn, reintenta |
| Timeout 3 veces | Log error, aborta ingest de esa fuente |
| 403 / 429 | Retry con backoff (respeto de rate limit) |
| 4xx distinto | Log error, no retry, aborta esa fuente |
| 5xx | Retry con backoff |
| JSON/XML malformado | Log error + snapshot para revisión, aborta esa fuente |

Si **una fuente** falla: el resto de la ingesta continúa. Solo log
"fuente X falló, snapshot parcial" pero no se rompe el batch.

## 7. Cache de respuestas crudas

Para evitar pegarle 12 veces a una API que devuelve lo mismo, una capa
de cache **en disco** (SQLite local o archivos JSON):

```
cache/
├── losilla_real_j9_2026-09-27T14:00:00Z.xml    # primera captura
├── losilla_real_j9_2026-09-27T22:00:00Z.xml    # segunda (solo si cambió)
└── ...
```

Política:

- TTL por fuente: 30 min para Odds API y API-Football (cambian rápido),
  6 horas para Football-Data, 1 hora para Losilla/Quiniwin.
- **Cache útil solo para replay** (debugging / re-derivación), no para
  reducir cuota. La cuota se cuenta por HTTP real, no por cache hit.
- **Cache NO se consulta para producción normal**. Solo se usa si la API
  externa está caída y queremos fallback al último conocido.

## 8. Configuración del scheduler (env vars)

```
# app/scheduler/config.py
SCHEDULER_ENABLED=true           # false para correr solo el web
SCHEDULER_TIMEZONE=UTC           # Europe/Madrid si querés
SCHEDULER_INGEST_INTERVAL_HOURS=12
SCHEDULER_INGEST_JITTER_SECONDS=300   # evitar ejec exactamente al mismo segundo
SCHEDULER_MAX_RETRIES=3
SCHEDULER_SNAPSHOT_LEAD_DAYS=4   # snapshot a T-4 días (configurable)
SCHEDULER_SNAPSHOT_LEAD_HOURS=72 # hasta T-3 días
SCHEDULER_FETCH_TIMEOUT_SEC=10
SCHEDULER_KEEP_FETCH_SESSIONS_DAYS=30
```

`SCHEDULER_KEEP_FETCH_SESSIONS_DAYS`: las `fetch_session` antiguas se
purgan después de N días (job diario de housekeeping).

## 9. El "scheduler runner" entrypoint

```
$ python -m app.scheduler.runner
```

Configuración mínima:

```python
# app/scheduler/runner.py
import asyncio
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from app.config import settings
from app.pipeline.jornada_runner import run_jornada_ingest

async def main():
    if not settings.scheduler_enabled:
        print("Scheduler is disabled; exiting")
        return
    sched = AsyncIOScheduler(timezone=settings.scheduler_timezone)
    sched.add_job(
        run_jornada_ingest,
        "interval",
        hours=settings.scheduler_ingest_interval_hours,
        jitter=settings.scheduler_ingest_jitter_seconds,
        coalesce=True,
        max_instances=1,
    )
    sched.start()
    try:
        await asyncio.Event().wait()    # bloquear para siempre
    except (KeyboardInterrupt, SystemExit):
        sched.shutdown()

if __name__ == "__main__":
    asyncio.run(main())
```

En Docker:

```dockerfile
FROM python:3.11-slim
WORKDIR /app
RUN pip install uv
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen
COPY . .
ENV PYTHONUNBUFFERED=1
CMD ["python", "-m", "app.scheduler.runner"]
```

## 10. Métricas mínimas a loguear

Cada batch deja trazas en `fetch_session`:

```
fetch_session:
  - id (uuid)
  - started_at
  - finished_at
  - source_stats:
      losilla_real: {status: ok, rows_inserted: 15}
      losilla_lae:  {status: ok, rows_inserted: 15}
      quiniwin:     {status: ok, rows_inserted: 15}
      football_data: {status: ok, rows_inserted: 135}
      odds_api:     {status: ok, rows_inserted: 32, "credits_consumed_estimate": 8}
      api_football: {status: missing_key, rows_inserted: 0}
  - total_rows
  - notes
```

Para monitoring (futuro): tabla `fetch_session` consultable por endpoint
admin/doors y por script de alertas (cuando una fuente falla 3 batches
seguidos, alerta).

## 11. Decisiones diferidas

- **Multi-jornada simultánea**: no se considera v1 (la quiniela juega una
  por semana). Si lo agregamos, será snapshot por jornada.
- **Trigger on-demand** (admin dispara fetch manual): queda para v2.
- **Distribución entre múltiples servidores**: no en v1.
- **Feature de "auto-recovery" si una fuente está caída varios días**:
  no en v1.
- **Manejo de cambios de Quota de APIs**: no en v1. Detectamos manualmente.

## 12. Diagrama de secuencia de un batch

```
[Scheduler tick]                        [Adapters]                  [Postgres]
       │                                    │                            │
       │  ──────── start ────────▶          │                            │
       │  fetch session id = uuid()         │                            │
       │                                    │                            │
       │  ──── Losilla.fetch_real() ────▶   │                            │
       │                                    │  GET api.eduardolosilla... │
       │                                    │  ◀────────── XML ──────── │
       │                                    │                            │
       │                                    │  INSERT ... ON CONFLICT   │
       │                                    │  ────────────────────▶   │
       │                                    │                            │
       │  ──── FootballData.fetch ──────▶   │                            │
       │                                    │  GET football-data.co.uk  │
       │                                    │  ◀────────── CSV ──────── │
       │                                    │                            │
       │                                    │  UPSERT partido_historico  │
       │                                    │  ────────────────────▶   │
       │                                    │                            │
       │  ──── OddsAPI.fetch (key) ─────▶   │                            │
       │                                    │  GET the-odds-api.com      │
       │                                    │  ◀────────── JSON ────────│
       │                                    │                            │
       │                                    │  UPSERT estimacion + pleno│
       │                                    │  ────────────────────▶   │
       │                                    │                            │
       │  ──── finish (stats, finished) ▶   │                            │
       │                                    │  UPDATE fetch_session     │
       │                                    │  ────────────────────▶   │
       │                                    │                            │
       ▼  log "batch complete"               │                            │
```

## 13. Lo que el admin puede tocar a mano

Endpoints para ajuste manual (`/admin/...`):

- **Forzar ingest de una jornada**: `POST /admin/jornadas/{id}/ingest?force=true`.
  Útil si una fuente estuvo caída varios días.
- **Ver la timeline de fetch_session**: `GET /admin/fetch-sessions?days=7`.
- **Stats de cuota**: `GET /admin/quotas/odds` mostrando créditos consumidos
  estimados este mes.

Esos endpoints los implementaremos cuando haya auth (MVP-02). Por ahora,
el admin puede ejecutar el CLI `python -m app.scheduler.runner --once`
para forzar un batch inmediato.
