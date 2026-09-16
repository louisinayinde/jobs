"""Tests de la fiche d'offre et de l'anti-doublon (US-4.2.T).

Deux moitiés :

- **la fiche** (US-4.2.1) — ce que le candidat lit : les champs, leur ordre,
  un lien ATS vraiment cliquable, aucun trou quand un champ manque, et des
  valeurs tierces qui restent du texte (ni Markdown, ni mention, ni
  référence croisée) ;
- **l'anti-doublon** (US-4.2.2) — ce que GitHub reçoit : jamais une seconde
  Issue pour une offre que le registre connaît, y compris après un run
  interrompu entre deux étapes, et un registre reconstructible depuis
  GitHub quand il est perdu.

Comme pour le client (US-4.1.T), aucun appel ne part vers GitHub :
`FauxGitHub` rejoue des réponses dans l'ordre, et **lève sur tout appel
qu'on ne lui a pas fourni** — « 0 appel » se vérifie donc en ne lui donnant
rien.
"""

from __future__ import annotations

import json
import logging
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import httpx
import pytest

from src.board import __main__ as board_cli
from src.board.fiche import GLUON, corps_issue, lien, lire_id, texte, titre_issue
from src.board.github import GitHubError
from src.board.publication import MARGE_HORLOGE, publier_offre, reconstruire
from src.board.registre import (
    COMMANDE_RESYNC,
    Fiche,
    RegistreFiches,
    RegistreIllisible,
)
from src.core.config import load_filters
from src.core.dedup import stable_id
from src.core.normalize import normalize
from src.core.scoring import GeoScorer
from tests.adapter_cases import ALL_ADAPTERS, fetch_case
from tests.test_board_github import (
    TOKEN,
    FauxGitHub,
    issue_creee,
    item_ajoute,
    offre,
    ok,
    projet,
    statut_ecrit,
)

#: Un lien Markdown de la forme que produit `lien` : `[texte](<url>)`.
LIEN_RE = re.compile(r"\[([^\]]*)\]\(<([^>]*)>\)")

ID = "greenhouse:gitlab:4012"
INSTANT = datetime(2026, 9, 16, 8, 15, tzinfo=timezone.utc)


def lignes_de_champs(corps: str) -> dict[str, str]:
    """`**Nom** : valeur  ` → {Nom: valeur}, dans l'ordre de la fiche."""
    champs = {}
    for ligne in corps.splitlines():
        trouve = re.fullmatch(r"\*\*(.+?)\*\* : (.*)  ", ligne)
        if trouve:
            champs[trouve.group(1)] = trouve.group(2)
    return champs


# ---------------------------------------------------------------------------
# US-4.2.1 — la fiche
# ---------------------------------------------------------------------------


def test_rendu_contient_entreprise_poste_localisation_score_et_lien_ats_cliquable() -> None:
    corps = corps_issue(offre())

    champs = lignes_de_champs(corps)
    assert champs["Entreprise"] == "GitLab"
    assert champs["Poste"] == "Backend Engineer"
    assert champs["Localisation"] == "London, UK · hybride"
    assert champs["Score"] == "60 — europe (london)"
    assert champs["Tech"] == "go, ruby"
    assert champs["Publiée le"] == "2026-09-14"

    # Le lien ATS : un seul lien Markdown, qui pointe exactement l'URL de
    # l'offre, en https, et dont le texte est l'hôte visé.
    liens = LIEN_RE.findall(champs["Lien ATS"])
    assert liens == [
        ("job-boards.greenhouse.io", "https://job-boards.greenhouse.io/gitlab/jobs/4012")
    ]
    morceaux = urlsplit(liens[0][1])
    assert (morceaux.scheme, morceaux.hostname) == ("https", "job-boards.greenhouse.io")
    assert LIEN_RE.fullmatch(champs["Lien ATS"])


def test_champs_dans_l_ordre_du_gabarit_puis_l_identifiant() -> None:
    corps = corps_issue(offre())

    assert list(lignes_de_champs(corps)) == [
        "Entreprise",
        "Poste",
        "Localisation",
        "Score",
        "Tech",
        "Lien ATS",
        "Publiée le",
        "Source",
    ]
    assert corps.endswith(f"\n\n<!-- jobradar:id={ID} -->\n")


