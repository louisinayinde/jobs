"""Filtre de rétention : la porte d'entrée du tableau (Feature 3.2).

C'est la première décision du pipeline qui **jette** de la donnée. La
normalisation (Feature 3.1) convertit sans juger ; ici on juge, et une
offre écartée ne sera jamais publiée, jamais dédupliquée, jamais changée
en CV. Trois mille cinq cents offres entrent à chaque run, quelques
dizaines en sortent : tout le rapport signal/bruit du projet se joue dans
ce module.

Quatre règles, appliquées dans cet ordre — et l'ordre compte, car c'est
lui qui décide **quel motif** est reporté quand plusieurs s'appliquent :

1. `title_exclude` — le rejet le plus net, prioritaire sur tout le reste ;
2. `title_include` — le poste cherché ;
3. `seniority` — le niveau visé ;
4. `location` — remote réel ou zone de relocalisation acceptée ;
5. `tech_include_any` — optionnel, désactivé par défaut.

**Rien n'est jeté en silence.** Chaque décision produit un `Verdict` qui
nomme la règle et le terme responsables, et le run affiche le décompte par
motif. Sans ça, « 3 492 offres collectées, 47 retenues » serait
indistinguable d'un filtre cassé qui rejette tout.

**Le doute profite à l'offre.** Un titre sans marqueur de séniorité — la
majorité — n'est pas un titre de séniorité inconnue *à rejeter*, c'est un
titre sur lequel la règle ne se prononce pas. Une règle qui rejetterait
par défaut viderait le tableau, et personne ne saurait pourquoi. Ce choix
est celui du pré-filtre de slug (US-2.5.1), et pour la même raison :
manquer une offre coûte plus cher que d'en afficher une de trop, qui se
repère d'un coup d'œil.

**La configuration mène, le module suit.** Les listes viennent toutes de
`filters.yaml` : ce module n'ajoute que des **synonymes** aux termes que
la configuration nomme déjà (« sr » pour `senior`, « Tokyo » pour `asia`).
Un niveau ou une région que le module ne connaît pas reste utilisable :
son propre nom sert de terme de recherche.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from functools import lru_cache
from typing import Iterable, Iterator, Mapping

from src.core.config import RetentionConfig
from src.core.normalize import TECH_VOCABULARY, Job

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Motifs de rejet
# ---------------------------------------------------------------------------

#: Une offre retenue n'a pas de motif : le champ reste vide.
RETENUE = ""

TITRE_EXCLU = "titre-exclu"
TITRE_NON_INCLUS = "titre-non-inclus"
SENIORITE = "seniorite"
LOCALISATION = "localisation"
TECH = "tech"

#: Les motifs dans l'ordre où les règles s'appliquent. Le bilan d'un run
#: les affiche dans cet ordre : un décompte qui garderait l'ordre
#: d'apparition changerait de forme à chaque run et ne se comparerait plus
#: d'un jour à l'autre.
MOTIFS: tuple[str, ...] = (
    TITRE_EXCLU,
    TITRE_NON_INCLUS,
    SENIORITE,
    LOCALISATION,
    TECH,
)


# ---------------------------------------------------------------------------
# Vocabulaire de séniorité (US-3.2.2)
# ---------------------------------------------------------------------------

#: Synonymes des niveaux de séniorité : nom du niveau → marqueurs écrits.
#:
#: Le nom du niveau lui-même est **toujours** un marqueur, ajouté
#: automatiquement : la table ne contient que ce qu'on ne devinerait pas.
#: Un niveau absent d'ici (`principal`, `lead`, ...) reste donc parfaitement
#: utilisable dans `filters.yaml` — il sera cherché sous son propre nom.
#:
#: **Règle de casse, reprise du détecteur de stack** (`normalize.py`) : un
#: marqueur tout en minuscules est cherché sans tenir compte de la casse ;
#: un marqueur qui porte une majuscule est cherché **à la casse exacte**.
#: C'est ce qui rend les chiffres romains utilisables : `II` trouve
#: « Software Engineer II » sans que `I` ne se déclenche sur le pronom
#: anglais « i ».
#:
#: **Absence assumée : « stage » seul.** Le mot désigne un stage en
#: français, mais aussi une étape en anglais — « Backend Engineer, Early
#: Stage Startup » disparaîtrait sans laisser de trace. « stagiaire » et
#: « internship » ne sont, eux, jamais ambigus.
SENIORITY_MARKERS: Mapping[str, tuple[str, ...]] = {
    "intern": (
        "intern",
        "internship",
        "stagiaire",
        "trainee",
        "apprentice",
        "apprenti",
        "alternance",
        "alternant",
        "working student",
        "werkstudent",
    ),
    "junior": (
        "junior",
        "jr",
        "graduate",
        "new grad",
        "entry level",
        "entry-level",
        "débutant",
        "debutant",
        "I",
    ),
    "mid": (
        "mid",
        "mid-level",
        "midlevel",
        "intermediate",
        "confirmé",
        "confirme",
        "II",
    ),
    "senior": (
        "senior",
        "sénior",
        "sr",
        "expérimenté",
        "experimente",
        "III",
        "IV",
    ),
    "staff": ("staff",),
    "principal": ("principal", "distinguished", "fellow"),
    "lead": ("lead", "tech lead", "team lead", "techlead"),
    "manager": ("manager", "head of", "director", "vp of", "vp,"),
}


# ---------------------------------------------------------------------------
# Géographie (US-3.2.3)
# ---------------------------------------------------------------------------

#: Ce que contient une zone : nom de région → pays, villes et codes qu'on
#: rencontre réellement dans le champ localisation des sources branchées.
#:
#: Sans cette table, `relocation_regions: ["asia"]` ne retiendrait que les
#: offres dont la localisation contient littéralement « Asia » — soit
#: aucune des 290 offres de Japan Dev, qui disent toutes « Tokyo, JP ».
#:
#: **Les zones sont disjointes, et c'est délibéré.** La France n'est pas
#: rangée dans `europe` alors qu'elle y est géographiquement : `filters.yaml`
#: les distingue (`geo_priority` donne 60 à l'Europe et 40 à la France), et
#: les fondre rendrait impossible d'accepter l'une sans l'autre.
#:
#: Liste volontairement courte et vérifiable plutôt qu'exhaustive : elle
#: couvre les priorités géo de `website.md`, et un terme manquant s'ajoute
#: ici — ou directement dans `relocation_regions`, qui accepte n'importe
#: quelle chaîne.
REGION_MEMBERS: Mapping[str, tuple[str, ...]] = {
    "asia": (
        "apac",
        "japan",
        "japon",
        "tokyo",
        "osaka",
        "kyoto",
        "singapore",
        "singapour",
        "hong kong",
        "korea",
        "seoul",
        "taiwan",
        "taipei",
        "vietnam",
        "hanoi",
        "ho chi minh",
        "indonesia",
        "jakarta",
        "philippines",
        "manila",
        "malaysia",
        "kuala lumpur",
        "thailand",
        "bangkok",
        "india",
        "bangalore",
        "bengaluru",
        "jst",
        "sgt",
    ),
    "europe": (
        "emea",
        "eu",
        "cet",
        "cest",
        "uk",
        "united kingdom",
        "london",
        "ireland",
        "dublin",
        "germany",
        "deutschland",
        "berlin",
        "munich",
        "hamburg",
        "netherlands",
        "amsterdam",
        "utrecht",
        "spain",
        "madrid",
        "barcelona",
        "valencia",
        "portugal",
        "lisbon",
        "lisboa",
        "porto",
        "italy",
        "milan",
        "rome",
        "poland",
        "warsaw",
        "krakow",
        "sweden",
        "stockholm",
        "denmark",
        "copenhagen",
        "norway",
        "oslo",
        "finland",
        "helsinki",
        "switzerland",
        "zurich",
        "geneva",
        "austria",
        "vienna",
        "belgium",
        "brussels",
        "czech",
        "prague",
        "romania",
        "bucharest",
        "greece",
        "athens",
        "estonia",
        "tallinn",
        "lithuania",
        "latvia",
        "hungary",
        "budapest",
        "bulgaria",
        "sofia",
    ),
    "france": (
        "fr",
        "french",
        "française",
        "francaise",
        "paris",
        "lyon",
        "marseille",
        "toulouse",
        "bordeaux",
        "lille",
        "nantes",
        "nice",
        "strasbourg",
        "rennes",
        "montpellier",
        "grenoble",
        "sophia antipolis",
    ),
}


# ---------------------------------------------------------------------------
# Correspondance de termes
# ---------------------------------------------------------------------------

#: Bornes d'un terme : ce qui, de part et d'autre, en ferait un morceau
#: d'un mot plus long. Un `\b` ne suffirait pas — « jr » suivi d'un point,
#: « mid » suivi d'un tiret sont des correspondances légitimes, et `\b` se
#: place au mauvais endroit dès que le terme finit par autre chose qu'une
#: lettre.
_BORNE_GAUCHE = r"(?<![A-Za-z0-9_])"
_BORNE_DROITE = r"(?![A-Za-z0-9_])"


@lru_cache(maxsize=512)
def _motif(terme: str) -> re.Pattern[str]:
    """Compile un terme en motif borné, à la casse voulue.

    Minuscules seules = terme sans ambiguïté, cherché sans tenir compte de
    la casse. Une majuscule = terme ambigu en anglais courant (`I`, `II`),
    cherché à la casse exacte. Même règle que `TECH_VOCABULARY`, pour la
    même raison.
    """
    drapeaux = 0 if any(c.isupper() for c in terme) else re.IGNORECASE
    return re.compile(_BORNE_GAUCHE + re.escape(terme) + _BORNE_DROITE, drapeaux)


def _mot_present(texte: str, terme: str) -> bool:
    """Le terme apparaît-il comme un mot entier dans `texte` ?"""
    return bool(terme) and bool(_motif(terme).search(texte))


def _sous_chaine_presente(texte_minuscule: str, terme: str) -> bool:
    """Le terme apparaît-il, même au milieu d'un mot ?

    C'est la comparaison documentée dans `filters.yaml` pour les titres
    (« insensible à la casse, sous-chaîne »), et celle du pré-filtre de
    slug (US-2.5.1). Elle est plus permissive que `_mot_present`, ce qui
    est le bon défaut pour une liste écrite à la main : « developer »
    trouve « Developers » et « Fullstack-Developer ».
    """
    terme = terme.strip().lower()
    return bool(terme) and terme in texte_minuscule


# ---------------------------------------------------------------------------
# US-3.2.4 — le vocabulaire technique, aligné sur ce qu'on filtre
# ---------------------------------------------------------------------------


def _entrees(vocabulaire: Mapping[str, tuple[str, ...]]) -> tuple[tuple[str, tuple[str, ...]], ...]:
    return tuple(sorted(vocabulaire.items()))


@lru_cache(maxsize=8)
def _index_alias(entrees: tuple[tuple[str, tuple[str, ...]], ...]) -> Mapping[str, str]:
    """Toutes les écritures d'une techno → son nom canonique, en minuscules.

    C'est ce qui fait que `tech_include_any: ["k8s"]` retient une offre dont
    `tech[]` porte `Kubernetes` : sans cet index, la comparaison se ferait
    entre l'alias écrit dans la config et le nom canonique produit par le
    détecteur, et ne correspondrait jamais.
    """
    index: dict[str, str] = {}
    for canonique, alias in entrees:
        index[canonique.lower()] = canonique
        for variante in alias:
            index.setdefault(variante.lower(), canonique)
    return index


def tech_vocabulary(
    retention: RetentionConfig,
    *,
    base: Mapping[str, tuple[str, ...]] = TECH_VOCABULARY,
) -> Mapping[str, tuple[str, ...]]:
    """Le vocabulaire du détecteur, augmenté des termes sur lesquels on filtre.

    `tech[]` est déduit par `normalize` contre un vocabulaire fermé
    (`TECH_VOCABULARY`). Filtrer sur un terme absent de ce vocabulaire
    rejetterait donc **toutes** les offres, sans que rien ne le dise : la
    techno cherchée serait invisible au détecteur. Chaque terme de
    `tech_include_any` inconnu du vocabulaire y est donc ajouté, et c'est
    ce vocabulaire-là que le run passe à `normalize`.

    Un terme ajouté est cherché **sans tenir compte de la casse** : il vient
    d'une main humaine, pas de la table soigneusement calibrée du module.
    Une techno dont le nom est aussi un mot anglais courant a sa place dans
    `TECH_VOCABULARY`, où la règle de casse la protège.
    """
    index = _index_alias(_entrees(base))
    supplement: dict[str, tuple[str, ...]] = {}
    vus: set[str] = set()

    for terme in retention.tech_include_any:
        propre = terme.strip()
        clef = propre.lower()
        if not propre or clef in index or clef in vus:
            continue
        vus.add(clef)
        supplement[propre] = (clef,)

    if not supplement:
        return base

    logger.info(
        "vocabulaire technique étendu pour la rétention : %s — "
        "sans quoi le filtre tech rejetterait tout",
        ", ".join(sorted(supplement)),
    )
    return {**base, **supplement}


# ---------------------------------------------------------------------------
# Le verdict
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Verdict:
    """Ce que le filtre a décidé d'une offre, et pourquoi.

    Le « pourquoi » n'est pas un luxe : c'est la seule chose qui distingue
    un filtre qui fait son travail d'un filtre cassé. `motif` nomme la
    règle, `detail` le terme qui l'a déclenchée.
    """

    gardee: bool
    motif: str = RETENUE
    detail: str = ""

    def __bool__(self) -> bool:
        return self.gardee

    @property
    def raison(self) -> str:
        """Phrase courte pour le journal, ou `""` si l'offre est retenue."""
        if self.gardee:
            return ""
        return f"{self.motif} ({self.detail})" if self.detail else self.motif


