# Handoff — próxima sesión

> Lista de fixes para el backend. NO trabajar en esta sesión — es para una futura.

**Contexto**: usuario está armando el cliente Next.js aparte. Estos issues se descubrieron durante la integración con la API real.

---

## MVP-05 — porra model (replaces MVP-04) ✅ IMPLEMENTADO

**Fecha**: 2026-10-05
**Spec**: `la_goyniela_client/docs/project/features/porra-model.md`

### Breaking change summary

The MVP-04 per-jornada `quini_config` JSONB column is GONE. Every peña now has its own porra for `(group, jornada)` with its own format (D, T) and budget. Picks are scoped to a porra (not a jornada). The Pleno al 15 is now `{local, visitante}` (instead of the old `pleno_signs: {M0, M1, M2}` dict).

| Before (MVP-04) | After (MVP-05) |
|---|---|
| `Jornada.quini_config` JSONB | Dropped — replaced by per-porra `Porra.budget_eur` + `Porra.dobles` + `Porra.triples` |
| `Pick.pleno_signs: {M0, M1, M2}` | `Pick.pleno_local` + `Pick.pleno_visitante` (∈ `{"0","1","2","M"}`) |
| `POST /groups/{gid}/jornadas/{jid}/picks` | `POST /groups/{gid}/porras/{pid}/picks` |
| `POST /groups/{gid}/jornadas/{jid}/open` | `POST /groups/{gid}/porras` (create the porra) |
| `POST /groups/{gid}/jornadas/{jid}/optimize` | `GET /groups/{gid}/porras/porra-formats?jornada_id=&budget_eur=` |
| `POST /groups/{gid}/jornadas/{jid}/cost` | Client computes from `GET /porra-formats` (no separate endpoint) |
| `POST /groups/{gid}/jornadas/{jid}/consensus` | `POST /groups/{gid}/porras/{pid}/propuesta` (computed media) + `PUT /groups/{gid}/porras/{pid}/boleto` (save final boleto) |
| Pick shape governed by `quini_config.matches[partido]` | Pick shape is FREE-FORM — a member can vote 1, 2, or 3 signs on any partido regardless of the porra's D/T format. The format only governs the FINAL BOLETO the admin saves. |
| Column cost formula: `simples + 2·dobles + 3·triples` (WRONG) | `column_cost(D, T) = 2^D × 3^T` (per Quiniela real) |

### Migration

`db/migrations/versions/c1d2e3f4a5b6_porra_model_replaces_mvp04.py` — drops `jornada.quini_config`, drops `pick.pleno_signs`, adds `pick.pleno_local` + `pick.pleno_visitante`, adds `pick.porra_id` FK, replaces the XOR constraint with a new shape that splits by `partido_orden`, creates the `porra` table with all CHECK constraints. Destructive — dev data is wiped.

### Schema & endpoints

- New Pydantic DTOs in `app/schemas/porra.py` (PorraCreateIn / PorraPatchIn / PorraOut / PorraDetailOut / PorraFormatOut / PickPartidoIn / PickPlenoIn / PicksSubmitIn / PicksOut / PropuestaOut / BoletoIn / BoletoOut / ...).
- New router in `app/api/v1/porras.py` with 11 endpoints under `/groups/{gid}/porras/...`.
- Media algorithm (the "media de la peña") lives in `app/services/propuesta.py` as pure functions (testable without a DB).
- The old `app/scheduler/quini_optimizer.py` and `app/schemas/quini.py` are DELETED.

### For the client (Next.js)

The client integration is in the roadmap for the client team (see `la_goyniela_client/docs/project/features/porra-model.md` §"Plan del cliente"). Until the client is updated, the picks / consensus / quini modes endpoints are incompatible.

---

## MVP-04 — modos de quiniela (dobles / triples) ✅ IMPLEMENTADO (DEPRECATED)

**Fecha**: 2026-10-05
**PR**: backend-only, sin cambios en el cliente hasta que el dev del cliente lo integre.

### Breaking change: pick shape `sign` → `signs` (list)

**ANTES** (MVP-02):
```json
{"partido_orden": 1, "sign": "1"}
```

