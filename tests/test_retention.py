"""Tests du filtre de rétention (US-3.2.T).

Le cœur de la sélection, donc le module où une régression coûte le plus
cher : un filtre trop large noie le tableau de revue, un filtre trop
étroit le vide sans rien dire. D'où une **table de cas** — titre,
localisation, séniorité → verdict attendu, écrit en toutes lettres —
plutôt qu'une poignée d'assertions dispersées : ajouter un cas est une
ligne, et la table se lit comme la spécification qu'elle est.

La configuration de la table (`CONFIG_DOD`) est écrite ici, pas lue dans
`config/filters.yaml`. Deux raisons : un test qui dépend du fichier du
dépôt change de verdict dès qu'on ajuste un terme de recherche d'emploi,
et surtout la table doit pouvoir exercer des règles que la configuration
réelle n'active pas — `staff` en `drop`, la France **hors** des zones de
relocalisation. Le fichier réel a son propre test, plus bas : il doit se
charger et laisser passer une offre cible réelle.
"""

from __future__ import annotations

from dataclasses import dataclass

import pytest

from src.core.config import (
    LocationFilter,
    RetentionConfig,
    SeniorityFilter,
    load_filters,
)
from src.core.normalize import TECH_VOCABULARY, Job, normalize
from src.core.retention import (
    LOCALISATION,
    MOTIFS,
    SENIORITE,
    TECH,
    TITRE_EXCLU,
    TITRE_NON_INCLUS,
    RetentionFilter,
    apply_retention,
    tech_vocabulary,
)
from tests.adapter_cases import ALL_ADAPTERS, fetch_case

REAL_FILTERS = "config/filters.yaml"

#: La configuration de référence de la table de cas.
#:
#: Elle diffère volontairement de `config/filters.yaml` sur deux points,
#: qui sont exactement ceux que la DoD demande d'éprouver :
#:
#: - `staff` est en `drop` (le dépôt le garde) — pour prouver que `drop`
#:   l'emporte, il faut un niveau que la configuration rejette ;
#: - la France n'est **pas** une zone de relocalisation (le dépôt l'accepte)
#:   — « On-site Paris rejeté, hors option France ».
#:
#: `swe` est ajouté aux titres inclus pour que « Staff SWE » soit jugé sur
#: sa séniorité et non recalé plus tôt sur son titre : un cas de test ne
#: prouve rien s'il tombe avant d'atteindre la règle visée.
CONFIG_DOD = RetentionConfig(
    title_include=["software engineer", "backend engineer", "developer", "swe"],
    title_exclude=["sales engineer", "engineering manager", "support engineer"],
    seniority=SeniorityFilter(keep=["mid", "senior"], drop=["intern", "junior", "staff"]),
    location=LocationFilter(require_any=["remote"], relocation_regions=["asia", "europe"]),
    tech_include_any=[],
)

FILTRE = RetentionFilter.from_config(CONFIG_DOD)


def job(
    titre: str = "Software Engineer",
    localisation: str = "Remote",
    remote_type: str = "remote",
    tech: tuple[str, ...] = (),
) -> Job:
    """Une offre normalisée réduite à ce que le filtre regarde."""
    return Job(
        id="1",
        source="greenhouse",
        entreprise="Acme",
        titre=titre,
        localisation=localisation,
        remote_type=remote_type,
        url="https://boards.greenhouse.io/acme/jobs/1",
        date="2026-09-01T00:00:00+00:00",
        description="",
        tech=tech,
    )


# ---------------------------------------------------------------------------
# La table : (titre, localisation, séniorité) → verdict attendu
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Cas:
    titre: str
    localisation: str
    remote_type: str
    gardee: bool
    #: Motif attendu, `""` si l'offre est retenue. Le vérifier interdit
    #: qu'un cas passe « par accident », rejeté par la mauvaise règle.
    motif: str
    pourquoi: str


