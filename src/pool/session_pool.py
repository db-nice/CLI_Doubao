import json
import os
import random
import re
import time
import hashlib
from typing import Any

from loguru import logger
from pydantic import BaseModel

from .fetcher import DoubaoAutomator


DEFAULT_AUTH_SESSION_FILE = os.getenv("DOUBAO_AUTH_SESSION_FILE", "session.json")
DEFAULT_GUEST_SESSION_FILE = os.getenv("DOUBAO_GUEST_SESSION_FILE", "guest_session.json")
DEFAULT_GUEST_SESSION_BACKUP_DIR = os.getenv("DOUBAO_GUEST_SESSION_BACKUP_DIR", "guest_session_backups")
GENERATED_GUEST_CAPTURE_CACHE_SOURCE = "runtime_capture"
TEMP_CONVERSATION_ID_PATTERN = re.compile(r"^(local|load)_", re.IGNORECASE)


def _get_guest_session_backup_dir() -> str:
    return str(
        os.getenv("DOUBAO_GUEST_SESSION_BACKUP_DIR", DEFAULT_GUEST_SESSION_BACKUP_DIR)
        or DEFAULT_GUEST_SESSION_BACKUP_DIR
    ).strip() or "guest_session_backups"


def _sanitize_guest_capture_cache(value: dict[str, Any] | None) -> dict[str, Any] | None:
    if not isinstance(value, dict):
        return None
    if str(value.get("generated_by") or "").strip() != GENERATED_GUEST_CAPTURE_CACHE_SOURCE:
        return None
    return value


def _read_json_file(path: str) -> dict[str, Any] | list[Any] | None:
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return None


def _build_guest_snapshot_id(identity: tuple[str, str, str, str] | None, *, source: str) -> str:
    if identity is None:
        return f"{source}:unknown"
    joined = "|".join(str(part or "") for part in identity)
    digest = hashlib.sha1(joined.encode("utf-8")).hexdigest()[:16]
    return f"{source}:{digest}"


def _normalize_stable_conversation_id(value: Any) -> str:
    normalized = str(value or "").strip()
    if not normalized or normalized == "0":
        return ""
    if TEMP_CONVERSATION_ID_PATTERN.match(normalized):
        return ""
    return normalized


def _normalize_conversation_ids(values: list[Any] | None) -> list[str]:
    seen: set[str] = set()
    normalized_ids: list[str] = []
    for value in values or []:
        normalized = _normalize_stable_conversation_id(value)
        if not normalized or normalized in seen:
            continue
        seen.add(normalized)
        normalized_ids.append(normalized)
    return normalized_ids


def _build_guest_snapshot_semantic_key(snapshot: dict[str, Any] | None) -> str:
    payload = snapshot if isinstance(snapshot, dict) else {}
    source = str(payload.get("source") or "").strip().lower()
    room_id = _normalize_stable_conversation_id(payload.get("room_id"))
    reason = str(payload.get("reason") or "").strip().lower() if source == "backup" else ""
    conversation_ids = ",".join(_normalize_conversation_ids(payload.get("conversation_ids") or []))
    return "|".join([source, room_id, conversation_ids, reason])


def _build_guest_pool_key(identity: tuple[str, str, str, str] | None) -> str:
    if identity is None:
        return "guest_pool:unknown"
    joined = "|".join(str(part or "") for part in identity)
    digest = hashlib.sha1(joined.encode("utf-8")).hexdigest()[:16]
    return f"guest_pool:{digest}"


