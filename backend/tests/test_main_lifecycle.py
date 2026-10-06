import asyncio

from fastapi import Request

from app import main
from app.errors import PlatformError


def test_seed_model_profiles_creates_default_profiles(db, monkeypatch):
    from app.models import ModelProfile

    main._seed_model_profiles(db)
    profiles = db.query(ModelProfile).all()
    assert {p.name for p in profiles} == {"lite", "standard", "high"}
    assert all(p.model_hash for p in profiles)
    assert db.query(ModelProfile).filter(ModelProfile.is_default.is_(True)).count() == 1


def test_seed_model_profiles_skips_nonempty_table(db, monkeypatch):
    from app.models import ModelProfile

    main._seed_model_profiles(db)
    profile = db.query(ModelProfile).filter(ModelProfile.name == "lite").one()
    profile.model_name = "user-configured-model"
    db.commit()
    main._seed_model_profiles(db)
    assert db.query(ModelProfile).count() == 3 and profile.model_name == "user-configured-model"


def test_lifespan_initializes_and_validates(db, monkeypatch):
    monkeypatch.setattr(main, "SessionLocal", lambda: db)
    monkeypatch.setattr(main.settings, "ENVIRONMENT", "development")
    monkeypatch.setattr(main, "load_all_templates", lambda session: 3)
    monkeypatch.setattr(main, "validate_template_model_references", lambda session: [])
    monkeypatch.setattr(main, "seed_default_admin", lambda session: None)

    async def run():
        async with main.lifespan(main.app):
            from app.models import ModelProfile

            assert db.query(ModelProfile).count() == 3

    asyncio.run(run())


def test_create_app_exception_handlers_return_safe_responses():
    app = main.create_app()
    request = Request({"type": "http", "method": "GET", "path": "/boom", "headers": []})
    platform_handler = app.exception_handlers[PlatformError]
    response = asyncio.run(platform_handler(request, PlatformError("bad request")))
    assert response.status_code == 500
    assert b'"code":"PLATFORM_ERROR"' in response.body
    fallback = app.exception_handlers[Exception]
    response = asyncio.run(fallback(request, RuntimeError("secret")))
    assert response.status_code == 500
    assert b"secret" not in response.body
