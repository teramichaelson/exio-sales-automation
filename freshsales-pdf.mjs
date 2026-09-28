const EXIO_SELLERS_PIPELINE_ID = '3000017890';
const MAX_PDF_BYTES = 20 * 1024 * 1024;

function salesBase(value) {
  const url = new URL(value);
  if (url.protocol !== 'https:') throw new Error('Freshsales URL must use HTTPS');
  const path = url.pathname.replace(/\/$/, '');
  if (path && path !== '/crm/sales') throw new Error('Unexpected Freshsales base path');
  return `${url.origin}/crm/sales`;
}

/** Uploads only a verified pre-call PDF to the one matched Exio Sellers deal. */
export async function attachScoutbotPdf({ baseUrl, apiKey, lookup, pdfBytes, reportKey, fetchImpl = fetch }) {
  if (!apiKey) throw new Error('Freshsales API key missing');
  if (!/^[A-Za-z0-9_-]{1,80}$/.test(reportKey || '')) throw new Error('Stable report key required');
  const data = pdfBytes instanceof Uint8Array ? pdfBytes : new Uint8Array(pdfBytes || []);
  if (data.length < 5 || data.length > MAX_PDF_BYTES ||
      new TextDecoder().decode(data.subarray(0, 5)) !== '%PDF-') {
    throw new Error('Invalid PDF bytes');
  }
  if (!lookup?.ok || lookup.deal_match_status !== 'SINGLE_EXIO_SELLERS_DEAL' ||
      String(lookup.deal_pipeline_id) !== EXIO_SELLERS_PIPELINE_ID ||
      !lookup.contact?.id || lookup.deals?.length !== 1 || !lookup.deals[0]?.id) {
    throw new Error('Unique Exio Sellers contact and deal required');
  }

  const dealId = String(lookup.deals[0].id);
  const base = salesBase(baseUrl);
  const headers = { Authorization: `Token token=${apiKey}` };
  const detailResponse = await fetchImpl(`${base}/api/deals/${encodeURIComponent(dealId)}`, { headers });
  if (!detailResponse.ok) throw new Error(`Deal verification failed: ${detailResponse.status}`);
  const detail = (await detailResponse.json()).deal;
  if (String(detail?.id) !== dealId || String(detail?.deal_pipeline_id) !== EXIO_SELLERS_PIPELINE_ID) {
    throw new Error('Deal changed or no longer in Exio Sellers');
  }

  const associationsPath = detail?.links?.document_associations;
  if (!/^\/crm\/sales\/deals\/\d+\/document_associations(?:\?.*)?$/.test(associationsPath || '') ||
      !associationsPath.includes(`/deals/${dealId}/`)) {
    throw new Error('Deal file-list path unavailable');
  }
  const filename = `Scoutbot-precall-${reportKey}.pdf`;
  const listUrl = `${new URL(base).origin}${associationsPath}`;
  const listResponse = await fetchImpl(listUrl, { headers });
  if (!listResponse.ok) throw new Error(`Deal file list failed: ${listResponse.status}`);
  const listed = await listResponse.json();
  if (!Array.isArray(listed.documents)) throw new Error('Deal file list shape unknown');
  const existing = listed.documents.find(item => item.name === filename);
  if (existing) return { status: 'ALREADY_ATTACHED', deal_id: dealId, document_id: existing.id };

  const form = new FormData();
  form.set('file', new Blob([data], { type: 'application/pdf' }), filename);
  form.set('file_name', filename);
  form.set('is_shared', 'false');
  form.set('targetable_id', dealId);
  form.set('targetable_type', 'Deal');
  const response = await fetchImpl(`${base}/api/documents`, { method: 'POST', headers, body: form });
  if (!response.ok) throw new Error(`Freshsales upload failed: ${response.status}`);
  const uploaded = await response.json();
  if (!uploaded?.id) throw new Error('Freshsales upload response lacks document ID');
  return { status: 'ATTACHED', deal_id: dealId, document_id: uploaded.id, filename };
}
