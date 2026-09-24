import test from 'node:test';
import assert from 'node:assert/strict';
import { correlateCompletedMeeting } from './meeting-match.mjs';

const zoom = { uuid: 'zoom-uuid', endedAt: '2026-09-24T20:00:00Z', hostEmail: 'roald@example.com' };
const lookup = email => ({
  ok: true, email, contact: { id: 123, name: 'Pat Example', email: 'pat@old.example' },
  deals: [{ id: 456, name: 'Example Deal' }],
  deal_pipeline_id: 3000017890, deal_match_status: 'SINGLE_EXIO_SELLERS_DEAL'
});

test('two invite addresses for one contact and deal resolve once after Zoom ends', () => {
  const result = correlateCompletedMeeting({
    zoom, attendeeEmails: ['pat@old.example', 'pat@new.example'],
    lookups: [lookup('pat@new.example'), lookup('pat@old.example')]
  });
  assert.equal(result.status, 'MATCHED_COMPLETED_ZOOM');
  assert.equal(result.contact.id, 123);
  assert.equal(result.deal_id, 456);
  assert.deepEqual(result.attendee_emails, ['pat@old.example', 'pat@new.example']);
});

test('meeting must have ended in Zoom', () => {
  assert.equal(correlateCompletedMeeting({
    zoom: { uuid: 'zoom-uuid', hostEmail: 'roald@example.com' },
    attendeeEmails: ['pat@old.example'], lookups: [lookup('pat@old.example')]
  }).status, 'AWAITING_COMPLETED_ZOOM');
});

test('different contacts, missing attendee, or multiple deals stop for review', () => {
  const first = lookup('pat@old.example');
  const second = lookup('pat@new.example');
  assert.equal(correlateCompletedMeeting({ zoom, attendeeEmails: [first.email, second.email],
    lookups: [first, { ...second, contact: { id: 999 } }] }).status, 'CONTACT_REVIEW');
  assert.equal(correlateCompletedMeeting({ zoom, attendeeEmails: [first.email, second.email],
    lookups: [first] }).status, 'ATTENDEE_REVIEW');
  assert.equal(correlateCompletedMeeting({ zoom, attendeeEmails: [first.email],
    lookups: [{ ...first, deals: [{ id: 456 }, { id: 789 }] }] }).status, 'DEAL_REVIEW');
});
