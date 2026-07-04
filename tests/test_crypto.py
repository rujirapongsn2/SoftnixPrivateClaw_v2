from claw.db.stores import ConnectorStore
from claw.security.crypto import SecretBox


def test_encrypt_decrypt_roundtrip():
    box = SecretBox("my-secret-key")
    token = box.encrypt("super-secret-value")
    assert token != "super-secret-value"
    assert token.startswith("enc::")
    assert box.decrypt(token) == "super-secret-value"


def test_plaintext_passthrough_on_decrypt():
    box = SecretBox("k")
    # Legacy plaintext (no prefix) is returned unchanged.
    assert box.decrypt("plain-value") == "plain-value"


def test_empty_values_untouched():
    box = SecretBox("k")
    assert box.encrypt("") == ""
    assert box.decrypt("") == ""


def test_wrong_key_cannot_decrypt():
    token = SecretBox("key-a").encrypt("secret")
    # Different key → decrypt fails gracefully, returns the ciphertext (not the secret).
    result = SecretBox("key-b").decrypt(token)
    assert result != "secret"


def test_map_helpers():
    box = SecretBox("k")
    enc = box.encrypt_map({"TOKEN": "abc", "URL": "https://x"})
    assert all(v.startswith("enc::") for v in enc.values())
    assert box.decrypt_map(enc) == {"TOKEN": "abc", "URL": "https://x"}


async def test_connector_env_encrypted_at_rest(db_factory, stores):
    box = SecretBox("deploy-secret")
    store = ConnectorStore(db_factory, secret_box=box)
    user = await stores["users"].get_or_create_by_email("c@x.y")

    await store.upsert(
        user.id, "gh", transport="stdio", command="run", env={"TOKEN": "ghp_secret123"}, enabled=True
    )

    # Reading through the store returns plaintext (decrypted for use).
    rows = await store.list_for_user(user.id)
    assert rows[0].env["TOKEN"] == "ghp_secret123"

    # But a store WITHOUT the box (simulating raw DB access) sees ciphertext only.
    raw_store = ConnectorStore(db_factory)
    raw = await raw_store.list_for_user(user.id)
    assert raw[0].env["TOKEN"].startswith("enc::")
    assert "ghp_secret123" not in raw[0].env["TOKEN"]
