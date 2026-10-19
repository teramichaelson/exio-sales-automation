# Budgeted buyer enrichment

This isolated Python worker replaces repeated browser research and spreadsheet imports with a resumable SQLite queue. It does not change the existing Node service or launch automatically.

## Data flow

Historical workbook -> immutable contact IDs and legacy history -> one research packet per exact domain -> candidate evidence -> optional bounded AI extraction -> validated proposals -> approve supported findings -> fill empty CRM fields and append a native source note -> read back -> durable audit.

Do not deploy buyer exports, SQLite databases, evidence packets or credentials to this repository. Use a private persistent volume and private backups. Only one worker process may own a database; all CLI entrypoints acquire an exclusive lock. Filesystem storage inside an unmounted container is not durable.

## Commands

Python 3.11 or newer. Normal operation uses the standard library. Migration alone needs `requirements.txt`.

```sh
python enrichment/worker.py --db /data/enrichment.sqlite migrate /private/Buyer_Website_Enrichment.xlsx
python enrichment/worker.py --db /data/enrichment.sqlite status
python enrichment/worker.py --db /data/enrichment.sqlite research --domains 10 --requests 60
python enrichment/worker.py --db /data/enrichment.sqlite export-evidence --out /private/evidence.json
python enrichment/extract.py --db /data/enrichment.sqlite --domain example.com --schema /private/verified-fields.json
python enrichment/worker.py --db /data/enrichment.sqlite propose CONTACT_ID /private/findings.json --schema /private/verified-fields.json
python enrichment/worker.py --db /data/enrichment.sqlite approve PROPOSAL_ID
python enrichment/worker.py --db /data/enrichment.sqlite apply PROPOSAL_ID
# Enable a controlled write only after checking its dry-run and current CRM workflows.
python enrichment/worker.py --db /data/enrichment.sqlite apply PROPOSAL_ID --execute
python enrichment/run.py --db /data/enrichment.sqlite
# Optional stages are explicit; --ai prepares proposals, it does not approve them.
python enrichment/run.py --db /data/enrichment.sqlite --ai --write-approved
```

`FRESHSALES_BASE_URL` and `FRESHSALES_API_KEY` must be supplied through a secret manager. `ENRICHMENT_WRITES=enabled` is additionally required for writes. Reuse the existing service's credential by a Railway variable reference, without copying its secret into chat or source code. There is no public HTTP endpoint.

AI defaults off. Configure `OPENAI_API_KEY` and an explicitly chosen `ENRICHMENT_AI_MODEL` to enable it. ChatGPT seat usage is not an API credential. API requests can incur separate charges; no live paid model request was made during development.

Current conservative limits are ten domain attempts, sixty website requests, five AI requests and twenty-five approved contact writes per tick. Domain and HTTP counters persist per UTC day, so repeated ticks cannot exceed their daily limits. AI also persists a five-call daily ceiling, 120,000 input-byte reservation and 30,000 reserved output bytes; each request allows at most 1,500 output tokens. Byte reservations are conservative controls, not dollar or token billing measurements. Change limits only after measuring quality and usage. `run.py` is intended to be a short scheduled job, not an AI conversation that wakes hourly to reason through a workbook.

## Matching and field rules

- Use exact normalized domains, not guessed company-name matches. Public email providers are excluded. Existing identity holds remain held. A website domain alone does not prove contact affiliation.
- Freshsales contacts must still be Type Buyer and have a matching business email when applying a proposal.
- Current schema is checked before every write; field IDs and option values come from the connected account. Supply only the supported relevant schema. The field schema and its live response hash must agree.
- Only empty custom Website, Preferred Minimum EBITDA, Preferred Industries and Preferred Regions can be filled. Existing values, including zero, remain intact. No annual company revenue is written as acquisition target revenue. Revenue ranges, EBITDA maxima and exceptions remain in dated native source notes.
- Findings require exact quotes from cached source text. Dropdown choices must exist. Source text and AI output are untrusted; AI output remains pending until the findings are reviewed. Evidence matching alone does not prove semantic accuracy.
- Prior reviewed, unavailable and identity-held contacts are not replayed as pending work. The legacy workbook and saved-update history are preserved, including inconsistent historical flags. No reviewed source means no invented criteria.
- No deals, owners, subscriptions or sending controls are modified. CRM field changes may still trigger existing tenant automations; inspect those before the first live pilot.

