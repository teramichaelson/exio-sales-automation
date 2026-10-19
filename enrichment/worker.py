#!/usr/bin/env python3
"""Bounded enrichment queue. Python 3.11+, no dependencies for normal operation."""
import argparse, collections, contextlib, datetime as dt, hashlib, html, ipaddress
import json, os, re, socket, sqlite3, time, urllib.parse, urllib.request, urllib.error
from html.parser import HTMLParser
from pathlib import Path

VERSION = '1'
FREE = {'gmail.com','googlemail.com','yahoo.com','hotmail.com','outlook.com','live.com','aol.com','icloud.com','me.com','msn.com','comcast.net','proton.me','protonmail.com','sbcglobal.net','att.net','verizon.net','mail.com'}
FIELDS = {'minimum_ebitda':'cf_preferred_minimum_ebitda','industries':'cf_preferred_industries','regions':'cf_preferred_regions','website':'cf_website'}
TERMINAL = {'reviewed','unavailable','identity_hold'}

def now(): return dt.datetime.now(dt.timezone.utc).isoformat()
def schema_hash(schema):
    canonical=[{'name':f['name'],'type':f.get('type'),'choices':sorted([{'id':c.get('id'),'value':c.get('value')} for c in f.get('choices',[])],key=lambda c:str(c['value']))} for f in schema if f.get('name') in FIELDS.values()]
    return digest(sorted(canonical,key=lambda f:f['name']))
def digest(v): return hashlib.sha256(json.dumps(v,sort_keys=True,default=str).encode()).hexdigest()
def domain(v):
    v = str(v or '').strip().lower()
    if not v: return None
    if '@' in v and '://' not in v: v=v.rsplit('@',1)[1]
    try:
        u=urllib.parse.urlsplit(v if '://' in v else 'https://'+v)
        if u.username or u.password or u.port not in (None,80,443): return None
        h=(u.hostname or '').encode('idna').decode().removeprefix('www.')
        if '.' not in h or h in FREE or not re.fullmatch(r'[a-z0-9.-]+',h): return None
        try: ipaddress.ip_address(h); return None
        except ValueError: return h
    except (ValueError,UnicodeError): return None

class Store:
    def __init__(self,path):
        self.db=sqlite3.connect(path,timeout=5); self.db.row_factory=sqlite3.Row
        self.db.executescript('''PRAGMA journal_mode=WAL;
        CREATE TABLE IF NOT EXISTS meta(key TEXT PRIMARY KEY,value TEXT);
        CREATE TABLE IF NOT EXISTS contacts(id TEXT PRIMARY KEY,domain TEXT,status TEXT,raw TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS firms(domain TEXT PRIMARY KEY,status TEXT NOT NULL,legacy TEXT,attempts INTEGER DEFAULT 0,next_try REAL DEFAULT 0);
        CREATE TABLE IF NOT EXISTS pages(url TEXT PRIMARY KEY,domain TEXT,text TEXT,sha TEXT,at TEXT);
        CREATE TABLE IF NOT EXISTS proposals(id TEXT PRIMARY KEY,contact_id TEXT,domain TEXT,payload TEXT,state TEXT DEFAULT 'pending',reason TEXT);
        CREATE TABLE IF NOT EXISTS writes(id TEXT PRIMARY KEY,state TEXT,before_json TEXT,after_json TEXT,at TEXT);
        CREATE TABLE IF NOT EXISTS ai_runs(id TEXT PRIMARY KEY,state TEXT,output TEXT);
        CREATE TABLE IF NOT EXISTS reports(id TEXT PRIMARY KEY,at TEXT,kind TEXT,payload TEXT,state TEXT);
        CREATE TABLE IF NOT EXISTS legacy_rows(sheet TEXT,row_index INTEGER,payload TEXT,PRIMARY KEY(sheet,row_index));
        CREATE TABLE IF NOT EXISTS usage(day TEXT PRIMARY KEY,domains INTEGER DEFAULT 0,requests INTEGER DEFAULT 0,ai_calls INTEGER DEFAULT 0,ai_input_bytes INTEGER DEFAULT 0,ai_output_bytes INTEGER DEFAULT 0);
        '''); self.db.commit()
    def summary(self):
        def c(table,col): return dict(self.db.execute(f'SELECT {col},count(*) FROM {table} GROUP BY {col}').fetchall())
        return {'contacts':self.db.execute('SELECT count(*) FROM contacts').fetchone()[0], 'contact_status':c('contacts','status'),'firm_status':c('firms','status'),'proposal_status':c('proposals','state'),'write_status':c('writes','state'),'usage':[dict(r) for r in self.db.execute('SELECT * FROM usage')], 'criteria_flags':dict(collections.Counter(type(x).__name__+':'+str(x) for r in self.db.execute('SELECT raw FROM contacts') for x in [json.loads(r[0]).get('coverage',{}).get('In criteria enrichment count')])), 'checkpoint':dict(self.db.execute('SELECT key,value FROM meta'))}
    def charge(self,col,amount,limit):
        assert col in {'domains','requests','ai_calls','ai_input_bytes','ai_output_bytes'}
        day=now()[:10]
        with self.db:
            self.db.execute('INSERT OR IGNORE INTO usage(day) VALUES (?)',(day,))
            used=self.db.execute(f'SELECT {col} FROM usage WHERE day=?',(day,)).fetchone()[0]
            if used+amount>limit: raise BudgetExceeded(col)
            self.db.execute(f'UPDATE usage SET {col}={col}+? WHERE day=?',(amount,day))

