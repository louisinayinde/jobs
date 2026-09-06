"""Tests des adaptateurs agrégateurs (US-2.3.T).

Vérifie le parsing des formats hétérogènes — JSON, RSS, thread Hacker News —
contre des **réponses réelles figées**, capturées le 2026-09-05 sur les
endpoints publics et rejouées via `httpx.MockTransport`. Aucun test de ce
fichier n'accède au réseau.

Les invariants communs à tous les adaptateurs (schéma `RawJob`, 404, corps
malformé, erreur serveur) sont testés une seule fois, dans
`test_adapters.py`, sur le catalogue `ALL_ADAPTERS` : ce fichier ne contient
que ce qui est propre aux agrégateurs.
"""

from __future__ import annotations

import json
import logging

import httpx
import pytest
import yaml

from src.adapters import ADAPTERS, AGGREGATOR_ADAPTERS, ATS_ADAPTERS
from src.adapters.base import (
    BROWSER_USER_AGENT,
    USER_AGENT,
    AdapterError,
    parse_rss,
    resolve_html_entities,
    to_iso_utc,
)
from src.adapters.hackernews import HackerNewsAdapter, parse_headline
from src.adapters.landingjobs import company_from_url
from src.adapters.registry import load_registry
from src.adapters.remoteok import RemoteOkAdapter, repair_mojibake
from src.core.config import Source
from tests.adapter_cases import (
    AGGREGATOR_CASES,
    AGGREGATOR_IDS,
    API_AGGREGATOR_CASES,
    API_AGGREGATOR_IDS,
    CRAWLED_CASES,
    CRAWLED_IDS,
    FIXTURES,
    AdapterCase,
    fetch_case,
    responder,
)

AGGREGATORS_YAML = FIXTURES.parent.parent / "config" / "aggregators.yaml"

CASES = {case.id: case for case in AGGREGATOR_CASES}


def _fetch(ats: str):
    return fetch_case(CASES[ats])


def _witness(ats: str, index: int = 0):
    return _fetch(ats)[index]


# ---------------------------------------------------------------------------
# US-2.3.0 — socle : pas de token, en-têtes de navigateur, entreprise de l'offre
# ---------------------------------------------------------------------------


def test_every_aggregator_declares_that_it_needs_no_token() -> None:
    for adapter_cls in AGGREGATOR_ADAPTERS:
        assert adapter_cls.requires_token is False, adapter_cls.ats


def test_every_ats_still_requires_a_token() -> None:
    for adapter_cls in ATS_ADAPTERS:
        assert adapter_cls.requires_token is True, adapter_cls.ats


def test_registry_loads_an_aggregator_entry_without_token(tmp_path, caplog) -> None:
    """Une source d'agrégateur n'a pas de token : ce n'est pas une entrée
    malformée, le registre doit l'accepter (US-2.3.0)."""
    path = tmp_path / "aggregators.yaml"
    path.write_text(
        yaml.safe_dump([{"nom": "RemoteOK", "ats": "remoteok"}], sort_keys=False),
        encoding="utf-8",
    )

    with caplog.at_level(logging.WARNING):
        sources = load_registry(path)

    assert sources == [Source(nom="RemoteOK", ats="remoteok", token="")]
    assert not [r for r in caplog.records if r.levelno >= logging.WARNING]


def test_registry_still_rejects_an_ats_entry_without_token(tmp_path, caplog) -> None:
    """La tolérance est ciblée : un board ATS sans token reste inexploitable."""
    path = tmp_path / "sources.yaml"
    path.write_text(
        yaml.safe_dump([{"nom": "Acme", "ats": "greenhouse"}], sort_keys=False),
        encoding="utf-8",
    )

    with caplog.at_level(logging.WARNING):
        sources = load_registry(path)

    assert sources == []
    assert any("Acme" in r.getMessage() and "token" in r.getMessage() for r in caplog.records)


def test_real_aggregators_yaml_loads_all_thirteen_sources() -> None:
    """Douze sources d'API ou de flux (Feature 2.3) plus une source crawlée
    (Japan Dev, Feature 2.5) : le mécanisme d'accès change, pas le registre."""
    raw = yaml.safe_load(AGGREGATORS_YAML.read_text(encoding="utf-8"))

    sources = load_registry(AGGREGATORS_YAML)

    # Le registre est tolérant : une entrée cassée serait ignorée en silence.
    assert len(sources) == len(raw) == 13
    assert all(ADAPTERS[s.ats] in AGGREGATOR_ADAPTERS for s in sources)


