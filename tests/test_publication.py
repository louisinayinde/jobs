"""Tests de la publication d'un lot d'offres (US-4.3.T).

Trois moitiés :

- **le lot** (US-4.3.1) — ce que GitHub reçoit quand on lui confie les
  offres nouvelles d'un run : l'ordre du score, aucune création pour une
  offre déjà publiée, un bilan qui compte ce qui est réellement arrivé sur
  le tableau, et ce qui se passe quand GitHub refuse ou tombe en panne ;
- **le run** — la publication branchée dans `collect.main` : les fiches
  créées dans l'ordre du classement affiché, et surtout la mémoire
  (`seen.json`) qui ne retient que ce qui a été traité ;
- **les workflows** — le jeton qui ouvre le tableau, et l'état commité même
  quand le run échoue.

Aucun appel ne part vers GitHub. `FauxTableau` joue un GitHub qui tient un
vrai tableau — il crée les Issues qu'on lui demande, pose les cartes, et lève
sur tout le reste — là où `FauxGitHub` (test_board_github) rejoue une liste
figée : il faut ici publier des lots dont la taille n'est pas écrite d'avance.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Callable

import httpx
import pytest
import yaml

from src import collect
from src.board.fiche import lire_id
from src.board.github import GitHubClient
from src.board.publication import (
    MAX_PUBLICATIONS,
    PAUSE_ENTRE_FICHES,
    BilanPublication,
    publier_lot,
)
from src.board.registre import Fiche, RegistreFiches
from src.core.dedup import stable_id
from src.core.scoring import ScoredJob
from src.core.secrets import SecretsError
from tests.test_board_github import (
    CONFIG,
    OPTIONS,
    TOKEN,
    FauxGitHub,
    issue_creee,
    item_ajoute,
    projet,
    statut_ecrit,
)
from tests.test_scoring import SCORER, job

CHEMIN_ISSUES = "/repos/louisinayinde/jobs/issues"

#: Capturé à l'import, avant que le conftest ne le remplace pour chaque test.
OUVRIR_GITHUB_REEL = collect.ouvrir_github


# ---------------------------------------------------------------------------
# Outillage
# ---------------------------------------------------------------------------


class FauxTableau:
    """Un GitHub qui tient un tableau, et garde chaque requête reçue.

    `pannes` associe le rang d'une **tentative** de création d'Issue (à
    partir de 1) à la réponse à rendre à sa place : une Issue n'est alors
    pas créée.
    """

    def __init__(
        self,
        *,
        pannes: dict[int, httpx.Response | Callable[[httpx.Request], httpx.Response]]
        | None = None,
        options: list[dict[str, str]] = OPTIONS,
        premier_numero: int = 1,
    ) -> None:
        self.pannes = dict(pannes or {})
        self.options = options
        self.premier_numero = premier_numero
        self.requetes: list[httpx.Request] = []
        self.appels: list[str] = []
        #: Les Issues créées, dans l'ordre : numéro, titre, corps.
        self.issues: list[dict[str, Any]] = []
        #: `node_id` des Issues posées sur le tableau, dans l'ordre.
        self.cartes: list[str] = []
        self.tentatives = 0

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requetes.append(request)
        chemin = request.url.path
        if request.method == "POST" and chemin == CHEMIN_ISSUES:
            self.appels.append("issue")
            self.tentatives += 1
            panne = self.pannes.get(self.tentatives)
            if panne is not None:
                return panne(request) if callable(panne) else panne
            corps = json.loads(request.content)
            numero = self.premier_numero + len(self.issues)
            self.issues.append({"number": numero, "title": corps["title"], "body": corps["body"]})
            return issue_creee(numero, corps["title"])

        if request.method == "POST" and chemin == "/graphql":
            corps = json.loads(request.content)
            requete, variables = corps["query"], corps["variables"]
            if "repositoryOwner" in requete:
                self.appels.append("projet")
                return projet(self.options)
            if "addProjectV2ItemById" in requete:
                self.appels.append("carte")
                self.cartes.append(variables["contenu"])
                return item_ajoute(f"PVTI_{variables['contenu']}")
            if "updateProjectV2ItemFieldValue" in requete:
                self.appels.append("statut")
                return statut_ecrit(variables["item"])

        raise AssertionError(f"appel inattendu : {request.method} {request.url}")

    def client(self) -> GitHubClient:
        return GitHubClient(TOKEN, CONFIG, client=httpx.Client(transport=httpx.MockTransport(self)))

    @property
    def titres(self) -> list[str]:
        return [issue["title"] for issue in self.issues]

    @property
    def identifiants(self) -> list[str | None]:
        """L'identifiant d'offre écrit dans chaque Issue créée, dans l'ordre."""
        return [lire_id(issue["body"]) for issue in self.issues]


