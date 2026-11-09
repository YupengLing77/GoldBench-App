import pytest
from goldbench.engine import *
from goldbench.data import fixture
C={'allow_short':True,'spread':True,'max_exposure':1}
Q={'bid_open':1999,'ask_open':2001}
def decision(w):return hold()|{'action':'rebalance','target_weight':w}

def test_full_weight_and_exit():
    buy=execute('10000','0',decision(1),Q,C)
    qty=D(buy['qty']);cash=D(buy['cash'])
    assert qty*2000<=D(buy['equity'])+D('.00001')
    assert abs(D(buy['equity'])-(10000-qty*2))<D('.000001')
    sell=execute(cash,qty,decision(0),Q,C)
    assert sell['equity']==pytest.approx(buy['equity']);assert sell['qty']=='0'

def test_flip_and_commission():
    cfg=C|{'commission_bps':10}
    a=execute('10000','0',decision(-.7),Q,cfg)
    b=execute(a['cash'],a['qty'],decision(.4),Q,cfg)
    assert b['delta']>abs(float(a['qty']))
    assert float(b['cash'])==pytest.approx(float(a['cash'])-b['delta']*b['fill_price']-b['commission'])
    assert b['exposure']<=1

def test_hold():
    a=execute('10000','0',decision(.5),Q,C)
    b=execute(a['cash'],a['qty'],hold(),{'bid_open':2100,'ask_open':2102},C)
    assert b['qty']==a['qty'];assert b['delta']==0;assert b['commission']==0

def test_no_spread():
    a=execute('10000','0',decision(-1),Q,C|{'spread':False})
    b=execute(a['cash'],a['qty'],decision(0),Q,C|{'spread':False})
    assert b['equity']==10000

def test_bankruptcy():
    a=execute('20000','-10',hold(),{'bid_open':3000,'ask_open':3002},C)
    assert a['bankrupt'];assert a['equity']==-10020;assert D(a['qty'])==0

@pytest.mark.parametrize('bad',[{'bid_open':0,'ask_open':1},{'bid_open':2,'ask_open':1},{'bid_open':'NaN','ask_open':1},None])
def test_bad_quote(bad):
    with pytest.raises(ValueError):quote(bad)

def test_visibility():
    rows=fixture('2026-10-01','2026-10-10')
    assert visible_bars(rows,'2026-10-08')[-1]['date']=='2026-10-06'
    assert next_day('2026-10-09')=='2026-10-12'
    assert next_day('2026-10-09',['2026-10-12'])=='2026-10-13'

def test_sma_gap():
    rows=fixture('2026-10-01','2026-10-30')
    assert sma(rows,'2026-10-28') is not None
    assert sma([r for r in rows if r['date']!='2026-10-23'],'2026-10-28') is None

def test_decision():
    for obj,cfg in [(decision(-1),C|{'allow_short':False}),(hold()|{'target_weight':0},C),(hold()|{'prob_up':float('nan')},C),(hold()|{'evidence_ids':['invented']},C)]:
        with pytest.raises(ValueError):validate_decision(obj,cfg,{})
    assert validate_decision(hold(),C,{})['action']=='hold'