@pytest.mark.parametrize("case", API_AGGREGATOR_CASES, ids=API_AGGREGATOR_IDS)
def test_aggregators_send_realistic_browser_headers(case: AdapterCase) -> None:
    """Sans ces en-têtes, 17 des sources répondent 403 à tort (audit 2026-09-05).

    Ne vaut que pour les agrégateurs d'**API et de flux** : une source
    crawlée s'identifie au contraire, voir le test suivant.
    """
    seen: list[httpx.Request] = []

    fetch_case(case, recorder=seen)

    assert seen, "l'adaptateur doit avoir émis au moins une requête"
    for request in seen:
        assert request.headers["user-agent"] == BROWSER_USER_AGENT
        assert request.headers["accept-language"] == "en-US,en;q=0.9"
        assert request.headers["referer"].startswith("http")


@pytest.mark.parametrize("case", CRAWLED_CASES, ids=CRAWLED_IDS)
def test_crawled_sources_identify_themselves_instead_of_faking_a_browser(
    case: AdapterCase,
) -> None:
    """La posture du projet, et le seul endroit où elle s'inverse.

    Un agrégateur expose un endpoint *fait pour* être consommé, et filtre le
    User-Agent « JobRadar » à tort : on lui envoie donc des en-têtes de
    navigateur pour ne pas être écarté par erreur. Un crawl, lui, charge des
    pages que le site n'a pas publiées comme une API — s'y présenter en
    Chrome empêcherait cet hôte de nous voir, de nous limiter, ou de nous
    exclure par un `User-agent: JobRadar` que `robots.py` respecte pourtant.

    D'où aussi l'absence de `Referer` : on ne vient d'aucune page.
    """
    seen: list[httpx.Request] = []

    fetch_case(case, recorder=seen)

    assert seen, "l'adaptateur doit avoir émis au moins une requête"
    for request in seen:
        assert request.headers["user-agent"] == USER_AGENT
        assert "referer" not in request.headers


@pytest.mark.parametrize("case", CRAWLED_CASES, ids=CRAWLED_IDS)
def test_the_name_we_crawl_under_is_the_one_robots_txt_can_exclude(
    case: AdapterCase,
) -> None:
    """Se nommer ne sert à rien si le nom envoyé n'est pas celui qu'on
    reconnaît dans `robots.txt` : un site nous excluerait sans effet."""
    from src.adapters.robots import ROBOTS_AGENT, parse_robots

    assert ROBOTS_AGENT in case.adapter_cls.headers["User-Agent"].lower()
    assert parse_robots(
        f"User-agent: {ROBOTS_AGENT}\nDisallow: /"
    ).blocks_everything


def test_ats_adapters_keep_the_honest_jobradar_user_agent() -> None:
    """Les en-têtes de navigateur sont réservés aux agrégateurs."""
    from tests.adapter_cases import ATS_CASES

    for case in ATS_CASES:
        seen: list[httpx.Request] = []
        fetch_case(case, recorder=seen)
        assert seen[0].headers["user-agent"] == USER_AGENT


@pytest.mark.parametrize("case", AGGREGATOR_CASES, ids=AGGREGATOR_IDS)
def test_company_comes_from_the_posting_not_from_the_source_name(
    case: AdapterCase,
) -> None:
    """Un agrégateur publie pour N entreprises : `Source.nom` ne nomme que la
    plateforme (US-2.3.0)."""
    jobs = fetch_case(case)

    assert jobs
    for job in jobs:
        assert job.entreprise
        assert job.entreprise != case.source.nom


def test_posting_without_identifiable_company_is_dropped_and_logged(caplog) -> None:
    """Règle anti-scam : sans employeur nommé, l'offre est écartée."""
    # Flux WWR réel, dont le titre du premier item perd son préfixe
    # « Entreprise: » — la seule trace de l'employeur chez WWR.
    feed = (FIXTURES / "wwr_programming.xml").read_text(encoding="utf-8")
    anonyme = feed.replace(
        "<title>Edfinity: Senior Software Engineer, remote</title>",
        "<title>Senior Software Engineer, remote</title>",
        1,
    )
    case = CASES["weworkremotely"]

    with caplog.at_level(logging.WARNING):
        jobs = case.build(responder(anonyme)).fetch(case.source)

    assert [job.titre for job in jobs] == [
        "AI/ML Engineer for an AI-Driven E-Commerce Platform",
        "Staff Software Engineer",
    ]
    warnings = [r.getMessage() for r in caplog.records if r.levelno == logging.WARNING]
    assert len(warnings) == 1
    assert "écartée" in warnings[0]
    assert "Senior Software Engineer, remote" in warnings[0]


