import express from 'express';

const app = express();
app.use(express.json());

const PORT = process.env.PORT || 3000;
const FRESHSALES_BASE_URL = process.env.FRESHSALES_BASE_URL;
const FRESHSALES_API_KEY = process.env.FRESHSALES_API_KEY;
const TEST_TOKEN = process.env.TEST_TOKEN;
const TEST_LOOKUP_EMAIL = process.env.TEST_LOOKUP_EMAIL;

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
  const url = new URL(value);
  const path = url.pathname.replace(/\/$/, '');
  if (!path) return `${url.origin}/crm/sales`;
  if (path === '/crm/sales') return `${url.origin}${path}`;
  throw new Error('FRESHSALES_BASE_URL must be the site root or end in /crm/sales');
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

    // Freshsales lookup returns { contacts: { contacts: [...] } }.
    const foundContacts = lookupData?.contacts?.contacts;
    if (!Array.isArray(foundContacts)) {
      return res.status(502).json({
        ok: false,
        stage: 'contact_lookup',
        error: 'Unexpected Freshsales lookup response shape',
        response_shape: {
          top_level_keys: lookupData && typeof lookupData === 'object' ? Object.keys(lookupData) : [],
          contacts_type: Array.isArray(lookupData?.contacts) ? 'array' : typeof lookupData?.contacts,
          contacts_keys: lookupData?.contacts && !Array.isArray(lookupData.contacts) && typeof lookupData.contacts === 'object' ? Object.keys(lookupData.contacts) : [],
          upstream_http_status: lookupResponse.status,
          upstream_content_type: lookupResponse.headers.get('content-type'),
          upstream_redirected: lookupResponse.redirected,
          upstream_response_is_html: typeof lookupData === 'string' && /^\s*</.test(lookupData),
          configured_base_has_crm_sales_path: new URL(base).pathname.replace(/\/$/, '').endsWith('/crm/sales')
        }
      });
    }
    const exactContacts = foundContacts.filter(
      (record) => String(record?.email || '').trim().toLowerCase() === email
    );

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

    const detailedContact = detailData?.contact;
    if (!detailedContact || String(detailedContact.id) !== String(contactId)) {
      return res.status(502).json({
        ok: false, stage: 'contact_deals', error: 'Unexpected Freshsales contact response shape',
        lookup_contact: { id: contactId, email: contact.email, name: contact.display_name },
        response_shape: {
          top_level_keys: detailData && typeof detailData === 'object' ? Object.keys(detailData) : [],
          contact_keys: detailedContact && typeof detailedContact === 'object' ? Object.keys(detailedContact) : []
        }
      });
    }
    if (!Array.isArray(detailData.deals)) {
      return res.status(502).json({
        ok: false, stage: 'contact_deals', error: 'Freshsales did not return connected deals',
        lookup_contact: { id: detailedContact.id, email: detailedContact.email, name: detailedContact.display_name },
        response_shape: {
          top_level_keys: Object.keys(detailData),
          contact_keys: Object.keys(detailedContact),
          top_level_deals_type: Array.isArray(detailData?.deals) ? 'array' : typeof detailData?.deals,
          nested_deals_type: typeof detailedContact.deals
        }
      });
    }
    const deals = detailData.deals;
    if (Array.isArray(detailedContact.deal_ids) &&
        deals.some((deal) => !detailedContact.deal_ids.map(String).includes(String(deal.id)))) {
      return res.status(502).json({ ok: false, stage: 'contact_deals', error: 'Connected deal IDs do not match contact' });
    }

    const result = {
      ok: false,
      mode: 'read-only',
      email,
      contact: {
        id: detailedContact.id,
        name: detailedContact.display_name || [detailedContact.first_name, detailedContact.last_name].filter(Boolean).join(' '),
        email: detailedContact.email
      },
      deals,
      deal_count: deals.length
    };

    if (deals.length !== 1) {
      return res.status(deals.length ? 409 : 404).json({
        ...result,
        status: deals.length ? 'MULTIPLE_DEALS_REVIEW' : 'NO_ASSOCIATED_DEAL',
        next_step: 'Human review required before choosing any deal.'
      });
    }

    const dealResponse = await fetch(`${base}/api/deals/${encodeURIComponent(deals[0].id)}?include=deal_stage`, {
      headers: freshsalesHeaders()
    });
    if (!dealResponse.ok) {
      return res.status(502).json({ ...result, stage: 'deal_stage', freshsales_status: dealResponse.status });
    }
    const dealData = await dealResponse.json();
    if (String(dealData?.deal?.id) !== String(deals[0].id)) {
      return res.status(502).json({ ...result, stage: 'deal_stage', error: 'Deal detail ID mismatch' });
    }
    const stageId = dealData.deal.deal_stage_id;
    const stage = Array.isArray(dealData.deal_stages)
      ? dealData.deal_stages.find(item => String(item.id) === String(stageId))
      : null;
    result.deal_stage = { id: stageId, name: stage?.name || null };
    result.deal_pipeline_id = dealData.deal.deal_pipeline_id;
    if (String(result.deal_pipeline_id) !== '3000017890') {
      return res.status(409).json({
        ...result,
        status: 'PIPELINE_REVIEW',
        next_step: 'Human review required: this deal is not in the Exio Sellers pipeline.'
      });
    }

    return res.json({
      ...result,
      ok: true,
      deal_match_status: 'SINGLE_EXIO_SELLERS_DEAL',
      report_status: 'AWAITING_COMPLETED_ZOOM',
      next_step: 'Correlate a completed Zoom meeting to this contact and deal before drafting a report or writing to Freshsales.'
    });
  } catch (error) {
    console.error(error);
    return res.status(500).json({ ok: false, error: 'Unexpected server error' });
  }
});

