"""Tests des cadences de collecte.

Une plateforme peut plafonner ses appels quotidiens : Remotive en autorise
quatre. La boucle de collecte normale en ferait 96. Ces tests vérifient que
la répartition entre les deux workflows tient cette promesse — et surtout
que le **cron** de la cadence lente reste sous la plus basse des limites
déclarées, puisque c'est lui, et non le code, qui compte les appels.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import httpx
import pytest
import yaml

from src import collect
from src.adapters import ADAPTERS, JapanDevAdapter, RemotiveAdapter
from src.adapters.registry import (
    CADENCES,
    FAST,
    FAST_RUNS_PER_DAY,
    SLOW,
    SLOW_RUNS_PER_DAY,
    cadence_of,
    load_all,
    split_by_cadence,
)
from src.core.config import Source

REPO_ROOT = Path(__file__).resolve().parents[1]
WORKFLOWS_DIR = REPO_ROOT / ".github" / "workflows"
CONFIG_DIR = REPO_ROOT / "config"


# ---------------------------------------------------------------------------
# La limite est déclarée par l'adaptateur, pas par la configuration
# ---------------------------------------------------------------------------


def test_remotive_declares_the_four_calls_a_day_its_terms_impose() -> None:
    assert RemotiveAdapter.max_calls_per_day == 4


def test_a_crawled_source_caps_itself_at_the_slow_cadence() -> None:
    """Le plafond n'est pas toujours imposé par la plateforme : il peut être
    celui qu'on **s'impose**. Un crawl de sitemap coûte trois requêtes de
    repérage par run à un site qui n'a rien demandé, là où un agrégateur
    expose une API faite pour ça. Les sources crawlées se déclarent donc à
    4 appels par jour, comme si la plateforme l'exigeait — la mécanique de
    cadence est la même, et rien à retenir de plus pour en brancher une."""
    assert JapanDevAdapter.max_calls_per_day == 4
    assert cadence_of(Source(nom="Japan Dev", ats="japandev", token="")) == SLOW


def test_the_rate_limited_platforms_are_exactly_those_two_today() -> None:
    """Si une autre plateforme se met à plafonner, ce test le rappelle — et
    le garde-fou du cron ci-dessous vérifiera que la cadence lente suffit."""
    plafonnees = {
        ats: cls.max_calls_per_day
        for ats, cls in ADAPTERS.items()
        if cls.max_calls_per_day is not None
    }

    assert plafonnees == {"remotive": 4, "japandev": 4}


def test_an_unlimited_platform_stays_in_the_fast_lane() -> None:
    assert cadence_of(Source(nom="RemoteOK", ats="remoteok", token="")) == FAST


def test_a_rate_limited_platform_moves_to_the_slow_lane() -> None:
    assert cadence_of(Source(nom="Remotive", ats="remotive", token="")) == SLOW


# ---------------------------------------------------------------------------
# Répartition des sources réelles
# ---------------------------------------------------------------------------


def test_the_real_registries_split_the_two_capped_sources_out() -> None:
    rapides, lentes = split_by_cadence(load_all(CONFIG_DIR))

    assert [source.nom for source in lentes] == ["Remotive", "Japan Dev"]
    assert len(rapides) == 37


def test_every_source_belongs_to_exactly_one_cadence() -> None:
    """Ni source oubliée, ni source collectée deux fois."""
    toutes = load_all(CONFIG_DIR)
    rapides, lentes = split_by_cadence(toutes)

    assert len(rapides) + len(lentes) == len(toutes)
    assert not {s.nom for s in rapides} & {s.nom for s in lentes}


def test_loading_by_cadence_matches_the_split() -> None:
    rapides, lentes = split_by_cadence(load_all(CONFIG_DIR))

    assert load_all(CONFIG_DIR, cadence=FAST) == rapides
    assert load_all(CONFIG_DIR, cadence=SLOW) == lentes


def test_load_all_rejects_an_unknown_cadence() -> None:
    with pytest.raises(ValueError) as excinfo:
        load_all(CONFIG_DIR, cadence="hebdomadaire")

    assert "hebdomadaire" in str(excinfo.value)


def test_load_all_ignores_a_missing_registry_file(tmp_path, caplog) -> None:
    """Un dépôt sans `aggregators.yaml` doit collecter ses ATS quand même."""
    (tmp_path / "sources.yaml").write_text(
        yaml.safe_dump([{"nom": "GitLab", "ats": "greenhouse", "token": "gitlab"}]),
        encoding="utf-8",
    )

    sources = load_all(tmp_path)

    assert [s.nom for s in sources] == ["GitLab"]


# ---------------------------------------------------------------------------
# Le garde-fou : c'est le cron qui compte les appels
# ---------------------------------------------------------------------------


def _load_workflow(name: str) -> dict[str, Any]:
    return yaml.safe_load((WORKFLOWS_DIR / name).read_text(encoding="utf-8"))


def _triggers(doc: dict[str, Any]) -> dict[str, Any]:
    # PyYAML résout la clé non quotée `on` en booléen `True` (YAML 1.1).
    return doc.get("on", doc.get(True))


def _field_values(field: str, span: int) -> int:
    """Nombre de déclenchements que produit un champ de cron sur `span`."""
    if field == "*":
        return span
    if field.startswith("*/"):
        pas = int(field[2:])
        return len(range(0, span, pas))
    return len(field.split(","))


def runs_per_day(cron: str) -> int:
    """Nombre de déclenchements quotidiens d'une expression cron.

    Ne gère que les champs utilisés par ce dépôt (valeur, liste, `*`,
    `*/n`), et suppose une planification quotidienne — ce qu'un test
    vérifie séparément.
    """
    minute, heure, jour, mois, semaine = cron.split()
    assert (jour, mois, semaine) == ("*", "*", "*"), f"cron non quotidien : {cron}"
    return _field_values(minute, 60) * _field_values(heure, 24)


@pytest.mark.parametrize(
    "cron, attendu",
    [
        ("*/15 * * * *", 96),
        ("7 */6 * * *", 4),
        ("0 3 * * *", 1),
        ("0 0,12 * * *", 2),
    ],
)
def test_runs_per_day_counts_correctly(cron, attendu) -> None:
    """L'outil de mesure lui-même, avant de s'en servir comme garde-fou."""
    assert runs_per_day(cron) == attendu


def test_the_fast_workflow_really_runs_the_declared_number_of_times() -> None:
    cron = _triggers(_load_workflow("collect.yml"))["schedule"][0]["cron"]

    assert runs_per_day(cron) == FAST_RUNS_PER_DAY


def test_the_slow_workflow_really_runs_the_declared_number_of_times() -> None:
    cron = _triggers(_load_workflow("collect-slow.yml"))["schedule"][0]["cron"]

    assert runs_per_day(cron) == SLOW_RUNS_PER_DAY


def test_the_slow_cron_stays_within_every_declared_platform_limit() -> None:
    """**Le garde-fou.** Baisser le cron de `collect-slow.yml`, ou brancher
    une plateforme plus stricte que Remotive, doit faire échouer ce test
    plutôt que de nous faire couper l'accès en production."""
    cron = _triggers(_load_workflow("collect-slow.yml"))["schedule"][0]["cron"]
    limites = [
        cls.max_calls_per_day
        for cls in ADAPTERS.values()
        if cls.max_calls_per_day is not None
    ]

    assert limites, "aucune plateforme plafonnée : la cadence lente n'a plus d'objet"
    assert runs_per_day(cron) <= min(limites)


