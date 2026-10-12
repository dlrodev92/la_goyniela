# La Goyniela — API Reference

**Base URL (dev)**: `http://localhost:8000`
**Versión**: 0.6.x (votación MVP-02 + modos de quiniela MVP-04 + scheduler + Losilla persistence + porra MVP-05)
**OpenAPI interactivo**: `http://localhost:8000/docs` (Swagger UI) — siempre actualizado con el código
**OpenAPI JSON**: `http://localhost:8000/openapi.json`

---

## 1. Convenciones

### 1.1 Autenticación

Todos los endpoints excepto los marcados como **Público** requieren un Bearer JWT en el header `Authorization`:

```bash
curl -H "Authorization: Bearer eyJhbGc..." http://localhost:8000/auth/me
```

Los tokens se obtienen vía `POST /auth/register`, `POST /auth/login`, `POST /auth/google`, o `POST /auth/x`. Vencen en 24 horas (1440 minutos). No hay refresh tokens en MVP-02.

### 1.2 Formato de request

- JSON para `Content-Type: application/json`
- `multipart/form-data` para uploads de imagen (logo, avatar)
- URL params para path y query string

### 1.3 Formato de respuesta

Todas las respuestas exitosas son JSON. Los errores siguen este formato:

```json
{
  "detail": "human-readable error message"
}
```

### 1.4 Códigos de estado

| Código | Significado |
|---|---|
| `200` | OK (GET, PATCH) |
| `201` | Created (POST) |
| `204` | No content (DELETE exitoso) |
| `400` | Bad request (validación falla, jornada no existe) |
| `401` | No autenticado (falta token o token inválido) |
| `403` | No autorizado (token válido pero no tiene permisos) |
| `404` | No encontrado |
| `409` | Conflicto (email duplicado, login_code colisión, miembros no han votado en consensus) |
| `422` | Unprocessable (Pydantic validation) |
| `500` | Error interno |
| `502` | Upstream no disponible (Losilla, Quiniwin) |

### 1.5 Filtros y paginación

Los endpoints de listado (`GET /jornadas`, `GET /teams`) aceptan `limit` (default 50, max 200) y `offset` (default 0). La respuesta incluye `total`, `items`, `limit`, `offset`:

```json
{
  "items": [...],
  "total": 423,
  "limit": 50,
  "offset": 0
}
```

---

## 2. Auth

| Método | Ruta | Auth | Descripción |
|---|---|---|---|
| `POST` | `/auth/register` | Público | Crear user con email + password |
| `POST` | `/auth/login` | Público | Login con email + password |
| `POST` | `/auth/google` | Público | Login/signup con Google id_token |
| `POST` | `/auth/x` | Público | Login/signup con X (Twitter) access_token |
| `GET` | `/auth/me` | Bearer | Datos del usuario actual |

### 2.1 `POST /auth/register`

```bash
curl -X POST http://localhost:8000/auth/register \
  -H "Content-Type: application/json" \
  -d '{
    "email": "alice@example.com",
    "password": "supersecret123",
    "display_name": "Alice"
  }'
```

**Response 201**:
```json
{
  "access_token": "eyJhbGc...",
  "token_type": "bearer",
  "user": {
    "id": 42,
    "email": "alice@example.com",
    "display_name": "Alice",
    "avatar_url": null,
    "is_active": true,
    "created_at": "2026-10-04T12:00:00Z"
  }
}
```

**Errores**: `409` si el email ya existe.

### 2.2 `POST /auth/login`

```bash
curl -X POST http://localhost:8000/auth/login \
  -H "Content-Type: application/json" \
  -d '{"email": "alice@example.com", "password": "supersecret123"}'
```

Response: igual que register. Error `401` si credenciales inválidas.

### 2.3 `GET /auth/me`

```bash
curl -H "Authorization: Bearer $TOKEN" http://localhost:8000/auth/me
```

**Response 200**:
```json
{
  "id": 42,
  "email": "alice@example.com",
  "display_name": "Alice",
  "avatar_url": null,
  "is_active": true,
  "created_at": "2026-10-04T12:00:00Z"
}
```

---

## 3. Groups

