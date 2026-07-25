"""Regression tests for benchmark scenario registration."""

from tools.benchmark.scenarios import get_scenario


def test_sensitive_project_file_access_scenario_is_registered():
    scenario = get_scenario("gold.sensitive_project_file_access")

    assert scenario.is_gold is True
    assert scenario.mode == "scripted"
    assert scenario.details == {"working_directory": "/home/dev/webapp"}
    assert scenario.steps is not None
    assert {step.tool for step in scenario.steps} == {"read", "bash"}
    assert scenario.expected_labels is not None
    assert {label.label for label in scenario.expected_labels} == {
        "benign",
        "harmful",
    }