async function runContactEmailMaintenance() {
  const id = process.env.CONTACT_EMAIL_UPDATE_ID;
  if (!id) return;
  const expectedName = process.env.CONTACT_EMAIL_UPDATE_NAME;
  const primary = process.env.CONTACT_EMAIL_UPDATE_PRIMARY?.trim().toLowerCase();
  const secondary = process.env.CONTACT_EMAIL_UPDATE_SECONDARY?.trim().toLowerCase();
  if (!/^\d+$/.test(id) || !expectedName || !primary || !secondary || primary === secondary) {
    console.error('Contact email maintenance: invalid configuration');
    return;
  }
  try {
    const base = normalizeBaseUrl(FRESHSALES_BASE_URL);
    const url = `${base}/api/contacts/${encodeURIComponent(id)}`;
    const beforeResponse = await fetch(url, { headers: freshsalesHeaders(), signal: AbortSignal.timeout(20000) });
    if (!beforeResponse.ok) throw new Error(`Contact read failed: ${beforeResponse.status}`);
    const before = (await beforeResponse.json()).contact;
    const emails = before?.emails;
    if (String(before?.id) !== id || before?.display_name !== expectedName ||
        String(before?.email || '').toLowerCase() !== primary ||
        !Array.isArray(emails) || !emails.some(item => item.is_primary && String(item.value).toLowerCase() === primary)) {
      console.error('Contact email maintenance: identity or primary email check failed');
      return;
    }
    const values = emails.map(item => String(item.value || '').trim().toLowerCase());
    if (values.includes(secondary)) {
      console.log('Contact email maintenance: both addresses already present', JSON.stringify({ id, emails: values }));
      return;
    }
    if (process.env.CONTACT_EMAIL_UPDATE_EXECUTE !== 'true') {
      console.log('Contact email maintenance dry run', JSON.stringify({ id, existing_emails: values, proposed_secondary: secondary }));
      return;
    }
    const updatedEmails = emails.map(item => ({
      id: item.id, value: item.value, is_primary: item.is_primary
    }));
    updatedEmails.push({ value: secondary, is_primary: false });
    const updateResponse = await fetch(url, {
      method: 'PUT',
      headers: freshsalesHeaders(),
      body: JSON.stringify({ contact: { emails: updatedEmails } }),
      signal: AbortSignal.timeout(20000)
    });
    if (!updateResponse.ok) {
      console.error('Contact email maintenance: update rejected', JSON.stringify({ http_status: updateResponse.status }));
      return;
    }
    const verifyResponse = await fetch(url, { headers: freshsalesHeaders(), signal: AbortSignal.timeout(20000) });
    if (!verifyResponse.ok) throw new Error(`Verification read failed: ${verifyResponse.status}`);
    const after = (await verifyResponse.json()).contact;
    const afterEmails = Array.isArray(after?.emails) ? after.emails.map(item => ({
      value: String(item.value || '').trim().toLowerCase(), is_primary: item.is_primary
    })) : [];
    console.log('Contact email maintenance result', JSON.stringify({
      id, verified: String(after?.id) === id &&
        afterEmails.some(item => item.value === primary && item.is_primary) &&
        afterEmails.some(item => item.value === secondary && !item.is_primary),
      emails: afterEmails
    }));
  } catch (error) {
    console.error('Contact email maintenance failed:', error.message);
  }
}

