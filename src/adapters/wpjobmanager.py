"""Adaptateurs des flux WP Job Manager : Jobspresso et EU Remote Jobs
(US-2.3.6).

**Décision tranchée** (l'US laissait le choix entre un adaptateur paramétré
et trois adaptateurs) : **deux classes, pas trois, ni une seule.**

L'inspection des flux le 2026-09-05 montre deux familles distinctes, et non
trois flux « de structure proche » :

- Jobspresso et EU Remote Jobs tournent tous deux sous **WP Job Manager**
  et exposent le même schéma — des éléments d'espace de noms `<company>`,
  `<location>`, `<job_type>` en plus du RSS standard. Une seule classe les
  couvre, paramétrée par l'URL du flux ; chaque site reste un `ats` distinct
  pour que `sources.yaml` n'ait pas à porter d'URL.
- NoDesk n'est **pas** un flux WP Job Manager : c'est un flux maison, sans
  élément d'entreprise, qu'il faut extraire du titre. Il a sa propre classe
  (`nodesk.py`).

Correction d'audit au passage : `docs/sources-audit.md` donnait
`euremotejobs.com/feed/` comme flux d'offres. C'est le flux du **blog** —
il ne renvoie que des articles de conseil. Le flux d'offres est
`euremotejobs.com/?feed=job_feed`, même convention que Jobspresso.
"""

from __future__ import annotations

from typing import ClassVar

from src.adapters.base import (
    AggregatorAdapter,
    RawJob,
    RssItem,
    clean_str,
    html_to_text,
    infer_remote_type,
    parse_rss,
)
from src.core.config import Source


class WpJobManagerAdapter(AggregatorAdapter):
    """Flux RSS produit par l'extension WordPress **WP Job Manager**.

    Sous-classer en déclarant `ats`, `site_url` et `feed_url` suffit à
    brancher un nouveau site tournant sur la même extension.
    """

    #: URL du flux d'offres — `?feed=job_feed` par convention WP Job Manager.
    feed_url: ClassVar[str]

    def _entries(self, source: Source) -> list[RssItem]:
        body = self._request_text(source, self.feed_url)
        if body is None:
            return []
        return parse_rss(source, self.ats, body)

    def _parse(self, source: Source, entry: RssItem) -> RawJob | None:
        titre = entry.title.strip()
        link = entry.link or entry.guid
        if not titre or not link:
            return None

        localisation = clean_str(entry.extras.get("location"))

        return RawJob(
            # `<post-id>` est l'identifiant WordPress ; les flux qui ne le
            # publient pas (EU Remote Jobs) laissent le slug de l'URL faire
            # l'affaire — il est tout aussi stable.
            id=clean_str(entry.extras.get("post-id")) or link.rstrip("/").rsplit("/", 1)[-1],
            ats=self.ats,
            # WP Job Manager publie l'entreprise dans un élément dédié :
            # pas d'heuristique sur le titre, contrairement à WWR et NoDesk.
            entreprise=clean_str(entry.extras.get("company")),
            titre=titre,
            localisation=localisation,
            # Aucun champ « mode de travail » : `<job_type>` et
            # `<job_category>` ne servent pas la même chose d'un site à
            # l'autre (chez Jobspresso ils sont même inversés), on ne se fie
            # donc qu'au libellé de localisation.
            remote_type=infer_remote_type(localisation),
            url=link,
            date=entry.date,
            description=html_to_text(entry.body),
        )


class JobspressoAdapter(WpJobManagerAdapter):
    ats = "jobspresso"
    site_url = "https://jobspresso.co/"
    feed_url = "https://jobspresso.co/?feed=job_feed"


class EuRemoteJobsAdapter(WpJobManagerAdapter):
    ats = "euremotejobs"
    site_url = "https://euremotejobs.com/"
    feed_url = "https://euremotejobs.com/?feed=job_feed"
