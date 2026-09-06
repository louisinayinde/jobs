"""Tests de l'adaptateur générique sitemap + schema.org (US-2.5.T).

Le parsing est vérifié contre des **réponses réelles figées**, capturées le
2026-09-06 sur `japan-dev.com` et `europeremotely.com`, et rejouées via un
`httpx.MockTransport` : aucun test de ce fichier n'accède au réseau.

Ce que ces tests protègent tient en une phrase : **un crawl doit coûter le
moins de requêtes possible, et aucune là où c'est interdit.** D'où le fait
que la plupart d'entre eux comptent des requêtes plutôt que des offres.

Les invariants communs à tous les adaptateurs (schéma `RawJob`, 404, corps
malformé, erreur serveur) sont testés une fois pour toutes dans
`test_adapters.py`, sur le catalogue `ALL_ADAPTERS` — l'adaptateur crawlé
y figure et n'a donc pas à les redire ici.

Rappel de périmètre : l'US-2.5.4 (Welcome to the Jungle) n'est pas
couverte. Le site est passé derrière un pare-feu applicatif entre l'audit
et l'écriture de cette feature — voir `docs/sources-audit.md`. Les tests de
parsing qu'elle demandait sont joués sur Japan Dev, qui expose exactement
le même format.
"""

from __future__ import annotations

import gzip
import json
import logging
from dataclasses import asdict
from pathlib import Path

import httpx
import pytest

from src.adapters import GreenhouseAdapter, JapanDevAdapter
from src.adapters.jobposting import (
    INITIAL_WINDOW_DAYS,
    JobPostingAdapter,
    SlugFilter,
    extract_job_posting,
    job_location,
    organization_name,
    slug_text,
)
from src.adapters.robots import (
    DEFAULT_CRAWL_DELAY,
    NO_ROBOTS,
    RobotsRules,
    parse_robots,
)
from src.adapters.sitemap import SitemapUrl, decompress, parse_sitemap, select_new
from src.core.config import Source
from src.core.state import MAX_KNOWN_URLS, CrawlState
from tests.adapter_cases import FIXTURES, RAWJOB_KEYS, crawl_kwargs

JAPANDEV = Source(nom="Japan Dev", ats="japandev", token="")

ROBOTS_URL = "https://japan-dev.com/robots.txt"
INDEX_URL = "https://japan-dev.com/sitemap.xml"
SHARD_URL = "https://japan-dev.com/cdn/sitemaps/sitemap.xml"
ALPACA_URL = (
    "https://japan-dev.com/jobs/alpaca/alpaca-senior-software-engineer---clearing-qb5uw2"
)
FULLSTACK_URL = "https://japan-dev.com/jobs/abbeal/abbeal-senior-fullstack-developer-8cbk7p"
#: Écartée par le pré-filtre : son slug ne porte aucun des termes de
#: `title_include` (« ai engineer » n'est ni software, ni backend, ni developer).
AI_ENGINEER_URL = "https://japan-dev.com/jobs/abbeal/abbeal-ai-engineer-maj4uw"


def fixture(name: str) -> str:
    return (FIXTURES / name).read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# Outillage : un serveur figé qui répond selon l'URL, et compte les requêtes
# ---------------------------------------------------------------------------


class Serveur:
    """Répond selon l'URL demandée et retient tout ce qui a été appelé.

    Compter les requêtes est le cœur de cette feature : « ne pas charger la
    page » est le comportement à prouver dans la moitié des tests.
    """

    def __init__(self, routes: dict[str, tuple[int, bytes | str]]) -> None:
        self.routes = routes
        self.appels: list[str] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        self.appels.append(url)
        statut, corps = self.routes.get(url, (404, "Not Found"))
        if isinstance(corps, str):
            corps = corps.encode("utf-8")
        return httpx.Response(statut, content=corps)

    @property
    def client(self) -> httpx.Client:
        return httpx.Client(transport=httpx.MockTransport(self))

    def pages(self) -> list[str]:
        """Les seules requêtes qui chargent une page d'offre."""
        return [url for url in self.appels if "/jobs/" in url]


def routes_japandev(**surcharges: tuple[int, bytes | str]) -> dict:
    """Le site tel qu'il répond réellement, surchargeable route par route."""
    base = {
        ROBOTS_URL: (200, fixture("japandev_robots.txt")),
        INDEX_URL: (200, fixture("japandev_sitemap.xml")),
        SHARD_URL: (200, fixture("japandev_shard.xml")),
        ALPACA_URL: (200, fixture("japandev_job.html")),
        FULLSTACK_URL: (200, fixture("japandev_job.html")),
        AI_ENGINEER_URL: (200, fixture("japandev_job.html")),
    }
    base.update(surcharges)
    return base


