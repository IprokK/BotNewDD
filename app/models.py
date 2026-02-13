"""SQLAlchemy models for multi-event quest platform."""
from datetime import datetime
from enum import Enum as PyEnum

from sqlalchemy import DateTime, Float, ForeignKey, Integer, String, Text, UniqueConstraint
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column, relationship
from sqlalchemy.sql import func

from app.database import Base


from sqlalchemy import JSON


# --- Enums ---
class TeamState(str, PyEnum):
    FREE_ROAM = "free_roam"
    ASSIGNED = "assigned"
    IN_VISIT = "in_visit"
    FINISHED = "finished"


class PlayerRole(str, PyEnum):
    ROLE_A = "ROLE_A"
    ROLE_B = "ROLE_B"


class VisitState(str, PyEnum):
    ARRIVED = "arrived"
    STARTED = "started"
    FINISHED = "finished"


class ContentAudience(str, PyEnum):
    TEAM = "TEAM"
    ROLE_A = "ROLE_A"
    ROLE_B = "ROLE_B"
    PLAYER = "PLAYER"


class DialogueType(str, PyEnum):
    LEAKED = "LEAKED"
    INTERACTIVE = "INTERACTIVE"


class UserRole(str, PyEnum):
    PLAYER = "PLAYER"
    STATION_HOST = "STATION_HOST"
    ADMIN = "ADMIN"
    SUPERADMIN = "SUPERADMIN"


# --- Models ---
class Event(Base):
    __tablename__ = "events"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    slug: Mapped[str] = mapped_column(String(100), unique=True, nullable=False, index=True)
    starts_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=True)
    ends_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=True)
    config: Mapped[dict] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    teams = relationship("Team", back_populates="event")
    players = relationship("Player", back_populates="event")
    stations = relationship("Station", back_populates="event")
    station_hosts = relationship("StationHost", back_populates="event")
    content_blocks = relationship("ContentBlock", back_populates="event")
    dialogue_threads = relationship("DialogueThread", back_populates="event")
    event_logs = relationship("EventLog", back_populates="event")
    registration_forms = relationship("RegistrationForm", back_populates="event")
    scan_codes = relationship("ScanCode", back_populates="event")


