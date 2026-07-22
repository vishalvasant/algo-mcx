from __future__ import annotations

import json
import threading
import time
from typing import Any, Callable

import structlog
import websocket

logger = structlog.get_logger(__name__)


class FlattradeMarketSocket:
  """Flattrade API V2 market WebSocket (auth uses t=a / accesstoken, not Shoonya t=c)."""

  WS_URL = "wss://piconnect.flattrade.in/PiConnectWSAPI/"

  def __init__(
    self,
    user_id: str,
    access_token: str,
    *,
    actid: str | None = None,
    on_quote: Callable[[dict[str, Any]], None] | None = None,
    on_open: Callable[[], None] | None = None,
    on_close: Callable[[], None] | None = None,
    on_error: Callable[[Any], None] | None = None,
  ) -> None:
    self._user_id = user_id
    self._actid = actid or user_id
    self._access_token = access_token
    self._on_quote = on_quote
    self._on_open = on_open
    self._on_close = on_close
    self._on_error = on_error

    self._ws: websocket.WebSocketApp | None = None
    self._thread: threading.Thread | None = None
    self._hb_thread: threading.Thread | None = None
    self._running = False
    self._connected = False
    self._authed = False
    self._subscribed: list[str] = []
    self._lock = threading.Lock()
    self._auth_failed = False

  @property
  def is_open(self) -> bool:
    return self._connected and self._authed

  @property
  def auth_failed(self) -> bool:
    return self._auth_failed

  def start(self) -> None:
    if self._running:
      return
    self._running = True
    self._auth_failed = False
    self._ws = websocket.WebSocketApp(
      self.WS_URL,
      on_open=self._handle_open,
      on_message=self._handle_message,
      on_error=self._handle_error,
      on_close=self._handle_close,
    )
    self._thread = threading.Thread(target=self._run, daemon=True, name="flattrade-ws")
    self._thread.start()

  def stop(self) -> None:
    self._running = False
    self._connected = False
    self._authed = False
    ws = self._ws
    self._ws = None
    if ws is not None:
      try:
        ws.close()
      except Exception:
        pass
    if self._thread and self._thread.is_alive():
      self._thread.join(timeout=3)
    self._thread = None

  def subscribe(self, instruments: list[str]) -> None:
    with self._lock:
      self._subscribed = list(instruments)
    if self.is_open and instruments:
      self._send_subscribe(instruments)

  def _run(self) -> None:
    assert self._ws is not None
    while self._running and not self._auth_failed:
      try:
        self._ws.run_forever(ping_interval=30, ping_timeout=10)
      except Exception as exc:
        logger.warning("flattrade_ws_run_exception", error=str(exc))
      self._connected = False
      self._authed = False
      if not self._running or self._auth_failed:
        break
      time.sleep(2)

  def _handle_open(self, _ws: Any) -> None:
    self._connected = True
    auth = {
      "t": "a",
      "uid": self._user_id,
      "actid": self._actid,
      "source": "API",
      "accesstoken": self._access_token,
    }
    try:
      assert self._ws is not None
      self._ws.send(json.dumps(auth))
      logger.info("flattrade_ws_auth_sent", user_id=self._user_id)
    except Exception as exc:
      logger.warning("flattrade_ws_auth_send_failed", error=str(exc))
      if self._on_error:
        self._on_error(exc)

  def _handle_message(self, _ws: Any, message: str) -> None:
    try:
      data = json.loads(message)
    except json.JSONDecodeError:
      return

    msg_type = data.get("t")
    if msg_type in ("ak", "ck"):
      status = str(data.get("s", "")).upper()
      if status == "OK":
        self._authed = True
        self._auth_failed = False
        logger.info("flattrade_ws_authenticated")
        with self._lock:
          instruments = list(self._subscribed)
        if instruments:
          self._send_subscribe(instruments)
        self._ensure_heartbeat()
        if self._on_open:
          self._on_open()
      else:
        self._auth_failed = True
        self._authed = False
        logger.warning("flattrade_ws_auth_rejected", response=data)
        if self._on_error:
          self._on_error(data)
        self.stop()
      return

    if msg_type == "h":
      return

    if msg_type in ("tk", "tf", "dk", "df") and self._on_quote:
      self._on_quote(data)

  def _handle_error(self, _ws: Any, error: Any) -> None:
    # Auth rejection also shows up as dict via message path; socket errors are noisy.
    err_s = str(error)
    if "opcode=8" in err_s:
      return
    logger.warning("flattrade_ws_error", error=err_s)
    if self._on_error:
      self._on_error(error)

  def _handle_close(self, _ws: Any, status: Any = None, msg: Any = None) -> None:
    self._connected = False
    self._authed = False
    logger.warning("flattrade_ws_closed", status=status, msg=msg)
    if self._on_close:
      self._on_close()

  def _send_subscribe(self, instruments: list[str]) -> None:
    if not self._ws or not instruments:
      return
    # Flattrade accepts hash-joined keys; batch to stay under message limits.
    chunk_size = 50
    for i in range(0, len(instruments), chunk_size):
      chunk = instruments[i : i + chunk_size]
      payload = {"t": "t", "k": "#".join(chunk)}
      try:
        self._ws.send(json.dumps(payload))
      except Exception:
        logger.exception("flattrade_ws_subscribe_failed")
        return
    logger.info("flattrade_ws_subscribed", count=len(instruments))

  def _ensure_heartbeat(self) -> None:
    if self._hb_thread and self._hb_thread.is_alive():
      return

    def _hb() -> None:
      while self._running and self._authed:
        time.sleep(30)
        if not self._running or not self._ws:
          break
        try:
          self._ws.send(json.dumps({"t": "h"}))
        except Exception:
          break

    self._hb_thread = threading.Thread(target=_hb, daemon=True, name="flattrade-ws-hb")
    self._hb_thread.start()
