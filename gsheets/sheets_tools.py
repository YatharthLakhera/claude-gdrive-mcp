"""
Google Sheets MCP tools.

Two tools wrap the Sheets v4 API for the QBR native-chart pipeline:
  - spreadsheets_batch_update — run a batchUpdate (addChart, repeatCell, ...)
  - spreadsheets_get          — read sheet properties / charts for verification

The skill builds the request payloads in scripts/google_native/sheets_charts.py
(pure, sandbox-side); these tools make the authenticated call. The OAuth token
stays inside the MCP — it is never returned to the skill sandbox.
"""

import logging
from typing import Any, Optional

from mcp.types import ToolAnnotations

from auth.service_decorator import require_google_service
from core.utils import handle_http_errors
from core.server import server

logger = logging.getLogger(__name__)


@server.tool(
    title="Spreadsheets Batch Update",
    annotations=ToolAnnotations(
        readOnlyHint=False,
        destructiveHint=False,
        idempotentHint=False,
        openWorldHint=True,
    ),
)
@handle_http_errors("spreadsheets_batch_update", service_type="sheets")
@require_google_service("sheets", "sheets")
async def spreadsheets_batch_update(
    service,
    user_google_email: str,
    spreadsheetId: str,
    requests: list,
) -> dict:
    """
    Apply a batch of update requests to a Google Sheet (spreadsheets.batchUpdate).

    Used by the QBR skill to create native charts (addChart) and set cell number
    formats (repeatCell). Build the `requests` array with
    scripts/google_native/sheets_charts.build_batch_requests().

    Args:
        spreadsheetId (str): Target spreadsheet id.
        requests (list): The batchUpdate `requests` array (list of request dicts).

    Returns:
        dict: {spreadsheetId, replies} — `replies` includes addChart.chart.chartId
              for each created chart (validate with sheets_charts.verify_add_chart_response).
    """
    logger.info(
        f"[spreadsheets_batch_update] Email: '{user_google_email}', "
        f"spreadsheetId='{spreadsheetId}', {len(requests)} requests"
    )
    body = {"requests": requests}
    resp = (
        service.spreadsheets()
        .batchUpdate(spreadsheetId=spreadsheetId, body=body)
        .execute()
    )
    return {
        "spreadsheetId": resp.get("spreadsheetId", spreadsheetId),
        "replies": resp.get("replies", []),
    }


@server.tool(
    title="Spreadsheets Get",
    annotations=ToolAnnotations(
        readOnlyHint=True,
        destructiveHint=False,
        idempotentHint=True,
        openWorldHint=True,
    ),
)
@handle_http_errors("spreadsheets_get", is_read_only=True, service_type="sheets")
@require_google_service("sheets", "sheets_read")
async def spreadsheets_get(
    service,
    user_google_email: str,
    spreadsheetId: str,
    fields: Optional[str] = None,
) -> dict:
    """
    Read a Google Sheet's metadata (spreadsheets.get). Use a `fields` mask to
    resolve sheet ids (`sheets(properties(sheetId,title))`) or verify charts
    (`sheets(charts(chartId,spec/title))`).

    Args:
        spreadsheetId (str): Target spreadsheet id.
        fields (str): Partial-response field mask. Strongly recommended.

    Returns:
        dict: The (masked) spreadsheet resource, including `sheets` with
              `properties` and/or `charts` per the field mask, plus
              `spreadsheetUrl` when not masked out.
    """
    logger.info(
        f"[spreadsheets_get] Email: '{user_google_email}', "
        f"spreadsheetId='{spreadsheetId}', fields={fields!r}"
    )
    req = service.spreadsheets().get(spreadsheetId=spreadsheetId, fields=fields)
    return req.execute()
