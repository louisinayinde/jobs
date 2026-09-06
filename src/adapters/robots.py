"""Lecture et application de `robots.txt` (US-2.5.3).

Les adaptateurs des Features 2.2 et 2.3 appellent une API ou un flux : la
plateforme les a publiés pour être consommés, la question du `robots.txt`
ne se pose pas. L'adaptateur générique de la Feature 2.5, lui, **charge des
pages HTML** — c'est un crawl, et un crawl se déclare.

Ce module n'est donc pas là pour la forme : c'est ce qui évite de se faire
bannir, et c'est la limite que le projet s'impose entre « ne pas être
filtré à tort » (les en-têtes de navigateur d'`AggregatorAdapter`) et
« passer outre un refus », qu'on ne fait pas.

Trois règles en sortent :

1. `Disallow: /` pour notre agent → la source est **désactivée**, aucune
   requête n'est émise vers ce domaine (Europe Remotely est ce cas) ;
2. un chemin interdit n'est jamais chargé, même s'il figure au sitemap ;
3. `Crawl-delay`, quand il est publié, impose l'attente entre deux
   requêtes — sinon c'est le défaut du projet qui s'applique.

`urllib.robotparser` de la bibliothèque standard n'est pas utilisé : il
récupère lui-même l'URL (donc hors de notre client, de nos en-têtes, de
notre timeout et de notre retry) et il ignore `Crawl-delay` pour l'agent
`*`. Le format est simple, on le lit nous-mêmes.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from urllib.parse import unquote, urlsplit

#: Attente minimale entre deux requêtes vers un même domaine, en secondes,
#: quand `robots.txt` ne publie pas de `Crawl-delay`.
#:
#: Une seconde n'est pas une valeur magique : c'est l'usage courant, et
#: avec le plafond de pages par run de l'adaptateur, elle borne le crawl à
#: une durée connue d'avance.
DEFAULT_CRAWL_DELAY = 1.0

#: Le nom sous lequel on se déclare dans `robots.txt`. Il correspond au
#: `User-Agent` de `base.USER_AGENT` : un site qui voudrait nous exclure
#: nommément doit pouvoir le faire.
ROBOTS_AGENT = "jobradar"

_DIRECTIVE_RE = re.compile(r"^\s*([A-Za-z-]+)\s*:\s*(.*?)\s*$")


@dataclass(frozen=True)
class RobotsRules:
    """Les règles qui nous concernent, extraites d'un `robots.txt`.

    - `allow` / `disallow` : chemins du groupe retenu, dans l'ordre du
      fichier. Les motifs `*` (n'importe quelle suite) et `$` (fin d'URL)
      sont pris en charge.
    - `crawl_delay` : secondes entre deux requêtes, ou `None` si le
      fichier n'en publie pas pour notre groupe.
    - `sitemaps` : les `Sitemap:` déclarés. Ils sont **globaux** au
      fichier, pas propres à un groupe — c'est ainsi que Japan Dev publie
      l'adresse de son sitemap.
    """

    allow: tuple[str, ...] = ()
    disallow: tuple[str, ...] = ()
    crawl_delay: float | None = None
    sitemaps: tuple[str, ...] = ()

    @property
    def blocks_everything(self) -> bool:
        """Le site nous interdit-il entièrement ?

        Vrai quand `Disallow: /` s'applique sans qu'aucun `Allow` ne le
        rouvre. C'est un refus explicite : la source est désactivée, pas
        contournée.
        """
        return not self.allows("/")

    @property
    def delay(self) -> float:
        """Attente à respecter entre deux requêtes, défaut du projet compris."""
        return DEFAULT_CRAWL_DELAY if self.crawl_delay is None else self.crawl_delay

    def allows(self, url: str) -> bool:
        """`url` (absolue ou chemin) est-elle autorisée au crawl ?

        Applique la règle de spécificité du standard : c'est le motif le
        **plus long** qui gagne, et à longueur égale l'autorisation
        l'emporte sur l'interdiction. Sans motif applicable, c'est
        autorisé — l'absence de règle vaut permission.
        """
        path = _path_of(url)
        meilleur_allow = max((len(p) for p in self.allow if _matches(p, path)), default=-1)
        meilleur_disallow = max(
            (len(p) for p in self.disallow if _matches(p, path)), default=-1
        )
        return meilleur_allow >= meilleur_disallow


#: Ce qu'on retient quand un site ne publie pas de `robots.txt` : tout est
#: autorisé, avec le délai par défaut. C'est le comportement standard.
NO_ROBOTS = RobotsRules()


def _path_of(url: str) -> str:
    """`https://host/a/b?x=1` → `/a/b?x=1` ; un chemin nu est rendu tel quel."""
    parts = urlsplit(url)
    chemin = parts.path or "/"
    return f"{chemin}?{parts.query}" if parts.query else chemin


def _matches(pattern: str, path: str) -> bool:
    """Le motif `robots.txt` `pattern` couvre-t-il `path` ?

    Un motif est un **préfixe** de chemin, où `*` remplace n'importe quelle
    suite de caractères et un `$` final ancre la fin de l'URL.
    """
    if not pattern:
        # `Disallow:` sans valeur ne veut rien dire — c'est même la façon
        # standard d'écrire « rien n'est interdit ». Ne couvre aucun chemin.
        return False

    ancre = pattern.endswith("$")
    motif = pattern[:-1] if ancre else pattern
    morceaux = [re.escape(unquote(part)) for part in motif.split("*")]
    regex = ".*".join(morceaux) + ("$" if ancre else "")
    return re.match(regex, unquote(path)) is not None


def parse_robots(text: str, agent: str = ROBOTS_AGENT) -> RobotsRules:
    """Extrait de `text` les règles qui s'appliquent à `agent`.

    Un `robots.txt` est une suite de groupes, chacun ouvert par une ou
    plusieurs lignes `User-agent:`. **Un seul groupe s'applique** : celui
    dont le nom d'agent correspond le plus précisément au nôtre, et à
    défaut le groupe `*`.

    Ce point n'est pas théorique. Europe Remotely publie treize groupes ;
    le premier (`Googlebot`) n'interdit que `/info/` et `/search/`, mais le
    dernier — `User-agent: *`, le seul qui nous concerne — porte
    `Disallow: /`. Lire le fichier ligne à ligne sans tenir compte des
    groupes conclurait exactement l'inverse de ce que le site demande.
    """
    groupes: dict[str, dict[str, list]] = {}
    #: Agents du groupe en cours d'écriture. Une nouvelle ligne
    #: `User-agent:` après une directive ouvre un nouveau groupe.
    courants: list[str] = []
    nouveau_groupe = True
    sitemaps: list[str] = []

    for ligne in text.splitlines():
        ligne = ligne.split("#", 1)[0]
        directive = _DIRECTIVE_RE.match(ligne)
        if not directive:
            continue

        cle, valeur = directive.group(1).lower(), directive.group(2)

        if cle == "sitemap":
            # Directive de fichier, hors groupe : elle vaut pour tout le monde.
            if valeur:
                sitemaps.append(valeur)
            continue

        if cle == "user-agent":
            if not nouveau_groupe:
                courants = []
                nouveau_groupe = True
            nom = valeur.lower()
            courants.append(nom)
            groupes.setdefault(nom, {"allow": [], "disallow": [], "crawl-delay": []})
            continue

        if not courants or cle not in {"allow", "disallow", "crawl-delay"}:
            continue

        nouveau_groupe = False
        for nom in courants:
            groupes[nom][cle].append(valeur)

    retenu = _select_group(groupes, agent.lower())
    if retenu is None:
        return RobotsRules(sitemaps=tuple(sitemaps))

    return RobotsRules(
        allow=tuple(p for p in retenu["allow"] if p),
        disallow=tuple(p for p in retenu["disallow"] if p),
        crawl_delay=_first_delay(retenu["crawl-delay"]),
        sitemaps=tuple(sitemaps),
    )


def _select_group(groupes: dict[str, dict[str, list]], agent: str) -> dict[str, list] | None:
    """Le groupe qui nous concerne : le nom d'agent le plus précis, sinon `*`."""
    candidats = [nom for nom in groupes if nom != "*" and nom and agent.startswith(nom)]
    if candidats:
        return groupes[max(candidats, key=len)]
    return groupes.get("*")


def _first_delay(valeurs: list[str]) -> float | None:
    """Premier `Crawl-delay` lisible du groupe, ou `None`."""
    for valeur in valeurs:
        try:
            delai = float(valeur)
        except ValueError:
            continue
        if delai >= 0:
            return delai
    return None
