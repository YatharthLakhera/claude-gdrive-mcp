"""
Custom Google Drive MCP Server.

A slimmed-down fork of taylorwilsdon/google_workspace_mcp that exposes only the
Drive tool surface plus the auth/scopes machinery needed to OAuth into Drive.

Two divergences from upstream:

1. Claude.ai-compatible tool surface. The four overlapping tools are renamed
   and reshaped to match Claude.ai's Google Drive connector contract:
   `search_files`, `read_file_content`, `download_file_content`, `create_file`.
   Two new tools (`list_recent_files`, `get_file_metadata`) are added to
   complete the surface. The remaining 10 original tools are preserved
   unchanged. See README.md for full details.

2. Mime-aware `create_file` (was `create_drive_file`) auto-detects encoding
   from `mimeType` so binary uploads (xlsx, pptx, pdf, ...) can be passed via
   `content=<base64-string>` and land in Drive as valid binary bytes. Upstream
   encodes everything as UTF-8 and corrupts binary.

Run locally:
    uv run main.py --transport streamable-http
or
    uv run main.py        # stdio

Deploy the public Drive surface on Railway with --transport streamable-http.
"""
import io
import argparse
import json
import logging
import os
import socket
import sys
from importlib import metadata, import_module
from dotenv import load_dotenv

_original_stdout = sys.stdout
if sys.platform == "darwin":
    sys.stdout = io.StringIO()


def _load_startup_dependencies():
    from auth.credential_store import get_credential_store, get_selected_backend
    from auth.oauth_config import (
        reload_oauth_config,
        is_stateless_mode,
        is_service_account_enabled,
        get_oauth_config,
    )
    from core.log_formatter import EnhancedLogFormatter, configure_file_logging
    from core.utils import check_credentials_directory_permissions
    from core.server import server, set_transport_mode, configure_server_for_http
    from core.tool_registry import (
        set_enabled_tools as set_enabled_tool_names,
        wrap_server_tool_method,
        filter_server_tools,
    )

    return (
        get_selected_backend,
        get_credential_store,
        get_oauth_config,
        reload_oauth_config,
        is_stateless_mode,
        is_service_account_enabled,
        EnhancedLogFormatter,
        configure_file_logging,
        check_credentials_directory_permissions,
        server,
        set_transport_mode,
        configure_server_for_http,
        set_enabled_tool_names,
        wrap_server_tool_method,
        filter_server_tools,
    )


(
    get_selected_backend,
    get_credential_store,
    get_oauth_config,
    reload_oauth_config,
    is_stateless_mode,
    is_service_account_enabled,
    EnhancedLogFormatter,
    configure_file_logging,
    check_credentials_directory_permissions,
    server,
    set_transport_mode,
    configure_server_for_http,
    set_enabled_tool_names,
    wrap_server_tool_method,
    filter_server_tools,
) = _load_startup_dependencies()

dotenv_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env")
load_dotenv(dotenv_path=dotenv_path)

logging.getLogger("googleapiclient.discovery_cache").setLevel(logging.ERROR)
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("httpcore").setLevel(logging.WARNING)

reload_oauth_config()

logging.basicConfig(
    level=logging.INFO, format="%(asctime)s - %(name)s - %(levelname)s - %(message)s"
)
logger = logging.getLogger(__name__)

configure_file_logging()

SERVICE_MODULES = {
    "drive": "gdrive.drive_tools",
    "sheets": "gsheets.sheets_tools",
    "slides": "gslides.slides_tools",
}


def safe_print(text):
    if not sys.stderr.isatty():
        logger.debug(f"[MCP Server] {text}")
        return
    try:
        print(text, file=sys.stderr)
    except UnicodeEncodeError:
        print(text.encode("ascii", errors="replace").decode(), file=sys.stderr)


def configure_safe_logging():
    class SafeEnhancedFormatter(EnhancedLogFormatter):
        def format(self, record):
            try:
                return super().format(record)
            except UnicodeEncodeError:
                service_prefix = self._get_ascii_prefix(record.name, record.levelname)
                safe_msg = (
                    str(record.getMessage())
                    .encode("ascii", errors="replace")
                    .decode("ascii")
                )
                return f"{service_prefix} {safe_msg}"

    for handler in logging.root.handlers:
        if isinstance(handler, logging.StreamHandler) and handler.stream.name in [
            "<stderr>",
            "<stdout>",
        ]:
            handler.setFormatter(SafeEnhancedFormatter(use_colors=True))


def _restore_stdout() -> None:
    captured_stdout = sys.stdout
    if captured_stdout is _original_stdout:
        return
    captured = ""
    required_stringio_methods = ("getvalue", "write", "flush")
    try:
        if all(
            callable(getattr(captured_stdout, method_name, None))
            for method_name in required_stringio_methods
        ):
            captured = captured_stdout.getvalue()
    finally:
        sys.stdout = _original_stdout
    if captured:
        print(captured, end="", file=sys.stderr)


