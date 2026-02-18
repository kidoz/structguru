"""Full-stack FastAPI + Celery + SQLAlchemy example with structguru."""

from __future__ import annotations

from typing import Any

from celery import Celery
from fastapi import FastAPI
from sqlalchemy import Column, Integer, String, create_engine
from sqlalchemy.orm import declarative_base, sessionmaker

from structguru import configure_structlog, logger
from structguru.integrations.asgi import StructguruMiddleware
from structguru.integrations.celery import setup_celery_logging
from structguru.integrations.sqlalchemy import setup_query_logging

# 1. Configure logging
configure_structlog(service="fullstack-app", json_logs=False)

# 2. Database setup
Base: Any = declarative_base()


class User(Base):
    __tablename__ = "users"
    id = Column(Integer, primary_key=True)
    name = Column(String)


engine = create_engine("sqlite:///:memory:")
Base.metadata.create_all(engine)
SessionLocal = sessionmaker(bind=engine)

# Add SQLAlchemy logging
setup_query_logging(engine, slow_threshold_ms=10)

# 3. Celery setup
celery_app = Celery("tasks", broker="memory://")
setup_celery_logging(propagate_context=True, context_keys=["request_id"])


@celery_app.task(name="process_user_task")
def process_user_task(user_id: int) -> None:
    logger.info("Processing user {user_id} in background", user_id=user_id)
    # Simulate work
    import time

    time.sleep(0.1)
    logger.info("Background task complete for user {user_id}", user_id=user_id)


# 4. FastAPI setup
app = FastAPI()
app.add_middleware(StructguruMiddleware)


@app.get("/users/{name}")
async def create_user(name: str):
    logger.info("Handling request to create user: {name}", name=name)

    with SessionLocal() as session:
        user = User(name=name)
        session.add(user)
        session.commit()
        session.refresh(user)

        logger.info("User {name} created with ID {id}", name=name, id=user.id)

        # Trigger background task
        process_user_task.delay(user.id)

    return {"id": user.id, "name": name}


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=8000)