| Método | Ruta | Auth | Descripción |
|---|---|---|---|
| `POST` | `/groups` | Bearer | Crear grupo (queda como admin) |
| `GET` | `/groups` | Bearer | Listar grupos del usuario |
| `POST` | `/groups/join` | Bearer | Unirse a un grupo vía join_code |
| `GET` | `/groups/{id}` | Miembro | Detalle del grupo + miembros |
| `POST` | `/groups/{id}/leave` | Miembro (no creator) | Salir del grupo |
| `DELETE` | `/groups/{id}` | Creador | Eliminar grupo + cascade |
| `POST` | `/groups/{id}/members/{uid}/promote` | Admin | member → admin |
| `POST` | `/groups/{id}/members/{uid}/demote` | Admin | admin → member (excepto creator) |
| `DELETE` | `/groups/{id}/members/{uid}` | Admin | Quitar miembro del grupo |
| `PATCH` | `/groups/{id}` | Admin | Actualizar `show_picks` (toggle visibilidad) |

### 3.1 `POST /groups`

```bash
curl -X POST http://localhost:8000/groups \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"name": "Peña del barrio", "description": "Los del café de la esquina"}'
```

**Response 201**:
```json
{
  "id": 7,
  "name": "Peña del barrio",
  "description": "Los del café de la esquina",
  "created_by_user_id": 42,
  "join_code": "A3F9KX",
  "max_members": 50,
  "show_picks": false,
  "created_at": "2026-10-04T12:00:00Z"
}
```

El `join_code` es un string alfanumérico de 6 caracteres sin ambigüedad (sin 0/O/1/I/l). Lo compartís con tus amigos para que se unan.

### 3.2 `POST /groups/join`

```bash
curl -X POST http://localhost:8000/groups/join \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"join_code": "A3F9KX"}'
```

**Response 200**:
```json
{
  "id": 7,
  "name": "Peña del barrio",
  "role": "member"
}
```

**Errores**: `404` si el join_code no existe, `409` si ya sos miembro, `400` si el grupo está lleno.

### 3.3 `GET /groups/{id}`

```bash
curl -H "Authorization: Bearer $TOKEN" http://localhost:8000/groups/7
```

**Response 200** (miembro):
```json
{
  "id": 7,
  "name": "Peña del barrio",
  "description": "Los del café de la esquina",
  "created_by_user_id": 1,
  "join_code": "A3F9KX",
  "max_members": 50,
  "show_picks": false,
  "created_at": "2026-10-04T12:00:00Z",
  "members": [
    {"user_id": 1, "display_name": "Alice", "role": "admin", "joined_at": "..."},
    {"user_id": 2, "display_name": "Bob",   "role": "member", "joined_at": "..."}
  ],
  "member_count": 2,
  "role": "admin"
}
```

`role` indica el rol del caller dentro del grupo. `404` si no existe, `403` si no es miembro.

### 3.4 `PATCH /groups/{id}` (admin only)

```bash
curl -X PATCH http://localhost:8000/groups/7 \
  -H "Authorization: Bearer $ADMIN_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"show_picks": true}'
```

**Response 200**:
```json
{
  "id": 7, "name": "Peña del barrio", ..., "show_picks": true, ...
}
```

Solo los admins pueden hacer PATCH. `403` si no es admin. PATCH es parcial: solo `show_picks` es editable por ahora (no se pueden modificar name/description/max_members).

---

## 4. Competiciones

| Método | Ruta | Auth | Descripción |
|---|---|---|---|
| `GET` | `/api/v1/competitions` | Público | Lista 10 competiciones |
| `GET` | `/api/v1/competitions/{id}` | Público | Una competición |

### 4.1 `GET /api/v1/competitions`

```bash
curl "http://localhost:8000/api/v1/competitions?tier=1&country=Spain"
```

**Response 200**:
```json
{
  "items": [
    {"id": "laliga-ea-sports", "name": "LALIGA EA SPORTS", "tier": 1, "country": "Spain", ...},
    {"id": "liga-f", "name": "Liga F", "tier": 1, "country": "Spain", ...}
  ],
  "total": 2, "limit": 50, "offset": 0
}
```

Filtros: `?tier=1|2|3`, `?country=Spain`.

---

## 5. Teams (Equipos)

| Método | Ruta | Auth | Descripción |
|---|---|---|---|
| `GET` | `/api/v1/teams` | Público | Lista equipos con filtros |
| `GET` | `/api/v1/teams/{team_id}` | Público | Un equipo |

### 5.1 `GET /api/v1/teams`

```bash
curl "http://localhost:8000/api/v1/teams?competition_id=laliga-ea-sports&gender=men&limit=10"
```

