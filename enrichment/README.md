# Budgeted buyer enrichment

This isolated Python worker replaces repeated browser research and spreadsheet imports with a resumable SQLite queue. It runs as a separate private service and scheduled runner; the existing Node service is unchanged.

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

AI requires `OPENAI_API_KEY`, the explicitly chosen `ENRICHMENT_AI_MODEL` and an enabled flag. ChatGPT seat usage is not an API credential. API requests incur metered charges; successful responses retain model/token usage without credentials.

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
4. Keep the old browser enrichment task paused. Run one complete source-backed dry-run, then one controlled production contact write with note and field readback. The earlier controlled live field/note pilot has passed; model extraction must also pass before broader activation.
5. Configure weekly/completion email delivery and deduplication. Schedule only this bounded worker after the pilot passes. Increase throughput gradually from the conservative validation limits.
6. Resolve identity and unavailable-source queues separately and reconcile historical criteria flags. Stop via the cron setting or remove `ENRICHMENT_WRITES=enabled`.

## Validation

```sh
python -m unittest discover -s enrichment -v
```

Tests cover domain exclusions, identity holds, exact evidence, dropdown options, financial metric separation, proposal deduplication, preservation of existing values, disabled writes, live affiliation changes, write/readback, uncertain-write recovery, persistent budgets, cached AI results, missing AI configuration, uncertain AI requests, reporting deduplication and checkpoint preservation. Integration test doubles exercise writes without modifying production contacts.

API contracts were checked against the official Freshsales API documentation at https://developers.freshworks.com/crm/api/ and OpenAI Responses API documentation at https://developers.openai.com/api/reference/resources/responses/methods/create .

## Private deployment and scheduling

The restored checkpoint lives on the private worker's persistent `/data` volume. The worker, runner and validation probe have no public domains. The runner calls authenticated `/tick` at ten minutes past each hour. The worker starts a background batch, skips overlaps and records its completed result in `last_job`.

Each source batch has at most 8 domains. Persistent UTC-day ceilings are 200 attempted domains and 1,000 website requests. Cached HTML is compressed; queue queries are indexed. Source collection stops at 400 MiB database size. A source packet alone does not count as interpreted or saved criteria.

AI requires the key/model and `ENRICHMENT_AI_ENABLED=enabled`. Strict structured extraction is followed by a separately cached semantic review before automatic approval. Exact evidence, schema and affiliation checks also run locally. Every domain advances to no-criteria, extracted or a review hold rather than replaying the first cached packet indefinitely. Up to five domains are interpreted per tick. Daily call/input/output reservations are atomic and survive restarts. Defaults are 5 calls, 120,000 input bytes and 30,000 reserved output bytes; configurable ceilings are 100 calls, 1,200,000 input bytes and 600,000 reserved output bytes per UTC day. Every call reserves 1,500 output tokens (6,000 bytes). These controls are not a billing guarantee. Token usage is retained separately in `ai_usage`.

Scheduled writes require `ENRICHMENT_WRITES=enabled`. At most 25 approved contacts are written per tick. Unsupported, rejected or uncertain outcomes remain held. Only blank supported fields are filled and dated source notes are appended; readback verifies both. Removing either enabled flag pauses that stage. Removing the runner cron pauses processing. The original workbook/history are preserved.

The reporting automation runs Mondays at 8 a.m. America/Los_Angeles, checks Gmail Sent for duplicates, and sends at most one weekly email plus a completion email. Its private read-only probe fetches fresh status. Pending research, identity/review holds, pending/approved proposals and uncertain writes prevent a false completion report. The original inventory is not a reconciliation of all buyers currently in Freshsales; newly added buyers and held identities require a separate inventory pass before the entire project can be declared complete.

Private routes include health/status, one-time checkpoint bootstrap, source collection/evidence, controlled extraction and proposals, usage, prepare/approve/apply and explicit uncertain-write reconciliation. These administrative routes must remain private.

The automated suite currently has 38 tests, including semantic-review rejection, no-criteria queue advancement, actual usage retention, atomic reservations and pause/resume after a daily budget. The October 19 credential check authenticated successfully and retrieved the configured gpt-4.1-mini model. The generation test was rejected with HTTP 429, error code credit_balance_exhausted, type insufficient_quota. AI and scheduled writes remain disabled pending funded API access and a successful extraction pilot. Source collection and weekly reporting remain active. No successful model output or new CRM update resulted from this credential test.
