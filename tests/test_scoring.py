"""Tests du scoring géo et du tri (US-3.4.T).

Deux choses à prouver, et chacune casse sans bruit :

- **les valeurs sont exactes.** Un score faux ne fait échouer aucun run :
  il envoie simplement l'offre japonaise sous l'offre « Remote (US) », et
  le tableau reste plein. D'où des égalités de valeurs, jamais des
  comparaisons ;
- **l'ordre est déterministe.** Un tri qui dépendrait de l'ordre d'un
  `set` ou d'une date mal lue donnerait un tableau différent à chaque
  quart d'heure. D'où des assertions sur la **séquence complète**.

Comme pour la rétention, la configuration de référence est écrite dans le
test : un test qui lirait `filters.yaml` changerait de verdict au premier
ajustement de priorités. Un test à part vérifie que la configuration
versionnée donne bien les valeurs de la DoD.
"""

from __future__ import annotations

import logging

import pytest

from src.adapters.base import RawJob
from src.core.config import RetentionConfig, ScoringConfig, TechBonusConfig, load_filters
from src.core.normalize import TECH_VOCABULARY, Job, normalize
from src.core.scoring import (
    DEFAULT,
    REMOTE_GLOBAL,
    SCORE_PLANCHER,
    GeoScorer,
    ScoredJob,
    ScoringReport,
    ne_nomme_aucun_lieu,
    trier,
)
from tests.adapter_cases import ALL_ADAPTERS, fetch_case

REAL_FILTERS = "config/filters.yaml"

#: Les priorités de `website.md`, dans l'ordre et aux valeurs de la DoD.
PRIORITES = {
    "remote_global": 100,
    "asia_relocation": 80,
    "europe": 60,
    "france": 40,
    "default": 0,
}

SCORER = GeoScorer.from_config(ScoringConfig(geo_priority=PRIORITES))


def job(
    localisation: str = "",
    remote_type: str = "remote",
    date: str = "2026-09-14T08:00:00+00:00",
    id: str = "4012",
    titre: str = "Backend Engineer",
    tech: tuple[str, ...] = (),
) -> Job:
    """Une offre normalisée réduite à ce que le scoring regarde."""
    return Job(
        id=id,
        source="greenhouse",
        entreprise="GitLab",
        titre=titre,
        localisation=localisation,
        remote_type=remote_type,
        url=f"https://boards.greenhouse.io/gitlab/jobs/{id}",
        date=date,
        description="",
        tech=tech,
    )


def ids(offres) -> list[str]:
    return [offre.job.id for offre in offres]


# ---------------------------------------------------------------------------
# US-3.4.1 — valeurs exactes
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("localisation", "remote_type", "score", "categorie"),
    [
        # La DoD, une catégorie par ligne.
        ("Anywhere in the World", "remote", 100, "remote_global"),
        ("Tokyo, JP", "onsite", 80, "asia_relocation"),
        ("Berlin, Germany", "hybrid", 60, "europe"),
        ("Paris, France", "onsite", 40, "france"),
    ],
    ids=["remote-first-global", "asie-reloc", "europe", "france"],
)
def test_chaque_priorite_geo_donne_sa_valeur_exacte(
    localisation: str, remote_type: str, score: int, categorie: str
) -> None:
    note = SCORER.noter(job(localisation, remote_type))

    assert note.score == score
    assert note.categorie == categorie


def test_la_configuration_versionnee_donne_les_valeurs_de_la_dod() -> None:
    """Le même quatuor, jugé par `config/filters.yaml` tel qu'il est commité."""
    scorer = GeoScorer.from_config(load_filters(REAL_FILTERS).scoring)

    assert [
        scorer.noter(job("Worldwide", "remote")).score,
        scorer.noter(job("Remote, Bangalore", "remote")).score,
        scorer.noter(job("Remote (Europe)", "remote")).score,
        scorer.noter(job("Lyon", "hybrid")).score,
    ] == [100, 80, 60, 40]


@pytest.mark.parametrize(
    "localisation",
    ["", "Remote", "Worldwide", "Anywhere in the World", "Remote (Worldwide)",
     "100% Remote", "Fully remote, work from anywhere", "Télétravail complet"],
)
def test_une_offre_remote_qui_ne_nomme_aucun_lieu_est_remote_global(localisation: str) -> None:
    note = SCORER.noter(job(localisation, "remote"))

    assert (note.score, note.categorie, note.detail) == (100, REMOTE_GLOBAL, "")


