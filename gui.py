#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
gui.py  —  퍼펫 API : 치지직 → 좀보이드 후원연동 (단일 창, 치지직 전용)

설치:  pip install chzzkpy PyQt5
실행:  python gui.py

구조 (양쪽 끝에 "돼지코(어댑터)"를 끼운 형태)
    DonationSource / ChzzkpySource : 치지직 수신 추상화. chzzkpy 의존은 ChzzkpySource 안에만 존재.
                                     -> 공식 API로 바꾸려면 이 클래스만 새로 구현하면 됨.
    GameAdapter / ZomboidAdapter   : 게임별 출력(경로 탐지 + rewards.txt 기록). 게임 확장 포인트.
    DonationWorker                 : 코어. 스레드+asyncio로 Source 를 돌리고 Qt 시그널로 GUI에 전달.
                                     -> chzzkpy 도 게임 파일도 직접 모름. 어댑터한테만 말 건다.
    MainWindow                     : PyQt5 단일 창 UI.
    PZ 원클릭 최적화               : 앱 실행 시 자동으로 PZ 설치 폴더를 찾아 JVM 힙을 RAM 절반으로
                                     설정하고 좀비 연산 패치 class 9개를 교체 (원본 자동 백업).

연결 안정화 (v3.3.0 — 장시간 방송 대응, 4중 방어):
    [1] WS 프로토콜 heartbeat : chzzkpy 게이트웨이에 aiohttp ws_connect(heartbeat=30) 주입.
                                30초마다 프로토콜 ping 전송 + pong 미수신 시 aiohttp가 소켓을
                                에러로 닫음 → chzzkpy 내부 재연결. (전송 계층에서 pong "검증")
    [2] Stale 워치독          : 서버발 모든 프레임의 수신 시각을 기록. 90초간 아무것도 못 받으면
                                살아있는 척하는 죽은 연결(NAT timeout 등)로 판정, 강제 재접속.
                                (chzzkpy poll_event는 ping을 보내기만 하고 pong을 검증하지 않아
                                 silent disconnect 를 스스로 못 잡음 — 이 워치독이 그 구멍을 막음)
    [3] 연결 타임아웃         : CONNECTED 이벤트까지 20초 제한. 멈춘 connect() 무한 대기 방지.
    [4] 지수 백오프           : 재접속 5→10→20→40→60초(최대). 연결 성공했던 시도 후엔 5초로 리셋.
                                방송 대기(오프라인)는 백오프 없이 5초 고정 폴링.
    + chzzkpy 이벤트 활용     : on_connect(상태 표시+도네 grace 리셋) / on_disconnect(내부 재연결
                                로그) / on_broadcast_close(방송 종료 즉시 감지) / on_client_error.

19세 방송:  네이버 NID 쿠키(성인인증 계정)를 넘기면 19+ 방송도 수신. 쿠키는 약 한 달이면 만료.
라인 포맷(모드 DonationReceiver.lua 규약):  amount,featureId,sender,message
    (featureId/sender/message URL 인코딩·featureId는 reward_tiers 매핑에 없으면 빈 문자열로
    기록되며, 그 경우 모드 쪽에선 통계만 잡히고 게임 효과는 발동하지 않음)
