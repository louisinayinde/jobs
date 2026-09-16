"""Tests du client GitHub Projects (US-4.1.T).

Aucun appel ne part vers GitHub : un `httpx.MockTransport` joue les réponses,
dans l'ordre, et enregistre chaque requête. Ce qui est vérifié, c'est donc
**ce que le client envoie** — méthode, chemin, en-têtes, payload — et **ce
qu'il fait de ce qu'il reçoit**, erreurs comprises.

Les réponses GraphQL reprennent la forme exacte de réponses réelles de
l'API (relevées le 2026-09-16) : un champ du mauvais type arrive en `{}`, un
Project absent en `projectV2: null` **accompagné** d'une erreur `NOT_FOUND`,
le tout sous un statut 200.

Les attentes (limites de débit, pannes) sont neutralisées et enregistrées
par la fixture `github_delays` de `conftest.py`.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Callable
from urllib.parse import unquote

import httpx
import pytest
import yaml

from src.board import __main__ as board_cli
from src.board import github
from src.board.fiche import MARQUEUR_ID, POSTE_INCONNU, corps_issue, titre_issue
from src.board.github import (
    ATTENTE_MAX,
    ATTENTE_SANS_INDICATION,
    MAX_RATE_LIMIT_RETRIES,
    MARGE_RESET,
    TITRE_MAX,
    GitHubAuthError,
    GitHubClient,
    GitHubError,
    GitHubNotFoundError,
    GitHubPermissionError,
    GitHubRateLimitError,
    Issue,
)
from src.core.config import BoardConfig, ConfigError, load_board
from src.core.normalize import Job
from src.core.scoring import ScoredJob
from src.core.secrets import SecretsError

TOKEN = "ghp_jeton-de-test-0123456789"

CONFIG = BoardConfig(
    repository="louisinayinde/jobs",
    project_owner="louisinayinde",
    project_number=3,
)

API = "https://api.github.com"

#: L'instant « maintenant » des tests de limite de débit, en secondes epoch.
MAINTENANT = 1_800_000_000.0


# ---------------------------------------------------------------------------
# Outillage
# ---------------------------------------------------------------------------


class FauxGitHub:
    """Rejoue des réponses dans l'ordre et garde chaque requête reçue."""

    def __init__(self, *reponses: httpx.Response | Callable[[httpx.Request], Any]) -> None:
        self.reponses = list(reponses)
        self.requetes: list[httpx.Request] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requetes.append(request)
        if not self.reponses:
            raise AssertionError(f"appel inattendu : {request.method} {request.url}")
        reponse = self.reponses.pop(0)
        return reponse(request) if callable(reponse) else reponse

    def client(self, config: BoardConfig = CONFIG) -> GitHubClient:
        http = httpx.Client(transport=httpx.MockTransport(self))
        return GitHubClient(TOKEN, config, client=http)

    def payload(self, index: int) -> dict[str, Any]:
        return json.loads(self.requetes[index].content)

    @property
    def appels(self) -> list[str]:
        return [f"{r.method} {r.url.path}" for r in self.requetes]


def ok(data: Any, **headers: str) -> httpx.Response:
    return httpx.Response(200, json=data, headers=headers)


def gql(data: Any) -> httpx.Response:
    return httpx.Response(200, json={"data": data})


def depot(has_issues: bool = True, scopes: str | None = "repo, project") -> httpx.Response:
    headers = {"X-OAuth-Scopes": scopes} if scopes is not None else {}
    return httpx.Response(
        200,
        json={"full_name": "louisinayinde/jobs", "has_issues": has_issues},
        headers=headers,
    )


def issue_creee(number: int = 42, titre: str = "GitLab — Backend Engineer") -> httpx.Response:
    return httpx.Response(
        201,
        json={
            "number": number,
            "node_id": f"I_kwDOUIzwRc{number}",
            "html_url": f"https://github.com/louisinayinde/jobs/issues/{number}",
            "title": titre,
        },
    )


