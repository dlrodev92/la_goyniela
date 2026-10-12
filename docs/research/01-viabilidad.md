# Viabilidad de La Goyniela

Fecha de investigación: **2026-09-20**. Audiencia inicial: el creador y sus amigos. Restricciones: fuentes gratuitas y operación semanal automática, con configuración inicial permitida.

## Conclusión

**Es viable avanzar hacia un prototipo automático condicionado; no está demostrada todavía la viabilidad de un servicio completo para todas las jornadas.** La parte combinatoria es abordable sin proveedores. Los principales límites están en el contexto de Liga F, las cuotas trazables para todos los encuentros, la confirmación del cierre y las condiciones de reutilización de exportaciones de terceros.

| Necesidad | Veredicto de esta investigación | Alcance que respaldan las evidencias |
|---|---|---|
| Información y contexto | Viable con limitaciones | Históricos estructurados de Primera/Segunda y métricas derivadas. No se ha verificado una fuente gratuita única de bajas, alineaciones y contexto para todos los partidos. |
| Probabilidades justificadas | Viable con limitaciones | Conversión transparente de cuotas donde existan. Hay porcentajes exportables para el boleto completo de muestra, pero sin trazabilidad individual de las cuotas originales. |
| Construcción del boleto | Viable como cálculo; condicionada como flujo semanal | Seleccionar columnas bajo presupuesto, mostrar cobertura y comparar alternativas. Requiere composición de jornada y estimaciones válidas; no demuestra rentabilidad. |
| Todo automático, todas las jornadas | No demostrado | Accesos puntuales no acreditan continuidad, licencias ni cobertura universal. Si falta un dato obligatorio, el sistema deberá abstenerse sin pedir carga manual. |

Los detalles medidos están en [verificación](03-verificacion.md); los estados por proveedor, en [fuentes](02-fuentes.md).

## Hallazgos que cambian el alcance

