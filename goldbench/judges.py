"""Independent, blind dual judges. Human review replaces, never averages, scores."""
import hashlib,json,math,random
from . import store
from .agents import poe_request

JUDGES=['GPT-4.1-mini','Gemini-3.1-Flash-Lite']
RUBRIC='''仅评当时实际可见证据，不猜模型身份、不联网，不以事后收益评判。三个维度分别0—4或null。
信息理解准确性：0核心错误；1重大误读；2基本正确但关键时点/冲突遗漏；3正确仅轻微遗漏；4准确区分事实推断不确定性并处理已见冲突。
分析操作一致性：0核心相反无解释；1重大矛盾；2方向对但仓位/费用连接缺失；3基本自洽；4判断风险仓位动作清楚并解释合理预测持仓分歧。
证据支持：0核心无据/虚构；1主要靠断言；2局部支持但关键缺口；3主要可追溯；4强度与风险仓位相称并处理已见反证。
基础组没有新闻工具不自动扣分；账户规则与不确定性可支持保守行动。平台缺失证据则受影响维度null，模型没有获取依据不能用null免责。不要因篇幅、工具数或盈利评分。
返回严格JSON：{"scores":[整数或null,整数或null,整数或null],"reasons":[逐维理由3条],"evidence_ids":[实际ID],"critical_dispute":false}。'''

def validate(result,evidence):
    if not isinstance(result,dict) or set(result)!={'scores','reasons','evidence_ids','critical_dispute'}:raise ValueError('judge_schema')
    if not isinstance(result['scores'],list) or len(result['scores'])!=3 or any(x is not None and (type(x)!=int or not 0<=x<=4) for x in result['scores']):raise ValueError('judge_score')
    if not isinstance(result['reasons'],list) or len(result['reasons'])!=3 or any(not isinstance(r,str) or not r.strip() for r in result['reasons']):raise ValueError('judge_reasons')
    if not isinstance(result['evidence_ids'],list) or any(x not in evidence for x in result['evidence_ids']):raise ValueError('judge_evidence')
    if type(result['critical_dispute'])!=bool:raise ValueError('judge_dispute')
    return result

def summary(ident):
    with store.db() as c:
        machine=[dict(r)|{'payload':json.loads(r['payload'])} for r in c.execute('SELECT * FROM judgments WHERE experiment=?',(ident,))]
        human=[json.loads(r[0]) for r in c.execute('SELECT payload FROM reviews WHERE experiment=? ORDER BY created',(ident,))]
    events=[e for e in store.events(ident) if e.get('valid_submission') and e['group']!='baseline' and not e['terminal']]
    random_ids=set()
    strata={}
    for e in events:strata.setdefault((e['model'],e['group']),[]).append(e)
    for key,items in strata.items():
        rng=random.Random(ident+'|'.join(key)+'review-v1');sample=rng.sample(items,math.ceil(len(items)*.2))
        for repeat in (1,2):
            options=[e for e in items if e['repeat']==repeat]
            if options and not any(e['repeat']==repeat for e in sample):sample.append(rng.choice(options))
        random_ids.update((e['agent'],e['day']) for e in sample)
    results=[]
    for e in events:
        pair=[next((x['payload'] for x in machine if x['agent']==e['agent'] and x['day']==e['day'] and x['judge']==j),None) for j in JUDGES]
        scores=None;trigger=False
        if all(p and p.get('status')=='scored' for p in pair):
            scores=[(a+b)/2 if a is not None and b is not None else None for a,b in zip(pair[0]['scores'],pair[1]['scores'])]
            trigger=any(x is None for x in scores) or any(a is not None and b is not None and abs(a-b)>=2 for a,b in zip(pair[0]['scores'],pair[1]['scores'])) or any(p['critical_dispute'] for p in pair)
        elif any(pair):trigger=True
        review=next((h for h in reversed(human) if h['agent']==e['agent'] and h['day']==e['day']),None)
        needed=trigger or (e['agent'],e['day']) in random_ids
        if review:scores=review['scores']
        total=sum(scores)/12*100 if scores and None not in scores else None
        results.append({'agent':e['agent'],'day':e['day'],'review_id':hashlib.sha256((ident+e['agent']+e['day']).encode()).hexdigest()[:16],
            'judges':pair,'scores':scores,'total':total,'review_required':needed,'reviewed':review is not None,
            'final':review is not None or bool(all(pair) and not needed and total is not None)})
    return results

def run(ident):
    exp=store.get(ident);store.status(ident,'judging')
    try:
        for e in store.events(ident):
            if not e.get('valid_submission') or e['group']=='baseline' or e['terminal']:continue
            for judge in JUDGES:
                with store.db() as c:
                    done=c.execute('SELECT 1 FROM judgments WHERE experiment=? AND agent=? AND day=? AND judge=?',(ident,e['agent'],e['day'],judge)).fetchone()
                if done:continue
                # Evidence is exactly the text shown to the agent, no future labels, NAV or name.
                package={'decision':e['decision'],'evidence':e.get('evidence',{}),'cutoff':e['cutoff'],'account':e.get('pre_account',{}),'rules':{k:exp['config'][k] for k in ('spread','allow_short','max_exposure')}}
                payload=None
                for attempt in range(2):
                    result,cost=poe_request(exp,e['agent']+'|judge|'+judge,e['day'],{'model':judge,'messages':[{'role':'system','content':RUBRIC},{'role':'user','content':store.dumps(package)}],'max_tokens':500})
                    try:
                        payload=validate(json.loads(result['choices'][0]['message']['content']),package['evidence'])|{'status':'scored','cost':cost}
                        break
                    except (ValueError,KeyError,TypeError):payload={'status':'invalid_output','scores':[None]*3,'cost':cost}
                with store.db() as c:c.execute('INSERT OR IGNORE INTO judgments VALUES(?,?,?,?,?)',(ident,e['agent'],e['day'],judge,store.dumps(payload)))
        store.status(ident,'awaiting_review')
    except Exception as e:store.status(ident,'blocked',str(e)[:300])