def test_the_same_posting_seen_on_two_aggregators_stays_two_rawjobs() -> None:
    """L'ingestion ne rapproche rien : la dédup inter-sources est la
    responsabilité de la Feature 3.3, pas celle des adaptateurs."""
    poste = "Staff Systems Engineer, IT"

    nodesk = [job for job in _fetch("nodesk") if job.titre == poste]
    wwr_like = [
        job for job in _fetch("weworkremotely") if job.titre == "Staff Software Engineer"
    ]

    assert len(nodesk) == 1
    assert len(wwr_like) == 1
    # Même employeur possible, mais deux `RawJob` distincts : identifiants,
    # sources et URLs différents. Rien n'a été fusionné.
    assert nodesk[0].id != wwr_like[0].id
    assert nodesk[0].ats != wwr_like[0].ats
    assert nodesk[0].url != wwr_like[0].url


# ---------------------------------------------------------------------------
# Parseur RSS partagé (US-2.3.0)
# ---------------------------------------------------------------------------


def test_rss_parser_resolves_html_entities_that_are_undefined_in_xml() -> None:
    """`&rsquo;` (NoDesk) et `&nbsp;` (Jobspresso) sont valides en HTML mais
    font échouer un parseur XML strict."""
    assert resolve_html_entities("l&rsquo;offre") == "l’offre"
    assert resolve_html_entities("a&nbsp;b") == "a b"
    # Les cinq entités XML légales sont laissées intactes, sinon le document
    # deviendrait mal formé.
    assert resolve_html_entities("a &amp; b &lt;c&gt;") == "a &amp; b &lt;c&gt;"
    # Les références numériques ne sont pas concernées.
    assert resolve_html_entities("d&#8217;accord") == "d&#8217;accord"
    # Une entité inconnue est laissée telle quelle plutôt que supprimée.
    assert resolve_html_entities("&pasunentite;") == "&pasunentite;"


def test_nodesk_feed_with_an_undefined_entity_parses_instead_of_crashing() -> None:
    """La fixture NoDesk contient un `&rsquo;` réel : sans la résolution
    préalable, `ElementTree` lèverait « undefined entity »."""
    raw = (FIXTURES / "nodesk.xml").read_text(encoding="utf-8")
    assert "&rsquo;" in raw

    jobs = _fetch("nodesk")

    assert len(jobs) == 3


def test_rss_parser_indexes_namespaced_elements_by_local_name() -> None:
    source = Source(nom="Test", ats="test", token="")
    feed = """<?xml version="1.0"?>
    <rss version="2.0" xmlns:job="https://example.com">
      <channel>
        <item>
          <title>Backend Engineer</title>
          <link>https://example.com/job/1</link>
          <pubDate>Mon, 17 Aug 2026 19:21:19 +0000</pubDate>
          <job:company>Acme</job:company>
          <job:location>Berlin</job:location>
        </item>
      </channel>
    </rss>"""

    items = parse_rss(source, "test", feed)

    assert len(items) == 1
    assert items[0].title == "Backend Engineer"
    assert items[0].link == "https://example.com/job/1"
    assert items[0].date == "2026-08-17T19:21:19+00:00"
    assert items[0].extras == {"company": "Acme", "location": "Berlin"}


def test_rss_parser_on_a_non_xml_body_raises_naming_the_source() -> None:
    source = Source(nom="We Work Remotely", ats="weworkremotely", token="")

    with pytest.raises(AdapterError) as excinfo:
        parse_rss(source, "weworkremotely", "<rss><channel><item>")

    assert "We Work Remotely" in str(excinfo.value)


def test_rss_pubdate_is_normalised_like_every_other_date_format() -> None:
    """Les flux RSS datent en RFC-822, les ATS en ISO ou en epoch : le
    pipeline ne doit voir qu'un seul format."""
    assert to_iso_utc("Mon, 17 Aug 2026 19:21:19 +0000") == "2026-08-17T19:21:19+00:00"
    assert to_iso_utc("Fri, 04 Sep 2026 08:00:00 +0200") == "2026-09-04T06:00:00+00:00"
    assert to_iso_utc("pas une date") == ""


