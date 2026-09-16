"""Tests de la normalisation `RawJob` → `Job` (US-3.1.T).

Trois familles de tests, et la troisième est celle qui compte :

1. le **mapping exact** d'une offre complète, champ à champ ;
2. les **défauts sûrs** quand la source est avare — jusqu'au cas limite de
   l'offre sans identité, qui est le seul que la normalisation refuse ;
3. le **passage des dix-huit adaptateurs**, sur leurs fixtures réelles.
   C'est la seule preuve de l'US-3.1.2 (« chaque adaptateur produit des
   `Job` valides ») qui ne se contente pas de le supposer : elle rejoue les
   réponses figées d'un ATS ou d'un agrégateur, et vérifie ce qui en sort.
   Brancher un adaptateur de plus dans `adapter_cases.ALL_ADAPTERS` le
   soumet à ce test sans une ligne à écrire ici.
"""

from __future__ import annotations

import logging
from dataclasses import asdict

import pytest

from src.adapters.base import RawJob, to_iso_utc
from src.core.normalize import (
    REMOTE_TYPES,
    TECH_VOCABULARY,
    Job,
    extract_tech,
    normalize,
    normalize_one,
    strip_markup,
)
from tests.adapter_cases import ALL_ADAPTER_IDS, ALL_ADAPTERS, fetch_case

#: Le schéma unifié : exactement les champs de l'US-3.1.1.
JOB_KEYS = {
    "id",
    "source",
    "entreprise",
    "titre",
    "localisation",
    "remote_type",
    "url",
    "date",
    "description",
    "tech",
}

COMPLET = RawJob(
    id="4212345",
    ats="greenhouse",
    entreprise="GitLab",
    titre="Senior Backend Engineer",
    localisation="Remote, EMEA",
    remote_type="remote",
    url="https://boards.greenhouse.io/gitlab/jobs/4212345",
    date="2026-07-08T06:30:39-04:00",
    description="Vous rejoignez une équipe distribuée.",
)


def _vide(**surcharges) -> RawJob:
    """Un `RawJob` dont tous les champs sont vides, sauf ceux passés."""
    champs = {nom: "" for nom in asdict(COMPLET)}
    return RawJob(**{**champs, **surcharges})


# ---------------------------------------------------------------------------
# US-3.1.1 — le schéma unifié
# ---------------------------------------------------------------------------


def test_le_schema_unifie_porte_exactement_les_champs_annonces() -> None:
    assert set(asdict(normalize_one(COMPLET))) == JOB_KEYS


def test_un_job_est_hashable_donc_utilisable_en_cle_de_dedup() -> None:
    """`tech` est un tuple et non une liste : c'est ce qui rend `Job`
    hashable, ce dont la Feature 3.3 se servira."""
    job = normalize_one(COMPLET)

    assert isinstance(job.tech, tuple)
    assert len({job, normalize_one(COMPLET)}) == 1


def test_les_trois_ingredients_de_l_id_stable_survivent_a_la_normalisation() -> None:
    """`ats:entreprise:job_id` (US-3.3.1) doit rester constructible."""
    job = normalize_one(COMPLET)

    assert f"{job.source}:{job.entreprise}:{job.id}" == "greenhouse:GitLab:4212345"


# ---------------------------------------------------------------------------
# US-3.1.T — mapping exact d'une offre complète
# ---------------------------------------------------------------------------


def test_un_rawjob_complet_est_mappe_champ_a_champ() -> None:
    job = normalize_one(COMPLET)

    assert job.id == "4212345"
    # `ats` devient `source` : c'est le seul champ renommé du schéma.
    assert job.source == "greenhouse"
    assert job.entreprise == "GitLab"
    assert job.titre == "Senior Backend Engineer"
    assert job.localisation == "Remote, EMEA"
    assert job.remote_type == "remote"
    assert job.url == "https://boards.greenhouse.io/gitlab/jobs/4212345"
    assert job.date == "2026-07-08T10:30:39+00:00"
    assert job.description == "Vous rejoignez une équipe distribuée."
    assert job.tech == ()


def test_normalize_conserve_l_ordre_des_offres() -> None:
    bruts = [
        _vide(id=str(numero), ats="greenhouse", url=f"https://x/{numero}")
        for numero in range(5)
    ]

    assert [job.id for job in normalize(bruts)] == ["0", "1", "2", "3", "4"]


# ---------------------------------------------------------------------------
# US-3.1.2 — champs manquants, défauts sûrs
# ---------------------------------------------------------------------------


def test_un_rawjob_presque_vide_donne_un_job_valide() -> None:
    job = normalize_one(_vide(id="42", ats="lever", url="https://jobs.lever.co/x/42"))

    assert job.est_valide
    assert job.entreprise == ""
    assert job.titre == ""
    assert job.localisation == ""
    assert job.date == ""
    assert job.description == ""
    assert job.tech == ()
    # Aucun champ ne vaut `None` : un manque est une chaîne vide.
    assert all(valeur is not None for valeur in asdict(job).values())


