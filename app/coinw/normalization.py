"""Normalize CoinW contract quantities without treating face value as total size."""
import math


def base_quantity(row, entry):
    face = float(row.get('baseSize') or 0)
    if row.get('currentPiece') is not None and face > 0:
        value = float(row['currentPiece']) * face
    else:
        unit = int(row.get('quantityUnit') or 0)
        quantity = float(row.get('quantity') or 0)
        value = quantity / entry if unit == 0 and entry > 0 else quantity * face if unit == 1 else quantity
    if not math.isfinite(value) or value < 0:
        raise ValueError('invalid_exchange_quantity')
    return value
