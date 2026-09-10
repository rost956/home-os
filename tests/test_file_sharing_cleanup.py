import os
from datetime import timedelta
from io import BytesIO

import pytest

import app.main as main_module
import app.services.file_sharing as sharing
from app.models import TemporaryFileTransfer, TemporarySharedFile
from app.services.file_sharing import cleanup_all, cleanup_expired_transfers, storage_path
from app.timezone import now_utc
from app.web import SHARED_FILES_DIR


def add_transfer(db, user, *, expires_at, name="item.txt", content=b"content"):
    transfer = TemporaryFileTransfer(owner_id=user.id, title="Cleanup", public_token=os.urandom(32).hex(), expires_at=expires_at)
    db.add(transfer)
    db.flush()
    shared_file = TemporarySharedFile(transfer_id=transfer.id, original_filename=name, storage_key=os.urandom(16).hex(), size_bytes=len(content))
    db.add(shared_file)
    db.commit()
    path = storage_path(SHARED_FILES_DIR, transfer.id, shared_file.storage_key)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(content)
    return transfer, shared_file, path


def test_cleanup_removes_expired_at_boundary_and_keeps_active(db, make_user):
    user = make_user("cleanup-owner")
    now = now_utc()
    expired, expired_file, expired_path = add_transfer(db, user, expires_at=now, content=b"1234")
    active, _, active_path = add_transfer(db, user, expires_at=now + timedelta(hours=1))

    summary = cleanup_expired_transfers(db, root=SHARED_FILES_DIR, now=now)

    assert summary.transfers_removed == summary.files_removed == 1
    assert summary.bytes_freed == 4
    assert db.get(TemporaryFileTransfer, expired.id) is None
    assert db.get(TemporarySharedFile, expired_file.id) is None
    assert not expired_path.parent.exists()
    assert db.get(TemporaryFileTransfer, active.id) is not None and active_path.exists()
    assert cleanup_expired_transfers(db, root=SHARED_FILES_DIR, now=now).transfers_removed == 0


def test_cleanup_missing_paths_and_failure_are_safe_retryable(db, make_user, monkeypatch):
    user = make_user("cleanup-retry")
    transfer, _, path = add_transfer(db, user, expires_at=now_utc() - timedelta(hours=1))
    path.unlink()
    assert cleanup_expired_transfers(db, root=SHARED_FILES_DIR, now=now_utc()).transfers_removed == 1

    retry, _, retry_path = add_transfer(db, user, expires_at=now_utc() - timedelta(hours=1))
    original = sharing.remove_transfer_storage
    monkeypatch.setattr(sharing, "remove_transfer_storage", lambda *_args: (_ for _ in ()).throw(OSError("denied")))
    failed = cleanup_expired_transfers(db, root=SHARED_FILES_DIR, now=now_utc())
    assert failed.failures == 1 and db.get(TemporaryFileTransfer, retry.id) is not None and retry_path.exists()
    monkeypatch.setattr(sharing, "remove_transfer_storage", original)
    assert cleanup_expired_transfers(db, root=SHARED_FILES_DIR, now=now_utc()).transfers_removed == 1


def test_orphan_cleanup_obeys_grace_and_never_follows_symlinks(db, make_user, tmp_path):
    now = now_utc()
    old = SHARED_FILES_DIR / "99991"
    fresh = SHARED_FILES_DIR / "99992"
    old.mkdir(parents=True, exist_ok=True)
    fresh.mkdir(parents=True, exist_ok=True)
    old_time = (now - timedelta(hours=25)).timestamp()
    os.utime(old, (old_time, old_time))
    cleanup = cleanup_all(db, root=SHARED_FILES_DIR, now=now, orphan_grace=timedelta(hours=24))
    assert cleanup.orphans_removed == 1 and not old.exists() and fresh.exists()
    outside = tmp_path / "outside.txt"
    outside.write_text("keep")
    link = SHARED_FILES_DIR / "99993"
    try:
        link.symlink_to(outside)
    except OSError:
        pytest.skip("Symlinks are unavailable in this test environment")
    os.utime(link, (old_time, old_time), follow_symlinks=False)
    cleanup_all(db, root=SHARED_FILES_DIR, now=now, orphan_grace=timedelta(hours=24))
    assert outside.read_text() == "keep" and not link.exists()