def crawl(serveur: Serveur, *, state: CrawlState | None = None, **kwargs):
    """Lance un crawl complet contre `serveur`."""
    arguments = crawl_kwargs(**kwargs)
    if state is not None:
        arguments["state"] = state
    return JapanDevAdapter(client=serveur.client, **arguments).fetch(JAPANDEV)


# ---------------------------------------------------------------------------
# US-2.5.2 — extraction du bloc `JobPosting`, sur page réelle
# ---------------------------------------------------------------------------


def test_a_real_job_page_yields_a_rawjob_with_the_exact_known_values() -> None:
    """La page témoin, champ par champ, contre les valeurs relevées en direct."""
    serveur = Serveur(routes_japandev())

    jobs = crawl(serveur)

    temoin = next(job for job in jobs if job.url == ALPACA_URL)
    assert temoin.id == "alpaca-senior-software-engineer---clearing-qb5uw2"
    assert temoin.ats == "japandev"
    assert temoin.entreprise == "Alpaca"
    assert temoin.titre == "Senior Software Engineer - Clearing"
    # `jobLocation` emboîte Tokyo (locality), Tokyo (region) et JP (country) :
    # aplati sans répétition.
    assert temoin.localisation == "Tokyo, JP"
    # `jobLocationType: TELECOMMUTE` prime sur le libellé de localisation.
    assert temoin.remote_type == "remote"
    assert temoin.date == "2026-04-08T00:00:00+00:00"
    assert "100% remote" in temoin.description
    assert "<" not in temoin.description


def test_the_employment_type_is_carried_by_the_first_line_of_the_description() -> None:
    """`RawJob` n'a pas de champ « contrat » : l'info passe par la description,
    code schema.org compris, pour rester lisible par un filtre anglophone."""
    serveur = Serveur(routes_japandev())

    temoin = crawl(serveur)[0]

    assert temoin.description.startswith("Type de contrat : temps plein (FULL_TIME)")


def test_the_right_json_ld_block_is_picked_among_several() -> None:
    """La page réelle en publie trois — `WebSite`, `Organization`, `JobPosting`.
    Prendre le premier venu donnerait le nom du site, pas celui de l'offre."""
    html = fixture("japandev_job.html")

    assert html.count("application/ld+json") == 3

    bloc = extract_job_posting(html)

    assert bloc is not None
    assert bloc["@type"] == "JobPosting"
    assert bloc["title"] == "Senior Software Engineer - Clearing"


def test_a_faq_block_before_the_job_posting_does_not_win() -> None:
    """Cas classique d'une page SEO : la FAQ est déclarée avant l'offre."""
    html = (
        '<script type="application/ld+json">'
        '{"@type": "FAQPage", "name": "Questions fréquentes"}</script>'
        '<script type="application/ld+json">'
        '{"@type": "JobPosting", "title": "Backend Engineer"}</script>'
    )

    assert extract_job_posting(html)["title"] == "Backend Engineer"


def test_a_job_posting_nested_in_a_graph_is_found() -> None:
    """Les extensions SEO emballent tout dans `@graph`."""
    html = (
        '<script type="application/ld+json">'
        '{"@graph": [{"@type": "WebPage"}, {"@type": "JobPosting", "title": "SRE"}]}'
        "</script>"
    )

    assert extract_job_posting(html)["title"] == "SRE"


def test_a_block_with_broken_json_does_not_disqualify_the_others() -> None:
    html = (
        '<script type="application/ld+json">{ceci n\'est pas du JSON</script>'
        '<script type="application/ld+json">'
        '{"@type": "JobPosting", "title": "Data Engineer"}</script>'
    )

    assert extract_job_posting(html)["title"] == "Data Engineer"


def test_a_page_without_any_job_posting_is_skipped_logged_and_the_others_continue(
    caplog,
) -> None:
    """Une page qui a perdu son bloc ne doit pas coûter les suivantes."""
    serveur = Serveur(
        routes_japandev(**{ALPACA_URL: (200, "<html><body>rien ici</body></html>")})
    )

    with caplog.at_level(logging.WARNING):
        jobs = crawl(serveur)

    # Les deux pages ont bien été demandées, une seule a donné une offre.
    # (Les deux servent la même fixture : c'est le compte des pages
    # chargées, et non leur URL, qui distingue ici les deux issues.)
    assert len(serveur.pages()) == 2
    assert len(jobs) == 1
    assert any(
        "JobPosting" in record.getMessage() and ALPACA_URL in record.getMessage()
        for record in caplog.records
    )


