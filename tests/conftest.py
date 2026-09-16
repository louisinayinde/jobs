"""Réglages partagés par toute la suite de tests.

Trois, et chacun protège la suite d'une nuisance qu'un test isolé ne peut
pas voir :

1. **L'attente du retry réseau est neutralisée** (US-2.4.2). Sans ça,
   chaque test qui simule une panne dormirait deux secondes pour de vrai —
   la suite passerait de trois secondes à plusieurs minutes, et personne ne
   la lancerait plus. C'est la même fonction qui porte le délai de crawl
   des sources sitemap (Feature 2.5), donc elle est couverte aussi.
   Le client GitHub (Feature 4.1) a sa propre attente — celle des limites
   de débit, qui peut durer un quart d'heure : elle l'est tout autant.
2. **L'état du crawl est redirigé vers un dossier temporaire**
   (Feature 2.5). Un adaptateur construit sans état explicite lit
   `state/crawl.json`, celui du dépôt : la suite écrirait alors un fichier
   de production, et pire, un test lisant le curseur qu'un vrai run y a
   laissé ne chargerait plus rien et passerait pour de mauvaises raisons.
   `seen.json` (Feature 3.3) et le registre des fiches (Feature 4.2) le
   sont de la même façon.
3. **Le run de collecte ne peut pas joindre GitHub** (Feature 4.3). Il
   publie désormais : un test qui le lance sur des offres retenues sans
   lui fournir de faux GitHub enverrait des requêtes au vrai tableau. Le
   client qu'il ouvre lève sur toute requête ; la pause entre deux fiches
   est neutralisée comme les autres attentes.
"""

from __future__ import annotations

import httpx
import pytest

from src import collect
from src.adapters import base
from src.board import github, publication, registre
from src.core import dedup, state


@pytest.fixture(autouse=True)
def backoff_delays(monkeypatch: pytest.MonkeyPatch) -> list[float]:
    """Remplace l'attente entre deux tentatives par un simple enregistrement."""
    delays: list[float] = []
    monkeypatch.setattr(base, "_wait", delays.append)
    return delays


@pytest.fixture(autouse=True)
def github_delays(monkeypatch: pytest.MonkeyPatch) -> list[float]:
    """Remplace les attentes du client GitHub par un simple enregistrement."""
    delays: list[float] = []
    monkeypatch.setattr(github, "_wait", delays.append)
    return delays


@pytest.fixture(autouse=True)
def publication_pauses(monkeypatch: pytest.MonkeyPatch) -> list[float]:
    """Remplace la pause entre deux fiches par un simple enregistrement."""
    pauses: list[float] = []
    monkeypatch.setattr(publication, "_pause", pauses.append)
    return pauses


def _github_interdit(request: httpx.Request) -> httpx.Response:
    raise AssertionError(
        f"le run a voulu joindre GitHub ({request.method} {request.url}) — "
        "passer `github=` ou `--sans-publication`"
    )


@pytest.fixture(autouse=True)
def github_injoignable(monkeypatch: pytest.MonkeyPatch) -> None:
    """Le client GitHub qu'ouvre le run lève sur toute requête."""

    def ouvrir(config):
        http = httpx.Client(transport=httpx.MockTransport(_github_interdit))
        return github.GitHubClient("jeton-de-test", config, client=http)

    monkeypatch.setattr(collect, "ouvrir_github", ouvrir)


@pytest.fixture(autouse=True)
def isolated_crawl_state(tmp_path, monkeypatch: pytest.MonkeyPatch):
    """Redirige l'état de crawl par défaut vers le dossier du test."""
    chemin = tmp_path / "state" / "crawl.json"
    monkeypatch.setattr(state, "DEFAULT_STATE_PATH", chemin)
    return chemin


@pytest.fixture(autouse=True)
def isolated_seen_state(tmp_path, monkeypatch: pytest.MonkeyPatch):
    """Redirige `seen.json` par défaut vers le dossier du test."""
    chemin = tmp_path / "state" / "seen.json"
    monkeypatch.setattr(dedup, "DEFAULT_SEEN_PATH", chemin)
    return chemin


@pytest.fixture(autouse=True)
def isolated_fiches_state(tmp_path, monkeypatch: pytest.MonkeyPatch):
    """Redirige le registre des fiches par défaut vers le dossier du test."""
    chemin = tmp_path / "state" / "fiches.json"
    monkeypatch.setattr(registre, "DEFAULT_FICHES_PATH", chemin)
    return chemin
