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
    # What kind of Telegram file file_id refers to: "video", "photo",
    # "document" (zips, pdfs, etc.), "audio", "voice", or "animation".
    # Decides which send_* method delivers it — see send_stored_file in app.py.
    file_type = Column(String, default="video")


class VideoFile(Base):
    """
    Extra files belonging to a Video entry, beyond its first one. A gallery
    entry (one Video row = one id, one thumbnail, one ad-unlock) can hold any
    number of files of any type; Video.file_id / Video.file_type hold the
    FIRST file and rows here hold the rest, in `position` order. Everything
    is delivered together once the ads are watched. Entries with no rows
    here are just single-file entries, exactly as before.
    """
    __tablename__ = "video_files"
    id = Column(Integer, primary_key=True)
    video_id = Column(Integer, index=True)
    file_id = Column(String)
    file_type = Column(String, default="video")
    position = Column(Integer, default=0)


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


class ScheduledDeletion(Base):
    """A message queued to be auto-deleted at delete_at. Stored in the DB
    (rather than kept only in memory) so a redeploy/restart doesn't silently
    lose a pending deletion — see schedule_delete() and run_deletion_sweep()
    in app.py."""
    __tablename__ = "scheduled_deletions"
    id = Column(Integer, primary_key=True)
    chat_id = Column(BigInteger)
    message_id = Column(BigInteger)
    delete_at = Column(DateTime)


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

# create_all only creates missing TABLES — it never adds a new column to a
# table that already exists. So for databases created before file_type was
# added, add it here (existing rows default to "video", which is what they
# all were). Safe to run on every startup: it's a no-op once the column exists.
from sqlalchemy import inspect, text
if "file_type" not in [c["name"] for c in inspect(engine).get_columns("videos")]:
    with engine.begin() as conn:
        conn.execute(text("ALTER TABLE videos ADD COLUMN file_type VARCHAR DEFAULT 'video'"))

Session = sessionmaker(bind=engine)