def test_a_posting_without_a_hiring_organization_is_discarded(caplog) -> None:
    """Règle anti-scam d'`AggregatorAdapter` : sans employeur, pas d'offre."""
    anonyme = (
        '<script type="application/ld+json">'
        '{"@type": "JobPosting", "title": "Backend Engineer",'
        ' "url": "https://japan-dev.com/jobs/x/backend-engineer-1"}</script>'
    )
    serveur = Serveur(routes_japandev(**{ALPACA_URL: (200, anonyme)}))

    with caplog.at_level(logging.WARNING):
        jobs = crawl(serveur)

    assert len(serveur.pages()) == 2
    assert len(jobs) == 1
    assert any("entreprise non identifiable" in r.getMessage() for r in caplog.records)


@pytest.mark.parametrize(
    "valeur, attendu",
    [
        ({"@type": "Organization", "name": "Alpaca"}, "Alpaca"),
        ("Alpaca", "Alpaca"),
        ([{"name": ""}, {"name": "Alpaca"}], "Alpaca"),
        (None, ""),
        ({"@type": "Organization"}, ""),
    ],
)
def test_organization_name_accepts_every_shape_schema_org_allows(valeur, attendu) -> None:
    assert organization_name(valeur) == attendu


@pytest.mark.parametrize(
    "valeur, attendu",
    [
        (
            {"address": {"addressLocality": "Tokyo", "addressCountry": "JP"}},
            "Tokyo, JP",
        ),
        # Doublon Tokyo/Tokyo : aplati une seule fois.
        (
            {
                "address": {
                    "addressLocality": "Tokyo",
                    "addressRegion": "Tokyo",
                    "addressCountry": "JP",
                }
            },
            "Tokyo, JP",
        ),
        # Plusieurs lieux : joints, sans répétition.
        (
            [
                {"address": {"addressLocality": "Paris"}},
                {"address": {"addressLocality": "Lyon"}},
            ],
            "Paris, Lyon",
        ),
        ("Remote", "Remote"),
        (None, ""),
    ],
)
def test_job_location_flattens_every_shape(valeur, attendu) -> None:
    assert job_location(valeur) == attendu


# ---------------------------------------------------------------------------
# US-2.5.1 — pré-filtrage sur le slug, avant toute requête
# ---------------------------------------------------------------------------


def test_a_url_whose_slug_misses_the_filter_is_never_requested() -> None:
    """**Le test qui compte.** Le pré-filtre n'a d'intérêt que s'il évite la
    requête ; s'il filtrait après coup, il ne servirait à rien."""
    serveur = Serveur(routes_japandev())

    crawl(serveur)

    assert AI_ENGINEER_URL not in serveur.appels
    assert sorted(serveur.pages()) == sorted([ALPACA_URL, FULLSTACK_URL])


def test_the_sitemap_pages_that_are_not_offers_are_never_requested() -> None:
    """L'accueil et la page de listing figurent au sitemap : les charger pour
    y chercher un `JobPosting` absent serait deux requêtes gâchées par run."""
    serveur = Serveur(routes_japandev())

    crawl(serveur)

    assert "https://japan-dev.com/" not in serveur.appels
    assert "https://japan-dev.com/jobs" not in serveur.appels


def test_the_real_sitemap_prefilter_cuts_two_offers_out_of_three() -> None:
    """Chiffre relevé sur le sitemap complet : 290 offres → 67 pages
    candidates. La fixture en garde la proportion, sur trois offres."""
    serveur = Serveur(routes_japandev())

    crawl(serveur)

    assert len(serveur.pages()) == 2


@pytest.mark.parametrize(
    "url, attendu",
    [
        (ALPACA_URL, "alpaca senior software engineer clearing qb5uw2"),
        ("https://x.tld/jobs/a/senior_backend_engineer", "senior backend engineer"),
    ],
)
def test_slug_text_turns_a_url_into_something_comparable_to_a_title(url, attendu) -> None:
    assert slug_text(url) == attendu


def test_an_empty_include_list_lets_everything_through() -> None:
    """Pas de terme = pas de filtre : le crawl reste borné par le plafond de
    pages, pas par une liste vide qu'on interpréterait comme « rien »."""
    filtre = SlugFilter(include=(), exclude=("engineering manager",))

    assert filtre.matches("https://x.tld/jobs/a/data-analyst-42")
    assert not filtre.matches("https://x.tld/jobs/a/engineering-manager-42")


def test_exclude_wins_over_include() -> None:
    filtre = SlugFilter(include=("engineer",), exclude=("sales engineer",))

    assert not filtre.matches("https://x.tld/jobs/a/sales-engineer-42")


