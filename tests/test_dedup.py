"""Tests de déduplication (US-3.3.T).

Deux choses à prouver, et chacune casse en silence :

- **l'identifiant est stable.** S'il bouge d'un run à l'autre — un
  horodatage, un ordre de dictionnaire, une casse —, la dédup ne dédup
  plus rien et le tableau reçoit la même offre tous les quarts d'heure ;
- **le filtrage a un effet réel.** Une mémoire qu'on lit sans jamais
  l'écrire, ou qu'on écrit sans la relire, laisse tous les tests
  unitaires verts et tout le pipeline inutile. D'où des tests qui
  vérifient l'**état après** — le contenu de `seen.json` sur disque —, et
  un run de collecte joué deux fois de suite.

Aucun test ne touche `state/seen.json` du dépôt : `conftest.py` redirige
le chemin par défaut vers un dossier temporaire, et un garde-fou vérifie
qu'il mord.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pytest
import yaml

from src.adapters import RawJob
from src.core.dedup import (
    ENTREPRISE_INCONNUE,
    DedupReport,
    SeenStore,
    stable_id,
)
from src.core.normalize import Job, normalize, normalize_one
from tests.adapter_cases import ALL_ADAPTERS, fetch_case

LUNDI = datetime(2026, 9, 14, 8, 0, tzinfo=timezone.utc)
MARDI = datetime(2026, 9, 15, 8, 0, tzinfo=timezone.utc)


def job(
    id: str = "4012",
    source: str = "greenhouse",
    entreprise: str = "GitLab",
    titre: str = "Backend Engineer",
) -> Job:
    """Une offre normalisée réduite à ce que la dédup regarde."""
    return Job(
        id=id,
        source=source,
        entreprise=entreprise,
        titre=titre,
        localisation="Remote",
        remote_type="remote",
        url=f"https://boards.greenhouse.io/{entreprise.lower()}/jobs/{id}",
        date="2026-09-14T08:00:00Z",
        description="",
    )


def raw(**champs: str) -> RawJob:
    """Une offre brute telle qu'un adaptateur Greenhouse la produirait."""
    valeurs = {
        "id": "4012",
        "ats": "greenhouse",
        "entreprise": "GitLab",
        "titre": "Backend Engineer",
        "localisation": "Remote",
        "remote_type": "remote",
        "url": "https://boards.greenhouse.io/gitlab/jobs/4012",
        "date": "2026-09-14T08:00:00Z",
        "description": "<p>Python et Go.</p>",
    }
    valeurs.update(champs)
    return RawJob(**valeurs)


# ---------------------------------------------------------------------------
# US-3.3.1 — l'identifiant stable
# ---------------------------------------------------------------------------


def test_l_identifiant_suit_le_format_source_entreprise_id() -> None:
    assert stable_id(job()) == "greenhouse:gitlab:4012"


def test_meme_offre_sur_deux_runs_donne_un_identifiant_identique() -> None:
    """DoD. Deux runs, c'est deux conversions indépendantes de la même
    réponse de plateforme : rien ne doit survivre de l'un à l'autre, et
    pourtant la chaîne doit être la même au caractère près."""
    premier_run = normalize_one(raw())
    second_run = normalize_one(raw())

    assert premier_run is not second_run
    assert stable_id(premier_run) == stable_id(second_run) == "greenhouse:gitlab:4012"


def test_l_identifiant_ne_depend_que_de_l_identite_de_l_offre() -> None:
    """Le titre reformulé, la description réécrite, la date republiée : une
    offre qu'un recruteur retouche reste la même offre, et ne doit pas
    revenir sur le tableau comme une neuve."""
    avant = normalize_one(raw())
    apres = normalize_one(
        raw(
            titre="Senior Backend Engineer (Go)",
            description="<p>Tout autre texte.</p>",
            date="2026-09-20T10:00:00Z",
            localisation="Remote, EMEA",
        )
    )

    assert stable_id(avant) == stable_id(apres)


def test_meme_offre_rejouee_depuis_une_fixture_reelle_garde_son_identifiant() -> None:
    """DoD, sur de vraies réponses : chaque adaptateur, rejoué deux fois,
    produit la même suite d'identifiants."""
    for case in ALL_ADAPTERS:
        premier = [stable_id(offre) for offre in normalize(fetch_case(case))]
        second = [stable_id(offre) for offre in normalize(fetch_case(case))]

        assert premier == second, case.id
        assert premier, f"{case.id} : aucune offre, le test ne prouverait rien"


