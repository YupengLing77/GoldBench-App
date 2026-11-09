"""Paid provider boundary: frozen pricing, atomic money/token reservations."""
import json
import os
import time
import httpx
from . import store, policies

QWEN = 'qwen3.8-2.4t-a95b'
DEEPSEEK = 'deepseek-v4-pro-0813'
FLASH = 'deepseek-v4.1-flash'
FLASH_MAX_OUTPUT = 393216  # Provider's documented physical limit.
DASHSCOPE_MODELS = (QWEN, DEEPSEEK, FLASH)
DASHSCOPE_URL = 'https://dashscope.aliyuncs.com/compatible-mode/v1/chat/completions'


class TotalTokenBudgetExhausted(RuntimeError):
    pass


def collect_stream(response):
    """Assemble OpenAI SSE without exposing reasoning through monitor APIs."""
    result={'choices':[], 'usage':{}}
    message={'role':'assistant'}; tools={}; finish=None
    for line in response.iter_lines():
        if not line.startswith('data:'):continue
        data=line[5:].strip()
        if data=='[DONE]':break
        if not data:continue
        chunk=json.loads(data)
        if chunk.get('error'):raise RuntimeError('dashscope_stream_error')
        for key in ('id','model','created','object'):
            if key in chunk:result[key]=chunk[key]
        if chunk.get('usage'):result['usage']=chunk['usage']
        for choice in chunk.get('choices',[]):
            if choice.get('finish_reason'):finish=choice['finish_reason']
            delta=choice.get('delta') or {}
            for key in ('content','reasoning_content'):
                if delta.get(key):message[key]=message.get(key,'')+delta[key]
            for i,part in enumerate(delta.get('tool_calls') or []):
                target=tools.setdefault(part.get('index',i),{'id':'','type':'function','function':{'name':'','arguments':''}})
                if part.get('id'):target['id']+=part['id']
                fn=part.get('function') or {}
                for key in ('name','arguments'):
                    if fn.get(key):target['function'][key]+=fn[key]
    if tools:message['tool_calls']=[tools[k] for k in sorted(tools)]
    if 'content' not in message:message['content']=None
    result['choices']=[{'index':0,'message':message,'finish_reason':finish}]
    return result


def request(exp, agent, day, body, timeout=120, retry=0):
    config = exp['config']
    driver = config['driver']
    if driver not in ('poe', 'dashscope') or not config.get('paid_enabled'):
        raise RuntimeError('paid_provider_not_enabled')
    env = 'DASHSCOPE_API_KEY' if driver == 'dashscope' else 'POE_API_KEY'
    key = os.getenv(env)
    if not key:
        raise RuntimeError(env + '_not_configured')
    if driver == 'dashscope' and body['model'] not in DASHSCOPE_MODELS:
        raise RuntimeError('unsupported_dashscope_model')
    price = config['prices'].get(body['model'])
    if not price:
        raise RuntimeError('model_price_not_configured')
    body = dict(body)
    total_only = policies.total_only(config)
    if total_only:
        # Every completion shares the one experiment cap; no per-call quota.
        body.pop('max_tokens', None)
        body.pop('max_completion_tokens', None)
        body.update(enable_thinking=True, enable_search=False, stream=True, stream_options={'include_usage':True})
        body.pop('reasoning_effort', None)
        body.pop('parallel_tool_calls', None)
        upper = len(store.dumps(body).encode('utf-8')) + 4096
        budget = store.token_budget(exp['id'], config['token_limit'])
        output = min(FLASH_MAX_OUTPUT, budget['remaining'] - upper - 10)
        if output <= 0:
            raise TotalTokenBudgetExhausted('experiment_token_budget_exhausted')
        body['max_completion_tokens'] = output
    output_limit=body.get('max_completion_tokens',body.get('max_tokens'))
    if type(output_limit) is not int or output_limit<=0:raise ValueError('positive_output_limit_required')
    if driver == 'dashscope' and not total_only:
        # This deployment requires thinking. max_completion_tokens bounds the
        # ENTIRE completion, including reasoning (max_tokens alone does not).
        body.pop('max_tokens',None)
        body.update(enable_thinking=True,reasoning_effort='low',max_completion_tokens=output_limit,
                    enable_search=False,parallel_tool_calls=False)
        # Thinking mode rejects forced function selection. Advertise only the
        # permitted schema and verify the actual returned call in the host loop.
        choice=body.get('tool_choice')
        if isinstance(choice,dict):
            name=choice['function']['name']
            body['tool_choice']='auto'
            body['messages']=list(body['messages'])+[{'role':'user','content':f'Complete this step by calling {name}. Do not answer with plain text.'}]
    upper = len(store.dumps(body).encode('utf-8')) + 2048
    # Provider documents a possible +10-token completion limit deviation.
    output_bound=output_limit+(10 if driver=='dashscope' else 0)
    reserved_tokens = upper + output_bound
    estimate = (upper*price['input'] + output_bound*price['output'])/1e6
    rid = store.reserve(exp['id'], agent, day, estimate, None if total_only else config['monthly_budget'],
                        tokens=reserved_tokens, token_limit=config.get('token_limit'))
    url = DASHSCOPE_URL if driver == 'dashscope' else 'https://api.poe.com/v1/chat/completions'
    def checked_response(response):
        if response.status_code < 400:return
        if total_only:response.read()
        try:
            error=response.json().get('error',{})
            if not isinstance(error,dict):error={}
            details={field:str(error[field]).replace(key,'[redacted]')[:500] for field in ('code','message') if field in error}
        except (ValueError,AttributeError):details={}
        if response.status_code in (400,401,403,404,429):
            store.finish_request(rid,0,{'provider':driver,'http_status':response.status_code,'error':details},tokens=0)
        raise RuntimeError(driver+'_http_'+str(response.status_code))
    if total_only:
        # Keep the outgoing checkpoint even when the connection fails before usage.
        # Its token reservation is NOT released or guessed from partial text.
        with store.db() as con:
            con.execute('UPDATE requests SET payload=? WHERE id=?',(store.dumps({'provider':driver,'request':body,'streaming':True}),rid))
    with httpx.Client(timeout=None if total_only else max(1,min(120,timeout))) as client:
        if total_only:
            with client.stream('POST',url,headers={'Authorization':'Bearer '+key},json=body) as response:
                checked_response(response)
                result=collect_stream(response)
        else:
            response = client.post(url, headers={'Authorization':'Bearer '+key}, json=body)
            if response.status_code == 429 and retry == 0:
                try:checked_response(response)
                except RuntimeError:pass
                try:delay=max(2,float(response.headers.get('Retry-After','2')))
                except ValueError:delay=2
                if delay+2 < timeout:
                    time.sleep(delay)
                    return request(exp,agent,day,body,timeout-delay,1)
            checked_response(response)
            result=response.json()
    usage = result.get('usage') or {}
    inp, out = usage.get('prompt_tokens'), usage.get('completion_tokens')
    if any(type(x) is not int or x<0 for x in (inp,out)):
        raise RuntimeError('usage_unavailable_reserved')
    total = usage.get('total_tokens', inp+out)
    if type(total) is not int or total < inp+out:
        raise RuntimeError('invalid_total_usage_reserved')
    cost = (inp*price['input']+out*price['output'])/1e6
    store.finish_request(rid,cost,{'provider':driver,'request':body,'response':result,
                                  'estimated_cost':True,'price':price},tokens=total)
    if total > reserved_tokens or cost > estimate+1e-9:
        raise RuntimeError('provider_usage_exceeded_reservation')
    return result,cost