def test_un_remote_type_absent_est_deduit_de_la_localisation() -> None:
    job = normalize_one(_vide(id="1", url="https://x/1", localisation="Remote (Japan)"))

    assert job.remote_type == "remote"


def test_un_remote_type_hors_vocabulaire_ne_traverse_pas_le_pipeline() -> None:
    """« flexible » ne correspondrait à aucune règle du filtre de localisation :
    on le rejoue depuis la localisation plutôt que de le propager."""
    job = normalize_one(
        _vide(id="1", url="https://x/1", remote_type="flexible", localisation="Paris")
    )

    assert job.remote_type == "unknown"
    assert job.remote_type in REMOTE_TYPES


def test_sans_localisation_ni_mode_de_travail_le_defaut_est_unknown() -> None:
    assert normalize_one(_vide(id="1", url="https://x/1")).remote_type == "unknown"


def test_un_titre_multiligne_est_aplati_sur_une_seule_ligne() -> None:
    """Les flux RSS indentent leurs `<title>` : le tableau de revue, non."""
    job = normalize_one(
        _vide(id="1", url="https://x/1", titre="  Senior\n   Backend\tEngineer  ")
    )

    assert job.titre == "Senior Backend Engineer"


def test_une_offre_sans_id_ni_url_est_ecartee_et_journalisee(caplog) -> None:
    """Hors DoD : sans identité, l'offre serait republiée à chaque run."""
    with caplog.at_level(logging.WARNING):
        assert normalize_one(_vide(ats="remoteok", titre="Backend Engineer")) is None

    assert any("écartée" in enregistrement.getMessage() for enregistrement in caplog.records)


def test_une_offre_sans_id_mais_avec_url_recoit_une_identite_derivee(caplog) -> None:
    with caplog.at_level(logging.WARNING):
        job = normalize_one(_vide(ats="nodesk", url="https://nodesk.co/jobs/abc"))

    assert job is not None
    assert job.id
    assert job.est_valide
    assert any("dérivée" in enregistrement.getMessage() for enregistrement in caplog.records)


def test_l_identite_derivee_est_stable_et_distingue_deux_urls() -> None:
    """Un hash instable ferait republier l'offre à chaque run (Feature 3.3)."""
    premier = normalize_one(_vide(url="https://nodesk.co/jobs/abc"))
    second = normalize_one(_vide(url="https://nodesk.co/jobs/abc"))
    autre = normalize_one(_vide(url="https://nodesk.co/jobs/def"))

    assert premier.id == second.id == "92000bc39691"
    assert premier.id != autre.id


def test_les_offres_ecartees_ne_comptent_pas_dans_le_resultat() -> None:
    bruts = [COMPLET, _vide(titre="offre fantôme"), COMPLET]

    assert len(normalize(bruts)) == 2


# ---------------------------------------------------------------------------
# US-3.1.T — description nettoyée
# ---------------------------------------------------------------------------


def test_une_description_html_ressort_en_texte_sans_balise() -> None:
    html = "<div><p>Stack :</p><ul><li>Python</li><li>Kotlin</li></ul></div>"

    description = normalize_one(_vide(id="1", url="https://x/1", description=html)).description

    assert "<" not in description
    assert ">" not in description
    # Le nettoyage garde la structure : une puce reste une puce.
    assert description == "Stack :\n\n- Python\n- Kotlin"


def test_une_description_html_echappee_est_nettoyee_aussi() -> None:
    """Greenhouse sert son `content` doublement encodé."""
    brut = _vide(id="1", url="https://x/1", description="&lt;p&gt;Remote &amp;amp; async&lt;/p&gt;")

    assert normalize_one(brut).description == "Remote & async"


def test_un_texte_deja_propre_traverse_la_normalisation_intact() -> None:
    """Le piège du double nettoyage : `HTMLParser` mangerait « <5 ms ».

    Repasser `html_to_text` sur une description déjà nettoyée par
    l'adaptateur amputerait la phrase au premier `<` suivi d'une lettre.
    D'où la condition de `strip_markup` — et ce test, qui la garde.
    """
    texte = "Latence p99 <5 ms sur 3 régions.\n\n- Python\n- Go"

    assert normalize_one(_vide(id="1", url="https://x/1", description=texte)).description == texte


def test_strip_markup_ne_touche_pas_a_un_texte_sans_balise() -> None:
    assert strip_markup("a < b et b > c") == "a < b et b > c"
    assert strip_markup("") == ""


