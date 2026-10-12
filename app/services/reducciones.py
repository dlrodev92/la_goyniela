"""Official Quiniela reducciones (R1..R6) — fixed table of D/T + cost.

The reduced jugada is the official "primitiva" Quiniela product where
the peña pays for a fixed number of apuestas (vs. the full
2^D × 3^T of a múltiple). The server validates the column count + signs
match the reduction's D/T.

Source: loteriasyapuestas.es (verify before shipping — these are the
2026/27 official reductions; if SELAE updates them, this table is
the single source of truth to update).
"""
from __future__ import annotations

from typing import Final


class ReduccionSpec:
    __slots__ = ("code", "dobles", "triples", "apuestas")

    def __init__(self, code: str, dobles: int, triples: int, apuestas: int):
        self.code = code
        self.dobles = dobles
        self.triples = triples
        self.apuestas = apuestas

    @property
    def cost_at_075(self) -> float:
        return self.apuestas * 0.75


REDUCCIONES: Final[dict[str, ReduccionSpec]] = {
    "R1": ReduccionSpec("R1", dobles=0, triples=4, apuestas=9),
    "R2": ReduccionSpec("R2", dobles=7, triples=0, apuestas=16),
    "R3": ReduccionSpec("R3", dobles=3, triples=3, apuestas=24),
    "R4": ReduccionSpec("R4", dobles=6, triples=2, apuestas=64),
    "R5": ReduccionSpec("R5", dobles=0, triples=8, apuestas=81),
    "R6": ReduccionSpec("R6", dobles=11, triples=0, apuestas=132),
}


def get_reduccion(code: str) -> ReduccionSpec:
    if code not in REDUCCIONES:
        raise ValueError(f"unknown reduccion code: {code!r}; valid: {list(REDUCCIONES)}")
    return REDUCCIONES[code]


__all__ = ["REDUCCIONES", "ReduccionSpec", "get_reduccion"]
