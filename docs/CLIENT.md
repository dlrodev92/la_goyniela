# La Goyniela — Guía de integración del cliente

**Para**: developer que va a construir el frontend (web, mobile, lo que sea)
**Versión API**: 0.4.x (votación + scheduler + Losilla + image upload, backend S3-compatible)
**Base URL (dev)**: `http://localhost:8000`

> **Antes de empezar**: leete [`docs/API.md`](./API.md) primero — ahí está el reference completo de cada endpoint con shapes exactos. Este doc asume que ya lo leíste y se enfoca en **cómo integrar la API en una app real** (auth flow, errores, gotchas, patrones).

---

## 1. Quickstart (5 minutos)

```bash
# 1. Levantar la API (Postgres + FastAPI)
cd la-goyniela
docker compose up -d
uv run alembic upgrade head
uv run python -m app.scripts.load_team_catalog  # primera vez
uv run uvicorn app.main:app --reload

# 2. Probar desde el cliente
curl http://localhost:8000/health
# {"status":"ok"}

# 3. Swagger UI para explorar
open http://localhost:8000/docs
```

Si ves `/health` → 200 OK y `/docs` → Swagger UI, estás listo para integrar.

---

## 2. Auth flow (lo más importante)

### 2.1 El contrato

- **Token**: JWT Bearer con expiración **24h** (1440 min). No hay refresh tokens en MVP-02.
- **Header**: `Authorization: Bearer <token>` en TODA request autenticada.
- **Errores**:
  - `401` → token ausente, expirado, o inválido. **Forzar logout**.
  - `403` → token válido pero el caller no tiene permisos (no es admin, no es miembro del grupo, etc.). **No es error de sesión**, es un error de UI ("no podés hacer esto").

### 2.2 Endpoints

```
POST /auth/register    → 201 { access_token, user }
POST /auth/login       → 200 { access_token, user }
POST /auth/google      → 200/201 { access_token, user }
POST /auth/x           → 200/201 { access_token, user }
GET  /auth/me          → 200 user
```

`/auth/register` y los OAuth endpoints hacen "link by email": si el email ya existe, devuelven el user existente con un token nuevo. No hay flujo de merge explícito.

### 2.3 Patrón recomendado (pseudo-código)

```javascript
// AuthContext que mantiene el token y user en memoria + localStorage

const AuthContext = createContext();

export function AuthProvider({ children }) {
  const [token, setToken] = useState(() => localStorage.getItem('token'));
  const [user, setUser] = useState(null);
  const [loading, setLoading] = useState(true);

  // Hidratar sesión al cargar la app
  useEffect(() => {
    if (!token) { setLoading(false); return; }
    fetch('/auth/me', { headers: { Authorization: `Bearer ${token}` } })
      .then(r => r.ok ? r.json() : null)
      .then(u => { if (u) setUser(u); else logout(); })
      .finally(() => setLoading(false));
  }, [token]);

  async function login(email, password) {
    const r = await fetch('/auth/login', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ email, password }),
    });
    if (!r.ok) throw new Error('credenciales inválidas');
    const { access_token, user } = await r.json();
    setToken(access_token);
    setUser(user);
    localStorage.setItem('token', access_token);
  }

  function logout() {
    setToken(null);
    setUser(null);
    localStorage.removeItem('token');
    // El cliente decide a dónde redirigir (login screen)
  }

  return <AuthContext.Provider value={{ token, user, login, logout, loading }}>{children}</AuthContext.Provider>;
}
```

### 2.4 Fetch wrapper (recomendado)

Centralizá el header y el manejo de 401/403:

