from datetime import datetime, timezone
import pandas as pd
from ict_trader.realtime.analysis.feature_snapshot import FeatureSnapshot, TFView
from ict_trader.realtime.analysis.mtf import aggregate_mtf
from ict_trader.realtime.analysis.fvg_state import update_fvg_states, fvg_quality
from ict_trader.realtime.analysis.risk import build_risk_plan

def snap(bias="bearish"):
    views = {tf: TFView(tf, bias, "BOS", 100, 1, 1.0) for tf in ("1D","4H","1H","15m")}
    return FeatureSnapshot("S","ETHUSDT",datetime.now(timezone.utc),100,
                           views,None,(),(),(),(),())

def test_mtf_conflict_gate():
    assert aggregate_mtf(snap("bullish"), "short")[1] == "htf_1d_conflict"
    assert aggregate_mtf(snap("bearish"), "short")[1] == "ok"

def test_fvg_state_expires_and_quality():
    df=pd.DataFrame({"open":[1],"high":[2],"low":[1],"close":[1.5]})
    s=update_fvg_states([{"direction":"bullish","top":2,"bottom":1,"created":-600}],df,max_age=10)[0]
    assert s.state=="EXPIRED"
    q=fvg_quality(s,displacement_atr=0.8,htf_overlap=True,correct_pd=True)
    assert q.quality>=0.5

def test_risk_plan_has_positive_net_rr():
    plan=build_risk_plan("long",100,2,{"level":98},{"price":98},[108])
    assert plan.stop < 100 and plan.net_rr >= 2 and plan.quantity > 0
