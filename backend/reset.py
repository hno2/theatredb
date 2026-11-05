from backend.user_management import get_session, _get_engine
from sqlmodel import Session, SQLModel

engine = _get_engine()

SQLModel.metadata.drop_all(engine)
