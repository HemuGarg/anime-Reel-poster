"""
Auto-post the next reel sitting in a Google Drive folder to an Instagram
Business/Creator account, using the caption typed into that file's
Google Drive "Description" field.

Runs once per invocation — intended to be triggered every morning by the
GitHub Actions workflow in .github/workflows/post_reel.yml (account 2 repo) (or any cron).

Required environment variables (set as GitHub Actions secrets, or export
locally for testing):
    GOOGLE_SERVICE_ACCOUNT_JSON   - full JSON key of a Google Cloud service
                                     account with Drive API access, as one
                                     line of text
    DRIVE_FOLDER_ID               - folder to pull unposted reels from
    POSTED_FOLDER_ID              - folder to move a reel into once posted
    IG_USER_ID                    - Instagram Business Account ID
    IG_ACCESS_TOKEN               - long-lived Instagram/Facebook access
                                     token with instagram_content_publish
    CLOUDINARY_CLOUD_NAME         - Cloudinary account cloud name
    CLOUDINARY_UPLOAD_PRESET      - an UNSIGNED Cloudinary upload preset
"""

import hashlib
import io
import json
import os
import random
import sys
import time

import requests
from google.oauth2 import service_account
from google.oauth2.credentials import Credentials as UserCredentials
from googleapiclient.discovery import build
from googleapiclient.http import MediaIoBaseDownload, MediaIoBaseUpload

DRIVE_SCOPES = ["https://www.googleapis.com/auth/drive"]
GRAPH_API_VERSION = "v21.0"


def load_caption_pool() -> list[str]:
    """anime_captions.json sits next to this script in the repo."""
    path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "anime_captions.json")
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except FileNotFoundError:
        return []


def pick_auto_caption(seed: str) -> str:
    """Pick a caption from the pool, seeded by the video's Drive file id.

    Every video gets its own caption, so the three reels posted on one day
    don't share one. The pick is stable if a run is retried, and two accounts
    never line up because their videos have different ids."""
    pool = load_caption_pool()
    if not pool:
        return ""
    return random.Random(seed).choice(pool)


def get_youtube_service():
    creds = UserCredentials(
        token=None,
        refresh_token=env("YOUTUBE_REFRESH_TOKEN"),
        client_id=env("YOUTUBE_CLIENT_ID"),
        client_secret=env("YOUTUBE_CLIENT_SECRET"),
        token_uri="https://oauth2.googleapis.com/token",
        scopes=["https://www.googleapis.com/auth/youtube.upload"],
    )
    return build("youtube", "v3", credentials=creds)


def upload_to_youtube(video_bytes: io.BytesIO, caption: str):
    """Caption's first line becomes the title (YouTube caps titles at 100
    chars); the full caption becomes the description, same as Instagram."""
    video_bytes.seek(0)
    first_line = caption.splitlines()[0] if caption else "Daily reel"
    title = first_line[:95]

    youtube = get_youtube_service()
    body = {
        "snippet": {
            "title": title,
            "description": caption,
            "categoryId": "22",  # People & Blogs
        },
        "status": {"privacyStatus": "public", "selfDeclaredMadeForKids": False},
    }
    media = MediaIoBaseUpload(video_bytes, mimetype="video/mp4", resumable=True)
    request = youtube.videos().insert(part="snippet,status", body=body, media_body=media)

    response = None
    while response is None:
        _, response = request.next_chunk()
    print(f"YouTube: uploaded as video id {response.get('id')}")


def post_to_facebook_page(video_url: str, caption: str):
    page_id = env("FB_PAGE_ID")
    page_token = env("FB_PAGE_ACCESS_TOKEN")
    url = f"https://graph-video.facebook.com/{GRAPH_API_VERSION}/{page_id}/videos"
    data = {"file_url": video_url, "description": caption, "access_token": page_token}
    resp = requests.post(url, data=data, timeout=300)
    if not resp.ok:
        print(f"Facebook Page upload failed: {resp.text}")
    resp.raise_for_status()
    print(f"Facebook Page: uploaded as video id {resp.json().get('id')}")


def env(name: str) -> str:
    value = os.environ.get(name)
    if not value:
        raise SystemExit(f"Missing required environment variable: {name}")
    return value


def has_env(*names: str) -> bool:
    """True only if every named variable is set and non-empty. A GitHub
    secret that doesn't exist arrives here as an empty string."""
    return all(os.environ.get(n) for n in names)