class BudgetExceeded(Exception): pass

@contextlib.contextmanager
def lock(path):
    import fcntl
    f=open(str(path)+'.lock','a')
    try:
        fcntl.flock(f,fcntl.LOCK_EX|fcntl.LOCK_NB); yield
    finally: f.close()

def migrate(store,workbook):
    # Only migration needs openpyxl; the original workbook is never changed.
    import openpyxl
    sha=hashlib.sha256(Path(workbook).read_bytes()).hexdigest()
    previous=store.db.execute("SELECT value FROM meta WHERE key='checkpoint_sha256'").fetchone()
    if previous:
        if previous[0]!=sha: raise ValueError('Different checkpoint: use a new database and reconcile')
        return store.summary()
    w=openpyxl.load_workbook(workbook,read_only=True,data_only=True)
    def records(name):
        rows=iter(w[name].values)
        for _ in range(3): next(rows)
        headers=next(rows)
        for row in rows:
            if row[0] is not None: yield dict(zip(headers,row))
    export={str(r['ID']):r for r in records('Buyer export')}
    coverage={str(r['Contact ID']):r for r in records('Record coverage')}
    if set(export)!=set(coverage) or not export: raise ValueError('Checkpoint inventory does not reconcile')
    reviewed_domains={}
    for r in records('Website queue'):
        d=domain(r['Domain'])
        if d: reviewed_domains[d]=r
    with store.db:
        for cid,r in coverage.items():
            raw={**export[cid],'coverage':r}; d=domain(r['Attributed domain'])
            state={'Pending firm research':'pending','Reviewed firm source':'reviewed','No unambiguous firm domain':'identity_hold','Unresolved firm affiliation':'identity_hold','Unavailable firm source':'unavailable','Source unavailable':'unavailable'}.get(r['Research status'],'identity_hold')
            store.db.execute('INSERT INTO contacts VALUES (?,?,?,?)',(cid,d,state,json.dumps(raw,default=str)))
        for d,r in reviewed_domains.items():
            s=str(r.get('Research status') or '').lower()
            state='reviewed' if s=='reviewed' else 'unavailable' if 'unavailable' in s else 'pending'
            store.db.execute('INSERT OR IGNORE INTO firms(domain,status,legacy) VALUES (?,?,?)',(d,state,json.dumps(r,default=str)))
        # Reviewed source at contact level prevents duplicate firm research even if queue is stale.
        for d,state in store.db.execute('SELECT DISTINCT domain,status FROM contacts WHERE domain IS NOT NULL ORDER BY CASE status WHEN \'reviewed\' THEN 0 ELSE 1 END').fetchall():
            store.db.execute('INSERT OR IGNORE INTO firms(domain,status) VALUES (?,?)',(d,state if state in TERMINAL else 'pending'))
            if state=='reviewed': store.db.execute("UPDATE firms SET status='reviewed' WHERE domain=?",(d,))
        for sheet in ['Firm research','Criteria enrichment','CRM updates','Import exceptions','Progress']:
            for i,r in enumerate(records(sheet)):
                store.db.execute('INSERT INTO legacy_rows VALUES (?,?,?)',(sheet,i,json.dumps(r,default=str)))
        store.db.execute('INSERT INTO meta VALUES (?,?)',('checkpoint_sha256',sha))
        store.db.execute('INSERT INTO meta VALUES (?,?)',('migrated_at',now()))
        store.db.execute('INSERT INTO meta VALUES (?,?)',('checkpoint_version','118'))
    w.close(); return store.summary()