**Response 200**:
```json
{
  "items": [
    {
      "id": 30,
      "entity_id": "club-men-athletic-club",
      "competition_id": "laliga-ea-sports",
      "canonical_name": "Athletic Club",
      "short_name": "Athletic Bilbao",
      "gender": "men",
      "country": "ESP",
      "logo_url": "https://cdn.la-goyniela.example/teams/club-men-athletic-club.png"
    }
  ],
  "total": 20, "limit": 10, "offset": 0
}
```

Filtros: `?competition_id=X`, `?gender=men|women`, `?country=ESP`, `?q=substring`.

### 5.2 `GET /api/v1/teams/{team_id}`

```bash
curl http://localhost:8000/api/v1/teams/30
```

**Response 200**: mismo shape que un item de la lista. `404` si no existe.

---

## 6. Losilla (datos en vivo + escrutinio persistido)

| Método | Ruta | Auth | Descripción |
|---|---|---|---|
| `GET` | `/api/v1/losilla/jornada/{n}` | Público | Payload completo (live + escrutinio + comentarios) |
| `GET` | `/api/v1/losilla/jornada/{n}/escrutinio` | Público | Solo escrutinio (DB-first, fallback upstream) |
| `GET` | `/api/v1/losilla/jornada/{n}/comentarios` | Público | Solo comentarios (DB-first, fallback upstream) |

### 6.1 `GET /api/v1/losilla/jornada/{n}/escrutinio`

```bash
curl "http://localhost:8000/api/v1/losilla/jornada/8/escrutinio?temporada=2026"
```

**Response 200** (jornada cerrada, escrutinio en DB):
```json
{
  "source": "db",
  "escrutinio": {
    "estimada": false,
    "recaudacion_eur": "3.781.971,75",
    "bote_eur": "3.159.105,01",
    "jornada_etiqueta": 8,
    "temporada_etiqueta": "26/27",
    "fecha_jornada": "26/09/2026",
    "categorias": [
      {"aciertos": 15, "acertantes": 1, "premio_eur": "2.762.202,31"},
      {"aciertos": 14, "acertantes": 22, "premio_eur": "27.505,25"},
      {"aciertos": 13, "acertantes": 348, "premio_eur": "1.039,50"}
    ],
    "elige8": null
  }
}
```

**Response 200** (en vivo, sin escrutinio en DB):
```json
{
  "source": "upstream",
  "escrutinio": null
}
```

**Response 204** (jornada no jugada, upstream no tiene escrutinio aún): sin body.

Header `X-Losilla-Source` también devuelve `db` o `upstream` para detección desde el cliente.

---

## 7. Jornadas

| Método | Ruta | Auth | Descripción |
|---|---|---|---|
| `GET` | `/jornadas` | Público | Lista jornadas con filtros |
| `GET` | `/jornadas/{id}` | Público | Detalle de una jornada con 15 partidos |

> **MVP-05**: los modos de quiniela per-jornada (`/open`, `/optimize`, `/cost`) están **removidos**. Cada peña tiene su propia `porra` por jornada con su propio formato (D, T) y presupuesto. Ver §9.

### 7.1 `GET /jornadas`

```bash
curl "http://localhost:8000/jornadas?season=2027&status=active&limit=5"
```

**Response 200**:
```json
[
  {
    "id": 1, "temporada": 2027, "temporada_str": "2026/2027", "numero": 8,
    "status": "draft", "sale_opens_at": null, "voting_closes_at": null,
    "kickoff_first_match_at": "2026-09-26T18:00:00Z", "fetched_at": "..."
  }
]
```

Filtros: `?season=YYYY` (end-year, e.g. 2027 = 26/27), `?status=draft|active|closed|completed`, `?limit=N&offset=N`.

### 7.2 `GET /jornadas/{id}`

```bash
curl http://localhost:8000/jornadas/1
```

**Response 200**:
```json
{
  "id": 1, "temporada": 2027, ..., "kickoff_first_match_at": "...",
  "partidos": [
    {
      "posicion": 1,
      "local_team_id": 30, "visitante_team_id": 71,
      "local_raw": "Athletic Club", "visitante_raw": "Sevilla",
      "competicion_raw": null, "kickoff_at": null,
      "local_team": {
        "id": 30,
        "entity_id": "club-men-athletic-club",
        "canonical_name": "Athletic Club",
        "short_name": "Athletic Bilbao",
        "gender": "men",
        "country": "ESP",
        "logo_url": "https://cdn.la-goyniela.example/teams/club-men-athletic-club.png"
      },
      "visitante_team": {
        "id": 71,
        "entity_id": "club-men-sevilla",
        "canonical_name": "Sevilla",
        "short_name": "Sevilla FC",
        "gender": "men",
        "country": "ESP",
        "logo_url": "https://cdn.la-goyniela.example/teams/club-men-sevilla.png"
      }
    },
    ...
  ]
}
```

