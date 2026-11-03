"""Execution-plan bounds and paired-control indexing."""
import math
from ..errors import PolicyDenied

def execution_limits(plan):
    """Return validated limits; caps cannot be disabled by a plan."""
    timeout=plan.get('timeoutS',10)
    cap=plan.get('maxOutputBytes',65536)
    if isinstance(timeout,bool) or not isinstance(timeout,(int,float)) or not math.isfinite(timeout) or not 0<timeout<=300:
        raise PolicyDenied('timeoutS must be >0 and <=300')
    if type(cap) is not int or not 1<=cap<=1048576: raise PolicyDenied('maxOutputBytes must be 1..1048576')
    return timeout,cap

def control_spec(plan, count, *, http=False):
    """Controls reference operations actually executed, not claimed outcomes."""
    key='requestIndex' if http else 'commandIndex'
    pos,neg=plan['positiveControl'],plan['negativeControl']
    pi=pos.get(key,0); ni=neg.get(key,1)
    marker=pos.get('expectedMarker',plan.get('expectedMarker'))
    if type(pi) is not int or type(ni) is not int or not 0<=pi<count or not 0<=ni<count or pi==ni:
        raise PolicyDenied('paired controls require distinct valid operation indices')
    if not isinstance(marker,str) or not marker or len(marker)>1000: raise PolicyDenied('positive control requires expectedMarker')
    return pi,ni,marker
