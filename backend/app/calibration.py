# app/calibration.py —— 质检权重反向校准（J1 质量闭环）
#
# 闭环逻辑：
#   低分自动改版（graph.py after_qc）→ 人工抽检驳回（routes.py review_content）
#   → 收集"假阳性"样本（auto 高分但 manual 驳回）→ 计算每维放水度
#   → 下调放水维度权重 → 下次质检更准 → 驳回率下降
#
# 核心纯函数：
#   - fit_weights:       由 FP/Pass 两集维度分计算放水度与新权重
#   - collect_labeled:   从 DB 收集既有 auto 又有 manual 的标注对
#   - recalibrate:       编排 collect→fit→持久化
#   - get_effective_weights: 读取最新校准覆盖权重（无则返回模板默认）
from statistics import mean
from typing import Dict, List, Optional, Tuple

from sqlalchemy.orm import Session

from app.config import settings
from app.models import ContentItem, QualityCalibration, QualityRecord, QuestionTemplate

# 校准守卫：最少标注样本数与假阳性数，不足则跳过
_MIN_LABELED_SAMPLES = 20
_MIN_FALSE_PASS = 5
# 学习步长：控制单次校准幅度（1.0 = 完全按放水度比例降权）
_DEFAULT_ALPHA = 1.0
# 权重下限钳制：防止某维度归零（相对均值的最小占比）
_WEIGHT_FLOOR_RATIO = 0.05


def fit_weights(
    default_weights: Dict[str, float],
    fp_dims: List[Dict[str, float]],
    pass_dims: List[Dict[str, float]],
    alpha: float = _DEFAULT_ALPHA,
) -> Tuple[Dict[str, float], Dict[str, float]]:
    """由假阳性集与通过集的维度分计算放水度与新权重（纯函数）。

    放水度 lenient_i = mean(FP 维度分_i) - mean(Pass 维度分_i)
    - lenient_i > 0：judge 在该维度对差样本给了高分（放水），应降权
    - lenient_i ≤ 0：该维度有判别力，保持或提权

    新权重 w'_i = w_i × (1 − α × normalized_lenient_i)，再归一化 + 下限钳制。

    返回 (new_weights, lenient_scores)。
    """
    if not fp_dims or not pass_dims:
        return dict(default_weights), {}

    # 收集所有维度 id
    all_dims = set(default_weights.keys())
    for row in fp_dims + pass_dims:
        all_dims.update(row.keys())

    # 计算每维放水度
    lenient: Dict[str, float] = {}
    for dim_id in all_dims:
        fp_scores = [r[dim_id] for r in fp_dims if dim_id in r]
        pass_scores = [r[dim_id] for r in pass_dims if dim_id in r]
        if fp_scores and pass_scores:
            lenient[dim_id] = round(mean(fp_scores) - mean(pass_scores), 2)
        else:
            lenient[dim_id] = 0.0

    # 归一化放水度到 [0, 1]（仅对正放水度降权）
    max_lenient = max(lenient.values()) if lenient else 0.0
    if max_lenient <= 0:
        # 没有放水维度，保持原权重
        return dict(default_weights), lenient

    # 计算新权重
    raw_weights: Dict[str, float] = {}
    for dim_id, w in default_weights.items():
        lenient_score = lenient.get(dim_id, 0.0)
        norm_l = max(lenient_score, 0.0) / max_lenient  # 只对正放水度降权
        raw_weights[dim_id] = w * (1.0 - alpha * norm_l)

    # 下限钳制：任何维度不低于平均权重的 _WEIGHT_FLOOR_RATIO
    n = len(raw_weights)
    if n > 0:
        raw_sum = sum(raw_weights.values())
        floor = (raw_sum / n) * _WEIGHT_FLOOR_RATIO
        raw_weights = {k: max(v, floor) for k, v in raw_weights.items()}

    # 归一化使总和为 1
    total = sum(raw_weights.values())
    if total > 0:
        new_weights = {k: round(v / total, 4) for k, v in raw_weights.items()}
    else:
        new_weights = dict(default_weights)

    return new_weights, lenient


def collect_labeled(
    db: Session,
    template_id: str,
    threshold: float,
    tenant_id: str | None = None,
) -> Tuple[List[Dict[str, float]], List[Dict[str, float]], int, int, float]:
    """从 DB 收集既有 auto 又有 manual_review 标注的内容，返回：
    (fp_dims, pass_dims, sample_size, false_pass_cnt, rejection_rate)

    - fp_dims: 假阳性集（auto 分 ≥ threshold 但 manual 驳回）的各维度分
    - pass_dims: 人工通过集的各维度分
    """
    # 查找该模板下有 manual_review 记录的内容条目
    items = (
        db.query(ContentItem)
        .filter(ContentItem.template_id == template_id, ContentItem.tenant_id == tenant_id)
        .all()
    )
    fp_dims: List[Dict[str, float]] = []
    pass_dims: List[Dict[str, float]] = []
    labeled = 0
    fp_count = 0
    manual_total = 0
    manual_reject = 0

    for item in items:
        records = (
            db.query(QualityRecord)
            .filter(QualityRecord.item_id == item.id)
            .order_by(QualityRecord.created_at)
            .all()
        )
        auto_rec = next((r for r in records if r.source == "auto"), None)
        manual_rec = next((r for r in records if r.source == "manual_review"), None)
        if not auto_rec or not manual_rec:
            continue
        if auto_rec.tenant_id != item.tenant_id or manual_rec.tenant_id != item.tenant_id:
            continue

        labeled += 1
        manual_total += 1
        auto_dims = auto_rec.dimension_scores or {}
        manual_score = manual_rec.score

        if manual_score == 0:
            # 人工驳回
            manual_reject += 1
            if auto_rec.score >= threshold:
                # 假阳性：auto 通过但 manual 驳回
                fp_count += 1
                fp_dims.append({k: float(v) for k, v in auto_dims.items()})
        else:
            # 人工通过
            pass_dims.append({k: float(v) for k, v in auto_dims.items()})

    rejection_rate = manual_reject / manual_total if manual_total > 0 else 0.0

    return fp_dims, pass_dims, labeled, fp_count, round(rejection_rate, 4)