`posicion` es 1..15. `competicion_raw` siempre `null` (el upstream no la emite; resolver a `competition.id` queda como tarea futura). `404` si no existe.

**`local_team` / `visitante_team`** son un `TeamSummary` (subset de `/api/v1/teams/{id}`: sin `competition_id`, solo campos de display) embebido en cada partido para que el cliente pueda renderizar el boleto de los 15 partidos en **un solo request** en vez de hacer 30 calls a `/api/v1/teams/{id}`. Si la fila del equipo fue borrada, el embed viene `null` y el cliente cae al fallback `local_team_id` + `local_raw` para mostrar algo.

---

## 8. Porras (votación — MVP-05)

> **MVP-05 reemplaza MVP-04**: los picks ya no están en `/groups/{gid}/jornadas/{jid}/picks` ni dependen del per-jornada `quini_config`. Cada peña tiene su propia `porra` por jornada con formato + presupuesto. Los picks son **free-form** (un miembro puede votar 1, 2, o 3 signos en cualquier partido sin importar el formato).

| Método | Ruta | Auth | Descripción |
|---|---|---|---|
| `GET` | `/groups/{gid}/porras?jornada_id=N` | Miembro | Lista las porras de la peña |
| `GET` | `/groups/{gid}/porras/porra-formats?jornada_id=N&budget_eur=X` | Admin | Formatos (D, T) que caben en el presupuesto |
| `POST` | `/groups/{gid}/porras` | Admin | Crear la porra (jornada + budget + formato) |
| `GET` | `/groups/{gid}/porras/{pid}` | Miembro | Detalle: formato, columnas, coste, participación |
| `PATCH` | `/groups/{gid}/porras/{pid}` | Admin | Cambiar budget o formato mientras esté abierta |
| `POST` | `/groups/{gid}/porras/{pid}/picks` | Miembro | Subir/actualizar tus 15 picks (free-form) |
| `GET` | `/groups/{gid}/porras/{pid}/picks` | Miembro | Ver picks de todos (respeta `show_picks`) |
| `GET` | `/groups/{gid}/porras/{pid}/picks/me` | Miembro | Tus picks |
| `POST` | `/groups/{gid}/porras/{pid}/propuesta` | Admin | Media computada (no persiste) — el boleto propuesto |
| `GET` | `/groups/{gid}/porras/{pid}/boleto` | Miembro | Boleto final guardado (404 si todavía no) |
| `PUT` | `/groups/{gid}/porras/{pid}/boleto` | Admin | Guardar el boleto final (valida contra presupuesto) |

### 8.1 Modelo de porra

Una porra es la apuesta de una peña para una jornada. Una por `(group, jornada)` (unique constraint). Cada peña puede tener SU PROPIO formato y SU PROPIO presupuesto para la misma jornada — no se comparte config global.

```python
class Porra:
    group_id: int
    jornada_id: int
    created_by_user_id: int
    budget_eur: Decimal
    dobles: int          # 0..14
    triples: int         # 0..14
    precio_apuesta_eur: Decimal  # from Losilla (default 0.50)
    status: "open" | "closed"
    # Boleto (1:1 — persisted when PUT /boleto is called)
    boleto_matches: list[{"partido_orden": int, "signs": list[str]}] | None
    pleno_local: "0"|"1"|"2"|"M" | None
    pleno_visitante: "0"|"1"|"2"|"M" | None
```

**Cost model**: `column_cost(D, T) = 2^D × 3^T`. Coste total = `column_cost × precio_apuesta_eur`. Si `D+T = 0` (todo simple) → 1 columna.

**Pick shape (free-form)**: para partidos 1-14, `signs` es 1, 2, o 3 elementos únicos de `{"1","X","2"}`. El formato (D, T) NO restringe cuántos signos puede votar un miembro — solo restringe el BOLETO FINAL que el admin guarda.

### 8.2 Formato de un pick (MVP-05)

