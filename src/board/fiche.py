"""Titre et corps de l'Issue d'une offre (US-4.1.2, gabarit final US-4.2.1).

Une offre classée (`ScoredJob`) devient une Issue titrée « Entreprise —
Poste », dont le corps est la fiche : ce que le candidat lit sur le tableau
pour décider, et le lien pour aller plus loin. Les champs viennent dans
l'ordre où on les lit pour trier une offre :

    **Entreprise** : GitLab
    **Poste** : Backend Engineer
    **Localisation** : London, UK · hybride
    **Score** : 60 — europe (london)
    **Tech** : go, ruby
    **Lien ATS** : [job-boards.greenhouse.io](<https://…/jobs/4012>)
    **Publiée le** : 2026-09-14
    **Source** : greenhouse

**Un champ vide est omis**, ligne comprise : jamais de « None », jamais de
« Tech : » suivi de rien. Seuls le score et le lien sont toujours là — une
offre publiée a toujours été notée, et `normalize` garantit son URL.

**Les valeurs sont du texte, pas du Markdown.** Elles viennent de sources
tierces : un intitulé « C# / .NET *Senior* » ne doit pas passer en
italique, un `<!--` dans une localisation ne doit pas masquer le reste de la
fiche, et surtout **aucune valeur ne doit agir sur GitHub** — un
`@octocat` notifierait un inconnu, un `autre/depot#12` inscrirait une
référence croisée dans l'Issue d'un autre dépôt. La ponctuation Markdown est
échappée, et un gluon invisible (U+2060) est glissé entre `@` ou `#` et ce
qui les suit : le texte reste le même à l'œil, GitHub n'y voit plus ni
mention ni référence.

**Pas de description dans la fiche.** Elle ferait parfois plusieurs pages,
et le lien ATS la montre mieux. Le CV Generator (Epic 6) la récupérera à la
source.

**L'identifiant stable est écrit dans la fiche**, dans un commentaire HTML
invisible au rendu : `<!-- jobradar:id=greenhouse:gitlab:4012 -->`.
`lire_id` le relit : c'est ce qui permet de reconstruire le registre des
fiches depuis GitHub (US-4.2.2) et au Decision Watcher (US-5.2.1) de
retrouver l'offre depuis l'Issue marquée.
"""

from __future__ import annotations

import re
from datetime import datetime
from urllib.parse import quote, unquote, urlsplit

from src.core.dedup import stable_id
from src.core.scoring import ScoredJob

#: Préfixe du commentaire qui porte l'identifiant stable dans le corps.
MARQUEUR_ID = "jobradar:id="

#: Titre d'une offre dont la source n'a donné aucun intitulé.
POSTE_INCONNU = "(poste sans titre)"

#: Le mode de travail, dit en français ; `unknown` n'est pas affiché.
MODES_DE_TRAVAIL = {"remote": "télétravail", "hybrid": "hybride", "onsite": "sur site"}

#: Gluon de mots (U+2060) : invisible, insécable, et il coupe `@nom` et `#12`.
GLUON = "\u2060"

#: Ponctuation qui a un sens au milieu d'une ligne en Markdown GitHub,
#: échappée par `\`. Pas `#`, `+` ou `-` : ils n'agissent qu'en début de
#: ligne, et une valeur n'y est jamais. Pas `(` : sans `[` il ne fait pas
#: de lien, et `[` est échappé.
_MARKDOWN_RE = re.compile(r"([\\`*_\[\]<>~&|])")

#: `@` ou `#` collé à ce qui ferait une mention ou une référence.
_ACTIF_RE = re.compile(r"([@#])(?=\w)")

_MARQUEUR_RE = re.compile(r"<!-- " + re.escape(MARQUEUR_ID) + r"(\S+) -->")