# ---------------------------------------------------------------------------
# US-2.3.1 — RemoteOK
# ---------------------------------------------------------------------------


def test_remoteok_skips_the_legal_header_entry_and_parses_the_real_postings() -> None:
    """La première entrée du tableau n'est pas une offre mais les mentions
    légales de l'API."""
    payload = json.loads((FIXTURES / "remoteok.json").read_text(encoding="utf-8"))
    assert "legal" in payload[0], "la fixture doit contenir l'entête légale"
    assert len(payload) == 4

    jobs = _fetch("remoteok")

    assert len(jobs) == 3
    assert all("legal" not in job.titre.lower() for job in jobs)

    witness = jobs[0]
    assert witness.id == "1137302"
    assert witness.entreprise == "Warehance"
    assert witness.titre == "Customer Support & Success Specialist"
    assert witness.url == (
        "https://remoteOK.com/remote-jobs/"
        "remote-customer-support-success-specialist-warehance-1137302"
    )
    assert witness.date == "2026-09-03T19:12:26+00:00"
    assert witness.remote_type == "remote"


def test_remoteok_gets_a_timeout_of_its_own_because_it_answers_in_40_seconds() -> None:
    assert RemoteOkAdapter.default_timeout == 60.0
    assert RemoteOkAdapter()._timeout == 60.0
    # Un timeout explicite reste prioritaire.
    assert RemoteOkAdapter(timeout=5.0)._timeout == 5.0


def test_remoteok_stays_remote_even_when_the_location_names_a_city() -> None:
    """RemoteOK ne publie que du télétravail : « Ipojuca » est une préférence
    de pays, pas un poste sur site."""
    jobs = _fetch("remoteok")

    assert [job.remote_type for job in jobs] == ["remote", "remote", "remote"]
    assert jobs[1].localisation == "Ipojuca"


def test_remoteok_descriptions_are_repaired_from_the_sources_double_encoding() -> None:
    """RemoteOK sert ses textes non-anglais en mojibake : le défaut est dans
    ses données, on le corrige à la lecture."""
    jobs = _fetch("remoteok")

    portugais = jobs[1].description
    assert "referência" in portugais
    assert "Ãª" not in portugais


def test_mojibake_repair_leaves_correct_text_untouched() -> None:
    assert repair_mojibake("texte déjà correct — ê") == (
        "texte déjà correct — ê"
    )
    assert repair_mojibake("") == ""
    assert repair_mojibake("Youâll") == "You’ll"


# ---------------------------------------------------------------------------
# US-2.3.2 — Remotive
# ---------------------------------------------------------------------------


def test_remotive_witness_values() -> None:
    witness = _witness("remotive", 1)

    assert witness.id == "2091101"
    assert witness.entreprise == "Lemon.io"
    assert witness.titre == "Senior React Full-stack Developer"
    assert witness.localisation == "LATAM, Europe, USA, Canada, APAC"
    assert witness.remote_type == "remote"
    assert witness.url == (
        "https://remotive.com/remote-jobs/software-development/"
        "senior-react-full-stack-developer-2091101"
    )
    assert witness.date == "2026-08-27T14:36:09+00:00"
    assert "<" not in witness.description


def test_remotive_search_term_comes_from_the_token() -> None:
    seen: list[httpx.Request] = []

    fetch_case(CASES["remotive"], recorder=seen)

    assert str(seen[0].url) == "https://remotive.com/api/remote-jobs?search=engineer"


# ---------------------------------------------------------------------------
# US-2.3.3 — We Work Remotely (RSS)
# ---------------------------------------------------------------------------


def test_wwr_extracts_title_link_and_description_exactly_for_one_witness() -> None:
    witness = _witness("weworkremotely")

    # WWR n'a pas d'élément « entreprise » : elle est le préfixe du titre.
    assert witness.entreprise == "Edfinity"
    assert witness.titre == "Senior Software Engineer, remote"
    assert witness.url == (
        "https://weworkremotely.com/remote-jobs/edfinity-senior-software-engineer-remote"
    )
    assert witness.localisation == "Anywhere in the World"
    assert witness.date == "2026-08-17T19:21:19+00:00"
    assert witness.description.startswith("Headquarters: Austin, TX")
    assert "<img" not in witness.description
    assert "<" not in witness.description


