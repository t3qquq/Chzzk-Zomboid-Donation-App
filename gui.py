#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
gui.py  —  퐁듀 런처 : 치지직 → 좀보이드 후원연동 (단일 창, 치지직 공식 Open API)

설치:  pip install "chzzkpy>=2.2.0" PyQt5
실행:  python gui.py

구조 (양쪽 끝에 "돼지코(어댑터)"를 끼운 형태)
    DonationSource / ChzzkOfficialSource : 치지직 수신 추상화. chzzkpy(공식 API 모듈) 의존은
                                           ChzzkOfficialSource 안에만 존재.
    GameAdapter / ZomboidAdapter   : 게임별 출력(경로 탐지 + rewards.txt 기록). 게임 확장 포인트.
    DonationWorker                 : 코어. 스레드+asyncio로 Source 를 돌리고 Qt 시그널로 GUI에 전달.
    MainWindow                     : PyQt5 단일 창 UI.
    PZ 원클릭 최적화               : 앱 실행 시 자동으로 PZ 설치 폴더를 찾아 JVM 힙을 RAM 절반으로
                                     설정하고 좀비 연산 패치 class 9개를 교체 (원본 자동 백업).

공식 Open API (v4.0.0 — 비공식 채팅 WS → 공식 세션 + 인증 중개 서버 전환):
    인증        : OAuth2. 게이트에서 브라우저 로그인 → localhost 콜백으로 code 수신 →
                  code 를 퐁듀 인증 서버(Cloudflare Workers, AUTH_SERVER)에 보내 토큰으로 교환.
                  Client Secret 은 인증 서버에만 존재 — 런처/exe 를 디컴파일해도 나오지 않는다.
                  Access Token(1일) + Refresh Token(30일·일회용). Refresh Token 은 config json
                  에 저장, 갱신될 때마다 즉시 재저장 → 재실행 시 무브라우저 자동 로그인.
    화이트리스트: 인증 서버가 토큰 발급/갱신과 한 몸으로 검사 (클라이언트에 검사 코드 없음).
                  목록에서 제거되면 다음 갱신(최대 1일) 시점에 403 → 연동 중단 + 게이트 복귀.
    수신 범위   : 공식 세션은 "로그인한 계정 본인 채널"의 후원 이벤트만 구독 가능.
                  → 임의 채널 입력 개념이 사라지고 게이트 1단계가 로그인으로 대체됨.
    19세 방송   : 공식 API 는 성인방송 여부와 무관하게 수신됨 — NID 쿠키/성인 게이트 전부 제거.
    방송 대기   : 세션은 방송 on/off 와 무관하게 유지됨 (방송 재시작 시 그대로 계속 수신).

연결 안정화 (장시간 방송 대응):
    [1] 프로토콜 heartbeat    : chzzkpy 공식 게이트웨이 자체 EIO3 ping 루프 + 수신 타임아웃
                                (ping_interval + ping_timeout) 사용.
    [2] Stale 워치독          : 서버발 모든 패킷 수신 시각을 스탬프. 90초간 아무것도 못 받으면
                                죽은 연결로 판정, 강제 재접속. (chzzkpy 게이트웨이는 소켓이
                                CLOSED 프레임만 뱉는 비정상 종료 시 스스로 못 빠져나오는 구멍이
                                있어 — 이 워치독이 막는다. 종료 시 ClientSession 강제 close 로
                                재접속 반복 시 세션 누수도 함께 막음)
    [3] 연결 타임아웃         : 세션 URL 발급→소켓→CONNECTED→후원 구독까지 20초 제한.
    [4] 지수 백오프           : 재접속 5→10→20→40→60초(최대). 연결 성공했던 시도 후엔 5초로 리셋.

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


# ── 화이트리스트 ───────────────────────────────────────────────────────────────
# 시즌 참가 채널 검증은 퐁듀 인증 서버(pongdu-auth)가 토큰 발급/갱신과 한 몸으로 수행한다.
# 스트리머 추가/삭제는 지금처럼 Whitelist 레포의 JSON 만 커밋하면 됨 (반영 최대 60초).
# 런처(클라이언트)에는 화이트리스트 검사 코드가 존재하지 않는다 — 우회할 표면 자체가 없음.


VERSION = "v4.0.0"

# ── 치지직 공식 Open API 애플리케이션 정보 ─────────────────────────────────────
# 치지직 개발자센터(developers.naver.com/chzzk)에서 앱 등록 후 발급값을 채운다.
#   · 필요한 API Scope : "유저 정보 조회"(채널 확인용) + "후원 조회"(세션 이벤트 구독용)
#   · 로그인 리디렉션 URL 은 아래 OAUTH_REDIRECT 와 정확히 일치하게 등록해야 함
#   · Client Secret 은 이 파일/exe 에 존재하지 않는다 — 퐁듀 인증 서버(Cloudflare Workers)에만
#     있고, 토큰 발급/갱신은 전부 그 서버를 거친다. 서버가 화이트리스트를 함께 검사하므로
#     화이트리스트에서 제거된 채널은 다음 토큰 갱신(최대 1일) 시점부터 자동 차단된다.
CHZZK_CLIENT_ID     = "d10781fb-294a-4ed7-a7c2-c0d5d5445203"
AUTH_SERVER         = "https://pongdu-auth.t3qquq.workers.dev"   # 퐁듀 인증 중개 서버
AUTH_TIMEOUT        = 15.0    # 인증 서버 응답 대기 한도 (초)
OAUTH_PORT          = 51925
OAUTH_REDIRECT      = f"http://localhost:{OAUTH_PORT}/callback"
LOGIN_TIMEOUT       = 300.0   # 브라우저 로그인 대기 한도 (초)







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

def center_on_screen(win):
    """창을 현재 커서가 있는 모니터의 작업영역(작업표시줄 제외) 중앙에 배치.
       fixedSize 창은 OS 캐스케이딩이 안 먹어 show() 후 좌상단에 뜨는 경우가 있어 명시적으로 이동."""
    from PyQt5.QtWidgets import QApplication, QDesktopWidget
    from PyQt5.QtGui import QCursor
    screen = QApplication.desktop().screenNumber(QCursor.pos())
    if screen < 0:
        screen = QApplication.desktop().primaryScreen()
    geo = QDesktopWidget().availableGeometry(screen)
    fg = win.frameGeometry()
    fg.moveCenter(geo.center())
    win.move(fg.topLeft())

