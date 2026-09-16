# JobRadar — Sources d'offres (liste complète)

Sources pour **remote-first software engineer**, classées par priorité d'usage.
Règle d'or pour être premier : **ATS direct > board spécialisé > agrégateur généraliste**. Un agrégateur est toujours en retard sur la source.

Ordre de tes priorités géo repris ici : ① remote-first global · ② remote Asie + relocalisation · ③ remote-first Europe · ④ France.

---

## 1. Moteurs ATS-directs — le plus important (« être premier »)

Ces sources tapent directement les ATS des entreprises, avant que les offres n'atteignent LinkedIn/Indeed.

| Source | URL | Note |
|---|---|---|
| **job-board-aggregator** (open source) | https://github.com/Feashliaa/job-board-aggregator | Pipeline Python qui indexe Greenhouse, Lever, Ashby, Workday, BambooHR, iCIMS, Paylocity. **À lire absolument** — c'est proche de ce que tu construis, réutilisable. |
| **HiddenJobs** | https://hidden-apply.vercel.app/ | Recherche directe sur Greenhouse/Lever/Ashby/Workday/Workable. Génère aussi des requêtes Google ciblées. |
| **ATS Search Query Generator** | https://hidden-apply.vercel.app/tools/ats-search-query-generator | Génère la « Google dork » qui cible les domaines ATS (voir §8). |
| **Ashby/Lever/Greenhouse tracker** | https://ashbyhq-scraper.vercel.app/home | Fusionne les 3 ATS en un feed unique. |
| **Apify — ATS Jobs actors** (payant, réf.) | https://apify.com/deadwood_data_solutions/greenhouse-lever-ashby-workable-jobs-api | 6 ATS normalisés, dédup `ats:company:id`, `onlyNewSinceLastRun`. Bon modèle de conception même si tu ne l'achètes pas. |

## 2. Patterns d'URL ATS — à mettre dans `sources.yaml`

Le `{slug}` se lit dans l'URL de la page carrière de l'entreprise. Endpoints JSON publics, sans clé :

| ATS | Board public | API JSON (à scraper) |
|---|---|---|
| Greenhouse | `boards.greenhouse.io/{slug}` | `https://boards-api.greenhouse.io/v1/boards/{slug}/jobs?content=true` |
| Lever | `jobs.lever.co/{slug}` | `https://api.lever.co/v0/postings/{slug}?mode=json` |
| Ashby | `jobs.ashbyhq.com/{slug}` | `https://api.ashbyhq.com/posting-api/job-board/{slug}` |
| Workable | `apply.workable.com/{slug}/` | `https://apply.workable.com/api/v3/accounts/{slug}/jobs` |
| SmartRecruiters | `careers.smartrecruiters.com/{slug}` | `https://api.smartrecruiters.com/v1/companies/{slug}/postings` |
| Recruitee | `{slug}.recruitee.com` | `https://{slug}.recruitee.com/api/offers` |

## 3. Entreprises remote-first à cibler (starter `sources.yaml`)