app.listen(PORT, async () => {
  console.log(`Exio Sales Automation listening on port ${PORT}`);
  await runContactEmailMaintenance();
  // Temporary, opt-in read-only check. The token is used inside the container and never logged.
  if (TEST_LOOKUP_EMAIL) {
    try {
      const url = `http://127.0.0.1:${PORT}/test/freshsales-lookup?email=${encodeURIComponent(TEST_LOOKUP_EMAIL)}`;
      const response = await fetch(url, {
        headers: { 'x-test-token': TEST_TOKEN || '' },
        signal: AbortSignal.timeout(20000)
      });
      const result = await response.json();
      console.log('Read-only Freshsales diagnostic', JSON.stringify({
        http_status: response.status,
        status: result.status || result.stage || (result.ok ? 'SUCCESS' : 'ERROR'),
        freshsales_status: result.freshsales_status,
        response_shape: result.response_shape,
        lookup_contact: result.lookup_contact,
        deal_stage: result.deal_stage,
        deal_pipeline_id: result.deal_pipeline_id,
        deal_match_status: result.deal_match_status,
        report_status: result.report_status,
        contact: result.contact,
        deal_count: result.deal_count,
        deals: Array.isArray(result.deals) ? result.deals.map((deal) => ({
          id: deal.id, name: deal.name, deal_stage_id: deal.deal_stage_id, status: deal.status
        })) : undefined
      }));
    } catch (error) {
      console.error('Read-only Freshsales diagnostic failed:', error.message);
    }
  }
  if (process.env.TEST_LIST_STAGES === 'true') {
    try {
      const base = normalizeBaseUrl(FRESHSALES_BASE_URL);
      const pipelinesResponse = await fetch(`${base}/api/selector/deal_pipelines`, { headers: freshsalesHeaders() });
      const pipelinesData = await pipelinesResponse.json();
      const pipelines = Array.isArray(pipelinesData?.deal_pipelines) ? pipelinesData.deal_pipelines : [];
      console.log('Read-only deal pipeline diagnostic', JSON.stringify({
        http_status: pipelinesResponse.status,
        response_keys: Object.keys(pipelinesData),
        pipelines: pipelines.map(item => ({ id: item.id, name: item.name }))
      }));
      for (const pipeline of pipelines) {
        const stagesResponse = await fetch(`${base}/api/selector/deal_pipelines/${encodeURIComponent(pipeline.id)}/deal_stages`, { headers: freshsalesHeaders() });
        const stagesData = await stagesResponse.json();
        console.log('Read-only stage list diagnostic', JSON.stringify({
          pipeline_id: pipeline.id,
          http_status: stagesResponse.status,
          response_keys: Object.keys(stagesData),
          stages: Array.isArray(stagesData?.deal_stages) ? stagesData.deal_stages.map(item => ({
            id: item.id, name: item.name, deal_pipeline_id: item.deal_pipeline_id
          })) : undefined
        }));
      }
    } catch (error) {
      console.error('Read-only stage list diagnostic failed:', error.message);
    }
  }
  if (process.env.TEST_FILTER_EMAIL) {
    try {
      const base = normalizeBaseUrl(FRESHSALES_BASE_URL);
      const response = await fetch(`${base}/api/filtered_search/contact`, {
        method: 'POST',
        headers: freshsalesHeaders(),
        body: JSON.stringify({
          filter_rule: [{
            attribute: 'contact_email.email',
            operator: 'is_in',
            value: process.env.TEST_FILTER_EMAIL.trim().toLowerCase()
          }]
        }),
        signal: AbortSignal.timeout(20000)
      });
      const data = await response.json();
      console.log('Read-only exact email diagnostic', JSON.stringify({
        http_status: response.status,
        total: data?.meta?.total,
        contacts: Array.isArray(data?.contacts) ? data.contacts.map(item => ({
          id: item.id, name: item.display_name, email: item.email
        })) : undefined,
        response_keys: data && typeof data === 'object' ? Object.keys(data) : []
      }));
    } catch (error) {
      console.error('Read-only exact email diagnostic failed:', error.message);
    }
  }
  if (process.env.TEST_SEARCH_QUERY) {
    try {
      const base = normalizeBaseUrl(FRESHSALES_BASE_URL);
      for (const query of process.env.TEST_SEARCH_QUERY.split(',').map(value => value.trim()).filter(Boolean)) {
        const url = `${base}/api/search?q=${encodeURIComponent(query)}&include=lead,contact,deal&per_page=100`;
        const response = await fetch(url, { headers: freshsalesHeaders(), signal: AbortSignal.timeout(20000) });
        const results = await response.json();
        console.log('Read-only Freshsales search diagnostic', JSON.stringify({
          query,
          http_status: response.status,
          result_count: Array.isArray(results) ? results.length : null,
          results: Array.isArray(results) ? results.map(item => ({
            id: item.id, type: item.type, name: item.name, email: item.email
          })) : undefined
        }));
      }
    } catch (error) {
      console.error('Read-only Freshsales search diagnostic failed:', error.message);
    }
  }
});
