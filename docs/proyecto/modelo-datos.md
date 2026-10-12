# La Goyniela — Modelo de datos (plano v1)

**Fecha**: 27 de septiembre de 2026
**Estado**: plano (sin implementación todavía)
**Postgres**: 16+ (recomendado)

Este documento describe las tablas, columnas, FK, índices y reglas del modelo
de datos de La Goyniela. Sale de las 7 decisiones críticas acordadas:

| # | Decisión |
|---|---|
| 1 | Postgres + SQLAlchemy + Alembic + psycopg3 |
| 2 | Tabla única `estimacion_snapshot` con UPSERT |
| 3 | UPSERT de `composicion` (se pisa si cambia) |
| 4 | Tabla `team_identity_map` para alias |
| 5 | Postgres ENUM `competition_type` |
| 6 | Trazabilidad completa por fila |
| 7 | `kickoff_at` nullable |

## Visión general

Hay **dos planos**:

1. **Plano de mercado** (esta versión): las tablas que sostienen los datos
   de las 5 fuentes (composición, estimaciones, histórico, H2H, identidad de
   equipos). Es lo que vamos a escribir primero.
2. **Plano de producto** (futuro): usuarios, grupos, picks, tickets. Esa es
   la camada 2. Las decisiones de abajo sientan las bases para que esa camada
   se pueda agregar **migrando sin romper el plano de mercado**.

Las tablas del plano de mercado se dividen en:

- **Catálogo**: `source` (las fuentes), `team_identity_map` (alias),
  `team_id_api_football` (cache de IDs de API-Football).
- **Jornada**: `jornada` (identidad temporada+número), `composicion`
  (los 15 partidos con su posición dentro de la jornada).
- **Estimaciones**: `estimacion_snapshot` (1X2 clubes), `pleno_snapshot`
  (marginales marcador Pleno al 15).
- **Contexto histórico**: `partido_historico` (Football-Data), `partido_h2h`
  (API-Football).
- **Auditoría**: `fetch_session` (cada batch del scheduler), y los 4 campos
  `source_id + captured_at + schema_version + fetch_session_id` en cada tabla
  de estimaciones.

## Diagrama (texto)

```
+---------------------+
|       source        |  -- catálogo
+---------------------+
| id PK               |
| code UNIQUE         |
| name                |
| kind (api/scrape/...)|
| base_url            |
+---------------------+

+----------------------------+     +------------------------------+
|     team_identity_map      |     |    team_id_api_football      |
+----------------------------+     +------------------------------+
| id PK                      |     | id PK                        |
| source          FK->source |     | source  FK->source           |
| raw_name                   |     | canonical_team_name UNIQUE   |
| canonical_team_name        |     | api_football_id             |
| competition (enum)         |     | season_seen (text)          |
| created_at                 |     | created_at                   |
+----------------------------+     +------------------------------+

              ▲                              ▲
              │  usa                         │  usa
              │                              │

+---------------------+        +-----------------------------+
|      jornada        | 1----* |        composicion          |
+---------------------+        +-----------------------------+
| id PK               |        | id PK                       |
| temporada (int)     |        | jornada_id FK               |
| numero (int)        |        | posicion (1..14 + 15=pleno) |
| temporada_str       |        | local_raw, visitante_raw     |
| UNIQUE(t, n)        |        | local_canonical, visitante_c |
+---------------------+        | competition_type (enum)     |
                               | kickoff_at TIMESTAMP NULL   |
                               | captured_at, schema_version|
                               | fetch_session_id FK NULL    |
                               | UNIQUE(jornada_id, posicion)|
                               +-----------------------------+

+---------------------------------+   +-------------------------------+
|     estimacion_snapshot         |   |        pleno_snapshot         |
+---------------------------------+   +-------------------------------+
| id PK                           |   | id PK                         |
| jornada_id  FK -> jornada      |   | jornada_id FK -> jornada      |
| partido_id  FK -> composicion   |   | source_id FK -> source        |
| source_id   FK -> source        |   | local_0, local_1, local_2,    |
| prob_1 (numeric 5,2)            |   |   local_M (numeric 5,2)       |
| prob_X (numeric 5,2)            |   | visitante_0, visitante_1,     |
| prob_2 (numeric 5,2)            |   |   visitante_2, visitante_M    |
| captured_at                     |   | captured_at, schema_version   |
| schema_version                  |   | fetch_session_id FK NULL      |
| fetch_session_id FK NULL        |   | UNIQUE(jornada_id, source_id) |
| UNIQUE(jornada_id, partido_id,  |   +-------------------------------+
|         source_id)              |
+---------------------------------+

+------------------+      +-------------------+
| partido_historico|      |   partido_h2h     |
+------------------+      +-------------------+
| id PK            |      | id PK             |
| jornada_id FK    |      | jornada_id FK     |
| partido_id FK    |      | partido_id FK     |
| team_side ('home'|      | source_id FK      |
|           visitor)|     | team1_canonical   |
| fecha            |      | team2_canonical   |
| opponent         |      | fixture_count     |
| goals_for        |      | full_response JSONB|
| goals_against    |      | captured_at       |
| outcome          |      | schema_version    |
| source_id FK     |      | fetch_session_id  |
| captured_at      |      | UNIQUE(jornada_id,|
| fetch_session_id |      |   partido_id,     |
+------+           |      |   team1_canonical,|
       |           |      |   team2_canonical,|
       |           |      |   source_id)      |
       v           |      +-------------------+
+------------------+
| partido_h2h      |  (mismo de arriba)
+------------------+

+------------------+
|  fetch_session   |
+------------------+
| id PK (uuid)     |
| started_at       |
| finished_at NULL |
| source_stats JSONB |
| notes TEXT       |
+------------------+
```

