from dataclasses import replace
from datetime import timedelta
from io import BytesIO

from PIL import Image
from sqlalchemy import create_engine, inspect

import app.main as main_module
from app.database import Base
from app.models import TemporaryFileTransfer, TemporarySharedFile
from app.services.file_sharing import storage_path, transfer_is_expired
from app.timezone import now_utc
from app.web import SHARED_FILES_DIR


def upload_payload(name: str, content: bytes = b"file content"):
    return [("files", (name, BytesIO(content), "application/octet-stream"))]


def create_transfer(client, title="Photos", files=None, ttl="24h"):
    return client.post(
        "/files/new",
        data={"title": title, "description": "Temporary", "ttl": ttl},
        files=files or [],
        follow_redirects=False,
    )


def image_payload(image_format: str) -> bytes:
    output = BytesIO()
    Image.new("RGBA" if image_format == "PNG" else "RGB", (1600, 1200), "blue").save(output, format=image_format)
    return output.getvalue()


def test_create_transfer_uploads_multiple_unicode_duplicate_files_and_ttl(client, db, make_user, login):
    user = make_user("file-owner")
    login(user.username)
    before = now_utc()

    response = create_transfer(
        client,
        title="Фото с машины",
        ttl="3d",
        files=[
            ("files", ("Фото машины 10.09.2026.jpg", BytesIO(b"one"), "image/jpeg")),
            ("files", ("photo.jpg", BytesIO(b"two"), "image/jpeg")),
            ("files", ("photo.jpg", BytesIO(b"three"), "image/jpeg")),
        ],
    )

    assert response.status_code == 303
    transfer = db.query(TemporaryFileTransfer).one()
    assert transfer.owner_id == user.id
    assert transfer.public_token and len(transfer.public_token) >= 40
    assert timedelta(days=3) - timedelta(seconds=2) <= transfer.expires_at - before <= timedelta(days=3, seconds=2)
    assert [file.original_filename for file in transfer.files] == ["Фото машины 10.09.2026.jpg", "photo.jpg", "photo.jpg"]
    assert len({file.storage_key for file in transfer.files}) == 3
    assert [storage_path(SHARED_FILES_DIR, transfer.id, file.storage_key).read_bytes() for file in transfer.files] == [b"one", b"two", b"three"]


def test_public_share_is_anonymous_private_and_downloads_original_filename(client, db, make_user, login):
    user = make_user("private-owner")
    login(user.username)
    create_transfer(client, files=upload_payload("договор.pdf", b"pdf bytes"))
    transfer = db.query(TemporaryFileTransfer).one()
    shared_file = transfer.files[0]
    client.post("/logout")

    page = client.get(f"/share/{transfer.public_token}")
    download = client.get(f"/share/{transfer.public_token}/files/{shared_file.id}/download")

    assert page.status_code == 200
    assert "private-owner" not in page.text
    assert page.headers["cache-control"] == "no-store"
    assert page.headers["x-robots-tag"] == "noindex, nofollow"
    assert download.status_code == 200 and download.content == b"pdf bytes"
    assert "attachment" in download.headers["content-disposition"]
    assert client.post(f"/share/{transfer.public_token}/files/{shared_file.id}/delete").status_code in {404, 405}


def test_expiry_denies_public_access_but_owner_can_manage(client, db, make_user, login):
    user = make_user("expired-owner")
    login(user.username)
    create_transfer(client, files=upload_payload("old.txt"))
    transfer = db.query(TemporaryFileTransfer).one()
    transfer.expires_at = now_utc() - timedelta(seconds=1)
    db.commit()

    public_page = client.get(f"/share/{transfer.public_token}")
    public_download = client.get(f"/share/{transfer.public_token}/files/{transfer.files[0].id}/download")
    owner_page = client.get(f"/files/{transfer.id}")

    assert transfer_is_expired(transfer, transfer.expires_at) is True
    assert public_page.status_code == public_download.status_code == 410
    assert "Срок действия ссылки истёк" in public_page.text
    assert owner_page.status_code == 200 and "Истекла" in owner_page.text


def test_token_file_mixing_and_owner_isolation_are_rejected(client, db, make_user, login):
    alice = make_user("file-alice")
    bob = make_user("file-bob")
    login(alice.username)
    create_transfer(client, title="Alice", files=upload_payload("alice.txt"))
    alice_transfer = db.query(TemporaryFileTransfer).one()
    login(bob.username)
    create_transfer(client, title="Bob", files=upload_payload("bob.txt"))
    bob_transfer = db.query(TemporaryFileTransfer).filter_by(owner_id=bob.id).one()

    mixed = client.get(f"/share/{alice_transfer.public_token}/files/{bob_transfer.files[0].id}/download")
    forbidden = client.get(f"/files/{alice_transfer.id}")
    foreign_upload = client.post(f"/files/{alice_transfer.id}/upload", files=upload_payload("no.txt"))
    foreign_download = client.get(f"/files/{alice_transfer.id}/download/{alice_transfer.files[0].id}")
    foreign_delete = client.post(f"/files/{alice_transfer.id}/files/{alice_transfer.files[0].id}/delete")

    assert mixed.status_code == forbidden.status_code == foreign_upload.status_code == foreign_download.status_code == foreign_delete.status_code == 404