```javascript
async function api(path, options = {}) {
  const token = localStorage.getItem('token');
  const res = await fetch(`/api${path}`, {  // o sin /api según el endpoint
    ...options,
    headers: {
      'Content-Type': 'application/json',
      ...(token ? { Authorization: `Bearer ${token}` } : {}),
      ...options.headers,
    },
    body: options.body && typeof options.body !== 'string' && !(options.body instanceof FormData)
      ? JSON.stringify(options.body)
      : options.body,
  });

  // 401 → sesión expirada, forzar logout
  if (res.status === 401) {
    localStorage.removeItem('token');
    window.location.href = '/login';  // o el flow que prefieras
    throw new Error('sesión expirada');
  }

  // 204 No Content
  if (res.status === 204) return null;

  // 4xx / 5xx → throw con detalle
  if (!res.ok) {
    const detail = await res.json().catch(() => ({ detail: res.statusText }));
    const err = new Error(detail.detail || 'error');
    err.status = res.status;
    err.body = detail;
    throw err;
  }

  // 2xx con body
  return res.json();
}
```

**Por qué**: si tenés 30 componentes que hacen `fetch`, no querés duplicar el header / manejo de 401 en 30 lugares. Un wrapper, una vez.

### 2.5 Logout

No hay endpoint `POST /auth/logout` (no hace falta server-side: el token es stateless JWT). Borrar el token del cliente es suficiente.

### 2.6 Google / X (OAuth)

El cliente recibe el `id_token` de Google o `access_token` de X y lo manda a la API. La API valida y crea/linkea el user. **No necesitás manejar el OAuth flow del lado API**, solo capturar el token del provider y mandarlo.

```javascript
// Después de Google Sign-In:
const idToken = googleUser.getAuthResponse().id_token;
const r = await api('/auth/google', { method: 'POST', body: { id_token: idToken } });
// → mismo { access_token, user } que /login
```

---

## 3. Manejo de errores

La API retorna errores en formato `{ "detail": "mensaje humano" }` (ver `docs/API.md` §1.3). **El `detail` ES para mostrar al usuario** (en español, claro). No lo wrapees en un mensaje genérico tipo "Error: 400".

### 3.1 Catálogo de errores comunes

| Status | Cuándo | Qué hacer en UI |
|---|---|---|
| `400` | Validación falla, falta FK | Mostrar `detail` tal cual |
| `401` | Token ausente/expirado/inválido | Logout forzado, redirigir a login |
| `403` | No autorizado (no admin / no miembro) | Mostrar `detail` o mensaje genérico |
| `404` | Recurso no existe | "No encontrado" o navegar atrás |
| `409` | Conflicto: email duplicado, ya sos miembro, faltan voters en consensus | Mostrar `detail` + acción contextual (ej. en consensus: "Falta que voten: Bob, Carol") |
| `413` | Upload > 5 MB | "Imagen demasiado grande (máx 5 MB)" |
| `415` | Tipo de archivo no soportado | "Solo se aceptan PNG, JPG, WebP" |
| `422` | Pydantic validation (shape incorrecto) | Mostrar `detail` (casi siempre útil para debug) |
| `502` | Upstream no disponible (Losilla, Quiniwin) | "Servicio externo no disponible, reintentá" |

### 3.2 Errores que SÍ son bugs (no mostrar al usuario)

- `500` → log + "Algo salió mal, intentá de nuevo"
- `502` con detail que es un stack trace → NO mostrar al usuario, log internamente

### 3.3 Errores específicos que requieren lógica

> **MVP-05**: el consensus endpoint está **removido**. Reemplazado por la **propuesta** (POST `/groups/{gid}/porras/{pid}/propuesta`) que no requiere quorum (funciona con participación parcial). El boleto final se guarda con PUT `/groups/{gid}/porras/{pid}/boleto` (admin). La respuesta de propuesta SIEMPRE trae `participation.missing_voters` para mostrar UI con quiénes faltan — pero la propuesta nunca falla con 409 por quorum.

**Propuesta (POST `/groups/{gid}/porras/{pid}/propuesta`)**:
- `200` con `participation.missing_voters: [{user_id, display_name}]` → mostrar UI con quiénes faltan (no es un error)
- `403` → "Solo admins pueden pedir la propuesta"

**Picks submit (POST `/groups/{gid}/porras/{pid}/picks`)**:
- `422` con detail tipo "expected 15 picks, got 14" o "value is not a valid PlenoValue" → validación del lado cliente, no mandar al server
- `404` con detail "porra not found" → UX issue, mostrar al admin
- `403` con detail "voting closed at ..." → el form debe deshabilitarse solo cuando `voting_closes_at < now()` o cuando la porra está `closed`