OPTIONS = [
    {"id": "f75ad846", "name": "Nouveau"},
    {"id": "47fc9ee4", "name": "En génération"},
    {"id": "98236657", "name": "CV prêt"},
    {"id": "0c1d2e3f", "name": "Ignoré"},
]


def projet(options: list[dict[str, str]] = OPTIONS, champ: dict | None = None) -> httpx.Response:
    field = champ if champ is not None else {
        "id": "PVTSSF_lAHOBKl0zs4ADeIfzgB_0qY",
        "name": "Status",
        "options": options,
    }
    return gql(
        {
            "repositoryOwner": {
                "projectV2": {
                    "id": "PVT_kwHOBKl0zs4ADeIf",
                    "title": "JobRadar",
                    "url": "https://github.com/users/louisinayinde/projects/3",
                    "field": field,
                }
            }
        }
    )


def item_ajoute(item: str = "PVTI_lAHOBKl0zs4ADeIfzgaaaa") -> httpx.Response:
    return gql({"addProjectV2ItemById": {"item": {"id": item}}})


def statut_ecrit(item: str = "PVTI_lAHOBKl0zs4ADeIfzgaaaa") -> httpx.Response:
    return gql({"updateProjectV2ItemFieldValue": {"projectV2Item": {"id": item}}})


ISSUE = Issue(
    number=42,
    node_id="I_kwDOUIzwRc42",
    url="https://github.com/louisinayinde/jobs/issues/42",
    titre="GitLab — Backend Engineer",
)


def offre(**champs: Any) -> ScoredJob:
    valeurs = dict(
        id="4012",
        source="greenhouse",
        entreprise="GitLab",
        titre="Backend Engineer",
        localisation="London, UK",
        remote_type="hybrid",
        url="https://job-boards.greenhouse.io/gitlab/jobs/4012",
        date="2026-09-14T08:00:00+00:00",
        description="We are hiring. cc @octocat",
        tech=("go", "ruby"),
    )
    score = champs.pop("score", 60)
    categorie = champs.pop("categorie", "europe")
    detail = champs.pop("detail", "london")
    valeurs.update(champs)
    return ScoredJob(job=Job(**valeurs), score=score, categorie=categorie, detail=detail)


@pytest.fixture
def horloge(monkeypatch: pytest.MonkeyPatch) -> float:
    monkeypatch.setattr(github, "_maintenant", lambda: MAINTENANT)
    return MAINTENANT


# ---------------------------------------------------------------------------
# board.yaml
# ---------------------------------------------------------------------------


def _ecrire(tmp_path: Path, data: Any) -> Path:
    chemin = tmp_path / "board.yaml"
    chemin.write_text(yaml.safe_dump(data, allow_unicode=True), encoding="utf-8")
    return chemin


def test_board_yaml_valide_charge_chaque_valeur(tmp_path: Path) -> None:
    chemin = _ecrire(
        tmp_path,
        {
            "repository": "acme/jobs",
            "project": {"owner": "acme-org", "number": 7},
            "status": {"field": "Colonne", "initial": "À trier"},
        },
    )

    config = load_board(chemin)

    assert config == BoardConfig(
        repository="acme/jobs",
        project_owner="acme-org",
        project_number=7,
        status_field="Colonne",
        status_initial="À trier",
    )
    assert (config.owner, config.repo) == ("acme", "jobs")


def test_board_yaml_sans_status_prend_status_et_nouveau(tmp_path: Path) -> None:
    chemin = _ecrire(tmp_path, {"repository": "a/b", "project": {"owner": "a", "number": 1}})

    config = load_board(chemin)

    assert (config.status_field, config.status_initial) == ("Status", "Nouveau")


def test_numero_de_project_vide_dit_quoi_faire(tmp_path: Path) -> None:
    chemin = _ecrire(tmp_path, {"repository": "a/b", "project": {"owner": "a", "number": None}})

    with pytest.raises(ConfigError) as excinfo:
        load_board(chemin)

    assert "number" in str(excinfo.value)
    assert "projects/<numéro>" in str(excinfo.value)


