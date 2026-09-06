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

Et **trois mécanismes d'accès**, qui ne se recoupent pas :

| Mécanisme | Feature | Ce qu'on lit | User-Agent | Sources |
|---|---|---|---|---|
| API JSON d'un ATS | 2.2 | le board d'une entreprise | `JobRadar/0.1` | 5 plateformes, 26 entreprises |
| API JSON ou flux RSS d'un agrégateur | 2.3 | un endpoint global | navigateur | 12 |
| **Sitemap + schema.org `JobPosting`** | 2.5 | des **pages HTML** | `JobRadar/0.1` | 1 (Japan Dev) |

Le troisième est un **crawl**, et c'est la seule famille qui en soit un : il
charge des pages que le site n'a pas publiées pour être consommées par une
machine. D'où trois obligations qui n'existent nulle part ailleurs —
`robots.txt`, incrémental, délai entre requêtes — détaillées plus bas.

Et d'où l'inversion du User-Agent, qui n'est pas une incohérence. Les
en-têtes de navigateur de la Feature 2.3 existent pour ne pas être filtré
**à tort** sur un endpoint publié pour être consommé. Un crawl, lui, doit
pouvoir être vu, limité, ou exclu par un `User-agent: JobRadar` — un droit
que `robots.py` respecte, et qui serait décoratif si on se présentait en
Chrome. Japan Dev sert `robots.txt`, ses sitemaps et ses pages en 200 sous
ce nom (vérifié le 2026-09-06).

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
| Panne passagère (réseau, timeout, 5xx) | **une** nouvelle tentative après backoff, puis `AdapterError` |
| Une offre du board est incomplète | offre ignorée + journalisée, **les autres sont retournées** |
| Un champ est absent | valeur par défaut sûre (`""`, `remote_type="unknown"`), **jamais de `KeyError`** |
| Offre d'agrégateur sans employeur | offre **écartée** + rejet journalisé (règle anti-scam) |

## Politesse réseau (US-2.4.2)

`HttpAdapter._request` applique trois règles à **chaque** requête, y compris
celles des adaptateurs qui paginent ou enchaînent plusieurs appels :

