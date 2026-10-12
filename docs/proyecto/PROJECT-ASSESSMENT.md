# La Goyniela — Assessment de proyecto

**Fecha**: 2 de octubre de 2026
**Estado**: v0.4.x — pipeline de datos completo (Quiniwin → DB → API → HTML)
**Tests**: 204 pasando
**Próximo milestone**: MVP-02 (votación) o MVP-03 (admin)

---

## TL;DR

El proyecto está en un **estado sólido como v0 funcional**. El pipeline de datos
está cerrado: Quiniwin ingesta composiciones automáticamente vía scheduler, las
jornadas viven en Postgres, y el HTML del prototipo se renderiza desde la API.

Lo que falta para producción:
- **Votación** (MVP-02) — el feature de usuario
- **Admin endpoints** (MVP-03) — para fix de stubs
- **Auth improvements** — refresh tokens, password reset

---

## Lo que está hecho

| Fase | Tarea | Estado |
|---|---|---|
| Bootstrap | T1-T4 (deps, DB session, Alembic, catalog models) | ✅ |
| Catalog | T5-T6 (9 competiciones, 423 equipos, 4 endpoints read-only) | ✅ |
| Users + Groups | T-users-1/2/3 (auth + grupos + roles) | ✅ |
| Jornada schema | T8 (5 tablas + dedup + endpoints) | ✅ |
| Quiniwin pipeline | T-data-1 (client + matcher + CLI + bootstrap) | ✅ |
| Scheduler | T-data-2 (AsyncIOScheduler + job Quiniwin diario) | ✅ |
| Losilla persistence | T-data-4 (CLI + router DB-first) | ✅ |
| Prototipo HTML | T7 (build_jornada.py contra API) | ✅ |

### Endpoints funcionales (24 rutas)

```
Auth          POST/GET  /auth/{register,login,google,x,me}
Grupos        9 CRUD   /groups, /groups/{id}, /groups/{id}/{leave,delete,members/{uid}/{promote,demote},members/{uid}}
Competiciones  GET      /api/v1/competitions, /api/v1/competitions/{id}
Equipos       GET      /api/v1/teams, /api/v1/teams/{id}
Losilla       GET      /api/v1/losilla/jornada/{n}, /escrutinio, /comentarios
Jornadas      GET      /jornadas, /jornadas/{id}
Health        GET      /health, /health/db
Docs          GET      /docs, /redoc, /openapi.json
```

### Pipeline de datos (end-to-end)

```
Quiniwin XML
    │
    ▼
[Scheduler job diario 09:00 Madrid]
    │
    ▼
[QuiniwinClient] → parse XML → match teams (short_name + canonical + alias)
    │
    ▼
[jornada + composicion]  ───►  /jornadas/{id}  ───►  build_jornada.py  ───►  mockups/jornada/jornada.html

[LosillaClient] (CLI on-demand)
    │
    ▼
[jornada_escrutinio + jornada_comentario]  ───►  /api/v1/losilla/.../{escrutinio,comentarios} (DB-first, fallback upstream)
```

---

## Estadísticas

```
Código (app/ + scripts/):   ~10.500 líneas
Tests:                       ~4.800 líneas
Tests pytest: 204 pasando
Endpoints:                 24
Tablas DB:                  13 (catalog, users, groups, jornada, fetch_session)
Tablas seedeadas:           5 (competition, source, team_identity_map, team_id_api_football, country)
Schemas Pydantic:           ~25
```

---

## Deuda técnica conocida

### Gaps de matcher** (~18 unique después de 3 iteraciones)

| Categoría | n | Acción |
|---|---|---|
| Truncados/tipos Quiniwin (Bodo Gli, Villarrea, Racing San, Rc Len) | 4 | Alias puntual |
| UEFA naming diff (Nápoles, Plzen, Sporting De Portugal) | 4 | Alias puntual |
| Liga F no en seed (Badalona, Logroño, Badalona W.) | 3 | Admin endpoint MVP-03 |
| Alias puntuales faltantes (Rayo, At. Madrid, Deportivo) | 3 | 5 min de aliases |
| Nacional con punto (Rep. Checa, Rep. Irlanda) | 2 | Alias puntual |

### Funcionalidades pendientes