"""

import asyncio
import json
import os
import re
import shutil
import sys
import threading
import time
from collections import namedtuple
from pathlib import Path

from PyQt5.QtCore import Qt, QObject, pyqtSignal, QTimer, QSharedMemory
from PyQt5.QtGui import QIcon, QPixmap
from PyQt5.QtWidgets import (
    QApplication, QWidget, QLabel, QLineEdit, QPushButton, QComboBox,
    QTextEdit, QVBoxLayout, QHBoxLayout, QGridLayout, QFileDialog, QFrame,
    QCheckBox, QStackedWidget, QMessageBox, QDialog, QScrollArea,
)


# ── 화이트리스트 (시즌 참가 채널 UUID) — 원격 fetch ────────────────────────────
# 시즌 중 스트리머 추가/삭제는 이 JSON 만 커밋하면 됨 (exe 재빌드 불필요). URL 바꿀 때만 재빌드.
#  파일 포맷 셋 다 지원 (32자리 hex만 추출):
#    1) 줄당 하나       UUID / URL / 텍스트 무엇이든. '#' 뒤는 주석
#    2) JSON 배열       ["uuid", ...]
#    3) JSON 객체       {"이름":"uuid", ...}  또는  {"whitelist":[...]}





VERSION = "v3.6.3"

WHITELIST_URL = "https://raw.githubusercontent.com/Project-PongDu/Whitelist/refs/heads/main/streamer%20whitelist.json"






UA = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"}
FORCE_ONLINE = False

# ── 로컬 설정 (게임 유저 폴더 ~/Zomboid 안에 저장 -> rewards.txt 옆이라 찾기 쉬움) ──
def find_zomboid_dir() -> Path:
    """OS별 좀보이드 유저 데이터 폴더(.../Zomboid)를 찾는다.
       ZomboidAdapter.find_path()의 rewards.txt 탐지와 동일한 후보 순회 패턴."""
    home = Path.home()
    cands = [
        home / "Zomboid",
        Path(os.environ.get("USERPROFILE", home)) / "Zomboid",
    ]
    for env in ("OneDrive", "OneDriveConsumer"):
        od = os.environ.get(env)
        if od:
            cands.append(Path(od) / "Zomboid")
    for c in cands:
        if c.exists():
            return c
    for drive in ("C:", "D:", "E:", "F:"):
        base = Path(drive + "\\Users")
        if base.exists():
            try:
                for user in base.iterdir():
                    p = user / "Zomboid"
                    if p.exists():
                        return p
            except OSError:
                pass
    return cands[0]   # 못 찾으면 home/Zomboid (게임 실행 전이라 폴더가 아직 없을 수 있음)

CONFIG_DIR = find_zomboid_dir()
CONFIG_PATH = CONFIG_DIR / "chzzk_donation_config.json"

def load_config() -> dict:
    try:
        return json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
    except Exception:
        return {}

def save_config(d: dict):
    try:
        CONFIG_DIR.mkdir(parents=True, exist_ok=True)
        CONFIG_PATH.write_text(json.dumps(d, ensure_ascii=False, indent=2), encoding="utf-8")
    except OSError:
        pass


# ── 리워드 프리셋 (Zomboid 폴더의 독립 json — 있으면 이걸 쓰고, 없으면 코드 기본값) ──
PRESET_PATH = CONFIG_DIR / "reward_preset.json"

def load_reward_preset() -> dict:
    try:
        return json.loads(PRESET_PATH.read_text(encoding="utf-8"))
    except Exception:
        return {}

def save_reward_preset(tiers: dict):
    """{amount(int): featureId} -> reward_preset.json 기록. 저장 버튼 = 내보내기."""
    try:
        CONFIG_DIR.mkdir(parents=True, exist_ok=True)
        data = {str(amt): fid for amt, fid in sorted(tiers.items())}
        PRESET_PATH.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    except OSError:
        pass

def reset_reward_preset():
    """프리셋 파일 삭제 -> 다음 로드부터 DEFAULT_REWARD_TIERS(코드 기본값) 사용."""
    try:
        PRESET_PATH.unlink(missing_ok=True)
    except OSError:
        pass


# ── 채널 입력 정규화 ──────────────────────────────────────────────────────────
HEX32 = re.compile(r"[0-9a-fA-F]{32}")

def resource_path(rel):
    """exe(PyInstaller)로 묶였을 때든 그냥 실행이든 리소스 파일 경로를 찾는다."""
    base = getattr(sys, "_MEIPASS", os.path.dirname(os.path.abspath(__file__)))
    return os.path.join(base, rel)

ICON_FILE = "pongdu.ico"

def extract_uuid(text: str):
    """입력 어디에 있든 32자리 hex(=채널 UUID)를 뽑는다. URL/라이브URL/생UUID 다 처리."""
    m = HEX32.search(text or "")
    return m.group(0).lower() if m else None


# ═══════════════════════════════════════════════════════════════════════════════
#  치지직 어댑터 (수신 방식 추상화 = "돼지코")
#  chzzkpy 의존은 ChzzkpySource 안에만 존재. 공식 API로 바꾸려면 이 클래스만 갈아끼우면 됨.
# ═══════════════════════════════════════════════════════════════════════════════
Donation = namedtuple("Donation", "amount sender message")   # 플랫폼 중립 도네 1건


class SourceError(Exception):
    pass

class AdultVerificationRequired(SourceError):   # 19+ 방송인데 쿠키 없음/만료
    pass

class ChannelOffline(SourceError):              # 방송 꺼져 있음
    pass

class StaleConnection(SourceError):             # 소켓은 열려있는 척하지만 수신이 끊긴 죽은 연결
    pass

class ConnectTimeout(SourceError):              # 제한 시간 안에 CONNECTED 못 받음
    pass


class DonationSource:
    """치지직 수신 인터페이스. chzzkpy든 공식 API든 이 4개만 구현하면 코어는 안 바뀐다."""

    async def resolve_channel(self, text):
        """입력(URL/채널명/UUID) -> (uuid, 표시이름). 못 찾으면 (None, 사유)."""
        raise NotImplementedError

    async def connect(self, uuid, emit, nid_aut=None, nid_ses=None, on_event=None):
        """연결 후 도네마다 emit(Donation) 호출. 정상 종료 시 리턴, 문제 시 SourceError.
           on_event(kind, detail): 연결 수명주기 통지 (같은 이벤트 루프에서 호출됨).
             kind ∈ connected / ws_reconnect / broadcast_close / stale / client_error"""
        raise NotImplementedError

    def request_close(self):
        """스레드 세이프 종료 요청 플래그만 세움 (즉시 리턴). 실제 정리는 connect()가 수행."""
        raise NotImplementedError

    async def close(self):
        raise NotImplementedError


# ── 연결 안정화 파라미터 ──────────────────────────────────────────────────────
WS_HEARTBEAT_SEC = 30.0   # [1] aiohttp WS 프로토콜 ping 주기. pong 미수신 시 aiohttp가 소켓 강제 종료
STALE_SEC        = 90.0   # [2] 이 시간 동안 서버로부터 프레임을 하나도 못 받으면 죽은 연결로 판정
                          #     (정상 연결은 서버 PING/우리 ping의 PONG 으로 최소 ~60초마다 수신 발생)
STALE_CHECK_SEC  = 5.0    # [2] 워치독 검사 주기 (stop 요청 반응 속도도 이 주기)
CONNECT_TIMEOUT  = 20.0   # [3] CONNECTED 이벤트까지 허용 시간
CLOSE_TIMEOUT    = 5.0    #     종료 정리 대기 한도

# ChzzkWebSocket.received_message 패치가 갱신하는 마지막 수신 시각.
# 이 앱은 동시에 1개 연결만 유지하므로 모듈 전역으로 충분 (다중 연결 확장 시 인스턴스화 필요).
_WS_ACTIVITY = {"t": 0.0}

_GW_PATCHED = False

def _patch_chzzkpy_gateway():
    """chzzkpy 게이트웨이에 2가지를 1회 주입한다 (chzzkpy 2.2.0 기준 — 빌드 시 버전 고정 권장).

    (a) received_message 래핑 — 서버발 모든 TEXT 프레임 수신 시각을 _WS_ACTIVITY 에 기록.
        chzzkpy poll_event 는 58초 수신 타임아웃 시 ping 을 '보내기만' 하고 pong 수신을 검증하지
        않아서, NAT timeout 등으로 소켓이 조용히 죽어도 영원히 연결된 척한다 (silent disconnect).
        이 스탬프를 워치독이 읽어 그 구멍을 막는다.
    (b) new_session 재구현 — session.ws_connect(url, heartbeat=WS_HEARTBEAT_SEC).
        aiohttp 가 30초마다 프로토콜 레벨 WS ping 을 보내고 pong 미수신 시 소켓을 에러로 닫음
        → poll_event 가 WSMsgType.ERROR 수신 → WebSocketClosure → chzzkpy 내부 자동 재연결.
        즉 전송 계층에서 pong 검증까지 되는 진짜 heartbeat 가 생긴다.

    패치 실패 시(라이브러리 구조 변경 등) 조용히 폴백 — 앱은 기존 방식 그대로 동작하고,
    이 경우에도 (a) 없이 stale 워치독은 grace 스탬프 기반으로 보수적으로 동작한다."""
    global _GW_PATCHED
    if _GW_PATCHED:
        return
    try:
        from chzzkpy.unofficial.chat import gateway as _gw

        _orig_recv = _gw.ChzzkWebSocket.received_message

        async def _recv_stamped(self, data):
            _WS_ACTIVITY["t"] = time.monotonic()
            return await _orig_recv(self, data)

        _gw.ChzzkWebSocket.received_message = _recv_stamped

        async def _new_session_hb(loop, session, channel_id, session_id=None):
            # chzzkpy 2.2.0 ChzzkWebSocket.new_session 로직 + heartbeat 인자
            server_id = abs(sum(ord(x) for x in channel_id)) % 9 + 1
            url = f"wss://kr-ss{server_id}.chat.naver.com/chat"
            socket = await session.ws_connect(url, heartbeat=WS_HEARTBEAT_SEC)
            ws = _gw.ChzzkWebSocket(socket, loop)
            ws.session_id = session_id
            return ws

        # from_client 는 cls.new_session(loop=…, session=…, …) 키워드 호출 → 평범한 함수로 대체 가능
        _gw.ChzzkWebSocket.new_session = _new_session_hb
        _GW_PATCHED = True
    except Exception:
        pass


class ChzzkpySource(DonationSource):
    """chzzkpy(비공식) 기반 구현. ← 이 파일에서 chzzkpy 를 import/호출하는 유일한 곳."""

    def __init__(self, grace_sec=3.0):
        self.grace = grace_sec
        self._client = None
        self._closing = False      # request_close()가 세우는 스레드 세이프 플래그
        self.was_connected = False # 직전 connect() 시도에서 CONNECTED 를 한 번이라도 받았는지 (백오프 리셋용)

    async def resolve_channel(self, text):

        global FORCE_ONLINE
        name = (text or "").strip()
        # 관리자 모드
        if name.lower() == "t3qquq":
            FORCE_ONLINE = True
            return "t3qquq", "t3qquq"
        FORCE_ONLINE = False

        uuid = extract_uuid(text)
        if uuid:
            return uuid, (await self._fetch_channel_name(uuid) or "")
        name = (text or "").strip()
        if not name:
            return None, "빈 입력"
        try:
            from chzzkpy.unofficial import Client
            c = Client()
            res = await c.search_channel(name)
            await c.close()
        except Exception:
            return None, "검색 실패"
        if not res:
            return None, "검색 결과 없음"
        return res[0].id, res[0].name

    async def _fetch_channel_name(self, uuid):
        """UUID로 직접 입력했을 때 채널명을 치지직 공개 API에서 가져온다. (chzzkpy 미지원이라 직접 호출)"""
        import aiohttp
        url = f"https://api.chzzk.naver.com/service/v1/channels/{uuid}"
        headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"}
        try:
            timeout = aiohttp.ClientTimeout(total=5)
            async with aiohttp.ClientSession(headers=headers, timeout=timeout) as s:
                async with s.get(url) as r:
                    data = await r.json()
            return ((data or {}).get("content") or {}).get("channelName")
        except Exception:
            return None

    async def connect(self, uuid, emit, nid_aut=None, nid_ses=None, on_event=None):
        from chzzkpy.unofficial.chat import ChatClient

        _patch_chzzkpy_gateway()

        def ev(kind, detail=""):
            if on_event:
                try:
                    on_event(kind, detail)
                except Exception:
                    pass

        self._closing = False
        self.was_connected = False
        self._client = ChatClient(uuid)
        client = self._client
        started = time.monotonic()          # 도네 grace 기준점 (on_connect 가 재연결마다 리셋)
        grace = self.grace
        offline_seen = {"v": False}         # on_broadcast_close 수신 여부

        @client.event
        async def on_connect():             # CONNECTED 수신 — 재연결 포함 매번 호출됨
            nonlocal started
            started = time.monotonic()      # (재)연결 직후 리플레이된 과거 도네 무시용 grace 리셋
            _WS_ACTIVITY["t"] = time.monotonic()
            self.was_connected = True
            ev("connected")

        @client.event
        async def on_disconnect():          # chzzkpy 내부 재연결 (소켓 비정상 종료/채팅채널 변경)
            ev("ws_reconnect")

        @client.event
        async def on_broadcast_close():     # 방송 종료 — 즉시 연결 정리 후 ChannelOffline 으로 전환
            offline_seen["v"] = True
            ev("broadcast_close")
            try:
                await client.close()
            except Exception:
                pass

        @client.event
        async def on_client_error(exc, *a, **k):   # 수신 파싱 오류 — 연결은 유지, 로그만
            ev("client_error", f"{type(exc).__name__}: {exc}")

        @client.event
        async def on_error(exc, *a, **k):          # 이벤트 핸들러 내부 오류 — 연결은 유지, 로그만
            ev("client_error", f"{type(exc).__name__}: {exc}")

        @client.event
        async def on_donation(message):     # chzzkpy 가 함수명으로 이벤트 매칭 -> 이름 고정
            if time.monotonic() - started < grace:
                return                       # (재)접속 직후 리플레이된 과거 도네 무시
            ex = getattr(message, "extras", None)
            try:
                amt = int(getattr(ex, "pay_amount", 0) or 0)
            except (TypeError, ValueError):
                amt = 0
            if amt <= 0:
                return
            anon = bool(getattr(ex, "is_anonymous", False))
            prof = getattr(message, "profile", None)
            nick = getattr(prof, "nickname", None) if prof else None
            sender = "익명의 후원자" if (anon or not nick) else nick
            body = (getattr(message, "content", "") or "").replace("\r", " ").replace("\n", " ").strip()
            emit(Donation(amt, sender, body))   # ← 코어로는 chzzkpy 객체가 아니라 Donation 만 넘어감

        _WS_ACTIVITY["t"] = time.monotonic()
        loop = asyncio.get_running_loop()
        start_task = loop.create_task(client.start(nid_aut, nid_ses))
        ready_task = loop.create_task(client.wait_until_connected())

        try:
            # ── [3] 1단계: CONNECTED 까지 타임아웃 감시 ──
            done, _ = await asyncio.wait(
                {start_task, ready_task},
                timeout=CONNECT_TIMEOUT,
                return_when=asyncio.FIRST_COMPLETED,
            )
            if start_task in done:                       # 연결도 전에 start 가 끝남 = 실패/종료
                exc = start_task.exception()
                if exc is not None:
                    self._raise_mapped(exc, offline_seen["v"])
                if offline_seen["v"]:
                    raise ChannelOffline()
                return                                   # 예외 없는 조기 종료 (close 등)
            if ready_task not in done:                   # 타임아웃
                raise ConnectTimeout("%d초 안에 연결되지 않음 (네트워크/치지직 서버 응답 없음)"
                                     % int(CONNECT_TIMEOUT))

            # ── [2] 2단계: 연결 유지 — stale 워치독 ──
            while True:
                done, _ = await asyncio.wait({start_task}, timeout=STALE_CHECK_SEC)
                if self._closing:                        # 사용자 중지
                    return
                if start_task in done:                   # 수신 루프 종료 (예외 or 정상)
                    exc = start_task.exception()
                    if exc is not None:
                        self._raise_mapped(exc, offline_seen["v"])
                    if offline_seen["v"]:
                        raise ChannelOffline()
                    return
                idle = time.monotonic() - _WS_ACTIVITY["t"]
                if idle > STALE_SEC:                     # 살아있는 척하는 죽은 연결
                    ev("stale", str(int(idle)))
                    raise StaleConnection("%d초간 서버 수신 없음" % int(idle))
        finally:
            self._client = None
            ready_task.cancel()
            if not start_task.done():
                start_task.cancel()
            try:                                          # ws/세션 정리 (예외 무시)
                await asyncio.wait_for(client.close(), timeout=CLOSE_TIMEOUT)
            except Exception:
                pass
            try:                                          # 취소된 태스크 회수 (pending 경고 방지)
                await asyncio.wait_for(
                    asyncio.gather(start_task, ready_task, return_exceptions=True),
                    timeout=CLOSE_TIMEOUT)
            except Exception:
                pass

    @staticmethod
    def _raise_mapped(e, offline_seen=False):
        """chzzkpy/네트워크 예외 → 의미 있는 SourceError 로 분류 (로그 가독성 + 워커 분기)."""
        import aiohttp
        try:
            from chzzkpy.unofficial.chat.error import (
                ChatConnectFailed, ConnectionClosed as ChzzkConnectionClosed)
        except Exception:                                # 라이브러리 구조 변경 대비
            ChatConnectFailed = ChzzkConnectionClosed = ()
        low = f"{type(e).__name__}: {e}".lower()

        if isinstance(e, ChatConnectFailed) or "adult" in low or "verification" in low:
            if "adult" in low or "verification" in low:
                raise AdultVerificationRequired() from e
            # channel_is_null: "Is the channel(...) broadcasting live?" / chat_channel_is_null
            if "broadcasting live" in low or "missing chat id" in low:
                raise ChannelOffline() from e
            raise SourceError("치지직 접속 거부: %s" % e) from e
        if offline_seen:                                 # 방송 종료로 우리가 닫은 뒤 발생한 후속 예외
            raise ChannelOffline() from e
        if isinstance(e, ChzzkConnectionClosed):
            raise SourceError("서버가 연결을 종료함 (code=%s)" % getattr(e, "code", "?")) from e
        if isinstance(e, asyncio.TimeoutError):
            raise SourceError("소켓 타임아웃") from e
        if isinstance(e, asyncio.CancelledError):
            raise SourceError("수신 루프 취소됨") from e
        if isinstance(e, (aiohttp.ClientError, OSError)):
            raise SourceError("네트워크 오류: %s: %s" % (type(e).__name__, e)) from e
        # 구버전 호환 문자열 매칭 폴백
        if any(k in low for k in ("chat_channel", "channel_is_null", "is_null", "not live", "offline")):
            raise ChannelOffline() from e
        raise SourceError("%s: %s" % (type(e).__name__, e)) from e

    def request_close(self):
        # 다른 스레드에서 호출됨 — 플래그만 세움. 워치독이 ≤STALE_CHECK_SEC 안에 감지해 정리.
        self._closing = True

    async def close(self):
        self._closing = True
        if self._client is not None:
            try:
                await self._client.close()
            except Exception:
                pass


# ═══════════════════════════════════════════════════════════════════════════════
#  게임 어댑터 (출력 대상 추상화 = "돼지코")
# ═══════════════════════════════════════════════════════════════════════════════
class GameAdapter:
    name = "game"

    def __init__(self):
        self.path = None  # Path | None

    def find_path(self):
        raise NotImplementedError

    def write(self, amount, feature_id, sender, message):
        raise NotImplementedError


class ZomboidAdapter(GameAdapter):
    name = "좀보이드"

    # featureId -> 표시 라벨. rewardManager.lua의 rewardHandlers 키와 반드시 1:1로 일치해야 함
    # (mod_source.txt 기준, 18개 고정값 — 임의로 이름 바꾸지 말 것).
    FEATURES = {
        "buff_roulette":       "버프 룰렛",
        "debuff_roulette":     "디버프 룰렛",
        "random_weapon":       "랜덤 무기",
        "zombie_roulette":     "좀비 룰렛",
        "vaccine":             "백신",
        "vehicle_drop":        "차량 공중보급",
        "sprinter5":           "스프린터 5마리",
        "random_teleport":     "랜덤 텔레포트",
        "random_skill_potion": "신체 강화 혈청",
        "mutant_spawn":        "특수좀비 소환",
        "inv_save_ticket":     "인벤토리 세이브 티켓",
        "missile":             "미사일 폭격",
        "zombie_rain":         "좀비 레인",
        "rise_up_dead_man":    "강령술",

        # "revive_ticket":       "즉시부활 티켓 (미구현)",
        # "secret_passage_kit":  "비밀통로 키트 (미구현)",
        # "horde_night":         "호드나이트 (미구현)",

        #미사용
        "bandit_melee":        "암살자 파견 (근접)",
        "bandit_ranged":       "암살자 파견 (원거리)",
        # "exile":               "산타마을 유배 (삭제예정)",
        # "backroom":            "백룸 (삭제예정)",
    }

    # 금액(원) -> featureId. 유저가 GUI에서 자유롭게 재배정 가능(reward_tiers).
    # 이 값은 config.json에 reward_tiers가 없을 때(첫 실행/구버전 마이그레이션)의 기본값.
    DEFAULT_REWARD_TIERS = {
        1000:   "buff_roulette",
        1100:   "debuff_roulette",
        2000:   "random_weapon",
        3000:   "zombie_roulette",
        5000:   "vaccine",
        7000:   "vehicle_drop",
        10000:  "sprinter5",
        15000:  "random_teleport",
        20000:  "random_skill_potion",
        30000:  "mutant_spawn",
        50000:  "inv_save_ticket",
        100000: "missile",
        150000: "zombie_rain",
        200000: "rise_up_dead_man",
    }

    def __init__(self):
        super().__init__()
        # amount(int) -> featureId. MainWindow가 config.json 로드 후 덮어쓴다 (_load_reward_tiers).
        self.reward_tiers = dict(self.DEFAULT_REWARD_TIERS)

    def find_path(self):
        home = Path.home()
        cands = [
            home / "Zomboid" / "Lua" / "rewards.txt",
            Path(os.environ.get("USERPROFILE", home)) / "Zomboid" / "Lua" / "rewards.txt",
        ]
        for env in ("OneDrive", "OneDriveConsumer"):
            od = os.environ.get(env)
            if od:
                cands.append(Path(od) / "Zomboid" / "Lua" / "rewards.txt")
        for c in cands:
            if c.parent.exists():
                return c
        for drive in ("C:", "D:", "E:", "F:"):
            base = Path(drive + "\\Users")
            if base.exists():
                try:
                    for user in base.iterdir():
                        p = user / "Zomboid" / "Lua"
                        if p.exists():
                            return p / "rewards.txt"
                except OSError:
                    pass
        return cands[0]

    @staticmethod
    def _enc(s):
        # 콤마·줄바꿈·퍼센트만 인코딩, 한글 등 유니코드는 raw UTF-8로 통과 (PZ Lua urldecode 호환)
        return (s or "").replace("%", "%25").replace(",", "%2C").replace("\n", "%0A").replace("\r", "%0D")

    def write(self, amount, feature_id, sender, message):
        # featureId는 영문 소문자+언더스코어만 쓰므로 _enc 안 걸어도 됨 (콤마/개행 없음)
        line = "%d,%s,%s,%s" % (int(amount), feature_id or "", self._enc(sender), self._enc(message))
        if self.path is None:
            raise RuntimeError("rewards.txt 경로가 설정되지 않음")
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with open(self.path, "a", encoding="utf-8") as f:
            f.write(line + "\n")
            f.flush()
        return line


# ═══════════════════════════════════════════════════════════════════════════════
#  런처 게이트용 헬퍼: 화이트리스트 / 방송 on-off / PZ 프로세스 감지
# ═══════════════════════════════════════════════════════════════════════════════
def parse_whitelist(raw: str) -> set:
    """원격에서 받은 본문을 32자리 hex UUID 집합으로 파싱. JSON 배열/객체와 줄단위 둘 다 지원."""
    raw = (raw or "").strip()
    if raw[:1] == "<":          # 뷰어/로그인 HTML(잘못된 URL·비공개 레포 등)은 즉시 무효 → 캐시 폴백
        return set()
    items = []
    if raw[:1] in "[{":
        try:
            obj = json.loads(raw)
            if isinstance(obj, dict):
                obj = obj.get("whitelist") or obj.get("channels") or list(obj.values())
            if isinstance(obj, list):
                items = [str(x) for x in obj]
        except Exception:
            items = []
    if not items:
        items = raw.splitlines()
    out = set()
    for it in items:
        it = it.split("#", 1)[0].strip()        # 줄 주석 제거
        u = extract_uuid(it)                     # URL이든 생 UUID든 hex만 뽑음 (소문자)
        if u:
            out.add(u)
    return out


async def fetch_whitelist() -> set:
    """원격 화이트리스트 fetch. 캐시 없음 — 매번 GitHub에서 받아오고,
       실패하면 빈 집합 반환 = 전원 차단 (fail-closed)."""
    import aiohttp
    try:
        timeout = aiohttp.ClientTimeout(total=6)
        async with aiohttp.ClientSession(headers=UA, timeout=timeout) as s:
            async with s.get(WHITELIST_URL) as r:
                raw = await r.text()
        return parse_whitelist(raw)
    except Exception:
        return set()


async def fetch_status(uuid: str):
    """(is_live, is_adult) 반환. chzzkpy live_status 한 번으로 방송 on/off + 19세 여부 동시 판정.
       방송 안 하면 live_status 가 None → (False, False)."""
    if FORCE_ONLINE:
        return (True, False)
    try:
        from chzzkpy.unofficial import Client
        c = Client()
        st = await c.live_status(channel_id=uuid)
        await c.close()
    except Exception:
        return (False, False)
    if st is None:
        return (False, False)
    is_live = (getattr(st, "status", "") == "OPEN")
    is_adult = bool(getattr(st, "adult", False))
    return (is_live, is_adult)


def pz_running() -> bool:
    """Project Zomboid 클라이언트가 실행 중인지 프로세스 목록으로 확인."""
    KEY = "projectzomboid"
    try:                                         # psutil 있으면 우선 (의존성 아님, 있으면 사용)
        import psutil
        for p in psutil.process_iter(["name"]):
            if KEY in (p.info.get("name") or "").lower():
                return True
        return False
    except Exception:
        pass
    import subprocess
    if os.name == "nt":                          # 배포 대상: Windows
        try:
            CREATE_NO_WINDOW = 0x08000000        # noconsole exe 에서 콘솔창 안 뜨게
            out = subprocess.run(["tasklist"], capture_output=True, text=True,
                                 creationflags=CREATE_NO_WINDOW).stdout.lower()
            return KEY in out
        except Exception:
            return False
    try:                                         # 개발용 폴백 (mac/linux)
        out = subprocess.run(["pgrep", "-fil", KEY], capture_output=True, text=True).stdout.lower()
        return KEY in out
    except Exception:
        return False


def pz_connected() -> bool:
    """pz_status.txt 읽어 인게임 접속 여부 확인.
    형식: CONNECTED|<unix timestamp>  — 10초 이상 갱신 없으면 False (Lua heartbeat 끊긴 것)."""
    TIMEOUT = 10
    home = Path.home()
    cands = [
        home / "Zomboid" / "Lua" / "pz_status.txt",
        Path(os.environ.get("USERPROFILE", home)) / "Zomboid" / "Lua" / "pz_status.txt",
    ]
    for env in ("OneDrive", "OneDriveConsumer"):
        od = os.environ.get(env)
        if od:
            cands.append(Path(od) / "Zomboid" / "Lua" / "pz_status.txt")
    for c in cands:
        if c.exists():
            try:
                raw = c.read_text(encoding="utf-8").strip()
                if not raw.startswith("CONNECTED"):
                    return False
                parts = raw.split("|")
                if len(parts) < 2:
                    return False
                ts = float(parts[1])
                return (time.time() - ts) <= TIMEOUT
            except Exception:
                return False
    return False


# ═══════════════════════════════════════════════════════════════════════════════
#  PZ 원클릭 최적화 (구 PZ_optimizer.zip 통합)
#  - JVM 힙을 전체 RAM의 절반으로: ProjectZomboid64.json(스팀 실행용) + .bat(직접 실행용) 패치
#  - 좀비 연산/청크 로딩 패치 class 9개 교체 (opt_conf/ 리소스, exe에 --add-data로 포함)
#  - 원본은 게임 폴더의 puppet_opt_backup/ 에 최초 1회 백업 → '해제'로 언제든 복원
#  - Program Files 등 쓰기 권한 없는 경로면 --pz-optimize 플래그로 자신을 관리자 재실행
#  주의: B41 전용 패치. 대상 class 원본이 하나라도 없으면(B42 등) 건드리지 않고 중단.
# ═══════════════════════════════════════════════════════════════════════════════
OPT_DIRNAME = "opt_conf"
OPT_CLASS_TARGETS = {                      # 패치 파일명 -> 게임 폴더 내 상대 경로
    "IsoChunkMap.class":                 "zombie/iso",
    "IsoWorld.class":                    "zombie/iso",
    "IsoWorld$CompDistToPlayer.class":   "zombie/iso",
    "IsoWorld$CompScoreToPlayer.class":  "zombie/iso",
    "IsoWorld$Frame.class":              "zombie/iso",
    "IsoWorld$MetaCell.class":           "zombie/iso",
    "IsoWorld$s_performance.class":      "zombie/iso",
    "NetworkZombiePacker.class":         "zombie/popman",
    "ZombieCountOptimiser.class":        "zombie/popman",
}
OPT_BACKUP_DIRNAME = "puppet_opt_backup"
PZ_APPID = "108600"

_XMX_RE = re.compile(r"-Xmx\d+[mMgG]")
_XMS_RE = re.compile(r"-Xms\d+[mMgG]")


def opt_conf_dir():
    """패치 리소스 폴더(opt_conf). 빌드에 안 들어갔으면 None → 최적화 기능 전체 비활성."""
    p = Path(resource_path(OPT_DIRNAME))
    return p if p.is_dir() else None


def total_ram_mb() -> int:
    """전체 물리 RAM (MB). Windows는 GlobalMemoryStatusEx, 개발용(posix)은 sysconf."""
    if os.name == "nt":
        import ctypes
        class MEMORYSTATUSEX(ctypes.Structure):
            _fields_ = [
                ("dwLength", ctypes.c_ulong), ("dwMemoryLoad", ctypes.c_ulong),
                ("ullTotalPhys", ctypes.c_ulonglong), ("ullAvailPhys", ctypes.c_ulonglong),
                ("ullTotalPageFile", ctypes.c_ulonglong), ("ullAvailPageFile", ctypes.c_ulonglong),
                ("ullTotalVirtual", ctypes.c_ulonglong), ("ullAvailVirtual", ctypes.c_ulonglong),
                ("ullAvailExtendedVirtual", ctypes.c_ulonglong),
            ]
        st = MEMORYSTATUSEX()
        st.dwLength = ctypes.sizeof(MEMORYSTATUSEX)
        if ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(st)):
            return int(st.ullTotalPhys // (1024 * 1024))
        return 0
    try:
        return int(os.sysconf("SC_PAGE_SIZE") * os.sysconf("SC_PHYS_PAGES") // (1024 * 1024))
    except (ValueError, OSError, AttributeError):
        return 0


def half_ram_mb() -> int:
    """할당할 힙 크기 = 전체 RAM의 절반 (256MB 단위 절사, 최소 2048)."""
    total = total_ram_mb()
    if total <= 0:
        return 0
    return max(2048, (total // 2 // 256) * 256)


def _steam_roots():
    """Steam 루트 후보들: 레지스트리(HKCU 우선) → 드라이브 스캔. 중복 제거."""
    roots = []
    if os.name == "nt":
        try:
            import winreg
            for hive, key, val in (
                (winreg.HKEY_CURRENT_USER,  r"Software\Valve\Steam",              "SteamPath"),
                (winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\WOW6432Node\Valve\Steam",  "InstallPath"),
                (winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\Valve\Steam",              "InstallPath"),
            ):
                try:
                    with winreg.OpenKey(hive, key) as k:
                        v, _ = winreg.QueryValueEx(k, val)
                    p = Path(str(v))
                    if p.exists():
                        roots.append(p)
                except OSError:
                    pass
        except ImportError:
            pass
    for drive in ("C:", "D:", "E:", "F:", "G:", "H:"):
        for sub in ("Steam", "SteamLibrary",
                    "Program Files (x86)\\Steam", "Program Files\\Steam"):
            p = Path(drive + "\\") / sub
            if p.exists():
                roots.append(p)
    seen, out = set(), []
    for r in roots:
        key = str(r).lower()
        if key not in seen:
            seen.add(key)
            out.append(r)
    return out


_VDF_PATH_RE = re.compile(r'"path"\s+"((?:[^"\\]|\\.)*)"')

def _libraries_from(steam_root: Path):
    """steamapps/libraryfolders.vdf 파싱 → 이 Steam이 아는 모든 라이브러리 폴더."""
    libs = [steam_root]
    vdf = steam_root / "steamapps" / "libraryfolders.vdf"
    try:
        raw = vdf.read_text(encoding="utf-8", errors="replace")
        for m in _VDF_PATH_RE.finditer(raw):
            libs.append(Path(m.group(1).replace("\\\\", "\\")))
    except OSError:
        pass
    return libs


def find_pz_dir():
    """PZ 설치 폴더 탐색. appmanifest_108600.acf가 있는 라이브러리 우선(별도 SteamLibrary 지원),
       없으면 폴더 존재만으로 폴백. 못 찾으면 None."""
    roots = _steam_roots()
    for root in roots:
        for lib in _libraries_from(root):
            steamapps = lib / "steamapps"
            if (steamapps / f"appmanifest_{PZ_APPID}.acf").exists():
                g = steamapps / "common" / "ProjectZomboid"
                if g.exists():
                    return g
    for root in roots:
        g = root / "steamapps" / "common" / "ProjectZomboid"
        if g.exists():
            return g
    return None


def _backup_once(game_dir: Path, rel):
    """원본을 puppet_opt_backup/에 백업. 이미 백업본이 있으면 건드리지 않음(최초 원본 보존)."""
    src = game_dir / rel
    dst = game_dir / OPT_BACKUP_DIRNAME / rel
    if src.exists() and not dst.exists():
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, dst)


def _patched_vmargs(args, mb: int):
    """vmArgs에서 기존 -Xms/-Xmx 제거 후 새 값 삽입 (나머지 옵션·순서 보존)."""
    out = [a for a in args if not (str(a).startswith("-Xms") or str(a).startswith("-Xmx"))]
    return [f"-Xms{mb}m", f"-Xmx{mb}m"] + out


def apply_pz_optimization(game_dir: Path) -> int:
    """최적화 적용. 반환: 설정한 힙(MB). 권한 문제는 PermissionError 그대로 던짐 → 호출자가 승격."""
    conf = opt_conf_dir()
    if conf is None:
        raise RuntimeError("패치 리소스(opt_conf)가 빌드에 포함되지 않음")
    mb = half_ram_mb()
    if mb <= 0:
        raise RuntimeError("RAM 크기를 알 수 없음")
    # 사전 검증: 대상 원본·패치 파일 전부 존재해야 진행 (B42 등 구조 다르면 아무것도 안 건드림)
    for fname, sub in OPT_CLASS_TARGETS.items():
        if not (conf / fname).exists():
            raise RuntimeError(f"패치 파일 누락: {fname}")
        if not (game_dir / sub / fname).exists():
            raise RuntimeError(f"게임 파일 구조가 예상과 다름: {sub}/{fname} 없음 (B41 맞는지 확인)")
    jpath = game_dir / "ProjectZomboid64.json"
    if not jpath.exists():
        raise RuntimeError("ProjectZomboid64.json 없음")
    # 백업 (최초 1회)
    for fname, sub in OPT_CLASS_TARGETS.items():
        _backup_once(game_dir, Path(sub) / fname)
    _backup_once(game_dir, Path("ProjectZomboid64.json"))
    _backup_once(game_dir, Path("ProjectZomboid64.bat"))
    # class 교체 + 바이트 검증
    for fname, sub in OPT_CLASS_TARGETS.items():
        src, dst = conf / fname, game_dir / sub / fname
        shutil.copy2(src, dst)
        if dst.read_bytes() != src.read_bytes():
            raise RuntimeError(f"복사 검증 실패: {fname}")
    # json (스팀 런처가 읽는 쪽): vmArgs의 Xms/Xmx만 교체, 나머지 보존
    data = json.loads(jpath.read_text(encoding="utf-8"))
    data["vmArgs"] = _patched_vmargs(data.get("vmArgs", []), mb)
    jpath.write_text(json.dumps(data, indent=2), encoding="utf-8")
    # bat (직접 실행용): 모든 -Xms/-Xmx 치환 — 바닐라 bat는 1번째 실행 라인이 -Xmx3072m 고정이라
    # json만 고치면 bat 실행 유저는 3GB로 돌게 됨. 두 라인 다 치환해야 함.
    bpath = game_dir / "ProjectZomboid64.bat"
    if bpath.exists():
        raw = bpath.read_text(encoding="utf-8", errors="replace")
        raw = _XMX_RE.sub(f"-Xmx{mb}m", raw)
        raw = _XMS_RE.sub(f"-Xms{mb}m", raw)
        bpath.write_text(raw, encoding="utf-8")
    return mb


def restore_pz_optimization(game_dir: Path) -> int:
    """puppet_opt_backup/의 원본을 전부 되돌린다. 반환: 복원한 파일 수."""
    bdir = game_dir / OPT_BACKUP_DIRNAME
    if not bdir.exists():
        raise RuntimeError("백업이 없음 — Steam '게임 파일 무결성 검사'로 복원해줘")
    n = 0
    for src in bdir.rglob("*"):
        if src.is_file():
            dst = game_dir / src.relative_to(bdir)
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src, dst)
            n += 1
    return n


def pz_optimize_state(game_dir):
    """('applied'|'partial'|'none', json의 Xmx MB). partial = class 일부만 패치 상태(게임 업데이트 등)."""
    conf = opt_conf_dir()
    if conf is None or game_dir is None:
        return ("none", 0)
    matched = total = 0
    for fname, sub in OPT_CLASS_TARGETS.items():
        p = conf / fname
        if not p.exists():
            continue
        total += 1
        t = game_dir / sub / fname
        try:
            if t.exists() and t.read_bytes() == p.read_bytes():
                matched += 1
        except OSError:
            pass
    heap = 0
    try:
        data = json.loads((game_dir / "ProjectZomboid64.json").read_text(encoding="utf-8"))
        for a in data.get("vmArgs", []):
            m = re.match(r"-Xmx(\d+)m$", str(a), re.I)
            if m:
                heap = int(m.group(1))
    except Exception:
        pass
    if total and matched == total and heap == half_ram_mb():
        return ("applied", heap)
    if matched > 0:
        return ("partial", heap)
    return ("none", heap)


def run_elevated_optimizer(action: str) -> bool:
    """관리자 권한으로 자기 자신을 --pz-optimize/--pz-restore 플래그로 재실행 (UAC 프롬프트).
       반환: 승격 프로세스 실행 성공 여부 (거부/실패 시 False)."""
    if os.name != "nt":
        return False
    import ctypes
    flag = "--pz-optimize" if action == "apply" else "--pz-restore"
    if getattr(sys, "frozen", False):
        exe, params = sys.executable, flag
    else:
        exe = sys.executable
        params = f'"{os.path.abspath(__file__)}" {flag}'
    try:
        r = ctypes.windll.shell32.ShellExecuteW(None, "runas", exe, params, None, 1)
        return int(r) > 32
    except Exception:
        return False


def _optimizer_cli(action: str):
    """--pz-optimize / --pz-restore 진입점 (승격 헬퍼). 단일 인스턴스 락을 안 잡으므로
       본체가 떠 있는 상태에서도 동작. 결과만 메시지박스로 알리고 종료."""
    _app = QApplication(sys.argv)
    try:
        game = find_pz_dir()
        if game is None:
            raise RuntimeError("Project Zomboid 설치 폴더를 못 찾음")
        if pz_running():
            raise RuntimeError("Project Zomboid가 실행 중이라 파일을 교체할 수 없음.\n게임 종료 후 다시 시도해줘.")
        if action == "apply":
            mb = apply_pz_optimization(game)
            QMessageBox.information(None, "게임 최적화",
                                    f"최적화 적용 완료!\n\n힙 메모리: {mb:,} MB (전체 RAM의 절반)\n경로: {game}")
        else:
            n = restore_pz_optimization(game)
            QMessageBox.information(None, "게임 최적화", f"원본 복원 완료 ({n}개 파일)\n경로: {game}")
        sys.exit(0)
    except Exception as e:
        QMessageBox.warning(None, "게임 최적화 실패", str(e))
        sys.exit(1)


# ═══════════════════════════════════════════════════════════════════════════════
#  코어: 수신 워커 (스레드 + asyncio -> Qt 시그널, 플랫폼/게임 중립)
# ═══════════════════════════════════════════════════════════════════════════════
class DonationWorker(QObject):
    donation = pyqtSignal(int, str, str)   # amount, sender, message
    status   = pyqtSignal(str, str)        # text, color(hex)
    resolved = pyqtSignal(str, str)        # uuid, display_name
    failed   = pyqtSignal(str)             # 멈춤
    note     = pyqtSignal(str)             # 로그용 (안 멈춤)
    adult_blocked = pyqtSignal()           # 19세 방송으로 전환 감지 (멈춤 + 게이트 복귀)

    def __init__(self, source, channel_text, nid_aut="", nid_ses="",
                 reconnect_sec=5.0, reconnect_max=60.0):
        super().__init__()
        self.source = source               # ← 어떤 수신 방식이든 DonationSource 만 받는다
        self.channel_text = channel_text
        self.nid_aut = nid_aut or None
        self.nid_ses = nid_ses or None
        self.reconnect = reconnect_sec     # [4] 백오프 시작값 (연결 성공 후엔 여기로 리셋)
        self.reconnect_max = reconnect_max # [4] 백오프 상한
        self._backoff = reconnect_sec
        self._attempt = 0                  # 연속 실패 재접속 카운터 (성공 시 리셋)
        self._had_conn = False             # 이번 시도에서 CONNECTED 를 받았는지
        self._stop = False
        self._thread = None
        self.loop = None
        self._last_note = None

    def _note_once(self, msg):
        if msg != self._last_note:
            self._last_note = msg
            self.note.emit(msg)

    def _emit(self, d):                    # Donation -> Qt 시그널
        self.donation.emit(d.amount, d.sender, d.message)

    def _on_src_event(self, kind, detail=""):
        """ChzzkpySource 연결 수명주기 통지 → 상태/로그 릴레이. (소스 루프 스레드에서 호출되지만
           pyqtSignal.emit 은 스레드 세이프 — queued connection 으로 GUI 스레드에 전달됨)"""
        if kind == "connected":
            self._had_conn = True
            self._attempt = 0
            self.status.emit("연결됨", "#5dcaa5")
            self.note.emit("치지직 연결됨 ✓  (이 시점 이후의 후원부터 수신)")
        elif kind == "ws_reconnect":
            self.status.emit("재연결 중…", "#ef9f27")
            self.note.emit("소켓 재연결 중 (서버측 종료/채팅채널 변경) — 자동 복구")
        elif kind == "broadcast_close":
            self.note.emit("방송 종료 신호 수신")
        elif kind == "stale":
            self.note.emit(f"⚠ Heartbeat/수신 두절 {detail}초 — 죽은 연결로 판정, 강제 재접속")
        elif kind == "client_error":
            self.note.emit("수신 처리 오류 (연결은 유지): " + detail)

    def start(self):
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def stop(self):
        self._stop = True
        try:
            self.source.request_close()            # 스레드 세이프 플래그 — 워치독이 곧바로 감지
        except Exception:
            pass
        if self.loop is not None:
            try:
                fut = asyncio.run_coroutine_threadsafe(self.source.close(), self.loop)
                fut.result(timeout=3)
            except Exception:
                pass

    def _run(self):
        self.loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self.loop)

        uuid, name = self.loop.run_until_complete(self.source.resolve_channel(self.channel_text))
        if not uuid:
            self.failed.emit(f"채널을 못 찾음 ({name}) — URL · 채널명 · UUID 확인해줘")
            self.status.emit("대기 중", "#5f5e5a")
            return
        self.resolved.emit(uuid, name or "")

        while not self._stop:
            self._had_conn = False
            grow_backoff = True            # 이번 실패를 백오프 증가에 반영할지 (방송 대기는 제외)
            try:
                self.status.emit("연결 중…", "#ef9f27")
                if self._attempt > 0:
                    self.note.emit(f"재접속 시도 #{self._attempt}")
                self.loop.run_until_complete(
                    self.source.connect(uuid, self._emit, self.nid_aut, self.nid_ses,
                                        on_event=self._on_src_event))
                if self._stop:
                    break
                self._note_once("연결 끊김 — 재접속 대기 중")
                self.status.emit("재접속 대기…", "#ef9f27")
            except AdultVerificationRequired:
                self.adult_blocked.emit()
                return
            except ChannelOffline:
                if self._stop:
                    break
                grow_backoff = False                  # 방송 대기: 고정 주기 폴링 (백오프 없음)
                self._backoff = self.reconnect
                self._attempt = 0                     # 오프라인 = 치지직 응답은 정상 → 실패 카운트 리셋
                self._note_once("방송이 꺼져 있어. 방송 시작하면 자동으로 연결됨.")
                self.status.emit("방송 대기 중", "#ef9f27")
            except SourceError as e:
                if self._stop:
                    break
                self.note.emit(f"연결 끊김: {e}")
                self.status.emit("재접속 대기…", "#ef9f27")
            except Exception as e:
                if self._stop:
                    break
                self.note.emit(f"연결 오류: {type(e).__name__}: {e}")
                self.status.emit("재접속 대기…", "#ef9f27")

            # ── [4] 지수 백오프: 5→10→20→40→60(최대). 연결에 성공했던 시도 후엔 5초부터 다시 ──
            # 방송 대기(오프라인)는 grow_backoff=False — 5초 고정 폴링 + 시도 카운트/로그 제외
            if self._had_conn:
                self._backoff = self.reconnect
                self._attempt = 0
            wait = self._backoff
            if grow_backoff and not self._had_conn:
                self._backoff = min(self._backoff * 2, self.reconnect_max)
                self._attempt += 1
            self._note_once(f"{int(wait)}초 후 재접속…")
            self._sleep(wait)

        self.status.emit("대기 중", "#5f5e5a")

    def _sleep(self, sec):
        end = time.monotonic() + sec
        while time.monotonic() < end and not self._stop:
            time.sleep(0.2)


# ── 메인 창 ───────────────────────────────────────────────────────────────────
DARK_QSS = """
QWidget { background:#23252b; color:#e8e8ea; font-family:'Malgun Gothic','맑은 고딕',sans-serif; font-size:13px; }
QLineEdit, QComboBox { background:#1b1d22; border:1px solid rgba(255,255,255,0.12); border-radius:8px; padding:7px 10px; color:#e8e8ea; }
QLineEdit:focus, QComboBox:focus { border:1px solid #1d9e75; }
QLineEdit:disabled { color:#5f5e5a; }
QTextEdit { background:#15171b; border:1px solid rgba(255,255,255,0.08); border-radius:8px; color:#b8bac0; font-family:Consolas,monospace; font-size:12px; }
QPushButton { background:#2b2e36; border:1px solid rgba(255,255,255,0.15); border-radius:8px; padding:7px 14px; color:#e8e8ea; }
QPushButton:hover { background:#343843; }
QPushButton#start { background:#1d9e75; color:#04342c; border:none; font-weight:bold; padding:10px 20px; }
QPushButton#start:hover { background:#22b384; }
QPushButton#start:disabled { background:#2b2e36; color:#5f5e5a; border:1px solid rgba(255,255,255,0.12); }
QPushButton#verify { background:#1d9e75; color:#04342c; border:none; font-weight:bold; padding:10px 24px; }
QPushButton#verify:hover { background:#22b384; }
QPushButton#verify:disabled { background:#2b2e36; color:#5f5e5a; border:1px solid rgba(255,255,255,0.12); }
QPushButton#stop  { background:#a32d2d; color:#ffe; border:none; font-weight:bold; padding:10px 20px; }
QPushButton#link  { background:transparent; border:none; color:#85b7eb; padding:2px; }
QCheckBox { color:#cfd0d4; font-size:12px; }
QLabel#muted { color:#9a9ca3; font-size:12px; }
QLabel#hint  { color:#6f7178; font-size:11px; }
QLabel#tier  { background:#2b2e36; border-radius:6px; padding:7px 10px; font-size:12px; color:#cfd0d4; }
QLabel#brand { font-size:13px; font-weight:bold; color:#e8e8ea; }
QLabel#ver   { color:#6f7178; font-size:11px; }
QLabel#sect  { font-size:15px; font-weight:bold; color:#e8e8ea; }
QLabel#welcome { font-size:20px; color:#e8e8ea; }
QLabel#err   { font-size:18px; font-weight:bold; color:#e24b4a; }
QLabel#linkok { color:#5dcaa5; font-weight:bold; }
QFrame#sep { background:rgba(255,255,255,0.08); max-height:1px; }
"""


def make_header() -> QWidget:
    """모든 화면 상단 공용 바: 로고 + '치지직 API Launcher' + 버전."""
    bar = QWidget()
    h = QHBoxLayout(bar); h.setContentsMargins(0, 0, 0, 4); h.setSpacing(8)
    ico = resource_path(ICON_FILE)
    if os.path.exists(ico):
        pm = QPixmap(ico)
        if not pm.isNull():
            logo = QLabel()
            logo.setPixmap(pm.scaled(28, 28, Qt.KeepAspectRatio, Qt.SmoothTransformation))
            h.addWidget(logo)
    brand = QLabel("치지직 API Launcher"); brand.setObjectName("brand")
    h.addWidget(brand); h.addStretch(1)
    ver = QLabel(VERSION); ver.setObjectName("ver")
    h.addWidget(ver)
    return bar



class RewardPresetDialog(QDialog):
    """리워드 프리셋 편집 창 — 편집·불러오기·초기화·저장을 한 곳에서 처리.
       저장하면 reward_preset.json 에 기록되고, MainWindow._load_reward_tiers 가 이 파일을 읽는다."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("리워드 프리셋 편집")
        self.setWindowFlags(self.windowFlags() & ~Qt.WindowContextHelpButtonHint)
        ico = resource_path(ICON_FILE)
        if os.path.exists(ico):
            self.setWindowIcon(QIcon(ico))
        self.setFixedSize(620, 800)
        self.rows = []      # [(row_widget, amt_edit, feat_combo, del_btn), ...]
        self.locked = False
        self._build()
        self.setStyleSheet(DARK_QSS)
        # 저장된 프리셋이 있으면 그걸 잠금 상태로, 없으면 기본 티어를 편집 상태로 (3.5 잠금 동작 유지)
        saved = load_reward_preset()
        self._load_rows(saved if saved else ZomboidAdapter.DEFAULT_REWARD_TIERS)
        self._set_locked(bool(saved))
        if saved:
            self._status_msg(f"프리셋 적용됨 ({len(self.rows)}개) — ‘다시 편집’으로 잠금 해제", ok=True)

    # --- 빌드 ---
    def _build(self):
        root = QVBoxLayout(self)
        root.setContentsMargins(18, 18, 18, 18); root.setSpacing(10)
        root.addWidget(self._muted("금액 ↔ 기능 편집 후 ‘저장’ (정확히 일치하는 금액만 발동)"))

        # 행이 늘어날 수 있으므로 스크롤 영역 안에 티어 테이블
        self.tiers_host = QWidget()
        self.tiers_box = QVBoxLayout(self.tiers_host)
        self.tiers_box.setContentsMargins(0, 0, 6, 0); self.tiers_box.setSpacing(4)
        self.tiers_box.setAlignment(Qt.AlignTop)
        scroll = QScrollArea(); scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.NoFrame)
        scroll.setWidget(self.tiers_host)
        root.addWidget(scroll, 1)

        trow = QHBoxLayout()
        self.add_btn = QPushButton("+ 행 추가"); self.add_btn.setObjectName("link")
        self.add_btn.clicked.connect(lambda: self._add_row())
        trow.addWidget(self.add_btn)
        trow.addStretch(1)
        self.import_btn = QPushButton("불러오기"); self.import_btn.setObjectName("link")
        self.import_btn.clicked.connect(self._import)
        trow.addWidget(self.import_btn)
        self.reset_btn = QPushButton("초기화"); self.reset_btn.setObjectName("link")
        self.reset_btn.clicked.connect(self._reset)
        trow.addWidget(self.reset_btn)
        root.addLayout(trow)

        sep = QFrame(); sep.setObjectName("sep"); sep.setFixedHeight(1)
        root.addWidget(sep)

        brow = QHBoxLayout()
        self.status = QLabel(""); self.status.setObjectName("muted")
        brow.addWidget(self.status, 1)
        self.save_btn = QPushButton("저장"); self.save_btn.setObjectName("start")
        self.save_btn.clicked.connect(self._save)
        brow.addWidget(self.save_btn)
        self.edit_btn = QPushButton("다시 편집")
        self.edit_btn.clicked.connect(self._unlock)
        brow.addWidget(self.edit_btn)
        close_btn = QPushButton("닫기")
        close_btn.clicked.connect(self.accept)
        brow.addWidget(close_btn)
        root.addLayout(brow)

    def _muted(self, t):
        l = QLabel(t); l.setObjectName("muted"); return l

    def _status_msg(self, text, ok):
        self.status.setText(text)
        self.status.setStyleSheet("color:#5dcaa5;" if ok else "color:#e24b4a;")

    # 잠금 상태 콤보박스: 드롭다운 화살표까지 완전히 안 보이게 (조작 여지 자체를 시각적으로 제거)
    LOCKED_COMBO_QSS = ("QComboBox::drop-down { width:0px; border:none; }"
                         "QComboBox::down-arrow { width:0px; height:0px; image:none; }")

    def _set_locked(self, locked):
        """저장 후 잠금 / ‘다시 편집’으로 해제 (3.5 잠금 UX). 행 위젯·버튼 표시를 일괄 전환."""
        self.locked = locked
        for _row, amt_edit, feat_combo, del_btn in self.rows:
            amt_edit.setEnabled(not locked)
            feat_combo.setEnabled(not locked)
            feat_combo.setStyleSheet(self.LOCKED_COMBO_QSS if locked else "")
            del_btn.setVisible(not locked)
        self.add_btn.setVisible(not locked)
        self.import_btn.setVisible(not locked)
        self.save_btn.setVisible(not locked)
        self.edit_btn.setVisible(locked)

    def _unlock(self):
        self._set_locked(False)
        self._status_msg("편집 모드 — 수정 후 ‘저장’", ok=True)

    # --- 행 관리 ---
    def _clear_rows(self):
        for row, _amt, _feat, _del in self.rows:
            self.tiers_box.removeWidget(row); row.deleteLater()
        self.rows = []

    def _load_rows(self, tiers):
        """{amount: featureId} -> 편집 테이블 재구성 (금액 오름차순). featureId 미등록 항목은 스킵."""
        self._clear_rows()
        items = []
        for k, v in tiers.items():
            try:
                amt = int(k)
            except (TypeError, ValueError):
                continue
            if amt > 0 and v in ZomboidAdapter.FEATURES:
                items.append((amt, v))
        for amt, fid in sorted(items):
            self._add_row(amt, fid)

    def _add_row(self, amount=None, feature_id=None):
        row = QWidget()
        h = QHBoxLayout(row); h.setContentsMargins(0, 0, 0, 0); h.setSpacing(6)
        amt_edit = QLineEdit("" if amount is None else str(amount))
        amt_edit.setPlaceholderText("금액")
        amt_edit.setFixedWidth(90)
        feat_combo = QComboBox()
        for fid, label in ZomboidAdapter.FEATURES.items():
            feat_combo.addItem(label, fid)
        if feature_id:
            idx = feat_combo.findData(feature_id)
            if idx >= 0:
                feat_combo.setCurrentIndex(idx)
        del_btn = QPushButton("✕"); del_btn.setObjectName("link"); del_btn.setFixedWidth(28)
        del_btn.clicked.connect(lambda: self._remove_row(row))
        if self.locked:
            amt_edit.setEnabled(False)
            feat_combo.setEnabled(False)
            feat_combo.setStyleSheet(self.LOCKED_COMBO_QSS)
            del_btn.hide()
        h.addWidget(amt_edit); h.addWidget(feat_combo, 1); h.addWidget(del_btn)
        self.tiers_box.addWidget(row)
        self.rows.append((row, amt_edit, feat_combo, del_btn))

    def _remove_row(self, row):
        for i, (w, _amt, _feat, _del) in enumerate(self.rows):
            if w is row:
                self.rows.pop(i)
                break
        self.tiers_box.removeWidget(row)
        row.deleteLater()

    # --- 저장 / 불러오기 / 초기화 ---
    def _collect(self):
        """편집 테이블 -> {amount: featureId}. 문제 있으면 status 표시 후 None."""
        tiers = {}
        for _row, amt_edit, feat_combo, _del in self.rows:
            txt = amt_edit.text().strip().replace(",", "")
            if not txt:
                continue
            try:
                amt = int(txt)
            except ValueError:
                self._status_msg(f"⚠ 잘못된 금액: {txt!r}", ok=False); return None
            if amt <= 0:
                self._status_msg(f"⚠ 금액은 1 이상이어야 함: {amt}", ok=False); return None
            if amt in tiers:
                self._status_msg(f"⚠ 금액 중복: {amt:,}", ok=False); return None
            tiers[amt] = feat_combo.currentData()
        if not tiers:
            self._status_msg("⚠ 저장할 티어가 없음 — 최소 1개 필요", ok=False); return None
        return tiers

    def _save(self):
        tiers = self._collect()
        if tiers is None:
            return
        save_reward_preset(tiers)               # Zomboid 폴더의 reward_preset.json에 기록
        self._load_rows(tiers)                  # 금액 오름차순으로 재렌더
        self._set_locked(True)                  # 저장 = 잠금 (‘다시 편집’으로 해제)
        self._status_msg(f"저장됨 ({len(tiers)}개) — 잠금 상태, ‘다시 편집’으로 수정", ok=True)

    def _import(self):
        """리워드 프리셋(JSON, {amount: featureId}) 파일 -> 편집 테이블에 로드.
           바로 저장하지 않음 — 내용 확인 후 ‘저장’을 눌러야 실제 적용."""
        fn, _ = QFileDialog.getOpenFileName(
            self, "리워드 프리셋 불러오기", str(Path.home()), "JSON (*.json)")
        if not fn:
            return
        try:
            raw = json.loads(Path(fn).read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as e:
            self._status_msg(f"⚠ 불러오기 실패: {e}", ok=False)
            return
        if not isinstance(raw, dict) or not raw:
            self._status_msg("⚠ 형식이 올바르지 않음 ({amount: featureId} JSON)", ok=False)
            return
        self._load_rows(raw)
        if not self.rows:
            # 전부 무효 — 이전 테이블은 이미 날아갔으므로 기본 티어로 복구
            self._load_rows(ZomboidAdapter.DEFAULT_REWARD_TIERS)
            self._status_msg("⚠ 유효한 티어가 없음 (featureId 불일치)", ok=False)
            return
        self._status_msg(f"불러옴 ({len(self.rows)}개) — ‘저장’을 눌러야 적용됨", ok=True)

    def _reset(self):
        box = QMessageBox(self)
        box.setWindowTitle("리워드 프리셋 초기화")
        box.setText("저장된 리워드 프리셋을 삭제하고 기본 티어로 되돌립니다.\n계속할까요?")
        yes_btn = box.addButton("초기화", QMessageBox.DestructiveRole)
        box.addButton("취소", QMessageBox.RejectRole)
        box.exec_()
        if box.clickedButton() is not yes_btn:
            return
        reset_reward_preset()                   # reward_preset.json 삭제 -> 기본 티어 사용 상태
        self._load_rows(ZomboidAdapter.DEFAULT_REWARD_TIERS)
        self._set_locked(False)                 # 프리셋이 사라졌으니 바로 편집 가능
        self._status_msg("초기화됨 — 기본 티어 사용", ok=True)


class MainWindow(QWidget):
    def __init__(self, preset=None):
        super().__init__()
        self.preset = preset or {}        # 런처에서 넘어온 {channel,uuid,name,autostart}
        self.setWindowTitle("PongDu Launcher  "+VERSION)
        ico = resource_path(ICON_FILE)
        if os.path.exists(ico):
            self.setWindowIcon(QIcon(ico))
        self.resize(620, 980)
        self.setFixedSize(620, 980)
        self.adapter = ZomboidAdapter()
        self.worker = None
        self.cfg = load_config()
        self._returning = False                       # 게이트 복귀 중복 방지
        self.guard = None                             # PZ 종료 + 19세 전환 감시 (연동 중에만)
        # 티어 편집은 게이트(RewardPresetDialog)로 이동 — 여기선 reward_preset.json만 읽는다.
        self._load_reward_tiers()
        self._build()
        self._restore()
        if self.preset.get("autostart"):
            ch = self.preset.get("channel", "")
            if ch:
                self.channel_input.setText(ch)
            QTimer.singleShot(300, self._start)   # 창 뜨고 나서 워커 시작 (방송은 이미 라이브 확인됨)

    def _build(self):
        root = QVBoxLayout(self)
        root.setContentsMargins(18, 18, 18, 18)
        root.setSpacing(10)


        title = QLabel("치지직 → 좀보이드 후원연동")
        title.setStyleSheet("font-size:15px; font-weight:bold;")
        root.addWidget(title)

        # 채널 — 런처에서 넘어왔으면 입력칸 대신 '연결됨' 라벨로 잠금
        self.channel_input = QLineEdit()
        self.channel_input.setPlaceholderText("https://chzzk.naver.com/live/…  또는  채널명")
        self.channel_state = QLabel(" "); self.channel_state.setObjectName("muted")
        if self.preset.get("name"):
            conn = QLabel(f"채널 연결됨: {self.preset['name']}"); conn.setObjectName("linkok")
            root.addWidget(conn)
            self.channel_input.hide(); self.channel_state.hide()
        else:
            root.addWidget(self._muted("치지직 채널  —  URL · 채널명 · UUID 아무거나"))
            root.addWidget(self.channel_input)
            root.addWidget(self.channel_state)

        # 경로
        root.addWidget(self._muted("rewards.txt 경로"))
        prow = QHBoxLayout()
        self.path_input = QLineEdit(); self.path_input.setReadOnly(True)
        prow.addWidget(self.path_input, 1)
        redetect = QPushButton("다시 탐지"); redetect.setObjectName("link"); redetect.clicked.connect(self._autodetect_path)
        choose = QPushButton("직접 지정"); choose.setObjectName("link"); choose.clicked.connect(self._choose_path)
        prow.addWidget(redetect); prow.addWidget(choose)
        root.addLayout(prow)

        # 시작/중지 + 상태
        srow = QHBoxLayout()
        self.start_btn = QPushButton("연동 시작"); self.start_btn.setObjectName("start")
        self.start_btn.clicked.connect(self._toggle)
        srow.addWidget(self.start_btn)
        self.status_dot = QLabel("●"); self.status_dot.setStyleSheet("color:#5f5e5a; font-size:14px;")
        self.status_text = QLabel("대기 중"); self.status_text.setObjectName("muted")
        srow.addWidget(self.status_dot); srow.addWidget(self.status_text); srow.addStretch(1)
        root.addLayout(srow)

        root.addWidget(self._sep())

        # 확정된 리워드 티어 (읽기 전용 — 편집은 게이트의 ‘리워드 프리셋 편집하기’)
        root.addWidget(self._muted("리워드 티어  —  편집은 게이트의 ‘리워드 프리셋 편집하기’에서"))
        self.tiers_host = QWidget()
        self.tiers_grid = QGridLayout(self.tiers_host)
        self.tiers_grid.setContentsMargins(0, 0, 6, 0)
        self.tiers_grid.setHorizontalSpacing(6); self.tiers_grid.setVerticalSpacing(4)
        tscroll = QScrollArea(); tscroll.setWidgetResizable(True)
        tscroll.setFrameShape(QFrame.NoFrame)
        tscroll.setWidget(self.tiers_host)
        tscroll.setFixedHeight(190)
        root.addWidget(tscroll)
        self._render_tier_display()

        root.addWidget(self._sep())

        # 테스트 후원
        trow = QHBoxLayout()
        trow.addWidget(self._muted("테스트 후원"))
        self.test_combo = QComboBox()
        self._build_test_combo()
        trow.addWidget(self.test_combo, 1)
        inject = QPushButton("확인"); inject.clicked.connect(self._inject_test)
        trow.addWidget(inject)
        root.addLayout(trow)

        # 로그
        root.addWidget(self._muted("실시간 도네 로그"))
        self.log = QTextEdit(); self.log.setReadOnly(True); self.log.setMinimumHeight(140)
        root.addWidget(self.log, 1)

        self.setStyleSheet(DARK_QSS)

    # --- 헬퍼 ---
    def _muted(self, t):
        l = QLabel(t); l.setObjectName("muted"); return l

    def _sep(self):
        f = QFrame(); f.setObjectName("sep"); f.setFixedHeight(1); return f

    def _render_tier_display(self):
        """adapter.reward_tiers -> 읽기 전용 2열 라벨 그리드 (금액 오름차순)."""
        while self.tiers_grid.count():
            item = self.tiers_grid.takeAt(0)
            w = item.widget()
            if w:
                w.deleteLater()
        for i, (amt, fid) in enumerate(sorted(self.adapter.reward_tiers.items())):
            label = self.adapter.FEATURES.get(fid, fid)
            l = QLabel(f"{amt:,} — {label}")
            l.setObjectName("tier")
            self.tiers_grid.addWidget(l, i // 2, i % 2)

    def _build_test_combo(self):
        self.test_combo.clear()
        for amt, fid in sorted(self.adapter.reward_tiers.items()):
            label = self.adapter.FEATURES.get(fid, fid)
            self.test_combo.addItem(f"{amt:,} — {label}", amt)

    # --- 설정 복원/저장 ---
    def _load_reward_tiers(self):
        """Zomboid/reward_preset.json > 코드 기본값 순. (편집은 게이트의 RewardPresetDialog에서)
           featureId가 FEATURES에 없는 항목/파싱 안 되는 키는 무시(방어적 마이그레이션)."""
        raw = load_reward_preset()
        if not isinstance(raw, dict) or not raw:
            return
        loaded = {}
        for k, v in raw.items():
            try:
                amt = int(k)
            except (TypeError, ValueError):
                continue
            if amt > 0 and v in self.adapter.FEATURES:
                loaded[amt] = v
        if loaded:
            self.adapter.reward_tiers = loaded

    def _restore(self):
        self.channel_input.setText(self.cfg.get("channel", ""))
        manual = self.cfg.get("path", "")
        if manual:
            self.adapter.path = Path(manual)
            self.path_input.setText(manual)
        else:
            self._autodetect_path()

    def _persist(self):
        self.cfg.update({
            "channel": self.channel_input.text().strip(),
            "path": str(self.adapter.path) if self.adapter.path else "",
        })
        save_config(self.cfg)

    def closeEvent(self, e):
        self._persist()
        if self.worker:
            self.worker.stop()
        super().closeEvent(e)

    # --- 경로 ---
    def _autodetect_path(self):
        p = self.adapter.find_path()
        self.adapter.path = p
        if p:
            self.path_input.setText(str(p))
            exists = p.exists()
            self._log(f"경로 {'탐지' if exists else '예정'}: {p}" + ("" if exists else "  (첫 후원 때 생성됨)"))
        else:
            self.path_input.setText("")
            self._log("rewards.txt 경로를 못 찾음. ‘직접 지정’으로 골라줘.")

    def _choose_path(self):
        start_dir = str(self.adapter.path.parent) if self.adapter.path else str(Path.home())
        fn, _ = QFileDialog.getSaveFileName(self, "rewards.txt 위치 선택", start_dir, "Text (*.txt)")
        if fn:
            self.adapter.path = Path(fn)
            self.path_input.setText(fn)
            self._log(f"경로 수동 지정: {fn}")

    # --- 시작/중지 ---
    def _toggle(self):
        if self.worker is None:
            self._start()
        else:
            self._back_to_gate()        # 중지 누르면 완전 초기 게이트로 복귀

    def _start(self):
        ch = self.channel_input.text().strip()
        if not ch:
            self._log("채널을 먼저 입력해줘."); return
        if self.adapter.path is None:
            self._log("rewards.txt 경로가 없어. ‘직접 지정’으로 골라줘."); return
        self._persist()
        source = ChzzkpySource()                       # ← 수신 어댑터. 공식 API 가면 여기만 교체.
        self.worker = DonationWorker(source, ch)
        self.worker.donation.connect(self._on_donation)
        self.worker.status.connect(self._on_status)
        self.worker.resolved.connect(self._on_resolved)
        self.worker.failed.connect(self._on_failed)
        self.worker.note.connect(self._log)
        self.worker.adult_blocked.connect(self._adult_to_gate)   # chzzkpy가 19세 전환 잡으면(최대 ~58s)
        self.worker.start()
        self.start_btn.setText("중지"); self.start_btn.setObjectName("stop"); self.setStyleSheet(DARK_QSS)
        self.channel_input.setEnabled(False)
        uuid = self.preset.get("uuid")
        if self.preset.get("autostart") and uuid:      # 게이트를 거쳐 들어온 경우만 감시
            self.guard = MainGuard(uuid)               # PZ 종료 + 19세 전환을 짧은 주기로 직접 폴링
            self.guard.pz_lost.connect(self._pz_to_gate)
            self.guard.adult_on.connect(self._adult_to_gate)
            self.guard.start()
        self._log("연동 시작…")

    def _kill_guard(self):
        if self.guard is not None:
            self.guard.shutdown(); self.guard = None

    def _stop(self):
        self._kill_guard()
        if self.worker:
            self.worker.stop(); self.worker = None
        self.start_btn.setText("연동 시작"); self.start_btn.setObjectName("start"); self.setStyleSheet(DARK_QSS)
        self.channel_input.setEnabled(True)
        self._on_status("대기 중", "#5f5e5a")
        self._log("중지됨.")

    def _back_to_gate(self, warn_adult=False):
        """워커 정리하고 완전 초기 상태의 게이트 창으로 돌아간다 (중지 / PZ 종료 / 19세 전환 공통).
           warn_adult=True 면 복귀 전에 경고창. _returning 을 먼저 세워 경고창 중 중복 트리거를 막는다."""
        if self._returning:
            return
        self._returning = True
        self._kill_guard()
        if self.worker:
            self.worker.stop(); self.worker = None
        if warn_adult:
            QMessageBox.warning(self, "연령제한 감지",
                                "방송에 연령제한이 걸려있습니다.\n연동을 중단하고 로그인 화면으로 돌아갑니다.")
        self._persist()
        preset = {
            "channel": self.channel_input.text().strip(),
            "uuid": self.preset.get("uuid", ""),
            "name": self.preset.get("name", ""),
        }
        self._gate = LauncherWindow(preset=preset if preset["uuid"] else None)
        self._gate.show()
        self.close()

    def _pz_to_gate(self):
        if self._returning:
            return
        self._log("Project Zomboid 종료 감지 — 게이트로 돌아갑니다.")
        self._back_to_gate()

    def _adult_to_gate(self):
        self._back_to_gate(warn_adult=True)

    # --- 시그널 핸들러 ---
    def _on_donation(self, amount, sender, message):
        feature_id = self.adapter.reward_tiers.get(amount, "")
        self.adapter.write(amount, feature_id, sender, message)
        if feature_id:
            label = self.adapter.FEATURES.get(feature_id, feature_id)
            self._log(f"{sender}  {amount:,}원  →  {label}")
        else:
            self._log(f"{sender}  {amount:,}원  (통계만)")

    def _on_status(self, text, color):
        self.status_text.setText(text)
        self.status_dot.setStyleSheet(f"color:{color}; font-size:14px;")

    def _on_resolved(self, uuid, name):
        short = f"{uuid[:8]}…{uuid[-3:]}"
        label = name if name else short
        self.channel_state.setText(f"채널 인식됨 · {label}")
        self.channel_state.setStyleSheet("color:#5dcaa5; font-size:12px;")

    def _on_failed(self, msg):
        self._log("⚠ " + msg)
        self._stop()

    def _inject_test(self):
        amt = self.test_combo.currentData()
        if amt is None:
            self._log("테스트할 티어가 없음. 리워드 티어를 먼저 저장해줘."); return
        if self.adapter.path is None:
            self._log("경로가 없어서 테스트 불가. 경로 먼저 지정해줘."); return
        feature_id = self.adapter.reward_tiers.get(amt, "")
        self.adapter.write(amt, feature_id, "테스트후원자", "테스트")
        label = self.adapter.FEATURES.get(feature_id, feature_id or "?")
        self._log(f"[테스트] {amt:,}원 적용  →  {label}")

    def _log(self, msg):
        ts = time.strftime("%H:%M:%S")
        self.log.append(f"<span style='color:#6f7178'>{ts}</span>  {msg}")


# ═══════════════════════════════════════════════════════════════════════════════
#  런처 게이트: 채널확인 → 화이트리스트 → 방송 → PZ → 연동시작 → 메인창
# ═══════════════════════════════════════════════════════════════════════════════
class LauncherCore(QObject):
    """게이트용 비동기 워커. resolve/화이트리스트 검증 + 방송·PZ 폴링을 한 루프에서 돌린다."""
    resolved = pyqtSignal(str, str)   # uuid, name
    invalid  = pyqtSignal()           # 파싱 실패 or 화이트리스트 미등재
    live     = pyqtSignal(bool)       # 방송 on/off
    adult    = pyqtSignal(bool)       # 19세 방송 여부
    pz       = pyqtSignal(bool)       # PZ 실행 여부
    connected = pyqtSignal(bool)      # PZ 연결 상태

    def __init__(self):
        super().__init__()
        self.loop = None
        self._uuid = None
        self._polling = False
        self._ready = threading.Event()
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()
        self._ready.wait(2)

    def _run(self):
        self.loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self.loop)
        self.loop.call_soon(self._ready.set)
        self.loop.run_forever()

    def _submit(self, coro):
        return asyncio.run_coroutine_threadsafe(coro, self.loop)

    # --- 채널 확인 (확인 버튼) ---
    def verify(self, text):
        self._submit(self._verify(text))

    async def _verify(self, text):
        src = ChzzkpySource()                  # resolve 는 기존 어댑터 재사용
        try:
            uuid, name = await src.resolve_channel(text)
        except Exception:
            uuid, name = None, ""

        if FORCE_ONLINE:
            self.resolved.emit(uuid, name or "")
            return

        wl = await fetch_whitelist()           # 확인 시도마다 매번 GitHub에서 새로 받아옴 (캐시 없음)
        if uuid and uuid in wl:                # wl 비었으면(=로드 실패) 전원 차단 = fail-closed
            self.resolved.emit(uuid, name or "")
        else:
            self.invalid.emit()

    # --- 방송 / PZ 폴링 (체크리스트) ---
    def start_poll(self, uuid):
        self._uuid = uuid
        if not self._polling:
            self._submit(self._poll())

    async def _poll(self):
        self._polling = True
        while self._polling:
            live, adult = await fetch_status(self._uuid)
            self.live.emit(live)
            self.adult.emit(adult)
            try:
                running = await self.loop.run_in_executor(None, pz_running)
            except Exception:
                running = False
            self.pz.emit(running)
            try:
                connected = await self.loop.run_in_executor(None, pz_connected)
            except Exception:
                connected = False
            self.connected.emit(connected)
            await asyncio.sleep(3)

    def stop_poll(self):
        self._polling = False

    def shutdown(self):
        self._polling = False
        if self.loop is not None:
            self.loop.call_soon_threadsafe(self.loop.stop)


class MainGuard(QObject):
    """메인 연동 중 감시 워커: PZ 종료 + 19세 전환 + 인게임 이탈을 폴링해서 시그널만 쏜다."""
    pz_lost  = pyqtSignal()
    adult_on = pyqtSignal()

    def __init__(self, uuid):
        super().__init__()
        self.uuid = uuid
        self.loop = None
        self._polling = False
        self._pz_misses = 0
        self._conn_misses = 0
        self._ready = threading.Event()
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()
        self._ready.wait(2)

    def _run(self):
        self.loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self.loop)
        self.loop.call_soon(self._ready.set)
        self.loop.run_forever()

    def start(self):
        self._polling = True
        asyncio.run_coroutine_threadsafe(self._poll(), self.loop)

    async def _poll(self):
        while self._polling:
            _, adult = await fetch_status(self.uuid)
            if adult:                              # 19세 전환 → 즉시 알림 후 종료
                self._polling = False
                self.adult_on.emit()
                break
            try:
                running = await self.loop.run_in_executor(None, pz_running)
            except Exception:
                running = False
            if running:
                self._pz_misses = 0
            else:
                self._pz_misses += 1               # 일시적 오탐 방지로 2회 연속 미감지 시 복귀
                if self._pz_misses >= 2:
                    self._polling = False
                    self.pz_lost.emit()
                    break
            try:
                conn = await self.loop.run_in_executor(None, pz_connected)
            except Exception:
                conn = True                        # 오류 시 오탐 방지로 연결 유지
            if conn:
                self._conn_misses = 0
            else:
                self._conn_misses += 1             # 일시적 오탐 방지로 2회 연속 미감지 시 복귀
                if self._conn_misses >= 2:
                    self._polling = False
                    self.pz_lost.emit()
                    break
            await asyncio.sleep(3)

    def shutdown(self):
        self._polling = False
        if self.loop is not None:
            self.loop.call_soon_threadsafe(self.loop.stop)


class LauncherWindow(QWidget):
    def __init__(self, preset=None):
        super().__init__()
        self.setWindowTitle("PongDu Launcher  "+VERSION)
        ico = resource_path(ICON_FILE)
        if os.path.exists(ico):
            self.setWindowIcon(QIcon(ico))
        self.resize(620, 340)
        self.setFixedSize(620, 340)
        self.core = LauncherCore()
        self.core.resolved.connect(self._on_resolved)
        self.core.invalid.connect(self._on_invalid)
        self.core.live.connect(self._on_live)
        self.core.adult.connect(self._on_adult)
        self.core.pz.connect(self._on_pz)
        self.core.connected.connect(self._on_connected)
        self._uuid = ""; self._name = ""
        self._live = False; self._pz = False; self._adult = False; self._connected = False
        self._adult_warned = False
        self.main_win = None
        self.cfg = load_config()       # opt_mode 등 (MainWindow와 같은 config.json 공유)
        self._game_dir = find_pz_dir() # PZ 설치 폴더 (최적화용, 못 찾으면 None)
        self._build()
        self.setStyleSheet(DARK_QSS)
        # preset 있으면 UUID 확인 완료 상태(체크리스트)부터 시작
        if preset and preset.get("uuid"):
            self.input.setText(preset.get("channel", ""))
            self._on_resolved(preset["uuid"], preset.get("name", ""))
        # 실행 즉시 원클릭 최적화 (창 뜬 뒤 살짝 늦게 — 최초 1회만 확인창, 이후 자동 유지)
        QTimer.singleShot(400, self._opt_startup)

    # --- 빌드 ---
    def _build(self):
        root = QVBoxLayout(self)
        root.setContentsMargins(18, 18, 18, 18); root.setSpacing(10)
        self.stack = QStackedWidget()
        self.stack.addWidget(self._page_input())     # 0
        self.stack.addWidget(self._page_invalid())   # 1
        self.stack.addWidget(self._page_check())      # 2
        root.addWidget(self.stack, 1)

    def _muted(self, t):
        l = QLabel(t); l.setObjectName("muted"); return l

    def _sect(self, t):
        l = QLabel(t); l.setObjectName("sect"); return l

    def _page_input(self):
        w = QWidget(); v = QVBoxLayout(w); v.setContentsMargins(0, 8, 0, 0); v.setSpacing(10)
        v.addSpacing(55);
        v.addWidget(self._sect("치지직 채널 확인"))
        v.addWidget(self._muted("치지직 채널  —  URL · 채널명 · UUID 아무거나"))
        self.input = QLineEdit()
        self.input.setPlaceholderText("https://chzzk.naver.com/live/…  또는  채널명")
        self.input.textChanged.connect(lambda s: self.verify_btn.setEnabled(bool(s.strip())))
        self.input.returnPressed.connect(self._verify)
        v.addWidget(self.input)
        row = QHBoxLayout(); row.addStretch(1)
        self.verify_btn = QPushButton("확인"); self.verify_btn.setObjectName("verify")
        self.verify_btn.setEnabled(False)            # 디폴트: 텍스트 없으면 회색 비활성
        self.verify_btn.clicked.connect(self._verify)
        row.addWidget(self.verify_btn); row.addStretch(1)
        v.addSpacing(8); v.addLayout(row); v.addStretch(1)
        # 게임 최적화 상태 (하단 고정) — 적용/해제 토글
        orow = QHBoxLayout(); orow.addStretch(1)
        self.opt_status = QLabel(""); self.opt_status.setObjectName("hint")
        orow.addWidget(self.opt_status)
        self.opt_btn = QPushButton(""); self.opt_btn.setObjectName("link")
        self.opt_btn.clicked.connect(self._opt_toggle)
        self.opt_btn.hide()
        orow.addWidget(self.opt_btn); orow.addStretch(1)
        v.addLayout(orow)
        return w

    def _page_invalid(self):
        w = QWidget(); v = QVBoxLayout(w); v.setContentsMargins(0, 8, 0, 0); v.setSpacing(14)
        v.addWidget(self._sect("치지직 채널 확인"))
        v.addStretch(1)
        e = QLabel("유효하지 않은 채널입니다"); e.setObjectName("err"); e.setAlignment(Qt.AlignCenter)
        v.addWidget(e)
        row = QHBoxLayout(); row.addStretch(1)
        again = QPushButton("다시입력"); again.clicked.connect(self._retry)
        row.addWidget(again); row.addStretch(1)
        v.addLayout(row); v.addStretch(1)
        return w

    def _page_check(self):
        w = QWidget(); v = QVBoxLayout(w); v.setContentsMargins(0, 8, 0, 0); v.setSpacing(12)
        v.addWidget(self._sect("치지직 채널 확인"))
        self.welcome = QLabel(""); self.welcome.setObjectName("welcome")
        self.welcome.setAlignment(Qt.AlignCenter); self.welcome.setTextFormat(Qt.RichText)
        v.addWidget(self.welcome)
        v.addSpacing(4)
        self.r_uuid = self._check_row(); v.addWidget(self.r_uuid[0])
        self.r_live = self._check_row(); v.addWidget(self.r_live[0])
        self.r_pz   = self._check_row(); v.addWidget(self.r_pz[0])
        self.r_conn = self._check_row(); v.addWidget(self.r_conn[0])
        self.adult_warn = QLabel("⚠ 19세(성인) 방송은 연동할 수 없습니다")
        self.adult_warn.setObjectName("err"); self.adult_warn.setAlignment(Qt.AlignCenter)
        self.adult_warn.setVisible(False)
        v.addWidget(self.adult_warn)
        v.addSpacing(6)

        prow = QHBoxLayout(); prow.addStretch(1)
        self.edit_preset_btn = QPushButton("리워드 프리셋 편집하기"); self.edit_preset_btn.setObjectName("link")
        self.edit_preset_btn.clicked.connect(self._open_preset_dialog)
        prow.addWidget(self.edit_preset_btn)
        self.preset_status = self._muted(""); self.preset_status.setAlignment(Qt.AlignCenter)
        prow.addWidget(self.preset_status)
        prow.addStretch(1)
        v.addLayout(prow)
        self._refresh_preset_status()

        v.addSpacing(4)
        row = QHBoxLayout(); row.addStretch(1)
        back_btn = QPushButton("이전"); back_btn.clicked.connect(self._back_to_input)
        row.addWidget(back_btn)
        self.connect_btn = QPushButton("연동 시작"); self.connect_btn.setObjectName("start")
        self.connect_btn.setEnabled(False)
        self.connect_btn.clicked.connect(self._go_main)
        row.addWidget(self.connect_btn); row.addStretch(1)
        v.addLayout(row); v.addStretch(1)
        return w

    def _refresh_preset_status(self):
        """저장된 reward_preset.json 유무를 상태 라벨에 표시 (편집창 닫힐 때마다 갱신)."""
        saved = load_reward_preset() if PRESET_PATH.exists() else None
        if saved:
            self.preset_status.setText(f"프리셋 적용됨 ({len(saved)}개)")
            self.preset_status.setStyleSheet("color:#5dcaa5;")
        else:
            self.preset_status.setText("기본 티어 사용")
            self.preset_status.setStyleSheet("")

    def _open_preset_dialog(self):
        """리워드 프리셋 편집 창 — 편집·불러오기·초기화·저장을 전부 이 창에서 처리."""
        dlg = RewardPresetDialog(self)
        dlg.exec_()
        self._refresh_preset_status()

    def _check_row(self):
        w = QWidget(); l = QHBoxLayout(w); l.setContentsMargins(120, 0, 0, 0); l.setSpacing(10)
        dot = QLabel("●"); dot.setStyleSheet("color:#ef9f27; font-size:12px;")
        txt = QLabel("")
        l.addWidget(dot); l.addWidget(txt); l.addStretch(1)
        return w, dot, txt

    @staticmethod
    def _set_row(row, done, text):
        _, dot, txt = row
        dot.setStyleSheet(f"color:{'#5dcaa5' if done else '#ef9f27'}; font-size:12px;")
        txt.setText(text)
        txt.setStyleSheet(f"color:{'#e8e8ea' if done else '#9a9ca3'};")

    # --- PZ 원클릭 최적화 ---
    def _opt_refresh_ui(self):
        """하단 상태 라벨 + 적용/해제 버튼 갱신."""
        if opt_conf_dir() is None:
            self.opt_status.setText("게임 최적화 — 패치 리소스 없음 (빌드에 opt_conf 미포함)")
            self.opt_btn.hide(); return
        if self._game_dir is None:
            self.opt_status.setText("게임 최적화 — PZ 설치 폴더를 못 찾음")
            self.opt_btn.hide(); return
        state, heap = pz_optimize_state(self._game_dir)
        if state == "applied":
            self.opt_status.setText(f"게임 최적화 적용됨 · 힙 {heap:,}MB (RAM 절반)")
            self.opt_btn.setText("해제")
        elif state == "partial":
            self.opt_status.setText("게임 최적화 일부만 적용됨 (게임 업데이트?) — 재적용 필요")
            self.opt_btn.setText("적용")
        else:
            self.opt_status.setText("게임 최적화 미적용")
            self.opt_btn.setText("적용")
        self.opt_btn.show()

    def _opt_startup(self):
        """앱 실행 시 자동 최적화. 동의창 없음.
           - 최초 실행(config.json에 opt_mode 없음): 강제 적용 + opt_mode='auto' 저장.
           - opt_mode='auto': 상태가 어긋나 있으면(게임 업데이트/RAM 변경 등) 조용히 재적용.
           - opt_mode='off': 사용자가 '해제'를 눌러 껐던 상태 — 자동 적용하지 않고 그대로 둠.
           (해제 상태는 이제 config.json에 저장되므로 앱을 껐다 켜도 유지된다.)"""
        self._opt_refresh_ui()
        if os.name != "nt" or self._game_dir is None or opt_conf_dir() is None:
            return
        if self.cfg.get("opt_mode") == "off":
            return
        state, _ = pz_optimize_state(self._game_dir)
        if state == "applied":
            return
        if pz_running():
            self.opt_status.setText("게임 최적화 대기 — PZ 실행 중엔 적용 불가 (게임 끄고 앱 재시작)")
            return
        self._opt_apply()
        self.cfg["opt_mode"] = "auto"
        save_config(self.cfg)

    def _opt_apply(self):
        try:
            mb = apply_pz_optimization(self._game_dir)
            self.opt_status.setText(f"게임 최적화 적용 완료 · 힙 {mb:,}MB")
        except PermissionError:
            # Program Files 등 쓰기 권한 없음 → 관리자 권한으로 자신을 재실행해 적용
            if run_elevated_optimizer("apply"):
                self.opt_status.setText("관리자 권한 창에서 적용 중…")
                QTimer.singleShot(6000, self._opt_refresh_ui)
            else:
                self.opt_status.setText("게임 최적화 실패 — 관리자 권한이 거부됨")
            return
        except Exception as e:
            self.opt_status.setText(f"게임 최적화 실패: {e}")
            return
        self._opt_refresh_ui()

    def _opt_toggle(self):
        """하단 적용/해제 토글. 선택 결과를 opt_mode('auto'|'off')로 config.json에 저장 —
           다음 앱 실행 시 _opt_startup이 이 값을 그대로 따른다 (해제하면 계속 해제 상태 유지)."""
        if self._game_dir is None:
            return
        if pz_running():
            QMessageBox.information(self, "게임 최적화",
                                    "Project Zomboid가 실행 중이라 파일을 바꿀 수 없어.\n게임을 먼저 종료해줘.")
            return
        state, _ = pz_optimize_state(self._game_dir)
        if state == "applied":
            try:
                restore_pz_optimization(self._game_dir)
                self.cfg["opt_mode"] = "off"; save_config(self.cfg)
            except PermissionError:
                if run_elevated_optimizer("restore"):
                    self.cfg["opt_mode"] = "off"; save_config(self.cfg)
                    self.opt_status.setText("관리자 권한 창에서 복원 중…")
                    QTimer.singleShot(6000, self._opt_refresh_ui)
                    return
                QMessageBox.warning(self, "게임 최적화", "복원 실패 — 관리자 권한이 거부됨")
            except Exception as e:
                QMessageBox.warning(self, "게임 최적화", f"복원 실패: {e}")
        else:
            self.cfg["opt_mode"] = "auto"; save_config(self.cfg)
            self._opt_apply()
        self._opt_refresh_ui()

    # --- 흐름 ---
    def _verify(self):
        txt = self.input.text().strip()
        if not txt:
            return
        self.verify_btn.setEnabled(False); self.verify_btn.setText("확인 중…")
        self.core.verify(txt)

    def _on_resolved(self, uuid, name):
        self._uuid = uuid
        self._name = name or (uuid[:8] + "…")
        self.verify_btn.setText("확인")
        self.welcome.setText(f"<span style='color:#5dcaa5; font-size:26px; font-weight:900'>[ {self._name} ]</span> 님, 환영합니다")
        self._live = False; self._pz = False; self._connected = False
        self._adult = False; self._adult_warned = False
        self.adult_warn.setVisible(False)
        self._set_row(self.r_uuid, True,  "UUID 확인 완료")
        self._set_row(self.r_live, False, "방송 상태 확인 중…")
        self._set_row(self.r_pz,   False, "Project Zomboid 확인 중…")
        self._set_row(self.r_conn, False, "인게임 접속 확인 중…")
        self.connect_btn.setEnabled(False)
        self.stack.setCurrentIndex(2)
        self.core.start_poll(uuid)

    def _on_invalid(self):
        self.verify_btn.setText("확인")
        self.stack.setCurrentIndex(1)

    def _retry(self):
        # '확인 누르기 직전' 상태로 — 입력 텍스트는 유지, 확인 버튼은 텍스트 있으면 다시 초록
        self.verify_btn.setEnabled(bool(self.input.text().strip()))
        self.stack.setCurrentIndex(0)
        self.input.setFocus()

    def _back_to_input(self):
        """체크리스트에서 이전 버튼 — 폴링 중단 + 상태 초기화 후 채널 입력 화면으로"""
        self.core.stop_poll()
        self._uuid = ""; self._name = ""
        self._live = False; self._pz = False; self._adult = False; self._connected = False
        self._adult_warned = False
        self.adult_warn.setVisible(False)
        self.input.setText("")
        self.verify_btn.setEnabled(False)
        self.verify_btn.setText("확인")
        self.stack.setCurrentIndex(0)
        self.input.setFocus()

    def _on_live(self, live):
        self._live = live
        self._set_row(self.r_live, live, "방송 중" if live else "방송이 오프라인 상태입니다")
        self._refresh()

    def _on_pz(self, running):
        self._pz = running
        self._set_row(self.r_pz, running,
                      "Project Zomboid 실행 중" if running else "Project Zomboid가 실행중이 아닙니다")
        self._refresh()

    def _on_connected(self, conn):
        self._connected = conn
        self._set_row(self.r_conn, conn,
                      "인게임 접속 완료" if conn else "인게임에 접속되지 않았습니다")
        self._refresh()

    def _on_adult(self, is_adult):
        self._adult = is_adult
        self.adult_warn.setVisible(is_adult)
        if is_adult and not self._adult_warned:
            self._adult_warned = True
            QMessageBox.warning(self, "연령제한 감지",
                                "방송에 연령제한이 걸려있습니다.\n"
                                "일반 방송으로 전환하면 자동으로 연동 가능해집니다.")
        elif not is_adult:
            self._adult_warned = False
        self._refresh()

    def _refresh(self):
        self.connect_btn.setEnabled(
            self._live and self._pz and self._connected and not self._adult
        )

    def _go_main(self):
        self.core.stop_poll()
        preset = {"channel": self.input.text().strip(),
                  "uuid": self._uuid, "name": self._name, "autostart": True}
        self.main_win = MainWindow(preset=preset)
        self.main_win.show()
        self.close()

    def closeEvent(self, e):
        try:
            self.core.shutdown()
        except Exception:
            pass
        super().closeEvent(e)


def main():
    # 승격 헬퍼 진입점 — 단일 인스턴스 락보다 먼저 처리해야 함
    # (본체가 떠 있는 상태에서 관리자 권한으로 재실행되는 프로세스라 락을 잡으면 안 됨)
    if "--pz-optimize" in sys.argv:
        _optimizer_cli("apply")
        return
    if "--pz-restore" in sys.argv:
        _optimizer_cli("restore")
        return

    app = QApplication(sys.argv)

    shared_mem = QSharedMemory("PuppetChzzkLauncher_SingleInstance")
    if not shared_mem.create(1):
        from PyQt5.QtWidgets import QMessageBox
        QMessageBox.warning(None, "중복 실행", "이미 실행 중입니다.")
        sys.exit(0)

    ico = resource_path(ICON_FILE)
    if os.path.exists(ico):
        app.setWindowIcon(QIcon(ico))
    win = LauncherWindow(); win.show()
    sys.exit(app.exec_())


if __name__ == "__main__":
    main()