def extract_uuid(text: str):
    """입력 어디에 있든 32자리 hex(=채널 UUID)를 뽑는다. URL/라이브URL/생UUID 다 처리."""
    m = HEX32.search(text or "")
    return m.group(0).lower() if m else None


# ═══════════════════════════════════════════════════════════════════════════════
#  치지직 어댑터 (수신 방식 추상화 = "돼지코")
#  chzzkpy(공식 Open API 모듈) 의존은 ChzzkOfficialSource 안에만 존재.
# ═══════════════════════════════════════════════════════════════════════════════
Donation = namedtuple("Donation", "amount sender message")   # 플랫폼 중립 도네 1건


class SourceError(Exception):
    pass

class AuthRequired(SourceError):                # 로그인 없음 / refresh token 만료·무효 → 재로그인 필요
    pass

class NotWhitelisted(SourceError):              # 인증 서버가 화이트리스트 미등재로 거부 (403)
    def __init__(self, channel_name=""):
        super().__init__(channel_name)
        self.channel_name = channel_name

class StaleConnection(SourceError):             # 소켓은 열려있는 척하지만 수신이 끊긴 죽은 연결
    pass

class ConnectTimeout(SourceError):              # 제한 시간 안에 연결/구독 완료 못 함
    pass


class DonationSource:
    """치지직 수신 인터페이스. 이 4개만 구현하면 코어(DonationWorker)는 안 바뀐다."""

    async def resolve_channel(self, text):
        """수신 대상 채널 확정 -> (uuid, 표시이름). 못 하면 (None, 사유).
           공식 API 는 로그인 계정 본인 채널 고정이라 text 는 무시된다.
           저장된 로그인이 만료됐으면 AuthRequired 를 던진다."""
        raise NotImplementedError

    async def connect(self, uuid, emit, on_event=None):
        """연결 후 도네마다 emit(Donation) 호출. 정상 종료 시 리턴, 문제 시 SourceError.
           on_event(kind, detail): 연결 수명주기 통지 (같은 이벤트 루프에서 호출됨).
             kind ∈ connected / stale"""
        raise NotImplementedError

    def request_close(self):
        """스레드 세이프 종료 요청 플래그만 세움 (즉시 리턴). 실제 정리는 connect()가 수행."""
        raise NotImplementedError

    async def close(self):
        raise NotImplementedError


# ── 연결 안정화 파라미터 ──────────────────────────────────────────────────────
STALE_SEC        = 90.0   # [2] 이 시간 동안 서버로부터 패킷을 하나도 못 받으면 죽은 연결로 판정
                          #     (정상 연결은 EIO3 ping/pong 으로 최소 ~25초마다 수신 발생)
STALE_CHECK_SEC  = 5.0    # [2] 워치독 검사 주기 (stop 요청 반응 속도도 이 주기)
CONNECT_TIMEOUT  = 20.0   # [3] 세션 URL 발급→소켓→CONNECTED→후원 구독까지 허용 시간
CLOSE_TIMEOUT    = 5.0    #     종료 정리 대기 한도

# 게이트웨이 received_message 래핑이 갱신하는 마지막 수신 시각.
# 이 앱은 동시에 1개 연결만 유지하므로 모듈 전역으로 충분 (다중 연결 확장 시 인스턴스화 필요).
_WS_ACTIVITY = {"t": 0.0}


