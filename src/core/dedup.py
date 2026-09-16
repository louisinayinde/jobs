"""Déduplication : ne laisser passer que les offres jamais vues (Feature 3.3).

Le Collector tourne toutes les quinze minutes et une offre reste en ligne
des semaines : sans mémoire, la même offre serait publiée sur le tableau
de revue près de cent fois par jour. Ce module est cette mémoire.

Trois pièces, une par US :

1. **`stable_id`** (US-3.3.1) — l'identité d'une offre, `source:entreprise:id`.
   Elle ne dépend que de ce que la source dit de l'offre, jamais de l'heure
   ni de l'ordre du run : la même offre donne la même chaîne d'un run à
   l'autre et d'une machine à l'autre ;
2. **`SeenStore.nouvelles`** (US-3.3.2) — le tri entre offres neuves, offres
   déjà vues lors d'un run précédent, et doublons du run courant ;
3. **`SeenStore.marquer` / `save`** (US-3.3.3) — la mémorisation, dans
   `state/seen.json`, fichier versionné que le workflow commite.

**La dédup vient après la rétention, et c'est délibéré.** Seules les offres
retenues entrent dans `seen.json` : quelques dizaines par jour au lieu de
trois mille cinq cents par run, donc un fichier qui reste lisible dans un
diff. Surtout, élargir `filters.yaml` fait apparaître des offres jusque-là
écartées **comme neuves** — ce qu'on attend d'un changement de critères.
Mémoriser les offres rejetées les aurait rendues invisibles pour toujours.

**Mêmes propriétés que l'état du crawl (`src/core/state.py`)** : un fichier
absent ou corrompu vaut un état vide, jamais un crash, et l'écriture est
atomique. Un `seen.json` perdu coûte une republication, pas un run en échec.

**Limite connue, nommée plutôt que traitée : la même offre sur deux sources.**
Un poste GitLab lu sur Greenhouse et relayé par RemoteOK porte deux
identifiants natifs sans rapport : il passe deux fois. Rapprocher par
entreprise et titre serait faux dans l'autre sens — GitLab publie plusieurs
« Backend Engineer » distincts, un par équipe, et la dédup en ferait
disparaître tous sauf un, sans le dire. Mieux vaut un doublon visible sur
le tableau qu'une offre perdue en silence.
"""

from __future__ import annotations

import json
import logging
import re
import unicodedata
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable, Iterator

from src.core.normalize import Job
from src.core.state import write_json_atomic

logger = logging.getLogger(__name__)

#: Emplacement par défaut, relatif à la racine du dépôt.
DEFAULT_SEEN_PATH = Path("state/seen.json")

#: Séparateur des trois composantes de l'identifiant stable.
SEPARATEUR = ":"

#: Composante `entreprise` d'une offre qui n'en a pas.
#:
#: Rare — un agrégateur sans employeur est déjà écarté à la collecte
#: (US-2.3.0) — mais l'identifiant garde alors ses trois composantes, et
#: reste découpable sans cas particulier.
ENTREPRISE_INCONNUE = "_"

_NON_ALPHANUM_RE = re.compile(r"[^a-z0-9]+")


# ---------------------------------------------------------------------------
# US-3.3.1 — l'identifiant stable
# ---------------------------------------------------------------------------


def _slug(texte: str) -> str:
    """`Société Générale` → `societe-generale`.

    Les accents tombent, la casse aussi, et toute suite de signes devient
    un seul tiret — donc jamais un `:`, ce qui garde l'identifiant
    découpable.
    """
    sans_accents = (
        unicodedata.normalize("NFKD", texte).encode("ascii", "ignore").decode("ascii")
    )
    return _NON_ALPHANUM_RE.sub("-", sans_accents.lower()).strip("-")


