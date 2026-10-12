# Opciones de alcance y preparación SDD

Estado: **propuesta posterior a la investigación; MVP no aprobado**. Consultas de fuentes: 20/09/2026; cierre documental: 21/09/2026.

## Decisiones ya acordadas

- La Quiniela española; primero para uso personal y con amigos.
- Datos gratuitos de forma sostenible, no pruebas temporales de planes de pago.
- Obtención y preparación semanal automática. Configuración inicial admitida; no introducir partidos, porcentajes o archivos manualmente cada semana.
- Investigar contexto, probabilidades y construcción del boleto como necesidades conectadas.
- Seleccionar las funcionalidades después de conocer los datos. Esta entrega implementa la investigación, no la aplicación.

No se han decidido presupuesto de apuestas, interfaz, tecnología, alojamiento, gestión de grupos ni distribución pública. Son decisiones de producto para después de revisar la viabilidad; no hace falta decidirlas para cerrar esta investigación.

## Capacidades y dependencias

Los ID remiten al [catálogo](02-fuentes.md). Dificultad relativa estimada para un desarrollador: baja, media o alta; no son mediciones de rendimiento ni compromisos de plazo.

| Capacidad candidata | Datos mínimos | Viabilidad observada | Dificultad / prioridad propuesta |
|---|---|---|---|
| Detectar el boleto y mostrar sus 15 partidos | Q1 o Q2/Q3/Q4, composición y cierre por canal | Composición verificada; automatización oficial y condiciones pendientes | Media / imprescindible |
| Explicar forma, goles, localía y descanso | F1; F3 para ampliar cobertura | Históricos masculinos disponibles; descanso completo requiere todos los torneos | Media / núcleo con cobertura visible |
| Mostrar estimaciones 1X2 y su procedencia | F1/F4 o Q2 etiquetado como tercero | Disponibilidad parcial de cuotas y completa de porcentajes en la muestra | Media / imprescindible |
| Comparar estimación con porcentajes apostados | Q2 LAE activo más estimación deportiva | Viable donde LAE esté publicado; no necesita estar disponible al abrir jornada | Baja-media / opcional en primera versión |
| Justificar cada probabilidad mediante cuotas originales | F4 o F3 con acceso real, casas y timestamps | No demostrado para todos los partidos, especialmente Liga F | Media / ampliación condicionada |
| Proponer pleno 0/1/2/M | Distribución conjunta de goles o modelo explícito validado | Q2 aporta solo marginales; 1X2 no determina el pleno | Media-alta / puerta específica antes de recomendar un boleto completo |
| Generar columnas bajo presupuesto | Boleto válido, probabilidades, reglas y precio | Cálculo viable; depende de resolver los datos anteriores | Media / núcleo tras las puertas de datos |
| Dobles, triples y reducidas | Motor combinatorio y garantías verificadas | Referencias disponibles; exportación según reglas por verificar | Media-alta / empezar simple, reducidas después |
| Revisar recomendaciones pasadas | Instantáneas previas + resultados oficiales | Se puede empezar a registrar; no hay histórico temporal completo acreditado | Media / núcleo para aprender |
| Bajas, sanciones y posibles alineaciones | F3/C1/C2 y tiempo de publicación | Cobertura/actualidad gratuita no comprobadas | Alta / ampliación |
| xG y meteorología | F5/C3, partidos/estadios asociados | Acceso xG no validado; clima documentado, utilidad sin demostrar | Media-alta / experimento, no promesa |
| Maximizar retorno esperado | Todo lo anterior + recaudación, reparto y modelo de apuestas ajenas | No demostrado; depende de supuestos fuertes | Alta / fuera del MVP |

## Alcance recomendado y alternativas

### A. Asistente semanal trazable — recomendado, condicionado

Un panel del boleto con contexto disponible, estimaciones identificadas por fuente, diferencias frente a porcentajes apostados cuando estén activos y un constructor simple dentro del presupuesto. Cada recomendación conserva su instantánea y se compara después con el resultado oficial.

El usuario elige presupuesto y puede fijar preferencias de signos; la recopilación funciona automáticamente. Preferencias voluntarias sobre la apuesta no son entrada manual de datos de proveedores. Si no interviene, el sistema utiliza la configuración previamente elegida. La interfaz muestra la información faltante y no fabrica explicaciones para rellenarla.

Primera propuesta de objetivo del constructor: cobertura del 14 con columnas distintas y coste limitado, con la incertidumbre explicada. El pleno necesita superar su puerta de validación; si no la supera, se puede ofrecer análisis parcial, pero no llamarlo una recomendación completa de los 15.

**Ventaja:** puede aportar utilidad sin entrenar un modelo sofisticado. **Limitación:** seguiría dependiendo de algunas estimaciones de terceros y de sus condiciones. No está aprobado ni se garantiza que supere las puertas siguientes.

### B. Asistente con contexto completo

Añade bajas, sanciones, alineaciones previstas, contexto de todas las competiciones y cuotas originales por casa. Depende sobre todo de verificar el plan gratuito de API-Football para la temporada y de cubrir Liga F con procedencia temporal.

**Ventaja:** mejor explicación de cada encuentro. **Coste:** más integración, identidad de jugadores, control de actualidad y tratamiento de noticias. No es defendible prometerlo hoy con las fuentes verificadas.

### C. Laboratorio de probabilidades y optimización

Añade modelos propios, comparación prospectiva contra el mercado, reducidas avanzadas y análisis del retorno esperado. Necesita histórico sin información futura y un modelo de cómo se distribuyen las apuestas y premios.