---

## 4. Cargas iniciales (al abrir la app)

Cuando el usuario abre la app, ¿qué necesitás?

```javascript
// 1. Si tiene token: hidratar sesión
GET /auth/me

// 2. Lista de grupos del usuario
GET /groups
// → array de { id, name, role, member_count, show_picks }

// 3. (Opcional) mostrar próximas jornadas
GET /jornadas?status=active&limit=5

// 4. (Opcional) para autocompletar picks: lista de equipos
GET /api/v1/teams?competition_id=laliga-ea-sports
// → cachear localmente (son ~423 equipos total)
```

**No hacer**:
- ❌ No traigas TODAS las jornadas al cargar — solo las activas/recientes (`?status=active` o `?status=draft&limit=10`)
- ❌ No traigas picks de todos los grupos del usuario upfront — solo cuando el usuario entra al grupo

---

## 5. Flujo de Peña (crear, unirse, administrar)

### 5.1 Crear peña (usuario autenticado)

```javascript
const { id, join_code } = await api('/groups', {
  method: 'POST',
  body: { name: 'Peña del café', description: '...' },
});
// Mostrar el join_code prominente con un botón "Copiar" y "Compartir"
```

El `join_code` es **string alfanumérico de 6 chars sin ambigüedad** (sin 0/O/1/I/l). Compartilo por WhatsApp / email / etc.

### 5.2 Unirse a peña

```javascript
const group = await api('/groups/join', {
  method: 'POST',
  body: { join_code: 'A3F9KX' },
});
// Redirigir a /groups/{id}
```

Si `409` → "Ya sos miembro de este grupo" o "Grupo lleno".

### 5.3 Roles

- `creator` (admin implícito, inmutable)
- `admin` (promoted by creator, puede promover/demote/eliminar)
- `member` (default)

El creator **no puede ser kickeado, demoteado, ni salir del grupo** (debe eliminar el grupo si quiere irse).

### 5.4 Toggle `show_picks` (admin)

```javascript
PATCH /groups/{id}  body: { show_picks: true }
// El admin decide cuando revelar los picks de cada uno
```

UX: un switch/toggle en la vista de admin del grupo. Cuando está `false`, los miembros ven solo "Alice votó / Bob NO votó" en la lista de picks. Cuando está `true`, ven los picks completos.

---

## 6. Flujo de Votación (Porra — MVP-05)

> **MVP-05 reemplaza MVP-04**: los picks ahora son **free-form** (no dependen del formato). El admin crea la **porra** de su peña para la jornada, fija el formato + presupuesto, y los miembros votan a su manera. El admin luego ve la propuesta de la media y confirma el boleto final.

### 6.1 Estructura del pick (MVP-05: free-form, no `quini_config`)

⚠️ **Breaking change MVP-05**: el `quini_config` per-jornada está **removido**. Cada miembro puede votar 1, 2, o 3 signos en cualquier partido 1-14 **sin importar el formato** (D, T) de la porra. El formato solo rige el BOLETO FINAL que el admin guarda.

```typescript
// Para partidos 1-14 (free-form — 1, 2, o 3 signos, sin restricción)
type PickPartido = { partido_orden: number; signs: string[] };  // 1-3 elementos únicos ∈ {"1","X","2"}

// Para partido 15 (pleno) — shape MVP-05: {local, visitante} ∈ {"0","1","2","M"}
type PickPleno = {
  partido_orden: 15;
  pleno: { local: '0'|'1'|'2'|'M'; visitante: '0'|'1'|'2'|'M' };
};

type Pick = PickPartido | PickPleno;
```

El endpoint `POST /groups/{gid}/porras/{pid}/picks` espera `{picks: Pick[]}` con exactamente 15 items cubriendo `partido_orden` 1..15.

### 6.2 El pleno al 15 (multi-sign) — la parte tricky

El partido 15 es especial: el usuario puede marcar VARIANTES en cada columna M0/M1/M2.

