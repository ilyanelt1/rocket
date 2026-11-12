def test_history_supports_htmx_partial_and_full_page(auth_client):
    client, _ = auth_client
    login = client.post(
        "/login",
        data={"username": "admin", "password": "admin-password-123"},
        follow_redirects=False,
    )
    assert login.status_code == 303

    full_page = client.get("/history")
    assert full_page.status_code == 200
    assert "<html" in full_page.text
    assert "htmx.org" in full_page.text
    assert 'hx-get="/history"' in full_page.text

    partial = client.get("/history", headers={"HX-Request": "true"})
    assert partial.status_code == 200
    assert 'id="history-results"' in partial.text
    assert "<html" not in partial.text