@pytest.mark.parametrize("number", ["3", 0, -1, True, 2.5])
def test_numero_de_project_invalide_refuse_sans_cast(tmp_path: Path, number: Any) -> None:
    chemin = _ecrire(tmp_path, {"repository": "a/b", "project": {"owner": "a", "number": number}})

    with pytest.raises(ConfigError) as excinfo:
        load_board(chemin)

    assert "number" in str(excinfo.value)


@pytest.mark.parametrize("repository", ["jobs", "a/b/c", "https://github.com/a/b", ""])
def test_repository_mal_forme_refuse(tmp_path: Path, repository: str) -> None:
    chemin = _ecrire(tmp_path, {"repository": repository, "project": {"owner": "a", "number": 1}})

    with pytest.raises(ConfigError) as excinfo:
        load_board(chemin)

    assert "repository" in str(excinfo.value)


def test_owner_du_project_manquant_nomme_la_cle(tmp_path: Path) -> None:
    chemin = _ecrire(tmp_path, {"repository": "a/b", "project": {"number": 1}})

    with pytest.raises(ConfigError) as excinfo:
        load_board(chemin)

    assert "owner" in str(excinfo.value)


def test_board_yaml_versionne_vise_le_depot_du_projet() -> None:
    """Le fichier du dépôt est lisible ; seul le numéro peut rester à remplir."""
    brut = yaml.safe_load(Path("config/board.yaml").read_text(encoding="utf-8"))
    assert brut["repository"] == "louisinayinde/jobs"
    assert brut["project"]["owner"] == "louisinayinde"
    assert brut["status"] == {"field": "Status", "initial": "Nouveau"}

    try:
        config = load_board("config/board.yaml")
    except ConfigError as exc:
        assert "« number »" in str(exc)
    else:
        assert config.project_number > 0


# ---------------------------------------------------------------------------
# US-4.1.1 — authentification
# ---------------------------------------------------------------------------


def test_github_token_absent_echoue_au_demarrage() -> None:
    with pytest.raises(SecretsError) as excinfo:
        GitHubClient.from_env(CONFIG, env={})

    assert "GITHUB_TOKEN" in str(excinfo.value)


def test_jeton_vide_refuse_a_la_construction() -> None:
    with pytest.raises(GitHubAuthError):
        GitHubClient("", CONFIG)


def test_chaque_requete_porte_le_jeton_et_la_version_d_api() -> None:
    faux = FauxGitHub(depot(), issue_creee())
    client = GitHubClient.from_env(
        CONFIG,
        env={"GITHUB_TOKEN": TOKEN},
        client=httpx.Client(transport=httpx.MockTransport(faux)),
    )

    client.verifier_acces()
    client.creer_issue("t", "c")

    for requete in faux.requetes:
        assert requete.headers["Authorization"] == f"Bearer {TOKEN}"
        assert requete.headers["X-GitHub-Api-Version"] == "2022-11-28"
        assert requete.headers["Accept"] == "application/vnd.github+json"
        assert requete.headers["User-Agent"].startswith("JobRadar/")
        assert str(requete.url).startswith(API)


def test_verifier_acces_lit_le_depot_et_rend_les_scopes() -> None:
    faux = FauxGitHub(depot(scopes="repo, project, workflow"))

    acces = faux.client().verifier_acces()

    assert faux.appels == ["GET /repos/louisinayinde/jobs"]
    assert acces.repository == "louisinayinde/jobs"
    assert acces.scopes == ("repo", "project", "workflow")


def test_verifier_acces_jeton_sans_scopes_annonces() -> None:
    """Jeton fine-grained ou d'installation : GitHub ne dit rien, pas d'erreur."""
    acces = FauxGitHub(depot(scopes=None)).client().verifier_acces()

    assert acces.scopes is None


def test_issues_desactivees_refusees_avec_le_reglage_a_changer() -> None:
    with pytest.raises(GitHubError) as excinfo:
        FauxGitHub(depot(has_issues=False)).client().verifier_acces()

    assert "Issues sont désactivées" in str(excinfo.value)
    assert "Settings" in str(excinfo.value)