def test_delete_single_file_and_transfer_removes_storage_safely(client, db, make_user, login):
    user = make_user("file-delete")
    login(user.username)
    create_transfer(
        client,
        files=[
            ("files", ("first.txt", BytesIO(b"first"), "text/plain")),
            ("files", ("second.txt", BytesIO(b"second"), "text/plain")),
        ],
    )
    transfer = db.query(TemporaryFileTransfer).one()
    transfer_id = transfer.id
    first, second = transfer.files
    first_id = first.id
    first_path = storage_path(SHARED_FILES_DIR, transfer_id, first.storage_key)
    second_path = storage_path(SHARED_FILES_DIR, transfer_id, second.storage_key)
    first_path.unlink()

    deleted_file = client.post(f"/files/{transfer_id}/files/{first_id}/delete", follow_redirects=False)
    assert deleted_file.status_code == 303
    db.expire_all()
    assert db.get(TemporarySharedFile, first_id) is None
    assert second_path.exists()

    deleted_transfer = client.post(f"/files/{transfer_id}/delete", follow_redirects=False)
    assert deleted_transfer.status_code == 303
    assert db.get(TemporaryFileTransfer, transfer_id) is None
    assert not second_path.exists()


def test_upload_validation_prevents_path_escape_and_oversized_metadata(client, db, make_user, login, monkeypatch):
    user = make_user("file-safety")
    login(user.username)
    constrained = replace(
        main_module.settings,
        file_share_max_file_bytes=3,
        file_share_max_transfer_bytes=5,
    )
    monkeypatch.setattr(main_module, "settings", constrained)

    rejected = create_transfer(client, files=upload_payload("../../evil.txt", b"four"))
    assert rejected.status_code == 200
    assert db.query(TemporaryFileTransfer).count() == 0
    assert not (SHARED_FILES_DIR.parent / "evil.txt").exists()

    created = create_transfer(client, files=upload_payload("../../safe.txt", b"ok"))
    assert created.status_code == 303
    transfer = db.query(TemporaryFileTransfer).one()
    assert transfer.files[0].original_filename == "safe.txt"
    assert storage_path(SHARED_FILES_DIR, transfer.id, transfer.files[0].storage_key).is_relative_to(SHARED_FILES_DIR.resolve())

    batch_too_large = create_transfer(
        client,
        title="Too large total",
        files=[
            ("files", ("one.txt", BytesIO(b"abc"), "text/plain")),
            ("files", ("two.txt", BytesIO(b"def"), "text/plain")),
        ],
    )
    assert batch_too_large.status_code == 200
    assert db.query(TemporaryFileTransfer).count() == 1


def test_missing_disk_file_returns_404_without_exposing_storage(client, db, make_user, login):
    user = make_user("file-missing")
    login(user.username)
    create_transfer(client, files=upload_payload("missing.bin"))
    transfer = db.query(TemporaryFileTransfer).one()
    shared_file = transfer.files[0]
    storage_path(SHARED_FILES_DIR, transfer.id, shared_file.storage_key).unlink()
    client.post("/logout")

    response = client.get(f"/share/{transfer.public_token}/files/{shared_file.id}/download")

    assert response.status_code == 404
    assert str(SHARED_FILES_DIR) not in response.text


def test_expiry_boundaries_and_runtime_migration(tmp_path, monkeypatch):
    now = now_utc()
    transfer = TemporaryFileTransfer(owner_id=1, title="Boundary", public_token="x" * 43, expires_at=now)
    assert transfer_is_expired(transfer, now - timedelta(microseconds=1)) is False
    assert transfer_is_expired(transfer, now) is True
    assert transfer_is_expired(transfer, now + timedelta(microseconds=1)) is True

    legacy_engine = create_engine(f"sqlite:///{(tmp_path / 'legacy.db').as_posix()}")
    Base.metadata.create_all(bind=legacy_engine)
    TemporarySharedFile.__table__.drop(bind=legacy_engine)
    TemporaryFileTransfer.__table__.drop(bind=legacy_engine)
    monkeypatch.setattr(main_module, "engine", legacy_engine)

    assert main_module.schema_change_required() is True
    main_module.ensure_runtime_schema()
    assert {"temporary_file_transfers", "temporary_shared_files"} <= set(inspect(legacy_engine).get_table_names())


