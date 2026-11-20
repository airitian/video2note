"""配置健壮性：数值钳制、保存校验、原子写权限、无令牌时 is_admin。"""
import json
import os
import stat

import pytest

from core import config


def test_get_int_clamped():
    g = config.get_int_clamped
    assert g({"chunk_seconds": "9999"}, "chunk_seconds", 180, 10, 600) == 600
    assert g({"chunk_seconds": "3"}, "chunk_seconds", 180, 10, 600) == 10
    assert g({"chunk_seconds": "abc"}, "chunk_seconds", 180, 10, 600) == 180
    assert g({}, "chunk_seconds", 180, 10, 600) == 180
    assert g({"max_concurrency": "0"}, "max_concurrency", 4, 1, 8) == 1
    assert g({"max_concurrency": 99}, "max_concurrency", 4, 1, 8) == 8
    assert g({"max_concurrency": None}, "max_concurrency", 4, 1, 8) == 4


def test_save_settings_rejects_bad_numbers():
    with pytest.raises(ValueError):
        config.save_settings({"chunk_seconds": "abc"})
    with pytest.raises(ValueError):
        config.save_settings({"chunk_seconds": 99999})
    with pytest.raises(ValueError):
        config.save_settings({"max_concurrency": "x"})


def test_save_settings_accepts_good_numbers():
    config.save_settings({"chunk_seconds": "300"})
    saved = json.loads(config.SETTINGS_FILE.read_text(encoding="utf-8"))
    assert saved["chunk_seconds"] == "300"
    # settings.json 含密钥，必须 0600
    assert stat.S_IMODE(os.stat(config.SETTINGS_FILE).st_mode) == 0o600


def test_atomic_write_content_and_mode(tmp_path):
    p = tmp_path / "secret.json"
    config._write_json(p, {"k": "v"}, private=True)
    assert json.loads(p.read_text(encoding="utf-8")) == {"k": "v"}
    assert stat.S_IMODE(os.stat(p).st_mode) == 0o600

    p2 = tmp_path / "note.txt"
    config.write_text_atomic(p2, "hello", 0o600)
    assert p2.read_text(encoding="utf-8") == "hello"
    assert stat.S_IMODE(os.stat(p2).st_mode) == 0o600


def test_is_admin_no_token_is_false():
    # conftest 预置了管理员令牌：对的令牌 True，其余一律 False
    assert config.is_admin("test-admin-token") is True
    assert config.is_admin("") is False
    assert config.is_admin("wrong-token") is False


def test_startup_guard(monkeypatch):
    # 有令牌（conftest 的管理员令牌）：非回环也放行
    assert config.startup_guard_error("0.0.0.0") is None
    # 无任何令牌：非回环拒绝、回环放行（仅警告）
    monkeypatch.setattr(config, "all_valid_tokens", lambda: [])
    err = config.startup_guard_error("0.0.0.0")
    assert err and "拒绝启动" in err
    assert config.startup_guard_error("127.0.0.1") is None
    assert config.startup_guard_error("localhost") is None
