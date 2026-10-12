# Verificación técnica y evaluación

Consultas directas: **2026-09-20 UTC**. Documentación completada el 21 de septiembre. Las métricas siguientes describen las muestras conservadas temporalmente, no un servicio en funcionamiento.

## Método y evidencia

Se realizaron **31 consultas HTTP públicas: 28 respuestas 200 y 3 errores**. Se guardaron URL, fecha UTC, tipo, tamaño y SHA-256 en [access-log.json](evidence/access-log.json). Las páginas consultadas mediante buscador/navegador se citan en los documentos, pero no se incluyen en ese recuento de peticiones directas.

El [analizador](../../scripts/research_probe.py) comprueba hashes y analiza los formatos de datos seleccionados; no considera toda respuesta 200 como una integración válida. El [resultado](evidence/sample-analysis.json) contiene métricas por muestra. Los originales permanecen fuera de Git, en una caché temporal, y pueden desaparecer. El registro permite repetir la consulta y detectar cambios; una huella no permite reconstruir un recurso antiguo.

No se hicieron consultas autenticadas a API-Football, football-data.org ni The Odds API. Sin clave y sin una cuenta gratuita no se puede acreditar su cobertura efectiva. No se probó disponibilidad semanal sostenida ni la publicación de datos antes del cierre: esta sesión es una comprobación puntual.

## Resultados medidos

### Histórico y próximos encuentros

| Muestra Football-Data | Partidos | Rango de fechas | Cuotas medias 1X2 válidas | Cuotas medias de cierre válidas |
|---|---:|---|---:|---:|
| SP1 2025/26 | 380 | 15/08/2025–24/05/2026 | 380 | 380 |
| SP2 2025/26 | 462 | 15/08/2025–31/05/2026 | 462 | 462 |
| SP1 2026/27 | 59 | 15/08/2026–17/09/2026 | 59 | 59 |
| SP2 2026/27 | 55 | 14/08/2026–14/09/2026 | 55 | 55 |
| fixtures.csv | 198 | 18/09/2026–20/09/2026 | 198 | 0 |

Son comprobaciones propias sobre las URL del registro. “Válidas” aquí significa tres números finitos mayores que uno; no demuestra exactitud ni representatividad. Las cuatro temporadas contienen resultados, sin duplicados según división/fecha/local/visitante. La muestra SP2 histórica termina antes de los playoffs: no asumir que sus 462 filas cubren cualquier eliminatoria.

`fixtures.csv` contiene 10 partidos SP1 y 11 SP2, además de otras ligas. Aunque el nombre sugiera próximos encuentros, al consultar al final del domingo los partidos de esa instantánea ya podían estar disputados. No usar el nombre del archivo como estado del partido ni su contenido como una captura anterior al cierre.

### Cruce de una jornada completa