def test_la_casse_et_les_accents_de_l_entreprise_ne_changent_pas_l_identifiant() -> None:
    """`sources.yaml` est écrit à la main : corriger « Gitlab » en « GitLab »
    ne doit pas faire republier toutes les offres de l'entreprise."""
    assert stable_id(job(entreprise="Gitlab")) == stable_id(job(entreprise="GitLab"))
    assert (
        stable_id(job(entreprise="Société Générale"))
        == stable_id(job(entreprise="societe generale"))
        == "greenhouse:societe-generale:4012"
    )


def test_l_identifiant_natif_est_garde_tel_quel() -> None:
    """C'est la plateforme qui l'a choisi : « abc » et « ABC » y sont peut-être
    deux offres, on ne décide pas à sa place."""
    assert stable_id(job(id="AbC")) != stable_id(job(id="abc"))


def test_deux_sources_ou_deux_entreprises_donnent_deux_identifiants() -> None:
    """Le même numéro d'offre n'est unique que chez sa plateforme."""
    base = stable_id(job(id="1"))

    assert stable_id(job(id="1", source="lever")) != base
    assert stable_id(job(id="1", entreprise="Malt")) != base


def test_l_identifiant_se_redecoupe_meme_quand_l_id_natif_contient_deux_points() -> None:
    """Ni la source ni le slug d'entreprise ne contiennent de `:` : les deux
    premiers séparateurs délimitent toujours les trois composantes."""
    offre = job(id="urn:job:42", entreprise="A:B Corp")

    assert stable_id(offre).split(":", 2) == ["greenhouse", "a-b-corp", "urn:job:42"]


def test_une_offre_sans_entreprise_garde_trois_composantes() -> None:
    assert stable_id(job(entreprise="")) == f"greenhouse:{ENTREPRISE_INCONNUE}:4012"
    assert stable_id(job(entreprise="!!!")) == f"greenhouse:{ENTREPRISE_INCONNUE}:4012"


def test_deux_offres_reelles_de_meme_identifiant_sont_bien_la_meme_offre() -> None:
    """Le risque inverse de l'instabilité : une **collision**. Deux offres
    distinctes sous le même identifiant, et la dédup en ferait disparaître
    une sans le dire. Sur les 51 offres réelles des fixtures, un identifiant
    partagé doit toujours désigner la même URL.

    Le cas se produit vraiment : la fixture Japan Dev sert la même page
    d'offre pour deux URLs du sitemap, et l'adaptateur lit l'identifiant
    dans la page. C'est exactement « la même offre deux fois dans un run ».
    """
    urls_par_id: dict[str, set[str]] = {}
    for case in ALL_ADAPTERS:
        for offre in normalize(fetch_case(case)):
            urls_par_id.setdefault(stable_id(offre), set()).add(offre.url)

    collisions = {cle: urls for cle, urls in urls_par_id.items() if len(urls) > 1}
    assert not collisions


# ---------------------------------------------------------------------------
# US-3.3.2 — le tri contre `seen.json`
# ---------------------------------------------------------------------------


def test_une_offre_deja_dans_seen_json_est_absente_du_resultat(tmp_path) -> None:
    """DoD."""
    chemin = tmp_path / "seen.json"
    chemin.write_text(
        json.dumps({"greenhouse:gitlab:4012": "2026-09-14T08:00:00Z"}), encoding="utf-8"
    )
    deja_vue, neuve = job(id="4012"), job(id="5000")

    bilan = SeenStore.load(chemin).nouvelles([deja_vue, neuve])

    assert deja_vue not in bilan.nouvelles
    assert bilan.nouvelles == [neuve]
    assert bilan.deja_vues == [deja_vue]


def test_une_nouvelle_offre_est_presente_et_ajoutee_a_seen_json(tmp_path) -> None:
    """DoD : présente dans le résultat **et** sur disque après le run — l'état
    après est relu depuis le fichier, pas depuis l'objet qui l'a écrit."""
    chemin = tmp_path / "state" / "seen.json"
    neuve = job(id="5000")

    vues = SeenStore.load(chemin)
    bilan = vues.nouvelles([neuve])
    vues.marquer(bilan, quand=LUNDI)
    vues.save()

    assert bilan.nouvelles == [neuve]
    assert json.loads(chemin.read_text(encoding="utf-8")) == {
        "greenhouse:gitlab:5000": "2026-09-14T08:00:00Z"
    }
    # Et le run suivant, qui relit ce fichier, ne la voit plus comme neuve.
    assert SeenStore.load(chemin).nouvelles([neuve]).nouvelles == []