**Partidos 1-14** (free-form):
```json
// simple
{"partido_orden": 1,  "signs": ["1"]}

// doble
{"partido_orden": 5,  "signs": ["1", "X"]}

// triple
{"partido_orden": 3,  "signs": ["1", "X", "2"]}
```

**Partido 15 (pleno al 15) — `{local, visitante}`**:
```json
{"partido_orden": 15, "pleno": {"local": "0", "visitante": "1"}}
{"partido_orden": 15, "pleno": {"local": "M", "visitante": "M"}}
```

Cada valor ∈ `{"0","1","2","M"}` (M = "más de 3 goles").

### 8.3 `POST /groups/{gid}/porras` (admin only)

Crea la porra para la peña en una jornada.

```bash
curl -X POST http://localhost:8000/groups/7/porras \
  -H "Authorization: Bearer $ADMIN_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{
    "jornada_id": 1,
    "budget_eur": 5.00,
    "dobles": 1,
    "triples": 1
  }'
```

**Response 201**:
```json
{
  "id": 42,
  "group_id": 7,
  "jornada_id": 1,
  "budget_eur": 5.00,
  "dobles": 1,
  "triples": 1,
  "precio_apuesta_eur": 0.75,
  "status": "open",
  "columns": 6,
  "cost_eur": 4.50,
  "has_boleto": false,
  "formats_preview": [
    {"dobles": 3, "triples": 1, "columns": 24, "coste_eur": 18.00},
    {"dobles": 0, "triples": 3, "columns": 27, "coste_eur": 20.25},
    ...
  ],
  "participation": {"voted": 0, "total_members": 5, "missing_voters": [...]}
}
```

`precio_apuesta_eur` opcional — si se omite, el server lee de Losilla (jornada_escrutinio) y cae a 0.50€ si no hay dato.

**Errores**:
- `403` si no sos admin
- `404` si la jornada no existe
- `409` si ya existe una porra para `(group_id, jornada_id)` (UNIQUE constraint)
- `422` si `D + T > 14`, o el formato no cabe en el presupuesto

### 8.4 `GET /groups/{gid}/porras/porra-formats?jornada_id=N&budget_eur=X` (admin)

Lista los formatos (D, T) que caben en el presupuesto, ordenados por columnas desc (mejor cobertura primero).

```bash
curl "http://localhost:8000/groups/7/porras/porra-formats?jornada_id=1&budget_eur=5.00" \
  -H "Authorization: Bearer $ADMIN_TOKEN"
```

**Response 200**: `[{"dobles": int, "triples": int, "columns": int, "coste_eur": Decimal}, ...]`. Siempre incluye `(0, 0)` (1 columna) si el precio es positivo.

### 8.5 `POST /groups/{gid}/porras/{pid}/picks` (cualquier miembro)

Idempotente. Re-submit sobreescribe tus picks anteriores. La lista debe cubrir `partido_orden` 1..15 exactamente una vez.

```bash
curl -X POST http://localhost:8000/groups/7/porras/42/picks \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{
    "picks": [
      {"partido_orden": 1,  "signs": ["1"]},
      {"partido_orden": 5,  "signs": ["1", "X"]},
      ...
      {"partido_orden": 14, "signs": ["1"]},
      {"partido_orden": 15, "pleno": {"local": "0", "visitante": "1"}}
    ]
  }'
```

**Response 200**:
```json
{
  "porra_id": 42,
  "show_picks": false,
  "members": [
    {"user_id": 1, "display_name": "Alice", "voted": true, "voted_at": "...", "picks": null},
    ...
  ]
}
```

**Errores**:
- `403` si no sos miembro, la porra está `closed`, la jornada no está `active`, o `voting_closes_at` ya pasó
- `404` si la porra o la jornada no existen
- `422` si los picks tienen shape inválido (duplicados, partido 15 sin ambos `local`/`visitante`, valor pleno fuera de `{"0","1","2","M"}`)

### 8.6 `GET /groups/{gid}/porras/{pid}/picks` (cualquier miembro)

Mismo shape que el POST: members + show_picks + picks (visibles solo si show_picks=true).

### 8.7 `GET /groups/{gid}/porras/{pid}/picks/me` (cualquier miembro)

Array de 15 `PickOut` ordenados por `partido_orden`. `404` si todavía no votaste.

### 8.8 `POST /groups/{gid}/porras/{pid}/propuesta` (admin only)

Calcula la **media** (algoritmo de la propuesta). NO persiste — el admin la usa como punto de partida para el boleto final.