def test_wwr_category_comes_from_the_token_with_a_documented_default() -> None:
    seen: list[httpx.Request] = []
    case = CASES["weworkremotely"]

    # Sans token : catégorie par défaut.
    fetch_case(case, recorder=seen)
    assert str(seen[0].url) == (
        "https://weworkremotely.com/categories/remote-programming-jobs.rss"
    )

    seen.clear()
    design = Source(nom="We Work Remotely", ats="weworkremotely", token="remote-design-jobs")
    case.build(responder(*case.bodies(), recorder=seen)).fetch(design)
    assert str(seen[0].url) == (
        "https://weworkremotely.com/categories/remote-design-jobs.rss"
    )


# ---------------------------------------------------------------------------
# US-2.3.4 — Hacker News « Who is Hiring »
# ---------------------------------------------------------------------------


def test_hn_extracts_company_and_location_from_three_known_comments() -> None:
    jobs = _fetch("hackernews")

    assert len(jobs) == 3
    assert [(job.entreprise, job.localisation, job.remote_type) for job in jobs] == [
        ("Modash.io", "Remote (Europe)", "remote"),
        ("Open Education Applications / Neon", "Utrecht, The Netherlands", "hybrid"),
        ("Snout", "Remote US or Ontario, Canada", "remote"),
    ]
    assert [job.titre for job in jobs] == [
        "Senior Product Engineer",
        "Senior/Lead Platform & DevOps Engineer, Senior Frontend Engineer, "
        "Senior Full-Stack Engineer",
        "Multiple Engineering + Product Roles",
    ]
    assert jobs[0].url == "https://news.ycombinator.com/item?id=49522903"
    assert jobs[0].date == "2026-09-01T15:01:54+00:00"


def test_hn_one_malformed_comment_does_not_interrupt_the_others(caplog) -> None:
    """La fixture contient un commentaire hors sujet, sans barre verticale."""
    thread = json.loads((FIXTURES / "hn_thread.json").read_text(encoding="utf-8"))
    assert len(thread["children"]) == 4, "la fixture doit contenir le commentaire cassé"

    with caplog.at_level(logging.WARNING):
        jobs = _fetch("hackernews")

    assert len(jobs) == 3
    warnings = [r.getMessage() for r in caplog.records if r.levelno == logging.WARNING]
    assert len(warnings) == 1
    assert "4DWW" in warnings[0]
    assert "écartée" in warnings[0]


def test_hn_picks_the_who_is_hiring_thread_not_who_wants_to_be_hired() -> None:
    """Le compte `whoishiring` publie deux fils par mois : seul celui des
    employeurs nous intéresse."""
    seen: list[httpx.Request] = []
    stories = json.loads((FIXTURES / "hn_stories.json").read_text(encoding="utf-8"))
    assert stories["hits"][1]["title"].startswith("Ask HN: Who wants to be hired?")

    fetch_case(CASES["hackernews"], recorder=seen)

    assert len(seen) == 2
    assert "search_by_date" in str(seen[0].url)
    # 49522897 = « Who is hiring? (September 2026) », le premier hit ;
    # 49522896 = « Who wants to be hired? », qu'il ne faut pas suivre.
    assert str(seen[1].url) == "https://hn.algolia.com/api/v1/items/49522897"


def test_hn_without_a_who_is_hiring_thread_returns_nothing_and_says_so(caplog) -> None:
    autres = json.dumps({"hits": [{"objectID": "1", "title": "Ask HN: Who wants to be hired?"}]})
    case = CASES["hackernews"]

    with caplog.at_level(logging.WARNING):
        jobs = case.build(responder(autres)).fetch(case.source)

    assert jobs == []
    assert any("Who is hiring" in r.getMessage() for r in caplog.records)


@pytest.mark.parametrize(
    "headline, expected",
    [
        (
            "Modash.io | Senior Product Engineer | Remote (Europe) | Full-time | "
            "€75k–110k | https://modash.io",
            ("Modash.io", "Senior Product Engineer", "Remote (Europe)"),
        ),
        # L'URL du site collée au nom de l'entreprise est retirée.
        (
            "Snout https://snout.com/ | Multiple Roles | Remote US | Full Time",
            ("Snout", "Multiple Roles", "Remote US"),
        ),
        # Le lieu prime sur le champ qui ne porte que le mode de travail.
        (
            "Acme | Backend Engineer | Utrecht, The Netherlands | HYBRID",
            ("Acme", "Backend Engineer", "Utrecht, The Netherlands"),
        ),
        # Faute de lieu, le mode de travail seul est retenu.
        (
            "Acme | Backend Engineer | REMOTE | Full-time | $150k",
            ("Acme", "Backend Engineer", "REMOTE"),
        ),
        # Ni contrat, ni salaire, ni URL ne passent pour un lieu.
        (
            "Acme | Backend Engineer | Full-time | $150k | https://acme.com",
            ("Acme", "Backend Engineer", ""),
        ),
        # Hors convention : pas d'entreprise, donc offre écartée en amont.
        ("Please normalize 4DWW", ("", "Please normalize 4DWW", "")),
    ],
)
def test_hn_headline_parsing(headline, expected) -> None:
    assert parse_headline(headline) == expected