class DoubaoSession(BaseModel):
    """Doubao API session config."""

    aid: str
    real_aid: str | None = None
    web_tab_id: str | None = None
    bot_id: str | None = None
    fp: str | None = None
    ms_token: str | None = None
    a_bogus: str | None = None
    cookie: str
    device_id: str
    tea_uuid: str
    web_id: str
    room_id: str
    x_flow_trace: str
    guest_capture_cache: dict[str, Any] | None = None

    @property
    def resolved_real_aid(self) -> str:
        return self.real_aid or self.aid

    def to_dict(self) -> dict[str, str]:
        guest_capture_cache = _sanitize_guest_capture_cache(self.guest_capture_cache)
        normalized_room_id = _normalize_stable_conversation_id(self.room_id) or "0"
        data = {
            "aid": self.aid,
            "cookie": self.cookie,
            "device_id": self.device_id,
            "tea_uuid": self.tea_uuid,
            "web_id": self.web_id,
            "room_id": normalized_room_id,
            "x_flow_trace": self.x_flow_trace,
        }
        if self.real_aid and self.real_aid != self.aid:
            data["real_aid"] = self.real_aid
        if self.web_tab_id:
            data["web_tab_id"] = self.web_tab_id
        if self.bot_id:
            data["bot_id"] = self.bot_id
        if self.fp:
            data["fp"] = self.fp
        if self.ms_token:
            data["ms_token"] = self.ms_token
        if guest_capture_cache:
            data["guest_capture_cache"] = guest_capture_cache
        return data

    @classmethod
    def from_dict(cls, data: dict[str, str]) -> "DoubaoSession":
        normalized = dict(data)
        normalized["guest_capture_cache"] = _sanitize_guest_capture_cache(
            normalized.get("guest_capture_cache")
        )
        return cls(**normalized)