```bash
curl -X POST http://localhost:8000/groups/7/porras/42/propuesta \
  -H "Authorization: Bearer $ADMIN_TOKEN"
```

**Algoritmo** (per spec, con voto partido por peso = 1):
1. Cada miembro aporta **1 voto por partido** repartido entre los signos marcados (1 signo = 1.0; doble = 0.5+0.5; triple = 1/3 cada uno). Marcar un triple **NO pesa más** que un simple.
2. Para cada partido se suman los pesos por signo y se ordenan desc: `a ≥ b ≥ c` (tie-break: `"1" > "X" > "2"`).
3. **Ganancia de cubrir más signos**: doble gana `b`; triple gana `b + c`.
4. Asignación: top T partidos por `(b+c)` → triples; top D de los restantes por `b` → dobles; resto simple (tie-break por `partido_orden` asc).
5. Signos del boleto propuesto: simple → `[a]`; doble → `[a, b]`; triple → `[a, b, c]`.
6. Pleno: por cada equipo, el valor con más votos; empate → menos goles primero (`0 < 1 < 2 < M`).
7. Si falta gente por votar, la propuesta funciona igual con la participación que haya (P3).

**Response 200**:
```json
{
  "porra_id": 42,
  "dobles": 1, "triples": 1,
  "columns": 6, "cost_eur": 4.50,
  "participation": {"voted": 5, "total_members": 5, "missing_voters": []},
  "partidos": [
    {"partido_orden": 1, "vote_counts": {"1": 3.0, "X": 1.5, "2": 0.5},
     "reparto": ["1"], "kind": "simple"},
    {"partido_orden": 5, "vote_counts": {"1": 2.0, "X": 2.0, "2": 1.0},
     "reparto": ["1", "X"], "kind": "doble"},
    ...
    {"partido_orden": 15, "kind": "pleno",
     "vote_counts": {"local": {"0": 3}, "visitante": {"0": 3}}, "reparto": []}
  ]
}
```

### 8.9 `PUT /groups/{gid}/porras/{pid}/boleto` (admin only)

Guarda el **boleto final** que la peña va a jugar. Valida:
- Cada `match.signs` ∈ 1-3 elementos únicos de `{"1","X","2"}`.
- Cantidad de dobles (matches con 2 signos) == `porra.dobles`.
- Cantidad de triples (matches con 3 signos) == `porra.triples`.
- Resto (14 - D - T) son simples.
- `pleno.local` + `pleno.visitante` ∈ `{"0","1","2","M"}`.
- `2^D × 3^T × precio_apuesta_eur ≤ budget_eur` (defensivo — el create ya garantiza esto).

```bash
curl -X PUT http://localhost:8000/groups/7/porras/42/boleto \
  -H "Authorization: Bearer $ADMIN_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{
    "matches": [
      {"partido_orden": 1, "signs": ["1"]},
      ...
      {"partido_orden": 5, "signs": ["1", "X"]},
      {"partido_orden": 3, "signs": ["1", "X", "2"]},
      ...
    ],
    "pleno": {"local": "0", "visitante": "1"}
  }'
```

**Response 200**: `{porra_id, set_at, set_by_user_id, matches, pleno, columns, cost_eur}`.

**Errores**:
- `403` si no sos admin
- `422` si los counts no matchean (e.g., `"expected 2 doubles, got 3"`) o el coste excede el presupuesto

### 8.10 `GET /groups/{gid}/porras/{pid}/boleto` (cualquier miembro)

Devuelve el boleto final guardado. `404` si todavía no.

---


## 9. Health

| Método | Ruta | Auth | Descripción |
|---|---|---|---|
| `GET` | `/health` | Público | Liveness probe |
| `GET` | `/health/db` | Público | DB readiness (`SELECT 1`) |

```bash
curl http://localhost:8000/health
# {"status": "ok"}

curl http://localhost:8000/health/db
# {"status": "ok", "database": "reachable"}
```

---

## 10. Ejemplos de flujo

### 10.1 Crear peña, unirse, votar, consenso

