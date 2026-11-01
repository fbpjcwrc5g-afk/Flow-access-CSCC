# Flow-access-CSCC

Reads patient data from Epic over FHIR R4, determines each patient's **insurance pathway**
(Original Medicare, Medicare Advantage, Medicaid, dual eligible, commercial, self-pay, and so on), and
triggers **RN case manager workflows** such as Important Message from Medicare delivery, MOON notices,
payer admission notifications, and financial counseling referrals.

## How it works

```
 Epic                                   flow-access                                   Case management
 ────                                   ───────────                                   ───────────────
 Event notifications ──► POST /notifications/fhir ─┐
 (FHIR Subscription,                               │   fetch Patient, Coverage,
  ADT via interface engine)                        ├──► Encounter from Epic FHIR R4 ──┐
                                                   │                                  ▼
 Bulk FHIR $export ◄──── flow-access poll ─────────┘             Pathway classifier (pathway_rules.yaml)
 (Group, incremental _since)                                                          │
                                                                                      ▼
                                                     Workflow rules (workflow_rules.yaml): pathway + event
                                                                                      │
                                                     De-duplicate (SQLite) ───────────┤
                                                                                      ▼
                                                     Task sinks: log / webhook / FHIR Task ──► worklists
```

1. **Authentication.** SMART Backend Services: the service signs a short-lived RS384 JWT with its private
   key and exchanges it at Epic's token endpoint for an access token (`src/flow_access/fhir/auth.py`).
2. **Ingestion.** There are two complementary modes:
   - **Real time:** `POST /notifications/fhir` accepts FHIR R4 Subscription notification bundles (the R4
     Subscriptions Backport format), bare FHIR resources, or a simple `{"patient_id", "encounter_id"}`
     body. The simple body suits an interface engine (Rhapsody, Cloverleaf, Mirth) that translates Epic
     HL7v2 ADT A01/A03/A08 messages. The service always re-reads current data from Epic before deciding.
   - **Batch:** `flow-access poll` runs Group-level Bulk FHIR `$export` with `_since`, so each cycle only
     picks up patients whose Patient, Encounter, or Coverage data changed.
3. **Pathway determination** (`src/flow_access/pathway.py`). Each active Coverage is classified first by
   coded `Coverage.type` values (Source of Payment Typology and HL7 ActCode), then by payor and plan name
   keywords. Coverages are ranked by `Coverage.order`. Active Medicare plus Medicaid together means
   dual eligible. Every result includes a confidence level, a `needs_verification` flag, and readable reasons.
4. **Clinical events** are derived from `Encounter.class` and `Encounter.status`: inpatient admission,
   inpatient discharge, observation start, ED visit, and insurance pathway change.
5. **Workflow rules** (`src/flow_access/config/workflow_rules.yaml`) map pathway + event to a task with a
   priority, assignee role, and due time anchored to the event (for example, IM within 48 hours of admission).
6. **Dispatch.** Each task fires once per rule, patient, and encounter. If delivery fails, the claim is
   released so the next notification or poll retries it.

## Priority plans, referral packets, and PCS forms

The priority plans are **L.A. Care**, **Blue Shield of California Promise**, **Molina**, and **Optum**.
They are defined in `src/flow_access/config/plans.yaml` and identified from the Coverage payor, plan, and
group names. Optum is modeled as a *delegated medical group*. When a patient's coverage shows both a
health plan and Optum (for example L.A. Care with Optum as the IPA), Optum's documents and network are
used, because the delegated group usually owns utilization management.

Once a plan is identified:

- **Admission** to inpatient or observation creates a *Start health plan discharge planning packet*
  task carrying the plan's utilization management contact.
- **Post-acute orders** in Epic (ServiceRequest) are matched by keyword to home health, SNF, DME, or
  transportation. Each creates a task with the plan's **required documents** and **contracted
  agencies** for that service.
- **Transportation** orders also produce a pre-filled **Physician Certification Statement (PCS)**:
  - **Member details:** name, date of birth, Medi-Cal CIN / plan member ID (`Coverage.subscriberId`),
    MRN, and contact information.
  - **Diagnoses:** active ICD-10-CM diagnoses, with current-encounter diagnoses first. Resolved and
    refuted Conditions are excluded.
  - **Clinical justification:** built only from chart-coded findings listed in `transport_criteria.yaml`,
    such as hemiplegia, wheelchair dependence, bed confinement, or oxygen dependence. These also suggest
    a minimum mode (wheelchair van, litter van, or ambulance). If no qualifying finding is coded, the
    justification is left for the prescriber and no mode is suggested.
  - **Prescriber:** the ordering provider's name and NPI, taken from the order's requester.
  - **Dates:** one-time trips default to today. Recurring trips are capped at 12 months.
  - The form is always marked **DRAFT**. It lists what is still missing and is never signed on a
    provider's behalf.

