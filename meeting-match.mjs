const EXIO_SELLERS_PIPELINE_ID = '3000017890';

const normalize = value => String(value || '').trim().toLowerCase();

/** Read-only decision for a completed Zoom meeting and CRM lookup results. */
export function correlateCompletedMeeting({ zoom, attendeeEmails, lookups }) {
  if (!zoom?.uuid || !zoom?.endedAt || !zoom?.hostEmail) {
    return { status: 'AWAITING_COMPLETED_ZOOM' };
  }

  const attendees = [...new Set((attendeeEmails || []).map(normalize).filter(Boolean))];
  if (!attendees.length) return { status: 'ATTENDEE_REVIEW', reason: 'No attendee email' };

  const byEmail = new Map();
  for (const lookup of lookups || []) {
    const email = normalize(lookup?.email);
    if (!attendees.includes(email) || byEmail.has(email)) {
      return { status: 'ATTENDEE_REVIEW', reason: 'Lookup set does not match attendee set' };
    }
    byEmail.set(email, lookup);
  }
  if (byEmail.size !== attendees.length) {
    return { status: 'ATTENDEE_REVIEW', reason: 'An attendee has no lookup result' };
  }

  const results = attendees.map(email => byEmail.get(email));
  if (results.some(result => !result?.contact?.id || !result?.ok)) {
    return { status: 'ATTENDEE_REVIEW', reason: 'An attendee was not uniquely verified' };
  }
  const contactIds = new Set(results.map(result => String(result.contact.id)));
  if (contactIds.size !== 1) {
    return { status: 'CONTACT_REVIEW', reason: 'Attendees resolve to different contacts' };
  }

  const dealIds = new Set(results.flatMap(result =>
    Array.isArray(result.deals) ? result.deals.map(deal => String(deal.id)) : []));
  if (dealIds.size !== 1 || results.some(result =>
    result.deal_match_status !== 'SINGLE_EXIO_SELLERS_DEAL' ||
    String(result.deal_pipeline_id) !== EXIO_SELLERS_PIPELINE_ID ||
    result.deals?.length !== 1)) {
    return { status: 'DEAL_REVIEW', reason: 'No single verified Exio Sellers deal' };
  }

  const contact = results[0].contact;
  return {
    status: 'MATCHED_COMPLETED_ZOOM',
    report_status: 'READY_TO_DRAFT',
    zoom_uuid: zoom.uuid,
    zoom_host_email: normalize(zoom.hostEmail),
    contact: { id: contact.id, name: contact.name, email: contact.email },
    attendee_emails: attendees,
    deal_id: results[0].deals[0].id
  };
}
