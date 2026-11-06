import pytest
from hermes_security.domain.dispositions import *
from hermes_security.domain.taxonomy import OWASP_2025, EFFECT_SATISFIES

def test_order_labels_and_vocab():
    assert severity_rank('critical') > severity_rank('high') > severity_rank('informational')
    assert max_severity(['low','high','medium']) == 'high'
    assert max_severity([]) == 'informational'
    assert bump_severity('medium') == 'high'
    assert bump_severity('low', 99) == 'critical'
    assert human_label('source_supported') == 'Supported by code'
    assert human_label('NOT_RUN') == 'NOT RUN'
    assert len(OWASP_2025) == 10 and OWASP_2025[-1] == 'A10:2025'
    assert EFFECT_SATISFIES['data-read'] == {'credential'}
    with pytest.raises(ValueError): bump_severity('low', -1)
    with pytest.raises(ValueError): severity_rank('urgent')
