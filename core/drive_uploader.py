import io
import json as _json_lib
from googleapiclient.discovery import build
from googleapiclient.http import MediaIoBaseUpload, MediaIoBaseDownload
from google.oauth2.service_account import Credentials
from PIL import Image

SCOPES = ["https://www.googleapis.com/auth/drive"]

REF_IMAGE_FOLDER = "参照画像"


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
    media = MediaIoBaseUpload(io.BytesIO(image_bytes), mimetype="image/webp")
    metadata = {"name": filename, "parents": [slug_folder_id]}
    file = service.files().create(
        body=metadata,
        media_body=media,
        fields="id, webViewLink",
        supportsAllDrives=True,
    ).execute()
    return file.get("webViewLink", "")


def upload_images_batch(
    images: list[tuple[bytes, str]],
    site_name: str,
    slug: str,
    credentials_dict: dict,
    parent_folder_id: str,
) -> list[str]:
    """
    複数画像を同一フォルダにまとめてアップロード。
    フォルダ構造を1回だけ作成してフォルダIDを使い回すことで、
    Shared Drive の結果整合性問題（重複フォルダ作成）を回避する。
    images: [(image_bytes, filename), ...]
    Returns: webViewLink のリスト（失敗分は空文字）
    """
    service = _get_service(credentials_dict)
    images_folder_id = _find_or_create_folder(service, "生成画像", parent_folder_id)
    site_folder_id = _find_or_create_folder(service, site_name or "default", images_folder_id)
    slug_folder_id = _find_or_create_folder(service, slug, site_folder_id)
    results = []
    for image_bytes, filename in images:
        try:
            media = MediaIoBaseUpload(io.BytesIO(image_bytes), mimetype="image/webp")
            metadata = {"name": filename, "parents": [slug_folder_id]}
            file = service.files().create(
                body=metadata, media_body=media,
                fields="id, webViewLink", supportsAllDrives=True,
            ).execute()
            results.append(file.get("webViewLink", ""))
        except Exception:
            results.append("")
    return results


def _find_or_create_folder_path(service, path: list, root_id: str) -> str:
    """パスリストを順番に辿り、存在しないフォルダは作成して末端のIDを返す。"""
    current = root_id
    for name in path:
        current = _find_or_create_folder(service, name, current)
    return current


def upload_reference_image(
    image_bytes: bytes,
    filename: str,
    site_id: str,
    credentials_dict: dict,
    parent_folder_id: str,
) -> str:
    """参照画像をDriveにアップロード: parent / 参照画像 / site_id / filename
    Returns: webViewLink
    """
    service = _get_service(credentials_dict)
    ref_folder_id = _find_or_create_folder(service, REF_IMAGE_FOLDER, parent_folder_id)
    site_folder_id = _find_or_create_folder(service, site_id, ref_folder_id)
    mimetype = "image/png" if filename.lower().endswith(".png") else "image/jpeg"
    media = MediaIoBaseUpload(io.BytesIO(image_bytes), mimetype=mimetype)
    metadata = {"name": filename, "parents": [site_folder_id]}
    file = service.files().create(
        body=metadata,
        media_body=media,
        fields="id, webViewLink",
        supportsAllDrives=True,
    ).execute()
    return file.get("webViewLink", "")


def list_reference_images(
    site_id: str,
    credentials_dict: dict,
    parent_folder_id: str,
) -> list[dict]:
    """サイトの参照画像一覧を返す（id・name・mimeType）"""
    service = _get_service(credentials_dict)
    # 参照画像フォルダを探す（なければ空リスト）
    query = (
        f"name='{REF_IMAGE_FOLDER}' and mimeType='application/vnd.google-apps.folder' "
        f"and '{parent_folder_id}' in parents and trashed=false"
    )
    res = service.files().list(
        q=query, fields="files(id)", supportsAllDrives=True, includeItemsFromAllDrives=True,
    ).execute()
    if not res.get("files"):
        return []
    ref_folder_id = res["files"][0]["id"]

    query = (
        f"name='{site_id}' and mimeType='application/vnd.google-apps.folder' "
        f"and '{ref_folder_id}' in parents and trashed=false"
    )
    res = service.files().list(
        q=query, fields="files(id)", supportsAllDrives=True, includeItemsFromAllDrives=True,
    ).execute()
    if not res.get("files"):
        return []
    site_folder_id = res["files"][0]["id"]

    res = service.files().list(
        q=f"'{site_folder_id}' in parents and trashed=false",
        fields="files(id, name, mimeType)",
        supportsAllDrives=True,
        includeItemsFromAllDrives=True,
    ).execute()
    return res.get("files", [])


def download_reference_images(
    site_id: str,
    credentials_dict: dict,
    parent_folder_id: str,
    max_images: int = 5,
) -> list[Image.Image]:
    """サイトの参照画像をDriveからDLしてPIL Imageのリストで返す（最大max_images枚）"""
    service = _get_service(credentials_dict)
    files = list_reference_images(site_id, credentials_dict, parent_folder_id)
    images = []
    for f in files[:max_images]:
        try:
            request = service.files().get_media(
                fileId=f["id"], supportsAllDrives=True,
            )
            buf = io.BytesIO()
            downloader = MediaIoBaseDownload(buf, request)
            done = False
            while not done:
                _, done = downloader.next_chunk()
            buf.seek(0)
            images.append(Image.open(buf).convert("RGB"))
        except Exception:
            continue
    return images


def delete_reference_image(
    file_id: str,
    credentials_dict: dict,
) -> tuple[bool, str]:
    """参照画像をDriveのゴミ箱に移動（永久削除はOrganizer権限が必要なためtrashを使用）。Returns: (success, error_message)"""
    try:
        service = _get_service(credentials_dict)
        service.files().update(
            fileId=file_id,
            body={"trashed": True},
            supportsAllDrives=True,
        ).execute()
        return True, ""
    except Exception as e:
        return False, str(e)


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
