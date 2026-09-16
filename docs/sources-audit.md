# Audit des sources — que reste-t-il de `website.md` ?

Audit exhaustif mené le **2026-09-05** : chaque lien de `website.md` a été
appelé pour vérifier s'il expose un endpoint machine utilisable sans clé.

> **Correction du premier passage.** Le premier audit concluait « 403,
> inexploitable » pour huit sites. C'était un artefact : les requêtes
> partaient sans en-têtes de navigateur réalistes. Avec un `User-Agent`,
> `Accept-Language` et `Referer` corrects, **17 des 25 sites réputés bloqués
> répondent 200**. Le tableau ci-dessous intègre ce second passage. Ce fichier est la source de vérité du périmètre de
l'Epic 2 ; `website.md` reste la liste d'intentions, celui-ci dit ce qui
marche vraiment.

## Résumé

| | Listé dans `website.md` | Exploitable | Branché aujourd'hui |
|---|---|---|---|
| Plateformes ATS | 6 | 7 (dont Teamtailor, non listé) | **5** |
| Entreprises | 35 | 27 | **26** |
| Agrégateurs & boards | ~43 | **13** | **13** |

**2 904 offres** (dont 1 016 techniques) sont déjà accessibles avec les
26 entreprises branchées. Côté agrégateurs, **12 exposent une API JSON ou un
flux RSS** — tous branchés depuis la Feature 2.3 — et **1 est atteignable via
sitemap + schema.org** : Japan Dev, branchée par la Feature 2.5.

> **Révision du 2026-09-06.** Le compte des agrégateurs exploitables passe de
> 14 à 13 : **Welcome to the Jungle est passée derrière un pare-feu
> applicatif** dans les vingt-quatre heures qui ont suivi l'audit. Le détail
> et la procédure de re-vérification sont plus bas, dans la section
> sitemap.

---

## 1. Plateformes ATS

| Plateforme | Endpoint | État |
|---|---|---|
| Greenhouse | `GET boards-api.greenhouse.io/v1/boards/{token}/jobs?content=true` | ✅ adaptateur |
| Lever | `GET api.lever.co/v0/postings/{token}?mode=json` | ✅ adaptateur |
| Ashby | `GET api.ashbyhq.com/posting-api/job-board/{token}` | ✅ adaptateur |
| SmartRecruiters | `GET api.smartrecruiters.com/v1/companies/{token}/postings` | ✅ adaptateur |
| Workable | `POST apply.workable.com/api/v3/accounts/{token}/jobs` | ✅ adaptateur |
| Recruitee | `GET {token}.recruitee.com/api/offers` | ⬜ US-2.2.5 |
| **Teamtailor** | `GET {token}.teamtailor.com/jobs.json` (JSON Feed) | ⬜ US-2.2.6 — **absent de `website.md`**, découvert via PayFit |

## 2. Entreprises

### Branchées (26) — voir `config/sources.yaml`

Greenhouse (13) · Lever (3) · Ashby (9) · Workable (1).

### Découvertes pendant l'audit (12 des 26)

`website.md` ne donnait ni ATS ni slug pour ces entreprises. Les slugs ont été
retrouvés en récupérant la page carrière et en y cherchant le domaine ATS.

| Entreprise | ATS | Token | Remarque |
|---|---|---|---|
| Ahrefs | greenhouse | `ahrefsjobs` | |
| Mattermost | greenhouse | `mattermost` | |
| Turing | greenhouse | `turing` | |
| Doctolib | greenhouse | `doctolib` | expose aussi un board Ashby actif |
| Contentsquare | lever | `contentsquare` | **Hotjar** y publie depuis son rachat |
| Qonto | lever | `qonto` | |
| Close | ashby | `Close` | majuscule significative |
| Alan | ashby | `alan` | |
| Ledger | ashby | `ledger` | |
| Supabase | ashby | `supabase` | `website.md` la disait sans ATS |
| Buffer | ashby | `buffer` | |
| Hugging Face | workable | `huggingface` | `website.md` la disait sans ATS |

Corrections apportées à `website.md` au passage : **Kraken** et **Zapier** sont
sur Ashby (pas Greenhouse), **Vercel** et **Sourcegraph** sur Greenhouse (pas
Ashby), et **Figma** n'est plus sur Lever du tout.

### Bloquées (9)

| Entreprise | Raison |
|---|---|
| PayFit | Teamtailor — débloquée par US-2.2.6 |
| Deel | board Ashby public vidé ; 282 offres servies uniquement dans le HTML de `deel.com/careers` |
| Automattic, Doist, Basecamp, Toggl, Aha!, HashiCorp, X-Team | site carrière maison, aucun ATS public |

