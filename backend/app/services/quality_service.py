"""
质检 Service
职责：质检标注、校准、质检记录查询
"""

from typing import Optional

from fastapi import HTTPException
from sqlalchemy import desc
from sqlalchemy.orm import Session

from app.calibration import recalibrate
from app.domain.status import can_transition_content
from app.engine.agreement import judge_vs_manual
from app.errors import ContentStateConflictError, TenantScopeDeniedError
from app.models import ContentItem, QualityCalibration, QualityRecord, QuestionTemplate, User
from app.rag.provenance import reference_snapshot
from app.repositories import QualityCalibrationRepository, QualityRecordRepository
from app.schemas import CalibrateRequest, CalibrationOut, QualityReviewRequest
from app.services.content_guard import require_valid_content
from app.tenancy import require_scope, scope_query
from app.versioning import quality_snapshot
from app.workflow.graph import resume_human_review


class QualityService:
    """质检业务逻辑层。"""

    def __init__(self, db: Session):
        self.db = db
        self.quality_repo = QualityRecordRepository(db)
        self.calibration_repo = QualityCalibrationRepository(db)

    def review_content(
        self, content_id: str, req: QualityReviewRequest, current_user: User
    ) -> QualityRecord:
        """人工质检标注（通过/驳回）。

        灰区人工卡点（P1-1）：条目若由 LangGraph interrupt 暂停（thread_id 存在且
        status=awaiting_review），凭 Command(resume) 恢复图，由 human_review 节点
        完成裁决落库；存量条目走旧路径。

        Args:
            content_id: 内容 ID
            req: 质检请求（通过/驳回 + 原因）
            current_user: 当前用户（记录评审人）

        Returns:
            质检记录

        Raises:
            HTTPException: 内容不存在
        """
        item = self.db.get(ContentItem, content_id)
        if item is None:
            raise HTTPException(status_code=404, detail="内容不存在")

        require_scope(item, current_user)
        # 通过/驳回的评分约定：通过=100，驳回=0
        score = 100.0 if req.pass_ else 0.0

        target = "passed" if req.pass_ else "rejected"
        if not can_transition_content(item.status, target):
            # 同一审核者重发同一裁决复用原记录；终态不能反向裁决或退回已发布内容。
            previous = (
                self.db.query(QualityRecord)
                .filter(
                    QualityRecord.item_id == content_id,
                    QualityRecord.source == "manual_review",
                )
                .order_by(desc(QualityRecord.created_at))
                .first()
            )
            reason = req.reason if not req.pass_ else None
            if (
                previous is not None
                and previous.reviewer == current_user.id
                and previous.score == score
                and (previous.reason or None) == (reason or None)
            ):
                return previous
            raise ContentStateConflictError(f"内容状态 {item.status} 不允许审核为 {target}")

        if req.pass_:
            require_valid_content(self.db, item)

        provenance = item.provenance or {}
        if req.pass_ and provenance.get("require_review"):
            if not req.reference_verified or not provenance.get("citations"):
                raise ContentStateConflictError("该内容要求人工核验实际参考来源后才能通过")
            item.provenance = {
                **provenance,
                "reference_review": {
                    "verified": True,
                    "reviewer": current_user.id,
                    **reference_snapshot(provenance["citations"]),
                },
            }
        # 若条目处于灰区卡点状态，通过 LangGraph 恢复执行
        if item.thread_id and item.status == "awaiting_review":
            resume_human_review(
                item.thread_id,
                {
                    "approved": req.pass_,
                    "score": score,
                    "reason": req.reason or "",
                    "reviewer_id": current_user.id,
                    "reference_verified": req.reference_verified,
                },
                self.db,
            )
            # 查询 human_review 节点创建的质检记录
            record = (
                self.db.query(QualityRecord)
                .filter(QualityRecord.item_id == content_id)
                .filter(QualityRecord.source == "manual_review")
                .order_by(desc(QualityRecord.created_at))
                .first()
            )
            return record

        # 存量条目走旧路径：直接创建质检记录
        template = (
            self.db.query(QuestionTemplate)
            .filter(QuestionTemplate.type_id == item.template_id)
            .first()
        )
        auto_record = (
            self.db.query(QualityRecord)
            .filter(
                QualityRecord.item_id == content_id,
                QualityRecord.source == "auto",
            )
            .order_by(desc(QualityRecord.created_at))
            .first()
        )
        frozen_snapshot = auto_record.config_snapshot if auto_record is not None else None
        record = self.quality_repo.create(
            item_id=content_id,
            score=score,
            dimension_scores={"manual": score},
            source="manual_review",
            reviewer=current_user.id,
            reason=req.reason if not req.pass_ else None,
            tenant_id=item.tenant_id,
            template_version=(
                auto_record.template_version
                if auto_record is not None
                else (template.version if template else None)
            ),
            config_snapshot=frozen_snapshot
            or (quality_snapshot(template, model_name=None) if template else None),
        )

        # 同步更新内容状态
        item.status = "passed" if req.pass_ else "rejected"
        item.qc_score = score
        self.db.commit()
        self.db.refresh(record)
        return record

    def calibrate_quality(
        self, req: CalibrateRequest, current_user: User | None = None
    ) -> CalibrationOut:
        """触发质检权重校准。

        用人工驳回样本反向校准 judge 维度权重：
        - 收集假阳性样本（auto 高分但 manual 驳回）
        - 计算放水度（假阳性集维度分 - 通过集维度分）
        - 下调放水维度权重

        Args:
            req: 校准请求（题型 + 超参）

        Returns:
            校准结果（新权重 + 放水度 + 样本统计）
        """
        template = (
            self.db.query(QuestionTemplate)
            .filter(QuestionTemplate.type_id == req.template_id)
            .first()
        )
        require_scope(template, current_user, shared=True)
        tenant_id = current_user.tenant_id if current_user is not None else None
        if "tenant_id" in req.model_fields_set:
            if (
                current_user is not None
                and current_user.role != "admin"
                and req.tenant_id != tenant_id
            ):
                raise TenantScopeDeniedError("仅全局管理员可选择其他租户的校准数据")
            tenant_id = req.tenant_id
        calib = recalibrate(
            self.db,
            req.template_id,
            alpha=req.alpha,
            min_samples=req.min_samples,
            min_fp=req.min_fp,
            tenant_id=tenant_id,
        )

        # 附带：本次校准样本集上 judge 与人工的二值判定一致性（P0-4，只读参考）
        pairs = self._auto_manual_pairs(req.template_id, tenant_id)
        out = CalibrationOut.model_validate(calib)
        out.agreement = judge_vs_manual(pairs, calib.threshold)
        return out

    def list_calibrations(
        self, template_id: str, skip: int = 0, limit: int = 20, current_user: User | None = None
    ) -> list[QualityCalibration]:
        """查询题型的历史校准记录。"""
        return (
            scope_query(self.db.query(QualityCalibration), QualityCalibration, current_user)
            .filter(QualityCalibration.template_id == template_id)
            .order_by(QualityCalibration.created_at.desc())
            .offset(skip)
            .limit(limit)
            .all()
        )

    def get_latest_calibration(self, template_id: str) -> Optional[QualityCalibration]:
        """获取题型的最新校准记录（当前生效权重）。"""
        return self.calibration_repo.get_latest_by_template(template_id)

    def get_quality_record(self, content_id: str) -> Optional[QualityRecord]:
        """查询内容的最新质检记录。"""
        return self.quality_repo.get_by_item(content_id)

    def list_manual_rejections(self, skip: int = 0, limit: int = 50) -> list[QualityRecord]:
        """查询人工驳回记录（用于校准分析）。"""
        return self.quality_repo.list_manual_rejections(skip, limit)

    def _auto_manual_pairs(self, template_id: str, tenant_id: str | None = None) -> list:
        """收集同 item 的 (auto 总分, manual 分) 标注对（各取该 item 的第一条）。"""
        rows = (
            self.db.query(QualityRecord.item_id, QualityRecord.source, QualityRecord.score)
            .join(ContentItem, ContentItem.id == QualityRecord.item_id)
            .filter(ContentItem.template_id == template_id)
            .filter(ContentItem.tenant_id == tenant_id, QualityRecord.tenant_id == tenant_id)
            .filter(QualityRecord.source.in_(["auto", "manual_review"]))
            .order_by(QualityRecord.item_id, QualityRecord.created_at)
            .all()
        )
        auto: dict = {}
        manual: dict = {}
        for item_id, source, score in rows:
            if source == "auto":
                auto.setdefault(item_id, float(score))
            else:
                manual.setdefault(item_id, float(score))

        # 返回同时有 auto 和 manual 的样本对
        common_ids = set(auto.keys()) & set(manual.keys())
        return [(auto[iid], manual[iid]) for iid in common_ids]