**Variantes válidas** (Quiniela estándar):
- **M0** (1 variante): `["0-0"]`
- **M1** (5 variantes): `["0-1", "0-2", "1-0", "1-1", "1-2"]`
- **M2** (7 variantes): `["2-0", "2-1", "2-2", "3-0", "3-1", "3-2", "M"]` (M = "más de 3 goles")

**UI recomendada para M0/M1/M2**:
```
Pleno al 15: Real Madrid vs Barcelona

M0 (sin goles)        M1 (1 gol)         M2 (2+ goles)
☐ 0-0                 ☐ 0-1              ☐ 2-0
                      ☐ 0-2              ☐ 2-1
                      ☐ 1-0              ☐ 2-2
                      ☐ 1-1              ☐ 3-0
                      ☐ 1-2              ☐ 3-1
                                         ☐ 3-2
                                         ☐ M (4+)
```

**Validación del cliente** (antes de mandar al server):
- 15 picks exactos
- `partido_orden` 1..15 (no duplicados, no gaps)
- Para 1-14: `signs` es una lista de 1, 2, o 3 elementos distintos ∈ {"1","X","2"}. **Free-form** — la longitud NO depende del formato de la porra (D, T). El formato solo gobierna el BOLETO FINAL.
- Para 15: `pleno` con `local` + `visitante`, ambos ∈ {"0","1","2","M"}. **No** `pleno_signs` viejo.

**Recomendación UX**: el form de picks debe permitir editar ANTES de submit. Un solo "Guardar" al final. No autosave.

### 6.3 Submit

```javascript
const response = await api(`/groups/${gid}/porras/${pid}/picks`, {
  method: 'POST',
  body: { picks: [/* 15 picks (MVP-05 free-form) */] },
});
// response.members = [..., {voted: true, picks: null} ...]  // picks null si show_picks=false
// El admin puede pedir luego la propuesta de la media:
// await api(`/groups/${gid}/porras/${pid}/propuesta`, {method: 'POST'})
```

**Idempotente**: si el user ya votó, sobreescribe sus picks anteriores. El botón "Guardar" puede presionarse múltiples veces sin problema.

### 6.4 Ver picks propios

```javascript
const myPicks = await api(`/groups/${gid}/jornadas/${jid}/picks/me`);
// 404 si no ha votado todavía
```

### 6.5 Ver picks de todos (respeta `show_picks`)

```javascript
const { show_picks, members } = await api(`/groups/${gid}/jornadas/${jid}/picks`);
// show_picks: bool (qué estaba seteado al momento del request)
// members: [{ user_id, display_name, voted, voted_at, picks (o null) }]
```

**UI**:
- Si `show_picks=false`: mostrar "X votó / Y no ha votado" (sin los picks)
- Si `show_picks=true`: mostrar los 15 picks de cada uno
- `picks: null` significa "no votó" o "show_picks=false" — no se puede distinguir del lado cliente, depende del context

**Recomendación**: refrescar el `show_picks` después de que el admin lo cambie (el toggle es asíncrono — el admin lo cambia, los demás ven el cambio en el próximo request).

---

## 7. Generar el Consenso (admin)

```javascript
// Botón "Generar boleto consensuado" (solo admin)
async function generateConsensus(gid, jid) {
  try {
    const consensus = await api(`/groups/${gid}/jornadas/${jid}/consensus`, {
      method: 'POST',
    });
    return consensus;  // 200 con el reporte completo
  } catch (err) {
    if (err.status === 409) {
      // Faltan voters — err.body.missing_voters tiene la lista
      return { missing: err.body.missing_voters };
    }
    throw err;
  }
}
```

**Flow UX**:
1. Admin click "Generar consenso"
2. Si 200 → mostrar el reporte con el boleto y el ranking
3. Si 409 → mostrar modal "Falta que voten: Bob, Carol" con botón "Recordar" (enviar notificación — futuro)

El reporte trae:
- `partidos[]` con `vote_counts` (cuántos por signo) y `consensus` (el/los signos ganadores)
- Para el pleno, `consensus.pleno` tiene M0/M1/M2 con la unión de variantes