@pytest.mark.parametrize(
    "contenu",
    [
        pytest.param(None, id="absent"),
        pytest.param(b"", id="vide"),
        pytest.param(b'{"greenhouse:gitlab:4012": "2026-09-1', id="tronque"),
        pytest.param(b"<<<<<<< HEAD\n{}\n=======\n{}\n>>>>>>> main", id="conflit-git"),
        pytest.param(b'["greenhouse:gitlab:4012"]', id="liste-au-lieu-de-mapping"),
        pytest.param(b"null", id="null"),
        pytest.param(b"\xff\xfe\x00binaire", id="binaire"),
    ],
)
def test_seen_json_absent_ou_corrompu_vaut_un_etat_vide_sans_crash(
    tmp_path, contenu: bytes | None
) -> None:
    """DoD. Un premier run, un commit malheureux, un conflit de fusion mal
    résolu : dans tous les cas on repart d'une mémoire vide, et **toutes**
    les offres passent — republier une fois coûte moins qu'un run en échec
    tous les quarts d'heure."""
    chemin = tmp_path / "seen.json"
    if contenu is not None:
        chemin.write_bytes(contenu)
    offres = [job(id="4012"), job(id="5000")]

    vues = SeenStore.load(chemin)

    assert len(vues) == 0
    assert vues.nouvelles(offres).nouvelles == offres


def test_un_seen_json_corrompu_est_reecrit_proprement_au_run_suivant(tmp_path) -> None:
    """L'état vide n'est pas qu'une lecture indulgente : le fichier cassé est
    remplacé par un fichier valide, sinon il resterait cassé pour toujours."""
    chemin = tmp_path / "seen.json"
    chemin.write_text("{pas du json", encoding="utf-8")

    vues = SeenStore.load(chemin)
    vues.marquer([job()], quand=LUNDI)
    vues.save()

    assert SeenStore.load(chemin).as_dict() == {"greenhouse:gitlab:4012": "2026-09-14T08:00:00Z"}


def test_une_entree_abimee_ne_coute_pas_les_autres(tmp_path, caplog) -> None:
    chemin = tmp_path / "seen.json"
    chemin.write_text(
        json.dumps(
            {
                "greenhouse:gitlab:4012": "2026-09-14T08:00:00Z",
                "greenhouse:gitlab:5000": 12,
                "": "2026-09-14T08:00:00Z",
            }
        ),
        encoding="utf-8",
    )

    vues = SeenStore.load(chemin)

    assert vues.as_dict() == {"greenhouse:gitlab:4012": "2026-09-14T08:00:00Z"}
    assert "2 entrée(s) invalide(s)" in caplog.text


def test_deux_offres_identiques_dans_le_meme_run_une_seule_est_retenue(tmp_path) -> None:
    """DoD. La première occurrence est gardée — l'ordre d'entrée est celui
    dont le scoring se servira pour départager — et la seconde est comptée,
    pas perdue sans trace."""
    premiere = job(titre="Backend Engineer")
    repetee = job(titre="Backend Engineer (republiée)")
    autre = job(id="5000")

    bilan = SeenStore.load(tmp_path / "seen.json").nouvelles([premiere, autre, repetee])

    assert bilan.nouvelles == [premiere, autre]
    assert bilan.doublons == [repetee]


def test_un_doublon_d_une_offre_deja_vue_n_est_compte_qu_une_fois_comme_deja_vue(
    tmp_path,
) -> None:
    vues = SeenStore(tmp_path / "seen.json", {"greenhouse:gitlab:4012": "2026-09-14T08:00:00Z"})

    bilan = vues.nouvelles([job(), job()])

    assert (len(bilan.nouvelles), len(bilan.deja_vues), len(bilan.doublons)) == (0, 1, 1)


def test_trier_ne_memorise_rien(tmp_path) -> None:
    """Le tri et la mémorisation sont deux temps : la publication
    (Feature 4.3) s'intercalera entre les deux, pour ne marquer vue qu'une
    offre réellement arrivée sur le tableau."""
    chemin = tmp_path / "seen.json"
    vues = SeenStore.load(chemin)

    vues.nouvelles([job()])
    vues.nouvelles([job()])

    assert len(vues) == 0
    assert not chemin.exists()


