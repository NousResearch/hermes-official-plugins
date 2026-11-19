"""Contained evidence reads; redact the complete file before cutting line ranges."""
from ..errors import TargetError
from ..canonical import sha256_hex
from .inventory import safe_read, validate_path
from .redact import redact_secrets


def read_excerpt(root, path, start, end, *, inventory_paths, max_chars=4000, expected_sha256=None):
    validate_path(path)
    if path not in inventory_paths: raise TargetError('Path is not in inventory')
    if type(start) is not int or type(end) is not int or start < 1 or end < start:
        raise TargetError('Invalid excerpt line range')
    if type(max_chars) is not int or max_chars < 1 or max_chars > 4000:
        raise TargetError('Invalid excerpt character limit')
    data, _ = safe_read(root, path)
    if expected_sha256 is not None and sha256_hex(data) != expected_sha256:
        raise TargetError('Excerpt file does not match inventory snapshot')
    try: text = data.decode('utf-8')
    except UnicodeError as exc: raise TargetError('Excerpt requires UTF-8 text') from exc
    if '\x00' in text: raise TargetError('Cannot excerpt binary files')
    if end > len(text.splitlines()): raise TargetError('Excerpt line range exceeds file')
    redacted, _ = redact_secrets(text)
    result = ''.join(redacted.splitlines(keepends=True)[start-1:end])
    # Do not cut a redaction marker or return an unverified prefix silently.
    if len(result) > max_chars: raise TargetError('Excerpt exceeds character limit; request fewer lines')
    return result


def verify_excerpt(root, path, start, end, code, *, inventory_paths, expected_sha256=None):
    if not isinstance(code, str) or len(code) > 4000: return False
    try: actual = read_excerpt(root, path, start, end, inventory_paths=inventory_paths, expected_sha256=expected_sha256)
    except (TargetError, OSError, ValueError): return False
    return [s.strip() for s in actual.splitlines()] == [s.strip() for s in code.splitlines()]
