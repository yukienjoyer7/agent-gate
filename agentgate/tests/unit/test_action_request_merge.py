from app.core.action_request import deep_merge_payload


def test_flat_keys_behave_like_shallow_merge():
    base = {"a": 1, "b": 2}
    updates = {"b": 3, "c": 4}
    assert deep_merge_payload(base, updates) == {"a": 1, "b": 3, "c": 4}


def test_nested_dict_preserves_sibling_keys():
    base = {"recipient": {"email": "a@x.com", "name": "A"}}
    updates = {"recipient": {"email": "b@x.com"}}
    merged = deep_merge_payload(base, updates)
    assert merged == {"recipient": {"email": "b@x.com", "name": "A"}}


def test_deeply_nested_dicts_merge_recursively():
    base = {"filters": {"date": {"from": "2024-01-01", "to": "2024-12-31"}, "status": "open"}}
    updates = {"filters": {"date": {"from": "2024-06-01"}}}
    merged = deep_merge_payload(base, updates)
    assert merged == {
        "filters": {"date": {"from": "2024-06-01", "to": "2024-12-31"}, "status": "open"}
    }


def test_list_values_are_replaced_not_merged():
    base = {"tags": ["a", "b"]}
    updates = {"tags": ["c"]}
    assert deep_merge_payload(base, updates) == {"tags": ["c"]}


def test_scalar_replacing_dict_replaces_wholesale():
    base = {"recipient": {"email": "a@x.com"}}
    updates = {"recipient": "b@x.com"}
    assert deep_merge_payload(base, updates) == {"recipient": "b@x.com"}


def test_dict_replacing_scalar_sets_wholesale():
    base = {"recipient": "a@x.com"}
    updates = {"recipient": {"email": "b@x.com"}}
    assert deep_merge_payload(base, updates) == {"recipient": {"email": "b@x.com"}}


def test_empty_updates_returns_equivalent_copy_not_same_object():
    base = {"a": 1}
    merged = deep_merge_payload(base, {})
    assert merged == base
    assert merged is not base


def test_base_is_not_mutated():
    base = {"recipient": {"email": "a@x.com", "name": "A"}}
    deep_merge_payload(base, {"recipient": {"email": "b@x.com"}})
    assert base == {"recipient": {"email": "a@x.com", "name": "A"}}