## Retry and duplicate handling

Writes are journaled before network calls and are never automatically retried after an uncertain response. Reruns skip any previous write journal entry. Inspect the current contact and all notes before reconciling `uncertain_review`; do not delete the journal to force a retry. Preserve existing native notes. A source URL already present in legacy notes requires reconciliation, rather than a second note. Stable proposal IDs provide native-note markers for new writes. Read back both the contact and source note before counting success.

There is no server-side conditional-update guarantee here. Pause the old enrichment automation and avoid concurrent edits to the same target fields during the pilot. For a rollback, inspect the before/after audit and restore only this worker's changed values after checking for newer edits. Do not bulk-replace contacts with old snapshots.

Website reads are HTTPS-only, bounded, same-domain and robots-aware; nonpublic IPs, cross-domain redirects and oversized/non-text sources are held. PDFs and JavaScript-only sites require another evidence path. Do not disable public-host validation to work around DNS issues. Transient research failures use backoff and at most three attempts; exhausted retries require review. Exact-domain grouping intentionally avoids merging unrelated subsidiaries.

## Reporting and activation

The reporting module creates a durable `pending_delivery` outbox row no more than once every seven days, plus one verified-completion row. It does not send email. A mail delivery adapter must search Sent for an existing report in the user's local calendar week, send through the authorized mailbox and verify sending before marking a report delivered. Seed its reporting state from existing reports before activation. Identity holds, pending contacts, pending proposals and uncertain writes prevent completion.

Before enabling a Railway cron job:

1. Restore the private checkpoint to a persistent `/data` volume; verify its inventory and checksum. Keep the original workbook unchanged.
2. Attach the existing Freshsales credential via variable references. Refresh and save the field schema privately.
3. Configure an API model/key and a measured spending ceiling if AI is required; leave AI off otherwise.
4. Keep the old browser enrichment task paused. Run one complete source-backed dry-run, then one controlled production contact write with note and field readback. API contact/schema/notes reads have been exercised; live writes are not yet verified.
5. Configure weekly/completion email delivery and deduplication. Schedule only this bounded worker after the pilot passes. Increase throughput gradually from the conservative validation limits.
6. Resolve identity and unavailable-source queues separately and reconcile historical criteria flags. Stop via the cron setting or remove `ENRICHMENT_WRITES=enabled`.

## Validation

```sh
python -m unittest discover -s enrichment -v
```

Tests cover domain exclusions, identity holds, exact evidence, dropdown options, financial metric separation, proposal deduplication, preservation of existing values, disabled writes, live affiliation changes, write/readback, uncertain-write recovery, persistent budgets, cached AI results, missing AI configuration, uncertain AI requests, reporting deduplication and checkpoint preservation. Integration test doubles exercise writes without modifying production contacts.

API contracts were checked against the official Freshsales API documentation at https://developers.freshworks.com/crm/api/ and OpenAI Responses API documentation at https://developers.openai.com/api/reference/resources/responses/methods/create .

## Private deployment follow-up

The branch also contains `service.py` and a Python Dockerfile. A separate Railway worker has been deployed with a persistent `/data` volume, no public domain and production writes disabled. Private health and authenticated status probes succeeded; unauthenticated status was rejected. The worker is still awaiting checkpoint transfer. The public-domain action was blocked by automatic approval review because the service contains buyer-data and CRM administrative routes. Do not expose it without explicit authorization. Prefer an authenticated private transfer path when available.

`/bootstrap` is authenticated, bounded and one-time: it integrity-checks a SQLite checkpoint before atomically installing it, and refuses to replace an existing checkpoint. `/prepare` produces a dry-run; `/approve` does not itself write to CRM; `/apply` additionally requires `ENRICHMENT_WRITES=enabled`. There is no automatic scheduler in the HTTP service. Run `run.py` as a separately managed bounded job only after the checkpoint and controlled write pilot pass. `test_service.py` adds six checks for authentication, health data exposure, missing checkpoint, invalid SQLite and one-time bootstrap. The combined suite now has 25 tests.