@pytest.mark.parametrize(
    "localisation",
    [
        "Remote (US only)",
        "Remote, Canada",
        "Remote, Canada; Remote, United States",
        "Anywhere in the US",
        "New York, NY (HQ); San Francisco, CA; Remote (US)",
        "Remote US or Ontario, Canada",
        "Australia",
    ],
)
def test_le_remote_menteur_tombe_au_plancher(localisation: str) -> None:
    """La limite nommée à l'US-3.2.3, rejugée ici : ces offres passent la
    rétention (`require_any: ["remote"]`) mais ne sont pas ouvertes à nous.
    Elles restent publiées, en bas du tableau."""
    note = SCORER.noter(job(localisation, "remote"))

    assert (note.score, note.categorie) == (0, DEFAULT)


def test_remote_global_exige_le_teletravail() -> None:
    """Une localisation vide ne dit pas « monde entier » pour une offre sur site."""
    assert SCORER.noter(job("", "onsite")).categorie == DEFAULT
    assert SCORER.noter(job("", "hybrid")).categorie == DEFAULT
    assert SCORER.noter(job("", "unknown")).categorie == DEFAULT


def test_une_offre_remote_dans_une_zone_prend_le_score_de_sa_zone() -> None:
    """« Remote (Europe) » n'est pas ouvert au monde : c'est du remote européen."""
    assert SCORER.noter(job("Remote (Europe)", "remote")).score == 60
    assert SCORER.noter(job("Japan - Remote", "remote")).score == 80


def test_une_offre_sur_plusieurs_zones_prend_la_meilleure() -> None:
    note = SCORER.noter(job("Paris; London; Lyon", "hybrid"))
    assert (note.score, note.categorie, note.detail) == (60, "europe", "london")

    note = SCORER.noter(job("LATAM, Europe, USA, Canada, APAC", "remote"))
    assert (note.score, note.categorie, note.detail) == (80, "asia_relocation", "apac")


def test_le_detail_nomme_le_terme_lu() -> None:
    assert SCORER.noter(job("Tokyo, JP", "remote")).detail == "tokyo"


def test_les_zones_sont_celles_de_la_retention_en_mot_entier() -> None:
    """`fr` est un code de la zone France : en sous-chaîne, il ferait de
    Frankfurt une offre française. La zone gagnante reste l'Allemagne."""
    note = SCORER.noter(job("Frankfurt, Germany", "onsite"))

    assert (note.categorie, note.detail) == ("europe", "germany")


def test_une_cle_suffixee_relocation_cherche_sa_zone() -> None:
    scorer = GeoScorer({"europe_relocation": 70})

    assert scorer.noter(job("Berlin", "onsite")).score == 70


def test_une_zone_inconnue_du_module_est_cherchee_telle_quelle(caplog) -> None:
    """`japan: 90` est une configuration valable ; une faute de frappe lui
    ressemble exactement, d'où l'avertissement."""
    with caplog.at_level(logging.WARNING, logger="src.core.scoring"):
        scorer = GeoScorer.from_config(
            ScoringConfig(geo_priority={"luxembourg": 90, "remote_globl": 100})
        )

    assert scorer.noter(job("Luxembourg", "onsite")).score == 90
    assert "luxembourg" in caplog.text
    assert "remote_globl" in caplog.text


def test_a_score_egal_entre_categories_la_premiere_ecrite_gagne() -> None:
    scorer = GeoScorer({"france": 50, "europe": 50})

    assert scorer.noter(job("Paris; Berlin", "hybrid")).categorie == "france"


# ---------------------------------------------------------------------------
# Le plancher : jamais d'exception
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("localisation", "remote_type"),
    [
        ("Wasquehal - 59", "unknown"),
        ("", "unknown"),
        ("???", "onsite"),
        ("🌍 🚀", "hybrid"),
        ("\n\t", "remote-ish"),
        ("x" * 10_000, "onsite"),
    ],
)
def test_une_offre_non_classable_recoit_le_plancher_sans_exception(
    localisation: str, remote_type: str
) -> None:
    note = SCORER.noter(job(localisation, remote_type))

    assert (note.score, note.categorie) == (0, DEFAULT)


def test_le_plancher_est_celui_de_la_configuration() -> None:
    scorer = GeoScorer({"europe": 60, "default": -10})

    assert scorer.noter(job("Mars", "onsite")).score == -10


def test_sans_default_le_plancher_vaut_zero() -> None:
    assert GeoScorer({"europe": 60}).noter(job("Mars", "onsite")).score == SCORE_PLANCHER == 0


def test_sans_aucune_priorite_tout_vaut_le_plancher() -> None:
    scorer = GeoScorer.from_config(ScoringConfig())

    assert {scorer.noter(job(loc)).score for loc in ("", "Tokyo", "Paris")} == {0}


