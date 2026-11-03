"""Deterministic identities; chain order is significant."""
from ..canonical import canonical_json, sha256_hex, stable_id

def finding_id(repo_key, rule_id, anchor):
    return stable_id("hsf", repo_key, rule_id, anchor)

def occurrence_id(scan_id, finding_id, location):
    return stable_id("occ", scan_id, finding_id, location)

def candidate_id(scan_id, rule_id, anchor, path, line):
    return stable_id("cand", scan_id, rule_id, anchor, path, line)

def primary_fingerprint(repo_key, rule_id, anchor):
    return "hermes-security/v1:sha256:" + sha256_hex(canonical_json([repo_key, rule_id, anchor]))

def scan_id(*parts):
    return stable_id("scan", *parts)

def chain_id(finding_ids):
    return stable_id("chn", list(finding_ids))
