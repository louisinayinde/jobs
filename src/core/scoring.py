"""Scoring géo : classer les offres retenues de la plus à la moins prioritaire (Feature 3.4).

La rétention (Feature 3.2) décide **si** une offre arrive sur le tableau ;
ce module décide **où**. Les priorités sont celles de `website.md` —
① remote-first global, ② Asie + relocalisation, ③ Europe, ④ France — et
leurs valeurs viennent de `scoring.geo_priority` dans `filters.yaml`.

Deux pièces, une par US :

1. **`GeoScorer.noter`** (US-3.4.1) — le score d'une offre, avec la
   catégorie et le terme qui l'ont donné ;
2. **`trier`** (US-3.4.2) — l'ordre du tableau : score décroissant, puis
   date la plus récente, puis ordre d'entrée.

**Les catégories sont lues dans la localisation, avec les zones de la
rétention.** `asia_relocation` cherche la zone `asia` de `REGION_MEMBERS`,
exactement la table qui a retenu l'offre. Une seconde table divergerait de
la première, et une offre retenue pour « Tokyo » serait classée comme si
elle ne venait de nulle part. Une clé se lit ainsi : son nom, privé du
suffixe `_relocation`, est la zone cherchée — et une zone que le module ne
connaît pas (`japan: 90`) est cherchée sous son propre nom, comme en
rétention.

**Une offre qui touche plusieurs zones prend la meilleure.** « Paris;
London; Lyon » vaut 60 et pas 40 : le poste est ouvert à Londres, et c'est
ce qu'on en retient de mieux.

**Le « remote menteur » est rejugé ici** (limite nommée à l'US-3.2.3). Une
offre n'est `remote_global` que si elle est en télétravail **et que sa
localisation ne nomme aucun lieu** : « Worldwide », « Anywhere in the
World », « Remote », ou rien du tout. « Remote, Canada », « Remote (US) »
ou « Anywhere in the US » nomment un lieu hors des zones acceptées : elles
reçoivent le score plancher. La rétention les laisse passer — le tableau
les affiche —, mais en bas, pas en haut. C'est le cas de quatre des six
offres remote des fixtures ATS réelles : sans cette règle, elles
passeraient toutes devant le Japon.

Le choix inverse — une liste de pays hors zone, et `remote_global` pour
tout le reste — classerait en tête le premier pays oublié de la liste.
Une liste de **mots neutres** se trompe dans l'autre sens : une formulation
mondiale inédite tombe au plancher. Elle reste publiée, et un mot s'ajoute
à `MOTS_NEUTRES`.

**Le plancher n'est jamais une exception.** Localisation vide, illisible,
ou `geo_priority` absent : l'offre reçoit `default` (0 s'il n'est pas
écrit), et le tri continue. Le scoring ordonne, il ne jette rien.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Iterable, Iterator, Mapping

from src.adapters.base import REMOTE
from src.core.config import ScoringConfig
from src.core.normalize import Job
from src.core.retention import REGION_MEMBERS, region_trouvee

logger = logging.getLogger(__name__)

#: Clé de `geo_priority` pour le télétravail sans restriction de lieu.
REMOTE_GLOBAL = "remote_global"

#: Clé de `geo_priority` du score plancher : l'offre qu'aucune catégorie
#: ne décrit.
DEFAULT = "default"

#: Score plancher quand `default` n'est pas écrit dans la configuration.
SCORE_PLANCHER = 0

#: Suffixe qu'une clé peut porter sans changer la zone cherchée :
#: `asia_relocation` cherche `asia`.
SUFFIXE_RELOCATION = "_relocation"

#: Les mots qui ne nomment **aucun lieu**. Une localisation d'offre remote
#: faite uniquement de ces mots est une offre ouverte au monde entier.
#:
#: Tout mot absent de cette liste compte comme un lieu, et c'est voulu :
#: c'est ce qui fait tomber « Remote, Canada » et « Anywhere in the US »
#: au plancher sans avoir à lister tous les pays hors zone. Pour la même
#: raison, `anywhere` n'est pas un marqueur mondial à lui seul — il est
#: neutre, et « in the US » qui le suit ne l'est pas.
MOTS_NEUTRES: frozenset[str] = frozenset(
    {
        # anglais
        "remote",
        "remotely",
        "anywhere",
        "worldwide",
        "world",
        "global",
        "globally",
        "international",
        "distributed",
        "fully",
        "full",
        "first",
        "only",
        "work",
        "from",
        "home",
        "wfh",
        "any",
        "location",
        "locations",
        "flexible",
        "in",
        "the",
        "of",
        "or",
        "and",
        # français
        "télétravail",
        "teletravail",
        "complet",
        "total",
        "partout",
        "monde",
        "dans",
        "le",
        "la",
        "en",
        "à",
        "a",
    }
)

#: Un mot, au sens de `MOTS_NEUTRES` : une suite de lettres. Les chiffres
#: (« 100% Remote ») et la ponctuation (« Remote (Worldwide) ») ne nomment
#: pas de lieu.
_MOT_RE = re.compile(r"[^\W\d_]+")

#: Date d'une offre sans date lisible : la plus ancienne possible, pour
#: qu'elle passe **après** ses égales datées.
_JAMAIS = datetime.min.replace(tzinfo=timezone.utc)


# ---------------------------------------------------------------------------
# US-3.4.1 — le score
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ScoredJob:
    """Une offre et son score, avec ce qui l'a décidé.

    `categorie` est la clé de `geo_priority` retenue, `detail` le terme de
    localisation qui l'a déclenchée (`tokyo`, `london`) — vide pour
    `remote_global` et `default`, qui se décident sur une absence. Les deux
    servent au journal du run : un score seul ne dit pas s'il est juste.
    """

    job: Job
    score: int
    categorie: str
    detail: str = ""


def ne_nomme_aucun_lieu(localisation: str) -> bool:
    """La localisation n'est-elle faite que de mots neutres (ou vide) ?"""
    return all(mot in MOTS_NEUTRES for mot in _MOT_RE.findall(localisation.lower()))


