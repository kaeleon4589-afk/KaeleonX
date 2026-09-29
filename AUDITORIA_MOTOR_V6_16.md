# KAELEON v6.16 — Expansión del universo de mercado

## Motivo
Los logs de producción mostraron `ENGINE_HEARTBEAT ... markets_scanned: 12`, confirmando que el discovery estaba limitado a una shortlist de 12 mercados aunque CoinW disponga de un universo mucho mayor.

## Cambio
Se amplía el universo de discovery sin relajar la calidad de las estrategias:

- `MARKET_SCANNER_DEPTH`: 12 → 30
- `MARKET_SCANNER_PARALLEL`: 3 → 5
- `MARKET_SCANNER_CACHE_SECONDS`: permanece en 30

También se alinean los defaults internos de `Settings`, `CoinWMarketScanner` y `MultiMarketCoordinator` para evitar que una ejecución sin variables explícitas vuelva accidentalmente a 12/3.

## No modificado
No se modifican:

- BREAKOUT_RETEST
- LIQUIDITY_SWEEP
- RR mínimo ni targets
- Stop Loss
- confirmación 1m/5m
- anti-chase
- HTF/regime filters
- tamaño de posición

## Efecto esperado
La plataforma mantiene los mismos criterios de calidad, pero permite que más mercados compitan por producir setups válidos. WATCHING y ARMED siguen siendo monitorizados por sus rutas prioritarias independientes del scanner rotatorio.

## Railway
Si Railway ya tiene definidas `MARKET_SCANNER_DEPTH` o `MARKET_SCANNER_PARALLEL`, esas variables de entorno tienen prioridad sobre el default del código y deben quedar en `30` y `5` respectivamente.
