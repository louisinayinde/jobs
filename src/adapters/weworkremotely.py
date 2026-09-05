"""Adaptateur We Work Remotely (US-2.3.3).

Flux RSS public : `https://weworkremotely.com/categories/{token}.rss`

`Source.token` porte la catégorie (défaut :
`remote-programming-jobs`). Premier consommateur du parseur RSS partagé de
`base.py`.

WWR ne publie **pas** l'entreprise dans un élément dédié : elle est le
préfixe du titre, avant le premier `« : »` — `Edfinity: Senior Software
Engineer, remote`. Un item dont le titre ne suit pas cette forme n'a donc
pas d'entreprise identifiable et est écarté par `AggregatorAdapter`
(règle anti-scam, US-2.3.0).
"""

from __future__ import annotations

from src.adapters.base import (
    REMOTE,
    AggregatorAdapter,
    RawJob,
    RssItem,
    clean_str,
    html_to_text,
    parse_rss,
)
from src.core.config import Source

BASE_URL = "https://weworkremotely.com/categories/{category}.rss"
DEFAULT_CATEGORY = "remote-programming-jobs"


class WeWorkRemotelyAdapter(AggregatorAdapter):
    ats = "weworkremotely"
    site_url = "https://weworkremotely.com/"

    def _entries(self, source: Source) -> list[RssItem]:
        url = BASE_URL.format(category=source.token or DEFAULT_CATEGORY)
        body = self._request_text(source, url)
        if body is None:
            return []
        return parse_rss(source, self.ats, body)

    def _parse(self, source: Source, entry: RssItem) -> RawJob | None:
        entreprise, _, titre = entry.title.partition(": ")
        if not titre:
            # Pas de séparateur : le titre entier est le poste, sans
            # entreprise. On le remonte quand même pour que le rejet soit
            # journalisé avec un libellé lisible.
            entreprise, titre = "", entry.title.strip()

        link = entry.link or entry.guid
        if not titre or not link:
            return None

        return RawJob(
            id=link.rstrip("/").rsplit("/", 1)[-1],
            ats=self.ats,
            entreprise=entreprise.strip(),
            titre=titre.strip(),
            # `<region>` : « Anywhere in the World », « USA Only », ...
            localisation=clean_str(entry.extras.get("region")),
            # WWR ne publie que du télétravail ; `<region>` n'exprime qu'une
            # restriction géographique de candidature.
            remote_type=REMOTE,
            url=link,
            date=entry.date,
            description=html_to_text(entry.body),
        )