GARDEE = Verdict(True)


# ---------------------------------------------------------------------------
# Le filtre
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class RetentionFilter:
    """Les règles de `retention:` prêtes à juger une offre.

    Construit depuis la configuration (`from_config`), ou directement pour
    un test. Toutes les listes vides = filtre neutre : tout passe. C'est le
    comportement documenté de `filters.yaml`, et le seul défaut sûr — une
    configuration muette ne doit pas vider le tableau.
    """

    title_include: tuple[str, ...] = ()
    title_exclude: tuple[str, ...] = ()
    seniority_keep: tuple[str, ...] = ()
    seniority_drop: tuple[str, ...] = ()
    location_require_any: tuple[str, ...] = ()
    relocation_regions: tuple[str, ...] = ()
    tech_include_any: tuple[str, ...] = ()
    #: Le vocabulaire que `normalize` doit utiliser pour que `tech[]` puisse
    #: contenir ce sur quoi on filtre. Hors comparaison : deux filtres aux
    #: mêmes règles sont les mêmes règles.
    #: `default_factory` et non `default` : un `dict` en défaut de champ est
    #: refusé par `dataclasses`, qui ne peut pas savoir qu'on ne le mutera
    #: jamais.
    vocabulaire: Mapping[str, tuple[str, ...]] = field(
        default_factory=lambda: TECH_VOCABULARY, compare=False, repr=False
    )

    @classmethod
    def from_config(
        cls,
        retention: RetentionConfig,
        *,
        base: Mapping[str, tuple[str, ...]] = TECH_VOCABULARY,
    ) -> RetentionFilter:
        """Construit le filtre depuis la section `retention:` de `filters.yaml`.

        Le vocabulaire technique est calculé ici, et pas ailleurs : c'est ce
        qui rend impossible de filtrer sur une techno que le détecteur ne
        sait pas voir (voir `tech_vocabulary`).
        """
        return cls(
            title_include=tuple(retention.title_include),
            title_exclude=tuple(retention.title_exclude),
            seniority_keep=tuple(retention.seniority.keep),
            seniority_drop=tuple(retention.seniority.drop),
            location_require_any=tuple(retention.location.require_any),
            relocation_regions=tuple(retention.location.relocation_regions),
            tech_include_any=tuple(retention.tech_include_any),
            vocabulaire=tech_vocabulary(retention, base=base),
        )

    # -- US-3.2.1 — le titre ------------------------------------------------

    def _juger_titre(self, job: Job) -> Verdict:
        """`title_exclude` d'abord, `title_include` ensuite.

        L'exclusion est prioritaire, comme le documente `filters.yaml` :
        « Sales Engineer » contient « engineer » et serait retenu sans cette
        priorité. Une offre sans titre échoue à `title_include` dès que la
        liste est non vide — ne pas savoir de quel poste il s'agit n'est pas
        une raison de le publier.
        """
        titre = job.titre.lower()

        for terme in self.title_exclude:
            if _sous_chaine_presente(titre, terme):
                return Verdict(False, TITRE_EXCLU, terme)

        if not self.title_include:
            return GARDEE
        if any(_sous_chaine_presente(titre, terme) for terme in self.title_include):
            return GARDEE
        return Verdict(False, TITRE_NON_INCLUS, job.titre)

    # -- US-3.2.2 — la séniorité --------------------------------------------

    def _juger_seniorite(self, job: Job) -> Verdict:
        """`drop` l'emporte sur `keep`, et le silence profite à l'offre.

        Les niveaux sont cherchés dans le **titre seul**. Une description
        dit « vous encadrerez des ingénieurs juniors » sans être une offre
        junior : la lire ferait rejeter exactement les offres seniors qu'on
        cherche.

        Trois cas, dans l'ordre :

        1. un niveau de `drop` est écrit dans le titre → rejet, même s'il
           figure aussi dans `keep` (`filters.yaml` : « à toujours rejeter ») ;
        2. `keep` vide → rien à exiger, l'offre passe ;
        3. `keep` non vide → l'offre passe si le titre porte un niveau de
           `keep`, **ou aucun niveau du tout**. Ce dernier point est le
           choix structurant du module : la majorité des titres n'affichent
           pas de séniorité (« Backend Engineer »), et les rejeter au motif
           qu'ils ne prouvent pas leur niveau viderait le tableau.

        Le prix de ce choix, à connaître avant d'écrire `keep` : un niveau
        que le module sait lire mais que `keep` ne cite pas **est** rejeté.
        Avec `keep: [mid, senior, staff]`, « Lead », « Principal »,
        « Director » et « Manager » tombent — ce que la recherche Google de
        `website.md` demande explicitement (`-manager -director`), mais qui
        emporte aussi « Tech Lead Backend Engineer ». Le remède tient en un
        mot ajouté à `keep`.
        """
        for niveau in self.seniority_drop:
            if _niveau_present(job.titre, niveau):
                return Verdict(False, SENIORITE, niveau)

        if not self.seniority_keep:
            return GARDEE
        if any(_niveau_present(job.titre, niveau) for niveau in self.seniority_keep):
            return GARDEE

        detectes = _niveaux_detectes(job.titre)
        if not detectes:
            # Aucun marqueur : la règle ne se prononce pas.
            return GARDEE
        return Verdict(False, SENIORITE, ", ".join(sorted(detectes)))

    # -- US-3.2.3 — localisation & remote -----------------------------------

    def _juger_localisation(self, job: Job) -> Verdict:
        """Remote réel **ou** zone de relocalisation acceptée.

        Les deux listes forment une seule porte : une offre passe si elle
        satisfait l'une ou l'autre. Les deux vides = aucune contrainte.

        Le texte examiné est la localisation **plus** le `remote_type`
        normalisé. Sans ce second terme, `require_any: ["remote"]` raterait
        toutes les offres de Remotive et de Japan Dev, qui affichent
        « Anywhere » ou « Tokyo, JP » alors que la source a dit, dans un
        champ dédié, qu'elles sont en télétravail.

        ⚠️ **Le « remote menteur » n'est pas traité ici.** « Remote (US
        only) » satisfait `require_any: ["remote"]` alors que le poste est
        hors d'atteinte. Le rejeter demanderait une clé de configuration qui
        n'existe pas encore ; en attendant, ces offres arrivent sur le
        tableau où leur localisation est affichée telle quelle.
        """
        if not self.location_require_any and not self.relocation_regions:
            return GARDEE

        texte = f"{job.localisation} {job.remote_type}"
        minuscule = texte.lower()

        if any(
            _sous_chaine_presente(minuscule, terme)
            for terme in self.location_require_any
        ):
            return GARDEE
        if any(_region_presente(texte, region) for region in self.relocation_regions):
            return GARDEE

        return Verdict(False, LOCALISATION, job.localisation or job.remote_type)

    # -- US-3.2.4 — la stack (optionnel) ------------------------------------

    def _juger_tech(self, job: Job) -> Verdict:
        """Filtre d'inclusion, désactivé tant que la liste est vide.

        La comparaison passe par le nom canonique : `k8s` dans la
        configuration retient une offre dont `tech[]` porte `Kubernetes`.
        """
        if not self.tech_include_any:
            return GARDEE

        index = _index_alias(_entrees(self.vocabulaire))
        attendues = {
            index.get(terme.strip().lower(), terme.strip())
            for terme in self.tech_include_any
            if terme.strip()
        }
        if not attendues or attendues & set(job.tech):
            return GARDEE
        return Verdict(False, TECH, ", ".join(sorted(attendues)))

    # -- L'enchaînement -----------------------------------------------------

    def juge(self, job: Job) -> Verdict:
        """Le verdict complet, motif compris. Première règle qui rejette gagne."""
        for regle in (
            self._juger_titre,
            self._juger_seniorite,
            self._juger_localisation,
            self._juger_tech,
        ):
            verdict = regle(job)
            if not verdict.gardee:
                return verdict
        return GARDEE

    def retient(self, job: Job) -> bool:
        return self.juge(job).gardee

    def appliquer(self, jobs: Iterable[Job]) -> RetentionReport:
        """Juge toutes les offres d'un run et retourne le bilan.

        L'ordre des offres retenues est celui d'entrée : le scoring
        (Feature 3.4) s'en sert pour départager deux offres à égalité de
        façon reproductible.
        """
        retenues: list[Job] = []
        rejets: list[tuple[Job, Verdict]] = []

        for job in jobs:
            verdict = self.juge(job)
            if verdict.gardee:
                retenues.append(job)
            else:
                rejets.append((job, verdict))
                # DEBUG et pas WARNING : sur un run réel, écarter est le cas
                # normal — trois mille lignes d'avertissement noieraient les
                # vraies anomalies. Le décompte par motif, lui, est affiché.
                logger.debug(
                    "offre écartée (%s) : « %s » — %s",
                    verdict.raison,
                    job.titre or job.id,
                    job.url,
                )

        return RetentionReport(retenues=retenues, rejets=rejets)


