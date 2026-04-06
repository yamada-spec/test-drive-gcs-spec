"""
「6年テキストテスト」Drive フォルダから PDF のみを GCS に同期する（Cloud Run 用）。

Cloud Run: gunicorn が main:app を起動。リクエストで同期開始。/health は生存確認のみ。
ローカル CLI: python main.py --sync

環境変数:
  GCS_BUCKET: 必須。アップロード先バケット名
  DRIVE_PARENT_FOLDER_ID: 必須。「6年テキストテスト」等の同期ルート Drive フォルダ ID。
    コードに ID を埋め込まず、デプロイ環境で明示設定する（誤同期・環境取り違え防止）。

認証:
  GOOGLE_APPLICATION_CREDENTIALS または ADC。Drive はサービスアカウントに共有されていること。
"""

from __future__ import annotations

import io
import os
import sys
from typing import Iterator

from google.api_core import exceptions as gcs_exceptions
from google.auth import default as google_auth_default
from google.auth.transport.requests import Request
from google.oauth2 import service_account
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError
from googleapiclient.http import MediaIoBaseDownload
from google.cloud import storage
from flask import Flask

MIME_FOLDER = "application/vnd.google-apps.folder"
MIME_PDF = "application/pdf"

# Drive 上の名前 → GCS プレフィックス（末尾スラッシュなし）
SUBFOLDER_TO_GCS_PREFIX: tuple[tuple[str, str], ...] = (
    ("テキスト", "common/textbook"),
    ("テスト", "common/test"),
)


def _drive_query_escape(s: str) -> str:
    return s.replace("\\", "\\\\").replace("'", "\\'")


def _credentials():
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


def _log_parent_children_names(
    service, parent_id: str, sought_folder_name: str
) -> None:
    """名前一致のフォルダが見つからないとき、親フォルダ直下の名前をすべてログに出す。"""
    items = _list_children(service, parent_id)
    print(
        f"[Drive] 親フォルダ {parent_id!r} 内にフォルダ {sought_folder_name!r} が見つかりません。"
        f" 直下は {len(items)} 件です。",
        file=sys.stderr,
    )
    for item in sorted(items, key=lambda x: x["name"]):
        label = "folder" if item["mimeType"] == MIME_FOLDER else "file"
        print(f"  - {item['name']!r} ({label})", file=sys.stderr)


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
        _log_parent_children_names(service, parent_id, name)
        return None
    return files[0]["id"]


def _iter_files_recursive(
    service, folder_id: str, rel_prefix: str
) -> Iterator[tuple[str, str, str]]:
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


def sync_drive_to_gcs(bucket_name: str, root_folder_id: str) -> list[str]:
    """
    ルート直下の「テキスト」「テスト」フォルダを探し、配下の PDF のみを GCS にアップロードする。
    """
    creds = _credentials()
    service = _drive_service(creds)
    gcs_client = storage.Client(
        credentials=creds, project=os.environ.get("GOOGLE_CLOUD_PROJECT")
    )

    lines: list[str] = []
    for drive_name, gcs_prefix in SUBFOLDER_TO_GCS_PREFIX:
        folder_id = _get_folder_id_by_name(service, root_folder_id, drive_name)
        if not folder_id:
            msg = (
                f"スキップ: Drive 上にフォルダが見つかりません "
                f"{drive_name!r} (親: {root_folder_id})"
            )
            print(msg, file=sys.stderr)
            lines.append(msg)
            continue

        for rel_path, file_id, mime in _iter_files_recursive(service, folder_id, ""):
            if mime != MIME_PDF:
                continue
            blob_name = _gcs_blob_name(gcs_prefix, rel_path)
            try:
                data = _download_media(service, file_id)
                _upload_bytes(
                    gcs_client,
                    bucket_name,
                    blob_name,
                    data,
                    "application/pdf",
                )
                ok = f"OK  gs://{bucket_name}/{blob_name}"
                print(ok)
                lines.append(ok)
            except (HttpError, gcs_exceptions.GoogleAPIError, OSError) as e:
                err = f"NG  {gcs_prefix}/{rel_path}: {e}"
                print(err, file=sys.stderr)
                lines.append(err)

    return lines


def main() -> None:
    bucket = os.environ.get("GCS_BUCKET")
    parent = os.environ.get("DRIVE_PARENT_FOLDER_ID")
    if not bucket or not parent:
        print(
            "環境変数 GCS_BUCKET と DRIVE_PARENT_FOLDER_ID を設定してください。",
            file=sys.stderr,
        )
        sys.exit(1)
    sync_drive_to_gcs(bucket, parent)


app = Flask(__name__)


@app.get("/health")
def health():
    """起動・プローブ用。同期は実行しない。"""
    return "ok", 200


@app.route("/", methods=["GET", "POST"])
def run_sync():
    try:
        main()
        return "Sync Completed Successfully", 200
    except Exception as e:
        return f"Error: {str(e)}", 500


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "--sync":
        main()
    else:
        port = int(os.environ.get("PORT", 8080))
        app.run(host="0.0.0.0", port=port)
