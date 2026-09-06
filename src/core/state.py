"""Mémoire du crawl d'un run à l'autre (US-2.5.0).

Le runner GitHub Actions est **éphémère** : il naît, collecte, et
disparaît. Rien de ce qu'il calcule ne survit — c'est déjà pour cette
raison que les plafonds d'appels sont tenus par le planificateur et non
par un compteur (US-2.4.2). Or l'adaptateur sitemap a besoin de savoir
**où il en était** : sans mémoire, « ne retenir que ce qui a changé depuis
le dernier run » n'a pas de sens, et il rechargerait tout le catalogue à
chaque passage.

D'où ce magasin : un fichier JSON versionné dans le dépôt, écrit par le
run et relu par le suivant. Le dépôt héberge « code, config **et état** »
depuis l'US-1.1.1 ; c'est la première pièce de cet état.

Deux propriétés font tout le comportement :

- **un fichier absent ou corrompu vaut un état vide**, jamais une erreur.
  Un premier run, un fichier tronqué par un commit malheureux : dans les
  deux cas on repart d'une page blanche et on recrawle, ce qui coûte du
  temps mais ne perd rien ;
- **l'écriture est atomique** (fichier temporaire puis `replace`) : un run
  interrompu en plein `save()` laisse l'ancien état intact plutôt qu'un
  JSON à moitié écrit que le run suivant jetterait.

Ce n'est pas `seen.json` (Feature 3.3), qui mémorisera les offres déjà
publiées sur le tableau. Deux fichiers, deux responsabilités : celui-ci
retient *où en est le crawl d'une source*, l'autre retiendra *quelles
offres ont déjà été vues*. Les mélanger ferait d'un fichier de 300 URLs
techniques la même chose qu'un journal de décisions.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

#: Emplacement par défaut, relatif à la racine du dépôt.
DEFAULT_STATE_PATH = Path("state/crawl.json")

#: Nombre maximum d'URLs mémorisées par source.
#:
#: Ne sert qu'aux sitemaps **sans `<lastmod>`**, où l'incrémental se repère
#: sur l'identité de l'URL : Japan Dev en publie 290, le plafond est donc
#: large. Il est là pour qu'un site qui se mettrait à publier 200 000 URLs
#: sans date ne fasse pas enfler indéfiniment un fichier versionné.
MAX_KNOWN_URLS = 5_000


class CrawlState:
    """Curseurs de crawl, indexés par source.

    Une entrée par source (sa clé est son `ats`) :

    - `cursor` : le `<lastmod>` le plus récent réellement traité, en
      ISO-8601 UTC. Le run suivant ne reprend que ce qui lui est
      postérieur ;
    - `urls`   : les URLs déjà chargées, pour les sitemaps sans
      `<lastmod>`. Vide sinon — un catalogue à `<lastmod>` n'a aucune
      raison de peser dans ce fichier.
    """

    def __init__(self, path: Path, data: dict[str, Any] | None = None) -> None:
        self.path = Path(path)
        self._data: dict[str, Any] = data or {}

    # -- lecture ------------------------------------------------------------

    @classmethod
    def load(cls, path: str | Path | None = None) -> CrawlState:
        """Charge l'état, ou en rend un vide si le fichier manque ou est cassé.

        `path` est résolu **à l'appel** et non figé dans la signature : la
        suite de tests redirige `DEFAULT_STATE_PATH` vers un dossier
        temporaire, pour qu'aucun test ne puisse écrire l'état réel du
        dépôt ni dépendre de celui qu'un run y aurait laissé.
        """
        chemin = Path(DEFAULT_STATE_PATH if path is None else path)
        if not chemin.is_file():
            return cls(chemin)

        try:
            brut = json.loads(chemin.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError, ValueError) as exc:
            logger.warning(
                "état de crawl illisible (%s) — on repart d'un état vide : %s",
                chemin,
                exc,
            )
            return cls(chemin)

        if not isinstance(brut, dict):
            logger.warning(
                "état de crawl invalide (%s) : mapping attendu, reçu %s — état vide",
                chemin,
                type(brut).__name__,
            )
            return cls(chemin)

        return cls(chemin, brut)

    def _entry(self, key: str) -> dict[str, Any]:
        entree = self._data.get(key)
        return entree if isinstance(entree, dict) else {}

    def cursor(self, key: str) -> str:
        """Dernier `<lastmod>` traité pour `key`, ou `""` au premier passage."""
        valeur = self._entry(key).get("cursor")
        return valeur if isinstance(valeur, str) else ""

    def known_urls(self, key: str) -> set[str]:
        """URLs déjà chargées pour `key` (sitemaps sans `<lastmod>`)."""
        valeur = self._entry(key).get("urls")
        if not isinstance(valeur, list):
            return set()
        return {url for url in valeur if isinstance(url, str)}

    # -- écriture -----------------------------------------------------------

    def record(self, key: str, *, cursor: str, urls: set[str]) -> None:
        """Enregistre l'avancement de `key`, en mémoire.

        Le curseur ne recule jamais : un sitemap qui republierait une date
        plus ancienne ne doit pas faire recharger tout un catalogue.
        `save()` reste à appeler pour que ça survive au run.
        """
        entree: dict[str, Any] = {"cursor": max(cursor, self.cursor(key))}
        if urls:
            # Tri : le fichier est versionné, un diff Git doit rester lisible
            # et ne montrer que les URLs réellement apparues ou disparues.
            entree["urls"] = sorted(urls)[:MAX_KNOWN_URLS]
        self._data[key] = entree

    def save(self) -> None:
        """Écrit l'état sur disque, de façon atomique."""
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporaire = self.path.with_suffix(f"{self.path.suffix}.tmp")
        contenu = json.dumps(self._data, indent=2, ensure_ascii=False, sort_keys=True)
        temporaire.write_text(contenu + "\n", encoding="utf-8")
        temporaire.replace(self.path)

    def as_dict(self) -> dict[str, Any]:
        """Copie de l'état, pour l'inspection et les tests."""
        return json.loads(json.dumps(self._data))
