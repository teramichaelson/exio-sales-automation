#!/usr/bin/env python3
"""One bounded tick; schedule externally only after deployment validation."""
import argparse,json,os,time
from worker import Store,CRM,lock,research,apply,digest,now,BudgetExceeded
from extract import prepare_domain

def report(store):
    status=store.summary()
    complete=(not any(status['contact_status'].get(x,0) for x in ('pending','identity_hold','review_hold')) and not any(status['proposal_status'].get(x,0) for x in ('pending','approved')) and not any(k!='verified' and v for k,v in status['write_status'].items()) and not any(status['firm_status'].get(k,0) for k in ('pending','retry','evidence_ready','extraction_hold')))
    kind='completion' if complete else 'weekly'
    last=store.db.execute('SELECT at FROM reports WHERE kind=? ORDER BY at DESC LIMIT 1',(kind,)).fetchone()
    import datetime as dt
    if last and (complete or dt.datetime.now(dt.timezone.utc)-dt.datetime.fromisoformat(last[0])<dt.timedelta(days=7)):return {'queued':False}
    rid=digest({'kind':kind,'at':now()[:10]})
    with store.db:store.db.execute('INSERT OR IGNORE INTO reports VALUES (?,?,?,?,?)',(rid,now(),kind,json.dumps(status),'pending_delivery'))
    return {'queued':True,'kind':kind,'id':rid}

def tick(store,do_research=True,do_ai=False,do_write=False):
    result={}
    if do_research:
        size=store.db.execute('PRAGMA page_count').fetchone()[0]*store.db.execute('PRAGMA page_size').fetchone()[0]
        if size>400*1024*1024:return {'research_stop':'storage_soft_limit','report':report(store)}
        try:result['research']=research(store,limit=min(8,int(os.environ.get('ENRICHMENT_BATCH_DOMAINS','8'))),requests=min(1000,int(os.environ.get('ENRICHMENT_REQUESTS_PER_DAY','1000'))),daily_limit=min(200,int(os.environ.get('ENRICHMENT_DOMAINS_PER_DAY','200'))))
        except Exception as e:result['research_stop']=type(e).__name__
    crm=CRM(os.environ['FRESHSALES_BASE_URL'],os.environ['FRESHSALES_API_KEY']) if do_ai or do_write else None
    if do_ai:
        schema=crm.schema();result['extraction']=[]
        for r in store.db.execute("SELECT domain FROM firms WHERE status='evidence_ready' LIMIT 5").fetchall():
            try:result['extraction'].append(prepare_domain(store,r['domain'],schema,review=True))
            except BudgetExceeded:
                result['extraction_stop']='daily_budget';break
            except Exception as e:
                with store.db:store.db.execute("UPDATE firms SET status='extraction_hold' WHERE domain=?",(r['domain'],))
                result['extraction'].append({'domain':r['domain'],'error':type(e).__name__})
    if do_write:
        result['writes']=[]
        for r in store.db.execute("SELECT id FROM proposals p WHERE state='approved' AND NOT EXISTS (SELECT 1 FROM writes w WHERE w.id=p.id) LIMIT 25").fetchall():
            try:result['writes'].append(apply(store,r['id'],crm,execute=True))
            except Exception as e:
                result['writes'].append({'proposal':r['id'],'error':type(e).__name__});break
            time.sleep(1)
    result['report']=report(store)
    return result
if __name__=='__main__':
    p=argparse.ArgumentParser();p.add_argument('--db',required=True);p.add_argument('--no-research',action='store_true');p.add_argument('--ai',action='store_true');p.add_argument('--write-approved',action='store_true');a=p.parse_args()
    with lock(a.db):print(json.dumps(tick(Store(a.db),not a.no_research,a.ai,a.write_approved),indent=2))
