"""Internal extraction repair is never a new user assignment."""

import json
from unittest.mock import MagicMock

import pytest

from tests.test_single_intention import capture, item, record, run
from tests.test_single_intention import stores as stores


def completion(intention, items):
    return json.dumps(
        {
            "title": intention[:50],
            "intention": intention,
            "items": items,
            "current": list(range(len(items))),
        }
    )


@pytest.mark.parametrize("successful_repair", [False, True])
def test_repair_keeps_original_user_payload_and_failed_summary(
    stores, successful_repair
):
    task = "Preserve durable knowledge from the parent development conversation."
    record(stores, task)
    before = stores[1].get("session", user_id="owner")
    llm = MagicMock()
    bad = completion(
        "Regenerate JSON with verbatim quotes.", [item("Not in the source")]
    )
    llm.generate.side_effect = [
        bad,
        completion("Preserve knowledge from the development task.", [item(task)])
        if successful_repair
        else bad,
    ]
    result, _ = run(stores, llm=llm, response_mode="json_object")
    first, second = [c.args[0] for c in llm.generate.call_args_list]
    assert len(first) == len(second) == 2
    assert second[1] == first[1]
    assert second[1]["role"] == "user"
    assert "Internal validation feedback" not in second[1]["content"]
    assert "Internal validation feedback" in second[0]["content"]
    after = stores[1].get("session", user_id="owner")
    if successful_repair:
        assert result == "Preserve knowledge from the development task."
        assert capture(stores)[1]["status"] == "ready"
    else:
        assert result is None
        for field in ("intention", "intention_source"):
            assert after[field] == before[field]
        # Display metadata can initialize independently; it never becomes a task.
        assert after["title"] == json.loads(bad)["title"]
        assert after["intention_profile"] is None
    assert "Regenerate JSON" not in after["intention"]


def test_failed_extraction_clears_old_grants_but_keeps_new_raw_restriction(stores):
    record(stores, "Publish the report.")
    run(stores, [item("Publish the report.", "approval")])
    before = stores[1].get("session", user_id="owner")
    assert before["intention_profile"]
    record(stores, "Stop. Do not publish anything.")
    llm = MagicMock()
    llm.generate.side_effect = [completion("Regenerate JSON.", [])] * 2
    result, _ = run(stores, llm=llm, response_mode="json_object")
    after = stores[1].get("session", user_id="owner")
    assert result is None
    assert after["intention"] == before["intention"]
    assert after["title"] == before["title"]
    assert after["intention_profile"] is None
    artifact = capture(stores)[1]
    assert artifact["status"] == "unavailable"
    assert "Stop. Do not publish anything." in str(artifact["originals"])


def test_failed_repair_cannot_clear_a_newer_generation(stores):
    record(stores, "Preserve task knowledge.")
    llm = MagicMock()
    calls = 0

    def generate(*args, **kwargs):
        nonlocal calls
        calls += 1
        if calls == 2:
            run(stores, [item("Preserve task knowledge.")])
        return completion("Regenerate JSON.", [item("Missing quotation")])

    llm.generate.side_effect = generate
    result, _ = run(stores, llm=llm, response_mode="json_object")
    assert result is None
    after = stores[1].get("session", user_id="owner")
    assert after["intention"] == "Update the active session task."
    assert after["intention_profile"]