---

## 8. Subida de imágenes (logos, avatars)

### 8.1 Subir logo de equipo (admin)

```javascript
async function uploadTeamLogo(teamId, file) {
  const form = new FormData();
  form.append('file', file);  // File object del <input type="file">
  const r = await fetch(`/admin/teams/${teamId}/logo`, {
    method: 'POST',
    headers: { Authorization: `Bearer ${token}` },  // NO Content-Type, lo pone el browser
    body: form,
  });
  if (!r.ok) {
    const err = await r.json().catch(() => ({}));
    throw Object.assign(new Error(err.detail), { status: r.status });
  }
  return r.json();  // { team_id, logo_url, size }
}
```

**Validación del cliente (antes de subir)**:
- Max 5 MB (`file.size <= 5 * 1024 * 1024`)
- Tipo: `file.type` ∈ {`image/png`, `image/jpeg`, `image/webp`}
- **No validar magic bytes** del lado cliente (es trabajo del server)

**Preview**: usar `URL.createObjectURL(file)` para mostrar preview antes de subir.

### 8.2 Subir avatar (user)

Igual que el team logo pero `POST /users/me/avatar`. El backend actualiza el `user.avatar_url` del usuario autenticado.

### 8.3 Mostrar imágenes

- **Si el `logo_url` o `avatar_url` es null/absent** → mostrar placeholder (icon genérico).
- **Si es relative path** (caso `STORAGE_BACKEND=local` con `STORAGE_PUBLIC_BASE_URL` configurado) → ya es URL completa, usala tal cual.
- **Si es URL de S3 / R2 / MinIO** → también ya es completa (con o sin dominio custom).

En resumen: **siempre** usá el valor del campo directamente como `src` de `<img>`. El backend se encarga de devolver URLs absolutas.

### 8.4 DELETE

```javascript
DELETE /admin/teams/${teamId}/logo   // 204
DELETE /users/me/avatar                // 204
```

---

## 9. CORS y desarrollo local

### 9.1 CORS en dev

En `app/core/config.py`, `cors_origins` está configurado como `["http://localhost:3000"]` por default. Si tu frontend corre en otro puerto/host, agregalo a `.env`:

```bash
CORS_ORIGINS=["http://localhost:3000","http://localhost:5173"]
```

### 9.2 Setup de local

```bash
# Terminal 1: API
cd la-goyniela
docker compose up -d
uv run alembic upgrade head
uv run python -m app.scripts.load_team_catalog
uv run uvicorn app.main:app --reload --port 8000

# Terminal 2: tu frontend
cd mi-frontend
npm run dev  # o lo que uses
```

Por default:
- API: `http://localhost:8000`
- Docs: `http://localhost:8000/docs`
- Frontend (Vite default): `http://localhost:5173`
- Frontend (CRA/Next default): `http://localhost:3000`

### 9.3 Variables de entorno del frontend

```bash
# .env del frontend
VITE_API_BASE_URL=http://localhost:8000
# o
REACT_APP_API_BASE_URL=http://localhost:8000
# o
NEXT_PUBLIC_API_BASE_URL=http://localhost:8000
```

---

## 10. Performance y caching

### 10.1 Lo que podés cachear localmente

- **Lista de equipos** (423 entries, casi nunca cambia) — cachear en localStorage al primer load
- **Lista de competiciones** (10 entries, cambia 1 vez al año) — cachear
- **Catálogo de variantes del pleno** (M0/M1/M2) — constante, hardcodear

### 10.2 Lo que NO debés cachear

- **Picks** — siempre fetch fresh
- **Jornadas activas** — refresh cada vez que el user entra al grupo
- **Token del user** — solo en localStorage, sin caché en memoria persistente

### 10.3 Polling (no hay websockets)

La API no tiene websockets ni server-sent events. Si querés "live updates" (ej. mostrar cuando alguien vota en tiempo real), tenés dos opciones:
- **Polling cada N segundos** (N=10 para grupos activos, N=60 para pasivos)
- **WebSockets propios** (no recomendado, fuera de scope MVP-02)

