"""Adaptateur Workable (US-2.2.4).

API job-board publique, sans clé — en **POST**, contrairement aux autres :
`https://apply.workable.com/api/v3/accounts/{token}/jobs`

Le `token` est le slug lu dans l'URL du board (`apply.workable.com/{token}/`).

Même limite que SmartRecruiters : la liste ne contient pas la description,
donc `RawJob.description` vaut `""` et le texte intégral est récupéré à la
demande par le JD Fetcher (Feature 6.2). L'URL publique n'est pas non plus
renvoyée : elle se reconstruit depuis le `shortcode`.
"""

from __future__ import annotations

import logging

from src.adapters.base import (
    REMOTE,
    HttpAdapter,
    RawJob,
    clean_str,
    infer_remote_type,
    join_locations,
    normalize_remote_type,
    to_iso_utc,
)
from src.core.config import Source

logger = logging.getLogger(__name__)

BASE_URL = "https://apply.workable.com/api/v3/accounts/{token}/jobs"
PUBLIC_URL = "https://apply.workable.com/{token}/j/{shortcode}/"


class WorkableAdapter(HttpAdapter):
    ats = "workable"

    def fetch(self, source: Source) -> list[RawJob]:
        payload = self._request_json(
            source,
            BASE_URL.format(token=source.token),
            method="POST",
            # Corps vide = aucun filtre : on veut tout le board, le tri se
            # fait chez nous (filtre de rétention, Feature 3.2).
            json_body={},
        )
        if payload is None:
            return []

        body = self._expect_mapping(source, payload, "réponse")
        entries = self._expect_list(source, body.get("results", []), "results")

        jobs: list[RawJob] = []
        for entry in entries:
            job = self._parse(source, entry)
            if job is not None:
                jobs.append(job)
        return jobs

    def _parse(self, source: Source, entry: object) -> RawJob | None:
        if not isinstance(entry, dict):
            logger.warning(
                "source « %s » (workable) : offre ignorée, mapping attendu", source.nom
            )
            return None

        job_id = str(entry.get("id") or "").strip()
        titre = clean_str(entry.get("title"))
        if not job_id or not titre:
            logger.warning(
                "source « %s » (workable) : offre ignorée, « id » ou « title » manquant",
                source.nom,
            )
            return None

        localisation = _localisation(entry)
        shortcode = clean_str(entry.get("shortcode"))
        url = (
            PUBLIC_URL.format(token=source.token, shortcode=shortcode)
            if shortcode
            else f"https://apply.workable.com/{source.token}/"
        )

        fallback = REMOTE if entry.get("remote") is True else infer_remote_type(localisation)

        return RawJob(
            id=job_id,
            ats=self.ats,
            entreprise=source.nom,
            titre=titre,
            localisation=localisation,
            remote_type=normalize_remote_type(entry.get("workplace"), fallback=fallback),
            url=url,
            date=to_iso_utc(entry.get("published")),
            description="",
        )


def _localisation(entry: dict) -> str:
    """Assemble « Ville, Région, Pays » pour chaque localisation visible."""
    locations = entry.get("locations")
    if not isinstance(locations, list) or not locations:
        single = entry.get("location")
        locations = [single] if isinstance(single, dict) else []

    labels = []
    for location in locations:
        if not isinstance(location, dict) or location.get("hidden") is True:
            continue
        parts = [
            clean_str(location.get("city")),
            clean_str(location.get("region")),
            clean_str(location.get("country")),
        ]
        label = ", ".join(part for part in parts if part)
        if label:
            labels.append(label)
    return join_locations(labels)
