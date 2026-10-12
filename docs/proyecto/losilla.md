# La Goyniela — Integración con eduardolosilla.es

**Fecha**: 30 de septiembre de 2026
**Estado**: implementado y validado contra datos reales
**Complemento de**: `docs/research/02-fuentes.md` (filas Q2/Q3), `docs/proyecto/arquitectura.md`

Este documento describe el componente que trae de `eduardolosilla.es` los
datos que la Quiniela española publica tras cada jornada:

- **Recaudación** (estimada mientras la jornada está abierta, real al cierre)
- **Bote** acumulado (estimado / real / pendiente de repartir)
- **Escrutinio oficial** por categoría (acertantes y premio para 15, 14, 13,
  12, 11, 10, y Elige 8 cuando existe)
- **Comentarios editoriales** de Losilla para cada partido (típicamente 14
  de los 15; el partido 15 / Pleno al 15 no lleva editorial)

No cubre la compra ni el envío de apuestas — solo la lectura. El uso queda
encuadrado en las restricciones acordadas del proyecto (personal y amigos,
datos gratuitos).

## 1. Resumen de qué hay

```
app/schemas/losilla.py            6 modelos Pydantic v2
app/clients/eduardolosilla.py     Cliente httpx async + cache TTL + throttle
app/api/v1/losilla.py             3 endpoints GET + singleton lazy
app/main.py                       Router registrado, tag "losilla", lifespan

scripts/probe_losilla_escrutinio.py   Script de investigación (CLI one-off)

tests/test_losilla_client.py      15 tests unitarios + integración con httpx_mock
tests/test_losilla_endpoints.py   12 tests de los endpoints
tests/fixtures/losilla/*.html     5 fixtures HTML reales (J1, J8, J9, J11, J70)
```

El script CLI y el cliente HTTP resuelven el mismo problema con dos
enfoques distintos:

| Aspecto | Script (`scripts/probe_losilla_escrutinio.py`) | Cliente (`app/clients/eduardolosilla.py`) |
|---|---|---|
| Runtime | Python stdlib, urllib, sync | httpx async, integrable en FastAPI |
| Uso | Investigación puntual, CI one-shot | Producción, dentro de la API |
| Cache | Disco en `/tmp` | Memoria (TTL) + disco opcional |
| Concurrencia | No | Sí (httpx.AsyncClient) |
| Throttle | `time.sleep(0.4)` | `await asyncio.sleep(0.4)` |
| Salida | stdout + JSON a `--output` | Schema Pydantic |
| Errores | Exit codes + logs | HTTPException 502 |

El script se mantiene porque es útil para depurar y validar el parser sin
tener que arrancar la API.

## 2. Arquitectura del parser

La página `/quiniela/ayudas/escrutinio/jornada_<N>` es **Angular Universal
SSR**. La información que nos interesa vive en dos componentes distintos
del HTML servido:

### 2.1. JSON del estado Angular (`<script type="application/json">…</script>`)

Un bloque con todo el estado serializado. Contiene, entre otras cosas:

```json
{
  "datosGeneralesQuiniela": { … },
  "jornada_11_2027": {
    "jornada": 11,
    "temporada": 2027,
    "recaudacion": "1.200.000,00",
    "boteEstimacion": "0,00",
    "bote": 0.001,
    "textoBote": "282000",
    "boteData": { "idQuiniela": 1611, "unico_acertante": "282000", "bote": 0.001 },
    "costeApuesta": 0.75,
    "fechaFinApuestas": 1791025200,
    "fechaFinPenas": 1791023400,
    "partidos": [
      { "ordenar": 1, "num": 1, "local": "ALBACETE", "visitante": "EIBAR",
        "dia": "SAB", "hora": "14:00", "resultado": "null-null",
        "escudo_local": "2848.png", "escudo_visitante": "2839.png",
        "id_besoccer": 25307 },
      … 14 más …
    ]
  }
}
```

**Truco importante**: el bloque está escapado con `&q;` por cada `"` además
de las entidades HTML estándar. Antes de `json.loads` hay que hacer
`unescape(block).replace("&q;", '"')`.

