import json, os, tempfile, unittest
from unittest.mock import patch
from pathlib import Path
from worker import *

SCHEMA=[{'name':'cf_preferred_minimum_ebitda','type':'dropdown','choices':[{'value':'$5M'}]}]
class FakeCRM:
 def __init__(self):self.c={'id':1,'email':'buyer@example.com','custom_field':{'cf_type':'Buyer','cf_preferred_minimum_ebitda':None}};self.calls=0;self.fail=False;self.saved_notes=[]
 def contact(self,cid):return json.loads(json.dumps(self.c))
 def notes(self,c):return self.saved_notes
 def schema(self):return SCHEMA
 def call(self,method,path,data):
  self.calls+=1
  if method=='PUT':self.c['custom_field'].update(data['contact']['custom_field'])
  elif method=='POST':self.saved_notes.append(data['note'])
  if self.fail:raise TimeoutError('response lost')
  return {'contact':self.c}
class Tests(unittest.TestCase):
 def setUp(self):
  self.t=tempfile.TemporaryDirectory();self.s=Store(str(Path(self.t.name)/'q.sqlite'))
  raw={'Email IDs':'buyer@example.com'};page='We acquire companies with minimum EBITDA of $5 million.'
  self.s.db.execute('INSERT INTO contacts VALUES (?,?,?,?)',('1','example.com','pending',json.dumps(raw)))
  self.s.db.execute('INSERT INTO firms(domain,status,legacy) VALUES (?,?,?)',('example.com','evidence_ready',json.dumps({'pages':[['https://example.com/criteria',page]]})));self.s.db.commit()
  self.finding={'key':'minimum_ebitda','value':'$5M','url':'https://example.com/criteria','quote':page,'scope':'firm'}
 def tearDown(self):self.s.db.close();self.t.cleanup()
 def proposal(self):return make_proposal(self.s,'1',[self.finding],SCHEMA)
 def test_domains(self):
  for v in ['name@gmail.com','localhost','127.0.0.1','https://user:pass@example.com','http://example.com:123']:self.assertIsNone(domain(v))
  self.assertEqual(domain('HTTPS://WWW.Example.COM/path'),'example.com')
 def test_holds(self):
  self.s.db.execute("UPDATE contacts SET status='identity_hold'")
  with self.assertRaises(ValueError):self.proposal()
 def test_evidence(self):
  self.finding['quote']='fabricated EBITDA evidence'
  with self.assertRaises(ValueError):self.proposal()
 def test_options(self):
  self.finding['value']='$99M'
  with self.assertRaises(ValueError):self.proposal()
 def test_no_ebitda_conversion(self):
  self.finding['quote']='We invest $5 million in each transaction.'
  self.s.db.execute('UPDATE firms SET legacy=?',(json.dumps({'pages':[['https://example.com/criteria',self.finding['quote']]]}),))
  with self.assertRaises(ValueError):self.proposal()
 def test_proposal_dedup(self):self.assertEqual(self.proposal(),self.proposal());self.assertEqual(self.s.db.execute('SELECT count(*) FROM proposals').fetchone()[0],1)
 def test_preserve_existing(self):
  p=json.loads(self.s.db.execute('SELECT payload FROM proposals WHERE id=?',(self.proposal(),)).fetchone()[0]);self.assertEqual(plan_fields({'id':1,'custom_field':{'cf_preferred_minimum_ebitda':'$3M'}},p),{})
 def test_dry_run(self):
  crm=FakeCRM();result=apply(self.s,self.proposal(),crm);self.assertEqual(result['state'],'dry_run');self.assertEqual(crm.calls,0)
 def test_live_disabled(self):
  with patch.dict(os.environ,{'ENRICHMENT_WRITES':''}):
   with self.assertRaises(ValueError):apply(self.s,self.proposal(),FakeCRM(),True)
 def test_apply_and_rerun(self):
  pid=self.proposal();crm=FakeCRM();self.s.db.execute("UPDATE proposals SET state='approved'");self.s.db.commit()
  with patch.dict(os.environ,{'ENRICHMENT_WRITES':'enabled'}):
   self.assertEqual(apply(self.s,pid,crm,True)['state'],'verified');self.assertTrue(apply(self.s,pid,crm,True)['skipped']);self.assertEqual(crm.calls,2)
 def test_timeout_does_not_retry(self):
  pid=self.proposal();crm=FakeCRM();crm.fail=True;self.s.db.execute("UPDATE proposals SET state='approved'");self.s.db.commit()
  with patch.dict(os.environ,{'ENRICHMENT_WRITES':'enabled'}):
   with self.assertRaises(TimeoutError):apply(self.s,pid,crm,True)
   self.assertEqual(apply(self.s,pid,crm,True)['state'],'uncertain_review');self.assertEqual(crm.calls,1)
 def test_budget_persists(self):
  self.s.charge('domains',1,1)
  other=Store(str(Path(self.t.name)/'q.sqlite'))
  with self.assertRaises(BudgetExceeded):other.charge('domains',1,1)
  other.db.close()
 def test_no_unsafe_tenant(self):
  with self.assertRaises(ValueError):CRM('https://evil.example','secret')
 def test_live_identity_change(self):
  crm=FakeCRM();crm.c['email']='someone@other.com'
  with self.assertRaises(ValueError):apply(self.s,self.proposal(),crm)
