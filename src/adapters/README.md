# Adaptateurs

Un **adaptateur** traduit la réponse d'une plateforme d'offres en une liste de
`RawJob`, le schéma pivot de l'ingestion. Tout ce qui suit dans le pipeline
(normalisation, filtre de rétention, dédup, scoring, publication) ne connaît que
`RawJob` : **ajouter une plateforme ne demande de toucher à rien d'autre**.

Deux familles, un seul contrat :

| | ATS (`config/sources.yaml`) | Agrégateur (`config/aggregators.yaml`) |
|---|---|---|
| Périmètre d'un endpoint | le board d'**une** entreprise | des offres de **N** entreprises |
| `token` | **requis** — il désigne le board | **optionnel** — paramètre de recherche |
| `RawJob.entreprise` | `Source.nom` | lu **dans l'offre** |
| `Source.nom` | l'entreprise | la plateforme (journaux seulement) |
| User-Agent | `JobRadar/0.1` | en-têtes de navigateur |
| Offre sans employeur | ne se produit pas | **écartée** (règle anti-scam) |

C'est `Adapter.requires_token` qui porte cette différence : le Source Registry
s'en sert pour accepter une entrée d'agrégateur sans token au lieu de la
rejeter comme malformée.

## Le contrat

```python
class Adapter(ABC):
    ats: ClassVar[str]              # la valeur écrite dans le champ `ats` de la config
    requires_token: ClassVar[bool]  # True pour un ATS, False pour un agrégateur
    def fetch(self, source: Source) -> list[RawJob]: ...
```

| Situation | Comportement attendu |
|---|---|
| Board introuvable (**404**) | liste vide + avertissement journalisé, **aucune exception** (le token a changé) |
| JSON malformé, schéma inattendu, statut ≥ 400 | `AdapterError` **nommant la source** |
| Panne réseau / timeout | `AdapterError` **nommant la source** |
| Une offre du board est incomplète | offre ignorée + journalisée, **les autres sont retournées** |
| Un champ est absent | valeur par défaut sûre (`""`, `remote_type="unknown"`), **jamais de `KeyError`** |
| Offre d'agrégateur sans employeur | offre **écartée** + rejet journalisé (règle anti-scam) |

L'isolation par source (une source en échec n'arrête pas le run) est la
responsabilité du Collector, pas de l'adaptateur — Feature 2.4.1.

## Le schéma `RawJob`

Tous les champs sont des `str` ; un champ absent vaut `""`, jamais `None`.

| Champ | Contenu |
|---|---|
| `id` | identifiant natif de l'offre côté ATS |
| `ats` | identifiant de l'adaptateur (`greenhouse`, `lever`, …) |
| `entreprise` | l'employeur : `Source.nom` chez un ATS, un champ de l'offre chez un agrégateur |
| `titre` | intitulé du poste |
| `localisation` | localisation(s) telles qu'affichées, jointes par `; ` |
| `remote_type` | `remote` \| `hybrid` \| `onsite` \| `unknown` |
| `url` | lien public vers l'offre |
| `date` | date de publication, ISO-8601 UTC à la seconde (`""` si inconnue) |
| `description` | texte brut, sans balises HTML |

## Plateformes ATS branchées

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
deux sources. Même limite chez l'APEC et NoDesk, côté agrégateurs.

Le `token` de `sources.yaml` est le slug lu dans l'URL du board public. Il est
**sensible à la casse** chez SmartRecruiters et Ashby.

## Agrégateurs branchés

| `ats` | Endpoint | Format | À quoi sert le `token` |
|---|---|---|---|
| `remoteok` | `GET remoteok.com/api` | JSON | — |
| `remotive` | `GET remotive.com/api/remote-jobs?search=…` | JSON | terme de recherche |
| `weworkremotely` | `GET weworkremotely.com/categories/{token}.rss` | RSS | catégorie |
| `hackernews` | `GET hn.algolia.com/api/v1/…` (2 appels) | JSON | — |
| `himalayas` | `GET himalayas.app/jobs/api?limit=…` | JSON | taille de page |
| `workingnomads` | `GET workingnomads.com/api/exposed_jobs/` | JSON | — |
| `nodesk` | `GET nodesk.co/remote-jobs/index.xml` | RSS | — |
| `jobspresso` | `GET jobspresso.co/?feed=job_feed` | RSS | — |
| `euremotejobs` | `GET euremotejobs.com/?feed=job_feed` | RSS | — |
| `landingjobs` | `GET landing.jobs/api/v1/jobs` | JSON | — |
| `freework` | `GET free-work.com/api/job_postings?contracts=…&page=N` | JSON | type de contrat |
| `apec` | `POST apec.fr/cms/webservices/rechercheOffre` | JSON | mots-clés |

Où l'entreprise se trouve, selon les cas : dans un **champ dédié** (RemoteOK,
Remotive, Himalayas, Working Nomads, Free-Work, APEC, et les flux WP Job
Manager) ; dans le **titre** — en préfixe chez WWR (`Edfinity: Senior…`), en
suffixe chez NoDesk (`Senior… at GitLab`) ; dans l'**URL** chez Landing.jobs
(`/at/{slug}/`) ; dans la **première ligne du commentaire** chez Hacker News.
Quand elle est introuvable, l'offre est écartée et le rejet journalisé.

Trois pièges qui ont coûté cher, et qui sont désormais couverts par des tests :

