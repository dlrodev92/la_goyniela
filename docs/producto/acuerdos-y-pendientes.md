# La Goyniela: acuerdos de producto y decisiones pendientes

Fecha de recopilación: **21 de septiembre de 2026**.

Este documento recopila la conversación de definición del proyecto. Su objetivo es permitir revisar el alcance y resolver las decisiones pendientes antes de convertirlo en especificaciones SDD y tareas de desarrollo.

**No es una descripción de funcionalidades ya implementadas.** Se ha realizado investigación de fuentes y herramientas; todavía no se ha construido la aplicación descrita aquí.

## 1. Cómo leer este documento

Se distinguen cuatro estados:

- **Acordado:** intención o comportamiento expresado por el usuario y aceptado durante la conversación.
- **Base aceptada, detalle pendiente:** se aceptó el planteamiento general, pero falta una regla precisa para implementarlo.
- **Propuesta pendiente:** recomendación del asistente que no recibió una aprobación específica posterior.
- **Dependencia por validar:** una fuente o capacidad está documentada, pero falta comprobar su acceso, cobertura o condiciones para nuestro caso.

Los últimos mensajes contienen propuestas del asistente sobre el presupuesto y el cierre que todavía no deben tratarse como decisiones definitivas. Un «OK» general al algoritmo tampoco resuelve todos sus casos límite.

La conversación más reciente acota la idea inicial de investigación extensa: queremos una herramienta sencilla para organizar una quiniela de grupo. Los informes anteriores son material de investigación y no convierten todas sus posibles funciones en requisitos del producto.

## 2. Objetivo y enfoque

### 2.1. Qué queremos construir

**Acordado.** Una aplicación para preparar La Quiniela española en peñas o grupos de amigos de **2 a 50 personas**.

La aplicación debe ayudar a:

1. Organizar usuarios y grupos.
2. Definir el presupuesto y la modalidad de cada jornada.
3. Consultar información útil de los partidos.
4. Recoger las propuestas manuales de los participantes.
5. Combinarlas en una jugada final del grupo.
6. Consultar y exportar esa jugada.

La expresión «jugada única del grupo» se refiere al resultado final acordado por el grupo. Puede contener varias columnas o combinaciones; no significa necesariamente una única apuesta sencilla.

### 2.2. Quién elige los resultados

**Acordado.** Los participantes marcan los resultados que consideran adecuados. La aplicación facilita datos y combina las propuestas; no sustituye sus decisiones por predicciones automáticas.

El presupuesto determina qué estructuras son posibles. No se ha definido una función que permita afirmar que una configuración sea universalmente «la mejor» o la más rentable.

### 2.3. Automatización

**Acordado.** La obtención y actualización periódica de los datos externos debe ser automática, una vez realizada la configuración inicial necesaria.

Esto no elimina la intervención humana en las decisiones del grupo: el administrador configura la jugada y las personas rellenan sus propuestas.

La compra o presentación efectiva de la apuesta ante un operador no forma parte del alcance definido. El producto prepara la jugada; no se ha acordado integrar la ejecución de apuestas ni pagos.

### 2.4. Forma de desarrollar

**Acordado.** Utilizar SDD —desarrollo guiado por especificaciones— para concretar primero lo que debe hacer el producto y desarrollarlo después.

Faltan la especificación funcional definitiva, los criterios de aceptación y las decisiones técnicas de implementación.

## 3. Usuarios, grupos y administración

### 3.1. Cuentas y pertenencia

**Acordado:**

- Cada persona dispone de una cuenta.
- Un usuario puede crear grupos e invitar a otros usuarios.
- El creador del grupo es su administrador.
- El grupo está pensado para entre 2 y 50 miembros.
- Cada grupo prepara sus propias jugadas.

**Pendiente:** método de registro, forma de invitación, aceptación de invitaciones, salida o expulsión de miembros, transferencia de administración y reglas para grupos que todavía tienen un único miembro.

No se ha fijado un límite de grupos por usuario.

### 3.2. Funciones del administrador

**Acordado o base aceptada:**

- Establecer el presupuesto de una jornada.
- Elegir la modalidad y la estructura de la jugada.
- Rellenar su propia propuesta.
- Generar la jugada con las propuestas recibidas, aunque no hayan participado todos.
- Resolver los empates de la combinación grupal.
- Confirmar el resultado final antes de su exportación, como base del flujo planteado.

