# La Goyniela — Feature Specification

> Documento de referencia para el dev que implementa el cliente. Cubre **qué hace cada feature, qué endpoints usa, qué validaciones aplica, qué permisos requiere, y cómo debe comportarse la UI en cada estado**. Asume que ya leyó [`API.md`](./API.md) (reference de endpoints) y [`CLIENT.md`](./CLIENT.md) (patrones de integración).

---

## 1. Overview

**La Goyniela** es una app de peña de La Quiniela (sorteo español). Un grupo de amigos quiere hacer una jugada grupal cada jornada, así que:

1. Se registran
2. Crean o se unen a un grupo (peña) con un código de 6 caracteres
3. Ven la próxima jornada activa con los 15 partidos
4. Cada uno vota sus 15 pronósticos (incluyendo el pleno al 15 multi-sign)
6. El admin genera el boleto consensuado cuando todos hayan votado
7. (Futuro) Scoring automático cuando se cierra la jornada y hay escrutinio

**Multi-tenancy**: cada grupo es un tenant aislado. Los picks son siempre por (grupo, jornada). No hay grupos "públicos" en MVP.

**Roles**: `creator` (admin implícito, inmutable) > `admin` (promoted) > `member` (default).

---

## 2. Conceptos

| Concepto | Definición |
|---|---|
| **Jornada** | Una semana de La Quiniela. Tiene 15 partidos. Estados: `draft` (Quiniwin publicó) → `active` (votación abierta) → `closed` (escrutinio oficial) → `completed` (scoring calculado). |
| **Composición** | Cada uno de los 15 partidos de una jornada. Tiene `local_team` y `visitante_team`. |
| **Pick** | Pronóstico de un user sobre un partido (MVP-05). Para 1-14: `signs` es una lista free-form de 1-3 elementos únicos ∈ `{"1","X","2"}`. Para 15: `pleno: {local, visitante}` con cada uno ∈ `{"0","1","2","M"}`. |
| **Pleno al 15** | Partido 15 (MVP-05). Goles del local ∈ {0, 1, 2, M} + goles del visitante ∈ {0, 1, 2, M}. M = "más de 3 goles". |
| **Porra** | La apuesta de UNA peña para UNA jornada. Tiene `budget_eur`, `dobles`, `triples`, `precio_apuesta_eur`, y opcionalmente un `boleto_matches` final. UNIQUE por `(group_id, jornada_id)`. |
| **Propuesta (media)** | Boleto propuesto calculado por el algoritmo de la media a partir de los votos. El admin lo usa como punto de partida para el boleto final. NO persiste. |
| **Boleto final** | El boleto que la peña juega. Lo guarda el admin con PUT. Validado contra el formato (D, T) + el precio de la porra. Persiste en `porra.boleto_matches`. |
| **`show_picks`** | Toggle del grupo (admin decide). Si `false`: solo se ve quién votó. Si `true`: se ven los picks completos de todos. |
| **`voting_closes_at`** | Timestamp del cierre de votación. Server-side enforce: POST /picks después → 403. |

---

## 3. Features

### 3.1 Auth (registro, login, OAuth, /me)

#### Endpoints

| Método | Ruta | Auth | Body | Devuelve |
|---|---|---|---|---|
| `POST` | `/auth/register` | público | `{email, password, display_name}` | 201 `{access_token, user}` |
| `POST` | `/auth/login` | público | `{email, password}` | 200 `{access_token, user}` |
| `POST` | `/auth/google` | público | `{id_token}` | 200/201 `{access_token, user}` |
| `POST` | `/auth/x` | público | `{access_token}` | 200/201 `{access_token, user}` |
| `GET` | `/auth/me` | Bearer | — | 200 `user` |

#### Validaciones