def test_an_unreadable_filters_file_neutralises_the_prefilter(tmp_path, caplog) -> None:
    """Une config cassée ne doit pas faire échouer la collecte : le filtre
    devient neutre, et le plafond de pages garde le crawl borné."""
    absent = tmp_path / "pas-de-filtres.yaml"

    with caplog.at_level(logging.WARNING):
        filtre = SlugFilter.from_filters(absent)

    assert filtre == SlugFilter()
    assert any("pré-filtre" in record.getMessage() for record in caplog.records)


# ---------------------------------------------------------------------------
# US-2.5.0 — sitemap incrémental
# ---------------------------------------------------------------------------

#: Trois offres datées, dont une seule postérieure au curseur du test.
SITEMAP_DATE = """<?xml version="1.0" encoding="UTF-8"?>
<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">
  <url><loc>https://japan-dev.com/jobs/a/backend-engineer-vieille</loc>
       <lastmod>2026-01-10</lastmod></url>
  <url><loc>https://japan-dev.com/jobs/a/backend-engineer-moyenne</loc>
       <lastmod>2026-02-10</lastmod></url>
  <url><loc>https://japan-dev.com/jobs/a/backend-engineer-recente</loc>
       <lastmod>2026-03-10</lastmod></url>
</urlset>
"""

RECENTE_URL = "https://japan-dev.com/jobs/a/backend-engineer-recente"


def test_only_the_page_modified_since_the_last_run_is_loaded(tmp_path) -> None:
    """Trois `<lastmod>`, un seul postérieur au dernier run → une seule page."""
    etat = CrawlState(tmp_path / "crawl.json")
    etat.record("japandev", cursor="2026-02-20T00:00:00+00:00", urls=set())
    serveur = Serveur(
        routes_japandev(
            **{
                SHARD_URL: (200, SITEMAP_DATE),
                RECENTE_URL: (200, fixture("japandev_job.html")),
            }
        )
    )

    crawl(serveur, state=etat)

    assert serveur.pages() == [RECENTE_URL]


def test_the_cursor_advances_to_the_newest_date_seen(tmp_path) -> None:
    """Sans quoi la même page serait rechargée à chaque run."""
    chemin = tmp_path / "crawl.json"
    etat = CrawlState(chemin)
    etat.record("japandev", cursor="2026-02-20T00:00:00+00:00", urls=set())
    serveur = Serveur(
        routes_japandev(
            **{
                SHARD_URL: (200, SITEMAP_DATE),
                RECENTE_URL: (200, fixture("japandev_job.html")),
            }
        )
    )

    crawl(serveur, state=etat)

    assert CrawlState.load(chemin).cursor("japandev") == "2026-03-10T00:00:00+00:00"


def test_a_second_run_with_nothing_new_loads_no_page_at_all(tmp_path) -> None:
    chemin = tmp_path / "crawl.json"
    serveur = Serveur(routes_japandev())

    crawl(serveur, state=CrawlState.load(chemin))
    premier = len(serveur.pages())
    crawl(serveur, state=CrawlState.load(chemin))

    assert premier == 2
    # Le second run se limite au repérage : robots.txt, index, sitemap.
    assert len(serveur.pages()) == 2


def test_a_sitemap_without_lastmod_falls_back_to_urls_already_loaded(tmp_path) -> None:
    """Le repli documenté, et le cas réel de Japan Dev : aucune date au
    sitemap, donc l'incrémental se repère sur l'identité de l'URL."""
    chemin = tmp_path / "crawl.json"
    assert "<lastmod>" not in fixture("japandev_shard.xml")

    crawl(Serveur(routes_japandev()), state=CrawlState.load(chemin))

    assert CrawlState.load(chemin).known_urls("japandev") == {ALPACA_URL, FULLSTACK_URL}


def test_a_url_filtered_out_never_enters_the_known_set(tmp_path) -> None:
    """Elle n'a pas été chargée : la marquer « vue » interdirait de la
    reprendre le jour où `filters.yaml` s'élargit."""
    chemin = tmp_path / "crawl.json"

    crawl(Serveur(routes_japandev()), state=CrawlState.load(chemin))

    assert AI_ENGINEER_URL not in CrawlState.load(chemin).known_urls("japandev")


def test_an_offer_that_leaves_the_sitemap_leaves_the_state(tmp_path) -> None:
    """Sinon le fichier d'état gonflerait sans fin, commit après commit."""
    chemin = tmp_path / "crawl.json"
    etat = CrawlState(chemin)
    etat.record(
        "japandev",
        cursor="",
        urls={ALPACA_URL, FULLSTACK_URL, "https://japan-dev.com/jobs/a/offre-retiree"},
    )
    etat.save()

    crawl(Serveur(routes_japandev()), state=CrawlState.load(chemin))

    connues = CrawlState.load(chemin).known_urls("japandev")
    assert "https://japan-dev.com/jobs/a/offre-retiree" not in connues