El voto del administrador tiene el mismo peso que el de cualquier participante. Su función de resolver empates no equivale a un voto con más peso.

**Pendiente:** alcance de una posible edición manual de la jugada final, momento de la generación definitiva y posibilidad de delegar la administración.

## 4. Jornada y ciclo de participación

### 4.1. Configuración

**Acordado.** El administrador define presupuesto y modalidad antes de que los participantes rellenen la propuesta correspondiente.

Necesitamos identificar el boleto de la jornada: partidos, orden y horarios. La fuente definitiva para ello aún no está seleccionada.

### 4.2. Apertura y propuestas

**Acordado o base aceptada:**

- Los participantes introducen manualmente sus resultados.
- Pueden modificar sus propuestas mientras la jornada esté abierta.
- La forma de rellenarlas depende de la modalidad elegida.
- Se pueden calcular previsualizaciones durante la participación.
- La jugada puede generarse con quienes hayan enviado propuesta; no es necesario que participe todo el grupo.

**Pendiente:** distinguir formalmente borrador y propuesta enviada, validación de propuestas incompletas y visibilidad de los votos antes del cierre.

### 4.3. Hora de cierre

**Acordado.** Las propuestas dejan de estar abiertas **una hora antes del inicio del primer partido del boleto**.

No se refiere al primer partido del día para cada usuario ni al inicio de una liga completa.

**Pendiente:** qué ocurre si cambia un horario, si el primer partido se aplaza y si el administrador puede cerrar antes del límite.

### 4.4. Generación y confirmación

**Base aceptada.** La jugada se obtiene con las propuestas recibidas. Si hay empates u otras decisiones pendientes, el administrador debe resolverlos antes de confirmar el resultado definitivo.

Se planteó bloquear las propuestas al cierre y mantener la jugada pendiente de confirmación si quedan decisiones por resolver.

**Propuesta pendiente del último mensaje:** generar automáticamente un borrador final al cierre, confirmar después por el administrador y no ofrecer cierre anticipado en la primera versión.

Hay que fijar esta secuencia en la especificación; todavía no se ha decidido completamente qué acciones son automáticas y cuáles requieren pulsar un botón.

## 5. Modalidades de jugada

### 5.1. Sencillas

**Acordado.** Permitir apuestas sencillas, con un signo por cada uno de los primeros 14 partidos y la selección correspondiente del Pleno al 15.

Durante la conversación se ha usado **0,75 € por apuesta sencilla**, tal como aparece en la captura aportada. La captura también muestra un mínimo de dos apuestas sencillas.

Estos importes y restricciones constituyen la base de los ejemplos, no una verificación independiente de todas las reglas actuales del operador. Deben validarse antes de implementar la generación final.

### 5.2. Múltiples directas

**Acordado.** Permitir una propuesta con signos fijos, dobles y triples.

Para los primeros 14 partidos, el desarrollo completo de una columna con `d` dobles y `t` triples produce:

```text
Número de apuestas sencillas = 2^d × 3^t
Coste del desarrollo = número de apuestas × precio unitario
```

Esta fórmula no es la fórmula de una reducción. Tampoco cierra las reglas de selección del Pleno al 15.

### 5.3. Reducidas

**Acordado.** Usar como base las seis reducidas que aparecen en la captura del usuario:

| Reducida | Configuración | Apuestas desarrolladas | Coste mostrado |
|---|---|---:|---:|
| 1.ª | 4 triples | 9 | 6,75 € |
| 2.ª | 7 dobles | 16 | 12,00 € |
| 3.ª | 3 dobles y 3 triples | 24 | 18,00 € |
| 4.ª | 2 triples y 6 dobles | 64 | 48,00 € |
| 5.ª | 8 triples | 81 | 60,75 € |
| 6.ª | 11 dobles | 132 | 99,00 € |

**Pendiente:** verificar el desarrollo exacto de cada reducción, sus condiciones y garantías aplicables. No basta con almacenar esta tabla de precios; hay que generar o representar correctamente las combinaciones que se juegan.

Las reducidas no cubren todas las combinaciones del desarrollo completo de sus dobles y triples.

### 5.4. Modo por columnas, como juega el grupo de amigos

**Acordado.** Es una modalidad central y debe existir además de las anteriores.