def test_les_offres_reelles_des_dix_huit_adaptateurs_sont_toutes_notees() -> None:
    """Aucune exception, aucun score hors de la configuration, et les
    catégories principales toutes représentées — un scorer qui renverrait
    toujours le plancher passerait sinon tous les tests de non-exception."""
    offres = [offre for case in ALL_ADAPTERS for offre in normalize(fetch_case(case))]

    notes = [SCORER.noter(offre) for offre in offres]

    assert len(notes) == len(offres)
    assert {note.score for note in notes} <= set(PRIORITES.values())
    assert {note.categorie for note in notes} == set(PRIORITES)


# ---------------------------------------------------------------------------
# US-3.4.2 — l'ordre
# ---------------------------------------------------------------------------


def test_une_liste_melangee_de_cinq_offres_donne_la_sequence_attendue() -> None:
    melange = [
        job("Paris, France", "onsite", id="france"),
        job("Remote, Canada", "remote", id="plancher"),
        job("Worldwide", "remote", id="global"),
        job("Berlin", "hybrid", id="europe"),
        job("Tokyo, JP", "remote", id="asie"),
    ]

    classement = SCORER.classer(melange)

    assert ids(classement) == ["global", "asie", "europe", "france", "plancher"]
    assert [offre.score for offre in classement] == [100, 80, 60, 40, 0]


def test_a_score_egal_la_plus_recente_passe_en_premier() -> None:
    ancienne = job("Tokyo", date="2026-08-01T09:00:00+00:00", id="ancienne")
    recente = job("Osaka", date="2026-09-10T09:00:00+00:00", id="recente")

    assert ids(SCORER.classer([ancienne, recente])) == ["recente", "ancienne"]
    assert ids(SCORER.classer([recente, ancienne])) == ["recente", "ancienne"]


def test_la_date_departage_a_la_seconde_et_quel_que_soit_le_suffixe_utc() -> None:
    """`Z` et `+00:00` sont la même heure : comparer les chaînes classerait
    `+00:00` avant `Z` à date égale, et une seconde d'écart ne compterait pas."""
    a = job("Tokyo", date="2026-09-10T09:00:01Z", id="a")
    b = job("Tokyo", date="2026-09-10T09:00:00+00:00", id="b")

    assert ids(SCORER.classer([b, a])) == ["a", "b"]


def test_a_score_et_date_egaux_l_ordre_d_entree_est_conserve() -> None:
    offres = [job("Tokyo", id=str(n)) for n in (3, 1, 2)]

    assert ids(SCORER.classer(offres)) == ["3", "1", "2"]


def test_une_offre_sans_date_lisible_passe_apres_ses_egales() -> None:
    offres = [
        job("Tokyo", date="", id="sans-date"),
        job("Tokyo", date="hier", id="illisible"),
        job("Tokyo", date="2019-01-01T00:00:00+00:00", id="datee"),
    ]

    assert ids(SCORER.classer(offres)) == ["datee", "sans-date", "illisible"]


def test_le_score_prime_toujours_sur_la_date() -> None:
    vieille_asie = job("Tokyo", date="2020-01-01T00:00:00+00:00", id="asie")
    fraiche_france = job("Paris", "onsite", date="2026-09-16T00:00:00+00:00", id="france")

    assert ids(SCORER.classer([fraiche_france, vieille_asie])) == ["asie", "france"]


def test_trier_ne_modifie_pas_l_entree() -> None:
    entree = [ScoredJob(job(id="a"), 0, DEFAULT), ScoredJob(job(id="b"), 100, REMOTE_GLOBAL)]
    copie = list(entree)

    assert ids(trier(entree)) == ["b", "a"]
    assert entree == copie


def test_le_tri_est_deterministe_sur_les_offres_reelles() -> None:
    offres = [offre for case in ALL_ADAPTERS for offre in normalize(fetch_case(case))]

    premier = SCORER.classer(offres)
    second = SCORER.classer(list(offres))

    assert premier.offres == second.offres
    scores = [offre.score for offre in premier]
    assert scores == sorted(scores, reverse=True)


# ---------------------------------------------------------------------------
# Le bilan
# ---------------------------------------------------------------------------


def test_le_bilan_compte_par_categorie_dans_l_ordre_de_la_configuration() -> None:
    classement = SCORER.classer(
        [job("Mars", "onsite"), job("Paris", "onsite"), job("Worldwide"), job("Paris", "onsite")]
    )

    assert classement.resume == "4 offre(s) classée(s) — remote_global 1, france 2, default 1"
    assert classement.jobs == [offre.job for offre in classement.offres]