- **register**: email válido + único, password ≥ 8 chars, display_name ≥ 1 char
- **register 409**: email duplicado. **El `detail` NO incluye el email** (post-fix #7).
- **login 401**: credenciales inválidas. Detail genérico.
- **google 401**: id_token inválido/expirado. Detail: `"google authentication failed"`. Log interno con stack.
- **google 5xx**: error upstream con Google. Detail: `"google authentication failed"`.
- **x 401**: access_token inválido (credenciales malas). Detail: `"invalid x credentials"`.
- **x 502** (post-fix #8): X API caída o inalcanzable. Detail: `"x service unavailable"`. **Distinto de 401**: el cliente debe mostrar "X no disponible" vs "credenciales inválidas".

#### Token lifecycle

- JWT HS256, expira **24h** (1440 min). No hay refresh tokens en MVP.
- Persistir en `localStorage` con key `token`.
- En cada page load: si hay token → `/auth/me` para hidratar sesión. Si 401 → logout.
- En 401 durante cualquier request → logout forzado.

#### UI/UX

- `/login` y `/register` separados. Register pide password 2 veces.
- Display "Member since YYYY-MM-DD" en perfil (`UserOut.created_at`).
- Después de register/login → redirigir a `/` (dashboard con grupos).
- Logout → confirmar modal ("¿Salir?") + limpiar localStorage.

#### OAuth (Google / X)

- Botón "Continuar con Google" abre Google Sign-In SDK → toma `id_token` → `POST /auth/google`.
- Botón "Continuar con X" abre OAuth flow de X → toma `access_token` → `POST /auth/x`.
- **NO hay flujo OAuth server-side** en MVP — el cliente maneja el OAuth flow y solo manda el token.

---

### 3.2 Profile + avatar (F2)

#### Endpoints

| Método | Ruta | Auth | Body | Devuelve |
|---|---|---|---|---|
| `GET` | `/auth/me` | Bearer | — | 200 `user` |
| `POST` | `/users/me/avatar` | Bearer | `multipart/form-data: file` | 200 `{user_id, avatar_url}` |
| `DELETE` | `/users/me/avatar` | Bearer | — | 204 |

#### Avatar upload

- Validación cliente (pre-submit):
  - `file.size <= 5 MB`
  - `file.type ∈ {image/png, image/jpeg, image/webp}`
  - Preview con `URL.createObjectURL(file)`
- **NO setear `Content-Type`** en el request — el browser lo hace con el boundary
- Validación server-side: max 5MB, magic bytes del archivo (no solo content-type header)
- Errores:
  - `413`: "imagen demasiado grande (máx 5 MB)"
  - `415`: "solo se aceptan PNG, JPG, WebP"
  - `400`: magic bytes no matchean content-type (archivo "falseado")
  - `200`: `{user_id, avatar_url}` (URL absoluta de R2)

#### Avatar display

- `UserOut.avatar_url: str | None` — URL absoluta o `null`.
- Si `null` → mostrar placeholder (icon genérico, iniciales del display_name, o default).
- Si URL → `<img src={user.avatar_url}>` directamente.

---

### 3.3 Grupos (F3) — la entidad multi-tenant

#### Endpoints

| Método | Ruta | Auth | Body | Devuelve |
|---|---|---|---|---|
| `POST` | `/groups` | Bearer | `{name, description?}` | 201 `group` (incluye `join_code`) |
| `GET` | `/groups` | Bearer | — | 200 `[{id, name, member_count, role, show_picks}]` |
| `POST` | `/groups/join` | Bearer | `{join_code}` | 200 `{id, name, role}` |
| `GET` | `/groups/{id}` | Miembro | — | 200 `group_detail` (con members + role del caller) |
| `POST` | `/groups/{id}/leave` | Miembro (no creator) | — | 204 |
| `DELETE` | `/groups/{id}` | Creator | — | 204 |
| `PATCH` | `/groups/{id}` | Admin | `{show_picks}` | 200 `group` |
| `POST` | `/groups/{id}/members/{uid}/promote` | Admin | — | 204 |
| `POST` | `/groups/{id}/members/{uid}/demote` | Admin | — | 204 |
| `DELETE` | `/groups/{id}/members/{uid}` | Admin | — | 204 |

#### Validaciones

- `name`: 2-30 chars, requerido
- `description`: max 500 chars, opcional
- `max_members`: default 50, no editable en MVP
- `join_code`: 6 chars alfanuméricos sin ambigüedad (sin `0/O/1/I/l`). **Case-sensitive**.

#### Errores (post-fixes #4 y #5)

```typescript
// POST /groups/join
r.status === 400 && r.body.detail.code === "group_full"      // grupo lleno
r.status === 409 && r.body.detail.code === "already_member"  // ya sos miembro
r.status === 404                                                    // código no existe

// POST /groups/{id}/leave (NO si sos creator)
r.status === 400 && r.body.detail === "creator cannot leave"  // creator intenta salirse

// POST /groups/{id}/members/{uid}/{promote,demote} (NO si target == caller)
r.status === 400                                                       // "cannot promote yourself" / "cannot demote yourself"

// DELETE /groups/{id}/members/{uid} (NO si target == caller)
r.status === 400                                                       // "cannot remove yourself"

// POST /groups/{id}/members/{uid}/demote (NO si target es creator)
r.status === 400                                                       // "cannot demote creator"
```

#### Roles

| Rol | Puede |
|---|---|
| **creator** (implícito) | Todo del admin + DELETE /groups + NO puede ser kickeado/demoteado/expulsado |
| **admin** | promote/demote/remove members, PATCH show_picks, POST consensus, dejar grupo |
| **member** | Ver, votar, dejar grupo |

#### UI/UX

- **Crear grupo**: modal con `name` + opcional `description`. Al éxito → redirigir a `/groups/{id}` y **mostrar el `join_code` prominentemente** (botón "Copiar" + "Compartir por WhatsApp").
- **Unirse**: input de 6 chars + botón. Validación local: uppercase + alfanumérico.
- **Detalle del grupo**:
  - Lista de miembros con `role` visible
  - Si `role === "admin"`: botones adicionales (toggle show_picks, promover/degradar/expulsar)
  - Si soy creator: indicador visual + botón "Eliminar grupo" (con confirm)
  - Si role === "admin" (no creator): NO mostrar el toggle de "Eliminar grupo"
- **Salir**: confirmar modal ("¿Salir de [nombre]? Vas a perder acceso a los picks de este grupo")
- **Eliminar grupo** (creator): confirmar modal doble ("Esta acción no se puede deshacer")

---

### 3.4 Teams catalog (F4) — solo para browse

#### Endpoints

| Método | Ruta | Auth | Devuelve |
|---|---|---|---|
| `GET` | `/api/v1/teams` | público | 200 `{items: [...], total, limit, offset}` |
| `GET` | `/api/v1/teams/{team_id}` | público | 200 `TeamOut` |

#### Filtros

- `?competition_id=laliga-ea-sports` (también `liga-f`, `uefa-champions-league`, etc.)
- `?gender=men|women`
- `?country=ESP` (3-letter ISO)
- `?q=substring` (busca en `canonical_name`)
- `?limit=N&offset=N` (default 50, max 200)

#### Caching strategy (cliente)

- Fetch la lista completa **una vez al primer load de la sesión** (423 entries, ~80KB).
- Cache en localStorage con TTL 24h.
- Refresh solo al primer load de sesión (no polling).
- El display de logos en picks/consensus viene del embed de `/jornadas/{id}` y `/picks`, NO de este endpoint.

---

### 3.5 Jornadas (F5) — el corazón de la app

#### Endpoints

| Método | Ruta | Auth | Devuelve |
|---|---|---|---|
| `GET` | `/jornadas` | público | 200 `[{jornada_summary}, ...]` |
| `GET` | `/jornadas?status=active` | público | 200 `[{jornada_summary con status=active}]` |
| `GET` | `/jornadas/{id}` | público | 200 `jornada_detail` (con 15 partidos + team embebido) |

#### Estados

| Estado | Significado | UI |
|---|---|---|
| `draft` | Quiniwin publicó pero sin voting_closes_at | "próximamente" |
| `active` | Votación abierta | Mostrar form de picks |
| `closed` | Escrutinio oficial disponible | "cerrada, sin consenso" |
| `completed` | Scoring calculado (futuro) | "cerrada, scoring hecho" |

#### Lógica del cliente

```typescript
// 1. Buscar jornada activa para el grupo del user
const activeJornadas = await api(`/jornadas?status=active`);
const jornada = activeJornadas[0];

// 2. Si no hay → mostrar "no hay jornada activa, esperá la próxima"
// 3. Si hay → cargar `/jornadas/{id}` para tener los 15 partidos con team embebido

// 4. Verificar si la votación está cerrada
const closed = jornada.voting_closes_at
  ? new Date(jornada.voting_closes_at) <= new Date()
  : false;
// → si closed: deshabilitar form, mostrar "votación cerrada"
// → si !closed y status === 'active': habilitar form

// 5. Ver si el user ya votó
const myPicks = await api(`/groups/${gid}/jornadas/${jornada.id}/picks/me`).catch(() => null);

// 6. Render: form editable si no closed y no voted
```

---

### 3.6 Porras (F6) — la feature principal (MVP-05)

> **MVP-05 reemplaza MVP-04**: el config per-jornada (`jornada.quini_config`) está **removido**. Cada peña tiene su propia `porra` por jornada con formato (D, T) + presupuesto. Los picks son **free-form** (sin restricción por formato). El algoritmo de la media genera la propuesta, el admin confirma el boleto final.

#### Endpoints

| Método | Ruta | Auth | Body | Devuelve |
|---|---|---|---|---|
| `GET` | `/groups/{gid}/porras?jornada_id=` | Miembro | — | 200 `[PorraOut]` |
| `GET` | `/groups/{gid}/porras/porra-formats?jornada_id=&budget_eur=` | Admin | — | 200 `[PorraFormatOut]` |
| `POST` | `/groups/{gid}/porras` | Admin | `{jornada_id, budget_eur, dobles, triples, precio_apuesta_eur?}` | 201 `PorraDetailOut` |
| `GET` | `/groups/{gid}/porras/{pid}` | Miembro | — | 200 `PorraDetailOut` |
| `PATCH` | `/groups/{gid}/porras/{pid}` | Admin | `{budget_eur?, dobles?, triples?}` | 200 `PorraDetailOut` |
| `POST` | `/groups/{gid}/porras/{pid}/picks` | Miembro | `{picks: [15 items]}` | 200 `PicksOut` |
| `GET` | `/groups/{gid}/porras/{pid}/picks` | Miembro | — | 200 `PicksOut` |
| `GET` | `/groups/{gid}/porras/{pid}/picks/me` | Miembro | — | 200 `[PickOut]` |
| `POST` | `/groups/{gid}/porras/{pid}/propuesta` | Admin | — | 200 `PropuestaOut` (no persiste) |
| `GET` | `/groups/{gid}/porras/{pid}/boleto` | Miembro | — | 200 `BoletoOut` |
| `PUT` | `/groups/{gid}/porras/{pid}/boleto` | Admin | `{matches, pleno}` | 200 `BoletoOut` |

Ver `docs/API.md` §8 para los shapes completos.

#### Flujo (lo que el cliente debe implementar)

1. **Admin crea la porra** (POST /porras) — elige `budget_eur`, `dobles`, `triples`. El server devuelve `formats_preview` con todos los formatos que caben + `participation` con la cantidad de miembros que ya votaron.
2. **Mientras está `open`**, los miembros votan (POST /porras/{pid}/picks) — pick shape **free-form**: cada partido 1-14 puede llevar 1, 2, o 3 signos únicos ∈ `{"1","X","2"}` independientemente del formato. Partido 15 usa `{local, visitante}` ∈ `{"0","1","2","M"}`.
3. **Admin pide la propuesta** (POST /porra-formats no — es `propuesta`) — algoritmo de la media, no persiste.
4. **Admin confirma el boleto** (PUT /boleto) — el server valida D/T/cost contra el formato + budget.

#### Shape de un pick

```typescript
// Partido 1-14 (free-form: 1, 2, o 3 signos)
{partido_orden: 1,  signs: ["1"]}        // simple
{partido_orden: 5,  signs: ["1", "X"]}   // doble
{partido_orden: 3,  signs: ["1","X","2"]} // triple

// Partido 15 (pleno)
{partido_orden: 15, pleno: {local: "0", visitante: "0"}}
{partido_orden: 15, pleno: {local: "M", visitante: "1"}}
```

#### Cost model

`column_cost(D, T) = 2^D × 3^T`. Coste total = `column_cost × precio_apuesta_eur`. Si `D+T=0` (todo simple) → 1 columna.

#### Algoritmo de la media (propuesta)

1. **Peso del voto.** Cada miembro aporta **1 voto por partido** repartido entre los signos marcados (1 signo = 1.0; doble = 0.5+0.5; triple = 1/3+1/3+1/3). Marcar triple **NO pesa más** que simple. *(Decidido P1, 2026-10-05.)*
2. **Reparto por partido.** Sumar pesos por signo → `s1, sx, s2`. Ordenar desc: `a ≥ b ≥ c`. Tie-break: `"1" > "X" > "2"`.
3. **Ganancia de cubrir más signos.** Doble gana `b`; triple gana `b + c`.
4. **Asignación.** Greedy: top T por `(b+c)` → triples; top D de los restantes por `b` → dobles; resto simple. Tie-break por `partido_orden` ascendente.
5. **Signos del boleto propuesto.** Simple `[a]`; doble `[a, b]`; triple `[a, b, c]`.
6. **Pleno al 15.** Por cada equipo, el valor con más votos; empate → menos goles primero (`0 < 1 < 2 < M`). *(Decidido P2.)*
7. **Falta gente por votar.** La propuesta funciona igual con la participación que haya; el admin decide cuándo confirmar. *(Decidido P3.)*

Implementación: `app/services/propuesta.py` (puro, testeable sin DB).

---

### 3.8 Image uploads (F8)

#### Endpoints

| Método | Ruta | Auth | Body | Devuelve |
|---|---|---|---|---|
| `POST` | `/admin/teams/{team_id}/logo` | Admin | `multipart/form-data: file` | 200 `{team_id, logo_url, size}` |
| `DELETE` | `/admin/teams/{team_id}/logo` | Admin | — | 204 |
| `POST` | `/users/me/avatar` | Bearer (self) | `multipart/form-data: file` | 200 `{user_id, avatar_url}` |
| `DELETE` | `/users/me/avatar` | Bearer (self) | — | 204 |

#### Validación (server)

- Max 5MB
- Magic bytes check (PNG: `\x89PNG\r\n\x1a\n`, JPEG: `\xff\xd8\xff`, WebP: `RIFF...WEBP`)
- 5xx: si falla la subida a R2 (problema de storage, no del cliente)

#### Validación cliente (pre-submit)

- `file.size <= 5 * 1024 * 1024`
- `file.type ∈ {image/png, image/jpeg, image/webp}`
- Preview con `URL.createObjectURL(file)` antes de subir
- Si todo OK → upload

#### Patrón de upload (frontend)

```typescript
const form = new FormData();
form.append("file", file);
const r = await fetch(`${baseUrl}/admin/teams/${teamId}/logo`, {
  method: "POST",
  headers: { Authorization: `Bearer ${token}` },
  // NO Content-Type — el browser lo setea con boundary
  body: form,
});
```

#### Display

- `logo_url` y `avatar_url` vienen **absolutas** del backend (ya sea R2 public dev URL o custom domain en prod).
- `<img src={logo_url}>` directo. Sin proxy, sin transformations.
- Si `null` → placeholder.

---

## 4. Catálogo consolidado de errores

| Status | Cuándo | Mensaje user-friendly (es-ES) |
|---|---|---|
| `400` | `/groups/join` con grupo lleno | "Este grupo está lleno" |
| `400` | `/groups/{id}/leave` siendo creator | "El creador no puede salir del grupo. Elimínalo si querés." |
| `400` | `/groups/{id}/members/{uid}/{promote,demote}` con target = caller | "No podés promoverte/d degradarte a vos mismo. Pedile a otro admin." |
| `400` | `/groups/{id}/members/{uid}` con target = caller | "No podés expulsarte a vos mismo. Usá 'Salir del grupo'." |
| `400` | `/groups/{id}/members/{uid}/demote` con target = creator | "El creador no puede ser degradado" |
| `400` | Validación Pydantic falló (ej. 15 picks en POST /picks → 422, no 400) | (raro) |
| `401` | Token ausente/expirado/inválido | "Tu sesión expiró. Volvé a iniciar sesión." |
| `401` | `/auth/login` con credenciales malas | — "Email o contraseña incorrectos" |
| `401` | `/auth/x` con access_token inválido | — "Credenciales de X inválidas" |
| `403` | POST /picks en jornada `draft` | — "La jornada no está activa" |
| `403` | POST /picks con `voting_closes_at` pasado | — "El plazo de votación cerró el [fecha]" |
| `403` | `GET /groups/{id}` no siendo miembro | — "No sos miembro de este grupo" |
| `403` | `POST /consensus` no siendo admin | — "Solo los admins pueden generar el consenso" |
| `404` | `/groups/join` con código inválido | "Código de invitación inválido" |
| `404` | `GET /jornadas/{id}` no existente | "Jornada no encontrada" |
| `409` | `POST /auth/register` email duplicado | — "Ese email ya está registrado" |
| `409` | `POST /groups/join` ya miembro | "Ya sos miembro de este grupo" |
| `409` | `POST /groups/{id}/consensus` faltan voters | "Falta que voten: [lista de nombres]" |
| `413` | Upload > 5MB | "La imagen es demasiado grande (máx 5 MB)" |
| `415` | Upload tipo no soportado | "Solo se aceptan imágenes PNG, JPG o WebP" |
| `422` | POST /picks con variantes del pleno inválidas o shape incorrecto | (Pydantic detail, mostrar el primer error) |
| `502` | `/auth/x` con X API caída | — "X (Twitter) no está disponible, reintentá más tarde" |
| `5xx` | Error interno | "Algo salió mal. Reintentá." |

---

## 5. Matriz de permisos

| Endpoint | Público | Miembro | Admin | Creator |
|---|---|---|---|---|
| `POST /auth/{register,login,google,x}` | ✅ | — | — | — |
| `GET /auth/me` | — | ✅ (Bearer) | ✅ | ✅ |
| `GET /api/v1/teams` | ✅ | ✅ | ✅ | ✅ |
| `GET /api/v1/teams/{id}` | ✅ | ✅ | ✅ | ✅ |
| `GET /api/v1/competitions` | ✅ | ✅ | ✅ | ✅ |
| `GET /api/v1/competitions/{id}` | ✅ | ✅ | ✅ | ✅ |
| `GET /jornadas` | ✅ | ✅ | ✅ | ✅ |
| `GET /jornadas/{id}` | ✅ | ✅ | ✅ | ✅ |
| `GET /api/v1/losilla/*` | ✅ | ✅ | ✅ | ✅ |
| `GET /health`, `/health/db` | ✅ | ✅ | ✅ | ✅ |
| `GET /groups` | — | ✅ | ✅ | ✅ |
| `POST /groups` | — | ✅ | ✅ | ✅ |
| `POST /groups/join` | — | ✅ (Bearer) | ✅ | ✅ |
| `GET /groups/{id}` | — | ✅ | ✅ | ✅ |
| `POST /groups/{id}/leave` | — | ✅ (no creator) | ✅ | ❌ |
| `DELETE /groups/{id}` | — | ❌ | ❌ | ✅ |
| `PATCH /groups/{id}` | — | ❌ | ✅ (solo `show_picks`) | ✅ |
| `POST /groups/{id}/members/{uid}/promote` | — | ❌ | ✅ (no self) | ✅ |
| `POST /groups/{id}/members/{uid}/demote` | — | ❌ | ✅ (no self, no creator) | ✅ |
| `DELETE /groups/{id}/members/{uid}` | — | ❌ | ✅ (no self) | ✅ |
| `POST /groups/{gid}/porras/{pid}/picks` | — | ✅ (porra open + jornada active) | ✅ | ✅ |
| `GET /groups/{gid}/porras/{pid}/picks` | — | ✅ | ✅ | ✅ |
| `GET /groups/{gid}/porras/{pid}/picks/me` | — | ✅ | ✅ | ✅ |
| `POST /groups/{gid}/porras/{pid}/propuesta` | — | ❌ | ✅ | ✅ |
| `PUT /groups/{gid}/porras/{pid}/boleto` | — | ❌ | ✅ | ✅ |
| `POST /admin/teams/{id}/logo` | — | ❌ | ✅ | ✅ |
| `DELETE /admin/teams/{id}/logo` | — | ❌ | ✅ | ✅ |
| `POST /users/me/avatar` | — | ✅ (Bearer, self) | ✅ | ✅ |
| `DELETE /users/me/avatar` | — | ✅ (Bearer, self) | ✅ | ✅ |

---

## 6. UI/UX guidelines

### 6.1 Layout general

- **Mobile-first**: app debe funcionar bien en mobile (375px ancho mínimo).
- **Loading states**: cada fetch async tiene un skeleton o shimmer. NUNCA un spinner sin contexto.
- **Empty states**: cada lista tiene un empty state con icono + mensaje + CTA. Ej: "No hay grupos todavía. ¡Creá el primero!"

### 6.2 Form de picks

- 14 inputs de radio en grid responsive (4 columnas desktop, 2 mobile)
- Pleno: 3 columnas (M0/M1/M2), cada variant como checkbox
- Botón "Guardar" sticky en el bottom de la pantalla (visible mientras hacés scroll)
- Disabled state visual claro (opacity 0.5 + cursor not-allowed)

### 6.3 Polling

- En `/groups/{gid}` (vista del grupo): polling cada 30 segundos de `/groups/{gid}/jornadas/{jid}/picks` para "live" del estado de votación
- Limpiar interval al desmontar (`useEffect` cleanup)
- No polling en otras vistas (página de inicio, perfil)

### 6.4 Tokens y auth

- Persistir token en `localStorage` (no `sessionStorage`, no cookies — la API es stateless)
- Auto-logout en 401 (wrapper centralizado en `lib/api.ts`)
- Loading state en cualquier page load que dependa de `/auth/me`

### 6.5 Errores

- Mostrar `body.detail.message` cuando el detail sea objeto con `message`
- Mostrar `body.detail` cuando sea string
- **No mostrar stack traces ni emails ni tokens** al usuario
- Toast en la esquina superior derecha para errores no bloqueantes (4xx)
- Modal o página de error para errores bloqueantes (5xx)

---

## 7. Acceptance criteria (pre-merge)

Antes de mergear el cliente, validar:

### Auth
- [ ] Register: email duplicado → 409 con "ese email ya está registrado"
- [ ] Login: credenciales malas → 401 con "email o contraseña incorrectos"
- [ ] Logout: limpia token, redirige a /login
- [ ] Token expirado (24h): auto-logout, redirige a /login
- [ ] OAuth Google: id_token inválido → 401
- [ ] OAuth X: access_token inválido → 401; X caído → 502

### Profile
- [ ] `/auth/me` muestra `display_name`, `email`, `created_at` ("Member since"), `avatar_url`
- [ ] Upload avatar: max 5MB, solo PNG/JPG/WebP, preview antes de subir
- [ ] Avatar se actualiza en navbar después del upload

### Grupos
- [ ] Crear grupo: muestra `join_code` prominentemente con botón "Copiar"
- [ ] Unirse: código inválido → 404 con "código inválido"
- [ ] Unirse: grupo lleno → 400 con código `group_full`
- [ ] Unirse: ya miembro → 409 con código `already_member`
- [ ] Salir (member): 204, redirige a /
- [ ] Salir (creator): 400 con "El creador no puede salir"
- [ ] Eliminar (creator): confirmar modal doble, 204, redirige a /
- [ ] Eliminar (admin no creator): 403
- [ ] Promote (admin): otro member → 204; self → 400; creator → 400
- [ ] Demote (admin): otro admin → 204; self → 400; creator → 400
- [ ] Remove (admin): otro member → 204; self → 400

### Picks
- [ ] Listar jornadas: muestra `?status=active` primero
- [ ] Ver jornada: render de 15 partidos con logos (del embed)
- [ ] Form de picks: 14 inputs + 1 pleno multi-sign
- [ ] Submit picks: idempotente, valida server-side
- [ ] Picks: variantes del pleno inválidas → 422
- [ ] Picks: jornada no active → 403
- [ ] Picks: voting_closes_at pasado → 403
- [ ] Ver picks (show_picks=false): solo `display_name` + `voted: true/false`
- [ ] Ver picks (show_picks=true): incluye `picks` array por miembro
- [ ] Ver mis picks (`/picks/me`): 200 con los 15 picks
- [ ] Ver mis picks: 404 si no he votado

### Consensus
- [ ] Generar consenso: solo admin, 403 si no
- [ ] Generar consenso: 409 con `missing_voters` si falta gente
- [ ] Generar consenso: 200 con reporte completo
- [ ] Reporte: partido 15 muestra M0/M1/M2 (siempre 3 keys)
- [ ] Reporte: empates en 1-14 → multi-marca todos

### Uploads
- [ ] Upload logo de equipo (admin): 200 con `logo_url`
- [ ] Upload avatar (user): 200 con `avatar_url`
- [ ] Avatar > 5MB → 413
- [ ] Avatar wrong type → 415
- [ ] NO setear Content-Type en multipart (verificar en network tab)

### Imagenes / Equipo
- [ ] GET /jornadas/{id} embebe `local_team` y `visitante_team` con logos
- [ ] Logos vienen de R2 (URL absoluta)
- [ ] `<img>` tag renderiza sin proxy

### Misc
- [ ] Polling cada 30s en /groups/{gid}
- [ ] No mostrar `competicion_raw` (siempre null)
- [ ] Si jornada no active: form disabled
- [ ] Si voting_closes_at pasado: form disabled
- [ ] Si usuario no es miembro: 403 (no leak de que el grupo existe)

---

## 8. Decisiones de producto (para que se entienda el "por qué")

- **No hay refresh tokens** en MVP. Trade-off: UX peor (re-login cada 24h) vs implementación más simple. Si te molesta, es 2-3 días de trabajo agregar refresh tokens rotativos.
- **No hay edición de picks separada** del submit. El UPSERT cubre el caso. Si querés historial de cambios, hay que agregar tabla `pick_history`.
- **No hay grupos públicos**. Si en el futuro se quiere "peña pública de La Goyniela", hay que agregar un flag al grupo.
- **Consensus se recalcula cada vez**. No se guarda. Si querés guardar el boleto final una vez generado (para auditoría o para re-generar reportes), hay que agregar tabla `consensus_snapshot`.
- **No hay "scoring post-jornada"** (cuántos aciertos tuvo cada uno). Está planeado para después de T-data-4. Cuando se implemente, el GET /picks también debería retornar `aciertos` por miembro comparando contra `jornada_escrutinio.payload`.
- **El form de picks acepta 14 inputs + 1 pleno en la misma submission**. NO hay "submit por partido". El usuario debe llenar todos antes de poder guardar.
- **El admin NO puede modificar el nombre/descripción/max_members del grupo vía PATCH**. Solo `show_picks` es editable. Si querés editar el resto, hay que agregar campos a `GroupPatchIn`.

---

## 9. Recursos

- [`docs/API.md`](./API.md) — reference de cada endpoint (curl + JSON shape)
- [`docs/CLIENT.md`](./CLIENT.md) — patrones de integración (auth flow, fetch wrapper, gotchas)
- [`docs/FRONTEND-IMPLEMENTATION.md`](./FRONTEND-IMPLEMENTATION.md) — orden de implementación sugerido
- `http://localhost:8000/docs` — Swagger UI interactivo
- `http://localhost:8000/openapi.json` — para generar SDKs con `openapi-generator`
- [`docs/HANDOFF-NEXT-SESSION.md`](./HANDOFF-NEXT-SESSION.md) — historial de contract fixes