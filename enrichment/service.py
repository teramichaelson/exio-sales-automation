"""Private admin surface for an isolated enrichment worker; all data routes authenticated."""
import hmac,json,os,sqlite3,tempfile,threading
from http.server import BaseHTTPRequestHandler,HTTPServer
from pathlib import Path
from worker import Store,CRM,lock,make_proposal,apply,research,fetch_page,Text,domain,now
DB=os.environ.get('ENRICHMENT_DB','/data/enrichment.sqlite')
TOKEN=os.environ.get('ENRICHMENT_ADMIN_TOKEN','')
MAX_UPLOAD=128*1024*1024
class Handler(BaseHTTPRequestHandler):
 def log_message(self,*args):pass
 def send(self,status,value):
  data=json.dumps(value,default=str).encode();self.send_response(status);self.send_header('Content-Type','application/json');self.send_header('Cache-Control','no-store');self.send_header('Content-Length',str(len(data)));self.end_headers();self.wfile.write(data)
 def auth(self):
  supplied=self.headers.get('Authorization','')
  if not TOKEN or not hmac.compare_digest(supplied,'Bearer '+TOKEN):self.send(401,{'error':'Unauthorized'});return False
  return True
 def do_GET(self):
  if self.path=='/health':self.send(200,{'ok':True});return
  if not self.auth():return
  try:
   if self.path=='/status':
    if not Path(DB).exists():self.send(200,{'state':'awaiting_checkpoint'});return
    with lock(DB):self.send(200,Store(DB).summary())
   else:self.send(404,{'error':'Unknown route'})
  except Exception as e:self.send(409,{'error_type':type(e).__name__})
 def do_POST(self):
  if not self.auth():return
  size=int(self.headers.get('Content-Length','0'))
  try:
   Path(DB).parent.mkdir(parents=True,exist_ok=True)
   if self.path=='/bootstrap':
    if not 0<size<=MAX_UPLOAD:self.send(413,{'error':'Upload size limit'});return
    with lock(DB):
     if Path(DB).exists():self.send(409,{'error':'Checkpoint already exists'});return
     fd,name=tempfile.mkstemp(dir=str(Path(DB).parent),suffix='.sqlite')
     try:
      with os.fdopen(fd,'wb') as f:
       remaining=size
       while remaining:
        chunk=self.rfile.read(min(1024*1024,remaining))
        if not chunk:raise ValueError('Incomplete upload')
        f.write(chunk);remaining-=len(chunk)
      c=sqlite3.connect(name)
      if c.execute('PRAGMA integrity_check').fetchone()[0]!='ok':raise ValueError('Invalid checkpoint')
      count=c.execute('SELECT count(*) FROM contacts').fetchone()[0]
      if count<1 or not c.execute("SELECT value FROM meta WHERE key='checkpoint_sha256'").fetchone():raise ValueError('Missing checkpoint metadata')
      c.close();os.replace(name,DB);self.send(201,{'restored_contacts':count})
     finally:
      if Path(name).exists():Path(name).unlink()
    return
   if size>100000:self.send(413,{'error':'Request size limit'});return
   body=json.loads(self.rfile.read(size) or b'{}')
   if not Path(DB).exists():self.send(409,{'error':'Checkpoint not restored'});return
   with lock(DB):
    s=Store(DB)
    if self.path=='/research':
     from worker import BudgetExceeded
     result=research(s,limit=10,requests=60,only_domain=body.get('domain'))
    elif self.path=='/source':
     from urllib.robotparser import RobotFileParser
     d=domain(body['url']);row=s.db.execute('SELECT status,legacy FROM firms WHERE domain=?',(d,)).fetchone()
     if not row or row['status']!='evidence_ready':raise ValueError('Research packet must exist first')
     robots=RobotFileParser();robots.parse(fetch_page('https://'+d+'/robots.txt',d,s).splitlines())
     if not robots.can_fetch('ExioBuyerResearch',body['url']):raise ValueError('Robots disallow')
     t=Text();t.feed(fetch_page(body['url'],d,s));packet=json.loads(row['legacy']);pages=dict(packet.get('pages',[]));pages[body['url']]=t.text();packet['pages']=list(pages.items());packet['retrieved_at']=now()
     with s.db:s.db.execute('UPDATE firms SET legacy=? WHERE domain=?',(json.dumps(packet),d))
     result={'domain':d,'url':body['url'],'text':t.text()}
    elif self.path=='/evidence':
     r=s.db.execute('SELECT status,legacy FROM firms WHERE domain=?',(body['domain'],)).fetchone();result=dict(r) if r else None
    elif self.path=='/inspect':
     crm=CRM(os.environ['FRESHSALES_BASE_URL'],os.environ['FRESHSALES_API_KEY']);c=crm.contact(body['contact_id']);result={'contact':c}
     try:result['notes']=crm.notes(c)
     except Exception as e:result['notes_error']={'type':type(e).__name__,'message':str(e)[:120]}
    elif self.path=='/prepare':
     crm=CRM(os.environ['FRESHSALES_BASE_URL'],os.environ['FRESHSALES_API_KEY']);pid=make_proposal(s,body['contact_id'],body['findings'],crm.schema());result={'proposal_id':pid,'dry_run':apply(s,pid,crm)}
    elif self.path=='/approve':
     with s.db:s.db.execute("UPDATE proposals SET state='approved' WHERE id=? AND state='pending'",(body['proposal_id'],))
     result={'approved':s.db.total_changes}
    elif self.path=='/apply':
     crm=CRM(os.environ['FRESHSALES_BASE_URL'],os.environ['FRESHSALES_API_KEY']);result=apply(s,body['proposal_id'],crm,execute=True)
    else:self.send(404,{'error':'Unknown route'});return
    s.db.close();self.send(200,result)
  except Exception as e:self.send(409,{'error_type':type(e).__name__,'error':str(e)[:160] if isinstance(e,ValueError) else 'Operation stopped; inspect audit before retry'})
if __name__=='__main__':
 if not TOKEN:raise SystemExit('ENRICHMENT_ADMIN_TOKEN is required')
 HTTPServer(('0.0.0.0',int(os.environ.get('PORT','8080'))),Handler).serve_forever()