CAS: tuple[Cas, ...] = (
    # -- le titre : ce qu'on cherche, ce qu'on refuse ------------------------
    Cas("Senior Software Engineer", "Remote", "remote", True, "",
        "le poste cible, dans sa forme la plus courante"),
    Cas("Sales Engineer", "Remote", "remote", False, TITRE_EXCLU,
        "« engineer » ne suffit pas : la vente est explicitement exclue"),
    Cas("Engineering Manager", "Remote", "remote", False, TITRE_EXCLU,
        "manager d'équipe, pas ingénieur"),
    Cas("Support Engineer", "Remote", "remote", False, TITRE_EXCLU,
        "troisième terme d'exclusion, pour que la liste soit lue en entier"),
    Cas("Senior Sales Engineer", "Remote", "remote", False, TITRE_EXCLU,
        "l'exclusion l'emporte sur l'inclusion ET sur une séniorité gardée"),
    Cas("software engineer", "Remote", "remote", True, "",
        "casse : tout en minuscules"),
    Cas("SOFTWARE ENGINEER", "Remote", "remote", True, "",
        "casse : tout en majuscules"),
    Cas("Data Scientist", "Remote", "remote", False, TITRE_NON_INCLUS,
        "un poste tech qui n'est pas le poste cherché"),
    Cas("Product Manager", "Remote", "remote", False, TITRE_NON_INCLUS,
        "hors périmètre, et non couvert par la liste d'exclusion"),
    Cas("", "Remote", "remote", False, TITRE_NON_INCLUS,
        "sans titre, on ne sait pas ce qu'on publierait"),
    # -- la séniorité --------------------------------------------------------
    Cas("Staff SWE", "Remote", "remote", False, SENIORITE,
        "« staff » est en drop : rejeté même si le titre est le bon"),
    Cas("Software Engineer II", "Remote", "remote", True, "",
        "le chiffre romain vaut « mid », qui est en keep"),
    Cas("Software Engineer Intern", "Remote", "remote", False, SENIORITE,
        "« intern » est en drop"),
    Cas("Junior Backend Engineer", "Remote", "remote", False, SENIORITE,
        "« junior » est en drop"),
    Cas("Backend Engineer", "Remote", "remote", True, "",
        "aucun marqueur de séniorité : le doute profite à l'offre"),
    # -- la localisation -----------------------------------------------------
    Cas("Backend Developer", "Remote (Asia)", "remote", True, "",
        "remote pur, et la zone est acceptée"),
    Cas("Backend Developer", "On-site Paris", "onsite", False, LOCALISATION,
        "ni remote ni zone acceptée — la France n'est pas une option ici"),
    Cas("Backend Developer", "Tokyo, JP", "onsite", True, "",
        "sur site, mais dans une zone de relocalisation acceptée"),
    Cas("Backend Developer", "Berlin, Germany", "onsite", True, "",
        "idem pour l'Europe"),
    Cas("Backend Developer", "New York, NY", "onsite", False, LOCALISATION,
        "sur site hors de toute zone acceptée"),
    Cas("Backend Developer", "Anywhere", "remote", True, "",
        "la localisation ne dit pas « remote », le champ dédié si"),
)

IDS = [f"{cas.titre or '(sans titre)'} @ {cas.localisation}" for cas in CAS]


@pytest.mark.parametrize("cas", CAS, ids=IDS)
def test_la_table_des_verdicts_attendus(cas: Cas) -> None:
    verdict = FILTRE.juge(job(cas.titre, cas.localisation, cas.remote_type))

    assert verdict.gardee is cas.gardee, cas.pourquoi
    assert verdict.motif == cas.motif, cas.pourquoi


def test_la_table_couvre_au_moins_quinze_cas() -> None:
    """Garde-fou : la DoD en demande quinze, et une table qui maigrit passe
    inaperçue — chaque cas supprimé fait toujours « tous les tests verts »."""
    assert len(CAS) >= 15
    assert len({(cas.titre, cas.localisation, cas.remote_type) for cas in CAS}) == len(CAS)


# ---------------------------------------------------------------------------
# US-3.2.1 — le titre
# ---------------------------------------------------------------------------


def test_la_casse_du_titre_ne_change_jamais_le_verdict() -> None:
    for titre in ("software engineer", "Software Engineer", "SOFTWARE ENGINEER"):
        assert FILTRE.retient(job(titre))
    for titre in ("sales engineer", "Sales Engineer", "SALES ENGINEER"):
        assert not FILTRE.retient(job(titre))


def test_l_exclusion_est_prioritaire_sur_l_inclusion() -> None:
    """« Sales Engineer » contient « engineer » : sans priorité, il passerait."""
    verdict = FILTRE.juge(job("Sales Engineer, Backend"))

    assert verdict.motif == TITRE_EXCLU
    assert verdict.detail == "sales engineer"


