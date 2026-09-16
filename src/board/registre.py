"""Registre des fiches : identifiant d'offre → Issue publiée (US-4.2.2).

`seen.json` (Feature 3.3) dit quelles offres ont été **vues** ; ce registre
dit lesquelles ont été **publiées**, et sous quel numéro d'Issue. C'est lui
qui garantit qu'une offre n'a jamais deux fiches : une offre qu'il connaît
ne déclenche aucune création, quel que soit l'état de `seen.json` — vidé à
la main, perdu, ou rejoué après un run qui a publié sans mémoriser.

Une entrée par offre, dans `state/fiches.json`, fichier versionné que les
workflows de collecte commitent avec le reste de `state/` :

```json
{
  "greenhouse:gitlab:4012": {"issue": 42, "node_id": "I_kwDO…", "item": "PVTI_…"},
  "lever:malt:9f1c":        {"issue": 43, "node_id": "I_kwDO…"},
  "ashby:ramp:77":          {"en_cours": "2026-09-16T08:15:00Z"}
}
```

- `issue` + `node_id` : l'Issue existe. `item` : sa carte est posée sur le
  tableau ; absent, la pose reste à faire ;
- `hors_tableau` : l'Issue existe mais n'était sur aucune carte lors d'une
  reconstruction — retirée à la main ou par la purge. Elle n'est pas
  reposée : on ne ressuscite pas une fiche qu'on a écartée ;
- `en_cours` : une création a été **envoyée** à cet instant sans réponse
  nette. L'Issue existe peut-être ; le run suivant la cherche sur GitHub
  avant d'en créer une autre.

**Un registre illisible arrête la publication**, et c'est la différence
avec `seen.json`. Un `seen.json` perdu coûte des offres qui repassent une
fois ; un registre perdu coûterait **une seconde fiche pour chaque offre
publiée**, des centaines de doublons sur le tableau. Le message renvoie à
`python -m src.board resync`, qui reconstruit le registre depuis les
identifiants écrits dans les fiches elles-mêmes. Un fichier **absent**, lui,
est un premier run : registre vide.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator

from src.core.state import write_json_atomic

#: Emplacement par défaut, relatif à la racine du dépôt.
DEFAULT_FICHES_PATH = Path("state/fiches.json")

#: La commande qui reconstruit le registre, citée dans les erreurs.
COMMANDE_RESYNC = "python -m src.board resync"

_FORMAT_DATE = "%Y-%m-%dT%H:%M:%SZ"


class RegistreIllisible(Exception):
    """`fiches.json` existe mais ne se lit pas : publier créerait des doublons."""


@dataclass(frozen=True)
class Fiche:
    """Ce que le registre sait de la fiche d'une offre."""

    #: Numéro de l'Issue, `None` tant que la création n'a pas de réponse nette.
    issue: int | None = None
    #: Identifiant GraphQL de l'Issue, celui qu'attend le Project.
    node_id: str = ""
    #: Identifiant de la carte sur le tableau, `""` tant qu'elle n'est pas posée.
    item: str = ""
    #: L'Issue a été retirée du tableau : ne pas la reposer.
    hors_tableau: bool = False
    #: Instant d'une création envoyée sans réponse nette, `None` sinon.
    en_cours: datetime | None = None

    @property
    def a_poser(self) -> bool:
        """L'Issue existe mais sa carte reste à poser sur le tableau."""
        return self.issue is not None and not self.item and not self.hors_tableau

    def en_json(self) -> dict[str, Any]:
        if self.issue is None:
            instant = self.en_cours or datetime.now(timezone.utc)
            return {"en_cours": instant.astimezone(timezone.utc).strftime(_FORMAT_DATE)}
        entree: dict[str, Any] = {"issue": self.issue, "node_id": self.node_id}
        if self.item:
            entree["item"] = self.item
        if self.hors_tableau:
            entree["hors_tableau"] = True
        return entree

    @classmethod
    def depuis_json(cls, brut: Any) -> Fiche:
        """Lit une entrée ; `ValueError` si sa forme n'est pas l'une des trois."""
        if not isinstance(brut, dict):
            raise ValueError(f"mapping attendu, reçu {type(brut).__name__}")
        if "en_cours" in brut:
            try:
                instant = datetime.strptime(brut["en_cours"], _FORMAT_DATE)
            except (TypeError, ValueError):
                raise ValueError(f"date « en_cours » illisible : {brut['en_cours']!r}") from None
            return cls(en_cours=instant.replace(tzinfo=timezone.utc))

        issue, node_id = brut.get("issue"), brut.get("node_id")
        item, hors = brut.get("item", ""), brut.get("hors_tableau", False)
        if isinstance(issue, bool) or not isinstance(issue, int) or issue <= 0:
            raise ValueError(f"numéro d'Issue invalide : {issue!r}")
        if not isinstance(node_id, str) or not node_id:
            raise ValueError(f"node_id invalide : {node_id!r}")
        if not isinstance(item, str) or not isinstance(hors, bool):
            raise ValueError("item ou hors_tableau invalide")
        return cls(issue=issue, node_id=node_id, item=item, hors_tableau=hors)