Recomendación: polling cada 30s cuando el user está en la vista de un grupo. Limpiar el interval al desmontar.

### 10.4 Tamaño de respuestas

- `GET /api/v1/teams` sin filtros: 423 entries ≈ 80 KB JSON. OK para fetch único + cache.
- `GET /jornadas` con 50 entries: ~20 KB. OK.
- `GET /groups/{id}/jornadas/{id}/picks` con 5 miembros: ~50 KB. OK.

No necesitás paginación agresiva en MVP-02.

---

## 11. Gotchas (cosas que muerden)

### 11.1 Token expira en 24h sin warning

El JWT expira silenciosamente. El user va a ver errores 401 de un día para otro. **Recomendación UX**: en el login screen, mostrar "Tu sesión expira en X horas" proactivamente. O: implementar refresh tokens (no está en MVP-02, queda para v2).

### 11.2 El `join_code` es case-sensitive

"A3F9KX" ≠ "a3f9kx". El generador de la API usa uppercase siempre, pero validalo del lado cliente.

### 11.3 `show_picks` es un toggle GLOBAL del grupo

Cuando el admin lo cambia, TODOS los miembros ven el cambio. No hay per-user override. Diseñá la UI asumiendo esto (no "mi visibilidad").

### 11.4 PATCH /groups/{id} solo edita `show_picks`

Si mandás `{ name: "nuevo" }`, Pydantic rechaza con 422 (`extra="forbid"`). El cliente debe saber que para cambiar name/description hay que usar otro endpoint (que no existe aún en MVP-02 — agregalo cuando MVP-03).

### 11.5 Picks son free-form (MVP-05); pleno es `{local, visitante}` (no `pleno_signs`)

**MVP-04 → MVP-05**: el `quini_config` per-jornada está **removido**. Los picks son **free-form** (1, 2, o 3 signos en cualquier partido 1-14 sin importar el formato de la porra). El formato (D, T) solo gobierna el BOLETO FINAL que el admin guarda.

Partido 15: el shape es ahora `{local, visitante}` con cada valor ∈ `{"0","1","2","M"}`. El viejo `pleno_signs: {M0, M1, M2}` está **removido**.

```typescript
// ANTES (MVP-02) — YA NO FUNCIONA
{"partido_orden": 1, "sign": "1"}  // → 422: "Extra inputs are not permitted"

// AHORA (MVP-04)
{"partido_orden": 1, "signs": ["1"]}
{"partido_orden": 5, "signs": ["1", "X"]}  // doble (si el config lo pide)
{"partido_orden": 3, "signs": ["1", "X", "2"]}  // triple (si el config lo pide)
```

Si mandás un pick de tipo equivocado para el partido, Pydantic 422. Validá en cliente antes de mandar. El detail del 422 también menciona el rename (`"MVP-04: field 'sign' was renamed to 'signs', now a list"`), pero solo si omitís `signs` totalmente.

### 11.6 El campo `competicion_raw` de `composicion` siempre es `null`

No lo muestres en la UI. El doc lo aclara pero es fácil caer si ves el shape completo del schema.

### 11.7 El partido 15 del backend usa `orden` y `num`, no `posicion`

En `JornadaLosilla.partidos` el campo es `orden` y `num`. En `ComposicionOut` (nuestra tabla) es `posicion`. Si usás datos de dos endpoints distintos, mapeá correctamente.

### 11.8 El consensus siempre devuelve las 3 M-keys del pleno, aunque estén vacías

```json
{
  "consensus": {
    "pleno": {
      "M0": ["0-0"],
      "M1": [],
      "M2": []    // ← siempre presente
    }
  }
}
```

No asumas que si la key falta, no hay picks. Si falta, es bug del backend.

### 11.9 Imágenes: el cliente NO maneja `multipart/form-data` boundaries

Cuando uses `fetch` con `FormData`, **NO seteés `Content-Type`** manualmente. El browser lo setea automáticamente con el boundary. Si lo seteás vos, no funciona.

