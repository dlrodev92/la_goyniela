# Boleto Modes & PDF — Integración del cliente

**Para**: equipo del cliente Next.js (`la_goyniela_client`).
**Versión API**: 0.10.x (sprint boleto-modes + PDF aplicado oct 2026).
**Backend en producción local**: `http://localhost:8000`.
**OpenAPI vivo**: `http://localhost:8000/docs`.

> **Antes de empezar**: leete [`docs/CLIENT.md`](./CLIENT.md) y [`docs/API.md`](./API.md) primero. Este doc cubre específicamente el **sprint boleto-modes** (oct 2026) y el endpoint de PDF. Para el superadmin-panel, leete `docs/HANDOFF-NEXT-SESSION.md`.

---

## 0. Resumen ejecutivo

El sprint boleto-modes agrega:

1. **4 modos de porra** (`modo` enum): `multiple` (legacy), `sencilla` (N columnas simples), `reducida` (R1–R6 oficial), `intuicion` (N columnas con fijos + variables).
2. **Boleto N-columnas** — el storage pasa de 1 columna a N columnas. Cambia el shape de `PUT /boleto` y `GET /boleto` (con compat hacia atrás).
3. **Budget advisor** — solo para `modo='intuicion'`. Devuelve las opciones `(columnas, fijos)` que entran en un presupuesto.
4. **PDF del boleto** — `GET /boleto.pdf` devuelve el boleto como PDF descargable.

**Suite backend**: `375/375 tests verde` ✅

---

## 1. El enum `PorraModo` (4 valores)

La porra ahora tiene un campo `modo: PorraModo` que determina cómo se juega:

```ts
type PorraModo = "multiple" | "sencilla" | "reducida" | "intuicion";
```

| Modo | Qué es | Campos requeridos | Tabla de予算 advisor |
|---|---|---|---|
| `multiple` (default) | 1 columna con D dobles + T triples (legacy MVP-04/05) | `dobles`, `triples` | No |
| `sencilla` | N columnas independientes de simples | `columnas: int` (N, 2–12) | No |
| `reducida` | 1 columna de una de las reducciones oficiales R1–R6 | `reduccion: "R1"\|"R2"\|...\|"R6"` | No |
| `intuicion` | N columnas donde F partidos están fijos (mismo signo en todas) y V=14-F varían | `fijos: int`, `columnas: int` | **Sí** |

> **El budget advisor SOLO aplica a `intuicion`**. Los otros modos el admin los configura directamente.

---

## 2. Storage del boleto: N columnas (BREAKING para el response)

El boleto se guarda como una **lista de columnas**. Cada columna tiene 14 rows (uno por partido 1–14).

### 2.1 Antes vs ahora

```diff
// ❌ Antes (legacy): 1 columna hardcoded
{
  "matches": [
    { "partido_orden": 1, "signs": ["1"] },
    { "partido_orden": 2, "signs": ["1", "X"] },
    ...
  ],
  "pleno": { "local": "1", "visitante": "M" }
}

// ✅ Ahora: N columnas explícitas
{
  "columnas": [
    {
      "matches": [
        { "partido_orden": 1, "signs": ["1"] },
        { "partido_orden": 2, "signs": ["1", "X"] },
        ...14 rows...
      ]
    },
    // ... más columnas si modo in ['sencilla', 'intuicion']
  ],
  "pleno": { "local": "1", "visitante": "M" }
}
```

### 2.2 Compat con clientes viejos

Si seguís mandando `matches: [14]` en `PUT /boleto`, el backend lo acepta como **alias de una sola columna** (lo wrappea en `columnas: [{matches: [...]}]` internamente). El **response** ya viene en el shape nuevo (`columnas: [...]`).

Recomendación: migrá el cliente al shape nuevo apenas puedas. El alias se va a quitar en un sprint futuro.

---

## 3. Creación de porra — `POST /porras`

### 3.1 Request (modo `intuicion`)

```http
POST /api/v1/groups/{g}/porras
{
  "jornada_id": 1,
  "budget_eur": "5.00",
  "modo": "intuicion",
  "fijos": 10,
  "columnas": 6,
  "precio_apuesta_eur": "0.75"
}
```