Ejemplo: el grupo decide jugar cuatro columnas sencillas. En los partidos que considera claros puede repetir el mismo signo en las cuatro columnas. En cuatro o cinco partidos más difíciles puede introducir alternativas diferentes entre columnas.

Esto significa que se juegan esas cuatro combinaciones concretas. No se genera automáticamente el producto cartesiano de todos los signos distintos utilizados.

Cada participante rellena el mismo número de columnas, definido en la configuración de la jornada. La combinación grupal se plantea comparando las propuestas **columna a columna y partido a partido**.

### 5.5. Relación entre sencillas y modo por columnas

**Pendiente.** Funcionalmente, el modo por columnas produce apuestas sencillas. Falta decidir si «sencillas» y «por columnas» deben aparecer como dos opciones distintas en la interfaz o como una misma opción con una explicación clara.

### 5.6. Otras modalidades

Se mencionó la existencia de condicionadas y otras formas de construir jugadas, pero no se incorporaron al alcance acordado. La base actual son las modalidades anteriores.

## 6. Presupuesto y distribución del dinero

### 6.1. Necesidad principal

**Acordado.** El administrador debe poder indicar cuánto dinero tiene el grupo y elegir cómo distribuirlo en una estructura de apuestas.

No todos los grupos quieren la misma distribución: algunos prefieren más columnas base y otros concentrar dobles o triples en menos propuestas.

También se mencionaron presupuestos elevados —por ejemplo 500 €, 1.000 € o 3.000 €—. No se han fijado límites de producto ni de cómputo para esos casos.

### 6.2. Diferencia entre columna base y apuesta sencilla desarrollada

Para evitar confusiones en el producto debemos distinguir:

- **Columna base o bloque:** una propuesta que puede contener dobles o triples.
- **Apuesta sencilla desarrollada:** una combinación concreta que forma parte de lo que se juega y que tiene un coste unitario.

Una base con dos dobles representa cuatro apuestas sencillas. Por eso decir «quiero dos apuestas sencillas y añadir dobles» requiere precisar si realmente se quieren dos bases.

Los nombres definitivos que verá el usuario siguen pendientes.

### 6.3. Ejemplos tratados

Con el precio de referencia de 0,75 €, un presupuesto de 5 € permite seis apuestas sencillas por 4,50 €, con 0,50 € de sobrante.

Si se parte de **dos bases**, un ejemplo de reparto es:

- Base A con dos dobles: cuatro apuestas.
- Base B con un doble: dos apuestas.
- Total: seis apuestas, 4,50 €.

Si se parte de **tres bases**, se comentaron estos repartos de apuestas desarrolladas:

| Reparto | Ejemplo de estructura | Total |
|---|---|---:|
| 2 + 2 + 2 | Un doble en cada base | 4,50 € |
| 4 + 1 + 1 | Dos dobles en una base y dos bases sencillas | 4,50 € |
| 3 + 2 + 1 | Una base con un triple, otra con un doble y otra sencilla | 4,50 € |

Son ejemplos de distribución lógica. No demuestran que todas las estructuras puedan introducirse juntas en un único formulario de un operador.

### 6.4. Flujo propuesto, aún pendiente de confirmación específica

1. Introducir el presupuesto máximo.
2. Elegir modalidad.
3. Ver las configuraciones compatibles, su coste y sobrante.
4. Seleccionar una configuración.
5. Abrir la participación.

Se propuso que el sobrante no se gaste automáticamente y que el administrador decida si cambia la configuración.

### 6.5. Lo que todavía no está decidido

- Si el MVP admite varias bases con estructuras diferentes en una misma jornada o una sola configuración homogénea.
- Cómo rellenan los participantes las propuestas cuando hay varias bases múltiples.
- Si mostramos todas las estructuras posibles o una selección ordenada.
- Con qué criterio se ordenan: coste, aprovechamiento del presupuesto, cantidad de columnas u otro.
- Cuáles son los límites máximos y cómo evitamos enumerar volúmenes innecesarios de combinaciones.
- Cómo se tratan combinaciones duplicadas entre bases.
- Cómo se refleja el mínimo de apuestas del canal donde finalmente se jugará.

No se ha acordado que las cuotas determinen automáticamente la distribución del dinero ni que la herramienta optimice el beneficio esperado.

## 7. Algoritmo para la jugada grupal

### 7.1. Igualdad de participación

**Acordado.** Cada participante tiene el mismo peso, incluido el administrador.