def test_un_bilan_vide_tient_en_une_ligne() -> None:
    assert SCORER.classer([]).resume == "0 offre(s) classée(s)"
    assert len(ScoringReport()) == 0


def test_mots_neutres() -> None:
    assert ne_nomme_aucun_lieu("Remote — Anywhere in the World (100%)")
    assert not ne_nomme_aucun_lieu("Remote — Anywhere in the US")


# ---------------------------------------------------------------------------
# Le scoring est branché dans le run, pas seulement écrit
# ---------------------------------------------------------------------------


def test_le_run_de_collecte_classe_les_offres_nouvelles(monkeypatch, capsys) -> None:
    """Les trois fixtures ATS réelles (GitLab, Malt, Ramp) : l'offre
    européenne en tête (celle de Malt, Staff, est écartée par la
    séniorité), puis les quatre « remote » nord-américaines, au plancher
    géo et départagées par leur bonus de stack."""
    from tests.test_dedup import _run_collecte

    code, sortie = _run_collecte(monkeypatch, capsys)

    assert code == 0
    assert "5 offre(s) classée(s) — europe 1, default 4" in sortie
    lignes = [ligne.strip() for ligne in sortie.splitlines() if ligne.startswith("    ")]
    assert lignes == [
        "62 europe (london) · stack +2 (Rails, Swift) : "
        "Ramp — Software Engineer, International [London]",
        "40 default · stack +40 (AWS, Azure, GCP, Go, Java, Python, Rails, Terraform) : "
        "Ramp — Software Engineer, Security, Stablecoin "
        "[New York, NY (HQ); San Francisco, CA; Remote (US)]",
        "36 default · stack +36 (GraphQL, PostgreSQL, Python, Rails, REST, Ruby, SQL, Vue) : "
        "GitLab — Backend Engineer (Ruby), AI Engineering: Agent Observability "
        "[Remote, Canada]",
        "30 default · stack +30 (JavaScript, React, TypeScript) : "
        "Ramp — Software Engineer, Frontend "
        "[New York, NY (HQ); Remote (Canada); San Francisco, CA; Remote (US); Miami, FL]",
        "15 default · stack +15 (GraphQL, Python, Rails, Ruby) : "
        "GitLab — Backend Engineer, AI Engineering: Duo Chat "
        "[Remote, Canada; Remote, United States]",
    ]


def test_le_second_run_n_a_plus_rien_a_classer(monkeypatch, capsys) -> None:
    from tests.test_dedup import _run_collecte

    _run_collecte(monkeypatch, capsys)
    code, sortie = _run_collecte(monkeypatch, capsys)

    assert code == 0
    assert "0 offre(s) classée(s)\n" in sortie


# ---------------------------------------------------------------------------
# Bonus de stack
# ---------------------------------------------------------------------------

STACK = TechBonusConfig(
    points={"python": 10, "typescript": 10, "k8s": 5, "go": 5},
    max=25,
)
SCORER_STACK = GeoScorer.from_config(ScoringConfig(geo_priority=PRIORITES, tech_bonus=STACK))


def test_chaque_techno_de_la_stack_ajoute_ses_points_au_score_geo() -> None:
    note = SCORER_STACK.noter(job("Remote (Europe)", tech=("Go", "Python", "React")))

    assert (note.score, note.bonus, note.stack) == (75, 15, ("Go", "Python"))
    assert (note.categorie, note.detail) == ("europe", "europe")


def test_un_alias_de_la_configuration_rapporte_sur_le_nom_canonique() -> None:
    note = SCORER_STACK.noter(job("Worldwide", tech=("Kubernetes",)))

    assert (note.score, note.stack) == (105, ("Kubernetes",))


def test_le_bonus_est_plafonne() -> None:
    note = SCORER_STACK.noter(job("Worldwide", tech=("Go", "Kubernetes", "Python", "TypeScript")))

    assert (note.score, note.bonus) == (125, 25)
    assert note.stack == ("Go", "Kubernetes", "Python", "TypeScript")


def test_sans_plafond_tout_compte() -> None:
    scorer = GeoScorer.from_config(
        ScoringConfig(tech_bonus=TechBonusConfig(points={"python": 10, "go": 10}))
    )

    assert scorer.noter(job("Mars", "onsite", tech=("Go", "Python"))).score == 20


