"""Rolling worker queue: remainders for unfinished files, monotonic worker coverage."""
from hermes_security.service import SecurityService
from test_end_to_end_static import worker


def start(tmp_path, files=4):
    root = tmp_path / 'repo'; root.mkdir()
    for i in range(files):
        (root / f'mod{i}.py').write_text(f'def f{i}():\n    return {i}\n')
    service = SecurityService(tmp_path / 'data')
    return root, service, service.start_scan(path=str(root))


def area_packet(plan):
    return next(p for p in plan['packets'] if p.get('review') == 'area')


def submit(service, plan, packet, attempt, coverage, role='investigator'):
    payload = worker(plan, [], role=role, attempt=attempt, coverage=coverage)
    payload['packetId'] = packet['packetId']
    return service.submit_worker_result(payload)


def test_queue_lists_packets_with_schedule(tmp_path):
    _, service, plan = start(tmp_path)
    queue = service.get_scan(plan['scanId'], 'queue')
    assert queue['schedule']['maxParallelWorkers'] == 10
    assert queue['pending'] == queue['total'] == len(plan['packets'])
    assert queue['items'][0]['role'] == 'baseline'
    summary = service.get_scan(plan['scanId'], 'summary')
    assert 'queue' in summary['sections'] and summary['schedule']['maxParallelWorkers'] == 10


def test_unfinished_area_files_become_a_remainder_packet(tmp_path):
    _, service, plan = start(tmp_path)
    sid, area = plan['scanId'], area_packet(plan)
    done, left = area['files'][:2], area['files'][2:]
    coverage = [{'unit': 'file:' + p, 'state': 'reviewed', 'note': 'Read fully.'} for p in done]
    coverage += [{'unit': 'file:' + p, 'state': 'deferred', 'note': 'Ran out of room.'} for p in left]
    submit(service, plan, area, 'att_area_1', coverage)
    packets = service.get_scan(sid, 'packets', limit=100)['items']
    remainder = next(p for p in packets if p.get('review') == 'remainder')
    assert remainder['parentPacketId'] == area['packetId'] and remainder['files'] == sorted(left)
    queue = service.get_scan(sid, 'queue', limit=100)
    assert {i['packetId']: i['state'] for i in queue['items']}[remainder['packetId']] == 'pending'
    # The fresh worker finishes the remainder; nothing new is queued and the packet is done.
    submit(service, plan, remainder, 'att_area_2', [{'unit': 'file:' + p, 'state': 'reviewed', 'note': 'Read fully.'} for p in left])
    packets = service.get_scan(sid, 'packets', limit=100)['items']
    assert sum(p.get('review') == 'remainder' for p in packets) == 1
    assert {i['packetId']: i['state'] for i in service.get_scan(sid, 'queue', limit=100)['items']}[remainder['packetId']] == 'done'


def test_a_later_attempt_that_finishes_the_packet_retires_its_remainder(tmp_path):
    _, service, plan = start(tmp_path)
    sid, area = plan['scanId'], area_packet(plan)
    done, left = area['files'][:2], area['files'][2:]
    submit(service, plan, area, 'att_area_1', [{'unit': 'file:' + p, 'state': 'reviewed', 'note': 'Read fully.'} for p in done])
    assert any(p.get('review') == 'remainder' for p in service.get_scan(sid, 'packets', limit=100)['items'])
    # The same worker submits again with the rest; nothing is left for a fresh worker.
    submit(service, plan, area, 'att_area_2', [{'unit': 'file:' + p, 'state': 'reviewed', 'note': 'Read fully.'} for p in left])
    queue = service.get_scan(sid, 'queue', limit=100)
    assert not any(i['review'] == 'remainder' for i in queue['items'])


def test_a_later_deferral_never_undoes_a_full_review(tmp_path):
    _, service, plan = start(tmp_path)
    sid, area = plan['scanId'], area_packet(plan)
    path = area['files'][0]
    submit(service, plan, area, 'att_area', [{'unit': 'file:' + path, 'state': 'reviewed', 'note': 'Read fully.'}])
    baseline = plan['packets'][0]
    submit(service, plan, baseline, 'att_base', [{'unit': 'file:' + path, 'state': 'deferred', 'note': 'Area worker owns it.'}], role='baseline')
    states = {u['unitId']: u['state'] for u in service.coverage(sid)['units']}
    assert states['file:' + path] == 'reviewed'
    # The parent can still correct coverage through a checkpoint.
    service.checkpoint(sid, coverage=[{'unit': 'file:' + path, 'state': 'deferred', 'note': 'Review was incomplete.'}])
    assert {u['unitId']: u['state'] for u in service.coverage(sid)['units']}['file:' + path] == 'deferred'