@pytest.mark.parametrize(
    ("champs", "absents"),
    [
        ({"tech": ()}, {"Tech"}),
        ({"tech": ("", "  ")}, {"Tech"}),
        ({"date": ""}, {"Publiée le"}),
        ({"date": "hier"}, {"Publiée le"}),
        ({"localisation": "", "remote_type": "unknown"}, {"Localisation"}),
        ({"entreprise": "  "}, {"Entreprise"}),
        (
            {"tech": (), "date": "", "localisation": "", "remote_type": "unknown", "detail": ""},
            {"Tech", "Publiée le", "Localisation"},
        ),
    ],
)
def test_champ_optionnel_manquant_ni_trou_ni_none(champs: dict[str, Any], absents: set[str]) -> None:
    corps = corps_issue(offre(**champs))

    assert "None" not in corps
    presents = lignes_de_champs(corps)
    assert absents.isdisjoint(presents)
    assert all(valeur.strip() for valeur in presents.values())
    # Aucune ligne de champ vide, aucun blanc en trop : chaque ligne est un
    # champ, sauf la ligne vide qui précède l'identifiant.
    lignes = corps.splitlines()
    assert lignes.count("") == 1
    assert len(presents) == len(lignes) - 2
    assert lire_id(corps) == stable_id(offre(**champs).job)


def test_localisation_sans_mode_connu_ou_mode_seul() -> None:
    assert lignes_de_champs(corps_issue(offre(remote_type="unknown")))["Localisation"] == "London, UK"
    assert (
        lignes_de_champs(corps_issue(offre(localisation="", remote_type="remote")))["Localisation"]
        == "télétravail"
    )
    assert lignes_de_champs(corps_issue(offre(remote_type="onsite")))["Localisation"].endswith(
        "· sur site"
    )


def test_score_sans_detail_ni_categorie() -> None:
    assert lignes_de_champs(corps_issue(offre(detail="")))["Score"] == "60 — europe"
    assert lignes_de_champs(corps_issue(offre(score=0, categorie="", detail="")))["Score"] == "0"


def test_valeurs_tierces_restent_du_texte() -> None:
    corps = corps_issue(
        offre(
            entreprise="@octocat & Co",
            titre="*Senior* C# <!-- dev_ops",
            localisation="voir autre/depot#12 [ici](https://evil.example)",
            tech=("c#",),
        )
    )
    champs = lignes_de_champs(corps)

    assert champs["Entreprise"] == f"@{GLUON}octocat \\& Co"
    assert champs["Poste"] == "\\*Senior\\* C# \\<!-- dev\\_ops"
    assert champs["Localisation"].startswith(f"voir autre/depot#{GLUON}12 \\[ici\\]")
    # « c# » n'est suivi de rien : pas de gluon inutile.
    assert champs["Tech"] == "c#"
    # Aucune mention, aucune référence, aucun lien, aucun commentaire ouvert
    # ne subsiste en dehors du marqueur final.
    sans_marqueur = corps.rsplit("<!-- jobradar", 1)[0]
    assert re.search(r"[@#]\w", sans_marqueur) is None
    assert re.search(r"(?<![\\(])<", sans_marqueur) is None  # hors `(<url>)`
    assert len(LIEN_RE.findall(corps)) == 1
    assert lire_id(corps) == "greenhouse:octocat-co:4012"


def test_valeur_multiligne_ramenee_sur_une_ligne() -> None:
    corps = corps_issue(offre(localisation="Paris\n\n# Titre\tFrance"))

    assert lignes_de_champs(corps)["Localisation"] == "Paris # Titre France · hybride"
    assert "\n#" not in corps


def test_le_titre_de_l_issue_n_est_pas_echappe() -> None:
    """GitHub affiche le titre en texte brut : un `\\*` y serait visible."""
    assert titre_issue(offre(titre="*Senior* C#")) == "GitLab — *Senior* C#"


@pytest.mark.parametrize(
    "url",
    [
        "javascript:alert(1)",
        "ftp://example.com/offre",
        "https:///sans-hote",
        "https://example.com/<script>",
        "https://example.com/a\\b",
        "https://example.com/\x07",
        "http://[::1",
        "",
    ],
)
def test_lien_mal_forme_jamais_cliquable(url: str) -> None:
    rendu = lien(url)

    assert "](" not in rendu
    assert "<" not in rendu.replace("\\<", "")