**AHORA** (MVP-04):
```json
{"partido_orden": 1, "signs": ["1"]}
{"partido_orden": 5, "signs": ["1", "X"]}   // doble
{"partido_orden": 3, "signs": ["1", "X", "2"]}  // triple
```

El campo `sign` (string) se renombró a `signs` (list de 1-3 elementos). El cliente debe actualizar sus DTOs y formularios. Un cliente viejo que mande `sign` recibe:
- `422` (Pydantic `extra="forbid"`)
- El detail del error menciona que el campo se renombró (el validator de `PickIn` para partidos 1-14 dice explícitamente `MVP-04: field 'sign' was renamed to 'signs', now a list` cuando `signs` es None)

### Cambios en DB

- `pick.sign VARCHAR(2)` → `pick.signs JSONB` (lista de 1-3 strings)
- `pick.ck_pick_sign_xor_pleno` → `ck_pick_signs_xor_pleno` (XOR entre `signs` y `pleno_signs`, igual que antes)
- `pick.ck_pick_sign_valid` → drop (la validación de la lista de signos ahora es Pydantic + endpoint, no DB)
- Nueva columna: `jornada.quini_config JSONB NULLABLE` — el config per-jornada. NULL = directa simple (legacy behavior).

Migración Alembic: `db/migrations/versions/b2c3d4e5f6a1_mvp04_quini_modes.py`. Hace backfill automático: las filas existentes con `sign='1'` se convierten en `signs=['1']` antes de dropear la columna vieja.

### Nuevos endpoints (3)

Todos bajo `/groups/{gid}/jornadas/{jid}/`:

- **POST `.../open`** (admin only) — escribe el config per-jornada. Idempotente mientras no haya picks. Una vez que algún miembro votó, el config se FREEZE y un segundo /open devuelve `409`.
- **POST `.../optimize`** (cualquier miembro) — dado `budget_eur` + `criterion` (max_columns o max_matches), devuelve el config recomendado. NO persiste.
- **POST `.../cost`** (cualquier miembro) — calcula el costo de un config candidate. NO persiste. Usado por la UI para mostrar "current cost: €X" mientras el admin edita.

### Consensus: top N + deterministic tie-break

**MVP-02** (antiguo): empate → multi-marca todos los empatados.
**MVP-04** (nuevo): para un partido con `kind=doble`, el consensus retorna top 2 signos por voto (con tie-break `1 > X > 2` determinístico). Para `triple`, top 3. Para `simple`, top 1.

Ejemplos:
- Doble con votos 4×"1", 3×"X", 2×"2" → `consensus.signs = ["1", "X"]` (no 4+3+2=top 2; determinístico).
- Triple con votos 5×"1", 4×"X", 3×"2" → `["1", "X", "2"]`.
- Simple con empate 3-3-3 → `["1"]` (top 1, "1" gana el desempate).

### El optimizer NO es "best bet"

`app/scheduler/quini_optimizer.py` es un helper de FEASIBILITY, no de optimalidad. No usa odds reales. La estrategia es: triples primero (más column-efficient), luego dobles, asignando por `partido_orden` ascendente. Misma estrategia para `max_columns` y `max_matches` en esta v1 — difieren solo en el caso edge de budget pequeño.

### Docs actualizados

- `docs/API.md` §7.5 (Jornadas — sección nueva de modos), §8.1 (Pick shape con `signs`), §8.7 (Consensus con top N).
- `docs/CLIENT.md` §6.1 (pick shape con `signs` + lista), §11.5 (nota sobre rename).
- `docs/FEATURES.md` §3.6.b (sección nueva "Modos de quiniela"), §3.6 (shape actualizado).

### Próximo paso para el cliente

Cuando integres MVP-04 en el cliente Next.js:

```typescript
// ANTES (MVP-02)
type PickSimple = { partido_orden: number; sign: '1' | 'X' | '2' };

// AHORA (MVP-04)
type PickSimple = { partido_orden: number; signs: ['1'] };
type PickDoble = { partido_orden: number; signs: ['1', 'X'] };  // no duplicates
type PickTriple = { partido_orden: number; signs: ['1', 'X', '2'] };
```

El form debe:
1. Para partidos default (no doble/triple en el config): 3 radio buttons (1/X/2) → `signs: [<valor>]`.
2. Para dobles: 3 checkboxes (1/X/2) → `signs: [seleccionados, max 2]`.
3. Para triples: 3 checkboxes → `signs: [seleccionados, 1-3]`.

