"""Réglages partagés par toute la suite de tests.

Un seul, mais indispensable : neutraliser l'attente du retry réseau
(US-2.4.2). Sans lui, chaque test qui simule une panne dormirait deux
secondes pour de vrai — la suite passerait de trois secondes à plusieurs
minutes, et personne ne la lancerait plus.

La fixture rend la liste des délais demandés : un test qui veut vérifier
le backoff lui-même n'a qu'à la prendre en argument.
"""

from __future__ import annotations

import pytest

from src.adapters import base


@pytest.fixture(autouse=True)
def backoff_delays(monkeypatch: pytest.MonkeyPatch) -> list[float]:
    """Remplace l'attente entre deux tentatives par un simple enregistrement."""
    delays: list[float] = []
    monkeypatch.setattr(base, "_wait", delays.append)
    return delays
