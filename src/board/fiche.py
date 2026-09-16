"""Titre et corps de l'Issue d'une offre (US-4.1.2).

Une offre classée (`ScoredJob`) devient une Issue titrée « Entreprise —
Poste », dont le corps est la fiche : ce que le candidat lit sur le tableau
pour décider, et le lien pour aller plus loin.

**Pas de description dans la fiche.** Elle ferait parfois plusieurs pages,
contient des `@mentions` et du balisage qui déclencheraient des
notifications ou casseraient le rendu, et le lien ATS la montre mieux. Le CV
Generator (Epic 6) la récupérera à la source.

**L'identifiant stable est écrit dans la fiche**, dans un commentaire HTML
invisible au rendu : `<!-- jobradar:id=greenhouse:gitlab:4012 -->`. C'est ce
que le Decision Watcher (US-5.2.1) relira pour retrouver l'offre depuis
l'Issue marquée.

Le gabarit définitif — champs, ordre, cas des champs vides — est l'objet de
la Feature 4.2 ; celui-ci en pose la forme.
"""

from __future__ import annotations

from datetime import datetime
from urllib.parse import quote, urlsplit

from src.core.dedup import stable_id
from src.core.scoring import ScoredJob

#: Préfixe du commentaire qui porte l'identifiant stable dans le corps.
MARQUEUR_ID = "jobradar:id="

#: Titre d'une offre dont la source n'a donné aucun intitulé.
POSTE_INCONNU = "(poste sans titre)"


def titre_issue(offre: ScoredJob) -> str:
    """« Entreprise — Poste », ou le poste seul quand l'entreprise manque."""
    job = offre.job
    poste = job.titre.strip() or POSTE_INCONNU
    entreprise = job.entreprise.strip()
    return f"{entreprise} — {poste}" if entreprise else poste


def corps_issue(offre: ScoredJob) -> str:
    """La fiche en Markdown. Un champ vide est omis, jamais écrit « None »."""
    job = offre.job
    localisation = ", ".join(v for v in (job.localisation.strip(), job.remote_type) if v)
    motif = f"{offre.categorie} ({offre.detail})" if offre.detail else offre.categorie

    lignes = [
        ("Entreprise", job.entreprise.strip()),
        ("Poste", job.titre.strip()),
        ("Localisation", localisation),
        ("Score", f"{offre.score} — {motif}"),
        ("Tech", ", ".join(job.tech)),
        ("Publiée le", _jour(job.date)),
        ("Source", job.source),
    ]
    corps = [f"**{nom}** : {valeur}  " for nom, valeur in lignes if valeur]
    corps.append("")
    corps.append(_lien(job.url))
    corps.append("")
    corps.append(f"<!-- {MARQUEUR_ID}{_commentaire_sur(stable_id(job))} -->")
    return "\n".join(corps) + "\n"


def _jour(date: str) -> str:
    """`2026-09-14T08:00:00+00:00` → `2026-09-14` ; `""` si illisible."""
    try:
        return datetime.fromisoformat(date).date().isoformat()
    except ValueError:
        return ""


def _lien(url: str) -> str:
    """Lien cliquable vers l'offre — seulement pour une URL http(s).

    La forme `<url>` tolère parenthèses et espaces dans l'URL, qui
    couperaient un lien Markdown ordinaire. Une URL d'un autre schéma
    (`javascript:`) est affichée en texte, jamais rendue cliquable.
    """
    if urlsplit(url).scheme in ("http", "https") and not any(c in url for c in "<>\n"):
        return f"[Voir l'offre](<{url}>)"
    return f"Lien : `{url}`"


def _commentaire_sur(texte: str) -> str:
    """Encode l'identifiant pour qu'il ne ferme jamais le commentaire HTML.

    Un identifiant natif peut contenir `--` ou `>`. Percent-encodé, il se
    relit sans perte avec `urllib.parse.unquote` ; le tiret simple et `:`
    restent lisibles (`greenhouse:societe-generale:4012`), seul un tiret
    doublé est encodé.
    """
    return quote(texte, safe=":-").replace("--", "-%2D")
