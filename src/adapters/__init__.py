"""Adaptateurs ATS et agrégateurs.

Chaque adaptateur implémente le contrat `Adapter.fetch(source) -> list[RawJob]`
(voir `base.py`). `ADAPTERS` fait le lien entre le champ `ats` d'une entrée de
configuration et la classe qui sait interroger cette plateforme : c'est le seul
point à toucher pour brancher une nouvelle source.

Deux familles, un seul contrat :

- les **ATS** (`config/sources.yaml`) exposent le board d'**une** entreprise,
  désigné par un `token` ; `RawJob.entreprise` vaut `Source.nom` ;
- les **agrégateurs** (`config/aggregators.yaml`) publient pour **N**
  entreprises depuis un endpoint global, sans token ; `RawJob.entreprise` est
  lu dans l'offre, et une offre sans employeur identifiable est écartée.

Voir `README.md` dans ce dossier pour la marche à suivre.
"""

from __future__ import annotations

from typing import Any

from src.adapters.apec import ApecAdapter
from src.adapters.ashby import AshbyAdapter
from src.adapters.base import (
    Adapter,
    AdapterError,
    AggregatorAdapter,
    HttpAdapter,
    RawJob,
    RssItem,
)
from src.adapters.freework import FreeWorkAdapter
from src.adapters.greenhouse import GreenhouseAdapter
from src.adapters.hackernews import HackerNewsAdapter
from src.adapters.himalayas import HimalayasAdapter
from src.adapters.landingjobs import LandingJobsAdapter
from src.adapters.lever import LeverAdapter
from src.adapters.nodesk import NoDeskAdapter
from src.adapters.remoteok import RemoteOkAdapter
from src.adapters.remotive import RemotiveAdapter
from src.adapters.smartrecruiters import SmartRecruitersAdapter
from src.adapters.weworkremotely import WeWorkRemotelyAdapter
from src.adapters.workable import WorkableAdapter
from src.adapters.workingnomads import WorkingNomadsAdapter
from src.adapters.wpjobmanager import (
    EuRemoteJobsAdapter,
    JobspressoAdapter,
    WpJobManagerAdapter,
)

#: Adaptateurs ATS : un board = une entreprise, désignée par son `token`.
ATS_ADAPTERS: tuple[type[Adapter], ...] = (
    GreenhouseAdapter,
    LeverAdapter,
    AshbyAdapter,
    SmartRecruitersAdapter,
    WorkableAdapter,
)

#: Adaptateurs d'agrégateurs : un endpoint global, N entreprises, pas de token.
AGGREGATOR_ADAPTERS: tuple[type[Adapter], ...] = (
    RemoteOkAdapter,
    RemotiveAdapter,
    WeWorkRemotelyAdapter,
    HackerNewsAdapter,
    HimalayasAdapter,
    WorkingNomadsAdapter,
    NoDeskAdapter,
    JobspressoAdapter,
    EuRemoteJobsAdapter,
    LandingJobsAdapter,
    FreeWorkAdapter,
    ApecAdapter,
)

#: `ats` d'une entrée de configuration → classe d'adaptateur.
ADAPTERS: dict[str, type[Adapter]] = {
    adapter.ats: adapter for adapter in ATS_ADAPTERS + AGGREGATOR_ADAPTERS
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
    "AGGREGATOR_ADAPTERS",
    "ATS_ADAPTERS",
    "Adapter",
    "AdapterError",
    "AggregatorAdapter",
    "ApecAdapter",
    "AshbyAdapter",
    "EuRemoteJobsAdapter",
    "FreeWorkAdapter",
    "GreenhouseAdapter",
    "HackerNewsAdapter",
    "HimalayasAdapter",
    "HttpAdapter",
    "JobspressoAdapter",
    "LandingJobsAdapter",
    "LeverAdapter",
    "NoDeskAdapter",
    "RawJob",
    "RemoteOkAdapter",
    "RemotiveAdapter",
    "RssItem",
    "SmartRecruitersAdapter",
    "UnknownAtsError",
    "WeWorkRemotelyAdapter",
    "WorkableAdapter",
    "WorkingNomadsAdapter",
    "WpJobManagerAdapter",
    "get_adapter",
]