class Text(HTMLParser):
    def __init__(self): super().__init__(); self.parts=[];self.skip=0;self.links=[]
    def handle_starttag(self,tag,attrs):
        if tag in ('script','style','noscript','svg'): self.skip+=1
        if tag=='a':
            a=dict(attrs)
            if a.get('href'): self.links.append(a['href'])
        if tag in ('p','div','li','h1','h2','h3','br'): self.parts.append('\n')
    def handle_endtag(self,tag):
        if tag in ('script','style','noscript','svg'):self.skip=max(0,self.skip-1)
    def handle_data(self,data):
        if not self.skip:self.parts.append(data)
    def text(self):return '\n'.join(re.sub(r'\s+',' ',x).strip() for x in ''.join(self.parts).splitlines() if x.strip())

class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self,*a,**k): return None

def public_url(url,expected):
    u=urllib.parse.urlsplit(url)
    if u.scheme!='https' or domain(url)!=expected or u.username or u.password or u.port not in (None,443): raise ValueError('Unapproved source host')
    ips={x[4][0] for x in socket.getaddrinfo(u.hostname,443,type=socket.SOCK_STREAM)}
    if not ips or any(not ipaddress.ip_address(x).is_global for x in ips):raise ValueError('Nonpublic source')

def fetch_page(url,expected,store,request_limit=60):
    cached=store.db.execute('SELECT text FROM pages WHERE url=?',(url,)).fetchone()
    if cached:return cached[0]
    opener=urllib.request.build_opener(NoRedirect)
    for hop in range(4):
        public_url(url,expected);store.charge('requests',1,request_limit)
        req=urllib.request.Request(url,headers={'User-Agent':'ExioBuyerResearch/1.0 (+https://goexio.com)'})
        try:
            with opener.open(req,timeout=15) as r:
                if 'text/' not in r.headers.get('Content-Type',''):raise ValueError('Non-text source requires review')
                content=r.read(512001)
                if len(content)>512000:raise ValueError('Source too large')
                text=content.decode('utf-8',errors='replace')
                with store.db:store.db.execute('INSERT OR REPLACE INTO pages VALUES (?,?,?,?,?)',(url,expected,text,digest(text),now()))
                return text
        except urllib.error.HTTPError as e:
            if e.code in (301,302,303,307,308) and e.headers.get('Location'):
                url=urllib.parse.urljoin(url,e.headers['Location']);continue
            raise
    raise ValueError('Redirect limit')

def evidence_packet(pages):
    # Candidates are for interpretation, never treated as verified investment preferences.
    lines=[]
    for url,text in pages:
        for line in text.splitlines():
            if re.search(r'EBITDA|acquis|investment criteria|revenue|geograph|target|invest in',line,re.I):
                lines.append({'url':url,'quote':line[:1600]})
    return lines[:30]