def test_reponse_401_leve_une_erreur_explicite_sans_retry() -> None:
    faux = FauxGitHub(httpx.Response(401, json={"message": "Bad credentials"}))

    with pytest.raises(GitHubAuthError) as excinfo:
        faux.client().verifier_acces()

    message = str(excinfo.value)
    assert "401" in message
    assert "GITHUB_TOKEN" in message
    assert "Bad credentials" in message
    assert TOKEN not in message
    assert len(faux.requetes) == 1


def test_401_sur_creation_d_issue_leve_aussi() -> None:
    faux = FauxGitHub(httpx.Response(401, json={"message": "Bad credentials"}))

    with pytest.raises(GitHubAuthError):
        faux.client().creer_issue("t", "c")


def test_depot_invisible_404_leve_not_found() -> None:
    faux = FauxGitHub(httpx.Response(404, json={"message": "Not Found"}))

    with pytest.raises(GitHubNotFoundError) as excinfo:
        faux.client().verifier_acces()

    assert "/repos/louisinayinde/jobs" in str(excinfo.value)
    assert len(faux.requetes) == 1


def test_403_de_droits_n_est_pas_pris_pour_une_limite_de_debit(github_delays: list[float]) -> None:
    faux = FauxGitHub(
        httpx.Response(
            403,
            json={"message": "Resource not accessible by personal access token"},
            headers={
                "x-ratelimit-remaining": "4999",
                "x-accepted-github-permissions": "issues=write",
            },
        )
    )

    with pytest.raises(GitHubPermissionError) as excinfo:
        faux.client().creer_issue("t", "c")

    assert "Resource not accessible" in str(excinfo.value)
    assert "issues=write" in str(excinfo.value)
    assert len(faux.requetes) == 1
    assert github_delays == []


def test_repr_ne_devoile_jamais_le_jeton() -> None:
    assert TOKEN not in repr(GitHubClient(TOKEN, CONFIG))


# ---------------------------------------------------------------------------
# US-4.1.2 — Issue
# ---------------------------------------------------------------------------


def test_creation_d_issue_envoie_titre_et_corps_attendus() -> None:
    faux = FauxGitHub(issue_creee(number=42))
    corps = "**Entreprise** : GitLab  \n"

    issue = faux.client().creer_issue("GitLab — Backend Engineer", corps)

    assert faux.appels == ["POST /repos/louisinayinde/jobs/issues"]
    assert faux.payload(0) == {"title": "GitLab — Backend Engineer", "body": corps}
    assert issue == Issue(
        number=42,
        node_id="I_kwDOUIzwRc42",
        url="https://github.com/louisinayinde/jobs/issues/42",
        titre="GitLab — Backend Engineer",
    )


def test_issue_depuis_une_offre_titre_entreprise_poste_et_fiche_en_corps() -> None:
    faux = FauxGitHub(issue_creee())
    une_offre = offre()

    faux.client().creer_issue_offre(une_offre)

    assert faux.payload(0) == {
        "title": "GitLab — Backend Engineer",
        "body": (
            "**Entreprise** : GitLab  \n"
            "**Poste** : Backend Engineer  \n"
            "**Localisation** : London, UK · hybride  \n"
            "**Score** : 60 — europe (london)  \n"
            "**Tech** : go, ruby  \n"
            "**Lien ATS** : [job-boards.greenhouse.io]"
            "(<https://job-boards.greenhouse.io/gitlab/jobs/4012>)  \n"
            "**Publiée le** : 2026-09-14  \n"
            "**Source** : greenhouse  \n"
            "\n"
            "---\n"
            "\n"
            "**Description**\n"
            "\n"
            "We are hiring. cc @\u2060octocat\n"
            "\n"
            "<!-- jobradar:id=greenhouse:gitlab:4012 -->\n"
        ),
    }


def test_titre_trop_long_borne_a_la_limite_de_github() -> None:
    faux = FauxGitHub(issue_creee())

    faux.client().creer_issue("x" * 1000, "c")

    titre = faux.payload(0)["title"]
    assert len(titre) == TITRE_MAX
    assert titre.endswith("…")