def test_une_liste_include_vide_laisse_passer_tous_les_titres() -> None:
    """Comportement documenté de `filters.yaml` : vide = aucune contrainte."""
    filtre = RetentionFilter(title_exclude=("sales engineer",))

    assert filtre.retient(job("Chef de projet"))
    assert not filtre.retient(job("Sales Engineer"))


def test_le_terme_est_cherche_en_sous_chaine_pas_en_mot_entier() -> None:
    """« developer » doit trouver « Developers » et « Fullstack-Developer » :
    une liste écrite à la main ne décline pas les pluriels."""
    filtre = RetentionFilter(title_include=("developer",))

    assert filtre.retient(job("Fullstack-Developer (m/w/d)"))
    assert filtre.retient(job("Backend Developers Wanted"))


# ---------------------------------------------------------------------------
# US-3.2.2 — la séniorité
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "titre, gardee",
    [
        ("Staff SWE", False),
        ("Software Engineer II", True),
        ("Intern Software Engineer", False),
        ("Software Engineer Internship", False),
        ("Senior Software Engineer", True),
        ("Sr. Software Engineer", True),
        ("Software Engineer, Jr.", False),
        ("Backend Engineer", True),
        ("Software Engineer III", True),
        ("Software Engineer I", False),
    ],
)
def test_les_niveaux_lisibles_dans_un_titre(titre: str, gardee: bool) -> None:
    assert FILTRE.retient(job(titre)) is gardee


def test_drop_l_emporte_sur_keep() -> None:
    """`filters.yaml` : « à toujours rejeter, même s'ils figurent aussi dans keep »."""
    filtre = RetentionFilter(seniority_keep=("senior", "staff"), seniority_drop=("staff",))

    assert filtre.retient(job("Senior Software Engineer"))
    assert not filtre.retient(job("Senior Staff Software Engineer"))


def test_un_titre_sans_marqueur_n_est_pas_rejete_faute_de_preuve() -> None:
    """Le choix structurant du module : la majorité des titres n'affichent
    aucun niveau, et les rejeter viderait le tableau."""
    filtre = RetentionFilter(seniority_keep=("mid", "senior", "staff"))

    assert filtre.retient(job("Backend Engineer"))
    assert filtre.retient(job("Software Engineer, Platform"))


@pytest.mark.parametrize("titre, niveau", [
    ("Principal Software Engineer", "principal"),
    ("Tech Lead Backend Engineer", "lead"),
    ("Software Engineering Director", "manager"),
])
def test_un_niveau_detecte_mais_hors_keep_est_rejete(titre: str, niveau: str) -> None:
    """L'autre versant, et le prix du choix : quand le titre *dit* un niveau
    que `keep` ne cite pas, l'offre tombe. C'est ce que demande la recherche
    Google de `website.md` (`-manager -director`), et c'est aussi ce qui
    emporte « Tech Lead » — un mot ajouté à `keep` suffit à le récupérer."""
    filtre = RetentionFilter(seniority_keep=("mid", "senior"))
    verdict = filtre.juge(job(titre))

    assert not verdict.gardee
    assert verdict.motif == SENIORITE
    assert niveau in verdict.detail


def test_un_niveau_inconnu_du_module_reste_utilisable_en_config() -> None:
    """`drop: ["vp"]` doit marcher sans que le module connaisse « vp »."""
    filtre = RetentionFilter(seniority_drop=("vp",))

    assert not filtre.retient(job("VP Engineering"))
    assert filtre.retient(job("Software Engineer"))


def test_la_seniorite_se_lit_dans_le_titre_pas_dans_la_description() -> None:
    """« vous encadrerez des ingénieurs juniors » ne fait pas une offre junior —
    lire la description rejetterait exactement les offres seniors cherchées."""
    offre = Job(
        id="1",
        source="greenhouse",
        entreprise="Acme",
        titre="Senior Software Engineer",
        localisation="Remote",
        remote_type="remote",
        url="https://x/1",
        date="",
        description="Vous encadrerez des développeurs junior et des stagiaires.",
        tech=(),
    )

    assert FILTRE.retient(offre)


