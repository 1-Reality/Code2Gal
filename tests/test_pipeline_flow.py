"""流程编排（pipeline）离线端到端测试。

注入 fake 的 fetch / LLM / package，覆盖：
dry-run x script 矩阵、三轮 LLM（创作->批注->导演 JSON）、导演重试环、
草稿兜底、阶段产物保存、strict、素材包与脚本路径错误。
"""

import json
from pathlib import Path

import pytest

from repo2gal.director import DIRECTOR_SCHEMA_URI
from repo2gal.errors import AssetPackError, GenerationError, UsageError, ValidationFailed
from repo2gal.fetcher import Contributor, RepoContext
from repo2gal.generator import Cast
from repo2gal.pipeline import RunOptions, run_pipeline

SCRIPT = "say:这是现成剧本。\nend;\n"
DRAFT = "[B]\nwidget:你好。\n[B]\n这是旁白。\n"
ANNOTATIONS = "[b000001] 开场无演出\n"
EXAMPLE_PACK = "builtin:cc0-chronicle"


def make_ctx():
    return RepoContext(
        owner="acme",
        name="widget",
        description="A widget",
        language="Rust",
        stars=42,
        created_at="2020-01-01",
        contributors=[Contributor("alice", 100)],
    )


def make_options(tmp_path, **overrides):
    defaults = dict(
        owner="acme",
        repo="widget",
        output_dir=tmp_path / "out",
        backup_root=tmp_path / "backup",
        reuse_backup=True,
        token=None,
        api_key=None,
    )
    defaults.update(overrides)
    return RunOptions(**defaults)


def make_plan(beats, **overrides):
    plan = {
        "$schema": DIRECTOR_SCHEMA_URI,
        "schemaVersion": 1,
        "sceneId": "start",
        "storyHash": "sha256:" + "0" * 64,
        "profile": "chronicle-subtle",
        "title": "Widget",
        "subtitle": "",
        "beats": beats,
    }
    plan.update(overrides)
    return plan


def default_plan():
    """与 DRAFT 两个节拍一一对应的最小合法计划。"""
    return make_plan(
        [
            {"id": "b000001", "kind": "dialogue", "speaker": "widget", "text": "你好。"},
            {"id": "b000002", "kind": "narration", "speaker": None, "text": "这是旁白。"},
        ]
    )


def fake_fetch(options, log, progress):
    log("fake fetch")
    return make_ctx()


class FakeLLM:
    """按顺序返回多轮响应；响应耗尽即失败，防止测试静默走到兜底。"""

    def __init__(self, texts=None):
        self.texts = list(texts) if texts is not None else []
        self.calls = []

    def complete(self, prompt, *, temperature=0.8):
        self.calls.append((prompt, temperature))
        if not self.texts:
            raise AssertionError(f"FakeLLM 响应耗尽（第 {len(self.calls)} 次调用）")
        return self.texts.pop(0)


def run(tmp_path, *, options=None, llm=None, package_fn=None, **kwargs):
    options = options or make_options(tmp_path)
    return run_pipeline(
        options,
        llm_client=llm,
        fetch_fn=fake_fetch,
        package_fn=package_fn,
        **kwargs,
    )


def pack_into(target):
    def package_fn(clean, output_dir, **kwargs):
        target["clean"] = clean
        return output_dir

    return package_fn


# --- 三轮 LLM 主流程 ---

def test_full_llm_mode_runs_three_rounds_and_packages(tmp_path):
    llm = FakeLLM([DRAFT, ANNOTATIONS, json.dumps(default_plan(), ensure_ascii=False)])
    packaged = {}
    artifacts = run(tmp_path, llm=llm, package_fn=pack_into(packaged))

    assert len(llm.calls) == 3
    assert [temperature for _, temperature in llm.calls] == [0.8, 0.3, 0.2]
    assert artifacts.prompt == llm.calls[0][0]  # 第一轮 prompt 即创作 prompt
    assert "[b000001]" in artifacts.draft and "[b000002]" in artifacts.draft
    assert artifacts.annotations == ANNOTATIONS
    assert artifacts.director_plan is not None
    assert artifacts.director_report.semantic_valid is True
    assert "widget:你好。;" in artifacts.clean
    assert "say:这是旁白。 -clear;" in artifacts.clean
    assert artifacts.raw.endswith("end;\n")
    assert artifacts.report is not None
    assert artifacts.output_dir == tmp_path / "out"
    assert packaged["clean"] == artifacts.clean  # 打包收到的是校验后的编译产物