- **RemoteOK répond en ~40 s.** Son adaptateur porte un `default_timeout` de
  60 s ; le défaut de 15 s l'excluait de fait. Sa première entrée de tableau
  n'est pas une offre mais les mentions légales de l'API. Ses textes
  non-anglais arrivent en mojibake, réparé à la lecture.
- **Remotive limite à 4 appels par jour** (conditions d'utilisation). Elle
  déclare donc `max_calls_per_day = 4`, ce qui la range dans la **cadence
  réduite** — voir plus bas.
- **L'APEC refuse tout champ inconnu** dans le corps du POST, avec un 500 qui
  nomme le champ fautif. Le corps de `apec.py` ne contient que des champs
  vérifiés en direct.

## Cadences de collecte

Certaines plateformes plafonnent leurs appels quotidiens. La boucle normale
en ferait 96 par jour et par source ; s'y tenir demanderait un compteur, et
un compteur ne survivrait pas au runner Actions, qui est éphémère. C'est donc
le **planificateur** qui garantit le compte :

| Cadence | Workflow | Cron | Appels/jour | Sources |
|---|---|---|---|---|
| `fast` | `collect.yml` | `*/15 * * * *` | 96 | toutes celles sans plafond |
| `slow` | `collect-slow.yml` | `7 */6 * * *` | 4 | celles qui en déclarent un |

L'affectation se **déduit** de `Adapter.max_calls_per_day` — jamais d'une clé
de configuration, qu'une édition pourrait faire sauter sans qu'on s'en
aperçoive. Une source appartient donc toujours à exactement une cadence, et
`python -m src.collect --cadence {fast,slow}` sélectionne la bonne liste.

Pour brancher une plateforme qui impose une limite : déclarer
`max_calls_per_day = N` sur son adaptateur, et rien d'autre. Si `N` tombe
sous les 4 appels du cron actuel, le test
`test_the_slow_cron_stays_within_every_declared_platform_limit` échoue et
rappelle qu'il faut espacer `collect-slow.yml` — plutôt que de le découvrir
quand la plateforme coupe l'accès.

## Ajouter un ATS

1. Créer `src/adapters/<ats>.py` avec une classe qui hérite de `HttpAdapter`,
   déclare `ats = "<ats>"` et implémente `fetch`.
2. Utiliser `self._request_json(source, url)` : il pose les en-têtes et le
   timeout, retourne `None` sur 404 et lève `AdapterError` sur le reste.
3. Mapper les champs avec les helpers de `base.py` :
   `clean_str`, `join_locations`, `html_to_text`, `to_iso_utc`,
   `normalize_remote_type`, `infer_remote_type`.
4. Ajouter la classe au tuple `ATS_ADAPTERS` dans `__init__.py`. C'est tout :
   `ADAPTERS` puis `KNOWN_ATS` en découlent, donc le nouvel `ats` devient
   automatiquement acceptable dans `sources.yaml`.
5. Figer une **réponse réelle** dans `tests/fixtures/<ats>_<board>.json` et
   ajouter un `AdapterCase` à `ATS_CASES` dans `tests/adapter_cases.py` : les
   tests transverses (schéma commun, 404, corps malformé, erreur serveur)
   s'appliquent alors d'office au nouvel adaptateur.

## Ajouter un agrégateur

Même marche à suivre, avec trois différences :

1. Hériter de **`AggregatorAdapter`** (et non de `HttpAdapter`), déclarer
   `site_url`, et implémenter `_entries` (récupérer les enregistrements bruts)
   et `_parse` (en faire un `RawJob`) plutôt que `fetch`. La classe de base
   applique alors les en-têtes de navigateur, le `Referer`, et la règle
   anti-scam.
2. Lire l'entreprise **dans l'offre**. Si elle est introuvable, laisser
   `entreprise=""` : `AggregatorAdapter` écarte l'offre et journalise le rejet.
   Ne jamais y mettre `Source.nom`, qui ne nomme que la plateforme.
3. Pour un flux RSS, appeler `self._request_text(...)` puis
   `parse_rss(source, self.ats, body)`. Le parseur résout au passage les
   entités HTML indéfinies en XML (`&nbsp;`, `&rsquo;`), qui feraient sinon
   échouer le document, et normalise le `<pubDate>` RFC-822.

Ajouter la classe à `AGGREGATOR_ADAPTERS`, l'entrée à `config/aggregators.yaml`
et l'`AdapterCase` à `AGGREGATOR_CASES`.

## Tests

Les fixtures sont des réponses **réellement capturées** le 2026-09-05 — sur les
boards publics GitLab (Greenhouse), Malt (Lever), Ramp (Ashby), Ubisoft
(SmartRecruiters) et BG Prevent (Workable), et sur les douze endpoints
d'agrégateurs — puis tronquées à trois offres témoins et rejouées via
`httpx.MockTransport`. Aucun test n'accède au réseau.

Le catalogue `tests/adapter_cases.py` associe chaque adaptateur à sa fixture.
Les tests transverses de `test_adapters.py` sont paramétrés dessus : **y
ajouter une entrée suffit** à couvrir un nouvel adaptateur, sans écrire de
test. Un garde-fou vérifie qu'aucun adaptateur branché n'est absent du
catalogue — il échapperait sinon silencieusement à ces tests.

```bash
pytest tests/test_adapters.py tests/test_aggregators.py -q
```