class SessionPool:
    """Manage loaded Doubao sessions."""

    def __init__(self, config_file: str | None = None, guest_config_file: str | None = None):
        self.session_map: dict[str, DoubaoSession] = {}
        self.conversation_state_map: dict[str, dict[str, str | int]] = {}
        self.auth_sessions: list[DoubaoSession] = []
        self.guest_sessions: list[DoubaoSession] = []
        self.auth_config_file = str(config_file or DEFAULT_AUTH_SESSION_FILE)
        self.guest_config_file = str(guest_config_file or DEFAULT_GUEST_SESSION_FILE)
        self.config_file = self.auth_config_file
        self.load_from_file()

    def create_session(
        self,
        guest: bool,
        aid: str,
        cookie: str,
        device_id: str,
        tea_uuid: str,
        web_id: str,
        room_id: str,
        x_flow_trace: str,
        real_aid: str | None = None,
        web_tab_id: str | None = None,
        bot_id: str | None = None,
        fp: str | None = None,
        ms_token: str | None = None,
        a_bogus: str | None = None,
        guest_capture_cache: dict[str, Any] | None = None,
    ) -> DoubaoSession:
        session = DoubaoSession(
            aid=aid,
            real_aid=real_aid,
            web_tab_id=web_tab_id,
            bot_id=bot_id,
            fp=fp,
            ms_token=ms_token,
            a_bogus=a_bogus,
            cookie=cookie,
            device_id=device_id,
            tea_uuid=tea_uuid,
            web_id=web_id,
            room_id=room_id,
            x_flow_trace=x_flow_trace,
            guest_capture_cache=_sanitize_guest_capture_cache(guest_capture_cache),
        )
        if guest:
            self.guest_sessions.append(session)
        else:
            self.auth_sessions.append(session)
        return session

    def get_session(self, conversation_id: str | None = None, guest: bool = False) -> DoubaoSession | None:
        if conversation_id is None:
            if guest:
                return self.guest_sessions[-1] if self.guest_sessions else None
            return random.choice(self.auth_sessions) if self.auth_sessions else None
        normalized_conversation_id = _normalize_stable_conversation_id(conversation_id)
        if not normalized_conversation_id:
            return None
        return self.session_map.get(normalized_conversation_id)

    def set_session(self, conversation_id: str, session: DoubaoSession):
        normalized_conversation_id = _normalize_stable_conversation_id(conversation_id)
        if not normalized_conversation_id:
            return
        self.session_map[normalized_conversation_id] = session

    @staticmethod
    def _session_identity(session: DoubaoSession | None) -> tuple[str, str, str, str] | None:
        if session is None:
            return None
        return (
            str(session.aid or ""),
            str(session.device_id or ""),
            str(session.web_id or ""),
            str(session.tea_uuid or ""),
        )

    @staticmethod
    def _guest_pool_identity(session: DoubaoSession | None) -> tuple[str, str] | None:
        if session is None:
            return None
        aid = str(getattr(session, "aid", "") or "").strip()
        device_id = str(getattr(session, "device_id", "") or "").strip()
        if not aid and not device_id:
            return None
        return aid, device_id

    def _guest_pool_key_for_session(self, session: DoubaoSession | None) -> str:
        identity = self._guest_pool_identity(session)
        return _build_guest_pool_key(identity) if identity is not None else "guest_pool:unknown"

    def _iter_guest_backup_records(self) -> list[dict[str, Any]]:
        directory = _get_guest_session_backup_dir()
        if not os.path.isdir(directory):
            return []

        records: list[dict[str, Any]] = []
        entries = sorted(
            (
                os.path.join(directory, name)
                for name in os.listdir(directory)
                if name.lower().endswith(".json")
            ),
            key=lambda item: os.path.getmtime(item),
            reverse=True,
        )
        for path in entries:
            payload = _read_json_file(path)
            if not isinstance(payload, dict):
                continue
            session_data = payload.get("session")
            if not isinstance(session_data, dict):
                continue
            try:
                session = DoubaoSession.from_dict(session_data)
            except Exception:
                continue
            metadata = payload.get("metadata") if isinstance(payload.get("metadata"), dict) else {}
            records.append(
                {
                    "path": path,
                    "payload": payload,
                    "session": session,
                    "pool_key": str(metadata.get("pool_key") or "").strip()
                    or self._guest_pool_key_for_session(session),
                }
            )
        return records

    def _find_guest_backup_records_for_session(self, session: DoubaoSession | None) -> list[dict[str, Any]]:
        target_pool_key = self._guest_pool_key_for_session(session)
        if not target_pool_key or target_pool_key == "guest_pool:unknown":
            return []
        return [
            item
            for item in self._iter_guest_backup_records()
            if str(item.get("pool_key") or "").strip() == target_pool_key
        ]

    def _write_guest_backup_payload(
        self,
        path: str,
        *,
        session: DoubaoSession,
        reason: str,
        conversation_ids: list[str],
        metadata: dict[str, Any] | None = None,
        previous_payload: dict[str, Any] | None = None,
    ) -> None:
        merged_metadata = {}
        if isinstance(previous_payload, dict) and isinstance(previous_payload.get("metadata"), dict):
            merged_metadata.update(previous_payload.get("metadata") or {})
        if isinstance(metadata, dict):
            merged_metadata.update(metadata)
        merged_metadata["pool_key"] = self._guest_pool_key_for_session(session)

        payload = {
            "reason": str(reason or "manual_reset"),
            "backup_at_ms": int(time.time() * 1000),
            "conversation_ids": _normalize_conversation_ids(conversation_ids),
            "session": session.to_dict(),
            "metadata": merged_metadata,
        }
        with open(path, "w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False, indent=4)

    def _cleanup_duplicate_guest_backups(
        self,
        records: list[dict[str, Any]],
        *,
        keep_path: str,
    ) -> None:
        normalized_keep_path = os.path.abspath(str(keep_path or ""))
        for record in records:
            path = os.path.abspath(str(record.get("path") or ""))
            if not path or path == normalized_keep_path:
                continue
            try:
                os.remove(path)
                logger.debug(f"Removed duplicate guest backup snapshot: {path}")
            except FileNotFoundError:
                continue
            except Exception as exc:
                logger.warning(f"Failed to remove duplicate guest backup snapshot {path}: {exc}")

    def _sessions_match(self, left: DoubaoSession | None, right: DoubaoSession | None) -> bool:
        return self._session_identity(left) == self._session_identity(right)

    def get_conversation_state(self, conversation_id: str | None) -> dict[str, str | int]:
        normalized_conversation_id = _normalize_stable_conversation_id(conversation_id)
        if not normalized_conversation_id:
            return {}
        return dict(self.conversation_state_map.get(normalized_conversation_id, {}))

    def update_conversation_state(
        self,
        conversation_id: str | None,
        *,
        section_id: str | None = None,
        latest_index: str | int | None = None,
        override: bool = False,
    ) -> dict[str, str | int]:
        normalized_conversation_id = _normalize_stable_conversation_id(conversation_id)
        if not normalized_conversation_id:
            return {}

        state = self.conversation_state_map.setdefault(normalized_conversation_id, {})

        if section_id not in (None, "", "0"):
            if override or state.get("section_id") in (None, "", "0"):
                state["section_id"] = str(section_id)

        if latest_index not in (None, ""):
            if override or state.get("latest_index") in (None, ""):
                state["latest_index"] = latest_index if isinstance(latest_index, int) else str(latest_index)

        return dict(state)

    def get_session_conversation_ids(self, session: DoubaoSession | None) -> list[str]:
        identity = self._session_identity(session)
        if identity is None:
            return []

        conversation_ids: list[str] = []
        seen: set[str] = set()

        def append_conversation_id(value: str | None):
            normalized = _normalize_stable_conversation_id(value)
            if not normalized or normalized in seen:
                return
            seen.add(normalized)
            conversation_ids.append(normalized)

        room_id = str(getattr(session, "room_id", "") or "").strip()
        append_conversation_id(room_id)

        for conversation_id, bound_session in self.session_map.items():
            if not conversation_id or conversation_id == "0":
                continue
            if self._sessions_match(session, bound_session):
                append_conversation_id(conversation_id)

        return conversation_ids

    def clear_session_bindings(
        self,
        session: DoubaoSession | None,
        *,
        clear_conversation_state: bool = True,
    ) -> list[str]:
        conversation_ids = self.get_session_conversation_ids(session)
        stale_keys = [
            conversation_id
            for conversation_id, bound_session in self.session_map.items()
            if self._sessions_match(session, bound_session)
        ]
        for conversation_id in stale_keys:
            self.session_map.pop(conversation_id, None)

        if clear_conversation_state:
            for conversation_id in conversation_ids:
                self.conversation_state_map.pop(conversation_id, None)

        return conversation_ids

    def upsert_auth_session(self, session: DoubaoSession) -> DoubaoSession:
        for existing in self.auth_sessions:
            if (
                existing.aid == session.aid
                and existing.device_id == session.device_id
                and existing.web_id == session.web_id
            ):
                for key, value in session.model_dump().items():
                    setattr(existing, key, value)
                return existing

        self.auth_sessions.append(session)
        return session

    def upsert_guest_session(self, session: DoubaoSession) -> DoubaoSession:
        for existing in self.guest_sessions:
            if self._sessions_match(existing, session):
                for key, value in session.model_dump().items():
                    setattr(existing, key, value)
                return existing

        self.guest_sessions.append(session)
        return session

    def remove_guest_session(self, session: DoubaoSession | None) -> bool:
        removed = False
        kept_sessions: list[DoubaoSession] = []
        for existing in self.guest_sessions:
            if self._sessions_match(existing, session):
                removed = True
                continue
            kept_sessions.append(existing)
        self.guest_sessions = kept_sessions
        return removed

    def backup_guest_session(
        self,
        session: DoubaoSession | None,
        *,
        reason: str,
        metadata: dict[str, Any] | None = None,
    ) -> tuple[str | None, list[str]]:
        if session is None:
            return None, []

        directory = _get_guest_session_backup_dir()
        os.makedirs(directory, exist_ok=True)
        conversation_ids = self.get_session_conversation_ids(session)
        existing_records = self._find_guest_backup_records_for_session(session)

        if existing_records:
            canonical_record = existing_records[0]
            backup_path = str(canonical_record.get("path") or "").strip()
            self._write_guest_backup_payload(
                backup_path,
                session=session,
                reason=reason,
                conversation_ids=conversation_ids,
                metadata=metadata,
                previous_payload=canonical_record.get("payload") if isinstance(canonical_record.get("payload"), dict) else None,
            )
            self._cleanup_duplicate_guest_backups(existing_records, keep_path=backup_path)
            logger.debug(f"Refreshed existing guest backup session at: {backup_path}")
            return backup_path, conversation_ids

        timestamp = time.strftime("%Y%m%d_%H%M%S", time.localtime())
        nonce = random.randint(1000, 9999)
        backup_path = os.path.join(directory, f"guest_session_backup_{timestamp}_{nonce}.json")
        self._write_guest_backup_payload(
            backup_path,
            session=session,
            reason=reason,
            conversation_ids=conversation_ids,
            metadata=metadata,
        )
        logger.debug(f"Backed up guest session to: {backup_path}")
        return backup_path, conversation_ids

    def refresh_guest_session_backup(
        self,
        session: DoubaoSession | None,
        *,
        reason: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> tuple[str | None, list[str]]:
        if session is None:
            return None, []

        existing_records = self._find_guest_backup_records_for_session(session)
        if not existing_records:
            return None, self.get_session_conversation_ids(session)

        canonical_record = existing_records[0]
        backup_path = str(canonical_record.get("path") or "").strip()
        previous_payload = canonical_record.get("payload") if isinstance(canonical_record.get("payload"), dict) else {}
        resolved_reason = str(reason or previous_payload.get("reason") or "manual_reset")
        conversation_ids = self.get_session_conversation_ids(session)
        self._write_guest_backup_payload(
            backup_path,
            session=session,
            reason=resolved_reason,
            conversation_ids=conversation_ids,
            metadata=metadata,
            previous_payload=previous_payload,
        )
        self._cleanup_duplicate_guest_backups(existing_records, keep_path=backup_path)
        logger.debug(f"Refreshed guest backup cache for pool at: {backup_path}")
        return backup_path, conversation_ids

    def build_guest_session_snapshot(
        self,
        session: DoubaoSession | None,
        *,
        source: str,
        active: bool = False,
        backup_path: str | None = None,
        backup_at_ms: int | None = None,
        reason: str | None = None,
        conversation_ids: list[str] | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> dict[str, Any] | None:
        if session is None:
            return None

        identity = self._session_identity(session)
        room_id = _normalize_stable_conversation_id(getattr(session, "room_id", None)) or None
        snapshot_id = (
            f"backup:{os.path.basename(backup_path)}"
            if source == "backup" and backup_path
            else _build_guest_snapshot_id(identity, source=source)
        )
        conversation_ids = _normalize_conversation_ids(
            list(conversation_ids or self.get_session_conversation_ids(session))
        )
        snapshot_room_id = room_id or (conversation_ids[0] if conversation_ids else None)
        label_suffix = snapshot_room_id[-6:] if snapshot_room_id else str(getattr(session, "device_id", "") or "")[-6:]
        label_prefix = "当前缓存" if source == "current" else "备份缓存"
        return {
            "snapshot_id": snapshot_id,
            "source": source,
            "active": bool(active),
            "label": f"{label_prefix} {label_suffix}" if label_suffix else label_prefix,
            "room_id": snapshot_room_id,
            "conversation_ids": conversation_ids,
            "conversation_count": len(conversation_ids),
            "backup_path": backup_path,
            "backup_at_ms": int(backup_at_ms) if backup_at_ms not in (None, "") else None,
            "reason": str(reason or "").strip() or None,
            "metadata": {
                **dict(metadata or {}),
                "pool_key": self._guest_pool_key_for_session(session),
            },
            "session": session.to_dict(),
        }

    def list_guest_session_backups(self, limit: int = 20) -> list[dict[str, Any]]:
        snapshot_items: list[dict[str, Any]] = []
        grouped_records: dict[str, dict[str, Any]] = {}
        for record in self._iter_guest_backup_records():
            pool_key = str(record.get("pool_key") or "").strip() or "guest_pool:unknown"
            if pool_key in grouped_records:
                continue
            grouped_records[pool_key] = record

        ordered_records = list(grouped_records.values())[: max(1, int(limit or 20))]
        for record in ordered_records:
            path = str(record.get("path") or "").strip()
            payload = record.get("payload") if isinstance(record.get("payload"), dict) else {}
            session = record.get("session") if isinstance(record.get("session"), DoubaoSession) else None
            if session is None:
                continue
            snapshot = self.build_guest_session_snapshot(
                session,
                source="backup",
                active=False,
                backup_path=path,
                backup_at_ms=payload.get("backup_at_ms"),
                reason=payload.get("reason"),
                conversation_ids=list(payload.get("conversation_ids") or []),
                metadata=payload.get("metadata") if isinstance(payload.get("metadata"), dict) else {},
            )
            if snapshot:
                snapshot_items.append(snapshot)
        return snapshot_items

    def list_guest_session_snapshots(self, limit_backups: int = 20) -> list[dict[str, Any]]:
        active_session = self.get_session(None, guest=True)
        active_identity = self._session_identity(active_session)
        snapshots: list[dict[str, Any]] = []

        for session in reversed(self.guest_sessions):
            identity = self._session_identity(session)
            snapshot = self.build_guest_session_snapshot(
                session,
                source="current",
                active=identity == active_identity,
            )
            if snapshot:
                snapshots.append(snapshot)

        snapshots.extend(self.list_guest_session_backups(limit=limit_backups))
        grouped_snapshots: dict[str, list[dict[str, Any]]] = {}
        group_order: list[str] = []

        def get_pool_key(snapshot: dict[str, Any]) -> str:
            metadata = snapshot.get("metadata") if isinstance(snapshot.get("metadata"), dict) else {}
            pool_key = str(metadata.get("pool_key") or "").strip()
            if pool_key:
                return pool_key
            session_data = snapshot.get("session") if isinstance(snapshot.get("session"), dict) else {}
            try:
                session = DoubaoSession.from_dict(session_data)
            except Exception:
                session = None
            return self._guest_pool_key_for_session(session)

        def choose_representative(group_items: list[dict[str, Any]]) -> dict[str, Any]:
            def rank(item: dict[str, Any]) -> tuple[int, int, int]:
                return (
                    0 if bool(item.get("active")) else 1,
                    0 if str(item.get("source") or "") == "current" else 1,
                    -int(item.get("backup_at_ms") or 0),
                )

            return sorted(group_items, key=rank)[0]

        for snapshot in snapshots:
            if not isinstance(snapshot, dict):
                continue
            normalized_snapshot = dict(snapshot)
            normalized_snapshot["conversation_ids"] = _normalize_conversation_ids(
                normalized_snapshot.get("conversation_ids") or []
            )
            normalized_snapshot["conversation_count"] = len(normalized_snapshot["conversation_ids"])
            pool_key = get_pool_key(normalized_snapshot)
            if pool_key not in grouped_snapshots:
                grouped_snapshots[pool_key] = []
                group_order.append(pool_key)
            grouped_snapshots[pool_key].append(normalized_snapshot)

        grouped_results: list[dict[str, Any]] = []
        for pool_key in group_order:
            items = grouped_snapshots.get(pool_key) or []
            if not items:
                continue
            representative = dict(choose_representative(items))
            representative_metadata = representative.get("metadata") if isinstance(representative.get("metadata"), dict) else {}
            representative["metadata"] = {
                **representative_metadata,
                "pool_key": pool_key,
                "group_size": len(items),
                "group_snapshot_ids": [str(item.get("snapshot_id") or "") for item in items if item.get("snapshot_id")],
            }
            grouped_results.append(representative)

        grouped_results.sort(
            key=lambda item: (
                0 if bool(item.get("active")) else 1,
                0 if str(item.get("source") or "") == "current" else 1,
                -int(item.get("backup_at_ms") or 0),
                str(item.get("room_id") or ""),
            )
        )
        return grouped_results

    def _get_guest_session_snapshot_exact(self, snapshot_id: str | None) -> dict[str, Any] | None:
        normalized_snapshot_id = str(snapshot_id or "").strip()
        if not normalized_snapshot_id:
            return None

        if normalized_snapshot_id.startswith("backup:"):
            filename = normalized_snapshot_id.split(":", 1)[1].strip()
            if not filename:
                return None
            backup_path = os.path.join(_get_guest_session_backup_dir(), filename)
            if not os.path.isfile(backup_path):
                return None
            payload = _read_json_file(backup_path)
            if not isinstance(payload, dict) or not isinstance(payload.get("session"), dict):
                return None
            try:
                session = DoubaoSession.from_dict(payload["session"])
            except Exception:
                return None
            return self.build_guest_session_snapshot(
                session,
                source="backup",
                active=False,
                backup_path=backup_path,
                backup_at_ms=payload.get("backup_at_ms"),
                reason=payload.get("reason"),
                conversation_ids=list(payload.get("conversation_ids") or []),
                metadata=payload.get("metadata") if isinstance(payload.get("metadata"), dict) else {},
            )

        for session in self.guest_sessions:
            current_snapshot = self.build_guest_session_snapshot(
                session,
                source="current",
                active=self._sessions_match(session, self.get_session(None, guest=True)),
            )
            if current_snapshot and current_snapshot.get("snapshot_id") == normalized_snapshot_id:
                return current_snapshot
        return None

    def _build_guest_snapshot_aliases(self, snapshot: dict[str, Any] | None) -> set[str]:
        payload = snapshot if isinstance(snapshot, dict) else {}
        aliases: set[str] = set()
        snapshot_id = str(payload.get("snapshot_id") or "").strip()
        if snapshot_id:
            aliases.add(snapshot_id)
        metadata = payload.get("metadata") if isinstance(payload.get("metadata"), dict) else {}
        pool_key = str(metadata.get("pool_key") or payload.get("pool_key") or "").strip()
        if pool_key:
            aliases.add(pool_key)
        for alias in metadata.get("group_snapshot_ids") or []:
            normalized_alias = str(alias or "").strip()
            if normalized_alias:
                aliases.add(normalized_alias)
        return aliases

    def get_guest_session_snapshot(self, snapshot_id: str | None) -> dict[str, Any] | None:
        normalized_snapshot_id = str(snapshot_id or "").strip()
        if not normalized_snapshot_id:
            return None

        exact_snapshot = self._get_guest_session_snapshot_exact(normalized_snapshot_id)
        if exact_snapshot is not None:
            return exact_snapshot

        grouped_snapshots = self.list_guest_session_snapshots(limit_backups=200)
        for snapshot in grouped_snapshots:
            if normalized_snapshot_id in self._build_guest_snapshot_aliases(snapshot):
                return snapshot
        return None

    def del_session(self, session: DoubaoSession):
        if session in self.auth_sessions:
            self.auth_sessions.remove(session)
            self.save_to_file(guest=False)
        elif session in self.guest_sessions:
            self.guest_sessions.remove(session)
            self.save_to_file(guest=True)

    def _save_sessions_to_path(self, path: str, sessions: list[DoubaoSession], label: str):
        directory = os.path.dirname(path)
        if directory:
            os.makedirs(directory, exist_ok=True)
        data: list[dict[str, Any]] = []
        include_guest_bindings = label == "guest"
        for session in sessions:
            payload = session.to_dict()
            if include_guest_bindings:
                conversation_ids = self.get_session_conversation_ids(session)
                if conversation_ids:
                    payload["conversation_ids"] = list(conversation_ids)
                conversation_state = {
                    conversation_id: dict(self.conversation_state_map.get(conversation_id) or {})
                    for conversation_id in conversation_ids
                    if isinstance(self.conversation_state_map.get(conversation_id), dict)
                }
                if conversation_state:
                    payload["conversation_state"] = conversation_state
            data.append(payload)
        with open(path, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=4)
        logger.debug(f"Saved {label} session config to: {path}")

    def save_to_file(self, guest: bool | None = None):
        try:
            if guest is None:
                self._save_sessions_to_path(self.auth_config_file, self.auth_sessions, "auth")
                self._save_sessions_to_path(self.guest_config_file, self.guest_sessions, "guest")
            elif guest:
                self._save_sessions_to_path(self.guest_config_file, self.guest_sessions, "guest")
            else:
                self._save_sessions_to_path(self.auth_config_file, self.auth_sessions, "auth")
        except Exception as exc:
            logger.error(f"Failed to save session config: {exc}")

    def _load_sessions_from_path(self, path: str, *, guest: bool, missing_level: str = "warning") -> int:
        if not os.path.exists(path):
            message = f"Session config file not found: {path}"
            if missing_level == "debug":
                logger.debug(message)
            else:
                logger.warning(message)
            return 0
        try:
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)

            for session_data in data:
                if not isinstance(session_data, dict):
                    continue
                raw_session_data = dict(session_data)
                serialized_conversation_ids = _normalize_conversation_ids(raw_session_data.pop("conversation_ids", None))
                serialized_conversation_state = raw_session_data.pop("conversation_state", None)
                raw_room_id = _normalize_stable_conversation_id(raw_session_data.get("room_id"))
                raw_session_data["room_id"] = raw_room_id or (serialized_conversation_ids[0] if serialized_conversation_ids else "0")
                session = self.create_session(guest=guest, **raw_session_data)
                if guest:
                    restored_conversation_ids = _normalize_conversation_ids(
                        serialized_conversation_ids + [getattr(session, "room_id", None)]
                    )
                    for conversation_id in restored_conversation_ids:
                        self.set_session(conversation_id, session)
                    if isinstance(serialized_conversation_state, dict):
                        for conversation_id, state in serialized_conversation_state.items():
                            if not isinstance(state, dict):
                                continue
                            normalized_conversation_id = _normalize_stable_conversation_id(conversation_id)
                            if not normalized_conversation_id:
                                continue
                            self.update_conversation_state(
                                normalized_conversation_id,
                                section_id=state.get("section_id"),
                                latest_index=state.get("latest_index"),
                                override=True,
                            )

            logger.info(f"Loaded {len(data)} {'guest' if guest else 'auth'} session snapshot(s) from {path}")
            return len(data)
        except Exception as exc:
            logger.error(f"Failed to load session config from {path}: {exc}")
            return 0

    def load_from_file(self):
        self.session_map = {}
        self.conversation_state_map = {}
        self.auth_sessions = []
        self.guest_sessions = []
        self._load_sessions_from_path(self.auth_config_file, guest=False, missing_level="warning")
        self._load_sessions_from_path(self.guest_config_file, guest=True, missing_level="debug")

    async def fetch_guest_session(self, num: int, aid: str | None = None, real_aid: str | None = None):
        if num > 0 and not aid:
            raise ValueError("Creating guest sessions requires an explicit aid; only pass real_aid when it differs.")
        for _ in range(num):
            automator = DoubaoAutomator()
            self.create_session(
                guest=True,
                aid=aid,
                real_aid=real_aid,
                **(await automator.run_automation()),
            )
        if num > 0:
            self.save_to_file(guest=True)


session_pool = SessionPool()

__all__ = [
    "DoubaoSession",
    "SessionPool",
    "session_pool",
]
