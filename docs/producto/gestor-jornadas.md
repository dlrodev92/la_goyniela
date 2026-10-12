# Gestor de jornadas — diseño

**Estado:** borrador para revisión.
**Fecha:** 2026-10-06.
**Relacionado:** `docs/producto/acuerdos-y-pendientes.md` (secciones 4, 7, 10, 12).

## 0. Resumen ejecutivo

El "gestor" es el componente que define el ciclo de vida de cada jornada
(apertura → cierre → escrutinio), hace cumplir la ventana de participación en
el servidor, y entrega al cliente la información consolidada para decidir
qué mostrar sin recalcular. Esta versión corrige además tres focos de bugs
actuales: team matcher (Andorra + género), comentario del Pleno al 15, y
porcentajes embebidos en la respuesta.

El alcance toca **API + motor**; el cliente se informa por contrato, no se
implementa en este repo (no hay frontend versionado acá).

## 1. Cambios al modelo de datos

### 1.1. `Jornada` — dos campos nuevos

```text
+ kickoff_last_match_at:   datetime | None   # fin práctico de la jornada
+ escrutinio_expected_at:  datetime | None   # kickoff_last + 2.5h
```

`voting_closes_at` (existente) cambia de semántica: ya no es "1h antes del
primer partido" (eso decía el doc de producto, ahora corregido), sino
**`kickoff_last_match_at - 1h`**. Eso exige que `kickoff_last_match_at`
esté calculado antes de evaluar la ventana.

`JornadaStatus` **no se toca**. Los 4 estados siguen siendo
`draft`, `active`, `closed`, `completed`.

### 1.2. `JornadaComentario` — sin cambio de schema

Partido 15 cabe en `UNIQUE(jornada_id, partido_orden)` (rango 1..15). Solo
hay que quitar el filtro 1..14 del ingest.

### 1.3. `JornadaEscrutinio.payload` — sin cambio de schema

El `payload` JSONB admite un nuevo bloque `estimaciones: list[PartidoEstimacion]`.
No requiere migración.

## 2. Ciclo de vida

### 2.1. Estados y transiciones

```text
draft     → active     : manager fija fechas y todas las Composicion tienen kickoff_at
active    → closed     : now >= voting_closes_at   (se evalúa al leer; no requiere job)
closed    → completed  : jornada_escrutinio persistido   (evento de Losilla, ya existe)
```

### 2.2. `effective_phase` calculado

`status` se mantiene estable; el cliente recibe un campo `effective_phase`
derivado cruzando `status` con la hora actual:

| `status` | `now < sale_opens_at` | `sale_opens_at ≤ now < voting_closes_at` | `now ≥ voting_closes_at` |
|---|---|---|---|
| `draft` (sin fechas) | "en preparación" | — | — |
| `active` | "lista, abre pronto" | **"in_window"** | — |
| `closed` | — | — | "escrutinio_pendiente" |
| `completed` | — | — | "escrutinio_disponible" |

La columna `status` solo se escribe en eventos (manager, Losilla). No se
introduce un cron de transición de status; la lectura calcula `effective_phase`.

## 3. Gestor de fechas

### 3.1. Servicio nuevo: `app/services/jornada_manager.py`

```python
class JornadaManager:
    async def configure_dates(
        self, jornada_id: int, *, kickoff_per_partido: list[datetime]
    ) -> Jornada: ...

    async def recompute_window(self, jornada_id: int) -> Jornada: ...
```

Reglas **duras** (no se pueden overridear en MVP):

- `voting_closes_at = kickoff_last_match_at - 1h`.
- `escrutinio_expected_at = kickoff_last_match_at + 2.5h`.
- `sale_opens_at` se setea explícitamente al configurar; queda como valor
  persistido (es decisión editorial del admin: cuándo abre la venta).

`recompute_window` se dispara cuando cambian los kickoffs (Quiniwin ingest
detecta un cambio y lo llama; admin usa el endpoint de la sección 3.2 para
corregir un kickoff puntual). El único contrato mutable es el kickoff por
partido; la ventana se recalcula sola.