@pytest.mark.parametrize(
    ("url", "attendu"),
    [
        ("https://jobs.lever.co/malt/9f1c", "https://jobs.lever.co/malt/9f1c"),
        ("http://example.com/offre (senior)", "http://example.com/offre%20(senior)"),
        ("  https://example.com/a?b=1&c=2  ", "https://example.com/a?b=1&c=2"),
    ],
)
def test_lien_bien_forme_cliquable(url: str, attendu: str) -> None:
    trouve = LIEN_RE.fullmatch(lien(url))

    assert trouve is not None
    assert trouve.group(2) == attendu
    assert trouve.group(1) == urlsplit(attendu).hostname


@pytest.mark.parametrize("natif", ["4012", "a-->b--c", "x y/z%20", "é:ü:>", "---"])
def test_lire_id_relit_ce_que_la_fiche_ecrit(natif: str) -> None:
    une_offre = offre(id=natif)

    assert lire_id(corps_issue(une_offre)) == stable_id(une_offre.job)


@pytest.mark.parametrize("corps", [None, "", "Issue ouverte à la main", "<!-- autre -->"])
def test_lire_id_sans_marqueur(corps: str | None) -> None:
    assert lire_id(corps) is None


def test_texte_ne_change_pas_une_valeur_ordinaire() -> None:
    assert texte("Backend Engineer, Platform (Remote)") == "Backend Engineer, Platform (Remote)"


def test_les_offres_reelles_ont_toutes_une_fiche_propre() -> None:
    """Les 51 offres des dix-huit adaptateurs : un lien ATS cliquable vers
    leur URL, aucun « None », et l'identifiant qui se relit."""
    scorer = GeoScorer.from_config(load_filters("config/filters.yaml").scoring)
    offres = [scorer.noter(job) for case in ALL_ADAPTERS for job in normalize(fetch_case(case))]
    assert len(offres) >= 50

    for une_offre in offres:
        corps = corps_issue(une_offre)
        champs = lignes_de_champs(corps)
        assert "None" not in corps
        assert {"Poste", "Score", "Lien ATS", "Source"} <= set(champs), une_offre.job.url
        trouve = LIEN_RE.fullmatch(champs["Lien ATS"])
        assert trouve is not None, une_offre.job.url
        assert trouve.group(2) == une_offre.job.url.strip().replace(" ", "%20")
        assert urlsplit(trouve.group(2)).scheme in ("http", "https")
        assert lire_id(corps) == stable_id(une_offre.job)


# ---------------------------------------------------------------------------
# US-4.2.2 — le registre
# ---------------------------------------------------------------------------


def test_registre_absent_vaut_registre_vide(tmp_path: Path) -> None:
    registre = RegistreFiches.load(tmp_path / "fiches.json")

    assert len(registre) == 0
    assert registre.get(ID) is None


def test_registre_ecrit_puis_relu_a_l_identique(tmp_path: Path) -> None:
    chemin = tmp_path / "state" / "fiches.json"
    registre = RegistreFiches(chemin)
    registre.enregistrer("b:x:1", Fiche(issue=42, node_id="I_42", item="PVTI_42"))
    registre.enregistrer("a:x:2", Fiche(issue=43, node_id="I_43"))
    registre.enregistrer("c:x:3", Fiche(issue=7, node_id="I_7", hors_tableau=True))
    registre.enregistrer("d:x:4", Fiche(en_cours=INSTANT))
    registre.save()

    brut = json.loads(chemin.read_text(encoding="utf-8"))
    assert list(brut) == ["a:x:2", "b:x:1", "c:x:3", "d:x:4"]
    assert brut == {
        "a:x:2": {"issue": 43, "node_id": "I_43"},
        "b:x:1": {"issue": 42, "node_id": "I_42", "item": "PVTI_42"},
        "c:x:3": {"issue": 7, "node_id": "I_7", "hors_tableau": True},
        "d:x:4": {"en_cours": "2026-09-16T08:15:00Z"},
    }
    relu = RegistreFiches.load(chemin)
    assert relu.as_dict() == registre.as_dict()
    assert relu.get("d:x:4") == Fiche(en_cours=INSTANT)
    assert [p.name for p in chemin.parent.iterdir()] == ["fiches.json"]


