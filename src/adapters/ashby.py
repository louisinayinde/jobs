"""Adaptateur Ashby (US-2.2.3).

API job-board publique, sans clé :
`https://api.ashbyhq.com/posting-api/job-board/{token}`

Le `token` est le nom du board lu dans l'URL (`jobs.ashbyhq.com/{token}`).
Ashby fournit déjà la description en texte brut (`descriptionPlain`) et un
champ `workplaceType` explicite.
"""

from __future__ import annotations

import logging

from src.adapters.base import (
    REMOTE,
    HttpAdapter,
    RawJob,
    clean_str,
    html_to_text,
    infer_remote_type,
    join_locations,
    normalize_remote_type,
    to_iso_utc,
)
from src.core.config import Source

logger = logging.getLogger(__name__)

BASE_URL = "https://api.ashbyhq.com/posting-api/job-board/{token}"


class AshbyAdapter(HttpAdapter):
    ats = "ashby"

    def fetch(self, source: Source) -> list[RawJob]:
        payload = self._request_json(source, BASE_URL.format(token=source.token))
        if payload is None:
            return []

        body = self._expect_mapping(source, payload, "réponse")
        entries = self._expect_list(source, body.get("jobs", []), "jobs")

        jobs: list[RawJob] = []
        for entry in entries:
            job = self._parse(source, entry)
            if job is not None:
                jobs.append(job)
        return jobs

    def _parse(self, source: Source, entry: object) -> RawJob | None:
        if not isinstance(entry, dict):
            logger.warning(
                "source « %s » (ashby) : offre ignorée, mapping attendu", source.nom
            )
            return None

        job_id = clean_str(entry.get("id"))
        # Ashby laisse passer des espaces en tête de titre (« Security Engineer »).
        titre = clean_str(entry.get("title"))
        if not job_id or not titre:
            logger.warning(
                "source « %s » (ashby) : offre ignorée, « id » ou « title » manquant",
                source.nom,
            )
            return None

        localisation = _localisation(entry)

        # `isRemote` est vrai dès qu'une localisation secondaire est remote,
        # alors que `workplaceType` décrit le poste principal : on garde
        # `workplaceType` et on ne lit `isRemote` qu'à défaut.
        fallback = REMOTE if entry.get("isRemote") is True else infer_remote_type(localisation)

        description = clean_str(entry.get("descriptionPlain")) or html_to_text(
            clean_str(entry.get("descriptionHtml"))
        )

        return RawJob(
            id=job_id,
            ats=self.ats,
            entreprise=source.nom,
            titre=titre,
            localisation=localisation,
            remote_type=_remote_type(entry, fallback),
            url=clean_str(entry.get("jobUrl")) or clean_str(entry.get("applyUrl")),
            date=to_iso_utc(entry.get("publishedAt")),
            description=description,
        )


def _localisation(entry: dict) -> str:
    """Localisation principale + localisations secondaires, dédupliquées."""
    parts = [clean_str(entry.get("location"))]
    secondary = entry.get("secondaryLocations")
    if isinstance(secondary, list):
        for item in secondary:
            if isinstance(item, dict):
                parts.append(clean_str(item.get("location")))
    return join_locations(parts)


def _remote_type(entry: dict, fallback: str) -> str:
    return normalize_remote_type(entry.get("workplaceType"), fallback=fallback)