La base planteada cuenta propuestas enviadas y completas, excluye borradores y no asigna votos a quienes no participan. Si no hay ninguna propuesta, no se genera una jugada.

La definición exacta de «completa» debe fijarse para cada modalidad.

### 7.2. Combinación por columnas

**Base aceptada.** Se compara la columna 1 de todos los participantes, luego la 2, etc. Dentro de cada columna se vota cada partido.

Ejemplo con diez participantes:

| Signo | Votos |
|---|---:|
| 1 | 6 |
| X | 3 |
| 2 | 1 |

Con el criterio de mayoría, el signo final sería `1`.

**Matiz que requiere confirmación explícita:** el usuario habló inicialmente de una media o regla de tres. Después se preguntó si quería mayoría por columna o reparto proporcional entre columnas, sin recibir una elección directa entre ambas opciones. Tras un OK general, la mayoría se tomó como base de trabajo.

Por tanto, la propuesta actual es **mayoría por columna**, pero debe confirmarse antes de convertirla en regla definitiva.

La diferencia es relevante: con cuatro columnas y un 75 % de apoyo al `1`, una mayoría independiente puede producir cuatro `1`; un reparto proporcional podría buscar tres `1` y una `X`. No son el mismo algoritmo.

### 7.3. Dobles y triples en las propuestas individuales

**Base aceptada.** En los modos múltiples y reducidos, cada usuario rellena una columna con los signos, dobles y triples permitidos por la configuración.

El voto de cada persona se reparte entre sus signos seleccionados:

| Selección | Aportación al 1 | Aportación a la X | Aportación al 2 |
|---|---:|---:|---:|
| 1 | 1 | 0 | 0 |
| X | 0 | 1 | 0 |
| 2 | 0 | 0 | 1 |
| 1X | 0,5 | 0,5 | 0 |
| 12 | 0,5 | 0 | 0,5 |
| X2 | 0 | 0,5 | 0,5 |
| 1X2 | 1/3 | 1/3 | 1/3 |

Cada persona aporta en total un punto por partido, independientemente de cuántos signos marque.

Los porcentajes grupales se obtienen sumando las aportaciones y dividiendo por el número de propuestas válidas.

### 7.4. Asignación final de fijos, dobles y triples

**Base aceptada, fórmula pendiente.** La idea es utilizar signos fijos donde exista más acuerdo y alternativas donde el voto esté más repartido, respetando la modalidad y el presupuesto seleccionados.

Falta decidir:

- Qué medida concreta identifica el desacuerdo.
- Qué partidos reciben los dobles y cuáles los triples.
- Cómo elegimos los dos signos de un doble.
- Si cada propuesta individual debe usar exactamente todos los dobles/triples configurados o puede usar menos.
- Cómo resolvemos empates tanto de signos como de prioridad entre partidos.
- Cómo aplicamos el desarrollo de una reducida a la selección agregada.

No se ha acordado una fórmula matemática definitiva ni un optimizador concreto.

### 7.5. Columnas repetidas

**Base planteada y aceptada de forma general.** Si dos columnas finales quedan idénticas, se avisa al administrador. No se sustituyen signos automáticamente sin intervención.

**Pendiente:** si se permite confirmar duplicados, si se eliminan y baja el coste, o si se exige resolverlos. También falta definir qué modificaciones puede hacer el administrador y cómo se presentan al grupo.

### 7.6. Empates

**Acordado como base.** El administrador decide cuando hay empate.

**Pendiente:** qué ocurre si no resuelve antes del inicio de los partidos. No se ha acordado un desempate aleatorio, por cuotas, por historial ni por prioridad fija de signos.

## 8. Pleno al 15

**Base aceptada con el OK general al algoritmo:**

- Cada persona selecciona un marcador completo.
- Para cada equipo utiliza `0`, `1`, `2` o `M`; `M` representa tres o más goles.
- Se elige el marcador completo más votado.
- En el modo por columnas, se introduce un marcador por columna.
- Los empates se remiten al administrador.

Elegir el marcador completo evita mezclar por separado los goles más votados de cada equipo y producir una combinación que nadie eligió.

**Pendiente:** si se admitirán varias selecciones de Pleno al 15 en múltiples o reducidas, cómo afecta al coste y cómo se adapta al canal de apuesta. No se ha decidido ofrecer esa ampliación.