- **Job Losilla lunes** en scheduler: definido en `app/scheduler/runner.py` pero no wired. 5 min para agregar `add_job(...)`.
- **`SCHEDULER_AUTORUN_ON_STARTUP`**: env var definida pero no aplicada.
- **`source` table seed en tests**: `fetch_session.source_id` queda NULL porque la tabla `source` no se seedea automáticamente.
- **`competicion_raw` siempre NULL**: el schema de Quiniwin no emite competición per-partido. Para enriquecer habría que cruzar heurísticamente (ej posiciones 11-14 son Liga F).

### Calidad de tests

- 6 tests de router duplicados entre `test_losilla_persistence.py` (líneas 469-697) y `test_losilla_db_first.py`.
- Flake en `test_groups.py::test_remove_member_from_group` cuando scheduler tests corren en medio. Pasa en aislamiento, probablemente issue de cleanup fixture.
- No hay tests para `scripts/mock_quiniela.py` (legacy).

### Decisiones diferidas

- Multi-container scheduler vs in-process (doc recomienda in-process para v1)
- Refresh tokens / password reset / email verify
- Sin ad tracking de salud del upstream Quiniwin
- Sin circuit breaker para rate limiting
- Sin monitoring / dashboards de `fetch_session`

---

## Riesgos prioritarios

1. **Quiniwin URL "no eterna"** (research 02-fuentes.md). Si cambia el patrón `_{a}-{b}_j{NN}.xml`, todo el matcher cae. Sin monitor.
2. **`bcrypt>=4.0,<4.1` pin** por compatibilidad con passlib 1.7. Si actualizamos passlib, hay que actualizar bcrypt también.
3. **Deprecation warning** de Python 3.12 sobre `crypt` module (usado por bcrypt vía passlib). Migración pendiente a `bcrypt` directo.
4. **Liga F fixtures**: posiciones 11-14 son Liga F. Si Quiniwin no incluye estos partidos, no hay datos de esos 4 partidos.
5. **No rate limiting** en scheduler — 20+ requests/día a Quiniwin. Sin circuit breaker.

---

## Próximos pasos recomendados

### Quick wins (≤30 min cada uno)

1. Wirear el job Losilla lunes en `app/scheduler/runner.py` — 5 min.
2. Agregar 8-10 aliases al matcher para bajar stubs a <10 — 15 min.
3. Eliminar duplicación de tests de router entre `test_losilla_persistence.py` y `test_losilla_db_first.py` — 10 min.
4. Investigar flake de `test_groups.py::test_remove_member_from_group` — 20 min.

### Features reales

5. **MVP-02 — Votación** (~1-2 semanas):
   - `POST /groups/{id}/picks` para votar
   - Endpoint de tally de votos
   - Algoritmo de scoring
   - Endpoint para exportar jugada final
6. **MVP-03 — Admin** (~1 semana):
   - `PATCH /api/v1/teams/{entity_id}` — fix stubs (logo, competition_id, gender, country)
   - `GET /api/v1/teams?competition=unknown` — lista stubs pendientes
   - `GET /admin/fetch-sessions` — historial de batches
   - `POST /admin/jornadas/{id}/ingest?force=true` — batch manual
   - Auth admin (JWT claim `is_admin`)
7. **Auth improvements** (~1 semana):
   - Refresh tokens
   - Password reset
   - Email verification

### Arquitectura

8. **Snapshot strategy** del `scheduler.md` — implementar snapshot final T-3/4d.
9. **API-Football integration** — llevar `scripts/team_ids_api_football.py` al `app/clients/` para H2H real en build_jornada.
10. **Odds API integration** — doc menciona "Spanish odds source" como decisión pendiente.

---

## Cómo seguir

```bash
# Levantar el stack
docker compose up -d                    # Postgres en :5433
uv run alembic upgrade head             # migraciones
uv run python -m app.scripts.load_team_catalog  # solo primera vez
uv run python -m app.scripts.ingest_quiniwin --temporada 2026 --jornadas 1..10
uv run python -m app.scheduler.runner  # en otra terminal

# Tests
uv run pytest tests/ --ignore=tests/test_team_catalog.py

# API
uv run uvicorn app.main:app --reload
open http://localhost:8000/docs
```

---

**Decisiones que dependen del usuario, no del equipo**:

- ¿Multi-container scheduler o in-process? (doc dice in-process, OK para v1)
- ¿Odds API gratis (The Odds API) o scraping?
- ¿Refresh tokens o re-login cada 24h?
- ¿Cuándo (tournament) Lanzar Quiniwin dejó de funcionar? ¿Plan B con API-Football directo?