def titre_issue(offre: ScoredJob) -> str:
    """« Entreprise — Poste », ou le poste seul quand l'entreprise manque.

    Le titre n'est pas échappé : GitHub l'affiche en texte brut, et une
    mention dans un titre ne notifie personne.
    """
    job = offre.job
    poste = job.titre.strip() or POSTE_INCONNU
    entreprise = job.entreprise.strip()
    return f"{entreprise} — {poste}" if entreprise else poste


def corps_issue(offre: ScoredJob) -> str:
    """La fiche en Markdown. Un champ vide est omis, jamais écrit « None »."""
    job = offre.job
    localisation = " · ".join(
        v for v in (texte(job.localisation), MODES_DE_TRAVAIL.get(job.remote_type, "")) if v
    )
    motif = f"{offre.categorie} ({offre.detail})" if offre.detail else offre.categorie

    lignes = [
        ("Entreprise", texte(job.entreprise)),
        ("Poste", texte(job.titre)),
        ("Localisation", localisation),
        ("Score", texte(f"{offre.score} — {motif}" if motif else str(offre.score))),
        ("Tech", texte(", ".join(t for t in job.tech if t.strip()))),
        ("Lien ATS", lien(job.url)),
        ("Publiée le", _jour(job.date)),
        ("Source", texte(job.source)),
    ]
    corps = [f"**{nom}** : {valeur}  " for nom, valeur in lignes if valeur]
    corps.append("")
    corps.append(f"<!-- {MARQUEUR_ID}{_commentaire_sur(stable_id(job))} -->")
    return "\n".join(corps) + "\n"


def lire_id(corps: str | None) -> str | None:
    """L'identifiant stable écrit dans une fiche, ou `None` s'il n'y en a pas.

    L'inverse exact de ce qu'écrit `corps_issue`. Un corps sans marqueur
    (Issue ouverte à la main) rend `None` ; deux marqueurs, le premier.
    """
    trouve = _MARQUEUR_RE.search(corps or "")
    return unquote(trouve.group(1)) if trouve else None


def texte(valeur: str) -> str:
    """Une valeur tierce rendue inerte : sur une ligne, sans Markdown ni mention."""
    une_ligne = " ".join(valeur.split())
    return _ACTIF_RE.sub(rf"\1{GLUON}", _MARKDOWN_RE.sub(r"\\\1", une_ligne))


def lien(url: str) -> str:
    """Lien cliquable vers l'offre — seulement pour une URL http(s) bien formée.

    Le texte du lien est l'hôte (`job-boards.greenhouse.io`) : on sait où
    l'on va avant de cliquer. La forme `<url>` tolère les parenthèses, qui
    couperaient un lien Markdown ordinaire ; les espaces sont encodés. Une
    URL d'un autre schéma (`javascript:`), sans hôte, ou portant `<`, `>`
    ou un caractère de contrôle est affichée en texte inerte, jamais rendue
    cliquable.
    """
    url = url.strip()
    try:
        morceaux = urlsplit(url)
        hote = morceaux.hostname
    except ValueError:
        return texte(url)
    propre = (
        morceaux.scheme in ("http", "https")
        and hote
        and not any(c in url for c in "<>\\")
        and all(c.isprintable() for c in url)
    )
    if not propre:
        return texte(url)
    return f"[{texte(hote)}](<{url.replace(' ', '%20')}>)"


def _jour(date: str) -> str:
    """`2026-09-14T08:00:00+00:00` → `2026-09-14` ; `""` si illisible."""
    try:
        return datetime.fromisoformat(date).date().isoformat()
    except ValueError:
        return ""


def _commentaire_sur(identifiant: str) -> str:
    """Encode l'identifiant pour qu'il ne ferme jamais le commentaire HTML.

    Un identifiant natif peut contenir `--`, `>` ou une espace. Percent-encodé,
    il se relit sans perte avec `urllib.parse.unquote` ; le tiret simple et
    `:` restent lisibles (`greenhouse:societe-generale:4012`), seul un tiret
    doublé est encodé.
    """
    return quote(identifiant, safe=":-").replace("--", "-%2D")
