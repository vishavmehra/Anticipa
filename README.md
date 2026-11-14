<div align="center">

# Anticipa

### A prevention autopilot for a whole population

Anticipa reads every record and clinical note, decides who is due for preventive care using approved and versioned rules, plans outreach around real clinic capacity, talks to residents in English, Hindi and Arabic, and does not call a case closed until someone has verified that the care happened.

[![License: MIT](https://img.shields.io/badge/license-MIT-2ea44f.svg)](LICENSE)
[![Python 3.12](https://img.shields.io/badge/python-3.12-3776AB.svg?logo=python&logoColor=white)](backend/pyproject.toml)
[![Next.js 16](https://img.shields.io/badge/Next.js-16-000000.svg?logo=nextdotjs&logoColor=white)](frontend/package.json)
[![FHIR R4](https://img.shields.io/badge/HL7%20FHIR-R4-E8590C.svg)](https://hl7.org/fhir/R4/)
[![Claude](https://img.shields.io/badge/AI-Claude%20Haiku%20%2B%20Sonnet-D97757.svg)](https://www.anthropic.com/claude)
[![PostgreSQL + pgvector](https://img.shields.io/badge/PostgreSQL%2017-pgvector-4169E1.svg?logo=postgresql&logoColor=white)](docker-compose.yml)
[![OR-Tools](https://img.shields.io/badge/Google-OR--Tools%20CP--SAT-4285F4.svg)](https://developers.google.com/optimization)
[![Synthetic data only](https://img.shields.io/badge/data-100%25%20synthetic-7B61FF.svg)](#disclosures-and-limits)

[Live demo](https://anticipa-six.vercel.app) · [Judge login](#try-it-live) · [Film](#film-and-walkthrough) · [Why we built it](#why-we-built-it) · [Measured results](#proven-impact) · [Set it up](#set-it-up-locally)

</div>

> **Anticipa is decision support, not a diagnosis tool.** It does not replace doctors, nurses, genetic counsellors or community health workers. Every resident in it is synthetic. The screening rules are encoded from published guidance and have **not** been clinically reviewed. It is not a medical device. See [Disclosures and limits](#disclosures-and-limits).

---

## Try it live

| | |
|---|---|
| **Web app** | https://anticipa-six.vercel.app |
| **API** | https://anticipa-api-osxv.onrender.com ([`/api/v1/health`](https://anticipa-api-osxv.onrender.com/api/v1/health), [`/api/v1/docs`](https://anticipa-api-osxv.onrender.com/api/v1/docs)) |
| **Judge login (read-only)** | Email `anticipa.judge@example.com` · Password `AnticipaJudge2026` |
| **Or** | **Continue with Google** on the sign-in page (also read-only) |

The judge account and Google sign-in are both the **observer** role: you can browse every dashboard, open any synthetic resident's journey, read verified quotes and the rule explanation behind every decision, and use **Ask Anticipa**. You cannot change data, approve rules, submit jobs or trigger paid AI work. The judge account is shared and public on purpose, so do not type real personal information into it.

**Cold start.** The API runs on Render's free plan. After a quiet spell it sleeps and the first request can take up to a minute. An uptime monitor checks it every 5 minutes to keep it awake, but if the first screen is slow, wait a moment and refresh.

**What the hosted app contains.** The hosted judging environment is a Supabase free-plan database with a stratified subset of about 1,100 synthetic residents. The full 200,000-resident run lives on the development laptop, and every number in this README says which dataset produced it. Counts you see in the hosted app can therefore differ from the figures below.

**A five-minute path.**

1. Sign in and open **Command Center**. Pick a state on the India map, then a district.
2. Open **Residents**, pick one and open their **journey**. Follow READ, DECIDE, PLAN, ACT, CLOSE for that one person, open the family pedigree and click **Show quote in note**.
3. Open **Evidence and guidelines**. Read a Hindi or Arabic note, see each extracted fact with its verified quote, and see which rule matched and why.
4. Open **Care planning** and read why cases were left out of the latest plan, one reason code each.
5. Click the robot at the bottom right to open **Ask Anticipa** and ask something like "Which district has the lowest closure rate?". Open **How I got this**.
6. Open **Guideline Studio** and see how an approved version is locked and how an editable copy can be dry-run to show who would become due before anyone approves it. Running a dry run needs a staff role, so the read-only login can look but not run.

### Film and walkthrough

**▶ Cinematic film (3 min):** [Watch First Light, the story of Anticipa in three minutes](https://github.com/vishavmehra/Anticipa/releases/download/film-v1/anticipa-film.mp4)

**▶ Product walkthrough (31 min):** [Watch the full narrated tour of every dashboard](https://github.com/vishavmehra/Anticipa/releases/download/walkthrough-v1/anticipa-walkthrough.mp4) · [Chapters](walkthrough/README.md)

---

## Contents

- [Why we built it](#why-we-built-it)
- [At a glance: problem, safety, quality, impact](#at-a-glance-problem-safety-quality-impact)
- [What Anticipa does](#what-anticipa-does)
- [Where the agents are](#where-the-agents-are)
- [Proven impact](#proven-impact)
- [Evaluation](#evaluation)
- [Safety and trust principles](#safety-and-trust-principles)
- [Architecture](#architecture)
- [Screenshots](#screenshots)
- [APIs and services we used](#apis-and-services-we-used)
- [Tech stack](#tech-stack)
- [API overview](#api-overview)
- [Judge access and roles](#judge-access-and-roles)
- [Set it up locally](#set-it-up-locally)
- [Environment variables](#environment-variables)
- [Cost and scale facts](#cost-and-scale-facts)
- [Disclosures and limits](#disclosures-and-limits)
- [Roadmap](#roadmap)
- [Repository layout](#repository-layout)
- [License](#license) · [Acknowledgements](#acknowledgements)

---

## Why we built it

Health systems are not short of data. They are short of a way to turn it into prevention. A district hospital, a state programme or an insurer already holds diagnoses, lab results, medicines, discharge letters and family history. What it rarely has is one place that connects all of it, decides who is due for what, checks that the clinics can actually see those people, and then confirms the care happened.

The size and the stakes of the problem are well documented:

- **The burden is global and growing.** Noncommunicable diseases killed at least **43 million people in 2021, 75% of non-pandemic-related deaths** worldwide, and **18 million of them died before age 70, 82% of those premature deaths in low and middle income countries**. WHO notes that high-impact interventions can be delivered through primary care to strengthen early detection and timely treatment. Source: [WHO fact sheet on noncommunicable diseases](https://www.who.int/news-room/fact-sheets/detail/noncommunicable-diseases).
- **In India the risk factors are already common.** The ICMR-INDIAB national study of **113,043 adults** found weighted prevalence of **diabetes 11.4%, prediabetes 15.3% and hypertension 35.5%**. Source: Anjana RM et al., [The Lancet Diabetes & Endocrinology 2023;11(7):474 to 489](https://doi.org/10.1016/S2213-8587(23)00119-5), ICMR-INDIAB-17.
- **India has a national programme to find people early.** Under the National Programme for Prevention and Control of Non-Communicable Diseases (NP-NCD), population-based screening targets adults aged 30 and above for five common conditions: hypertension, diabetes, and cancers of the oral cavity, cervix and breast. Source: [MoHFW NP-NCD Operational Guidelines](https://www.mohfw.gov.in/sites/default/files/NP-NCD%20Operational%20Guidelines_0.pdf).

These figures show how big the field is. They are context, not results produced by Anticipa.

### What goes wrong between the data and the care

| Problem | What it looks like in practice | What Anticipa does about it |
|---|---|---|
| **Records are fragmented** | Structured codes say one thing, a free-text note says another, and family history lives in a letter or a conversation. | Reads notes in English, Hindi and Arabic, extracts facts with an exact supporting quote, and joins them to the structured record. |
| **Preventive gaps stay invisible** | A resident whose mother had breast cancer at 46 looks "average risk" if only codes are read. | In our 10,000-resident evaluation cohort, reading notes surfaced **1,858 family-history care gaps that structured records alone found none of**. |
| **Outreach ignores capacity** | Invitations go out first come first served, and some districts are served far better than others. | An OR-Tools planner places cases into real clinic slots, with a fairness term across districts and languages. |
| **Nobody checks the care happened** | A reminder is sent and the case is silently marked done. | A case closes only with verified completion evidence, checked by a different person from the one who submitted it. |
| **AI is hard to trust in health** | A model that decides who is eligible cannot be audited. | AI reads and talks. Deterministic, versioned rules decide. Every fact, rule and action is traceable. |

Screening programmes are our worked examples, not the boundary of the product. Anticipa is a preventive-care intelligence and coordination platform: the rule engine, evidence layer, planner and closure loop work for any programme that can be written as a versioned, testable rule.

---

## At a glance: problem, safety, quality, impact

| Question | Where to look |
|---|---|
| **Problem size** | [Why we built it](#why-we-built-it) for the sourced burden. [Cost and scale facts](#cost-and-scale-facts): 200,000 residents, about 1.2 million rule evaluations, 27.1 million clinical records, 196,830 care-gap cases. |
| **Problem severity** | The same section, plus the safety design: [Safety and trust principles](#safety-and-trust-principles), where a wrong answer in health is treated as a hazard and not as a typo. |
| **Solution quality** | [What Anticipa does](#what-anticipa-does), [Architecture](#architecture) and [Where the agents are](#where-the-agents-are): a complete loop from reading to verified closure, with deterministic decisions and bounded agents. |
| **Proven impact** | [Proven impact](#proven-impact) and [Evaluation](#evaluation): measured numbers with the method beside each, a held-out test split run once, and the failed gates published with their causes. |

---

## What Anticipa does

```mermaid
flowchart LR
    R["READ<br/>records and notes"] --> D["DECIDE<br/>approved, versioned rules"]
    D --> P["PLAN<br/>capacity and equity"]
    P --> A["ACT<br/>outreach in EN, HI, AR"]
    A --> C["CLOSE THE LOOP<br/>verified completion evidence"]
    C -. "new evidence changes the next decision" .-> R
```

<p align="center">
  <img src="diagrams/03-end-to-end-flow.png" alt="End to end flow: read, decide, plan, act, close the loop, with the AI steps, deterministic steps, data stores and human review marked" width="100%">
</p>

### READ: turn records and notes into verified evidence

- **Data fabric.** Synthetic residents are generated with MITRE Synthea, normalised from FHIR R4 into PostgreSQL, and linked into families by a deterministic family linker that keeps the provenance of every relationship. Ground truth is generated separately and kept apart from the pipeline that is being tested.
- **Evidence engine.** Claude Haiku reads each clinical note and returns structured facts: the condition or procedure, who it is about (the resident, a relative, someone else), whether it is negated, how certain and how recent it is. Hard cases escalate to Claude Sonnet, capped at 10% of a run. Every fact must carry an **exact quote** from the note, and a deterministic matcher checks that quote against the source text with character offsets. A fact whose quote cannot be verified is dropped or sent to human review.
- **Languages.** English, Hindi and Arabic notes, including Arabic right to left, with deliberate hard cases: negated family history, the wrong relative, wrong ages, past screenings, conflicting statements and ambiguous wording.
- **Search.** PostgreSQL full-text search by default. Semantic search with pgvector and Qwen3-Embedding-8B through Nebius Token Factory is opt-in, behind an explicit spend approval.

<p align="center">
  <img src="diagrams/04-synthetic-data-pipeline.png" alt="Synthetic data pipeline: Synthea generation, FHIR normalisation, bulk load, family linking, document generation with planted facts and isolated ground truth" width="100%">
</p>

<p align="center">
  <img src="diagrams/05-evidence-extraction.png" alt="Evidence extraction: consent gate, untrusted-data fence, model router, quote verification, acceptance policy, review queue and capped escalation to the stronger model" width="100%">
</p>

### DECIDE: rules decide, AI never does

- **Guideline engine.** Rules are a small, validated, versioned language. A pure function evaluates one resident against one approved guideline version and returns an outcome with a criterion-level explanation: eligible, not eligible, already completed, temporarily excluded, requires clinical review or insufficient evidence.
- **Programmes in the box.** Six guideline packs (colorectal average risk, colorectal family history, breast, cervical, BRCA risk assessment, BRCA genetic counselling referral, 69 rules, 181 regression cases) and an **India national programme pack** (hypertension, diabetes and oral cancer screening approved, breast examination and cervical VIA kept as drafts, 31 rules, 84 regression cases, encoded from the MoHFW operational guidelines).
- **Guideline Studio.** Edit a copy of a guideline and run a **dry run** that reads real records and saves nothing, to see who would become due before anyone approves the change. Approved versions are immutable.
- **Care-gap cases.** Every eligible or review outcome opens a case with a validated status lifecycle, an assignee, a deterministic priority (family-history and review cases first) and a full history.

<p align="center">
  <img src="diagrams/07-guideline-engine.png" alt="Guideline engine: draft, review and approve a version, then evaluate a resident with a pure function that returns an outcome and an explanation" width="100%">
</p>

<p align="center">
  <img src="diagrams/08-care-gap-case-lifecycle.png" alt="Care-gap case lifecycle: validated status transitions from identified to completed, closed not completed, escalated or cancelled" width="100%">
</p>

### PLAN: fit demand to real capacity, fairly

- **Capacity planner.** Google OR-Tools CP-SAT places cases into clinic service slots. Hard constraints (service match, open clinic, seat capacity, planning horizon, travel tier, consent) are checked by an independent validator, and a plan that breaks one is never saved.
- **Equity term.** The objective includes a fairness term across district and language groups.
- **Explainable gaps.** A case the plan cannot place gets a reason code and a plain explanation, for example capacity exhausted or outside travel eligibility.

<p align="center">
  <img src="diagrams/09-capacity-planning.png" alt="Capacity planning: cases and slots into the OR-Tools CP-SAT model with an equity term, an independent hard-constraint validator, allocations and unallocated reasons" width="100%">
</p>

### ACT: reach residents in their language

- **Outreach.** Approved message templates in English, Hindi and Arabic. Delivery, responses and appointments are **simulated and labelled as simulated**. Nobody is contacted.
- **Resident conversation agent.** A resident replies and gets help with logistics (when, where, what to bring, a proposed other slot). Any health question goes to a clinician. See [safety](#safety-and-trust-principles) for the layered design and its measured results.
- **Consent and opt-out.** An append-only consent ledger (outreach by channel, and AI processing). Planning, outreach and AI jobs all honour it, and a missing consent table fails closed.

### CLOSE: verify, do not assume

- A case closes as **completed** only with completion evidence that a different person has verified. The alternative is **closed, not completed**, with an authorised reason. 20 of 20 improper closure attempts were refused in evaluation.
- Every action writes to an append-only audit table that the database itself protects from update and delete.

### See it and ask about it

- **Command Center** with a tilted-glass **India map**: country, then state, then district, with a cross-filter across programmes.
- **Resident journey** and **family pedigree**: one payload from evidence to closure, with the quote behind every relative.
- **Ask Anticipa**: natural-language questions over verified aggregates. It returns an answer, a chart or table and a "How I got this" panel, and it refuses clinical questions and anything about identifiable people.
- **Programs and impact**, **Evaluation**, **Insights**, **Reports**, **Residents**, **Engagement**, **Operations**, plus a system section (jobs, audit, integrations, live monitor) for administrators.

---

## Where the agents are

"Agentic" here means models that read, converse and call tools inside strict limits. Agents propose and talk. Deterministic code decides and enforces.

| Agent or workflow | What it does | What bounds it |
|---|---|---|
| **Evidence extraction agent** | Reads notes and returns structured facts with quotes. Escalates hard cases from Haiku to Sonnet. | Schema-validated output, deterministic quote verification, review queue, 10% escalation cap, per-run spend cap. |
| **Ask Anticipa** | Answers population questions with a tool loop of up to 4 model calls over 9 read-only analytics tools. | Aggregates only, no free SQL, every number checked against tool output, refusal rules, rate limit, daily budget. |
| **Resident conversation agent** | Replies to a resident about an appointment in English, Hindi or Arabic. | A deterministic classifier runs first. Health matters, emergencies and eligibility disputes go to a person. A deterministic reply check blocks clinical language and booking claims. |
| **Capacity planner** | Solves an optimisation problem as a tool, then explains what it left out. | Independent hard-constraint validator, fixed seed, input snapshot hash. |
| **Loop-closure workflow** | Moves a case through states and asks for evidence. | Validated transitions, maker-checker on evidence, mandatory reasons. |

---

## Proven impact

Everything below was **measured on synthetic data with the product's own code**. Nothing is projected, and no outcome such as lives saved or money saved is claimed, because the project holds no cited benefit model for that.

### What reading notes changes (10,000-resident AI cohort, six guidelines, 60,000 resident and guideline pairs)

| Measured result | Number | How it was measured |
|---|---|---|
| Family-history care gaps found by reading notes | **1,858** among 10,000 residents (1,184 genetic counselling referrals, 363 BRCA risk assessments, 311 colorectal family-history cases). Structured records alone found **0**. | The same approved rules run twice in memory: structured records only, then structured records plus facts extracted from notes. |
| Care gaps that should be flagged, found | Structured records alone: **1,521 of 1,887 (81%)**. Reading notes: **1,872 (99%)**. | Compared with the planted truth of the 1,969-resident development split, all nine guidelines. |
| Repeat invitations stopped | **356** invitations that structured records would send were for screenings already done. Invitations that should not go out fell from **269 to 3** of 4,754 decisions. | Against the planted screening truth, three screening guidelines. |
| The honest trade | **1,995** invitations are held for review, 1,952 of them because a note mentions a screening that cannot be dated or confirmed. Against the screening truth, **73 of 111** overdue gaps wait for review instead of an invitation. | Same comparison. Caution costs speed, and we say so. |
| Rules reproduce the stored decisions | **60,000 of 60,000** | Recomputed outcome compared with the stored decision. |

### What optimised planning changes (200,000-resident run, 165,376 plannable cases)

| Measured result | First come first served | OR-Tools CP-SAT |
|---|---:|---:|
| Cases placed | 103,523 of 165,376 (62.6%) | **104,048 (62.9%)** |
| Gap between best and worst served district | 13.2 points | **0.6 points** |
| Language coverage gap | 0.45 points | **0.06 points** |
| Placed in the resident's own district | 79.7% | **79.8%** |
| Median wait | 6.5 days | **6.4 days** |
| Hard-constraint violations | 0 | 0 |

Seats run out in most screening services, so the optimiser mainly changes **who** gets a seat: the gain is fairness, with 525 more cases placed. Both plans were rebuilt from the same input snapshot (hash recorded).

### What the full loop does (2,000 cases sent through READ to CLOSE in simulation)

| Measured result | Number |
|---|---|
| Cases that reached each stage | READ 2,000, DECIDE 2,000, PLAN 2,000, ACT 2,000, CLOSE 950 |
| Completed, each with verified evidence | 758, and **0** completed without verified evidence |
| Closed, not completed | 192, and **0** without a reason |
| Slots over-reserved or over-allocated | 0 and 0 |
| Problems found by the end-to-end verifier | 0 |

After that verified sample, the whole plan went through the same simulated loop: **104,048** planned cases, **97,865** reminders delivered, **47,374** attended, **40,180** closed as completed with verified evidence and **9,761** closed as not completed with a reason. Across all **196,830** cases that is a **20.8% closure rate** (40,938 completed). Outreach, replies and attendance are simulated; every completion still passed the same evidence check.

### What the end-to-end run found and fixed

The incremental re-run exposed a real defect: cases opened before the re-run stayed open after the newest evaluation changed, so **7,057 cases would have been planned for residents who already had the screening**. We fixed it with a reconcile step that now runs at the end of every evaluation job, and added tests. We also recovered from a stalled database insert and a machine crash from checkpoints with no duplicate evaluations (1,200,000 rows exactly).

---

## Evaluation

The final evaluation ran **once** on a held-out test split (8,031 residents, 26,122 documents, 103,352 planted facts) after the development split was used for tuning. Ground truth is planted before the text is generated and stored apart from the pipeline. Of 37 gates, 31 were evaluated: **24 pass, 5 fail and 2 warn**, and **6 could not be evaluated**. We publish all of them. Run: `20261112T190718820889Z-a6af9874`, dataset `syn-20261108-s42`, code commit `eb62f42`.

### Strongest results

| What we guard against | Result on the held-out test |
|---|---|
| Facts without a verified source quote | 99.97% of 101,525 extracted facts have a verified quote. **None** of the 98,068 accepted facts lacks one. |
| A relative's condition attributed to the resident | **0 of 12,180** |
| A negation flipped (for example "no family history of cancer") | **0 of 2,430** |
| A planned procedure read as done | **0 of 337** |
| An uncertain statement read as certain | **0 of 1,526** |
| An injected instruction in a note followed | **0 of 263** |
| Who a statement is about, and whether it is negated | 99.99% and 99.99% |
| Concept-level extraction | F1 **0.936** (precision 0.964, recall 0.909) |
| Languages | Strict F1 close across English 0.791, Hindi 0.783, Arabic 0.780 |
| Rule engine regression | **181 of 181** authored cases pass, identical on repeat runs |
| Explainability | All **48,186** stored decisions carry a full explanation |
| Planner | No hard constraint broken on an independent check. All **34,636** unplanned cases have a valid reason. |
| Closure | No case closed without verified evidence. 20 of 20 improper closures refused. |
| End-to-end eligibility | Agrees with the planted truth on **92.5%** of 20,922 scored decisions (98.3% where age and sex decide). |

### Failed gates, in plain words

<details>
<summary><strong>Five gates fail. Here is why, and what the product does about it.</strong></summary>

The common cause is **timing**: the extractor says "current" where the ground truth says "historical", almost entirely for listed medications and past conditions. A medication list carries no date, so the historical label comes from the structured record and cannot be read from the note.

| Gate | Observed | Proposed bar | Why it failed | Mitigation in the product |
|---|---:|---|---|---|
| Strict precision of accepted facts | 0.811 | 0.95 | The concept is right for 96.4% of facts. Strict scoring loses facts whose timing differs (current versus historical). | Uncertain facts (3.4%) go to the human review queue. Every fact shows its verified quote. Most screening rules accept both current and historical for personal history. |
| Strict recall of facts shown to people | 0.778 | 0.90 | Detection is high (93.8% found at the right place) so the shortfall is timing again. By category: observations 98.6%, procedures 97.4%, family history 88.0%, conditions 55.6%, medications 42.0%. | Screening-relevant facts score far higher than medications. Uncertain facts are routed to review, not dropped. |
| Historical read as current | 22.3% | 1% | 85.5% of the errors are medications and 12.5% conditions. Family history 0.09%, preventive care 0.23%, procedures 0. | No screening rule reads medications. Only three selectors read timing. |
| Care gaps missed by the rules | 16.8% | 2% | 82 of 487 overdue residents were marked as not needing screening. 306 of the 487 went to review instead. Cause not fully traced. | Most were not lost: they were flagged for review, which keeps a person in the loop. |
| False certainty where review was expected | 30.0% | 2% | 306 of 1,020 got a firm answer. Most are the engine correctly applying other rules first (personal history, family-history deferral) that the expected outcome cannot see. | The gate partly measures the limits of the expected outcome. The engine lists the rules behind every decision. |

**Not evaluated** (and never reported as passed): reviewed hallucination rate (needs a person to check about 200 facts), keyword versus semantic search quality in three languages, label error rate (all 107,563 labels are machine made and unreviewed), planner behaviour on impossible scenarios, a cost ceiling and a latency target (no budget was set).

**Next fixes**, in order: agree one timing convention for undated listed items, apply it to the prompt and the labels, then re-score on the development split before any new test run.

</details>

### Other measured evaluations

| Evaluation | Result |
|---|---|
| **Ask Anticipa**, 25 live questions (20 answerable, 5 that must be refused) | Accuracy 20 of 20, every answer grounded 25 of 25, refusals correct 5 of 5. Median latency 2.9 s, p95 4.9 s. Cost **$0.00028 per question**. |
| **Resident conversation**, 71 red-team and logistics messages (37 English, 17 Hindi, 17 Arabic) | **71 of 71 passed.** Escalation recall on emergency, medical and eligibility messages **40 of 40**. Emergency messages **11 of 11** got the urgent reply. Of 19 model-written replies judged by a second model, **0** gave clinical advice, claimed a booking, followed an injection or invented a fact. Language matched 71 of 71. Total cost $0.006. |
| **Held-out stress set**, 38 harder messages | First scored 26 of 27 hand-offs and 8 of 9 emergencies on prompt version 1, a **failure** against the 100% gate (a message about taking too many tablets was escalated but did not get the emergency text). We fixed it and it now passes 38 of 38. **That set was used to tune the fix, so it is no longer an independent test**, and we say so. |
| **Test suite** | 1,566 backend tests passed in a module-by-module run, plus 181 and 84 rule regression cases. Frontend type check and lint clean. `npm audit` and `pip-audit` report no known vulnerabilities. |
| **Security and QA review** | 22 findings (1 high, 5 medium, 11 low, 5 info). 8 fixed, the rest recorded as open or owner decisions. See [Disclosures and limits](#disclosures-and-limits). |

To reproduce, see [Run the evaluation](#run-the-evaluation).

---

## Safety and trust principles

1. **AI reads and talks. Deterministic code decides eligibility.** No model output can make a resident eligible or ineligible.
2. **Every extracted fact has a verifiable source.** An exact quote, offsets, and a deterministic check against the note. No verified quote, no accepted fact.
3. **Every decision names its guideline, its rule version and its reasons.** A decision can be replayed. Approved versions are immutable.
4. **Subject and negation are first-class.** The system separates the resident, relatives and other people, and positive, negated, uncertain and historical statements.
5. **Human review for uncertainty and high impact.** Uncertain facts, clinical-review outcomes and every health question reach a person.
6. **Consent is enforced, not decorated.** Opt-outs stop planning, outreach and AI processing. Emergency replies are still sent on a denied channel as an audited safety override.
7. **Closure needs proof.** Verified evidence, checked by someone other than the submitter.
8. **Everything is audited.** An append-only audit trail with database-level protection and row-level security on every table.
9. **Documents are untrusted input.** Notes are treated as data. Prompt injection inside a note is part of the test set (0 of 263 followed).
10. **Synthetic data is labelled as synthetic.** Simulated messages, bookings and outcomes say so on screen.

<details>
<summary><strong>The resident conversation agent, layer by layer</strong></summary>

1. A **deterministic classifier** (English, Hindi and Arabic keyword lists, with Arabic letter folding and Hindi nukta folding) runs before any model call and routes: emergency, medical, eligibility or record dispute, opt-out, prompt injection, otherwise logistics. The first five get a fixed, approved reply per language and a clinician hand-off.
2. **Logistics only** goes to the model through the router. The model sees the case's screening label, its appointment or planned slot, up to three open alternative slots and approved preparation text, as untrusted data. It never books anything. A proposed slot is stored as a suggestion for a coordinator.
3. A **deterministic reply check** runs on every model reply: no clinical language in any of the three languages, no claim that anything was booked or changed, every number must come from the case facts, correct script, no links or phone numbers, approved length. A failed check falls back to the approved text plus an escalation.
4. **Consent** is checked before anything is written. A provider outage returns an approved "could not prepare a reply" text, never an invented answer.

Everything is simulated, and every message is marked simulated at the database level.

</details>

---

## Architecture

Anticipa is a **modular monolith** with a separate background worker, not a microservice mesh. That was a deliberate choice for a team of this size: simple to run, easy to reason about, and each module has a clear boundary that could be split out later.

<p align="center">
  <img src="diagrams/01-system-context.png" alt="Anticipa system context: users, the web app, the API, the database, AI providers and data standards" width="100%">
</p>

<p align="center">
  <img src="diagrams/02-container-architecture.png" alt="Anticipa container architecture: Next.js on Vercel, FastAPI and embedded worker on Render, Supabase Auth and Postgres, Claude and Nebius through the model router" width="100%">
</p>

All diagrams, as editable draw.io sources (`diagrams/src/`) plus SVG and PNG exports, are in [`diagrams/`](diagrams/). The ones for each part are embedded in the sections above and below.

<details>
<summary><b>Diagram gallery: engagement, closure, consent, Ask Anticipa, caching, auth, deployment, FHIR, evaluation, geography and security</b></summary>

<p align="center"><img src="diagrams/10-engagement-and-conversation-safety.png" alt="Engagement and resident conversation safety: consent gate, keyword pre-classifier, model reply, deterministic post-check, escalation and emergency override" width="100%"></p>
<p align="center"><img src="diagrams/11-loop-closure.png" alt="Loop closure: completion evidence submitted, verified by a different person, then the case closes" width="100%"></p>
<p align="center"><img src="diagrams/12-consent-enforcement.png" alt="Consent enforcement points across planning, outreach and AI processing" width="100%"></p>
<p align="center"><img src="diagrams/13-ask-anticipa.png" alt="Ask Anticipa grounding loop: read-only tools, server-built charts, numeric grounding check and refusals" width="100%"></p>
<p align="center"><img src="diagrams/14-analytics-snapshot-and-caching.png" alt="Analytics snapshot refresh after jobs, HTTP caching with ETag and client stale-while-revalidate" width="100%"></p>
<p align="center"><img src="diagrams/15-auth-and-rbac.png" alt="Authentication and role based access: Supabase sign-in, roles, read-only observer and Google sign-in" width="100%"></p>
<p align="center"><img src="diagrams/16-deployment-topology.png" alt="Deployment topology: Vercel web app, Render API, Supabase, local Docker stack and the GitHub publish snapshot" width="100%"></p>
<p align="center"><img src="diagrams/17-fhir-mapping-and-write-back.png" alt="FHIR R4 mapping and write-back resources loaded into HAPI FHIR" width="100%"></p>
<p align="center"><img src="diagrams/18-evaluation-harness.png" alt="Evaluation harness: dev and test split, single final run, gates and isolation from pipeline code" width="100%"></p>
<p align="center"><img src="diagrams/19-india-geography-drilldown.png" alt="India geography drill-down from country to state to pilot districts" width="100%"></p>
<p align="center"><img src="diagrams/20-security-layers.png" alt="Security layers from the browser to the database" width="100%"></p>

</details>

```mermaid
flowchart TB
    subgraph Client
      UI["Next.js 16 app<br/>Vercel"]
    end
    subgraph API["FastAPI modular monolith (Render)"]
      GW["Auth, RBAC, audit, envelope"]
      subgraph Modules
        DF["Data fabric"]
        EV["Evidence engine"]
        GE["Guideline engine"]
        CG["Care-gap engine"]
        PL["Capacity planner"]
        EN["Engagement and conversation"]
        CL["Loop closure"]
        AN["Analytics and Ask"]
      end
      MR["Model router"]
      JQ["PostgreSQL job queue and worker"]
    end
    UI -->|"/api/v1 bearer JWT"| GW
    GW --> Modules
    EV --> MR
    AN --> MR
    EN --> MR
    Modules --> JQ
    MR --> CLAUDE["Anthropic Claude"]
    MR -. "opt-in" .-> NEB["Nebius Token Factory"]
    Modules --> PG[("PostgreSQL 17 + pgvector<br/>Supabase in the cloud")]
    GW -. "JWKS verify" .-> SA["Supabase Auth"]
    DF --> SYN["Synthea"]
    DF -. "optional loader" .-> HAPI["HAPI FHIR R4"]
```

### The model router

Every model call in the system goes through one provider-independent router. No module talks to a provider directly.

<p align="center">
  <img src="diagrams/06-model-router.png" alt="Model router: task-based routing, structured output validation, retries, budgets, accounting, and the Claude and Nebius provider adapters" width="100%">
</p>

- **Task-based routing** from `config/models.toml`. A model ID is checked at startup against a live-verified provider catalogue, and Meta models are rejected.
- **Structured output validation** with one repair turn, then fallback. A refusal never falls back.
- **Retries, timeouts, rate-limit handling** with back-off that honours `retry-after`.
- **Cost, token and latency accounting** for every call, written to `model_invocations` with prompt version, validation status and retry count. Prompt text is never stored.
- **Budgets and caps**: a per-run and a daily spend cap. Over a cap, the model is skipped and the deterministic layer still works.
- **Policy**: bulk extraction is Haiku only, escalation to Sonnet is capped, and **Nebius is off unless explicitly enabled**. There is no silent fallback from Claude to Nebius.

### Data model in one paragraph

Residents, clinical records and documents feed **evidence facts** (each with sources and quotes). **Guideline versions** hold **rule versions**. An **eligibility evaluation** joins one resident to one rule version with its explanation. A **care-gap case** points at an evaluation, moves through a validated status history and owns **allocations**, **appointments**, **communication events**, **conversation messages** and **completion evidence**. **Consent records** and **audit events** are append-only. Ground truth lives in a separate store.

### Reproducible and resumable

Seeds are recorded, runs are checkpointed, and a re-run touches only changed residents. Generation, ingestion, evaluation and planning can all be interrupted and resumed. Each planning run stores an input snapshot hash.

---

## Screenshots

Captured from the running application by automated Playwright runs. Images 1 to 6 are from the live build. Images 7 and 8 are automated test renders that use fixture data, so their totals differ from the live demo.

| | |
|---|---|
| ![Command Center with the India map](walkthrough/img/command-center.png) | ![Population health intelligence](walkthrough/img/population.png) |
| **1. Command Center.** KPIs, programme completion and the tilted-glass India map. The 0.7% closure rate is real: only 2,000 cases were simulated through closure. | **2. Population.** 200,000 synthetic residents, 27.1 million clinical records, 434,851 clinical documents and 120,360 residents in linked families. |
| ![The preventive care journey](walkthrough/img/care-journey.png) | ![Evidence and guidelines with a Hindi note](walkthrough/img/evidence-review.png) |
| **3. Care journey.** The funnel from identification to completion, with cohorts and bottlenecks. | **4. Evidence and guidelines.** A Hindi family-history note, each fact with "Quote verified (exact)", and the rule-by-rule reasoning that led to "Not eligible". |
| ![Guideline Studio](walkthrough/img/guideline-studio.png) | ![Care planning studio](walkthrough/img/care-planning.png) |
| **5. Guideline Studio.** Approved versions are locked. Make an editable copy and dry-run it before anyone approves. | **6. Care planning studio.** 104,048 of 165,376 cases placed, with the reason each other case was left out. |
| ![Ask Anticipa](walkthrough/img/ask-anticipa.png) | ![Resident journey with pedigree](walkthrough/img/resident-pedigree.png) |
| **7. Ask Anticipa** (test render, fixture data). Answers carry a table, a "How I got this" panel and the model and latency used. | **8. Resident journey and pedigree** (test render, fixture data). The stored relatives, who has cancer history, the quote from the note, and the closure rule. |

---

## APIs and services we used

| Service | What we use it for | How |
|---|---|---|
| **Anthropic Claude API** (Haiku 5.5, Sonnet 5.5) | Note extraction (Haiku, with Sonnet escalation), Ask Anticipa, the resident conversation agent and its judge. An Opus route exists for assistive evidence review. | Through the model router, with prompt caching, structured output and spend caps. Provider keys stay server-side. |
| **Nebius Token Factory** | Embeddings with `Qwen/Qwen3-Embedding-8B`, stored at 1,024 dimensions, for multilingual semantic retrieval. | OpenAI-compatible endpoint, **opt-in** behind `ANTICIPA_ENABLE_NEBIUS=1` and an approved cost estimate. Default search is PostgreSQL full text. |
| **Supabase** | Auth (email and Google), hosted PostgreSQL for the judging environment, Storage for documents. | JWT verified through JWKS (ES256 and RS256). Roles come only from `app_metadata`. Data API exposure is blocked, row-level security is on, and the browser only ever holds a publishable key. |
| **Render** | Hosts the FastAPI API with the embedded job worker (free plan, Singapore). | Docker image with a health check, and a one-off `anticipa migrate` on first deploy. |
| **Vercel** | Hosts the Next.js web app. | `/api/*` rewrite to the Render API, so the browser sees one origin and the API needs no CORS. |
| **HAPI FHIR R4** | A local FHIR R4 server in the Docker stack. A loader maps the judge subset and Anticipa's write-back resources (Task, Appointment, Communication, DetectedIssue, Flag, Provenance, Consent) to transaction bundles. | `anticipa datafabric hapi-load` with `--dry-run` and `--verify-only`. Idempotent `PUT` with stable ids. |
| **MITRE Synthea 4.0.0** | Synthetic patient histories and FHIR resources. | Portable JRE and jar bootstrapped by `scripts/bootstrap_synthea.ps1`. Batches are seeded, resumable and validated. |
| **Google OR-Tools (CP-SAT)** | The capacity planner. | Grouped-equity model, fixed seed, independent validator. |
| **DataMeet maps (CC BY)** | State and district outlines for the India map. | Built into TopoJSON by `frontend/scripts/build-india-geo.mjs` and drawn with d3-geo. |
| **Clinical guidance** | The rule packs. | USPSTF and USMSTF recommendations for the six core guidelines. MoHFW NP-NCD operational guidelines (2016 and 2023 to 2030) for the India pack. |

---

## Tech stack

| Layer | Technology |
|---|---|
| Frontend | Next.js 16 (App Router), React 19, TypeScript, Tailwind CSS 4, d3-geo and TopoJSON, Playwright and axe-core for tests |
| Backend | Python 3.12, FastAPI, Pydantic v2, SQLAlchemy 2, Alembic, psycopg 3, httpx |
| Data | PostgreSQL 17 with pgvector 0.8.7, FHIR R4, HAPI FHIR v8.12.0 |
| AI | Anthropic SDK, a provider-independent model router, Nebius Token Factory (opt-in) |
| Optimisation | Google OR-Tools CP-SAT |
| Auth | Supabase Auth, JWKS-verified JWTs, role-based access control |
| Jobs | PostgreSQL-backed queue using `FOR UPDATE SKIP LOCKED`, an embedded or standalone worker, checkpointed and resumable |
| Infra | Docker Compose locally, Render (API), Vercel (web), Supabase (auth and database) |
| Quality | pytest, Ruff, mypy, ESLint, `tsc --noEmit`, Playwright, `pip-audit`, `npm audit` |

---

## API overview

The API has **113 routes** under `/api/v1`. Responses use a `{data, meta}` envelope and errors use one error shape with a request id. Every request carries `Authorization: Bearer <Supabase JWT>`, and the web app gates actions with the `permissions[]` returned by `GET /auth/me`. Interactive docs are at [`/api/v1/docs`](https://anticipa-api-osxv.onrender.com/api/v1/docs).

| Group | What it covers |
|---|---|
| `health`, `auth`, `integrations` | Liveness and readiness, the signed-in user and permissions, integration health |
| `residents` | Residents, family, clinical records, timeline, evidence, evaluations, journey, pedigree, profile, consents |
| `evidence` | Evidence list and review queue, AI cohort, extraction and embedding jobs, search |
| `guidelines`, `rules`, `evaluations` | Guideline versions and lifecycle, rule validation, Studio dry runs, population evaluation jobs, eligibility decisions |
| `care-gaps`, `closure` | Cases, assignment, escalation, status, completion evidence, closure |
| `planning`, `appointments` | Clinics, capacity slots, planning runs, allocations and unallocated reasons, confirm and release, appointments |
| `engagement`, `conversation` | Drafts, simulated events and responses, simulations, the resident conversation thread |
| `analytics`, `ask` | Command Center, funnel, geography, programmes, population explorer, impact, evaluation, processing monitor, Ask Anticipa |
| `jobs`, `audit` | Background job status, progress, retry and cancel, the audit trail |

<details>
<summary><strong>Key endpoints</strong></summary>

| Method and path | Purpose |
|---|---|
| `GET /api/v1/auth/me` | The signed-in user, role and permissions |
| `GET /api/v1/analytics/command-center` | KPIs, priority, closure trend, equity, alerts |
| `GET /api/v1/analytics/geography?level=country\|state\|district&code=` | India map measures per state or district |
| `GET /api/v1/analytics/impact` | The stored proven-impact baselines |
| `GET /api/v1/analytics/evaluation` | The final evaluation run with gates |
| `POST /api/v1/ask` | Grounded natural-language question over aggregates |
| `GET /api/v1/residents/{id}/journey` | Evidence to closure for one resident in one payload |
| `GET /api/v1/residents/{id}/pedigree` | Stored relatives and their history with sources |
| `GET /api/v1/evidence?validation_status=requires_review` | The human review queue |
| `POST /api/v1/evidence/extraction-jobs` | Queue extraction on the AI cohort (supports `dry_run` cost estimate) |
| `POST /api/v1/guidelines/{id}/versions/{vid}/dry-run` | Guideline Studio dry run, saves nothing |
| `POST /api/v1/guidelines/{id}/versions/{vid}/approve` | Approve a version (clinical reviewer) |
| `POST /api/v1/rules/evaluation-jobs` | Population evaluation with approved rules only |
| `POST /api/v1/planning/runs` | Submit a capacity planning run |
| `GET /api/v1/planning/runs/{id}/unallocated` | Cases the plan could not place, with reasons |
| `POST /api/v1/planning/allocations/{id}/confirm` | Confirm an allocation, reserve a seat, hold a simulated appointment |
| `POST /api/v1/care-gaps/{id}/conversation/messages` | Simulated resident message and the agent's reply |
| `POST /api/v1/care-gaps/{id}/completion-evidence` | Submit completion evidence |
| `POST /api/v1/care-gaps/{id}/close` | Close a case (verified evidence or an authorised exception) |
| `POST /api/v1/residents/{id}/consents` | Record a consent decision (append-only) |
| `GET /api/v1/audit` | Audit events, newest first |

</details>

---

## Judge access and roles

| Role | Can do | Used for |
|---|---|---|
| **observer** | Read-only across dashboards, residents, evidence, guidelines, cases, plans and closure history. Can use Ask Anticipa. Cannot write, approve, submit jobs or trigger other AI calls. | **The judge login and Continue with Google** |
| **care_coordinator** | Work and assign cases, escalate, record consent, type simulated resident messages, submit completion evidence. Scoped to assigned cases. | Staff role in the video |
| **clinical_reviewer** | Review the evidence queue, approve guideline versions, verify completion evidence. | Staff role in the video |
| **population_admin** | Manage capacity and planning, case management, run processing. | Staff role in the video |
| **system_admin** | Jobs, audit, integrations, configuration and the live monitor. | Staff role in the video |

A person who signs up with an email of their own gets no role, and the API returns 403 on every route. Staff accounts exist for the walkthrough video. **Their credentials are not published.** The judge login and Google sign-in are all you need.

---

## Set it up locally

You can run everything on a laptop. The one external requirement is a free **Supabase** project for sign-in. AI features need an Anthropic API key and are optional for browsing.

**Prerequisites:** Docker, Python 3.12 with [uv](https://docs.astral.sh/uv/), Node.js 20.9 or newer, Git. The Synthea bootstrap script is PowerShell. On macOS or Linux download the Synthea jar and a Java 21 runtime yourself.

### 1. Clone and configure

```bash
git clone https://github.com/vishavmehra/anticipa.git
cd anticipa
cp .env.example .env              # then fill in the values listed in "Environment variables"
cp frontend/.env.example frontend/.env.local
```

In `.env`, set `POSTGRES_PORT=5433` and point `DATABASE_URL` at `localhost:5433`, so a local PostgreSQL on 5432 does not clash. The compose file reads the same variable.

### 2. Start PostgreSQL (and optionally HAPI FHIR)

```bash
docker compose up -d postgres          # PostgreSQL 17 + pgvector on 127.0.0.1:5433
docker compose up -d hapi              # optional: HAPI FHIR R4 on 127.0.0.1:8090
```

### 3. Backend

```bash
cd backend
uv sync
uv run anticipa migrate                # alembic upgrade head
uv run anticipa seed-users             # creates one account per role plus a read-only judge observer in Supabase Auth
ANTICIPA_EMBEDDED_WORKER=1 uv run uvicorn anticipa.main:app --port 8000
```

`seed-users` needs `SUPABASE_URL`, `SUPABASE_SECRET_KEY`, and the passwords you choose in `ANTICIPA_DEMO_PASSWORD` and `ANTICIPA_JUDGE_PASSWORD`. For a separate worker process, run `uv run anticipa worker` instead of setting the embedded flag.

### 4. Frontend

```bash
cd frontend
npm install
npm run dev                            # http://localhost:3000, /api is rewritten to ANTICIPA_API_ORIGIN
```

Sign in with an account you seeded. The API's OpenAPI document is at `http://localhost:8000/api/v1/openapi.json`, and `npm run gen:types` regenerates the typed client.

### 5. Build a synthetic population (optional, for the full pipeline)

```bash
./scripts/bootstrap_synthea.ps1        # PowerShell: portable JRE 21 + Synthea 4.0.0 into tools/

# generate, load and family-link 10,000 residents (resumable: re-run the same command to continue)
uv run --directory backend python -m anticipa.datafabric.pipeline run \
  --residents 10000 --batch-size 500 --base-seed 42 --workers 4 --dataset-version syn-demo-s42

# plant facts and write multilingual notes plus ground truth
uv run --directory backend python -m anticipa.datafabric.docgen generate \
  --dataset-version syn-demo-s42 --residents 10000 --seed 42

# import the guideline packs as drafts, then approve them in the app as a clinical_reviewer
uv run --directory backend python -m anticipa.guidelines import-drafts
uv run --directory backend python -m anticipa.guidelines import-drafts config/guidelines/rules_india_draft.json

# evaluate every resident with approved rules and open care-gap cases
uv run --directory backend python -m anticipa.guidelines evaluate --dataset syn-demo-s42 --create-cases

# capacity planning (each command has --help)
uv run --directory backend python -m anticipa.planning generate-capacity
uv run --directory backend python -m anticipa.planning run

uv run --directory backend python -m anticipa.analytics refresh   # refresh the precomputed dashboard aggregates
```

Evidence extraction spends real money. Use the API's `dry_run` first: `POST /api/v1/evidence/cohorts` to fix the cohort, then `POST /api/v1/evidence/extraction-jobs` with `"dry_run": true` to see the scope and cost estimate before queuing it. Approval of guidelines is a human action by design, so the commands above never approve anything.

### Run the tests

```bash
uv run --directory backend pytest                 # integration tests need the Docker services; paid provider tests are opt-in with -m live
cd frontend && npm run typecheck && npm run lint
cd frontend && npm run test:e2e                   # Playwright, against a running web app and API
```

### Run the evaluation

```bash
uv run --directory backend python -m anticipa.evaluation run --dataset-version syn-demo-s42 --split dev   # the evaluator makes no model calls
uv run --directory backend python -m anticipa.evaluation impact                                           # rules-only and first-come baselines
uv run --directory backend python -m anticipa.ask estimate                                                # cost per Ask question, no model call
uv run --directory backend python -m anticipa.conversation dryrun                                         # what the deterministic layer catches, no model call
```

`ask eval` and `conversation eval` call the live model with a hard run budget.

### Deploy

The API image builds from the repository root: `docker build -f backend/Dockerfile -t anticipa-api .`. Set `ANTICIPA_EMBEDDED_WORKER=1` for single-service hosting and run `anticipa migrate` on first deploy. On Vercel, set `ANTICIPA_API_ORIGIN` to the API URL.

---

## Environment variables

Names only. Copy `.env.example` to `.env` and fill in values. Never commit `.env`.

<details>
<summary><strong>Backend (<code>.env</code>)</strong></summary>

| Variable | Purpose |
|---|---|
| `ANTHROPIC_API_KEY` | Claude. Needed for extraction, Ask Anticipa and the conversation agent. |
| `NEBIUS_API_KEY`, `NEBIUS_BASE_URL` | Nebius Token Factory, used only when `ANTICIPA_ENABLE_NEBIUS=1`. |
| `ANTICIPA_ENABLE_NEBIUS` | Opt-in switch for any Nebius call, embeddings included. |
| `POSTGRES_USER`, `POSTGRES_PASSWORD`, `POSTGRES_DB`, `POSTGRES_PORT` | Local Docker PostgreSQL. |
| `DATABASE_URL`, `MIGRATION_DATABASE_URL` | Runtime database and, optionally, a different migration target. |
| `SUPABASE_URL`, `SUPABASE_PUBLISHABLE_KEY` | Auth verification and sign-in. |
| `SUPABASE_SECRET_KEY`, `SUPABASE_DB_URL` | Server-side only: seeding users and cloud migrations. |
| `ANTICIPA_ENV` | Environment name. |
| `ANTICIPA_EMBEDDED_WORKER` | Run the job worker inside the API process. |
| `ANTICIPA_DEMO_PASSWORD`, `ANTICIPA_JUDGE_PASSWORD` | Passwords applied by `seed-users`. |
| `ANTICIPA_GOOGLE_DEFAULT_ROLE` | Role for Google sign-in with no explicit role. Only `observer` is accepted. |
| `ANTICIPA_DOCUMENT_STORE` | Where documents live: database or Supabase Storage. |
| `ANTICIPA_AI_DAILY_BUDGET_USD`, `ANTICIPA_AI_RUN_BUDGET_USD` | Spend caps for AI jobs. |
| `ANTICIPA_ASK_DAILY_BUDGET_USD`, `ANTICIPA_ASK_RATE_PER_HOUR`, `ANTICIPA_ASK_RATE_PER_DAY` | Ask Anticipa limits. |
| `ANTICIPA_CONVERSE_DAILY_BUDGET_USD`, `ANTICIPA_CONVERSE_RATE_PER_HOUR` | Conversation agent limits. |
| `HAPI_FHIR_BASE_URL` | Local HAPI FHIR server. |
| `RENDER_API_KEY` | Deployment automation only. |

</details>

<details>
<summary><strong>Frontend (<code>frontend/.env.local</code>)</strong></summary>

| Variable | Purpose |
|---|---|
| `NEXT_PUBLIC_SUPABASE_URL` | Supabase project URL. |
| `NEXT_PUBLIC_SUPABASE_PUBLISHABLE_KEY` | Public key only. Never put a secret key here. |
| `ANTICIPA_API_ORIGIN` | Destination of the `/api/*` rewrite to FastAPI. |

</details>

---

## Cost and scale facts

| Fact | Number | Note |
|---|---|---|
| Synthetic residents | **200,000** | Dataset `syn-20261108-s42`. **Reduced from a 1,000,000 target.** We do not claim a million. |
| Rule evaluations | **about 1.2 million** (1,211,981) | 200,000 residents against six guidelines, plus an incremental re-run. Stored as 5.0 GB across 1.2 million rows. |
| Population run time | about 58 minutes, 57.8 residents per second overall | Wall clock on one laptop, measured. We do not claim a faster figure. |
| Clinical records and documents | 27.1 million records, 434,851 documents | As shown on the Population page for this dataset. |
| Care-gap cases | **196,830** in total: 116,472 from the six core guidelines (71,593 residents with at least one) plus 80,358 from the India pack for the first 100,000 full-profile residents | Measured on the 200,000-resident run. |
| Capacity | 168,045 usable seats across 167 service pools, 10 pilot districts | Simulated clinic capacity. |
| AI extraction cohort | 10,000 residents, 32,523 notes, 126,371 facts | Fixed cohort, doubles as the evaluation cohort. |
| **AI cost for that cohort** | **$26.89 total, $0.83 per 1,000 notes** | Includes 650 Sonnet escalations. Estimated from configured prices and **not reconciled with a provider bill**. |
| Extraction latency | median 3.4 s, p95 6.8 s per call | Runs as a background job, so it does not block a user. |
| Ask Anticipa | $0.00028 per question | 25 live questions, with prompt-cache hits. |
| Resident conversation | $0.000137 per model reply | Deterministic replies make no model call. |
| Accepted facts | 122,058 accepted, 4,280 sent to review, 33 rejected | Across the cohort. |

Extrapolation, not a measurement: at the measured rate, a one-million-resident rules run would take roughly 4.8 hours. That has not been run.

---

## Disclosures and limits

We would rather tell you than have you find out.

- **All data is synthetic.** Residents, notes, family links, clinics, capacity, appointments, messages and outcomes are generated or simulated. The ten pilot districts (Bhopal, Indore, Jabalpur, Gwalior, Pune, Nagpur, Lucknow, Varanasi, Bengaluru Urban, Mysuru) were chosen for the demonstration. The figures describe synthetic residents, not the health of any real district.
- **The rules are not clinically reviewed.** They are encoded from published guidance (USPSTF, USMSTF and MoHFW) and approved in the system by the project owner. Clinical review status remains **pending** in the database, API and audit trail. They are demonstration rules and must be reviewed by qualified clinicians before any real use. The approved message texts and keyword lists have not had clinical review either.
- **This is not a medical device,** it is not a diagnostic system, and it makes no clinical decisions.
- **No clinical validation.** Generated notes are cleaner and more regular than real documentation, so these results do not transfer to real records. The ground-truth labels are machine made and unreviewed, and the extraction prompt and the note templates come from the same project.
- **Evaluation gaps.** Five gates fail and six were not evaluated, as described above. Gate thresholds are proposals, set before any measurement.
- **Simulated engagement.** No message was sent to anyone. Delivery failures, responses and outcomes are drawn from a seeded simulation. "Median time to attend" is not shown because it would be near zero by construction.
- **Capacity and travel are synthetic.** District distance is a ring, not real travel time. Every case in the baseline run has the same priority, so priority-weighted coverage could not be measured there.
- **Cost figures are estimates** from configured prices, not a provider bill.
- **Terminology services, national health stacks and real messaging are not implemented.** See [APIs and services we used](#apis-and-services-we-used). The FHIR Consent mapping is a shape only and has not been validated against HAPI.
- **Map boundaries.** Map data: DataMeet community, CC BY, with the 2019 reorganisation and the full territory of Jammu and Kashmir and Ladakh as in the Government of India map. **Boundaries are for orientation, not a survey. Check the Survey of India for official boundaries.**
- **Known security limitations.** A security and QA review found 22 items: 8 fixed (security headers, CSV formula injection, URL scheme validation, a publish-time secret scan and others) and the rest open or owner decisions. The main ones: rate limits are per process and in memory, the Supabase browser client writes JavaScript-readable session cookies, API database connections have no statement timeout, 403 denials reach the application log but not the audit table, and the same clinical reviewer can submit and approve a guideline version. The review verified authentication and authorisation on all 113 routes, parameterised SQL, no stored XSS paths, no secret in any commit, and zero known vulnerable dependencies. It did **not** verify cloud-side Supabase settings or run dynamic penetration tests.
- **Hosted limits.** The hosted environment runs on free plans, holds only a subset of residents, and may be slow after sleeping.

---

## Roadmap

Honest next steps, in the order we would take them.

1. **Clinical review** of every rule pack by licensed clinicians, then a maker-checker on guideline approval.
2. **Fix the timing gap.** Agree one convention for undated listed medications and past conditions, apply it to the prompt and labels, and re-score on the development split before any new held-out run.
3. **Human-reviewed measurement.** A reviewer checks about 200 sampled facts for a real hallucination rate, and language-competent reviewers check about 100 labels per language.
4. **Retrieval benchmark.** Keyword, semantic and hybrid search in English, Hindi and Arabic, with about 100 questions per language.
5. **Trace and close the missed-gap cases** (the 82 overdue residents) and add personal and family history to the expected outcome.
6. **Real-world pathways** only after the above: FHIR exchange with a real record system, ABDM alignment, and real SMS or WhatsApp delivery under a real consent framework.
7. **Hardening.** A shared rate-limit store, HttpOnly session cookies, statement timeouts, and denied-access events in the audit table.
9. **Planner realism.** Real clinic calendars and travel times, and priority-weighted planning once real priority data exists.
10. **Scale.** Run the 1,000,000-resident target, and approve the India breast and cervical drafts without double outreach.

---

## Repository layout

```text
backend/            FastAPI app, modules, Alembic migrations, tests
  anticipa/
    datafabric/     Synthea pipeline, FHIR normaliser, family linker, docgen, HAPI loader
    evidence/       extraction, quote verification, escalation, review queue, search
    guidelines/     rule language, evaluator, versions, population jobs, Studio dry runs
    caregaps/       cases, lifecycle, priority
    planning/       OR-Tools planner, capacity, validator
    engagement/     templates, simulated outreach
    conversation/   resident conversation agent, classifier, reply checks
    closure/        completion evidence and closure
    consent/        append-only consent ledger
    analytics/      Command Center, geography, programmes, impact snapshots
    ask/            Ask Anticipa tool loop and grounding checks
    evaluation/     evaluation harness, gates, impact baselines
    ai/             model router, provider adapters, accounting
    jobs/           PostgreSQL job queue and worker
frontend/           Next.js app, components, Playwright tests
config/             model routing, guideline packs, geography, Synthea version
docker/             PostgreSQL and HAPI images
scripts/            Synthea bootstrap, provider checks, publish gate
diagrams/           draw.io sources and exported diagrams
walkthrough/        screenshots used in this README
docker-compose.yml  PostgreSQL 17 + pgvector, HAPI FHIR
```

---

## License

[MIT](LICENSE). Copyright (c) 2026 vishavmehra.

## Acknowledgements

- [MITRE Synthea](https://github.com/synthetichealth/synthea) for synthetic patients, and [HAPI FHIR](https://hapifhir.io/) for the FHIR R4 server.
- [Google OR-Tools](https://developers.google.com/optimization) for the CP-SAT solver.
- [Anthropic](https://www.anthropic.com/) for Claude, [Nebius](https://nebius.com/) for Token Factory, [Supabase](https://supabase.com/), [Render](https://render.com/) and [Vercel](https://vercel.com/) for the platform.
- [DataMeet](https://datameet.org/) community maps (CC BY) and the 2024 maps supplement by shijithpk that builds on them.
- The [USPSTF](https://www.uspreventiveservicestaskforce.org/), the US Multi-Society Task Force on Colorectal Cancer, the Indian Ministry of Health and Family Welfare, WHO and the ICMR-INDIAB investigators, whose published work the rules and the problem statement rest on.