def lot(*localisations: tuple[str, str]) -> list[ScoredJob]:
    """Des offres classées par le vrai scorer, une par `(localisation, mode)`.

    Chacune a un identifiant et un titre qui disent d'où elle vient, pour
    lire l'ordre des créations sans ambiguïté.
    """
    jobs = [
        job(localisation, remote, id=f"offre-{rang}", titre=f"Poste {localisation}")
        for rang, (localisation, remote) in enumerate(localisations, start=1)
    ]
    return SCORER.classer(jobs).offres


def fiche_complete(numero: int) -> Fiche:
    return Fiche(issue=numero, node_id=f"I_{numero}", item=f"PVTI_{numero}")


def publier(tableau: FauxTableau | FauxGitHub, registre: RegistreFiches, offres, **options) -> BilanPublication:
    return publier_lot(tableau.client(), registre, offres, **options)


# ---------------------------------------------------------------------------
# US-4.3.T — le lot
# ---------------------------------------------------------------------------


def test_lot_de_3_offres_triees_3_fiches_creees_dans_l_ordre_du_score(tmp_path: Path) -> None:
    # Données dans le désordre : France, monde entier, Europe.
    offres = lot(("Paris", "onsite"), ("Worldwide", "remote"), ("Berlin, Germany", "onsite"))
    assert [(o.job.id, o.score) for o in offres] == [
        ("offre-2", 100),
        ("offre-3", 60),
        ("offre-1", 40),
    ]
    tableau = FauxTableau()

    bilan = publier(tableau, RegistreFiches(tmp_path / "fiches.json"), offres)

    assert tableau.titres == [
        "GitLab — Poste Worldwide",
        "GitLab — Poste Berlin, Germany",
        "GitLab — Poste Paris",
    ]
    assert tableau.identifiants == [
        "greenhouse:gitlab:offre-2",
        "greenhouse:gitlab:offre-3",
        "greenhouse:gitlab:offre-1",
    ]
    # L'ordre complet des appels : le tableau lu une fois, avant la première
    # Issue, puis chaque fiche entière — Issue, carte, statut — avant la suivante.
    assert tableau.appels == ["projet"] + ["issue", "carte", "statut"] * 3
    assert tableau.cartes == ["I_kwDOUIzwRc1", "I_kwDOUIzwRc2", "I_kwDOUIzwRc3"]
    assert [p.issue for p in bilan.publiees] == [1, 2, 3]


def test_lot_contenant_une_offre_deja_publiee_2_creations_seulement(tmp_path: Path) -> None:
    offres = lot(("Worldwide", "remote"), ("Berlin, Germany", "onsite"), ("Paris", "onsite"))
    registre = RegistreFiches(tmp_path / "fiches.json")
    registre.enregistrer(stable_id(offres[1].job), fiche_complete(7))
    tableau = FauxTableau(premier_numero=10)

    bilan = publier(tableau, registre, offres)

    assert tableau.appels.count("issue") == 2
    assert tableau.titres == ["GitLab — Poste Worldwide", "GitLab — Poste Paris"]
    assert tableau.appels == ["projet"] + ["issue", "carte", "statut"] * 2
    assert [(p.issue, p.creee) for p in bilan.publications] == [(10, True), (7, False), (11, True)]
    assert {cle: f["issue"] for cle, f in RegistreFiches.load(registre.path).as_dict().items()} == {
        "greenhouse:gitlab:offre-1": 10,
        "greenhouse:gitlab:offre-2": 7,
        "greenhouse:gitlab:offre-3": 11,
    }


