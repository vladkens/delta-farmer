from apps.omni import OmniConfig
from lib import crypto


def test_encrypted_captcha_key_is_decrypted(monkeypatch):
    # `config encrypt` encrypts captcha_key; the solver must receive the plain key.
    monkeypatch.setenv(crypto.ENV_NAME, "secret")
    monkeypatch.delattr(crypto._get_encryption_password, "cached_password", raising=False)
    cfg = OmniConfig.model_validate(
        {
            "accounts": [{"name": "main", "privkey": "0x" + "11" * 32}],
            "symbols": ["BTC"],
            "trade_duration": [1, 1],
            "trade_cooldown": [1, 1],
            "captcha_key": crypto.encrypt_value("solver-key", password="secret"),
        }
    )

    assert cfg.captcha_key.get_secret_value() == "solver-key"
