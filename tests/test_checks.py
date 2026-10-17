"""Independent selections, semantic comparisons, and notification regressions."""

import copy

import pytest

from data.plugins.astrbot_plugin_model_watcher.catalog import (
    WatchSpec,
    compare_catalogs,
)
from data.plugins.astrbot_plugin_model_watcher.renderer import format_notification


@pytest.mark.parametrize(
    "value,expected",
    [
        ([], ()),
        (["pricing", "membership", "pricing"], ("membership", "pricing")),
    ],
)
def test_selection_normalization(value, expected):
    spec = WatchSpec.from_entry(
        {
            "entry_id": "one",
            "__template_key": "openrouter",
            "base_url": "https://openrouter.ai/api",
            "comparison_mode": value,
        }
    )
    assert spec.comparison_mode == expected


@pytest.mark.parametrize("value", [True, 1, {}, [None], ["unknown"], "pricing"])
def test_invalid_selection_is_not_silently_replaced(value):
    with pytest.raises(ValueError):
        WatchSpec.from_entry(
            {
                "entry_id": "one",
                "__template_key": "openrouter",
                "base_url": "https://openrouter.ai/api",
                "comparison_mode": value,
            }
        )


@pytest.mark.parametrize(
    "group,before,after,paths",
    [
        (
            "pricing",
            {"pricing": {"prompt": "1"}},
            {"pricing": {"prompt": "2"}},
            {"pricing.prompt"},
        ),
        (
            "context",
            {
                "context_length": 8,
                "top_provider": {"max_completion_tokens": 2, "is_moderated": False},
            },
            {
                "context_length": 16,
                "top_provider": {"max_completion_tokens": 4, "is_moderated": True},
            },
            {"context_length", "top_provider.max_completion_tokens"},
        ),
        (
            "modalities",
            {"architecture": {"input_modalities": ["text"], "tokenizer": "a"}},
            {"architecture": {"input_modalities": ["text", "image"], "tokenizer": "b"}},
            {"architecture.input_modalities"},
        ),
        (
            "parameters",
            {"supported_parameters": []},
            {"supported_parameters": ["tools"]},
            {"supported_parameters"},
        ),
        (
            "reasoning",
            {
                "reasoning": {"mandatory": False},
                "default_parameters": {"temperature": 1},
            },
            {
                "reasoning": {"mandatory": True},
                "default_parameters": {"temperature": 0.5},
            },
            {"reasoning.mandatory", "default_parameters.temperature"},
        ),
        (
            "voices",
            {"supported_voices": ["old"]},
            {"supported_voices": ["new"]},
            {"supported_voices"},
        ),
        (
            "lifecycle",
            {"expiration_date": None},
            {"expiration_date": "2027-01-01"},
            {"expiration_date"},
        ),
        (
            "aliases",
            {"canonical_slug": "a", "alias_target": {"slug": "a", "name": "A"}},
            {"canonical_slug": "b", "alias_target": {"slug": "b", "name": "B"}},
            {"canonical_slug", "alias_target.slug"},
        ),
        (
            "metadata",
            {
                "description": "a",
                "custom": {"version": 1},
                "architecture": {"tokenizer": "a"},
            },
            {
                "description": "b",
                "custom": {"version": 2},
                "architecture": {"tokenizer": "b"},
            },
            {"description", "custom.version", "architecture.tokenizer"},
        ),
        (
            "benchmarks",
            {"benchmarks": {"rank": 1}},
            {"benchmarks": {"rank": 2}},
            {"benchmarks.rank"},
        ),
    ],
)
def test_each_group_selects_only_its_attributes(group, before, after, paths):
    old = {"gone": {}, "same": {**before, "unrelated": 1}}
    new = {"added": {}, "same": {**after, "unrelated": 2}}
    result = compare_catalogs(old, new, (), checks=(group,), openrouter=True)
    assert result["added"] == result["removed"] == []
    expected = paths | ({"unrelated"} if group == "metadata" else set())
    assert {field["path"] for field in result["changed"]["same"]} == expected


def test_combined_categories_ignore_unselected_changes_and_honor_exclusions():
    old = {
        "gone": {},
        "same": {
            "pricing": {"prompt": "1", "completion": "2"},
            "description": "a",
            "expiration_date": None,
        },
    }
    new = {
        "added": {},
        "same": {
            "pricing": {"prompt": "2", "completion": "3"},
            "description": "b",
            "expiration_date": "2027-01-01",
        },
    }
    original = copy.deepcopy((old, new))
    result = compare_catalogs(
        old,
        new,
        ("pricing.completion",),
        checks=("membership", "pricing", "lifecycle"),
        openrouter=True,
    )
    assert result["added"] == ["added"]
    assert result["removed"] == ["gone"]
    assert {field["path"] for field in result["changed"]["same"]} == {
        "pricing.prompt",
        "expiration_date",
    }
    assert (old, new) == original
    assert compare_catalogs(old, new, (), checks=(), openrouter=True) == {
        "added": [],
        "removed": [],
        "changed": {},
    }
    assert compare_catalogs(old, new, ("$",), checks=("pricing",), openrouter=True) == {
        "added": [],
        "removed": [],
        "changed": {},
    }


