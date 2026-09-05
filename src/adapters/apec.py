"""Adaptateur APEC (US-2.3.9).

`POST https://www.apec.fr/cms/webservices/rechercheOffre`
Priorité géo ④ (France, cadres).

**Pas d'API documentée** : c'est l'endpoint que le front de l'APEC appelle
lui-même. Deux conséquences pratiques :

- le corps de la requête est **strict** — un champ inconnu renvoie un 500
  dont le message nomme le champ fautif et liste les 34 propriétés
  acceptées. C'est désagréable en production mais très efficace en mise au
  point : le serveur dit exactement quoi corriger. `SEARCH_BODY` ci-dessous
  n'utilise que des champs vérifiés le 2026-09-05 ;
- la pagination passe par `pagination.startIndex` (et `range` pour la
  taille de page), pas par un paramètre d'URL.

`Source.token` porte les mots-clés de recherche (défaut : `developpeur`).
"""

from __future__ import annotations

import logging
from typing import Any

from src.adapters.base import (
    AggregatorAdapter,
    RawJob,
    clean_str,
    html_to_text,
    infer_remote_type,
    to_iso_utc,
)
from src.core.config import Source

logger = logging.getLogger(__name__)

BASE_URL = "https://www.apec.fr/cms/webservices/rechercheOffre"
PUBLIC_URL = "https://www.apec.fr/candidat/recherche-emploi.html/emploi/detail-offre/{numero}"
DEFAULT_KEYWORDS = "developpeur"

#: Offres par page, et nombre maximum de pages parcourues par run.
PAGE_SIZE = 20
MAX_PAGES = 5


class ApecAdapter(AggregatorAdapter):
    ats = "apec"
    site_url = "https://www.apec.fr/candidat/recherche-emploi.html"

    def _entries(self, source: Source) -> list[object]:
        entries: list[object] = []

        for page in range(MAX_PAGES):
            payload = self._request_json(
                source,
                BASE_URL,
                method="POST",
                json_body=_search_body(
                    source.token or DEFAULT_KEYWORDS, start_index=page * PAGE_SIZE
                ),
            )
            if payload is None:
                break

            body = self._expect_mapping(source, payload, "réponse")
            results = self._expect_list(source, body.get("resultats") or [], "resultats")
            entries.extend(results)

            if len(results) < PAGE_SIZE:
                break
            total = body.get("totalCount")
            if isinstance(total, int) and len(entries) >= total:
                break

        return entries

    def _parse(self, source: Source, entry: object) -> RawJob | None:
        if not isinstance(entry, dict):
            logger.warning(
                "agrégateur « %s » (apec) : offre ignorée, mapping attendu", source.nom
            )
            return None

        numero = clean_str(entry.get("numeroOffre"))
        job_id = numero or str(entry.get("id") or "").strip()
        titre = clean_str(entry.get("intitule"))
        if not job_id or not titre:
            logger.warning(
                "agrégateur « %s » (apec) : offre ignorée, « numeroOffre » ou "
                "« intitule » manquant",
                source.nom,
            )
            return None

        localisation = clean_str(entry.get("lieuTexte"))
        # `texteOffre` est un extrait tronqué (~250 caractères, terminé par
        # « ... ») : le texte intégral est sur la page de l'offre, que le
        # JD Fetcher ira chercher si l'offre est retenue (Feature 6.2).
        description = html_to_text(clean_str(entry.get("texteOffre")))

        return RawJob(
            id=job_id,
            ats=self.ats,
            # `nomCommercial` est vide sur les offres confidentielles : elles
            # sont alors écartées comme toute offre sans employeur nommé.
            entreprise=clean_str(entry.get("nomCommercial")),
            titre=titre,
            localisation=localisation,
            # L'APEC ne renvoie pas le mode de travail dans les résultats de
            # recherche, seulement dans les facettes : on s'en tient au
            # libellé de lieu.
            remote_type=infer_remote_type(localisation),
            url=PUBLIC_URL.format(numero=numero) if numero else "",
            date=to_iso_utc(entry.get("datePublication"))
            or to_iso_utc(entry.get("dateValidation")),
            description=description,
        )


def _search_body(keywords: str, *, start_index: int) -> dict[str, Any]:
    """Corps du POST de recherche.

    Chaque clé a été validée contre le serveur : en ajouter une inconnue
    (`idsSecteursActivite`, par exemple) fait répondre 500. Les listes vides
    signifient « aucun filtre sur ce critère ».
    """
    return {
        "lieux": [],
        "fonctions": [],
        "statutPoste": [],
        "typesContrat": [],
        "typesConvention": [],
        "niveauxExperience": [],
        "secteursActivite": [],
        "typesTeletravail": [],
        "sorts": [{"type": "SCORE", "direction": "DESCENDING"}],
        "pagination": {"range": PAGE_SIZE, "startIndex": start_index},
        "activeFiltre": True,
        "motsCles": keywords,
    }
