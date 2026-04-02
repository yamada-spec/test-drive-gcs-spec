"""
Google Drive 上の指定フォルダを Google Cloud Storage にコピーする。

環境変数:
  GCS_BUCKET: 必須。アップロード先バケット名
  DRIVE_PARENT_FOLDER_ID: 必須。直下に common, bq-staging, analysis, chat-logs がある Drive フォルダの ID

認証:
  GOOGLE_APPLICATION_CREDENTIALS にサービスアカウント JSON を指定するか、
  gcloud auth application-default login で ADC を用意する。
  Drive の対象ファイルはサービスアカウントに共有されている必要がある。

  必要なスコープ: Drive 読み取り、GCS 書き込み（コード内で指定）
"""

from __future__ import annotations

import io
import os
import sys
from dataclasses import dataclass
from enum import Enum
from typing import Callable, Iterator

import html2text
from google.api_core import exceptions as gcs_exceptions
from google.auth import default as google_auth_default
from google.auth.transport.requests import Request
from google.oauth2 import service_account
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError
from googleapiclient.http import MediaIoBaseDownload
from google.cloud import storage

# Drive API
MIME_FOLDER = "application/vnd.google-apps.folder"
MIME_GOOGLE_DOC = "application/vnd.google-apps.document"
MIME_GOOGLE_SHEET = "application/vnd.google-apps.spreadsheet"
MIME_GOOGLE_SLIDES = "application/vnd.google-apps.presentation"
EXPORT_HTML = "text/html"
EXPORT_PDF = "application/pdf"
EXPORT_TXT = "text/plain"
EXPORT_CSV = "text/csv"


class TransformMode(str, Enum):
    """Drive サブフォルダごとのコピー方針（仕様に合わせる）。"""

    COMMON = "common"
    BQ_STAGING = "bq_staging"
    ANALYSIS = "analysis"
    CHAT_LOGS = "chat_logs"


@dataclass(frozen=True)
class FolderRule:
    drive_subfolder_name: str
    gcs_prefix: str
    mode: TransformMode


RULES: tuple[FolderRule, ...] = (
    FolderRule("common", "common", TransformMode.COMMON),
    FolderRule("bq-staging", "bq-staging", TransformMode.BQ_STAGING),
    FolderRule("analysis", "analysis", TransformMode.ANALYSIS),
    FolderRule("chat-logs", "chat-logs", TransformMode.CHAT_LOGS),
)


def _drive_query_escape(s: str) -> str:
    return s.replace("\\", "\\\\").replace("'", "\\'")


def _with_pdf_extension(path: str) -> str:
    if path.lower().endswith(".pdf"):
        return path
    base, _ = os.path.splitext(path)
    return f"{base}.pdf"


def _with_csv_extension(path: str) -> str:
    if path.lower().endswith(".csv"):
        return path
    base, _ = os.path.splitext(path)
    return f"{base}.csv"


def _with_md_extension(path: str) -> str:
    if path.lower().endswith(".md"):
        return path
    return f"{path}.md"


def _with_txt_extension(path: str) -> str:
    if path.lower().endswith(".txt"):
        return path
    base, _ = os.path.splitext(path)
    return f"{base}.txt"


def _credentials():
    """Drive + GCS 用。単一のサービスアカウント JSON があればそれを使用。"""
    path = os.environ.get("GOOGLE_APPLICATION_CREDENTIALS")
    scopes = (
        "https://www.googleapis.com/auth/drive.readonly",
        "https://www.googleapis.com/auth/devstorage.read_write",
    )
    if path and os.path.isfile(path):
        return service_account.Credentials.from_service_account_file(path, scopes=scopes)
    creds, _ = google_auth_default(scopes=scopes)
    if hasattr(creds, "with_scopes"):
        creds = creds.with_scopes(scopes)
    if creds.expired and creds.refresh_token:
        creds.refresh(Request())
    return creds


def _drive_service(creds):
    return build("drive", "v3", credentials=creds, cache_discovery=False)


def _list_children(service, parent_id: str) -> list[dict]:
    """フォルダ直下のファイル・フォルダ一覧（ページング対応）。"""
    items: list[dict] = []
    page_token = None
    q = f"'{parent_id}' in parents and trashed = false"
    while True:
        resp = (
            service.files()
            .list(
                q=q,
                spaces="drive",
                fields="nextPageToken, files(id, name, mimeType)",
                pageToken=page_token,
                pageSize=1000,
                supportsAllDrives=True,
                includeItemsFromAllDrives=True,
            )
            .execute()
        )
        items.extend(resp.get("files", []))
        page_token = resp.get("nextPageToken")
        if not page_token:
            break
    return items


