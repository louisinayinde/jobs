"""Publier une offre sur le tableau, jamais deux fois (US-4.2.2).

`publier_offre` enchaîne les deux gestes du client GitHub (Feature 4.1) —
créer l'Issue, poser sa carte — en consultant le registre des fiches avant
chacun et en l'écrivant **sur disque juste après**. Chaque étape faite est
donc connue du run suivant, même si celui-ci s'arrête net à l'étape d'après :

| Registre avant           | Appels à GitHub                          |
|--------------------------|------------------------------------------|
| offre inconnue           | création de l'Issue, pose de la carte    |
| Issue connue, sans carte | pose de la carte seulement               |
| Issue et carte connues   | **aucun**                                |
| création `en_cours`      | recherche de l'Issue, puis selon trouvée |

**Le cas `en_cours` est celui qui fait les doublons.** Un timeout ou un 502
sur la création ne dit pas si l'Issue existe (voir `github.py`). L'entrée
est posée *avant* l'envoi ; si la réponse ne vient pas, elle reste, et la
publication suivante liste les Issues du dépôt modifiées depuis cet instant
pour y chercher l'identifiant de l'offre. Trouvée, elle est adoptée ; absente,
la création est refaite. Un refus net (401, 403, 422…) garantit au contraire
que rien n'a été créé : l'entrée est retirée aussitôt.

**`publier_lot` (US-4.3.1)** publie les offres nouvelles d'un run, dans
l'ordre où elles arrivent — celui du score, que `GeoScorer.classer` a fixé —
et rend un bilan : ce qui est arrivé sur le tableau, ce qui y était déjà, ce
que GitHub a refusé, ce qui reste pour le run suivant. Quatre règles :

- **le tableau est vérifié avant la première Issue.** Un Project introuvable
  ou sans option « Nouveau » lèverait sinon *après* la création : une Issue
  sans carte par run, jusqu'à ce que quelqu'un regarde ;
- **une panne arrête le lot**, elle ne le traverse pas. Un 401, une limite de
  débit épuisée, un 502 frappent toutes les offres suivantes de la même
  façon : les essayer quand même ne ferait que multiplier les appels voués
  à l'échec. Les offres restantes sont *reportées* — non mémorisées comme
  vues, elles reviennent au run suivant, toujours dans l'ordre du score ;
- **un refus propre à l'offre (422) ne l'arrête pas.** GitHub rejette ce
  contenu-là et rejetterait le même au run suivant : la retenter à chaque
  quart d'heure bloquerait tout ce qui est classé derrière elle. Elle est
  signalée, et comptée comme traitée ;
- **le nombre de fiches par run est plafonné**, et une pause sépare deux
  fiches. GitHub limite les créations de contenu (80 par minute, 500 par
  heure) ; le premier run après un `seen.json` vidé a plusieurs centaines
  d'offres à publier. Au-delà du plafond, les offres sont reportées : les
  meilleures partent d'abord, les autres suivent de quart d'heure en quart
  d'heure. Une offre que le registre connaît déjà ne coûte aucun appel, et
  passe même plafond atteint.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Iterable

from src.board.github import GitHubClient, GitHubError, Issue
from src.board.registre import Fiche, RegistreFiches
from src.core.dedup import stable_id
from src.core.scoring import ScoredJob

logger = logging.getLogger(__name__)

#: Recul appliqué à l'instant d'une création incertaine avant de chercher
#: l'Issue : l'horloge du runner et celle de GitHub ne tombent pas pile
#: ensemble, et une Issue manquée ici serait une Issue en double.
MARGE_HORLOGE = timedelta(minutes=10)

#: Fiches publiées au plus par run. À 3 appels par fiche et une collecte par
#: quart d'heure, 50 fiches font 200 créations d'Issue par heure : sous les
#: 500 que GitHub tolère, avec la marge d'un run lent qui publierait aussi.
MAX_PUBLICATIONS = 50

#: Pause entre deux fiches, en secondes. GitHub demande au moins une seconde
#: entre deux écritures ; sans elle, un lot de 50 fiches enchaîne 150
#: écritures en moins d'une minute et déclenche la limite secondaire.
PAUSE_ENTRE_FICHES = 1.0

#: Statut d'un refus qui tient au contenu envoyé, pas à l'état de GitHub.
STATUT_REFUS_OFFRE = 422


def _pause(secondes: float) -> None:
    """Attente entre deux fiches. Isolée pour que les tests la neutralisent."""
    time.sleep(secondes)


@dataclass(frozen=True)
class Publication:
    """Ce qu'a fait `publier_offre` pour une offre."""

    identifiant: str
    #: Numéro de l'Issue de l'offre — créée maintenant ou bien avant.
    issue: int
    #: Une Issue a été créée par cet appel.
    creee: bool
    #: Une carte a été posée sur le tableau par cet appel.
    posee: bool


