"""
Google Drive MCP Tools

This module provides MCP tools for interacting with Google Drive API.
"""

import asyncio
import logging
import io
import base64
import binascii

from typing import Optional, List, Dict, Any, Callable, Awaitable, BinaryIO
from tempfile import NamedTemporaryFile, SpooledTemporaryFile
from urllib.parse import urlparse
from urllib.request import url2pathname
from pathlib import Path

import httpx
from googleapiclient.errors import HttpError
from googleapiclient.http import MediaIoBaseDownload, MediaIoBaseUpload

from mcp.types import ToolAnnotations, EmbeddedResource, BlobResourceContents, TextContent

from auth.service_decorator import require_google_service
from auth.oauth_config import is_stateless_mode
from core.attachment_storage import get_attachment_storage, get_attachment_url
from core.utils import (
    GOOGLE_API_WRITE_RETRIES,
    IMAGE_MIME_TYPES,
    encode_image_content,
    extract_office_xml_text,
    extract_pdf_text,
    handle_http_errors,
    validate_file_path,
)
from core.server import server
from core.config import get_transport_mode
from core.http_utils import (
    redact_url as _redact_url,
    ssrf_safe_stream as _ssrf_safe_stream,
)
from gdrive.drive_helpers import (
    DRIVE_QUERY_PATTERNS,
    FOLDER_MIME_TYPE,
    build_drive_list_params,
    check_public_link_permission,
    format_permission_info,
    get_drive_image_url,
    resolve_drive_item,
    resolve_file_type_mime,
    resolve_folder_id,
    validate_expiration_time,
    validate_share_role,
    validate_share_type,
)

logger = logging.getLogger(__name__)

DOWNLOAD_CHUNK_SIZE_BYTES = 256 * 1024  # 256 KB
UPLOAD_CHUNK_SIZE_BYTES = 5 * 1024 * 1024  # 5 MB (Google recommended minimum)
MAX_DOWNLOAD_BYTES = 2 * 1024 * 1024 * 1024  # 2 GB safety limit for URL downloads
SHARED_DRIVE_ORGANIZER_CONCURRENCY_LIMIT = 10


async def _stream_url_with_validation(
    url: str, write_chunk: Optional[Callable[[bytes], Awaitable[None]]] = None
) -> tuple[int, Optional[str]]:
    """Stream a remote file with shared status and size validation."""
    total_bytes = 0

    redacted_url = _redact_url(url)

    async with _ssrf_safe_stream(url) as resp:
        if resp.status_code != 200:
            request = getattr(resp, "request", None)
            if request is None:
                parsed_url = urlparse(url)
                request = httpx.Request(
                    "GET",
                    f"{parsed_url.scheme}://{redacted_url}",
                )
            raise httpx.HTTPStatusError(
                f"Failed to fetch file from URL: {redacted_url} (status {resp.status_code})",
                request=request,
                response=resp,
            )

        content_type = resp.headers.get("Content-Type")
        async for chunk in resp.aiter_bytes(chunk_size=DOWNLOAD_CHUNK_SIZE_BYTES):
            total_bytes += len(chunk)
            if total_bytes > MAX_DOWNLOAD_BYTES:
                raise ValueError(
                    f"Download from {redacted_url} exceeded {MAX_DOWNLOAD_BYTES} byte limit "
                    f"({total_bytes} bytes)"
                )
            if write_chunk is not None:
                await write_chunk(chunk)

    return total_bytes, content_type


async def _download_url_to_bytes(
    url: str,
) -> tuple[BinaryIO, Optional[str]]:
    """Download a remote file into a spooled temporary file with bounded streaming."""
    spool = SpooledTemporaryFile(max_size=UPLOAD_CHUNK_SIZE_BYTES)

    try:

        async def _collect(chunk: bytes) -> None:
            await asyncio.to_thread(spool.write, chunk)

        _total_bytes, content_type = await _stream_url_with_validation(url, _collect)
        await asyncio.to_thread(spool.seek, 0)
        return spool, content_type
    except Exception:
        spool.close()
        raise


async def _get_file_size(file_obj: BinaryIO) -> int:
    """Measure a possibly spooled file off the event loop and restore position."""

    def _measure_size() -> int:
        file_obj.seek(0, io.SEEK_END)
        size = file_obj.tell()
        file_obj.seek(0)
        return size

    return await asyncio.to_thread(_measure_size)


@server.tool(
    title="Search Files",
    annotations=ToolAnnotations(
        readOnlyHint=True,
        destructiveHint=False,
        idempotentHint=True,
        openWorldHint=True,
    ),
)
@handle_http_errors("search_files", is_read_only=True, service_type="drive")
@require_google_service("drive", "drive_read")
async def search_files(
    service,
    user_google_email: str,
    query: str,
    page_size: int = 10,
    page_token: Optional[str] = None,
    drive_id: Optional[str] = None,
    include_items_from_all_drives: bool = True,
    corpora: Optional[str] = None,
    file_type: Optional[str] = None,
    detailed: bool = True,
    order_by: Optional[str] = None,
) -> dict:
    """
    Searches for files and folders within a user's Google Drive, including shared drives.

    Args:
        user_google_email (str): The user's Google email address. Required.
        query (str): The search query string. Supports Google Drive search operators.
                     NOTE: Owner-based queries ('user@example.com' in owners) DO NOT WORK in Shared Drives
                     because files are owned by the shared drive itself, not individual users.
                     For recent files by a specific user in Shared Drives, search by modifiedTime
                     and use order_by='modifiedTime desc' instead.
        page_size (int): The maximum number of files to return. Defaults to 10.
        page_token (Optional[str]): Page token from a previous response's nextPageToken to retrieve the next page of results.
        drive_id (Optional[str]): ID of the shared drive to search. If None, behavior depends on `corpora` and `include_items_from_all_drives`.
        include_items_from_all_drives (bool): Whether shared drive items should be included in results. Defaults to True. This is effective when not specifying a `drive_id`.
        corpora (Optional[str]): Bodies of items to query (e.g., 'user', 'domain', 'drive', 'allDrives').
                                 If 'drive_id' is specified and 'corpora' is None, it defaults to 'drive'.
                                 Otherwise, Drive API default behavior applies. Prefer 'user' or 'drive' over 'allDrives' for efficiency.
        file_type (Optional[str]): Restrict results to a specific file type. Accepts a friendly
                                   name ('folder', 'document'/'doc', 'spreadsheet'/'sheet',
                                   'presentation'/'slides', 'form', 'drawing', 'pdf', 'shortcut',
                                   'script', 'site', 'jam'/'jamboard') or any raw MIME type
                                   string (e.g. 'application/pdf'). Defaults to None (all types).
        detailed (bool): Whether to include size, modified time, and link in results. Defaults to True.
        order_by (Optional[str]): Sort order. Comma-separated list of sort keys with optional 'desc' modifier.
                                  Valid keys: 'createdTime', 'folder', 'modifiedByMeTime', 'modifiedTime',
                                  'name', 'name_natural', 'quotaBytesUsed', 'recency', 'sharedWithMeTime',
                                  'starred', 'viewedByMeTime'. Example: 'modifiedTime desc' or 'folder,modifiedTime desc,name'.
                                  Defaults to None (Drive API default ordering).

    Returns:
        dict: {"files": [...], "nextPageToken": "<str or null>"}.
              Each file dict contains: id, name, title, mimeType, parentId,
              fileSize, modifiedTime, webViewLink. Null values are returned
              where Drive doesn't provide a field (e.g. Google native files
              have no fileSize; root items have no parentId).
    """
    logger.info(
        f"[search_files] Invoked. Email: '{user_google_email}', Query: '{query}', file_type: '{file_type}'"
    )

    # Check if the query looks like a structured Drive query or free text
    # Look for Drive API operators and structured query patterns
    is_structured_query = any(pattern.search(query) for pattern in DRIVE_QUERY_PATTERNS)

    if is_structured_query:
        final_query = query
        logger.info(
            f"[search_files] Using structured query as-is: '{final_query}'"
        )
    else:
        # For free text queries, wrap in fullText contains
        escaped_query = query.replace("'", "\\'")
        final_query = f"fullText contains '{escaped_query}'"
        logger.info(
            f"[search_files] Reformatting free text query '{query}' to '{final_query}'"
        )

    if file_type is not None:
        mime = resolve_file_type_mime(file_type)
        final_query = f"({final_query}) and mimeType = '{mime}'"
        logger.info(f"[search_files] Added mimeType filter: '{mime}'")

    list_params = build_drive_list_params(
        query=final_query,
        page_size=page_size,
        drive_id=drive_id,
        include_items_from_all_drives=include_items_from_all_drives,
        corpora=corpora,
        page_token=page_token,
        detailed=detailed,
        order_by=order_by,
    )

    results = await asyncio.to_thread(service.files().list(**list_params).execute)
    raw_files = results.get("files", [])
    files_out = []
    for item in raw_files:
        parents = item.get("parents") or []
        files_out.append({
            "id": item["id"],
            "name": item["name"],
            "title": item["name"],
            "mimeType": item["mimeType"],
            "parentId": parents[0] if parents else None,
            "fileSize": item.get("size"),
            "modifiedTime": item.get("modifiedTime"),
            "webViewLink": item.get("webViewLink"),
        })
    return {
        "files": files_out,
        "nextPageToken": results.get("nextPageToken"),
    }


@server.tool(
    title="List Recent Files",
    annotations=ToolAnnotations(
        readOnlyHint=True,
        destructiveHint=False,
        idempotentHint=True,
        openWorldHint=True,
    ),
)
@handle_http_errors("list_recent_files", is_read_only=True, service_type="drive")
@require_google_service("drive", "drive_read")
async def list_recent_files(
    service,
    user_google_email: str,
    page_size: int = 10,
    page_token: Optional[str] = None,
) -> dict:
    """
    Lists the user's most recently modified files in Google Drive (across all
    drives the user has access to). Excludes trashed items.

    Args:
        page_size (int): Maximum number of files to return. Defaults to 10.
        page_token (Optional[str]): Page token from a prior nextPageToken.

    Returns:
        dict: {"files": [...], "nextPageToken": "<str or null>"} — same shape as search_files.
    """
    logger.info(
        f"[list_recent_files] Invoked. Email: '{user_google_email}', page_size={page_size}"
    )

    list_params = build_drive_list_params(
        query="trashed=false",
        page_size=page_size,
        page_token=page_token,
        detailed=True,
        order_by="modifiedTime desc",
    )

    results = await asyncio.to_thread(service.files().list(**list_params).execute)
    raw_files = results.get("files", [])
    files_out = []
    for item in raw_files:
        parents = item.get("parents") or []
        files_out.append({
            "id": item["id"],
            "name": item["name"],
            "title": item["name"],
            "mimeType": item["mimeType"],
            "parentId": parents[0] if parents else None,
            "fileSize": item.get("size"),
            "modifiedTime": item.get("modifiedTime"),
            "webViewLink": item.get("webViewLink"),
        })
    return {
        "files": files_out,
        "nextPageToken": results.get("nextPageToken"),
    }


@server.tool(
    title="Get File Metadata",
    annotations=ToolAnnotations(
        readOnlyHint=True,
        destructiveHint=False,
        idempotentHint=True,
        openWorldHint=True,
    ),
)
@handle_http_errors("get_file_metadata", is_read_only=True, service_type="drive")
@require_google_service("drive", "drive_read")
async def get_file_metadata(
    service,
    user_google_email: str,
    fileId: str,
) -> dict:
    """
    Returns metadata for a single Google Drive file by ID.

    Args:
        fileId (str): The Drive file ID.

    Returns:
        dict: {id, title, mimeType, fileSize, parentId, modifiedTime, webViewLink}.
              fileSize and parentId may be null for some file types (e.g., Google
              native files have no fileSize; root items have no parentId).
    """
    logger.info(
        f"[get_file_metadata] Invoked. Email: '{user_google_email}', fileId='{fileId}'"
    )
    resolved_id, metadata = await resolve_drive_item(
        service,
        fileId,
        extra_fields="name, mimeType, size, parents, modifiedTime, webViewLink",
    )
    parents = metadata.get("parents") or []
    return {
        "id": resolved_id,
        "title": metadata.get("name"),
        "mimeType": metadata.get("mimeType"),
        "fileSize": metadata.get("size"),
        "parentId": parents[0] if parents else None,
        "modifiedTime": metadata.get("modifiedTime"),
        "webViewLink": metadata.get("webViewLink"),
    }