def research(store,limit=10,requests=60,only_domain=None):
    from urllib.robotparser import RobotFileParser
    output=[]
    firms=store.db.execute("SELECT domain FROM firms WHERE EXISTS (SELECT 1 FROM contacts c WHERE c.domain=firms.domain AND c.status='pending') AND status IN ('pending','retry') AND attempts<3 AND next_try<=? ORDER BY (SELECT count(*) FROM contacts c WHERE c.domain=firms.domain) DESC,domain LIMIT ?",(time.time(),limit)).fetchall()
    if only_domain:
        firms=store.db.execute("SELECT domain FROM firms WHERE domain=? AND status IN ('pending','retry') AND attempts<3 AND next_try<=?",(only_domain,time.time())).fetchall()
    for row in firms:
        d=row[0];store.charge('domains',1,limit);pages=[]
        try:
            root='https://'+d+'/'
            robots=RobotFileParser()
            try: robots.parse(fetch_page(root+'robots.txt',d,store,requests).splitlines())
            except urllib.error.HTTPError as e:
                if e.code==404: robots.parse([])
                else: raise
            if not robots.can_fetch('ExioBuyerResearch',root):raise ValueError('Robots disallow')
            text=fetch_page(root,d,store,requests);p=Text();p.feed(text);pages.append((root,p.text()))
            links=sorted({urllib.parse.urljoin(root,x).split('#')[0] for x in p.links if re.search(r'criteria|investment|approach|strategy|acquisition',x,re.I)})
            for url in links[:2]:
                if domain(url)!=d or not robots.can_fetch('ExioBuyerResearch',url):continue
                time.sleep(1)
                h=Text();h.feed(fetch_page(url,d,store,requests));pages.append((url,h.text()))
            packet=evidence_packet(pages)
            with store.db:store.db.execute("UPDATE firms SET status='evidence_ready',legacy=?,attempts=attempts+1 WHERE domain=?",(json.dumps({'pages':pages,'candidates':packet,'retrieved_at':now()}),d))
            output.append({'domain':d,'pages':len(pages),'candidates':len(packet)})
        except BudgetExceeded: raise
        except Exception as e:
            permanent=isinstance(e,ValueError) or (isinstance(e,urllib.error.HTTPError) and e.code in (401,403,404,410))
            with store.db:store.db.execute('UPDATE firms SET status=?,attempts=attempts+1,next_try=? WHERE domain=?',('unavailable' if permanent else 'retry',time.time()+3600,d))
            output.append({'domain':d,'state':'unavailable' if permanent else 'retry','error_type':type(e).__name__})
    return output

def make_proposal(store,contact_id,findings,schema):
    row=store.db.execute('SELECT * FROM contacts WHERE id=?',(str(contact_id),)).fetchone()
    if not row or row['status']!='pending':raise ValueError('Contact is not in pending research; preserve checkpoint status')
    d=row['domain']; firm=store.db.execute('SELECT * FROM firms WHERE domain=?',(d,)).fetchone()
    packet=json.loads(firm['legacy'] or '{}') if firm else {}
    pages=dict(packet.get('pages',[]))
    # Affiliation is explicit: exact business email domain OR separately reviewed identity evidence.
    raw=json.loads(row['raw']);email_text=str(raw.get('Email IDs') or raw.get('Work email') or '')
    emails=re.findall(r'[\w.+-]+@[\w.-]+',email_text)
    if not any(domain(e)==d for e in emails):raise ValueError('Affiliation needs manual identity review')
    validated=[]
    by_name={f['name']:f for f in schema}
    for f in findings:
        if set(f)!={'key','value','url','quote','scope'}:raise ValueError('Unexpected finding shape')
        if f['scope']!='firm':raise ValueError('Only clearly labeled firm criteria allowed')
        if f['url'] not in pages or len(f['quote'])<15 or f['quote'] not in pages[f['url']]:raise ValueError('Missing exact source evidence')
        key=f['key']
        if key not in {*FIELDS,'revenue','detail'}:raise ValueError('Unsupported criteria')
        value=f['value']
        if key in ('minimum_ebitda','industries','regions'):
            field=by_name.get(FIELDS[key]);options={c['value'] for c in field.get('choices',[])} if field else set()
            if not options:raise ValueError('Unverified dropdown')
            vals=value if isinstance(value,list) else [value]
            if not vals or any(v not in options for v in vals):raise ValueError('Unsupported dropdown option')
            if key!='minimum_ebitda' and not isinstance(value,list):raise ValueError('Multi-select must be an array')
            if key=='minimum_ebitda' and (not isinstance(value,str) or 'ebitda' not in f['quote'].lower()):raise ValueError('EBITDA evidence required')
        validated.append(f)
    if not validated:raise ValueError('No supported findings; keep record pending for review')
    payload={'contact_id':str(contact_id),'domain':d,'findings':validated,'schema_sha':schema_hash(schema),'firm_published':True}
    pid=digest(payload)
    with store.db:
        store.db.execute("UPDATE proposals SET state='superseded' WHERE contact_id=? AND id<>? AND state='pending'",(str(contact_id),pid))
        store.db.execute('INSERT OR IGNORE INTO proposals(id,contact_id,domain,payload) VALUES (?,?,?,?)',(pid,str(contact_id),d,json.dumps(payload)))
    return pid