# ---------------------------------------------------------------------------
# Détection de séniorité et de région (partagées, donc hors du filtre)
# ---------------------------------------------------------------------------


@lru_cache(maxsize=256)
def _marqueurs(niveau: str) -> tuple[str, ...]:
    """Tous les termes qui trahissent un niveau, son propre nom compris.

    Un niveau inconnu de `SENIORITY_MARKERS` n'est pas une erreur de
    configuration : il est simplement cherché tel quel. `filters.yaml` peut
    ainsi écrire `drop: ["principal", "vp"]` sans que ce module ait à
    connaître « vp ».
    """
    clef = niveau.strip().lower()
    if not clef:
        return ()
    synonymes = SENIORITY_MARKERS.get(clef, ())
    return (clef, *(terme for terme in synonymes if terme.lower() != clef))


def _niveau_present(titre: str, niveau: str) -> bool:
    return any(_mot_present(titre, terme) for terme in _marqueurs(niveau))


def _niveaux_detectes(titre: str) -> set[str]:
    """Les niveaux du vocabulaire du module lisibles dans le titre.

    Sert uniquement à **nommer** le niveau dans le motif de rejet : la
    décision, elle, ne regarde que les niveaux que la configuration cite.
    """
    return {niveau for niveau in SENIORITY_MARKERS if _niveau_present(titre, niveau)}


