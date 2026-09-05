"""Adaptateur Free-Work (US-2.3.8).

API publique, sans clé :
`https://www.free-work.com/api/job_postings?contracts={token}&page=N`
Priorité géo ④ (France, IT).

`Source.token` porte le type de contrat (défaut : `permanent` ; l'API
accepte aussi `contractor`, `fixed-term`, `apprenticeship`, `internship`).

**Pagination gérée** : le format Hydra annonce le total dans
`hydra:totalItems` et sert 30 offres par page. On s'arrête à `MAX_PAGES`
pages — le Collector passe toutes les 15 min et les offres sont triées par
date, donc les premières pages suffisent à voir les nouveautés ; parcourir
les 147 pages du catalogue à chaque run serait impoli et inutile.
"""

from __future__ import annotations

import logging

from src.adapters.base import (
    HYBRID,
    ONSITE,
    REMOTE,
    UNKNOWN_REMOTE_TYPE,
    AggregatorAdapter,
    RawJob,
    clean_str,
    html_to_text,
    to_iso_utc,
)
from src.core.config import Source

logger = logging.getLogger(__name__)

BASE_URL = "https://www.free-work.com/api/job_postings?contracts={contracts}&page={page}"
PUBLIC_URL = "https://www.free-work.com/fr/tech-it/{metier}/job-mission/{slug}"
DEFAULT_CONTRACTS = "permanent"

#: Nombre maximum de pages parcourues par run (30 offres par page).
MAX_PAGES = 5

#: `remoteMode` de Free-Work → notre vocabulaire.
_REMOTE_MODES = {"full": REMOTE, "partial": HYBRID, "none": ONSITE}


class FreeWorkAdapter(AggregatorAdapter):
    ats = "freework"
    site_url = "https://www.free-work.com/"

    def _entries(self, source: Source) -> list[object]:
        contracts = source.token or DEFAULT_CONTRACTS
        entries: list[object] = []

        for page in range(1, MAX_PAGES + 1):
            payload = self._request_json(
                source, BASE_URL.format(contracts=contracts, page=page)
            )
            if payload is None:
                break

            body = self._expect_mapping(source, payload, "réponse")
            members = self._expect_list(source, body.get("hydra:member", []), "hydra:member")
            entries.extend(members)

            # Page incomplète : c'est la dernière, inutile d'en demander une
            # de plus juste pour la voir revenir vide.
            if not members:
                break
            total = body.get("hydra:totalItems")
            if isinstance(total, int) and len(entries) >= total:
                break

        return entries

    def _parse(self, source: Source, entry: object) -> RawJob | None:
        if not isinstance(entry, dict):
            logger.warning(
                "agrégateur « %s » (freework) : offre ignorée, mapping attendu", source.nom
            )
            return None

        job_id = str(entry.get("id") or "").strip()
        titre = clean_str(entry.get("title"))
        if not job_id or not titre:
            logger.warning(
                "agrégateur « %s » (freework) : offre ignorée, « id » ou « title » manquant",
                source.nom,
            )
            return None

        company = entry.get("company")
        company = company if isinstance(company, dict) else {}

        location = entry.get("location")
        location = location if isinstance(location, dict) else {}

        return RawJob(
            id=job_id,
            ats=self.ats,
            entreprise=clean_str(company.get("name")),
            titre=titre,
            localisation=clean_str(location.get("label")),
            remote_type=_REMOTE_MODES.get(
                clean_str(entry.get("remoteMode")).lower(), UNKNOWN_REMOTE_TYPE
            ),
            url=_public_url(entry),
            date=to_iso_utc(entry.get("publishedAt")),
            description=html_to_text(clean_str(entry.get("description"))),
        )


def _public_url(entry: dict) -> str:
    """Reconstruit l'URL publique, absente de la réponse de l'API.

    Elle se compose du slug du métier et de celui de l'offre :
    `/fr/tech-it/{metier}/job-mission/{slug}`.
    """
    slug = clean_str(entry.get("slug"))
    if not slug:
        return ""
    job = entry.get("job")
    metier = clean_str(job.get("slug")) if isinstance(job, dict) else ""
    if not metier:
        return f"https://www.free-work.com/fr/tech-it/jobs/{slug}"
    return PUBLIC_URL.format(metier=metier, slug=slug)