@pytest.mark.parametrize(
    "contenu",
    [
        "{ tronqué",
        "<<<<<<< HEAD\n{}\n=======\n{}\n>>>>>>> main\n",
        "[]",
        json.dumps({ID: {"issue": "42", "node_id": "I_42"}}),
        json.dumps({ID: {"issue": True, "node_id": "I_42"}}),
        json.dumps({ID: {"issue": 42}}),
        json.dumps({ID: {"issue": 42, "node_id": "I_42", "item": 3}}),
        json.dumps({ID: {"en_cours": "hier"}}),
        json.dumps({ID: 42}),
    ],
)
def test_registre_illisible_arrete_la_publication(tmp_path: Path, contenu: str) -> None:
    """Contrairement à `seen.json` : un registre vide recréerait toutes les fiches."""
    chemin = tmp_path / "fiches.json"
    chemin.write_text(contenu, encoding="utf-8")

    with pytest.raises(RegistreIllisible) as excinfo:
        RegistreFiches.load(chemin)

    assert COMMANDE_RESYNC in str(excinfo.value)
    assert str(chemin) in str(excinfo.value)


def test_la_suite_n_ecrit_jamais_le_registre_du_depot(isolated_fiches_state: Path) -> None:
    reel = Path("state/fiches.json")
    avant = reel.read_bytes() if reel.exists() else None

    registre = RegistreFiches.load()
    registre.enregistrer(ID, Fiche(issue=1, node_id="I_1"))
    registre.save()

    assert registre.path == isolated_fiches_state
    assert (reel.read_bytes() if reel.exists() else None) == avant


# ---------------------------------------------------------------------------
# US-4.2.2 — la publication sans doublon
# ---------------------------------------------------------------------------


def publication_complete(number: int = 42) -> list[httpx.Response]:
    """Les quatre réponses d'une première publication : Issue, Project, carte, statut."""
    item = f"PVTI_{number}"
    return [issue_creee(number), projet(), item_ajoute(item), statut_ecrit(item)]


def creations(faux: FauxGitHub) -> int:
    return faux.appels.count("POST /repos/louisinayinde/jobs/issues")


def test_offre_au_mapping_existant_zero_appel_de_creation(tmp_path: Path) -> None:
    registre = RegistreFiches(tmp_path / "fiches.json")
    registre.enregistrer(ID, Fiche(issue=42, node_id="I_42", item="PVTI_42"))
    faux = FauxGitHub()  # lève sur tout appel

    publication = publier_offre(faux.client(), registre, offre())

    assert faux.requetes == []
    assert (publication.issue, publication.creee, publication.posee) == (42, False, False)


def test_deux_publications_successives_une_seule_fiche(tmp_path: Path) -> None:
    chemin = tmp_path / "fiches.json"
    faux = FauxGitHub(*publication_complete(42))
    client = faux.client()

    premiere = publier_offre(client, RegistreFiches.load(chemin), offre())
    # Le run suivant repart du fichier, comme un runner neuf.
    seconde = publier_offre(client, RegistreFiches.load(chemin), offre())
    # Et la même offre deux fois dans un même run.
    registre = RegistreFiches.load(chemin)
    publier_offre(client, registre, offre())
    publier_offre(client, registre, offre())

    assert creations(faux) == 1
    assert len(faux.requetes) == 4
    assert (premiere.creee, premiere.posee) == (True, True)
    assert (seconde.issue, seconde.creee, seconde.posee) == (42, False, False)
    assert RegistreFiches.load(chemin).as_dict() == {
        ID: {"issue": 42, "node_id": "I_kwDOUIzwRc42", "item": "PVTI_42"}
    }