@lru_cache(maxsize=64)
def _termes_region(region: str) -> tuple[str, ...]:
    """Le nom de la région et ce qu'elle contient."""
    clef = region.strip().lower()
    if not clef:
        return ()
    membres = REGION_MEMBERS.get(clef, ())
    return (clef, *(terme for terme in membres if terme.lower() != clef))


def _region_presente(texte: str, region: str) -> bool:
    """Correspondance **mot entier**, contrairement aux titres.

    La table de régions contient des codes de deux lettres (`fr`, `eu`,
    `uk`) qu'on rencontre tels quels dans une localisation (« Paris, FR »).
    En sous-chaîne, ils tagueraient la moitié du monde.
    """
    return any(_mot_present(texte, terme) for terme in _termes_region(region))


# ---------------------------------------------------------------------------
# Le bilan
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class RetentionReport:
    """Ce qu'a donné la rétention sur un run : ce qui passe, ce qui tombe, pourquoi."""

    retenues: list[Job] = field(default_factory=list)
    rejets: list[tuple[Job, Verdict]] = field(default_factory=list)

    @property
    def total(self) -> int:
        return len(self.retenues) + len(self.rejets)

    @property
    def par_motif(self) -> dict[str, int]:
        """Décompte des rejets par règle, dans l'ordre fixe de `MOTIFS`.

        Les motifs à zéro sont omis : une ligne de bilan doit tenir sur une
        ligne. L'ordre, lui, est fixe — deux runs se comparent d'un coup
        d'œil.
        """
        comptes = {motif: 0 for motif in MOTIFS}
        for _, verdict in self.rejets:
            comptes[verdict.motif] = comptes.get(verdict.motif, 0) + 1
        return {motif: n for motif, n in comptes.items() if n}

    @property
    def resume(self) -> str:
        """Une ligne pour le journal du run."""
        detail = ", ".join(f"{motif} {n}" for motif, n in self.par_motif.items())
        base = f"{len(self.retenues)} retenue(s) sur {self.total}"
        if not self.rejets:
            return base
        return f"{base} — {len(self.rejets)} écartée(s) ({detail})"

    def __iter__(self) -> Iterator[Job]:
        """Itérer sur le bilan, c'est itérer sur les offres retenues."""
        return iter(self.retenues)


def apply_retention(
    jobs: Iterable[Job], retention: RetentionConfig
) -> RetentionReport:
    """Raccourci : construit le filtre depuis la config et l'applique.

    ⚠️ Ne convient qu'aux offres **déjà normalisées avec le bon
    vocabulaire**. Un run qui filtre sur `tech_include_any` doit construire
    le `RetentionFilter` d'abord, passer son `.vocabulaire` à `normalize`,
    puis appeler `.appliquer` — c'est ce que fait `src/collect.py`.
    """
    return RetentionFilter.from_config(retention).appliquer(jobs)
