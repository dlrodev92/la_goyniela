# La Goyniela — Estado al cierre de la exploración

**Fecha**: 27 de septiembre de 2026
**Fase**: validación de fuentes y armado del pipeline de mock data.
**Próxima fase**: definir el modelo de datos + endpoints HTTP de la API.

Este archivo es el snapshot del proyecto al cierre del trabajo de exploración
empezado en la sesión actual. Si Engram (memoria persistente) responde, este
contenido se guarda vía `mem_save` además. Si no, este archivo es la fuente de
verdad hasta que se decida otra cosa.

## Resumen ejecutivo

El proyecto arrancó como una investigación sobre qué fuentes públicas
gratuitas pueden sostener un asistente semanal para jugar La Quiniela
española entre amigos. Después de validar 11 fuentes candidatas
(`docs/research/02-fuentes.md`), se eligieron **5 concretas**:

| # | Fuente | Cubre | Costo | Tipo |
|--:|---|---|---|---|
| 1 | Losilla `?jornada=X&temporada=Y` | Composición 15 + %1X2 + Pleno marginales | Gratis, sin key | Sin auth |
| 2 | Quiniwin XML | Composición 15 (backup) | Gratis, sin key | Sin auth |
| 3 | Football-Data.co.uk | Histórico últimos N partidos LaLiga 1ª + 2ª | Gratis, sin key | Sin auth |
| 4 | The Odds API | Cuotas 1X2 casas UK (multi-liga: LaLiga 1, LaLiga 2, UEFA NL) | Free tier 500 créditos/mes | Con key |
| 5 | API-Football | H2H entre dos equipos + lesiones/alineaciones (futuro) | Free tier 100 req/día | Con key |

Las 5 fuentes se validaron en vivo con datos reales para J9 y J10 de la
temporada 2026/27. Hay un script que arma un JSON normalizado por partido
incluyendo: composición Losilla, estimación LAE/REAL, consenso %1X2 mercado UK
derivado de cuotas Odds API, histórico de últimos 5 por equipo (Football-Data),
y H2H cuando los IDs de equipos se conocen (API-Football).

## Decisiones de producto acordadas

1. **Cuotas UK** se muestran como **%1X2 desmarginado** (no cuotas crudas) con
   etiqueta explícita "Consenso mercado UK". Es honesto y comparable con LAE.
2. **Histórico por equipo**: últimos 5 partidos jugados. Formato compacto
   `{date, home, score, away, outcome}` con outcome ∈ {W, D, L}.
3. **H2H**: si se puede, se devuelve `matches[]`; si no, marcar
   `unknown_team_ids` y mostrar "Sin historial" en el cliente. Nunca inventar.
4. **Selección (UEFA)**: histórico y H2H no aplican (no hay fuente gratuita de
   selecciones). UI muestra "Sin datos" con honestidad. **Cuotas sí aplican**
   porque Odds API UEFA NL funciona.
5. **Pleno al 15**: se muestra la estimación Losilla de marginales L y V (0, 1,
   2, M) más la cuota h2h 1X2 de Odds API. El mercado "Correct Score" de Odds
   API no está disponible en plan free.
6. **Identidades**: alias map manual entre los 3 namespaces (Losilla, Odds API,
   FD). Femenino y masculino siempre separados (sufijo "F" marcado en Liga F).
7. **Casas UK concretas que aparecieron** (varían por jornada): betfred_uk,
   betway, leovegas, smarkets. NO se asumen bet365/williamhill/betfair.

## Limitaciones conocidas (aceptadas, no resueltas)

- **API-Football free plan** solo da acceso a temporadas 2022-2024. Equipos
  subidos a LaLiga 1/2 en 25/26 y 26/27 (Ceuta, Granada, Real Oviedo,
  Sporting Gijón, Eibar, Las Palmas, Andorra, Sabadell, Castellón, Eldense,
  etc.) **no tienen ID conocido**. Para H2H esos partidos marcan
  `unknown_team_ids`.
- **Odds API** no devuelve cuotas para partidos ya jugados (la cuota se cierra
  pre-kickoff).
- **Odds API UEFA NL** no incluye partidos muy menores (Azerbaijan vs
  Liechtenstein, Malta vs Gibraltar) en su plan free.
- **Odds API free** no expone el mercado "Correct Score" → Pleno al 15 lleva
  solo h2h, no marcador exacto.
- **Football-Data** no cubre **Liga F** ni selecciones nacionales. Filiales
  (Real Sociedad B, Celta Fortuna) no siempre aparecen.
- **Losilla** no expone timestamp por partido ni identificadores de casas de
  apuestas en sus exportaciones. Hay que confiar en su dato sin auditar.
- **SELAE oficial** devolvió 403 — no hay open data oficial.
- **Pleno al 15 producto de marginales** es una hipótesis de independencia
  entre goles locales y visitante. Multiplicarlos no representa una
  distribución conjunta observada.

## Stack técnico actual

