# v6.22 — Perfil recovery para probar el recorrido completo

Cambio respecto a v6.21. Ataca WATCHING → ARMED en LIQUIDITY_SWEEP.
La apertura del descubrimiento de v6.20 no había abierto la confirmación del barrido.

En recovery, una vela cerrada de 5m que barre y recupera el nivel puede armar
sin esperar otra vela de 5m. La confirmación posterior de 1m y el precio
actual siguen decidiendo la ejecución. La recuperación exige cerrar por encima
del nivel para LONG o por debajo para SHORT, con profundidad, mecha, volumen,
contexto de tendencia, stop y objetivo válidos. No basta con aproximarse al nivel.

| Regla | Antes / strict | recovery v6.22 |
|---|---:|---:|
| Profundidad mínima del barrido | 0.15 ATR | 0.10 ATR |
| Mecha mínima | 32% | 20% |
| Volumen relativo mínimo del barrido | 0.70 | 0.55 |
| Calidad mínima para armar | 68 | 60 |
| Otra vela cerrada de 5m tras recuperar | Obligatoria | No obligatoria |
| Cuerpo mínimo si se evalúa una vela posterior | 24% | 12% |
| Volumen relativo de esa continuación | 0.65 | 0.50 |
| Posición de cierre LONG / SHORT de continuación | ≥62% / ≤38% | ≥52% / ≤48% |
| Extensión máxima de confirmación | 1.10 ATR | 1.50 ATR |
| Extensión máxima de zona de entrada | 0.90 ATR | 1.50 ATR |

Estos umbrales son parámetros de prueba, no valores optimizados mediante backtest.
Se conserva la fórmula de puntuación: bajar el mínimo de admisión no infla
la calidad calculada cambiando su escala. Las métricas y el perfil se incorporan
a los metadatos; la continuación informa todas las condiciones fallidas.

## Activación

Desplegar el proyecto completo o los tres módulos de estrategia juntos y reiniciar
el proceso del worker. Mantener:

```
TRADE_PREARM_PROFILE=recovery
TRADE_ARMED_ENTRY_ENABLED=true
TRADE_LEGACY_ENTRY_FALLBACK_ENABLED=false
TRADING_WORKER_ENABLED=true
```

No hay variables nuevas ni migraciones. `strict` conserva las reglas anteriores;
un nombre de perfil inválido también selecciona strict. El detector legacy sigue
usando sus reglas estrictas. Las modificaciones v6.21 están incluidas en el ZIP.

## Qué se verificó

- 299 pruebas pasaron en la suite completa. Tras conservar la escala original de
  puntuación, se repitieron las 73 pruebas de armado, recuperación y cotización:
  todas pasaron. Compilación Python correcta.
- 14 casos nuevos: simetría LONG/SHORT, strict/recovery, rechazo sin recuperación,
  sin volumen o sin mecha, y extensión observada en ASTER.
- Recorrido con velas sintéticas y componentes reales de estrategia, orquestador,
  riesgo y PaperExecutionEngine: WATCHING → ARMED → confirmación 1m → envío → fill.
  Incluye volumen del barrido que el umbral anterior rechazaba y cotización
  inicialmente envejecida 23.681 segundos, refrescada por el proveedor simulado.
- Con velas de 1m neutrales no se envió ninguna orden. Con confirmación válida
  se ejecutó exactamente una. Una llamada posterior no duplicó la operación.
- La prueba de métricas ASTER admite 1.306855 ATR en recovery; 2.416696 ATR sigue
  rechazado. No es una reproducción íntegra de los mercados del log.

Estas pruebas verifican el software con datos sintéticos. No se ha desplegado
esta versión desde aquí ni se ha enviado una operación a CoinW. La frecuencia
y calidad reales deben medirse tras el despliegue; no se garantiza una operación
en un plazo determinado.

## Seguimiento tras desplegar

En SETUP_ARMED del monitor buscar watch_trace.prearm_profile=recovery y
confirmation_mode=closed_5m_reclaim. Después comprobar TRIGGER, aprobación de
riesgo, submitted y filled en el embudo. Los metadatos conservan también el modo
closed_5m_continuation cuando la confirmación vino de una vela posterior.
Comparar tasas de paso y resultados por setup y perfil, evitando contar usuarios
replicados como oportunidades independientes. El score es una métrica interna,
no una probabilidad de éxito.

No se han cambiado tamaño de posición, apalancamiento, stop, mínimo RR,
protecciones de ejecución, cotización fresca, idempotencia ni reglas de breakout.
