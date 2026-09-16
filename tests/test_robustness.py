"""Tests de robustesse et de politesse de la collecte (US-2.4.T).

Deux promesses, vérifiées ici sur des pannes simulées et jamais sur du
réseau réel :

- **isolation par source** (US-2.4.1) — une source qui tombe ne fait pas
  tomber le run ; seul un échec *général* sort en erreur ;
- **politesse réseau** (US-2.4.2) — un timeout par requête, une seconde
  tentative après backoff pour les pannes passagères et elles seules, et un
  User-Agent qui nous identifie.

Les pannes sont jouées par un `httpx.MockTransport` qui répond selon l'URL
appelée : les adaptateurs réels sont donc exercés de bout en bout, avec
leurs vraies URLs et leurs vrais parseurs.

L'attente du backoff est neutralisée par la fixture `backoff_delays` de
`conftest.py` — sinon chaque panne simulée coûterait deux secondes.
"""

from __future__ import annotations

import logging
from pathlib import Path

import httpx
import pytest

from src import collect
from src.adapters import AdapterError, GreenhouseAdapter, RemoteOkAdapter
from src.adapters.base import (
    BROWSER_USER_AGENT,
    DEFAULT_TIMEOUT,
    MAX_ATTEMPTS,
    RETRY_DELAYS,
    RETRYABLE_STATUS,
    USER_AGENT,
)
from src.collect import collect_sources
from src.core.config import Source
from tests.adapter_cases import (
    AGGREGATOR_IDS,
    AGGREGATOR_CASES,
    API_AGGREGATOR_CASES,
    API_AGGREGATOR_IDS,
    ALL_ADAPTER_IDS,
    ALL_ADAPTERS,
    ATS_CASES,
    fetch_case,
)

FIXTURES = Path(__file__).resolve().parent / "fixtures"

GITLAB = Source(nom="GitLab", ats="greenhouse", token="gitlab")
MALT = Source(nom="Malt", ats="lever", token="malt")
RAMP = Source(nom="Ramp", ats="ashby", token="ramp")

#: Trois sources sur trois plateformes différentes : de quoi vérifier qu'une
#: panne isolée laisse bien passer les deux autres.
TROIS_SOURCES = [GITLAB, MALT, RAMP]

GREENHOUSE_HOST = "greenhouse.io"
LEVER_HOST = "lever.co"
ASHBY_HOST = "ashbyhq.com"


def fixture(name: str) -> str:
    return (FIXTURES / name).read_text(encoding="utf-8")


def routing_client(
    routes: dict[str, tuple[int, str]],
    *,
    default: tuple[int, str] = (200, "{}"),
    recorder: list[httpx.Request] | None = None,
) -> httpx.Client:
    """Client hors réseau qui choisit sa réponse selon l'URL appelée.

    `routes` associe un fragment d'URL à un couple `(statut, corps)` ; les
    URLs non listées reçoivent `default`. C'est ce qui permet de faire
    tomber **une** plateforme sur trois sans toucher aux deux autres.
    """

    def handler(request: httpx.Request) -> httpx.Response:
        if recorder is not None:
            recorder.append(request)
        for fragment, (status, body) in routes.items():
            if fragment in str(request.url):
                return httpx.Response(status, content=body)
        return httpx.Response(default[0], content=default[1])

    return httpx.Client(transport=httpx.MockTransport(handler))


def always(status: int, body: str = "indisponible") -> httpx.Client:
    """Client dont **toutes** les requêtes reçoivent le même statut."""
    return routing_client({}, default=(status, body))


# ---------------------------------------------------------------------------
# US-2.4.1 — une source en échec n'emporte pas le run
# ---------------------------------------------------------------------------


def test_one_source_out_of_three_fails_and_the_other_two_are_still_collected() -> None:
    """La promesse de la feature, sur trois adaptateurs réels."""
    client = routing_client(
        {
            GREENHOUSE_HOST: (200, fixture("greenhouse_gitlab.json")),
            LEVER_HOST: (500, "erreur interne"),
            ASHBY_HOST: (200, fixture("ashby_ramp.json")),
        }
    )

    report = collect_sources(TROIS_SOURCES, client=client)

    assert [result.source.nom for result in report.succeeded] == ["GitLab", "Ramp"]
    assert [result.source.nom for result in report.failed] == ["Malt"]
    # Les offres des deux sources saines sont bien là, pas seulement leur statut.
    assert len(report.jobs) == 6
    assert {job.entreprise for job in report.jobs} == {"GitLab", "Ramp"}


