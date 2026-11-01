"""Paragraph requests and display labels do not weaken authority validation."""

import json
from unittest.mock import MagicMock

import pytest

from intaris.intention_profile import validate_items
from tests.test_single_intention import capture, item, record, run
from tests.test_single_intention import stores as stores

REQUEST = "debug this error `Overview response scope does not match the request`"
MESSAGE = REQUEST + "\n\nSeen in a nested sidebar\nMain -> Planner -> Worker"


def answer(
    items, *, title="Debug nested sidebar", intention="Debug the sidebar error."
):
    return json.dumps(
        {
            "title": title,
            "intention": intention,
            "items": items,
            "current": [n for n, row in enumerate(items) if row["kind"] == "request"],
        }
    )


def validate(text, quote, kind="request"):
    return validate_items(
        [item(quote, kind)],
        [
            {
                "ref": "message:paragraph",
                "origin": "recorded_user_event",
                "content": text,
            }
        ],
    )


@pytest.mark.parametrize("separator", ["\n\n", "\r\n\r\n", "\n \t\n"])
def test_unpunctuated_whole_request_paragraph_is_valid(separator):
    text = REQUEST + separator + "Observed in the nested sidebar"
    accepted, dropped = validate(text, REQUEST)
    assert dropped == 0
    assert accepted[0]["quote"] == REQUEST


@pytest.mark.parametrize(
    "text,quote",
    [
        (
            "Inspect the error\nwithout deploying\n\nObserved in sidebar",
            "Inspect the error",
        ),
        (
            "Inspect the error without deploying\n\nObserved in sidebar",
            "Inspect the error",
        ),
        (
            "Prefix to request\ninspect the error\n\nObserved in sidebar",
            "inspect the error",
        ),
        ("Deploy only if:\n\nChecks pass", "Deploy only if:"),
        ("Inspect this,\n\nwithout changing anything", "Inspect this,"),
        ("```\nInspect error\n\nDetails\n```", "Inspect error"),
        ("> Inspect error\n\nDetails", "Inspect error"),
    ],
)
def test_paragraph_acceptance_does_not_allow_partial_or_embedded_requests(text, quote):
    accepted, dropped = validate(text, quote)
    assert not accepted
    assert dropped == 1


@pytest.mark.parametrize("kind", ["approval", "restriction", "revocation"])
def test_paragraph_rule_never_expands_permission_acceptance(kind):
    assert validate("Deploy only if\n\nChecks pass", "Deploy only if", kind) == ([], 1)


@pytest.mark.parametrize("response_mode", ["json_schema", "json_object"])
def test_request_context_and_restrictions_persist_without_repair(stores, response_mode):
    text = MESSAGE + "\n\nDo not deploy."
    record(stores, text)
    llm = MagicMock()
    llm.generate.side_effect = [
        answer(
            [
                item(REQUEST),
                item(text, "context"),
                item("Do not deploy.", "restriction"),
            ]
        )
    ]
    assert run(stores, llm=llm, response_mode=response_mode)[0]
    llm.generate.assert_called_once()
    row = stores[1].get("session", user_id="owner")
    assert row["title"] == "Debug nested sidebar"
    assert row["intention_source"] == "user"
    artifact = capture(stores)[1]
    assert artifact["status"] == "ready"
    assert artifact["items"][1]["quote"] == text
    assert artifact["items"][2]["quote"] == "Do not deploy."


@pytest.mark.parametrize("response_mode", ["json_schema", "json_object"])
def test_partial_statement_gets_one_actionable_source_preserving_repair(
    stores, response_mode
):
    text = "Inspect the error\nwithout deploying\n\nObserved in sidebar"
    record(stores, text)
    llm = MagicMock()
    llm.generate.side_effect = [
        answer([item("Inspect the error")]),
        answer([item("Inspect the error\nwithout deploying"), item(text, "context")]),
    ]
    assert run(stores, llm=llm, response_mode=response_mode)[0]
    first, second = [call.args[0] for call in llm.generate.call_args_list]
    assert len(llm.generate.call_args_list) == 2
    assert first[1] == second[1]
    assert "Quote the complete original paragraph" in second[0]["content"]


@pytest.mark.parametrize("response_mode", ["json_schema", "json_object"])
@pytest.mark.parametrize("intention", ["Generated summary.", ""])
def test_failed_profile_still_initializes_title_but_never_authority(
    stores, response_mode, intention
):
    record(stores, "Inspect error only. Do not deploy.")
    before = stores[1].get("session", user_id="owner")
    llm, bus, events = MagicMock(), MagicMock(), MagicMock()
    llm.generate.side_effect = [
        answer([item("Never present in source")], intention=intention)
    ] * 2
    assert (
        run(
            stores,
            llm=llm,
            response_mode=response_mode,
            event_bus=bus,
            event_store=events,
        )[0]
        is None
    )
    after = stores[1].get("session", user_id="owner")
    assert after["title"] == "Debug nested sidebar"
    assert after["intention"] == before["intention"]
    assert after["intention_source"] == before["intention_source"]
    assert after["intention_profile"] is None
    assert "Do not deploy." in str(capture(stores)[1]["originals"])
    bus.publish.assert_called_once_with(
        {
            "type": "session_updated",
            "session_id": "session",
            "user_id": "owner",
            "title": "Debug nested sidebar",
        }
    )
    events.append.assert_not_called()


@pytest.mark.parametrize(
    "race", ["manual", "new_message", "new_generation", "superseded"]
)
def test_failed_profile_title_obeys_existing_durable_and_generation_guards(
    stores, race
):
    record(stores, "Inspect error only.")
    llm, bus = MagicMock(), MagicMock()

    def generate(*_, **__):
        if race == "manual":
            stores[1].update_session("session", user_id="owner", title="Manual title")
        elif race == "new_message":
            stores[1].observe_user_message("session", user_id="owner", event_seq=100)
        elif race == "new_generation":
            run(stores, [item("Inspect error only.")])
        return answer([item("Absent quote")])

    llm.generate.side_effect = generate
    assert (
        run(
            stores,
            llm=llm,
            event_bus=bus,
            freshness_check=(lambda: False) if race == "superseded" else None,
        )[0]
        is None
    )
    after = stores[1].get("session", user_id="owner")
    assert after["title"] != "Debug nested sidebar"
    if race == "manual":
        assert after["title"] == "Manual title"
    if race == "new_generation":
        assert after["intention_profile"]
    bus.publish.assert_not_called()


def test_existing_title_preserved_on_failed_profile(stores):
    record(stores, "Inspect error only.")
    stores[1].update_session("session", user_id="owner", title="Existing title")
    llm, bus = MagicMock(), MagicMock()
    llm.generate.side_effect = [answer([item("Absent quote")])] * 2
    run(stores, llm=llm, event_bus=bus)
    assert stores[1].get("session", user_id="owner")["title"] == "Existing title"
    bus.publish.assert_not_called()