def _get_folder_id_by_name(service, parent_id: str, name: str) -> str | None:
    esc = _drive_query_escape(name)
    q = (
        f"'{parent_id}' in parents and name = '{esc}' "
        f"and mimeType = '{MIME_FOLDER}' and trashed = false"
    )
    resp = (
        service.files()
        .list(
            q=q,
            fields="files(id, name)",
            pageSize=10,
            supportsAllDrives=True,
            includeItemsFromAllDrives=True,
        )
        .execute()
    )
    files = resp.get("files", [])
    if not files:
        return None
    return files[0]["id"]


def _iter_files_recursive(
    service, folder_id: str, rel_prefix: str
) -> Iterator[tuple[str, str, str]]:
    """
    (相対パス, fileId, mimeType) を再帰的に列挙。
    rel_prefix は末尾スラッシュ付き（例: "sub/"）。
    """
    for item in _list_children(service, folder_id):
        name = item["name"]
        mid = item["mimeType"]
        iid = item["id"]
        if mid == MIME_FOLDER:
            yield from _iter_files_recursive(service, iid, f"{rel_prefix}{name}/")
        else:
            yield f"{rel_prefix}{name}", iid, mid


def _download_media(service, file_id: str) -> bytes:
    request = service.files().get_media(fileId=file_id, supportsAllDrives=True)
    buf = io.BytesIO()
    downloader = MediaIoBaseDownload(buf, request)
    done = False
    while not done:
        _, done = downloader.next_chunk()
    return buf.getvalue()


def _export_media(service, file_id: str, mime: str) -> bytes:
    request = service.files().export_media(fileId=file_id, mimeType=mime)
    buf = io.BytesIO()
    downloader = MediaIoBaseDownload(buf, request)
    done = False
    while not done:
        _, done = downloader.next_chunk()
    return buf.getvalue()


def _html_to_markdown(html: str) -> str:
    h = html2text.HTML2Text()
    h.ignore_links = False
    h.body_width = 0
    return h.handle(html)


def _gcs_blob_name(gcs_prefix: str, relative_path: str) -> str:
    p = f"{gcs_prefix.rstrip('/')}/{relative_path.lstrip('/')}"
    return p.replace("\\", "/")


def _upload_bytes(
    client: storage.Client,
    bucket_name: str,
    blob_name: str,
    data: bytes,
    content_type: str | None,
) -> None:
    bucket = client.bucket(bucket_name)
    blob = bucket.blob(blob_name)
    blob.upload_from_string(data, content_type=content_type)


def _content_type_for_upload(mime: str) -> str | None:
    if mime.startswith("application/") or mime.startswith("text/"):
        return mime
    return None


def _plain_binary_upload(
    service,
    gcs_client: storage.Client,
    bucket_name: str,
    blob_base: str,
    file_id: str,
    mime: str,
) -> str:
    """通常ファイルはメディア取得。Sheet は CSV、スライドは PDF エクスポート。"""
    if mime == MIME_GOOGLE_SHEET:
        data = _export_media(service, file_id, EXPORT_CSV)
        out = _with_csv_extension(blob_base)
        _upload_bytes(gcs_client, bucket_name, out, data, "text/csv")
        return out
    if mime == MIME_GOOGLE_SLIDES:
        data = _export_media(service, file_id, EXPORT_PDF)
        out = _with_pdf_extension(blob_base)
        _upload_bytes(gcs_client, bucket_name, out, data, "application/pdf")
        return out
    data = _download_media(service, file_id)
    ct = _content_type_for_upload(mime)
    _upload_bytes(gcs_client, bucket_name, blob_base, data, ct)
    return blob_base