def stable_id(job: Job) -> str:
    """Identifiant de dédup d'une offre : `source:entreprise:id`.

    Le format de l'US-3.3.1 (`ats:entreprise:job_id`), où `ats` est devenu
    `source` à la normalisation (US-3.1.1).

    - `source` est déjà un nom de plateforme en minuscules (`greenhouse`) ;
    - `entreprise` est **réduite à un slug**. Pour un ATS, elle vient de
      `sources.yaml`, écrite à la main : corriger « Gitlab » en « GitLab »
      ne doit pas faire republier toutes les offres de GitLab. Pour un
      agrégateur, elle vient de l'offre elle-même, dont la casse varie
      d'une annonce à l'autre ;
    - `id` est l'identifiant natif, **laissé tel quel** : c'est la
      plateforme qui l'a choisi, et « abc » et « ABC » y sont peut-être
      deux offres.

    Ni `source` ni le slug ne contiennent de `:`. Les deux premiers
    séparateurs délimitent donc toujours les composantes, même quand l'`id`
    natif en contient : `identifiant.split(":", 2)` rend les trois
    ingrédients d'origine.
    """
    entreprise = _slug(job.entreprise) or ENTREPRISE_INCONNUE
    return SEPARATEUR.join((job.source, entreprise, job.id))


# ---------------------------------------------------------------------------
# US-3.3.2 — le tri
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class DedupReport:
    """Ce qu'a donné la dédup sur un run."""

    #: Offres jamais vues, dans l'ordre d'entrée — la seule sortie qui
    #: continue dans le pipeline.
    nouvelles: list[Job] = field(default_factory=list)
    #: Offres déjà mémorisées par un run précédent.
    deja_vues: list[Job] = field(default_factory=list)
    #: Répétitions d'une offre déjà rencontrée **dans ce même run** — la
    #: première occurrence, elle, est dans `nouvelles` ou `deja_vues`.
    doublons: list[Job] = field(default_factory=list)

    @property
    def total(self) -> int:
        return len(self.nouvelles) + len(self.deja_vues) + len(self.doublons)

    @property
    def resume(self) -> str:
        """Une ligne pour le journal du run.

        Les déjà-vues sont affichées même à zéro : c'est le chiffre qui dit
        que la mémoire fonctionne. Au deuxième run d'une journée calme, il
        doit valoir presque tout ; s'il reste à zéro run après run, c'est
        que `seen.json` n'est pas commité, et le tableau se remplit de
        doublons.
        """
        base = (
            f"{len(self.nouvelles)} nouvelle(s) sur {self.total} — "
            f"{len(self.deja_vues)} déjà vue(s)"
        )
        if self.doublons:
            base += f", {len(self.doublons)} doublon(s) dans le run"
        return base

    def __iter__(self) -> Iterator[Job]:
        """Itérer sur le bilan, c'est itérer sur les offres nouvelles."""
        return iter(self.nouvelles)


# ---------------------------------------------------------------------------
# US-3.3.2 / US-3.3.3 — la mémoire
# ---------------------------------------------------------------------------