Un scraper par entreprise casserait le modèle « un adaptateur = une
plateforme, réutilisable pour N entreprises ». Ces 8 sont hors périmètre ;
elles remonteront via les agrégateurs.

Piège rencontré : les slugs `aha` sur Greenhouse et sur Workable existent tous
les deux mais appartiennent à d'**autres** organisations (une clinique
vétérinaire, un organisme de formation). Toujours vérifier le contenu du board,
pas seulement le code HTTP.

## 3. Agrégateurs & boards de niche

### API JSON ou flux RSS (12) — périmètre de la Feature 2.3

| Source | Endpoint | Format | Priorité géo |
|---|---|---|---|
| RemoteOK | `remoteok.com/api` | JSON (1ʳᵉ entrée = mentions légales, à sauter) | ① |
| Remotive | `remotive.com/api/remote-jobs?search=engineer` | JSON | ① |
| We Work Remotely | `weworkremotely.com/categories/remote-programming-jobs.rss` | RSS | ① |
| Himalayas | `himalayas.app/jobs/api?limit=N` | JSON | ① |
| Working Nomads | `workingnomads.com/api/exposed_jobs/` | JSON | ① |
| NoDesk | `nodesk.co/remote-jobs/index.xml` | RSS | ① |
| Jobspresso | `jobspresso.co/?feed=job_feed` | RSS | ① |
| HN « Who is Hiring » | `hn.algolia.com/api/v1/` | JSON | ① |
| Landing.jobs | `landing.jobs/api/v1/jobs` | JSON | ③ |
| EU Remote Jobs | `euremotejobs.com/?feed=job_feed` | RSS | ③ |
| Free-Work | `free-work.com/api/job_postings?contracts=permanent&page=N` | JSON | ④ |
| **APEC** | `POST apec.fr/cms/webservices/rechercheOffre` | JSON | ④ |

RemoteOK répond en ~40 s : prévoir un timeout dédié, au-delà du défaut de 15 s.
L'APEC n'a pas d'API documentée — c'est l'endpoint que son propre front
appelle. Le corps du POST est strict : un champ inconnu renvoie un 500 qui
nomme le champ fautif, ce qui rend la mise au point rapide.

**Tous ces appels doivent partir avec des en-têtes de navigateur réalistes.**
Le `User-Agent` « JobRadar/0.1 » suffit pour les ATS, pas pour les agrégateurs.

### Corrections apportées en branchant ces 12 sources (Feature 2.3)

Écrire les adaptateurs a démenti deux points de l'audit et fait apparaître
trois contraintes que l'appel `curl` seul ne montrait pas.

| Point | Ce que disait l'audit | Ce qui est vrai |
|---|---|---|
| **EU Remote Jobs** | `euremotejobs.com/feed/` | Ce chemin sert le flux du **blog** — que des articles de conseil, aucune offre. Le flux d'offres est `?feed=job_feed`, même convention que Jobspresso. Le tableau ci-dessus est corrigé. |
| **Nombre de flux RSS** | « 4 des 11 sources sont des flux RSS » (backlog) | Le tableau en liste bien **12**, dont **4 RSS** : WWR, NoDesk, Jobspresso, EU Remote Jobs. Les 12 sont branchées. |
| **Landing.jobs** | API JSON exploitable | Exact, mais l'API **ne renvoie pas le nom de l'entreprise** : il n'existe que dans le slug de l'URL (`/at/{slug}/`), d'où il est reconstitué. |
| **Remotive** | API JSON ouverte | Ses conditions d'utilisation demandent **au plus 4 appels par jour** (les offres y sont retardées de 24 h de toute façon) et une citation de la source. Réglé : elle est collectée par `collect-slow.yml`, planifié 4 fois par jour, et exclue de la boucle de 15 min. La citation est satisfaite — le pipeline republie l'URL Remotive de l'offre. |
| **RemoteOK** | API JSON ouverte | Ses textes non-anglais sont servis en **mojibake** (double encodage UTF-8) : le défaut est dans ses données, il est réparé à la lecture. |

Deux flux RSS (NoDesk, Jobspresso) publient des entités HTML indéfinies en XML
(`&rsquo;`, `&nbsp;`) : un parseur XML strict échoue dessus. Elles sont résolues
avant le parsing.

