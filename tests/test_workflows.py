"""Tests secrets & intégrité des workflows (US-1.3.T) — volet workflows.

Vérifie que les workflows sont du YAML valide et déclarent les bons
déclencheurs, sans jamais exécuter GitHub Actions.

Ce que ces workflows *collectent* — la répartition des sources entre la
cadence normale et la cadence réduite, et le respect des plafonds d'appels
des plateformes — est testé dans `test_cadences.py`.
"""

from pathlib import Path
from typing import Any

import yaml

REPO_ROOT = Path(__file__).resolve().parents[1]
WORKFLOWS_DIR = REPO_ROOT / ".github" / "workflows"


def _load(name: str) -> dict[str, Any]:
    return yaml.safe_load((WORKFLOWS_DIR / name).read_text(encoding="utf-8"))


def _triggers(doc: dict[str, Any]) -> dict[str, Any]:
    """Section `on:` du workflow.

    PyYAML (YAML 1.1) résout la clé non quotée `on` en booléen `True` —
    c'est l'ATS-gotcha classique des workflows GitHub Actions. On tente
    donc `"on"` puis `True` pour rester robuste au comportement du loader.
    """
    return doc.get("on", doc.get(True))


WORKFLOWS = ("collect.yml", "collect-slow.yml", "purge.yml", "cv.yml")


def test_every_workflow_file_is_valid_yaml() -> None:
    for name in WORKFLOWS:
        doc = _load(name)
        assert isinstance(doc, dict)


def test_no_workflow_file_is_left_untested() -> None:
    """Un workflow ajouté sans être listé ici échapperait à ces contrôles."""
    presents = {path.name for path in WORKFLOWS_DIR.glob("*.yml")}

    assert presents == set(WORKFLOWS)


def test_collect_workflow_declares_schedule_every_15_minutes() -> None:
    doc = _load("collect.yml")
    schedule = _triggers(doc)["schedule"]

    assert schedule[0]["cron"] == "*/15 * * * *"


def test_purge_workflow_declares_a_daily_cron() -> None:
    doc = _load("purge.yml")
    schedule = _triggers(doc)["schedule"]
    cron = schedule[0]["cron"]
    minute, hour, day, month, weekday = cron.split()

    assert (day, month, weekday) == ("*", "*", "*")
    assert minute.isdigit()
    assert hour.isdigit()


def test_cv_workflow_triggers_on_issues_labeled() -> None:
    doc = _load("cv.yml")
    issues_trigger = _triggers(doc)["issues"]

    assert "labeled" in issues_trigger["types"]


def test_cv_workflow_guards_on_the_cv_label() -> None:
    doc = _load("cv.yml")
    job = next(iter(doc["jobs"].values()))

    condition = job["if"]
    assert "label" in condition
    assert "cv" in condition