def test_hn_comment_url_points_at_the_comment_itself() -> None:
    assert HackerNewsAdapter.ats == "hackernews"
    for job in _fetch("hackernews"):
        assert job.url == f"https://news.ycombinator.com/item?id={job.id}"


# ---------------------------------------------------------------------------
# US-2.3.5 — Himalayas & Working Nomads
# ---------------------------------------------------------------------------


def test_himalayas_witness_values() -> None:
    witness = _witness("himalayas", 1)

    assert witness.id == "senior-devops-engineer"
    assert witness.entreprise == "Nextiva"
    assert witness.titre == "Senior DevOps Engineer"
    assert witness.localisation == "Mexico"
    assert witness.remote_type == "remote"
    assert witness.url == "https://himalayas.app/companies/nextiva/jobs/senior-devops-engineer"
    # `pubDate` vaut 1788602108, un epoch en secondes et non une date ISO.
    assert witness.date == "2026-09-05T09:55:08+00:00"


def test_workingnomads_witness_values_and_id_read_from_the_url() -> None:
    witness = _witness("workingnomads")

    # Le flux n'a pas de champ « id » : il est dans l'URL de redirection.
    assert witness.id == "1835309"
    assert witness.entreprise == "Peroptyx"
    assert witness.titre == "AI Content Analyst (No Experience Required)"
    assert witness.localisation == "Australia"
    assert witness.remote_type == "remote"
    assert witness.date == "2026-09-04T16:15:04+00:00"


# ---------------------------------------------------------------------------
# US-2.3.6 — NoDesk, Jobspresso, EU Remote Jobs
# ---------------------------------------------------------------------------


def test_jobspresso_and_euremotejobs_share_one_wp_job_manager_adapter() -> None:
    """Décision de l'US : deux classes, pas trois. Les deux flux WP Job
    Manager partagent la même, NoDesk a la sienne."""
    from src.adapters.wpjobmanager import WpJobManagerAdapter

    assert issubclass(ADAPTERS["jobspresso"], WpJobManagerAdapter)
    assert issubclass(ADAPTERS["euremotejobs"], WpJobManagerAdapter)
    assert not issubclass(ADAPTERS["nodesk"], WpJobManagerAdapter)
    # Chaque site reste un `ats` distinct : la config n'a pas à porter d'URL.
    assert ADAPTERS["jobspresso"].feed_url != ADAPTERS["euremotejobs"].feed_url


def test_euremotejobs_reads_the_job_feed_not_the_blog_feed() -> None:
    """Correction d'audit : `/feed/` ne sert que des articles de blog."""
    seen: list[httpx.Request] = []

    fetch_case(CASES["euremotejobs"], recorder=seen)

    assert str(seen[0].url) == "https://euremotejobs.com/?feed=job_feed"


def test_wp_job_manager_reads_the_company_from_its_dedicated_element() -> None:
    jobspresso = _witness("jobspresso")
    assert jobspresso.id == "163413"
    assert jobspresso.entreprise == "Hopper"
    assert jobspresso.titre == "Principal Product Manager, Conversational AI"
    assert jobspresso.localisation == "Various US States"
    assert jobspresso.date == "2026-08-29T02:12:12+00:00"

    euremote = _witness("euremotejobs")
    assert euremote.entreprise == "Yfood Labs"
    assert euremote.localisation == "Germany"
    # Pas de `<post-id>` dans ce flux : le slug de l'URL fait l'identifiant.
    assert euremote.id == "ausendienst-im-handel-gebietsverkaufsleiter-stuttgart-m-w-d"


def test_wp_job_manager_prefers_the_full_content_over_the_truncated_summary() -> None:
    """`<description>` est tronqué, `<content:encoded>` est complet."""
    witness = _witness("jobspresso")

    assert len(witness.description) > 5000
    assert "<" not in witness.description


