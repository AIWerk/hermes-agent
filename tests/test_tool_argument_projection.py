from agent.tool_argument_projection import project_tool_args_for_display


def test_known_tool_projects_only_allowlisted_keys_and_redacts_secret_text():
    projected = project_tool_args_for_display(
        "terminal",
        {
            "command": "curl -H 'Authorization: Bearer sk-abcdefghijklmnopqrstuvwxyz123456' https://example.test",
            "timeout": 30,
            "api_key": "never-export-this",
            "prompt_text": "also-secret",
        },
    )

    assert set(projected) == {"command", "timeout"}
    assert "abcdefghijklmnopqrstuvwxyz123456" not in projected["command"]
    assert projected["command"] != (
        "curl -H 'Authorization: Bearer sk-abcdefghijklmnopqrstuvwxyz123456' https://example.test"
    )


def test_unknown_tool_fails_closed_without_argument_values():
    assert project_tool_args_for_display(
        "future_secret_tool",
        {"query": "private", "token": "secret", "path": "/private/file"},
    ) == {}


def test_url_fields_strip_userinfo_and_credential_query_parameters():
    projected = project_tool_args_for_display(
        "browser_navigate",
        {"url": "https://alice:opensesame@example.test/path?token=opaque-value&view=full"},
    )

    rendered = projected["url"]
    assert "alice" not in rendered
    assert "opensesame" not in rendered
    assert "opaque-value" not in rendered
    assert "example.test" in rendered
    assert "view=full" in rendered


def test_non_mapping_arguments_fail_closed():
    assert project_tool_args_for_display("terminal", "command=secret") == {}