## 9. Información de los partidos

### 9.1. Histórico deportivo

**Acordado.** Mostrar:

- Los cinco últimos enfrentamientos entre los dos equipos.
- Los cinco últimos partidos de cada equipo.
- Fecha, rival, local/visitante y resultado, con identificación de la competición cuando esté disponible.

La intención es aportar contexto al usuario para que elija sus signos.

**Pendiente:** si los últimos cinco incluyen todas las competiciones oficiales o solo la liga, si se excluyen expresamente amistosos y qué se muestra cuando hay menos de cinco encuentros disponibles.

La consulta debe corresponder a partidos anteriores al encuentro analizado. Debemos distinguir equipos masculinos y femeninos aunque compartan nombre de club.

### 9.2. Fuente candidata del histórico

API-Football fue identificada como candidata mediante consultas como:

```http
GET /fixtures?team=ID_EQUIPO&last=5
GET /fixtures/headtohead?h2h=ID_A-ID_B&last=5
```

**Dependencia por validar.** La solución conceptual del histórico se dio por resuelta, pero falta probar con una clave el acceso a la temporada y la cobertura necesarias. El plan gratuito limita las temporadas disponibles.

Primera, Segunda y Liga F son relevantes para las jornadas investigadas. No debemos dar por hecho que una fuente cubra todos los partidos de todos los boletos.

### 9.3. Cuotas

**Acordado.** Queremos mostrar cuotas `1/X/2` de tres casas del **mercado español**, cuando estén disponibles.

La propuesta de usar William Hill UK, Unibet UK y Betfair Sportsbook UK fue corregida por el usuario: las versiones británicas no satisfacen ese requisito.

Se identificaron como candidatas anunciadas en Odds-API.io:

- Luckia ES.
- Retabet ES.
- Bwin ES.

No se eligieron como las tres mayores del mercado ni se validó que devuelvan todas las competiciones necesarias.

**Estado acordado:** dejar la función definida y pendiente de encontrar una API gratuita adecuada.

Hallazgos de la investigación de esta conversación:

- The Odds API dispone de un plan gratuito, pero la selección británica propuesta no cumple el requisito español.
- Odds-API.io anuncia versiones españolas, pero su página de precios indica que las nuevas claves gratuitas están suspendidas. El plan anunciado para hasta cinco casas cuesta 99 £/mes en la fecha de consulta.
- Bwin ES dispone de documentación oficial de API, pero exige solicitar y obtener acceso. No se ha confirmado gratuidad para este proyecto.

Las tarifas y condiciones pueden cambiar. Estos hallazgos no equivalen a una contratación ni a una integración probada.

La aplicación debe mostrar la fecha de actualización de las cuotas y «Sin datos» cuando falten. La ausencia de una casa no debe obligar a inventar o completar cuotas con otro mercado regional.

### 9.4. Notas relevantes: bajas y sanciones

**Base aceptada.** Mantener esta sección sencilla: información estructurada de bajas y sanciones, sin incorporar noticias generales o rumores a la primera versión.

API-Football es la primera candidata, con:

```http
GET /injuries?fixture=ID_PARTIDO
```

La documentación describe información de jugadores ausentes y motivos, incluidas lesiones y sanciones. El endpoint figura en el plan gratuito de 100 peticiones diarias, con las restricciones de temporadas del plan.

**Pendiente de validar:** cobertura real por competición y temporada mediante `coverage.injuries`, acceso a la temporada actual, calidad y disponibilidad previa al cierre.

Propuesta de presentación aceptada como base:

- Jugador.
- Equipo.
- Motivo de la ausencia.
- Estado cuando esté disponible.
- Fecha de actualización.

Una respuesta vacía no demuestra que no existan bajas. Mostrar «Sin información de bajas» cuando no se disponga de datos suficientes.

Sportmonks fue identificado como alternativa con datos `sidelined`, pero su plan gratuito no cubre las competiciones españolas necesarias.

## 10. Arquitectura de recopilación y caché

### 10.1. Flujo acordado

```text
Proveedor externo
    ↓ consultas programadas desde nuestro servidor
Proceso de recopilación y normalización
    ↓
Nuestra base de datos
    ↓
Nuestra API
    ↓
Cliente web
```

**Acordado:**