@dataclass(frozen=True)
class GeoScorer:
    """Les priorités de `scoring.geo_priority`, prêtes à noter une offre.

    `priorites` garde l'ordre de la configuration : il départage deux
    catégories de même score (la première écrite gagne) et fixe l'ordre du
    bilan.
    """

    priorites: Mapping[str, int] = field(default_factory=dict)

    @classmethod
    def from_config(cls, scoring: ScoringConfig) -> GeoScorer:
        scorer = cls(dict(scoring.geo_priority))
        for clef in scorer._zones:
            zone = _zone(clef)
            if zone not in REGION_MEMBERS:
                # Pas une erreur : `japan: 90` est une configuration valable.
                # Mais une faute de frappe (`remote_globl`) ressemble
                # exactement à ça, et ferait disparaître une catégorie sans
                # un mot — d'où l'avertissement.
                logger.warning(
                    "geo_priority.%s : zone « %s » inconnue du module, "
                    "cherchée telle quelle dans la localisation",
                    clef,
                    zone,
                )
        return scorer

    @property
    def plancher(self) -> int:
        """Le score d'une offre qu'aucune catégorie ne décrit."""
        return self.priorites.get(DEFAULT, SCORE_PLANCHER)

    @property
    def _zones(self) -> list[str]:
        """Les clés qui désignent une zone géographique."""
        return [clef for clef in self.priorites if clef not in (REMOTE_GLOBAL, DEFAULT)]

    def noter(self, job: Job) -> ScoredJob:
        """Le score de l'offre : la meilleure catégorie qu'elle satisfait.

        Ne lève jamais : une offre sans localisation, ou une configuration
        sans `geo_priority`, donne le plancher.
        """
        candidats: list[ScoredJob] = []

        if (
            REMOTE_GLOBAL in self.priorites
            and job.remote_type == REMOTE
            and ne_nomme_aucun_lieu(job.localisation)
        ):
            candidats.append(ScoredJob(job, self.priorites[REMOTE_GLOBAL], REMOTE_GLOBAL))

        for clef in self._zones:
            terme = region_trouvee(job.localisation, _zone(clef))
            if terme:
                candidats.append(ScoredJob(job, self.priorites[clef], clef, terme))

        if not candidats:
            return ScoredJob(job, self.plancher, DEFAULT)
        # `max` rend le premier des ex æquo : l'ordre de la configuration.
        return max(candidats, key=lambda candidat: candidat.score)

    def classer(self, jobs: Iterable[Job]) -> ScoringReport:
        """Note toutes les offres d'un run et les trie (US-3.4.1 + 3.4.2)."""
        return ScoringReport(
            offres=trier(self.noter(job) for job in jobs),
            categories=(*self._categories_ordonnees(), DEFAULT),
        )

    def _categories_ordonnees(self) -> list[str]:
        return [clef for clef in self.priorites if clef != DEFAULT]