def test_le_resume_retourne_egale_le_nombre_reellement_publie(tmp_path: Path) -> None:
    """Trois offres, trois situations : inconnue (créée), déjà sur le tableau
    (rien), Issue d'un run précédent sans carte (carte posée). Deux fiches
    arrivent réellement sur le tableau — autant que de cartes posées."""
    offres = lot(("Worldwide", "remote"), ("Berlin, Germany", "onsite"), ("Paris", "onsite"))
    registre = RegistreFiches(tmp_path / "fiches.json")
    registre.enregistrer(stable_id(offres[1].job), fiche_complete(7))
    registre.enregistrer(stable_id(offres[2].job), Fiche(issue=8, node_id="I_8"))
    tableau = FauxTableau(premier_numero=10)

    bilan = publier(tableau, registre, offres)

    assert len(bilan.publiees) == len(tableau.cartes) == 2
    assert tableau.appels.count("issue") == 1
    assert len(bilan.deja_publiees) == 1
    assert bilan.resume == "2 fiche(s) publiée(s) sur 3 offre(s), 1 déjà sur le tableau"
    assert bilan.en_echec is False


def test_le_resume_compte_juste_meme_quand_le_lot_s_interrompt(tmp_path: Path) -> None:
    offres = lot(("Worldwide", "remote"), ("Berlin, Germany", "onsite"), ("Paris", "onsite"))
    tableau = FauxTableau(pannes={2: httpx.Response(401, json={"message": "Bad credentials"})})

    bilan = publier(tableau, RegistreFiches(tmp_path / "fiches.json"), offres)

    assert len(bilan.publiees) == len(tableau.issues) == len(tableau.cartes) == 1
    assert bilan.resume.startswith(
        "1 fiche(s) publiée(s) sur 3 offre(s), 2 reportée(s) au run suivant — INTERROMPUE : "
        "GitHubAuthError"
    )


def test_lot_vide_zero_appel_resume_zero_sans_erreur(tmp_path: Path) -> None:
    faux = FauxGitHub()  # lève sur tout appel
    registre = RegistreFiches(tmp_path / "fiches.json")

    bilan = publier(faux, registre, [])

    assert faux.requetes == []
    assert len(bilan.publiees) == 0
    assert bilan.resume == "0 fiche(s) publiée(s) sur 0 offre(s)"
    assert (bilan.erreur, bilan.en_echec) == ("", False)
    assert not registre.path.exists()


def test_lot_dont_toutes_les_offres_sont_deja_publiees_zero_appel(tmp_path: Path) -> None:
    """Pas même la lecture du tableau : elle n'a lieu qu'avant une première écriture."""
    offres = lot(("Worldwide", "remote"), ("Paris", "onsite"))
    registre = RegistreFiches(tmp_path / "fiches.json")
    for rang, offre in enumerate(offres, start=1):
        registre.enregistrer(stable_id(offre.job), fiche_complete(rang))
    faux = FauxGitHub()

    bilan = publier(faux, registre, offres)

    assert faux.requetes == []
    assert (len(bilan.publiees), len(bilan.deja_publiees)) == (0, 2)
    assert bilan.traitees == offres


# ---------------------------------------------------------------------------
# Ajouts hors DoD — pannes, refus, plafond, pause
# ---------------------------------------------------------------------------


def test_panne_le_lot_s_arrete_et_reporte_le_reste_sans_autre_appel(tmp_path: Path) -> None:
    offres = lot(("Worldwide", "remote"), ("Berlin, Germany", "onsite"), ("Paris", "onsite"))
    tableau = FauxTableau(pannes={2: httpx.Response(401, json={"message": "Bad credentials"})})
    registre = RegistreFiches(tmp_path / "fiches.json")

    bilan = publier(tableau, registre, offres)

    # Rien après la panne : la troisième offre n'a pas été tentée.
    assert tableau.appels == ["projet", "issue", "carte", "statut", "issue"]
    assert bilan.interrompu and bilan.en_echec
    assert bilan.traitees == offres[:1]
    assert bilan.reportees == offres[1:]
    # Un refus net ne laisse rien au registre pour l'offre en échec.
    assert list(RegistreFiches.load(registre.path)) == ["greenhouse:gitlab:offre-1"]