@dataclass(frozen=True)
class BilanPublication:
    """Ce qu'a fait `publier_lot` d'un lot d'offres, dans l'ordre du lot."""

    #: Offres qui ont leur fiche sur le tableau — arrivée par cet appel ou avant.
    publications: list[Publication] = field(default_factory=list)
    #: Offres refusées par GitHub pour leur contenu : ni publiées, ni retentées.
    refusees: list[ScoredJob] = field(default_factory=list)
    #: Offres traitées, publiées ou refusées : celles à mémoriser comme vues.
    traitees: list[ScoredJob] = field(default_factory=list)
    #: Offres laissées au run suivant — plafond atteint ou lot interrompu.
    reportees: list[ScoredJob] = field(default_factory=list)
    #: Motif de l'interruption du lot, `""` s'il est allé au bout.
    erreur: str = ""

    @property
    def publiees(self) -> list[Publication]:
        """Les fiches **arrivées sur le tableau** par cet appel.

        Une Issue créée, ou une Issue d'un run précédent dont la carte vient
        d'être posée. Une offre déjà sur le tableau n'en fait pas partie.
        """
        return [p for p in self.publications if p.creee or p.posee]

    @property
    def deja_publiees(self) -> list[Publication]:
        return [p for p in self.publications if not (p.creee or p.posee)]

    @property
    def interrompu(self) -> bool:
        return bool(self.erreur)

    @property
    def en_echec(self) -> bool:
        """Le run doit le signaler : lot interrompu, ou offre refusée."""
        return self.interrompu or bool(self.refusees)

    @property
    def resume(self) -> str:
        """Une ligne pour le journal du run.

        Le nombre publié est affiché même à zéro : c'est le chiffre à
        surveiller. Des offres nouvelles run après run et zéro fiche publiée,
        c'est une publication en panne, pas un marché calme.
        """
        total = len(self.publications) + len(self.refusees) + len(self.reportees)
        morceaux = [f"{len(self.publiees)} fiche(s) publiée(s) sur {total} offre(s)"]
        if self.deja_publiees:
            morceaux.append(f"{len(self.deja_publiees)} déjà sur le tableau")
        if self.refusees:
            morceaux.append(f"{len(self.refusees)} refusée(s) par GitHub")
        if self.reportees:
            morceaux.append(f"{len(self.reportees)} reportée(s) au run suivant")
        ligne = ", ".join(morceaux)
        if self.erreur:
            ligne += f" — INTERROMPUE : {self.erreur}"
        return ligne


def publier_lot(
    github: GitHubClient,
    registre: RegistreFiches,
    offres: Iterable[ScoredJob],
    *,
    max_publications: int = MAX_PUBLICATIONS,
    maintenant: datetime | None = None,
) -> BilanPublication:
    """Publie chaque offre du lot, dans l'ordre reçu, sans jamais lever.

    Les erreurs GitHub sont rendues dans le bilan (`erreur`, `refusees`) :
    l'appelant a toujours de quoi mémoriser ce qui a été fait avant la panne.
    """
    publications: list[Publication] = []
    refusees: list[ScoredJob] = []
    traitees: list[ScoredJob] = []
    reportees: list[ScoredJob] = []
    erreur = ""
    tableau_verifie = False
    a_appele = False

    for offre in offres:
        identifiant = stable_id(offre.job)
        if erreur:
            reportees.append(offre)
            continue

        appelle = _demande_github(registre.get(identifiant))
        if appelle:
            if sum(1 for p in publications if p.creee or p.posee) >= max_publications:
                reportees.append(offre)
                continue
            if a_appele:
                _pause(PAUSE_ENTRE_FICHES)
            a_appele = True

        try:
            if appelle and not tableau_verifie:
                github.option_statut()
                tableau_verifie = True
            publication = publier_offre(github, registre, offre, maintenant=maintenant)
        except GitHubError as exc:
            if exc.statut == STATUT_REFUS_OFFRE:
                logger.error(
                    "offre %s refusée par GitHub, elle ne sera pas retentée — %s",
                    identifiant,
                    exc,
                )
                refusees.append(offre)
                traitees.append(offre)
                continue
            erreur = f"{type(exc).__name__} — {exc}"
            logger.error("publication interrompue sur l'offre %s — %s", identifiant, erreur)
            reportees.append(offre)
            continue

        publications.append(publication)
        traitees.append(offre)

    return BilanPublication(
        publications=publications,
        refusees=refusees,
        traitees=traitees,
        reportees=reportees,
        erreur=erreur,
    )