def _zone(clef: str) -> str:
    """`asia_relocation` → `asia` ; `europe` → `europe`."""
    return clef.removesuffix(SUFFIXE_RELOCATION)


# ---------------------------------------------------------------------------
# US-3.4.2 — le tri
# ---------------------------------------------------------------------------


def _instant(date: str) -> datetime:
    """La date de publication, ou la plus ancienne possible si illisible.

    `normalize` garantit un format ISO unique, et une comparaison de
    chaînes suffirait presque — mais « presque » ferait passer une date en
    `Z` après la même date en `+00:00`. Parser coûte une ligne.
    """
    try:
        instant = datetime.fromisoformat(date)
    except ValueError:
        return _JAMAIS
    return instant if instant.tzinfo else instant.replace(tzinfo=timezone.utc)


def trier(offres: Iterable[ScoredJob]) -> list[ScoredJob]:
    """Du meilleur score au moins bon, sans toucher à l'entrée.

    À score égal, l'offre **la plus récente** passe devant : sur un tableau
    de revue, une offre fraîche est celle où candidater tôt compte le plus.
    À score et date égaux, l'**ordre d'entrée** est conservé — celui des
    sources, que la rétention et la dédup préservent. `sorted` est stable :
    deux runs sur les mêmes offres donnent le même tableau.

    Une offre sans date lisible passe après ses égales datées.
    """
    return sorted(
        offres,
        key=lambda offre: (-offre.score, -_instant(offre.job.date).timestamp()),
    )


# ---------------------------------------------------------------------------
# Le bilan
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ScoringReport:
    """Les offres d'un run, notées et triées."""

    offres: list[ScoredJob] = field(default_factory=list)
    #: L'ordre d'affichage du décompte : celui de la configuration, le
    #: plancher en dernier. Fixe, pour que deux runs se comparent.
    categories: tuple[str, ...] = (DEFAULT,)

    @property
    def jobs(self) -> list[Job]:
        """Les offres seules, dans l'ordre du tableau."""
        return [offre.job for offre in self.offres]

    @property
    def par_categorie(self) -> dict[str, int]:
        """Décompte par catégorie, catégories vides omises."""
        comptes = {categorie: 0 for categorie in self.categories}
        for offre in self.offres:
            comptes[offre.categorie] = comptes.get(offre.categorie, 0) + 1
        return {categorie: n for categorie, n in comptes.items() if n}

    @property
    def resume(self) -> str:
        """Une ligne pour le journal du run."""
        base = f"{len(self.offres)} offre(s) classée(s)"
        if not self.offres:
            return base
        detail = ", ".join(f"{categorie} {n}" for categorie, n in self.par_categorie.items())
        return f"{base} — {detail}"

    def __iter__(self) -> Iterator[ScoredJob]:
        return iter(self.offres)

    def __len__(self) -> int:
        return len(self.offres)