def test_creation_incertaine_reportee_puis_adoptee_sans_doublon(tmp_path: Path) -> None:
    """Un 502 sur la création : l'offre est reportée avec son `en_cours`. Au
    run suivant, l'Issue est retrouvée sur GitHub et adoptée, pas recréée."""
    offres = lot(("Worldwide", "remote"), ("Paris", "onsite"))
    chemin = tmp_path / "fiches.json"
    premier = FauxTableau(pannes={1: httpx.Response(502, text="Bad Gateway")})

    bilan = publier(premier, RegistreFiches.load(chemin), offres)

    assert bilan.reportees == offres
    assert "en_cours" in RegistreFiches.load(chemin).as_dict()["greenhouse:gitlab:offre-1"]

    # L'Issue avait en fait été créée : GitHub la liste.
    from tests.test_fiche import issue_listee

    reprise = FauxGitHub(
        projet(),
        httpx.Response(200, json=[issue_listee(offres[0], 42)]),
        item_ajoute("PVTI_42"),
        statut_ecrit("PVTI_42"),
        issue_creee(43),
        item_ajoute("PVTI_43"),
        statut_ecrit("PVTI_43"),
    )
    bilan = publier(reprise, RegistreFiches.load(chemin), offres)

    assert reprise.appels.count(f"POST {CHEMIN_ISSUES}") == 1
    assert [(p.issue, p.creee, p.posee) for p in bilan.publications] == [
        (42, False, True),
        (43, True, True),
    ]
    assert len(bilan.publiees) == 2


def test_offre_refusee_422_signalee_non_bloquante(tmp_path: Path, caplog) -> None:
    offres = lot(("Worldwide", "remote"), ("Berlin, Germany", "onsite"), ("Paris", "onsite"))
    refus = httpx.Response(422, json={"message": "Validation Failed"})
    tableau = FauxTableau(pannes={2: refus})
    registre = RegistreFiches(tmp_path / "fiches.json")

    bilan = publier(tableau, registre, offres)

    assert tableau.titres == ["GitLab — Poste Worldwide", "GitLab — Poste Paris"]
    assert bilan.refusees == [offres[1]]
    # Traitée : mémorisée comme vue, elle ne bloquera pas les runs suivants.
    assert bilan.traitees == offres
    assert bilan.reportees == []
    assert (bilan.interrompu, bilan.en_echec) == (False, True)
    assert "1 refusée(s) par GitHub" in bilan.resume
    assert "greenhouse:gitlab:offre-2 refusée par GitHub" in caplog.text
    assert "greenhouse:gitlab:offre-2" not in RegistreFiches.load(registre.path)


def test_tableau_sans_option_nouveau_aucune_issue_creee(tmp_path: Path) -> None:
    """Sans cette vérification préalable, chaque run créerait une Issue
    orpheline avant de découvrir que sa carte ne peut pas être posée."""
    offres = lot(("Worldwide", "remote"), ("Paris", "onsite"))
    tableau = FauxTableau(options=[{"id": "a", "name": "Todo"}, {"id": "b", "name": "Done"}])
    registre = RegistreFiches(tmp_path / "fiches.json")

    bilan = publier(tableau, registre, offres)

    assert tableau.appels == ["projet"]
    assert bilan.reportees == offres
    assert "« Nouveau »" in bilan.erreur
    assert not registre.path.exists()


def test_plafond_atteint_le_reste_est_reporte_sauf_ce_qui_ne_coute_rien(tmp_path: Path) -> None:
    offres = lot(
        ("Worldwide", "remote"),
        ("Berlin, Germany", "onsite"),
        ("Tokyo, Japan", "onsite"),
        ("Paris", "onsite"),
    )
    registre = RegistreFiches(tmp_path / "fiches.json")
    # La dernière du classement est déjà publiée : elle passe, plafond ou non.
    registre.enregistrer(stable_id(offres[3].job), fiche_complete(7))
    tableau = FauxTableau()

    bilan = publier(tableau, registre, offres, max_publications=2)

    assert tableau.appels.count("issue") == 2
    assert [o.job.id for o in bilan.traitees] == [offres[0].job.id, offres[1].job.id, offres[3].job.id]
    assert bilan.reportees == [offres[2]]
    assert bilan.en_echec is False
    assert bilan.resume == (
        "2 fiche(s) publiée(s) sur 4 offre(s), 1 déjà sur le tableau, "
        "1 reportée(s) au run suivant"
    )