```javascript
// ✓ BIEN
fetch(url, { method: 'POST', body: formData });

// ✗ MAL
fetch(url, {
  method: 'POST',
  headers: { 'Content-Type': 'multipart/form-data; boundary=...' },
  body: formData
});
```

### 11.10 Idempotencia de POST /picks

El endpoint es idempotente (segundo submit sobreescribe). Si el user click "Guardar" dos veces, no se rompe. **Pero** no es un upsert de "picks parciales": si mandás 14 picks, los 14 sobreescriben los 14 anteriores, pero el pick 15 queda intacto. **Siempre mandá los 15 picks juntos**.

### 11.11 `/jornadas` embebe `TeamSummary`, así que `/api/v1/teams` solo hace falta para el catálogo global

`GET /jornadas/{id}`, `GET /groups/{gid}/jornadas/{jid}/picks` y `/picks/me` incluyen `local_team` y `visitante_team` (un `TeamSummary` con `id`, `entity_id`, `canonical_name`, `short_name`, `gender`, `country`, `logo_url`) en cada partido, suficiente para dibujar el boleto entero en **un solo request**. **No hace falta** llamar `/api/v1/teams/{id}` 30 veces para conseguir los logos.

`/api/v1/teams` y `/api/v1/teams/{id}` siguen existiendo pero quedan para el catálogo global (búsqueda, filtros por competition/gender/country, dropdowns de admin). Traete ese catálogo una vez al abrir la app y cachealo localmente — son ~423 entries.

Si la fila del equipo fue borrada pero `composicion` todavía la referencia (escenario raro, el FK es RESTRICT), el embed viene `null` y caés al fallback `local_team_id` + `local_raw` para mostrar algo.

---

## 12. Checklist de integración

Antes de mergear el cliente, validá:

- [ ] Auth flow: register, login, logout, Google, X. Token persiste en localStorage.
- [ ] Fetch wrapper centralizado (header Authorization + manejo de 401).
- [ ] Manejo de errores unificado (`detail` siempre se muestra).
- [ ] Crear peña: muestra `join_code` con botón "Copiar" y "Compartir".
- [ ] Unirse a peña: input de 6 chars, validación local + server.
- [ ] Ver grupos del user: `GET /groups` en el dashboard.
- [ ] Ver detalle del grupo: `GET /groups/{id}` con members.
- [ ] Lista de equipos: cachear localmente, autocompletar en picks form.
- [ ] Picks form: 15 inputs, validación local (sign vs pleno_signs), preview antes de submit.
- [ ] Picks submit: idempotente, mostrar éxito o detalle de error.
- [ ] Toggle `show_picks`: solo admin, refleja inmediatamente en la vista de picks.
- [ ] Consensus: solo admin, muestra `missing_voters` si 409, muestra reporte si 200.
- [ ] Upload logo: validación local (size, type), preview, muestra URL retornada.
- [ ] Upload avatar: igual, con `user.avatar_url` actualizado.
- [ ] Polish: loading states, error states, empty states, optimistic updates donde aplique.
- [ ] CORS configurado para tu dev origin en `app/core/config.py`.
- [ ] Variables de entorno del frontend apuntan a la API correcta.
- [ ] No mostrar `competicion_raw` en la UI (siempre es null).
- [ ] No asumir keys del pleno faltantes — siempre 3 keys M0/M1/M2.
- [ ] No setear `Content-Type` en uploads (deja que el browser lo haga).

---

## 13. Recursos

- **API reference**: [`docs/API.md`](./API.md) — cada endpoint con curl + JSON shape
- **Swagger UI**: `http://localhost:8000/docs` — interactivo, botón "Try it out"
- **OpenAPI JSON**: `http://localhost:8000/openapi.json` — para generar SDKs con openapi-generator
- **Source del backend**: `app/api/v1/*.py` y `app/schemas/*.py`
- **Estado del proyecto**: `docs/proyecto/PROJECT-ASSESSMENT.md` — qué está hecho y qué falta
- **Design del scheduler**: `docs/proyecto/scheduler.md` — para entender el ciclo de ingesta (útil para debugging)