### 3.2. Endpoint admin: `POST /api/v1/admin/jornadas/{id}/dates`

Recibe los 15 kickoffs por partido (o simplificado: `kickoff_first_match_at` +
`kickoff_last_match_at` si los intermedios no importan), los persiste en
`Composicion.kickoff_at`, llama al manager. Auth: admin only.

## 4. Validación de apuestas

### 4.1. Helper compartido

`assert_jornada_open(session, jornada, *, for_bets=True)` en
`app/services/jornada_manager.py`. Lanza `HTTPException(409, ...)` con
detalle estructurado y headers de fecha.

### 4.2. Endpoints a actualizar

- `POST /groups/{gid}/porras` (create_porra) — bloquear si no está "in_window".
- `POST /groups/{gid}/porras/{pid}/picks` (submit_picks) — ya tiene 3 guards;
  los consolidamos en el helper.
- `PATCH /groups/{gid}/porras/{pid}` (patch_porra) — bloquear si la jornada
  está "closed" o "completed".
- `PUT /groups/{gid}/porras/{pid}/boleto` (put_boleto) — bloquear si
  "completed".

`POST /porras/{pid}/propuesta` es solo lectura, no bloquea.

### 4.3. Respuesta de error

```json
{
  "detail": "voting_window_closed",
  "sale_opens_at": "2026-10-08T19:00:00Z",
  "voting_closes_at": "2026-10-11T14:00:00Z",
  "now": "2026-10-11T15:30:00Z",
  "effective_phase": "escrutinio_pendiente"
}
```

## 5. Team matcher

### 5.1. Tres problemas concretos

1. **FC Andorra (club, Segunda) vs Andorra (selección nacional)** — el matcher
   crea stubs en lugar de matchear el row del catalog.
2. **Mezcla masculino/femenino** — los stubs siempre crean con `gender='men'`
   y `competition_id='unknown'`. Si cae un "Athletic Club (F)" sin match, queda
   como hombre.
3. **Búsqueda ciega** — antes de crear un stub, no consulta
   `data/team-catalog.json` (el seed que ya usa `load_team_catalog.py`).

### 5.2. Cambios

1. **Antes del stub, buscar en el catalog seed.** Si hay match por
   `(normalized_name, gender)`, usar ese `entity_id` y `competition_id`. Esto
   resuelve Andorra y el gender mix sin heurísticas mágicas.
2. **Nuevo flag `gender_known: bool` en `TeamIdentityMap`** (default
   `True`). Para los stubs sin marker se persiste `gender='men'` (mismo
   comportamiento que ya tenían) **pero `gender_known=False`**, para que la
   UI renderice un "?" o un fallback sin asumir certeza. Los stubs con marker
   femenino/masculino y los rows seedeados del catálogo van con
   `gender_known=True`.
4. Si el nombre matchea un equipo nacional masculino (e.g. "España") y la
   competición es UEFA, OK — el matcher actual ya tiene `strip_masculine_marker`
   que maneja "(m)".

### 5.3. Tests nuevos

- `test_find_or_create_uses_catalog_when_present` — "Athletic Club" matchea el
  row seedeado de LaLiga masculina.
- `test_find_or_create_stub_gender_known_false_when_no_marker` — sin marker ni
  match, persiste `gender='men'` y `gender_known=False`.
- `test_find_or_create_stub_gender_known_true_with_female_marker` — con marker
  femenino, `gender='women'` y `gender_known=True`.
- `test_find_or_create_resolves_fc_andorra_via_catalog` — "FC Andorra" matchea
  el row `club-men-fc-andorra`.
- `test_find_or_create_keeps_female_marker` — "Athletic Club (F)" no se
  confunde con el Athletic masculino.

## 6. Losilla

### 6.1. Comentario del Pleno al 15

