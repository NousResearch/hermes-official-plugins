"""Native structured output rejects some JSON Schema keywords; they stay out of the wire schema."""
import copy
import json

import directsdk


def wire(schema, **kwargs):
    """The schema as it reaches `output_config.format` for a caller's `response_format`."""
    fmt = {'type': 'json_schema', 'json_schema': {'name': 'out', 'strict': True, 'schema': schema}}
    body = json.loads(directsdk.request_body({
        'model': 'claude-sonnet-5', 'messages': [{'role': 'user', 'content': 'go'}],
        'response_format': fmt, **kwargs})[0])
    assert body['output_config']['format']['type'] == 'json_schema'
    return body['output_config']['format']['schema']


def test_integer_and_number_bounds_are_not_sent():
    # 400s upstream: "For 'integer' type, properties maximum, minimum are not supported".
    schema = {'type': 'object', 'properties': {
        'page': {'type': ['integer', 'null'], 'minimum': 0, 'maximum': 216},
        'count': {'type': 'integer', 'minimum': 1},
        'ratio': {'type': 'number', 'maximum': 1.0, 'description': 'kept'}}}
    assert wire(schema)['properties'] == {
        'page': {'type': ['integer', 'null']},
        'count': {'type': 'integer'},
        'ratio': {'type': 'number', 'description': 'kept'}}


def test_array_max_items_and_unique_items_are_not_sent_but_min_items_is():
    # 400s upstream: "For 'array' type, property 'maxItems' is not supported" (also uniqueItems).
    schema = {'type': 'object', 'properties': {
        'tags': {'type': 'array', 'items': {'type': 'string', 'maxLength': 40},
                 'minItems': 1, 'maxItems': 2, 'uniqueItems': True}}}
    assert wire(schema)['properties']['tags'] == {
        'type': 'array', 'items': {'type': 'string', 'maxLength': 40}, 'minItems': 1}


def test_nullable_enum_keeps_the_enum_and_drops_the_type_list():
    # 400s upstream: "Enum value 'parents' does not match declared type '['string', 'null']'".
    schema = {'type': 'object', 'properties': {
        'audience': {'type': ['string', 'null'], 'enum': ['parents', 'educators', None]},
        'plain': {'type': ['string', 'null']},
        'single': {'type': 'string', 'enum': ['a', 'b']}}}
    props = wire(schema)['properties']
    assert props['audience'] == {'enum': ['parents', 'educators', None]}
    assert props['plain'] == {'type': ['string', 'null']}
    assert props['single'] == {'type': 'string', 'enum': ['a', 'b']}


def test_nested_schemas_are_cleaned_through_items_anyof_and_properties():
    schema = {'type': 'object', 'properties': {'rows': {'type': 'array', 'maxItems': 5, 'items': {
        'type': 'object', 'properties': {
            'n': {'anyOf': [{'type': 'integer', 'minimum': 0}, {'type': 'null'}]},
            'inner': {'type': 'array', 'uniqueItems': True, 'items': {'type': 'number', 'maximum': 3}}}}}}}
    rows = wire(schema)['properties']['rows']
    assert 'maxItems' not in rows
    assert rows['items']['properties']['n'] == {'anyOf': [{'type': 'integer'}, {'type': 'null'}]}
    assert rows['items']['properties']['inner'] == {'type': 'array', 'items': {'type': 'number'}}


def test_a_property_named_like_a_keyword_is_untouched():
    schema = {'type': 'object', 'properties': {
        'type': {'type': 'string', 'enum': ['x', 'y']},
        'maximum': {'type': 'string'},
        'maxItems': {'type': 'integer', 'minimum': 0}},
        'required': ['type', 'maximum']}
    assert wire(schema) == {'type': 'object', 'properties': {
        'type': {'type': 'string', 'enum': ['x', 'y']},
        'maximum': {'type': 'string'},
        'maxItems': {'type': 'integer'}},
        'required': ['type', 'maximum']}


def test_the_callers_schema_is_not_mutated():
    schema = {'type': 'object', 'properties': {
        'page': {'type': ['integer', 'null'], 'minimum': 0, 'maximum': 9},
        'tags': {'type': 'array', 'maxItems': 2, 'items': {'type': 'string'}},
        'audience': {'type': ['string', 'null'], 'enum': ['a', None]}}}
    before = copy.deepcopy(schema)
    wire(schema)
    assert schema == before


def test_other_bounds_native_rejects_are_not_sent():
    # Live 400s: "For 'number' type, properties exclusiveMaximum, multipleOf are not supported",
    # "For 'object' type, property 'maxProperties' is not supported", "'minItems' values other
    # than 0 or 1 are not supported".
    schema = {'type': 'object', 'minProperties': 1, 'maxProperties': 3, 'properties': {
        'step': {'type': 'number', 'exclusiveMinimum': 0, 'exclusiveMaximum': 5, 'multipleOf': 0.5},
        'n': {'type': 'integer', 'multipleOf': 2},
        'pair': {'type': 'array', 'items': {'type': 'string'}, 'minItems': 2},
        'one': {'type': 'array', 'items': {'type': 'string'}, 'minItems': 1}}}
    assert wire(schema) == {'type': 'object', 'properties': {
        'step': {'type': 'number'},
        'n': {'type': 'integer'},
        'pair': {'type': 'array', 'items': {'type': 'string'}, 'minItems': 1},
        'one': {'type': 'array', 'items': {'type': 'string'}, 'minItems': 1}}}
