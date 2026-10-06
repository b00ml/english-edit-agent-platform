# tests/test_quality.py —— 质检加权聚合与多轮采样稳定性单测
import json
import statistics
from types import SimpleNamespace

import pytest
from pydantic import ValidationError

from app.config import settings
from app.engine.quality import (
    _aggregate_score,
    _build_judge_prompt,
    aggregate_rounds,
    resolve_judge_model,
    run_quality_check,
)
from app.errors import QualityCheckError

RULES = [
    {"id": "kp_match", "weight": 0.3},
    {"id": "diff_match", "weight": 0.2},
    {"id": "distractor", "weight": 0.3},
    {"id": "unambiguous", "weight": 0.2},
]


class TestAggregateScore:
    def test_full_weighted(self):
        dims = {"kp_match": 80, "diff_match": 90, "distractor": 70, "unambiguous": 60}
        # 80*.3 + 90*.2 + 70*.3 + 60*.2 = 24+18+21+12 = 75
        assert _aggregate_score(RULES, dims) == 75.0

    def test_missing_dimension_rejected(self):
        # 缺失维度不能靠归一化绕过完整性校验。
        dims = {"kp_match": 80, "distractor": 70, "unambiguous": 60}
        with pytest.raises(ValidationError):
            _aggregate_score(RULES, dims)

    def test_empty_rules_rejected(self):
        # 未声明有效规则时应失败，不取任意维度均值。
        dims = {"other": 100.0}
        with pytest.raises(QualityCheckError):
            _aggregate_score([], dims)


class TestAggregateRounds:
    ROUNDS = [
        {"kp_match": 85, "diff_match": 78, "distractor": 90, "unambiguous": 82},
        {"kp_match": 75, "diff_match": 88, "distractor": 80, "unambiguous": 92},
        {"kp_match": 80, "diff_match": 83, "distractor": 85, "unambiguous": 87},
    ]

    def test_aggregate_returns_per_dimension_mean(self):
        agg = aggregate_rounds(self.ROUNDS)
        assert agg["kp_match"] == 80.0
        assert agg["diff_match"] == pytest.approx((78 + 88 + 83) / 3)
        assert agg["distractor"] == 85.0

    def test_aggregate_rejects_missing_dimension_per_round(self):
        # 部分轮次缺维度时，不用其余采样伪装完整结果。
        rounds = [
            {"kp_match": 90},
            {"kp_match": 70},
            {"kp_match": 80, "distractor": 100},
        ]
        with pytest.raises(ValidationError):
            aggregate_rounds(rounds)

    def test_aggregate_reduces_variance(self):
        # 单轮各轮次维度分存在抖动（方差 > 0）
        kp_scores = [r["kp_match"] for r in self.ROUNDS]
        single_var = statistics.pvariance(kp_scores)
        assert single_var > 0

        # 均值聚合后同一样本的质检分恒定（重复聚合结果一致，方差为 0）
        agg = aggregate_rounds(self.ROUNDS)
        assert agg["kp_match"] == 80.0
        assert aggregate_rounds(self.ROUNDS) == agg

        # 聚合均值的方差显著低于单次采样方差（J4：同一样本多次质检方差下降）
        assert statistics.pvariance([agg["kp_match"]]) < single_var


class TestBuildJudgePrompt:
    def _make_template(self, rules):

        return SimpleNamespace(quality_rules=rules)

    def test_rubric_injected_into_prompt(self):
        # rubric 与 description 应被结构化注入到 user prompt
        rules = [
            {
                "id": "kp_match",
                "weight": 0.3,
                "description": "题干与答案是否匹配知识点",
                "rubric": {
                    "90-100": "精准命中",
                    "70-89": "基本命中",
                    "0-69": "不匹配",
                },
            }
        ]
        template = self._make_template(rules)
        system, user = _build_judge_prompt(template, {"stem": "..."})
        assert "评分标准" in user
        assert "kp_match（权重 0.3）：题干与答案是否匹配知识点" in user
        assert "90-100 分：精准命中" in user
        assert "70-89 分：基本命中" in user
        assert "0-69 分：不匹配" in user
        # system prompt 从 .st 文件加载，强调尺度一致与评分原则
        assert "尺度一致" in system
        assert "评分原则" in system
        # judge 质检技能规范（skills/judge/SKILL.md）应注入 system prompt
        assert "质检技能规范" in system
        assert "Anti-Patterns" in system

    def test_legacy_rules_without_rubric_ok(self):
        # 旧模板仅含 id/weight 仍可正常生成 prompt
        template = self._make_template([{"id": "kp_match", "weight": 0.3}])
        system, user = _build_judge_prompt(template, {"stem": "..."})
        assert "kp_match（权重 0.3）" in user
        assert "维度id" in user


# ---------------------------------------------------------------------------
# P1-2 / OPT-021：judge 独立模型解析链（自偏好偏差治理）
# ---------------------------------------------------------------------------


class _Template:
    def __init__(self, run_config=None):
        self.run_config = run_config or {}


class TestResolveJudgeModel:
    def test_template_override_wins(self, monkeypatch):
        monkeypatch.setattr(settings, "JUDGE_MODEL_NAME", "global-judge")
        assert resolve_judge_model(_Template({"judge_model": "tpl-judge"})) == "tpl-judge"

    def test_global_judge_model(self, monkeypatch):
        monkeypatch.setattr(settings, "JUDGE_MODEL_NAME", "global-judge")
        assert resolve_judge_model(_Template()) == "global-judge"

    def test_fallback_to_llm_model_name(self, monkeypatch):
        monkeypatch.setattr(settings, "JUDGE_MODEL_NAME", "")
        assert resolve_judge_model(_Template()) == settings.LLM_MODEL_NAME


class TestRunQualityCheckJudgeModelChain:
    def _run(self, monkeypatch, model_arg):
        captured = {}

        def _fake_call(client, model, system_prompt, user_prompt, rule_ids):
            captured["model"] = model
            return (
                json.dumps({"dimension_scores": {"kp_match": 80.0}}),
                1.0,
                SimpleNamespace(prompt_tokens=1, completion_tokens=1),
            )

        monkeypatch.setattr("app.engine.quality._call_judge_once", _fake_call)
        monkeypatch.setattr("app.engine.quality.record_trace", lambda **kw: None)
        monkeypatch.setattr("app.engine.quality.OpenAI", lambda **kw: None)
        template = SimpleNamespace(quality_rules=[{"id": "kp_match", "weight": 1.0}], version=1)
        score, dims = run_quality_check(template, {"stem": "x"}, model_arg, rounds=1)
        return captured["model"], score, dims

    def test_explicit_model_wins(self, monkeypatch):
        monkeypatch.setattr(settings, "JUDGE_MODEL_NAME", "global-judge")
        model, score, dims = self._run(monkeypatch, "explicit-judge")
        assert model == "explicit-judge" and score == 80.0 and dims == {"kp_match": 80.0}

    def test_global_judge_model_when_no_arg(self, monkeypatch):
        monkeypatch.setattr(settings, "JUDGE_MODEL_NAME", "global-judge")
        model, _, _ = self._run(monkeypatch, "")
        assert model == "global-judge"

    def test_fallback_to_llm_model_name(self, monkeypatch):
        monkeypatch.setattr(settings, "JUDGE_MODEL_NAME", "")
        model, _, _ = self._run(monkeypatch, "")
        assert model == settings.LLM_MODEL_NAME
