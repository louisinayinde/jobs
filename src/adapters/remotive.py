"""Adaptateur Remotive (US-2.3.2).

API publique, sans clé :
`https://remotive.com/api/remote-jobs?search=engineer`

Le paramètre `search` est porté par `Source.token` : une entrée sans token
récupère tout le flux. C'est le seul agrégateur dont le token sert à
filtrer côté serveur plutôt qu'à désigner un board.

⚠️ **Conditions d'utilisation.** Remotive demande de ne pas interroger
l'API plus de **4 fois par jour** — ses offres sont de toute façon
retardées de 24 h — et de citer Remotive en source avec un lien vers
l'offre. Le second point est satisfait : `RawJob.url` est l'URL Remotive,
que le pipeline republie telle quelle.

Le premier est incompatible avec la boucle de collecte de 15 min (96 appels
par jour). D'où `max_calls_per_day = 4`, qui range cette source dans la
**cadence lente** : elle est collectée par `collect-slow.yml`, planifié
quatre fois par jour, et exclue de `collect.yml`. Le compte est garanti par
le planificateur GitHub Actions et non par un compteur applicatif — le
runner étant éphémère, il ne pourrait rien mémoriser d'un run à l'autre.
"""

from __future__ import annotations

import logging

from src.adapters.base import (
    REMOTE,
    AggregatorAdapter,
    RawJob,
    clean_str,
    html_to_text,
    to_iso_utc,
)
from src.core.config import Source

logger = logging.getLogger(__name__)

BASE_URL = "https://remotive.com/api/remote-jobs"
DEFAULT_SEARCH = "engineer"


class RemotiveAdapter(AggregatorAdapter):
    ats = "remotive"
    site_url = "https://remotive.com/"
    #: Plafond imposé par les conditions d'utilisation de Remotive.
    max_calls_per_day = 4

    def _entries(self, source: Source) -> list[object]:
        search = source.token or DEFAULT_SEARCH
        payload = self._request_json(source, f"{BASE_URL}?search={search}")
        if payload is None:
            return []

        body = self._expect_mapping(source, payload, "réponse")
        return self._expect_list(source, body.get("jobs", []), "jobs")

    def _parse(self, source: Source, entry: object) -> RawJob | None:
        if not isinstance(entry, dict):
            logger.warning(
                "agrégateur « %s » (remotive) : offre ignorée, mapping attendu", source.nom
            )
            return None

        job_id = str(entry.get("id") or "").strip()
        titre = clean_str(entry.get("title"))
        if not job_id or not titre:
            logger.warning(
                "agrégateur « %s » (remotive) : offre ignorée, « id » ou « title » manquant",
                source.nom,
            )
            return None

        return RawJob(
            id=job_id,
            ats=self.ats,
            entreprise=clean_str(entry.get("company_name")),
            titre=titre,
            # `candidate_required_location` dit d'où l'on peut postuler
            # (« Worldwide », « USA only ») : c'est la seule géographie du flux.
            localisation=clean_str(entry.get("candidate_required_location")),
            # Remotive ne publie que du télétravail.
            remote_type=REMOTE,
            url=clean_str(entry.get("url")),
            date=to_iso_utc(entry.get("publication_date")),
            description=html_to_text(clean_str(entry.get("description"))),
        )
