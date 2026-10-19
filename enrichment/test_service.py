import json,threading,tempfile,urllib.request,urllib.error,unittest,sqlite3
from pathlib import Path
import service
from worker import Store
class ServiceTests(unittest.TestCase):
 def setUp(self):
  self.tmp=tempfile.TemporaryDirectory();service.DB=str(Path(self.tmp.name)/'worker.sqlite');service.TOKEN='test-only-token'
  self.server=service.HTTPServer(('127.0.0.1',0),service.Handler);self.thread=threading.Thread(target=self.server.serve_forever,daemon=True);self.thread.start();self.base='http://127.0.0.1:'+str(self.server.server_port)
 def tearDown(self):self.server.shutdown();self.server.server_close();self.thread.join();self.tmp.cleanup()
 def request(self,path,body=None,auth=True):
  h={'Authorization':'Bearer test-only-token'} if auth else {}
  req=urllib.request.Request(self.base+path,data=body,headers=h)
  try:
   with urllib.request.urlopen(req) as r:return r.status,json.load(r)
  except urllib.error.HTTPError as e:return e.code,json.load(e)
 def test_health_contains_no_data(self):self.assertEqual(self.request('/health',auth=False),(200,{'ok':True}))
 def test_status_requires_auth(self):self.assertEqual(self.request('/status',auth=False)[0],401)
 def test_no_checkpoint_status(self):self.assertEqual(self.request('/status')[1]['state'],'awaiting_checkpoint')
 def test_invalid_database_rejected(self):self.assertEqual(self.request('/bootstrap',b'invalid sqlite')[0],409);self.assertFalse(Path(service.DB).exists())
 def test_mutations_require_checkpoint(self):self.assertEqual(self.request('/apply',b'{}')[0],409)
 def test_bootstrap_is_one_time(self):
  path=str(Path(self.tmp.name)/'source.sqlite');s=Store(path)
  with s.db:s.db.execute('INSERT INTO contacts VALUES (?,?,?,?)',('1','example.com','pending','{}'));s.db.execute('INSERT INTO meta VALUES (?,?)',('checkpoint_sha256','test-digest'))
  s.db.execute('PRAGMA wal_checkpoint(TRUNCATE)');s.db.close();data=Path(path).read_bytes()
  self.assertEqual(self.request('/bootstrap',data),(201,{'restored_contacts':1}));self.assertEqual(self.request('/bootstrap',data)[0],409)
if __name__=='__main__':unittest.main()