| Endpoint | Purpose |
| --- | --- |
| `POST /patients/{id}/packet` | Full packet from live Epic data. Optional body: `{"needs": [...], "transport": {...}}` |
| `POST /patients/{id}/pcs?format=html\|json\|pdf` | Pre-filled PCS form (`pdf` requires the plan's fillable PDF to be configured) |
| `POST /packet/bundle`, `POST /pcs/bundle` | The same, from a supplied FHIR Bundle |

`transport` accepts `mode`, `start_date`, `end_date`, `trip_type` (`one_way`, `round_trip`, `recurring`),
`frequency`, `origin`, `destination`, `reason`, and `additional_limitations`.

```bash
flow-access packet examples/la_care_stroke_discharge.json --pcs-html pcs.html
```

### What your team needs to configure

- **`contracted_agencies.csv`**: replace the `EXAMPLE` rows with your contracted home health agencies,
  SNFs, and DME suppliers for each plan (`plan_id, service, name, phone, fax, address, service_area, notes`).
  Case management leadership can maintain this file without code changes.
- **`plans.yaml`**:
  - Fill in each `CONFIGURE` value: UM and transportation contacts, plan-specific documents, and keywords
    matching how the plans are named in your Epic payor and plan records.
  - To output each plan's official PCS PDF, save the fillable PDF and set `pcs_form.template_pdf`.
  - Map the PDF's field names to PCS data paths in `field_map`, for example
    `{"Member Name": "patient.name", "CIN": "patient.member_id", "Dx": "diagnoses", "Justification": "clinical_justification"}`.
- **`transport_criteria.yaml`**: have your medical director or UM team review the ICD-10 criteria and
  suggested minimum modes.
- **Product lines** in `pathway_rules.yaml`, for example L.A. Care Medicare Plus and Molina Medicare
  Complete Care treated as dual eligible, and L.A. Care Covered treated as commercial. Confirm these
  against your payor build.

## Quick start (no Epic connection needed)

```bash
python3 -m venv .venv && . .venv/bin/activate
pip install -e ".[dev]"
pytest                                                      # includes a simulated Epic server
flow-access evaluate examples/medicare_advantage_admission.json   # offline dry run
```

Run the API:

```bash
FLOW_WEBHOOK_TOKEN=change-me flow-access serve --port 8000
# Dry-run any FHIR Bundle (one Patient + Coverage/Encounter resources) against the rules:
curl -X POST localhost:8000/evaluate/bundle -H 'content-type: application/json' \
  -d "{\"bundle\": $(cat examples/medicare_advantage_admission.json)}"
```

| Endpoint | Purpose |
| --- | --- |
| `POST /notifications/fhir` | Inbound event notifications (requires `Authorization: Bearer $FLOW_WEBHOOK_TOKEN`) |
| `POST /patients/{id}/evaluate?encounter_id=...` | Pull live data from Epic and evaluate one patient |
| `POST /evaluate/bundle` | Evaluate a supplied Bundle; dry run unless `"dispatch": true` |
| `GET /tasks?patient_id=...` | Tasks that have been dispatched |
| `GET /.well-known/jwks.json` | Public signing key to register with Epic |
| `GET /health` | Liveness check |

## Connecting to Epic

1. **Register a Backend Systems app** on [Epic on FHIR](https://fhir.epic.com/) (or through your
   organization's Epic Showroom process). Request R4 read access to `Patient`, `Coverage`, `Encounter`,
   `Condition`, `ServiceRequest`, and `Practitioner`, plus `Group` Bulk Data `$export` if you use polling.
   Add `Task.Create` only if you plan to use the `fhir_task` sink. Condition, ServiceRequest, and
   Practitioner are optional: without them, pathway workflows still run, but packets have no detected
   orders and the PCS form has no diagnoses or prescriber.
2. **Create a signing key** with `flow-access generate-key --out epic_private_key.pem`. Register the
   public key, or host `/.well-known/jwks.json` and register that URL as the JWK Set URL.
3. **Have your Epic analysts** activate the client ID in your environment, map it to a background user,
   and (for polling) build a Group, such as a registry of inpatients or a payer-attributed population.
4. **For real-time events**, work with your Epic team to forward ADT events, either as FHIR
   notifications or through your interface engine, to `/notifications/fhir`.
5. Configure the environment variables below (an `.env` file also works) and start with
   `FLOW_TASK_SINKS=log` against your non-production Epic environment.

| Variable | Description |
| --- | --- |
| `FLOW_EPIC_FHIR_BASE_URL` | e.g. `https://<host>/api/FHIR/R4` |
| `FLOW_EPIC_TOKEN_URL` | e.g. `https://<host>/oauth2/token` |
| `FLOW_EPIC_CLIENT_ID` | Client ID from the Epic app registration |
| `FLOW_EPIC_PRIVATE_KEY_PATH` | Path to the PEM private key |
| `FLOW_EPIC_KEY_ID` | `kid` header; must match the registered key |
| `FLOW_EPIC_SCOPES` | Space-separated system scopes |
| `FLOW_EPIC_BULK_GROUP_ID` | Group FHIR ID for `flow-access poll` |
| `FLOW_WEBHOOK_TOKEN` | Shared secret for inbound notifications; the endpoint is disabled if unset |
| `FLOW_TASK_SINKS` | Comma-separated: `log`, `webhook`, `fhir_task` |
| `FLOW_TASK_WEBHOOK_URL` / `FLOW_TASK_WEBHOOK_TOKEN` | Target for the `webhook` sink (care management platform, integration engine) |
| `FLOW_DB_PATH` | SQLite file for de-duplication, pathway history, and the polling cursor |
| `FLOW_PATHWAY_RULES_PATH` / `FLOW_WORKFLOW_RULES_PATH` | Override the bundled YAML rules |
| `FLOW_PLANS_PATH` / `FLOW_AGENCIES_PATH` / `FLOW_TRANSPORT_CRITERIA_PATH` | Override plan profiles, contracted agencies, and PCS criteria |

## Customizing the rules

- **`pathway_rules.yaml`**: add your organization's payor names, plan names, and any local coding
  systems. Epic financial classes and payor naming vary between organizations, so review this with
  patient access and revenue cycle teams. Test changes with `/evaluate/bundle` or `flow-access evaluate`.
- **`workflow_rules.yaml`**: each rule lists `pathways`, `events`, `priority`, and `due_within_hours`.

> The regulatory timeframes in the default workflow rules (IM, MOON, VA notification) reflect common CMS
> and VA requirements but are **starting points only**. Validate them with compliance and case management
> leadership before using them in production.

## Security and compliance notes

- This service handles PHI. Deploy it inside your HIPAA-compliant environment, with TLS in front of the
  API, and secure the SQLite file (or replace `Store` with your managed database).
- Logs contain FHIR IDs, rule IDs, and pathways, but no names or demographics.
- Inbound notifications require a bearer token, compared in constant time. Private keys and `.env`
  files are git-ignored.
- PCS forms and packets contain PHI by design (name, DOB, member ID, diagnoses). Serve them only to
  authorized case management staff.
- `/evaluate/bundle`, `/patients/{id}/evaluate`, `/tasks`, and the packet and PCS endpoints have no
  end-user authentication. Put them
  behind your API gateway or SSO before exposing them outside a trusted network.

## Project layout

```
src/flow_access/
  fhir/auth.py       SMART Backend Services JWT auth and JWKS
  fhir/client.py     FHIR R4 reads, paged search, Bulk Data $export
  fhir/extract.py    Field extraction from Epic resources
  pathway.py         Insurance pathway classifier
  plans.py           Plan / delegated group profiles, documents, contracted agencies
  clinical.py        ICD-10 diagnoses, post-acute need detection, transport criteria
  pcs.py             Pre-filled PCS form: HTML rendering and plan PDF filling
  packets.py         Per-need referral packets
  templates/         PCS form HTML template
  workflows.py       Encounter events and the workflow rules engine
  service.py         Orchestration, notification parsing, bulk polling
  sinks.py           Log, webhook, and FHIR Task outputs
  store.py           SQLite de-duplication, pathway history, cursors
  api.py / cli.py    HTTP API and command line
  config/            Default pathway and workflow rules
tests/               Unit and end-to-end tests with a simulated Epic server
```