⚠️ **Vérifie chaque slug** en ouvrant la page carrière (le slug est dans l'URL). ✅ = assez sûr · ❓ = à confirmer.

| Entreprise | ATS probable | URL board | Conf. |
|---|---|---|---|
| Kraken | Greenhouse | `boards.greenhouse.io/kraken` | ✅ |
| GitLab | Greenhouse | `boards.greenhouse.io/gitlab` | ✅ |
| Stripe | Greenhouse | `boards.greenhouse.io/stripe` | ✅ |
| Coinbase | Greenhouse | `boards.greenhouse.io/coinbase` | ✅ |
| Linear | Ashby | `jobs.ashbyhq.com/linear` | ✅ |
| Malt (FR) | Lever | `jobs.lever.co/malt` | ✅ |
| Vercel | Ashby | `jobs.ashbyhq.com/vercel` | ❓ |
| Ramp | Ashby | `jobs.ashbyhq.com/ramp` | ❓ |
| Zapier | Greenhouse | `boards.greenhouse.io/zapier` | ❓ |
| Cloudflare | Greenhouse | `boards.greenhouse.io/cloudflare` | ❓ |
| Grafana Labs | Greenhouse | `boards.greenhouse.io/grafanalabs` | ❓ |
| Elastic | Greenhouse | `boards.greenhouse.io/elastic` | ❓ |
| Sourcegraph | Ashby | `jobs.ashbyhq.com/sourcegraph` | ❓ |
| Deel | Ashby | `jobs.ashbyhq.com/deel` | ❓ |
| Remote.com | Greenhouse | `boards.greenhouse.io/remotecom` | ❓ |
| Supabase | — | (page carrière propre) | ❓ |
| Hugging Face | — | (page carrière propre) | ❓ |

Autres cibles remote-first fortes (SWE, à onboarder pareil) : **Automattic, Doist, Buffer, Hotjar, Ahrefs, Basecamp, Mattermost, Toggl, Close, Aha!, HashiCorp, Turing, X-Team, Ledger (FR), Qonto (FR), Doctolib (FR), Alan (FR), PayFit (FR)**.

Extrait `sources.yaml` pour démarrer :
```yaml
sources:
  - { nom: Kraken,   ats: greenhouse, token: kraken }
  - { nom: GitLab,   ats: greenhouse, token: gitlab }
  - { nom: Stripe,   ats: greenhouse, token: stripe }
  - { nom: Coinbase, ats: greenhouse, token: coinbase }
  - { nom: Linear,   ats: ashby,      token: linear }
  - { nom: Malt,     ats: lever,      token: malt }
```

---

## 4. Agrégateurs remote généralistes fiables (priorité ①)

| Source | URL |
|---|---|
| Wellfound (ex-AngelList) — startups, salaire/equity affichés | https://wellfound.com/jobs |
| We Work Remotely — catégorie Programming | https://weworkremotely.com/categories/remote-programming-jobs |
| RemoteOK — gros volume tech, API JSON | https://remoteok.com/ · API : `https://remoteok.com/api` |
| Remotive — API JSON | https://remotive.com/ · API : `https://remotive.com/api/remote-jobs?search=engineer` |
| Himalayas — filtres timezone | https://himalayas.app/jobs |
| Working Nomads | https://www.workingnomads.com/jobs |
| Remote.co | https://remote.co/remote-jobs/developer/ |
| Jobspresso | https://jobspresso.co/remote-work/ |
| Arc.dev — remote dev, matching par stack | https://arc.dev/remote-jobs |
| NoDesk *(tu l'as)* | https://nodesk.co/remote-jobs/ |
| Remote100K — roles 100k+, vérifiés | https://remote100k.com/ |
| DailyRemote | https://dailyremote.com/remote-developer-jobs |
| Y Combinator — Work at a Startup *(tu l'as)* | https://www.ycombinator.com/jobs |

## 5. Asie + relocalisation (priorité ②)

| Source | URL |
|---|---|
| **Relocate.me** — spécialiste relocalisation + visa | https://relocate.me/ |
| Relocate.me — SWE Japon | https://relocate.me/international-jobs/software-engineer/japan |
| **Tech in Asia Jobs** — SEA (Indonésie, SG, Vietnam, PH…) | https://www.techinasia.com/jobs |
| **TokyoDev** *(tu l'as)* — Japon, anglophone | https://www.tokyodev.com/ |
| **Japan Dev** — Japon, startups tech anglophones | https://japan-dev.com/ |
| Wantedly — Japon | https://www.wantedly.com/ |
| NodeFlair — Singapour / SEA, données salaire | https://nodeflair.com/jobs |
| Glints — Asie du Sud-Est | https://glints.com/ |
| JobCube SG *(tu l'as)* | https://jobcube.sg/ |
| Remote Rocketship — filtre Asie *(tu l'as)* | https://www.remoterocketship.com/country/asia/jobs/software-engineer |
| **Guide GitHub — tech jobs with relocation** | https://github.com/AndrewStetsenko/tech-jobs-with-relocation |

## 6. Europe remote-first (priorité ③)

| Source | URL |
|---|---|
| Landing.jobs — tech Ibérie / Sud Europe | https://landing.jobs/ |
| Honeypot — DACH (DE/NL/AT/CH), dev | https://www.honeypot.io/ |
| Jobgether — EU, filtrage timezone | https://jobgether.com/ |
| Europe Remotely | https://europeremotely.com/ |
| WeAreDevelopers — Europe, dev | https://www.wearedevelopers.com/jobs |
| OfferZen — dev, EU/UK/ZA | https://www.offerzen.com/ |
| Welcome to the Jungle (ex-Otta) — UK/EU mid-senior | https://www.welcometothejungle.com/ |
| EuroTopTech *(tu l'as, payant)* — 100k+ EU | https://www.eurotoptech.com/jobs |
| EU Remote Jobs *(tu l'as)* | https://euremotejobs.com/ |

## 7. France (priorité ④)

| Source | URL |
|---|---|
| **Welcome to the Jungle** — la référence tech FR | https://www.welcometothejungle.com/fr/jobs |
| LesJeudis — IT/dev historique | https://www.lesjeudis.com/ |
| Free-Work — IT (freelance + CDI) | https://www.free-work.com/fr |
| Choose Your Boss — dev | https://www.chooseyourboss.com/ |
| HelloWork | https://www.hellowork.com/ |
| APEC — cadres | https://www.apec.fr/ |
| Indeed France (filtre remote) | https://fr.indeed.com/ |

## 8. Méta, annuaires & techniques

- **Hacker News « Who is Hiring »** (mensuel, cherche « remote »/« visa ») : mirror consultable https://hnhiring.com/ · API : `https://hn.algolia.com/api/v1/`
- **Google dork ATS** (à réutiliser dans tes recherches manuelles) :
  ```
  site:boards.greenhouse.io ("Software Engineer" OR "SWE") ("remote" OR "anywhere")
  -intern -senior -staff -principal -manager -director after:2026-07-01
  ```
  (remplace le domaine par `jobs.lever.co`, `jobs.ashbyhq.com`, etc.)
- **jobboardsearch.com** — annuaire de job boards de niche, filtrable (remote, Asie, relocation…) : https://jobboardsearch.com/
- **Canal Telegram Relocate.me** — alertes relocation quotidiennes.

## 9. Ta liste actuelle — validée

| Lien | Verdict |
|---|---|
| kraken.com/careers | ✅ Page carrière d'entreprise remote-first (crypto) — passe-la en ATS direct (Greenhouse). |
| euremotejobs.com | ✅ Agrégateur EU, correct (voir §6). |
| nodesk.co/…/engineering | ✅ Curé, fiable. |
| meetfrank.com/…software-engineering | ⚪ App emploi, volume moyen — secondaire. |
| eurotoptech.com/jobs | ✅ Réel, EU 100k+, mais **payant**. |
| ycombinator.com/jobs | ✅ Excellent (startups, beaucoup de remote). |
| remoterocketship.com/…/asia | ✅ Bon pour ta priorité ② (filtre Asie). |
| jobleads.com | ⚪ Orienté service premium/exécutif — signal/bruit moyen, basse priorité. |
| jobcube.sg | ✅ Niche Singapour, utile pour l'Asie. |
| tokyodev.com | ✅ Top pour le Japon. |
| productjobsanywhere.com | ⚪ Orienté Product, peu de SWE — basse priorité. |
| omnijobs.io | ⚪ Agrégateur généraliste — OK en complément. |

---

## Notes de fiabilité

- **Priorise l'ATS direct** (§2-3) : c'est là que tu gagnes le pari du « premier ». Les agrégateurs (§4) servent de filet, pas de source principale.
- **Dédup inter-sources indispensable** : la même offre apparaîtra sur l'ATS ET 3 agrégateurs. Ton ID `ats:entreprise:job_id` (US-3.3.1) règle ça.
- **Méfie-toi du « remote » menteur** : beaucoup d'offres taguées remote sont hybrides/timezone-locked. Ton filtre de localisation (US-3.2.3) doit exiger un vrai remote et repérer les mentions de relocalisation.
- **Anti-scam** : sur les agrégateurs ouverts (RemoteOK, Indeed), écarte les posts sans entreprise identifiable ou demandant un paiement.
- **Slugs à re-vérifier périodiquement** : une entreprise peut changer d'ATS. Si un endpoint renvoie 404, le token a changé — ré-ouvre la page carrière.