"""Private admin surface for an isolated enrichment worker; all data routes authenticated."""
import hmac,json,os,sqlite3,tempfile,threading
from http.server import BaseHTTPRequestHandler,HTTPServer
from pathlib import Path
from worker import Store,CRM,lock,make_proposal,apply,research,fetch_page,Text,domain,now,repair_fields
DB=os.environ.get('ENRICHMENT_DB','/data/enrichment.sqlite')
JOB_THREAD=None
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
  global JOB_THREAD
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
   if self.path=='/tick':
    if JOB_THREAD and JOB_THREAD.is_alive():self.send(200,{'state':'already_running'});return
    if not Path(DB).exists():self.send(409,{'error':'Checkpoint not restored'});return
    def background():
     from run import tick
     try:
      with lock(DB):
       s=Store(DB)
       with s.db:s.db.execute('INSERT OR REPLACE INTO meta VALUES (?,?)',('last_job',json.dumps({'state':'running','started_at':now()})))
       enabled=bool(os.environ.get('OPENAI_API_KEY') and os.environ.get('ENRICHMENT_AI_MODEL') and os.environ.get('ENRICHMENT_AI_ENABLED')=='enabled')
       result=tick(s,do_research=True,do_ai=enabled,do_write=os.environ.get('ENRICHMENT_WRITES')=='enabled')
       with s.db:s.db.execute('INSERT OR REPLACE INTO meta VALUES (?,?)',('last_job',json.dumps({'state':'finished','finished_at':now(),'ai_enabled':enabled,'result':result})))
       s.db.close()
     except Exception as e:
      s=Store(DB)
      with s.db:s.db.execute('INSERT OR REPLACE INTO meta VALUES (?,?)',('last_job',json.dumps({'state':'stopped','at':now(),'error_type':type(e).__name__})))
      s.db.close()
    JOB_THREAD=threading.Thread(target=background,daemon=True);JOB_THREAD.start();self.send(202,{'state':'started'});return
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
    elif self.path=='/seat-batch':
     from seat import batch
     result=batch(s,body.get('limit',5))
    elif self.path=='/seat-review':
     from seat import finish
     crm=CRM(os.environ['FRESHSALES_BASE_URL'],os.environ['FRESHSALES_API_KEY']) if body.get('outcome')=='criteria' else None
     result=finish(s,body,crm)
    elif self.path=='/seat-schema':
     from worker import FIELDS
     crm=CRM(os.environ['FRESHSALES_BASE_URL'],os.environ['FRESHSALES_API_KEY']);result=[{'name':f['name'],'type':f.get('type'),'choices':[{'value':c['value']} for c in f.get('choices',[])]} for f in crm.schema() if f['name'] in FIELDS.values()]
    elif self.path=='/extract':
     from extract import prepare_domain
     crm=CRM(os.environ['FRESHSALES_BASE_URL'],os.environ['FRESHSALES_API_KEY']);result=prepare_domain(s,body['domain'],crm.schema(),review=True)
    elif self.path=='/proposals':
     result=[dict(r) for r in s.db.execute("SELECT id,contact_id,domain,payload,state,reason FROM proposals WHERE state IN ('pending','approved') LIMIT 25")]
    elif self.path=='/ai-usage':
     result=[dict(r) for r in s.db.execute('SELECT * FROM ai_usage ORDER BY at DESC LIMIT 20')]
    elif self.path=='/prepare':
     crm=CRM(os.environ['FRESHSALES_BASE_URL'],os.environ['FRESHSALES_API_KEY']);pid=make_proposal(s,body['contact_id'],body['findings'],crm.schema());result={'proposal_id':pid,'dry_run':apply(s,pid,crm)}
    elif self.path=='/approve':
     with s.db:s.db.execute("UPDATE proposals SET state='approved' WHERE id=? AND state='pending'",(body['proposal_id'],))
     result={'approved':s.db.total_changes}
    elif self.path=='/repair-fields':
     crm=CRM(os.environ['FRESHSALES_BASE_URL'],os.environ['FRESHSALES_API_KEY']);result=repair_fields(s,body['proposal_id'],crm)
    elif self.path=='/apply':
     crm=CRM(os.environ['FRESHSALES_BASE_URL'],os.environ['FRESHSALES_API_KEY']);result=apply(s,body['proposal_id'],crm,execute=True)
    else:self.send(404,{'error':'Unknown route'});return
    s.db.close();self.send(200,result)
  except Exception as e:self.send(409,{'error_type':type(e).__name__,'error':str(e)[:160] if isinstance(e,ValueError) else 'Operation stopped; inspect audit before retry'})
if __name__=='__main__':
 if not TOKEN:raise SystemExit('ENRICHMENT_ADMIN_TOKEN is required')
 HTTPServer(('0.0.0.0',int(os.environ.get('PORT','8080'))),Handler).serve_forever()