def test_the_page_cap_holds_the_cursor_back_so_nothing_is_skipped(tmp_path) -> None:
    """Le piège : plafonner les pages **et** avancer le curseur jusqu'au bout
    ferait disparaître en silence les offres qu'on n'a pas eu le temps de
    charger. Le curseur s'arrête à la dernière page réellement chargée."""

    class UneSeulePage(JapanDevAdapter):
        max_pages_per_run = 1

    chemin = tmp_path / "crawl.json"
    etat = CrawlState(chemin)
    # Curseur posé entre la plus vieille et les deux autres : deux offres
    # nouvelles, un plafond d'une page.
    etat.record("japandev", cursor="2026-01-20T00:00:00+00:00", urls=set())
    serveur = Serveur(
        routes_japandev(
            **{
                SHARD_URL: (200, SITEMAP_DATE),
                "https://japan-dev.com/jobs/a/backend-engineer-moyenne": (
                    200,
                    fixture("japandev_job.html"),
                ),
                RECENTE_URL: (200, fixture("japandev_job.html")),
            }
        )
    )

    UneSeulePage(client=serveur.client, **crawl_kwargs(state=etat)).fetch(JAPANDEV)

    # La plus ancienne des deux nouvelles est passée en premier, et le
    # curseur s'est arrêté sur elle : la plus récente reste à faire.
    assert serveur.pages() == ["https://japan-dev.com/jobs/a/backend-engineer-moyenne"]
    assert CrawlState.load(chemin).cursor("japandev") == "2026-02-10T00:00:00+00:00"


def test_a_sitemap_index_is_followed_to_its_children() -> None:
    """Le sitemap racine de Japan Dev est un **index** : sans le suivre, on
    ne verrait pas une seule offre."""
    serveur = Serveur(routes_japandev())

    crawl(serveur)

    assert serveur.appels[:3] == [ROBOTS_URL, INDEX_URL, SHARD_URL]


def test_a_gzipped_sitemap_is_decompressed() -> None:
    """L'usage veut qu'un gros sitemap soit publié en `.xml.gz`, et `httpx`
    ne le décompresse pas : il est servi comme un fichier gzip entier, pas
    comme une réponse à `Content-Encoding`."""
    serveur = Serveur(
        routes_japandev(
            **{SHARD_URL: (200, gzip.compress(fixture("japandev_shard.xml").encode()))}
        )
    )

    jobs = crawl(serveur)

    assert len(jobs) == 2


def test_decompress_leaves_plain_bytes_untouched() -> None:
    assert decompress(b"<urlset/>") == b"<urlset/>"


def test_a_corrupt_sitemap_raises_an_adapter_error_naming_the_source() -> None:
    from src.adapters.base import AdapterError

    with pytest.raises(AdapterError) as excinfo:
        parse_sitemap(JAPANDEV, "japandev", b"<urlset><url>")

    assert "Japan Dev" in str(excinfo.value)


def test_select_new_sorts_oldest_first_so_capping_never_skips() -> None:
    urls = [
        SitemapUrl("https://x.tld/c", "2026-03-01T00:00:00+00:00"),
        SitemapUrl("https://x.tld/a", "2026-01-01T00:00:00+00:00"),
        SitemapUrl("https://x.tld/b", "2026-02-01T00:00:00+00:00"),
    ]

    retenues = select_new(urls, cursor="", known=set())

    assert [url.loc for url in retenues] == [
        "https://x.tld/a",
        "https://x.tld/b",
        "https://x.tld/c",
    ]


def test_the_first_run_starts_from_a_bounded_window(tmp_path) -> None:
    """Sans curseur, « depuis le dernier run » désignerait 240 000 pages."""
    from datetime import datetime, timedelta, timezone

    chemin = tmp_path / "crawl.json"
    trop_vieille = (
        datetime.now(timezone.utc) - timedelta(days=INITIAL_WINDOW_DAYS + 1)
    ).isoformat()
    recente = datetime.now(timezone.utc).isoformat()
    sitemap = (
        '<?xml version="1.0"?><urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">'
        f"<url><loc>https://japan-dev.com/jobs/a/backend-engineer-vieille</loc>"
        f"<lastmod>{trop_vieille}</lastmod></url>"
        f"<url><loc>{RECENTE_URL}</loc><lastmod>{recente}</lastmod></url>"
        "</urlset>"
    )
    serveur = Serveur(
        routes_japandev(
            **{
                SHARD_URL: (200, sitemap),
                RECENTE_URL: (200, fixture("japandev_job.html")),
            }
        )
    )

    crawl(serveur, state=CrawlState.load(chemin))

    assert serveur.pages() == [RECENTE_URL]