def test_titre_multiligne_ramene_sur_une_ligne() -> None:
    faux = FauxGitHub(issue_creee())

    faux.client().creer_issue("GitLab —\n  Backend\tEngineer", "c")

    assert faux.payload(0)["title"] == "GitLab — Backend Engineer"


def test_titre_sans_entreprise_ni_poste() -> None:
    assert titre_issue(offre(entreprise="")) == "Backend Engineer"
    assert titre_issue(offre(titre="  ")) == f"GitLab — {POSTE_INCONNU}"


def test_fiche_sans_champs_optionnels_ni_trou_ni_none() -> None:
    corps = corps_issue(
        offre(tech=(), date="", localisation="", remote_type="unknown", detail="")
    )

    assert "None" not in corps
    assert "**Tech**" not in corps
    assert "**Publiée le**" not in corps
    assert "**Localisation**" not in corps
    assert "**Score** : 60 — europe  " in corps
    assert " :  " not in corps


def test_fiche_ne_reprend_pas_la_description() -> None:
    """Ni pavé de texte, ni @mention qui notifierait un inconnu."""
    assert "@octocat" not in corps_issue(offre())


def test_lien_non_http_jamais_rendu_cliquable() -> None:
    corps = corps_issue(offre(url="javascript:alert(1)"))

    assert "](" not in corps
    assert "**Lien ATS** : javascript:alert(1)  " in corps


def test_identifiant_de_la_fiche_ne_ferme_pas_le_commentaire_et_se_relit() -> None:
    corps = corps_issue(offre(id="a-->b--c"))
    commentaire = corps.rstrip("\n").splitlines()[-1]

    assert commentaire.startswith(f"<!-- {MARQUEUR_ID}") and commentaire.endswith(" -->")
    interieur = commentaire[len(f"<!-- {MARQUEUR_ID}") : -len(" -->")]
    assert "--" not in interieur and ">" not in interieur
    assert unquote(interieur) == "greenhouse:gitlab:a-->b--c"


def test_422_de_validation_remonte_le_message_de_github() -> None:
    faux = FauxGitHub(httpx.Response(422, json={"message": "Validation Failed"}))

    with pytest.raises(GitHubError) as excinfo:
        faux.client().creer_issue("t", "c")

    assert "422" in str(excinfo.value)
    assert "Validation Failed" in str(excinfo.value)


def test_creation_d_issue_en_502_n_est_pas_rejouee(github_delays: list[float]) -> None:
    """La requête a pu aboutir : la rejouer risquerait une fiche en double."""
    faux = FauxGitHub(httpx.Response(502), issue_creee())

    with pytest.raises(GitHubError) as excinfo:
        faux.client().creer_issue("t", "c")

    assert len(faux.requetes) == 1
    assert "a pu aboutir" in str(excinfo.value)
    assert github_delays == []