def test_full_script_mode_skips_llm(tmp_path):
    script = tmp_path / "story.txt"
    script.write_text(SCRIPT, encoding="utf-8")
    llm = FakeLLM()
    options = make_options(tmp_path, script=script)
    called = []

    def fake_package(clean, output_dir, **kwargs):
        called.append(1)
        return output_dir

    artifacts = run(tmp_path, options=options, llm=llm, package_fn=fake_package)

    assert not llm.calls
    assert artifacts.raw == SCRIPT
    assert artifacts.draft == "" and artifacts.director_plan is None
    assert artifacts.output_dir == tmp_path / "out"
    assert called


def test_dry_run_without_script_stops_before_llm(tmp_path):
    llm = FakeLLM()
    options = make_options(tmp_path, dry_run=True)
    artifacts = run(tmp_path, options=options, llm=llm)

    assert not llm.calls
    assert artifacts.prompt
    assert artifacts.raw == "" and artifacts.clean == ""
    assert artifacts.report is None
    assert artifacts.output_dir is None


def test_dry_run_with_script_validates_without_packaging(tmp_path):
    script = tmp_path / "story.txt"
    script.write_text(SCRIPT, encoding="utf-8")
    options = make_options(tmp_path, dry_run=True, script=script)
    packaged = []

    artifacts = run(tmp_path, options=options, package_fn=lambda *a, **kw: packaged.append(1))

    assert artifacts.report is not None
    assert artifacts.clean.endswith("end;\n")
    assert artifacts.output_dir is None
    assert not packaged  # 没有打包


# --- 导演重试环与兜底 ---

def test_director_retry_feeds_feedback_and_recovers(tmp_path):
    llm = FakeLLM(
        [
            DRAFT,
            ANNOTATIONS,
            "not-json",
            json.dumps(default_plan(), ensure_ascii=False),
        ]
    )
    artifacts = run(tmp_path, llm=llm, package_fn=lambda clean, output, **kw: output)

    assert len(llm.calls) == 4
    assert llm.calls[2][1] == 0.2
    assert "上一次输出未通过校验" in llm.calls[3][0]
    assert "不是合法 JSON" in llm.calls[3][0]
    assert artifacts.director_plan is not None
    assert artifacts.director_report.semantic_valid is True
    assert "widget:你好。;" in artifacts.clean


def test_director_retries_exhausted_uses_draft_fallback(tmp_path):
    llm = FakeLLM([DRAFT, ANNOTATIONS, "bad-1", "bad-2", "bad-3"])
    artifacts = run(tmp_path, llm=llm, package_fn=lambda clean, output, **kw: output)

    assert len(llm.calls) == 5  # 1 次 + 默认重试 2 次
    assert artifacts.director_plan is None
    assert artifacts.director_report.degraded is True
    assert artifacts.output_dir == tmp_path / "out"
    assert "widget:你好。;" in artifacts.clean
    assert "say:这是旁白。 -clear;" in artifacts.clean


def test_format_retries_zero_disables_retry(tmp_path):
    llm = FakeLLM([DRAFT, ANNOTATIONS, "bad"])
    options = make_options(tmp_path, format_retries=0)
    artifacts = run(tmp_path, options=options, llm=llm, package_fn=lambda clean, output, **kw: output)

    assert len(llm.calls) == 3
    assert artifacts.director_plan is None
    assert artifacts.director_report.degraded is True


def test_negative_format_retries_rejected(tmp_path):
    options = make_options(tmp_path, format_retries=-1)
    with pytest.raises(UsageError, match="format-retries"):
        run(tmp_path, options=options)


def test_unknown_profile_rejected_before_fetch(tmp_path):
    called = []
    options = make_options(tmp_path, profile="unknown-profile")
    with pytest.raises(UsageError, match="profile"):
        run_pipeline(options, fetch_fn=lambda *args: called.append(1))
    assert not called


def test_llm_transport_failure_aborts_with_generation_error(tmp_path):
    class BoomLLM:
        def complete(self, prompt, *, temperature=0.8):
            raise GenerationError("服务不可用")

    with pytest.raises(GenerationError):
        run(tmp_path, llm=BoomLLM())


