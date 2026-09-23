# Exio Sales Automation - Test 2

This is a read-only starter service for Railway.

## Current capability

`GET /test/freshsales-lookup?email=person@example.com`

The endpoint:
1. Finds an exact Freshsales contact by email.
2. Fetches that contact with connected deals.
3. Returns the contact and deals as JSON.
4. Does not create, update, delete, or attach anything in Freshsales.

## Required Railway variables

- `FRESHSALES_BASE_URL` - full Freshsales bundle alias, ending in `/crm/sales`
- `FRESHSALES_API_KEY` - regenerated Freshsales API key
- `TEST_TOKEN` - a private random value used to protect this temporary test endpoint

Railway automatically provides `PORT`.

## Test request

Send a GET request to:

`https://YOUR-RAILWAY-DOMAIN/test/freshsales-lookup?email=REAL_TEST_EMAIL`

Include the header:

`x-test-token: YOUR_TEST_TOKEN`

Do not enable write operations until contact/deal matching has been verified with controlled records.