def test_the_failing_source_is_named_in_the_report_and_in_the_logs(caplog) -> None:
    client = routing_client({LEVER_HOST: (500, "erreur interne")})

    with caplog.at_level(logging.WARNING):
        report = collect_sources(TROIS_SOURCES, client=client)

    (echec,) = report.failed
    assert "500" in echec.error
    assert any(
        "Malt" in record.getMessage() and "continue" in record.getMessage()
        for record in caplog.records
    )


def test_the_order_of_the_sources_is_preserved_in_the_report() -> None:
    report = collect_sources(TROIS_SOURCES, client=always(500))

    assert [result.source for result in report.results] == TROIS_SOURCES


def test_a_source_whose_board_moved_is_not_a_failure() -> None:
    """404 = le board a bougé : la source a répondu, elle n'a rien à offrir."""
    client = routing_client(
        {
            GREENHOUSE_HOST: (404, "Not Found"),
            LEVER_HOST: (200, fixture("lever_malt.json")),
            ASHBY_HOST: (200, fixture("ashby_ramp.json")),
        }
    )

    report = collect_sources(TROIS_SOURCES, client=client)

    assert report.failed == []
    assert len(report.succeeded) == 3
    gitlab = report.results[0]
    assert gitlab.ok and gitlab.jobs == []


def test_an_adapter_bug_is_isolated_just_like_a_network_failure(
    monkeypatch, caplog
) -> None:
    """L'échec attendu est `AdapterError` — mais un `KeyError` d'adaptateur
    ne doit pas plus emporter le run qu'une API en panne."""

    class AdaptateurCasse:
        def fetch(self, source: Source) -> list:
            raise KeyError("champ_absent")

    vrai_get_adapter = collect.get_adapter

    def get_adapter(ats: str, **kwargs):
        return AdaptateurCasse() if ats == "lever" else vrai_get_adapter(ats, **kwargs)

    monkeypatch.setattr(collect, "get_adapter", get_adapter)
    client = routing_client(
        {
            GREENHOUSE_HOST: (200, fixture("greenhouse_gitlab.json")),
            ASHBY_HOST: (200, fixture("ashby_ramp.json")),
        }
    )

    with caplog.at_level(logging.WARNING):
        report = collect_sources(TROIS_SOURCES, client=client)

    assert [result.source.nom for result in report.failed] == ["Malt"]
    assert "KeyError" in report.failed[0].error
    assert len(report.jobs) == 6


def test_an_unknown_ats_is_isolated_too() -> None:
    """Une entrée de config qui a échappé au registre ne fait pas tomber le run."""
    sources = [GITLAB, Source(nom="Mystère", ats="plateforme-inconnue", token="x")]
    client = routing_client({GREENHOUSE_HOST: (200, fixture("greenhouse_gitlab.json"))})

    report = collect_sources(sources, client=client)

    assert [result.source.nom for result in report.succeeded] == ["GitLab"]
    assert "plateforme-inconnue" in report.failed[0].error


def test_an_empty_source_list_gives_an_empty_report_that_is_not_a_failure() -> None:
    report = collect_sources([], client=always(500))

    assert report.results == []
    assert report.jobs == []
    # Rien à interroger n'est pas « tout a échoué » : la cadence lente peut
    # légitimement être vide.
    assert not report.all_failed


# ---------------------------------------------------------------------------
# Une source qui se tarit : le succès silencieux
# ---------------------------------------------------------------------------


def test_a_source_that_comes_back_with_nothing_is_flagged(caplog) -> None:
    """Une API qui ferme en répondant « collection vide » compterait sinon
    comme un succès : c'est le trou par lequel Free-Work est passé."""
    client = routing_client(
        {
            GREENHOUSE_HOST: (200, '{"jobs": []}'),
            LEVER_HOST: (200, fixture("lever_malt.json")),
            ASHBY_HOST: (200, fixture("ashby_ramp.json")),
        }
    )

    with caplog.at_level(logging.WARNING):
        report = collect_sources(TROIS_SOURCES, client=client)

    assert [result.source.nom for result in report.empty] == ["GitLab"]
    assert any(
        "GitLab" in record.getMessage() and "0 offre" in record.getMessage()
        for record in caplog.records
    )


def test_an_empty_source_is_a_success_not_a_failure() -> None:
    """Un board sans poste ouvert est légitime : on avertit, on n'échoue pas."""
    client = routing_client({GREENHOUSE_HOST: (200, '{"jobs": []}')})

    report = collect_sources([GITLAB], client=client)

    assert report.failed == []
    assert report.succeeded == report.results
    assert not report.all_failed


