"""Tests du registre de sources (US-2.1.T).

Vérifie le chargement correct du registre tolérant (`load_registry`) et sa
tolérance aux entrées cassées : une entrée invalide est ignorée et
journalisée, sans empêcher le chargement des autres.
"""

import logging
from pathlib import Path

import yaml

from src.adapters.registry import KNOWN_ATS, load_registry
from src.core.config import Source

REPO_ROOT = Path(__file__).resolve().parents[1]
REAL_SOURCES = REPO_ROOT / "config" / "sources.yaml"


def _write_yaml(path: Path, data) -> Path:
    path.write_text(yaml.safe_dump(data, sort_keys=False), encoding="utf-8")
    return path


def test_valid_entry_loads_as_source_with_exact_expected_values(tmp_path: Path) -> None:
    path = _write_yaml(
        tmp_path / "sources.yaml",
        [{"nom": "Acme", "ats": "greenhouse", "token": "acme"}],
    )

    sources = load_registry(path)

    assert sources == [Source(nom="Acme", ats="greenhouse", token="acme")]


def test_real_sources_yaml_loads_every_entry_and_only_known_ats() -> None:
    raw = yaml.safe_load(REAL_SOURCES.read_text(encoding="utf-8"))

    sources = load_registry(REAL_SOURCES)

    # Le registre est tolérant : une entrée cassée serait ignorée en silence.
    # On vérifie donc qu'aucune des entrées réelles ne l'est.
    assert len(sources) == len(raw)
    assert all(s.ats in KNOWN_ATS for s in sources)


def test_three_entries_load_as_three_sources_in_preserved_order(tmp_path: Path) -> None:
    path = _write_yaml(
        tmp_path / "sources.yaml",
        [
            {"nom": "GitLab", "ats": "greenhouse", "token": "gitlab"},
            {"nom": "Figma", "ats": "lever", "token": "figma"},
            {"nom": "Ramp", "ats": "ashby", "token": "ramp"},
        ],
    )

    sources = load_registry(path)

    assert len(sources) == 3
    assert [s.nom for s in sources] == ["GitLab", "Figma", "Ramp"]


def test_entry_without_token_is_skipped_with_one_warning_others_load(
    tmp_path: Path, caplog
) -> None:
    path = _write_yaml(
        tmp_path / "sources.yaml",
        [
            {"nom": "GitLab", "ats": "greenhouse", "token": "gitlab"},
            {"nom": "Broken Co", "ats": "greenhouse"},
            {"nom": "Figma", "ats": "lever", "token": "figma"},
        ],
    )

    with caplog.at_level(logging.WARNING):
        sources = load_registry(path)

    assert [s.nom for s in sources] == ["GitLab", "Figma"]
    warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert len(warnings) == 1
    assert "Broken Co" in warnings[0].message


def test_unknown_ats_is_skipped_with_message_naming_the_source(
    tmp_path: Path, caplog
) -> None:
    path = _write_yaml(
        tmp_path / "sources.yaml",
        [{"nom": "Mystery Corp", "ats": "bamboohr", "token": "mystery"}],
    )

    with caplog.at_level(logging.WARNING):
        sources = load_registry(path)

    assert sources == []
    assert any("Mystery Corp" in r.message for r in caplog.records)