**Où se trouve l'entreprise** — ce n'est jamais au même endroit, et c'est ce qui
fait la différence entre un agrégateur et un ATS : champ dédié (RemoteOK,
Remotive, Himalayas, Working Nomads, Free-Work, APEC, flux WP Job Manager),
préfixe du titre (WWR), suffixe du titre (NoDesk), slug de l'URL
(Landing.jobs), première ligne du commentaire (Hacker News). Une offre dont
l'employeur reste introuvable est **écartée** : c'est la règle anti-scam de
`website.md`.

### Atteignables par sitemap + schema.org `JobPosting` (1 sur 2) — Feature 2.5

C'est le second mécanisme, et il ne demande aucun accord privé : toute source
qui veut apparaître dans **Google for Jobs** est obligée de publier un bloc
JSON-LD `JobPosting` sur chaque page d'offre, et de lister ces pages dans son
`sitemap.xml`. C'est un format standard, stable et destiné aux machines.

| Source | Sitemap | JSON-LD | Volume | robots.txt | État |
|---|---|---|---|---|---|
| Japan Dev | 1 index → 1 sitemap de 1 449 URLs, dont **290 offres** | ✅ `JobPosting` complet | ~290 offres | 2 règles (deux PDF), offres autorisées | ✅ adaptateur `japandev` |
| Welcome to the Jungle | 24 shards × 10 000 URLs, `<lastmod>` sur **100 %** | ✅ `JobPosting` complet | ~240 000 offres | aucune règle **le 2026-09-05** | ⛔ bloquée depuis le 2026-09-06 |

Le crawl intégral est hors de question, mais il est inutile : le `<lastmod>`
du sitemap permet de ne récupérer que les offres nouvelles depuis le dernier
run, et l'URL contient le slug du poste — on peut donc **filtrer sur le titre
avant même de charger la page**. Le volume réel retombe à quelques dizaines de
requêtes par jour.

#### Japan Dev — branchée, avec une correction d'audit

Vérifiée en direct le **2026-09-06**, et conforme sur tout sauf un point :

- `sitemap.xml` est un **index** qui renvoie vers
  `japan-dev.com/cdn/sitemaps/sitemap.xml`, lequel liste 1 449 URLs dont
  **290 offres** (`/jobs/{entreprise}/{slug}`) ;
- ⚠️ **aucune de ces 1 449 URLs ne porte de `<lastmod>`.** L'incrémental par
  date, qui est le mécanisme nominal de la Feature 2.5, ne s'y applique donc
  pas : c'est le repli sur les URLs déjà chargées qui prend le relais, et
  Japan Dev en est le cas d'école ;
- le pré-filtrage sur le slug ramène ces 290 offres à **67 pages candidates**
  avec le `title_include` actuel de `filters.yaml` — les 223 autres ne coûtent
  pas une requête ;
- le `JobPosting` est complet : `title`, `hiringOrganization`, `jobLocation`,
  `datePosted`, `description`, `employmentType`, `identifier`, et
  `jobLocationType: TELECOMMUTE` sur les offres en télétravail.

#### Welcome to the Jungle — bloquée le lendemain de l'audit

**Vérifié le 2026-09-06 : le site est entièrement derrière un pare-feu
applicatif AWS WAF.** Toutes les adresses testées — `robots.txt`,
`sitemap.xml`, `sitemap_index.xml`, `/fr/sitemap.xml`, une page d'offre —
répondent **HTTP 202** avec une page de défi JavaScript (`awswaf`,
`challenge.js`) au lieu du contenu. Le comportement est identique avec et sans
en-têtes de navigateur complets, en HTTP/1.1 comme en HTTP/2, sur `www.` comme
sur le domaine nu, et il se reproduit à chaque essai.

Ce n'est donc pas un blocage à contourner par un en-tête, comme l'étaient les
17 sites du second passage : c'est un défi qui exige l'exécution de
JavaScript, c'est-à-dire un navigateur headless. WTTJ rejoint donc la
catégorie « protection anti-bot » ci-dessous, avec la même conclusion que
NodeFlair ou TokyoDev — le coût d'infra et la posture ne valent pas la source.

**Ce que ça ne coûte pas.** L'adaptateur générique de la Feature 2.5 ne
connaît la structure HTML d'aucun site : la brancher le jour où le pare-feu
tombe demande une sous-classe de trois lignes (`ats`, `site_url`,
`sitemap_url`), une entrée dans `aggregators.yaml`, et une fixture capturée.
Rien de ce travail n'est perdu.

**Avant de la rebrancher**, refaire ce contrôle :