def get_drive_service():
    creds_info = json.loads(env("GOOGLE_SERVICE_ACCOUNT_JSON"))
    creds = service_account.Credentials.from_service_account_info(
        creds_info, scopes=DRIVE_SCOPES
    )
    return build("drive", "v3", credentials=creds)


def find_next_video(drive, folder_id: str):
    """Return the oldest un-posted video file in the watched folder, or None."""
    query = (
        f"'{folder_id}' in parents and "
        "mimeType contains 'video/' and trashed = false"
    )
    resp = (
        drive.files()
        .list(
            q=query,
            orderBy="createdTime",
            fields="files(id, name, description, mimeType)",
            pageSize=1,
        )
        .execute()
    )
    files = resp.get("files", [])
    return files[0] if files else None


def download_video(drive, file_id: str) -> io.BytesIO:
    request = drive.files().get_media(fileId=file_id)
    buffer = io.BytesIO()
    downloader = MediaIoBaseDownload(buffer, request)
    done = False
    while not done:
        _, done = downloader.next_chunk()
    buffer.seek(0)
    return buffer


def upload_to_cloudinary(video_bytes: io.BytesIO, filename: str) -> tuple[str, str]:
    cloud_name = env("CLOUDINARY_CLOUD_NAME")
    preset = env("CLOUDINARY_UPLOAD_PRESET")
    url = f"https://api.cloudinary.com/v1_1/{cloud_name}/video/upload"
    files = {"file": (filename, video_bytes, "video/mp4")}
    data = {"upload_preset": preset}
    resp = requests.post(url, files=files, data=data, timeout=300)
    resp.raise_for_status()
    payload = resp.json()
    return payload["secure_url"], payload["public_id"]


def delete_from_cloudinary(public_id: str):
    """Remove the temporary copy now that Instagram has fetched it.

    Deleting requires a *signed* request (API key + secret), unlike the
    unsigned upload above, so this needs two extra env vars.
    """
    cloud_name = env("CLOUDINARY_CLOUD_NAME")
    api_key = env("CLOUDINARY_API_KEY")
    api_secret = env("CLOUDINARY_API_SECRET")

    timestamp = str(int(time.time()))
    string_to_sign = f"public_id={public_id}&timestamp={timestamp}{api_secret}"
    signature = hashlib.sha1(string_to_sign.encode("utf-8")).hexdigest()

    url = f"https://api.cloudinary.com/v1_1/{cloud_name}/video/destroy"
    data = {
        "public_id": public_id,
        "timestamp": timestamp,
        "api_key": api_key,
        "signature": signature,
    }
    resp = requests.post(url, data=data, timeout=60)
    if not resp.ok:
        # Not fatal - the reel is already posted. Just log it and move on.
        print(f"Warning: could not delete Cloudinary copy: {resp.text}")
        return
    print(f"Cloudinary cleanup: {resp.json().get('result')}")


def create_ig_container(video_url: str, caption: str) -> str:
    ig_user_id = env("IG_USER_ID")
    token = env("IG_ACCESS_TOKEN")
    url = f"https://graph.instagram.com/{GRAPH_API_VERSION}/{ig_user_id}/media"
    params = {
        "media_type": "REELS",
        "video_url": video_url,
        "caption": caption,
        "access_token": token,
    }
    resp = requests.post(url, data=params, timeout=60)
    if not resp.ok:
        print(f"Instagram rejected the container request: {resp.text}")
    resp.raise_for_status()
    return resp.json()["id"]


def wait_until_processed(container_id: str, timeout_seconds: int = 300):
    token = env("IG_ACCESS_TOKEN")
    url = f"https://graph.instagram.com/{GRAPH_API_VERSION}/{container_id}"
    deadline = time.time() + timeout_seconds
    while time.time() < deadline:
        resp = requests.get(
            url,
            params={"fields": "status_code", "access_token": token},
            timeout=30,
        )
        resp.raise_for_status()
        status = resp.json().get("status_code")
        if status == "FINISHED":
            return
        if status == "ERROR":
            raise RuntimeError("Instagram failed to process the video.")
        time.sleep(10)
    raise TimeoutError("Timed out waiting for Instagram to finish processing.")


def publish_container(container_id: str) -> dict:
    ig_user_id = env("IG_USER_ID")
    token = env("IG_ACCESS_TOKEN")
    url = f"https://graph.instagram.com/{GRAPH_API_VERSION}/{ig_user_id}/media_publish"
    resp = requests.post(
        url, data={"creation_id": container_id, "access_token": token}, timeout=60
    )
    if not resp.ok:
        print(f"Instagram rejected the publish request: {resp.text}")
    resp.raise_for_status()
    return resp.json()


