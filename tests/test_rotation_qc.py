"""模型轮换 + 生成后质检（2026-11-22）。

全部用假探活/假评审驱动，不打真实接口。
"""
import json

from core import config, llm, pipeline, store


def _set_rotation(models: str):
    f = config.SETTINGS_FILE
    f.write_text(json.dumps({"llm_rotation_models": models}), encoding="utf-8")


def _cleanup_settings():
    config.SETTINGS_FILE.unlink(missing_ok=True)


# ---------- 名单解析 ----------

def test_rotation_models_parsing():
    _set_rotation("a/A, b/B\nc/C,,a/A")
    try:
        assert pipeline.rotation_models() == ["a/A", "b/B", "c/C"]
    finally:
        _cleanup_settings()


def test_rotation_models_empty():
    _set_rotation("")
    try:
        assert pipeline.rotation_models() == []
    finally:
        _cleanup_settings()


# ---------- 计数落盘轮转 ----------

def test_rotation_index_cycles(tmp_path, monkeypatch):
    monkeypatch.setattr(pipeline, "TASK_DIR", tmp_path / "tasks")
    assert [pipeline._rotation_next_index(3) for _ in range(5)] == [0, 1, 2, 0, 1]


# ---------- 分配：探活跳过 ----------

def test_assign_skips_dead_model(monkeypatch):
    _set_rotation("m/A,m/B,m/C")
    monkeypatch.setattr(pipeline, "_rotation_next_index", lambda n: 0)
    monkeypatch.setattr(pipeline, "_probe_model", lambda m: m != "m/A")
    t = store.Task(id="rot1")
    try:
        got = pipeline.assign_rotation_model(t)
        assert got == "m/B"
        assert t.options["llm_model"] == "m/B"
    finally:
        _cleanup_settings()


def test_assign_all_dead_falls_back(monkeypatch):
    _set_rotation("m/A,m/B")
    monkeypatch.setattr(pipeline, "_rotation_next_index", lambda n: 0)
    monkeypatch.setattr(pipeline, "_probe_model", lambda m: False)
    t = store.Task(id="rot2")
    try:
        assert pipeline.assign_rotation_model(t) is None
        assert "llm_model" not in t.options
    finally:
        _cleanup_settings()


def test_assign_respects_explicit_model(monkeypatch):
    _set_rotation("m/A,m/B")
    monkeypatch.setattr(pipeline, "_probe_model",
                        lambda m: (_ for _ in ()).throw(AssertionError("不应探活")))
    t = store.Task(id="rot3", options={"llm_model": "user/chosen"})
    try:
        assert pipeline.assign_rotation_model(t) == "user/chosen"
    finally:
        _cleanup_settings()


# ---------- 裁判选择：不能是生成模型自己 ----------

def test_judge_is_next_in_rotation(monkeypatch):
    _set_rotation("m/A,m/B,m/C")
    t = store.Task(id="qc0", options={"llm_model": "m/C"})
    try:
        assert pipeline._judge_model_for(t) == "m/A"   # 环回第一个
        t.options["llm_model"] = "m/A"
        assert pipeline._judge_model_for(t) == "m/B"
    finally:
        _cleanup_settings()


# ---------- 质检报告生成 ----------

def test_generate_qc_builds_report(monkeypatch):
    _set_rotation("m/A,m/B")
    monkeypatch.setattr(llm, "review_note",
                        lambda src, note, style, judge: {
                            "fit": "基本符合",
                            "problems": ["小问题"],
                            "suggestions": ["小建议"]})
    t = store.Task(id="qc1", transcript="原" * 100,
                   options={"llm_model": "m/A"})
    t.variants = {"general": {"note": "稿" * 90, "sig": "x",
                              "info": {"model": "m/A", "seconds": 12.5,
                                       "ts": 1.0}}}
    try:
        qc = pipeline.generate_qc(t)
        assert qc["judge_model"] == "m/B"
        item = qc["items"]["general"]
        assert item["model"] == "m/A"
        assert item["ratio"] == 0.9
        assert item["seconds"] == 12.5
        assert item["fit"] == "基本符合"
        assert t.qc is qc or t.qc == qc
        assert not pipeline._qc_stale(t)   # 刚生成的报告不过期
    finally:
        _cleanup_settings()


def test_qc_stale_after_new_variant():
    t = store.Task(id="qc2", transcript="原文")
    t.qc = {"at_ts": 100.0, "items": {}}
    t.variants = {"general": {"note": "新稿", "info": {"ts": 200.0}}}
    assert pipeline._qc_stale(t)