Verificado en las 6 fixtures de Losilla (j1, j4, j8, j9, j11, j70): el
markup HTML solo trae 14 marcadores `PARTIDO N:` (1..14). El partido 15 sí
existe en el estado SSR pero no tiene editorial. Por lo tanto:

- Partidos 1..14: comentario editorial desde el HTML.
- Partido 15: **comentario sintético** derivado de los campos del SSR
  `porc_15L_0/1/2/M` y `porc_15V_0/1/2/M`. Formato propuesto:
  `"Local: 0=6%, 1=16%, 2=23%, M=55% · Visita: 0=51%, 1=34%, 2=12%, M=3%"`.

El parser genera este string al ingestar. El cliente puede consumirlo
directamente o usar el `PartidoEstimacion` estructurado (sección 6.2).

### 6.2. Porcentajes embebidos

Hoy: los porcentajes por partido viven en `RecaudacionBoteLive` (SSR vivo)
y **no se persisten**. El `JornadaEscrutinio.payload` solo guarda el
`escrutinio_oficial` final.

**Cambio:** extender `jornada_escrutinio.payload` con
`estimaciones: list[PartidoEstimacion]`. La shape cubre dos casos:

- **Partidos 1..14**: campos 1X2 (`uno_porcentaje`, `x_porcentaje`,
  `dos_porcentaje`).
- **Partido 15 (Pleno al 15)**: campos Pleno (`pleno_local`,
  `pleno_visitante`) con splits 0/1/2/M para cada equipo.

```python
class PartidoEstimacion(BaseModel):
    partido_orden: int = Field(..., ge=1, le=15)
    # Partidos 1..14 (1X2)
    uno_porcentaje: float | None = None
    x_porcentaje: float | None = None
    dos_porcentaje: float | None = None
    # Partido 15 (Pleno al 15): 0/1/2/M para cada equipo
    pleno_local: dict[str, float] | None = None
    pleno_visitante: dict[str, float] | None = None
    fuente: str = "losilla_live"
    fetched_at: datetime
```

En `losilla_ingest.run_ingest`: capturar del bloque SSR y mergear en el JSONB.
El cliente los recibe embebidos en la misma respuesta que la composición, no
en una segunda llamada.

### 6.3. Tests nuevos

- `test_ingest_persists_15_comentarios` — 15 filas esperadas, partido 15 con
  comentario sintético derivado de `porc_15L_*` / `porc_15V_*`.
- `test_ingest_persists_estimaciones_for_each_partido` — 15 estimaciones cuando
  la fixture las trae (fixture `j11_open`).

### 6.4. Cron diario 23:00 — poll de escrutinio reciente

Para evitar que las jornadas que cierran entre martes y domingo queden
`closed` hasta el lunes siguiente, se agrega un nuevo job diario a las 23:00
Europe/Madrid:

- **Job**: `losilla_poll_recent_closed_job` en `app/scheduler/jobs.py`.
- **Trigger**: `cron(hour=23, minute=0, timezone=Europe/Madrid)`.
- **Filtro**: jornadas de la temporada activa donde
  `kickoff_last_match_at + 3h < now AND jornada_escrutinio IS NULL`.
- **Comportamiento**: llama a `losilla_run_ingest` por cada candidato (igual
  que el job semanal). Idempotente y self-healing.

El job semanal de los lunes sigue existiendo como backstop para casos
extremos (downtime del upstream, retry tras error transitorio).

**Settings nuevos en `SchedulerSettings`:**

- `scheduler_losilla_daily_poll_hour: int = 23`
- `scheduler_losilla_daily_poll_minute: int = 0`
- `scheduler_losilla_daily_poll_enabled: bool = True` (kill switch)

Registro en `SchedulerRunner.add_jobs` con `coalesce=True`,
`max_instances=1`, `misfire_grace_time=3600` (mismo patrón que los otros).

## 7. Contrato API (cliente)

### 7.1. `GET /api/v1/jornadas` (lista)

