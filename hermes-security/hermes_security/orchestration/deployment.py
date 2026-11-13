"""Deployment evidence is not file coverage or runtime validation authority."""
import re
from copy import deepcopy

from ..domain.validate import validate_deployment_evidence
from ..errors import ValidationError


UNIT = re.compile(r'deployment:([^#]+)#([a-z0-9][a-z0-9_-]{0,63})\Z')


def check_unit(ident, unit, paths, *, worker):
    match = UNIT.fullmatch(ident)
    detail = unit.get('deployment')
    if not match or match[1] not in paths or validate_deployment_evidence(detail):
        raise ValidationError('deployment evidence requires an inventory path, stable key and structured detail')
    disposition = detail['disposition']
    state = {'pending': 'unknown', 'resolved': 'reviewed', 'deferred': 'deferred'}[disposition]
    if unit.get('state') != state:
        raise ValidationError('deployment evidence state must match its disposition')
    if worker and disposition != 'pending':
        raise ValidationError('deployment evidence disposition belongs to the parent checkpoint, not workers')
    if disposition != 'pending' and not detail['reviewNote'].strip():
        raise ValidationError('deployment evidence disposition requires a review note')
    if disposition == 'resolved' and detail['missingEvidence']:
        raise ValidationError('resolved deployment evidence cannot retain missing evidence')
    if disposition in {'pending', 'deferred'} and not detail['missingEvidence']:
        raise ValidationError('unresolved deployment evidence must name missing evidence')
    return deepcopy(detail)


def pending(units):
    return [u for u in units if u.get('kind') == 'deployment'
            and u.get('deployment', {}).get('disposition', 'pending') == 'pending']
