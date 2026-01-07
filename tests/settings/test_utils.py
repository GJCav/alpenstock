import pytest
import alpenstock.settings.utils as su
from types import MappingProxyType

def test_var_placeholder_match():
    # Basic cases
    m = su.match_env_var_placeholder("${MY_ENV}")
    assert m is not None
    assert m.group(1) == "MY_ENV"
    
    m = su.match_env_var_placeholder("$ANOTHER_ENV")
    assert m is not None
    assert m.group(1) == "ANOTHER_ENV"
    
    # Numbers in names
    m = su.match_env_var_placeholder("${ENV_123}")
    assert m is not None
    assert m.group(1) == "ENV_123"
    
    m = su.match_env_var_placeholder("$ENV456")
    assert m is not None
    assert m.group(1) == "ENV456"
    
    m = su.match_env_var_placeholder("${555}")
    assert m is not None
    assert m.group(1) == "555"
    
    m = su.match_env_var_placeholder("$666")
    assert m is not None
    assert m.group(1) == "666"
    
    # Leading/trailing spaces
    m = su.match_env_var_placeholder("  ${MY_ENV}  ")
    assert m is not None
    assert m.group(1) == "MY_ENV"
    
    m = su.match_env_var_placeholder("  $ANOTHER_ENV  ")
    assert m is not None
    assert m.group(1) == "ANOTHER_ENV"

    # No '$' or malformed cases
    m = su.match_env_var_placeholder("NO_ENV_VAR")
    assert m is None
    
    m = su.match_env_var_placeholder("prefix_${ENV}_suffix")
    assert m is None
    
    m = su.match_env_var_placeholder("$$DOUBLE_DOLLAR")
    assert m is None
    
    m = su.match_env_var_placeholder("${}")
    assert m is None
    
    m = su.match_env_var_placeholder("$")
    assert m is None


def test_replace_env_vars_basic(monkeypatch):
    monkeypatch.setenv("TEST_ENV", "success")
    monkeypatch.setenv("ANOTHER_ENV", "42")
    
    # Test primitive types
    assert su.replace_env_vars("${TEST_ENV}") == "success"
    assert su.replace_env_vars("$ANOTHER_ENV") == "42"
    assert su.replace_env_vars("No env here") == "No env here"
    assert su.replace_env_vars("Value is $ANOTHER_ENV") == "Value is $ANOTHER_ENV"
    assert su.replace_env_vars("$MISSING_ENV") == ""
    assert su.replace_env_vars(123) == 123
    assert su.replace_env_vars(45.6) == 45.6
    assert su.replace_env_vars(True) is True
    assert su.replace_env_vars(None) is None
    
    # Test common mutable mapping class: dict
    data = {
        'key1': '${TEST_ENV}',             # should be replaced
        'key2': 'Value is $ANOTHER_ENV',   # should NOT be replaced
        'key3': 'No env here',             # should NOT be replaced
        'key4': '$MISSING_ENV',            # should be replaced with ""
        'key5': 123,                       # non-string, should remain unchanged
    }
    data = su.replace_env_vars(data)
    assert data['key1'] == "success"
    assert data['key2'] == "Value is $ANOTHER_ENV"
    assert data['key3'] == "No env here"
    assert data['key4'] == ""
    assert data['key5'] == 123
    
    # Test common mutable sequence class: list
    data = ['${TEST_ENV}', 'Value is $ANOTHER_ENV', '$MISSING_ENV', 789]
    data = su.replace_env_vars(data)
    assert data[0] == "success"
    assert data[1] == "Value is $ANOTHER_ENV"
    assert data[2] == ""
    assert data[3] == 789

    # Test common immutable sequence class: tuple
    data = ('${TEST_ENV}', 'Value is $ANOTHER_ENV', '$MISSING_ENV', 789)
    with pytest.raises(TypeError) as excinfo:
        su.replace_env_vars(data)
    assert "Unsupported collection type" in str(excinfo.value)

    # Test common immutable mapping class: MappingProxyType
    data = MappingProxyType({
        'key1': '${TEST_ENV}',
    })
    with pytest.raises(TypeError) as excinfo:
        su.replace_env_vars(data)
    assert "Unsupported collection type" in str(excinfo.value)
    
    # Test other common collection types but not mutable mappings or sequences,
    # for example, set
    data = {'${TEST_ENV}', '$ANOTHER_ENV'}
    with pytest.raises(TypeError) as excinfo:
        su.replace_env_vars(data)
    assert "Unsupported collection type" in str(excinfo.value)
    
    
    # Test nested structures
    data = {
        'a': '${TEST_ENV}',
        12: '$ANOTHER_ENV',
        None: '$MISSING_ENV',
        'list': [
            '${TEST_ENV}',
            {
                'nested_key': '$ANOTHER_ENV'
            },
            '$MISSING_ENV'
        ]
    }
    data = su.replace_env_vars(data)
    assert data['a'] == "success"
    assert data[12] == "42"
    assert data[None] == ""
    assert data['list'][0] == 'success'
    assert data['list'][1]['nested_key'] == "42"
    assert data['list'][2] == ""
    