# ---------------------------------------------------------------------------
# US-2.5.3 — robots.txt
# ---------------------------------------------------------------------------


def test_a_site_that_disallows_everything_gets_no_page_request(caplog) -> None:
    """**Non négociable.** Europe Remotely publie `Disallow: /` pour `*` :
    la source est désactivée, pas contournée.

    Une requête part quand même — celle du `robots.txt` lui-même, qu'il faut
    bien lire pour connaître la règle. Ce qui doit rester à zéro, c'est le
    reste : ni sitemap, ni page d'offre.
    """
    serveur = Serveur(
        routes_japandev(**{ROBOTS_URL: (200, fixture("europeremotely_robots.txt"))})
    )

    with caplog.at_level(logging.WARNING):
        jobs = crawl(serveur)

    assert jobs == []
    assert serveur.appels == [ROBOTS_URL]
    assert any("Disallow" in record.getMessage() for record in caplog.records)


def test_the_star_group_wins_even_when_it_is_declared_last() -> None:
    """Le vrai piège d'Europe Remotely : treize groupes, et le premier
    (`Googlebot`) n'interdit que deux répertoires. Lire le fichier sans
    tenir compte des groupes conclurait l'inverse de ce que le site demande."""
    regles = parse_robots(fixture("europeremotely_robots.txt"))

    assert regles.blocks_everything
    assert regles.disallow == ("/",)


def test_a_real_robots_that_allows_offers_does_not_block_the_crawl() -> None:
    """Japan Dev n'interdit que deux PDF : les offres passent."""
    regles = parse_robots(fixture("japandev_robots.txt"))

    assert not regles.blocks_everything
    assert regles.allows(ALPACA_URL)
    assert not regles.allows("https://japan-dev.com/cdn/resources/a87bb10da/"
                            "japan_dev_salary_guide_2022.pdf")


def test_a_disallowed_path_is_never_requested_even_if_the_sitemap_lists_it() -> None:
    interdit = "https://japan-dev.com/jobs/a/backend-engineer-secret"
    sitemap = (
        '<?xml version="1.0"?><urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">'
        f"<url><loc>{interdit}</loc></url></urlset>"
    )
    serveur = Serveur(
        routes_japandev(
            **{
                ROBOTS_URL: (200, "User-agent: *\nDisallow: /jobs/a/\n"),
                SHARD_URL: (200, sitemap),
                interdit: (200, fixture("japandev_job.html")),
            }
        )
    )

    crawl(serveur)

    assert serveur.pages() == []


def test_a_missing_robots_txt_means_everything_is_allowed() -> None:
    """404 sur `robots.txt` : c'est l'absence de règle, donc la permission."""
    serveur = Serveur(routes_japandev(**{ROBOTS_URL: (404, "Not Found")}))

    jobs = crawl(serveur)

    assert len(jobs) == 2


def test_an_unreachable_robots_txt_fails_the_source_rather_than_crawling_it() -> None:
    """On ne crawle pas un site dont on n'a pas pu lire les règles. La source
    passe en échec, et le Collector continue avec les autres (US-2.4.1)."""
    from src.adapters.base import AdapterError

    serveur = Serveur(routes_japandev(**{ROBOTS_URL: (503, "indisponible")}))

    with pytest.raises(AdapterError) as excinfo:
        crawl(serveur)

    assert "Japan Dev" in str(excinfo.value)
    assert serveur.pages() == []


def test_the_crawl_delay_is_applied_between_two_pages_and_not_before_the_first(
    backoff_delays,
) -> None:
    """Politesse : deux pages chargées = une attente, pas deux."""
    serveur = Serveur(routes_japandev())

    crawl(serveur)

    assert len(serveur.pages()) == 2
    assert backoff_delays == [DEFAULT_CRAWL_DELAY]


def test_a_published_crawl_delay_overrides_our_default(backoff_delays) -> None:
    serveur = Serveur(
        routes_japandev(**{ROBOTS_URL: (200, "User-agent: *\nCrawl-delay: 5\n")})
    )

    crawl(serveur)

    assert backoff_delays == [5.0]