def test_nodesk_reads_the_company_from_the_title_suffix() -> None:
    witness = _witness("nodesk")

    assert witness.entreprise == "GitLab"
    assert witness.titre == "Staff Systems Engineer, IT"
    assert witness.url == "https://nodesk.co/remote-jobs/gitlab-staff-systems-engineer-it/"
    assert witness.date == "2026-09-04T06:00:00+00:00"
    # Le flux ne porte aucune localisation : la valeur par défaut est vide,
    # pas une invention.
    assert witness.localisation == ""


def test_nodesk_title_without_the_separator_yields_no_company() -> None:
    feed = (FIXTURES / "nodesk.xml").read_text(encoding="utf-8")
    anonyme = feed.replace(
        "<title>Staff Systems Engineer, IT at GitLab</title>",
        "<title>Staff Systems Engineer</title>",
        1,
    )
    case = CASES["nodesk"]

    jobs = case.build(responder(anonyme)).fetch(case.source)

    assert [job.entreprise for job in jobs] == ["Graphy", "General Assembly"]


# ---------------------------------------------------------------------------
# US-2.3.7 — Landing.jobs
# ---------------------------------------------------------------------------


def test_landingjobs_witness_values() -> None:
    witness = _witness("landingjobs", 2)

    assert witness.id == "19410"
    assert witness.titre == "(Senior) Data Engineer"
    assert witness.localisation == "Munich, DE; Lisbon, PT; Cologne, DE"
    assert witness.date == "2026-03-17T10:23:40+00:00"
    assert "<" not in witness.description


def test_landingjobs_company_is_reconstructed_from_the_url_slug() -> None:
    """L'API ne renvoie pas le nom de l'employeur : il n'est que dans l'URL."""
    payload = json.loads((FIXTURES / "landingjobs.json").read_text(encoding="utf-8"))
    assert "company" not in payload[0]

    assert [job.entreprise for job in _fetch("landingjobs")] == [
        "Inscale",
        "Damia Group Portugal",
        "Ki Performance",
    ]


@pytest.mark.parametrize(
    "url, expected",
    [
        ("https://landing.jobs/at/inscale/senior-java-dev", "Inscale"),
        ("https://landing.jobs/at/damia-group-portugal/backend", "Damia Group Portugal"),
        # URL hors convention : pas d'entreprise, l'offre sera écartée.
        ("https://landing.jobs/jobs/12345", ""),
        ("", ""),
    ],
)
def test_landingjobs_company_from_url(url, expected) -> None:
    assert company_from_url(url) == expected


def test_landingjobs_description_joins_the_three_html_blocks() -> None:
    """Les technos vivent dans `main_requirements` et `nice_to_have`, pas
    dans la seule `role_description`."""
    payload = json.loads((FIXTURES / "landingjobs.json").read_text(encoding="utf-8"))
    witness = _witness("landingjobs")

    assert len(witness.description) > len(payload[0]["role_description"])
    assert "Spring Boot" in witness.description


# ---------------------------------------------------------------------------
# US-2.3.8 — Free-Work
# ---------------------------------------------------------------------------


def test_freework_witness_values() -> None:
    witness = _witness("freework")

    assert witness.id == "661682"
    assert witness.entreprise == "Avanda"
    assert witness.titre == "Acheteur IT"
    assert witness.localisation == "Martigny, Valais, Suisse"
    assert witness.date == "2026-09-05T11:02:56+00:00"
    # L'URL publique n'est pas renvoyée par l'API : elle est reconstruite
    # depuis le slug du métier et celui de l'offre.
    assert witness.url == (
        "https://www.free-work.com/fr/tech-it/acheteur-euse/job-mission/acheteur-it-29"
    )


def test_freework_paginates_until_the_total_is_reached() -> None:
    """La réponse annonce `hydra:totalItems` : inutile de redemander une page
    une fois le compte atteint."""
    seen: list[httpx.Request] = []

    fetch_case(CASES["freework"], recorder=seen)

    # La fixture contient 3 offres pour un total annoncé de 3 : une requête.
    assert len(seen) == 1
    assert str(seen[0].url) == (
        "https://www.free-work.com/api/job_postings?contracts=permanent&page=1"
    )