**Validaciones** (422 con mensaje claro):
- `fijos` y `columnas` obligatorios cuando `modo='intuicion'`
- `0 ≤ fijos ≤ 13`, `2 ≤ columnas ≤ 12`
- `columnas × precio_apuesta_eur ≤ budget_eur` (con `precio_apuesta_eur=0.75` por default si no se manda)
- `fijos + variables = 14` (donde `variables = 14 - fijos`)

### 3.2 Request (modo `reducida`)

```http
POST /api/v1/groups/{g}/porras
{
  "jornada_id": 1,
  "budget_eur": "50.00",
  "modo": "reducida",
  "reduccion": "R4"
}
```

**Tabla de reducciones oficiales** (validar contra loteriasyapuestas.es antes de shipping):

| Code | Dobles | Triples | Apuestas jugadas | Coste @ 0.75€ |
|---|---|---|---|---|
| `R1` | 0 | 4 | 9 | 6.75€ |
| `R2` | 7 | 0 | 16 | 12.00€ |
| `R3` | 3 | 3 | 24 | 18.00€ |
| `R4` | 6 | 2 | 64 | 48.00€ |
| `R5` | 0 | 8 | 81 | 60.75€ |
| `R6` | 11 | 0 | 132 | 99.00€ |

### 3.3 Request (modo `sencilla`)

```http
POST /api/v1/groups/{g}/porras
{
  "jornada_id": 1,
  "budget_eur": "5.00",
  "modo": "sencilla",
  "columnas": 4,
  "precio_apuesta_eur": "0.75"
}
```

### 3.4 Request (modo `multiple` — legacy, sigue igual)

```http
POST /api/v1/groups/{g}/porras
{
  "jornada_id": 1,
  "budget_eur": "5.00",
  "modo": "multiple",  // o simplemente omitir
  "dobles": 1,
  "triples": 0,
  "precio_apuesta_eur": "0.75"
}
```

### 3.5 Response

Todos los modos devuelven `PorraOut` con los nuevos campos:

```json
{
  "id": 42,
  "group_id": 1,
  "jornada_id": 1,
  "modo": "intuicion",
  "fijos": 10,
  "columnas": 6,
  "dobles": 0,
  "triples": 0,
  "precio_apuesta_eur": "0.75",
  "budget_eur": "5.00",
  "columns": 6,
  "cost_eur": "4.50",
  "has_boleto": false,
  ...
}
```

---

## 4. PATCH porra — `PATCH /porras/{id}`

Permite cambiar el modo, los fijos, las columnas, los dobles/triples, etc. Si ya hay un boleto guardado, **el modo y el formato NO se pueden cambiar** (422 con mensaje). Para cambiar el formato con boleto existente: `DELETE /boleto` primero, después PATCH, después `PUT /boleto` con el nuevo formato.

**Validación post-merge** (post-patch state):
- `modo='intuicion'`: `fijos`, `columnas` requeridos, costo ≤ budget
- `modo='multiple'`: `dobles + triples ≤ 14`, `not (0, 0)`, costo ≤ budget
- `modo='sencilla'`: `columnas` requerido, `fijos`/`dobles`/`triples` deben ser 0
- `modo='reducida'`: `reduccion` requerida, `dobles`/`triples` se derivan de la tabla

---

## 5. Budget advisor (solo `intuicion`)

### 5.1 Endpoint

```http
GET /api/v1/groups/{g}/porras/intuicion-options?budget_eur=5.00
```

- **Auth**: cualquier miembro del grupo (no requiere admin)
- **Query**: `budget_eur` (decimal, gt 0, le 10000, 2 decimales)

### 5.2 Response

```json
{
  "budget_eur": "5.00",
  "options": [
    { "columnas": 6, "fijos": 10, "variables": 4, "cost_eur": "4.50" },
    { "columnas": 5, "fijos": 11, "variables": 3, "cost_eur": "3.75" },
    { "columnas": 4, "fijos": 11, "variables": 3, "cost_eur": "3.00" },
    { "columnas": 3, "fijos": 12, "variables": 2, "cost_eur": "2.25" },
    { "columnas": 2, "fijos": 13, "variables": 1, "cost_eur": "1.50" }
  ]
}
```