@pytest.mark.parametrize(
    "robots, chemin, autorise",
    [
        ("User-agent: *\nDisallow: /", "/jobs/a/b", False),
        ("User-agent: *\nDisallow:", "/jobs/a/b", True),
        ("", "/jobs/a/b", True),
        # `Allow` plus précis que `Disallow` : le plus long gagne.
        ("User-agent: *\nDisallow: /\nAllow: /jobs/", "/jobs/a/b", True),
        # Le `$` ancre la fin de l'URL.
        ("User-agent: *\nDisallow: /*.pdf$", "/doc/guide.pdf", False),
        ("User-agent: *\nDisallow: /*.pdf$", "/doc/guide.pdf.html", True),
        # Un groupe qui ne nous nomme pas ne nous concerne pas.
        ("User-agent: Googlebot\nDisallow: /", "/jobs/a/b", True),
        # Un groupe qui nous nomme prime sur `*`.
        (
            "User-agent: *\nDisallow:\n\nUser-agent: JobRadar\nDisallow: /",
            "/jobs/a/b",
            False,
        ),
        # Les commentaires ne sont pas des règles.
        ("User-agent: *\n# Disallow: /\n", "/jobs/a/b", True),
    ],
)
def test_robots_rules_are_applied_the_way_the_standard_says(
    robots, chemin, autorise
) -> None:
    assert parse_robots(robots).allows(chemin) is autorise


def test_a_sitemap_declared_in_robots_is_read() -> None:
    """Japan Dev publie l'adresse de son sitemap dans `robots.txt` — c'est le
    repli quand un adaptateur n'en déclare pas."""
    regles = parse_robots(fixture("japandev_robots.txt"))

    assert regles.sitemaps == ("https://japan-dev.com/sitemap.xml",)


def test_no_robots_at_all_still_carries_the_default_delay() -> None:
    assert NO_ROBOTS.delay == DEFAULT_CRAWL_DELAY
    assert RobotsRules(crawl_delay=0.0).delay == 0.0


# ---------------------------------------------------------------------------
# Le magasin d'état
# ---------------------------------------------------------------------------


def test_a_missing_state_file_reads_as_an_empty_state(tmp_path) -> None:
    etat = CrawlState.load(tmp_path / "jamais-ecrit.json")

    assert etat.cursor("japandev") == ""
    assert etat.known_urls("japandev") == set()


def test_a_corrupt_state_file_reads_as_empty_and_is_logged(tmp_path, caplog) -> None:
    """Un état illisible coûte un recrawl, jamais un run en échec."""
    chemin = tmp_path / "crawl.json"
    chemin.write_text("{ceci n'est pas du JSON", encoding="utf-8")

    with caplog.at_level(logging.WARNING):
        etat = CrawlState.load(chemin)

    assert etat.cursor("japandev") == ""
    assert any("illisible" in record.getMessage() for record in caplog.records)


def test_the_cursor_never_moves_backwards(tmp_path) -> None:
    """Un sitemap qui republierait une date ancienne ne doit pas déclencher
    le rechargement de tout un catalogue."""
    etat = CrawlState(tmp_path / "crawl.json")
    etat.record("japandev", cursor="2026-03-01T00:00:00+00:00", urls=set())

    etat.record("japandev", cursor="2026-01-01T00:00:00+00:00", urls=set())

    assert etat.cursor("japandev") == "2026-03-01T00:00:00+00:00"


def test_the_state_file_survives_a_reload(tmp_path) -> None:
    chemin = tmp_path / "crawl.json"
    etat = CrawlState(chemin)
    etat.record("japandev", cursor="2026-03-01T00:00:00+00:00", urls={ALPACA_URL})
    etat.save()

    relu = CrawlState.load(chemin)

    assert relu.cursor("japandev") == "2026-03-01T00:00:00+00:00"
    assert relu.known_urls("japandev") == {ALPACA_URL}


def test_the_known_urls_are_capped_so_a_versioned_file_cannot_grow_forever(
    tmp_path,
) -> None:
    etat = CrawlState(tmp_path / "crawl.json")

    etat.record(
        "japandev",
        cursor="",
        urls={f"https://x.tld/jobs/a/{n}" for n in range(MAX_KNOWN_URLS + 100)},
    )

    assert len(etat.known_urls("japandev")) == MAX_KNOWN_URLS


def test_the_state_is_written_sorted_so_a_git_diff_stays_readable(tmp_path) -> None:
    """Le fichier est versionné : un diff qui remue 290 lignes à chaque run
    rendrait l'historique illisible."""
    chemin = tmp_path / "crawl.json"
    etat = CrawlState(chemin)
    etat.record("japandev", cursor="", urls={"https://x.tld/b", "https://x.tld/a"})
    etat.save()

    assert json.loads(chemin.read_text(encoding="utf-8"))["japandev"]["urls"] == [
        "https://x.tld/a",
        "https://x.tld/b",
    ]


def test_the_cursor_is_not_saved_when_the_run_fails(tmp_path) -> None:
    """Sinon un sitemap illisible ferait sauter en silence tout ce qu'il
    contenait : le run suivant reprendrait après, sans l'avoir lu."""
    from src.adapters.base import AdapterError

    chemin = tmp_path / "crawl.json"
    serveur = Serveur(routes_japandev(**{SHARD_URL: (200, "<urlset><url>")}))

    with pytest.raises(AdapterError):
        crawl(serveur, state=CrawlState.load(chemin))

    assert not chemin.exists()