def test_public_image_previews_validate_content_cache_and_preserve_original(client, db, make_user, login):
    user = make_user("preview-owner")
    login(user.username)
    jpeg = image_payload("JPEG")
    png = image_payload("PNG")
    created = create_transfer(client, files=[("files", ("photo.jpg", BytesIO(jpeg), "application/octet-stream")), ("files", ("drawing.png", BytesIO(png), "application/octet-stream")), ("files", ("evil.jpg", BytesIO(b"not an image"), "image/jpeg")), ("files", ("manual.pdf", BytesIO(b"%PDF-1.7"), "application/pdf"))])
    assert created.status_code == 303
    transfer = db.query(TemporaryFileTransfer).one()
    jpeg_file, png_file, evil_file, pdf_file = transfer.files
    original = storage_path(SHARED_FILES_DIR, transfer.id, jpeg_file.storage_key).read_bytes()
    client.post("/logout")

    page = client.get(f"/share/{transfer.public_token}")
    jpeg_preview = client.get(f"/share/{transfer.public_token}/files/{jpeg_file.id}/preview")
    cached_preview = client.get(f"/share/{transfer.public_token}/files/{jpeg_file.id}/preview")
    png_preview = client.get(f"/share/{transfer.public_token}/files/{png_file.id}/preview")
    evil_preview = client.get(f"/share/{transfer.public_token}/files/{evil_file.id}/preview")
    pdf_preview = client.get(f"/share/{transfer.public_token}/files/{pdf_file.id}/preview")

    assert page.status_code == 200 and "share-gallery" in page.text and "share-file-card" in page.text
    assert f'/files/{jpeg_file.id}/preview' in page.text and f'/files/{png_file.id}/preview' in page.text
    assert f'/files/{evil_file.id}/preview' not in page.text and f'/files/{pdf_file.id}/preview' not in page.text
    assert jpeg_preview.status_code == cached_preview.status_code == png_preview.status_code == 200
    assert jpeg_preview.headers["cache-control"] == "no-store" and jpeg_preview.headers["content-type"].startswith("image/jpeg")
    assert evil_preview.status_code == pdf_preview.status_code == 404
    assert storage_path(SHARED_FILES_DIR, transfer.id, jpeg_file.storage_key).read_bytes() == original
    preview_path = SHARED_FILES_DIR / str(transfer.id) / "previews" / f"{jpeg_file.storage_key}.jpg"
    assert preview_path.is_file()


def test_public_preview_enforces_token_expiry_paths_cleanup_and_https_share_url(client, db, make_user, login):
    alice = make_user("preview-alice")
    bob = make_user("preview-bob")
    login(alice.username)
    create_transfer(client, title="Alice", files=[("files", ("photo.jpg", BytesIO(image_payload("JPEG")), "image/jpeg"))])
    alice_transfer = db.query(TemporaryFileTransfer).filter_by(owner_id=alice.id).one()
    alice_file = alice_transfer.files[0]
    assert client.get(f"/files/{alice_transfer.id}", headers={"x-forwarded-proto": "https"}).text.find(f"https://testserver/share/{alice_transfer.public_token}") >= 0
    client.post("/logout")
    login(bob.username)
    create_transfer(client, title="Bob", files=[("files", ("photo.jpg", BytesIO(image_payload("JPEG")), "image/jpeg"))])
    bob_transfer = db.query(TemporaryFileTransfer).filter_by(owner_id=bob.id).one()
    client.post("/logout")

    assert client.get(f"/share/{alice_transfer.public_token}/files/{bob_transfer.files[0].id}/preview").status_code == 404
    assert client.get(f"/share/{alice_transfer.public_token}/files/{alice_file.id}/preview").status_code == 200
    preview_dir = SHARED_FILES_DIR / str(alice_transfer.id) / "previews"
    login(alice.username)
    assert client.post(f"/files/{alice_transfer.id}/delete", follow_redirects=False).status_code == 303
    assert not preview_dir.exists()

    create_transfer(client, title="Expired", files=[("files", ("photo.jpg", BytesIO(image_payload("JPEG")), "image/jpeg"))])
    expired = db.query(TemporaryFileTransfer).filter_by(owner_id=alice.id).one()
    expired.expires_at = now_utc() - timedelta(seconds=1)
    db.commit()
    client.post("/logout")
    assert client.get(f"/share/{expired.public_token}/files/{expired.files[0].id}/preview").status_code == 410
    assert str(SHARED_FILES_DIR) not in client.get(f"/share/{expired.public_token}/files/{expired.files[0].id}/preview").text