def calibrate(exp):
    """Verify forced tool selection and tool-result continuation, within this trial's cap."""
    model = exp['config']['models'][0]
    agent = 'calibration|'+model
    day = exp['config']['start']
    completed = next((e for e in store.events(exp['id']) if e['agent']==agent and e.get('status')=='calibration_passed'),None)
    if completed:
        return completed
    tool = {'type':'function','function':{'name':'calibration_echo','description':'Echo the requested integer.',
            'parameters':{'type':'object','properties':{'value':{'type':'integer'}},'required':['value'],'additionalProperties':False}}}
    messages = [{'role':'user','content':'Call calibration_echo with value 1. This is an API compatibility check, not market research.'}]
    usage = {'input_tokens':0,'output_tokens':0,'cost':0}
    event = {'agent':agent,'day':day,'group':'calibration','valid_submission':False,'terminal':False,
             'trace':[],'evidence':{},'decision':None,'equity':None,'label':None}
    try:
        for expected in (1,2):
            body = {'model':model,'messages':messages,'tools':[tool],
                    'tool_choice':{'type':'function','function':{'name':'calibration_echo'}},'max_tokens':2048 if model == DEEPSEEK else 768}
            if policies.total_only(exp['config']):
                body.pop('tool_choice',None)
                body.pop('max_tokens',None)
            result,cost = request(exp,agent,day,body)
            usage['cost'] += cost
            usage['input_tokens'] += result['usage']['prompt_tokens']
            usage['output_tokens'] += result['usage']['completion_tokens']
            msg = result['choices'][0]['message']
            calls = msg.get('tool_calls') or []
            if len(calls)!=1 or calls[0]['function']['name']!='calibration_echo':
                raise RuntimeError('calibration_tool_call_missing')
            args = json.loads(calls[0]['function']['arguments'])
            if args != {'value':expected} or type(args['value']) is not int:
                raise RuntimeError('calibration_tool_arguments')
            messages.extend([msg,{'role':'tool','tool_call_id':calls[0]['id'],'content':store.dumps({'value':expected})}])
            if expected==1:messages.append({'role':'user','content':'The previous tool returned value 1. Now call calibration_echo with value 2.'})
        event.update(usage,status='calibration_passed',checks=['tool_call','tool_result_continuation','usage_reported','thinking_completion_bounded'])
    except Exception:
        # Preserve a checkpoint for definite failures; unknown requests remain
        # reserved and resume still requires checking the requests ledger.
        event.update(usage,status='calibration_failed')
        store.save_event(exp['id'],event)
        raise
    # Failed calibration checkpoints may exist after an explicit safe resume.
    with store.db() as con:
        con.execute('INSERT OR REPLACE INTO events VALUES(?,?,?,?)',(exp['id'],agent,day,store.dumps(event)))
    return event