def test_stage_seul_n_est_pas_un_marqueur_de_stage() -> None:
    """« Early Stage Startup » ne doit pas disparaître comme une offre de stage."""
    assert FILTRE.retient(job("Backend Engineer, Early Stage Startup"))
    assert not FILTRE.retient(job("Stagiaire Developer"))


# ---------------------------------------------------------------------------
# US-3.2.3 — localisation & remote
# ---------------------------------------------------------------------------


def test_le_champ_remote_type_compte_autant_que_le_texte_de_localisation() -> None:
    """Remotive et Japan Dev affichent « Anywhere » ou « Tokyo, JP » et
    déclarent le télétravail dans un champ dédié : ne lire que le texte
    perdrait ces offres."""
    filtre = RetentionFilter(location_require_any=("remote",))

    assert filtre.retient(job(localisation="Anywhere", remote_type="remote"))
    assert not filtre.retient(job(localisation="Anywhere", remote_type="onsite"))


def test_une_zone_de_relocalisation_couvre_ses_pays_et_ses_villes() -> None:
    """Sans la table des régions, `["asia"]` ne retiendrait aucune des
    290 offres de Japan Dev, qui disent toutes « Tokyo, JP »."""
    filtre = RetentionFilter(relocation_regions=("asia",))

    assert filtre.retient(job(localisation="Tokyo, JP", remote_type="onsite"))
    assert filtre.retient(job(localisation="Singapore", remote_type="onsite"))
    assert not filtre.retient(job(localisation="Austin, TX", remote_type="onsite"))


def test_les_zones_sont_disjointes_la_france_n_est_pas_dans_l_europe() -> None:
    """`geo_priority` leur donne deux scores distincts : les fondre rendrait
    impossible d'accepter l'une sans l'autre."""
    europe = RetentionFilter(relocation_regions=("europe",))
    france = RetentionFilter(relocation_regions=("france",))
    paris = job(localisation="Paris, FR", remote_type="onsite")

    assert not europe.retient(paris)
    assert france.retient(paris)
    assert europe.retient(job(localisation="Berlin", remote_type="onsite"))


def test_l_option_france_fait_basculer_le_verdict_d_une_offre_parisienne() -> None:
    """Le pendant exact du cas « On-site Paris rejeté (hors option France) »."""
    paris = job("Backend Developer", "On-site Paris", "onsite")

    assert not FILTRE.retient(paris)
    avec_france = RetentionFilter.from_config(
        RetentionConfig(
            title_include=list(CONFIG_DOD.title_include),
            location=LocationFilter(
                require_any=["remote"], relocation_regions=["asia", "europe", "france"]
            ),
        )
    )

    assert avec_france.retient(paris)


def test_un_code_pays_ne_matche_pas_au_milieu_d_un_mot() -> None:
    """La table contient « fr », « eu », « uk » : en sous-chaîne, ils
    tagueraient la moitié du monde."""
    filtre = RetentionFilter(relocation_regions=("france",))

    # « Somewhere » n'est dans aucune table : seul le code décide.
    assert filtre.retient(job(localisation="Somewhere, FR", remote_type="onsite"))
    assert not filtre.retient(job(localisation="Frankfurt", remote_type="onsite"))
    assert not filtre.retient(job(localisation="Freiburg", remote_type="onsite"))


def test_les_deux_listes_de_localisation_vides_ne_contraignent_rien() -> None:
    assert RetentionFilter().retient(job(localisation="Pyongyang", remote_type="onsite"))


# ---------------------------------------------------------------------------
# US-3.2.4 — la stack (optionnel)
# ---------------------------------------------------------------------------


def test_sans_tech_dans_la_config_aucune_offre_n_est_rejetee_pour_raison_tech() -> None:
    """La DoD : `tech_include_any` absent ou vide = no-op."""
    assert not CONFIG_DOD.tech_include_any
    assert FILTRE.retient(job("Software Engineer", tech=()))
    assert all(
        FILTRE.juge(job(cas.titre, cas.localisation, cas.remote_type)).motif != TECH
        for cas in CAS
    )


def test_le_filtre_tech_retient_l_offre_qui_mentionne_une_techno_cible() -> None:
    filtre = RetentionFilter(tech_include_any=("Python", "Go"))

    assert filtre.retient(job(tech=("Django", "Python")))
    assert not filtre.retient(job(tech=("Java", "Spring")))