def test_l_ordre_des_offres_nouvelles_est_celui_d_entree(tmp_path) -> None:
    offres = [job(id=str(n)) for n in (9, 3, 7, 1)]

    bilan = SeenStore.load(tmp_path / "seen.json").nouvelles(offres)

    assert list(bilan) == offres


def test_le_bilan_affiche_les_deja_vues_meme_a_zero() -> None:
    """C'est le chiffre qui dit que la mémoire marche : absent du journal, un
    `seen.json` jamais commité passerait inaperçu."""
    assert DedupReport(nouvelles=[job()]).resume == "1 nouvelle(s) sur 1 — 0 déjà vue(s)"
    assert (
        DedupReport(nouvelles=[job()], deja_vues=[job(id="2")], doublons=[job()]).resume
        == "1 nouvelle(s) sur 3 — 1 déjà vue(s), 1 doublon(s) dans le run"
    )


# ---------------------------------------------------------------------------
# US-3.3.3 — la mémorisation
# ---------------------------------------------------------------------------


def test_une_offre_deja_connue_garde_sa_date_de_premiere_vue(tmp_path) -> None:
    """Réécrire la date à chaque run ferait bouger chaque ligne du fichier
    tous les quarts d'heure : le diff Git ne montrerait plus rien."""
    vues = SeenStore.load(tmp_path / "seen.json")

    assert vues.marquer([job()], quand=LUNDI) == 1
    assert vues.marquer([job(), job(id="5000")], quand=MARDI) == 1

    assert vues.as_dict() == {
        "greenhouse:gitlab:4012": "2026-09-14T08:00:00Z",
        "greenhouse:gitlab:5000": "2026-09-15T08:00:00Z",
    }


def test_seen_json_est_ecrit_trie_et_sans_temporaire_residuel(tmp_path) -> None:
    """Fichier versionné : trié pour un diff lisible, et l'écriture atomique
    ne laisse pas de `.tmp` qu'un `git add state/` embarquerait."""
    chemin = tmp_path / "state" / "seen.json"
    vues = SeenStore.load(chemin)
    vues.marquer([job(id="9"), job(id="1", source="ashby"), job(id="5")], quand=LUNDI)

    vues.save()

    cles = list(json.loads(chemin.read_text(encoding="utf-8")))
    assert cles == sorted(cles)
    assert [p.name for p in chemin.parent.iterdir()] == ["seen.json"]


def test_le_temporaire_de_seen_json_est_ignore_par_git() -> None:
    assert "state/*.tmp" in Path(".gitignore").read_text(encoding="utf-8")


def test_la_suite_n_ecrit_jamais_la_memoire_du_depot(isolated_seen_state) -> None:
    """Le chemin par défaut est redirigé par `conftest.py` : un test qui
    lirait la mémoire d'un vrai run trouverait « déjà vues » les offres
    qu'il attend neuves, et passerait ou échouerait pour de mauvaises
    raisons.

    Le fichier du dépôt existe — les workflows le commitent : c'est son
    contenu qui ne doit pas bouger, pas son absence qui est vérifiée."""
    reel = Path("state/seen.json")
    avant = reel.read_bytes() if reel.exists() else None

    vues = SeenStore.load()
    vues.marquer([job()])
    vues.save()

    assert vues.path == isolated_seen_state
    assert isolated_seen_state.is_file()
    assert (reel.read_bytes() if reel.exists() else None) == avant


# ---------------------------------------------------------------------------
# La dédup est branchée dans le run, pas seulement écrite
# ---------------------------------------------------------------------------


def _run_collecte(
    monkeypatch, capsys, *argv: str, tableau: Any = None
) -> tuple[int, str]:
    """Un run de collecte sur les trois fixtures ATS, qui publie sur un faux
    tableau (`FauxTableau`, un neuf par défaut) : la mémoire n'est écrite
    que pour les offres publiées."""
    from src import collect
    from tests.test_publication import FauxTableau
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

    tableau = tableau if tableau is not None else FauxTableau()
    code = collect.main(["--cadence", "fast", *argv], client=client, github=tableau.client())
    return code, capsys.readouterr().out