# ---------------------------------------------------------------------------
# Invariant : le crawl produit le même schéma que les ATS
# ---------------------------------------------------------------------------


def test_the_crawler_produces_the_same_rawjob_keys_as_an_ats_adapter() -> None:
    """Le mécanisme d'accès change, le schéma pivot ne bouge pas."""
    crawle = crawl(Serveur(routes_japandev()))[0]
    ats = GreenhouseAdapter(
        client=httpx.Client(
            transport=httpx.MockTransport(
                lambda request: httpx.Response(
                    200, content=fixture("greenhouse_gitlab.json")
                )
            )
        )
    ).fetch(Source(nom="GitLab", ats="greenhouse", token="gitlab"))[0]

    assert set(asdict(crawle)) == set(asdict(ats)) == RAWJOB_KEYS


def test_a_new_crawled_source_needs_three_declarations_and_nothing_else() -> None:
    """La promesse « un seul adaptateur couvre N sites » : brancher une
    source de plus ne demande ni parseur, ni méthode, ni test."""

    class AutreSite(JobPostingAdapter):
        ats = "autresite"
        site_url = "https://autre.tld/"
        sitemap_url = "https://autre.tld/sitemap.xml"

    assert AutreSite.requires_token is False
    assert AutreSite(client=httpx.Client()).ats == "autresite"


def test_the_adapter_declares_no_site_specific_html_parsing() -> None:
    """Garde-fou de posture : le jour où quelqu'un ajoute un sélecteur CSS ou
    un `find(\"div\")` dans ce module, il écrit un scraper par site — ce que
    `docs/sources-audit.md` refuse explicitement."""
    source = Path("src/adapters/jobposting.py").read_text(encoding="utf-8")

    for interdit in ("BeautifulSoup", "select_one", "find_all", "querySelector"):
        assert interdit not in source


# ---------------------------------------------------------------------------
# La chaîne de bout en bout : l'état doit survivre au runner
# ---------------------------------------------------------------------------

import yaml  # noqa: E402 — regroupé ici, ne sert qu'à cette section

WORKFLOW_LENT = Path(".github/workflows/collect-slow.yml")


def _workflow() -> dict:
    return yaml.safe_load(WORKFLOW_LENT.read_text(encoding="utf-8"))


def test_the_slow_workflow_commits_the_crawl_state_back() -> None:
    """Sans ce commit, l'incrémental ne sert à rien en production : le runner
    est éphémère, et chaque run repartirait du début."""
    etapes = _workflow()["jobs"]["collect-slow"]["steps"]
    commandes = "\n".join(etape.get("run", "") for etape in etapes)

    assert "git add state/" in commandes
    assert "git push" in commandes


def test_the_workflow_that_writes_the_state_is_serialised() -> None:
    """Deux runs qui écriraient `state/crawl.json` en même temps se
    marcheraient dessus, et l'un des deux perdrait son avancement."""
    doc = _workflow()

    assert doc["concurrency"]["group"]
    assert doc["concurrency"]["cancel-in-progress"] is False


def test_the_workflow_may_write_to_the_repository() -> None:
    """Le `GITHUB_TOKEN` par défaut est en lecture seule sur bien des dépôts :
    sans cette permission, le commit d'état échouerait à chaque run."""
    assert _workflow()["jobs"]["collect-slow"]["permissions"]["contents"] == "write"


def test_the_state_directory_is_versioned_and_documented() -> None:
    """Un dossier d'état non versionné serait recréé vide à chaque run."""
    assert (Path("state") / "README.md").is_file()


def test_the_suite_never_writes_the_repository_state(isolated_crawl_state) -> None:
    """Régression. Un adaptateur construit sans état lit `state/crawl.json`,
    celui du dépôt — et un test l'y a écrit avant que ce garde-fou existe.
    Le vrai dégât n'est pas le fichier : c'est qu'un test lisant le curseur
    d'un run réel ne chargerait plus rien et passerait pour de mauvaises
    raisons. La fixture `isolated_crawl_state` de `conftest.py` redirige le
    chemin par défaut ; ce test vérifie qu'elle mord.
    """
    serveur = Serveur(routes_japandev())

    # Sans `state=` : c'est exactement le cas qui fuyait.
    JapanDevAdapter(
        client=serveur.client,
        slug_filter=SlugFilter.from_filters("config/filters.yaml"),
    ).fetch(JAPANDEV)

    assert isolated_crawl_state.is_file()
    assert not (Path("state") / "crawl.json").exists()