# --- 阶段产物保存 ---

def test_save_stage_outputs_writes_rounds_and_report(tmp_path):
    stage_dir = tmp_path / "stages"
    llm = FakeLLM([DRAFT, ANNOTATIONS, json.dumps(default_plan(), ensure_ascii=False)])
    options = make_options(tmp_path, save_stage_outputs=stage_dir)
    run(tmp_path, options=options, llm=llm, package_fn=lambda clean, output, **kw: output)

    assert (stage_dir / "01-draft.md").exists()
    draft_text = (stage_dir / "01-draft.md").read_text(encoding="utf-8")
    assert "[b000001]" in draft_text and "[b000002]" in draft_text
    assert (stage_dir / "02-annotations.md").exists()
    assert (stage_dir / "03-director-attempt-1.json").exists()
    report = json.loads((stage_dir / "03-director-report.json").read_text(encoding="utf-8"))
    assert report["semanticValid"] is True


def test_save_stage_outputs_keeps_failed_attempt_and_feedback(tmp_path):
    stage_dir = tmp_path / "stages"
    llm = FakeLLM([DRAFT, ANNOTATIONS, "bad", json.dumps(default_plan(), ensure_ascii=False)])
    options = make_options(tmp_path, save_stage_outputs=stage_dir)
    run(tmp_path, options=options, llm=llm, package_fn=lambda clean, output, **kw: output)

    assert (stage_dir / "03-director-attempt-1.json").read_text(encoding="utf-8") == "bad"
    assert (stage_dir / "03-director-feedback-1.md").exists()
    assert (stage_dir / "03-director-attempt-2.json").exists()


# --- 演出编译路径 ---

def test_transition_compiles_into_change_bg_args(tmp_path):
    plan = default_plan()
    plan["beats"][0]["stage"] = {"background": "bg.webp"}
    plan["beats"][0]["cue"] = {
        "anchor": "before",
        "actions": [
            {"kind": "screen.transition", "preset": "shockwaveIn", "phase": "enter", "duration": "short"}
        ],
    }
    llm = FakeLLM([DRAFT, ANNOTATIONS, json.dumps(plan, ensure_ascii=False)])
    artifacts = run(tmp_path, llm=llm, package_fn=lambda clean, output, **kw: output)

    assert "changeBg:bg.webp -enter=shockwaveIn -enterDuration=500;" in artifacts.clean
    assert artifacts.director_report.degraded is False


def test_screen_effect_compiles_and_reports_cue_count(tmp_path):
    plan = default_plan()
    plan["beats"][1]["cue"] = {
        "anchor": "during",
        "actions": [{"kind": "screen.effect", "preset": "snow", "intensity": "subtle"}],
    }
    llm = FakeLLM([DRAFT, ANNOTATIONS, json.dumps(plan, ensure_ascii=False)])
    artifacts = run(tmp_path, llm=llm, package_fn=lambda clean, output, **kw: output)

    assert "pixiInit;" in artifacts.clean
    assert "pixiPerform:snow;" in artifacts.clean
    assert artifacts.director_report.cue_count == 1


def test_annotation_round_can_be_empty(tmp_path):
    llm = FakeLLM([DRAFT, "", json.dumps(default_plan(), ensure_ascii=False)])
    artifacts = run(tmp_path, llm=llm, package_fn=lambda clean, output, **kw: output)
    assert artifacts.annotations == ""
    assert artifacts.director_plan is not None


# --- strict 与脚本模式校验 ---

def test_narration_after_dialogue_is_cleared_before_packaging(tmp_path):
    """编译产物里的旁白必须带 -clear，避免 WebGAL 继承上一句说话人。"""
    llm = FakeLLM([DRAFT, ANNOTATIONS, json.dumps(default_plan(), ensure_ascii=False)])
    captured = {}
    artifacts = run(tmp_path, llm=llm, package_fn=pack_into(captured))

    assert "widget:你好。;" in artifacts.clean
    assert "say:这是旁白。 -clear;" in artifacts.clean
    assert captured["clean"] == artifacts.clean


def test_strict_raises_validation_failed_on_downgrade(tmp_path):
    script = tmp_path / "story.txt"
    script.write_text("say:你好;\nunknownCommand:foo;\nend;\n", encoding="utf-8")
    options = make_options(tmp_path, script=script, strict=True)
    with pytest.raises(ValidationFailed) as exc:
        run(tmp_path, options=options)
    assert "strict" in str(exc.value)