# ---------------------------------------------------------------------------
# US-3.1.T — dates de formats variés, un seul format en sortie
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "brute, attendue",
    [
        # ISO avec offset (Greenhouse).
        ("2026-07-08T06:30:39-04:00", "2026-07-08T10:30:39+00:00"),
        # ISO suffixé Z, avec millisecondes (SmartRecruiters, Workable).
        ("2026-09-05T03:52:53.056Z", "2026-09-05T03:52:53+00:00"),
        # RFC-822 d'un `<pubDate>` RSS (We Work Remotely, NoDesk).
        ("Mon, 17 Aug 2026 19:21:19 +0000", "2026-08-17T19:21:19+00:00"),
        # Timestamp epoch servi comme chaîne, en secondes puis en
        # millisecondes (Lever).
        ("1785760723", "2026-08-03T12:38:43+00:00"),
        ("1785760723455", "2026-08-03T12:38:43+00:00"),
        # Date seule, sans heure (schema.org `datePosted`).
        ("2026-04-08", "2026-04-08T00:00:00+00:00"),
        # Illisible ou absente : chaîne vide, jamais d'exception.
        ("hier", ""),
        ("", ""),
    ],
)
def test_toute_date_lisible_ressort_dans_un_format_unique(brute, attendue) -> None:
    assert normalize_one(_vide(id="1", url="https://x/1", date=brute)).date == attendue


def test_une_annee_seule_n_est_pas_lue_comme_un_timestamp() -> None:
    """Garde-fou du seuil de neuf chiffres : « 2026 » vaudrait 1970."""
    assert to_iso_utc("2026") == ""


def test_la_date_deja_normalisee_ne_bouge_plus() -> None:
    """Idempotence : c'est ce qui autorise à repasser la normalisation."""
    une_fois = normalize_one(_vide(id="1", url="https://x/1", date="2026-07-08T06:30:39-04:00"))
    deux_fois = normalize_one(_vide(id="1", url="https://x/1", date=une_fois.date))

    assert deux_fois.date == une_fois.date


# ---------------------------------------------------------------------------
# US-3.1.1 — le champ `tech[]`, seul champ déduit
# ---------------------------------------------------------------------------


def test_la_stack_est_lue_dans_le_titre_et_dans_la_description() -> None:
    job = normalize_one(
        _vide(
            id="1",
            url="https://x/1",
            titre="Senior Python Engineer",
            description="Stack : PostgreSQL, Kubernetes et un peu de Terraform.",
        )
    )

    assert job.tech == ("Kubernetes", "PostgreSQL", "Python", "Terraform")


def test_la_stack_est_triee_et_sans_doublon() -> None:
    """Ordre stable : `tech[]` finira dans un fichier versionné."""
    tech = extract_tech("Rust, rust, RUST et Docker", "docker, Rust")

    assert tech == ("Docker", "Rust")


@pytest.mark.parametrize(
    "texte, attendu",
    [
        # Le mot ambigu en minuscules : de l'anglais courant, pas une stack.
        ("we go to production twice a day", ()),
        ("the rest of the team is in Europe", ()),
        # Le même écrit comme la techno : reconnu.
        ("Go and Rust services", ("Go", "Rust")),
        ("Golang microservices", ("Go",)),
        ("REST APIs and gRPC", ("gRPC", "REST")),
        # Un mot plus long ne doit pas déclencher le mot court.
        ("JavaScript everywhere", ("JavaScript",)),
        ("Java 21 and Spring Boot", ("Java", "Spring")),
        ("NoSQL stores", ("NoSQL",)),
        ("NoSQL and SQL", ("NoSQL", "SQL")),
        # Ponctuation et versions collées au terme.
        ("python3, C++11, k8s", ("C++", "Kubernetes", "Python")),
        ("Node.js / NestJS", ("NestJS", "Node.js")),
        ("Senior Backend Engineer (TS/Node)", ("Node.js", "TypeScript")),
        # En minuscules, ce sont des mots courants : pas une stack.
        ("each node of the graph, ts timestamps", ()),
        (".NET 8 sur Azure", (".NET", "Azure")),
        # Rien de reconnu : tuple vide, jamais `None`.
        ("Nous cherchons quelqu'un de curieux.", ()),
        ("", ()),
    ],
)
def test_le_detecteur_de_stack_distingue_la_techno_de_l_anglais_courant(texte, attendu) -> None:
    assert extract_tech(texte) == attendu


def test_le_nom_de_l_entreprise_gitlab_ne_devient_pas_une_techno() -> None:
    """GitLab est la première ligne de `sources.yaml` : si `gitlab` était une
    variante de Git, chacune de ses offres porterait une techno inventée."""
    assert extract_tech("GitLab Backend Engineer", "We use GitHub Actions") == ()
    assert extract_tech("Git, Docker, Linux") == ("Docker", "Git", "Linux")


