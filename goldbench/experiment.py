"""Explicit, resumable single-model experiment runner. No credentials in config."""
import argparse
import fcntl
import hashlib
import json
import os
from pathlib import Path
from . import store, prompts, policies
from .trial import load_credentials


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--config', default='configs/deepseek_october.json')
    parser.add_argument('--env-file', default='runtime/qwen.env')
    parser.add_argument('--prepare', action='store_true')
    parser.add_argument('--resume', action='store_true')
    args = parser.parse_args()
    load_credentials(args.env_file)
    from .api import Config, preflight, build, detail
    from .worker import run
    config_path = Path(args.config)
    raw = json.loads(config_path.read_text())
    config = Config(**raw)
    store.init()
    state_file = store.DATA/(config_path.stem+'_state.json')
    with open(state_file.with_suffix('.lock'), 'a+') as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            raise RuntimeError('experiment_runner_already_running') from None
        if state_file.exists():
            ident = json.loads(state_file.read_text())['experiment_id']
            exp = store.get(ident)
            for key in raw:
                if exp['config'].get(key) != raw[key]:
                    raise RuntimeError('frozen_config_changed:'+key)
            if args.resume:
                if exp['status'] not in ('prepared','paused','blocked'):
                    raise RuntimeError('experiment_not_resumable')
                with store.db() as con:
                    pending = con.execute("SELECT COUNT(*) FROM requests WHERE experiment=? AND status!='complete'",(ident,)).fetchone()[0]
                    orphan = con.execute('SELECT COUNT(*) FROM requests r LEFT JOIN events e ON r.experiment=e.experiment AND r.agent=e.agent AND r.day=e.day WHERE r.experiment=? AND e.day IS NULL',(ident,)).fetchone()[0]
                if pending or orphan:
                    raise RuntimeError('unknown_or_uncheckpointed_requests_require_reconciliation')
                run(ident)
        else:
            report = preflight(config)
            print(store.dumps({'stage':'preflight','can_start':report['can_start'],'issues':report['issues'],
                              'coverage':report['coverage'],'token_limit':config.token_limit}),flush=True)
            if not report['can_start']:
                raise RuntimeError('preflight_failed')
            frozen, dataset, _ = build(config)
            frozen.update(prompt_version=prompts.PROMPT_VERSION,
                          prompt_sha256=hashlib.sha256(prompts.system_prompt().encode()).hexdigest(),
                          excluded_dates_reason='User-authorized exclusions for five missing Friday quotes; not actual exchange holidays.',
                          pricing_note=('Conservative USD estimate at 0.35/1.4 per million; Flash Beijing peak CNY 2/8; actual bill may be lower.' if policies.total_only(frozen) else 'Conservative USD estimate at 1.5/4.5 per million; official Beijing peak CNY 9/27; actual bill may be lower.'))
            ident = store.create(frozen, dataset, initial_status='prepared')
            fd = os.open(state_file, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(fd, 'w') as output:
                output.write(store.dumps({'experiment_id':ident}))
            print(store.dumps({'stage':'prepared','experiment_id':ident,'prompt_version':prompts.PROMPT_VERSION}),flush=True)
            if not args.prepare:
                run(ident)
        result = detail(ident)
        print(store.dumps({'stage':'result','experiment_id':ident,'status':result['status'],'error':result.get('error'),
                          'token_budget':result['token_budget'],
                          'accounts':[{'agent':a['agent'],'metrics':a['metrics'],'rounds':len(a['events'])} for a in result['accounts']]}),flush=True)


if __name__ == '__main__':
    main()