def test_une_pause_entre_deux_fiches_et_aucune_pour_une_offre_sans_appel(
    tmp_path: Path, publication_pauses: list[float]
) -> None:
    offres = lot(
        ("Worldwide", "remote"), ("Berlin, Germany", "onsite"), ("Paris", "onsite")
    )
    registre = RegistreFiches(tmp_path / "fiches.json")
    registre.enregistrer(stable_id(offres[1].job), fiche_complete(7))

    publier(FauxTableau(), registre, offres)

    assert publication_pauses == [PAUSE_ENTRE_FICHES]


def test_le_plafond_par_defaut_tient_sous_la_limite_horaire_de_github() -> None:
    """500 créations de contenu par heure : quatre runs par heure au plafond
    doivent rester dessous."""
    assert 4 * MAX_PUBLICATIONS < 500
    assert PAUSE_ENTRE_FICHES >= 1.0


# ---------------------------------------------------------------------------
# Le run publie : la publication est branchée, pas seulement écrite
# ---------------------------------------------------------------------------


def _run(monkeypatch, capsys, *argv: str, tableau: FauxTableau | None) -> tuple[int, str, str]:
    from tests.test_robustness import (
        ASHBY_HOST,
        GREENHOUSE_HOST,
        LEVER_HOST,
        TROIS_SOURCES,
        fixture,
        routing_client,
    )

    client = routing_client(
        {
            GREENHOUSE_HOST: (200, fixture("greenhouse_gitlab.json")),
            LEVER_HOST: (200, fixture("lever_malt.json")),
            ASHBY_HOST: (200, fixture("ashby_ramp.json")),
        }
    )
    monkeypatch.setenv("GITHUB_TOKEN", "jeton-de-test")
    monkeypatch.setattr(collect, "load_all", lambda **kwargs: list(TROIS_SOURCES))
    github = tableau.client() if tableau is not None else None
    code = collect.main(["--cadence", "fast", *argv], client=client, github=github)
    sortie = capsys.readouterr()
    return code, sortie.out, sortie.err


#: Les six offres retenues des trois fixtures ATS, dans l'ordre du classement
#: (voir test_scoring : deux européennes, puis quatre au plancher).
TITRES_CLASSES = [
    "Malt — Staff Software Engineer",
    "Ramp — Software Engineer, International",
    "GitLab — Backend Engineer, AI Engineering: Duo Chat",
    "GitLab — Backend Engineer (Ruby), AI Engineering: Agent Observability",
    "Ramp — Software Engineer, Security, Stablecoin",
    "Ramp — Software Engineer, Frontend",
]


def test_le_run_publie_les_offres_nouvelles_dans_l_ordre_du_classement(
    monkeypatch, capsys, isolated_seen_state, isolated_fiches_state
) -> None:
    tableau = FauxTableau()

    code, sortie, _ = _run(monkeypatch, capsys, tableau=tableau)

    assert code == 0
    assert tableau.titres == TITRES_CLASSES
    assert len(tableau.cartes) == 6
    assert "6 fiche(s) publiée(s) sur 6 offre(s)" in sortie
    assert "  #1 lever:malt:" in sortie
    assert "6 offre(s) mémorisée(s), 6 au total" in sortie
    # Mémoire et registre désignent exactement les mêmes offres.
    vues = json.loads(isolated_seen_state.read_text(encoding="utf-8"))
    fiches = json.loads(isolated_fiches_state.read_text(encoding="utf-8"))
    assert set(vues) == set(fiches) == set(tableau.identifiants)


def test_le_second_run_ne_publie_rien(monkeypatch, capsys) -> None:
    _run(monkeypatch, capsys, tableau=FauxTableau())

    silencieux = FauxTableau()
    code, sortie, _ = _run(monkeypatch, capsys, tableau=silencieux)

    assert code == 0
    assert silencieux.requetes == []
    assert "0 fiche(s) publiée(s) sur 0 offre(s)" in sortie