- `options` ordenado por `columnas` DESC (más cobertura primero)
- `variables = 14 - fijos` SIEMPRE
- Solo se devuelven opciones con `cost_eur ≤ budget_eur`

### 5.3 Tabla completa (cuando hay budget para todas)

| Columnas | Fijos | Variables | Coste @ 0.75€ |
|---|---|---|---|
| 2 | 13 | 1 | 1.50€ |
| 3 | 12 | 2 | 2.25€ |
| 4 | 11 | 3 | 3.00€ |
| 5 | 11 | 3 | 3.75€ |
| 6 | 10 | 4 | 4.50€ |
| 7 | 10 | 4 | 5.25€ |
| 8 | 9 | 5 | 6.00€ |
| 9 | 9 | 5 | 6.75€ |
| 10 | 8 | 6 | 7.50€ |
| 11 | 8 | 6 | 8.25€ |
| 12 | 7 | 7 | 9.00€ |

---

## 6. PUT /boleto — el boleto con N columnas

### 6.1 Request (modo `intuicion`)

```http
PUT /api/v1/groups/{g}/porras/{p}/boleto
{
  "columnas": [
    {
      "matches": [
        { "partido_orden": 1, "signs": ["1"] },
        { "partido_orden": 2, "signs": ["1"] },
        { "partido_orden": 3, "signs": ["X"] },
        { "partido_orden": 4, "signs": ["1"] },
        { "partido_orden": 5, "signs": ["1"] },
        { "partido_orden": 6, "signs": ["1"] },
        { "partido_orden": 7, "signs": ["1"] },
        { "partido_orden": 8, "signs": ["1"] },
        { "partido_orden": 9, "signs": ["1"] },
        { "partido_orden": 10, "signs": ["1"] },
        { "partido_orden": 11, "signs": ["2"] },
        { "partido_orden": 12, "signs": ["1"] },
        { "partido_orden": 13, "signs": ["1"] },
        { "partido_orden": 14, "signs": ["1"] }
      ]
    },
    // ... 5 columnas más (total 6) ...
  ],
  "pleno": { "local": "0", "visitante": "0" }
}
```

### 6.2 Validación por modo

| `modo` | Reglas |
|---|---|
| `multiple` | Exactamente 1 columna. La columna debe tener D rows con 2 signos, T rows con 3, el resto con 1. |
| `reducida` | Exactamente 1 columna. La columna debe tener D dobles + T triples según la tabla R1–R6. |
| `sencilla` | Exactamente N columnas (= `porra.columnas`). Cada columna tiene 14 rows con 1 signo cada una. |
| `intuicion` | Exactamente N columnas (= `porra.columnas`). Cada partido fijo (1..F) tiene el mismo signo en TODAS las columnas. Variables pueden variar. |

**Error típico** (422):
```json
{
  "detail": "intuicion: fijo partido 5 must have the same sign in all columns, got ['1', 'X', '1', '1', '1', '1']"
}
```

### 6.3 Alias legacy

```http
PUT /api/v1/groups/{g}/porras/{p}/boleto
{
  "matches": [ /* 14 rows */ ],
  "pleno": { ... }
}
```

Se acepta como `columnas: [{matches: [...]}]` (1 columna). **Migrá al shape nuevo** lo antes posible.

### 6.4 Response

`BoletoOut` con el shape nuevo:
```json
{
  "porra_id": 42,
  "modo": "intuicion",
  "set_at": "2026-10-08T18:33:00+00:00",
  "set_by_user_id": 7,
  "columnas": [
    { "matches": [ /* 14 rows */ ] },
    { "matches": [ /* 14 rows */ ] },
    ...
  ],
  "pleno": { "local": "0", "visitante": "0" },
  "columns": 6,
  "cost_eur": "4.50",
  "reduccion": null
}
```

---

## 7. PDF del boleto — `GET /boleto.pdf`

```http
GET /api/v1/groups/{g}/porras/{p}/boleto.pdf
```

