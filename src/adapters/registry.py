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
    - il lui manque `nom` ou `ats` (ou leur type est invalide) ;
    - son `ats` n'est pas un adaptateur connu (`KNOWN_ATS`) ;
    - il lui manque `token` **alors que son adaptateur en exige un**.

    Ce dernier point est ce qui distingue un ATS d'un agrégateur : le token
    désigne le board d'une entreprise chez le premier, tandis que le second
    n'a qu'un endpoint global et se déclare `requires_token = False`. Une
    entrée d'agrégateur sans token est donc légitime, pas malformée.

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

        if ats not in KNOWN_ATS:
            logger.warning("source « %s » ignorée : ats inconnu « %s »", label, ats)
            continue

        # Le token est vérifié après l'`ats`, car c'est l'adaptateur qui dit
        # s'il en faut un : un ATS désigne le board d'une entreprise par son
        # token, un agrégateur n'a qu'un endpoint global (US-2.3.0).
        token = entry.get("token")
        if token is None:
            token = ""
        if not isinstance(token, str):
            logger.warning(
                "source « %s » ignorée : « token » invalide, attendu une chaîne", label
            )
            continue
        if not token and ADAPTERS[ats].requires_token:
            logger.warning("source « %s » ignorée : « token » manquant", label)
            continue

        sources.append(Source(nom=nom, ats=ats, token=token))

    return sources


# ---------------------------------------------------------------------------
# Cadences de collecte
# ---------------------------------------------------------------------------

#: Période du cron de `collect.yml`, en minutes.
COLLECT_INTERVAL_MINUTES = 15

#: Appels par jour et par source dans la boucle de collecte normale.
FAST_RUNS_PER_DAY = 24 * 60 // COLLECT_INTERVAL_MINUTES

#: Appels par jour de `collect-slow.yml` (cron toutes les 6 heures).
SLOW_RUNS_PER_DAY = 4

#: Les deux cadences, telles qu'écrites en argument de `python -m src.collect`.
FAST = "fast"
SLOW = "slow"
CADENCES = (FAST, SLOW)


def cadence_of(source: Source) -> str:
    """Cadence à laquelle `source` peut être interrogée.

    `SLOW` dès que la plateforme plafonne ses appels sous ce que la boucle
    de 15 min consommerait. C'est le cas de Remotive, qui n'en autorise que
    quatre par jour : l'y laisser reviendrait à en faire 96 et à se faire
    couper l'accès.
    """
    limite = ADAPTERS[source.ats].max_calls_per_day
    if limite is not None and limite < FAST_RUNS_PER_DAY:
        return SLOW
    return FAST


def split_by_cadence(sources: list[Source]) -> tuple[list[Source], list[Source]]:
    """Répartit `sources` en `(rapides, lentes)`, ordre du fichier préservé."""
    rapides = [source for source in sources if cadence_of(source) == FAST]
    lentes = [source for source in sources if cadence_of(source) == SLOW]
    return rapides, lentes


def load_all(config_dir: str | Path = "config", *, cadence: str | None = None) -> list[Source]:
    """Charge les deux registres — entreprises puis agrégateurs.

    `cadence` restreint le résultat à `FAST` ou à `SLOW` ; `None` renvoie
    tout. Les fichiers absents sont ignorés : un dépôt sans
    `aggregators.yaml` collecte simplement ses ATS.
    """
    directory = Path(config_dir)
    sources: list[Source] = []
    for name in ("sources.yaml", "aggregators.yaml"):
        path = directory / name
        if not path.is_file():
            logger.warning("registre absent, ignoré : %s", path)
            continue
        sources.extend(load_registry(path))

    if cadence is None:
        return sources
    if cadence not in CADENCES:
        raise ValueError(f"cadence inconnue « {cadence} » — attendu : {', '.join(CADENCES)}")
    return [source for source in sources if cadence_of(source) == cadence]