1. **La cobertura debe seguir el boleto oficial.** La incorporación habitual de cuatro partidos de Liga F en 2026/27 está anunciada por el [CSD](https://www.csd.gob.es/es/la-quiniela-abre-una-nueva-etapa-de-visibilidad-y-crecimiento-para-el-futbol-femenino). La [jornada 8 publicada por SELAE](https://www.loteriasyapuestas.es/es/resultados?drawId=137306001) confirma cuatro encuentros femeninos. No basta con Primera y Segunda masculinas.
2. **Existen jornadas con otras competiciones.** La [composición oficial de la jornada 68 de 2025/26](https://www.loteriasyapuestas.es/f/loterias/web_corporativa/Comunicacion/Avisos_de_interes/Composici%C3%B3n%20FNMT%20jornada%2068%C2%AA%20La%20Quiniela%20de%2020%20-%2021%20de%20Junio%20de%202026.pdf) mezcla selecciones y playoff. No fijar ligas o días de ejecución por suposición.
3. **Obtener el dato y justificarlo son problemas distintos.** Una probabilidad publicada por un tercero puede servir como referencia, pero no equivale a disponer de casa, mercado, cuota y momento de observación para auditarla.
4. **La fecha de consulta importa tanto como el histórico.** Las cuotas de cierre de un partido del domingo pueden incorporar información posterior al cierre del boleto. Un backtest que las use como entrada semanal estaría usando información futura.
5. **El mercado ya ofrece buena parte de las piezas.** La oportunidad de La Goyniela es una hipótesis de mejor integración, explicación y control de calidad; no se sostiene afirmar que no existe ninguna ayuda.

## Comparación de herramientas

Revisión de páginas y documentación públicas, sin instalar ejecutables, registrarse ni apostar. “No observado” significa que la capacidad no se acreditó en la muestra, no que sea inexistente.

| Herramienta y evidencia primaria | Qué se ha observado | Qué aporta a la investigación | Límite o diferencia por validar |
|---|---|---|---|
| [Quinielista / Eduardo Losilla](https://www.eduardolosilla.es/quiniela/ayudas/analisis) | Análisis por signo, históricos y exportación para análisis particular. | Referencia central del flujo quinielista y candidata de datos. | No se verificó la trazabilidad completa de sus estimaciones ni una autorización general de integración recurrente. |
| [Comparador de cuotas de Losilla](https://www.eduardolosilla.es/quiniela/ayudas/cuotas) | Cuotas y probabilidades descontando margen; atribuye el comparador a Winvictus. | Comparar mercado y porcentajes apostados ya es una capacidad existente. | No confundir su frecuencia anunciada con la de otras secciones ni con un compromiso de servicio. |
| [Megaquin 1X2](https://www.megaquin1x2.com/) | Descarga gratuita, generación de combinaciones, salida TXT y escrutador. | Benchmark de construcción y exportación de boletos. | No se ejecutó ni se acreditó una interfaz para automatización sin interacción. Gratuito no implica código abierto. |
| [Condiciones de Megaquin](https://www.megaquin1x2.com/condiciones/probabilidad_apuestas.php) | Filtros por probabilidades de categorías y relación premio/coste. | Obliga a diferenciar objetivos: acierto, cobertura y premio. | Sus ejemplos comerciales no constituyen una validación independiente de rentabilidad. |
| [Quiniwin](https://www.quiniwin.com/quinielas/descargas.html) | Programas demo/gratuitos y formato XML de jornadas. | Exportación estructurada comprobada; referencia de combinatoria. | En la muestra hay valores de plantilla: no tomar columnas por resultados oficiales. No se validó licencia de redistribución. |
| [Free1X2](https://sourceforge.net/projects/free1x2/) | Proyecto C#/.NET, GPLv2, última actualización indicada: 2013. | Candidato para estudiar algoritmos y formatos. | No se compiló ni se auditó compatibilidad con las reglas actuales. Reutilizar código requiere revisar su licencia. |
| [Quinielaticas](https://quinielaticas.com/) | Tablas, probabilidades/apostados, formación y seguimiento de una peña. | Ejemplo de presentar análisis semanal y transparencia. | La muestra utiliza hojas incrustadas; no se verificó una API de datos para terceros. |
| [ApuestaQuiniela](https://www.apuestaquiniela.es/quiniela) | Calendario, porcentajes, resultados e históricos de premios. | Segunda referencia para contrastar nombres y composición. | La página consultada mantenía partidos pendientes que SELAE ya mostraba finalizados: las instantáneas no son intercambiables. |
| [Quini15](https://www.quini15.es/) | Pronósticos declarados como generados por IA y páginas de análisis por partido. | Competidor directo de una experiencia explicativa sencilla. | No se observó prueba pública de calibración ni constructor por presupuesto en las páginas revisadas. |

En [un análisis de Quini15 de Liga F](https://www.quini15.es/jornada/2027/8/at-madrid-f-logrono-f) se observó una cabecera de Segunda División y una discrepancia entre el porcentaje de empate de la tabla y el usado en el texto. Es un ejemplo puntual, no una auditoría del producto. Sugiere un criterio útil para La Goyniela: explicaciones generadas desde el mismo dato validado, con identidad de competición y fecha visibles.

## Oportunidad de producto a validar

Un recorrido semanal: **entender el boleto → comparar estimaciones y sus límites → elegir cobertura dentro del presupuesto → revisar lo que ocurrió con la información que había disponible**.

Las hipótesis de utilidad son ahorrar búsquedas, evitar confundir porcentajes apostados con probabilidad deportiva y mostrar qué se gana o se pierde al añadir una cobertura. Ninguna exige un modelo propio de IA en la primera versión. Las explicaciones pueden ser plantillas deterministas basadas en hechos.

No se ha hecho una investigación con usuarios ni una evaluación estadística del rendimiento de competidores. La revisión de mercado no pretende ser un censo exhaustivo; sí cubre análisis, datos, combinatoria, peñas, código abierto y pronósticos con IA.

## Límites de esta entrega

Se hicieron consultas de lectura a fuentes públicas y análisis de muestras. No se crearon cuentas, contrataron servicios, obtuvieron permisos de terceros, desplegaron tareas recurrentes, entrenaron modelos ni enviaron mensajes o apuestas. Las API con clave se clasifican como documentadas y no verificadas con una cuenta gratuita.

La elección del MVP queda para la siguiente conversación, a partir de [las opciones y puertas de decisión](04-alcance-sdd.md).