- **Auth**: cualquier miembro del grupo
- **Response** (200, `Content-Type: application/pdf`):
  ```
  Content-Disposition: attachment; filename="boleto_j11_g1_p42.pdf"
  %PDF-1.3
  ... (1 página A4) ...
  ```
- **Errores**:
  - `404`: no hay boleto guardado todavía
  - `403`: el caller no es miembro del grupo
  - `200`: boleto renderizado

### 7.1 Layout del PDF

```
LA QUINIELA - Boleto oficial
Grupo: La Peña Test   Jornada: 11   Temporada: 26/27

Partido    Enfrentamiento              Signo(s)
1          Real Madrid vs Barcelona        1
2          Atletico vs Sevilla             X
...
14         Valencia vs Villarreal          2
Pleno al 15:  0 - 0

Coste: 1.50 EUR   Columnas: 2   Precio/columna: 0.75 EUR
Generado: 2026-10-08 18:33 UTC   Porra ID: 42
```

**Para N columnas (sencilla/intuicion)**: el renderer las acomoda 3 por fila con header "Columna N" arriba de cada grupo. Para 6 columnas: 2 filas. Para 12: 4 filas. Todo en una sola página A4.

### 7.2 Manejo en TypeScript

```ts
async function downloadBoletoPdf(gid: number, pid: number, token: string) {
  const r = await fetch(`/api/v1/groups/${gid}/porras/${pid}/boleto.pdf`, {
    headers: { Authorization: `Bearer ${token}` },
  });

  if (!r.ok) {
    if (r.status === 404) {
      toast.error("Guardá el boleto antes de descargarlo");
    } else if (r.status === 403) {
      toast.error("No sos miembro del grupo");
    } else {
      toast.error(`Error: ${r.status}`);
    }
    return;
  }

  // El response es BINARIO, no JSON. Usar Blob + URL.createObjectURL.
  const blob = await r.blob();
  const url = URL.createObjectURL(blob);

  // Opción 1: descargar
  const a = document.createElement("a");
  a.href = url;
  a.download = `boleto_g${gid}_p${pid}.pdf`;
  a.click();

  // Opción 2: abrir en nueva pestaña (preview)
  // window.open(url, "_blank");

  URL.revokeObjectURL(url);
}
```

### 7.3 Limitación de acentos

El PDF usa Helvetica (Latin-1) por default. Acentos en nombres de equipo (`Atlético`, `Málaga`) se renderizan como `?`. Si el cliente necesita acentos correctos, hay que registrar una fuente TTF (ej. DejaVu Sans). **Fuera del alcance de este sprint** — flagueado para iteración futura.

---

## 8. TypeScript types sugeridos