- La aplicación tiene su propia API.
- Las consultas a proveedores se hacen desde el servidor.
- Los datos se guardan o cachean centralmente.
- Se comparten entre todos los grupos que consultan esos partidos.
- Abrir o recargar la web no provoca una consulta al proveedor externo.
- Una tarea programada, por ejemplo un cron, mantiene los datos actualizados.

El número de grupos o visitas no debe multiplicar directamente el número de peticiones al proveedor.

### 10.2. Frecuencia

Se propuso consultar cuotas y bajas dos veces al día y hacer una actualización adicional antes del cierre.

**Propuesta operativa pendiente de fijar:** horarios exactos, frecuencia por tipo de dato, gestión de varias jornadas simultáneas y adaptación a las cuotas de cada proveedor.

No es necesario actualizar el histórico deportivo a la misma frecuencia que las cuotas.

### 10.3. Persistencia y fallos

La base de datos puede servir inicialmente como almacenamiento compartido de los datos externos. No se ha acordado añadir Redis ni otra infraestructura específica.

**Pendiente:** cuándo caduca un dato, cuánto tiempo se conserva, qué hacer si falla una actualización y cómo avisar de que la información está desactualizada.

También hay que validar que las condiciones del proveedor permitan almacenar y mostrar los datos en nuestro producto. Tener una API propia no elimina las condiciones de la fuente externa.

## 11. Qué información del grupo conservamos

### 11.1. Propuestas individuales

**Acordado.** No queremos un histórico permanente de todos los votos de todos los participantes.

Las propuestas se conservan mientras sean necesarias para editar, previsualizar y generar la jugada. La base planteada es eliminarlas después de confirmar la definitiva y resolver decisiones pendientes.

**Pendiente:** momento exacto de eliminación, posibilidad de corregir una confirmación y política para copias de seguridad. No ejecutar borrados antes de guardar correctamente la jugada final.

### 11.2. Jugada final

**Acordado.** Conservar al menos:

- Grupo.
- Jornada.
- Modalidad.
- Selección y combinaciones finales necesarias para representar lo que se va a jugar.
- Coste.
- Fecha de confirmación, como metadato propuesto para identificar la versión definitiva.

No se ha acordado almacenar un historial completo de todas las previsualizaciones ni una auditoría detallada por usuario.

### 11.3. Presentación y exportación

Inicialmente se consideraron PDF, Markdown o una vista web. La base más reciente es **vista web con impresión o guardado como PDF**, evitando almacenar un PDF separado si no hace falta.

**Pendiente:** diseño final y formato de exportación definitivo. Este documento de planificación en Markdown no implica que Markdown deba ser un formato de exportación de apuestas.

La jugada exportada representa el acuerdo del grupo. No acredita que se haya comprado o presentado: el resguardo del operador sería un documento distinto.

## 12. Funciones propuestas que todavía no están confirmadas

Estas recomendaciones aparecieron en los últimos mensajes y requieren aceptación explícita:

1. **Estado de participación:** mostrar cuántos han enviado propuesta, por ejemplo «8 de 12».
2. **Borradores:** guardar una propuesta incompleta para retomarla y enviarla después.
3. **Votos ocultos hasta el cierre:** evitar que las propuestas de otros condicionen la elección personal.
4. **Bloqueo de configuración tras recibir propuestas:** cambiar modalidad o columnas exigiría reiniciar la participación con aviso.
5. **Sin cierre anticipado en el MVP:** mantener abierto hasta el límite establecido.
6. **Generación automática al cierre:** producir el borrador y dejar la confirmación al administrador.
7. **Presupuesto sin cobros:** el administrador registra el dinero disponible y las aportaciones se organizan fuera de la aplicación.
8. **Sobrante sin gasto automático:** mostrarlo y dejar que el administrador decida.

También se propuso posponer chat, notificaciones externas, pagos y seguimiento automático de premios. Son propuestas de recorte de alcance, no funcionalidades aprobadas para una fase posterior.

## 13. Inventario de decisiones pendientes

Las casillas representan decisiones por resolver, no tareas de programación ya aprobadas. El orden agrupa primero lo que condiciona más el producto.

### A. Algoritmo y resultado de la jugada