- **Lenguaje**: Python 3 (stdlib only: `urllib`, `xml.etree`, `csv`, `json`)
- **HTTP framework previsto**: FastAPI (no instalado todavía)
- **Persistencia prevista**: SQLite (alcanza para personal+amigos); Postgres
  si se anticipa escala
- **Scheduler previsto**: cron externo o proceso con APScheduler, separado
  del web server para no pegarle a proveedores por request
- **Sin dependencias externas por ahora** — todo en stdlib + lo que ya está
  validado en scripts/

## Archivos del proyecto

```
scripts/
├── research_probe.py              (existente, investigación previa)
├── probe_sources.py               (smoke test de las 5 fuentes)
├── mock_quiniela.py               (mock normalizado por jornada, 15 partidos)
├── build_team_ids.py              (descubridor de IDs API-Football)
├── team_ids_api_football.py       (auto-generado, 58 IDs SP1+SP2+Liga F)
└── mocks/
    ├── odds_api_sample.json        (respuesta plausible de Odds API v4)
    └── api_football_h2h_sample.json  (respuesta plausible de fixtures/headtohead)

docs/
├── producto/
│   └── acuerdos-y-pendientes.md   (acuerdos de producto, no tocado)
├── research/                       (estudio previo, no tocado)
└── proyecto/
    └── estado-exploracion.md       (este archivo)
```

Cache local de fetches: `/tmp/la-goyniela-mock/` (auto-descubierto, regenerable).

## Aprendizajes no obvios

- **Odds API devuelve nombres de equipos completos** ("Czech Republic",
  "Northern Ireland", "Republic of Ireland") mientras los canónicos pueden ser
  cortos ("Czechia", "Ireland"). Hay que normalizar los outcomes también, no
  solo los inputs del quiniela. Bug encontrado y arreglado.
- **Casas UK que aparecen varían por jornada** — el parser cuenta outcomes con
  precio (no asume casas fijas). Lo importante es la cantidad, no cuáles.
- **My `normalize_team` quitaba "atletico" como prefijo** y rompía "Atletico
  Madrid" → "madrid". El orden correcto es: primero matchear el nombre
  completo, después aplicar prefix stripping solo si no se encontró.
- **API-Football free plan usa `season=2024`** (no se puede llegar a 2026 sin
  plan pago). La tabla auto-descubierta es estable hasta que la free tier
  expanda.
- **"Espanol" NO debe mapear a "Spain"** — es el club Espanyol (id=540), no
  la selección nacional.
- **Odds API key en plan free**: el endpoint `soccer_uefa_nations_league` es
  el umbrella UEFA que responde (NL + ECQ + WCQ en rotación). Otros endpoints
  UEFA (champions league qualifying, european championship qualifying, world
  cup qualifying) devuelven 404 en plan free.
- **Pos 15 es internacional con frecuencia** — Pleno al 15 suele ser un
  amistoso o clasificatoria de selección, no un club. Hay que tratarlo como
  caso especial en la UI y el modelo.

## Próximo paso

Definir el **modelo de datos** y los **endpoints HTTP** de la API. Esto incluye:

- Tablas: `Jornada`, `Partido`, `Estimacion1X2`, `EstimacionCuotaUK`,
  `PartidoHistorico` (FD), `PartidoH2H` (API-Football), `SourceFetch`
  (auditoría), `Snapshot` (inmutable pre-cierre).
- Endpoints candidatos:
  - `GET /jornadas/current` y `/jornadas/{season}/{number}`
  - `GET /partidos/{id}` con contexto completo
  - `GET /partidos/{id}/estimaciones` con provenance
  - `GET /partidos/{id}/h2h` (cuando aplique)
  - `GET /admin/doors` (estado de G1-G5)
- Decidir stack: SQLite vs Postgres vs JSON files para v1.
- Decidir auth: deferida a MVP-02 según acuerdos de producto, pero el primer
  loop end-to-end necesita al menos un token local por grupo.

Alternativamente, **codear FastAPI directo** con la estructura ya clara,
iterando sobre el código en lugar de diseñar primero. Decisión del usuario.

## Notas operacionales

- **NO COMMITEAR las API keys al repo**. Las keys viven solo en el chat de
  esta sesión. Para correr los scripts, exportarlas en el shell.
- **Cache en `/tmp/`** se puede borrar en cualquier momento; los scripts
  re-fetchean y re-populan.
- **Engram (memoria persistente) tuvo fallas de write** durante esta sesión.
  Si vuelve a funcionar, este archivo se duplica ahí vía `mem_save`.

## Resumen JSON outputs producidos en esta sesión

- `/tmp/goyniela-mock-keys.json` — smoke test con keys reales
- `/tmp/goyniela-mock-j9-final.json` — J9/2026/27 (Segunda división + Liga F), 15 partidos
- `/tmp/goyniela-mock-j10-v4.json` — J10/2026/27 (selecciones UEFA), 15 partidos
- `/tmp/goyniela-mock-j9-IDs.json` — J9 con tabla de IDs API-Football ya integrada

Cada JSON es la salida del `scripts/mock_quiniela.py` con datos reales (sin mocks
de fallback cuando hay key).