class RegistreFiches:
    """Identifiant stable d'offre → `Fiche`, lu et écrit dans `fiches.json`."""

    def __init__(self, path: Path, fiches: dict[str, Fiche] | None = None) -> None:
        self.path = Path(path)
        self._fiches: dict[str, Fiche] = dict(fiches or {})

    @staticmethod
    def chemin(path: str | Path | None = None) -> Path:
        """`path`, ou l'emplacement par défaut lu au moment de l'appel."""
        return Path(DEFAULT_FICHES_PATH if path is None else path)

    @classmethod
    def load(cls, path: str | Path | None = None) -> RegistreFiches:
        """Charge le registre ; vide si le fichier manque, **erreur** s'il est cassé.

        `path` est résolu à l'appel, comme pour les autres fichiers d'état :
        la suite de tests redirige `DEFAULT_FICHES_PATH`.
        """
        chemin = cls.chemin(path)
        if not chemin.is_file():
            return cls(chemin)

        def illisible(motif: str) -> RegistreIllisible:
            return RegistreIllisible(
                f"registre des fiches illisible ({chemin}) : {motif} — publication "
                f"refusée, elle recréerait des fiches existantes. Reconstruire le "
                f"registre depuis GitHub : {COMMANDE_RESYNC}"
            )

        try:
            brut = json.loads(chemin.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError, ValueError) as exc:
            raise illisible(str(exc)) from exc
        if not isinstance(brut, dict):
            raise illisible(f"mapping attendu, reçu {type(brut).__name__}")

        fiches: dict[str, Fiche] = {}
        for identifiant, entree in brut.items():
            try:
                fiches[identifiant] = Fiche.depuis_json(entree)
            except ValueError as exc:
                raise illisible(f"entrée « {identifiant} » : {exc}") from exc
        return cls(chemin, fiches)

    def __contains__(self, identifiant: object) -> bool:
        return identifiant in self._fiches

    def __len__(self) -> int:
        return len(self._fiches)

    def __iter__(self) -> Iterator[str]:
        return iter(self._fiches)

    def fiches(self) -> list[Fiche]:
        return list(self._fiches.values())

    def get(self, identifiant: str) -> Fiche | None:
        return self._fiches.get(identifiant)

    def enregistrer(self, identifiant: str, fiche: Fiche) -> None:
        """Pose ou remplace l'entrée, en mémoire ; `save()` reste à appeler."""
        self._fiches[identifiant] = fiche

    def oublier(self, identifiant: str) -> None:
        self._fiches.pop(identifiant, None)

    def poser(self, identifiant: str, item: str) -> Fiche:
        """Note la carte posée sur le tableau pour une fiche déjà enregistrée."""
        fiche = replace(self._fiches[identifiant], item=item)
        self._fiches[identifiant] = fiche
        return fiche

    def save(self) -> None:
        """Écrit `fiches.json`, trié et de façon atomique."""
        write_json_atomic(
            self.path, {cle: fiche.en_json() for cle, fiche in self._fiches.items()}
        )

    def as_dict(self) -> dict[str, dict[str, Any]]:
        """Le contenu tel qu'il serait écrit, pour l'inspection et les tests."""
        return {cle: fiche.en_json() for cle, fiche in sorted(self._fiches.items())}