def test_creation_d_issue_en_timeout_n_est_pas_rejouee() -> None:
    def timeout(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("lecture trop longue", request=request)

    faux = FauxGitHub(timeout, issue_creee())

    with pytest.raises(GitHubError) as excinfo:
        faux.client().creer_issue("t", "c")

    assert len(faux.requetes) == 1
    assert "a pu aboutir" in str(excinfo.value)


def test_lecture_en_502_rejouee_une_fois(github_delays: list[float]) -> None:
    faux = FauxGitHub(httpx.Response(502), depot())

    faux.client().verifier_acces()

    assert len(faux.requetes) == 2
    assert github_delays == [2.0]


def test_lecture_en_panne_persistante_finit_en_erreur() -> None:
    faux = FauxGitHub(httpx.Response(503), httpx.Response(503))

    with pytest.raises(GitHubError) as excinfo:
        faux.client().verifier_acces()

    assert "503" in str(excinfo.value)
    assert len(faux.requetes) == 2


# ---------------------------------------------------------------------------
# US-4.1.3 — Project + statut
# ---------------------------------------------------------------------------


def test_ajout_au_project_positionne_le_statut_nouveau() -> None:
    faux = FauxGitHub(projet(), item_ajoute("PVTI_carte42"), statut_ecrit("PVTI_carte42"))

    item = faux.client().ajouter_au_projet(ISSUE)

    assert faux.appels == ["POST /graphql"] * 3
    lecture, ajout, statut = (faux.payload(i) for i in range(3))

    assert lecture["variables"] == {"owner": "louisinayinde", "number": 3, "champ": "Status"}
    assert ajout["variables"] == {"projet": "PVT_kwHOBKl0zs4ADeIf", "contenu": "I_kwDOUIzwRc42"}
    assert "addProjectV2ItemById" in ajout["query"]
    assert statut["variables"] == {
        "projet": "PVT_kwHOBKl0zs4ADeIf",
        "item": "PVTI_carte42",
        "champ": "PVTSSF_lAHOBKl0zs4ADeIfzgB_0qY",
        # L'identifiant de l'option « Nouveau » dans `OPTIONS`.
        "option": "f75ad846",
    }
    assert "updateProjectV2ItemFieldValue" in statut["query"]
    assert item.id == "PVTI_carte42"
    assert item.statut == "Nouveau"


def test_statut_initial_suit_board_yaml() -> None:
    config = BoardConfig("louisinayinde/jobs", "louisinayinde", 3, status_initial="Ignoré")
    faux = FauxGitHub(projet(), item_ajoute(), statut_ecrit())

    item = faux.client(config).ajouter_au_projet(ISSUE)

    assert faux.payload(2)["variables"]["option"] == "0c1d2e3f"
    assert item.statut == "Ignoré"


def test_statut_explicite_prime_sur_la_configuration() -> None:
    faux = FauxGitHub(projet(), item_ajoute(), statut_ecrit())

    faux.client().ajouter_au_projet(ISSUE, statut="CV prêt")

    assert faux.payload(2)["variables"]["option"] == "98236657"


def test_option_trouvee_sans_egard_a_la_casse() -> None:
    faux = FauxGitHub(projet(options=[{"id": "abc", "name": " nouveau "}]), item_ajoute(), statut_ecrit())

    faux.client().ajouter_au_projet(ISSUE)

    assert faux.payload(2)["variables"]["option"] == "abc"


def test_project_lu_une_seule_fois_pour_plusieurs_fiches() -> None:
    faux = FauxGitHub(
        projet(), item_ajoute("A"), statut_ecrit("A"), item_ajoute("B"), statut_ecrit("B")
    )
    client = faux.client()

    client.ajouter_au_projet(ISSUE)
    client.ajouter_au_projet(ISSUE)

    requetes = [faux.payload(i)["query"] for i in range(5)]
    assert sum("projectV2(number" in q for q in requetes) == 1


def test_option_absente_rien_n_est_ajoute_au_tableau() -> None:
    """Le Project par défaut de GitHub n'a que Todo / In Progress / Done."""
    defaut = [
        {"id": "f75ad846", "name": "Todo"},
        {"id": "47fc9ee4", "name": "In Progress"},
        {"id": "98236657", "name": "Done"},
    ]
    faux = FauxGitHub(projet(options=defaut))

    with pytest.raises(GitHubNotFoundError) as excinfo:
        faux.client().ajouter_au_projet(ISSUE)

    message = str(excinfo.value)
    assert "« Nouveau »" in message
    assert "« Todo », « In Progress », « Done »" in message
    assert len(faux.requetes) == 1


def test_project_introuvable_nomme_le_numero_et_le_proprietaire() -> None:
    faux = FauxGitHub(
        httpx.Response(
            200,
            json={
                "data": {"repositoryOwner": {"projectV2": None}},
                "errors": [
                    {
                        "type": "NOT_FOUND",
                        "path": ["repositoryOwner", "projectV2"],
                        "message": "Could not resolve to a ProjectV2 with the number 3.",
                    }
                ],
            },
        )
    )

    with pytest.raises(GitHubNotFoundError) as excinfo:
        faux.client().ajouter_au_projet(ISSUE)

    assert "Project n°3" in str(excinfo.value)
    assert "louisinayinde" in str(excinfo.value)


def test_proprietaire_inconnu_leve_not_found() -> None:
    faux = FauxGitHub(gql({"repositoryOwner": None}))

    with pytest.raises(GitHubNotFoundError):
        faux.client().projet()


def test_project_introuvable_signale_le_scope_project_manquant() -> None:
    faux = FauxGitHub(depot(scopes="repo, workflow"), gql({"repositoryOwner": {"projectV2": None}}))
    client = faux.client()
    client.verifier_acces()

    with pytest.raises(GitHubNotFoundError) as excinfo:
        client.projet()

    assert "scope « project »" in str(excinfo.value)


def test_champ_de_statut_absent_ou_d_un_autre_type() -> None:
    faux = FauxGitHub(projet(champ={}))

    with pytest.raises(GitHubNotFoundError) as excinfo:
        faux.client().ajouter_au_projet(ISSUE)

    assert "« Status »" in str(excinfo.value)
    assert "status.field" in str(excinfo.value)


def test_erreur_graphql_en_200_n_est_pas_un_succes() -> None:
    faux = FauxGitHub(
        projet(),
        httpx.Response(
            200,
            json={"errors": [{"type": "UNPROCESSABLE", "message": "Content already archived"}]},
        ),
    )

    with pytest.raises(GitHubError) as excinfo:
        faux.client().ajouter_au_projet(ISSUE)

    assert "Content already archived" in str(excinfo.value)


def test_erreur_graphql_de_droits_leve_permission() -> None:
    faux = FauxGitHub(
        httpx.Response(
            200,
            json={"errors": [{"type": "INSUFFICIENT_SCOPES", "message": "needs read:project"}]},
        )
    )

    with pytest.raises(GitHubPermissionError):
        faux.client().projet()


# ---------------------------------------------------------------------------
# Limites de débit
# ---------------------------------------------------------------------------


def test_rate_limit_403_avec_reset_attend_puis_rejoue(
    horloge: float, github_delays: list[float]
) -> None:
    limite = httpx.Response(
        403,
        json={"message": "API rate limit exceeded for user ID 1."},
        headers={"x-ratelimit-remaining": "0", "x-ratelimit-reset": str(int(horloge) + 42)},
    )
    faux = FauxGitHub(limite, issue_creee(number=7))

    issue = faux.client().creer_issue("t", "c")

    assert github_delays == [42 + MARGE_RESET]
    assert len(faux.requetes) == 2
    assert faux.payload(0) == faux.payload(1)
    assert issue.number == 7


def test_rate_limit_reset_deja_passe_attend_la_seule_marge(
    horloge: float, github_delays: list[float]
) -> None:
    limite = httpx.Response(
        403, headers={"x-ratelimit-remaining": "0", "x-ratelimit-reset": str(int(horloge) - 5)}
    )
    faux = FauxGitHub(limite, depot())

    faux.client().verifier_acces()

    assert github_delays == [MARGE_RESET]


def test_limite_secondaire_retry_after(github_delays: list[float]) -> None:
    faux = FauxGitHub(
        httpx.Response(403, json={"message": "You have exceeded a secondary rate limit"},
                       headers={"retry-after": "30"}),
        issue_creee(),
    )

    faux.client().creer_issue("t", "c")

    assert github_delays == [30.0]
    assert len(faux.requetes) == 2


def test_limite_secondaire_sans_indication_attend_une_minute(github_delays: list[float]) -> None:
    faux = FauxGitHub(
        httpx.Response(403, json={"message": "You have exceeded a secondary rate limit."}),
        issue_creee(),
    )

    faux.client().creer_issue("t", "c")

    assert github_delays == [ATTENTE_SANS_INDICATION]


def test_429_sans_en_tete_attend_une_minute(github_delays: list[float]) -> None:
    faux = FauxGitHub(httpx.Response(429), depot())

    faux.client().verifier_acces()

    assert github_delays == [ATTENTE_SANS_INDICATION]


def test_rate_limit_graphql_servi_en_200_attend_puis_rejoue(
    horloge: float, github_delays: list[float]
) -> None:
    limite = httpx.Response(
        200,
        json={"errors": [{"type": "RATE_LIMITED", "message": "API rate limit exceeded"}]},
        headers={"x-ratelimit-remaining": "0", "x-ratelimit-reset": str(int(horloge) + 10)},
    )
    faux = FauxGitHub(limite, projet())

    assert faux.client().projet().titre == "JobRadar"
    assert github_delays == [10 + MARGE_RESET]


def test_rate_limit_persistant_finit_en_erreur_nommee(
    horloge: float, github_delays: list[float]
) -> None:
    def limite() -> httpx.Response:
        return httpx.Response(
            403, headers={"x-ratelimit-remaining": "0", "x-ratelimit-reset": str(int(horloge) + 5)}
        )

    faux = FauxGitHub(*[limite() for _ in range(MAX_RATE_LIMIT_RETRIES + 1)])

    with pytest.raises(GitHubRateLimitError):
        faux.client().creer_issue("t", "c")

    assert len(faux.requetes) == MAX_RATE_LIMIT_RETRIES + 1
    assert len(github_delays) == MAX_RATE_LIMIT_RETRIES


def test_reset_trop_lointain_echoue_sans_bloquer_le_runner(
    horloge: float, github_delays: list[float]
) -> None:
    reset = int(horloge + ATTENTE_MAX + 600)
    faux = FauxGitHub(
        httpx.Response(403, headers={"x-ratelimit-remaining": "0", "x-ratelimit-reset": str(reset)})
    )

    with pytest.raises(GitHubRateLimitError) as excinfo:
        faux.client().creer_issue("t", "c")

    assert github_delays == []
    assert len(faux.requetes) == 1
    assert "UTC" in str(excinfo.value)


# ---------------------------------------------------------------------------
# python -m src.board check
# ---------------------------------------------------------------------------


@pytest.fixture
def board_yaml(tmp_path: Path) -> str:
    return str(
        _ecrire(
            tmp_path,
            {"repository": "louisinayinde/jobs", "project": {"owner": "louisinayinde", "number": 3}},
        )
    )


def _check(board_yaml: str, faux: FauxGitHub, env: dict[str, str] | None = None) -> int:
    return board_cli.main(
        ["check", "--board", board_yaml],
        env={"GITHUB_TOKEN": TOKEN} if env is None else env,
        client=httpx.Client(transport=httpx.MockTransport(faux)),
    )


def test_check_mise_en_place_complete(board_yaml: str, capsys: pytest.CaptureFixture[str]) -> None:
    faux = FauxGitHub(depot(), projet())

    assert _check(board_yaml, faux) == 0

    sortie = capsys.readouterr().out
    assert "jeton accepté" in sortie
    assert "Project « JobRadar »" in sortie
    assert "prêt à publier" in sortie
    # Lecture seule : aucune mutation.
    assert all("mutation" not in r.content.decode() for r in faux.requetes)


def test_check_sans_option_nouveau(board_yaml: str, capsys: pytest.CaptureFixture[str]) -> None:
    faux = FauxGitHub(depot(), projet(options=[{"id": "x", "name": "Todo"}]))

    assert _check(board_yaml, faux) == 1
    assert "sans option « Nouveau »" in capsys.readouterr().err


def test_check_jeton_refuse(board_yaml: str, capsys: pytest.CaptureFixture[str]) -> None:
    faux = FauxGitHub(httpx.Response(401, json={"message": "Bad credentials"}))

    assert _check(board_yaml, faux) == 1
    assert "GitHubAuthError" in capsys.readouterr().err


def test_check_sans_jeton(board_yaml: str, capsys: pytest.CaptureFixture[str]) -> None:
    assert _check(board_yaml, FauxGitHub(), env={}) == 1
    assert "GITHUB_TOKEN" in capsys.readouterr().err


def test_check_numero_non_renseigne(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    chemin = _ecrire(tmp_path, {"repository": "a/b", "project": {"owner": "a", "number": None}})

    assert _check(str(chemin), FauxGitHub()) == 1
    assert "number" in capsys.readouterr().err