def test_strict_allows_clean_script(tmp_path):
    script = tmp_path / "story.txt"
    script.write_text(SCRIPT, encoding="utf-8")
    options = make_options(tmp_path, script=script, strict=True)
    artifacts = run(
        tmp_path,
        options=options,
        package_fn=lambda clean, output_dir, **kwargs: output_dir,
    )
    assert artifacts.output_dir is not None


def test_cast_whitelist_applies_to_script(tmp_path):
    """角色白名单对 --script 同样生效：未声明的 ASCII 角色会被降级。"""
    script = tmp_path / "story.txt"
    script.write_text("stranger:你好;\nend;\n", encoding="utf-8")
    options = make_options(tmp_path, script=script, strict=True)
    with pytest.raises(ValidationFailed):
        run(tmp_path, options=options)


def test_default_assets_reject_change_figure_reference(tmp_path):
    script = tmp_path / "story.txt"
    script.write_text("changeFigure:character.guide.normal;\nend;\n", encoding="utf-8")
    artifacts = run(tmp_path, options=make_options(tmp_path, script=script),
                    package_fn=lambda clean, output, **kw: output)
    assert artifacts.report.downgrades == 1
    assert artifacts.clean.splitlines()[0].startswith(";[repo2gal]")


# --- save-prompt 与脚本路径 ---

def test_save_prompt_writes_file(tmp_path):
    target = tmp_path / "nested" / "prompt.md"
    options = make_options(tmp_path, dry_run=True, save_prompt=target)
    artifacts = run(tmp_path, options=options)
    assert target.read_text(encoding="utf-8") == artifacts.prompt


def test_missing_script_raises_usage_error(tmp_path):
    options = make_options(tmp_path, script=tmp_path / "missing.txt")
    with pytest.raises(UsageError):
        run(tmp_path, options=options)


def test_script_symlink_rejected(tmp_path):
    real = tmp_path / "real.txt"
    real.write_text(SCRIPT, encoding="utf-8")
    link = tmp_path / "link.txt"
    link.symlink_to(real)
    options = make_options(tmp_path, script=link)
    with pytest.raises(UsageError):
        run(tmp_path, options=options)


# --- Asset Pack v1 ---

def test_asset_pack_flows_from_prompt_through_validator_to_packager(tmp_path):
    script = tmp_path / "story.txt"
    script.write_text(
        "changeBg:background.archive;\n"
        "changeFigure:character.guide.normal;\n"
        "bgm:bgm.archive;\n"
        "changeBg:bg.webp;\n"
        "bgm:s_Title.mp3;\n"
        "end;\n",
        encoding="utf-8",
    )
    options = make_options(
        tmp_path,
        script=script,
        asset_pack=EXAMPLE_PACK,
        public_assets=True,
        strict=True,
    )
    captured = {}

    def fake_package(clean, output_dir, **kwargs):
        captured["clean"] = clean
        captured["pack"] = kwargs["asset_pack"]
        return output_dir

    artifacts = run(tmp_path, options=options, package_fn=fake_package)

    assert "background.archive" in artifacts.prompt
    assert "character.guide.normal" in artifacts.prompt
    assert "bgm.archive" in artifacts.prompt
    assert "bg.webp" in artifacts.prompt and "s_Title.mp3" in artifacts.prompt
    assert artifacts.report.downgrades == 0
    assert captured["clean"] == artifacts.clean
    assert captured["pack"].name == "@repo2gal/example-cc0-chronicle"
    assert artifacts.asset_pack is captured["pack"]


def test_invalid_asset_pack_fails_before_fetch(tmp_path):
    called = []
    options = make_options(tmp_path, asset_pack=tmp_path / "missing")

    def should_not_fetch(*args):
        called.append(1)
        return make_ctx()

    with pytest.raises(AssetPackError):
        run_pipeline(options, fetch_fn=should_not_fetch)
    assert not called


def test_public_assets_requires_asset_pack_before_fetch(tmp_path):
    called = []
    options = make_options(tmp_path, public_assets=True)

    with pytest.raises(UsageError, match="--asset-pack"):
        run_pipeline(options, fetch_fn=lambda *args: called.append(1))
    assert not called
