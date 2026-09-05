"""Adaptateur Hacker News « Who is Hiring » (US-2.3.4).

API Algolia publique, sans clé, en deux appels :

1. `search_by_date?tags=story,author_whoishiring` → le fil du mois courant ;
2. `items/{id}` → ses commentaires de premier niveau, une offre chacun.

**Le format le plus hostile du périmètre : du texte libre.** Il n'y a pas
de schéma, seulement une convention respectée par la plupart des
annonceurs — une première ligne de champs séparés par des barres verticales :

    Entreprise | Poste | Lieu | Contrat | Salaire | URL

L'extraction est donc **heuristique**, et assumée comme telle : un
commentaire qui ne suit pas la convention n'a pas d'entreprise
identifiable, il est écarté et journalisé (US-2.3.0) sans interrompre le
traitement des autres. Mieux vaut perdre quelques annonces atypiques que
publier des fiches sans employeur.
"""

from __future__ import annotations

import logging
import re
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

STORIES_URL = (
    "https://hn.algolia.com/api/v1/search_by_date"
    "?tags=story,author_whoishiring&hitsPerPage={limit}"
)
ITEM_URL = "https://hn.algolia.com/api/v1/items/{story_id}"
COMMENT_URL = "https://news.ycombinator.com/item?id={comment_id}"

#: Le compte `whoishiring` publie deux fils par mois — « Who is hiring? »
#: (les employeurs) et « Who wants to be hired? » (les candidats). Seul le
#: premier nous intéresse.
STORY_TITLE_PREFIX = "ask hn: who is hiring?"
#: Nombre de fils examinés pour trouver le plus récent « Who is hiring? ».
STORIES_LIMIT = 6

FIELD_SEPARATOR = "|"

_URL_RE = re.compile(r"https?://\S+|\bwww\.\S+")
#: Champs de la ligne d'entête qui ne sont pas un lieu.
_EMPLOYMENT_HINTS = (
    "full-time", "full time", "fulltime", "part-time", "part time",
    "contract", "contractor", "intern", "internship", "freelance",
    "permanent", "c2c", "w2", "cdi", "cdd",
)
_SALARY_RE = re.compile(r"[$€£₹]|\d\s*k\b|\bk\b|\bsalar|\bequity\b|\bofferings?\b", re.IGNORECASE)
#: Un lieu tient en quelques mots : au-delà, c'est une phrase de contexte.
_MAX_LOCATION_LENGTH = 80
#: Mots qui nomment le mode de travail, à retirer pour voir s'il reste un lieu.
_WORK_MODE_RE = re.compile(
    r"\b(remote|onsite|on-site|on site|hybrid|hybride|anywhere|télétravail)\b",
    re.IGNORECASE,
)


class HackerNewsAdapter(AggregatorAdapter):
    ats = "hackernews"
    site_url = "https://news.ycombinator.com/"

    def _entries(self, source: Source) -> list[Any]:
        story_id = self._current_story_id(source)
        if story_id is None:
            return []

        payload = self._request_json(source, ITEM_URL.format(story_id=story_id))
        if payload is None:
            return []

        body = self._expect_mapping(source, payload, "réponse")
        # Seuls les commentaires de premier niveau sont des offres ; leurs
        # propres enfants sont des questions et des réponses.
        return self._expect_list(source, body.get("children") or [], "children")

    def _current_story_id(self, source: Source) -> int | None:
        """Identifiant du fil « Who is hiring? » le plus récent."""
        payload = self._request_json(source, STORIES_URL.format(limit=STORIES_LIMIT))
        if payload is None:
            return None

        body = self._expect_mapping(source, payload, "réponse")
        hits = self._expect_list(source, body.get("hits") or [], "hits")

        for hit in hits:
            if not isinstance(hit, dict):
                continue
            if clean_str(hit.get("title")).lower().startswith(STORY_TITLE_PREFIX):
                try:
                    return int(hit.get("objectID"))
                except (TypeError, ValueError):
                    continue

        logger.warning(
            "agrégateur « %s » (hackernews) : aucun fil « Who is hiring? » dans les "
            "%d dernières publications de whoishiring",
            source.nom,
            STORIES_LIMIT,
        )
        return None

    def _parse(self, source: Source, entry: Any) -> RawJob | None:
        if not isinstance(entry, dict):
            logger.warning(
                "agrégateur « %s » (hackernews) : commentaire ignoré, mapping attendu",
                source.nom,
            )
            return None

        comment_id = str(entry.get("id") or "").strip()
        text = html_to_text(clean_str(entry.get("text")))
        if not comment_id or not text:
            # Commentaire supprimé ou vide : ce n'est pas une offre cassée.
            return None

        headline, *_ = text.splitlines()
        entreprise, titre, localisation = parse_headline(headline)

        return RawJob(
            id=comment_id,
            ats=self.ats,
            entreprise=entreprise,
            titre=titre,
            localisation=localisation,
            # Le mode de travail est écrit en clair dans la ligne d'entête
            # (« REMOTE », « ONSITE », « HYBRID ») : on la relit entière.
            remote_type=infer_remote_type(headline),
            url=COMMENT_URL.format(comment_id=comment_id),
            date=to_iso_utc(entry.get("created_at")),
            description=text,
        )


def parse_headline(headline: str) -> tuple[str, str, str]:
    """`Entreprise | Poste | Lieu | ...` → `(entreprise, poste, lieu)`.

    Retourne une entreprise vide quand la ligne ne suit pas la convention —
    l'offre est alors écartée par `AggregatorAdapter`.
    """
    fields = [field.strip() for field in headline.split(FIELD_SEPARATOR)]
    fields = [field for field in fields if field]

    if len(fields) < 2:
        # Ni barre verticale, ni deux champs : ce n'est pas une annonce
        # (message de discussion, commentaire hors sujet). On remonte le
        # texte comme titre pour que le rejet soit lisible dans le journal.
        return "", headline.strip()[:_MAX_LOCATION_LENGTH], ""

    # Beaucoup d'annonceurs collent l'URL de leur site au nom de
    # l'entreprise : « Snout https://snout.com/ ».
    entreprise = _URL_RE.sub("", fields[0]).strip(" -–—,;")
    titre = fields[1]

    return entreprise, titre, _pick_location(fields[2:])


def _pick_location(fields: list[str]) -> str:
    """Choisit, parmi les champs restants, celui qui est un lieu.

    La convention met le lieu juste après le poste, mais tous les
    annonceurs ne la suivent pas : on prend donc le premier champ qui n'est
    ni un type de contrat, ni un salaire, ni une URL.

    Un champ réduit au seul mode de travail (« HYBRID », « REMOTE ») n'est
    retenu qu'en dernier recours : `Utrecht, The Netherlands | HYBRID` doit
    donner la ville, pas le mode — que `remote_type` porte déjà.
    """
    candidates = [
        field
        for field in fields
        if len(field) <= _MAX_LOCATION_LENGTH
        and not any(hint in field.lower() for hint in _EMPLOYMENT_HINTS)
        and not _SALARY_RE.search(field)
        and not _URL_RE.search(field)
    ]

    for field in candidates:
        if not _is_bare_work_mode(field):
            return field

    return candidates[0] if candidates else ""


def _is_bare_work_mode(field: str) -> bool:
    """Le champ se réduit-il au mode de travail, sans aucun lieu ?

    « REMOTE » oui ; « Remote (Europe) » non — il reste « Europe ».
    """
    if infer_remote_type(field) == "unknown":
        return False
    stripped = _WORK_MODE_RE.sub("", field).strip(" ()[]-–—/,;")
    return not stripped