def test_le_filtre_tech_nomme_ce_qu_il_attendait() -> None:
    verdict = RetentionFilter(tech_include_any=("Python",)).juge(job(tech=("Java",)))

    assert verdict.motif == TECH
    assert verdict.detail == "Python"


def test_un_alias_ecrit_dans_la_config_trouve_son_nom_canonique() -> None:
    """`tech_include_any: ["k8s"]` doit retenir une offre taguée `Kubernetes` :
    la config est écrite à la main, le détecteur produit des noms canoniques."""
    filtre = RetentionFilter(tech_include_any=("k8s", "postgres"))

    assert filtre.retient(job(tech=("Kubernetes",)))
    assert filtre.retient(job(tech=("PostgreSQL",)))


def test_un_terme_inconnu_du_vocabulaire_y_est_ajoute() -> None:
    """Sans ça, filtrer sur une techno absente de `TECH_VOCABULARY`
    rejetterait tout : le détecteur ne pourrait jamais la produire."""
    config = RetentionConfig(tech_include_any=["Zig", "python"])
    vocabulaire = tech_vocabulary(config)

    assert "Zig" in vocabulaire
    # « python » est déjà connu : rien n'est dupliqué sous un autre nom.
    assert "python" not in vocabulaire
    assert vocabulaire["Python"] == TECH_VOCABULARY["Python"]


def test_le_terme_ajoute_est_reellement_vu_par_le_detecteur() -> None:
    """Bout en bout : config → vocabulaire → `normalize` → `tech[]` → verdict."""
    from src.adapters.base import RawJob

    filtre = RetentionFilter.from_config(RetentionConfig(tech_include_any=["Zig"]))
    brut = RawJob(
        id="1",
        ats="greenhouse",
        entreprise="Acme",
        titre="Backend Engineer",
        localisation="Remote",
        remote_type="remote",
        url="https://x/1",
        date="",
        description="Notre stack est écrite en Zig et en Rust.",
    )

    (offre,) = normalize([brut], vocabulaire=filtre.vocabulaire)

    assert "Zig" in offre.tech
    assert filtre.retient(offre)


def test_un_vocabulaire_non_etendu_rendrait_la_techno_invisible() -> None:
    """Le contre-exemple qui justifie `from_config` : normaliser avec le
    vocabulaire du module rejetterait l'offre alors qu'elle correspond."""
    from src.adapters.base import RawJob

    brut = RawJob(
        id="1",
        ats="greenhouse",
        entreprise="Acme",
        titre="Backend Engineer",
        localisation="Remote",
        remote_type="remote",
        url="https://x/1",
        date="",
        description="Notre stack est écrite en Zig.",
    )
    filtre = RetentionFilter.from_config(RetentionConfig(tech_include_any=["Zig"]))

    (sans_extension,) = normalize([brut])

    assert "Zig" not in sans_extension.tech
    assert not filtre.retient(sans_extension)


# ---------------------------------------------------------------------------
# Le bilan du run
# ---------------------------------------------------------------------------


def test_le_bilan_compte_les_rejets_par_motif_dans_un_ordre_fixe() -> None:
    offres = [
        job("Sales Engineer"),
        job("Data Scientist"),
        job("Software Engineer Intern"),
        job("Backend Developer", "New York, NY", "onsite"),
        job("Senior Software Engineer"),
    ]

    bilan = FILTRE.appliquer(offres)

    assert [offre.titre for offre in bilan.retenues] == ["Senior Software Engineer"]
    assert bilan.total == 5
    assert bilan.par_motif == {
        TITRE_EXCLU: 1,
        TITRE_NON_INCLUS: 1,
        SENIORITE: 1,
        LOCALISATION: 1,
    }
    # L'ordre est celui de `MOTIFS`, pas celui d'apparition : deux runs se
    # comparent d'un coup d'œil.
    assert list(bilan.par_motif) == [m for m in MOTIFS if m in bilan.par_motif]
    assert "1 retenue(s) sur 5" in bilan.resume


def test_un_bilan_sans_rejet_ne_liste_aucun_motif() -> None:
    bilan = FILTRE.appliquer([job("Senior Software Engineer")])

    assert bilan.par_motif == {}
    assert bilan.resume == "1 retenue(s) sur 1"
    assert list(bilan) == bilan.retenues


