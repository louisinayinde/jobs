"""Adaptateurs ATS et agrégateurs.

Chaque adaptateur implémente le contrat `Adapter.fetch(source) -> list[RawJob]`
(voir `base.py`). `ADAPTERS` fait le lien entre le champ `ats` de
`sources.yaml` et la classe qui sait interroger cette plateforme : c'est le
seul point à toucher pour brancher un nouvel ATS.

Voir `README.md` dans ce dossier pour la marche à suivre.
"""

from __future__ import annotations

from typing import Any

from src.adapters.ashby import AshbyAdapter
from src.adapters.base import Adapter, AdapterError, HttpAdapter, RawJob
from src.adapters.greenhouse import GreenhouseAdapter
from src.adapters.lever import LeverAdapter
from src.adapters.smartrecruiters import SmartRecruitersAdapter
from src.adapters.workable import WorkableAdapter

#: `ats` de `sources.yaml` → classe d'adaptateur.
ADAPTERS: dict[str, type[Adapter]] = {
    adapter.ats: adapter
    for adapter in (
        GreenhouseAdapter,
        LeverAdapter,
        AshbyAdapter,
        SmartRecruitersAdapter,
        WorkableAdapter,
    )
}


class UnknownAtsError(KeyError):
    """Aucun adaptateur ne connaît cet identifiant d'ATS."""


def get_adapter(ats: str, **kwargs: Any) -> Adapter:
    """Instancie l'adaptateur correspondant à `ats`.

    Lève `UnknownAtsError` — dont le message liste les ATS pris en charge —
    si l'identifiant est inconnu. Les `kwargs` sont passés au constructeur
    de l'adaptateur (`client`, `timeout`).
    """
    try:
        adapter_cls = ADAPTERS[ats]
    except KeyError as exc:
        connus = ", ".join(sorted(ADAPTERS))
        raise UnknownAtsError(
            f"ats inconnu « {ats} » — adaptateurs disponibles : {connus}"
        ) from exc
    return adapter_cls(**kwargs)


__all__ = [
    "ADAPTERS",
    "Adapter",
    "AdapterError",
    "AshbyAdapter",
    "GreenhouseAdapter",
    "HttpAdapter",
    "LeverAdapter",
    "RawJob",
    "SmartRecruitersAdapter",
    "UnknownAtsError",
    "WorkableAdapter",
    "get_adapter",
]