if __name__=='__main__':unittest.main()

class AITests(unittest.TestCase):
 setUp=Tests.setUp
 tearDown=Tests.tearDown
 def test_ai_cache_and_budget(self):
  from extract import extract
  count=[]
  def fake(req):
   count.append(1)
   return {'status':'completed','output':[{'content':[{'type':'output_text','text':json.dumps({'findings':[self.finding]})}]}]}
  with patch.dict(os.environ,{'ENRICHMENT_AI_MODEL':'configured-test-model','OPENAI_API_KEY':'test-not-real'}):
   self.assertEqual(extract(self.s,'example.com',SCHEMA,fake),extract(self.s,'example.com',SCHEMA,fake));self.assertEqual(len(count),1)
 def test_ai_not_configured(self):
  from extract import extract
  with patch.dict(os.environ,{'OPENAI_API_KEY':'','ENRICHMENT_AI_MODEL':''}):
   with self.assertRaises(ValueError):extract(self.s,'example.com',SCHEMA)
 def test_ai_uncertain_not_retried(self):
  from extract import extract
  def fail(req):raise TimeoutError()
  with patch.dict(os.environ,{'ENRICHMENT_AI_MODEL':'configured-test-model','OPENAI_API_KEY':'test-not-real'}):
   with self.assertRaises(TimeoutError):extract(self.s,'example.com',SCHEMA,fail)
   with self.assertRaises(ValueError):extract(self.s,'example.com',SCHEMA,fail)

class ReportTests(unittest.TestCase):
 setUp=Tests.setUp
 tearDown=Tests.tearDown
 proposal=Tests.proposal
 def test_weekly_outbox_dedup(self):
  from run import report
  self.assertTrue(report(self.s)['queued']);self.assertFalse(report(self.s)['queued'])
 def test_old_reviewed_contact_is_not_reprocessed(self):
  self.s.db.execute("UPDATE contacts SET status='reviewed'")
  with self.assertRaises(ValueError):self.proposal()

class SchemaTests(unittest.TestCase):
 def test_schema_hash_ignores_display_metadata(self):
  a=[{'name':'cf_preferred_minimum_ebitda','type':'dropdown','position':1,'choices':[{'id':1,'value':'$5M','position':1}]}]
  b=[{'name':'cf_preferred_minimum_ebitda','type':'dropdown','position':9,'choices':[{'id':9876,'value':'$5M','position':2}]}]
  self.assertEqual(schema_hash(a),schema_hash(b))
  b[0]['choices'][0]['value']='$6M';self.assertNotEqual(schema_hash(a),schema_hash(b))

class MultiSelectTests(unittest.TestCase):
 def test_wire_encoding_and_readback(self):
  self.assertEqual(serialize_fields({'x':['A','B']}),{'x':'A;B'})
  self.assertTrue(field_matches('B;A',['A','B']))
  self.assertFalse(field_matches(None,['A']))
  self.assertFalse(field_matches('A;C',['A','B']))

class RecoveryTests(unittest.TestCase):
 setUp=Tests.setUp
 tearDown=Tests.tearDown
 proposal=Tests.proposal
 def test_repair_never_repeats_note(self):
  pid=self.proposal();crm=FakeCRM();crm.c['custom_field']['cf_preferred_minimum_ebitda']=None
  crm.saved_notes=[{'description':'[Exio enrichment '+pid+'] source evidence'}]
  self.s.db.execute('INSERT INTO writes VALUES (?,?,?,?,?)',(pid,'uncertain_review',json.dumps(crm.c),None,now()));self.s.db.commit()
  with patch.dict(os.environ,{'ENRICHMENT_WRITES':'enabled'}):result=repair_fields(self.s,pid,crm)
  self.assertEqual(result['state'],'verified');self.assertEqual(len(crm.saved_notes),1);self.assertEqual(crm.calls,1)
 def test_repair_preserves_newer_value(self):
  pid=self.proposal();crm=FakeCRM();before=json.dumps(crm.c);crm.c['custom_field']['cf_preferred_minimum_ebitda']='$3M';crm.saved_notes=[{'description':'[Exio enrichment '+pid+'] source'}]
  self.s.db.execute('INSERT INTO writes VALUES (?,?,?,?,?)',(pid,'uncertain_review',before,None,now()));self.s.db.commit()
  with patch.dict(os.environ,{'ENRICHMENT_WRITES':'enabled'}):
   with self.assertRaises(ValueError):repair_fields(self.s,pid,crm)
  self.assertEqual(crm.calls,0)
