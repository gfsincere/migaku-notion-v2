"""Media uploads for card creation: PUT file-sync-worker-api.migaku.com/data/SRSMEDIA/<name>.

Used when v2 (or the sibling preply-migaku project) wants to attach an
image or audio file to a Migaku card. Per the HAR (entries #16, #17, #18),
the flow is:

    1. PUT https://file-sync-worker-api.migaku.com/data/SRSMEDIA/<urlencoded-filename>
        Authorization: Bearer <firebase id_token>
        Content-Type: application/octet-stream
        body: raw bytes (webp / m4a / png / ...)

    2. Server responds 200 with:
        {"filePath": "<userId>/SRSMEDIA/<uuid>_<filename>"}

    3. The returned `filePath` is what you reference in subsequent
       /push/enqueue payloads (cards[i].image, cards[i].audio).

The filename in the URL needs URL-encoding (e.g. CJK characters and
spaces). `urllib.parse.quote(filename, safe='')` is correct.

This is critical for the preply-migaku integration: tutoring sessions
record audio of the teacher pronouncing each new word, and that audio
attaches to the Migaku card. v2 owns the upload step.
"""
from __future__ import annotations

from pathlib import Path
from urllib.parse import quote

import requests

from . import auth, const  # noqa: F401


def upload_srs_media(
    token: auth.FirebaseAuthToken,
    filename: str,
    data: bytes,
    *,
    content_type: str = "application/octet-stream",
) -> str:
    """Upload `data` as SRSMEDIA/<filename>. Returns the server-side filePath.

    Implementation outline:
        token = auth.ensure_fresh(token)
        url = f"{const.FILE_SYNC_DATA_PREFIX}/{quote(filename, safe='')}"
        resp = requests.put(
            url,
            data=data,
            headers={
                "Authorization": f"Bearer {token.id_token}",
                "Content-Type": content_type,
            },
            timeout=120,
        )
        resp.raise_for_status()
        return resp.json()["filePath"]
    """
    token = auth.ensure_fresh(token)
    url = f"{const.FILE_SYNC_DATA_PREFIX}/{quote(filename, safe='')}"
    resp = requests.put(
        url,
        data=data,
        headers={
            "Authorization": f"Bearer {token.id_token}",
            "Content-Type": content_type,
        },
        timeout=120,
    )
    if not resp.ok:
        raise RuntimeError(
            f"SRSMEDIA upload failed ({resp.status_code}) for {filename}: {resp.text[:500]}"
        )
    payload = resp.json()
    fp = payload.get("filePath")
    if not fp:
        raise RuntimeError(f"SRSMEDIA upload missing filePath for {filename}")
    return str(fp)


def upload_srs_media_file(
    token: auth.FirebaseAuthToken,
    path: Path,
    *,
    content_type: str | None = None,
) -> str:
    """Convenience: read `path`, infer content-type from suffix, upload.

    Implementation outline:
        if content_type is None:
            content_type = {
                ".webp": "image/webp",
                ".png":  "image/png",
                ".jpg":  "image/jpeg",
                ".jpeg": "image/jpeg",
                ".m4a":  "audio/mp4",
                ".mp3":  "audio/mpeg",
                ".ogg":  "audio/ogg",
            }.get(path.suffix.lower(), "application/octet-stream")
        return upload_srs_media(
            token, filename=path.name, data=path.read_bytes(),
            content_type=content_type,
        )
    """
    guessed = content_type
    if guessed is None:
        guessed = {
            ".webp": "image/webp",
            ".png": "image/png",
            ".jpg": "image/jpeg",
            ".jpeg": "image/jpeg",
            ".m4a": "audio/mp4",
            ".mp3": "audio/mpeg",
            ".ogg": "audio/ogg",
        }.get(path.suffix.lower(), "application/octet-stream")
    return upload_srs_media(
        token,
        filename=path.name,
        data=path.read_bytes(),
        content_type=guessed,
    )


__all__ = ["upload_srs_media", "upload_srs_media_file", "quote"]
