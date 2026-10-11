#!/usr/bin/env python3
"""Validate embedded specification fixtures; this is not a conformant runtime."""

from __future__ import annotations

from copy import deepcopy
from hashlib import sha256
import json
from pathlib import Path
import re
import subprocess
import sys
from tempfile import TemporaryDirectory

from jsonschema import Draft202012Validator, FormatChecker, ValidationError


ROOT = Path(__file__).resolve().parents[1]
DOCUMENT = ROOT / "ai_language_and_context_standard.md"
PREFIX = "urn:asdaif:example:"
REGISTRY = {
    PREFIX + suffix
    for suffix in (
        "retry-scope", "role-maintainer", "client", "request", "response",
        "operation", "policy-source", "vocab-v1", "extractor-v1", "principal",
    )
}


def require(condition, message):
    if not condition:
        raise ValueError(message)


def unique_object(pairs):
    result = {}
    for key, value in pairs:
        require(key not in result, f"Duplicate JSON key: {key}")
        result[key] = value
    return result


def reject_constant(value):
    raise ValueError(f"Invalid JSON constant: {value}")


def decode_json(value):
    return json.loads(
        value, object_pairs_hook=unique_object, parse_constant=reject_constant
    )


def digest(data):
    return sha256(data).hexdigest()


def strings(value):
    if isinstance(value, str):
        yield value
    elif isinstance(value, dict):
        for child in value.values():
            yield from strings(child)
    elif isinstance(value, list):
        for child in value:
            yield from strings(child)


def evidence_spans(value):
    if isinstance(value, dict):
        if {"source_id", "start_byte", "end_byte", "span_sha256"} <= value.keys():
            yield value
        for child in value.values():
            yield from evidence_spans(child)
    elif isinstance(value, list):
        for child in value:
            yield from evidence_spans(child)


def validate_records(records, schema_validator, source_bytes):
    by_id = {}
    for record in records:
        schema_validator.validate(record)
        require(record["id"] not in by_id, "Duplicate record ID")
        by_id[record["id"]] = record
    for record in records:
        require(record["tenant"] == "example", "Unexpected fixture tenant")
        require(record["scope_id"] == PREFIX + "retry-scope", "Wrong fixture scope")
        for value in strings(record):
            if value.startswith("urn:asdaif:"):
                require(value in by_id or value in REGISTRY, f"Unresolved ID: {value}")
        payload = record["payload"]
        kind = payload["type"]
        if kind == "source":
            data = source_bytes[record["id"]]
            require(len(data) == payload["byte_length"], "Wrong source byte length")
            require(digest(data) == payload["bytes_sha256"], "Wrong source digest")
        if kind == "snapshot":
            members = [by_id[item]["payload"] for item in payload["source_ids"]]
            require(all(item["type"] == "source" for item in members), "Not a source")
            identities = [item["logical_id"] for item in members]
            require(len(set(identities)) == len(identities), "Duplicate logical source")
            inventory = sorted(
                [item["logical_id"], item["bytes_sha256"]] for item in members
            )
            # This fixture has only ASCII strings; compact JSON is JCS-equivalent here.
            canonical = json.dumps(inventory, separators=(",", ":")).encode()
            require(digest(canonical) == payload["inventory_sha256"], "Bad inventory")
            require(payload["inventory_selector"] == "exact:spec/retry.md", "Bad scope")
            if payload["parent"] is not None:
                parent = by_id[payload["parent"]]["payload"]
                require(payload["generation"] > parent["generation"], "Rewound generation")
        snapshot = None
        if "snapshot_id" in payload:
            snapshot = by_id[payload["snapshot_id"]]["payload"]
            require(snapshot["type"] == "snapshot", "Wrong snapshot reference")
        for span in evidence_spans(payload):
            data = source_bytes[span["source_id"]]
            start, end = span["start_byte"], span["end_byte"]
            require(0 <= start < end <= len(data), "Invalid evidence coordinates")
            require(digest(data[start:end]) == span["span_sha256"], "Bad span digest")
            if snapshot is not None:
                require(span["source_id"] in snapshot["source_ids"], "Stale evidence")
        if kind == "statement":
            for old_id in payload.get("supersedes", []):
                old = by_id[old_id]["payload"]
                require(old["kind"] == payload["kind"], "Changed supersession kind")
                require(old["owner"] == payload["owner"], "Unreviewed issuer change")
                require(old["snapshot_id"] != payload["snapshot_id"], "Self supersession")
            start, end = payload["valid_from"], payload["valid_until"]
            # Fixture dates, when present, use the same UTC ISO format.
            require(not (start and end) or start < end, "Reversed validity interval")
        if kind == "representation":
            require(set(payload["source_ids"]) <= set(snapshot["source_ids"]), "Stale summary")
            require(digest(b"[]") == payload["config_sha256"], "Wrong fixture config")
            for statement_id in payload["statement_ids"]:
                statement = by_id[statement_id]["payload"]
                require(statement["snapshot_id"] == payload["snapshot_id"], "Mixed summary")
        if kind == "context_bundle":
            request = by_id[payload["request_id"]]["payload"]
            for name in ("snapshot_id", "head_generation", "policy_epoch", "purpose"):
                require(payload[name] == request[name], f"Bundle mismatch: {name}")
            require(payload["head_generation"] == snapshot["generation"], "Stale generation")
            require(set(payload["mandatory_ids"]) <= set(payload["record_ids"]), "Missing obligation")
            require(payload["input_tokens"] <= payload["budget_tokens"] <= request["budget_tokens"], "Budget exceeded")
            require(request["tokenizer_id"] == "example/utf8-byte-v1", "Wrong fixture tokenizer")
            require(len(payload["context_text"].encode()) == payload["input_tokens"], "Wrong token count")
            require(not (payload["purpose"] == "act" and payload["decision"] == "ready" and payload["gaps"]), "Unsafe ready bundle")
            for span in payload["reopened"]:
                excerpt = source_bytes[span["source_id"]][span["start_byte"]:span["end_byte"]]
                require(excerpt.decode() in payload["context_text"], "Evidence absent from rendered context")
    return by_id


