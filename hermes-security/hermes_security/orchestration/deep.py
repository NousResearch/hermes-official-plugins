"""Independent passes and explicit budget/saturation stop conditions."""
from ..canonical import stable_id
from ..errors import ValidationError
from .standard import lanes


def build_packets(scan_id, files, profiles, passes=3):
    if type(passes) is not int or not 1 <= passes <= 8:
        raise ValidationError('deep_passes must be an integer from 1 to 8')
    return [{'packetId': stable_id('pkt', scan_id, 'deep-pass', n), 'attemptId': stable_id('att', scan_id, 'deep-pass', n),
             'role': 'deep-pass', 'pass': n, 'lanes': [r['id'] for r in lanes()],
             'files': sorted(f['path'] for f in files), 'profiles': list(profiles),
             'brief': 'Independent static review. Do not read earlier pass findings before forming hypotheses. Follow source-to-sink paths, cite exact evidence, and record negative results.'}
            for n in range(1, passes + 1)]


def stop_reason(new_root_counts, budget, canceled=False):
    if canceled:
        return 'canceled'
    if len(new_root_counts) >= budget:
        return 'budget passes reached'
    if len(new_root_counts) >= 2 and new_root_counts[-2:] == [0, 0]:
        return 'two consecutive passes with no new root cause'
    return None