class ChzzkOfficialSource(DonationSource):
    """치지직 공식 Open API(chzzkpy 2.2.0 공식 모듈) 기반 구현.
       ← 이 파일에서 chzzkpy 를 import/호출하는 유일한 곳.

    refresh_token : 저장돼 있던 리프레시 토큰 (없으면 브라우저 로그인 필요)
    on_token(tok) : 새 refresh token 발급 시 호출 — 일회용 토큰이라 매번 즉시 저장해야 함.
                    (워커 스레드에서 호출될 수 있으니 파일 저장 등 스레드 세이프한 작업만)
    """

    def __init__(self, refresh_token=None, on_token=None, grace_sec=3.0):
        self.grace = grace_sec
        self._refresh_token = refresh_token or None
        self._on_token = on_token
        self._client = None            # chzzkpy.Client (앱 단위)
        self._user = None              # chzzkpy.UserClient (로그인 유저 단위)
        self._closing = False          # request_close()가 세우는 스레드 세이프 플래그
        self.was_connected = False     # 직전 connect() 시도에서 구독 완료까지 갔는지 (백오프 리셋용)
        self._donation_sink = None     # 현재 connect() 의 도네 콜백 (연결 중에만 non-None)

    # ── 클라이언트/이벤트 준비 ──
    async def _ensure_client(self):
        if self._client is None:
            from chzzkpy import Client
            # Secret 은 인증 서버에만 있다. chzzkpy 의 토큰 발급 경로는 사용하지 않으므로
            # 빈 문자열을 넣는다 — 자동 갱신(refresh)은 _install_server_refresh 로 대체됨.
            self._client = Client(CHZZK_CLIENT_ID, "")
            self._bind_events(self._client)
        return self._client

    # ── 인증 서버 호출 (토큰 발급/갱신 + 화이트리스트 검사) ──
    async def _auth_server(self, path, payload):
        """퐁듀 인증 서버 호출. 성공 시 {accessToken, refreshToken, expiresIn, channelId, channelName}.
           401→AuthRequired / 403→NotWhitelisted / 그 외→SourceError."""
        import aiohttp
        try:
            timeout = aiohttp.ClientTimeout(total=AUTH_TIMEOUT)
            async with aiohttp.ClientSession(timeout=timeout) as s:
                async with s.post(AUTH_SERVER + path, json=payload) as r:
                    try:
                        data = await r.json()
                    except Exception:
                        data = {}
                    if r.status == 200:
                        return data
                    if r.status == 401:
                        raise AuthRequired()
                    if r.status == 403:
                        raise NotWhitelisted(data.get("channelName") or "")
                    raise SourceError("인증 서버 오류 (%d): %s"
                                      % (r.status, data.get("detail") or data.get("error") or ""))
        except SourceError:
            raise
        except (aiohttp.ClientError, OSError, asyncio.TimeoutError) as e:
            raise SourceError("인증 서버 연결 실패: %s: %s" % (type(e).__name__, e)) from e

    async def _adopt_tokens(self, data):
        """인증 서버 응답의 토큰으로 UserClient 를 구성하고 채널 정보를 반영한다.
           채널은 인증 서버가 이미 확정했으므로(fetch_self 불필요) UserClient 를 직접 구성한다."""
        from chzzkpy.authorization import AccessToken
        from chzzkpy.client import UserClient
        client = await self._ensure_client()
        if client.http is None or not hasattr(client.loop, "create_task"):
            await client._async_setup_hook()               # loop/http 초기화 (initial_async_setup 과 동일 경로)
        at = AccessToken(
            access_token=data["accessToken"], refresh_token=data["refreshToken"],
            token_type="Bearer", expires_in=int(data.get("expiresIn") or 86400))
        self._user = UserClient(client, at)
        client.user_client.append(self._user)
        self._user.channel_id = data.get("channelId")
        self._user.channel_name = data.get("channelName") or ""
        self._install_server_refresh(self._user)
        self._store_token()
        if not self._user.channel_id:
            raise SourceError("채널 정보 확인 실패 — 인증 서버 응답에 channelId 없음")
        return self._user.channel_id, self._user.channel_name or ""

    def _install_server_refresh(self, user):
        """chzzkpy 의 자동 갱신(UserClient.refresh)이 Secret 으로 직접 갱신하려는 것을
           인증 서버 경유로 교체한다. 갱신마다 서버가 화이트리스트를 재검사하므로
           장시간 연동 중에도 회수가 관철된다."""
        import datetime
        source = self

        async def refresh():
            data = await source._auth_server(
                "/refresh", {"refreshToken": user.access_token.refresh_token})
            from chzzkpy.authorization import AccessToken
            at = AccessToken(
                access_token=data["accessToken"], refresh_token=data["refreshToken"],
                token_type="Bearer", expires_in=int(data.get("expiresIn") or 86400))
            user._connection.access_token = user.access_token = at
            user._token_generated_at = datetime.datetime.now()
            source._store_token()

        user.refresh = refresh

    def _bind_events(self, client):
        """chzzkpy 는 함수명으로 이벤트를 매칭한다 (dispatch("donation") → on_donation)."""
        source = self

        @client.event
        async def on_donation(message):
            sink = source._donation_sink
            if sink is not None:
                try:
                    sink(message)
                except Exception:
                    pass

    def _store_token(self):
        """현재 refresh token 을 밖으로 통지. 일회용이라 갱신 즉시 저장 안 하면 다음 실행 때 로그인 풀림."""
        u = self._user
        if u is None:
            return
        try:
            tok = u.access_token.refresh_token
        except Exception:
            return
        if tok and tok != self._refresh_token:
            self._refresh_token = tok
            if self._on_token:
                try:
                    self._on_token(tok)
                except Exception:
                    pass

    # ── 로그인 ──
    async def login_with_refresh(self):
        """저장된 refresh token 으로 무브라우저 로그인 (인증 서버 경유 — 화이트리스트 검사 포함).
           성공 시 (channel_id, 채널명)."""
        if not self._refresh_token:
            raise AuthRequired()
        data = await self._auth_server("/refresh", {"refreshToken": self._refresh_token})
        return await self._adopt_tokens(data)

    async def login_with_browser(self, cancel_event=None):
        """브라우저 OAuth2 로그인. localhost 콜백으로 code 를 받아 인증 서버에서 토큰으로 교환한다
           (화이트리스트 검사 포함). chzzkpy Client.login() 은 취소/타임아웃 시 서버 정리가 안 되고
           noconsole exe 에서 print 를 쓰는 문제가 있어 사용하지 않는다."""
        import secrets
        import webbrowser
        import aiohttp.web
        client = await self._ensure_client()

        state = secrets.token_urlsafe(16)
        result = {}
        got = asyncio.Event()

        async def _handle(request):
            result["code"] = request.query.get("code")
            result["state"] = request.query.get("state")
            got.set()
            return aiohttp.web.Response(
                text="로그인 완료! 이 창을 닫고 퐁듀 런처로 돌아가 주세요.",
                content_type="text/plain", charset="utf-8")

        web_app = aiohttp.web.Application()
        web_app.router.add_get("/callback", _handle)
        runner = aiohttp.web.AppRunner(web_app)
        await runner.setup()
        try:
            # localhost 가 ::1 로 먼저 해석되는 환경이 있어 IPv4/IPv6 양쪽에 바인드한다.
            # (IPv6 미지원 환경에서는 IPv4 단독으로 폴백)
            started = False
            for hosts in (["127.0.0.1", "::1"], "127.0.0.1"):
                try:
                    await aiohttp.web.TCPSite(runner, hosts, OAUTH_PORT).start()
                    started = True
                    break
                except OSError as e:
                    last_err = e
            if not started:
                raise SourceError("OAuth 콜백 포트(%d) 사용 불가 — 다른 프로그램이 점유 중: %s"
                                  % (OAUTH_PORT, last_err))

            url = client.generate_authorization_token_url(
                redirect_url=OAUTH_REDIRECT, state=state)
            webbrowser.open(url)

            waits = {asyncio.ensure_future(got.wait())}
            if cancel_event is not None:
                waits.add(asyncio.ensure_future(cancel_event.wait()))
            done, pending = await asyncio.wait(
                waits, timeout=LOGIN_TIMEOUT, return_when=asyncio.FIRST_COMPLETED)
            for t in pending:
                t.cancel()
            if cancel_event is not None and cancel_event.is_set():
                raise AuthRequired()                       # 사용자 취소 → 로그인 대기 상태로
            if not got.is_set():
                raise ConnectTimeout("%d초 안에 브라우저 로그인이 완료되지 않음"
                                     % int(LOGIN_TIMEOUT))
        finally:
            try:
                await runner.cleanup()                     # 콜백 서버는 어떤 경로든 반드시 내림
            except Exception:
                pass

        if result.get("state") != state or not result.get("code"):
            raise SourceError("OAuth 응답 비정상 (state 불일치 또는 code 없음)")
        data = await self._auth_server("/token", {"code": result["code"], "state": state})
        return await self._adopt_tokens(data)

    # ── DonationSource 구현 ──
    async def resolve_channel(self, text):
        # 공식 API 는 로그인 계정 본인 채널 고정 — text 는 사용하지 않는다.
        if self._user is not None and self._user.channel_id:
            return self._user.channel_id, self._user.channel_name or ""
        return await self.login_with_refresh()             # AuthRequired 는 그대로 전파

    async def connect(self, uuid, emit, on_event=None):
        from chzzkpy import UserPermission

        def ev(kind, detail=""):
            if on_event:
                try:
                    on_event(kind, detail)
                except Exception:
                    pass

        self._closing = False
        self.was_connected = False
        await self._ensure_client()
        if self._user is None:
            await self.login_with_refresh()                # AuthRequired 전파 → 워커가 게이트 복귀
        user = self._user

        started = {"t": time.monotonic()}                  # 도네 grace 기준점
        grace = self.grace

        def sink(message):                                 # chzzkpy Donation → 중립 Donation
            if time.monotonic() - started["t"] < grace:
                return                                     # 접속 직후 이벤트 방어 (리플레이 대비)
            try:
                amt = int(getattr(message, "pay_amount", 0) or 0)
            except (TypeError, ValueError):
                amt = 0
            if amt <= 0:
                return
            donator_id = getattr(message, "donator_id", "") or ""
            nick = (getattr(message, "donator_name", "") or "").strip()
            # 공식 API 규약: 익명 후원은 donatorChannelId == "anonymous", 닉네임 빈 문자열
            sender = "익명의 후원자" if (donator_id == "anonymous" or not nick) else nick
            body = (getattr(message, "donation_text", "") or "") \
                .replace("\r", " ").replace("\n", " ").strip()
            emit(Donation(amt, sender, body))

        self._donation_sink = sink
        try:
            # ── [3] 1단계: 세션 URL 발급 → 소켓 → CONNECTED → 후원 구독까지 타임아웃 ──
            try:
                await asyncio.wait_for(
                    user.connect(UserPermission(donation=True), addition_connect=True),
                    timeout=CONNECT_TIMEOUT)
            except asyncio.TimeoutError:
                raise ConnectTimeout("%d초 안에 연결되지 않음 (네트워크/치지직 서버 응답 없음)"
                                     % int(CONNECT_TIMEOUT))
            except SourceError:
                raise
            except Exception as e:
                self._raise_mapped(e)

            self.was_connected = True
            self._store_token()            # connect 경로의 자동 refresh 가 토큰을 갈았을 수 있음
            started["t"] = time.monotonic()
            _WS_ACTIVITY["t"] = time.monotonic()
            ev("connected")

            gw = getattr(user, "_gateway", None)
            read_task = getattr(gw, "_read_background_loop", None) if gw else None
            if gw is not None:             # [2] 수신 스탬프 — 이후 모든 패킷 수신 시각 기록
                _orig_recv = gw.received_message

                async def _recv_stamped(pkt):
                    _WS_ACTIVITY["t"] = time.monotonic()
                    return await _orig_recv(pkt)

                gw.received_message = _recv_stamped

            # ── [2] 2단계: 연결 유지 — stale 워치독 + 종료 요청 감시 ──
            while True:
                if read_task is not None:
                    done, _ = await asyncio.wait({read_task}, timeout=STALE_CHECK_SEC)
                else:
                    await asyncio.sleep(STALE_CHECK_SEC)
                    done = set()
                if self._closing:                          # 사용자 중지
                    return
                if read_task is not None and read_task in done:
                    exc = read_task.exception()
                    if exc is not None:
                        self._raise_mapped(exc)
                    return                                 # 서버측 정상 종료 → 워커가 재접속
                idle = time.monotonic() - _WS_ACTIVITY["t"]
                if idle > STALE_SEC:                       # 살아있는 척하는 죽은 연결
                    ev("stale", str(int(idle)))
                    raise StaleConnection("%d초간 서버 수신 없음" % int(idle))
        finally:
            self._donation_sink = None
            gw = getattr(user, "_gateway", None)
            try:                                           # 정상 종료 시도 (예외 무시)
                await asyncio.wait_for(user.disconnect(), timeout=CLOSE_TIMEOUT)
            except Exception:
                pass
            # chzzkpy 게이트웨이 뒷정리 강제 수행:
            #  · disconnect 가 죽은 소켓에 send 하다 중간에 실패하면 태스크/소켓이 남는다
            #  · gateway 가 들고 있는 aiohttp.ClientSession 은 chzzkpy 가 절대 안 닫는다(누수)
            if gw is not None:
                try:
                    gw.is_connected = False
                    for tname in ("_ping_loop_task", "_read_background_loop"):
                        t = getattr(gw, tname, None)
                        if t is not None and not t.done():
                            t.cancel()
                    ws = getattr(gw, "websocket", None)
                    if ws is not None and not ws.closed:
                        await asyncio.wait_for(ws.close(), timeout=CLOSE_TIMEOUT)
                except Exception:
                    pass
                try:
                    sess = getattr(gw, "session", None)
                    if sess is not None and not sess.closed:
                        await sess.close()
                except Exception:
                    pass
            # UserClient 내부 상태 리셋 — disconnect 실패 시에도 다음 connect() 가 깨끗하게 시작
            try:
                user._gateway = None
                user._gateway_id = None
                user._session_id = None
                user._gateway_ready.clear()
            except Exception:
                pass
            self._store_token()

    # ── 예외 매핑 ──
    @staticmethod
    def _raise_mapped(e):
        """연결/수신 단계 예외 → 의미 있는 SourceError 로 분류 (로그 가독성 + 워커 분기)."""
        import aiohttp
        try:
            from chzzkpy.error import (
                UnauthorizedException, LoginRequired, ForbiddenException,
                TooManyRequestsException, ChatConnectFailed, ReceiveErrorPacket)
        except Exception:                                  # 라이브러리 구조 변경 대비
            UnauthorizedException = LoginRequired = ForbiddenException = ()
            TooManyRequestsException = ChatConnectFailed = ReceiveErrorPacket = ()
        if isinstance(e, (UnauthorizedException, LoginRequired)):
            raise AuthRequired() from e
        if isinstance(e, ForbiddenException):
            raise SourceError("호출 권한 없음(403) — 개발자센터 앱 API Scope('후원 조회') 확인 필요") from e
        if isinstance(e, TooManyRequestsException):
            raise SourceError("치지직 API 호출 제한(429) — 잠시 후 자동 재시도") from e
        if isinstance(e, ChatConnectFailed):
            raise SourceError("세션 접속 실패: %s" % e) from e
        if isinstance(e, ReceiveErrorPacket):
            raise SourceError("소켓 수신 오류 (서버측 종료 추정)") from e
        if isinstance(e, ConnectionError):
            raise SourceError("Heartbeat PONG 미수신 — 연결 유실") from e
        if isinstance(e, asyncio.TimeoutError):
            raise SourceError("소켓 타임아웃 (수신 두절)") from e
        if isinstance(e, asyncio.CancelledError):
            raise SourceError("수신 루프 취소됨") from e
        if isinstance(e, (aiohttp.ClientError, OSError)):
            raise SourceError("네트워크 오류: %s: %s" % (type(e).__name__, e)) from e
        raise SourceError("%s: %s" % (type(e).__name__, e)) from e

    def request_close(self):
        # 다른 스레드에서 호출됨 — 플래그만 세움. 워치독이 ≤STALE_CHECK_SEC 안에 감지해 정리.
        self._closing = True

    async def close(self):
        self._closing = True
        if self._user is not None:
            try:
                await asyncio.wait_for(self._user.disconnect(), timeout=CLOSE_TIMEOUT)
            except Exception:
                pass
        if self._client is not None and getattr(self._client, "http", None) is not None:
            try:
                await self._client.http.close()            # Open API HTTP 세션 정리
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
async def fetch_live(uuid: str) -> bool:
    """방송 on/off 판정 — 치지직 공개 폴링 엔드포인트 직접 호출 (로그인 불필요).
       게이트 UX 용도라 실패는 그냥 False (연동 자체는 방송 여부와 무관하게 유지됨)."""
    if FORCE_ONLINE:
        return True
    import aiohttp
    url = f"https://api.chzzk.naver.com/polling/v2/channels/{uuid}/live-status"
    try:
        timeout = aiohttp.ClientTimeout(total=5)
        async with aiohttp.ClientSession(headers=UA, timeout=timeout) as s:
            async with s.get(url) as r:
                data = await r.json()
    except Exception:
        return False
    return (((data or {}).get("content") or {}).get("status")) == "OPEN"