## Tablas y columnas

### ENUM (a nivel DB)

```sql
CREATE TYPE competition_type AS ENUM ('sp1', 'sp2', 'liga_f', 'uefa');
CREATE TYPE team_side        AS ENUM ('home', 'away');
CREATE TYPE source_kind      AS ENUM ('api', 'scrape', 'csv', 'fixture_xml');
```

### `source` — catálogo de fuentes

```sql
CREATE TABLE source (
  id          SERIAL PRIMARY KEY,
  code        TEXT NOT NULL UNIQUE,    -- 'losilla_real', 'losilla_lae',
                                        -- 'odds_api_uk', 'football_data',
                                        -- 'api_football_h2h', etc.
  name        TEXT NOT NULL,           -- "Losilla Probabilidad REAL"
  kind        source_kind NOT NULL,
  base_url    TEXT,
  rate_limit  TEXT,                     -- texto libre, ej "100 req/día"
  is_active   BOOLEAN NOT NULL DEFAULT TRUE,
  created_at  TIMESTAMP NOT NULL DEFAULT NOW(),
  updated_at  TIMESTAMP NOT NULL DEFAULT NOW()
);
```

### `team_identity_map` — alias map por fuente

```sql
CREATE TABLE team_identity_map (
  id                SERIAL PRIMARY KEY,
  source_id         INT NOT NULL REFERENCES source(id),
  raw_name          TEXT NOT NULL,
  canonical_team_name TEXT NOT NULL,
  competition       competition_type, -- NULL si ambiguity (ej. "Tenerife" masc+fem)
  notes             TEXT,
  created_at        TIMESTAMP NOT NULL DEFAULT NOW(),
  UNIQUE (source_id, raw_name)
);
CREATE INDEX idx_team_identity_canonical
  ON team_identity_map (canonical_team_name);
```

Seed inicial desde `scripts/mock_quiniela.py` (SP_CANONICAL, UEFA_CANONICAL).

### `team_id_api_football` — cache de IDs API-Football

```sql
CREATE TABLE team_id_api_football (
  id                SERIAL PRIMARY KEY,
  source_id         INT NOT NULL REFERENCES source(id),
  canonical_team_name TEXT NOT NULL,
  api_football_id   INT NOT NULL,
  season_seen       INT,                -- ej. 2024 (última vista)
  created_at        TIMESTAMP NOT NULL DEFAULT NOW(),
  UNIQUE (source_id, canonical_team_name, season_seen)
);
```

El contenido actual `scripts/team_ids_api_football.py` se migra acá en
producción. Una vez cargado, se puede borrar el .py o dejarlo como generador.

### `jornada` — identidad temporada + número

```sql
CREATE TABLE jornada (
  id            SERIAL PRIMARY KEY,
  temporada     INT NOT NULL,         -- 2027 representa 2026/27
  numero        INT NOT NULL,         -- 1..38 + jornadas playoff
  temporada_str TEXT NOT NULL,        -- "2026/27"
  created_at    TIMESTAMP NOT NULL DEFAULT NOW(),
  UNIQUE (temporada, numero)
);
CREATE INDEX idx_jornada_temporada_numero ON jornada(temporada, numero);
```

### `composicion` — 15 partidos por jornada (UPSERT si cambia)

