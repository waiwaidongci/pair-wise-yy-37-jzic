from __future__ import annotations
import re
from datetime import datetime
from .domain import ConflictError, ValidationError
TITLE='空气污染源许可与合规检查'; ENTITY='排污许可'; ID_PREFIX='AQ'
SEVERITIES=['low', 'medium', 'high', 'critical']; STATES=['draft', 'submitted', 'inspection', 'correction', 'approved']; TRANSITIONS={'draft': ['submitted'], 'submitted': ['inspection'], 'inspection': ['correction'], 'correction': ['approved'], 'approved': []}; TRANSITION_ROLES={'submitted': ['applicant'], 'inspection': ['inspector'], 'correction': ['inspector'], 'approved': ['compliance_manager']}
CREATE_ROLES=set(['applicant']); RECORD_ROLES=set(['applicant', 'inspector']); AUDIT_ROLES=set(['compliance_manager', 'viewer']); VIEW_ROLES=set(['applicant', 'inspector', 'compliance_manager', 'viewer'])
SEVERITY_WEIGHT={'low': 1.0, 'medium': 3.0, 'high': 6.0, 'critical': 9.0}; DEADLINE_HOURS={'low': 72, 'medium': 24, 'high': 8, 'critical': 4}; TERMINAL_STATES=set(['approved'])
QUEUE_STATES=('submitted', 'correction'); BATCH_STATUSES=('queued', 'claimed', 'executed'); ACTIVE_BATCH_STATUSES=('claimed', 'executed'); SCHEDULE_ROLES=set(['inspector', 'compliance_manager']); INSPECTOR_ROLES=set(['inspector']); INSPECTOR_MANAGE_ROLES=set(['compliance_manager'])
DAY_PATTERN=re.compile(r'^\d{4}-\d{2}-\d{2}$')
def normalize_day(value):
    if value is None:
        from datetime import timezone
        return datetime.now(timezone.utc).date().isoformat()
    if not isinstance(value,str) or not DAY_PATTERN.match(value): raise ValidationError("日期必须是YYYY-MM-DD格式")
    try: datetime.strptime(value,"%Y-%m-%d")
    except ValueError: raise ValidationError("日期不是有效日历日期")
    return value
def priority_score(severity,quantity=0.0,threshold=1.0,open_records=0):
    if severity not in SEVERITY_WEIGHT: raise ValidationError("unknown severity")
    ratio=quantity/threshold if threshold>0 else 1.0
    return max(0,min(10,int(round(SEVERITY_WEIGHT[severity]+min(4.0,ratio*4.0)+min(3.0,float(open_records))))))
def response_deadline_hours(severity,quantity=0.0,threshold=1.0):
    if severity not in DEADLINE_HOURS: raise ValidationError("unknown severity")
    ratio=quantity/threshold if threshold>0 else 1.0
    return max(1,int(DEADLINE_HOURS[severity]/max(1.0,ratio)))
def escalation_required(severity,quantity=0.0,threshold=1.0):
    return severity==SEVERITIES[-1] or (threshold>0 and quantity>=threshold)
def can_transition(current,target): return target in TRANSITIONS.get(current,[])
def validate_transition(current,target):
    if current not in STATES or target not in STATES: raise ValidationError("未知状态")
    if not can_transition(current,target): raise ConflictError(f"不能从{current}转换到{target}")
def completion_blockers(target,open_records): return ["仍有未关闭事项"] if target in TERMINAL_STATES and open_records>0 else []
def role_for_transition(target): return set(TRANSITION_ROLES.get(target,[]))
def queue_basis(severity,quantity,threshold,open_records):
    # 严重度、申报量（相对许可量）和未关闭整改共同决定优先级
    return {
        "priority": priority_score(severity,quantity,threshold,open_records),
        "severity": severity, "quantity": float(quantity),
        "threshold": float(threshold), "open_records": int(open_records),
    }
def queue_sort_key(basis):
    return (-int(basis["priority"]), -float(basis["quantity"]), int(basis.get("item_id",0)))
def choose_inspector(load_rows, default_capacity):
    # 当天名额按当前负荷分配：剩余名额最多（并列时当前负荷最低、编号最小）者得
    best=None
    for row in load_rows:
        capacity=int(row["daily_capacity"] or default_capacity)
        load=int(row["load"])
        remaining=capacity-load
        if remaining<=0: continue
        candidate=(remaining,-load,-int(row["id"]),capacity,row["name"])
        if best is None or candidate[:3]>best[0]: best=(candidate[:3],int(row["id"]),row["name"],remaining,capacity,load)
    if best is None: return None
    _,inspector_id,name,remaining,capacity,load=best
    return {"id":inspector_id,"name":name,"remaining":remaining,"capacity":capacity,"load":load}
def ensure_batch_status(value):
    if value not in BATCH_STATUSES: raise ValidationError("批次状态非法")
    return value
