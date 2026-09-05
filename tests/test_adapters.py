"""Tests des adaptateurs ATS (US-2.2.T) et tests transverses de tous les
adaptateurs branchés, agrégateurs compris (US-2.3.T).

Le parsing est vérifié contre des **réponses réelles figées**, capturées le
2026-09-05 sur les endpoints publics et rejouées via un
`httpx.MockTransport` : aucun test de ce fichier n'accède au réseau.

Les tests transverses en fin de fichier sont paramétrés par
`ALL_ADAPTERS` (voir `tests/adapter_cases.py`) : ils couvrent donc les
douze agrégateurs comme les cinq ATS, sans une ligne de test en plus.
Les tests propres aux agrégateurs vivent dans `test_aggregators.py`.
"""

from __future__ import annotations

import json
import logging
from dataclasses import asdict
from pathlib import Path

import httpx
import pytest

from src.adapters import (
    ADAPTERS,
    Adapter,
    AdapterError,
    AshbyAdapter,
    GreenhouseAdapter,
    LeverAdapter,
    RawJob,
    SmartRecruitersAdapter,
    UnknownAtsError,
    WorkableAdapter,
    get_adapter,
)
from src.adapters.base import USER_AGENT, html_to_text, to_iso_utc
from src.adapters.registry import KNOWN_ATS
from src.core.config import Source
from tests.adapter_cases import (
    ALL_ADAPTER_IDS,
    ALL_ADAPTERS,
    RAWJOB_KEYS,
    fetch_body,
    fetch_case,
    responder,
)

FIXTURES = Path(__file__).resolve().parent / "fixtures"

GITLAB = Source(nom="GitLab", ats="greenhouse", token="gitlab")
MALT = Source(nom="Malt", ats="lever", token="malt")
RAMP = Source(nom="Ramp", ats="ashby", token="ramp")
UBISOFT = Source(nom="Ubisoft", ats="smartrecruiters", token="Ubisoft2")
BG_PREVENT = Source(nom="BG Prevent", ats="workable", token="bg-prevent")


# ---------------------------------------------------------------------------
# Outillage : rejoue une réponse figée sans toucher au réseau
# ---------------------------------------------------------------------------


def _responder(body: str, status: int = 200, recorder: list | None = None):
    return responder(body, status=status, recorder=recorder)


def _fetch(adapter_cls, fixture: str, source: Source, **kwargs) -> list[RawJob]:
    body = (FIXTURES / fixture).read_text(encoding="utf-8")
    return adapter_cls(client=_responder(body, **kwargs)).fetch(source)


def _fetch_body(adapter_cls, body: str, source: Source, **kwargs) -> list[RawJob]:
    return adapter_cls(client=_responder(body, **kwargs)).fetch(source)


# ---------------------------------------------------------------------------
# US-2.2.0 — contrat d'adaptateur commun
# ---------------------------------------------------------------------------


def test_adapter_abc_cannot_be_instantiated_without_fetch() -> None:
    class Incomplet(Adapter):
        ats = "incomplet"

    with pytest.raises(TypeError):
        Incomplet()  # type: ignore[abstract]


@pytest.mark.parametrize("ats, adapter_cls", sorted(ADAPTERS.items()))
def test_every_registered_adapter_honours_the_contract(ats, adapter_cls) -> None:
    assert issubclass(adapter_cls, Adapter)
    assert adapter_cls.ats == ats


def test_get_adapter_returns_the_matching_implementation() -> None:
    assert isinstance(get_adapter("greenhouse"), GreenhouseAdapter)
    assert isinstance(get_adapter("lever"), LeverAdapter)


def test_get_adapter_on_unknown_ats_names_the_supported_ones() -> None:
    with pytest.raises(UnknownAtsError) as excinfo:
        get_adapter("bamboohr")

    message = str(excinfo.value)
    assert "bamboohr" in message
    assert "greenhouse" in message


def test_source_registry_accepts_exactly_the_implemented_ats() -> None:
    """`sources.yaml` ne doit accepter que des ATS réellement branchés."""
    assert KNOWN_ATS == frozenset(ADAPTERS)


# ---------------------------------------------------------------------------
# US-2.2.1 — Greenhouse, sur fixture réelle
# ---------------------------------------------------------------------------