def test_seen_json_vide_mais_registre_intact_zero_doublon(
    monkeypatch, capsys, isolated_seen_state
) -> None:
    """Le scénario du branchement : `seen.json` vidé à la main. Les offres
    repassent comme neuves, et le registre empêche toute seconde fiche."""
    _run(monkeypatch, capsys, tableau=FauxTableau())
    isolated_seen_state.unlink()

    reprise = FauxTableau()
    code, sortie, _ = _run(monkeypatch, capsys, tableau=reprise)

    assert code == 0
    assert reprise.requetes == []
    assert "0 fiche(s) publiée(s) sur 6 offre(s), 6 déjà sur le tableau" in sortie
    assert len(json.loads(isolated_seen_state.read_text(encoding="utf-8"))) == 6


def test_panne_au_milieu_du_lot_le_run_echoue_et_ne_memorise_que_le_publie(
    monkeypatch, capsys, isolated_seen_state, isolated_fiches_state
) -> None:
    tableau = FauxTableau(pannes={3: httpx.Response(401, json={"message": "Bad credentials"})})

    code, sortie, erreurs = _run(monkeypatch, capsys, tableau=tableau)

    assert code == 1
    assert "ÉCHEC de publication" in erreurs
    assert "2 fiche(s) publiée(s) sur 6 offre(s), 4 reportée(s) au run suivant" in sortie
    # Écrits malgré l'échec : le workflow les commite dans tous les cas.
    vues = json.loads(isolated_seen_state.read_text(encoding="utf-8"))
    assert set(vues) == set(tableau.identifiants)
    assert len(vues) == 2

    # Le run suivant publie les quatre restantes, et elles seules.
    reprise = FauxTableau(premier_numero=3)
    code, sortie, _ = _run(monkeypatch, capsys, tableau=reprise)

    assert code == 0
    assert "4 nouvelle(s) sur 6 — 2 déjà vue(s)" in sortie
    assert reprise.titres == TITRES_CLASSES[2:]
    assert len(json.loads(isolated_fiches_state.read_text(encoding="utf-8"))) == 6


def test_plafond_du_run_les_meilleures_d_abord_le_reste_au_run_suivant(monkeypatch, capsys) -> None:
    premier = FauxTableau()
    code, sortie, _ = _run(monkeypatch, capsys, "--max-publications", "4", tableau=premier)

    assert code == 0
    assert premier.titres == TITRES_CLASSES[:4]
    assert "2 reportée(s) au run suivant" in sortie
    assert "4 offre(s) mémorisée(s)" in sortie

    second = FauxTableau(premier_numero=5)
    _run(monkeypatch, capsys, "--max-publications", "4", tableau=second)

    assert second.titres == TITRES_CLASSES[4:]


@pytest.mark.parametrize("valeur", ["0", "-3", "beaucoup"])
def test_un_plafond_non_positif_est_refuse(valeur: str) -> None:
    with pytest.raises(SystemExit):
        collect.build_parser().parse_args(["--max-publications", valeur])


def test_sans_publication_ni_github_ni_memoire(
    monkeypatch, capsys, isolated_seen_state, isolated_fiches_state
) -> None:
    """Le conftest rend GitHub injoignable : un seul appel ferait échouer le test."""
    code, sortie, _ = _run(monkeypatch, capsys, "--sans-publication", tableau=None)

    assert code == 0
    assert "6 offre(s) classée(s)" in sortie
    assert "rien n'est publié ni mémorisé" in sortie
    assert not isolated_seen_state.exists()
    assert not isolated_fiches_state.exists()


def test_sans_publication_ne_demande_pas_de_jeton(monkeypatch, capsys) -> None:
    from tests.test_robustness import TROIS_SOURCES, routing_client

    monkeypatch.delenv("GITHUB_TOKEN", raising=False)
    monkeypatch.setattr(collect, "load_all", lambda **kwargs: list(TROIS_SOURCES))

    code = collect.main(["--sans-publication"], client=routing_client({}, default=(404, "")))

    assert code == 0