def test_une_offre_hors_stack_garde_son_score_geo() -> None:
    note = SCORER_STACK.noter(job("Remote (Europe)", tech=("Java", "Spring")))

    assert note == SCORER.noter(job("Remote (Europe)", tech=("Java", "Spring")))
    assert (note.score, note.bonus, note.stack) == (60, 0, ())


def test_le_motif_nomme_la_zone_puis_la_stack() -> None:
    assert SCORER_STACK.noter(job("London", "onsite", tech=("Python",))).motif == (
        "europe (london) · stack +10 (Python)"
    )
    assert SCORER_STACK.noter(job("Mars", "onsite", tech=("Go",))).motif == "default · stack +5 (Go)"
    assert SCORER_STACK.noter(job("Mars", "onsite")).motif == "default"


def test_la_stack_peut_faire_passer_une_zone_moins_prioritaire_devant() -> None:
    asie = job("Tokyo", "onsite", id="asie")
    europe = job("Berlin", "onsite", id="europe", tech=("Python", "TypeScript", "Go"))

    assert ids(SCORER_STACK.classer([asie, europe])) == ["europe", "asie"]


def test_une_techno_du_bonus_inconnue_du_detecteur_est_detectee_et_rapporte() -> None:
    """Sans extension du vocabulaire, « Supabase » ne serait jamais dans
    `tech[]` et son bonus ne rapporterait jamais rien, sans un mot."""
    from src.core.retention import RetentionFilter

    scoring = ScoringConfig(tech_bonus=TechBonusConfig(points={"Supabase": 10}))
    filtre = RetentionFilter.from_config(RetentionConfig(), termes_bonus=scoring.tech_bonus.points)
    scorer = GeoScorer.from_config(scoring, vocabulaire=filtre.vocabulaire)
    brut = RawJob(
        id="1",
        ats="greenhouse",
        entreprise="Acme",
        titre="Backend Engineer",
        localisation="Mars",
        remote_type="onsite",
        url="https://boards.greenhouse.io/acme/jobs/1",
        date="2026-09-14T08:00:00+00:00",
        description="We run on supabase.",
    )

    (offre,) = normalize([brut], vocabulaire=filtre.vocabulaire)

    assert scorer.noter(offre).stack == ("Supabase",)


def test_la_configuration_versionnee_recompense_la_stack_du_candidat() -> None:
    filtres = load_filters(REAL_FILTERS)
    scorer = GeoScorer.from_config(filtres.scoring)

    assert scorer.noter(job("Mars", "onsite", tech=("Next.js", "Node.js"))).bonus == 20
    assert scorer.noter(job("Mars", "onsite", tech=("Kubernetes",))).bonus == 5
    assert scorer.noter(job("Mars", "onsite", tech=("Java",))).bonus == 1
    assert scorer.noter(job("Mars", "onsite", tech=("React",))).bonus == 10
    assert scorer.noter(job("Mars", "onsite", tech=("Vue",))).bonus == 3
    assert scorer.bonus_max is not None
    tout = tuple(scorer.bonus_tech)
    assert scorer.noter(job("Mars", "onsite", tech=tout)).bonus == scorer.bonus_max


def test_toute_techno_detectee_rapporte_des_points_dans_la_configuration_versionnee() -> None:
    """Une techno ajoutée au détecteur sans être notée rapporterait 0 sans
    que rien ne le signale."""
    scorer = GeoScorer.from_config(load_filters(REAL_FILTERS).scoring)

    assert set(TECH_VOCABULARY) <= set(scorer.bonus_tech)
    assert all(points >= 1 for points in scorer.bonus_tech.values())


def test_le_journal_du_run_s_arrete_au_top_et_compte_le_reste() -> None:
    from src.collect import _lignes_classement

    classement = SCORER.classer([job("Worldwide", id=str(n)) for n in range(13)])

    lignes = _lignes_classement(classement, limite=10)

    assert len(lignes) == 11
    assert lignes[-1] == "  … et 3 autre(s)"


def test_des_priorites_illisibles_arretent_le_run_avant_la_premiere_requete(
    monkeypatch, capsys, tmp_path
) -> None:
    from src import collect

    filtres = tmp_path / "filters.yaml"
    filtres.write_text(
        "retention: {}\nscoring:\n  geo_priority:\n    europe: soixante\n", encoding="utf-8"
    )
    monkeypatch.setenv("GITHUB_TOKEN", "jeton-de-test")

    def interdit(**kwargs):
        raise AssertionError("les sources ne doivent pas être chargées")

    monkeypatch.setattr(collect, "load_all", interdit)

    assert collect.main(["--filters", str(filtres)]) == 1
    assert "geo_priority.europe" in capsys.readouterr().err