- [ ] **ALG-01 — Mayoría o proporcionalidad.** Confirmar expresamente mayoría independiente por columna o reparto proporcional entre columnas. Base actual: mayoría por columna.
- [ ] **ALG-02 — Restricciones individuales.** ¿Cada usuario debe utilizar exactamente los dobles y triples configurados o puede usar menos?
- [ ] **ALG-03 — Medida de desacuerdo.** Definir cómo ordenamos los partidos para asignar dobles y triples.
- [ ] **ALG-04 — Selección de alternativas.** Fijar cómo se escogen los dos signos de cada doble y cómo se resuelven empates entre partidos candidatos.
- [ ] **ALG-05 — Columnas duplicadas.** Decidir si se permiten, se eliminan o se exige modificarlas, y qué ocurre con el coste.
- [ ] **ALG-06 — Autoridad del administrador.** ¿Puede editar cualquier resultado final o solo resolver empates y duplicados?
- [ ] **ALG-07 — Empates sin resolver.** Definir qué ocurre si el administrador no confirma a tiempo.
- [ ] **ALG-08 — Pleno al 15.** Confirmar la base propuesta y decidir si admitimos varias selecciones en múltiples/reducidas o un solo marcador.
- [ ] **ALG-09 — Reducidas.** Validar los desarrollos concretos, su representación y las garantías que se pueden explicar al usuario.

### B. Presupuesto y modalidades

- [ ] **PRE-01 — Flujo de configuración.** Confirmar presupuesto → modalidad → opciones compatibles → selección → apertura.
- [ ] **PRE-02 — Nombres y opciones.** Resolver si sencillas y por columnas son la misma modalidad en la interfaz; distinguir bases de apuestas desarrolladas.
- [ ] **PRE-03 — Varias bases.** Decidir si el MVP permite repartir el presupuesto entre bases con diferentes dobles/triples y cómo se rellenan.
- [ ] **PRE-04 — Criterio de recomendación.** Mostrar todas las estructuras o una selección; definir su orden sin prometer rentabilidad.
- [ ] **PRE-05 — Sobrante.** Confirmar que no se gasta automáticamente ni se arrastra a otra jornada sin decisión del grupo.
- [ ] **PRE-06 — Límites y precio.** Verificar precio, mínimos del canal de apuesta y máximos de presupuesto/columnas que soportará el producto.
- [ ] **PRE-07 — Aportaciones.** Confirmar si solo registramos presupuesto total o también aportaciones individuales. No hay cobros integrados acordados.

### C. Participación, cierre y permisos

- [ ] **FLU-01 — Borrador y envío.** Confirmar guardado parcial y qué requisitos hacen válida una propuesta enviada.
- [ ] **FLU-02 — Visibilidad.** Decidir si se ven las propuestas ajenas o porcentajes agregados durante la participación, y quién puede verlos.
- [ ] **FLU-03 — Cambios de configuración.** Decidir qué ocurre con las propuestas existentes si el administrador cambia presupuesto o modalidad.
- [ ] **FLU-04 — Generación.** Precisar cuándo puede generar el administrador y qué se genera automáticamente al cierre.
- [ ] **FLU-05 — Cierre anticipado.** Permitirlo o excluirlo del MVP.
- [ ] **FLU-06 — Confirmación.** Determinar si la jugada confirmada puede reabrirse o corregirse y hasta cuándo.
- [ ] **FLU-07 — Participación mínima.** Confirmar los casos de cero propuestas y una única propuesta válida dentro de un grupo.
- [ ] **FLU-08 — Invitaciones y miembros.** Elegir mecanismo de invitación y reglas para entradas, salidas o expulsiones durante una jornada abierta.
- [ ] **FLU-09 — Administración.** Definir transferencia del rol y qué ocurre si el administrador abandona el grupo.
- [ ] **FLU-10 — Estado de participación.** Confirmar contador de propuestas y si se identifican las personas pendientes.

### D. Datos e integraciones

- [ ] **DAT-01 — Boleto oficial.** Elegir y verificar fuente de composición, orden, identificador de jornada y horarios.
- [ ] **DAT-02 — Cambios de calendario.** Definir comportamiento ante aplazamientos, cancelaciones y cambios del primer partido.
- [ ] **DAT-03 — Alcance del histórico.** Elegir liga o todas las competiciones oficiales; tratamiento de amistosos y menos de cinco resultados.
- [ ] **DAT-04 — Acceso al histórico.** Probar con clave la temporada y cobertura necesarias del proveedor elegido.
- [ ] **DAT-05 — Cuotas españolas gratuitas.** Encontrar proveedor adecuado y validar tres casas, mercado 1X2, competiciones y condiciones de uso.
- [ ] **DAT-06 — Bajas y sanciones.** Probar cobertura real por liga/temporada y cómo interpretar estados y motivos.
- [ ] **DAT-07 — Identidad de equipos y partidos.** Definir correspondencias entre fuentes sin mezclar equipos masculinos/femeninos ni competiciones.
- [ ] **DAT-08 — Actualización y fallos.** Fijar cron, caducidad, conservación de la última información válida y avisos de datos desactualizados.
- [ ] **DAT-09 — Uso de los datos.** Confirmar permisos de almacenamiento y presentación para los proveedores elegidos.