class Team(Base):
    __tablename__ = "teams"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    event_id: Mapped[int] = mapped_column(ForeignKey("events.id", ondelete="CASCADE"), nullable=False)
    name: Mapped[str] = mapped_column(String(100), nullable=False)
    status: Mapped[str] = mapped_column(String(50), default="active")
    score_total: Mapped[float] = mapped_column(Float, default=0)
    current_state: Mapped[str] = mapped_column(
        String(20), default=TeamState.FREE_ROAM.value, nullable=False
    )
    current_station_id: Mapped[int | None] = mapped_column(
        ForeignKey("stations.id", ondelete="SET NULL"), nullable=True
    )
    team_progress: Mapped[dict] = mapped_column(JSON, default=dict)
    qr_token: Mapped[str | None] = mapped_column(String(255), unique=True, nullable=True, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    __table_args__ = (UniqueConstraint("event_id", "name", name="uq_teams_event_name"),)

    event = relationship("Event", back_populates="teams")
    current_station = relationship("Station", back_populates="teams_at_station", foreign_keys=[current_station_id])
    players = relationship("Player", back_populates="team")
    station_visits = relationship("StationVisit", back_populates="team")
    team_chat_messages = relationship("TeamChatMessage", back_populates="team", order_by="TeamChatMessage.created_at")


class Player(Base):
    __tablename__ = "players"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    event_id: Mapped[int] = mapped_column(ForeignKey("events.id", ondelete="CASCADE"), nullable=False)
    tg_id: Mapped[int] = mapped_column(Integer, nullable=False, index=True)
    team_id: Mapped[int | None] = mapped_column(ForeignKey("teams.id", ondelete="SET NULL"), nullable=True)
    role: Mapped[str | None] = mapped_column(String(20), nullable=True)  # ROLE_A, ROLE_B
    player_progress: Mapped[dict] = mapped_column(JSON, default=dict)
    flags: Mapped[dict] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    __table_args__ = (UniqueConstraint("event_id", "tg_id", name="uq_players_event_tg"),)

    event = relationship("Event", back_populates="players")
    team = relationship("Team", back_populates="players")
    ratings = relationship("Rating", back_populates="player")
    team_chat_sent = relationship("TeamChatMessage", back_populates="sender", foreign_keys="TeamChatMessage.sender_player_id")


class TeamChatMessage(Base):
    """Сообщения между участниками команды (напарниками) — видны в админке."""
    __tablename__ = "team_chat_messages"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    event_id: Mapped[int] = mapped_column(ForeignKey("events.id", ondelete="CASCADE"), nullable=False)
    team_id: Mapped[int] = mapped_column(ForeignKey("teams.id", ondelete="CASCADE"), nullable=False)
    sender_player_id: Mapped[int] = mapped_column(ForeignKey("players.id", ondelete="CASCADE"), nullable=False)
    text: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    team = relationship("Team", back_populates="team_chat_messages")
    sender = relationship("Player", back_populates="team_chat_sent", foreign_keys=[sender_player_id])


class Station(Base):
    __tablename__ = "stations"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    event_id: Mapped[int] = mapped_column(ForeignKey("events.id", ondelete="CASCADE"), nullable=False)
    name: Mapped[str] = mapped_column(String(100), nullable=False)
    capacity: Mapped[int] = mapped_column(Integer, default=1)
    config: Mapped[dict] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    event = relationship("Event", back_populates="stations")
    teams_at_station = relationship("Team", back_populates="current_station", foreign_keys="Team.current_station_id")
    hosts = relationship("StationHost", back_populates="station")
    visits = relationship("StationVisit", back_populates="station")


class StationHost(Base):
    __tablename__ = "station_hosts"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    event_id: Mapped[int] = mapped_column(ForeignKey("events.id", ondelete="CASCADE"), nullable=False)
    tg_id: Mapped[int] = mapped_column(Integer, nullable=False, index=True)
    station_id: Mapped[int] = mapped_column(ForeignKey("stations.id", ondelete="CASCADE"), nullable=False)
    name: Mapped[str | None] = mapped_column(String(100), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    __table_args__ = (UniqueConstraint("event_id", "tg_id", name="uq_station_hosts_event_tg"),)

    event = relationship("Event", back_populates="station_hosts")
    station = relationship("Station", back_populates="hosts")


class StationVisit(Base):
    __tablename__ = "station_visits"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    event_id: Mapped[int] = mapped_column(ForeignKey("events.id", ondelete="CASCADE"), nullable=False)
    team_id: Mapped[int] = mapped_column(ForeignKey("teams.id", ondelete="CASCADE"), nullable=False)
    station_id: Mapped[int] = mapped_column(ForeignKey("stations.id", ondelete="CASCADE"), nullable=False)
    state: Mapped[str] = mapped_column(String(20), default=VisitState.ARRIVED.value, nullable=False)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    ended_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    points_awarded: Mapped[float] = mapped_column(Float, default=0)
    host_notes: Mapped[str | None] = mapped_column(Text, nullable=True)
    host_rating: Mapped[int | None] = mapped_column(Integer, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    team = relationship("Team", back_populates="station_visits")
    station = relationship("Station", back_populates="visits")
    ratings = relationship("Rating", back_populates="station_visit")


class ContentBlock(Base):
    __tablename__ = "content_blocks"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    event_id: Mapped[int] = mapped_column(ForeignKey("events.id", ondelete="CASCADE"), nullable=False)
    key: Mapped[str] = mapped_column(String(100), nullable=False, index=True)
    type: Mapped[str] = mapped_column(String(50), default="text")
    payload: Mapped[dict] = mapped_column(JSON, default=dict)
    audience: Mapped[str] = mapped_column(String(20), default=ContentAudience.TEAM.value)
    station_id: Mapped[int | None] = mapped_column(ForeignKey("stations.id", ondelete="SET NULL"), nullable=True)
    version: Mapped[int] = mapped_column(Integer, default=1)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    event = relationship("Event", back_populates="content_blocks")


class DialogueThread(Base):
    __tablename__ = "dialogue_threads"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    event_id: Mapped[int] = mapped_column(ForeignKey("events.id", ondelete="CASCADE"), nullable=False)
    key: Mapped[str] = mapped_column(String(100), nullable=False, index=True)
    type: Mapped[str] = mapped_column(String(20), default=DialogueType.LEAKED.value)
    title: Mapped[str] = mapped_column(String(255), default="")
    config: Mapped[dict] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    event = relationship("Event", back_populates="dialogue_threads")
    messages = relationship("DialogueMessage", back_populates="thread", order_by="DialogueMessage.order_index")


class DialogueMessage(Base):
    __tablename__ = "dialogue_messages"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    event_id: Mapped[int] = mapped_column(ForeignKey("events.id", ondelete="CASCADE"), nullable=False)
    thread_id: Mapped[int] = mapped_column(ForeignKey("dialogue_threads.id", ondelete="CASCADE"), nullable=False)
    audience: Mapped[str] = mapped_column(String(20), default=ContentAudience.TEAM.value)  # Кому: TEAM, ROLE_A, ROLE_B
    payload: Mapped[dict] = mapped_column(JSON, default=dict)  # {text, character, reply_options: [{text, next_message_id}]}
    order_index: Mapped[int] = mapped_column(Integer, default=0)
    gate_rules: Mapped[dict | None] = mapped_column(JSON, nullable=True)  # {condition_type, scheduled_at?, station_id?, after_message_id?}
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    thread = relationship("DialogueThread", back_populates="messages")


class DialogueScheduledDelivery(Base):
    """Отправка запланированного сообщения диалога (push в Telegram)."""
    __tablename__ = "dialogue_scheduled_deliveries"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    message_id: Mapped[int] = mapped_column(ForeignKey("dialogue_messages.id", ondelete="CASCADE"), nullable=False)
    team_id: Mapped[int] = mapped_column(ForeignKey("teams.id", ondelete="CASCADE"), nullable=False)
    delivered_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class TeamGroup(Base):
    """Группа команд (напр. «Волна 13:00»)."""
    __tablename__ = "team_groups"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    event_id: Mapped[int] = mapped_column(ForeignKey("events.id", ondelete="CASCADE"), nullable=False)
    name: Mapped[str] = mapped_column(String(100), nullable=False)
    team_ids: Mapped[list] = mapped_column(JSON, default=list)  # [1, 2, 3]


class DialogueStartConfig(Base):
    """Правило старта диалога: когда и для кого он становится доступен."""
    __tablename__ = "dialogue_start_configs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    event_id: Mapped[int] = mapped_column(ForeignKey("events.id", ondelete="CASCADE"), nullable=False)
    thread_id: Mapped[int] = mapped_column(ForeignKey("dialogue_threads.id", ondelete="CASCADE"), nullable=False)
    start_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)  # null = только вручную
    target_type: Mapped[str] = mapped_column(String(20), default="all")  # all | teams | group
    target_team_ids: Mapped[list] = mapped_column(JSON, default=list)  # для target_type=teams
    target_group_id: Mapped[int | None] = mapped_column(ForeignKey("team_groups.id", ondelete="SET NULL"), nullable=True)
    order_index: Mapped[int] = mapped_column(Integer, default=0)  # последовательность
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class DialogueThreadUnlock(Base):
    """Факт разблокировки диалога для команды (появился в мини-аппе, уведомление отправлено)."""
    __tablename__ = "dialogue_thread_unlocks"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    thread_id: Mapped[int] = mapped_column(ForeignKey("dialogue_threads.id", ondelete="CASCADE"), nullable=False)
    team_id: Mapped[int] = mapped_column(ForeignKey("teams.id", ondelete="CASCADE"), nullable=False)
    unlocked_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class DialogueReply(Base):
    """Ответ участника на сообщение (для ветвления и условий)."""
    __tablename__ = "dialogue_replies"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    event_id: Mapped[int] = mapped_column(ForeignKey("events.id", ondelete="CASCADE"), nullable=False)
    player_id: Mapped[int] = mapped_column(ForeignKey("players.id", ondelete="CASCADE"), nullable=False)
    message_id: Mapped[int] = mapped_column(ForeignKey("dialogue_messages.id", ondelete="CASCADE"), nullable=False)
    reply_text: Mapped[str] = mapped_column(String(500), nullable=False)
    next_message_id: Mapped[int | None] = mapped_column(ForeignKey("dialogue_messages.id", ondelete="SET NULL"), nullable=True)
    replied_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class Delivery(Base):
    __tablename__ = "deliveries"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    event_id: Mapped[int] = mapped_column(ForeignKey("events.id", ondelete="CASCADE"), nullable=False)
    content_block_id: Mapped[int] = mapped_column(
        ForeignKey("content_blocks.id", ondelete="CASCADE"), nullable=False
    )
    team_id: Mapped[int | None] = mapped_column(ForeignKey("teams.id", ondelete="CASCADE"), nullable=True)
    player_id: Mapped[int | None] = mapped_column(ForeignKey("players.id", ondelete="CASCADE"), nullable=True)
    delivered_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    seen_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class Rating(Base):
    __tablename__ = "ratings"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    event_id: Mapped[int] = mapped_column(ForeignKey("events.id", ondelete="CASCADE"), nullable=False)
    station_visit_id: Mapped[int] = mapped_column(
        ForeignKey("station_visits.id", ondelete="CASCADE"), nullable=False
    )
    player_id: Mapped[int] = mapped_column(ForeignKey("players.id", ondelete="CASCADE"), nullable=False)
    station_rating: Mapped[int] = mapped_column(Integer, nullable=False)
    host_rating: Mapped[int] = mapped_column(Integer, nullable=False)
    comment: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    station_visit = relationship("StationVisit", back_populates="ratings")
    player = relationship("Player", back_populates="ratings")


class EventLog(Base):
    __tablename__ = "event_log"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    event_id: Mapped[int] = mapped_column(ForeignKey("events.id", ondelete="CASCADE"), nullable=False)
    team_id: Mapped[int | None] = mapped_column(ForeignKey("teams.id", ondelete="SET NULL"), nullable=True)
    player_id: Mapped[int | None] = mapped_column(ForeignKey("players.id", ondelete="SET NULL"), nullable=True)
    event_type: Mapped[str] = mapped_column(String(50), nullable=False, index=True)
    data: Mapped[dict] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    event = relationship("Event", back_populates="event_logs")


class RegistrationForm(Base):
    """Анкета регистрации участника квеста (заполняется в боте)."""
    __tablename__ = "registration_forms"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    event_id: Mapped[int] = mapped_column(ForeignKey("events.id", ondelete="CASCADE"), nullable=False)
    tg_id: Mapped[int] = mapped_column(Integer, nullable=False, index=True)
    full_name: Mapped[str] = mapped_column(String(255), nullable=False)
    university: Mapped[str] = mapped_column(String(100), nullable=False)  # ИТМО, СПбГУ, Политех, Другое
    university_other: Mapped[str | None] = mapped_column(String(255), nullable=True)  # если Другое
    course_status: Mapped[str] = mapped_column(String(50), nullable=False)
    participation_format: Mapped[str] = mapped_column(String(50), nullable=False)  # Один / Есть пара
    partner_name: Mapped[str | None] = mapped_column(String(255), nullable=True)
    isu_number: Mapped[str | None] = mapped_column(String(50), nullable=True)
    interests: Mapped[str | None] = mapped_column(Text, nullable=True)
    music_preferences: Mapped[str | None] = mapped_column(Text, nullable=True)
    films_games: Mapped[str | None] = mapped_column(Text, nullable=True)
    character_type: Mapped[str | None] = mapped_column(String(255), nullable=True)
    comment: Mapped[str | None] = mapped_column(Text, nullable=True)
    photo_file_id: Mapped[str | None] = mapped_column(String(255), nullable=True)  # Telegram file_id
    wave_preference: Mapped[str | None] = mapped_column(String(100), nullable=True)  # 13:00, 15:00, 17:00, В перерывах между парами
    privacy_consent: Mapped[bool] = mapped_column(nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    __table_args__ = (UniqueConstraint("event_id", "tg_id", name="uq_registration_forms_event_tg"),)

    event = relationship("Event", back_populates="registration_forms")


class ScanCode(Base):
    """QR-коды, при сканировании которых игрок получает предмет в инвентарь."""
    __tablename__ = "scan_codes"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    event_id: Mapped[int] = mapped_column(ForeignKey("events.id", ondelete="CASCADE"), nullable=False)
    code: Mapped[str] = mapped_column(String(128), nullable=False, index=True)
    item_key: Mapped[str] = mapped_column(String(64), nullable=False)
    name: Mapped[str | None] = mapped_column(String(255), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    __table_args__ = (UniqueConstraint("event_id", "code", name="uq_scan_codes_event_code"),)

    event = relationship("Event", back_populates="scan_codes")


class EventUser(Base):
    """Maps tg_id + event_id to role (PLAYER, STATION_HOST, ADMIN, SUPERADMIN)."""
    __tablename__ = "event_users"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    tg_id: Mapped[int] = mapped_column(Integer, nullable=False, index=True)
    event_id: Mapped[int] = mapped_column(ForeignKey("events.id", ondelete="CASCADE"), nullable=False)
    role: Mapped[str] = mapped_column(String(20), nullable=False)
    station_id: Mapped[int | None] = mapped_column(
        ForeignKey("stations.id", ondelete="SET NULL"), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    __table_args__ = (UniqueConstraint("tg_id", "event_id", name="uq_event_users_tg_event"),)