def test_registre_illisible_arrete_le_run_avant_la_premiere_requete(
    monkeypatch, capsys, isolated_fiches_state
) -> None:
    isolated_fiches_state.parent.mkdir(parents=True, exist_ok=True)
    isolated_fiches_state.write_text("<<<<<<< HEAD\n{}\n", encoding="utf-8")
    monkeypatch.setenv("GITHUB_TOKEN", "jeton-de-test")

    def interdit(**kwargs):
        raise AssertionError("les sources ne doivent pas être interrogées")

    monkeypatch.setattr(collect, "load_all", interdit)

    assert collect.main([]) == 1
    assert "python -m src.board resync" in capsys.readouterr().err


def test_board_yaml_invalide_arrete_le_run_avant_la_premiere_requete(
    monkeypatch, capsys, tmp_path
) -> None:
    board = tmp_path / "board.yaml"
    board.write_text("repository: louisinayinde/jobs\nproject:\n  owner: louisinayinde\n", encoding="utf-8")
    monkeypatch.setenv("GITHUB_TOKEN", "jeton-de-test")

    def interdit(**kwargs):
        raise AssertionError("les sources ne doivent pas être interrogées")

    monkeypatch.setattr(collect, "load_all", interdit)

    assert collect.main(["--board", str(board)]) == 1
    assert "tableau de revue mal configuré" in capsys.readouterr().err


def test_le_run_ouvre_github_par_le_jeton_et_la_config_du_tableau(monkeypatch) -> None:
    """Le vrai `ouvrir_github`, que le conftest remplace partout ailleurs —
    construit sans émettre la moindre requête."""
    monkeypatch.setenv("GITHUB_TOKEN", TOKEN)

    with OUVRIR_GITHUB_REEL(CONFIG) as client:
        assert isinstance(client, GitHubClient)
        assert client.config == CONFIG

    monkeypatch.delenv("GITHUB_TOKEN")
    with pytest.raises(SecretsError):
        OUVRIR_GITHUB_REEL(CONFIG)


def test_la_suite_ne_peut_pas_joindre_github(monkeypatch) -> None:
    """Le garde-fou du conftest mord : sans lui, un test de run oublieux
    publierait sur le vrai tableau."""
    client = collect.ouvrir_github(CONFIG)

    with pytest.raises(AssertionError, match="joindre GitHub"):
        client.verifier_acces()


# ---------------------------------------------------------------------------
# Les workflows
# ---------------------------------------------------------------------------

WORKFLOWS_DE_COLLECTE = {"collect.yml": "collect", "collect-slow.yml": "collect-slow"}


def _job(nom: str, job_id: str) -> dict[str, Any]:
    doc = yaml.safe_load((Path(".github/workflows") / nom).read_text(encoding="utf-8"))
    return doc["jobs"][job_id]


@pytest.mark.parametrize("nom, job_id", WORKFLOWS_DE_COLLECTE.items())
def test_la_collecte_publie_avec_le_jeton_personnel(nom: str, job_id: str) -> None:
    """Le `GITHUB_TOKEN` d'Actions ne voit pas les Projects d'un compte
    (US-4.1.1) : c'est le secret `JOBRADAR_TOKEN` qui doit être exposé."""
    etapes = [e for e in _job(nom, job_id)["steps"] if "src.collect" in e.get("run", "")]

    assert len(etapes) == 1
    assert "--sans-publication" not in etapes[0]["run"]
    assert etapes[0]["env"]["GITHUB_TOKEN"] == "${{ secrets.JOBRADAR_TOKEN }}"


@pytest.mark.parametrize("nom, job_id", WORKFLOWS_DE_COLLECTE.items())
def test_l_etat_est_commite_meme_quand_le_run_echoue(nom: str, job_id: str) -> None:
    """Un run qui publie puis échoue a écrit des fiches : sans ce commit, le
    registre serait perdu, et la seule protection restante contre les
    doublons serait la recherche des créations `en_cours`."""
    etape = next(
        e for e in _job(nom, job_id)["steps"] if "git add state/" in e.get("run", "")
    )

    assert etape["if"] == "always()"
