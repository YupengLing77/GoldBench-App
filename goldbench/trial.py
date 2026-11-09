"""One resumable Qwen trial; credentials are local, never serialized into config."""
import argparse
import fcntl
import json
import os
from pathlib import Path
import shlex
from . import store


def load_credentials(path):
    path = Path(path)
    if not path.exists():
        return
    for number, line in enumerate(path.read_text(encoding='utf-8').splitlines(), 1):
        if not line.strip() or line.lstrip().startswith('#'):
            continue
        key, sep, raw = line.partition('=')
        key = key.strip()
        if not sep or key not in ('DASHSCOPE_API_KEY','TUSHARE_TOKEN'):
            raise ValueError(f'credentials_file_invalid_line:{number}')
        try:values = shlex.split(raw.strip(), comments=True)
        except ValueError:raise ValueError(f'credentials_file_invalid_line:{number}') from None
        if len(values)>1:raise ValueError(f'credentials_file_invalid_line:{number}')
        if values:os.environ[key] = values[0]


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--config', default='configs/qwen_trial.json')
    parser.add_argument('--env-file', default='runtime/qwen.env')
    parser.add_argument('--prepare', action='store_true')
    parser.add_argument('--resume', action='store_true')
    parser.add_argument('--followup', action='store_true',help='Retry a completed trial with zero valid decisions, sharing its original token budget.')
    args = parser.parse_args()
    load_credentials(args.env_file)
    from .api import Config,build,preflight,detail
    from .worker import run
    config = Config(**json.loads(Path(args.config).read_text()))
    if config.driver!='dashscope' or config.token_limit!=87000 or config.groups!=['extended'] or config.repeats!=1:
        raise ValueError('trial_requires_single_extended_qwen_and_87000_limit')
    store.init()
    state_file = store.DATA/'qwen_trial.json'
    with open(store.DATA/'qwen_trial.lock','a+') as lock:
        try:fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
        except OSError:raise RuntimeError('qwen_trial_already_running') from None
        if state_file.exists():
            state=json.loads(state_file.read_text())
            ident = state['experiment_id']
            exp = store.get(ident)
            if args.followup:
                prior=detail(ident)
                accounts=[a for a in prior['accounts'] if a['group']=='extended']
                if exp['status'] not in ('awaiting_review','incomplete') or any(a['metrics']['submission_rate'] for a in accounts):
                    raise RuntimeError('followup_only_for_completed_zero_submission_trial')
                root=state.get('root_experiment_id',ident)
                config=Config(**(config.model_dump(mode='json')|{'token_budget_parent':root}))
                report=preflight(config)
                if not report['can_start']:raise RuntimeError('followup_preflight_failed:'+str(report['issues']))
                frozen,dataset,_=build(config)
                ident=store.create(frozen,dataset,initial_status='prepared')
                tmp=state_file.with_suffix('.new')
                fd=os.open(tmp,os.O_WRONLY|os.O_CREAT|os.O_EXCL,0o600)
                with os.fdopen(fd,'w') as output:output.write(store.dumps({'experiment_id':ident,'root_experiment_id':root,'token_limit':87000}))
                os.replace(tmp,state_file)
                print(store.dumps({'stage':'followup','experiment_id':ident,'budget_root':root,'token_budget':store.token_budget(ident,87000)}),flush=True)
                run(ident)
                exp=store.get(ident)
            if args.resume and exp['status'] in ('blocked','paused'):
                with store.db() as con:
                    pending = con.execute("SELECT COUNT(*) FROM requests WHERE experiment=? AND status!='complete'",(ident,)).fetchone()[0]
                    orphan = con.execute('SELECT COUNT(*) FROM requests r LEFT JOIN events e ON r.experiment=e.experiment AND r.agent=e.agent AND r.day=e.day WHERE r.experiment=? AND e.day IS NULL',(ident,)).fetchone()[0]
                if pending or orphan:raise RuntimeError('unknown_or_uncheckpointed_requests_require_reconciliation')
                run(ident)
            result = detail(ident)
        else:
            report = preflight(config)
            print(store.dumps({'stage':'preflight','can_start':report['can_start'],'issues':report['issues'],
                               'coverage':report['coverage'],'token_limit':config.token_limit,
                               'credentials':{'dashscope':bool(os.getenv('DASHSCOPE_API_KEY')),'tushare':bool(os.getenv('TUSHARE_TOKEN'))}}),flush=True)
            if args.prepare:
                return
            if not report['can_start']:
                raise RuntimeError('preflight_failed')
            frozen, dataset, coverage = build(config)
            ident = store.create(frozen,dataset,initial_status='prepared')
            fd = os.open(state_file,os.O_WRONLY|os.O_CREAT|os.O_EXCL,0o600)
            with os.fdopen(fd,'w') as output:
                output.write(store.dumps({'experiment_id':ident,'token_limit':87000}))
            print(store.dumps({'stage':'running','experiment_id':ident}),flush=True)
            run(ident)
            result = detail(ident)
        accounts = [a for a in result['accounts'] if a['group']=='extended']
        print(store.dumps({'stage':'result','experiment_id':result['id'],'status':result['status'],'error':result.get('error'),
                           'token_budget':result['token_budget'],
                           'accounts':[{'agent':a['agent'],'metrics':a['metrics'],'rounds':len(a['events']),
                                        'decisions':[{'day':e['day'],'action':e.get('decision',{}).get('action') if e.get('decision') else None,
                                                      'weight':e.get('decision',{}).get('target_weight') if e.get('decision') else None,
                                                      'equity':e.get('equity'),'status':e['status'],'research_calls':len(e.get('trace',[])),
                                                      'routing_calls':e.get('routing_calls',0)} for e in a['events']]} for a in accounts]}),flush=True)


if __name__=='__main__':
    try:main()
    except (ValueError,RuntimeError) as exc:
        print(store.dumps({'stage':'stopped','reason':str(exc)}),flush=True)
        raise SystemExit(1)
