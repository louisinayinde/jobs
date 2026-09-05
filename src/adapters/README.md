# Adaptateurs

Un **adaptateur** traduit la réponse d'une plateforme d'offres en une liste de
`RawJob`, le schéma pivot de l'ingestion. Tout ce qui suit dans le pipeline
(normalisation, filtre de rétention, dédup, scoring, publication) ne connaît que
`RawJob` : **ajouter une plateforme ne demande de toucher à rien d'autre**.

## Le contrat

```python
class Adapter(ABC):
    ats: ClassVar[str]                                  # la valeur écrite dans sources.yaml
    def fetch(self, source: Source) -> list[RawJob]: ...
```

| Situation | Comportement attendu |
|---|---|
| Board introuvable (**404**) | liste vide + avertissement journalisé, **aucune exception** (le token a changé) |
| JSON malformé, schéma inattendu, statut ≥ 400 | `AdapterError` **nommant la source** |
| Panne réseau / timeout | `AdapterError` **nommant la source** |
| Une offre du board est incomplète | offre ignorée + journalisée, **les autres sont retournées** |
| Un champ est absent | valeur par défaut sûre (`""`, `remote_type="unknown"`), **jamais de `KeyError`** |

L'isolation par source (une source en échec n'arrête pas le run) est la
responsabilité du Collector, pas de l'adaptateur — Feature 2.4.1.

## Le schéma `RawJob`

Tous les champs sont des `str` ; un champ absent vaut `""`, jamais `None`.

| Champ | Contenu |
|---|---|
| `id` | identifiant natif de l'offre côté ATS |
| `ats` | identifiant de l'adaptateur (`greenhouse`, `lever`, …) |
| `entreprise` | `Source.nom`, tel qu'écrit dans `sources.yaml` |
| `titre` | intitulé du poste |
| `localisation` | localisation(s) telles qu'affichées, jointes par `; ` |
| `remote_type` | `remote` \| `hybrid` \| `onsite` \| `unknown` |
| `url` | lien public vers l'offre |
| `date` | date de publication, ISO-8601 UTC à la seconde (`""` si inconnue) |
| `description` | texte brut, sans balises HTML |

## Plateformes branchées

| `ats` | Endpoint | Description dans la liste ? |
|---|---|---|
| `greenhouse` | `GET boards-api.greenhouse.io/v1/boards/{token}/jobs?content=true` | ✅ |
| `lever` | `GET api.lever.co/v0/postings/{token}?mode=json` | ✅ |
| `ashby` | `GET api.ashbyhq.com/posting-api/job-board/{token}` | ✅ |
| `smartrecruiters` | `GET api.smartrecruiters.com/v1/companies/{token}/postings` (paginé) | ❌ |
| `workable` | `POST apply.workable.com/api/v3/accounts/{token}/jobs` | ❌ |

SmartRecruiters et Workable n'exposent pas la description dans la réponse de
liste : la récupérer coûterait une requête **par offre**. Leur
`RawJob.description` vaut donc `""` — le texte intégral est récupéré à la
demande par le JD Fetcher (Feature 6.2), uniquement pour l'offre retenue.
Conséquence : le filtre tech optionnel (US-3.2.4) ne peut rien conclure sur ces
deux sources.

Le `token` de `sources.yaml` est le slug lu dans l'URL du board public. Il est
**sensible à la casse** chez SmartRecruiters.

## Ajouter une plateforme

1. Créer `src/adapters/<ats>.py` avec une classe qui hérite de `HttpAdapter`,
   déclare `ats = "<ats>"` et implémente `fetch`.
2. Utiliser `self._request_json(source, url)` : il pose le User-Agent et le
   timeout, retourne `None` sur 404 et lève `AdapterError` sur le reste.
3. Mapper les champs avec les helpers de `base.py` :
   `clean_str`, `join_locations`, `html_to_text`, `to_iso_utc`,
   `normalize_remote_type`, `infer_remote_type`.
4. Ajouter la classe au tuple `ADAPTERS` dans `__init__.py`. C'est tout :
   `KNOWN_ATS` (registre des sources) en découle, donc le nouvel `ats` devient
   automatiquement acceptable dans `sources.yaml`.
5. Figer une **réponse réelle** dans `tests/fixtures/<ats>_<board>.json` et
   l'ajouter à la liste `ALL_ATS` de `tests/test_adapters.py` : les tests
   transverses (schéma commun, 404, JSON malformé, erreur serveur) s'appliquent
   alors d'office au nouvel adaptateur.

## Tests

Les fixtures sont des réponses **réellement capturées** le 2026-09-05 sur les
boards publics GitLab (Greenhouse), Malt (Lever), Ramp (Ashby), Ubisoft
(SmartRecruiters) et BG Prevent (Workable), rejouées via `httpx.MockTransport`.
Aucun test n'accède au réseau.

```bash
pytest tests/test_adapters.py -q
```