def main():
    _restore_stdout()
    configure_safe_logging()

    parser = argparse.ArgumentParser(description="Custom Google Drive MCP Server")
    parser.add_argument(
        "--single-user",
        action="store_true",
        help="Single-user mode - bypass session mapping and use any credentials from the credentials directory",
    )
    parser.add_argument(
        "--transport",
        choices=["stdio", "streamable-http"],
        default=None,
        help="Transport mode (default: stdio; override via WORKSPACE_MCP_TRANSPORT)",
    )
    parser.add_argument(
        "--read-only",
        action="store_true",
        help="Read-only mode - drop write scopes and disable mutating tools",
    )
    args = parser.parse_args()

    if not args.read_only:
        _env_ro = os.getenv("WORKSPACE_MCP_READ_ONLY", "").strip().lower()
        if _env_ro in {"true", "1", "yes"}:
            args.read_only = True

    if args.transport is None:
        _env_transport = os.getenv("WORKSPACE_MCP_TRANSPORT", "").strip().lower()
        if _env_transport in {"stdio", "streamable-http"}:
            args.transport = _env_transport
        else:
            args.transport = "stdio"

    port = int(os.getenv("PORT", os.getenv("WORKSPACE_MCP_PORT", 8000)))
    base_uri = os.getenv("WORKSPACE_MCP_BASE_URI", "http://localhost")
    host = os.getenv("WORKSPACE_MCP_HOST", "0.0.0.0")
    external_url = os.getenv("WORKSPACE_EXTERNAL_URL")
    display_url = external_url if external_url else f"{base_uri}:{port}"

    safe_print("Custom Google Drive MCP Server")
    safe_print("=" * 35)
    try:
        version = metadata.version("custom-gdrive-mcp")
    except metadata.PackageNotFoundError:
        version = "dev"
    safe_print(f"  Version: {version}")
    safe_print(f"  Transport: {args.transport}")
    if args.transport == "streamable-http":
        safe_print(f"  URL: {display_url}")
        safe_print(f"  OAuth Callback: {display_url}/oauth2callback")
    safe_print(f"  Mode: {'Single-user' if args.single_user else 'Multi-user'}")
    if args.read_only:
        safe_print("  Read-Only: Enabled")
    safe_print(f"  Python: {sys.version.split()[0]}")

    client_secret = os.getenv("GOOGLE_OAUTH_CLIENT_SECRET", "Not Set")
    redacted_secret = (
        f"{client_secret[:4]}...{client_secret[-4:]}"
        if len(client_secret) > 8
        else "Invalid or too short"
    )
    workspace_creds_dir = os.getenv("WORKSPACE_MCP_CREDENTIALS_DIR")
    google_creds_dir = os.getenv("GOOGLE_MCP_CREDENTIALS_DIR")
    if workspace_creds_dir:
        creds_dir_display = os.path.expanduser(workspace_creds_dir)
        creds_dir_source = "WORKSPACE_MCP_CREDENTIALS_DIR"
    elif google_creds_dir:
        creds_dir_display = os.path.expanduser(google_creds_dir)
        creds_dir_source = "GOOGLE_MCP_CREDENTIALS_DIR"
    else:
        creds_dir_display = os.path.join(
            os.path.expanduser("~"), ".google_workspace_mcp", "credentials"
        )
        creds_dir_source = "default"

    config_vars = {
        "GOOGLE_OAUTH_CLIENT_ID": os.getenv("GOOGLE_OAUTH_CLIENT_ID", "Not Set"),
        "GOOGLE_OAUTH_CLIENT_SECRET": redacted_secret,
        "USER_GOOGLE_EMAIL": os.getenv("USER_GOOGLE_EMAIL", "Not Set"),
        "CREDENTIALS_DIR": f"{creds_dir_display} ({creds_dir_source})",
        "MCP_SINGLE_USER_MODE": os.getenv("MCP_SINGLE_USER_MODE", "false"),
        "MCP_ENABLE_OAUTH21": os.getenv("MCP_ENABLE_OAUTH21", "false"),
        "WORKSPACE_MCP_STATELESS_MODE": os.getenv(
            "WORKSPACE_MCP_STATELESS_MODE", "false"
        ),
        "GOOGLE_SERVICE_ACCOUNT_KEY_FILE": os.getenv(
            "GOOGLE_SERVICE_ACCOUNT_KEY_FILE", "Not Set"
        ),
    }
    safe_print("")
    safe_print("Active Configuration:")
    for key, value in config_vars.items():
        safe_print(f"  - {key}: {value}")
    safe_print("")

    wrap_server_tool_method(server)

    from auth.scopes import set_enabled_tools, set_read_only

    set_enabled_tools(list(SERVICE_MODULES.keys()))
    set_enabled_tool_names(None)
    if args.read_only:
        set_read_only(True)

    safe_print("Loading Drive tool module...")
    for svc, mod in SERVICE_MODULES.items():
        try:
            import_module(mod)
            safe_print(f"  Drive - Google Drive API integration ({mod})")
        except ModuleNotFoundError as exc:
            logger.error("Failed to import tool '%s': %s", svc, exc, exc_info=True)
            safe_print(f"  Failed to load {svc} tool module ({exc}).")
            sys.exit(1)
    safe_print("")

    filter_server_tools(server)

    if args.single_user:
        if os.getenv("MCP_ENABLE_OAUTH21", "false").lower() == "true":
            safe_print("ERROR: --single-user is incompatible with OAuth 2.1 mode.")
            sys.exit(1)
        if is_stateless_mode():
            safe_print("ERROR: --single-user is incompatible with stateless mode.")
            sys.exit(1)
        if is_service_account_enabled():
            safe_print("ERROR: --single-user is incompatible with service account mode.")
            sys.exit(1)
        os.environ["MCP_SINGLE_USER_MODE"] = "1"
        safe_print("Single-user mode enabled")
        safe_print("")

    if is_service_account_enabled():
        user_email = os.getenv("USER_GOOGLE_EMAIL")
        if not user_email:
            safe_print("ERROR: Service account mode requires USER_GOOGLE_EMAIL.")
            sys.exit(1)
        sa_config = get_oauth_config()
        try:
            if sa_config.service_account_key_file:
                with open(sa_config.service_account_key_file) as f:
                    key_data = json.load(f)
            else:
                key_data = json.loads(sa_config.service_account_key_json)
            required_fields = {"type", "project_id", "private_key", "client_email"}
            missing = required_fields - set(key_data.keys())
            if missing:
                safe_print(
                    f"ERROR: Service account key missing fields: {', '.join(sorted(missing))}"
                )
                sys.exit(1)
            if key_data.get("type") != "service_account":
                safe_print(
                    f"ERROR: Service account key has type {key_data.get('type')!r}, expected 'service_account'."
                )
                sys.exit(1)
        except FileNotFoundError as e:
            safe_print(f"ERROR: Service account key file not found: {e}")
            sys.exit(1)
        except json.JSONDecodeError as e:
            safe_print(f"ERROR: Service account key invalid JSON: {e}")
            sys.exit(1)
        except (IOError, OSError) as e:
            safe_print(f"ERROR: Failed to read service account key: {e}")
            sys.exit(1)
        safe_print(f"Service account mode enabled; impersonating: {user_email}")
        safe_print("")

    backend = get_selected_backend()
    if (
        not is_stateless_mode()
        and not is_service_account_enabled()
        and backend != "gcs"
    ):
        try:
            check_credentials_directory_permissions()
        except (PermissionError, OSError) as e:
            safe_print(f"ERROR: Credentials directory permission check failed: {e}")
            sys.exit(1)

    try:
        set_transport_mode(args.transport)

        if args.transport == "streamable-http":
            configure_server_for_http()
            safe_print(f"Starting HTTP server on {base_uri}:{port}")
            if external_url:
                safe_print(f"  External URL: {external_url}")
        else:
            safe_print("Starting STDIO server")
            if not is_service_account_enabled():
                from auth.oauth_callback_server import ensure_oauth_callback_available

                success, error_msg = ensure_oauth_callback_available(
                    "stdio", port, base_uri
                )
                if success:
                    safe_print(
                        f"  OAuth callback server on {display_url}/oauth2callback"
                    )
                else:
                    safe_print(
                        f"  WARNING: OAuth callback server failed to start: {error_msg}"
                    )

        safe_print("Ready for MCP connections")
        safe_print("")

        if args.transport == "streamable-http":
            try:
                with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
                    s.bind((host, port))
            except OSError as e:
                safe_print(f"Socket error: {e}")
                safe_print(f"ERROR: Port {port} already in use.")
                sys.exit(1)
            server.run(
                transport="streamable-http",
                host=host,
                port=port,
                stateless_http=is_stateless_mode(),
            )
        else:
            server.run()
    except KeyboardInterrupt:
        safe_print("\nServer shutdown requested")
        from auth.oauth_callback_server import cleanup_oauth_callback_server

        cleanup_oauth_callback_server()
        sys.exit(0)
    except Exception as e:
        safe_print(f"\nServer error: {e}")
        logger.error(f"Unexpected error running server: {e}", exc_info=True)
        from auth.oauth_callback_server import cleanup_oauth_callback_server

        cleanup_oauth_callback_server()
        sys.exit(1)


if __name__ == "__main__":
    main()
