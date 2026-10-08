from scripts.yahoo_5m_factor_lab import simulate
from scripts.yahoo_5m_regime_lab import regime_features
from tests.test_yahoo_5m_exits import fixture


def test_arbitrary_symbol_has_no_dependency_on_original_trade_names():
    data,feats,config=fixture()
    data['NVDA']=data.pop('SOXL')
    feats={'NVDA':feats['SOXL']}
    result=simulate(data,feats,config,['2026-07-13'],symbols=['NVDA'])
    assert len(result['trades'])==1
    assert result['trades'][0]['symbol']=='NVDA'
    assert all(bar['cash']>=0 for bar in result['bars'])
    indicators=regime_features(data,pairs=[('NVDA','SMH')])
    assert list(indicators)==['NVDA']
