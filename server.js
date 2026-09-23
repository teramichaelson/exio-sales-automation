import express from 'express';

const app = express();
app.use(express.json());

const PORT = process.env.PORT || 3000;
const FRESHSALES_BASE_URL = process.env.FRESHSALES_BASE_URL;
const FRESHSALES_API_KEY = process.env.FRESHSALES_API_KEY;
const TEST_TOKEN = process.env.TEST_TOKEN;

function requireConfig() {
  const missing = [];
  if (!FRESHSALES_BASE_URL) missing.push('FRESHSALES_BASE_URL');
  if (!FRESHSALES_API_KEY) missing.push('FRESHSALES_API_KEY');
  if (!TEST_TOKEN) missing.push('TEST_TOKEN');
  return missing;
}

function freshsalesHeaders() {
  return {
    Authorization: `Token token=${FRESHSALES_API_KEY}`,
    'Content-Type': 'application/json'
  };
}

function normalizeBaseUrl(value) {
  return value.replace(/\/$/, '');
}

app.get('/health', (_req, res) => {
  const missing = requireConfig();
  res.status(missing.length ? 503 : 200).json({
    ok: missing.length === 0,
    service: 'exio-sales-automation',
    mode: 'read-only-test',
    missing_config: missing
  });
});

app.get('/test/freshsales-lookup', async (req, res) => {
  try {
    const suppliedToken = req.get('x-test-token');
    if (!TEST_TOKEN || suppliedToken !== TEST_TOKEN) {
      return res.status(401).json({ ok: false, error: 'Unauthorized test request' });
    }

    const missing = requireConfig();
    if (missing.length) {
      return res.status(503).json({ ok: false, error: 'Missing configuration', missing_config: missing });
    }

    const email = String(req.query.email || '').trim().toLowerCase();
    if (!email || !email.includes('@')) {
      return res.status(400).json({ ok: false, error: 'Provide a valid email query parameter.' });
    }

    const base = normalizeBaseUrl(FRESHSALES_BASE_URL);
    const lookupUrl = `${base}/api/lookup?q=${encodeURIComponent(email)}&f=email&entities=contact`;

    const lookupResponse = await fetch(lookupUrl, { headers: freshsalesHeaders() });
    const lookupText = await lookupResponse.text();

    let lookupData;
    try {
      lookupData = JSON.parse(lookupText);
    } catch {
      lookupData = lookupText;
    }

    if (!lookupResponse.ok) {
      return res.status(502).json({
        ok: false,
        stage: 'contact_lookup',
        freshsales_status: lookupResponse.status,
        error: lookupData
      });
    }

    const exactContacts = Array.isArray(lookupData)
      ? lookupData.filter((record) => record?.type === 'contact' && String(record?.email || '').toLowerCase() === email)
      : [];

    if (exactContacts.length === 0) {
      return res.status(404).json({
        ok: false,
        status: 'CONTACT_NOT_FOUND',
        email
      });
    }

    if (exactContacts.length > 1) {
      return res.status(409).json({
        ok: false,
        status: 'MULTIPLE_CONTACTS',
        email,
        contacts: exactContacts
      });
    }

    const contact = exactContacts[0];
    const contactId = contact.id;
    const detailUrl = `${base}/api/contacts/${encodeURIComponent(contactId)}?include=deals`;

    const detailResponse = await fetch(detailUrl, { headers: freshsalesHeaders() });
    const detailText = await detailResponse.text();

    let detailData;
    try {
      detailData = JSON.parse(detailText);
    } catch {
      detailData = detailText;
    }

    if (!detailResponse.ok) {
      return res.status(502).json({
        ok: false,
        stage: 'contact_deals',
        freshsales_status: detailResponse.status,
        error: detailData
      });
    }

    const deals = Array.isArray(detailData?.deals) ? detailData.deals : [];

    return res.json({
      ok: true,
      mode: 'read-only',
      email,
      contact: {
        id: contact.id,
        name: contact.name,
        email: contact.email
      },
      deals,
      deal_count: deals.length,
      next_step: 'Review deal data and define deterministic open-deal selection rule before any CRM write actions are enabled.'
    });
  } catch (error) {
    console.error(error);
    return res.status(500).json({ ok: false, error: 'Unexpected server error' });
  }
});

app.listen(PORT, () => {
  console.log(`Exio Sales Automation listening on port ${PORT}`);
});
