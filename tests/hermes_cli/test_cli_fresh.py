from types import SimpleNamespace

from cli import HermesCLI


def _cli(history=None):
    cli = HermesCLI.__new__(HermesCLI)
    cli.conversation_history = history or []
    cli.session_id = "source-session"
    return cli


def test_fresh_rejects_nonpositive_message_count():
    assert _cli()._parse_fresh_message_count("/fresh 0") is None


def test_fresh_rejects_extra_arguments():
    assert _cli()._parse_fresh_message_count("/fresh 5 extra") is None


def test_fresh_caps_message_count():
    cli = _cli()
    assert cli._parse_fresh_message_count("/fresh 999999") == cli._FRESH_MAX_MESSAGES


def test_fresh_flattens_multipart_message_content():
    cli = _cli(
        [
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": "first"},
                    {"type": "image_url", "image_url": {"url": "data:image/png;base64,..."}},
                    {"type": "text", "text": "second"},
                ],
            }
        ]
    )
    block, carried = cli._build_fresh_carryover_context(1)
    assert carried == 1
    assert "first\n[image_url]\nsecond" in block
    assert "data:image" not in block
