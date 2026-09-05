"""Adaptateur Lever (US-2.2.2).

API postings publique, sans clé :
`https://api.lever.co/v0/postings/{token}?mode=json`

Le `token` est le slug lu dans l'URL du board (`jobs.lever.co/{token}`).
La réponse est une liste de postings à plat, avec les descriptions déjà
disponibles en texte brut (`descriptionPlain`, `additionalPlain`).
"""

from __future__ import annotations

import logging
from typing import Any

from src.adapters.base import (
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

BASE_URL = "https://api.lever.co/v0/postings/{token}?mode=json"


class LeverAdapter(HttpAdapter):
    ats = "lever"

    def fetch(self, source: Source) -> list[RawJob]:
        payload = self._request_json(source, BASE_URL.format(token=source.token))
        if payload is None:
            return []

        entries = self._expect_list(source, payload, "réponse")

        jobs: list[RawJob] = []
        for entry in entries:
            job = self._parse(source, entry)
            if job is not None:
                jobs.append(job)
        return jobs

    def _parse(self, source: Source, entry: object) -> RawJob | None:
        if not isinstance(entry, dict):
            logger.warning(
                "source « %s » (lever) : offre ignorée, mapping attendu", source.nom
            )
            return None

        job_id = clean_str(entry.get("id"))
        titre = clean_str(entry.get("text"))
        if not job_id or not titre:
            logger.warning(
                "source « %s » (lever) : offre ignorée, « id » ou « text » manquant",
                source.nom,
            )
            return None

        categories = entry.get("categories")
        categories = categories if isinstance(categories, dict) else {}
        localisation = join_locations(categories.get("allLocations")) or clean_str(
            categories.get("location")
        )

        url = clean_str(entry.get("hostedUrl")) or clean_str(entry.get("applyUrl"))

        return RawJob(
            id=job_id,
            ats=self.ats,
            entreprise=source.nom,
            titre=titre,
            localisation=localisation,
            # Lever expose `workplaceType` (remote / hybrid / onsite) ; on ne
            # retombe sur le texte de localisation que s'il est absent.
            remote_type=normalize_remote_type(
                entry.get("workplaceType"), fallback=infer_remote_type(localisation)
            ),
            url=url,
            date=to_iso_utc(entry.get("createdAt")),
            description=_description(entry),
        )


def _description(entry: dict[str, Any]) -> str:
    """Reconstitue la description complète d'un posting Lever.

    Lever éclate la fiche en trois morceaux : l'introduction
    (`descriptionPlain`), les sections à puces (`lists`) et l'annexe
    (`additionalPlain`). Les concaténer donne le texte que le filtre tech
    et le CV Adapter attendent — les sections à puces contiennent les
    technologies, absentes de la seule introduction.
    """
    blocks: list[str] = []

    intro = clean_str(entry.get("descriptionPlain")) or html_to_text(
        clean_str(entry.get("description"))
    )
    if intro:
        blocks.append(intro)

    sections = entry.get("lists")
    if isinstance(sections, list):
        for section in sections:
            if not isinstance(section, dict):
                continue
            titre = clean_str(section.get("text"))
            corps = html_to_text(clean_str(section.get("content")))
            block = "\n".join(part for part in (titre, corps) if part)
            if block:
                blocks.append(block)

    extra = clean_str(entry.get("additionalPlain")) or html_to_text(
        clean_str(entry.get("additional"))
    )
    if extra:
        blocks.append(extra)

    return "\n\n".join(blocks).replace("\xa0", " ").strip()