**Limitación**: el JSON **siempre** contiene la jornada **en juego** (la
actual), no la pedida en la URL. Si abres `jornada_9`, el JSON trae la J11.
Por eso el cliente expone `JornadaLosilla.ssr_shipped_jornada` y un
`warning` para que el caller distinga.

### 2.2. Bloque HTML `<app-tabla-categorias>`

Cuando la jornada está cerrada y LAE ya publicó los datos, la página
renderiza un componente con la tabla real:

```
Recaudación 1.453.174,50 € Bote 0,00 €
Escrutinio de la Quiniela JORNADA 9 - 26/27 (26/09/2026)
Acertantes Euros *
15 Aciertos 0 0,00 €
14 Aciertos 0 0,00 €
13 Aciertos 2 51.595,23 €
12 Aciertos 53 2.056,38 €
11 Aciertos 648 168,19 €
10 Aciertos 4.639 28,19 €
Elige 8 7 4.240,11 €
```

Esta parte **sí** respeta la jornada de la URL. La parseamos quitando
los tags y aplicando regex.

**Mientras la jornada está abierta**: la página muestra `Recaudación
Estimada 1.200.000,00 €` y todas las filas de acertantes a 0.

### 2.3. Comentarios editoriales `<app-listado-comentarios-partidos>`

Quince bloques (uno por partido), con la cabecera
`PARTIDO N: <LOCAL> ... <VISITANTE>` y un `<div
class="c-detalle-partido-simple__porcentajes__tabs__comentarios">` con el
texto. **El partido 15 (selecciones, pleno al 15) nunca lleva editorial.**

## 3. Endpoints HTTP

Tag OpenAPI: `losilla`. Prefijo: `/api/v1/losilla`.

### 3.1. `GET /api/v1/losilla/jornada/{jornada}`

Devuelve el payload completo.

| Param | Dónde | Tipo | Default | Notas |
|---|---|---|---|---|
| `jornada` | path | int ≥ 1 | — | Número de jornada |
| `temporada` | query | int en [1998, 2100] | `2027` | Año final de la temporada (2026/27 → 2027) |
| `force_fresh` | query | bool | `false` | Bypasea cache en memoria |

**Respuesta 200**: `JornadaLosilla`.

**Errores**:
- `422` — `jornada < 1` o `temporada` fuera de rango
- `502` — upstream devolvió 5xx o error de red

Campos clave del payload:

| Campo | Origen | Notas |
|---|---|---|
| `jornada`, `temporada` | Request | Lo que pidió el caller |
| `ssr_shipped_jornada` | JSON SSR | Puede no coincidir con `jornada` si la pedida es histórica |
| `warning` | Calculado | Presente cuando `ssr_shipped_jornada != jornada` |
| `recaudacion_bote_live` | JSON SSR | Datos live de la jornada en juego |
| `escrutinio_oficial` | HTML | Real si la jornada está cerrada; estimada si está abierta; `null` si upstream no lo cargó |
| `partidos` | JSON SSR + HTML | 15 elementos con metadatos + comentario editorial (14/15 lo tendrán) |
| `comentarios_presentes` | Calculado | 14 en condiciones normales |
| `fetched` | Cliente | `true` si vino de upstream en esta llamada, `false` si vino del cache |
| `sha256` | HTML | Hash para validar cache |

### 3.2. `GET /api/v1/losilla/jornada/{jornada}/escrutinio`

Solo el bloque `EscrutinioOficial`.

- **200**: el bloque
- **204**: upstream no renderizó el bloque (típico: jornada en juego sin
  números publicados aún)
- **502**: upstream 5xx

### 3.3. `GET /api/v1/losilla/jornada/{jornada}/comentarios`

Lista filtrada a partidos con `comentario != null` (típicamente 14 de 15).

### 3.4. Ejemplo de uso

```bash
# Jornada en juego — trae live + estimada
curl -sS http://127.0.0.1:8765/api/v1/losilla/jornada/11 | jq '.recaudacion_bote_live'

# Jornada cerrada con bote real
curl -sS http://127.0.0.1:8765/api/v1/losilla/jornada/8/escrutinio | jq '.categorias'

# Solo comentarios editoriales
curl -sS http://127.0.0.1:8765/api/v1/losilla/jornada/11/comentarios | jq '.[0].comentario'
```

