"""Tests de chargement & validation config (US-1.2.T).

Prouve que la config valide donne les bonnes valeurs et que l'invalide
échoue clairement — jamais un cast silencieux ni une stacktrace YAML brute.
"""

from pathlib import Path

import pytest
import yaml

from src.core.config import ConfigError, load_filters, load_sources

REPO_ROOT = Path(__file__).resolve().parents[1]
REAL_FILTERS = REPO_ROOT / "config" / "filters.yaml"
REAL_SOURCES = REPO_ROOT / "config" / "sources.yaml"


def _write_yaml(path: Path, data) -> Path:
    path.write_text(yaml.safe_dump(data, sort_keys=False), encoding="utf-8")
    return path


# ---------------------------------------------------------------------------
# filters.yaml — chargement valide, champ à champ
# ---------------------------------------------------------------------------


def test_valid_filters_yaml_loads_with_exact_expected_values() -> None:
    raw = yaml.safe_load(REAL_FILTERS.read_text(encoding="utf-8"))

    filters = load_filters(REAL_FILTERS)

    assert filters.retention_days == raw["retention_days"]
    assert filters.retention.title_include == raw["retention"]["title_include"]
    assert filters.retention.title_exclude == raw["retention"]["title_exclude"]
    assert filters.retention.seniority.keep == raw["retention"]["seniority"]["keep"]
    assert filters.retention.seniority.drop == raw["retention"]["seniority"]["drop"]
    assert filters.retention.location.require_any == raw["retention"]["location"]["require_any"]
    assert (
        filters.retention.location.relocation_regions
        == raw["retention"]["location"]["relocation_regions"]
    )
    assert filters.retention.tech_include_any == raw["retention"]["tech_include_any"]
    assert filters.scoring.geo_priority == raw["scoring"]["geo_priority"]


# ---------------------------------------------------------------------------
# clé requise manquante
# ---------------------------------------------------------------------------


def test_missing_required_key_raises_config_error_naming_the_key(tmp_path: Path) -> None:
    path = _write_yaml(tmp_path / "filters.yaml", {"scoring": {"geo_priority": {}}})

    with pytest.raises(ConfigError) as excinfo:
        load_filters(path)

    assert "retention" in str(excinfo.value)


def test_missing_nested_scoring_key_raises_config_error(tmp_path: Path) -> None:
    path = _write_yaml(tmp_path / "filters.yaml", {"retention": {}})

    with pytest.raises(ConfigError) as excinfo:
        load_filters(path)

    assert "scoring" in str(excinfo.value)


# ---------------------------------------------------------------------------
# mauvais type — pas de cast silencieux
# ---------------------------------------------------------------------------


def test_wrong_type_for_retention_days_raises_explicit_error_not_silent_cast(
    tmp_path: Path,
) -> None:
    path = _write_yaml(
        tmp_path / "filters.yaml",
        {
            "retention_days": "sept",
            "retention": {},
            "scoring": {},
        },
    )

    with pytest.raises(ConfigError) as excinfo:
        load_filters(path)

    assert "retention_days" in str(excinfo.value)


def test_wrong_type_for_geo_priority_value_raises_explicit_error(tmp_path: Path) -> None:
    path = _write_yaml(
        tmp_path / "filters.yaml",
        {
            "retention": {},
            "scoring": {"geo_priority": {"europe": "soixante"}},
        },
    )

    with pytest.raises(ConfigError) as excinfo:
        load_filters(path)

    assert "geo_priority.europe" in str(excinfo.value)


# ---------------------------------------------------------------------------
# fichier absent
# ---------------------------------------------------------------------------


def test_missing_file_raises_clear_error_not_raw_yaml_stacktrace(tmp_path: Path) -> None:
    missing = tmp_path / "does-not-exist.yaml"

    with pytest.raises(ConfigError) as excinfo:
        load_filters(missing)

    message = str(excinfo.value)
    assert "introuvable" in message
    assert str(missing) in message


def test_missing_sources_file_raises_config_error(tmp_path: Path) -> None:
    missing = tmp_path / "does-not-exist.yaml"

    with pytest.raises(ConfigError):
        load_sources(missing)


# ---------------------------------------------------------------------------
# title_exclude vide vs absente — comportements documentés et distincts
# ---------------------------------------------------------------------------


def test_title_exclude_absent_key_means_no_exclusion(tmp_path: Path) -> None:
    path = _write_yaml(
        tmp_path / "filters.yaml",
        {"retention": {"title_include": ["engineer"]}, "scoring": {}},
    )

    filters = load_filters(path)

    assert filters.retention.title_exclude == []


def test_title_exclude_explicit_empty_list_means_no_exclusion(tmp_path: Path) -> None:
    path = _write_yaml(
        tmp_path / "filters.yaml",
        {"retention": {"title_include": ["engineer"], "title_exclude": []}, "scoring": {}},
    )

    filters = load_filters(path)

    assert filters.retention.title_exclude == []


def test_title_exclude_absent_and_explicit_empty_are_equivalent(tmp_path: Path) -> None:
    """Comportement documenté : absent et `[]` produisent le même résultat
    (aucune exclusion) — les deux cas sont testés distinctement pour
    verrouiller cette équivalence."""
    with_key = _write_yaml(
        tmp_path / "with_key.yaml",
        {"retention": {"title_exclude": []}, "scoring": {}},
    )
    without_key = _write_yaml(
        tmp_path / "without_key.yaml",
        {"retention": {}, "scoring": {}},
    )

    assert load_filters(with_key).retention.title_exclude == load_filters(
        without_key
    ).retention.title_exclude


# ---------------------------------------------------------------------------
# sources.yaml
# ---------------------------------------------------------------------------


def test_real_sources_yaml_has_three_entries_with_expected_values() -> None:
    sources = load_sources(REAL_SOURCES)

    assert len(sources) == 3
    assert sources[0].nom == "GitLab"
    assert sources[0].ats == "greenhouse"
    assert sources[0].token == "gitlab"


def test_source_entry_missing_token_raises_config_error_naming_the_key(tmp_path: Path) -> None:
    path = _write_yaml(
        tmp_path / "sources.yaml",
        [{"nom": "Acme", "ats": "greenhouse"}],
    )

    with pytest.raises(ConfigError) as excinfo:
        load_sources(path)

    assert "token" in str(excinfo.value)