class SeenStore:
    """Les offres déjà vues : identifiant stable → date de première vue.

    La date n'est pas nécessaire à la dédup — un `set` suffirait — mais
    elle coûte une colonne et rend le fichier lisible : on sait **quand**
    une offre est apparue, sans fouiller le `git log`. Elle ne change
    jamais une fois écrite.
    """

    def __init__(self, path: Path, vues: dict[str, str] | None = None) -> None:
        self.path = Path(path)
        self._vues: dict[str, str] = dict(vues or {})

    # -- lecture ------------------------------------------------------------

    @classmethod
    def load(cls, path: str | Path | None = None) -> SeenStore:
        """Charge `seen.json`, ou un état vide si le fichier manque ou est cassé.

        `path` est résolu **à l'appel**, comme pour l'état du crawl : la
        suite de tests redirige `DEFAULT_SEEN_PATH`, et aucun test ne peut
        écrire ni lire la mémoire réelle du dépôt.

        Un fichier corrompu vaut un état vide, et le prix est connu : les
        offres encore en ligne repassent comme neuves **une fois**, puis
        sont mémorisées à nouveau. Un run en échec, lui, ne publierait
        rien du tout, et recommencerait au quart d'heure suivant.
        """
        chemin = Path(DEFAULT_SEEN_PATH if path is None else path)
        if not chemin.is_file():
            return cls(chemin)

        try:
            brut = json.loads(chemin.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError, ValueError) as exc:
            logger.warning(
                "offres vues illisibles (%s) — on repart d'un état vide : %s",
                chemin,
                exc,
            )
            return cls(chemin)

        if not isinstance(brut, dict):
            logger.warning(
                "offres vues invalides (%s) : mapping attendu, reçu %s — état vide",
                chemin,
                type(brut).__name__,
            )
            return cls(chemin)

        # Une entrée abîmée (édition à la main, conflit mal résolu) ne doit
        # pas coûter les autres : on garde tout ce qui a la bonne forme.
        vues = {
            cle: valeur
            for cle, valeur in brut.items()
            if isinstance(cle, str) and cle and isinstance(valeur, str)
        }
        if len(vues) != len(brut):
            logger.warning(
                "offres vues (%s) : %d entrée(s) invalide(s) ignorée(s)",
                chemin,
                len(brut) - len(vues),
            )
        return cls(chemin, vues)

    def __contains__(self, identifiant: object) -> bool:
        return identifiant in self._vues

    def __len__(self) -> int:
        return len(self._vues)

    def nouvelles(self, jobs: Iterable[Job]) -> DedupReport:
        """Sépare les offres jamais vues des autres, **sans rien mémoriser**.

        Le tri et la mémorisation sont deux temps distincts, et c'est ce qui
        permettra à la publication (Feature 4.3) de s'intercaler : une offre
        ne doit être marquée vue qu'une fois réellement arrivée sur le
        tableau. Marquée avant, un échec de publication la ferait
        disparaître pour toujours.

        Deux offres de même identifiant dans le run — la même source
        interrogée deux fois, un agrégateur qui liste une annonce dans deux
        catégories — ne comptent qu'une fois : la **première** est gardée,
        l'ordre d'entrée étant celui que le scoring (Feature 3.4) utilise
        pour départager.
        """
        nouvelles: list[Job] = []
        deja_vues: list[Job] = []
        doublons: list[Job] = []
        rencontres: set[str] = set()

        for job in jobs:
            identifiant = stable_id(job)
            if identifiant in rencontres:
                doublons.append(job)
            elif identifiant in self._vues:
                deja_vues.append(job)
            else:
                nouvelles.append(job)
            rencontres.add(identifiant)

        return DedupReport(nouvelles=nouvelles, deja_vues=deja_vues, doublons=doublons)

    # -- écriture -----------------------------------------------------------

    def marquer(self, jobs: Iterable[Job], *, quand: datetime | None = None) -> int:
        """Mémorise les offres, en mémoire. Retourne le nombre d'ajouts.

        Une offre déjà connue garde sa date de première vue : la réécrire à
        chaque run ferait bouger chaque ligne du fichier toutes les quinze
        minutes, et le diff Git ne montrerait plus rien d'utile.
        `save()` reste à appeler pour que ça survive au run.
        """
        date = (quand or datetime.now(timezone.utc)).strftime("%Y-%m-%dT%H:%M:%SZ")
        ajouts = 0
        for job in jobs:
            identifiant = stable_id(job)
            if identifiant not in self._vues:
                self._vues[identifiant] = date
                ajouts += 1
        return ajouts

    def save(self) -> None:
        """Écrit `seen.json`, trié et de façon atomique."""
        write_json_atomic(self.path, self._vues)

    def as_dict(self) -> dict[str, str]:
        """Copie de l'état, pour l'inspection et les tests."""
        return dict(self._vues)
