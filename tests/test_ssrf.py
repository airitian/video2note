"""SSRF 校验：只用 IP 字面量与保留主机名，全程不发真实网络请求。"""
import pytest

from core.ssrf import validate_public_url


@pytest.mark.parametrize("url", [
    "http://127.0.0.1/admin",
    "http://127.0.0.1:8765/api/tasks",
    "http://169.254.169.254/latest/meta-data/",   # 云元数据
    "http://localhost:8765/",
    "http://10.0.0.5/video",
    "http://172.16.0.9/x",
    "http://192.168.1.1/x",
    "http://[::1]/x",
    "http://0.0.0.0/x",
    "ftp://example.com/file",                      # 非 http/https
    "file:///etc/passwd",
    "not-a-url",
    "",
])
def test_rejects_non_public(url):
    with pytest.raises(ValueError):
        validate_public_url(url)


@pytest.mark.parametrize("url", [
    "http://8.8.8.8/video.mp4",
    "https://1.1.1.1/x",
])
def test_accepts_public_ip_literal(url):
    assert validate_public_url(url) == url


def test_env_bypass(monkeypatch):
    monkeypatch.setenv("V2N_ALLOW_PRIVATE_URLS", "1")
    assert validate_public_url("http://127.0.0.1:8765/x")
