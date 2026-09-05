"""Adaptateur Himalayas (US-2.3.5).

API publique, sans clé : `https://himalayas.app/jobs/api?limit=N`

`Source.token` porte la taille de page (défaut : 100). Le flux expose
`nextCursor`, mais on ne pagine pas : le Collector tourne toutes les 15 min
et ne s'intéresse qu'aux offres récentes, que la première page contient
déjà.
"""

from __future__ import annotations

import logging

from src.adapters.base import (
    REMOTE,
    AggregatorAdapter,
    RawJob,
    clean_str,
    html_to_text,
    join_locations,
    to_iso_utc,
)
from src.core.config import Source

logger = logging.getLogger(__name__)

BASE_URL = "https://himalayas.app/jobs/api?limit={limit}"
DEFAULT_LIMIT = "100"


class HimalayasAdapter(AggregatorAdapter):
    ats = "himalayas"
    site_url = "https://himalayas.app/"

    def _entries(self, source: Source) -> list[object]:
        payload = self._request_json(
            source, BASE_URL.format(limit=source.token or DEFAULT_LIMIT)
        )
        if payload is None:
            return []

        body = self._expect_mapping(source, payload, "réponse")
        return self._expect_list(source, body.get("jobs", []), "jobs")

    def _parse(self, source: Source, entry: object) -> RawJob | None:
        if not isinstance(entry, dict):
            logger.warning(
                "agrégateur « %s » (himalayas) : offre ignorée, mapping attendu", source.nom
            )
            return None

        titre = clean_str(entry.get("title"))
        # Le flux n'a pas de champ `id` : le `guid` est l'URL canonique de
        # l'offre, dont le dernier segment est un identifiant stable.
        guid = clean_str(entry.get("guid"))
        job_id = guid.rstrip("/").rsplit("/", 1)[-1] if guid else ""
        if not job_id or not titre:
            logger.warning(
                "agrégateur « %s » (himalayas) : offre ignorée, « guid » ou « title » manquant",
                source.nom,
            )
            return None

        return RawJob(
            id=job_id,
            ats=self.ats,
            entreprise=clean_str(entry.get("companyName")),
            titre=titre,
            # `locationRestrictions` liste les pays depuis lesquels on peut
            # postuler ; vide = ouvert au monde entier.
            localisation=join_locations(entry.get("locationRestrictions")),
            # Himalayas ne publie que du télétravail.
            remote_type=REMOTE,
            url=guid or clean_str(entry.get("applicationLink")),
            # `pubDate` est un epoch en secondes, pas une date ISO.
            date=to_iso_utc(entry.get("pubDate")),
            description=html_to_text(clean_str(entry.get("description")))
            or clean_str(entry.get("excerpt")),
        )
