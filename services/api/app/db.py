from __future__ import annotations

from collections.abc import Generator

from sqlalchemy import create_engine
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker

from app.config import get_settings


class Base(DeclarativeBase):
    pass


settings = get_settings()


def resolve_database_url() -> str:
    if settings.database_url:
        return settings.database_url
    if settings.state_backend == "oss":
        return "sqlite:///:memory:"
    raise RuntimeError("DATABASE_URL is required unless STATE_BACKEND=oss")


database_url = resolve_database_url()
connect_args = {"check_same_thread": False, "timeout": 30} if database_url.startswith("sqlite") else {}
engine = create_engine(database_url, future=True, pool_pre_ping=True, connect_args=connect_args)
SessionLocal = sessionmaker(bind=engine, class_=Session, expire_on_commit=False, autoflush=False)


def init_db() -> None:
    from app import models  # noqa: F401

    Base.metadata.create_all(bind=engine)


def get_db() -> Generator[Session, None, None]:
    session = SessionLocal()
    try:
        yield session
    finally:
        session.close()