def _get_template_defaults(template: QuestionTemplate) -> Tuple[Dict[str, float], float]:
    """从模板 quality_rules 提取默认权重字典与阈值。"""
    rules = template.quality_rules or []
    default_weights = {}
    for r in rules:
        if isinstance(r, dict):
            rid = r.get("id")
            if rid:
                default_weights[rid] = float(r.get("weight", 0.0))
    run_config = template.run_config or {}
    threshold = float(run_config.get("quality_threshold", settings.QUALITY_THRESHOLD))
    return default_weights, threshold


def recalibrate(
    db: Session,
    template_id: str,
    alpha: float = _DEFAULT_ALPHA,
    min_samples: int = _MIN_LABELED_SAMPLES,
    min_fp: int = _MIN_FALSE_PASS,
    tenant_id: str | None = None,
) -> QualityCalibration:
    """编排校准全流程：collect → fit → 持久化，返回校准记录。

    样本不足时仍写入一条记录（note 标注"样本不足"），但不改变默认权重。
    """
    template = db.query(QuestionTemplate).filter(QuestionTemplate.type_id == template_id).first()
    if template is None:
        raise ValueError(f"题型模板不存在: {template_id}")

    default_weights, threshold = _get_template_defaults(template)

    fp_dims, pass_dims, sample_size, fp_count, rejection_rate = collect_labeled(
        db, template_id, threshold, tenant_id
    )

    # 守卫：样本不足时保持默认权重
    if sample_size < min_samples or fp_count < min_fp:
        calib = QualityCalibration(
            template_id=template_id,
            tenant_id=tenant_id,
            weights=default_weights,
            threshold=threshold,
            default_weights=default_weights,
            default_threshold=threshold,
            sample_size=sample_size,
            false_pass_cnt=fp_count,
            lenient={},
            rejection_rate=rejection_rate,
            note=f"样本不足（标注 {sample_size} 条，假阳性 {fp_count} 条），"
            f"需 ≥{min_samples} 标注且 ≥{min_fp} 假阳性，保持默认权重",
        )
        db.add(calib)
        db.commit()
        db.refresh(calib)
        return calib

    # 计算放水度与新权重
    new_weights, lenient = fit_weights(default_weights, fp_dims, pass_dims, alpha)

    # 阈值再校准：驳回率超标则建议收紧
    new_threshold = threshold
    target_rejection = 0.05
    if rejection_rate > target_rejection:
        # 收紧到通过集 auto 分的 P10 附近（不低于默认阈值）
        pass_scores = [mean(d.values()) for d in pass_dims if d]
        if pass_scores:
            p10 = sorted(pass_scores)[max(0, len(pass_scores) // 10)]
            new_threshold = max(threshold, round(p10, 1))

    calib = QualityCalibration(
        template_id=template_id,
        tenant_id=tenant_id,
        weights=new_weights,
        threshold=new_threshold,
        default_weights=default_weights,
        default_threshold=threshold,
        sample_size=sample_size,
        false_pass_cnt=fp_count,
        lenient=lenient,
        rejection_rate=rejection_rate,
        note=f"校准成功：{fp_count} 个假阳性样本反向降权，"
        f"驳回率 {rejection_rate:.1%}（目标 ≤5%）",
    )
    db.add(calib)
    db.commit()
    db.refresh(calib)
    return calib


def get_effective_weights(
    db: Session, template_id: str, tenant_id: str | None = None
) -> Tuple[Optional[Dict[str, float]], Optional[float]]:
    """读取最新校准记录的生效权重与阈值。

    无校准记录时返回 (None, None)，调用方回退到模板默认值。
    """
    calib = (
        db.query(QualityCalibration)
        .filter(
            QualityCalibration.template_id == template_id, QualityCalibration.tenant_id == tenant_id
        )
        .order_by(QualityCalibration.created_at.desc())
        .first()
    )
    if calib is None:
        return None, None
    return calib.weights, calib.threshold


def list_calibrations(db: Session, template_id: Optional[str] = None) -> List[QualityCalibration]:
    """查询校准记录列表（可按模板过滤），按时间倒序。"""
    query = db.query(QualityCalibration)
    if template_id:
        query = query.filter(QualityCalibration.template_id == template_id)
    return query.order_by(QualityCalibration.created_at.desc()).all()
