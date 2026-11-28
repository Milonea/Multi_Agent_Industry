"""Validate the deliberately small JSON Schema vocabulary of delegation contracts."""
import json
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def validate(value, kind):
    schema = json.loads((ROOT / '.agents/contracts' / f'delegation-{kind}.schema.json').read_text())

    def check(item, rule):
        types = {'object': dict, 'array': list, 'string': str, 'boolean': bool,
                 'integer': int, 'null': type(None)}
        allowed = rule.get('type', [])
        allowed = [allowed] if isinstance(allowed, str) else allowed
        if allowed and not any(isinstance(item, types[t]) and not (t == 'integer' and isinstance(item, bool)) for t in allowed):
            raise ValueError('delegation contract type mismatch')
        if 'const' in rule and item != rule['const'] or 'enum' in rule and item not in rule['enum']:
            raise ValueError('delegation contract value mismatch')
        if isinstance(item, dict):
            props = rule.get('properties', {})
            if not set(rule.get('required', [])) <= item.keys() or rule.get('additionalProperties') is False and item.keys() - props.keys():
                raise ValueError('delegation contract fields mismatch')
            for key in item.keys() & props.keys():
                check(item[key], props[key])
        if isinstance(item, list):
            if len(item) < rule.get('minItems', 0):
                raise ValueError('delegation contract array too short')
            for child in item:
                check(child, rule.get('items', {}))
        if isinstance(item, str) and (len(item) < rule.get('minLength', 0) or 'pattern' in rule and re.search(rule['pattern'], item) is None):
            raise ValueError('delegation contract string mismatch')
        if isinstance(item, int) and item < rule.get('minimum', item):
            raise ValueError('delegation contract number mismatch')
    check(value, schema)
    return value
