from __future__ import annotations

import json
import threading
import time
from typing import Any, Callable

import structlog
import websocket

logger = structlog.get_logger(__name__)


class FlattradeMarketSocket:
  """Flattrade Pi Connect WebSocket — touchline feed per pi.flattrade.in/docs.

  Subscribe payload: {"t": "t", "k": "BFO|token#NFO|token..."}
  First tick per instrument is t=tk (snapshot), then t=tf (incremental).
  """

  WS_URL = "wss://piconnect.flattrade.in/PiConnectWSAPI/"
  DEFAULT_CHUNK = 50
  BFO_CHUNK = 25
  # websocket-client requires ping_interval > ping_timeout
  PING_INTERVAL_SEC = 20
  PING_TIMEOUT_SEC = 10

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
    self._running = False
    self._connected = False
    self._authed = False
    self._subscribed: list[str] = []
    self._subscribed_set: set[str] = set()
    self._lock = threading.Lock()
    self._auth_failed = False

  @property
  def is_open(self) -> bool:
    return self._connected and self._authed

  @property
  def auth_failed(self) -> bool:
    return self._auth_failed

  @staticmethod
  def _order_bfo_first(instruments: list[str]) -> list[str]:
    bfo = [k for k in instruments if k.upper().startswith("BFO|")]
    rest = [k for k in instruments if not k.upper().startswith("BFO|")]
    return bfo + rest

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
    with self._lock:
      self._subscribed = []
      self._subscribed_set = set()

  def subscribe(self, instruments: list[str]) -> None:
    ordered = self._order_bfo_first(list(dict.fromkeys(instruments)))
    with self._lock:
      prev = set(self._subscribed_set)
      self._subscribed = ordered
      self._subscribed_set = set(ordered)
      to_send = ordered if not prev else [k for k in ordered if k not in prev]
    if self.is_open and to_send:
      self._send_subscribe(to_send, full_resync=not prev)
    elif self.is_open and ordered and not to_send:
      # Universe changed order but same keys — force resync for BFO reliability.
      self._send_subscribe(ordered, full_resync=True)

  def _run(self) -> None:
    assert self._ws is not None
    while self._running and not self._auth_failed:
      try:
        # Keepalive for long-lived NFO touchline streams (library + Flattrade heartbeat).
        self._ws.run_forever(
          ping_interval=self.PING_INTERVAL_SEC,
          ping_timeout=self.PING_TIMEOUT_SEC,
          ping_payload='{"t":"h"}',
        )
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
        logger.info("flattrade_ws_authenticated", ack=msg_type)
        with self._lock:
          instruments = list(self._subscribed)
        if instruments:
          self._send_subscribe(instruments, full_resync=True)
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

  def _send_subscribe(self, instruments: list[str], *, full_resync: bool = False) -> None:
    if not self._ws or not instruments:
      return
    ordered = self._order_bfo_first(instruments)
    bfo = [k for k in ordered if k.upper().startswith("BFO|")]
    rest = [k for k in ordered if not k.upper().startswith("BFO|")]

    def _chunks(group: list[str], size: int) -> list[list[str]]:
      return [group[i : i + size] for i in range(0, len(group), size)]

    batches: list[tuple[list[str], str]] = []
    for chunk in _chunks(bfo, self.BFO_CHUNK):
      batches.append((chunk, "BFO"))
    for chunk in _chunks(rest, self.DEFAULT_CHUNK):
      batches.append((chunk, "MIX"))

    for chunk, label in batches:
      payload = {"t": "t", "k": "#".join(chunk)}
      try:
        self._ws.send(json.dumps(payload))
        logger.info(
          "flattrade_ws_subscribed",
          batch=label,
          count=len(chunk),
          full_resync=full_resync,
        )
        time.sleep(0.12)
      except Exception:
        logger.exception("flattrade_ws_subscribe_failed", batch=label)
        return