```ts
// types/api.ts

export type PorraModo = "multiple" | "sencilla" | "reducida" | "intuicion";
export type Sign = "1" | "X" | "2";
export type PlenoValue = "0" | "1" | "2" | "M";

// ---- Porra ----

export interface PorraBase {
  id: number;
  group_id: number;
  jornada_id: number;
  budget_eur: string;        // Decimal as string
  precio_apuesta_eur: string; // Decimal as string
  status: "open" | "closed";
  created_at: string;
  updated_at: string;
  columns: number;           // apuestas jugadas (computed)
  cost_eur: string;          // computed
  has_boleto: boolean;
}

export interface PorraOut extends PorraBase {
  modo: PorraModo;
  fijos: number | null;      // only intuicion
  columnas: number | null;   // only intuicion/sencilla
  dobles: number;            // only multiple/reducida
  triples: number;           // only multiple/reducida
  reduccion: string | null;  // only reducida
}

// ---- Porra create/patch (discriminated union) ----

export interface PorraCreateInBase {
  jornada_id: number;
  budget_eur: string;
  precio_apuesta_eur?: string;
}

export interface PorraCreateInMultiple extends PorraCreateInBase {
  modo?: "multiple";         // default si se omite
  dobles: number;
  triples: number;
}

export interface PorraCreateInSencilla extends PorraCreateInBase {
  modo: "sencilla";
  columnas: number;          // 2..12
}

export interface PorraCreateInReducida extends PorraCreateInBase {
  modo: "reducida";
  reduccion: "R1" | "R2" | "R3" | "R4" | "R5" | "R6";
}

export interface PorraCreateInIntuicion extends PorraCreateInBase {
  modo: "intuicion";
  fijos: number;             // 0..13
  columnas: number;          // 2..12
}

export type PorraCreateIn =
  | PorraCreateInMultiple
  | PorraCreateInSencilla
  | PorraCreateInReducida
  | PorraCreateInIntuicion;

// PATCH acepta los mismos campos, todos opcionales:
export type PorraPatchIn = Partial<PorraCreateIn>;

// ---- Boleto ----

export interface BoletoMatch {
  partido_orden: number;     // 1..14
  signs: Sign[];             // 1, 2, o 3 elementos
}

export interface BoletoColumna {
  matches: BoletoMatch[];    // exactamente 14
}

export interface PlenoIn {
  local: PlenoValue;
  visitante: PlenoValue;
}

// Body para PUT /boleto
export interface BoletoInV2 {
  columnas: BoletoColumna[];   // 1+ columnas
  pleno: PlenoIn;
}

// Alias legacy (sigue funcionando)
export interface BoletoInLegacy {
  matches: BoletoMatch[];      // 14 rows = 1 columna
  pleno: PlenoIn;
}

export type BoletoIn = BoletoInV2 | BoletoInLegacy;

// Response de GET /boleto (siempre el shape nuevo)
export interface BoletoOut {
  porra_id: number;
  modo: PorraModo;
  set_at: string;
  set_by_user_id: number | null;
  columnas: BoletoColumna[];
  pleno: PlenoIn;
  columns: number;            // cuántas columnas tiene el boleto
  cost_eur: string;
  reduccion: string | null;   // solo si modo='reducida'
}

// ---- Budget advisor ----

export interface IntuicionOptionOut {
  columnas: number;
  fijos: number;
  variables: number;          // = 14 - fijos
  cost_eur: string;
}

export interface IntuicionOptionsOut {
  budget_eur: string;
  options: IntuicionOptionOut[];
}

// ---- Reducciones ----

export type ReduccionCode = "R1" | "R2" | "R3" | "R4" | "R5" | "R6";

export interface ReduccionSpec {
  code: ReduccionCode;
  dobles: number;
  triples: number;
  apuestas: number;          // columnas que juega la reducción
  cost_at_075: number;       // = apuestas * 0.75
}
```

---

## 9. Flujos UI sugeridos

### 9.1 Crear porra con modo intuicion (paso a paso)

```
┌─ Crear porra — J11 ───────────────────────────┐
│                                              │
│  Modo:                                       │
│  ( ) Múltiple   ( ) Sencilla                 │
│  ( ) Reducida   (●) Intuición (personal)     │
│                                              │
│  ── Modo intuición ──                         │
│                                              │
│  Presupuesto: [  5.00 €  ▼]                 │
│  ┌─ Opciones del budget advisor ─────────┐  │
│  │ ● 6 columnas · 10 fijos · 4.50€      │  │
│  │ ○ 5 columnas · 11 fijos · 3.75€      │  │
│  │ ○ 4 columnas · 11 fijos · 3.00€      │  │
│  │ ○ 3 columnas · 12 fijos · 2.25€      │  │
│  │ ○ 2 columnas · 13 fijos · 1.50€      │  │
│  └───────────────────────────────────────┘  │
│  → GET /porras/intuicion-options?budget=5   │
│                                              │
│  [ Crear porra ]                             │
│  → POST /porras { modo, fijos, columnas }    │
│                                              │
│  ─ Tras crear ─                              │
│  [ Generar propuesta ]  [ Editar formato ]   │
│  → POST /propuesta                           │
│                                              │
│  [ Rellenar columnas ] (UI grid N×14)        │
│  ─ Fijo 1: [1 ▼]   Fijo 2: [1 ▼] ...       │
│  ─ Variable 11: cada columna tiene un select│
│                                              │
│  [ Confirmar boleto oficial ]                │
│  → PUT /boleto { columnas: [...], pleno }   │
│                                              │
│  [ 📄 Descargar PDF ]                        │
│  → GET /boleto.pdf                           │
└──────────────────────────────────────────────┘
```

