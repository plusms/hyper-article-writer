import io
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
    site_folder_id = _find_or_create_folder(service, site_name or "default", parent_folder_id)
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