def _demande_github(fiche: Fiche | None) -> bool:
    """Publier cette offre appellera GitHub : elle n'est pas entièrement sur le tableau."""
    return fiche is None or fiche.en_cours is not None or fiche.a_poser


def publier_offre(
    github: GitHubClient,
    registre: RegistreFiches,
    offre: ScoredJob,
    *,
    maintenant: datetime | None = None,
) -> Publication:
    """Donne à l'offre sa fiche sur le tableau, sans jamais en créer une seconde."""
    identifiant = stable_id(offre.job)
    fiche = registre.get(identifiant)
    creee = posee = False

    if fiche is not None and fiche.en_cours is not None:
        fiche = _retrouver(github, registre, identifiant, fiche.en_cours)

    if fiche is None:
        registre.enregistrer(
            identifiant, Fiche(en_cours=maintenant or datetime.now(timezone.utc))
        )
        registre.save()
        try:
            issue = github.creer_issue_offre(offre)
        except GitHubError as exc:
            if not exc.peut_avoir_abouti:
                registre.oublier(identifiant)
                registre.save()
            raise
        fiche = Fiche(issue=issue.number, node_id=issue.node_id)
        registre.enregistrer(identifiant, fiche)
        registre.save()
        creee = True

    if fiche.a_poser:
        assert fiche.issue is not None
        item = github.ajouter_au_projet(
            Issue(number=fiche.issue, node_id=fiche.node_id, url="", titre="")
        )
        fiche = registre.poser(identifiant, item.id)
        registre.save()
        posee = True

    assert fiche.issue is not None
    return Publication(identifiant=identifiant, issue=fiche.issue, creee=creee, posee=posee)


def _retrouver(
    github: GitHubClient, registre: RegistreFiches, identifiant: str, depuis: datetime
) -> Fiche | None:
    """Tranche une création incertaine : l'Issue adoptée, ou l'entrée retirée."""
    trouvee = github.lister_fiches(depuis=depuis - MARGE_HORLOGE).get(identifiant)
    if trouvee is None:
        logger.warning(
            "offre %s : la création interrompue n'a pas abouti, nouvelle création",
            identifiant,
        )
        registre.oublier(identifiant)
        registre.save()
        return None

    logger.warning(
        "offre %s : la création interrompue avait abouti — Issue #%d adoptée",
        identifiant,
        trouvee.number,
    )
    fiche = Fiche(issue=trouvee.number, node_id=trouvee.node_id)
    registre.enregistrer(identifiant, fiche)
    registre.save()
    return fiche


def reconstruire(github: GitHubClient, path: str | Path | None = None) -> RegistreFiches:
    """Le registre tel que GitHub le montre, sans rien y écrire.

    Chaque Issue du dépôt qui porte un identifiant d'offre devient une
    entrée ; une Issue absente du tableau est notée `hors_tableau`, pour ne
    pas reposer une carte qu'on a retirée. Le registre rendu n'est pas
    encore sauvegardé.
    """
    items = github.items_du_projet()
    registre = RegistreFiches(RegistreFiches.chemin(path))
    for identifiant, issue in github.lister_fiches().items():
        item = items.get(issue.number, "")
        registre.enregistrer(
            identifiant,
            Fiche(
                issue=issue.number,
                node_id=issue.node_id,
                item=item,
                hors_tableau=not item,
            ),
        )
    return registre