def test_owner_cleanup_extend_rotate_and_storage_summary(client, db, make_user, login):
    alice = make_user("actions-alice")
    bob = make_user("actions-bob")
    login(alice.username)
    response = client.post("/files/new", data={"title": "Alice", "ttl": "24h"}, files=[("files", ("one.txt", BytesIO(b"one"), "text/plain"))], follow_redirects=False)
    assert response.status_code == 303
    alice_transfer = db.query(TemporaryFileTransfer).filter_by(owner_id=alice.id).one()
    old_token = alice_transfer.public_token
    alice_transfer.expires_at = now_utc() - timedelta(minutes=1)
    db.commit()
    assert client.post(f"/files/{alice_transfer.id}/extend", data={"ttl": "1h"}, follow_redirects=False).status_code == 303
    db.expire_all()
    assert db.get(TemporaryFileTransfer, alice_transfer.id).expires_at > now_utc()
    assert client.post(f"/files/{alice_transfer.id}/rotate-link", follow_redirects=False).status_code == 303
    db.expire_all()
    fresh = db.get(TemporaryFileTransfer, alice_transfer.id)
    assert fresh.public_token != old_token
    assert client.get(f"/share/{old_token}").status_code == 404
    assert client.get(f"/share/{fresh.public_token}").status_code == 200
    page = client.get("/files")
    assert "Хранилище" in page.text and "Активные: 1" in page.text

    login(bob.username)
    bob_transfer, _, bob_path = add_transfer(db, bob, expires_at=now_utc() - timedelta(hours=1))
    client.post("/logout")
    login(alice.username)
    client.post("/files/cleanup-expired", follow_redirects=False)
    assert db.get(TemporaryFileTransfer, bob_transfer.id) is not None and bob_path.exists()
    assert client.post(f"/files/{bob_transfer.id}/extend", data={"ttl": "1h"}).status_code == 404


def test_global_storage_cap_rejects_overflow(client, db, make_user, login, monkeypatch):
    user = make_user("cap-owner")
    login(user.username)
    constrained = main_module.settings.__class__(**{**main_module.settings.__dict__, "file_share_max_storage_bytes": 2, "file_share_min_free_bytes": 0})
    monkeypatch.setattr(main_module, "settings", constrained)
    response = client.post("/files/new", data={"title": "No room", "ttl": "24h"}, files=[("files", ("large.txt", BytesIO(b"123"), "text/plain"))])
    assert response.status_code == 200
    assert db.query(TemporaryFileTransfer).count() == 0


def test_expired_cleanup_removes_derived_preview_directory(client, db, make_user, login):
    from PIL import Image

    output = BytesIO()
    Image.new("RGB", (80, 60), "green").save(output, format="JPEG")
    user = make_user("preview-cleanup")
    login(user.username)
    client.post("/files/new", data={"title": "Preview", "ttl": "24h"}, files=[("files", ("photo.jpg", BytesIO(output.getvalue()), "image/jpeg"))])
    transfer = db.query(TemporaryFileTransfer).one()
    shared_file = transfer.files[0]
    client.post("/logout")
    assert client.get(f"/share/{transfer.public_token}/files/{shared_file.id}/preview").status_code == 200
    preview_dir = SHARED_FILES_DIR / str(transfer.id) / "previews"
    transfer.expires_at = now_utc() - timedelta(seconds=1)
    db.commit()

    assert cleanup_expired_transfers(db, root=SHARED_FILES_DIR, now=now_utc()).transfers_removed == 1
    assert not preview_dir.exists()
