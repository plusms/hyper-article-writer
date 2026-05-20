import io
import json as _json_lib
from googleapiclient.discovery import build
from googleapiclient.http import MediaIoBaseUpload
from google.oauth2.service_account import Credentials

SCOPES = ["https://www.googleapis.com/auth/drive"]


def _get_service(credentials_dict: dict):
    creds = Credentials.from_service_account_info(credentials_dict, scopes=SCOPES)
    return build("drive", "v3", credentials=creds)


def _find_or_create_folder(service, name: str, parent_id: str) -> str:
    query = (
        f"name='{name}' and mimeType='application/vnd.google-apps.folder' "
        f"and '{parent_id}' in parents and trashed=false"
    )
    results = service.files().list(
        q=query,
        fields="files(id)",
        supportsAllDrives=True,
        includeItemsFromAllDrives=True,
    ).execute()
    files = results.get("files", [])
    if files:
        return files[0]["id"]
    metadata = {
        "name": name,
        "mimeType": "application/vnd.google-apps.folder",
        "parents": [parent_id],
    }
    folder = service.files().create(
        body=metadata, fields="id", supportsAllDrives=True,
    ).execute()
    return folder["id"]


def upload_image(
    image_bytes: bytes,
    filename: str,
    site_name: str,
    slug: str,
    credentials_dict: dict,
    parent_folder_id: str,
) -> str:
    """
    Drive にアップロード: parent_folder / site_name / slug / filename
    Returns: webViewLink
    """
    service = _get_service(credentials_dict)
    images_folder_id = _find_or_create_folder(service, "生成画像", parent_folder_id)
    site_folder_id = _find_or_create_folder(service, site_name or "default", images_folder_id)
    slug_folder_id = _find_or_create_folder(service, slug, site_folder_id)
    media = MediaIoBaseUpload(io.BytesIO(image_bytes), mimetype="image/png")
    metadata = {"name": filename, "parents": [slug_folder_id]}
    file = service.files().create(
        body=metadata,
        media_body=media,
        fields="id, webViewLink",
        supportsAllDrives=True,
    ).execute()
    return file.get("webViewLink", "")


def _find_or_create_folder_path(service, path: list, root_id: str) -> str:
    """パスリストを順番に辿り、存在しないフォルダは作成して末端のIDを返す。"""
    current = root_id
    for name in path:
        current = _find_or_create_folder(service, name, current)
    return current


def upload_json(
    data: dict,
    filename: str,
    folder_name,
    credentials_dict: dict,
    parent_folder_id: str,
) -> str:
    """JSONデータをDriveにアップロードして webViewLink を返す。
    folder_name は str（1階層）または list（複数階層）で指定可能。
    例: "edit_logs" または ["修正ログ", "本文", "地域"]
    """
    service = _get_service(credentials_dict)
    if isinstance(folder_name, list):
        folder_id = _find_or_create_folder_path(service, folder_name, parent_folder_id)
    else:
        folder_id = _find_or_create_folder(service, folder_name, parent_folder_id)
    content = _json_lib.dumps(data, ensure_ascii=False, indent=2).encode("utf-8")
    media = MediaIoBaseUpload(io.BytesIO(content), mimetype="application/json")
    metadata = {"name": filename, "parents": [folder_id]}
    file = service.files().create(
        body=metadata,
        media_body=media,
        fields="id, webViewLink",
        supportsAllDrives=True,
    ).execute()
    return file.get("webViewLink", "")