class CRM:
    def __init__(self,base,key):
        u=urllib.parse.urlsplit(base)
        if u.scheme!='https' or u.hostname!='goexio.freshworks.com' or u.path.rstrip('/') not in ('','/crm/sales'):raise ValueError('Unexpected CRM tenant')
        self.base='https://goexio.freshworks.com/crm/sales';self.key=key
    def call(self,method,path,data=None):
        if not path.startswith('/api/'):raise ValueError('Unexpected API path')
        time.sleep(1)
        req=urllib.request.Request(self.base+path,method=method,headers={'Authorization':'Token token='+self.key,'Content-Type':'application/json','Accept':'application/json'},data=json.dumps(data).encode() if data is not None else None)
        # Writes are NEVER retried automatically. Reconcile uncertain outcomes first.
        with urllib.request.build_opener(NoRedirect).open(req,timeout=20) as r:
            try:return json.load(r)
            except json.JSONDecodeError:raise ValueError('CRM did not return JSON: '+str(r.status)+' '+str(r.headers.get('Content-Type')))
    def contact(self,cid):
        if not str(cid).isdigit():raise ValueError('Invalid contact id')
        c=self.call('GET','/api/contacts/'+str(cid))['contact']
        if str(c.get('id'))!=str(cid):raise ValueError('Contact ID mismatch')
        return c
    def notes(self,contact):
        path=contact.get('links',{}).get('notes','')
        expected='/crm/sales/contacts/'+str(contact['id'])+'/notes'
        if urllib.parse.urlsplit(path).path!=expected:raise ValueError('Unexpected notes link')
        all_notes=[]
        for page in range(1,101):
            req=urllib.request.Request('https://goexio.freshworks.com'+path+('&' if '?' in path else '?')+'per_page=100&page='+str(page),headers={'Authorization':'Token token='+self.key,'Accept':'application/json','Content-Type':'application/json'})
            with urllib.request.build_opener(NoRedirect).open(req,timeout=20) as r:d=json.load(r)
            rows=d.get('notes')
            if not isinstance(rows,list):raise ValueError('Unexpected notes response')
            all_notes.extend(rows)
            if len(rows)<100:return all_notes
        raise ValueError('Notes pagination incomplete')
    def schema(self):return self.call('GET','/api/settings/contacts/fields')['fields']

def plan_fields(contact,payload):
    if str(contact.get('id'))!=payload['contact_id']:raise ValueError('ID mismatch')
    custom=contact.get('custom_field',{});updates={}
    for f in payload['findings']:
        key=FIELDS.get(f['key'])
        if key and custom.get(key) in (None,'',[]):updates[key]=f['value']
    return updates