def test_greenhouse_fixture_yields_three_jobs_with_exact_witness_values() -> None:
    jobs = _fetch(GreenhouseAdapter, "greenhouse_gitlab.json", GITLAB)

    assert len(jobs) == 3

    witness = jobs[0]
    assert witness.id == "8620720002"
    assert witness.ats == "greenhouse"
    assert witness.entreprise == "GitLab"
    assert witness.titre == "Backend Engineer (Ruby), AI Engineering: Agent Observability"
    assert witness.localisation == "Remote, Canada"
    assert witness.remote_type == "remote"
    assert witness.url == "https://job-boards.greenhouse.io/gitlab/jobs/8620720002"
    # `first_published` valait 2026-07-08T06:30:39-04:00 → normalisé en UTC.
    assert witness.date == "2026-07-08T10:30:39+00:00"
    assert witness.description.startswith(
        "GitLab is the intelligent orchestration platform for DevSecOps."
    )
    assert "Ruby on Rails" in witness.description


def test_greenhouse_calls_the_board_api_with_content_and_our_user_agent() -> None:
    seen: list[httpx.Request] = []
    body = (FIXTURES / "greenhouse_gitlab.json").read_text(encoding="utf-8")

    GreenhouseAdapter(client=_responder(body, recorder=seen)).fetch(GITLAB)

    assert len(seen) == 1
    assert str(seen[0].url) == (
        "https://boards-api.greenhouse.io/v1/boards/gitlab/jobs?content=true"
    )
    assert seen[0].headers["user-agent"] == USER_AGENT


def test_greenhouse_description_is_plain_text_without_markup() -> None:
    jobs = _fetch(GreenhouseAdapter, "greenhouse_gitlab.json", GITLAB)

    # Greenhouse renvoie du HTML doublement échappé (`&lt;p&gt;`) : ni les
    # balises ni leur forme échappée ne doivent survivre.
    for job in jobs:
        assert "<" not in job.description
        assert "&lt;" not in job.description
        assert "&nbsp;" not in job.description


# ---------------------------------------------------------------------------
# US-2.2.2 — Lever, sur fixture réelle
# ---------------------------------------------------------------------------


def test_lever_fixture_yields_three_jobs_with_exact_witness_values() -> None:
    jobs = _fetch(LeverAdapter, "lever_malt.json", MALT)

    assert len(jobs) == 3

    witness = jobs[0]
    assert witness.id == "5549f929-b192-43aa-8c1e-d7811f673e1f"
    assert witness.ats == "lever"
    assert witness.entreprise == "Malt"
    assert witness.titre == "Staff Software Engineer"
    assert witness.localisation == "Paris; London; Lyon"
    assert witness.remote_type == "hybrid"
    assert witness.url == "https://jobs.lever.co/malt/5549f929-b192-43aa-8c1e-d7811f673e1f"
    # `createdAt` valait 1785760723455 (epoch ms) → même format que Greenhouse.
    assert witness.date == "2026-08-03T12:38:43+00:00"
    assert witness.description.startswith("🪐 Discover our galaxy")


def test_lever_description_includes_the_bullet_sections_not_just_the_intro() -> None:
    """Les technos vivent dans `lists`, pas dans `descriptionPlain`."""
    jobs = _fetch(LeverAdapter, "lever_malt.json", MALT)

    witness = jobs[0]
    intro_only = json.loads((FIXTURES / "lever_malt.json").read_text(encoding="utf-8"))[0][
        "descriptionPlain"
    ]
    assert len(witness.description) > len(intro_only)
    assert "<" not in witness.description


def test_lever_calls_the_postings_api_in_json_mode() -> None:
    seen: list[httpx.Request] = []
    body = (FIXTURES / "lever_malt.json").read_text(encoding="utf-8")

    LeverAdapter(client=_responder(body, recorder=seen)).fetch(MALT)

    assert str(seen[0].url) == "https://api.lever.co/v0/postings/malt?mode=json"


# ---------------------------------------------------------------------------
# US-2.2.3 / US-2.2.4 — Ashby, SmartRecruiters, Workable
# ---------------------------------------------------------------------------


def test_ashby_fixture_yields_three_jobs_with_exact_witness_values() -> None:
    jobs = _fetch(AshbyAdapter, "ashby_ramp.json", RAMP)

    assert len(jobs) == 3

    witness = jobs[0]
    assert witness.id == "d1183b00-6590-4fe4-a585-28d84e578fe3"
    assert witness.titre == "Software Engineer, Security, Stablecoin"
    assert witness.localisation == "New York, NY (HQ); San Francisco, CA; Remote (US)"
    assert witness.remote_type == "remote"
    assert witness.url == (
        "https://jobs.ashbyhq.com/ramp/d1183b00-6590-4fe4-a585-28d84e578fe3"
    )
    assert witness.date == "2026-05-13T20:28:09+00:00"
    assert witness.description.startswith("ABOUT RAMP")


