from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from command_center import attachments, auth, db
from command_center.assistant import ingest as assistant_ingest
from command_center.setup_wizard import status as setup_status

PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 64
JPEG = b"\xff\xd8\xff\xe0" + b"\x00" * 64
GIF = b"GIF89a" + b"\x00" * 64
WEBP = b"RIFF\x24\x00\x00\x00WEBPVP8 " + b"\x00" * 64
SVG = b'<svg xmlns="http://www.w3.org/2000/svg"><script>alert(1)</script></svg>'


@pytest.fixture()
def photo_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    target = tmp_path / "attachments"
    monkeypatch.setattr(attachments, "ATTACHMENTS_DIR", target)
    return target


@pytest.fixture()
def client(tmp_path: Path, photo_dir: Path, monkeypatch: pytest.MonkeyPatch) -> TestClient:
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "test.db")
    monkeypatch.setattr(auth, "has_valid_credentials", lambda: False)
    monkeypatch.setattr(setup_status, "is_setup_complete", lambda: True)
    monkeypatch.setattr(assistant_ingest, "rebuild_index", lambda *a, **k: False)
    from command_center.app import app

    with TestClient(app) as test_client:
        yield test_client


def _new_item(client: TestClient) -> int:
    response = client.post("/items", json={"lane": "urgent", "title": "Photo task"})
    return response.json()["id"]


def _upload(client: TestClient, item_id: int, content: bytes, name: str = "shot.png"):
    return client.post(f"/items/{item_id}/attachments", files={"file": (name, content, "image/png")})


# --- attachments.py ----------------------------------------------------------


@pytest.mark.parametrize(
    ("content", "expected"),
    [(PNG, ("png", "image/png")), (JPEG, ("jpg", "image/jpeg")), (GIF, ("gif", "image/gif")), (WEBP, ("webp", "image/webp"))],
)
def test_sniff_image_type_recognises_browser_formats(content: bytes, expected: tuple[str, str]) -> None:
    assert attachments.sniff_image_type(content) == expected


def test_sniff_image_type_rejects_svg_and_text() -> None:
    assert attachments.sniff_image_type(SVG) is None
    assert attachments.sniff_image_type(b"just some text") is None


def test_save_upload_stores_under_a_random_name(photo_dir: Path) -> None:
    stored_name, content_type, size = attachments.save_upload(PNG)

    assert content_type == "image/png"
    assert size == len(PNG)
    assert attachments.path_for(stored_name) == photo_dir / stored_name
    assert (photo_dir / stored_name).read_bytes() == PNG


def test_save_upload_rejects_empty_oversized_and_non_images(photo_dir: Path) -> None:
    with pytest.raises(attachments.AttachmentError):
        attachments.save_upload(b"")
    with pytest.raises(attachments.AttachmentError):
        attachments.save_upload(b"\x89PNG\r\n\x1a\n" + b"\x00" * attachments.MAX_UPLOAD_BYTES)
    with pytest.raises(attachments.AttachmentError):
        attachments.save_upload(SVG)
    assert not photo_dir.exists() or not any(photo_dir.iterdir())


def test_path_for_refuses_anything_save_upload_would_not_generate() -> None:
    assert attachments.path_for("../../secrets/google_token.json") is None
    assert attachments.path_for("..\\db.sqlite") is None
    assert attachments.path_for("abc.png") is None
    assert attachments.path_for("0" * 32 + ".svg") is None
    assert attachments.path_for("0" * 32 + ".png") is not None


# --- routes ------------------------------------------------------------------


def test_upload_list_serve_and_delete_round_trip(client: TestClient, photo_dir: Path) -> None:
    item_id = _new_item(client)

    uploaded = _upload(client, item_id, PNG, "receipt.png")
    assert uploaded.status_code == 200
    photo = uploaded.json()
    assert photo["original_name"] == "receipt.png"

    listed = client.get(f"/items/{item_id}/attachments").json()
    assert [p["id"] for p in listed] == [photo["id"]]

    served = client.get(photo["url"])
    assert served.status_code == 200
    assert served.content == PNG
    assert served.headers["content-type"] == "image/png"
    assert served.headers["x-content-type-options"] == "nosniff"

    assert client.delete(photo["url"]).status_code == 200
    assert client.get(photo["url"]).status_code == 404
    assert client.get(f"/items/{item_id}/attachments").json() == []
    assert not any(photo_dir.iterdir())


def test_served_type_comes_from_the_bytes_not_the_upload(client: TestClient) -> None:
    item_id = _new_item(client)
    # Declared as PNG and named .png, but the bytes are a JPEG.
    photo = _upload(client, item_id, JPEG, "lying.png").json()
    assert client.get(photo["url"]).headers["content-type"] == "image/jpeg"


def test_upload_rejects_svg_with_a_readable_message(client: TestClient) -> None:
    item_id = _new_item(client)
    response = _upload(client, item_id, SVG, "evil.svg")
    assert response.status_code == 400
    assert "JPEG, PNG, GIF or WebP" in response.json()["detail"]


def test_upload_to_a_missing_item_is_404(client: TestClient) -> None:
    assert _upload(client, 999999, PNG).status_code == 404


def test_deleting_an_unknown_photo_is_404(client: TestClient) -> None:
    assert client.delete("/attachments/999999").status_code == 404


def test_brief_card_shows_the_photo_count(client: TestClient) -> None:
    item_id = _new_item(client)
    _upload(client, item_id, PNG)
    _upload(client, item_id, GIF, "b.gif")

    response = client.get("/brief")

    assert "photos: 2" in response.text
    assert "attachments-changed" in response.text


def test_brief_includes_voice_dictation_controls(client: TestClient) -> None:
    response = client.get("/brief")
    assert "toggleVoice()" in response.text
    assert "interimResults = true" in response.text