## 4. Cliente en código

Para casos en los que el caller necesita acceso programático (por ejemplo
el scheduler ingestando datos), el cliente se puede instanciar
directamente:

```python
from app.clients.eduardolosilla import EduardoLosillaClient

async with EduardoLosillaClient() as client:
    payload = await client.get_jornada(jornada=11, temporada=2027)

    if payload.warning:
        LOG.warning("SSR mismatch: %s", payload.warning)

    for partido in payload.partidos:
        if partido.comentario:
            LOG.info("%s vs %s: %s",
                     partido.local, partido.visitante,
                     partido.comentario[:80])
```

El cliente también acepta un `httpx.AsyncClient` externo y un `cache_dir`
para persistir el HTML descargado a disco (útil para re-hidratar el parser
tras cambios en upstream):

```python
async with httpx.AsyncClient(timeout=10.0) as ac:
    client = EduardoLosillaClient(
        client=ac,
        cache_dir=Path("/var/cache/la-goyniela/losilla"),
    )
```

## 5. Tests

```
tests/test_losilla_client.py      15 tests
tests/test_losilla_endpoints.py   12 tests
```

### Cobertura del cliente (`test_losilla_client.py`)

- **Parsers puros** (sin red, sin async): `_strip_tags`, `_parse_state`,
  `_parse_escrutinio_oficial`, `_parse_comentarios`.
- **Cliente end-to-end** con `httpx_mock`: jornadas abiertas, cerradas
  con bote real, cerradas de otras temporadas, sin estado (404
  semántico), con error 5xx upstream.
- **Cache**: hit de cache (segunda llamada no fetcha), `force_fresh=True`
  saltea cache, TTL diferenciado.

### Cobertura de los endpoints (`test_losilla_endpoints.py`)

- Los 3 endpoints contra jornadas reales (J8 con bote, J11 en juego,
  J70 de 25/26).
- Validación de query params (`temporada` fuera de rango → 422).
- Validación de path param (`jornada < 1` → 422).
- 204 cuando el bloque de escrutinio no está.
- 502 cuando upstream devuelve 5xx.
- OpenAPI documenta la tag y los 3 paths.

### Fixtures

`tests/fixtures/losilla/*.html` contiene HTML real capturado con el script.
Si un test empieza a fallar tras un cambio en upstream, lo más probable
es que el marcado haya cambiado — comparar el HTML actual con la fixture.

### Cómo correrlos

```bash
.venv/bin/python -m pytest tests/test_losilla_client.py tests/test_losilla_endpoints.py -v
```

## 6. Operación

### 6.1. Throttle

Una llamada cada 0,4 s como mínimo contra `eduardolosilla.es`. Es un
throttle por cortesía, no un SLA. Si la API recibe ráfagas de llamadas
distribuidas a varias jornadas distintas, el throttle se aplica por
instancia del cliente (no global).

### 6.2. Cache

| Tipo | TTL por defecto | Por qué |
|---|---|---|
| Jornada en juego (shipped == requested) | 5 min | La recaudación estimada y los % LAE cambian |
| Jornada cerrada (shipped != requested) | 1 h | Los datos son inmutables una vez publicados |

Configurable por parámetros del constructor (`open_cache_ttl_s`,
`closed_cache_ttl_s`). En `app/api/v1/losilla.py` los defaults son los
arriba listados.

Opcional: `cache_dir` persiste el HTML descargado a disco. Útil para
auditoría y para que el parser se pueda re-ejecutar contra el HTML ya
capturado (modo offline).

### 6.3. Comportamiento ante fallos

| Caso upstream | Comportamiento del cliente | Comportamiento del endpoint |
|---|---|---|
| 5xx | `httpx.HTTPStatusError` | `HTTPException 502` con el status upstream en `detail` |
| Red caída / timeout | `httpx.HTTPError` | `HTTPException 502` con tipo y mensaje |
| 200 sin `<script>` con estado | Devuelve payload con `recaudacion_bote_live=None` y `escrutinio_oficial=None` | 200 normal |
| 200 sin bloque `<app-tabla-categorias>` | Devuelve payload con `escrutinio_oficial=None` | El endpoint `/escrutinio` devuelve **204** |

