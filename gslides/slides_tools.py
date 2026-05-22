"""
Google Slides MCP tools.

Two tools wrap the Slides v1 API for the QBR native-chart pipeline:
  - presentations_batch_update — run a batchUpdate (deleteObject, createSheetsChart)
  - presentations_get          — read slides / page elements for slot resolution + verification

The skill builds the request payloads in scripts/google_native/slides_links.py
(pure, sandbox-side); these tools make the authenticated call. The OAuth token
stays inside the MCP.

Drive eventual-consistency: a freshly-converted Sheet can briefly 404 from the
Slides API when createSheetsChart references it. presentations_batch_update
retries on a transient 4xx/5xx with truncated exponential backoff + jitter
(≈1,2,4,8,16s) up to a ~30s wall before surfacing the error.
"""

import logging
import random
import time
from typing import Optional

from googleapiclient.errors import HttpError
from mcp.types import ToolAnnotations

from auth.service_decorator import require_google_service
from core.utils import handle_http_errors
from core.server import server

logger = logging.getLogger(__name__)

# Statuses worth retrying after Drive→Slides eventual-consistency lag.
_RETRYABLE_STATUS = {400, 404, 429, 500, 503}
_MAX_ATTEMPTS = 6
_WALL_SECONDS = 30.0


def _retryable(exc: HttpError) -> bool:
    status = getattr(getattr(exc, "resp", None), "status", None)
    try:
        status = int(status)
    except (TypeError, ValueError):
        return False
    return status in _RETRYABLE_STATUS


@server.tool(
    title="Presentations Batch Update",
    annotations=ToolAnnotations(
        readOnlyHint=False,
        destructiveHint=False,
        idempotentHint=False,
        openWorldHint=True,
    ),
)
@handle_http_errors("presentations_batch_update", service_type="slides")
@require_google_service("slides", "slides")
async def presentations_batch_update(
    service,
    user_google_email: str,
    presentationId: str,
    requests: list,
    writeControl: Optional[dict] = None,
) -> dict:
    """
    Apply a batch of update requests to a Google Slides presentation
    (presentations.batchUpdate).

    Used by the QBR skill to delete chart placeholders and embed LINKED Sheets
    charts (createSheetsChart, linkingMode=LINKED). Build `requests` with
    scripts/google_native/slides_links.build_link_requests(). Pass `writeControl`
    {"requiredRevisionId": ...} (from presentations_get) to guard against
    clobbering concurrent human edits.

    Retries transient Drive eventual-consistency failures (a just-converted
    Sheet not yet visible to Slides) with exponential backoff up to ~30s.

    Args:
        presentationId (str): Target presentation id.
        requests (list): batchUpdate `requests` array.
        writeControl (dict): Optional {"requiredRevisionId": "..."}.

    Returns:
        dict: {presentationId, replies, writeControl}.
    """
    body: dict = {"requests": requests}
    if writeControl:
        body["writeControl"] = writeControl

    logger.info(
        f"[presentations_batch_update] Email: '{user_google_email}', "
        f"presentationId='{presentationId}', {len(requests)} requests"
    )

    deadline = time.monotonic() + _WALL_SECONDS
    last_exc: Optional[HttpError] = None
    for attempt in range(_MAX_ATTEMPTS):
        try:
            resp = (
                service.presentations()
                .batchUpdate(presentationId=presentationId, body=body)
                .execute()
            )
            return {
                "presentationId": resp.get("presentationId", presentationId),
                "replies": resp.get("replies", []),
                "writeControl": resp.get("writeControl"),
            }
        except HttpError as exc:
            if not _retryable(exc) or time.monotonic() >= deadline:
                raise
            last_exc = exc
            delay = min(2 ** attempt, 16)
            delay += random.uniform(-0.25 * delay, 0.25 * delay)
            delay = max(0.0, min(delay, deadline - time.monotonic()))
            logger.warning(
                f"[presentations_batch_update] transient error "
                f"(attempt {attempt + 1}/{_MAX_ATTEMPTS}), retrying in {delay:.1f}s: {exc}"
            )
            time.sleep(delay)
    # Exhausted retries.
    raise last_exc  # type: ignore[misc]


@server.tool(
    title="Presentations Get",
    annotations=ToolAnnotations(
        readOnlyHint=True,
        destructiveHint=False,
        idempotentHint=True,
        openWorldHint=True,
    ),
)
@handle_http_errors("presentations_get", is_read_only=True, service_type="slides")
@require_google_service("slides", "slides_read")
async def presentations_get(
    service,
    user_google_email: str,
    presentationId: str,
    fields: Optional[str] = None,
) -> dict:
    """
    Read a Google Slides presentation (presentations.get). Use a `fields` mask:
      - slot resolution: slides(objectId,pageElements(objectId,description,title,size,transform,shape/text,elementGroup))
      - embed verification: slides(pageElements(objectId,sheetsChart))
      - revision guard: revisionId

    Args:
        presentationId (str): Target presentation id.
        fields (str): Partial-response field mask. Strongly recommended.

    Returns:
        dict: The (masked) presentation resource.
    """
    logger.info(
        f"[presentations_get] Email: '{user_google_email}', "
        f"presentationId='{presentationId}', fields={fields!r}"
    )
    req = service.presentations().get(presentationId=presentationId, fields=fields)
    return req.execute()