def test_un_vocabulaire_sur_mesure_remplace_celui_du_module() -> None:
    """La porte d'entrée de la Feature 3.2.4, qui y ajoutera
    `retention.tech_include_any`."""
    maison = {"COBOL": ("cobol",)}

    assert extract_tech("Python et COBOL", vocabulaire=maison) == ("COBOL",)
    assert normalize_one(
        _vide(id="1", url="https://x/1", titre="COBOL Engineer"), vocabulaire=maison
    ).tech == ("COBOL",)


def test_deux_vocabulaires_sur_mesure_ne_se_confondent_pas_apres_un_gc() -> None:
    """Le cache des motifs est indexé sur le **contenu** du vocabulaire.

    L'indexer sur l'identité de l'objet (`id()`) paraît naturel et se
    trompe rarement — jusqu'à ce que le ramasse-miettes réattribue
    l'adresse d'un dict libéré à un autre, et qu'un run cherche les
    technologies de quelqu'un d'autre sans que rien ne le signale.
    """
    import gc

    for _ in range(3):
        premier = {"COBOL": ("cobol",)}
        assert extract_tech("Python et COBOL", vocabulaire=premier) == ("COBOL",)
        del premier
        gc.collect()

        second = {"Fortran": ("fortran",)}
        assert extract_tech("COBOL et Fortran", vocabulaire=second) == ("Fortran",)
        del second
        gc.collect()


def test_le_vocabulaire_ne_contient_pas_de_nom_canonique_en_double() -> None:
    """Garde-fou : deux entrées de même nom se masqueraient silencieusement."""
    assert len(TECH_VOCABULARY) == len(set(TECH_VOCABULARY))
    assert all(TECH_VOCABULARY.values()), "toute techno doit avoir au moins une variante"


# ---------------------------------------------------------------------------
# US-3.1.2 — les dix-huit adaptateurs, sur leurs fixtures réelles
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("case", ALL_ADAPTERS, ids=ALL_ADAPTER_IDS)
def test_chaque_adaptateur_produit_des_jobs_valides(case) -> None:
    bruts = fetch_case(case)
    jobs = normalize(bruts)

    assert jobs, "la fixture doit contenir au moins une offre"
    # Aucune offre réelle n'est perdue en route : celles qui le seraient
    # n'auraient ni identifiant ni URL, ce qu'aucun adaptateur ne produit.
    assert len(jobs) == len(bruts)

    for job in jobs:
        assert isinstance(job, Job)
        assert set(asdict(job)) == JOB_KEYS
        assert job.est_valide
        assert job.source == case.adapter_cls.ats
        assert job.remote_type in REMOTE_TYPES
        assert isinstance(job.tech, tuple)
        assert all(isinstance(nom, str) for nom in job.tech)
        # La description ne porte plus de balise, et la date est unique.
        assert "<p" not in job.description and "</" not in job.description
        assert job.date == to_iso_utc(job.date)
        # Le titre tient sur une ligne : le tableau de revue l'affiche tel quel.
        assert "\n" not in job.titre


@pytest.mark.parametrize("case", ALL_ADAPTERS, ids=ALL_ADAPTER_IDS)
def test_chaque_adaptateur_donne_le_meme_jeu_de_cles(case) -> None:
    """Le schéma unifié l'est vraiment : dix-huit sources, un seul jeu de clés."""
    assert {frozenset(asdict(job)) for job in normalize(fetch_case(case))} == {
        frozenset(JOB_KEYS)
    }


def test_la_normalisation_detecte_une_vraie_stack_sur_une_offre_reelle() -> None:
    """Test de régression : une offre capturée en vrai, pas fabriquée."""
    cas = next(case for case in ALL_ADAPTERS if case.adapter_cls.ats == "greenhouse")
    jobs = normalize(fetch_case(cas))

    assert any(job.tech for job in jobs), (
        "aucune des offres GitLab ne mentionne une techno connue — "
        "le vocabulaire ou l'extraction a régressé"
    )


# ---------------------------------------------------------------------------
# US-3.1.2 — la normalisation est branchée dans le run, pas seulement écrite
# ---------------------------------------------------------------------------


def test_le_run_de_collecte_normalise_ce_qu_il_a_collecte(monkeypatch, capsys) -> None:
    """Sans ce test, le normaliseur pourrait être parfait et jamais appelé."""
    from src import collect
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

    code = collect.main(["--cadence", "fast", "--sans-publication"], client=client)

    sortie = capsys.readouterr().out
    assert code == 0
    # Les neuf offres brutes des trois fixtures deviennent neuf `Job`.
    assert "9 offre(s) brutes" in sortie
    assert "9 offre(s) normalisée(s)" in sortie
    # Rien n'a été perdu à la normalisation : le suffixe « sans identité »
    # ne s'affiche que s'il y a une perte. (Le filtre de rétention, lui,
    # écarte bel et bien — c'est son travail, et une autre ligne du bilan.)
    assert "sans identité" not in sortie