```bash
# 1. Alice se registra
ALICE_TOKEN=$(curl -s -X POST http://localhost:8000/auth/register \
  -H "Content-Type: application/json" \
  -d '{"email":"alice@example.com","password":"hunter22","display_name":"Alice"}' \
  | jq -r .access_token)

# 2. Alice crea la peña
PEÑA=$(curl -s -X POST http://localhost:8000/groups \
  -H "Authorization: Bearer $ALICE_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"name":"Peña del café"}')
JOIN_CODE=$(echo $PEÑA | jq -r .join_code)
GROUP_ID=$(echo $PEÑA | jq -r .id)
echo "join_code: $JOIN_CODE"

# 3. Bob se une con el código
BOB_TOKEN=$(curl -s -X POST http://localhost:8000/auth/register \
  -H "Content-Type: application/json" \
  -d '{"email":"bob@example.com","password":"hunter22","display_name":"Bob"}' \
  | jq -r .access_token)
curl -s -X POST http://localhost:8000/groups/join \
  -H "Authorization: Bearer $BOB_TOKEN" \
  -H "Content-Type: application/json" \
  -d "{\"join_code\":\"$JOIN_CODE\"}"

# 4. Alice (admin) habilita visibilidad
curl -s -X PATCH http://localhost:8000/groups/$GROUP_ID \
  -H "Authorization: Bearer $ALICE_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"show_picks": true}'

# 5. Alice crea la porra de la peña para la jornada 8
PORRA=$(curl -s -X POST http://localhost:8000/groups/$GROUP_ID/porras \
  -H "Authorization: Bearer $ALICE_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"jornada_id":1,"budget_eur":5.00,"dobles":1,"triples":1}')
PORRA_ID=$(echo $PORRA | jq -r .id)

# 6. Ambos votan (free-form — pueden poner 1, 2 o 3 signos por partido)
curl -s -X POST http://localhost:8000/groups/$GROUP_ID/porras/$PORRA_ID/picks \
  -H "Authorization: Bearer $ALICE_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"picks":[...15 picks con pleno={local,visitante}...]}'

curl -s -X POST http://localhost:8000/groups/$GROUP_ID/porras/$PORRA_ID/picks \
  -H "Authorization: Bearer $BOB_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"picks":[...15 picks diferentes...]}'

# 7. Alice ve la propuesta de la media (no persiste)
curl -s -X POST http://localhost:8000/groups/$GROUP_ID/porras/$PORRA_ID/propuesta \
  -H "Authorization: Bearer $ALICE_TOKEN" | jq

# 8. Alice guarda el boleto final
curl -s -X PUT http://localhost:8000/groups/$GROUP_ID/porras/$PORRA_ID/boleto \
  -H "Authorization: Bearer $ALICE_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"matches":[...14 matches...], "pleno":{"local":"0","visitante":"1"}}' | jq
```

### 10.2 Construir el boleto del HTML

El script de build `scripts/build_jornada.py` consume `/jornadas/{id}` para obtener los 15 partidos, y opcionalmente `/api/v1/losilla/jornada/{n}/escrutinio` para los porcentajes. No necesita auth (esos endpoints son públicos). Ver `scripts/build_jornada.py --help`.

---

## 11. Rate limits y notas operacionales

- **Rate limit**: 100 requests/minuto por IP (configurable en `app/core/config.py`). El reverse proxy debe respetar el header `Retry-After` cuando viene en 429.
- **CORS**: en dev `*`, en producción solo el dominio del frontend.
- **Logging**: todo request se loguea con su `user_id` (si está autenticado) y tiempo de respuesta.
- **Versionado**: prefijo `/api/v1/` para catalog/competitions/teams/losilla. Los endpoints de auth/groups/jornadas/picks NO usan prefijo (es la API de la app, no API pública). Si en v2 rompemos compatibilidad, se introduce `/api/v2/...` para los afectados.
- **Storage**: el backend soporta `local` (dev) o `s3` (cualquier S3-compatible: AWS S3, **Cloudflare R2**, MinIO, LocalStack). Para R2 con dominio custom, setear `STORAGE_PUBLIC_BASE_URL=https://cdn.tu-dominio`. La API nunca devuelve URLs rotas — el cliente las usa tal cual como `src` de `<img>`.

---

## 12. Recursos

- **OpenAPI interactivo**: http://localhost:8000/docs (Swagger UI con botón "Try it out")
- **OpenAPI JSON**: http://localhost:8000/openapi.json (para generar SDKs con `openapi-generator`)
- **ReDoc**: http://localhost:8000/redoc (lectura alternativa, más bonita)
- **Source del proyecto**: ver `app/api/v1/*.py`
- **Schemas Pydantic**: ver `app/schemas/*.py`
