"""Lecture des secrets requis depuis l'environnement.

Chaque secret sensible (`LLM_API_KEY`, `GITHUB_TOKEN`) est stocké côté
GitHub Actions et injecté en variable d'environnement au runtime — jamais
en dur dans le code. `require_env` échoue tôt et explicitement si une
variable requise est absente ou vide, plutôt que de laisser `None` se
propager silencieusement plus loin dans le pipeline.
"""

from __future__ import annotations

import os
from typing import Mapping


class SecretsError(Exception):
    """Variable d'environnement requise absente ou vide."""


def require_env(name: str, env: Mapping[str, str] | None = None) -> str:
    """Retourne la valeur de `name` dans `env` (ou `os.environ`), ou échoue.

    Lève `SecretsError` nommant la clé absente si la variable n'est pas
    définie ou est vide, pour un échec explicite au démarrage.
    """
    source = env if env is not None else os.environ
    value = source.get(name)
    if not value:
        raise SecretsError(f"variable d'environnement requise absente : « {name} »")
    return value