def test_a_source_with_offers_is_never_flagged_as_empty() -> None:
    client = routing_client({GREENHOUSE_HOST: (200, fixture("greenhouse_gitlab.json"))})

    report = collect_sources([GITLAB], client=client)

    assert report.empty == []


def test_a_moved_board_counts_as_empty_too() -> None:
    """404 : la source a répondu, mais n'a plus rien — c'est exactement le
    genre de disparition silencieuse que ce marqueur doit rendre visible."""
    report = collect_sources([GITLAB], client=always(404, "Not Found"))

    assert report.failed == []
    assert [result.source.nom for result in report.empty] == ["GitLab"]


def test_a_failed_source_is_not_counted_as_empty() -> None:
    """`empty` dit « a répondu, sans rien » — pas « n'a pas répondu »."""
    report = collect_sources([GITLAB], client=always(500))

    assert report.empty == []
    assert len(report.failed) == 1


# ---------------------------------------------------------------------------
# US-2.4.T — code de sortie du run
# ---------------------------------------------------------------------------


def _run_main(monkeypatch, sources: list[Source], client: httpx.Client) -> int:
    monkeypatch.setenv("GITHUB_TOKEN", "jeton-de-test")
    monkeypatch.setattr(collect, "load_all", lambda **kwargs: list(sources))
    # La publication a ses propres tests (test_publication.py).
    return collect.main(["--cadence", "fast", "--sans-publication"], client=client)


def test_a_run_where_every_source_fails_exits_non_zero(monkeypatch, capsys) -> None:
    code = _run_main(monkeypatch, TROIS_SOURCES, always(500))

    assert code != 0
    sortie = capsys.readouterr()
    # Message clair, sur la sortie d'erreur, qui dit combien et quoi.
    assert "ÉCHEC" in sortie.err
    assert "3" in sortie.err
    assert "aucune offre" in sortie.err
    for nom in ("GitLab", "Malt", "Ramp"):
        assert f"KO   {nom}" in sortie.out


def test_a_run_where_only_one_source_fails_still_exits_zero(
    monkeypatch, capsys
) -> None:
    """Le quotidien d'une veille sur 38 sources : ce n'est pas un échec de run."""
    client = routing_client(
        {
            GREENHOUSE_HOST: (200, fixture("greenhouse_gitlab.json")),
            LEVER_HOST: (500, "erreur interne"),
            ASHBY_HOST: (200, fixture("ashby_ramp.json")),
        }
    )

    code = _run_main(monkeypatch, TROIS_SOURCES, client)

    assert code == 0
    sortie = capsys.readouterr().out
    assert "2 source(s) collectée(s), 1 en échec, 6 offre(s) brutes" in sortie


def test_a_fully_successful_run_exits_zero(monkeypatch, capsys) -> None:
    client = routing_client(
        {
            GREENHOUSE_HOST: (200, fixture("greenhouse_gitlab.json")),
            LEVER_HOST: (200, fixture("lever_malt.json")),
            ASHBY_HOST: (200, fixture("ashby_ramp.json")),
        }
    )

    code = _run_main(monkeypatch, TROIS_SOURCES, client)

    assert code == 0
    assert "3 source(s) collectée(s), 0 en échec, 9 offre(s) brutes" in (
        capsys.readouterr().out
    )


def test_the_run_names_the_sources_that_came_back_empty(monkeypatch, capsys) -> None:
    """« 3 collectées, 0 en échec » cacherait la source tarie : le bilan la
    nomme sur une ligne à part."""
    client = routing_client(
        {
            GREENHOUSE_HOST: (200, '{"jobs": []}'),
            LEVER_HOST: (200, fixture("lever_malt.json")),
            ASHBY_HOST: (200, fixture("ashby_ramp.json")),
        }
    )

    code = _run_main(monkeypatch, TROIS_SOURCES, client)

    assert code == 0
    sortie = capsys.readouterr().out
    assert "vide GitLab (greenhouse) : 0 offre" in sortie
    assert "1 source(s) sans aucune offre — GitLab" in sortie


def test_a_run_where_nothing_is_empty_says_nothing_about_it(
    monkeypatch, capsys
) -> None:
    """Pas de ligne de bruit quand tout va bien."""
    client = routing_client(
        {
            GREENHOUSE_HOST: (200, fixture("greenhouse_gitlab.json")),
            LEVER_HOST: (200, fixture("lever_malt.json")),
            ASHBY_HOST: (200, fixture("ashby_ramp.json")),
        }
    )

    _run_main(monkeypatch, TROIS_SOURCES, client)

    assert "sans aucune offre" not in capsys.readouterr().out