def test_freework_stops_at_max_pages_on_a_catalogue_that_never_ends() -> None:
    """Garde-fou de politesse : on ne parcourt pas les 147 pages à chaque run."""
    from src.adapters.freework import MAX_PAGES

    payload = json.loads((FIXTURES / "freework.json").read_text(encoding="utf-8"))
    # Total volontairement énorme : rien n'arrête la pagination sauf MAX_PAGES.
    payload["hydra:totalItems"] = 100_000
    seen: list[httpx.Request] = []
    case = CASES["freework"]

    jobs = case.build(responder(json.dumps(payload), recorder=seen)).fetch(case.source)

    assert len(seen) == MAX_PAGES
    assert len(jobs) == 3 * MAX_PAGES


def test_freework_maps_its_remote_mode_vocabulary() -> None:
    from src.adapters.freework import FreeWorkAdapter

    payload = json.loads((FIXTURES / "freework.json").read_text(encoding="utf-8"))
    for entry, mode in zip(payload["hydra:member"], ("full", "partial", "none")):
        entry["remoteMode"] = mode
    payload["hydra:totalItems"] = 3
    case = CASES["freework"]

    jobs = FreeWorkAdapter(client=responder(json.dumps(payload))).fetch(case.source)

    assert [job.remote_type for job in jobs] == ["remote", "hybrid", "onsite"]


# ---------------------------------------------------------------------------
# US-2.3.9 — APEC
# ---------------------------------------------------------------------------


def test_apec_witness_values() -> None:
    witness = _witness("apec")

    assert witness.id == "179261918W"
    assert witness.entreprise == "CD Consulting"
    assert witness.titre == "Concepteur développeur (Développeur informatique) F/H"
    assert witness.localisation == "Wasquehal - 59"
    assert witness.date == "2026-09-02T16:51:05+00:00"
    assert witness.url == (
        "https://www.apec.fr/candidat/recherche-emploi.html/emploi/detail-offre/179261918W"
    )


def test_apec_is_queried_by_post_with_a_body_of_known_fields_only() -> None:
    """Le corps est strict : un champ inconnu fait répondre 500."""
    from src.adapters.apec import PAGE_SIZE

    seen: list[httpx.Request] = []

    fetch_case(CASES["apec"], recorder=seen)

    assert seen[0].method == "POST"
    assert str(seen[0].url) == "https://www.apec.fr/cms/webservices/rechercheOffre"

    body = json.loads(seen[0].content)
    assert body["motsCles"] == "developpeur"
    assert body["pagination"] == {"range": PAGE_SIZE, "startIndex": 0}
    # Champs vérifiés contre le serveur le 2026-09-05 : ajouter une clé hors
    # de cette liste ferait répondre 500.
    assert set(body) == {
        "lieux",
        "fonctions",
        "statutPoste",
        "typesContrat",
        "typesConvention",
        "niveauxExperience",
        "secteursActivite",
        "typesTeletravail",
        "sorts",
        "pagination",
        "activeFiltre",
        "motsCles",
    }


def test_apec_paginates_through_start_index() -> None:
    """La pagination n'est pas dans l'URL mais dans `pagination.startIndex`."""
    from src.adapters.apec import MAX_PAGES, PAGE_SIZE

    payload = json.loads((FIXTURES / "apec.json").read_text(encoding="utf-8"))
    # Une page pleine à chaque appel : seul MAX_PAGES arrête la boucle.
    payload["resultats"] = payload["resultats"] * (PAGE_SIZE // 3 + 1)
    payload["resultats"] = payload["resultats"][:PAGE_SIZE]
    payload["totalCount"] = 100_000
    seen: list[httpx.Request] = []
    case = CASES["apec"]

    case.build(responder(json.dumps(payload), recorder=seen)).fetch(case.source)

    assert len(seen) == MAX_PAGES
    start_indexes = [json.loads(r.content)["pagination"]["startIndex"] for r in seen]
    assert start_indexes == [page * PAGE_SIZE for page in range(MAX_PAGES)]


def test_apec_confidential_posting_without_a_company_is_dropped(caplog) -> None:
    payload = json.loads((FIXTURES / "apec.json").read_text(encoding="utf-8"))
    payload["resultats"][0]["nomCommercial"] = ""
    case = CASES["apec"]

    with caplog.at_level(logging.WARNING):
        jobs = case.build(responder(json.dumps(payload))).fetch(case.source)

    assert [job.entreprise for job in jobs] == ["SP SEARCH", "Akanea"]
    assert any("écartée" in r.getMessage() for r in caplog.records)
