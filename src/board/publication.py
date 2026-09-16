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

La publication d'un lot entier, dans l'ordre du score et avec son bilan, est
l'objet de la Feature 4.3.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

from src.board.github import GitHubClient, GitHubError, Issue
from src.board.registre import Fiche, RegistreFiches
from src.core.dedup import stable_id
from src.core.scoring import ScoredJob

logger = logging.getLogger(__name__)

#: Recul appliqué à l'instant d'une création incertaine avant de chercher
#: l'Issue : l'horloge du runner et celle de GitHub ne tombent pas pile
#: ensemble, et une Issue manquée ici serait une Issue en double.
MARGE_HORLOGE = timedelta(minutes=10)


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
