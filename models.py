from sqlalchemy import create_engine, Column, Integer, String, Boolean, DateTime, BigInteger
from sqlalchemy.orm import declarative_base, sessionmaker
import datetime
import os

Base = declarative_base()


class Video(Base):
    __tablename__ = "videos"
    id = Column(Integer, primary_key=True)
    title = Column(String)
    file_id = Column(String)


class Unlock(Base):
    __tablename__ = "unlocks"
    id = Column(Integer, primary_key=True)
    user_id = Column(BigInteger, index=True)
    video_id = Column(Integer, index=True)
    ad_watched = Column(Boolean, default=False)
    unlocked_at = Column(DateTime, nullable=True)


# DATABASE_URL env var lets you swap SQLite for Postgres later without code changes.
DB_URL = os.environ.get("DATABASE_URL", "sqlite:///bot.db")
# Render's Postgres URLs start with "postgres://", but SQLAlchemy 1.4+ requires
# "postgresql://" — rewrite it so this works without manual editing.
if DB_URL.startswith("postgres://"):
    DB_URL = DB_URL.replace("postgres://", "postgresql://", 1)
engine = create_engine(DB_URL)
Base.metadata.create_all(engine)
Session = sessionmaker(bind=engine)
