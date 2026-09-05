"""Adaptateur NoDesk (US-2.3.6).

Flux RSS public : `https://nodesk.co/remote-jobs/index.xml`

Flux maison, sans élément dédié à l'entreprise : comme chez WWR elle vit
dans le titre, mais **en suffixe** — `Staff Systems Engineer, IT at
GitLab`. On coupe donc sur le dernier `« at »`. Un titre sans ce
séparateur n'a pas d'entreprise identifiable et l'offre est écartée.

Le flux publie `&rsquo;`, entité valide en HTML mais indéfinie en XML :
c'est `resolve_html_entities` (base.py) qui la neutralise avant le parsing.
"""

from __future__ import annotations

from src.adapters.base import (
    REMOTE,
    AggregatorAdapter,
    RawJob,
    RssItem,
    html_to_text,
    parse_rss,
)
from src.core.config import Source

BASE_URL = "https://nodesk.co/remote-jobs/index.xml"

SEPARATOR = " at "


class NoDeskAdapter(AggregatorAdapter):
    ats = "nodesk"
    site_url = "https://nodesk.co/"

    def _entries(self, source: Source) -> list[RssItem]:
        body = self._request_text(source, BASE_URL)
        if body is None:
            return []
        return parse_rss(source, self.ats, body)

    def _parse(self, source: Source, entry: RssItem) -> RawJob | None:
        titre, separator, entreprise = entry.title.rpartition(SEPARATOR)
        if not separator:
            # `rpartition` met tout dans le troisième membre quand il ne
            # trouve pas le séparateur : on rétablit l'ordre attendu.
            titre, entreprise = entreprise, ""

        link = entry.link or entry.guid
        if not titre.strip() or not link:
            return None

        return RawJob(
            id=link.rstrip("/").rsplit("/", 1)[-1],
            ats=self.ats,
            entreprise=entreprise.strip(),
            titre=titre.strip(),
            # Le flux ne porte aucune localisation : elle n'est visible que
            # sur la page de l'offre, que le JD Fetcher ira chercher si
            # l'offre est retenue (Feature 6.2).
            localisation="",
            # NoDesk ne référence que des postes en télétravail.
            remote_type=REMOTE,
            url=link,
            date=entry.date,
            description=html_to_text(entry.body),
        )