Se cruzó la **jornada 8 de 2026/27** de Quiniwin y Losilla con las fechas del CSV. La composición también se contrastó visualmente con la [publicación de SELAE](https://www.loteriasyapuestas.es/es/resultados?drawId=137306001).

| Posiciones del boleto | Composición Quiniwin/Losilla | Coincidencias en fixtures.csv | Implicación |
|---|---:|---:|---|
| 1–7 | 7/7 | 7/7 SP1 | Candidatas para cuotas/contexto masculino. |
| 8–10 | 3/3 | 3/3 SP2 | Segunda debe formar parte de la cobertura. |
| 11–14 | 4/4 | 0/4 | Los cuatro encuentros femeninos no están en este CSV. |
| Pleno al 15 | 1/1 | 1/1 SP1 | Hay 1X2, pero esto no basta para obtener la distribución de marcadores. |
| Total | 15/15 | **11/15** | Faltan datos de Liga F, no la composición de sus casillas. |

El cruce utiliza alias explícitos revisados para la muestra, no un algoritmo universal: `RAYO` / `Vallecano` / `Rayo Vallecano`, `ATH.CLUB` / `Ath Bilbao` / `Athletic Club`, `SPORTING` / `Sp Gijon`, `DEPORTIVO` / `La Coruna`. Los sufijos femeninos se preservan. Configurar equivalencias iniciales es compatible con la automatización; corregirlas a mano cada semana no lo sería. Los casos nuevos ambiguos deberán quedar sin asociar hasta resolverlos de manera fiable.

### Exportaciones de porcentajes

| Muestra | Estructura | Primeros 14 | Pleno | Observación |
|---|---|---|---|---|
| REAL J8, temporada 2027 | 15 posiciones completas | 14 vectores suman 100 | Dos marginales de cuatro categorías suman 100 cada una | Sin identificadores de casas ni timestamp por partido. |
| REAL J9, temporada 2027 | 15 posiciones completas | 14 vectores válidos | Marginales disponibles | Disponibilidad del dato no prueba procedencia de cada cuota. |
| REAL J1, temporada 2026 | 15 posiciones completas | 14 vectores válidos | Marginales disponibles | Acredita una muestra histórica, no un archivo exhaustivo ni sus horas de captura. |
| LAE J8, temporada 2027 | Activo | 14 vectores válidos | Marginales disponibles | Son apuestas del público, no probabilidades deportivas. |
| LAE J9, temporada 2027 | `activo="no"` | Ceros | Ceros | Representan ausencia de publicación: nunca probabilidades cero ni división por cero. |

El analizador usa tolerancia de 0,021 puntos porcentuales para sumas redondeadas; es una tolerancia de investigación, no una política de producción aprobada. El resultado detallado está en [sample-analysis.json](evidence/sample-analysis.json).

**El pleno requiere cuidado adicional:** ocho valores marginales no son dieciséis probabilidades conjuntas observadas. Multiplicar las marginales implica una hipótesis de independencia entre goles que habría que declarar y evaluar. `M` significa tres o más goles; no es un marcador exacto ni determina por sí solo el 1X2 cuando ambos equipos tienen M.

### Composición, horarios y errores

- Quiniwin devolvió 15 partidos en las muestras J1 2023/24 y J8/J9 2026/27. En J8 los pronósticos y la columna supuestamente ganadora contenían unos, con un pleno predeterminado: se clasifican como plantilla, no escrutinio.
- El estado embebido de Losilla contiene jornada/temporada y campos como `fechaFinApuestas`, `cierreParticulares` y `cierreDnp`. En la página de escrutinio, el bloque general se refería a J9 mientras existía un bloque específico de J8. No mezclar sus cierres y resultados; tampoco identificar el cierre de una administración con el de todos los canales.
- SELAE portada: **403** directo. API-Football página de cobertura: **403** directo, aunque el navegador de investigación pudo leerla. No son pruebas de que su API autenticada esté caída.
- ClubElo CSV: **502**. No se probó recuperación ni se acreditó disponibilidad; la respuesta de la portada no valida el feed.
- Understat devolvió HTML actual e histórico, pero no se encontraron los datos embebidos `datesData` esperados por ese patrón de extracción: **cero registros xG extraídos**, no “cero xG” de los equipos. Puede requerir otra integración; no se resolvió en esta investigación.
- StatsBomb: catálogo de 80 combinaciones competición/temporada, con Liga F 2023/24; no acredita suministro para el boleto actual.

## Qué se necesita para una operación semanal automática

Propuesta técnica para evaluar el MVP, todavía sin implementación:

1. Descubrir jornadas desde una fuente seleccionada y permitida; usar temporada + número, versión de composición, fecha y cierre del canal. El calendario del boleto no coincide necesariamente con una jornada de liga.
2. Asociar cada casilla a un partido mediante identificadores del proveedor y un catálogo de equivalencias con competición, categoría y fecha. Un parecido de nombre no basta; conservar orientación local/visitante.
3. Capturar solo los datos necesarios, guardar origen, hora de consulta y de publicación cuando exista, y validar cobertura. Distinguir “no disponible”, “inactivo”, “caducado” y cero real.
4. Emitir una instantánea reproducible previa al cierre. Los datos posteriores pertenecen a otra versión y no reescriben la recomendación histórica.
5. Continuar con fuentes alternativas permitidas si cubren el mismo dato. Si falta contexto opcional, mostrarlo como ausente; si falta composición/cierre/probabilidad obligatoria, no emitir un boleto completo presentado como válido.
6. Registrar resultado oficial y comparar con la instantánea anterior. Para anulados/aplazados, separar resultado deportivo del resultado usado en el concurso.

No se necesita tiempo real de segundos para investigar una ayuda previa al cierre. Como presupuesto de consultas orientativo, dos competiciones × dos capturas/día × 31 días × un mercado × una región = **124 créditos/mes** en el esquema básico de [The Odds API](https://the-odds-api.com/liveapi/guides/v4/). Ampliar mercados/regiones, endpoints por evento o frecuencia cambia ese cálculo. Reservar margen para nuevas jornadas y fallos; no gastar el cupo en cada visita del usuario.

Para API-Football se propone medir el consumo real —incluida paginación— y reservar al menos 20 de sus 100 peticiones diarias. No se afirma que todos los endpoints por 15 partidos quepan: depende del acceso efectivo y de cómo agrupen las respuestas.

## Evaluación de probabilidades: protocolo propuesto

**No se han entrenado ni evaluado modelos en esta entrega.** Lo siguiente define cómo evitar conclusiones engañosas en la fase de experimentación.

### Estimaciones de referencia

Para cuotas decimales de una misma casa y momento, una primera normalización reproducible es:

```text
q_i = 1 / cuota_i
p_i = q_i / (q_1 + q_X + q_2)
```

Es una aproximación de retirada de margen, no la probabilidad verdadera. Normalizar por casa antes de combinar; excluir mercados incompletos, cuotas inválidas y estados suspendidos. No mezclar 1X2 a tiempo reglamentario con clasificación tras prórroga. Comparar consenso y dispersión; un solo proveedor no permite estimar desacuerdo entre casas.

Baselines: frecuencias históricas por competición estimadas solo en entrenamiento, favorito del mercado y un modelo sencillo de goles/Elo. Para goles puede estudiarse [Dixon–Coles](https://www.research.lancs.ac.uk/portal/en/publications/modelling-association-football-scores-and-inefficiencies-in-the-football-betting-market%28d16276a2-d6e0-483b-a708-1d29663f1992%29.html), sin asumir que su resultado histórico se traslada al presente. No aplicar sin validar parámetros masculinos a Liga F.

### Prueba temporal

- Ordenar por fecha; entrenar con pasado, validar después y mantener un tramo final fuera de ajuste. No repartir partidos al azar entre entrenamiento y test.
- Exigir que cada entrada estuviese disponible antes del cierre del boleto, no solo antes de su partido. Las cuotas finales se admiten como comparación posterior, nunca como información operativa anticipada.
- Si no hay timestamps históricos fiables, usar el histórico para experimentos exploratorios y empezar un registro prospectivo; no etiquetar esa simulación como rendimiento real semanal.
- Medir log loss, Brier multiclase, diagramas de calibración por signo y rendimiento por competición. Menor Brier no demuestra por sí solo mejor calibración: también influye la discriminación, como explica [scikit-learn](https://scikit-learn.org/1.8/modules/calibration.html).
- Comparar sobre los mismos partidos y publicar porcentaje de cobertura/abstención. Mostrar tamaño de muestra e intervalos por remuestreo de jornadas. Un puñado de plenos o premios no demuestra superioridad.

### Construcción y evaluación del boleto

Los primeros 14 admiten 3^14 = 4.782.969 columnas de signos. Un desarrollo completo de d dobles y t triples contiene 2^d × 3^t columnas antes de considerar variantes del pleno y las reglas del canal. Una reducida sacrifica combinaciones: cualquier garantía depende de que el resultado caiga dentro del pronóstico de partida y de sus condiciones.

Seleccionar columnas distintas de mayor probabilidad maximiza la cobertura del 14 bajo independencia entre partidos y un número fijo de columnas. Ese objetivo no es igual a maximizar la posibilidad de algún premio ni el retorno. Para categorías inferiores, los eventos de premio de varias columnas se solapan; sumar sus probabilidades individuales no da la probabilidad de ganar al menos una vez. Usar enumeración de escenarios o simulación con error informado.

Los porcentajes apostados pueden orientar una comparación con el mercado, pero `p/q` no demuestra retorno positivo. Estimar premios exige recaudación, reglas, bote y distribución conjunta de apuestas; multiplicar porcentajes marginales del público no reproduce cómo juega una peña. No usar recaudación final como si se conociera antes del cierre.

El precio de referencia es **0,75 € por apuesta**, recogido en las [normas enlazadas actualmente por SELAE](https://www.loteriasyapuestas.es/f/loterias/documentos/normativa/Normativa%20de%20los%20juegos/Normas_de_la_Quiniela_y_Elige8_Octubre_2019.pdf) y consistente con el metadato observado de Losilla. El PDF está fechado en 2019: verificar versiones, mínimos, expansión del pleno y canal antes de implementar costes definitivos. Elige8 queda fuera del MVP propuesto.

## Escenarios de aceptación para la siguiente fase

| Caso | Resultado exigible |
|---|---|
| XML LAE inactivo y todo ceros | Datos ausentes; sin cálculo de valor ni probabilidades inventadas. |
| Club masculino y femenino comparten nombre | Identidades distintas; no cruzar sus históricos. |
| Nombre ambiguo o dos partidos del mismo par | No unir solo por nombre; exigir competición/fecha e identidad. |
| HTML 200 sin registros | Fuente no disponible para esa capacidad. |
| Proveedor devuelve 403/429 o agota cuota | Sin evasión; respetar límites, registrar fallo y usar alternativa válida o abstenerse. |
| Página muestra metadatos de J9 y resultados J8 | Versionar por jornada; no mezclar bloques. |
| Partido aplazado, sustituido o decidido por sorteo | Usar composición y resultado oficiales del concurso. |
| Cuota posterior al cierre, alineación tardía o noticia posterior | Excluir de la instantánea predictiva original. |
| Pleno solo con marginales | No presentarlo como distribución conjunta observada. |
| Presupuesto insuficiente o columnas duplicadas | Coste exacto según reglas, sin sobrepasar presupuesto; informar límites. |
| Reducida o varias columnas con premios solapados | Garantías condicionadas y cobertura sin doble conteo. |
| Explicación muestra porcentajes distintos del cálculo | Rechazar salida inconsistente. |

Estas son pruebas de aceptación propuestas para futuras especificaciones. Las comprobaciones efectivamente realizadas ahora son las descargas, análisis estructurales y cruces descritos arriba.
