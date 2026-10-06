"""Content admission guards shared by manual review and publication."""

from sqlalchemy.orm import Session

from app.engine.content_validation import enforce_content
from app.errors import ContentStateConflictError, ContentValidationError
from app.models import ContentItem, QuestionTemplate


def require_valid_content(session: Session, item: ContentItem) -> None:
    template = (
        session.query(QuestionTemplate).filter(QuestionTemplate.type_id == item.template_id).first()
    )
    if template is None:
        raise ContentStateConflictError("题型模板不存在，无法执行发布/审核前确定性校验")
    try:
        item.validation_report = enforce_content(
            item.payload, template.output_schema, template.run_config
        ).model_dump()
    except ContentValidationError as exc:
        raise ContentStateConflictError(f"题目确定性校验未通过: {exc}") from exc