def test_a_cadence_with_no_source_at_all_is_not_an_error(monkeypatch, capsys) -> None:
    code = _run_main(monkeypatch, [], always(500))

    assert code == 0
    assert "aucune source" in capsys.readouterr().out


# ---------------------------------------------------------------------------
# US-2.4.2 — un retry, et un seul, sur les pannes passagères
# ---------------------------------------------------------------------------


def sequence_client(
    responses: list[tuple[int, str]], recorder: list[httpx.Request]
) -> httpx.Client:
    """Client qui sert `responses` dans l'ordre, une par appel."""

    def handler(request: httpx.Request) -> httpx.Response:
        recorder.append(request)
        status, body = responses[min(len(recorder), len(responses)) - 1]
        return httpx.Response(status, content=body)

    return httpx.Client(transport=httpx.MockTransport(handler))


def test_a_transient_failure_then_a_success_costs_exactly_two_calls() -> None:
    seen: list[httpx.Request] = []
    client = sequence_client(
        [(503, "indisponible"), (200, fixture("greenhouse_gitlab.json"))], seen
    )

    jobs = GreenhouseAdapter(client=client).fetch(GITLAB)

    assert len(seen) == 2
    # Et le retry sert vraiment : les offres viennent de la seconde réponse.
    assert len(jobs) == 3


def test_the_retry_waits_the_declared_backoff_before_calling_again(
    backoff_delays,
) -> None:
    seen: list[httpx.Request] = []
    client = sequence_client(
        [(503, "indisponible"), (200, fixture("greenhouse_gitlab.json"))], seen
    )

    GreenhouseAdapter(client=client).fetch(GITLAB)

    assert backoff_delays == list(RETRY_DELAYS)


def test_a_transient_failure_that_persists_gives_up_after_the_declared_attempts() -> None:
    seen: list[httpx.Request] = []
    client = routing_client({}, default=(503, "indisponible"), recorder=seen)

    with pytest.raises(AdapterError) as excinfo:
        GreenhouseAdapter(client=client).fetch(GITLAB)

    assert len(seen) == MAX_ATTEMPTS == 2
    assert "GitLab" in str(excinfo.value)
    assert "503" in str(excinfo.value)


def test_a_network_failure_is_retried_then_reported() -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        raise httpx.ConnectError("connexion refusée", request=request)

    client = httpx.Client(transport=httpx.MockTransport(handler))

    with pytest.raises(AdapterError) as excinfo:
        GreenhouseAdapter(client=client).fetch(GITLAB)

    assert len(seen) == MAX_ATTEMPTS
    assert "GitLab" in str(excinfo.value)


@pytest.mark.parametrize("status", [400, 401, 403, 410, 422])
def test_a_client_error_is_not_retried_because_it_will_not_fix_itself(status) -> None:
    seen: list[httpx.Request] = []
    client = routing_client({}, default=(status, "refusé"), recorder=seen)

    with pytest.raises(AdapterError):
        GreenhouseAdapter(client=client).fetch(GITLAB)

    assert len(seen) == 1


def test_a_rate_limit_is_not_retried_either() -> None:
    """429 veut dire « vous appelez trop » : rappeler serait le mauvais geste.
    La réponse du projet au débit, c'est `max_calls_per_day` et la cadence
    lente — d'où l'absence de 429 dans `RETRYABLE_STATUS`."""
    seen: list[httpx.Request] = []
    client = routing_client({}, default=(429, "trop d'appels"), recorder=seen)

    with pytest.raises(AdapterError):
        GreenhouseAdapter(client=client).fetch(GITLAB)

    assert 429 not in RETRYABLE_STATUS
    assert len(seen) == 1


def test_a_moved_board_is_not_retried() -> None:
    seen: list[httpx.Request] = []
    client = routing_client({}, default=(404, "Not Found"), recorder=seen)

    assert GreenhouseAdapter(client=client).fetch(GITLAB) == []
    assert len(seen) == 1


def test_a_source_that_keeps_failing_costs_the_run_two_calls_not_the_others() -> None:
    """Le retry vit dans l'adaptateur, l'isolation dans le Collector : une
    source en panne coûte ses deux appels, et rien de plus."""
    seen: list[httpx.Request] = []
    client = routing_client(
        {
            GREENHOUSE_HOST: (200, fixture("greenhouse_gitlab.json")),
            LEVER_HOST: (503, "indisponible"),
            ASHBY_HOST: (200, fixture("ashby_ramp.json")),
        },
        recorder=seen,
    )

    report = collect_sources(TROIS_SOURCES, client=client)

    appels_lever = [r for r in seen if LEVER_HOST in str(r.url)]
    assert len(appels_lever) == MAX_ATTEMPTS
    assert len(report.succeeded) == 2