def expect_rejected(function, label):
    try:
        function()
    except (ValueError, ValidationError):
        return
    raise ValueError(f"Invalid example was accepted: {label}")


def check_negative_cases(records, validator, sources):
    cases = [
        ("unknown core field", 0, lambda r: r.update(unrecognized=True)),
        ("unsupported version", 0, lambda r: r.update(format="asdaif/99")),
        ("unsafe integer", 0, lambda r: r["payload"].update(byte_length=2**53)),
        ("bad source digest", 0, lambda r: r["payload"].update(bytes_sha256="0" * 64)),
        ("inconsistent inventory", 1, lambda r: r["payload"].update(inventory_sha256="0" * 64)),
        ("missing exact rule", 2, lambda r: r["payload"].pop("rule")),
        ("requirement without evidence", 2, lambda r: r["payload"].update(evidence=[])),
        ("empty evidence span", 2, lambda r: r["payload"]["evidence"][0].update(end_byte=0)),
        ("unresolved actor", 2, lambda r: r["payload"]["rule"].update(actor=PREFIX + "missing")),
        ("cross-tenant envelope", 2, lambda r: r.update(tenant="unrelated")),
        ("unsafe ready decision", -1, lambda r: r["payload"].update(decision="ready")),
        ("wrong tokenizer count", -1, lambda r: r["payload"].update(input_tokens=1)),
        ("budget overflow", -1, lambda r: r["payload"].update(budget_tokens=10)),
        ("missing mandatory record", -1, lambda r: r["payload"].update(record_ids=[])),
        ("stale bundle generation", -1, lambda r: r["payload"].update(head_generation=9)),
    ]
    for label, index, mutate in cases:
        changed = deepcopy(records)
        mutate(changed[index])
        expect_rejected(lambda: validate_records(changed, validator, sources), label)
    expect_rejected(lambda: decode_json('{"x":1,"x":2}'), "duplicate JSON key")
    expect_rejected(lambda: decode_json('{"x":NaN}'), "non-JSON numeric constant")
    return len(cases) + 2


def check_four_state_examples():
    states = {"T": (1, 0), "F": (0, 1), "U": (0, 0), "C": (1, 1)}
    names = {value: key for key, value in states.items()}
    order = "TFUC"
    and_rows = ("TFUC", "FFFF", "UFUF", "CFFC")
    or_rows = ("TTTT", "TFUC", "TUUT", "TCTC")
    for i, left_name in enumerate(order):
        a = states[left_name]
        for j, right_name in enumerate(order):
            b = states[right_name]
            conjunction = (a[0] & b[0], a[1] | b[1])
            disjunction = (a[0] | b[0], a[1] & b[1])
            require(names[conjunction] == and_rows[i][j], "Four-state ALL mismatch")
            require(names[disjunction] == or_rows[i][j], "Four-state ANY mismatch")
    return 32


def replace_once(text, old, new):
    require(text.count(old) == 1, f"Ambiguous example revision: {old}")
    return text.replace(old, new)