def _process_file(
    service,
    gcs_client: storage.Client,
    bucket_name: str,
    gcs_prefix: str,
    mode: TransformMode,
    relative_path: str,
    file_id: str,
    mime: str,
) -> str:
    blob_base = _gcs_blob_name(gcs_prefix, relative_path)

    # common: PDF 等はそのまま。Google ドキュメントのみ PDF としてエクスポート（変換なしの実体コピー）
    if mode == TransformMode.COMMON:
        if mime == MIME_GOOGLE_DOC:
            data = _export_media(service, file_id, EXPORT_PDF)
            out = _with_pdf_extension(blob_base)
            _upload_bytes(gcs_client, bucket_name, out, data, "application/pdf")
            return out
        return _plain_binary_upload(
            service, gcs_client, bucket_name, blob_base, file_id, mime
        )

    # bq-staging: CSV / NDJSON 等はそのまま。スプレッドシートは CSV エクスポート
    if mode == TransformMode.BQ_STAGING:
        if mime == MIME_GOOGLE_DOC:
            data = _export_media(service, file_id, EXPORT_PDF)
            out = _with_pdf_extension(blob_base)
            _upload_bytes(gcs_client, bucket_name, out, data, "application/pdf")
            return out
        return _plain_binary_upload(
            service, gcs_client, bucket_name, blob_base, file_id, mime
        )

    # analysis: Google ドキュメントのみ HTML→Markdown
    if mode == TransformMode.ANALYSIS:
        if mime == MIME_GOOGLE_DOC:
            raw = _export_media(service, file_id, EXPORT_HTML)
            md = _html_to_markdown(raw.decode("utf-8", errors="replace"))
            out = _with_md_extension(blob_base)
            _upload_bytes(
                gcs_client, bucket_name, out, md.encode("utf-8"), "text/markdown"
            )
            return out
        return _plain_binary_upload(
            service, gcs_client, bucket_name, blob_base, file_id, mime
        )

    # chat-logs: テキストそのまま。Google ドキュメントはプレーンテキストでエクスポート
    if mode == TransformMode.CHAT_LOGS:
        if mime == MIME_GOOGLE_DOC:
            data = _export_media(service, file_id, EXPORT_TXT)
            out = _with_txt_extension(blob_base)
            _upload_bytes(gcs_client, bucket_name, out, data, "text/plain; charset=utf-8")
            return out
        return _plain_binary_upload(
            service, gcs_client, bucket_name, blob_base, file_id, mime
        )

    raise ValueError(f"未対応のモード: {mode}")


def migrate(
    drive_parent_id: str,
    bucket_name: str,
    rules: tuple[FolderRule, ...] = RULES,
    on_skip: Callable[[str], None] | None = None,
) -> None:
    creds = _credentials()
    service = _drive_service(creds)
    gcs_client = storage.Client(credentials=creds, project=os.environ.get("GOOGLE_CLOUD_PROJECT"))

    for rule in rules:
        folder_id = _get_folder_id_by_name(service, drive_parent_id, rule.drive_subfolder_name)
        if not folder_id:
            msg = f"Drive 上にフォルダが見つかりません: {rule.drive_subfolder_name!r} (親: {drive_parent_id})"
            if on_skip:
                on_skip(msg)
            else:
                print(msg, file=sys.stderr)
            continue

        for rel_path, fid, mime in _iter_files_recursive(service, folder_id, ""):
            try:
                uploaded = _process_file(
                    service,
                    gcs_client,
                    bucket_name,
                    rule.gcs_prefix,
                    rule.mode,
                    rel_path,
                    fid,
                    mime,
                )
                print(f"OK  gs://{bucket_name}/{uploaded}")
            except (HttpError, gcs_exceptions.GoogleAPIError, OSError) as e:
                print(f"NG  {rule.gcs_prefix}/{rel_path}: {e}", file=sys.stderr)


def main() -> None:
    bucket = os.environ.get("GCS_BUCKET")
    parent = os.environ.get("DRIVE_PARENT_FOLDER_ID")
    if not bucket or not parent:
        print(
            "環境変数 GCS_BUCKET と DRIVE_PARENT_FOLDER_ID を設定してください。",
            file=sys.stderr,
        )
        sys.exit(1)
    migrate(parent, bucket)


if __name__ == "__main__":
    main()


from flask import Flask
app = Flask(__name__)

@app.route("/", methods=["POST", "GET"])
def run_sync():
    try:
        main() # 既存の main() 関数を呼び出す
        return "Sync Completed Successfully", 200
    except Exception as e:
        return f"Error: {str(e)}", 500

if __name__ == "__main__":
    # Cloud Run の環境変数 PORT を使用
    port = int(os.environ.get("PORT", 8080))
    app.run(host="0.0.0.0", port=port)