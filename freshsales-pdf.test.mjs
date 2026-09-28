import test from 'node:test';
import assert from 'node:assert/strict';
import { attachScoutbotPdf } from './freshsales-pdf.mjs';

const lookup = {
  ok: true, deal_match_status: 'SINGLE_EXIO_SELLERS_DEAL', deal_pipeline_id: 3000017890,
  contact: { id: 123 }, deals: [{ id: 456 }]
};
const pdfBytes = new TextEncoder().encode('%PDF-1.4\nexample');
const args = { baseUrl: 'https://crm.example', apiKey: 'test-only', lookup, pdfBytes, reportKey: 'event-123' };
const json = value => ({ ok: true, json: async () => value });
const detail = { deal: { id: 456, deal_pipeline_id: 3000017890,
  links: { document_associations: '/crm/sales/deals/456/document_associations' } } };

test('uploads one private PDF to the verified deal and checks multipart fields', async () => {
  let posts = 0;
  const fetchImpl = async (url, options) => {
    if (url.endsWith('/api/deals/456')) return json(detail);
    if (url.endsWith('/document_associations')) return json({ documents: [] });
    assert.ok(url.endsWith('/api/documents'));
    assert.equal(options.method, 'POST');
    assert.equal(options.body.get('targetable_type'), 'Deal');
    assert.equal(options.body.get('targetable_id'), '456');
    assert.equal(options.body.get('is_shared'), 'false');
    assert.equal(options.body.get('file').type, 'application/pdf');
    posts++;
    return json({ id: 789 });
  };
  const result = await attachScoutbotPdf({ ...args, fetchImpl });
  assert.equal(result.status, 'ATTACHED');
  assert.equal(result.document_id, 789);
  assert.equal(posts, 1);
});

test('existing report is not uploaded again', async () => {
  const fetchImpl = async url => url.endsWith('/api/deals/456') ? json(detail) :
    json({ documents: [{ id: 777, name: 'Scoutbot-precall-event-123.pdf' }] });
  const result = await attachScoutbotPdf({ ...args, fetchImpl });
  assert.equal(result.status, 'ALREADY_ATTACHED');
});

test('ambiguous deal or invalid PDF fails before network access', async () => {
  const fetchImpl = () => { throw new Error('network should not be called'); };
  await assert.rejects(attachScoutbotPdf({ ...args, lookup: { ...lookup, deals: [{ id: 456 }, { id: 789 }] }, fetchImpl }));
  await assert.rejects(attachScoutbotPdf({ ...args, pdfBytes: new TextEncoder().encode('not a pdf'), fetchImpl }));
});