def move_to_posted(drive, file_id: str, posted_folder_id: str):
    file_info = drive.files().get(fileId=file_id, fields="parents").execute()
    previous_parents = ",".join(file_info.get("parents", []))
    drive.files().update(
        fileId=file_id,
        addParents=posted_folder_id,
        removeParents=previous_parents,
        fields="id, parents",
    ).execute()


def explain_empty_folder(drive, folder_id: str):
    """Called when no video was found: print WHY, so the log is enough to fix it."""
    try:
        folder = drive.files().get(fileId=folder_id, fields="id, name").execute()
    except Exception as exc:  # noqa: BLE001
        print(f"Cannot open the folder behind DRIVE_FOLDER_ID: {exc}")
        print(
            "-> Either DRIVE_FOLDER_ID is wrong (copy it from the folder's address bar),"
            " or the folder is not shared with the service account as Editor."
        )
        return

    print(f"The service account can open the folder: {folder.get('name')!r}")
    try:
        resp = (
            drive.files()
            .list(
                q=f"'{folder_id}' in parents and trashed = false",
                fields="files(name, mimeType)",
                pageSize=20,
            )
            .execute()
        )
    except Exception as exc:  # noqa: BLE001
        print(f"Could not list the folder's contents: {exc}")
        return

    files = resp.get("files", [])
    if not files:
        print(
            "-> The folder looks EMPTY to the service account. Check that the video is"
            " directly inside this folder (not in a subfolder), and that this is the"
            " folder whose ID is in DRIVE_FOLDER_ID."
        )
        return

    print(f"Files the service account can see ({len(files)}):")
    for f in files:
        print(f"   - {f['name']}  [{f['mimeType']}]")
    print(
        "-> None of these is a video file (type must start with video/). A Drive"
        " shortcut or a still-processing upload will not count."
    )


def main():
    drive_folder_id = env("DRIVE_FOLDER_ID")
    posted_folder_id = env("POSTED_FOLDER_ID")

    drive = get_drive_service()
    video = find_next_video(drive, drive_folder_id)

    if not video:
        print("No new reels waiting in the Drive folder today.")
        explain_empty_folder(drive, drive_folder_id)
        return

    caption = (video.get("description") or "").strip()
    if not caption:
        caption = pick_auto_caption(video["id"])
        if caption:
            print("No caption set on the Drive file - using an auto-picked one.")
            try:
                drive.files().update(
                    fileId=video["id"], body={"description": caption}
                ).execute()
            except Exception as exc:  # noqa: BLE001
                print(f"Warning: could not write caption back to Drive: {exc}")

    print(f"Posting: {video['name']}  (caption: {caption[:60]!r})")

    video_bytes = download_video(drive, video["id"])
    video_url, cloudinary_public_id = upload_to_cloudinary(video_bytes, video["name"])
    print("Uploaded to Cloudinary, publishing to Instagram...")

    container_id = create_ig_container(video_url, caption)
    wait_until_processed(container_id)  # Instagram has now fetched the video
    result = publish_container(container_id)

    # YouTube and Facebook are optional per account: skipped (not failed) when
    # their secrets aren't configured, so the cleanup and the move to "Posted"
    # below always run once Instagram has published.
    if has_env("YOUTUBE_REFRESH_TOKEN", "YOUTUBE_CLIENT_ID", "YOUTUBE_CLIENT_SECRET"):
        try:
            upload_to_youtube(video_bytes, caption)
        except Exception as exc:  # noqa: BLE001 - don't let YouTube issues block the rest
            print(f"Warning: YouTube upload failed: {exc}")
    else:
        print("YouTube secrets not set for this account - skipping YouTube.")

    if has_env("FB_PAGE_ID", "FB_PAGE_ACCESS_TOKEN"):
        try:
            post_to_facebook_page(video_url, caption)
        except Exception as exc:  # noqa: BLE001 - don't let Facebook issues block the rest
            print(f"Warning: Facebook Page upload failed: {exc}")
    else:
        print("Facebook Page secrets not set for this account - skipping Facebook.")

    delete_from_cloudinary(cloudinary_public_id)
    move_to_posted(drive, video["id"], posted_folder_id)
    print(f"Done. Instagram media id: {result.get('id')}")


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:  # noqa: BLE001 - surface any failure clearly in Actions logs
        print(f"FAILED: {exc}", file=sys.stderr)
        sys.exit(1)
