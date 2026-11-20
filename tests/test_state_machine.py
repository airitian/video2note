"""状态机纯逻辑：删除墓碑、整理失败分流、本地 TTL 清理。"""
import time

from core import pipeline, qiniu_archive, store
from core.config import TASK_DIR


def test_delete_tombstone_blocks_resurrection():
    t = store.create("https://example.com/a", "douyin", {})
    store.save(t)
    path = TASK_DIR / f"{t.id}.json"
    assert path.exists()
    store.remove(t.id)
    assert store.is_deleted(t.id) is True
    assert store.get(t.id) is None
    assert not path.exists()
    # worker 手里还攥着旧对象继续 save：墓碑必须拦住，不许复活
    store.save(t)
    assert not path.exists()
    assert store.get(t.id) is None


def test_polish_failure_sets_failed_stage(monkeypatch):
    t = store.create("https://example.com/b", "douyin", {})
    t.transcript = "已有转写"

    def boom(task, style, emit=None, force=False):
        raise RuntimeError("LLM 挂了")

    monkeypatch.setattr(pipeline, "_polish_body", boom)
    assert pipeline._catch_polish(t, "detailed") is False
    assert t.failed_stage == "polishing"
    assert t.status == "transcribed"


def test_retry_routes_polish_only(monkeypatch):
    t = store.create("https://example.com/c", "douyin", {})
    t.transcript = "已有转写"
    t.status = "transcribed"
    t.failed_stage = "polishing"
    calls = []
    monkeypatch.setattr(
        pipeline, "submit_polish",
        lambda task, style, force=False: calls.append(force))
    assert pipeline.retry(t) == "polish"
    assert calls == [True]


def test_local_ttl_cleanup(monkeypatch, tmp_path):
    from core.config import MEDIA_DIR
    t = store.create("https://example.com/d", "douyin", {})
    t.status = "done"
    t.updated_at = time.time() - 30 * 86400      # 30 天前，TTL 默认 7
    store.save(t)
    mdir = MEDIA_DIR / t.id
    mdir.mkdir(parents=True, exist_ok=True)
    (mdir / "video.mp4").write_bytes(b"x" * 100)
    (mdir / "audio.mp3").write_bytes(b"y" * 50)
    s = qiniu_archive.local_ttl_cleanup()
    assert s["removed"] == 2 and s["freed"] == 150
    assert not (mdir / "video.mp4").exists()
    # 任务记录保留
    assert store.get(t.id) is not None


def test_local_ttl_keeps_fresh_and_running(monkeypatch):
    from core.config import MEDIA_DIR
    t = store.create("https://example.com/e", "douyin", {})
    t.status = "running"                          # 非终态不动
    t.updated_at = time.time() - 30 * 86400
    store.save(t)
    mdir = MEDIA_DIR / t.id
    mdir.mkdir(parents=True, exist_ok=True)
    (mdir / "video.mp4").write_bytes(b"x" * 10)
    s = qiniu_archive.local_ttl_cleanup()
    assert (mdir / "video.mp4").exists()