def apply(store,pid,crm,execute=False):
    proposal=store.db.execute('SELECT * FROM proposals WHERE id=?',(pid,)).fetchone()
    if not proposal:raise ValueError('Unknown proposal')
    prior=store.db.execute('SELECT state FROM writes WHERE id=?',(pid,)).fetchone()
    if prior:return {'state':prior[0],'skipped':True}
    p=json.loads(proposal['payload']); c=crm.contact(p['contact_id'])
    if schema_hash(crm.schema())!=p['schema_sha']:raise ValueError('Schema changed; regenerate proposal')
    emails=[c.get('email','')]+[e.get('value','') for e in c.get('emails',[])]
    if not any(domain(e)==p['domain'] for e in emails):raise ValueError('Live affiliation mismatch')
    if c.get('custom_field',{}).get('cf_type')!='Buyer':raise ValueError('Live contact is not Buyer')
    updates=plan_fields(c,p)
    note='[Exio enrichment '+pid+']\nDated '+now()[:10]+'\nFirm-published acquisition criteria, not newly submitted individual preferences.\n'+ '\n'.join(str(f['value'])+'\nSource: '+f['url']+'\nEvidence: '+f['quote'] for f in p['findings'])
    # Append a distinct native note with a durable idempotency marker.
    result={'contact_id':p['contact_id'],'fill_empty_custom_fields':updates,'note_for_review':note,'state':'dry_run'}
    if not execute:return result
    if os.getenv('ENRICHMENT_WRITES')!='enabled':raise ValueError('Live writes disabled')
    if proposal['state']!='approved':raise ValueError('Proposal is not approved')
    with store.db:store.db.execute('INSERT INTO writes VALUES (?,?,?,?,?)',(pid,'in_flight',json.dumps(c),None,now()))
    try:
        existing_notes=crm.notes(c)
        note_exists=any('[Exio enrichment '+pid+']' in str(n.get('description','')) for n in existing_notes)
        if not note_exists and any(f['url'] in str(n.get('description','')) for f in p['findings'] for n in existing_notes):raise ValueError('Legacy source note exists; reconcile rather than duplicate')
        if updates:crm.call('PUT','/api/contacts/'+p['contact_id'],{'contact':{'custom_field':updates}})
        if not note_exists:crm.call('POST','/api/notes',{'note':{'description':note,'targetable_type':'Contact','targetable_id':int(p['contact_id'])}})
        after=crm.contact(p['contact_id'])
        if any(after.get('custom_field',{}).get(k)!=v for k,v in updates.items()):raise ValueError('Readback mismatch')
        if not any('[Exio enrichment '+pid+']' in str(n.get('description','')) for n in crm.notes(after)):raise ValueError('Note readback mismatch')
        with store.db:
            store.db.execute('UPDATE writes SET state=?,after_json=? WHERE id=?',('verified',json.dumps(after),pid))
            store.db.execute("UPDATE proposals SET state='verified' WHERE id=?",(pid,))
            store.db.execute("UPDATE contacts SET status='reviewed' WHERE id=?",(p['contact_id'],))
        return {**result,'state':'verified'}
    except Exception:
        with store.db:store.db.execute("UPDATE writes SET state='uncertain_review' WHERE id=?",(pid,))
        raise

def main():
    p=argparse.ArgumentParser();p.add_argument('--db',required=True)
    sub=p.add_subparsers(dest='command',required=True)
    a=sub.add_parser('migrate');a.add_argument('workbook')
    sub.add_parser('status')
    a=sub.add_parser('research');a.add_argument('--domains',type=int,default=10);a.add_argument('--requests',type=int,default=60)
    a=sub.add_parser('propose');a.add_argument('contact_id');a.add_argument('findings');a.add_argument('--schema',required=True)
    a=sub.add_parser('approve');a.add_argument('proposal_id')
    a=sub.add_parser('apply');a.add_argument('proposal_id');a.add_argument('--execute',action='store_true')
    a=sub.add_parser('export-evidence');a.add_argument('--out',required=True)
    a=p.parse_args()
    with lock(a.db):
        s=Store(a.db)
        if a.command=='migrate':result=migrate(s,a.workbook)
        elif a.command=='status':result=s.summary()
        elif a.command=='research':
            if not 1<=a.domains<=100 or not 1<=a.requests<=600:raise ValueError('Batch limit exceeded')
            result=research(s,a.domains,a.requests)
        elif a.command=='propose':result=make_proposal(s,a.contact_id,json.loads(Path(a.findings).read_text()),json.loads(Path(a.schema).read_text()))
        elif a.command=='approve':
            with s.db:s.db.execute("UPDATE proposals SET state='approved' WHERE id=? AND state='pending'",(a.proposal_id,))
            result={'approved':s.db.total_changes}
        elif a.command=='apply':result=apply(s,a.proposal_id,CRM(os.environ['FRESHSALES_BASE_URL'],os.environ['FRESHSALES_API_KEY']),a.execute)
        elif a.command=='export-evidence':
            rows=[dict(r) for r in s.db.execute("SELECT domain,legacy FROM firms WHERE status='evidence_ready'")]
            Path(a.out).write_text(json.dumps(rows,indent=2));result={'domains':len(rows)}
        print(json.dumps(result,indent=2,default=str))

if __name__=='__main__':main()