```bash
UA='Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36'
curl -s -o /dev/null -w '%{http_code} %{content_type}\n' -A "$UA" -L \
  https://www.welcometothejungle.com/sitemap.xml
# 200 application/xml → rebranchable ; 202 text/html → toujours le WAF.
```

### Fermées pour de bon (~30)

**Refus explicite** — Europe Remotely publie `Disallow: /` dans son
`robots.txt`. La question est tranchée : on n'y touche pas.

**Protection anti-bot y compris sur le sitemap** — Welcome to the Jungle
(depuis le 2026-09-06, voir ci-dessus), NodeFlair, Glints,
Remote Rocketship, HelloWork, Indeed France. TokyoDev est un cas cruel : son
sitemap est accessible et liste 143 offres, mais chaque page d'offre répond
403. Wellfound interdit `/_jobs/` et ne publie aucun sitemap d'offres.
Franchir ces protections demanderait un navigateur headless avec rotation
d'IP : coût d'infra, maintenance permanente et posture agressive, pour un
projet dont le budget est « ≈ 0 € ».

**Accessibles mais sans données structurées** — DailyRemote, Arc.dev,
Remote100K, OmniJobs, WeAreDevelopers, Relocate.me, Jobgether, LesJeudis,
Wantedly, OfferZen, Remote.co, Honeypot, MeetFrank, JobLeads,
ProductJobsAnywhere, YC Work at a Startup. Leurs pages répondent 200 mais ne
portent pas de `JobPosting` : il faudrait écrire un parseur HTML **par site**,
à refaire à chaque refonte de leur front. C'est le coût de maintenance qu'on
refuse, pas la difficulté technique.

**Cas particuliers** — Tech in Asia n'expose que le flux de son blog.
EuroTopTech est payant. `jobboardsearch.com` et le guide GitHub
« tech-jobs-with-relocation » sont des annuaires à lire à la main. Les entrées
§1 de `website.md` (HiddenJobs, Apify, job-board-aggregator…) sont des outils
de référence, pas des sources.

### Si l'une de ces sources redevient prioritaire

Trois leviers, dans l'ordre de coût croissant : vérifier qu'un `User-Agent`
réaliste ne suffit pas (c'est ce qui a débloqué 17 sites) ; chercher l'API
interne que le front appelle (c'est ce qui a débloqué l'APEC) ; chercher le
sitemap et le JSON-LD (c'est ce qui a débloqué WTTJ). Le navigateur headless
n'arrive qu'après, et reste déconseillé.

## Comment refaire cet audit

Les slugs et les endpoints bougent. Pour re-vérifier :

```bash
# Un board ATS répond-il, et avec combien d'offres ?
curl -s "https://boards-api.greenhouse.io/v1/boards/<slug>/jobs" | jq '.jobs | length'

# Quel ATS une entreprise utilise-t-elle ?
curl -sL -A 'Mozilla/5.0' https://<entreprise>/careers \
  | grep -oE '(job-boards|boards)\.greenhouse\.io/[a-zA-Z0-9_.-]+|jobs\.lever\.co/[a-zA-Z0-9_.-]+|jobs\.ashbyhq\.com/[a-zA-Z0-9_.-]+|apply\.workable\.com/[a-zA-Z0-9_.-]+' \
  | sort -u
```

Vérifier ensuite que le board appartient bien à l'entreprise visée : un slug
qui répond 200 n'est pas un slug correct.

```bash
# Une source est-elle atteignable par sitemap + schema.org ?
UA='Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36'

# 1. Le sitemap répond-il vraiment du XML ? (202 + text/html = pare-feu)
curl -s -o /dev/null -w '%{http_code} %{content_type}\n' -A "$UA" -L https://<site>/sitemap.xml

# 2. Une page d'offre porte-t-elle un bloc JobPosting ?
curl -s -A "$UA" -L '<url-d-une-offre>' \
  | grep -o 'application/ld+json' | wc -l

# 3. Le robots.txt nous autorise-t-il ? (le groupe `*` peut être le dernier)
curl -s -A "$UA" -L https://<site>/robots.txt
```

⚠️ **Un statut 2xx ne suffit pas à conclure.** Un pare-feu applicatif répond
volontiers `202` avec une page de défi : c'est ce qui distingue le cas WTTJ
d'un vrai accès. Regarder le `Content-Type` et la taille du corps, pas
seulement le code.

⚠️ **Le périmètre bouge en jours, pas en trimestres.** WTTJ est passée
d'« aucune règle, 240 000 URLs accessibles » à « pare-feu sur tout le site »
en vingt-quatre heures. Un endpoint vérifié la veille n'est pas un endpoint
acquis.