**Ventaja:** permite investigar mejoras medibles. **Coste:** mayor trabajo estadístico y validación; no implica encontrar una ventaja. Propuesto como evolución, no como primera entrega para los amigos.

Si A no supera las puertas de datos, la alternativa coherente con “todo automático y gratuito” es limitar capacidades/jornadas explícitamente o mantener solo un laboratorio histórico. No cambiar silenciosamente a cargas manuales ni asumir proveedores de pago.

## Puertas para pasar de viabilidad a desarrollo

| ID | Incógnita | Comprobación concreta | Criterio de salida |
|---|---|---|---|
| G1 | Uso recurrente de Q2/Q3/Q4 y almacenamiento/uso compartido | Revisar autorización aplicable a las exportaciones; si es necesaria, obtener aclaración del titular antes de implantar la integración. | Condiciones documentadas compatibles o fuente alternativa. Ningún correo enviado en esta investigación. |
| G2 | Temporada y datos efectivos del plan gratuito F3 | Con una cuenta gratuita, consultar catálogo/temporadas y fixtures de Primera, Segunda y Liga F; medir cuotas, bajas, fechas y paginación sobre el boleto real. | Matriz por partido/campo y consumo real. Ninguna función basada solo en el catálogo comercial. |
| G3 | Cuotas trazables y pleno | Verificar mercados de F4/F3 y su hora; para pleno, comprobar distribución disponible o evaluar un modelo de goles con sus supuestos. | Identificar lo observado frente a lo estimado; 15 casillas cubiertas o limitar expresamente la salida. |
| G4 | Cierre, composición, resultados y casos excepcionales | Verificar canal escogido, cambio de jornada, sustituciones y resultado oficial; probar una jornada ordinaria y otra de composición diferente. | Identidad/versión inequívocas; ningún cierre derivado ciegamente de la hora del último partido. |
| G5 | Funcionamiento sostenido | Piloto sin apuestas durante al menos tres jornadas consecutivas, incluyendo una jornada entre semana o competición distinta cuando el calendario lo permita. | Operación sin importaciones manuales; cobertura, retraso, fallos y abstenciones registrados; si falta variedad, esa validación sigue pendiente. |

G2 necesita una clave configurada por su titular; no se solicitaron secretos en el chat ni se crearon cuentas. G5 necesita tiempo de observación futuro: no puede darse por superada con estas descargas. Estas puertas son conclusiones explícitas de la investigación, no tareas realizadas ni un impedimento para entregar el informe actual.

## Coste y operación

El coste objetivo de proveedores es 0 €. Eso no equivale a coste total cero. Un proceso programado en un equipo disponible puede evitar una nueva factura de alojamiento, pero depende de encendido, red y mantenimiento; no se ha elegido ni desplegado infraestructura. Un servicio alojado requeriría revisar almacenamiento, ejecución programada y acceso del grupo antes de presupuestarlo.

La operación deberá compartir caché entre usuarios, limitar consultas, guardar instantáneas permitidas y hacer visible la última actualización. No consultar todas las API en cada carga de pantalla. Monitorizar cobertura de las 15 casillas, tiempo desde actualización, errores de asociación, consumo del plan, ejecución previa al cierre y contradicciones entre datos y explicación. Una ejecución automática fallida es un estado que se informa, no una invitación a introducir porcentajes a mano.

## Cómo continuar con SDD

La documentación actual es la evidencia de descubrimiento. Cuando se elija alcance y se resuelvan las puertas aplicables, convertirlo en especificaciones pequeñas y trazables. No se impone un framework SDD ni se instala un generador.

| Especificación futura | Resultado observable | Evidencia/dependencia |
|---|---|---|
| SPEC-001 Jornada y fuentes | Detectar versión/cierre, asociar partidos y abstenerse ante ambigüedad | G1/G4; XML, fuentes oficiales y caso J8/J9 |
| SPEC-002 Contexto y probabilidades | Diferenciar hechos, porcentajes del público y estimaciones; conservar origen/tiempo | G2/G3; campos faltantes, margen y pleno |
| SPEC-003 Constructor | Columnas válidas, coste bajo presupuesto, objetivo y garantías visibles | SPEC-001/002; reglas verificadas y elección del objetivo |
| SPEC-004 Historial y evaluación | Reproducir lo recomendado antes del cierre y medir resultados posteriores | Instantáneas, resultados del concurso, protocolo temporal |

Cada especificación deberá contener: problema y usuario, ejemplos de entrada/salida, comportamiento dentro/fuera de alcance, dependencias, errores, criterios Given/When/Then y pruebas vinculadas. Después vendrán diseño técnico y tareas. Las decisiones pendientes se resuelven antes de aprobar la especificación correspondiente, no durante su implementación.

Interfaces conceptuales mínimas a preservar, sin fijar aún API HTTP ni base de datos:

- **Jornada:** temporada, número, versión del boleto, partidos ordenados, cierre y canal/origen.
- **Observación:** partido/competición, tipo de dato, valor, proveedor, consulta, publicación si se conoce y calidad.
- **Estimación:** 1X2 o pleno, método/versión, instante de corte, entradas y supuestos.
- **Propuesta de boleto:** configuración de presupuesto/objetivo, columnas, coste, cobertura estimada e instantánea de origen.

La trazabilidad deseada es **necesidad → evidencia → decisión → especificación → prueba**. La arquitectura, esquema y funcionalidades definitivas quedan sujetos a la revisión del usuario; esta entrega no los presenta como compromisos ya aceptados.
