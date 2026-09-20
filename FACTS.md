# Verified facts

Single source of truth. Every number used anywhere in this repo, in a post, or
out loud must appear here with how it was established. If it isn't here, don't
say it.

Last verified: **2026-09-20**.

Six claims have already been retracted from this project. The pattern in every
case was the same: a number that was real, attached to an interpretation that
was not. Check the interpretation, not just the arithmetic.

---

## A. Measured by us, reproducible

Run `BUDGET=25 .venv/bin/python run.py`, `.venv/bin/python freshness.py`,
`.venv/bin/python crosscheck.py` to reproduce.

| Fact | Value | How |
|---|---|---|
| Resources in the directory | **1,759** | live `GET /api/v2/resources/count`, 2026-09-20 |
| Services in the directory | **7,577** | live `GET /api/v2/services/count`, 2026-09-20 |
| Curation dataset | **2,569 rows / 816 resources** | live `GET /api/v2/datathon/content_curation_dataset`, 2026-09-20 |
| Approved orgs in our snapshot | **813** | counted over `data_v2/` (816 harvested, 3 inactive) |
| **No verification signal at all** — no `verified_at`, no `certified_at`, `certified` not set | **523 (64.3%)** | counted |
| No `verified_at` *or* `certified_at` **date** (75 of these carry a `certified` flag) | 598 (73.6%) | counted |
| `verified_at` present | 144, newest **2022-10-12** | counted |
| `certified_at` present | 124, newest **2026-09-15** (9 in 2026) | counted |
| `updated_at` present | **813/813**, median **53 days**, 808 within 90 days | counted |
| Structural phone defects, whole corpus | **8 of 813** | `verify.check_phone_format` |
| Graph size | **7,405 nodes** | FalkorDB, full corpus |
| Backend equivalence | both agree on every query, `crosscheck.py` exits 0 | full corpus, hops 1–3 |
| Median freshness | **36.2 / 100** | `freshness.py` |
| Agent cost | **$0.0046/listing**, ~8.8s | 22 listings, live OpenRouter pricing |
| Agent false positives | **2 of 7 discrepancies** (pre-fix) | hand-audited 80 listings |
| Sites publishing a sitemap | **49 of 60 (81%)**, 32 declared in `robots.txt` | live probe, random sample |
| Sites carrying `<lastmod>` | 39 of 60 (65%) | same |
| Jev cost per organisation | **$0.000316**, ~1.3s → **~$0.26** for all 813 | 12 orgs, live pricing |

### Calibration — held out, `calibrate.py`

444 (claim, page) rows over 43 organisations. Split **by organisation**, so no
site's page text crosses the split. Ground truth is a mechanical oracle — does the
stored value appear, normalised, in the fetched page — so nothing graded its own
work. Rows the oracle cannot decide are **excluded**, not guessed.

| Fact | Value |
|---|---|
| AUROC, held-out test half (three-way Choice) | **0.992** |
| ECE, held-out | 0.042 (two-noul form: 0.024) |
| `support ≥ 0.69` | **precision 1.000, recall 0.867** (tp 65, fp 0, fn 10) |
| `contradict ≥ 0.99` | **12 flagged, 0 of them actually on the page** |
| Two-noul form at `contradict ≥ 0.99` | flags **nothing**; at ≥0.90 flags 14, of which **5 are actually on the page** |

**This measures faithfulness, not factuality** — whether the judge reads the page
correctly, not whether the directory is right about the world. A phone number can
be correct and simply unpublished. Say it that way.

## B. Verified in a browser, on the live site

| Fact | How |
|---|---|
| Building Futures' Call button is `tel:null`; the number `510-808-7410` is printed beside it as text | loaded `sfserviceguide.org/organizations/2399`, read the `tel:` hrefs |
| Same fault on SF LGBT Center (2346) and Calvary Street Ministries (2087) | same method |
| Larkin Street Youth Clinic's number is absent from the listing; the org's own page shows `415-673-0911` under a `Phone` heading, with "Dial extension 259 for the youth clinic" | read the rendered page |
| v1 API corrupts phone numbers that v2 serves correctly (`94410046`/EG vs `(209) 441-0046`/US, same phone id 4895) | both APIs, side by side |

## C. ShelterTech's own claims — cite as theirs, not as fact

| Claim | Source | Caveat |
|---|---|---|
| 16,000+ monthly users | sheltertech.org | **The same page says "3,000+ services and 900+ organizations". The live API says 7,577 and 1,759.** Their marketing copy is out of date, so the user figure may be too. Say "ShelterTech report 16,000+ monthly users." |
| ~$200,000/year to maintain and expand the guide | ShelterTech programs page | their figure |
| Datathons every two weeks, involving members of the homeless community and paid community reps | help.sfserviceguide.org | **Article dated 5 November 2019.** Seven years old. Do not assert the current cadence. |
| Final review by the Homeless Advocacy Project (JDC) | same | same caveat |

**A search engine now returns this repo as a "third-party source corroborating"
the 16,000 figure. It is not. We cited them; do not cite ourselves back.**

## D. Our estimates — always label as such

| Claim | Status |
|---|---|
| Field half-lives (schedule 180d, phone 1095d, …) | **Judgement, not measured.** Never fitted to data. The right method is to mine their change-request history. |
| Field weights | same |
| ~204 volunteer-hours to move the median from 36 to 70 | arithmetic on the above. An estimate resting on an estimate. |
| Agent post-fix false-positive rate (0 of 80) | **Fitted, not held out** — the guard was built on the data it was measured on. Not a precision claim. |

---

## E. Retracted — do not repeat

| Claim | Why it died |
|---|---|
| "24 of 189 phone numbers are undialable (13%)" | Artifact of reading v1, which mangles US area codes into international dialling codes. 21 of 23 vanish on v2. |
| "An addiction treatment helpline can't be called" | The live page dials it correctly. |
| "5106544000105 should be 510.777.9560" (Meals on Wheels) | We compared only the first of ten stored numbers. The main line was two rows below. |
| "47 duplicate listings" | Same name + same address are usually distinct programme listings, not duplicates. |
| "Website-identity check finds N broken listings" | The count measured our crawl depth, not their data — 53 findings became 18 as the crawler improved. |
| **"73.6% have never been verified"** + "volunteers at monthly datathons, more work than hours available" | **Two errors.** `verified_at` was abandoned around 2022, so it measures a dead field, not neglect. And the directory is actively maintained: 808 of 813 listings updated within 90 days, in daily batches of 40–80 consistent with datathon sessions. Datathons are biweekly, not monthly. |

---

## F. The correct framing

The directory is **actively maintained**. That is measurable: 808 of 813 listings
touched within 90 days, in batches that look like datathon sessions.

What is missing is **provenance**. `updated_at` records that something changed;
it cannot distinguish a listing someone carefully confirmed against reality from
one where a typo was fixed. The field that would carry that — `verified_at` —
stopped being written around 2022. `certified_at` is still used, but rarely: nine
records in 2026.

So the honest problem is not "nobody checks." It is **"there is no machine-readable
record of what was checked, so nobody — including ShelterTech — can tell a fresh
listing from a stale one."** That is what the freshness index is for, and it is
why the index reports 36.2: almost nothing carries evidence stronger than
"well-formed".

This is a smaller and more accurate claim than the one this project started with.
It is also the only one that survives checking.
