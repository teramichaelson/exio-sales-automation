# Exio Sales Automation

The Railway service currently provides a protected, read-only Freshsales lookup and a tested meeting-correlation decision module. It does not retrieve Zoom data or write reports or files to Freshsales.

## Live lookup

`GET /test/freshsales-lookup?email=person@example.com` with the private `x-test-token` header:

1. Finds an exact primary or secondary Freshsales Contact email, verifying secondary addresses on the full Contact record.
2. Returns Contact ID, name, primary email, connected deals and Deal IDs, stage, and pipeline.
3. Returns `SINGLE_EXIO_SELLERS_DEAL` and `AWAITING_COMPLETED_ZOOM` only for one verified Exio Sellers deal. Missing or multiple Contacts/deals require human review.

The test token and Freshsales API key remain in Railway environment variables. Never put their values in GitHub or chat.

## Meeting decision module

`meeting-match.mjs` accepts a completed Zoom meeting identity (UUID, end time, host), selected external attendee email addresses, and the results of the Freshsales lookup for each address. It returns one draft-ready Contact/Deal match when all addresses resolve to the same Contact and one Exio Sellers deal. Missing completion evidence or ambiguous matches wait or require review. Run `node --test meeting-match.test.mjs` for the controlled cases.

This module is not yet connected to Calendar or Zoom. The caller must provide completion evidence from authorized Zoom access, not an unverified calendar end time. Roald's host data is needed for his calls and Jason's for his. Reports and Freshsales writes remain disabled.

## Railway variables

- `FRESHSALES_BASE_URL`: Freshsales site root or `/crm/sales` URL.
- `FRESHSALES_API_KEY`: private Freshsales API key.
- `TEST_TOKEN`: private test endpoint token.
- `PORT`: supplied by Railway.

Temporary diagnostics are opt-in and should be cleared after testing. See `IMPLEMENTATION_LOG.md` for progress and remaining gates.
