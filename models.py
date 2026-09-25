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
    caption = Column(String, nullable=True)
    thumbnail_file_id = Column(String, nullable=True)


class Unlock(Base):
    __tablename__ = "unlocks"
    id = Column(Integer, primary_key=True)
    user_id = Column(BigInteger, index=True)
    video_id = Column(Integer, index=True)
    ad_watched = Column(Boolean, default=False)
    unlocked_at = Column(DateTime, nullable=True)


class Channel(Base):
    """Channels the bot auto-posts new videos to. Managed via /addchannel,
    /listchannels, /removechannel — no redeploy needed to add or remove one."""
    __tablename__ = "channels"
    id = Column(Integer, primary_key=True)
    chat_id = Column(String)  # e.g. "-1001234567890" or "@channelhandle"
    title = Column(String, nullable=True)


class Setting(Base):
    """Small key-value store for admin-configurable settings (e.g. the hub
    channel link) that shouldn't require an env var + redeploy to change."""
    __tablename__ = "settings"
    key = Column(String, primary_key=True)
    value = Column(String)


# DATABASE_URL env var lets you swap SQLite for Postgres later without code changes.
DB_URL = os.environ.get("DATABASE_URL", "sqlite:///bot.db")
# Render's Postgres URLs start with "postgres://", and older SQLAlchemy setups
# default to the psycopg2 driver — but we're using psycopg (v3) instead, since
# it has reliable prebuilt wheels on newer Python versions. Rewrite explicitly
# so the right driver is always used regardless of what Render provides.
if DB_URL.startswith("postgres://"):
    DB_URL = DB_URL.replace("postgres://", "postgresql+psycopg://", 1)
elif DB_URL.startswith("postgresql://"):
    DB_URL = DB_URL.replace("postgresql://", "postgresql+psycopg://", 1)
engine = create_engine(DB_URL)
Base.metadata.create_all(engine)
Session = sessionmaker(bind=engine)