### 9.2 Crear porra con modo reducida (más simple)

```
┌─ Crear porra — J11 ───────────────────────────┐
│                                              │
│  Modo:                                       │
│  ( ) Múltiple   ( ) Sencilla                 │
│  (●) Reducida   ( ) Intuición                │
│                                              │
│  ── Modo reducida ──                         │
│                                              │
│  Reducción oficial:                          │
│  ┌────────────────────────────────────────┐  │
│  │ R1: 0D+4T, 9 apuestas,   6.75€        │  │
│  │ R2: 7D+0T, 16 apuestas, 12.00€        │  │
│  │ R3: 3D+3T, 24 apuestas, 18.00€        │  │
│  │ R4: 6D+2T, 64 apuestas, 48.00€        │  │
│  │ R5: 0D+8T, 81 apuestas, 60.75€        │  │
│  │ R6: 11D+0T, 132 apuestas, 99.00€      │  │
│  └────────────────────────────────────────┘  │
│  Seleccionada: R4 (64 apuestas, 48.00€)       │
│                                              │
│  [ Crear porra ]                             │
│  → POST /porras { modo:'reducida',           │
│                   reduccion:'R4' }            │
│  (D=6, T=2, columns=64 derivados)             │
│                                              │
│  [ Confirmar boleto oficial ]                │
│  → PUT /boleto { columnas: [{matches:...}],  │
│                   pleno } (64 columnas × 14) │
└──────────────────────────────────────────────┘
```

---

## 10. Migración del cliente

### 10.1 Pasos

1. [ ] **Switch al shape `columnas: [...]` en el body de PUT /boleto** (el alias `matches: [...]` sigue funcionando, pero dejá de usarlo)
2. [ ] **Parsear el response nuevo** de GET /boleto con `columnas: [...]` (1+ entradas) en vez del viejo `matches: [14]`
3. [ ] **UI del editor**: cuando `modo='intuicion'` y N>1, renderizar N cuadrículas 14×1 (los `fijos` partidos son read-only con el mismo valor en todas)
4. [ ] **Botón "Descargar PDF"** en la pantalla del boleto confirmado, llamando a `GET /boleto.pdf` con `URL.createObjectURL` (no `r.json()`)
5. [ ] **Budget advisor** solo visible cuando `modo='intuicion'`. Para los otros modos, UI directa (D/T para multiple, R1-R6 para reducida, N para sencilla)
6. [ ] **TypeScript types**: copiar los de §8 a `types/api.ts`

### 10.2 Cosas que NO hay que tocar

- Los `signs: ["1"]`, `["1", "X"]`, `["1", "X", "2"]` siguen igual (1 = simple, 2 = doble, 3 = triple)
- El Pleno al 15 sigue siendo `PlenoIn { local, visitante }` con valores `"0"|"1"|"2"|"M"`
- El precio por columna sigue siendo 0.75€ en 2026/27 (leído de Losilla, no hardcoded)
- La validación de presupuesto (cost ≤ budget) la hace el backend, no el cliente
- La tabla R1-R6 puede cambiar de temporada — si pasa, el backend la actualiza y avisa (no hay acción del cliente)

---

## 11. Recursos

- [Diseño del gestor de jornadas](./producto/gestor-jornadas.md) — el sistema completo de gestión de la quiniela
- [API reference](./API.md) — todos los endpoints con shapes exactas
- [Guía de integración del cliente](./CLIENT.md) — auth flow, errores, patrones generales
- [OpenAPI interactivo](http://localhost:8000/docs) — siempre actualizado con el código
- [Handoff de sesión previo](./HANDOFF-NEXT-SESSION.md) — fixes pendientes, el sprint superadmin-panel, etc.
- [FRONTEND-IMPLEMENTATION.md del cliente](../la_goyniela_client/FRONTEND-IMPLEMENTATION.md) — el doc consolidado para el dev del cliente (incluye el sprint superadmin-panel)