def pz_running() -> bool:
    """Project Zomboid 클라이언트가 실행 중인지 프로세스 목록으로 확인."""
    KEY = "projectzomboid"
    try:                                         # psutil 있으면 우선 (의존성 아님, 있으면 사용)
        import psutil # pyright: ignore[reportMissingModuleSource]
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
        raise RuntimeError("백업이 없음 — Steam '게임 파일 무결성 검사'로 복원해 주세요")
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
            raise RuntimeError("Project Zomboid가 실행 중이라 파일을 교체할 수 없습니다.\n게임 종료 후 다시 시도해 주세요.")
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
    auth_lost = pyqtSignal()               # 로그인 만료/무효 감지 (멈춤 + 게이트 복귀 → 재로그인)
    whitelist_lost = pyqtSignal()          # 연동 중 화이트리스트에서 제거됨 (멈춤 + 게이트 복귀)

    def __init__(self, source, channel_text="",
                 reconnect_sec=5.0, reconnect_max=60.0):
        super().__init__()
        self.source = source               # ← 어떤 수신 방식이든 DonationSource 만 받는다
        self.channel_text = channel_text   # 공식 API 소스는 사용 안 함 (로그인 계정 채널 고정)
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
        """소스 연결 수명주기 통지 → 상태/로그 릴레이. (소스 루프 스레드에서 호출되지만
           pyqtSignal.emit 은 스레드 세이프 — queued connection 으로 GUI 스레드에 전달됨)"""
        if kind == "connected":
            self._had_conn = True
            self._attempt = 0
            self.status.emit("연결됨", "#5dcaa5")
            self.note.emit("치지직 연결됨 ✓  (이 시점 이후의 후원부터 수신 — 방송 on/off 무관 유지)")
        elif kind == "stale":
            self.note.emit(f"⚠ Heartbeat/수신 두절 {detail}초 — 죽은 연결로 판정, 강제 재접속")

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

        try:
            uuid, name = self.loop.run_until_complete(
                self.source.resolve_channel(self.channel_text))
        except AuthRequired:
            self.auth_lost.emit()
            self.status.emit("대기 중", "#5f5e5a")
            return
        except NotWhitelisted:
            self.whitelist_lost.emit()
            self.status.emit("대기 중", "#5f5e5a")
            return
        except Exception as e:
            uuid, name = None, f"{type(e).__name__}: {e}"
        if not uuid:
            self.failed.emit(f"치지직 로그인/채널 확인 실패 ({name})")
            self.status.emit("대기 중", "#5f5e5a")
            return
        self.resolved.emit(uuid, name or "")

        while not self._stop:
            self._had_conn = False
            try:
                self.status.emit("연결 중…", "#ef9f27")
                if self._attempt > 0:
                    self.note.emit(f"재접속 시도 #{self._attempt}")
                self.loop.run_until_complete(
                    self.source.connect(uuid, self._emit, on_event=self._on_src_event))
                if self._stop:
                    break
                self._note_once("연결 끊김 — 재접속 대기 중")
                self.status.emit("재접속 대기…", "#ef9f27")
            except AuthRequired:
                # Access Token 만료(1일) 등 — 저장된 refresh 로 조용히 재로그인 1회 시도.
                # (갱신은 인증 서버가 화이트리스트를 재검사하므로 회수도 여기서 관철된다)
                try:
                    self.note.emit("로그인 갱신 중…")
                    self.loop.run_until_complete(self.source.login_with_refresh())
                    continue                       # 갱신 성공 → 백오프 없이 즉시 재접속
                except NotWhitelisted:
                    self.whitelist_lost.emit()
                    return
                except Exception:
                    self.auth_lost.emit()          # refresh 도 실패 → 게이트에서 재로그인
                    return
            except NotWhitelisted:
                self.whitelist_lost.emit()
                return
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
            if self._had_conn:
                self._backoff = self.reconnect
                self._attempt = 0
            wait = self._backoff
            if not self._had_conn:
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
        # 불러오기(편집중) ↔ 내보내기(저장후) 겸용 버튼
        self.io_btn = QPushButton("불러오기"); self.io_btn.setObjectName("link")
        self.io_btn.clicked.connect(self._on_io)
        trow.addWidget(self.io_btn)
        self.reset_btn = QPushButton("초기화"); self.reset_btn.setObjectName("link")
        self.reset_btn.clicked.connect(self._reset)
        trow.addWidget(self.reset_btn)
        root.addLayout(trow)

        sep = QFrame(); sep.setObjectName("sep"); sep.setFixedHeight(1)
        root.addWidget(sep)

        brow = QHBoxLayout()
        self.status = QLabel(""); self.status.setObjectName("muted")
        brow.addWidget(self.status, 1)
        # 저장(편집중) ↔ 다시 편집(저장후) 겸용 버튼
        self.save_btn = QPushButton("저장"); self.save_btn.setObjectName("start")
        self.save_btn.clicked.connect(self._on_primary)
        brow.addWidget(self.save_btn)
        # 닫기(편집중) ↔ 확인(저장후) — 둘 다 accept, 텍스트만 다름
        self.close_btn = QPushButton("닫기")
        self.close_btn.clicked.connect(self.accept)
        brow.addWidget(self.close_btn)
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
        """저장 후 잠금 / ‘다시 편집’으로 해제 (3.5 잠금 UX). 행 위젯·버튼 표시/텍스트를 일괄 전환.
           편집중: 행추가 / 불러오기·초기화·저장·닫기
           저장후: (행추가 숨김) / 내보내기·초기화·다시 편집·확인"""
        self.locked = locked
        for _row, amt_edit, feat_combo, del_btn in self.rows:
            amt_edit.setEnabled(not locked)
            feat_combo.setEnabled(not locked)
            feat_combo.setStyleSheet(self.LOCKED_COMBO_QSS if locked else "")
            del_btn.setVisible(not locked)
        self.add_btn.setVisible(not locked)
        self.io_btn.setText("내보내기" if locked else "불러오기")
        self.save_btn.setText("다시 편집" if locked else "저장")
        self.close_btn.setText("확인" if locked else "닫기")

    def _on_io(self):
        """겸용 버튼: 편집중이면 불러오기, 저장후면 내보내기."""
        if self.locked:
            self._export()
        else:
            self._import()

    def _on_primary(self):
        """겸용 버튼: 편집중이면 저장, 저장후면 다시 편집."""
        if self.locked:
            self._unlock()
        else:
            self._save()

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
        self._status_msg(f"저장됨 ({len(tiers)}개) — ‘다시 편집’으로 수정 / ‘내보내기’로 파일 저장", ok=True)

    def _export(self):
        """확정된(잠금 상태) 프리셋을 사용자가 지정한 경로에 JSON({amount: featureId})으로 저장."""
        tiers = self._collect()
        if tiers is None:                       # 잠금 상태라 정상적으론 발생 안 하지만 방어적으로
            return
        fn, _ = QFileDialog.getSaveFileName(
            self, "리워드 프리셋 내보내기", str(Path.home() / "reward_preset.json"), "JSON (*.json)")
        if not fn:
            return
        try:
            Path(fn).write_text(
                json.dumps({str(k): v for k, v in tiers.items()}, ensure_ascii=False, indent=2),
                encoding="utf-8")
        except OSError as e:
            self._status_msg(f"⚠ 내보내기 실패: {e}", ok=False)
            return
        self._status_msg(f"내보냄 ({len(tiers)}개) → {Path(fn).name}", ok=True)

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
        self.guard = None                             # PZ 종료 + 인게임 이탈 감시 (연동 중에만)
        # 티어 편집은 게이트(RewardPresetDialog)로 이동 — 여기선 reward_preset.json만 읽는다.
        self._load_reward_tiers()
        self._build()
        self._restore()
        if self.preset.get("autostart"):
            QTimer.singleShot(300, self._start)   # 창 뜨고 나서 워커 시작 (게이트에서 로그인 완료됨)

    def _build(self):
        root = QVBoxLayout(self)
        root.setContentsMargins(18, 18, 18, 18)
        root.setSpacing(10)


        title = QLabel("치지직 → 좀보이드 후원연동")
        title.setStyleSheet("font-size:15px; font-weight:bold;")
        root.addWidget(title)

        # 채널 — 공식 API 는 로그인 계정 본인 채널 고정 (게이트에서 로그인 완료 후 진입)
        name = self.preset.get("name", "")
        conn = QLabel(f"채널 연결됨: {name}" if name else "치지직 로그인 계정 채널로 연동됩니다")
        conn.setObjectName("linkok")
        root.addWidget(conn)

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
        self.tiers_host.setToolTip("리워드 프리셋을 수정하려면 ‘중지’ 버튼을 눌러 이전 화면으로 돌아가 주세요")
        self.tiers_grid = QGridLayout(self.tiers_host)
        self.tiers_grid.setContentsMargins(0, 0, 6, 0)
        self.tiers_grid.setHorizontalSpacing(6); self.tiers_grid.setVerticalSpacing(4)
        tscroll = QScrollArea(); tscroll.setWidgetResizable(True)
        tscroll.setFrameShape(QFrame.NoFrame)
        tscroll.setWidget(self.tiers_host)
        tscroll.setToolTip("리워드 프리셋을 수정하려면 ‘중지’ 버튼을 눌러 이전 화면으로 돌아가 주세요")
        tscroll.setFixedHeight(380)
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
        manual = self.cfg.get("path", "")
        if manual:
            self.adapter.path = Path(manual)
            self.path_input.setText(manual)
        else:
            self._autodetect_path()

    def _persist(self):
        self.cfg.update({
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
            self._log("rewards.txt 경로를 못 찾음. ‘직접 지정’으로 선택해 주세요.")

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

    def _save_token(self, tok):
        """워커 스레드에서 호출됨 — refresh token 갱신 즉시 저장 (Qt 객체 접근 금지)."""
        _persist_refresh_token(tok)

    def _start(self):
        if self.adapter.path is None:
            self._log("rewards.txt 경로가 없음. ‘직접 지정’으로 선택해 주세요."); return
        self._persist()
        source = ChzzkOfficialSource(                  # ← 수신 어댑터 (치지직 공식 Open API)
            refresh_token=load_config().get("chzzk_refresh_token", ""),
            on_token=self._save_token)
        self.worker = DonationWorker(source)
        self.worker.donation.connect(self._on_donation)
        self.worker.status.connect(self._on_status)
        self.worker.resolved.connect(self._on_resolved)
        self.worker.failed.connect(self._on_failed)
        self.worker.note.connect(self._log)
        self.worker.auth_lost.connect(self._auth_to_gate)   # 로그인 만료 → 게이트에서 재로그인
        self.worker.whitelist_lost.connect(self._wl_to_gate) # 시즌 목록 제거 감지 → 게이트 복귀
        self.worker.start()
        self.start_btn.setText("중지"); self.start_btn.setObjectName("stop"); self.setStyleSheet(DARK_QSS)
        uuid = self.preset.get("uuid")
        if self.preset.get("autostart") and uuid:      # 게이트를 거쳐 들어온 경우만 감시
            self.guard = MainGuard(uuid)               # PZ 종료/인게임 이탈을 짧은 주기로 직접 폴링
            self.guard.pz_lost.connect(self._pz_to_gate)
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
        self._on_status("대기 중", "#5f5e5a")
        self._log("중지됨.")

    def _back_to_gate(self, warn_auth=False, warn_wl=False):
        """워커 정리하고 게이트 창으로 돌아간다 (중지 / PZ 종료 / 로그인 만료 / 미등재 공통).
           warn_auth=True 면 저장 토큰을 지우고 경고창 후 로그인 화면부터 다시 시작.
           warn_wl=True 면 미등재 경고 후 게이트로 (게이트 자동 로그인이 미등재 화면을 띄운다).
           _returning 을 먼저 세워 경고창 중 중복 트리거를 막는다."""
        if self._returning:
            return
        self._returning = True
        self._kill_guard()
        if self.worker:
            self.worker.stop(); self.worker = None
        if warn_auth:
            _clear_refresh_token()
            QMessageBox.warning(self, "로그인 만료",
                                "치지직 로그인이 만료됐습니다.\n연동을 중단하고 로그인 화면으로 돌아갑니다.")
        elif warn_wl:
            QMessageBox.warning(self, "시즌 참가 목록 변경",
                                "채널이 시즌 참가 목록에서 제외되어 연동이 중단됩니다.")
        self._persist()
        preset = None
        if not (warn_auth or warn_wl) and self.preset.get("uuid"):
            preset = {"uuid": self.preset["uuid"], "name": self.preset.get("name", "")}
        self._gate = LauncherWindow(preset=preset)
        center_on_screen(self._gate)
        self._gate.show()
        self.close()

    def _pz_to_gate(self):
        if self._returning:
            return
        self._log("Project Zomboid 종료 감지 — 게이트로 돌아갑니다.")
        self._back_to_gate()

    def _auth_to_gate(self):
        self._back_to_gate(warn_auth=True)

    def _wl_to_gate(self):
        self._back_to_gate(warn_wl=True)

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
        self._log(f"채널 인식됨 · {label}")

    def _on_failed(self, msg):
        self._log("⚠ " + msg)
        self._stop()

    def _inject_test(self):
        amt = self.test_combo.currentData()
        if amt is None:
            self._log("테스트할 티어가 없음. 리워드 티어를 먼저 저장해 주세요."); return
        if self.adapter.path is None:
            self._log("경로가 없어 테스트 불가. 경로를 먼저 지정해 주세요."); return
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
def _persist_refresh_token(tok: str):
    """새 refresh token 즉시 저장 (일회용이라 유실 시 다음 실행 때 재로그인 필요).
       워커/코어 스레드에서 호출되므로 Qt 객체는 건드리지 않는다."""
    cfg = load_config()
    cfg["chzzk_refresh_token"] = tok
    save_config(cfg)


def _clear_refresh_token():
    cfg = load_config()
    if cfg.pop("chzzk_refresh_token", None) is not None:
        save_config(cfg)


class LauncherCore(QObject):
    """게이트용 비동기 워커. 치지직 로그인/화이트리스트 검증 + 방송·PZ 폴링을 한 루프에서 돌린다."""
    resolved = pyqtSignal(str, str)   # uuid, name (로그인 + 화이트리스트 통과)
    invalid  = pyqtSignal()           # 화이트리스트 미등재
    login_needed = pyqtSignal(str)    # 자동 로그인 실패/취소 → 로그인 버튼 표시 (detail=사유, 빈 문자열 가능)
    live     = pyqtSignal(bool)       # 방송 on/off
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

    # --- 치지직 로그인 ---
    def auto_login(self):
        """실행 직후: 저장된 refresh token 으로 무브라우저 로그인 시도."""
        self._submit(self._auto_login())

    def browser_login(self):
        """로그인 버튼: 브라우저 OAuth 로그인."""
        self._login_cancel = asyncio.Event()
        self._submit(self._browser_login())

    def cancel_login(self):
        ev = getattr(self, "_login_cancel", None)
        if ev is not None and self.loop is not None:
            self.loop.call_soon_threadsafe(ev.set)

    async def _auto_login(self):
        tok = load_config().get("chzzk_refresh_token", "")
        if not tok:
            self.login_needed.emit("")
            return
        source = ChzzkOfficialSource(refresh_token=tok, on_token=_persist_refresh_token)
        try:
            uuid, name = await source.login_with_refresh()
        except AuthRequired:
            _clear_refresh_token()                 # 만료/무효 토큰은 지워서 다음부터 바로 버튼 표시
            self.login_needed.emit("저장된 로그인이 만료됐습니다. 다시 로그인해 주세요.")
            return
        except NotWhitelisted:
            self.invalid.emit()                    # 로그인 자체는 유효 — 토큰은 유지
            return
        except Exception as e:
            self.login_needed.emit(f"자동 로그인 실패: {e}")
            return
        finally:
            await source.close()
        self._gate_pass(uuid, name)

    async def _browser_login(self):
        source = ChzzkOfficialSource(on_token=_persist_refresh_token)
        try:
            uuid, name = await source.login_with_browser(cancel_event=self._login_cancel)
        except AuthRequired:                       # 사용자 취소
            self.login_needed.emit("")
            return
        except NotWhitelisted:
            self.invalid.emit()
            return
        except Exception as e:
            self.login_needed.emit(f"로그인 실패: {e}")
            return
        finally:
            await source.close()
        self._gate_pass(uuid, name)

    def _gate_pass(self, uuid, name):
        """로그인 + 화이트리스트 통과 (검사는 인증 서버가 토큰 발급과 한 몸으로 수행)."""
        global FORCE_ONLINE
        FORCE_ONLINE = bool(load_config().get("force_online"))   # 관리자/테스트 모드 (config 플래그)
        self.resolved.emit(uuid, name or "")

    # --- 방송 / PZ 폴링 (체크리스트) ---
    def start_poll(self, uuid):
        self._uuid = uuid
        if not self._polling:
            self._submit(self._poll())

    async def _poll(self):
        self._polling = True
        while self._polling:
            live = await fetch_live(self._uuid)
            self.live.emit(live)
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
    """메인 연동 중 감시 워커: PZ 종료 + 인게임 이탈을 폴링해서 시그널만 쏜다."""
    pz_lost  = pyqtSignal()

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
        self.core.login_needed.connect(self._on_login_needed)
        self.core.live.connect(self._on_live)
        self.core.pz.connect(self._on_pz)
        self.core.connected.connect(self._on_connected)
        self._uuid = ""; self._name = ""
        self._live = False; self._pz = False; self._connected = False
        self._logging_in = False
        self.main_win = None
        self.cfg = load_config()       # opt_mode 등 (MainWindow와 같은 config.json 공유)
        self._game_dir = find_pz_dir() # PZ 설치 폴더 (최적화용, 못 찾으면 None)
        self._build()
        self.setStyleSheet(DARK_QSS)
        # preset 있으면 로그인 완료 상태(체크리스트)부터 시작, 없으면 자동 로그인 시도
        if preset and preset.get("uuid"):
            self._on_resolved(preset["uuid"], preset.get("name", ""))
        else:
            QTimer.singleShot(200, self._start_auto_login)
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
        v.addSpacing(45);
        v.addWidget(self._sect("치지직 로그인"))
        self.login_hint = self._muted("연동할 치지직 계정으로 로그인하세요")
        v.addWidget(self.login_hint)
        self.login_status = QLabel(""); self.login_status.setObjectName("muted")
        self.login_status.setAlignment(Qt.AlignCenter)
        v.addWidget(self.login_status)
        row = QHBoxLayout(); row.addStretch(1)
        self.login_btn = QPushButton("치지직 로그인"); self.login_btn.setObjectName("verify")
        self.login_btn.setEnabled(False)             # 자동 로그인 시도가 끝나면 활성화
        self.login_btn.clicked.connect(self._login_click)
        row.addWidget(self.login_btn)
        self.login_cancel_btn = QPushButton("취소")
        self.login_cancel_btn.clicked.connect(self._login_cancel_click)
        self.login_cancel_btn.hide()
        row.addWidget(self.login_cancel_btn)
        row.addStretch(1)
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
        v.addWidget(self._sect("치지직 로그인"))
        v.addStretch(1)
        e = QLabel("시즌 참가 목록에 없는 채널입니다"); e.setObjectName("err"); e.setAlignment(Qt.AlignCenter)
        v.addWidget(e)
        h = self._muted("로그인한 계정의 채널이 화이트리스트에 등록돼 있어야 합니다")
        h.setAlignment(Qt.AlignCenter)
        v.addWidget(h)
        row = QHBoxLayout(); row.addStretch(1)
        again = QPushButton("다른 계정으로 로그인"); again.clicked.connect(self._retry)
        row.addWidget(again); row.addStretch(1)
        v.addLayout(row); v.addStretch(1)
        return w

    def _page_check(self):
        w = QWidget(); v = QVBoxLayout(w); v.setContentsMargins(0, 8, 0, 0); v.setSpacing(12)
        v.addWidget(self._sect("연동 준비 확인"))
        self.welcome = QLabel(""); self.welcome.setObjectName("welcome")
        self.welcome.setAlignment(Qt.AlignCenter); self.welcome.setTextFormat(Qt.RichText)
        v.addWidget(self.welcome)
        v.addSpacing(4)
        self.r_uuid = self._check_row(); v.addWidget(self.r_uuid[0])
        self.r_live = self._check_row(); v.addWidget(self.r_live[0])
        self.r_pz   = self._check_row(); v.addWidget(self.r_pz[0])
        self.r_conn = self._check_row(); v.addWidget(self.r_conn[0])
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
        back_btn = QPushButton("로그아웃"); back_btn.clicked.connect(self._logout)
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
                                    "Project Zomboid가 실행 중이라 파일을 바꿀 수 없습니다.\n게임을 먼저 종료해 주세요.")
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
    def _start_auto_login(self):
        """실행 직후: 저장된 로그인으로 조용히 시도. 실패하면 login_needed 로 버튼이 열린다."""
        self._logging_in = True
        self.login_btn.setEnabled(False)
        self.login_status.setText("저장된 로그인 확인 중…")
        self.core.auto_login()

    def _login_click(self):
        self._logging_in = True
        self.login_btn.setEnabled(False)
        self.login_cancel_btn.show()
        self.login_status.setText("브라우저에서 치지직 로그인을 완료해 주세요…")
        self.core.browser_login()

    def _login_cancel_click(self):
        self.core.cancel_login()

    def _on_login_needed(self, detail):
        """자동 로그인 실패/브라우저 로그인 실패·취소 → 로그인 버튼 대기 상태."""
        self._logging_in = False
        self.login_btn.setEnabled(True)
        self.login_cancel_btn.hide()
        self.login_status.setText(detail or "")
        self.stack.setCurrentIndex(0)

    def _on_resolved(self, uuid, name):
        self._uuid = uuid
        self._name = name or (uuid[:8] + "…")
        self._logging_in = False
        self.login_cancel_btn.hide()
        self.login_status.setText("")
        self.welcome.setText(f"<span style='color:#5dcaa5; font-size:26px; font-weight:900'>[ {self._name} ]</span> 님, 환영합니다")
        self._live = False; self._pz = False; self._connected = False
        self._set_row(self.r_uuid, True,  "치지직 로그인 완료")
        self._set_row(self.r_live, False, "방송 상태 확인 중…")
        self._set_row(self.r_pz,   False, "Project Zomboid 확인 중…")
        self._set_row(self.r_conn, False, "인게임 접속 확인 중…")
        self.connect_btn.setEnabled(False)
        self.stack.setCurrentIndex(2)
        self.core.start_poll(uuid)

    def _on_invalid(self):
        self._logging_in = False
        self.login_cancel_btn.hide()
        self.stack.setCurrentIndex(1)

    def _retry(self):
        """화이트리스트 미등재 → 다른 계정 로그인. 저장 토큰을 지우고 로그인 화면으로."""
        _clear_refresh_token()
        self._on_login_needed("")

    def _logout(self):
        """체크리스트에서 로그아웃 — 폴링 중단 + 저장 토큰 삭제 후 로그인 화면으로"""
        self.core.stop_poll()
        _clear_refresh_token()
        self._uuid = ""; self._name = ""
        self._live = False; self._pz = False; self._connected = False
        self._on_login_needed("")

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

    def _refresh(self):
        self.connect_btn.setEnabled(self._live and self._pz and self._connected)

    def _go_main(self):
        self.core.stop_poll()
        preset = {"uuid": self._uuid, "name": self._name, "autostart": True}
        self.main_win = MainWindow(preset=preset)
        center_on_screen(self.main_win)
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
    win = LauncherWindow(); center_on_screen(win); win.show()
    sys.exit(app.exec_())


if __name__ == "__main__":
    main()