def test_l_ordre_des_offres_retenues_est_celui_d_entree() -> None:
    """Le scoring (Feature 3.4) s'en sert pour départager deux ex æquo."""
    offres = [job(f"Software Engineer {n}") for n in ("Alpha", "Beta", "Gamma")]

    assert FILTRE.appliquer(offres).retenues == offres


def test_apply_retention_est_le_raccourci_equivalent() -> None:
    offres = [job("Senior Software Engineer"), job("Sales Engineer")]

    assert apply_retention(offres, CONFIG_DOD).retenues == [offres[0]]


def test_une_configuration_entierement_vide_ne_rejette_rien() -> None:
    """Le défaut sûr : une `retention:` muette ne doit pas vider le tableau."""
    bilan = apply_retention(
        [job("Chef de projet", "Pyongyang", "onsite")], RetentionConfig()
    )

    assert len(bilan.retenues) == 1
    assert not bilan.rejets


# ---------------------------------------------------------------------------
# Régression : la configuration réelle, sur des offres réelles
# ---------------------------------------------------------------------------


def test_une_offre_cible_reelle_passe_la_porte() -> None:
    """Test de régression demandé par la DoD : une offre copiée d'un ATS —
    ici les trois offres GitLab de la fixture Greenhouse — jugée par les
    règles réellement versionnées dans `config/filters.yaml`."""
    filtre = RetentionFilter.from_config(load_filters(REAL_FILTERS).retention)
    cas = next(case for case in ALL_ADAPTERS if case.adapter_cls.ats == "greenhouse")

    bilan = filtre.appliquer(normalize(fetch_case(cas), vocabulaire=filtre.vocabulaire))

    assert bilan.retenues, (
        "aucune offre GitLab ne passe le filtre de rétention — "
        "les règles réelles ou le filtre ont régressé"
    )
    retenue = bilan.retenues[0]
    assert "Backend Engineer" in retenue.titre
    assert retenue.remote_type == "remote"


def test_les_regles_reelles_ne_rejettent_pas_tout_ce_qui_est_collecte() -> None:
    """Garde-fou de bout en bout : sur les fixtures réelles des dix-huit
    adaptateurs, le filtre doit retenir quelque chose **et** rejeter quelque
    chose. Tout retenir ou tout rejeter sont les deux façons de casser ce
    module sans qu'aucun test unitaire ne s'en aperçoive."""
    filtre = RetentionFilter.from_config(load_filters(REAL_FILTERS).retention)
    offres = [
        offre
        for case in ALL_ADAPTERS
        for offre in normalize(fetch_case(case), vocabulaire=filtre.vocabulaire)
    ]

    bilan = filtre.appliquer(offres)

    assert bilan.retenues
    assert bilan.rejets
    assert bilan.total == len(offres)


# ---------------------------------------------------------------------------
# La rétention est branchée dans le run, pas seulement écrite
# ---------------------------------------------------------------------------


def test_le_run_de_collecte_applique_la_retention(monkeypatch, capsys) -> None:
    """Sans ce test, le filtre pourrait être parfait et jamais appelé."""
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
    assert "9 offre(s) normalisée(s)" in sortie
    # Cinq des neuf offres réelles passent les règles du dépôt — l'offre
    # Staff de Malt tombe sur la séniorité ; le décompte
    # par motif est affiché, sans quoi un filtre qui rejette tout
    # ressemblerait à un marché de l'emploi calme.
    assert "5 retenue(s) sur 9" in sortie
    assert f"{TITRE_NON_INCLUS} 3" in sortie


def test_des_regles_illisibles_arretent_le_run_avant_la_premiere_requete(
    monkeypatch, capsys, tmp_path
) -> None:
    """Collecter sans savoir quoi retenir n'a aucun intérêt, et trente-sept
    appels réseau pour rien encore moins."""
    from src import collect

    appels: list[object] = []
    monkeypatch.setenv("GITHUB_TOKEN", "jeton-de-test")
    monkeypatch.setattr(collect, "load_all", lambda **kwargs: appels.append(kwargs))

    code = collect.main(["--filters", str(tmp_path / "absent.yaml")])

    assert code == 1
    assert not appels, "aucune source ne doit être chargée, encore moins interrogée"
    assert "rétention illisibles" in capsys.readouterr().err