```json
{
  "id": 123, "temporada": 2026, "temporada_str": "26/27", "numero": 8,
  "status": "active",
  "effective_phase": "in_window",
  "sale_opens_at": "2026-10-08T19:00:00Z",
  "voting_closes_at": "2026-10-11T14:00:00Z",
  "kickoff_first_match_at": "2026-10-11T15:00:00Z",
  "kickoff_last_match_at": "2026-10-11T22:00:00Z",
  "escrutinio_expected_at": "2026-10-12T00:30:00Z",
  "now": "2026-10-10T12:00:00Z",
  "fetched_at": "..."
}
```

### 7.2. `GET /api/v1/jornadas/{id}` (detalle)

Lo anterior + `partidos: ComposicionOut[]` + `estimaciones: PartidoEstimacion[]`
(nuevo, embebido).

`ComposicionOut.local_team` / `visitante_team` (de tipo `TeamSummary`) suman
`gender_known: bool` para que el cliente decida si mostrar "?" cuando es
`False`.

### 7.3. `GET /api/v1/losilla/jornada/{n}/comentarios`

Devolver los 15 comentarios, incluido el 15 con string vacío si no hay
editorial.

## 8. Plan de implementación (slices verificables)

| # | Slice | Verificación |
|---|---|---|
| 1 | Migración Alembic: `kickoff_last_match_at` y `escrutinio_expected_at` en `Jornada` | `alembic upgrade head` + `pytest tests/test_jornadas.py` |
| 2 | `JornadaManager` con tests unitarios (puro) | `pytest tests/test_jornada_manager.py` |
| 3 | Endpoint admin `POST /admin/jornadas/{id}/dates` | Test E2E con cliente admin |
| 4 | `assert_jornada_open` y aplicación en los 4 endpoints de mutación | Tests cubriendo: en ventana, cerrada 1h antes, completed |
| 5 | Team matcher: catalog lookup, `gender_known` flag en stubs | `pytest tests/test_team_matcher.py` con casos nuevos |
| 6 | Losilla: persistir partido 15 (string vacío si no hay) | `pytest tests/test_losilla_persistence.py` con 15 filas esperadas |
| 7 | Losilla: persistir estimaciones en `payload.estimaciones` | Test parsea fixture `j11_open` y verifica 15 estimaciones |
| 8 | Exponer `effective_phase`, `kickoff_last_match_at`, `escrutinio_expected_at`, `estimaciones` en endpoints | Tests de schema en `tests/test_jornadas.py` |
| 9 | Cron diario 23:00 (`losilla_poll_recent_closed_job` + `SchedulerSettings` + registro en `SchedulerRunner`) | `pytest tests/test_scheduler.py` con `kickoff_last_match_at + 3h < now` |

## 9. Decisiones de revisión (changelog de la sesión)

Todas las preguntas abiertas se resolvieron durante la revisión del
2026-10-06. Cambios sobre el diseño original:

1. **Pleno al 15**: comentario sintético generado a partir de
   `porc_15L_0/1/2/M` y `porc_15V_0/1/2/M` del SSR; partido 15 se persiste
   siempre con ese texto.
2. **`PartidoEstimacion`**: campos separados para 1X2 (1..14) y Pleno al 15
   (splits 0/1/2/M para local y visitante).
3. **`voting_closes_at` y `escrutinio_expected_at`**: derivadas duras. No
   hay `manual_override`. La única entrada mutable es el kickoff por partido.
4. **`gender` en stubs**: se mantiene `'men'` y se agrega `gender_known: bool`
   para distinguir certeza. Sin breaking change en el cliente.
5. **Cron diario 23:00**: nuevo job `losilla_poll_recent_closed_job` que
   procesa las jornadas recién cerradas (`kickoff_last_match_at + 3h < now`).
6. **Losilla publica 14 editoriales + 1 set de porcentajes Pleno al 15**:
   refuta el supuesto original; partido 15 no tiene editorial upstream.