### E. Conservación, exportación y alcance del MVP

- [ ] **SAL-01 — Eliminación de votos.** Fijar el momento y la relación con correcciones y copias de seguridad.
- [ ] **SAL-02 — Jugadas finales guardadas.** Decidir cuánto se conservan y cómo se consultan jornadas anteriores.
- [ ] **SAL-03 — Exportación.** Confirmar vista imprimible/PDF y el detalle necesario de las combinaciones, especialmente en reducidas.
- [ ] **SAL-04 — Resguardo real.** Decidir si queda completamente fuera o si más adelante se podrá adjuntar manualmente.
- [ ] **MVP-01 — Recorte definitivo.** Confirmar que pagos, chat, notificaciones externas y seguimiento de premios quedan fuera de la primera versión.
- [ ] **MVP-02 — Registro.** Elegir método de acceso y recuperación de cuenta.
- [ ] **MVP-03 — Implementación.** Seleccionar tecnologías, alojamiento, base de datos y ejecución de tareas programadas cuando el alcance funcional esté cerrado.
- [ ] **MVP-04 — Especificaciones SDD.** Convertir los acuerdos en flujos y criterios de aceptación, con ejemplos de votos, presupuestos, empates, reducidas y fallos de datos.

## 14. Orden recomendado para continuar

1. Confirmar el criterio de mayoría y cerrar las reglas de agregación.
2. Definir el gestor de presupuesto, sobre todo si habrá varias bases diferentes.
3. Cerrar apertura, cambios, cierre y confirmación.
4. Definir permisos, conservación y exportación.
5. Validar las integraciones y registrar qué partes del producto pueden funcionar sin ellas.
6. Redactar las especificaciones del MVP y sus criterios de aceptación.
7. Elegir la implementación y desarrollar por bloques verificables.

La disponibilidad de cuotas no debe confundirse con el funcionamiento de los grupos y la votación: la propuesta actual permite avanzar con la preparación de jugadas aunque algunas fichas de partido todavía no dispongan de datos externos.

## 15. Referencias y documentación previa

Estas fuentes respaldan las capacidades investigadas, no una garantía de acceso gratuito ni de cobertura completa. Sus condiciones deben revisarse antes de integrar.

- [API-Football: guía y endpoints](https://www.api-football.com/news/post/how-to-get-started-with-api-football-the-complete-beginners-guide).
- [API-Football: planes y restricciones](https://www.api-football.com/pricing).
- [The Odds API: servicio y planes](https://the-odds-api.com/).
- [The Odds API: casas disponibles por región](https://the-odds-api.com/sports-odds-data/bookmaker-apis.html).
- [Odds-API.io: catálogo europeo](https://odds-api.io/sportsbooks/europe).
- [Odds-API.io: precios y estado del plan gratuito](https://odds-api.io/pricing).
- [Bwin ES: solicitud de acceso](https://sportsapi.bwin.es/articles/getinvolved.html).
- [Sportmonks: lesiones y sanciones](https://www.sportmonks.com/glossary/injuries-and-suspensions/).
- Captura aportada por el usuario: modalidades y seis reducidas. No se copia la imagen personal al repositorio.

Investigación previa del proyecto:

- [Viabilidad y herramientas](../research/01-viabilidad.md).
- [Fuentes de datos](../research/02-fuentes.md).
- [Verificación técnica](../research/03-verificacion.md).
- [Alcance inicial y SDD](../research/04-alcance-sdd.md).

**Prevalencia funcional:** para interpretar la intención actual del producto, esta recopilación recoge las precisiones posteriores de la conversación. Los informes iniciales conservan su valor como investigación; sus propuestas de alcance no deben asumirse automáticamente como funciones acordadas.