@server.tool(
    title="Read File Content",
    annotations=ToolAnnotations(
        readOnlyHint=True,
        destructiveHint=False,
        idempotentHint=True,
        openWorldHint=True,
    ),
)
@handle_http_errors("read_file_content", is_read_only=True, service_type="drive")
@require_google_service("drive", "drive_read")
async def read_file_content(
    service,
    user_google_email: str,
    fileId: str,
) -> str:
    """
    Retrieves the content of a specific Google Drive file by ID, supporting files in shared drives.

    • Native Google Docs, Sheets, Slides → exported as text / CSV.
    • Office files (.docx, .xlsx, .pptx) → unzipped & parsed with std-lib to
      extract readable text.
    • PDFs → text extracted with pypdf when possible; scanned/image-only PDFs
      fall back to a download hint.
    • Images → returned as base64 with MIME metadata for multimodal clients.
    • Any other file → downloaded; tries UTF-8 decode, else notes binary.

    Args:
        fileId: Drive file ID.

    Returns:
        str: The file content as plain text with metadata header.
    """
    file_id = fileId
    logger.info(f"[read_file_content] Invoked. File ID: '{file_id}'")

    resolved_file_id, file_metadata = await resolve_drive_item(
        service,
        file_id,
        extra_fields="name, webViewLink",
    )
    file_id = resolved_file_id
    mime_type = file_metadata.get("mimeType", "")
    file_name = file_metadata.get("name", "Unknown File")
    export_mime_type = {
        "application/vnd.google-apps.document": "text/plain",
        "application/vnd.google-apps.spreadsheet": "text/csv",
        "application/vnd.google-apps.presentation": "text/plain",
    }.get(mime_type)

    request_obj = (
        service.files().export_media(fileId=file_id, mimeType=export_mime_type)
        if export_mime_type
        else service.files().get_media(fileId=file_id)
    )
    fh = io.BytesIO()
    downloader = MediaIoBaseDownload(fh, request_obj)
    loop = asyncio.get_event_loop()
    done = False
    while not done:
        status, done = await loop.run_in_executor(None, downloader.next_chunk)

    file_content_bytes = fh.getvalue()

    # Attempt Office XML extraction only for actual Office XML files
    office_mime_types = {
        "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        "application/vnd.openxmlformats-officedocument.presentationml.presentation",
        "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    }

    if mime_type in office_mime_types:
        # Offload Office XML extraction to a thread to avoid blocking the event loop
        office_text = await asyncio.to_thread(
            extract_office_xml_text, file_content_bytes, mime_type
        )
        if office_text:
            body_text = office_text
        else:
            # Fallback: try UTF-8; otherwise flag binary
            try:
                body_text = file_content_bytes.decode("utf-8")
            except UnicodeDecodeError:
                body_text = (
                    f"[Binary or unsupported text encoding for mimeType '{mime_type}' - "
                    f"{len(file_content_bytes)} bytes]"
                )
    elif mime_type == "application/pdf":
        # Offload PDF text extraction to a thread to avoid blocking the event loop
        pdf_text = await asyncio.to_thread(extract_pdf_text, file_content_bytes)
        if pdf_text:
            body_text = pdf_text
        else:
            body_text = (
                f"[Could not extract text from PDF ({len(file_content_bytes)} bytes) "
                f"- the file may be scanned/image-only. "
                f"Use download_file_content to fetch the raw bytes instead.]"
            )
    elif mime_type in IMAGE_MIME_TYPES:
        body_text = encode_image_content(file_content_bytes, mime_type)
    else:
        # For non-Office files (including Google native files), try UTF-8 decode directly
        try:
            body_text = file_content_bytes.decode("utf-8")
        except UnicodeDecodeError:
            body_text = (
                f"[Binary or unsupported text encoding for mimeType '{mime_type}' - "
                f"{len(file_content_bytes)} bytes]"
            )

    # Assemble response
    header = (
        f'File: "{file_name}" (ID: {file_id}, Type: {mime_type})\n'
        f"Link: {file_metadata.get('webViewLink', '#')}\n\n--- CONTENT ---\n"
    )
    return header + body_text


DOWNLOAD_FILE_CONTENT_MAX_MB_DEFAULT = 25


def _get_download_max_bytes() -> int:
    import os
    raw = os.environ.get("DOWNLOAD_FILE_CONTENT_MAX_MB", "").strip()
    try:
        mb = int(raw) if raw else DOWNLOAD_FILE_CONTENT_MAX_MB_DEFAULT
        if mb <= 0:
            mb = DOWNLOAD_FILE_CONTENT_MAX_MB_DEFAULT
    except ValueError:
        mb = DOWNLOAD_FILE_CONTENT_MAX_MB_DEFAULT
    return mb * 1024 * 1024


@server.tool(
    title="Download File Content",
    annotations=ToolAnnotations(
        readOnlyHint=True,
        destructiveHint=False,
        idempotentHint=True,
        openWorldHint=True,
    ),
)
@handle_http_errors(
    "download_file_content", is_read_only=True, service_type="drive"
)
@require_google_service("drive", "drive_read")
async def download_file_content(
    service,
    user_google_email: str,
    fileId: str,
    export_format: Optional[str] = None,
) -> TextContent:
    """
    Downloads a Google Drive file and returns its raw bytes as a base64-encoded blob.

    For Google native files (Docs, Sheets, Slides), exports to a useful format:
    - Google Docs -> PDF (default) or DOCX if export_format='docx'
    - Google Sheets -> XLSX (default), PDF if export_format='pdf', or CSV if export_format='csv'
    - Google Slides -> PDF (default) or PPTX if export_format='pptx'

    For other files, downloads the original file format.

    Size limit: DOWNLOAD_FILE_CONTENT_MAX_MB env var (default 25 MB). Files
    larger than the limit return an error pointing to read_file_content for
    text extraction, or a Drive shareable link as a fallback.

    Args:
        fileId: The Google Drive file ID to download.
        export_format: Optional export format for Google native files.
                      Options: 'pdf', 'docx', 'xlsx', 'csv', 'pptx'.

    Returns:
        TextContent: a single text content block whose `text` is the base64-
        encoded file bytes. The mimeType is carried in `_meta` for debuggers
        but the consumer (e.g. SKILL.md) is expected to know the expected
        mime from prior search_files / get_file_metadata results. This shape
        replaces the previous EmbeddedResource return, which claude.ai's MCP
        client rejects for non-Google-native mimes (xlsx, pptx, text/plain).
    """
    file_id = fileId
    logger.info(
        f"[download_file_content] Invoked. File ID: '{file_id}', Export format: {export_format}"
    )

    # Resolve shortcuts and get file metadata
    resolved_file_id, file_metadata = await resolve_drive_item(
        service,
        file_id,
        extra_fields="name, webViewLink, mimeType",
    )
    file_id = resolved_file_id
    mime_type = file_metadata.get("mimeType", "")
    file_name = file_metadata.get("name", "Unknown File")

    # Determine export format for Google native files
    export_mime_type = None
    output_filename = file_name
    output_mime_type = mime_type

    if mime_type == "application/vnd.google-apps.document":
        # Google Docs
        if export_format == "docx":
            export_mime_type = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
            output_mime_type = export_mime_type
            if not output_filename.endswith(".docx"):
                output_filename = f"{Path(output_filename).stem}.docx"
        else:
            # Default to PDF
            export_mime_type = "application/pdf"
            output_mime_type = export_mime_type
            if not output_filename.endswith(".pdf"):
                output_filename = f"{Path(output_filename).stem}.pdf"

    elif mime_type == "application/vnd.google-apps.spreadsheet":
        # Google Sheets
        if export_format == "csv":
            export_mime_type = "text/csv"
            output_mime_type = export_mime_type
            if not output_filename.endswith(".csv"):
                output_filename = f"{Path(output_filename).stem}.csv"
        elif export_format == "pdf":
            export_mime_type = "application/pdf"
            output_mime_type = export_mime_type
            if not output_filename.endswith(".pdf"):
                output_filename = f"{Path(output_filename).stem}.pdf"
        else:
            # Default to XLSX
            export_mime_type = (
                "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
            )
            output_mime_type = export_mime_type
            if not output_filename.endswith(".xlsx"):
                output_filename = f"{Path(output_filename).stem}.xlsx"

    elif mime_type == "application/vnd.google-apps.presentation":
        # Google Slides
        if export_format == "pptx":
            export_mime_type = "application/vnd.openxmlformats-officedocument.presentationml.presentation"
            output_mime_type = export_mime_type
            if not output_filename.endswith(".pptx"):
                output_filename = f"{Path(output_filename).stem}.pptx"
        else:
            # Default to PDF
            export_mime_type = "application/pdf"
            output_mime_type = export_mime_type
            if not output_filename.endswith(".pdf"):
                output_filename = f"{Path(output_filename).stem}.pdf"

    # Download the file
    request_obj = (
        service.files().export_media(fileId=file_id, mimeType=export_mime_type)
        if export_mime_type
        else service.files().get_media(fileId=file_id)
    )

    fh = io.BytesIO()
    downloader = MediaIoBaseDownload(fh, request_obj)
    loop = asyncio.get_event_loop()
    done = False
    while not done:
        status, done = await loop.run_in_executor(None, downloader.next_chunk)

    file_content_bytes = fh.getvalue()
    size_bytes = len(file_content_bytes)
    size_mb = size_bytes / (1024 * 1024) if size_bytes else 0

    max_bytes = _get_download_max_bytes()
    if size_bytes > max_bytes:
        max_mb = max_bytes // (1024 * 1024)
        raise Exception(
            f"File '{file_name}' (ID: {file_id}) is {size_mb:.1f} MB, which "
            f"exceeds the download_file_content size limit of {max_mb} MB. "
            f"Options: "
            f"(1) call read_file_content to extract text content (works for Docs, "
            f"Sheets, Slides, PDFs, Office files); "
            f"(2) call get_drive_shareable_link to share the file directly with "
            f"the user instead of inlining bytes; "
            f"(3) raise the DOWNLOAD_FILE_CONTENT_MAX_MB env var on the MCP server "
            f"if you genuinely need to inline a larger file. "
            f"Note: very large base64 blobs can also exceed client-side context limits."
        )

    b64_blob = base64.b64encode(file_content_bytes).decode("utf-8")
    logger.info(
        f"[download_file_content] Successfully downloaded {size_mb:.2f} MB "
        f"({size_bytes} bytes) as {output_mime_type}"
    )
    # Return as TextContent rather than EmbeddedResource(BlobResourceContents):
    # claude.ai's MCP client rejects EmbeddedResource for non-Google-native
    # mimes (xlsx, pptx, text/plain) with "Resources of type X are not
    # currently supported." TextContent is rendered universally; consumers
    # decode `text` as base64 to recover bytes. Mime metadata is preserved
    # in `_meta` for any client that introspects it (the LLM is expected to
    # know the expected mime from prior search_files/get_file_metadata).
    return TextContent(
        type="text",
        text=b64_blob,
        meta={
            "uri": f"drive://{file_id}",
            "mimeType": output_mime_type,
            "size_bytes": size_bytes,
            "encoding": "base64",
        },
    )


@server.tool(
    title="List Drive Items",
    annotations=ToolAnnotations(
        readOnlyHint=True,
        destructiveHint=False,
        idempotentHint=True,
        openWorldHint=True,
    ),
)
@handle_http_errors("list_drive_items", is_read_only=True, service_type="drive")
@require_google_service("drive", "drive_read")
async def list_drive_items(
    service,
    user_google_email: str,
    folder_id: str = "root",
    page_size: int = 100,
    page_token: Optional[str] = None,
    drive_id: Optional[str] = None,
    include_items_from_all_drives: bool = True,
    corpora: Optional[str] = None,
    file_type: Optional[str] = None,
    detailed: bool = True,
    order_by: Optional[str] = None,
    resource_type: str = "items",
    query: Optional[str] = None,
    include_organizers: bool = False,
) -> str:
    """
    Lists files/folders or shared drive containers, supporting shared drives.
    If `drive_id` is specified, lists items within that shared drive. `folder_id` is then relative to that drive (or use drive_id as folder_id for root).
    If `drive_id` is not specified, lists items from user's "My Drive" and accessible shared drives (if `include_items_from_all_drives` is True).
    Set `resource_type` to "shared_drives" to list shared drive containers instead of folder contents.

    Args:
        user_google_email (str): The user's Google email address. Required.
        folder_id (str): The ID of the Google Drive folder. Defaults to 'root'. For a shared drive, this can be the shared drive's ID to list its root, or a folder ID within that shared drive.
        page_size (int): The maximum number of items to return. Defaults to 100.
        page_token (Optional[str]): Page token from a previous response's nextPageToken to retrieve the next page of results.
        drive_id (Optional[str]): ID of the shared drive. If provided, the listing is scoped to this drive.
        include_items_from_all_drives (bool): Whether items from all accessible shared drives should be included if `drive_id` is not set. Defaults to True.
        corpora (Optional[str]): Corpus to query ('user', 'drive', 'allDrives'). If `drive_id` is set and `corpora` is None, 'drive' is used. If None and no `drive_id`, API defaults apply.
        file_type (Optional[str]): Restrict results to a specific file type. Accepts a friendly
                                   name ('folder', 'document'/'doc', 'spreadsheet'/'sheet',
                                   'presentation'/'slides', 'form', 'drawing', 'pdf', 'shortcut',
                                   'script', 'site', 'jam'/'jamboard') or any raw MIME type
                                   string (e.g. 'application/pdf'). Defaults to None (all types).
        detailed (bool): Whether to include size, modified time, and link in results. Defaults to True.
        order_by (Optional[str]): Sort order. Comma-separated list of sort keys with optional 'desc' modifier.
                                  Valid keys: 'createdTime', 'folder', 'modifiedByMeTime', 'modifiedTime',
                                  'name', 'name_natural', 'quotaBytesUsed', 'recency', 'sharedWithMeTime',
                                  'starred', 'viewedByMeTime'. Example: 'modifiedTime desc' or 'folder,modifiedTime desc,name'.
                                  Defaults to None (Drive API default ordering).
        resource_type (str): What to list. Use "items" for folder contents or
                             "shared_drives" for shared drive containers. Defaults to "items".
        query (Optional[str]): Shared drive query used only when resource_type="shared_drives",
                               e.g. "name contains 'Engineering'".
        include_organizers (bool): When resource_type="shared_drives", include principals
                                   with the organizer role. This costs one extra permissions.list
                                   API call per shared drive returned. Defaults to False.

    Returns:
        str: A formatted list of files/folders in the specified folder or shared drives.
             Includes a nextPageToken line when more results are available.
    """
    logger.info(
        f"[list_drive_items] Invoked. Email: '{user_google_email}', Folder ID: '{folder_id}', "
        f"File Type: '{file_type}', Resource Type: '{resource_type}'"
    )

    normalized_resource_type = resource_type.lower().strip()
    if normalized_resource_type == "shared_drives":
        return await _list_shared_drives_impl(
            service=service,
            user_google_email=user_google_email,
            page_size=page_size,
            page_token=page_token,
            query=query,
            include_organizers=include_organizers,
        )
    if normalized_resource_type != "items":
        raise ValueError("resource_type must be either 'items' or 'shared_drives'")

    resolved_folder_id = await resolve_folder_id(service, folder_id)
    final_query = f"'{resolved_folder_id}' in parents and trashed=false"

    if file_type is not None:
        mime = resolve_file_type_mime(file_type)
        final_query = f"({final_query}) and mimeType = '{mime}'"
        logger.info(f"[list_drive_items] Added mimeType filter: '{mime}'")

    list_params = build_drive_list_params(
        query=final_query,
        page_size=page_size,
        drive_id=drive_id,
        include_items_from_all_drives=include_items_from_all_drives,
        corpora=corpora,
        page_token=page_token,
        detailed=detailed,
        order_by=order_by,
    )

    results = await asyncio.to_thread(service.files().list(**list_params).execute)
    files = results.get("files", [])
    if not files:
        return f"No items found in folder '{folder_id}'."

    next_token = results.get("nextPageToken")
    header = (
        f"Found {len(files)} items in folder '{folder_id}' for {user_google_email}:"
    )
    formatted_items_text_parts = [header]
    for item in files:
        if detailed:
            size_str = f", Size: {item.get('size', 'N/A')}" if "size" in item else ""
            drive_id_str = (
                f", Drive ID: {item['driveId']}" if item.get("driveId") else ""
            )
            formatted_items_text_parts.append(
                f'- Name: "{item["name"]}" (ID: {item["id"]}, Type: {item["mimeType"]}{size_str}{drive_id_str}, Modified: {item.get("modifiedTime", "N/A")}) Link: {item.get("webViewLink", "#")}'
            )
        else:
            formatted_items_text_parts.append(
                f'- Name: "{item["name"]}" (ID: {item["id"]}, Type: {item["mimeType"]})'
            )
    if next_token:
        formatted_items_text_parts.append(f"nextPageToken: {next_token}")
    text_output = "\n".join(formatted_items_text_parts)
    return text_output


async def _list_shared_drives_impl(
    service,
    user_google_email: str,
    page_size: int = 100,
    page_token: Optional[str] = None,
    query: Optional[str] = None,
    include_organizers: bool = False,
) -> str:
    """List shared drives available to the authenticated user."""
    logger.info(
        f"[list_shared_drives] Invoked. Email: '{user_google_email}', page_size: {page_size}, "
        f"include_organizers: {include_organizers}"
    )

    list_params: Dict[str, Any] = {
        "pageSize": min(max(page_size, 1), 100),
        "fields": (
            "drives(id, name, createdTime, hidden, "
            "restrictions, capabilities(canManageMembers, canEdit)), "
            "nextPageToken"
        ),
    }
    if page_token:
        list_params["pageToken"] = page_token
    if query:
        list_params["q"] = query

    results = await asyncio.to_thread(service.drives().list(**list_params).execute)
    drives = results.get("drives", [])
    if not drives:
        return f"No shared drives found for {user_google_email}."

    if include_organizers:

        async def _fetch_organizers(d: Dict[str, Any]) -> None:
            try:
                permissions = []
                next_permission_page_token = None

                while True:
                    list_kwargs: Dict[str, Any] = {
                        "fileId": d["id"],
                        "supportsAllDrives": True,
                        "useDomainAdminAccess": False,
                        "fields": (
                            "nextPageToken, "
                            "permissions(emailAddress, displayName, role, type, domain)"
                        ),
                        "pageSize": 100,
                    }
                    if next_permission_page_token:
                        list_kwargs["pageToken"] = next_permission_page_token

                    perms = await asyncio.to_thread(
                        service.permissions().list(**list_kwargs).execute
                    )
                    permissions.extend(perms.get("permissions", []))

                    next_permission_page_token = perms.get("nextPageToken")
                    if not next_permission_page_token:
                        break

                d["_organizers"] = [
                    p for p in permissions if p.get("role") == "organizer"
                ]
            except HttpError as e:
                status = getattr(e, "status_code", None)
                if status is None and getattr(e, "resp", None) is not None:
                    status = getattr(e.resp, "status", None)
                reason = getattr(e, "reason", None) or str(e)
                d["_organizers_error"] = f"{status or 'unknown'}: {reason}"

        organizer_fetch_sem = asyncio.Semaphore(
            SHARED_DRIVE_ORGANIZER_CONCURRENCY_LIMIT
        )

        async def _bounded_fetch_organizers(d: Dict[str, Any]) -> None:
            async with organizer_fetch_sem:
                await _fetch_organizers(d)

        await asyncio.gather(*[_bounded_fetch_organizers(d) for d in drives])

    next_token = results.get("nextPageToken")
    parts = [f"Found {len(drives)} shared drives for {user_google_email}:"]
    for d in drives:
        caps = d.get("capabilities") or {}
        rest = d.get("restrictions") or {}
        cap_flags = ", ".join(k for k, v in caps.items() if v) or "none"
        rest_flags = ", ".join(k for k, v in rest.items() if v) or "none"
        hidden = " [hidden]" if d.get("hidden") else ""
        parts.append(
            f'- Name: "{d["name"]}" (ID: {d["id"]}, Created: {d.get("createdTime", "N/A")}){hidden} '
            f"Capabilities: {cap_flags}; Restrictions: {rest_flags}"
        )
        if include_organizers:
            err = d.get("_organizers_error")
            if err:
                parts.append(f"  Organizers: <error: {err}>")
            else:
                organizers = d.get("_organizers", [])
                if not organizers:
                    parts.append("  Organizers: <none returned>")
                else:
                    for o in organizers:
                        identifier = (
                            o.get("emailAddress")
                            or o.get("domain")
                            or o.get("displayName")
                            or o.get("type", "?")
                        )
                        display = o.get("displayName")
                        kind = o.get("type", "?")
                        suffix = (
                            f' ("{display}")'
                            if display and display != identifier
                            else ""
                        )
                        parts.append(f"  Organizer ({kind}): {identifier}{suffix}")
    if next_token:
        parts.append(f"nextPageToken: {next_token}")
    return "\n".join(parts)


async def _create_drive_folder_impl(
    service,
    user_google_email: str,
    folder_name: str,
    parent_folder_id: str = "root",
) -> str:
    """Internal implementation for create_drive_folder. Used by tests."""
    resolved_folder_id = await resolve_folder_id(service, parent_folder_id)
    file_metadata = {
        "name": folder_name,
        "parents": [resolved_folder_id],
        "mimeType": FOLDER_MIME_TYPE,
    }
    created_file = await asyncio.to_thread(
        service.files()
        .create(
            body=file_metadata,
            fields="id, name, webViewLink",
            supportsAllDrives=True,
        )
        .execute
    )
    link = created_file.get("webViewLink", "")
    return (
        f"Successfully created folder '{created_file.get('name', folder_name)}' (ID: {created_file.get('id', 'N/A')}) "
        f"in folder '{parent_folder_id}' for {user_google_email}. Link: {link}"
    )


@server.tool(
    title="Create Drive Folder",
    annotations=ToolAnnotations(
        readOnlyHint=False,
        destructiveHint=False,
        idempotentHint=False,
        openWorldHint=True,
    ),
)
@handle_http_errors("create_drive_folder", service_type="drive")
@require_google_service("drive", "drive_file")
async def create_drive_folder(
    service,
    user_google_email: str,
    folder_name: str,
    parent_folder_id: str = "root",
) -> str:
    """
    Creates a new folder in Google Drive, supporting creation within shared drives.

    Args:
        user_google_email (str): The user's Google email address. Required.
        folder_name (str): The name for the new folder.
        parent_folder_id (str): The ID of the parent folder. Defaults to 'root'.
            For shared drives, use a folder ID within that shared drive.

    Returns:
        str: Confirmation message with folder name, ID, and link.
    """
    logger.info(
        f"[create_drive_folder] Invoked. Email: '{user_google_email}', Folder: '{folder_name}', Parent: '{parent_folder_id}'"
    )
    return await _create_drive_folder_impl(
        service, user_google_email, folder_name, parent_folder_id
    )


# When disableConversionToGoogleType is False, map source MIME types to their
# Google-native equivalents so an uploaded csv/docx/xlsx/pptx is auto-converted.
AUTO_CONVERT_MIME_MAP = {
    "text/csv": "application/vnd.google-apps.spreadsheet",
    "text/markdown": "application/vnd.google-apps.document",
    "text/plain": "application/vnd.google-apps.document",
    "text/html": "application/vnd.google-apps.document",
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document": "application/vnd.google-apps.document",
    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet": "application/vnd.google-apps.spreadsheet",
    "application/vnd.openxmlformats-officedocument.presentationml.presentation": "application/vnd.google-apps.presentation",
}


@server.tool(
    title="Create File",
    annotations=ToolAnnotations(
        readOnlyHint=False,
        destructiveHint=False,
        idempotentHint=False,
        openWorldHint=True,
    ),
)
@handle_http_errors("create_file", service_type="drive")
@require_google_service("drive", "drive_file")
async def create_file(
    service,
    user_google_email: str,
    title: str,
    content: Optional[str] = None,
    parentId: str = "root",
    mimeType: str = "text/plain",
    fileUrl: Optional[str] = None,
    disableConversionToGoogleType: bool = False,
) -> dict:
    """
    Creates a new file in Google Drive, supporting creation within shared drives.
    Accepts either direct content or a fileUrl to fetch the content from.

    Args:
        title (str): The name for the new file.
        content (Optional[str]): If provided, the content to write to the file. Encoding is
            chosen by mimeType: text/* and application/{json,xml,javascript,yaml,x-yaml} are
            UTF-8 encoded; everything else is treated as base64 and decoded to raw bytes
            (use this for xlsx, pptx, pdf, images, and any other binary payload).
        parentId (str): The ID of the parent folder. Defaults to 'root'.
        mimeType (str): The MIME type of the source content. Defaults to 'text/plain'.
        fileUrl (Optional[str]): If provided, fetches the file content from this URL. Supports file://, http://, and https:// protocols.
        disableConversionToGoogleType (bool): When False (default), source MIME types
            with a Google-native equivalent are auto-converted (e.g. text/csv → Sheet,
            text/plain → Doc, docx → Doc). Set to True to store the file as-is.

    Returns:
        dict: {"id": "<drive file id>", "title": "<file name>"}.
    """
    # Internal aliases — keep legacy names to minimize body churn.
    file_name = title
    folder_id = parentId
    mime_type = mimeType
    logger.info(
        f"[create_file] Invoked. Email: '{user_google_email}', Title: {file_name}, ParentID: {folder_id}, mimeType: {mime_type}, fileUrl: {fileUrl}, disableConversion: {disableConversionToGoogleType}"
    )

    if content is None and fileUrl is None and mime_type != FOLDER_MIME_TYPE:
        raise Exception("You must provide either 'content' or 'fileUrl'.")

    # Create folder (no content or media_body). Prefer create_drive_folder for new code.
    if mime_type == FOLDER_MIME_TYPE:
        resolved_parent = await resolve_folder_id(service, folder_id)
        folder_metadata = {
            "name": file_name,
            "parents": [resolved_parent],
            "mimeType": FOLDER_MIME_TYPE,
        }
        created_folder = await asyncio.to_thread(
            service.files()
            .create(body=folder_metadata, fields="id, name", supportsAllDrives=True)
            .execute
        )
        return {
            "id": created_folder.get("id"),
            "title": created_folder.get("name", file_name),
        }

    file_data = None
    resolved_folder_id = await resolve_folder_id(service, folder_id)

    target_mime_type = mime_type
    if not disableConversionToGoogleType and mime_type in AUTO_CONVERT_MIME_MAP:
        target_mime_type = AUTO_CONVERT_MIME_MAP[mime_type]
        logger.info(
            f"[create_file] Auto-converting {mime_type} → {target_mime_type} "
            f"(disableConversionToGoogleType=False)"
        )

    file_metadata = {
        "name": file_name,
        "parents": [resolved_folder_id],
        "mimeType": target_mime_type,
    }

    # Prefer fileUrl if both are provided
    if fileUrl:
        logger.info(f"[create_file] Fetching file from URL: {fileUrl}")

        # Check if this is a file:// URL
        parsed_url = urlparse(fileUrl)
        if parsed_url.scheme == "file":
            # Handle file:// URL - read from local filesystem
            logger.info(
                "[create_file] Detected file:// URL, reading from local filesystem"
            )
            transport_mode = get_transport_mode()
            running_streamable = transport_mode == "streamable-http"
            if running_streamable:
                logger.warning(
                    "[create_file] file:// URL requested while server runs in streamable-http mode. Ensure the file path is accessible to the server (e.g., Docker volume) or use an HTTP(S) URL."
                )

            # Convert file:// URL to a cross-platform local path
            raw_path = parsed_url.path or ""
            netloc = parsed_url.netloc
            if netloc and netloc.lower() != "localhost":
                raw_path = f"//{netloc}{raw_path}"
            file_path = url2pathname(raw_path)

            # Validate path safety and verify file exists
            path_obj = validate_file_path(file_path)
            if not path_obj.exists():
                extra = (
                    " The server is running via streamable-http, so file:// URLs must point to files inside the container or remote host."
                    if running_streamable
                    else ""
                )
                raise Exception(f"Local file does not exist: {file_path}.{extra}")
            if not path_obj.is_file():
                extra = (
                    " In streamable-http/Docker deployments, mount the file into the container or provide an HTTP(S) URL."
                    if running_streamable
                    else ""
                )
                raise Exception(f"Path is not a file: {file_path}.{extra}")

            logger.info(f"[create_file] Reading local file: {file_path}")

            # Read file and upload
            file_data = await asyncio.to_thread(path_obj.read_bytes)
            total_bytes = len(file_data)
            logger.info(f"[create_file] Read {total_bytes} bytes from local file")

            media = MediaIoBaseUpload(
                io.BytesIO(file_data),
                mimetype=mime_type,
                resumable=True,
                chunksize=UPLOAD_CHUNK_SIZE_BYTES,
            )

            logger.info("[create_file] Starting upload to Google Drive...")
            created_file = await asyncio.to_thread(
                service.files()
                .create(
                    body=file_metadata,
                    media_body=media,
                    fields="id, name, webViewLink",
                    supportsAllDrives=True,
                )
                .execute,
                num_retries=GOOGLE_API_WRITE_RETRIES,
            )
        # Handle HTTP/HTTPS URLs
        elif parsed_url.scheme in ("http", "https"):
            # when running in stateless mode, deployment may not have access to local file system
            if is_stateless_mode():
                with SpooledTemporaryFile(max_size=UPLOAD_CHUNK_SIZE_BYTES) as spool:

                    async def _write_spool(chunk: bytes) -> None:
                        await asyncio.to_thread(spool.write, chunk)

                    _total, content_type = await _stream_url_with_validation(
                        fileUrl, _write_spool
                    )
                    await asyncio.to_thread(spool.seek, 0)

                    # Try to get MIME type from Content-Type header
                    if content_type and content_type != "application/octet-stream":
                        mime_type = content_type
                        file_metadata["mimeType"] = content_type
                        logger.info(
                            f"[create_file] Using MIME type from Content-Type header: {content_type}"
                        )

                    media = MediaIoBaseUpload(
                        spool,
                        mimetype=mime_type,
                        resumable=True,
                        chunksize=UPLOAD_CHUNK_SIZE_BYTES,
                    )

                    created_file = await asyncio.to_thread(
                        service.files()
                        .create(
                            body=file_metadata,
                            media_body=media,
                            fields="id, name, webViewLink",
                            supportsAllDrives=True,
                        )
                        .execute,
                        num_retries=GOOGLE_API_WRITE_RETRIES,
                    )
            else:
                # Stream download to temp file with SSRF protection, then upload
                with NamedTemporaryFile() as temp_file:

                    async def _write_chunk(chunk: bytes) -> None:
                        await asyncio.to_thread(temp_file.write, chunk)

                    total_bytes, content_type = await _stream_url_with_validation(
                        fileUrl, _write_chunk
                    )

                    logger.info(
                        f"[create_file] Downloaded {total_bytes} bytes "
                        f"from URL before upload."
                    )

                    if content_type and content_type != "application/octet-stream":
                        mime_type = content_type
                        file_metadata["mimeType"] = mime_type
                        logger.info(
                            f"[create_file] Using MIME type from "
                            f"Content-Type header: {mime_type}"
                        )

                    # Reset file pointer to beginning for upload
                    temp_file.seek(0)

                    media = MediaIoBaseUpload(
                        temp_file,
                        mimetype=mime_type,
                        resumable=True,
                        chunksize=UPLOAD_CHUNK_SIZE_BYTES,
                    )

                    logger.info(
                        "[create_file] Starting upload to Google Drive..."
                    )
                    created_file = await asyncio.to_thread(
                        service.files()
                        .create(
                            body=file_metadata,
                            media_body=media,
                            fields="id, name, webViewLink",
                            supportsAllDrives=True,
                        )
                        .execute,
                        num_retries=GOOGLE_API_WRITE_RETRIES,
                    )
        else:
            if not parsed_url.scheme:
                raise Exception(
                    "fileUrl is missing a URL scheme. Use file://, http://, or https://."
                )
            raise Exception(
                f"Unsupported URL scheme '{parsed_url.scheme}'. Only file://, http://, and https:// are supported."
            )
    elif content is not None:
        # Auto-detect encoding from mime_type:
        #   text-ish mimes (text/*, application/json, etc.) -> utf-8 encode
        #   anything else (xlsx, pptx, pdf, png, ...) -> treat content as base64 and decode
        _TEXT_MIME_PREFIXES = ("text/",)
        _TEXT_MIME_EXACT = {
            "application/json",
            "application/xml",
            "application/javascript",
            "application/x-yaml",
            "application/yaml",
        }
        is_text_payload = (
            mime_type.startswith(_TEXT_MIME_PREFIXES) or mime_type in _TEXT_MIME_EXACT
        )
        if is_text_payload:
            file_data = content.encode("utf-8")
        else:
            try:
                file_data = base64.b64decode(content, validate=True)
            except (binascii.Error, ValueError) as exc:
                raise Exception(
                    f"create_drive_file: mime_type '{mime_type}' is treated as binary, "
                    f"so 'content' must be base64-encoded. b64decode failed: {exc}"
                ) from exc
        media = io.BytesIO(file_data)

        created_file = await asyncio.to_thread(
            service.files()
            .create(
                body=file_metadata,
                media_body=MediaIoBaseUpload(
                    media,
                    mimetype=mime_type,
                    resumable=True,
                    chunksize=UPLOAD_CHUNK_SIZE_BYTES,
                ),
                fields="id, name, webViewLink",
                supportsAllDrives=True,
            )
            .execute,
            num_retries=GOOGLE_API_WRITE_RETRIES,
        )

    link = created_file.get("webViewLink", "")
    logger.info(
        f"[create_file] Successfully created '{created_file.get('name', file_name)}' "
        f"(ID: {created_file.get('id', 'N/A')}) in folder '{folder_id}'. Link: {link}"
    )
    return {
        "id": created_file.get("id"),
        "title": created_file.get("name", file_name),
    }


@server.tool(
    title="Create Resumable Upload Session",
    annotations=ToolAnnotations(
        readOnlyHint=False,
        destructiveHint=False,
        idempotentHint=False,
        openWorldHint=True,
    ),
)
@handle_http_errors("create_resumable_upload_session", service_type="drive")
@require_google_service("drive", "drive_file")
async def create_resumable_upload_session(
    service,
    user_google_email: str,
    title: str,
    mimeType: str,
    parentId: str = "root",
    disableConversionToGoogleType: bool = False,
) -> dict:
    """
    Creates a Google Drive resumable upload session and returns the upload URL.

    The caller (e.g. a skill running in claude.ai's sandbox) uses the returned
    `upload_url` to PUT raw file bytes directly to Google — bypassing MCP
    transport limits on large content arguments. Only the short URL travels
    through MCP; the bytes go straight from the sandbox to Google.

    Args:
        title: The name for the new file.
        mimeType: The source MIME type of the content (e.g.
            "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
            for xlsx, "text/plain" for markdown). This is what the bytes
            on the wire actually are.
        parentId: The ID of the parent folder. Defaults to 'root'.
        disableConversionToGoogleType: When False (default), source MIME types
            with a Google-native equivalent are auto-converted (e.g. text/csv →
            Sheet, xlsx → Sheet). Set to True to store the file as-is —
            REQUIRED for binary uploads that must not be converted (xlsx, pptx,
            pdf, images) and for keeping markdown as .md rather than Google
            Doc.

    Returns:
        dict: {
            "upload_url": "<Google's resumable upload URL — one-shot, ~1 week TTL>",
            "title": "<file name>",
            "mimeType": "<target MIME stored on Drive — may differ from source if conversion ran>",
            "parentId": "<resolved parent folder ID>"
        }

        The caller PUTs raw bytes to `upload_url` with HTTP PUT and a
        `Content-Length` header. Google's PUT response body contains the
        created file's `id` and `name`. The skill repo ships
        `scripts/upload_to_drive.py` which implements this PUT correctly.

    Note: The session is bound to the metadata (name, parent, mimeType) posted
    at creation. The caller cannot change those when uploading bytes. The URL
    is one-shot and short-lived.
    """
    file_name = title
    folder_id = parentId
    mime_type = mimeType

    logger.info(
        f"[create_resumable_upload_session] Email: '{user_google_email}', "
        f"Title: {file_name}, ParentID: {folder_id}, mimeType: {mime_type}, "
        f"disableConversion: {disableConversionToGoogleType}"
    )

    resolved_folder_id = await resolve_folder_id(service, folder_id)

    # Mirror create_file's conversion logic so consumers see consistent
    # disableConversionToGoogleType semantics across both tools.
    target_mime_type = mime_type
    if not disableConversionToGoogleType and mime_type in AUTO_CONVERT_MIME_MAP:
        target_mime_type = AUTO_CONVERT_MIME_MAP[mime_type]
        logger.info(
            f"[create_resumable_upload_session] Auto-converting "
            f"{mime_type} → {target_mime_type} (disableConversionToGoogleType=False)"
        )

    file_metadata = {
        "name": file_name,
        "parents": [resolved_folder_id],
        "mimeType": target_mime_type,
    }

    # Extract OAuth bearer token from the authenticated service. The Drive
    # service is built with credentials by require_google_service; we need
    # raw token access to make the resumable-init POST ourselves rather than
    # going through googleapiclient (which doesn't expose "give me a session
    # URL but don't upload anything yet" as a first-class API).
    credentials = getattr(getattr(service, "_http", None), "credentials", None)
    if credentials is None:
        raise Exception(
            "Could not extract credentials from service object — cannot "
            "create resumable upload session."
        )

    # Refresh the token if it's expired. credentials.valid is False when
    # the token has expired but a refresh_token is available.
    if hasattr(credentials, "valid") and not credentials.valid:
        from google.auth.transport.requests import Request as GoogleAuthRequest
        logger.info("[create_resumable_upload_session] Refreshing OAuth token")
        await asyncio.to_thread(credentials.refresh, GoogleAuthRequest())

    access_token = getattr(credentials, "token", None)
    if not access_token:
        raise Exception(
            "OAuth credentials have no usable token attribute after refresh."
        )

    # POST to Google's resumable upload init endpoint. The Location header
    # in the 200 response is the session URL the caller will PUT bytes to.
    init_url = (
        "https://www.googleapis.com/upload/drive/v3/files"
        "?uploadType=resumable&supportsAllDrives=true"
    )

    async with httpx.AsyncClient(timeout=30.0) as client:
        response = await client.post(
            init_url,
            headers={
                "Authorization": f"Bearer {access_token}",
                "Content-Type": "application/json; charset=UTF-8",
                # The bytes the caller will upload are typed as the SOURCE
                # mimeType, not the (possibly-converted) target.
                "X-Upload-Content-Type": mime_type,
            },
            json=file_metadata,
        )

    if response.status_code != 200:
        raise Exception(
            f"Failed to create resumable upload session "
            f"(HTTP {response.status_code}): {response.text}"
        )

    upload_url = response.headers.get("Location")
    if not upload_url:
        raise Exception(
            "Google returned a 200 but no Location header for the resumable session."
        )

    logger.info(
        f"[create_resumable_upload_session] Session created for '{file_name}' "
        f"in folder '{resolved_folder_id}'"
    )

    return {
        "upload_url": upload_url,
        "title": file_name,
        "mimeType": target_mime_type,
        "parentId": resolved_folder_id,
    }


# Mapping of file extensions to source MIME types for Google Docs conversion
GOOGLE_DOCS_IMPORT_FORMATS = {
    ".md": "text/markdown",
    ".markdown": "text/markdown",
    ".txt": "text/plain",
    ".text": "text/plain",
    ".html": "text/html",
    ".htm": "text/html",
    ".docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    ".doc": "application/msword",
    ".rtf": "application/rtf",
    ".odt": "application/vnd.oasis.opendocument.text",
}

GOOGLE_DOCS_MIME_TYPE = "application/vnd.google-apps.document"


def _detect_source_format(file_name: str, content: Optional[str] = None) -> str:
    """
    Detect the source MIME type based on file extension.
    Falls back to text/plain if unknown.
    """
    ext = Path(file_name).suffix.lower()
    if ext in GOOGLE_DOCS_IMPORT_FORMATS:
        return GOOGLE_DOCS_IMPORT_FORMATS[ext]

    # If content is provided and looks like markdown, use markdown
    if content and (content.startswith("#") or "```" in content or "**" in content):
        return "text/markdown"

    return "text/plain"


@server.tool(
    title="Import to Google Doc",
    annotations=ToolAnnotations(
        readOnlyHint=False,
        destructiveHint=False,
        idempotentHint=False,
        openWorldHint=True,
    ),
)
@handle_http_errors("import_to_google_doc", service_type="drive")
@require_google_service("drive", "drive_file")
async def import_to_google_doc(
    service,
    user_google_email: str,
    file_name: str,
    content: Optional[str] = None,
    file_path: Optional[str] = None,
    file_url: Optional[str] = None,
    source_format: Optional[str] = None,
    folder_id: str = "root",
) -> str:
    """
    Imports a file (Markdown, DOCX, TXT, HTML, RTF, ODT) into Google Docs format with automatic conversion.

    Google Drive automatically converts the source file to native Google Docs format,
    preserving formatting like headings, lists, bold, italic, etc.
    For batch operations, prefer file_path for files on disk so callers do not need
    to load full file contents into their context.

    Args:
        user_google_email (str): The user's Google email address. Required.
        file_name (str): The name for the new Google Doc (extension will be ignored).
        content (Optional[str]): Text content for text-based formats. Use only for short snippets or content already in memory.
        file_path (Optional[str]): Local file path or file:// URL for any supported format (MD, TXT, HTML, DOCX, ODT, RTF). Appropriate for larger files than content, but file_path may still load the file into memory or perform non-streaming reads. Avoid very large files that could exceed memory or time limits; use streaming/chunked uploads or an alternative API for huge files.
        file_url (Optional[str]): Remote URL to fetch the file from (http/https).
        source_format (Optional[str]): Source format hint ('md', 'markdown', 'docx', 'txt', 'html', 'rtf', 'odt').
                                       Auto-detected from file_name extension if not provided.
        folder_id (str): The ID of the parent folder. Defaults to 'root'.

    Returns:
        str: Confirmation message with the new Google Doc link.

    Examples:
        # Import a markdown file from disk (preferred for batch operations)
        import_to_google_doc(file_name="My Doc.md", file_path="/path/to/my-doc.md", source_format="md")

        # Import markdown content directly
        import_to_google_doc(file_name="My Doc.md", content="# Title\\n\\nHello **world**")

        # Import a local DOCX file
        import_to_google_doc(file_name="Report", file_path="/path/to/report.docx")

        # Import from URL
        import_to_google_doc(file_name="Remote Doc", file_url="https://example.com/doc.md")
    """
    logger.info(
        f"[import_to_google_doc] Invoked. Email: '{user_google_email}', "
        f"File Name: '{file_name}', Source Format: '{source_format}', Folder ID: '{folder_id}'"
    )

    # Validate inputs
    source_count = sum(1 for x in [content, file_path, file_url] if x is not None)
    if source_count == 0:
        raise ValueError(
            "You must provide one of: 'content', 'file_path', or 'file_url'."
        )
    if source_count > 1:
        raise ValueError("Provide only one of: 'content', 'file_path', or 'file_url'.")

    # Determine source MIME type
    if source_format:
        # Normalize format hint
        format_key = f".{source_format.lower().lstrip('.')}"
        if format_key in GOOGLE_DOCS_IMPORT_FORMATS:
            source_mime_type = GOOGLE_DOCS_IMPORT_FORMATS[format_key]
        else:
            raise ValueError(
                f"Unsupported source_format: '{source_format}'. "
                f"Supported: {', '.join(ext.lstrip('.') for ext in GOOGLE_DOCS_IMPORT_FORMATS.keys())}"
            )
    else:
        # Auto-detect from file_name, file_path, or file_url
        detection_name = file_path or file_url or file_name
        source_mime_type = _detect_source_format(detection_name, content)

    logger.info(f"[import_to_google_doc] Detected source MIME type: {source_mime_type}")

    # Clean up file name (remove extension since it becomes a Google Doc)
    doc_name = Path(file_name).stem if Path(file_name).suffix else file_name

    # Resolve folder
    resolved_folder_id = await resolve_folder_id(service, folder_id)

    # File metadata - destination is Google Docs format
    file_metadata = {
        "name": doc_name,
        "parents": [resolved_folder_id],
        "mimeType": GOOGLE_DOCS_MIME_TYPE,  # Target format = Google Docs
    }

    file_data: bytes
    remote_file_data: Optional[BinaryIO] = None
    remote_content_type: Optional[str] = None

    # Handle content (string input for text formats)
    if content is not None:
        file_data = content.encode("utf-8")
        logger.info(f"[import_to_google_doc] Using content: {len(file_data)} bytes")

    # Handle file_path (local file)
    elif file_path is not None:
        parsed_url = urlparse(file_path)

        # Handle file:// URL format
        if parsed_url.scheme == "file":
            raw_path = parsed_url.path or ""
            netloc = parsed_url.netloc
            if netloc and netloc.lower() != "localhost":
                raw_path = f"//{netloc}{raw_path}"
            actual_path = url2pathname(raw_path)
        elif parsed_url.scheme == "":
            # Regular path
            actual_path = file_path
        else:
            raise ValueError(
                f"file_path should be a local path or file:// URL, got: {file_path}"
            )

        path_obj = validate_file_path(actual_path)
        if not path_obj.exists():
            raise FileNotFoundError(f"File not found: {actual_path}")
        if not path_obj.is_file():
            raise ValueError(f"Path is not a file: {actual_path}")

        file_data = await asyncio.to_thread(path_obj.read_bytes)
        logger.info(f"[import_to_google_doc] Read local file: {len(file_data)} bytes")

        # Re-detect format from actual file if not specified
        if not source_format:
            source_mime_type = _detect_source_format(actual_path)
            logger.info(
                f"[import_to_google_doc] Re-detected from path: {source_mime_type}"
            )

    # Handle file_url (remote file)
    elif file_url is not None:
        parsed_url = urlparse(file_url)
        if parsed_url.scheme not in ("http", "https"):
            raise ValueError(f"file_url must be http:// or https://, got: {file_url}")

        # SSRF protection: block internal/private network URLs and validate redirects
        remote_file_data, remote_content_type = await _download_url_to_bytes(file_url)

    # Upload with conversion
    if remote_file_data is not None:
        with remote_file_data:
            remote_size = await _get_file_size(remote_file_data)

            logger.info(
                f"[import_to_google_doc] Downloaded from URL: {remote_size} bytes"
            )

            # Prefer the Content-Type from the download; fall back to URL-based detection
            if not source_format:
                ct_base = (remote_content_type or "").split(";", 1)[0].strip()
                if ct_base and ct_base != "application/octet-stream":
                    source_mime_type = ct_base
                    logger.info(
                        f"[import_to_google_doc] Using Content-Type from response: {source_mime_type}"
                    )
                else:
                    source_mime_type = _detect_source_format(file_url)
                    logger.info(
                        f"[import_to_google_doc] Detected from URL path: {source_mime_type}"
                    )

            media = MediaIoBaseUpload(
                remote_file_data,
                mimetype=source_mime_type,  # Source format
                resumable=True,
                chunksize=UPLOAD_CHUNK_SIZE_BYTES,
            )

            logger.info(
                f"[import_to_google_doc] Uploading to Google Drive with conversion: "
                f"{source_mime_type} → {GOOGLE_DOCS_MIME_TYPE}"
            )

            created_file = await asyncio.to_thread(
                service.files()
                .create(
                    body=file_metadata,
                    media_body=media,
                    fields="id, name, webViewLink, mimeType",
                    supportsAllDrives=True,
                )
                .execute,
                num_retries=GOOGLE_API_WRITE_RETRIES,
            )
    else:
        media = MediaIoBaseUpload(
            io.BytesIO(file_data),
            mimetype=source_mime_type,  # Source format
            resumable=True,
            chunksize=UPLOAD_CHUNK_SIZE_BYTES,
        )

        logger.info(
            f"[import_to_google_doc] Uploading to Google Drive with conversion: "
            f"{source_mime_type} → {GOOGLE_DOCS_MIME_TYPE}"
        )

        created_file = await asyncio.to_thread(
            service.files()
            .create(
                body=file_metadata,
                media_body=media,
                fields="id, name, webViewLink, mimeType",
                supportsAllDrives=True,
            )
            .execute,
            num_retries=GOOGLE_API_WRITE_RETRIES,
        )

    result_mime = created_file.get("mimeType", "unknown")
    if result_mime != GOOGLE_DOCS_MIME_TYPE:
        logger.warning(
            f"[import_to_google_doc] Conversion may have failed. "
            f"Expected {GOOGLE_DOCS_MIME_TYPE}, got {result_mime}"
        )

    link = created_file.get("webViewLink", "No link available")
    doc_id = created_file.get("id", "N/A")

    confirmation = (
        f"✅ Successfully imported '{doc_name}' as Google Doc\n"
        f"   Document ID: {doc_id}\n"
        f"   Source format: {source_mime_type}\n"
        f"   Folder: {folder_id}\n"
        f"   Link: {link}"
    )

    logger.info(f"[import_to_google_doc] Success. Link: {link}")
    return confirmation


@server.tool(
    title="Get Drive File Permissions",
    annotations=ToolAnnotations(
        readOnlyHint=True,
        destructiveHint=False,
        idempotentHint=True,
        openWorldHint=True,
    ),
)
@handle_http_errors(
    "get_drive_file_permissions", is_read_only=True, service_type="drive"
)
@require_google_service("drive", "drive_read")
async def get_drive_file_permissions(
    service,
    user_google_email: str,
    file_id: str,
) -> str:
    """
    Gets detailed metadata about a Google Drive file including sharing permissions.

    Args:
        user_google_email (str): The user's Google email address. Required.
        file_id (str): The ID of the file to check permissions for.

    Returns:
        str: Detailed file metadata including sharing status and URLs.
    """
    logger.info(
        f"[get_drive_file_permissions] Checking file {file_id} for {user_google_email}"
    )

    resolved_file_id, _ = await resolve_drive_item(service, file_id)
    file_id = resolved_file_id

    try:
        # Get comprehensive file metadata including permissions with details
        file_metadata = await asyncio.to_thread(
            service.files()
            .get(
                fileId=file_id,
                fields="id, name, mimeType, size, modifiedTime, owners, "
                "permissions(id, type, role, emailAddress, domain, expirationTime, permissionDetails), "
                "webViewLink, webContentLink, shared, sharingUser, viewersCanCopyContent",
                supportsAllDrives=True,
            )
            .execute
        )

        # Format the response
        output_parts = [
            f"File: {file_metadata.get('name', 'Unknown')}",
            f"ID: {file_id}",
            f"Type: {file_metadata.get('mimeType', 'Unknown')}",
            f"Size: {file_metadata.get('size', 'N/A')} bytes",
            f"Modified: {file_metadata.get('modifiedTime', 'N/A')}",
            "",
            "Sharing Status:",
            f"  Shared: {file_metadata.get('shared', False)}",
        ]

        # Add sharing user if available
        sharing_user = file_metadata.get("sharingUser")
        if sharing_user:
            output_parts.append(
                f"  Shared by: {sharing_user.get('displayName', 'Unknown')} ({sharing_user.get('emailAddress', 'Unknown')})"
            )

        # Process permissions
        permissions = file_metadata.get("permissions", [])
        if permissions:
            output_parts.append(f"  Number of permissions: {len(permissions)}")
            output_parts.append("  Permissions:")
            for perm in permissions:
                output_parts.append(f"    - {format_permission_info(perm)}")
        else:
            output_parts.append("  No additional permissions (private file)")

        # Add URLs
        output_parts.extend(
            [
                "",
                "URLs:",
                f"  View Link: {file_metadata.get('webViewLink', 'N/A')}",
            ]
        )

        # webContentLink is only available for files that can be downloaded
        web_content_link = file_metadata.get("webContentLink")
        if web_content_link:
            output_parts.append(f"  Direct Download Link: {web_content_link}")

        has_public_link = check_public_link_permission(permissions)

        if has_public_link:
            output_parts.extend(
                [
                    "",
                    "✅ This file is shared with 'Anyone with the link' - it can be inserted into Google Docs",
                ]
            )
        else:
            output_parts.extend(
                [
                    "",
                    "❌ This file is NOT shared with 'Anyone with the link' - it cannot be inserted into Google Docs",
                    "   To fix: Right-click the file in Google Drive → Share → Anyone with the link → Viewer",
                ]
            )

        return "\n".join(output_parts)

    except Exception as e:
        logger.error(f"Error getting file permissions: {e}")
        return f"Error getting file permissions: {e}"


@server.tool(
    title="Check Drive File Public Access",
    annotations=ToolAnnotations(
        readOnlyHint=True,
        destructiveHint=False,
        idempotentHint=True,
        openWorldHint=True,
    ),
)
@handle_http_errors(
    "check_drive_file_public_access", is_read_only=True, service_type="drive"
)
@require_google_service("drive", "drive_read")
async def check_drive_file_public_access(
    service,
    user_google_email: str,
    file_name: str,
    drive_id: Optional[str] = None,
) -> str:
    """
    Searches for a file by name and checks if it has public link sharing enabled.

    Args:
        user_google_email (str): The user's Google email address. Required.
        file_name (str): The name of the file to check.
        drive_id (Optional[str]): ID of the shared drive to scope the search to. When set, the
                                  underlying files.list call uses corpora='drive' and the given
                                  driveId, which is required to reliably find files that live only
                                  in that shared drive. When None, behaviour is unchanged
                                  (default API corpora applies).

    Returns:
        str: Information about the file's sharing status and whether it can be used in Google Docs.
    """
    logger.info(
        f"[check_drive_file_public_access] Searching for {file_name}"
        + (f" within drive_id={drive_id}" if drive_id else "")
    )

    # Search for the file
    escaped_name = file_name.replace("'", "\\'")
    query = f"name = '{escaped_name}'"

    list_params: Dict[str, Any] = {
        "q": query,
        "pageSize": 10,
        "fields": "files(id, name, mimeType, webViewLink)",
        "supportsAllDrives": True,
        "includeItemsFromAllDrives": True,
    }
    if drive_id:
        list_params["corpora"] = "drive"
        list_params["driveId"] = drive_id

    results = await asyncio.to_thread(service.files().list(**list_params).execute)

    files = results.get("files", [])
    if not files:
        return f"No file found with name '{file_name}'"

    if len(files) > 1:
        output_parts = [f"Found {len(files)} files with name '{file_name}':"]
        for f in files:
            output_parts.append(f"  - {f['name']} (ID: {f['id']})")
        output_parts.append("\nChecking the first file...")
        output_parts.append("")
    else:
        output_parts = []

    # Check permissions for the first file
    file_id = files[0]["id"]
    resolved_file_id, _ = await resolve_drive_item(service, file_id)
    file_id = resolved_file_id

    # Get detailed permissions
    file_metadata = await asyncio.to_thread(
        service.files()
        .get(
            fileId=file_id,
            fields="id, name, mimeType, permissions, webViewLink, webContentLink, shared",
            supportsAllDrives=True,
        )
        .execute
    )

    permissions = file_metadata.get("permissions", [])

    has_public_link = check_public_link_permission(permissions)

    output_parts.extend(
        [
            f"File: {file_metadata['name']}",
            f"ID: {file_id}",
            f"Type: {file_metadata['mimeType']}",
            f"Shared: {file_metadata.get('shared', False)}",
            "",
        ]
    )

    if has_public_link:
        output_parts.extend(
            [
                "✅ PUBLIC ACCESS ENABLED - This file can be inserted into Google Docs",
                f"Use with insert_doc_image_url: {get_drive_image_url(file_id)}",
            ]
        )
    else:
        output_parts.extend(
            [
                "❌ NO PUBLIC ACCESS - Cannot insert into Google Docs",
                "Fix: Drive → Share → 'Anyone with the link' → 'Viewer'",
            ]
        )

    return "\n".join(output_parts)


@server.tool(
    title="Update Drive File",
    annotations=ToolAnnotations(
        readOnlyHint=False,
        destructiveHint=True,
        idempotentHint=False,
        openWorldHint=True,
    ),
)
@handle_http_errors("update_drive_file", is_read_only=False, service_type="drive")
@require_google_service("drive", "drive_file")
async def update_drive_file(
    service,
    user_google_email: str,
    file_id: str,
    # File metadata updates
    name: Optional[str] = None,
    description: Optional[str] = None,
    mime_type: Optional[str] = None,
    # Folder organization
    add_parents: Optional[str] = None,  # Comma-separated folder IDs to add
    remove_parents: Optional[str] = None,  # Comma-separated folder IDs to remove
    # File status
    starred: Optional[bool] = None,
    trashed: Optional[bool] = None,
    # Sharing and permissions
    writers_can_share: Optional[bool] = None,
    copy_requires_writer_permission: Optional[bool] = None,
    # Custom properties
    properties: Optional[dict] = None,  # User-visible custom properties
) -> str:
    """
    Updates metadata and properties of a Google Drive file.

    Args:
        user_google_email (str): The user's Google email address. Required.
        file_id (str): The ID of the file to update. Required.
        name (Optional[str]): New name for the file.
        description (Optional[str]): New description for the file.
        mime_type (Optional[str]): New MIME type (note: changing type may require content upload).
        add_parents (Optional[str]): Comma-separated folder IDs to add as parents.
        remove_parents (Optional[str]): Comma-separated folder IDs to remove from parents.
        starred (Optional[bool]): Whether to star/unstar the file.
        trashed (Optional[bool]): Whether to move file to/from trash.
        writers_can_share (Optional[bool]): Whether editors can share the file.
        copy_requires_writer_permission (Optional[bool]): Whether copying requires writer permission.
        properties (Optional[dict]): Custom key-value properties for the file.

    Returns:
        str: Confirmation message with details of the updates applied.
    """
    logger.info(f"[update_drive_file] Updating file {file_id} for {user_google_email}")

    current_file_fields = (
        "name, description, mimeType, parents, starred, trashed, webViewLink, "
        "writersCanShare, copyRequiresWriterPermission, properties"
    )
    resolved_file_id, current_file = await resolve_drive_item(
        service,
        file_id,
        extra_fields=current_file_fields,
    )
    file_id = resolved_file_id

    # Build the update body with only specified fields
    update_body = {}
    if name is not None:
        update_body["name"] = name
    if description is not None:
        update_body["description"] = description
    if mime_type is not None:
        update_body["mimeType"] = mime_type
    if starred is not None:
        update_body["starred"] = starred
    if trashed is not None:
        update_body["trashed"] = trashed
    if writers_can_share is not None:
        update_body["writersCanShare"] = writers_can_share
    if copy_requires_writer_permission is not None:
        update_body["copyRequiresWriterPermission"] = copy_requires_writer_permission
    if properties is not None:
        update_body["properties"] = properties

    async def _resolve_parent_arguments(parent_arg: Optional[str]) -> Optional[str]:
        if not parent_arg:
            return None
        parent_ids = [part.strip() for part in parent_arg.split(",") if part.strip()]
        if not parent_ids:
            return None

        resolved_ids = []
        for parent in parent_ids:
            resolved_parent = await resolve_folder_id(service, parent)
            resolved_ids.append(resolved_parent)
        return ",".join(resolved_ids)

    resolved_add_parents = await _resolve_parent_arguments(add_parents)
    resolved_remove_parents = await _resolve_parent_arguments(remove_parents)

    # Build query parameters for parent changes
    query_params = {
        "fileId": file_id,
        "supportsAllDrives": True,
        "fields": "id, name, description, mimeType, parents, starred, trashed, webViewLink, writersCanShare, copyRequiresWriterPermission, properties",
    }

    if resolved_add_parents:
        query_params["addParents"] = resolved_add_parents
    if resolved_remove_parents:
        query_params["removeParents"] = resolved_remove_parents

    # Only include body if there are updates
    if update_body:
        query_params["body"] = update_body

    # Perform the update
    updated_file = await asyncio.to_thread(
        service.files().update(**query_params).execute
    )

    # Build response message
    output_parts = [
        f"✅ Successfully updated file: {updated_file.get('name', current_file['name'])}"
    ]
    output_parts.append(f"   File ID: {file_id}")

    # Report what changed
    changes = []
    if name is not None and name != current_file.get("name"):
        changes.append(f"   • Name: '{current_file.get('name')}' → '{name}'")
    if description is not None:
        old_desc_value = current_file.get("description")
        new_desc_value = description
        should_report_change = (old_desc_value or "") != (new_desc_value or "")
        if should_report_change:
            old_desc_display = (
                old_desc_value if old_desc_value not in (None, "") else "(empty)"
            )
            new_desc_display = (
                new_desc_value if new_desc_value not in (None, "") else "(empty)"
            )
            changes.append(f"   • Description: {old_desc_display} → {new_desc_display}")
    if add_parents:
        changes.append(f"   • Added to folder(s): {add_parents}")
    if remove_parents:
        changes.append(f"   • Removed from folder(s): {remove_parents}")
    current_starred = current_file.get("starred")
    if starred is not None and starred != current_starred:
        star_status = "starred" if starred else "unstarred"
        changes.append(f"   • File {star_status}")
    current_trashed = current_file.get("trashed")
    if trashed is not None and trashed != current_trashed:
        trash_status = "moved to trash" if trashed else "restored from trash"
        changes.append(f"   • File {trash_status}")
    current_writers_can_share = current_file.get("writersCanShare")
    if writers_can_share is not None and writers_can_share != current_writers_can_share:
        share_status = "can" if writers_can_share else "cannot"
        changes.append(f"   • Writers {share_status} share the file")
    current_copy_requires_writer_permission = current_file.get(
        "copyRequiresWriterPermission"
    )
    if (
        copy_requires_writer_permission is not None
        and copy_requires_writer_permission != current_copy_requires_writer_permission
    ):
        copy_status = (
            "requires" if copy_requires_writer_permission else "doesn't require"
        )
        changes.append(f"   • Copying {copy_status} writer permission")
    if properties:
        changes.append(f"   • Updated custom properties: {properties}")

    if changes:
        output_parts.append("")
        output_parts.append("Changes applied:")
        output_parts.extend(changes)
    else:
        output_parts.append("   (No changes were made)")

    output_parts.append("")
    output_parts.append(f"View file: {updated_file.get('webViewLink', '#')}")

    return "\n".join(output_parts)


@server.tool(
    title="Get Drive Shareable Link",
    annotations=ToolAnnotations(
        readOnlyHint=True,
        destructiveHint=False,
        idempotentHint=True,
        openWorldHint=True,
    ),
)
@handle_http_errors("get_drive_shareable_link", is_read_only=True, service_type="drive")
@require_google_service("drive", "drive_read")
async def get_drive_shareable_link(
    service,
    user_google_email: str,
    file_id: str,
) -> str:
    """
    Gets the shareable link for a Google Drive file or folder.

    Args:
        user_google_email (str): The user's Google email address. Required.
        file_id (str): The ID of the file or folder to get the shareable link for. Required.

    Returns:
        str: The shareable links and current sharing status.
    """
    logger.info(
        f"[get_drive_shareable_link] Invoked. Email: '{user_google_email}', File ID: '{file_id}'"
    )

    resolved_file_id, _ = await resolve_drive_item(service, file_id)
    file_id = resolved_file_id

    file_metadata = await asyncio.to_thread(
        service.files()
        .get(
            fileId=file_id,
            fields="id, name, mimeType, webViewLink, webContentLink, shared, "
            "permissions(id, type, role, emailAddress, domain, expirationTime)",
            supportsAllDrives=True,
        )
        .execute
    )

    output_parts = [
        f"File: {file_metadata.get('name', 'Unknown')}",
        f"ID: {file_id}",
        f"Type: {file_metadata.get('mimeType', 'Unknown')}",
        f"Shared: {file_metadata.get('shared', False)}",
        "",
        "Links:",
        f"  View: {file_metadata.get('webViewLink', 'N/A')}",
    ]

    web_content_link = file_metadata.get("webContentLink")
    if web_content_link:
        output_parts.append(f"  Download: {web_content_link}")

    permissions = file_metadata.get("permissions", [])
    if permissions:
        output_parts.append("")
        output_parts.append("Current permissions:")
        for perm in permissions:
            output_parts.append(f"  - {format_permission_info(perm)}")

    return "\n".join(output_parts)


@server.tool(
    title="Manage Drive Access",
    annotations=ToolAnnotations(
        readOnlyHint=False,
        destructiveHint=True,
        idempotentHint=False,
        openWorldHint=True,
    ),
)
@handle_http_errors("manage_drive_access", is_read_only=False, service_type="drive")
@require_google_service("drive", "drive_file")
async def manage_drive_access(
    service,
    user_google_email: str,
    file_id: str,
    action: str,
    share_with: Optional[str] = None,
    role: Optional[str] = None,
    share_type: str = "user",
    permission_id: Optional[str] = None,
    recipients: Optional[List[Dict[str, Any]]] = None,
    send_notification: bool = True,
    email_message: Optional[str] = None,
    expiration_time: Optional[str] = None,
    allow_file_discovery: Optional[bool] = None,
    new_owner_email: Optional[str] = None,
    move_to_new_owners_root: bool = False,
) -> str:
    """
    Consolidated tool for managing Google Drive file and folder access permissions.

    Supports granting, batch-granting, updating, revoking permissions, and
    transferring file ownership -- all through a single entry point.

    Args:
        user_google_email (str): The user's Google email address. Required.
        file_id (str): The ID of the file or folder. Required.
        action (str): The access management action to perform. Required. One of:
            - "grant": Share with a single user, group, domain, or anyone.
            - "grant_batch": Share with multiple recipients in one call.
            - "update": Modify an existing permission (role or expiration).
            - "revoke": Remove an existing permission.
            - "transfer_owner": Transfer file ownership to another user.
        share_with (Optional[str]): Email address (user/group), domain name (domain),
            or omit for 'anyone'. Used by "grant".
        role (Optional[str]): Permission role -- 'reader', 'commenter', or 'writer'.
            Used by "grant" (defaults to 'reader') and "update".
        share_type (str): Type of sharing -- 'user', 'group', 'domain', or 'anyone'.
            Used by "grant". Defaults to 'user'.
        permission_id (Optional[str]): The permission ID to modify or remove.
            Required for "update" and "revoke" actions.
        recipients (Optional[List[Dict[str, Any]]]): List of recipient objects for
            "grant_batch". Each should have: email (str), role (str, optional),
            share_type (str, optional), expiration_time (str, optional). For domain
            shares use 'domain' field instead of 'email'.
        send_notification (bool): Whether to send notification emails. Defaults to True.
            Used by "grant" and "grant_batch".
        email_message (Optional[str]): Custom notification email message.
            Used by "grant" and "grant_batch".
        expiration_time (Optional[str]): Expiration in RFC 3339 format
            (e.g., "2025-01-15T00:00:00Z"). Used by "grant" and "update".
        allow_file_discovery (Optional[bool]): For 'domain'/'anyone' shares, whether
            the file appears in search. Used by "grant".
        new_owner_email (Optional[str]): Email of the new owner.
            Required for "transfer_owner".
        move_to_new_owners_root (bool): Move file to the new owner's My Drive root.
            Defaults to False. Used by "transfer_owner".

    Returns:
        str: Confirmation with details of the permission change applied.
    """
    valid_actions = ("grant", "grant_batch", "update", "revoke", "transfer_owner")
    if action not in valid_actions:
        raise ValueError(
            f"Invalid action '{action}'. Must be one of: {', '.join(valid_actions)}"
        )

    logger.info(
        f"[manage_drive_access] Invoked. Email: '{user_google_email}', "
        f"File ID: '{file_id}', Action: '{action}'"
    )

    # --- grant: share with a single recipient ---
    if action == "grant":
        effective_role = role or "reader"
        validate_share_role(effective_role)
        validate_share_type(share_type)

        if share_type in ("user", "group") and not share_with:
            raise ValueError(f"share_with is required for share_type '{share_type}'")
        if share_type == "domain" and not share_with:
            raise ValueError(
                "share_with (domain name) is required for share_type 'domain'"
            )

        resolved_file_id, file_metadata = await resolve_drive_item(
            service, file_id, extra_fields="name, webViewLink"
        )
        file_id = resolved_file_id

        permission_body: Dict[str, Any] = {
            "type": share_type,
            "role": effective_role,
        }
        if share_type in ("user", "group"):
            permission_body["emailAddress"] = share_with
        elif share_type == "domain":
            permission_body["domain"] = share_with

        if expiration_time:
            validate_expiration_time(expiration_time)
            permission_body["expirationTime"] = expiration_time

        if share_type in ("domain", "anyone") and allow_file_discovery is not None:
            permission_body["allowFileDiscovery"] = allow_file_discovery

        create_params: Dict[str, Any] = {
            "fileId": file_id,
            "body": permission_body,
            "supportsAllDrives": True,
            "fields": "id, type, role, emailAddress, domain, expirationTime",
        }
        if share_type in ("user", "group"):
            create_params["sendNotificationEmail"] = send_notification
            if email_message:
                create_params["emailMessage"] = email_message

        created_permission = await asyncio.to_thread(
            service.permissions().create(**create_params).execute
        )

        return "\n".join(
            [
                f"Successfully shared '{file_metadata.get('name', 'Unknown')}'",
                "",
                "Permission created:",
                f"  - {format_permission_info(created_permission)}",
                "",
                f"View link: {file_metadata.get('webViewLink', 'N/A')}",
            ]
        )

    # --- grant_batch: share with multiple recipients ---
    if action == "grant_batch":
        if not recipients:
            raise ValueError("recipients list is required for 'grant_batch' action")

        resolved_file_id, file_metadata = await resolve_drive_item(
            service, file_id, extra_fields="name, webViewLink"
        )
        file_id = resolved_file_id

        results: List[str] = []
        success_count = 0
        failure_count = 0

        for recipient in recipients:
            r_share_type = recipient.get("share_type", "user")

            if r_share_type == "domain":
                domain = recipient.get("domain")
                if not domain:
                    results.append("  - Skipped: missing domain for domain share")
                    failure_count += 1
                    continue
                identifier = domain
            else:
                r_email = recipient.get("email")
                if not r_email:
                    results.append("  - Skipped: missing email address")
                    failure_count += 1
                    continue
                identifier = r_email

            r_role = recipient.get("role", "reader")
            try:
                validate_share_role(r_role)
            except ValueError as e:
                results.append(f"  - {identifier}: Failed - {e}")
                failure_count += 1
                continue

            try:
                validate_share_type(r_share_type)
            except ValueError as e:
                results.append(f"  - {identifier}: Failed - {e}")
                failure_count += 1
                continue

            r_perm_body: Dict[str, Any] = {
                "type": r_share_type,
                "role": r_role,
            }
            if r_share_type == "domain":
                r_perm_body["domain"] = identifier
            else:
                r_perm_body["emailAddress"] = identifier

            if recipient.get("expiration_time"):
                try:
                    validate_expiration_time(recipient["expiration_time"])
                    r_perm_body["expirationTime"] = recipient["expiration_time"]
                except ValueError as e:
                    results.append(f"  - {identifier}: Failed - {e}")
                    failure_count += 1
                    continue

            r_create_params: Dict[str, Any] = {
                "fileId": file_id,
                "body": r_perm_body,
                "supportsAllDrives": True,
                "fields": "id, type, role, emailAddress, domain, expirationTime",
            }
            if r_share_type in ("user", "group"):
                r_create_params["sendNotificationEmail"] = send_notification
                if email_message:
                    r_create_params["emailMessage"] = email_message

            try:
                created_perm = await asyncio.to_thread(
                    service.permissions().create(**r_create_params).execute
                )
                results.append(f"  - {format_permission_info(created_perm)}")
                success_count += 1
            except HttpError as e:
                results.append(f"  - {identifier}: Failed - {str(e)}")
                failure_count += 1

        output_parts = [
            f"Batch share results for '{file_metadata.get('name', 'Unknown')}'",
            "",
            f"Summary: {success_count} succeeded, {failure_count} failed",
            "",
            "Results:",
        ]
        output_parts.extend(results)
        output_parts.extend(
            [
                "",
                f"View link: {file_metadata.get('webViewLink', 'N/A')}",
            ]
        )
        return "\n".join(output_parts)

    # --- update: modify an existing permission ---
    if action == "update":
        if not permission_id:
            raise ValueError("permission_id is required for 'update' action")
        if not role and not expiration_time:
            raise ValueError(
                "Must provide at least one of: role, expiration_time for 'update' action"
            )

        if role:
            validate_share_role(role)
        if expiration_time:
            validate_expiration_time(expiration_time)

        resolved_file_id, file_metadata = await resolve_drive_item(
            service, file_id, extra_fields="name"
        )
        file_id = resolved_file_id

        effective_role = role
        if not effective_role:
            current_permission = await asyncio.to_thread(
                service.permissions()
                .get(
                    fileId=file_id,
                    permissionId=permission_id,
                    supportsAllDrives=True,
                    fields="role",
                )
                .execute
            )
            effective_role = current_permission.get("role")

        update_body: Dict[str, Any] = {"role": effective_role}
        if expiration_time:
            update_body["expirationTime"] = expiration_time

        updated_permission = await asyncio.to_thread(
            service.permissions()
            .update(
                fileId=file_id,
                permissionId=permission_id,
                body=update_body,
                supportsAllDrives=True,
                fields="id, type, role, emailAddress, domain, expirationTime",
            )
            .execute
        )

        return "\n".join(
            [
                f"Successfully updated permission on '{file_metadata.get('name', 'Unknown')}'",
                "",
                "Updated permission:",
                f"  - {format_permission_info(updated_permission)}",
            ]
        )

    # --- revoke: remove an existing permission ---
    if action == "revoke":
        if not permission_id:
            raise ValueError("permission_id is required for 'revoke' action")

        resolved_file_id, file_metadata = await resolve_drive_item(
            service, file_id, extra_fields="name"
        )
        file_id = resolved_file_id

        await asyncio.to_thread(
            service.permissions()
            .delete(
                fileId=file_id,
                permissionId=permission_id,
                supportsAllDrives=True,
            )
            .execute
        )

        return "\n".join(
            [
                f"Successfully removed permission from '{file_metadata.get('name', 'Unknown')}'",
                "",
                f"Permission ID '{permission_id}' has been revoked.",
            ]
        )

    # --- transfer_owner: transfer file ownership ---
    # action == "transfer_owner"
    if not new_owner_email:
        raise ValueError("new_owner_email is required for 'transfer_owner' action")

    resolved_file_id, file_metadata = await resolve_drive_item(
        service, file_id, extra_fields="name, owners"
    )
    file_id = resolved_file_id

    current_owners = file_metadata.get("owners", [])
    current_owner_emails = [o.get("emailAddress", "") for o in current_owners]

    transfer_body: Dict[str, Any] = {
        "type": "user",
        "role": "owner",
        "emailAddress": new_owner_email,
    }

    await asyncio.to_thread(
        service.permissions()
        .create(
            fileId=file_id,
            body=transfer_body,
            transferOwnership=True,
            moveToNewOwnersRoot=move_to_new_owners_root,
            supportsAllDrives=True,
            fields="id, type, role, emailAddress",
        )
        .execute
    )

    output_parts = [
        f"Successfully transferred ownership of '{file_metadata.get('name', 'Unknown')}'",
        "",
        f"New owner: {new_owner_email}",
        f"Previous owner(s): {', '.join(current_owner_emails) or 'Unknown'}",
    ]
    if move_to_new_owners_root:
        output_parts.append(f"File moved to {new_owner_email}'s My Drive root.")
    output_parts.extend(["", "Note: Previous owner now has editor access."])

    return "\n".join(output_parts)


@server.tool(
    title="Copy Drive File",
    annotations=ToolAnnotations(
        readOnlyHint=False,
        destructiveHint=False,
        idempotentHint=False,
        openWorldHint=True,
    ),
)
@handle_http_errors("copy_drive_file", is_read_only=False, service_type="drive")
@require_google_service("drive", "drive_file")
async def copy_drive_file(
    service,
    user_google_email: str,
    file_id: str,
    new_name: Optional[str] = None,
    parent_folder_id: str = "root",
) -> str:
    """
    Creates a copy of an existing Google Drive file.

    This tool copies the template document to a new location with an optional new name.
    The copy maintains all formatting and content from the original file.

    Args:
        user_google_email (str): The user's Google email address. Required.
        file_id (str): The ID of the file to copy. Required.
        new_name (Optional[str]): New name for the copied file. If not provided, uses "Copy of [original name]".
        parent_folder_id (str): The ID of the folder where the copy should be created. Defaults to 'root' (My Drive).

    Returns:
        str: Confirmation message with details of the copied file and its link.
    """
    logger.info(
        f"[copy_drive_file] Invoked. Email: '{user_google_email}', File ID: '{file_id}', New name: '{new_name}', Parent folder: '{parent_folder_id}'"
    )

    resolved_file_id, file_metadata = await resolve_drive_item(
        service, file_id, extra_fields="name, webViewLink, mimeType"
    )
    file_id = resolved_file_id
    original_name = file_metadata.get("name", "Unknown File")

    resolved_folder_id = await resolve_folder_id(service, parent_folder_id)

    copy_body = {}
    if new_name:
        copy_body["name"] = new_name
    else:
        copy_body["name"] = f"Copy of {original_name}"

    if resolved_folder_id != "root":
        copy_body["parents"] = [resolved_folder_id]

    copied_file = await asyncio.to_thread(
        service.files()
        .copy(
            fileId=file_id,
            body=copy_body,
            supportsAllDrives=True,
            fields="id, name, webViewLink, mimeType, parents",
        )
        .execute
    )

    output_parts = [
        f"Successfully copied '{original_name}'",
        "",
        f"Original file ID: {file_id}",
        f"New file ID: {copied_file.get('id', 'N/A')}",
        f"New file name: {copied_file.get('name', 'Unknown')}",
        f"File type: {copied_file.get('mimeType', 'Unknown')}",
        f"Location: {parent_folder_id}",
        "",
        f"View copied file: {copied_file.get('webViewLink', 'N/A')}",
    ]

    return "\n".join(output_parts)


@server.tool(
    title="Set Drive File Permissions",
    annotations=ToolAnnotations(
        readOnlyHint=False,
        destructiveHint=True,
        idempotentHint=False,
        openWorldHint=True,
    ),
)
@handle_http_errors(
    "set_drive_file_permissions", is_read_only=False, service_type="drive"
)
@require_google_service("drive", "drive_file")
async def set_drive_file_permissions(
    service,
    user_google_email: str,
    file_id: str,
    link_sharing: Optional[str] = None,
    writers_can_share: Optional[bool] = None,
    copy_requires_writer_permission: Optional[bool] = None,
) -> str:
    """
    Sets file-level sharing settings and controls link sharing for a Google Drive file or folder.

    This is a high-level tool for the most common permission changes. Use this to toggle
    "anyone with the link" access or configure file-level sharing behavior. For managing
    individual user/group permissions, use share_drive_file or update_drive_permission instead.

    Args:
        user_google_email (str): The user's Google email address. Required.
        file_id (str): The ID of the file or folder. Required.
        link_sharing (Optional[str]): Control "anyone with the link" access for the file.
            - "off": Disable "anyone with the link" access for this file.
            - "reader": Anyone with the link can view.
            - "commenter": Anyone with the link can comment.
            - "writer": Anyone with the link can edit.
        writers_can_share (Optional[bool]): Whether editors can change permissions and share.
            If False, only the owner can share. Defaults to None (no change).
        copy_requires_writer_permission (Optional[bool]): Whether viewers and commenters
            are prevented from copying, printing, or downloading. Defaults to None (no change).

    Returns:
        str: Summary of all permission changes applied to the file.
    """
    logger.info(
        f"[set_drive_file_permissions] Invoked. Email: '{user_google_email}', "
        f"File ID: '{file_id}', Link sharing: '{link_sharing}', "
        f"Writers can share: {writers_can_share}, Copy restriction: {copy_requires_writer_permission}"
    )

    if (
        link_sharing is None
        and writers_can_share is None
        and copy_requires_writer_permission is None
    ):
        raise ValueError(
            "Must provide at least one of: link_sharing, writers_can_share, copy_requires_writer_permission"
        )

    valid_link_sharing = {"off", "reader", "commenter", "writer"}
    if link_sharing is not None and link_sharing not in valid_link_sharing:
        raise ValueError(
            f"Invalid link_sharing '{link_sharing}'. Must be one of: {', '.join(sorted(valid_link_sharing))}"
        )

    resolved_file_id, file_metadata = await resolve_drive_item(
        service, file_id, extra_fields="name, webViewLink"
    )
    file_id = resolved_file_id
    file_name = file_metadata.get("name", "Unknown")

    output_parts = [f"Permission settings updated for '{file_name}'", ""]
    changes_made = []

    # Handle file-level settings via files().update()
    file_update_body = {}
    if writers_can_share is not None:
        file_update_body["writersCanShare"] = writers_can_share
    if copy_requires_writer_permission is not None:
        file_update_body["copyRequiresWriterPermission"] = (
            copy_requires_writer_permission
        )

    if file_update_body:
        await asyncio.to_thread(
            service.files()
            .update(
                fileId=file_id,
                body=file_update_body,
                supportsAllDrives=True,
                fields="id",
            )
            .execute
        )
        if writers_can_share is not None:
            state = "allowed" if writers_can_share else "restricted to owner"
            changes_made.append(f"  - Editors sharing: {state}")
        if copy_requires_writer_permission is not None:
            state = "restricted" if copy_requires_writer_permission else "allowed"
            changes_made.append(f"  - Viewers copy/print/download: {state}")

    # Handle link sharing via permissions API
    if link_sharing is not None:
        current_permissions = await asyncio.to_thread(
            service.permissions()
            .list(
                fileId=file_id,
                supportsAllDrives=True,
                fields="permissions(id, type, role)",
            )
            .execute
        )
        anyone_perms = [
            p
            for p in current_permissions.get("permissions", [])
            if p.get("type") == "anyone"
        ]

        if link_sharing == "off":
            if anyone_perms:
                for perm in anyone_perms:
                    await asyncio.to_thread(
                        service.permissions()
                        .delete(
                            fileId=file_id,
                            permissionId=perm["id"],
                            supportsAllDrives=True,
                        )
                        .execute
                    )
                changes_made.append(
                    "  - Link sharing: disabled (restricted to specific people)"
                )
            else:
                changes_made.append("  - Link sharing: already off (no change)")
        else:
            if anyone_perms:
                await asyncio.to_thread(
                    service.permissions()
                    .update(
                        fileId=file_id,
                        permissionId=anyone_perms[0]["id"],
                        body={
                            "role": link_sharing,
                            "allowFileDiscovery": False,
                        },
                        supportsAllDrives=True,
                        fields="id, type, role",
                    )
                    .execute
                )
                changes_made.append(f"  - Link sharing: updated to '{link_sharing}'")
            else:
                await asyncio.to_thread(
                    service.permissions()
                    .create(
                        fileId=file_id,
                        body={
                            "type": "anyone",
                            "role": link_sharing,
                            "allowFileDiscovery": False,
                        },
                        supportsAllDrives=True,
                        fields="id, type, role",
                    )
                    .execute
                )
                changes_made.append(f"  - Link sharing: enabled as '{link_sharing}'")

    output_parts.append("Changes:")
    if changes_made:
        output_parts.extend(changes_made)
    else:
        output_parts.append("  - No changes (already configured)")
    output_parts.extend(["", f"View link: {file_metadata.get('webViewLink', 'N/A')}"])

    return "\n".join(output_parts)
