# KAELEON Engine V2

## Objetivo

Reemplazar el árbol de decisión heredado del motor v6 por un pipeline pequeño y auditable. La ejecución, CoinW, balances, persistencia, Telegram y PositionManager no se reescriben.

## Flujo de producción

```text
Market data
  -> RegimeEngineV2
     -> TREND       -> BreakoutRetestStrategyV2
     -> RANGE       -> LiquiditySweepStrategyV2
     -> SHOCK       -> NO_TRADE
     -> DEAD        -> NO_TRADE
     -> TRANSITION  -> NO_TRADE
  -> WATCH (solo precursor válido)
  -> ARMED (estructura completa)
  -> 1m + order-book confirmation
  -> TradeIntent
  -> risk / execution / position manager existentes
```

## BREAKOUT_RETEST_V2

- Contexto 15m + 1h alineado.
- Breakout cerrado en 5m, con cuerpo, volumen y extensión controlados.
- Retest real del nivel; fallo estructural cancela el setup.
- Confirmación de continuación en vela cerrada.
- No persigue entradas desarrolladas.
- SL detrás de la estructura con piso de ruido ATR.
- TP estructural con tope RR configurable.

## LIQUIDITY_SWEEP_V2

- Solo opera en régimen RANGE.
- No está obligado a seguir la tendencia previa.
- Detecta barrido de un extremo de liquidez de 30 velas.
- Exige reclaim del nivel y confirmación de reversión.
- Opera hacia el lado opuesto del rango.
- Rechaza sweep sin wick/reclaim/espacio RR suficiente.

## Entry lifecycle V2

El nuevo `EntryLifecycleV2` mantiene el contrato de recuperación existente, pero reduce el árbol de decisión a tres estados claros:

- `WATCH`: precursor todavía incompleto.
- `ARMED`: estructura válida, esperando confirmación 1m/live.
- `TRIGGERED`: quote ejecutable + micro-confirmación + geometría RR válida.

Un setup consumido no puede abrir una segunda operación.

## Compatibilidad

Los módulos v6 antiguos permanecen físicamente en el repositorio como rollback. `StrategyRouter` solo usa el motor legado cuando recibe etiquetas de régimen antiguas explícitas (`TREND_CONTINUATION`, `VOLATILE_SWEEP`, etc.). `RegimeEngineV2` de producción no emite esas etiquetas.