def test_premiere_publication_cree_pose_et_enregistre(tmp_path: Path) -> None:
    faux = FauxGitHub(*publication_complete(42))
    registre = RegistreFiches(tmp_path / "fiches.json")

    publication = publier_offre(faux.client(), registre, offre())

    assert faux.appels == [
        "POST /repos/louisinayinde/jobs/issues",
        "POST /graphql",
        "POST /graphql",
        "POST /graphql",
    ]
    assert faux.payload(0)["title"] == "GitLab — Backend Engineer"
    assert faux.payload(2)["variables"]["contenu"] == "I_kwDOUIzwRc42"
    assert publication.identifiant == ID


def test_deux_offres_distinctes_deux_fiches(tmp_path: Path) -> None:
    faux = FauxGitHub(issue_creee(42), projet(), item_ajoute("A"), statut_ecrit("A"),
                      issue_creee(43), item_ajoute("B"), statut_ecrit("B"))
    registre = RegistreFiches(tmp_path / "fiches.json")
    client = faux.client()

    publier_offre(client, registre, offre(id="1"))
    publier_offre(client, registre, offre(id="2"))

    assert creations(faux) == 2
    assert {cle: f["issue"] for cle, f in registre.as_dict().items()} == {
        "greenhouse:gitlab:1": 42,
        "greenhouse:gitlab:2": 43,
    }


def test_issue_connue_sans_carte_seule_la_carte_est_posee(tmp_path: Path) -> None:
    registre = RegistreFiches(tmp_path / "fiches.json")
    registre.enregistrer(ID, Fiche(issue=42, node_id="I_42"))
    faux = FauxGitHub(projet(), item_ajoute("PVTI_42"), statut_ecrit("PVTI_42"))

    publication = publier_offre(faux.client(), registre, offre())

    assert creations(faux) == 0
    assert faux.payload(1)["variables"]["contenu"] == "I_42"
    assert (publication.creee, publication.posee) == (False, True)
    assert RegistreFiches.load(registre.path).get(ID).item == "PVTI_42"


def test_issue_retiree_du_tableau_jamais_reposee(tmp_path: Path) -> None:
    registre = RegistreFiches(tmp_path / "fiches.json")
    registre.enregistrer(ID, Fiche(issue=42, node_id="I_42", hors_tableau=True))
    faux = FauxGitHub()

    publication = publier_offre(faux.client(), registre, offre())

    assert faux.requetes == []
    assert publication.posee is False


def test_echec_de_pose_l_issue_reste_enregistree_et_n_est_pas_recreee(tmp_path: Path) -> None:
    chemin = tmp_path / "fiches.json"
    faux = FauxGitHub(
        issue_creee(42),
        projet(),
        httpx.Response(401, json={"message": "Bad credentials"}),
    )

    with pytest.raises(GitHubError):
        publier_offre(faux.client(), RegistreFiches.load(chemin), offre())

    # Écrit sur disque dès la création, avant la pose qui a échoué.
    assert RegistreFiches.load(chemin).as_dict() == {
        ID: {"issue": 42, "node_id": "I_kwDOUIzwRc42"}
    }

    reprise = FauxGitHub(projet(), item_ajoute("PVTI_42"), statut_ecrit("PVTI_42"))
    publication = publier_offre(reprise.client(), RegistreFiches.load(chemin), offre())

    assert creations(reprise) == 0
    assert (publication.issue, publication.posee) == (42, True)


@pytest.mark.parametrize(
    "reponse",
    [
        httpx.Response(422, json={"message": "Validation Failed"}),
        httpx.Response(401, json={"message": "Bad credentials"}),
        httpx.Response(403, json={"message": "Resource not accessible"}),
    ],
)
def test_refus_net_de_creation_rien_n_est_retenu(tmp_path: Path, reponse: httpx.Response) -> None:
    chemin = tmp_path / "fiches.json"
    faux = FauxGitHub(reponse)

    with pytest.raises(GitHubError) as excinfo:
        publier_offre(faux.client(), RegistreFiches.load(chemin), offre())

    assert excinfo.value.peut_avoir_abouti is False
    assert RegistreFiches.load(chemin).as_dict() == {}


def _panne(request: httpx.Request) -> httpx.Response:
    raise httpx.ReadTimeout("timeout", request=request)


