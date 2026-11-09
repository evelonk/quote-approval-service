from contextlib import asynccontextmanager
from uuid import UUID

from fastapi import Body, Depends, FastAPI, HTTPException
from fastapi.security import HTTPBasic, HTTPBasicCredentials
from sqlalchemy import select, text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from app.database import make_engine
from app.models import (ClientOrganization, HistoryEvent, Product, Quote, QuoteVersion, User)
from app.schemas import DraftInput, DraftOutput, VersionInput, LeadInput, DecisionInput, Input
from app.security import DUMMY_HASH, password_hasher
from app import workflow as w


def create_app(database_url=None):
    engine = make_engine(database_url)

    @asynccontextmanager
    async def lifespan(_):
        yield
        engine.dispose()

    app = FastAPI(title="Quote Approval Service", version="0.2.0", lifespan=lifespan)
    basic = HTTPBasic()

    def get_session():
        with Session(engine) as session:
            yield session

    def get_user(credentials: HTTPBasicCredentials = Depends(basic),
                 session: Session = Depends(get_session)):
        user = session.scalar(select(User).where(User.username == credentials.username))
        valid = password_hasher.verify(credentials.password, user.password_hash if user else DUMMY_HASH)
        if not valid or user is None:
            raise HTTPException(401, "Invalid credentials", headers={"WWW-Authenticate": "Basic"})
        return user

    def get_manager(user: User = Depends(get_user)):
        w.role(user, "manager")
        return user

    def finish(session, version, user):
        session.commit()
        return w.version_output(session, version, user)

    @app.get("/health")
    def health(session: Session = Depends(get_session)):
        try:
            session.execute(text("SELECT 1"))
        except SQLAlchemyError:
            raise HTTPException(503, "Database unavailable") from None
        return {"status": "ok"}

    @app.get("/catalog")
    def catalog(user: User = Depends(get_user), session: Session = Depends(get_session)):
        w.role(user, "manager", "lead")
        return [dict(id=p.id, name=p.name, price=str(p.price))
                for p in session.scalars(select(Product).order_by(Product.id))]

    @app.post("/quotes", status_code=201, response_model=DraftOutput)
    def create_draft(data: DraftInput, user: User = Depends(get_manager),
                     session: Session = Depends(get_session)):
        w.organization(session, data.client_org_id, user)
        quote = Quote(owner_id=user.id, client_org_id=data.client_org_id, comment=data.comment)
        session.add(quote)
        session.flush()
        w.new_version(session, quote, data.client_org_id, data.comment, 1, user)
        session.commit()
        session.refresh(quote)
        return quote

    @app.get("/quotes/{quote_id}", response_model=DraftOutput)
    def read_draft(quote_id: UUID, user: User = Depends(get_manager),
                   session: Session = Depends(get_session)):
        quote = w.lock_quote(session, quote_id, user)
        version = session.scalar(select(QuoteVersion).where(QuoteVersion.quote_id == quote.id)
                                 .order_by(QuoteVersion.number.desc()).limit(1).with_for_update())
        if version is None or not w.visible(session, version, user):
            raise HTTPException(404, "Quote not found")
        w.expire(session, version)
        session.commit()
        return dict(id=quote.id, owner_id=quote.owner_id, client_org_id=version.client_org_id,
                    comment=version.comment, status=version.status, created_at=quote.created_at)

    @app.get("/versions")
    def list_versions(user: User = Depends(get_user), session: Session = Depends(get_session)):
        # Establish one global lock order even when concurrent lists overlap.
        pairs = session.execute(select(QuoteVersion.quote_id, QuoteVersion.id)
                                .order_by(QuoteVersion.quote_id, QuoteVersion.number)).all()
        result = []
        for quote_id, version_id in pairs:
            version = session.get(QuoteVersion, version_id)
            if not w.visible(session, version, user):
                continue
            version = w.lock_version(session, quote_id, version_id, user)
            w.expire(session, version)
            result.append(w.version_output(session, version, user))
        session.commit()
        return result

    @app.get("/quotes/{quote_id}/versions")
    def quote_versions(quote_id: UUID, user: User = Depends(get_user),
                       session: Session = Depends(get_session)):
        w.lock_quote(session, quote_id, user)
        versions = session.scalars(select(QuoteVersion).where(QuoteVersion.quote_id == str(quote_id))
                                   .order_by(QuoteVersion.number).with_for_update()).all()
        result = []
        for version in versions:
            if w.visible(session, version, user):
                w.expire(session, version)
                result.append(w.version_output(session, version, user))
        if not result:
            raise HTTPException(404, "Quote not found")
        session.commit()
        return result

    @app.post("/quotes/{quote_id}/versions", status_code=201)
    def next_version(quote_id: UUID, data: Input = Body(default=Input()),
                     user: User = Depends(get_manager), session: Session = Depends(get_session)):
        quote = w.lock_quote(session, quote_id, user)
        previous = session.scalar(select(QuoteVersion).where(QuoteVersion.quote_id == quote.id)
                                  .order_by(QuoteVersion.number.desc()).limit(1).with_for_update())
        if previous is None or not w.visible(session, previous, user):
            raise HTTPException(404, "Version not found")
        w.expire(session, previous)
        unsent_deadline_passed = (previous.status in ("ready", "pending")
                                  and w.utc(previous.expires_at) <= w.now(session))
        if previous.status not in ("returned", "declined", "expired") and not unsent_deadline_passed:
            raise HTTPException(409, "Finish the current version before creating another")
        version = w.new_version(session, quote, previous.client_org_id, "", previous.number + 1, user)
        return finish(session, version, user)

    @app.get("/quotes/{quote_id}/versions/{version_id}")
    def read_version(quote_id: UUID, version_id: UUID, user: User = Depends(get_user),
                     session: Session = Depends(get_session)):
        version = w.lock_version(session, quote_id, version_id, user)
        w.expire(session, version)
        return finish(session, version, user)

    @app.put("/quotes/{quote_id}/versions/{version_id}")
    def edit_version(quote_id: UUID, version_id: UUID, data: VersionInput,
                     user: User = Depends(get_manager), session: Session = Depends(get_session)):
        version = w.lock_version(session, quote_id, version_id, user)
        w.set_terms(session, version, data, user)
        return finish(session, version, user)

    @app.post("/quotes/{quote_id}/versions/{version_id}/finalize")
    def finalize(quote_id: UUID, version_id: UUID, data: Input = Body(default=Input()),
                 user: User = Depends(get_manager), session: Session = Depends(get_session)):
        version = w.lock_version(session, quote_id, version_id, user)
        w.finalize(session, version, user)
        return finish(session, version, user)

    @app.post("/quotes/{quote_id}/versions/{version_id}/approval")
    def approval(quote_id: UUID, version_id: UUID, data: LeadInput,
                 user: User = Depends(get_user), session: Session = Depends(get_session)):
        w.role(user, "lead")
        version = w.lock_version(session, quote_id, version_id, user)
        w.lead_decide(session, version, data, user)
        return finish(session, version, user)

    @app.post("/quotes/{quote_id}/versions/{version_id}/send")
    def send(quote_id: UUID, version_id: UUID, data: Input = Body(default=Input()),
             user: User = Depends(get_manager), session: Session = Depends(get_session)):
        version = w.lock_version(session, quote_id, version_id, user)
        w.send(session, version, user)
        return finish(session, version, user)

    @app.post("/quotes/{quote_id}/versions/{version_id}/decision")
    def decide(quote_id: UUID, version_id: UUID, data: DecisionInput,
               user: User = Depends(get_user), session: Session = Depends(get_session)):
        w.role(user, "client")
        version = w.lock_version(session, quote_id, version_id, user)
        w.client_decide(session, version, data, user)
        return finish(session, version, user)

    @app.get("/quotes/{quote_id}/versions/{version_id}/history")
    def history(quote_id: UUID, version_id: UUID, user: User = Depends(get_user),
                session: Session = Depends(get_session)):
        version = w.lock_version(session, quote_id, version_id, user)
        w.expire(session, version)
        session.flush()
        rows = session.scalars(select(HistoryEvent).where(HistoryEvent.version_id == version.id)
                               .order_by(HistoryEvent.id)).all()
        # Clients never receive staff events, internal comments or approval metadata.
        result = [dict(id=e.id, action=e.action, status=e.status,
                       actor_id=e.actor_id, created_at=w.utc(e.created_at)) for e in rows
                  if user.role != "client" or e.action in ("sent", "accepted", "declined", "expired")]
        session.commit()
        return result

    return app
