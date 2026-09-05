"""Adaptateur Landing.jobs (US-2.3.7).

API publique, sans clé : `https://landing.jobs/api/v1/jobs`
Priorité géo ③ (Europe du Sud / Ibérie).

Particularité : **l'API ne renvoie pas le nom de l'entreprise**. Le seul
endroit où il figure est l'URL de l'offre,
`landing.jobs/at/{slug}/{poste}`. On reconstitue donc l'employeur depuis ce
slug — une offre dont l'URL ne suit pas cette forme n'a pas d'entreprise
identifiable et est écartée (règle anti-scam, US-2.3.0).
"""

from __future__ import annotations

import logging
import re

from src.adapters.base import (
    REMOTE,
    AggregatorAdapter,
    RawJob,
    clean_str,
    html_to_text,
    infer_remote_type,
    join_locations,
    to_iso_utc,
)
from src.core.config import Source

logger = logging.getLogger(__name__)

BASE_URL = "https://landing.jobs/api/v1/jobs"

_COMPANY_IN_URL = re.compile(r"/at/([^/]+)/")


class LandingJobsAdapter(AggregatorAdapter):
    ats = "landingjobs"
    site_url = "https://landing.jobs/"

    def _entries(self, source: Source) -> list[object]:
        payload = self._request_json(source, BASE_URL)
        if payload is None:
            return []
        return self._expect_list(source, payload, "réponse")

    def _parse(self, source: Source, entry: object) -> RawJob | None:
        if not isinstance(entry, dict):
            logger.warning(
                "agrégateur « %s » (landingjobs) : offre ignorée, mapping attendu", source.nom
            )
            return None

        job_id = str(entry.get("id") or "").strip()
        titre = clean_str(entry.get("title"))
        if not job_id or not titre:
            logger.warning(
                "agrégateur « %s » (landingjobs) : offre ignorée, « id » ou « title » manquant",
                source.nom,
            )
            return None

        url = clean_str(entry.get("url"))
        localisation = _localisation(entry)

        return RawJob(
            id=job_id,
            ats=self.ats,
            entreprise=company_from_url(url),
            titre=titre,
            localisation=localisation,
            remote_type=REMOTE if entry.get("remote") is True else infer_remote_type(localisation),
            url=url,
            date=to_iso_utc(entry.get("published_at")),
            description=_description(entry),
        )


def company_from_url(url: str) -> str:
    """`.../at/damia-group-portugal/backend-...` → `Damia Group Portugal`.

    Le slug est la seule trace de l'employeur dans l'API : on le rend
    lisible en remplaçant les tirets par des espaces et en capitalisant.
    Le nom exact reste celui affiché sur la page de l'offre — cette
    reconstitution sert à identifier l'employeur, pas à le citer au mot près.
    """
    match = _COMPANY_IN_URL.search(url)
    if not match:
        return ""
    return " ".join(word.capitalize() for word in match.group(1).split("-") if word)


def _localisation(entry: dict) -> str:
    """Assemble « Ville, PT » pour chaque localisation de l'offre."""
    locations = entry.get("locations")
    if not isinstance(locations, list):
        return ""

    labels = []
    for location in locations:
        if not isinstance(location, dict):
            continue
        parts = [clean_str(location.get("city")), clean_str(location.get("country_code"))]
        label = ", ".join(part for part in parts if part)
        if label:
            labels.append(label)
    return join_locations(labels)


def _description(entry: dict) -> str:
    """Recolle les trois blocs HTML de la fiche Landing.jobs.

    L'API éclate l'annonce en `role_description`, `main_requirements` et
    `nice_to_have` : les technologies vivent dans les deux derniers, que le
    filtre tech (US-3.2.4) et le CV Adapter attendent.
    """
    blocks = [
        html_to_text(clean_str(entry.get(key)))
        for key in ("role_description", "main_requirements", "nice_to_have")
    ]
    return "\n\n".join(block for block in blocks if block)