def test_ashby_prefers_workplace_type_over_the_is_remote_flag() -> None:
    """`isRemote` est vrai dès qu'une localisation secondaire est remote."""
    jobs = _fetch(AshbyAdapter, "ashby_ramp.json", RAMP)

    assert [job.remote_type for job in jobs] == ["remote", "hybrid", "hybrid"]


def test_smartrecruiters_fixture_yields_three_jobs_with_exact_witness_values() -> None:
    jobs = _fetch(SmartRecruitersAdapter, "smartrecruiters_ubisoft.json", UBISOFT)

    assert len(jobs) == 3

    witness = jobs[0]
    assert witness.id == "744000147631529"
    assert witness.titre == "Project Coordinator"
    assert witness.localisation == "Taguig, NCR, Philippines"
    assert witness.remote_type == "onsite"
    assert witness.url == "https://jobs.smartrecruiters.com/Ubisoft2/744000147631529"
    assert witness.date == "2026-09-05T03:52:53+00:00"
    # Limite documentée : la liste SmartRecruiters ne porte pas la description.
    assert witness.description == ""


def test_workable_fixture_is_fetched_with_post_and_parsed() -> None:
    seen: list[httpx.Request] = []
    body = (FIXTURES / "workable_bgprevent.json").read_text(encoding="utf-8")

    jobs = WorkableAdapter(client=_responder(body, recorder=seen)).fetch(BG_PREVENT)

    assert seen[0].method == "POST"
    assert str(seen[0].url) == "https://apply.workable.com/api/v3/accounts/bg-prevent/jobs"

    assert len(jobs) == 1
    witness = jobs[0]
    assert witness.id == "6081338"
    assert witness.titre == "Software Engineer"
    assert witness.remote_type == "remote"
    # L'URL publique n'est pas renvoyée par l'API : elle est reconstruite.
    assert witness.url == "https://apply.workable.com/bg-prevent/j/E2C96AB706/"
    assert witness.date == "2026-09-04T00:00:00+00:00"


# ---------------------------------------------------------------------------
# Invariants du schéma commun
# ---------------------------------------------------------------------------


def test_the_catalogue_covers_every_registered_adapter() -> None:
    """Garde-fou : un adaptateur branché mais absent du catalogue échapperait
    silencieusement à tous les tests transverses."""
    assert {case.adapter_cls.ats for case in ALL_ADAPTERS} == set(ADAPTERS)


def test_greenhouse_and_lever_produce_the_same_rawjob_keys() -> None:
    greenhouse = _fetch(GreenhouseAdapter, "greenhouse_gitlab.json", GITLAB)[0]
    lever = _fetch(LeverAdapter, "lever_malt.json", MALT)[0]

    assert set(asdict(greenhouse)) == RAWJOB_KEYS
    assert set(asdict(lever)) == RAWJOB_KEYS


@pytest.mark.parametrize("case", ALL_ADAPTERS, ids=ALL_ADAPTER_IDS)
def test_every_adapter_produces_the_common_schema(case) -> None:
    """Le contrat `RawJob` vaut pour les 5 ATS **et** les 12 agrégateurs."""
    jobs = fetch_case(case)

    assert jobs, "la fixture doit contenir au moins une offre"
    for job in jobs:
        fields = asdict(job)
        assert set(fields) == RAWJOB_KEYS
        # Aucun champ ne vaut `None` : un manque se traduit par `""`.
        assert all(isinstance(value, str) for value in fields.values())
        assert job.ats == case.adapter_cls.ats
        assert job.id and job.titre and job.url
        assert job.remote_type in {"remote", "hybrid", "onsite", "unknown"}
        assert "<" not in job.description
        # Toute offre a un employeur nommé — c'est `Source.nom` chez un ATS,
        # un champ de l'offre chez un agrégateur (US-2.3.0).
        assert job.entreprise
        if not case.aggregator:
            assert job.entreprise == case.source.nom


# ---------------------------------------------------------------------------
# Cas limites : 404, champ absent, JSON malformé
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("case", ALL_ADAPTERS, ids=ALL_ADAPTER_IDS)
def test_http_404_returns_an_empty_list_without_raising(case, caplog) -> None:
    with caplog.at_level(logging.WARNING):
        jobs = fetch_body(case, "Not Found", status=404)

    assert jobs == []
    assert any(case.source.nom in record.getMessage() for record in caplog.records)


def test_job_without_location_field_falls_back_to_default_remote_type() -> None:
    """Fixture réelle amputée de sa clé `location` : pas de `KeyError`."""
    jobs = _fetch(GreenhouseAdapter, "greenhouse_no_location.json", GITLAB)

    assert len(jobs) == 1
    assert jobs[0].localisation == ""
    assert jobs[0].remote_type == "unknown"


