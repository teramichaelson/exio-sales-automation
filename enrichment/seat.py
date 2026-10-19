"""Small evidence batches reviewed in ChatGPT; no external model API calls."""
import json
from worker import digest,make_proposal,apply,now,FIELDS

def packet(store,d):
    r=store.db.execute("SELECT legacy FROM firms WHERE domain=? AND status='evidence_ready'",(d,)).fetchone()
    if not r:raise ValueError('Evidence is no longer pending')
    p=json.loads(r[0]);pages=p.get('pages',[])
    return p,pages,digest(pages)

def batch(store,limit=5):
    output=[];remaining=40000
    for r in store.db.execute("SELECT domain FROM firms WHERE status='evidence_ready' AND EXISTS(SELECT 1 FROM contacts c WHERE c.domain=firms.domain AND c.status='pending') ORDER BY domain LIMIT ?",(max(1,min(5,int(limit))),)).fetchall():
        p,pages,sha=packet(store,r[0]);total=sum(len(t) for u,t in pages)
        allowance=min(12000,remaining);visible=[]
        for u,t in pages:
            if allowance<=0:break
            v=t[:allowance];visible.append([u,v]);allowance-=len(v);remaining-=len(v)
        output.append({'domain':r[0],'evidence_sha':sha,'pages':visible,'truncated':total>sum(len(t) for u,t in visible),'contact_count':store.db.execute("SELECT count(*) FROM contacts WHERE domain=? AND status='pending'",(r[0],)).fetchone()[0]})
        if remaining<=0:break
    return output

def finish(store,body,crm=None):
    d=body['domain'];p,pages,sha=packet(store,d)
    if body['evidence_sha']!=sha:raise ValueError('Evidence changed; read it again')
    outcome=body['outcome']
    if outcome not in ('no_criteria','review_hold','criteria'):raise ValueError('Unsupported review outcome')
    if outcome=='no_criteria' and not body.get('full_source_reviewed'):raise ValueError('Full supplied source must be reviewed')
    ids=[r[0] for r in store.db.execute("SELECT id FROM contacts WHERE domain=? AND status='pending'",(d,))]
    results=[]
    if outcome=='criteria':
        if not crm:raise ValueError('CRM connection required')
        if not 1<=len(body.get('findings',[]))<=10:raise ValueError('Bounded supported findings required')
        schema=crm.schema()
        for cid in ids[:10]:
            pid=make_proposal(store,cid,body['findings'],schema)
            with store.db:store.db.execute("UPDATE proposals SET state='approved',reason=? WHERE id=? AND state='pending'",('Reviewed in ChatGPT against supplied evidence',pid))
            results.append(apply(store,pid,crm,execute=True))
        # Remaining contacts are eligible on the next pass, not silently skipped.
        left=store.db.execute("SELECT count(*) FROM contacts WHERE domain=? AND status='pending'",(d,)).fetchone()[0]
        state='evidence_ready' if left else 'extracted'
    else:
        state='no_criteria' if outcome=='no_criteria' else 'extraction_hold'
        with store.db:store.db.execute("UPDATE contacts SET status=? WHERE domain=? AND status='pending'",(outcome,d))
    with store.db:
        store.db.execute('UPDATE firms SET status=? WHERE domain=?',(state,d))
        store.db.execute('INSERT OR REPLACE INTO meta VALUES (?,?)',('seat_review:'+d,json.dumps({'at':now(),'outcome':outcome,'source_sha':sha,'reason':str(body.get('reason',''))[:500]})))
    return {'domain':d,'outcome':outcome,'contacts':len(ids),'writes':[{k:v for k,v in r.items() if k in ('contact_id','state','skipped','fill_empty_custom_fields')} for r in results]}
