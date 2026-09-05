"""Adaptateur SmartRecruiters (US-2.2.4).

API postings publique, sans clé :
`https://api.smartrecruiters.com/v1/companies/{token}/postings`

Le `token` est l'identifiant d'entreprise lu dans l'URL du board
(`careers.smartrecruiters.com/{token}`) — il est **sensible à la casse**.

Limite assumée : la liste des postings ne contient **pas** la description
(il faudrait un appel par offre, soit des centaines de requêtes par run).
`RawJob.description` vaut donc `""` pour cette source ; le texte intégral
est récupéré à la demande par le JD Fetcher (Feature 6.2), c'est-à-dire
uniquement pour l'offre effectivement retenue pour un CV.
"""

from __future__ import annotations

import logging

from src.adapters.base import (
    HYBRID,
    ONSITE,
    REMOTE,
    UNKNOWN_REMOTE_TYPE,
    HttpAdapter,
    RawJob,
    clean_str,
    join_locations,
    to_iso_utc,
)
from src.core.config import Source

logger = logging.getLogger(__name__)

BASE_URL = "https://api.smartrecruiters.com/v1/companies/{token}/postings"
PUBLIC_URL = "https://jobs.smartrecruiters.com/{token}/{job_id}"

#: Taille de page acceptée par l'API.
PAGE_SIZE = 100
#: Garde-fou : au-delà, on arrête de paginer et on journalise. Un board de
#: plus de 1000 offres est soit une erreur, soit hors du périmètre veille.
MAX_PAGES = 10


class SmartRecruitersAdapter(HttpAdapter):
    ats = "smartrecruiters"

    def fetch(self, source: Source) -> list[RawJob]:
        jobs: list[RawJob] = []
        base = BASE_URL.format(token=source.token)

        for page in range(MAX_PAGES):
            offset = page * PAGE_SIZE
            payload = self._request_json(source, f"{base}?limit={PAGE_SIZE}&offset={offset}")
            if payload is None:
                return jobs

            body = self._expect_mapping(source, payload, "réponse")
            entries = self._expect_list(source, body.get("content", []), "content")

            for entry in entries:
                job = self._parse(source, entry)
                if job is not None:
                    jobs.append(job)

            if len(entries) < PAGE_SIZE:
                return jobs

        logger.warning(
            "source « %s » (smartrecruiters) : pagination arrêtée à %d pages (%d offres)",
            source.nom,
            MAX_PAGES,
            len(jobs),
        )
        return jobs

    def _parse(self, source: Source, entry: object) -> RawJob | None:
        if not isinstance(entry, dict):
            logger.warning(
                "source « %s » (smartrecruiters) : offre ignorée, mapping attendu", source.nom
            )
            return None

        job_id = str(entry.get("id") or "").strip()
        titre = clean_str(entry.get("name"))
        if not job_id or not titre:
            logger.warning(
                "source « %s » (smartrecruiters) : offre ignorée, « id » ou « name » manquant",
                source.nom,
            )
            return None

        location = entry.get("location")
        location = location if isinstance(location, dict) else {}

        return RawJob(
            id=job_id,
            ats=self.ats,
            entreprise=source.nom,
            titre=titre,
            localisation=_localisation(location),
            remote_type=_remote_type(location),
            url=PUBLIC_URL.format(token=source.token, job_id=job_id),
            date=to_iso_utc(entry.get("releasedDate")),
            description="",
        )


def _localisation(location: dict) -> str:
    full = clean_str(location.get("fullLocation"))
    if full:
        return full
    pays = clean_str(location.get("country")).upper()
    return join_locations([location.get("city"), location.get("region"), pays])


def _remote_type(location: dict) -> str:
    if location.get("remote") is True:
        return REMOTE
    if location.get("hybrid") is True:
        return HYBRID
    # Un mapping `location` présent avec `remote: false` est une affirmation
    # de l'ATS ; un `location` absent ne dit rien.
    if "remote" in location:
        return ONSITE
    return UNKNOWN_REMOTE_TYPE
