"""Point d'entrée du Board Janitor, déclenché quotidiennement par `purge.yml`.

Squelette (Feature 1.3) : la logique de purge réelle arrive avec
l'Epic 7. Pour l'instant, valide seulement que les secrets requis sont
présents avant de s'arrêter.
"""

from __future__ import annotations

from src.core.secrets import require_env


def main() -> int:
    require_env("GITHUB_TOKEN")
    print("purge: squelette — logique de purge non encore implémentée (Epic 7)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