def test_deux_runs_successifs_ne_retiennent_les_offres_qu_une_fois(
    monkeypatch, capsys, isolated_seen_state
) -> None:
    """Le test qui compte le plus : sans lui, `seen.json` pourrait être lu
    parfaitement et jamais écrit, ou écrit parfaitement et jamais relu."""
    code, premier = _run_collecte(monkeypatch, capsys)

    assert code == 0
    assert "5 nouvelle(s) sur 5 — 0 déjà vue(s)" in premier
    assert "5 offre(s) mémorisée(s), 5 au total" in premier
    memoire = json.loads(isolated_seen_state.read_text(encoding="utf-8"))
    assert len(memoire) == 5
    assert all(cle.split(":")[0] in {"greenhouse", "lever", "ashby"} for cle in memoire)

    code, second = _run_collecte(monkeypatch, capsys)

    assert code == 0
    assert "0 nouvelle(s) sur 5 — 5 déjà vue(s)" in second
    assert "0 offre(s) mémorisée(s), 5 au total" in second
    assert json.loads(isolated_seen_state.read_text(encoding="utf-8")) == memoire


def test_le_run_accepte_un_autre_chemin_de_memoire(monkeypatch, capsys, tmp_path) -> None:
    ailleurs = tmp_path / "ailleurs" / "seen.json"

    code, _ = _run_collecte(monkeypatch, capsys, "--seen", str(ailleurs))

    assert code == 0
    assert len(json.loads(ailleurs.read_text(encoding="utf-8"))) == 5


def test_un_run_dont_toutes_les_sources_echouent_n_ecrit_pas_la_memoire(
    monkeypatch, capsys, isolated_seen_state
) -> None:
    from src import collect
    from tests.test_robustness import TROIS_SOURCES, routing_client

    monkeypatch.setenv("GITHUB_TOKEN", "jeton-de-test")
    monkeypatch.setattr(collect, "load_all", lambda **kwargs: list(TROIS_SOURCES))

    code = collect.main(
        ["--cadence", "fast"], client=routing_client({}, default=(500, "panne"))
    )

    assert code == 1
    assert not isolated_seen_state.exists()


# ---------------------------------------------------------------------------
# La mémoire survit au runner : les workflows la commitent
# ---------------------------------------------------------------------------

WORKFLOWS_QUI_ECRIVENT_LA_MEMOIRE = {"collect.yml": "collect", "collect-slow.yml": "collect-slow"}


def _workflow(nom: str) -> dict:
    return yaml.safe_load((Path(".github/workflows") / nom).read_text(encoding="utf-8"))


@pytest.mark.parametrize("nom, job_id", WORKFLOWS_QUI_ECRIVENT_LA_MEMOIRE.items())
def test_chaque_workflow_de_collecte_commite_la_memoire(nom: str, job_id: str) -> None:
    """Sans ce commit, le runner éphémère emporte `seen.json` avec lui, et
    chaque run repart d'une mémoire vide : la dédup ne sert à rien en
    production alors que tous les autres tests passent."""
    job_doc = _workflow(nom)["jobs"][job_id]
    commandes = "\n".join(etape.get("run", "") for etape in job_doc["steps"])

    assert "git add state/" in commandes
    assert "git push" in commandes
    assert job_doc["permissions"]["contents"] == "write"


def test_les_workflows_qui_ecrivent_la_memoire_sont_serialises_ensemble() -> None:
    """Les deux écrivent `seen.json` : un groupe **commun**, sinon un run
    rapide et un run lent pousseraient deux versions concurrentes."""
    groupes = {nom: _workflow(nom)["concurrency"] for nom in WORKFLOWS_QUI_ECRIVENT_LA_MEMOIRE}

    assert len({groupe["group"] for groupe in groupes.values()}) == 1
    assert all(groupe["cancel-in-progress"] is False for groupe in groupes.values())


@pytest.mark.parametrize("nom, job_id", WORKFLOWS_QUI_ECRIVENT_LA_MEMOIRE.items())
def test_un_run_en_attente_part_de_la_memoire_la_plus_recente(nom: str, job_id: str) -> None:
    """Sans `ref`, `actions/checkout` prend le commit de l'événement : un run
    mis en file derrière un autre repartirait de la mémoire d'avant, et
    re-publierait les offres que son prédécesseur vient de mémoriser."""
    checkout = next(
        etape
        for etape in _workflow(nom)["jobs"][job_id]["steps"]
        if str(etape.get("uses", "")).startswith("actions/checkout")
    )

    assert checkout["with"]["ref"] == "${{ github.ref }}"
