"""Catalogue des adaptateurs et outillage de rejeu, partagés par les tests.

Ce module n'est pas un module de test : il porte la liste des adaptateurs
branchés, la fixture réelle de chacun, et les helpers qui rejouent ces
réponses via `httpx.MockTransport`. Aucun test n'accède au réseau.

**Ajouter un adaptateur à `ALL_ADAPTERS` suffit** à lui appliquer tous les
tests transverses de `test_adapters.py` — schéma `RawJob` commun, 404, JSON
malformé, erreur serveur — sans écrire une ligne de test.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import httpx

from src.adapters import (
    Adapter,
    ApecAdapter,
    AshbyAdapter,
    EuRemoteJobsAdapter,
    FreeWorkAdapter,
    GreenhouseAdapter,
    HackerNewsAdapter,
    HimalayasAdapter,
    JobspressoAdapter,
    LandingJobsAdapter,
    LeverAdapter,
    NoDeskAdapter,
    RawJob,
    RemoteOkAdapter,
    RemotiveAdapter,
    SmartRecruitersAdapter,
    WeWorkRemotelyAdapter,
    WorkableAdapter,
    WorkingNomadsAdapter,
)
from src.core.config import Source

FIXTURES = Path(__file__).resolve().parent / "fixtures"

#: Le schéma pivot : tout adaptateur doit produire exactement ces clés.
RAWJOB_KEYS = {
    "id",
    "ats",
    "entreprise",
    "titre",
    "localisation",
    "remote_type",
    "url",
    "date",
    "description",
}


@dataclass(frozen=True)
class AdapterCase:
    """Un adaptateur, sa ou ses fixtures, et la source qui l'interroge.

    `fixtures` est une séquence car un adaptateur peut enchaîner plusieurs
    requêtes : Hacker News cherche d'abord le fil du mois, puis en lit les
    commentaires. Les corps sont servis dans l'ordre, le dernier étant
    répété si l'adaptateur pagine.
    """

    adapter_cls: type[Adapter]
    fixtures: tuple[str, ...]
    source: Source
    #: Un agrégateur lit l'entreprise dans l'offre, pas dans `Source.nom`.
    aggregator: bool = False

    @property
    def id(self) -> str:
        return self.adapter_cls.ats

    def bodies(self) -> list[str]:
        return [(FIXTURES / name).read_text(encoding="utf-8") for name in self.fixtures]


# --- ATS : un board = une entreprise, désignée par son token ---------------

ATS_CASES = [
    AdapterCase(
        GreenhouseAdapter,
        ("greenhouse_gitlab.json",),
        Source(nom="GitLab", ats="greenhouse", token="gitlab"),
    ),
    AdapterCase(
        LeverAdapter,
        ("lever_malt.json",),
        Source(nom="Malt", ats="lever", token="malt"),
    ),
    AdapterCase(
        AshbyAdapter,
        ("ashby_ramp.json",),
        Source(nom="Ramp", ats="ashby", token="ramp"),
    ),
    AdapterCase(
        SmartRecruitersAdapter,
        ("smartrecruiters_ubisoft.json",),
        Source(nom="Ubisoft", ats="smartrecruiters", token="Ubisoft2"),
    ),
    AdapterCase(
        WorkableAdapter,
        ("workable_bgprevent.json",),
        Source(nom="BG Prevent", ats="workable", token="bg-prevent"),
    ),
]

# --- Agrégateurs : un endpoint global, N entreprises, pas de token ---------

AGGREGATOR_CASES = [
    AdapterCase(
        RemoteOkAdapter,
        ("remoteok.json",),
        Source(nom="RemoteOK", ats="remoteok", token=""),
        aggregator=True,
    ),
    AdapterCase(
        RemotiveAdapter,
        ("remotive.json",),
        Source(nom="Remotive", ats="remotive", token="engineer"),
        aggregator=True,
    ),
    AdapterCase(
        WeWorkRemotelyAdapter,
        ("wwr_programming.xml",),
        Source(nom="We Work Remotely", ats="weworkremotely", token=""),
        aggregator=True,
    ),
    AdapterCase(
        HackerNewsAdapter,
        ("hn_stories.json", "hn_thread.json"),
        Source(nom="HN Who is Hiring", ats="hackernews", token=""),
        aggregator=True,
    ),
    AdapterCase(
        HimalayasAdapter,
        ("himalayas.json",),
        Source(nom="Himalayas", ats="himalayas", token="100"),
        aggregator=True,
    ),
    AdapterCase(
        WorkingNomadsAdapter,
        ("workingnomads.json",),
        Source(nom="Working Nomads", ats="workingnomads", token=""),
        aggregator=True,
    ),
    AdapterCase(
        NoDeskAdapter,
        ("nodesk.xml",),
        Source(nom="NoDesk", ats="nodesk", token=""),
        aggregator=True,
    ),
    AdapterCase(
        JobspressoAdapter,
        ("jobspresso.xml",),
        Source(nom="Jobspresso", ats="jobspresso", token=""),
        aggregator=True,
    ),
    AdapterCase(
        EuRemoteJobsAdapter,
        ("euremotejobs.xml",),
        Source(nom="EU Remote Jobs", ats="euremotejobs", token=""),
        aggregator=True,
    ),
    AdapterCase(
        LandingJobsAdapter,
        ("landingjobs.json",),
        Source(nom="Landing.jobs", ats="landingjobs", token=""),
        aggregator=True,
    ),
    AdapterCase(
        FreeWorkAdapter,
        ("freework.json",),
        Source(nom="Free-Work", ats="freework", token="permanent"),
        aggregator=True,
    ),
    AdapterCase(
        ApecAdapter,
        ("apec.json",),
        Source(nom="APEC", ats="apec", token="developpeur"),
        aggregator=True,
    ),
]

#: Tous les adaptateurs branchés. Les tests transverses s'y appliquent.
ALL_ADAPTERS = ATS_CASES + AGGREGATOR_CASES
ALL_ADAPTER_IDS = [case.id for case in ALL_ADAPTERS]
ATS_IDS = [case.id for case in ATS_CASES]
AGGREGATOR_IDS = [case.id for case in AGGREGATOR_CASES]


# ---------------------------------------------------------------------------
# Rejeu hors réseau
# ---------------------------------------------------------------------------


def responder(
    *bodies: str, status: int = 200, recorder: list[httpx.Request] | None = None
) -> httpx.Client:
    """Client `httpx` qui sert `bodies` dans l'ordre, sans toucher au réseau.

    Le dernier corps est répété indéfiniment : un adaptateur qui pagine voit
    donc la même page revenir, ce qui suffit à tester le parsing (les tests
    de pagination, eux, comptent les requêtes via `recorder`).
    """
    queue = list(bodies)

    def handler(request: httpx.Request) -> httpx.Response:
        if recorder is not None:
            recorder.append(request)
        body = queue.pop(0) if len(queue) > 1 else queue[0]
        return httpx.Response(status, content=body)

    return httpx.Client(transport=httpx.MockTransport(handler))


def fetch_case(
    case: AdapterCase, *, recorder: list[httpx.Request] | None = None
) -> list[RawJob]:
    """Rejoue les fixtures de `case` et retourne les `RawJob` produits."""
    client = responder(*case.bodies(), recorder=recorder)
    return case.adapter_cls(client=client).fetch(case.source)


def fetch_body(case: AdapterCase, body: str, *, status: int = 200) -> list[RawJob]:
    """Rejoue un corps de réponse arbitraire (404, JSON malformé, ...)."""
    client = responder(body, status=status)
    return case.adapter_cls(client=client).fetch(case.source)
