"""WebSocket hub for real-time updates."""
import json
from collections import defaultdict
from typing import Any

from fastapi import WebSocket


class ConnectionManager:
    """Manages WebSocket connections by channel."""

    def __init__(self):
        # channel -> set of WebSocket
        self._channels: dict[str, set[WebSocket]] = defaultdict(set)

    async def connect(
        self,
        websocket: WebSocket,
        channel: str,
        extra_channels: list[str] | None = None,
    ) -> None:
        await websocket.accept()
        self._channels[channel].add(websocket)
        for ch in extra_channels or []:
            self._channels[ch].add(websocket)

    def disconnect(self, websocket: WebSocket, channel: str, extra_channels: list[str] | None = None) -> None:
        self._channels[channel].discard(websocket)
        for ch in extra_channels or []:
            self._channels[ch].discard(websocket)

    async def broadcast(self, channel: str, event: str, payload: dict[str, Any]) -> None:
        """Send event to all connections in channel."""
        message = json.dumps({"event": event, "data": payload})
        dead = set()
        for ws in self._channels.get(channel, set()):
            try:
                await ws.send_text(message)
            except Exception:
                dead.add(ws)
        for ws in dead:
            self._channels[channel].discard(ws)

    async def broadcast_event(self, event_id: int, event: str, payload: dict[str, Any]) -> None:
        """Broadcast to event channel (all participants of event)."""
        await self.broadcast(f"event:{event_id}", event, payload)

    async def broadcast_team(self, team_id: int, event: str, payload: dict[str, Any]) -> None:
        await self.broadcast(f"team:{team_id}", event, payload)

    async def broadcast_station(self, station_id: int, event: str, payload: dict[str, Any]) -> None:
        await self.broadcast(f"station:{station_id}", event, payload)

    async def broadcast_admin(self, event_id: int, event: str, payload: dict[str, Any]) -> None:
        await self.broadcast(f"admin:{event_id}", event, payload)


ws_manager = ConnectionManager()
