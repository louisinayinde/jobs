"""Point d'entrée du CV Generator, déclenché par `cv.yml` sur label `cv`.

Squelette (Feature 1.3) : la logique d'adaptation LLM et de rendu PDF
réelle arrive avec l'Epic 6. Pour l'instant, valide seulement que les
secrets requis sont présents avant de s'arrêter.
"""

from __future__ import annotations

from src.core.secrets import require_env


def main() -> int:
    require_env("LLM_API_KEY")
    require_env("GITHUB_TOKEN")
    print("cv.generate: squelette — logique de génération non encore implémentée (Epic 6)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
