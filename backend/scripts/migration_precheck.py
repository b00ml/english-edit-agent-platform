"""迁移前预检：数据库连通性、Alembic 单一 head、当前 revision。"""

from __future__ import annotations

import ast
import sys
from pathlib import Path

# 兼容 `python scripts/migration_precheck.py` 直接文件入口；此时 Python
# 默认只把 scripts/ 放入 sys.path，无法解析同级 app 包。
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from sqlalchemy import create_engine, text  # noqa: E402
from sqlalchemy.exc import SQLAlchemyError  # noqa: E402

from app.config import settings  # noqa: E402


def main() -> int:
    """执行预检，任一项失败返回非零退出码。"""
    try:
        from alembic.config import Config
        from alembic.migration import MigrationContext
        from alembic.script import ScriptDirectory
    except ImportError:
        Config = MigrationContext = ScriptDirectory = None  # type: ignore[assignment]
    engine = create_engine(settings.DATABASE_URL, pool_pre_ping=True)
    try:
        with engine.connect() as connection:
            connection.execute(text("SELECT 1"))
            exists = connection.execute(text("SELECT to_regclass('public.content_item')")).scalar()
            if exists is not None:
                duplicates = connection.execute(
                    text(
                        "SELECT thread_id, count(*) FROM content_item WHERE thread_id IS NOT NULL "
                        "GROUP BY thread_id HAVING count(*) > 1 LIMIT 10"
                    )
                ).all()
                if duplicates:
                    print(
                        "ERROR: content_item 存在重复 thread_id，先人工核对，迁移不会自动删数据",
                        file=sys.stderr,
                    )
                    return 3
            if MigrationContext is not None:
                context = MigrationContext.configure(connection)
                current = sorted(context.get_current_heads())
            else:
                rows = connection.execute(text("SELECT version_num FROM alembic_version")).all()
                current = sorted(str(row[0]) for row in rows)
        if ScriptDirectory is not None:
            alembic_ini = Path(__file__).resolve().parents[1] / "alembic.ini"
            config = Config(str(alembic_ini))
            config.set_main_option("script_location", str(alembic_ini.parent / "alembic"))
            config.set_main_option("sqlalchemy.url", settings.DATABASE_URL)
            heads = sorted(ScriptDirectory.from_config(config).get_heads())
        else:
            heads = _manual_heads(Path(__file__).resolve().parents[1] / "alembic" / "versions")
        if len(heads) != 1:
            print(f"ERROR: Alembic heads 必须为单一 head，实际: {heads}", file=sys.stderr)
            return 2
        print(f"database=ok current_revision={','.join(current) or '<base>'} head={heads[0]}")
        return 0
    except (OSError, RuntimeError, ValueError, SQLAlchemyError) as exc:
        print(f"ERROR: migration precheck failed: {exc}", file=sys.stderr)
        return 1
    finally:
        engine.dispose()


def _manual_heads(versions_dir: Path) -> list[str]:
    """无 Alembic CLI 时解析迁移文件，仍能阻断多 head。"""
    revisions: set[str] = set()
    parents: set[str] = set()
    for path in versions_dir.glob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        values: dict[str, object] = {}
        for node in tree.body:
            target: ast.expr | None = None
            value_node: ast.expr | None = None
            if isinstance(node, ast.Assign) and node.targets:
                target = node.targets[0]
                value_node = node.value
            elif isinstance(node, ast.AnnAssign):
                target = node.target
                value_node = node.value
            if not isinstance(target, ast.Name) or target.id not in {"revision", "down_revision"}:
                continue
            if value_node is None:
                continue
            try:
                values[target.id] = ast.literal_eval(value_node)
            except (ValueError, SyntaxError):
                continue
        revision = values.get("revision")
        if isinstance(revision, str):
            revisions.add(revision)
        down_revision = values.get("down_revision")
        if isinstance(down_revision, str):
            parents.add(down_revision)
        elif isinstance(down_revision, (tuple, list)):
            parents.update(value for value in down_revision if isinstance(value, str))
    return sorted(revisions - parents)


if __name__ == "__main__":
    raise SystemExit(main())