| Règle | Valeur | Où la changer |
|---|---|---|
| Timeout | 15 s, **surchargeable par adaptateur** | `DEFAULT_TIMEOUT`, ou `default_timeout` sur la classe |
| Nouvelles tentatives | 1, après 2 s | `RETRY_DELAYS` dans `base.py` |
| User-Agent | `JobRadar/0.1` (ATS et crawl) · navigateur (agrégateurs d'API) | `USER_AGENT` / `BROWSER_HEADERS` / `CRAWL_HEADERS` |

Le retry est **ciblé** : il ne se déclenche que sur ce qui peut se réparer
tout seul — panne réseau, timeout, et les statuts de `RETRYABLE_STATUS`
(500, 502, 503, 504). Un 403 ou un 404 ne se répare pas en rappelant deux
secondes plus tard ; le réessayer n'ajouterait que du bruit chez la
plateforme.

Le **429 en est volontairement absent**. Il signifie « vous appelez trop » :
y répondre par un appel de plus est exactement le mauvais geste. La réponse
du projet au débit, c'est `max_calls_per_day` et la cadence lente — voir
plus bas.

`RETRY_DELAYS` est un tuple de délais : une valeur = une seconde tentative,
deux valeurs = deux, avec l'espacement qu'on y écrit. C'est le seul endroit
à toucher pour changer la politique, et `MAX_ATTEMPTS` en découle.

L'attente passe par `base._wait`, isolée exprès : `tests/conftest.py` la
neutralise pour toute la suite, sinon chaque panne simulée coûterait deux
secondes réelles.

## Isolation par source (US-2.4.1)

Une source en échec **n'arrête pas le run** — mais c'est la responsabilité du
Collector (`src/collect.py`), pas de l'adaptateur, qui lui se contente de
lever une `AdapterError` nommant la source.

`collect_sources` interroge chaque source dans son propre `try/except`, et
rend un `CollectReport` : `succeeded`, `failed`, `jobs`. Il attrape
`Exception` et pas seulement `AdapterError` — l'échec attendu est bien
`AdapterError`, mais un adaptateur qui bute sur un cas inédit lèvera un
`KeyError`, et ce bug ne doit pas coûter la collecte des trente-six autres
sources.

Le run ne sort en erreur (code 1) que si **toutes** les sources échouent :
là, ce n'est plus une source qui a un problème, c'est le runner, un secret
ou nous. Un 404 n'est pas un échec : la source a répondu, elle n'a
simplement plus ce board.

### Le troisième état : la source tarie

Une source qui répond **sans une seule offre** n'est ni un succès ni un
échec, et c'est le trou par lequel une panne passe inaperçue : une API qui
ferme en renvoyant une collection vide compte comme un succès dans un bilan
« 37 collectées, 0 en échec ». C'est exactement ce qui est arrivé à
Free-Work, dont l'endpoint répond `200` avec `hydra:totalItems: 0` quelle
que soit la requête.

D'où un état nommé — `SourceResult.empty`, `CollectReport.empty` — un
avertissement journalisé, et une ligne dédiée dans le bilan du run qui
**nomme** les sources concernées. Un board sans poste ouvert existe aussi,
donc c'est un avertissement, jamais un échec : ce qui compte, c'est qu'une
source qui se tarit ne puisse plus le faire en silence.

Les trois états se lisent d'un coup d'œil dans le journal, à marqueurs de
même largeur :

```
  ok   GitLab (greenhouse) : 229 offre(s)
  vide Free-Work (freework) : 0 offre
  KO   Malt (lever) : AdapterError: source « Malt » (lever) : statut HTTP 500 …
```

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
| `japandev` | `GET japan-dev.com/sitemap.xml` + pages d'offres | sitemap + JSON-LD | — |

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

## Sources lues par crawl : sitemap + schema.org (Feature 2.5)

Toute source qui veut apparaître dans **Google for Jobs** doit publier un bloc
JSON-LD `JobPosting` sur chaque page d'offre et lister ces pages dans son
`sitemap.xml`. Deux formats standards, stables, faits pour les machines : un
**seul** adaptateur les lit tous, là où un scraper demanderait un parseur HTML
par site — le coût de maintenance que `docs/sources-audit.md` refuse.

`JobPostingAdapter` (`jobposting.py`) hérite d'`AggregatorAdapter` : mêmes
en-têtes, même règle anti-scam, même schéma `RawJob`. Ce qui s'y ajoute :

| Précaution | US | Où | Ce qu'elle évite |
|---|---|---|---|
| On s'identifie (`JobRadar/0.1`, sans `Referer`) | 2.5.3 | `CRAWL_HEADERS` | crawler sous un faux nom un site qu'on ne fait que lire |
| `robots.txt` lu et respecté | 2.5.3 | `robots.py` | se faire bannir, et crawler ce qu'un site refuse |
| Incrémental sur `<lastmod>` | 2.5.0 | `sitemap.py` + `core/state.py` | recharger le catalogue à chaque run |
| Pré-filtrage sur le slug d'URL | 2.5.1 | `SlugFilter` | charger une page pour la jeter ensuite |
| Plafond de pages par run | — | `max_pages_per_run` | un run de durée inconnue |
| Délai entre deux requêtes | 2.5.3 | `RobotsRules.delay` | frapper un hôte en rafale |

Trois points qui ont demandé une décision, et qui sont couverts par des tests :

- **`Disallow: /` désactive la source**, il ne se contourne pas. Une seule
  requête part alors : celle du `robots.txt` lui-même. Et c'est le groupe
  `User-agent: *` qui fait foi, **même déclaré en dernier** — Europe Remotely
  en publie treize, et le premier (`Googlebot`) dit l'inverse du dernier.
- **Un `robots.txt` injoignable met la source en échec**, pas en libre-service.
  Un 404 est autre chose : c'est l'absence de règle, donc la permission.
- **Sitemap sans `<lastmod>` → repli sur les URLs déjà chargées.** Japan Dev
  n'en publie aucun sur ses 1 449 URLs. On reste incrémental, en se repérant
  sur l'identité de l'URL au lieu d'une date ; le prix est de mémoriser ces
  URLs dans `state/crawl.json`, ce que le plafond `MAX_KNOWN_URLS` borne.

Le **curseur n'avance que jusqu'à la dernière page réellement chargée** : le
plafond de pages ne doit jamais faire enjamber en silence des offres qu'on n'a
pas eu le temps de lire. Et il n'est enregistré qu'en cas de succès — un
sitemap illisible ne doit pas faire sauter ce qu'il contenait.

### Où l'état vit

`state/crawl.json`, versionné dans le dépôt et commité par `collect-slow.yml`
(le runner Actions est éphémère : sans commit, l'incrémental n'existe pas en
production). Voir `state/README.md`. Un fichier absent ou corrompu vaut un
état vide : ça coûte un recrawl, jamais un run en échec.

### Ajouter une source crawlée

1. Sous-classer `JobPostingAdapter` en déclarant `ats`, `site_url`,
   `sitemap_url` et, si besoin, `job_url_pattern`. **Rien d'autre** — pas de
   parseur, pas de méthode : le jour où une sous-classe a besoin d'un
   sélecteur HTML, c'est un scraper qu'on écrit, et un test le refuse.
2. Ajouter la classe à `AGGREGATOR_ADAPTERS` et l'entrée à
   `config/aggregators.yaml`.
3. Figer **quatre** réponses réelles dans `tests/fixtures/` — `robots.txt`,
   l'index de sitemaps, le sitemap d'URLs, une page d'offre — et ajouter
   l'`AdapterCase` correspondant avec `extra=lambda: crawl_kwargs()`, qui lui
   donne un état neuf et le pré-filtre du dépôt.

Avant tout ça, vérifier que le site est réellement atteignable : un pare-feu
applicatif répond volontiers `202` avec une page de défi JavaScript, ce qui
n'est pas un accès. La commande de contrôle est dans `docs/sources-audit.md` —
c'est ce qui a fait sortir Welcome to the Jungle du périmètre le 2026-09-06,
la veille de son branchement.

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

Ce plafond n'est pas toujours celui de la plateforme : il peut être celui
qu'on **s'impose**. Les sources crawlées (Feature 2.5) déclarent 4 appels par
jour de leur propre chef — un crawl coûte au minimum trois requêtes de
repérage par run à un site qui n'a rien demandé, là où un agrégateur expose
une API faite pour ça. La mécanique est la même, il n'y a rien de plus à
retenir.

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

Les tests de robustesse et de politesse vivent à part, dans
`tests/test_robustness.py` : ils simulent pannes, lenteurs et 5xx via un
`httpx.MockTransport` qui répond **selon l'URL appelée**, ce qui permet de
faire tomber une plateforme sur trois et de vérifier que les deux autres
sont bien collectées.

```bash
pytest tests/test_robustness.py -q
```

Ceux de l'adaptateur crawlé vivent dans `tests/test_jobposting.py`. Ils
**comptent des requêtes** plus souvent qu'ils ne comptent des offres : « ne
pas charger la page » est la moitié de ce que cette famille promet.

```bash
pytest tests/test_jobposting.py -q
```
