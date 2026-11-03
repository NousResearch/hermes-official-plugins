"""Large repositories must stay drivable through the capped agent tools: summary + paginated packets."""
import json

import pytest

from hermes_security.service import SecurityService
from hermes_security.errors import ValidationError
from hermes_security.tools import encode_result, MAX_RESULT_CHARS


def big_repo(tmp_path, count=700):
    root = tmp_path / 'repo'; root.mkdir()
    for i in range(count):
        sub = root / ('package_with_a_long_directory_name_%03d' % (i % 20)); sub.mkdir(exist_ok=True)
        (sub / ('module_with_a_long_file_name_number_%03d.py' % i)).write_text('def handler(request):\n    return request.args\n')
    return root, SecurityService(tmp_path / 'data')


def test_summary_and_packets_sections_fit_under_tool_cap(tmp_path):
    root, service = big_repo(tmp_path)
    plan = service.start_scan(path=str(root))
    start = json.loads(encode_result(plan, args={'path': str(root)}))
    assert start['truncated'] and start['result_omitted'] and start['scan_id'] == plan['scanId']
    assert start['header']['snapshotDigest'] == plan['snapshotDigest']
    assert start['header']['methodologyVersion'] == plan['methodologyVersion']
    assert start['header']['packetIds'] == [p['packetId'] for p in plan['packets']]

    summary = service.get_scan(plan['scanId'], section='summary')
    encoded = encode_result(summary, args={'scan_id': plan['scanId'], 'section': 'summary'})
    assert len(encoded) <= MAX_RESULT_CHARS and not json.loads(encoded).get('truncated')
    for key in ('scanId', 'root', 'snapshotDigest', 'methodologyVersion', 'submit', 'workerBriefPath', 'inventory'):
        assert summary[key] == plan[key]
    assert summary['mode'] == 'standard' and summary['safetyLevel'] == 'static'
    assert summary['packetCount'] == len(plan['packets']) == len(summary['packetIds'])
    assert summary['inventory']['files'] == 700 and 'packets' in summary['sections']

    seen = []
    offset = 0
    while True:
        page = service.get_scan(plan['scanId'], section='packets', limit=2, offset=offset)
        encoded = encode_result(page, args={'scan_id': plan['scanId'], 'section': 'packets', 'limit': 2, 'offset': offset})
        assert len(encoded) <= MAX_RESULT_CHARS and not json.loads(encoded).get('truncated')
        assert page['total'] == len(plan['packets'])
        seen.extend(page['items'])
        offset += 2
        if not page['items'] or offset >= page['total']:
            break
    assert seen == plan['packets']


def test_unknown_section_still_rejected(tmp_path):
    root, service = big_repo(tmp_path, count=2)
    plan = service.start_scan(path=str(root))
    with pytest.raises(ValidationError):
        service.get_scan(plan['scanId'], section='packet')