def run_example(artifacts, revised=False):
    policy = artifacts["policy-python"]
    tests = artifacts["tests-python"]
    if revised:
        policy = replace_once(policy, "max_attempts: int = 3", "max_attempts: int = 2")
        policy = replace_once(policy, "deadline_ms: int = 1000", "deadline_ms: int = 600")
        tests = replace_once(tests, "test_get_has_three_total_attempts", "test_get_has_two_total_attempts")
        tests = replace_once(tests, "(result.status, result.attempts), (503, 3)", "(result.status, result.attempts), (503, 2)")
        tests = replace_once(tests, "result.elapsed_ms, 1000", "result.elapsed_ms, 600")
        tests = replace_once(tests, '[("GET", 1000), ("GET", 450)]', '[("GET", 600), ("GET", 50)]')
    with TemporaryDirectory(prefix="asdaiformat-example-") as temporary:
        root = Path(temporary)
        (root / "policy.py").write_text(policy)
        (root / "client.py").write_text(artifacts["client-python"])
        (root / "test_retry.py").write_text(tests)
        result = subprocess.run(
            [sys.executable, "-m", "unittest", "-v", "test_retry"],
            cwd=root, capture_output=True, text=True, timeout=30,
        )
        require(result.returncode == 0, result.stdout + result.stderr)
        count = re.search(r"Ran (\d+) tests?", result.stderr)
        require(count is not None, "Missing unittest result")
        return int(count.group(1))


def main():
    document = DOCUMENT.read_text()
    blocks = re.findall(r"^```([^\n]*)\n(.*?)^```[ \t]*$", document, re.M | re.S)
    require(document.count("\n```") == 2 * len(blocks), "Unbalanced Markdown fences")
    json_count = 0
    for language, body in blocks:
        if language == "json":
            decode_json(body)
            json_count += 1
    matches = re.findall(
        r"<!-- artifact: ([a-z0-9-]+) -->\n```[^\n]*\n(.*?)^```[ \t]*$",
        document, re.M | re.S,
    )
    artifacts = dict(matches)
    require(len(artifacts) == len(matches), "Duplicate artifact marker")
    schema = decode_json(artifacts["core-schema"])
    Draft202012Validator.check_schema(schema)
    validator = Draft202012Validator(schema, format_checker=FormatChecker())
    records = decode_json(artifacts["example-catalog"])
    records += [decode_json(artifacts[name]) for name in (
        "representation-example", "context-request-example", "context-bundle-example"
    )]
    sources = {
        PREFIX + name: artifacts[name].encode()
        for name in ("source-spec-r0", "source-spec-r2")
    }
    by_id = validate_records(records, validator, sources)
    for name, attempts, duration in (("req-get", 3, 1000), ("req-get-r2", 2, 600)):
        rule = by_id[PREFIX + name]["payload"]["rule"]
        require(rule["action"]["args"][1:] == [
            {"op": "literal", "value": attempts}, {"op": "literal", "value": duration}
        ], "Fixture requirement changed")
        require(f"{attempts} total attempts within {duration} ms".encode() in sources[
            PREFIX + ("source-spec-r0" if name == "req-get" else "source-spec-r2")
        ], "Source and normalized requirement disagree")
    negative_count = check_negative_cases(records, validator, sources)
    state_count = check_four_state_examples()
    anchors = re.findall(r'<a id="([^"]+)"></a>', document)
    require(len(anchors) == len(set(anchors)) == 17, "Invalid section anchors")
    require(set(anchors) == {f"s{i}" for i in range(1, 18)}, "Missing section")
    for target in re.findall(r"\]\(#([^\)]+)\)", document):
        require(target in anchors, f"Broken section link: {target}")
    expected = {"LANG": 10, "SEM": 9, "REP": 5, "DATA": 7, "STORE": 8,
                "CTX": 8, "API": 2, "SEC": 6, "REC": 2, "EVAL": 2, "EVO": 6}
    identifiers = re.findall(r"^\*\*([A-Z]+-\d+) —", document, re.M)
    required = {f"{family}-{n:02d}" for family, count in expected.items()
                for n in range(1, count + 1)}
    require(set(identifiers) == required and len(identifiers) == len(required), "Requirement inventory mismatch")
    require("insertion -->" not in document, "Unfilled document placeholder")
    original_count = run_example(artifacts)
    revised_count = run_example(artifacts, revised=True)
    print(f"PASS: {json_count} JSON blocks; {len(records)} schema-valid records; source hashes, spans, inventories, references, and budgets")
    print(f"PASS: {negative_count} invalid-input checks; {state_count} four-state truth-table cases")
    print(f"PASS: {original_count} original-policy tests; {revised_count} revised-policy tests")
    print(f"PASS: 17 section anchors; {len(required)} numbered requirements")
    print("Scope: document fixtures only; no runtime conformance or comparative benchmark claim.")


if __name__ == "__main__":
    try:
        main()
    except (ValueError, KeyError, ValidationError) as error:
        print(f"FAIL: {error}", file=sys.stderr)
        sys.exit(1)
