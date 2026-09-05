"""Adaptateur Working Nomads (US-2.3.5).

API publique, sans clé :
`https://www.workingnomads.com/api/exposed_jobs/`

Le flux est une liste plate, sans champ `id` : l'identifiant est le nombre
lu dans l'URL de redirection (`/job/go/1835309/`).
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
    to_iso_utc,
)
from src.core.config import Source

logger = logging.getLogger(__name__)

BASE_URL = "https://www.workingnomads.com/api/exposed_jobs/"

_ID_IN_URL = re.compile(r"/job/go/(\d+)")


class WorkingNomadsAdapter(AggregatorAdapter):
    ats = "workingnomads"
    site_url = "https://www.workingnomads.com/"

    def _entries(self, source: Source) -> list[object]:
        payload = self._request_json(source, BASE_URL)
        if payload is None:
            return []
        return self._expect_list(source, payload, "réponse")

    def _parse(self, source: Source, entry: object) -> RawJob | None:
        if not isinstance(entry, dict):
            logger.warning(
                "agrégateur « %s » (workingnomads) : offre ignorée, mapping attendu",
                source.nom,
            )
            return None

        titre = clean_str(entry.get("title"))
        url = clean_str(entry.get("url"))
        match = _ID_IN_URL.search(url)
        job_id = match.group(1) if match else ""
        if not job_id or not titre:
            logger.warning(
                "agrégateur « %s » (workingnomads) : offre ignorée, « url » ou « title » "
                "manquant ou inexploitable",
                source.nom,
            )
            return None

        localisation = clean_str(entry.get("location"))

        return RawJob(
            id=job_id,
            ats=self.ats,
            entreprise=clean_str(entry.get("company_name")),
            titre=titre,
            localisation=localisation,
            # Working Nomads ne publie que du télétravail ; la localisation
            # n'exprime qu'une restriction de pays ou de fuseau, et ne doit
            # jamais faire retomber l'offre sur « unknown ».
            remote_type=infer_remote_type(localisation, fallback=REMOTE),
            url=url,
            date=to_iso_utc(entry.get("pub_date")),
            description=html_to_text(clean_str(entry.get("description"))),
        )
