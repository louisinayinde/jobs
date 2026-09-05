"""Adaptateur Greenhouse (US-2.2.1).

API board publique, sans clé :
`https://boards-api.greenhouse.io/v1/boards/{token}/jobs?content=true`

Le `token` est le « board token » lu dans l'URL de la page carrière
(`boards.greenhouse.io/{token}`). `content=true` embarque la description
complète dans la réponse de liste : une seule requête par entreprise.
"""

from __future__ import annotations

import logging

from src.adapters.base import (
    HttpAdapter,
    RawJob,
    clean_str,
    html_to_text,
    infer_remote_type,
    to_iso_utc,
)
from src.core.config import Source

logger = logging.getLogger(__name__)

BASE_URL = "https://boards-api.greenhouse.io/v1/boards/{token}/jobs?content=true"


class GreenhouseAdapter(HttpAdapter):
    ats = "greenhouse"

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
                "source « %s » (greenhouse) : offre ignorée, mapping attendu", source.nom
            )
            return None

        job_id = str(entry.get("id") or "").strip()
        titre = clean_str(entry.get("title"))
        if not job_id or not titre:
            logger.warning(
                "source « %s » (greenhouse) : offre ignorée, « id » ou « title » manquant",
                source.nom,
            )
            return None

        # `location` peut être absent ou `null` sur les offres multi-bureaux.
        location = entry.get("location")
        localisation = clean_str(location.get("name")) if isinstance(location, dict) else ""

        url = clean_str(entry.get("absolute_url")) or (
            f"https://boards.greenhouse.io/{source.token}/jobs/{job_id}"
        )
        # `first_published` est la vraie date de mise en ligne ; `updated_at`
        # ne sert que de repli quand l'offre n'a jamais été republiée.
        date = to_iso_utc(entry.get("first_published")) or to_iso_utc(entry.get("updated_at"))

        return RawJob(
            id=job_id,
            ats=self.ats,
            entreprise=source.nom,
            titre=titre,
            localisation=localisation,
            # Greenhouse n'a pas de champ « mode de travail » : la seule
            # information disponible est le libellé de localisation.
            remote_type=infer_remote_type(localisation),
            url=url,
            date=date,
            description=html_to_text(clean_str(entry.get("content"))),
        )