def test_null_location_is_treated_like_a_missing_one() -> None:
    body = json.dumps({"jobs": [{"id": 1, "title": "SWE", "location": None}]})

    jobs = _fetch_body(GreenhouseAdapter, body, GITLAB)

    assert jobs[0].localisation == ""
    assert jobs[0].remote_type == "unknown"
    assert jobs[0].date == ""
    assert jobs[0].description == ""


@pytest.mark.parametrize("case", ALL_ADAPTERS, ids=ALL_ADAPTER_IDS)
def test_malformed_payload_raises_an_adapter_error_naming_the_source(case) -> None:
    """Un corps tronqué est inexploitable, qu'on attende du JSON ou du RSS."""
    with pytest.raises(AdapterError) as excinfo:
        fetch_body(case, '{"jobs": [')

    assert case.source.nom in str(excinfo.value)


@pytest.mark.parametrize("case", ALL_ADAPTERS, ids=ALL_ADAPTER_IDS)
def test_server_error_raises_an_adapter_error_naming_the_source(case) -> None:
    with pytest.raises(AdapterError) as excinfo:
        fetch_body(case, "boom", status=500)

    assert case.source.nom in str(excinfo.value)
    assert "500" in str(excinfo.value)


def test_unexpected_schema_raises_an_adapter_error() -> None:
    with pytest.raises(AdapterError) as excinfo:
        _fetch_body(GreenhouseAdapter, '{"jobs": "pas une liste"}', GITLAB)

    assert "GitLab" in str(excinfo.value)


def test_network_failure_raises_an_adapter_error_naming_the_source() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectTimeout("timeout", request=request)

    client = httpx.Client(transport=httpx.MockTransport(handler))

    with pytest.raises(AdapterError) as excinfo:
        GreenhouseAdapter(client=client).fetch(GITLAB)

    assert "GitLab" in str(excinfo.value)


def test_one_broken_job_is_skipped_and_the_others_are_kept(caplog) -> None:
    body = json.dumps(
        {
            "jobs": [
                {"id": 1, "title": "Backend Engineer", "location": {"name": "Remote"}},
                {"title": "Offre sans id"},
                "pas un mapping",
                {"id": 2, "title": "Frontend Engineer", "location": {"name": "Remote"}},
            ]
        }
    )

    with caplog.at_level(logging.WARNING):
        jobs = _fetch_body(GreenhouseAdapter, body, GITLAB)

    assert [job.id for job in jobs] == ["1", "2"]
    assert len([r for r in caplog.records if r.levelno == logging.WARNING]) == 2


def test_empty_board_returns_an_empty_list() -> None:
    assert _fetch_body(GreenhouseAdapter, '{"jobs": []}', GITLAB) == []
    assert _fetch_body(LeverAdapter, "[]", MALT) == []


# ---------------------------------------------------------------------------
# Helpers de parsing
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "value, expected",
    [
        # Greenhouse : ISO avec offset.
        ("2026-07-08T06:30:39-04:00", "2026-07-08T10:30:39+00:00"),
        # Ashby : ISO UTC avec millisecondes.
        ("2026-05-13T20:28:09.574+00:00", "2026-05-13T20:28:09+00:00"),
        # SmartRecruiters / Workable : ISO suffixé Z.
        ("2026-09-05T03:52:53.056Z", "2026-09-05T03:52:53+00:00"),
        # Lever : epoch en millisecondes.
        (1785760723455, "2026-08-03T12:38:43+00:00"),
        # Epoch en secondes.
        (1785760723, "2026-08-03T12:38:43+00:00"),
        # Date naïve : considérée UTC.
        ("2026-01-02T03:04:05", "2026-01-02T03:04:05+00:00"),
        # Illisible ou absent : chaîne vide, jamais d'exception.
        ("hier", ""),
        (None, ""),
        ("", ""),
    ],
)
def test_to_iso_utc_normalises_every_ats_date_format(value, expected) -> None:
    assert to_iso_utc(value) == expected


def test_html_to_text_strips_tags_and_keeps_list_items() -> None:
    html = "<div><p>Stack&nbsp;:</p><ul><li>Python</li><li>Kotlin</li></ul></div>"

    assert html_to_text(html) == "Stack :\n\n- Python\n- Kotlin"


def test_html_to_text_handles_escaped_html_from_greenhouse() -> None:
    assert html_to_text("&lt;p&gt;Remote &amp;amp; async&lt;/p&gt;") == "Remote & async"


def test_html_to_text_on_empty_input_returns_empty_string() -> None:
    assert html_to_text("") == ""