Antes de cargar el form, llamar `GET /groups/{gid}/jornadas/{jid}/open` (no existe como GET — usar el `quini_config` que viene en `GET /jornadas/{id}` cuando lo agreguemos). Por ahora el cliente debe traer el config via... ah, **no expusimos el config en GET /jornadas/{id} todavía**. Eso queda como TODO para MVP-04 v2.

---

## 🔴 Bloqueante

### 1. Ruta de envío de votos está mal
- **Archivo**: `app/api/v1/picks.py` línea 205
- **Bug**: `@router.post("")` debe ser `@router.post("/picks")`
- **Síntoma**: hoy la ruta efectiva es `POST /groups/{g}/jornadas/{j}` (sin sufijo), pero el `docs/API.md` y el cliente esperan `/picks`
- **Tests que NO lo detectan**: `tests/test_picks.py:161` (usa la ruta vieja — está pasando por accidente)
- **Fix**: cambiar handler + actualizar el test

## 🟠 Reglas de negocio

### 2. Votos con jornada fuera de plazo
- **Archivo**: `app/api/v1/picks.py` (mismo handler del fix #1)
- **Bug**: `POST /picks` solo mira `voting_closes_at`. Una jornada en `draft` o `completed` sin fecha de cierre acepta votos incorrectamente
- **Fix**: agregar `if jornada.status != JornadaStatus.active: raise HTTPException(403, ...)` después del check de `voting_closes_at`

### 3. Validar variantes del pleno en el schema
- **Archivo**: `app/schemas/pick.py` `PickIn._check_sign_or_pleno`
- **Variantes válidas**:
  - M0: `["0-0"]`
  - M1: `["0-1", "0-2", "1-0", "1-1", "1-2"]`
  - M2: `["2-0", "2-1", "2-2", "3-0", "3-1", "3-2", "M"]`
- **Exigir**: al menos una variante sumada entre M0/M1/M2 (no pleno 100% vacío, que Quiniela real no acepta)
- **Hoy**: acepta cualquier string y un pleno totalmente vacío

### 4. Impedir self-modify del admin
- **Archivo**: `app/api/v1/groups.py`
- **Endpoints a proteger**:
  - `promote_member_to_admin`: si `target == caller` → 400 "cannot promote yourself"
  - `demote_admin_to_member`: si `target == caller` → 400 "cannot demote yourself, use /leave"
  - `remove_member_from_group`: si `target == caller` → 400 "cannot remove yourself, use /leave"
- **Nota**: el creator inmutable ya está manejado. Esto es para admins creados por promote.
- (Para salir del grupo ya existe `POST /groups/{id}/leave`.)

## 🟡 Contrato

### 5. /groups/join: distinguir "grupo lleno" vs "ya sos miembro"
- **Archivo**: `app/api/v1/groups.py` `join_group`
- **Hoy**: ambos devuelven 409 con texto distinto (no machine-parseable)
- **Fix sugerido**:
  - grupo lleno → 400 con `{"detail": {"code": "group_full"}}` o status code distinto
  - ya sos miembro → 409 con `{"detail": {"code": "already_member"}}`

### 6. UserOut debe incluir `created_at`
- **Archivo**: `app/schemas/auth.py` `UserOut`
- **Hoy**: no tiene `created_at` aunque el `docs/API.md` lo lista
- **Fix**: agregar el campo. `is_active` también, si lo querés mantener.

### 7. No meter texto de excepción en `detail`
- **Archivos**: `app/api/v1/auth.py`
- **Endpoints afectados**:
  - `/auth/google` y `/auth/x` incluyen `f"{type(exc).__name__}: {exc}"` en detail
  - `register` 409 repite el email en detail
- **Fix**: mensajes fijos user-friendly, el stack trace va al log interno

### 8. /auth/x: 401 cuando debería ser 502
- **Archivo**: `app/api/v1/auth.py`
- **Bug**: `/auth/x` responde 401 cuando la falla es upstream de X (red, timeout, etc). Debería ser **502 Bad Gateway** para distinguir "credenciales inválidas de X" vs "no pudimos contactar a X".

---

## Estado actual del proyecto (referencia)

- API corriendo en :8000
- DB en docker postgres:5433
- 12 jornadas ingestadas (J1-J12), J11 está `active` con `voting_closes_at = hoy + 7 días`
- 423 logos subidos a R2, public dev URL: `https://pub-91e15d2d541144f3918344f5e6045495.r2.dev/`
- `STORAGE_S3_PUBLIC_BASE_URL` configurado en `.env`
- **375/375 tests pasando**
- ⚠️ Creds de R2 API Token quedaron expuestas en el chat del usuario — rotar ASAP

---

## N-column boleto support (oct 2026) ✅ IMPLEMENTADO

**Fecha**: 2026-10-12
**Migration**: `9c0d1e2f3a4b_boleto_columns_and_reducciones.py`
**PR**: backend-only.

### Backend implementation status

| Modo     | Backend | Comentario                                                                              |
|----------|---------|------------------------------------------------------------------------------------------|
| `multiple`  | ✅ ready | 1 column, D/T matches `porra.dobles`/`triples`. Legacy `matches: [14]` alias still works. |
| `sencilla`  | ✅ ready | N columns of simples, every match has 1 sign. `columnas` carries N.                       |
| `reducida`  | ✅ ready | 1 column, D/T counts match the `R1..R6` spec from `app.services.reducciones`.            |
| `intuicion` | ✅ ready | N columns where the first `fijos` partidos share the same sign across all columns.       |

### Storage shape (N-column phase)

- `porra.boleto_columns JSONB` — canonical: `[{"matches": [{partido_orden, signs}, ...]}, ...]`.
- `porra.boleto_matches JSONB` — legacy mirror of the FIRST column (for the
  `boleto_matches IS NULL OR jsonb_array_length(boleto_matches) = 14` DB
  CHECK and pre-N-column consumers).
- `porra.reduccion VARCHAR(4) NULLABLE` — only set when `modo='reducida'`.

### Reducciones table (R1..R6)

`app/services/reducciones.py` — `REDUCCIONES` dict with the 6 official
reductions. Each spec carries `(dobles, triples, apuestas)`. The
`apuestas` count is what the column-cost math uses (NOT `2^D × 3^T`).

| Code | D  | T  | Apuestas | Cost @ 0.75€ |
|------|----|----|----------|---------------|
| R1   | 0  | 4  | 9        | 6.75€         |
| R2   | 7  | 0  | 16       | 12.00€        |
| R3   | 3  | 3  | 24       | 18.00€        |
| R4   | 6  | 2  | 64       | 48.00€        |
| R5   | 0  | 8  | 81       | 60.75€        |
| R6   | 11 | 0  | 132      | 99.00€        |

### PDF (multi-column)

`GET /boleto.pdf` renders N columns side-by-side (3 per page-row). The
header labels each column ("Columna 1", "Columna 2", ...); the body
shows the same 14 rows per column with the column-specific sign. The
pleno + cost footer are rendered once at the end.

### Tests

- `tests/test_boleto_columns.py` — 9 new tests covering all 4 modes +
  the multi-column PDF.
- `tests/test_porra.py` — updated `test_put_boleto_happy_path` to read
  `body["columnas"]` (legacy `matches` response field replaced).
- `tests/test_boleto_pdf.py` — updated to pass `columnas=[matches]` to
  `render_boleto_pdf` (renderer signature changed from `matches=`).

### Known follow-ups (deferred)

- The DB-level CHECK on `boleto_matches` is kept at 14 rows; a
  `boleto_columns` equivalent (N*14 rows where N is the columns count
  per mode) could be added for defense in depth, but the endpoint
  layer is the source of truth.
- The `boleto-modes-contract.md` doc in the client side still
  describes the legacy `matches: [14]` shape; the next client sprint
  should swap to `columnas: [...]`.

## Orden de ejecución sugerido

1. Fix #1 (ruta) — sin esto, el cliente no puede enviar votos
2. Fix #2 (status check) — sin esto, votos fuera de plazo pasan
3. Fix #3 (validar variantes pleno) — sin esto, datos inválidos en DB
4. Fix #4 (self-modify) — sin esto, admin puede degradarse a sí mismo
5. Fixes #5-8 (contrato) — pueden ir en cualquier orden, son pulido