@pytest.mark.parametrize(
    "before,after",
    [
        ({}, {"architecture": {"input_modalities": ["text"]}}),
        ({"alias_target": {"slug": "old"}}, {}),
        ({"top_provider": {"context_length": 10}}, {}),
    ],
)
def test_metadata_does_not_leak_other_categories_when_parents_appear(before, after):
    assert not compare_catalogs(
        {"m": before}, {"m": after}, (), checks=("metadata",), openrouter=True
    )["changed"]
    assert compare_catalogs(
        {"m": before},
        {"m": after},
        (),
        checks=("modalities", "aliases", "context"),
        openrouter=True,
    )["changed"]


def test_selected_groups_do_not_compare_unclassified_scalar_models():
    assert not compare_catalogs(
        {"m": "old"}, {"m": "new"}, (), checks=("pricing",), openrouter=True
    )["changed"]
    assert compare_catalogs(
        {"m": "old"}, {"m": "new"}, (), checks=("metadata",), openrouter=True
    )["changed"]


def test_set_order_and_price_formatting_do_not_notify():
    old = {
        "m": {
            "pricing": {
                "prompt": "0.0000010",
                "overrides": [{"min_prompt_tokens": 100, "prompt": "0.0000020"}],
            },
            "architecture": {
                "input_modalities": ["text", "image"],
                "output_modalities": ["text", "audio"],
            },
            "supported_parameters": ["tools", "temperature"],
            "supported_voices": ["a", "b"],
            "reasoning": {"supported_efforts": ["low", "high"]},
        }
    }
    new = copy.deepcopy(old)
    new["m"]["pricing"]["prompt"] = "1e-6"
    new["m"]["pricing"]["overrides"][0]["prompt"] = "0.000002"
    for path in ("input_modalities", "output_modalities"):
        new["m"]["architecture"][path].reverse()
    for path in ("supported_parameters", "supported_voices"):
        new["m"][path].reverse()
    new["m"]["reasoning"]["supported_efforts"].reverse()
    assert not compare_catalogs(old, new, (), openrouter=True)["changed"]


@pytest.mark.parametrize(
    "old,new", [(None, "0"), (False, "0"), ("NaN", "0"), ("1", "1.01")]
)
def test_prices_retain_unknown_zero_and_actual_change_distinctions(old, new):
    assert compare_catalogs(
        {"m": {"pricing": {"prompt": old}}},
        {"m": {"pricing": {"prompt": new}}},
        (),
        checks=("pricing",),
        openrouter=True,
    )["changed"]


def test_missing_price_is_not_zero():
    changes = compare_catalogs(
        {"m": {"pricing": {}}},
        {"m": {"pricing": {"prompt": "0"}}},
        (),
        checks=("pricing",),
        openrouter=True,
    )["changed"]["m"]
    assert changes[0]["old_exists"] is False


def test_existing_schedule_switches_are_silent_but_rule_changes_are_visible():
    rules = [
        {"utc_start": 0, "utc_end": 1200, "prompt": "1"},
        {"utc_start": 1200, "utc_end": 0, "prompt": "2"},
    ]
    old = {"m": {"pricing": {"prompt": "1", "overrides": rules}}}
    new = copy.deepcopy(old)
    new["m"]["pricing"]["prompt"] = "2.00"
    assert not compare_catalogs(old, new, (), checks=("pricing",), openrouter=True)[
        "changed"
    ]
    assert compare_catalogs(old, new, (), checks=("pricing",), openrouter=False)[
        "changed"
    ]
    new["m"]["pricing"]["overrides"][0]["prompt"] = "1.5"
    changes = compare_catalogs(old, new, (), checks=("pricing",), openrouter=True)[
        "changed"
    ]
    assert {field["path"] for field in changes["m"]} == {
        "pricing.prompt",
        "pricing.overrides",
    }
    new = copy.deepcopy(old)
    new["m"]["pricing"]["overrides"].reverse()
    assert compare_catalogs(old, new, (), checks=("pricing",), openrouter=True)[
        "changed"
    ]


@pytest.mark.parametrize(
    "extra", [{"min_prompt_tokens": 100}, {"unknown_condition": True}]
)
def test_conditional_or_unknown_prices_are_not_mistaken_for_schedule_switches(extra):
    rules = [
        {"utc_start": 0, "utc_end": 1200, "prompt": "1", **extra},
        {"utc_start": 1200, "utc_end": 0, "prompt": "2", **extra},
    ]
    old = {"m": {"pricing": {"prompt": "1", "overrides": rules}}}
    new = copy.deepcopy(old)
    new["m"]["pricing"]["prompt"] = "2"
    assert compare_catalogs(old, new, (), checks=("pricing",), openrouter=True)[
        "changed"
    ]


def test_notifications_label_groups_and_show_list_deltas_and_unknown_values():
    changes = compare_catalogs(
        {
            "m": {
                "supported_parameters": ["seed", "temperature"],
                "expiration_date": None,
            }
        },
        {
            "m": {
                "supported_parameters": ["temperature", "tools"],
                "expiration_date": "2027-01-01",
            }
        },
        (),
        checks=("parameters", "lifecycle"),
        openrouter=True,
    )
    text = format_notification(
        {
            "name": "Test",
            "time": "2026-10-17",
            "count": 1,
            "changes": changes,
            "openrouter": True,
        }
    )
    assert "工具调用与支持参数" in text
    assert '新增 ["tools"]' in text
    assert '移除 ["seed"]' in text
    assert "temperature" not in text
    assert "信息补全" in text
