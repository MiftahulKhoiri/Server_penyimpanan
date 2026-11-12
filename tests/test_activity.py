from __future__ import annotations

from tests.conftest import DEFAULT_PASSWORD, auth_header, create_user_direct, login, upload_simple


def test_my_activity_only_mine_with_filters(user_client):
    c, h, uid = user_client
    fid = upload_simple(c, h, "a.txt", b"x").json()["id"]
    c.get(f"/api/v1/files/{fid}/download", headers=h)
    c.patch(f"/api/v1/files/{fid}/rename", headers=h, json={"name": "b.txt"})
    c.delete(f"/api/v1/files/{fid}", headers=h)
    c.post(f"/api/v1/trash/{fid}/restore", headers=h)
    create_user_direct(c, "siti")
    login(c, "siti")

    body = c.get("/api/v1/activity", headers=h).json()
    actions = [i["action"] for i in body["items"]]
    assert {"auth.login", "file.upload", "file.download", "file.rename", "file.delete", "trash.restore"} <= set(actions)
    assert all(i["user_id"] == uid for i in body["items"])
    files_only = c.get("/api/v1/activity", headers=h, params={"action": "file."}).json()
    assert {i["action"] for i in files_only["items"]} == {"file.upload", "file.download", "file.rename", "file.delete"}
    exact = c.get("/api/v1/activity", headers=h, params={"action": "file.rename"}).json()
    assert exact["total"] == 1 and exact["items"][0]["details"] == {"old_name": "a.txt", "new_name": "b.txt"}
    page = c.get("/api/v1/activity", headers=h, params={"page_size": 2}).json()
    assert len(page["items"]) == 2 and page["total"] == body["total"]
    assert c.get("/api/v1/activity").status_code == 401


def test_admin_activity(user_client):
    c, h, uid = user_client
    fresh = c.post("/api/v1/auth/change-password", headers=h,
                   json={"current_password": DEFAULT_PASSWORD, "new_password": "PasswordBaru99"}).json()
    h = auth_header(fresh)  # token lama sengaja dicabut saat ganti password
    assert c.get("/api/v1/admin/activity", headers=h).status_code == 403
    create_user_direct(c, "admin", role="admin")
    ha = auth_header(login(c, "admin"))
    everything = c.get("/api/v1/admin/activity", headers=ha, params={"page_size": 200}).json()
    actions = {i["action"] for i in everything["items"]}
    assert {"system.start", "auth.password_change", "auth.login"} <= actions
    system = [i for i in everything["items"] if i["action"] == "system.start"]
    assert system[0]["user_id"] is None and "version" in system[0]["details"]
    budi = c.get("/api/v1/admin/activity", headers=ha, params={"user_id": uid}).json()
    assert budi["total"] >= 2 and all(i["user_id"] == uid for i in budi["items"])
    future = c.get("/api/v1/admin/activity", headers=ha, params={"since": "2100-01-01T00:00:00Z"}).json()
    assert future["total"] == 0
    assert "PasswordBaru99" not in str(everything) and DEFAULT_PASSWORD not in str(everything)
