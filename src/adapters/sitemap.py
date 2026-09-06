"""Lecture de `sitemap.xml`, en incrémental (US-2.5.0).

Un sitemap est la table des matières qu'un site publie pour les moteurs.
Il en existe deux formes, et l'une renvoie à l'autre :

- un **index** (`<sitemapindex>`) qui liste d'autres sitemaps — Welcome to
  the Jungle en déclare 24, Japan Dev un seul ;
- un **jeu d'URLs** (`<urlset>`) qui liste les pages, chacune avec un
  `<lastmod>` optionnel.

Les deux peuvent être servis compressés : l'usage veut qu'un gros sitemap
soit publié en `.xml.gz`. La décompression est faite ici, sur les octets
et non sur l'extension de l'URL — un serveur peut très bien servir du gzip
sous une adresse en `.xml`.

**Tout l'intérêt est dans le `<lastmod>`.** Recharger les ~240 000 pages
d'un catalogue à chaque run serait impossible autant qu'impoli ; ne
retenir que ce qui a changé depuis le dernier passage ramène le volume à
quelques dizaines de pages. Ce module se contente de lire et de
sélectionner : c'est l'adaptateur qui décide quoi charger, et le magasin
d'état (`src/core/state.py`) qui se souvient du dernier passage.
"""

from __future__ import annotations

import gzip
import logging
from dataclasses import dataclass
from xml.etree import ElementTree

from src.adapters.base import AdapterError, to_iso_utc
from src.core.config import Source

logger = logging.getLogger(__name__)

#: Signature d'un flux gzip, en tête de fichier.
_GZIP_MAGIC = b"\x1f\x8b"

#: `{http://ns}loc` → `loc` : les sitemaps déclarent un espace de noms, on
#: indexe par nom local plutôt que d'en dépendre.
_LOCAL = lambda tag: tag.rsplit("}", 1)[-1]  # noqa: E731


@dataclass(frozen=True)
class SitemapUrl:
    """Une entrée `<url>` de sitemap.

    - `loc`     : l'adresse de la page
    - `lastmod` : date de dernière modification, ISO-8601 UTC, ou `""`
      quand le sitemap n'en publie pas — c'est le cas de Japan Dev, et
      c'est ce qui déclenche le repli documenté de l'adaptateur.
    """

    loc: str
    lastmod: str = ""


@dataclass(frozen=True)
class Sitemap:
    """Le contenu d'un document sitemap.

    Un document est soit un index, soit un jeu d'URLs — jamais les deux —
    donc l'une des deux listes est toujours vide.
    """

    urls: tuple[SitemapUrl, ...] = ()
    #: Adresses des sitemaps enfants, quand le document est un index.
    children: tuple[str, ...] = ()


def decompress(data: bytes) -> bytes:
    """Décompresse `data` si c'est du gzip, sinon la rend telle quelle.

    Le test porte sur les octets, pas sur l'extension de l'URL : `httpx` ne
    décompresse que ce qui est annoncé par un en-tête `Content-Encoding`, or
    un `.xml.gz` est servi comme un fichier gzip *à part entière*
    (`Content-Type: application/gzip`) et arrive donc compressé.
    """
    if not data.startswith(_GZIP_MAGIC):
        return data
    try:
        return gzip.decompress(data)
    except (OSError, EOFError, gzip.BadGzipFile):
        # En-tête gzip mais contenu illisible : on rend les octets bruts,
        # le parseur XML lèvera l'erreur nommant la source.
        return data


def parse_sitemap(source: Source, ats: str, data: bytes) -> Sitemap:
    """Parse un document sitemap, index ou jeu d'URLs, gzip compris.

    Lève `AdapterError` — nommant la source — si le document n'est pas du
    XML exploitable, pour qu'un sitemap corrompu soit traité comme
    n'importe quelle autre réponse inexploitable (le Collector marque la
    source en échec et continue avec les autres).
    """
    try:
        root = ElementTree.fromstring(decompress(data))
    except ElementTree.ParseError as exc:
        raise AdapterError(
            f"source « {source.nom} » ({ats}) : sitemap illisible — {exc}"
        ) from exc

    if _LOCAL(root.tag) == "sitemapindex":
        return Sitemap(children=tuple(_child_locs(root)))

    urls: list[SitemapUrl] = []
    for element in root:
        if _LOCAL(element.tag) != "url":
            continue
        champs = {_LOCAL(enfant.tag): (enfant.text or "").strip() for enfant in element}
        loc = champs.get("loc", "")
        if not loc:
            continue
        urls.append(SitemapUrl(loc=loc, lastmod=to_iso_utc(champs.get("lastmod", ""))))

    return Sitemap(urls=tuple(urls))


def _child_locs(root: ElementTree.Element) -> list[str]:
    locs: list[str] = []
    for element in root:
        if _LOCAL(element.tag) != "sitemap":
            continue
        for enfant in element:
            if _LOCAL(enfant.tag) == "loc" and (enfant.text or "").strip():
                locs.append(enfant.text.strip())
                break
    return locs


def select_new(
    urls: tuple[SitemapUrl, ...] | list[SitemapUrl],
    *,
    cursor: str,
    known: set[str],
) -> list[SitemapUrl]:
    """Ne retient que ce qui n'a pas déjà été vu, et le trie du plus ancien.

    Deux régimes, selon ce que le sitemap publie :

    - **avec `<lastmod>`** — l'entrée est retenue si sa date est
      strictement postérieure au `cursor`, c'est-à-dire au dernier
      passage. C'est le cas nominal, et le seul qui passe à l'échelle d'un
      catalogue de 240 000 pages.
    - **sans `<lastmod>`** — repli documenté : l'entrée est retenue si son
      URL n'a **jamais** été chargée (`known`). On reste incrémental, en
      se repérant sur l'identité de l'URL au lieu d'une date. C'est le cas
      de Japan Dev, dont le sitemap n'en publie aucun ; le coût, borné par
      la taille du sitemap, est de mémoriser ces URLs d'un run à l'autre.

    Le tri place les plus anciennes en tête : quand l'adaptateur plafonne
    le nombre de pages d'un run, ce sont les URLs les plus anciennes qui
    passent, et le curseur n'avance que jusqu'à celles réellement
    chargées. Rien n'est enjambé.
    """
    def est_nouvelle(url: SitemapUrl) -> bool:
        if url.lastmod:
            return url.lastmod > cursor
        return url.loc not in known

    retenues = [url for url in urls if est_nouvelle(url)]
    # Les entrées sans date gardent l'ordre du sitemap : `""` trie avant
    # toute date, elles passent donc en premier — ce qui est le bon ordre,
    # puisqu'elles n'ont, elles, aucune chance d'être reprises par le
    # curseur si on les manque.
    return sorted(retenues, key=lambda url: url.lastmod)
