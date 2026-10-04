from contextlib import asynccontextmanager
from datetime import datetime
from uuid import UUID

from fastapi import Depends, FastAPI, HTTPException
from fastapi.security import HTTPBasic, HTTPBasicCredentials
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select, text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from app.database import make_engine
from app.models import ClientOrganization, Quote, User
from app.security import DUMMY_HASH, password_hasher


class DraftInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    client_org_id: int = Field(gt=0, strict=True)
    comment: str = Field(default="", max_length=1000)


class DraftOutput(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: UUID
    owner_id: int
    client_org_id: int
    comment: str
    status: str
    created_at: datetime


def create_app(database_url=None):
    engine = make_engine(database_url)

    @asynccontextmanager
    async def lifespan(_):
        yield
        engine.dispose()

    app = FastAPI(title="Quote Approval Service", version="0.1.0", lifespan=lifespan)
    basic = HTTPBasic()

    def get_session():
        with Session(engine) as session:
            yield session

    def get_user(credentials: HTTPBasicCredentials = Depends(basic),
                 session: Session = Depends(get_session)):
        user = session.scalar(select(User).where(User.username == credentials.username))
        valid = password_hasher.verify(
            credentials.password, user.password_hash if user else DUMMY_HASH
        )
        if not valid or user is None:
            raise HTTPException(401, "Invalid credentials", headers={"WWW-Authenticate": "Basic"})
        return user

    def get_manager(user: User = Depends(get_user)):
        if user.role != "manager":
            raise HTTPException(403, "Only managers can access drafts")
        return user

    @app.get("/health")
    def health(session: Session = Depends(get_session)):
        try:
            session.execute(text("SELECT 1"))
        except SQLAlchemyError:
            raise HTTPException(503, "Database unavailable") from None
        return {"status": "ok"}

    @app.post("/quotes", status_code=201, response_model=DraftOutput)
    def create_draft(data: DraftInput, user: User = Depends(get_manager),
                     session: Session = Depends(get_session)):
        organization = session.scalar(select(ClientOrganization).where(
            ClientOrganization.id == data.client_org_id,
            ClientOrganization.manager_id == user.id,
        ))
        if organization is None:
            raise HTTPException(404, "Client organization not found")
        quote = Quote(owner_id=user.id, client_org_id=organization.id, comment=data.comment)
        session.add(quote)
        session.commit()
        session.refresh(quote)
        return quote

    @app.get("/quotes/{quote_id}", response_model=DraftOutput)
    def read_draft(quote_id: UUID, user: User = Depends(get_manager),
                   session: Session = Depends(get_session)):
        quote = session.scalar(select(Quote).join(
            ClientOrganization, Quote.client_org_id == ClientOrganization.id
        ).where(Quote.id == str(quote_id), Quote.owner_id == user.id,
                ClientOrganization.manager_id == user.id))
        if quote is None:
            raise HTTPException(404, "Quote not found")
        return quote

    return app
