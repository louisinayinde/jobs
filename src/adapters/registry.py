"""Source Registry (Feature 2.1) : fournit au Collector la liste des
sources à interroger.

Contrairement à `core.config.load_sources` (strict : lève `ConfigError`
sur toute entrée invalide, utilisé par les tests de validation de config),
`load_registry` est tolérant — une entrée cassée de `sources.yaml` est
ignorée et journalisée, plutôt que de faire échouer tout le chargement.
Le Collector doit pouvoir tourner même si une entrée est mal renseignée.
"""

from __future__ import annotations

import logging
from pathlib import Path

from src.adapters import ADAPTERS
from src.core.config import Source, _read_yaml

logger = logging.getLogger(__name__)

# ATS pris en charge, dérivé des adaptateurs réellement branchés (Feature 2.2) :
# ajouter un adaptateur à `ADAPTERS` suffit à le rendre acceptable dans
# `sources.yaml`, sans liste à maintenir en double ici.
KNOWN_ATS = frozenset(ADAPTERS)


def load_registry(path: str | Path) -> list[Source]:
    """Charge `sources.yaml` en une liste de `Source`, dans l'ordre du fichier.

    Une entrée est ignorée — avec un avertissement journalisé nommant la
    source quand son `nom` est exploitable — si :
    - ce n'est pas un mapping ;
    - il lui manque `nom`, `ats` ou `token` (ou un type invalide) ;
    - son `ats` n'est pas un adaptateur connu (`KNOWN_ATS`).

    Les autres entrées du fichier chargent normalement.
    """
    raw = _read_yaml(Path(path))
    if raw is None:
        return []
    if not isinstance(raw, list):
        logger.warning(
            "sources.yaml doit contenir une liste, reçu %s — registre vide",
            type(raw).__name__,
        )
        return []

    sources: list[Source] = []
    for index, entry in enumerate(raw):
        if not isinstance(entry, dict):
            logger.warning(
                "sources.yaml[%d] ignorée : entrée invalide, attendu un mapping", index
            )
            continue

        nom = entry.get("nom")
        label = nom if isinstance(nom, str) and nom else f"sources.yaml[{index}]"

        if not isinstance(nom, str) or not nom:
            logger.warning("%s ignorée : « nom » manquant ou invalide", label)
            continue

        ats = entry.get("ats")
        if not isinstance(ats, str) or not ats:
            logger.warning("source « %s » ignorée : « ats » manquant ou invalide", label)
            continue

        token = entry.get("token")
        if not isinstance(token, str) or not token:
            logger.warning("source « %s » ignorée : « token » manquant", label)
            continue

        if ats not in KNOWN_ATS:
            logger.warning("source « %s » ignorée : ats inconnu « %s »", label, ats)
            continue

        sources.append(Source(nom=nom, ats=ats, token=token))

    return sources