# ---------------------------------------------------------------------------
# US-2.4.2 — timeout par requête
# ---------------------------------------------------------------------------


def sent_timeout(request: httpx.Request) -> float:
    """Le timeout de lecture réellement posé sur la requête."""
    return request.extensions["timeout"]["read"]


def test_a_source_that_sleeps_past_the_timeout_is_cut_at_the_configured_limit() -> None:
    """Une source qui ne répond jamais : le timeout tranche, à la limite
    configurée, et l'échec est nommé."""
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        raise httpx.ReadTimeout("la source ne répond pas", request=request)

    client = httpx.Client(transport=httpx.MockTransport(handler))

    with pytest.raises(AdapterError) as excinfo:
        GreenhouseAdapter(client=client, timeout=5.0).fetch(GITLAB)

    assert "GitLab" in str(excinfo.value)
    assert {sent_timeout(request) for request in seen} == {5.0}


def test_a_source_that_times_out_does_not_stop_the_run() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if LEVER_HOST in str(request.url):
            raise httpx.ReadTimeout("la source ne répond pas", request=request)
        body = (
            fixture("greenhouse_gitlab.json")
            if GREENHOUSE_HOST in str(request.url)
            else fixture("ashby_ramp.json")
        )
        return httpx.Response(200, content=body)

    client = httpx.Client(transport=httpx.MockTransport(handler))

    report = collect_sources(TROIS_SOURCES, client=client)

    assert [result.source.nom for result in report.failed] == ["Malt"]
    assert len(report.jobs) == 6


@pytest.mark.parametrize("case", ALL_ADAPTERS, ids=ALL_ADAPTER_IDS)
def test_every_adapter_sends_its_declared_timeout_on_every_request(case) -> None:
    seen: list[httpx.Request] = []

    fetch_case(case, recorder=seen)

    assert seen, "l'adaptateur doit avoir émis au moins une requête"
    for request in seen:
        assert sent_timeout(request) == case.adapter_cls.default_timeout


def test_remoteok_gets_the_longer_timeout_its_api_requires() -> None:
    """Garde-fou : l'API répond en ~40 s, le défaut de 15 s l'exclurait."""
    assert RemoteOkAdapter.default_timeout > DEFAULT_TIMEOUT
    assert RemoteOkAdapter.default_timeout >= 40.0


def test_an_explicit_timeout_overrides_the_adapter_default() -> None:
    seen: list[httpx.Request] = []
    client = routing_client(
        {GREENHOUSE_HOST: (200, fixture("greenhouse_gitlab.json"))}, recorder=seen
    )

    GreenhouseAdapter(client=client, timeout=3.5).fetch(GITLAB)

    assert sent_timeout(seen[0]) == 3.5


# ---------------------------------------------------------------------------
# US-2.4.2 — le User-Agent part bien, sur chaque requête
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("case", ALL_ADAPTERS, ids=ALL_ADAPTER_IDS)
def test_every_request_carries_a_user_agent(case) -> None:
    """Y compris les requêtes de suite : Hacker News et les paginés en
    émettent plusieurs, et aucune ne doit partir anonyme."""
    seen: list[httpx.Request] = []

    fetch_case(case, recorder=seen)

    assert seen
    for request in seen:
        assert request.headers.get("user-agent")


@pytest.mark.parametrize("case", ATS_CASES, ids=[c.id for c in ATS_CASES])
def test_ats_requests_identify_jobradar_by_name(case) -> None:
    seen: list[httpx.Request] = []

    fetch_case(case, recorder=seen)

    for request in seen:
        assert request.headers["user-agent"] == USER_AGENT


@pytest.mark.parametrize("case", API_AGGREGATOR_CASES, ids=API_AGGREGATOR_IDS)
def test_aggregator_requests_use_the_browser_user_agent(case) -> None:
    """Les agrégateurs filtrent le UA « JobRadar » à tort (audit 2026-09-05)."""
    seen: list[httpx.Request] = []

    fetch_case(case, recorder=seen)

    for request in seen:
        assert request.headers["user-agent"] == BROWSER_USER_AGENT


def test_the_user_agent_is_sent_on_the_retry_too() -> None:
    """Le retry passe par le même chemin : il ne doit pas perdre les en-têtes."""
    seen: list[httpx.Request] = []
    client = sequence_client(
        [(503, "indisponible"), (200, fixture("greenhouse_gitlab.json"))], seen
    )

    GreenhouseAdapter(client=client).fetch(GITLAB)

    assert len(seen) == 2
    assert {request.headers["user-agent"] for request in seen} == {USER_AGENT}
