"""The model sometimes sends the `items` list as JSON text. Only the shape is repaired."""

import pytest

from ordr.bot.llm import _arguments, _strict

ITEM = {"sku_id": "a", "spin_id": "b", "qty": 1, "heard_as": "haalu"}


def test_items_as_json_text_becomes_the_same_list():
    text = '[{"sku_id": "a", "spin_id": "b", "qty": 1, "heard_as": "haalu"}]'
    assert _arguments({"items": text}) == {"items": [ITEM]}


def test_a_real_list_and_other_tools_are_left_alone():
    assert _arguments({"items": [ITEM]}) == {"items": [ITEM]}
    assert _arguments({"query": "[1]"}) == {"query": "[1]"}


@pytest.mark.parametrize("text", ["not json", "", '{"a": 1}', "5", "[" * 100_000])
def test_anything_else_goes_to_the_gate_unchanged(text):
    assert _arguments({"items": text}) == {"items": text}


@pytest.mark.parametrize("value", [None, "x", 3, [1]])
def test_input_that_is_not_an_object_is_empty(value):
    assert _arguments(value) == {}


def test_strict_mode_keeps_the_schema_and_drops_only_number_limits():
    schema = {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "items": {
                "type": "array",
                "minItems": 1,
                "items": {
                    "type": "object",
                    "additionalProperties": False,
                    "properties": {"qty": {"type": "integer", "exclusiveMinimum": 0}},
                    "required": ["qty"],
                },
            }
        },
        "required": ["items"],
    }
    tool = {"name": "prepare_order", "description": "d", "input_schema": schema}
    strict = _strict(tool)
    assert strict["strict"] is True
    item = strict["input_schema"]["properties"]["items"]["items"]
    assert item["properties"]["qty"] == {"type": "integer"}
    assert item["additionalProperties"] is False and item["required"] == ["qty"]
    assert schema["properties"]["items"]["items"]["properties"]["qty"]["exclusiveMinimum"] == 0
    other = {"name": "search_products", "description": "d", "input_schema": schema}
    assert _strict(other) is other
