# scripts/judge_stability.py —— J4 judge 稳定采样方差验证
# 对同一样本重复质检 batch 次，对比「单次采样(rounds=1)」与「多轮均值(rounds=N)」的
# 总分方差，证明多轮均值能降低抖动（方差下降），满足 J4 验收。
#
# 用法（在 backend 容器内执行，具备 DB 与 LLM 访问）：
#   python scripts/judge_stability.py --template single_choice --payload-json '<payload>' --batch 5 --rounds 3
import argparse
import json
import statistics
import sys
from types import SimpleNamespace

sys.path.insert(0, __file__.rsplit("/", 1)[0] + "/..")

import yaml  # noqa: E402

from app.engine.quality import run_quality_check  # noqa: E402
from app.template_loader import _validate_template  # noqa: E402


def parse_args():
    p = argparse.ArgumentParser(description="judge 稳定采样方差验证")
    p.add_argument("--template", default="single_choice", help="题型模板 type_id")
    p.add_argument("--payload-json", help="待质检内容（JSON 字符串）")
    p.add_argument("--payload-file", help="待质检内容文件路径（JSON 文件，推荐）")
    p.add_argument("--batch", type=int, default=5, help="重复质检次数 K")
    p.add_argument("--rounds", type=int, default=3, help="多轮采样聚合轮数 N（对比 rounds=1）")
    p.add_argument("--output-file", help="报告写入路径（可选，规避终端输出丢失）")
    return p.parse_args()


def _load_template(type_id: str) -> SimpleNamespace:
    """从 templates/*.yaml 读取模板，构造带 quality_rules 的轻量对象（不依赖 DB）。"""
    import glob
    import os

    tpl_dir = os.path.join(os.path.dirname(__file__), "..", "app", "templates")
    for filepath in glob.glob(os.path.join(tpl_dir, "*.yaml")):
        with open(filepath, "r", encoding="utf-8") as f:
            data = yaml.safe_load(f)
        if data.get("type_id") == type_id:
            errors = _validate_template(data)
            if errors:
                raise ValueError(f"模板 {type_id} 校验失败: {'; '.join(errors)}")
            return SimpleNamespace(
                type_id=data["type_id"],
                version=data["version"],
                quality_rules=data["quality_rules"],
            )
    raise ValueError(f"未找到模板 {type_id}")


def main() -> int:
    args = parse_args()
    template = _load_template(args.template)
    if args.payload_file:
        with open(args.payload_file, "r", encoding="utf-8") as f:
            payload = json.load(f)
    else:
        payload = json.loads(args.payload_json)

    # 交替采样：单次与多轮均值交错执行，消除 judge 跨时间的系统性漂移，
    # 确保两组分数在相同时间窗内对比，公平评估「均值聚合是否降低抖动」。
    single_scores: list[float] = []
    multi_scores: list[float] = []
    for _ in range(args.batch):
        single_scores.append(run_quality_check(template, payload, rounds=1)[0])
        multi_scores.append(run_quality_check(template, payload, rounds=args.rounds)[0])

    single_var = statistics.pvariance(single_scores)
    multi_var = statistics.pvariance(multi_scores)
    lines = [
        f"样本: {template.type_id} v{template.version}",
        f"重复次数 K={args.batch}，多轮采样轮数 N={args.rounds}",
        f"  单次采样 总分序列: {[round(s, 2) for s in single_scores]}",
        f"  多轮均值 总分序列: {[round(s, 2) for s in multi_scores]}",
        f"  单次采样 方差: {single_var:.4f}",
        f"  多轮均值 方差: {multi_var:.4f}",
    ]
    if single_var == 0:
        lines.append("[结论] 单次采样方差为 0（无抖动），无需采样平均")
        rc = 0
    elif multi_var < single_var:
        lines.append(f"[通过] 多轮均值方差下降 {(1 - multi_var / single_var) * 100:.1f}%")
        rc = 0
    else:
        lines.append(f"[未通过] 多轮均值方差未下降（{multi_var:.4f} >= {single_var:.4f}）")
        rc = 1

    report = "\n".join(lines)
    print(report)
    if args.output_file:
        with open(args.output_file, "w", encoding="utf-8") as f:
            f.write(report + "\n")
    return rc


if __name__ == "__main__":
    raise SystemExit(main())