def test_the_fast_cron_would_break_the_limit_which_is_why_remotive_is_excluded() -> None:
    """Explicite la raison d'être des deux cadences."""
    cron = _triggers(_load_workflow("collect.yml"))["schedule"][0]["cron"]

    assert runs_per_day(cron) > RemotiveAdapter.max_calls_per_day


# ---------------------------------------------------------------------------
# Les deux workflows appellent bien chacun leur cadence
# ---------------------------------------------------------------------------


def _run_steps(doc: dict[str, Any]) -> list[str]:
    job = next(iter(doc["jobs"].values()))
    return [step["run"] for step in job["steps"] if "run" in step]


@pytest.mark.parametrize(
    "workflow, cadence",
    [("collect.yml", FAST), ("collect-slow.yml", SLOW)],
)
def test_each_workflow_passes_its_own_cadence(workflow, cadence) -> None:
    commandes = _run_steps(_load_workflow(workflow))

    assert f"python -m src.collect --cadence {cadence}" in commandes


def test_the_slow_workflow_is_valid_yaml_and_can_be_triggered_by_hand() -> None:
    doc = _load_workflow("collect-slow.yml")

    assert isinstance(doc, dict)
    assert "workflow_dispatch" in _triggers(doc)


# ---------------------------------------------------------------------------
# Le point d'entrée
# ---------------------------------------------------------------------------


def empty_client() -> httpx.Client:
    """Client hors réseau dont tous les boards répondent « introuvable »."""
    return httpx.Client(
        transport=httpx.MockTransport(lambda request: httpx.Response(404, text="nope"))
    )


def test_collect_defaults_to_the_fast_cadence() -> None:
    assert collect.build_parser().parse_args([]).cadence == FAST


def test_collect_rejects_a_cadence_that_is_not_declared() -> None:
    with pytest.raises(SystemExit):
        collect.build_parser().parse_args(["--cadence", "hebdomadaire"])


@pytest.mark.parametrize("cadence", CADENCES)
def test_collect_runs_and_lists_only_its_own_cadence(
    cadence, monkeypatch, capsys
) -> None:
    monkeypatch.setenv("GITHUB_TOKEN", "jeton-de-test")
    monkeypatch.chdir(REPO_ROOT)

    # Client hors réseau : tous les boards répondent 404, donc aucune offre
    # et aucun échec — ce test ne juge que la **sélection** des sources.
    code = collect.main(["--cadence", cadence], client=empty_client())

    assert code == 0
    sortie = capsys.readouterr().out
    attendues = {source.nom for source in load_all(CONFIG_DIR, cadence=cadence)}
    autres = {source.nom for source in load_all(CONFIG_DIR)} - attendues

    assert attendues
    for nom in attendues:
        assert nom in sortie
    for nom in autres:
        assert nom not in sortie


def test_collect_still_fails_early_without_its_secret(monkeypatch) -> None:
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)

    with pytest.raises(Exception):
        collect.main(["--cadence", SLOW])
