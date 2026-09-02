"""Wave 1 detector census: every historical web-server surface is present."""

import ast
from pathlib import Path

import hermes_cli.web_server as web_server


_HISTORICAL_SYMBOLS = (
    'AssistantSupportRequest',
    'AssistantTTSRequest',
    'AssistantTodoAddRequest',
    'AssistantTodoEditRequest',
    'AssistantTodoUpdateRequest',
    'CuiContactCreateRequest',
    'CuiContactHideRequest',
    '_CalendarHtmlToTextParser',
    '_add_todo_item',
    '_admin_api_action_for',
    '_admin_permission_middleware',
    '_assistant_display_name_from_config',
    '_assistant_invalidate_resource_cache',
    '_assistant_support_section',
    '_assistant_ui_locale_from_config',
    '_assistant_user_display_name',
    '_assistant_user_display_name_from_config',
    '_calendar_account_config_for_ref',
    '_clean_assistant_ui_locale',
    '_clean_calendar_reader_body',
    '_clean_dashboard_display_name',
    '_configured_gateway_user_ids',
    '_contact_matches_query',
    '_contact_search_haystack',
    '_contacts_from_gmail_query_blocks',
    '_contacts_from_google_workspace_query_interactions',
    '_cui_actor_owns_gateway_session',
    '_dashboard_user_for_cui_actor',
    '_deliver_support_message',
    '_enforce_admin_api_permission',
    '_enforce_cui_session_visible',
    '_explicit_delivery_targets',
    '_fetch_google_workspace_calendar_event_detail',
    '_fetch_microsoft_calendar_event_detail',
    '_format_support_message',
    '_format_swiss_datetime',
    '_handle_assistant_support',
    '_html_fragment_to_plain_text',
    '_iter_nested_display_candidates',
    '_list_sessions_rich_all',
    '_newline',
    '_open_system_folder',
    '_parse_google_workspace_event_detail',
    '_plain_calendar_reader_html',
    '_plain_email_reader_html',
    '_project_cui_message_rows_public',
    '_project_session_list_rows_public',
    '_read_todo_lines',
    '_redact_sensitive_text',
    '_restore_custom_endpoint_env',
    '_safe_support_diagnostics',
    '_safe_support_multiline',
    '_safe_support_text',
    '_sanitize_public_message_value',
    '_search_contacts_payload',
    '_session_gateway_subject_id',
    '_session_hidden_from_cui_recents',
    '_shared_folder_agent_downloads_html',
    '_snapshot_custom_endpoint_env',
    '_support_delivery_targets',
    '_support_log_path',
    '_system_delivery_targets',
    '_telegram_target_from_chat_id',
    '_todo_line_number',
    '_update_todo_item_done',
    '_update_todo_item_text',
    '_write_contacts_store_payload',
    '_write_hidden_contact_keys',
    '_write_manual_contacts',
    '_write_todo_lines',
    'add_assistant_todo',
    'create_cui_contact',
    'edit_assistant_todo',
    'get_cui_context_contacts',
    'get_cui_frequent_contacts',
    'get_profiles_sessions_sidebar_compat',
    'hide_cui_contact',
    'open_assistant_shared_folder_file',
    'open_assistant_shared_folder_root',
    'request_gate',
    'search_cui_contacts',
    'submit_assistant_support',
    'synthesize_assistant_speech',
    'transcribe_assistant_audio',
    'update_assistant_todo',
    'upload_assistant_attachments',
    'view_assistant_calendar_event',
    'view_assistant_email',
)


def test_wave1_web_server_historical_symbols_are_present():
    source = Path(web_server.__file__).read_text(encoding="utf-8")
    present = {
        node.name
        for node in ast.walk(ast.parse(source))
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))
    }
    missing = [name for name in _HISTORICAL_SYMBOLS if name not in present]
    assert missing == []
