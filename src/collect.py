"""Point d'entrée du Collector.

Deux cadences, deux workflows, une seule commande :

- `--cadence fast` (défaut) — `collect.yml`, toutes les 15 min, soit 96
  appels par jour et par source ;
- `--cadence slow` — `collect-slow.yml`, toutes les 6 heures, soit 4.

La cadence lente existe pour les plateformes qui plafonnent leurs appels
sous ce que la boucle normale consommerait : aujourd'hui Remotive, qui n'en
autorise que quatre par jour. C'est le **planificateur** qui garantit le
compte, pas un compteur applicatif : le runner Actions est éphémère, il ne
mémoriserait rien d'un run à l'autre. Chaque source appartient donc à
exactement une cadence, et sa cadence se déduit de son adaptateur
(`Adapter.max_calls_per_day`), jamais d'une ligne de configuration qu'on
pourrait perdre de vue.

Squelette (Feature 1.3) : la sélection des sources est réelle et testée, la
récupération des offres arrive avec la Feature 2.4 (robustesse) puis
l'Epic 3.
"""

from __future__ import annotations

import argparse
import logging

from src.adapters.registry import CADENCES, FAST, load_all
from src.core.secrets import require_env

logger = logging.getLogger(__name__)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m src.collect",
        description="Collecte les offres des sources de la cadence demandée.",
    )
    parser.add_argument(
        "--cadence",
        choices=CADENCES,
        default=FAST,
        help=(
            "fast : sources sans plafond d'appels (toutes les 15 min) ; "
            "slow : sources qui limitent leurs appels quotidiens (toutes les 6 h)"
        ),
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    require_env("GITHUB_TOKEN")

    sources = load_all(cadence=args.cadence)
    if not sources:
        # Ce n'est pas une erreur : la cadence lente peut légitimement être
        # vide si aucune plateforme ne plafonne ses appels.
        print(f"collect [{args.cadence}] : aucune source pour cette cadence")
        return 0

    pluriel = "s" if len(sources) > 1 else ""
    print(f"collect [{args.cadence}] : {len(sources)} source{pluriel} à interroger")
    for source in sources:
        print(f"  - {source.nom} ({source.ats})")
    print("collect: squelette — logique de collecte non encore implémentée (Epic 2)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