```sql
CREATE TABLE composicion (
  id                  SERIAL PRIMARY KEY,
  jornada_id          INT NOT NULL REFERENCES jornada(id) ON DELETE CASCADE,
  posicion            SMALLINT NOT NULL CHECK (posicion BETWEEN 1 AND 15),
  local_raw           TEXT NOT NULL,
  visitante_raw       TEXT NOT NULL,
  local_canonical     TEXT,            -- puede ser NULL si alias map no lo resuelve
  visitante_canonical TEXT,
  competition_type    competition_type NOT NULL,
  kickoff_at          TIMESTAMP,       -- nullable; de FD fixtures / Odds / API-Football
  competition_code    TEXT,            -- ej. "LaLiga", "Segunda", "Liga F", "UEFA Nations League"
  captured_at         TIMESTAMP NOT NULL DEFAULT NOW(),
  schema_version      TEXT,
  fetch_session_id    UUID,            -- nullable hasta que se enchufe el scheduler
  UNIQUE (jornada_id, posicion)
);
CREATE INDEX idx_comp_jornada ON composicion(jornada_id);
CREATE INDEX idx_comp_canonical_local ON composicion(local_canonical);
CREATE INDEX idx_comp_canonical_visit ON composicion(visitante_canonical);
```

NOTA: `competition_code` (texto libre) complementa el ENUM `competition_type`
para reflejar el proveedor original (ej. "UEFA Nations League" vs "UEFA Women's
Championship" — ambos son "uefa" en el ENUM pero se diferencian aquí).
Evaluar si se mantiene en producción.

### `estimacion_snapshot` — 1X2 por partido + fuente (UPSERT)

```sql
CREATE TABLE estimacion_snapshot (
  id              SERIAL PRIMARY KEY,
  jornada_id      INT NOT NULL REFERENCES jornada(id) ON DELETE CASCADE,
  partido_id      INT NOT NULL REFERENCES composicion(id) ON DELETE CASCADE,
  source_id       INT NOT NULL REFERENCES source(id),
  prob_1          NUMERIC(5, 2) NOT NULL,
  prob_x          NUMERIC(5, 2) NOT NULL,
  prob_2          NUMERIC(5, 2) NOT NULL,
  -- Implied para cuotas (en cents o ratio). NULL para estimación LAE/REAL
  implied_1_raw   NUMERIC(8, 3),
  implied_x_raw   NUMERIC(8, 3),
  implied_2_raw   NUMERIC(8, 3),
  overround_pct   NUMERIC(5, 2),
  houses_count    SMALLINT,
  captured_at     TIMESTAMP NOT NULL DEFAULT NOW(),
  schema_version  TEXT,
  fetch_session_id UUID,
  UNIQUE (jornada_id, partido_id, source_id),
  CONSTRAINT prob_sum_1x2 CHECK (
    ABS((prob_1 + prob_x + prob_2) - 100.0) < 0.5
  )
);
CREATE INDEX idx_estim_jornada_source ON estimacion_snapshot(jornada_id, source_id);
CREATE INDEX idx_estim_captured ON estimacion_snapshot(captured_at);
```

CHECK constraint con tolerancia 0.5pp (más generoso que 0.021 del probe de
research porque acá viene de múltiples casas con redondeos diferentes).

### `pleno_snapshot` — marcador exacto para Pleno al 15 (UPSERT)

```sql
CREATE TABLE pleno_snapshot (
  id              SERIAL PRIMARY KEY,
  jornada_id      INT NOT NULL REFERENCES jornada(id) ON DELETE CASCADE,
  source_id       INT NOT NULL REFERENCES source(id),
  -- 4 marginales por lado × 4 categorías (0/1/2/M)
  local_0         NUMERIC(5, 2),
  local_1         NUMERIC(5, 2),
  local_2         NUMERIC(5, 2),
  local_M         NUMERIC(5, 2),
  visit_0         NUMERIC(5, 2),
  visit_1         NUMERIC(5, 2),
  visit_2         NUMERIC(5, 2),
  visit_M         NUMERIC(5, 2),
  captured_at     TIMESTAMP NOT NULL DEFAULT NOW(),
  schema_version  TEXT,
  fetch_session_id UUID,
  UNIQUE (jornada_id, source_id),
  CONSTRAINT pleno_4cats CHECK (
    local_0 IS NOT NULL AND local_1 IS NOT NULL
    AND local_2 IS NOT NULL AND local_M IS NOT NULL
    AND visit_0 IS NOT NULL AND visit_1 IS NOT NULL
    AND visit_2 IS NOT NULL AND visit_M IS NOT NULL
  )
);
CREATE INDEX idx_pleno_captured ON pleno_snapshot(captured_at);
```

NOTA: para Odds API Pleno, hoy no se guarda porque el mercado Correct Score
no está en plan free. Si en el futuro se habilita, agregar columnas
`cs_outcomes JSONB`.

### `partido_historico` — Football-Data últimos N por equipo

```sql
CREATE TABLE partido_historico (
  id                SERIAL PRIMARY KEY,
  jornada_id        INT NOT NULL REFERENCES jornada(id) ON DELETE CASCADE,
  partido_id        INT NOT NULL REFERENCES composicion(id) ON DELETE CASCADE,
  team_side         team_side NOT NULL,    -- ¿es histórico del local o del visitante?
  fecha             TIMESTAMP NOT NULL,
  home_team         TEXT NOT NULL,
  away_team         TEXT NOT NULL,
  goals_for         SMALLINT,
  goals_against     SMALLINT,
  outcome           CHAR(1) CHECK (outcome IN ('W', 'D', 'L')),
  division          TEXT,
  source_id         INT NOT NULL REFERENCES source(id),
  captured_at       TIMESTAMP NOT NULL DEFAULT NOW(),
  schema_version    TEXT,
  fetch_session_id  UUID,
  UNIQUE (jornada_id, partido_id, team_side, fecha, home_team, away_team)
);
CREATE INDEX idx_ph_jornada_partido ON partido_historico(jornada_id, partido_id);
CREATE INDEX idx_ph_fecha ON partido_historico(fecha DESC);
```

### `partido_h2h` — H2H entre dos equipos (cache de API-Football)

```sql
CREATE TABLE partido_h2h (
  id                SERIAL PRIMARY KEY,
  jornada_id        INT NOT NULL REFERENCES jornada(id) ON DELETE CASCADE,
  partido_id        INT NOT NULL REFERENCES composicion(id) ON DELETE CASCADE,
  team1_canonical   TEXT NOT NULL,
  team2_canonical   TEXT NOT NULL,
  fixture_count     INT NOT NULL,
  full_response     JSONB NOT NULL,        -- raw payload API-Football, normalizado por el adapter
  source_id         INT NOT NULL REFERENCES source(id),
  captured_at       TIMESTAMP NOT NULL DEFAULT NOW(),
  schema_version    TEXT,
  fetch_session_id  UUID,
  UNIQUE (jornada_id, partido_id, team1_canonical, team2_canonical, source_id)
);
CREATE INDEX idx_h2h_jornada_partido ON partido_h2h(jornada_id, partido_id);
```

JSONB permite reconstruir el histórico si hace falta para análisis o para
regenerar vistas si la API cambia. No es un detalle decorativo.

### `fetch_session` — cada batch de fetches del scheduler

```sql
CREATE TABLE fetch_session (
  id            UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  started_at    TIMESTAMP NOT NULL DEFAULT NOW(),
  finished_at   TIMESTAMP,
  source_stats  JSONB,           -- ej. {"losilla_real": "ok", "odds_api_uk": "missing_key", ...}
  total_rows    INT,
  notes         TEXT
);
CREATE INDEX idx_fs_started ON fetch_session(started_at DESC);
```

Esta tabla es **a posteriori**: cuando arranca un batch de fetches, el
scheduler genera un UUID y lo asigna a cada `INSERT/UPDATE`. Al final, ese
UUID se cierra con `finished_at` + stats.

## Tablas diferibles (no se crean en esta migración)

Estas **NO** entran en este plano. Se modelan más adelante como parte del
plano de producto:

- `user`, `group`, `group_membership`
- `propuesta` (per-participant picks)
- `ticket` (boleto final confirmado)
- `ticket_picks` (las 14 casillas × n marks del ticket)
- `audit_log` (acciones admin)
- `session` (auth tokens cuando llegue MVP-02)
- `notification` (cuando llegue el feature que ya está fuera del MVP)

Las decisiones de snapshot, trazabilidad y UPSERT ya están diseñadas para
que estas tablas se agreguen **sin romper** lo actual: todas se montan sobre
`jornada_id` + `composicion_id`, y agregan columnas propias (picks, votos,
cierre confirmado, etc.).

## Vistas SQL recomendadas (para endpoints)

Estas vistas no son obligatorias pero aceleran los endpoints más comunes:

```sql
-- Estimación consolidada (todas las fuentes) por partido
CREATE VIEW v_estimacion_partido AS
SELECT
  c.jornada_id,
  c.id AS partido_id,
  c.posicion,
  c.local_canonical,
  c.visitante_canonical,
  c.competition_type,
  c.kickoff_at,
  s.code AS source_code,
  e.prob_1, e.prob_x, e.prob_2,
  e.captured_at
FROM composicion c
LEFT JOIN estimacion_snapshot e
  ON e.partido_id = c.id
JOIN source s ON s.id = e.source_id;

-- Vista "lo que ve el grupo" (snapshot consumido por UI)
CREATE VIEW v_jornada_snapshot AS
SELECT
  j.temporada, j.numero, j.temporada_str,
  c.posicion, c.local_canonical, c.visitante_canonical,
  c.competition_type, c.kickoff_at,
  e.prob_1, e.prob_x, e.prob_2,
  e.captured_at, s.code AS source_code
FROM jornada j
JOIN composicion c ON c.jornada_id = j.id
LEFT JOIN estimacion_snapshot e ON e.partido_id = c.id
LEFT JOIN source s ON s.id = e.source_id;
```

## Reglas de integridad operacionales

Estas NO son constraints de DB porque requieren consultas y lógica:

1. **Una sola `composicion` por (jornada, posición)** — enforced por UNIQUE
   en la tabla `composicion`. Si cambia el local/visitante, **UPSERT pisa**.
   El partido_id es estable.

2. **`estimacion_snapshot` se atas a `(jornada_id, partido_id, source_id)`**
   — enforced por UNIQUE. UPSERT normal.

3. **`fetch_session_id` debe ser consistente** para todas las filas de un
   mismo batch. No enforced por DB (sería costoso). Validado por el
   scheduler: si falla, se hace rollback del batch completo.

4. **El scheduler congela la composición** después del primer fetch útil
   (no enforced por DB). Confiamos en que el scheduler no escribe cuando
   ya arrancó el primer partido de la jornada (decisión #2).

5. **`kickoff_at` puede ser NULL** (decisión #7). La UI lo trata como
   "fecha desconocida" y no lo usa para calcular cierre automáticamente.

6. **`competition_type` siempre NOT NULL**. Si el partido no tiene
   competition conocida, queda `'uefa'` por defecto con `competition_code`
   explicitando.

## Tamaño esperado (estimación rápida)

Asumiendo 40 jornadas/año, 15 partidos/jornada:

| Tabla | Rows/año | Bytes/row | Total |
|---|---:|---:|---:|
| jornada | 40 | 100 | ~4 KB |
| composicion | 600 | 300 | ~180 KB |
| estimacion_snapshot (5 fuentes × 15 partidos) | 12 000 | 250 | ~3 MB |
| pleno_snapshot (3 fuentes) | 120 | 400 | ~48 KB |
| partido_historico (5 últimos × 2 equipos × 600) | 6 000 | 200 | ~1.2 MB |
| partido_h2h (cuando aplica) | ~600 | 1 KB | ~600 KB |
| fetch_session (52 semanas × 2 fetches/día) | ~14 600 | 100 | ~1.5 MB |
| team_identity_map | ~150 | 200 | ~30 KB |
| team_id_api_football | 58 | 200 | ~12 KB |
| **TOTAL primer año** | | | **~6 MB** |

Total despreciable para Postgres. Ni siquiera vamos a llegar al millón de
filas en 5 años.

## Migraciones

- **Primera migración** (Alembic `0001_initial.py`): crea las 9 tablas del
  plano de mercado + los 3 ENUM + el seed de `source` con las 5 fuentes
  activas + el seed de `team_identity_map` desde el alias map actual
  (~150 aliases) + el seed de `team_id_api_football` desde el .py
  auto-generado.

- **Migraciones siguientes**: solo cuando cambien las tablas. El plano de
  producto (usuarios, grupos) entra como migración 0002, 0003, sin tocar
  las tablas de mercado.

## Decisiones de implementación diferidas (no en este plano)

- **Pool de conexiones**: `psycopg_pool` con `min_size=2, max_size=10` (a
  definir cuando codeemos, no en este plano).
- **Async vs sync driver**:倾向 asyncpg para FastAPI (recomendado), pero
  sin impacto en el modelo (mismas tablas).
- **Triggers**: ninguno ahora. Todos los checks son UNIQUE + CHECK +
  decoración de aplicación. Si aparecen patrones de auditoría frecuentes,
  triggers podrían ayudar.
- **Caché de lectura**: no. La aplicación FastAPI cachea el último
  `GET /jornadas/current` en memoria por 5-10 minutos como mucho. Para
  resto, queries directas a Postgres.
