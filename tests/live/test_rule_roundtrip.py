"""September 14 regression: Mail discarded a begins-with operator during creation."""

import os
import uuid

import pytest

from apple_mail_mcp.mail_connector import AppleMailConnector

pytestmark = [
    pytest.mark.live,
    pytest.mark.skipif("not config.getoption('--run-live')", reason="Requires --run-live"),
]


def test_disabled_rule_retains_subject_operator():
    """Create a disabled never-matching rule, read native state, then remove only it."""
    if not os.environ.get("MAIL_TEST_ACCOUNT"):
        pytest.skip("Explicit MAIL_TEST_ACCOUNT required")
    connector = AppleMailConnector()
    name = "MCP disabled verification " + str(uuid.uuid4())
    index = connector.create_rule(
        name=name,
        conditions=[
            {"field": "to", "operator": "contains", "value": "never-match@example.invalid"},
            {"field": "subject", "operator": "begins_with", "value": "Accepted:"},
        ],
        actions={"mark_read": True},
        enabled=False,
    )
    try:
        result = connector._run_applescript(f'''tell application "Mail"
set r to rule {index}
if name of r is not "{name}" then error "Rule identity changed"
set c to first rule condition of r whose rule type is subject header
return {{enabled of r, expression of c, qualifier of c as text}}
end tell''')
        assert result == "false, Accepted:, begins with value"
    finally:
        rules = connector.list_rules()
        match = next((r for r in rules if r["name"] == name), None)
        if match:
            connector.delete_rule(match["index"])