### 6.4. Reconexión y circuit breaker

El cliente no implementa circuit breaker. El `httpx.AsyncClient` reusa
conexiones por keep-alive. Si upstream está caído, cada llamada
tarda el timeout (20 s por defecto) y devuelve 502. Para integración
con el scheduler, considerar añadir circuit breaker a nivel de la capa
de ingesta (ver `docs/proyecto/scheduler.md`).

## 7. Limitaciones conocidas

1. **El partido 15 (selecciones / pleno al 15) no tiene comentario
   editorial.** `comentarios_presentes` será 14/15 en condiciones
   normales.

2. **`porcentajes_lae` no está expuesto.** Los porcentajes oficiales
   de LAE vienen del endpoint `api.eduardolosilla.es/servicios/v1/
   porcentajes_lae` (ya documentado en `docs/research/02-fuentes.md`).
   Estos endpoints podrían integrarse en un cliente separado en el
   futuro si el proyecto decide usarlos.

3. **El SSR siempre trae la jornada en juego.** Si pides
   `jornada_9`, el `recaudacion_bote_live` será de la J11 aunque el
   `escrutinio_oficial` sea el real de la J9. El `warning` lo deja
   explícito.

4. **Sin licencia explícita de redistribución.** El apartado 2.10 de
   las condiciones legales de Losilla reserva derechos. Encaja con el
   uso personal y de amigos definido en el readme del proyecto, pero
   no para republicar los datos en un servicio público.

5. **HTML del upstream puede cambiar.** Los selectores CSS
   (`...__comentarios`, `c-tabla-categorias`, etc.) son contratos
   implícitos con el marcado de la web. Si Losilla cambia el marcado,
   los parsers pueden romperse silenciosamente. Las fixtures de tests
   sirven como snapshot de la versión actual.

## 8. Variables de entorno

El cliente y los endpoints no requieren env vars propias. Heredan del
`Settings` global (`app/core/config.py`):

| Env var | Default | Propósito |
|---|---|---|
| `log_level` | `INFO` | Nivel de logging; subir a `DEBUG` para ver requests upstream |
| `cors_origins` | `["http://localhost:3000"]` | Lista de orígenes permitidos para CORS |

El `User-Agent` del cliente está hardcodeado en
`app/clients/eduardolosilla.py` (`LaGoyniela/0.1 (read-only client;
contact via repo)`). Cambiarlo en código si es necesario.

## 9. Próximos pasos posibles

1. Cliente separado para los endpoints JSON de
   `api.eduardolosilla.es/servicios/v1/` (`porcentajes_lae`,
   `porcentajes_quinielista`, `probabilidad_real`, `analisisSigno`).
   Son fuentes de probabilidad adicionales al porcentaje editorial
   que ya tenemos.
2. Cache persistente en Redis para sobrevivir reinicios (hoy solo
   memoria + opcional disco).
3. Background task que ingeste la última jornada cerrada cada cierto
   tiempo y persista los `PartidoLosilla.comentario` en la base para
   poder mostrarlos en el UI sin tener que pegarle a Losilla cada
   vez.
4. Versionado de la API (`/api/v2/losilla`) cuando cambie el shape
   de los schemas.

## 10. Glosario rápido

- **Jornada**: una semana de La Quiniela. Cada temporada tiene entre
  42 y 70 jornadas.
- **Pleno al 15**: partido 15 de cada jornada, donde se predice el
  resultado a goles (0, 1, 2, M+). Tiene su propia categoría de premios.
- **Elige 8**: modalidad de apuesta con su propio escrutinio.
- **Es estimada / estimada**: la recaudación que se muestra mientras
  la jornada está abierta; el valor real se publica al cierre.
- **Acertantes**: número de apuestas que acertaron esa categoría en
  esa jornada.
- **SSR (Server-Side Rendering)**: Angular Universal pinta el HTML
  inicial en el servidor y embebe el estado en un `<script>` JSON.