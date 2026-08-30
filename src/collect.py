"""Point d'entrée du Collector, déclenché toutes les 15 min par `collect.yml`.

Squelette (Feature 1.3) : la logique de collecte réelle arrive avec
l'Epic 2. Pour l'instant, valide seulement que les secrets requis sont
présents avant de s'arrêter.
"""

from __future__ import annotations

from src.core.secrets import require_env


def main() -> int:
    require_env("GITHUB_TOKEN")
    print("collect: squelette — logique de collecte non encore implémentée (Epic 2)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
