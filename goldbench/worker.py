import time, json, hashlib
from concurrent.futures import ThreadPoolExecutor
from . import store, data, agents
from .engine import days,next_day,execute,hold,sma,quote,cutoff

MODELS=['GPT-5-mini','Gemini-3-Flash','Claude-Haiku-4.5','GPT-5-nano']

def roster(config):
    return [(f'{model}|{group}|{repeat}',model,group,repeat) for group in ('base','extended') for model in config['models'] for repeat in (1,2)]+[(f'baseline|{name}',name,'baseline',0) for name in ('cash','buy_hold','sma20')]

def round_task(exp,identity,day,terminal,existing):
    aid,model,group,repeat=identity;config=exp['config']
    prior=[e for e in existing if e['agent']==aid and e['day']<day]
    last=prior[-1] if prior else {}
    if any(e['agent']==aid and e['day']==day for e in existing):return
    cash,qty=last.get('cash','10000'),last.get('qty','0')
    event={'agent':aid,'model':model,'group':group,'repeat':repeat,'day':day,'terminal':terminal,
           'cash':cash,'qty':qty,'equity':None,'valid_submission':False,'decision':None,'trace':[],
           'cutoff':cutoff(day).isoformat(),'label':None,'cost':0,'quality':None}
    event['pre_account']={'cash':cash,'qty':qty,'last_equity':last.get('equity'),'valuation_date':last.get('day')}
    by={r['date']:r for r in exp['dataset']['prices']};row=by.get(day)
    if group=='baseline' and model=='cash':
        store.save_event(exp['id'],event|{'equity':10000.,'status':'no_trade','decision':hold('零息现金基准'), 'valid_submission':not terminal,'delta':0,'commission':0})
        return
    if last.get('bankrupt'):
        store.save_event(exp['id'],event|{'status':'account_terminated','bankrupt':True});return
    if not terminal and last and last.get('equity') is None:
        # Missing previous mark pauses the whole cohort for this round; recover valuation only.
        if row:
            result=execute(cash,qty,hold(),row,config);event.update(result)
        event['status']='platform_paused';store.save_event(exp['id'],event);return
    if terminal:
        decision=hold('统一终点退出')|{'action':'rebalance','target_weight':0}
    elif group=='baseline':
        decision=hold('确定性基准')
        target=None
        if model=='buy_hold' and not any(abs(float(e.get('qty',0)))>0 for e in prior):target=1
        if model=='sma20':target=sma(exp['dataset']['prices'],day,config['holidays'])
        if target is not None:
            decision.update(action='rebalance',target_weight=target if config['allow_short'] else max(0,target))
        event['valid_submission']=True
    else:
        # Mature history only; the previous round's endpoint is still in the future at cutoff.
        history=[{k:e.get(k) for k in ('day','decision','status','equity','label')} for e in prior[-30:]]
        for h in history:
            if h['decision']:
                h['decision']=dict(h['decision'],rationale=h['decision']['rationale'][:500])
            if next_day(h['day'],config['holidays'])>=day:h['label']=None
        research=agents.Research(exp,aid,day,history)
        try:
            decision,usage=agents.run_agent(research,model,repeat,group,{'cash':cash,'qty':qty,'last_equity':last.get('equity'), 'valuation_date':last.get('day')})
            if decision:agents.validate_decision(decision,config,research.evidence)
            event.update(usage);event['valid_submission']=decision is not None
        except Exception as e:
            # Propagate uncertain provider/budget failures: do not label the cohort complete.
            if config['driver']=='poe':raise
            decision=None;event['error']=str(e)[:200]
        event['trace']=research.trace;event['evidence']=research.evidence
    event['decision']=decision
    action=decision or hold('失败保持原数量')
    try:
        result=execute(cash,qty,action,row,config);event.update(result)
        if not decision:event['status']='failure_hold'
    except ValueError as e:event.update(status='data_unexecutable',error=str(e))
    if decision and not terminal:
        try:
            b,a=quote(row);nb,na=quote(by.get(next_day(day,config['holidays'])))
            event['label']=float((nb+na)/(b+a)-1)
        except ValueError:pass
    # This includes the exact decision, evidence and resulting ledger in one atomic checkpoint.
    store.save_event(exp['id'],event)

def run(ident):
    exp=store.get(ident);config=exp['config'];schedule=days(config['start'],config['end'],config['holidays'])
    store.status(ident,'running')
    try:
        for day in schedule+[next_day(schedule[-1],config['holidays'])]:
            if store.get(ident)['status']=='pause_requested':store.status(ident,'paused');return
            existing=store.events(ident);terminal=day not in schedule
            with ThreadPoolExecutor(max_workers=2) as pool:
                jobs=[pool.submit(round_task,exp,identity,day,terminal,existing) for identity in roster(config)]
                for job in jobs:job.result()
        events=store.events(ident)
        missing=any(e['status'] in ('data_unexecutable','platform_paused') for e in events)
        store.status(ident,'incomplete' if missing else 'completed_fixture' if config['driver']=='fixture' else 'awaiting_review')
    except Exception as e:store.status(ident,'blocked',str(e)[:300])

def main():
    store.init()
    # One scheduler per local database; a second launch must not restart active work.
    lock=open(store.DATA/'worker.lock','a+b')
    lock.seek(0);lock.write(b'0');lock.flush();lock.seek(0)
    try:
        if __import__('os').name=='nt':
            import msvcrt
            msvcrt.locking(lock.fileno(),msvcrt.LK_NBLCK,1)
        else:
            import fcntl
            fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
    except OSError:
        print('Worker already running',flush=True);return
    # A previous process may have exited during a paid request; require explicit diagnosis.
    for exp in store.experiments():
        if exp['status'] in ('running','pause_requested','judging'):
            store.status(exp['id'],'paused' if exp['config']['driver']=='fixture' else 'blocked','worker_restarted_check_requests')
    print('GoldBench worker ready',flush=True)
    while True:
        queued=[e for e in store.experiments() if e['status']=='queued']
        if queued:run(queued[-1]['id'])
        judging=[e for e in store.experiments() if e['status']=='judge_queued']
        if judging:
            from .judges import run as judge_run
            judge_run(judging[-1]['id'])
        time.sleep(.5)

if __name__=='__main__':main()