@pytest.mark.parametrize("panne", [httpx.Response(502, text="Bad Gateway"), _panne])
def test_creation_incertaine_reste_en_cours(tmp_path: Path, panne: Any) -> None:
    chemin = tmp_path / "fiches.json"
    faux = FauxGitHub(panne)

    with pytest.raises(GitHubError) as excinfo:
        publier_offre(faux.client(), RegistreFiches.load(chemin), offre(), maintenant=INSTANT)

    assert excinfo.value.peut_avoir_abouti is True
    assert creations(faux) == 1
    assert RegistreFiches.load(chemin).as_dict() == {ID: {"en_cours": "2026-09-16T08:15:00Z"}}


def issue_listee(une_offre: Any, number: int, **champs: Any) -> dict[str, Any]:
    return {
        "number": number,
        "node_id": f"I_{number}",
        "html_url": f"https://github.com/louisinayinde/jobs/issues/{number}",
        "title": titre_issue(une_offre),
        "body": corps_issue(une_offre),
        **champs,
    }


def test_creation_incertaine_qui_avait_abouti_est_adoptee_sans_doublon(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    registre = RegistreFiches(tmp_path / "fiches.json")
    registre.enregistrer(ID, Fiche(en_cours=INSTANT))
    faux = FauxGitHub(
        ok([issue_listee(offre(id="autre"), 41), issue_listee(offre(), 42)]),
        projet(),
        item_ajoute("PVTI_42"),
        statut_ecrit("PVTI_42"),
    )

    with caplog.at_level(logging.WARNING):
        publication = publier_offre(faux.client(), registre, offre())

    assert creations(faux) == 0
    liste = faux.requetes[0]
    assert (liste.method, liste.url.path) == ("GET", "/repos/louisinayinde/jobs/issues")
    assert liste.url.params["state"] == "all"
    assert liste.url.params["since"] == (INSTANT - MARGE_HORLOGE).strftime("%Y-%m-%dT%H:%M:%SZ")
    assert (publication.issue, publication.creee, publication.posee) == (42, False, True)
    assert RegistreFiches.load(registre.path).as_dict() == {
        ID: {"issue": 42, "node_id": "I_42", "item": "PVTI_42"}
    }
    assert "#42 adoptée" in caplog.text


def test_creation_incertaine_qui_n_avait_pas_abouti_est_refaite(tmp_path: Path) -> None:
    registre = RegistreFiches(tmp_path / "fiches.json")
    registre.enregistrer(ID, Fiche(en_cours=INSTANT))
    faux = FauxGitHub(ok([issue_listee(offre(id="autre"), 41)]), *publication_complete(43))

    publication = publier_offre(faux.client(), registre, offre())

    assert creations(faux) == 1
    assert (publication.issue, publication.creee) == (43, True)


# ---------------------------------------------------------------------------
# Lecture des fiches existantes sur GitHub
# ---------------------------------------------------------------------------


def test_lister_fiches_parcourt_les_pages_et_ignore_le_reste(
    caplog: pytest.LogCaptureFixture,
) -> None:
    page1 = [issue_listee(offre(id=str(n)), n) for n in range(1, 101)]
    page1[3]["body"] = "Issue ouverte à la main"
    page1[4]["pull_request"] = {"url": "…"}
    page1[5]["body"] = None
    page2 = [issue_listee(offre(id="2"), 150)]  # doublon de l'Issue #2
    faux = FauxGitHub(ok(page1), ok(page2))

    with caplog.at_level(logging.WARNING):
        fiches = faux.client().lister_fiches()

    assert [r.url.params["page"] for r in faux.requetes] == ["1", "2"]
    assert all(r.url.params["per_page"] == "100" and "since" not in r.url.params for r in faux.requetes)
    assert len(fiches) == 97
    assert "greenhouse:gitlab:4" not in fiches
    assert fiches["greenhouse:gitlab:2"].number == 2
    assert "Issues #2 et #150" in caplog.text


def test_items_du_projet_filtre_le_depot_et_suit_les_curseurs() -> None:
    def items(noeuds: list[dict[str, Any]], suite: str | None) -> httpx.Response:
        return ok(
            {
                "data": {
                    "node": {
                        "items": {
                            "pageInfo": {"hasNextPage": suite is not None, "endCursor": suite},
                            "nodes": noeuds,
                        }
                    }
                }
            }
        )

    def carte(item: str, number: int, depot: str = "LouisInayinde/jobs") -> dict[str, Any]:
        return {"id": item, "content": {"number": number, "repository": {"nameWithOwner": depot}}}

    faux = FauxGitHub(
        projet(),
        items([carte("A", 1), carte("X", 1, "autre/depot"), {"id": "D", "content": {}}], "c1"),
        items([carte("B", 2), {"id": "N", "content": None}], None),
    )

    assert faux.client().items_du_projet() == {1: "A", 2: "B"}
    assert faux.payload(1)["variables"]["apres"] is None
    assert faux.payload(2)["variables"]["apres"] == "c1"


# ---------------------------------------------------------------------------
# Reconstruction : python -m src.board resync
# ---------------------------------------------------------------------------


def items_page(cartes: dict[int, str]) -> httpx.Response:
    noeuds = [
        {"id": item, "content": {"number": n, "repository": {"nameWithOwner": "louisinayinde/jobs"}}}
        for n, item in cartes.items()
    ]
    return ok(
        {"data": {"node": {"items": {"pageInfo": {"hasNextPage": False}, "nodes": noeuds}}}}
    )


def test_reconstruire_note_les_cartes_et_les_issues_retirees(tmp_path: Path) -> None:
    faux = FauxGitHub(
        projet(),
        items_page({42: "PVTI_42"}),
        ok([issue_listee(offre(), 42), issue_listee(offre(id="7"), 7)]),
    )

    registre = reconstruire(faux.client(), tmp_path / "fiches.json")

    assert registre.as_dict() == {
        ID: {"issue": 42, "node_id": "I_42", "item": "PVTI_42"},
        "greenhouse:gitlab:7": {"issue": 7, "node_id": "I_7", "hors_tableau": True},
    }
    assert not registre.path.exists()
    assert all("mutation" not in r.content.decode() for r in faux.requetes)


def test_resync_reecrit_un_registre_illisible_puis_la_publication_ne_recree_rien(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    board = tmp_path / "board.yaml"
    board.write_text(
        "repository: louisinayinde/jobs\nproject: {owner: louisinayinde, number: 3}\n",
        encoding="utf-8",
    )
    fiches = tmp_path / "fiches.json"
    fiches.write_text("<<<<<<< HEAD\n", encoding="utf-8")
    faux = FauxGitHub(
        projet(),
        items_page({42: "PVTI_42"}),
        ok([issue_listee(offre(), 42), issue_listee(offre(id="7"), 7)]),
    )

    code = board_cli.main(
        ["resync", "--board", str(board), "--fiches", str(fiches)],
        env={"GITHUB_TOKEN": TOKEN},
        client=httpx.Client(transport=httpx.MockTransport(faux)),
    )

    assert code == 0
    sortie = capsys.readouterr().out
    assert "2 fiche(s) trouvée(s) sur GitHub — 1 sur le tableau, 1 hors du tableau" in sortie
    assert "avant : illisible" in sortie
    registre = RegistreFiches.load(fiches)
    assert len(registre) == 2

    rien = FauxGitHub()
    publier_offre(rien.client(), registre, offre())
    publier_offre(rien.client(), registre, offre(id="7"))
    assert rien.requetes == []


def test_resync_en_echec_ne_touche_pas_au_registre(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    board = tmp_path / "board.yaml"
    board.write_text(
        "repository: louisinayinde/jobs\nproject: {owner: louisinayinde, number: 3}\n",
        encoding="utf-8",
    )
    fiches = tmp_path / "fiches.json"
    fiches.write_text(json.dumps({ID: {"issue": 42, "node_id": "I_42"}}), encoding="utf-8")
    avant = fiches.read_bytes()
    faux = FauxGitHub(httpx.Response(401, json={"message": "Bad credentials"}))

    code = board_cli.main(
        ["resync", "--board", str(board), "--fiches", str(fiches)],
        env={"GITHUB_TOKEN": TOKEN},
        client=httpx.Client(transport=httpx.MockTransport(faux)),
    )

    assert code == 1
    assert "GitHubAuthError" in capsys.readouterr().err
    assert fiches.read_bytes() == avant


def test_marge_horloge_couvre_un_decalage_de_quelques_minutes() -> None:
    assert MARGE_HORLOGE >= timedelta(minutes=5)
