# Catálogo de fuentes

Consultado el **2026-09-20**. Los precios y coberturas corresponden a las páginas consultadas y deben volver a comprobarse antes de integrar.

## Cómo leer los estados

- **V — Verificada técnicamente:** se obtuvo y analizó una muestra de datos útil mediante una petición directa. No acredita estabilidad futura ni permisos de publicación.
- **D — Documentada, no verificada:** se revisó la oferta o la página; falta una respuesta útil de datos, clave o prueba de cobertura.
- **X — Descartada para el núcleo gratuito automático:** la cobertura, restricciones o naturaleza del recurso no resuelven la necesidad. Puede servir para investigar o como referencia.

“Sin SLA verificado” significa que no se acreditó una garantía de disponibilidad o actualización. La ausencia de una licencia explícita no se interpreta como permiso de redistribución. Las conclusiones de uso son criterios de selección del proyecto, no un dictamen jurídico.

## Quiniela: composición, porcentajes y premios

| ID / fuente | Datos y cobertura | Acceso e histórico | Actualización / límites | Estado y decisión |
|---|---|---|---|---|
| Q1 [SELAE](https://www.loteriasyapuestas.es/es/la-quiniela) | Autoridad para boleto, resultados, premios y reglas. | Web y documentos PDF; se revisó una jornada actual y composición histórica. No se identificó una API pública documentada para este proyecto. | Según publicación; cierre específico por jornada/canal. | **D.** Consulta web disponible, petición directa a la portada devolvió 403. Integración automática no acreditada; no eludir el bloqueo. |
| Q2 [Losilla: porcentajes](https://www.eduardolosilla.es/quiniela/ayudas/porcentajes) | Porcentajes de su clientela, LAE y estimaciones llamadas “REAL”; primeros 14 y marginales de goles del pleno. | XML/CSV enlazados públicamente, sin clave en las muestras. Jornadas 8/9 de 2026/27 y 1 de 2025/26 verificadas. | La página anuncia LAE diario y estimaciones horarias. Sin cuota/SLA documentado. El XML probado no incluye timestamp de cálculo. | **V.** Candidata técnica central; LAE puede estar inactivo. No sustituye cuotas originales auditables. |
| Q3 [Losilla: horarios](https://www.eduardolosilla.es/quiniela/ayudas/horarios) y [escrutinio](https://www.eduardolosilla.es/quiniela/ayudas/escrutinio) | Identificación de jornada, partidos, horarios y datos de escrutinio publicados. | HTML con estado estructurado embebido; muestra actual analizada, sin API pública estable acreditada. | Cambia con la jornada. Los datos generales y el bloque mostrado pueden corresponder a jornadas distintas. | **V parcial.** Horarios/metadatos recuperables. Premios definitivos e histórico completo no certificados en esta prueba. |
| Q4 [Quiniwin: descargas](https://www.quiniwin.com/quinielas/descargas.html) | Orden y nombres de los 15 partidos. | XML; ejemplo público 2023/24 y muestras 8/9 de 2026/27 descargadas. | Archivo con fecha de generación, sin hora individual de partidos en la muestra. Sin SLA/cuota publicados. | **V.** Candidata para composición, no para resultados o probabilidades. El patrón de URL actual se comprobó, no se presume eterno. |
| Q5 [ApuestaQuiniela](https://www.apuestaquiniela.es/quiniela) | Boleto, horarios, porcentajes, recaudación y escrutinio histórico. | HTML visible; no se verificó exportación estructurada ni API. | Frecuencia y hora de los porcentajes no acreditadas. | **D.** Contraste de lectura, no dependencia aprobada. Los porcentajes pueden diferir por momento de captura. |

**Condiciones Q1–Q5.** [SELAE](https://www.loteriasyapuestas.es/es/centro-de-ayuda/informacion-pie/aviso-legal) no se trata como un conjunto de datos con licencia abierta. Losilla explica la descarga para análisis particular en su [ayuda de análisis](https://www.eduardolosilla.es/quiniela/ayudas/analisis), mientras sus [condiciones, apartado 2.10](https://www.eduardolosilla.es/general/condiciones-legales), reservan derechos y no conceden una autorización general de reproducción. Queda pendiente aclarar el alcance de almacenamiento y uso recurrente en una herramienta compartida; no se presume que la exportación autorice una réplica pública. Para Quiniwin y ApuestaQuiniela no se acreditó una licencia de integración o redistribución.

### Exportaciones comprobadas

Los enlaces se descubrieron en la interfaz pública; son ejemplos de lectura, no contratos propios de La Goyniela:

```text
https://api.eduardolosilla.es/servicios/v1/probabilidad_real?jornada=8&temporada=2027
https://api.eduardolosilla.es/servicios/v1/porcentajes_lae?jornada=8&temporada=2027
https://api.eduardolosilla.es/servicios/v1/porcentajes_quinielista?jornada=9&temporada=2027
https://www.quiniwin.com/xml/2026-2027_j08.xml
```

Los dos primeros y el XML de Quiniwin tienen muestras directas. El enlace `porcentajes_quinielista` fue observado en la página; sus datos también aparecieron en el CSV conjunto, pero ese endpoint particular no se consultó. En Losilla, `temporada=2027` representa 2026/27. La exportación `analisisSigno` es un CSV con varias secciones y filas especiales para el pleno; no es una tabla rectangular sencilla. No adivinar el tipo de porcentaje por su posición.

## Fútbol y cuotas

| ID / fuente | Datos / cobertura | Gratis y acceso | Histórico / frecuencia | Estado y uso propuesto |
|---|---|---|---|---|
| F1 [Football-Data.co.uk](https://football-data.co.uk/spainm.php) | Resultados, goles, estadísticas y cuotas de Primera y Segunda; no Liga F en los archivos examinados. | CSV sin cuenta: temporadas y `fixtures.csv`. No confundir con football-data.org. | Histórico amplio; 2025/26 y 2026/27 comprobados. El archivo de próximos encuentros se reemplaza. | **V.** Principal candidato para investigación histórica y contexto masculino. |
| F2 [football-data.org](https://www.football-data.org/coverage) | Calendario, resultados y clasificación de LaLiga y otras competiciones; Segunda/Liga F no figuran en la lista gratuita consultada. | [Plan gratuito](https://www.football-data.org/pricing): 12 competiciones, 10 llamadas/minuto, horarios/resultados demorados. Clave inicial. | La profundidad histórica gratuita exacta no se verificó. Cuotas son un añadido de pago. | **D.** Alternativa parcial de calendario, insuficiente como proveedor único. |
| F3 [API-Football](https://www.api-football.com/coverage) | Catálogo incluye LaLiga, Segunda y Primera División Femenina. Bajas, alineaciones y estadísticas dependen de cobertura. | [Gratis](https://www.api-football.com/pricing): 100 peticiones/día, endpoints incluidos, temporadas limitadas. Clave inicial. | No se comprobó con cuenta qué temporadas actuales, campos ni cuotas ofrece gratis por competición. | **D.** Primera candidata a verificar para cubrir contexto de Liga F. El catálogo general no demuestra el acceso gratuito efectivo. |
| F4 [The Odds API](https://the-odds-api.com/) | Cuotas por casa y mercado con tiempos de actualización. | 500 créditos/mes gratis; clave. El histórico queda fuera del plan gratuito. | Se puede construir un histórico propio desde el alta. [Catálogo](https://the-odds-api.com/sports-odds-data/sports-apis.html) incluye Primera/Segunda; no se encontró Liga F española en la lista consultada. | **D.** Candidata para probabilidades trazables, sin cobertura total acreditada. |
| F5 [Understat](https://understat.com/league/La_liga/2026) | xG de LaLiga y ligas seleccionadas; el selector consultado llega a 2014/15. | Web gratuita. No se acreditó API pública/licencia de integración. | Las páginas actual e histórica respondieron, pero no se extrajo ningún registro xG. No se verificó frecuencia. | **D.** Contexto avanzado opcional; no dependencia del MVP. |
| F6 [ClubElo](https://clubelo.com/) | Ratings de clubes como posible referencia de fuerza. | Acceso CSV descrito por [soccerdata](https://github.com/probberechts/soccerdata/blob/master/soccerdata/clubelo.py). | La consulta a `api.clubelo.com/2026-09-19` devolvió 502; `/API` redirigió a la portada. | **D.** No se verificó feed actual, cobertura ni permiso de reutilización. Un Elo calculado sobre históricos autorizados evita esa dependencia. |
| F7 [StatsBomb / Hudl Open Data](https://github.com/hudl/open-data) | Competiciones/temporadas seleccionadas y eventos. El catálogo contiene Liga F 2023/24, no Liga F 2026/27. | JSON público; catálogo analizado. | No es un suministro semanal general. No se comprobó que cada temporada listada esté completa. | **V catálogo / X núcleo.** Útil para estudiar modelos, no para actualidad semanal. |
| F8 [OpenFootball España](https://github.com/openfootball/espana) | Resultados y calendarios de Primera/Segunda, formato Football.TXT. | Repositorio CC0; carpetas 2012/13–2026/27 observadas. | Actualización comunitaria sin SLA acreditado; no se descargó una temporada para auditar integridad. | **D.** Alternativa abierta de resultados; no aporta cuotas ni prueba cobertura de Liga F. |
| F9 [Sportmonks gratis](https://www.sportmonks.com/football-api/free-plan/) | Plan permanente limitado a liga danesa y escocesa en la página consultada. | Cuenta gratuita, diferente de prueba temporal de planes comerciales. | Cobertura gratuita no resuelve las ligas principales del boleto. | **X núcleo.** No financiar el MVP con un trial. |
| F10 [Sofascore](https://sofascore.helpscoutdocs.com/article/129-sports-data-api-availability) | Mucho contexto visible para consulta humana. | Su FAQ indica que no comparte endpoints por acuerdos con proveedores. | No se evaluaron endpoints privados ni técnicas para eludir bloqueos. | **X núcleo.** Una biblioteca de scraping no convierte sus datos en una API autorizada. |
| F11 [FBref / Sports Reference](https://www.sports-reference.com/blog/category/uncategorized/) | Fuente históricamente usada para estadísticas avanzadas. | El proveedor anunció en enero de 2026 la retirada de datos avanzados. | No se acreditó una reposición del feed. | **X para xG del MVP.** Los tutoriales antiguos no prueban disponibilidad actual. |

### Condiciones y advertencias específicas de las fuentes

- **F1:** la [página de datos](https://www.football-data.co.uk/data) anuncia descargas gratuitas para análisis y actualizaciones periódicas; las [notas](https://football-data.co.uk/notes.txt) distinguen cuotas previas y de cierre. Advierte de cuotas de Pinnacle desactualizadas desde julio de 2025. No usar esas columnas como referencia actual sin comprobación. El [disclaimer](https://football-data.co.uk/disclaimer.php) no es una licencia abierta de redistribución. La descarga para investigar está ofrecida; servir archivos a terceros requiere aclaración.
- **F2:** [condiciones](https://www.football-data.org/client/register) y límites del plan aplican también al acceso gratuito. El acceso a datos no implica derechos sobre escudos y marcas.
- **F3:** sus [condiciones](https://www.api-football.com/terms) prohíben cuentas múltiples para ampliar la cuota y revender datos; además indican que no otorgan derechos generales de publicación de los datos de terceros. Registrar una aplicación no resuelve por sí solo esa cuestión. No se ha creado ninguna cuenta.
- **F4:** la [documentación](https://the-odds-api.com/liveapi/guides/v4/) calcula el coste según mercados/regiones y publica cabeceras de consumo. Sus [condiciones de agosto de 2026](https://the-odds-api.com/terms-and-conditions.html) permiten almacenamiento y análisis, pero prohíben redistribuir un feed de datos independiente. No confundir `the-odds-api.com` con proveedores de nombre similar.
- **F5/F6:** no se encontró una autorización de integración suficiente en las páginas revisadas. Eso queda pendiente; un HTTP 200 no la sustituye.
- **F7:** el [acuerdo de datos](https://raw.githubusercontent.com/statsbomb/open-data/master/LICENSE.pdf) establece uso de investigación, restricciones de redistribución/explotación comercial y atribución. El acceso abierto al repositorio no equivale a una licencia MIT de los datos.

## Contexto adicional y herramientas auxiliares

| ID / fuente | Posible utilidad | Automatización e histórico | Decisión |
|---|---|---|---|
| C1 [RFEF: resoluciones disciplinarias](https://rfef.es/es/federacion/normativas-y-circulares/sanciones) | Suspensiones confirmadas. | Web/PDF por resolución; no se ha validado extracción sistemática ni asociación por jugador/competición. | **D.** Complemento futuro. Contar tarjetas no basta para afirmar que alguien está sancionado. |
| C2 [Comunicados de clubes, ejemplo](https://www.realmadrid.com/es-ES/noticias/futbol/primer-equipo/partes-medicos/parte-medico-de-valverde-20-09-2026) | Bajas y confirmaciones oficiales. | Publicación irregular y formatos distintos; no se acreditó feed gratuito común. Deben conservarse fecha y URL. | **D.** No prometer cobertura completa ni asignar impacto numérico sin validación. |
| C3 [Open-Meteo](https://open-meteo.com/en/pricing) | Lluvia, viento y temperatura previstos en el estadio. | API gratuita no comercial hasta 10.000 llamadas/día, sin garantía de servicio. No se ejecutó una consulta. | **D.** Factible técnicamente como ampliación; utilidad predictiva no demostrada. |
| T1 [soccerdata](https://github.com/probberechts/soccerdata) | Adaptadores Python para diversas fuentes. | Biblioteca de acceso, no proveedor ni licencia conjunta de los datos. No se instaló. | Estudiar solo después de seleccionar fuentes; no activar proxies ni evasión de bloqueos. |

Para clima, una evaluación retrospectiva debe emplear la previsión disponible entonces; el tiempo observado después no sirve como entrada histórica previa al boleto. [Open-Meteo distingue sus API históricas](https://open-meteo.com/en/docs/historical-forecast-api); todavía habría que comprobar qué producto conserva el instante de emisión necesario.

No se seleccionan redes sociales, rumorología ni un servicio LLM comercial como dependencias. Las fuentes de prensa pueden aportar contexto, pero no se verificó un suministro gratuito homogéneo ni su permiso de almacenamiento. El criterio es la utilidad verificable, no acumular variables.

## Selección recomendada, todavía condicionada

- **Investigar primero:** Q2/Q4 para composición y porcentajes; F1 para histórico y cuotas disponibles; F3 para resolver Liga F; F4 para trazabilidad del mercado.
- **Opcionales:** F2/F8 como alternativas parciales de resultados, F5/C1/C2/C3 para enriquecer contexto.
- **No usar como soporte principal:** F6 hasta recuperar el feed, F7 para actualidad, F9/F10/F11 por los motivos anteriores.
- **Dependencias comunes:** Q2 y Q3 pertenecen al mismo proveedor. Dos exportaciones del mismo sitio no son redundancia independiente. Tampoco promediar un agregado con casas que ya incluye como si fueran observaciones independientes.
