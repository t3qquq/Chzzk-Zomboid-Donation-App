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

19세 방송:  네이버 NID 쿠키(성인인증 계정)를 넘기면 19+ 방송도 수신. 쿠키는 약 한 달이면 만료.
라인 포맷(모드 DonationReceiver.lua 규약):  amount,featureId,sender,message
    (featureId/sender/message URL 인코딩·featureId는 reward_tiers 매핑에 없으면 빈 문자열로
    기록되며, 그 경우 모드 쪽에선 통계만 잡히고 게임 효과는 발동하지 않음)
"""

import asyncio
import json
import os
import re
import sys
import threading
import time
from collections import namedtuple
from pathlib import Path

from PyQt5.QtCore import Qt, QObject, pyqtSignal, QTimer, QSharedMemory
from PyQt5.QtGui import QIcon, QPixmap
from PyQt5.QtWidgets import (
    QApplication, QWidget, QLabel, QLineEdit, QPushButton, QComboBox,
    QTextEdit, QVBoxLayout, QHBoxLayout, QFileDialog, QFrame,
    QCheckBox, QStackedWidget, QMessageBox,
)


# ── 화이트리스트 (시즌 참가 채널 UUID) — 원격 fetch ────────────────────────────
# 시즌 중 스트리머 추가/삭제는 이 JSON 만 커밋하면 됨 (exe 재빌드 불필요). URL 바꿀 때만 재빌드.
#  파일 포맷 셋 다 지원 (32자리 hex만 추출):
#    1) 줄당 하나       UUID / URL / 텍스트 무엇이든. '#' 뒤는 주석
#    2) JSON 배열       ["uuid", ...]
#    3) JSON 객체       {"이름":"uuid", ...}  또는  {"whitelist":[...]}


VERSION = "v2.1.0"



WHITELIST_URL = "https://raw.githubusercontent.com/t3qquq/myPZ-Configs/refs/heads/main/streamer%20whitelist.json"
UA = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"}
FORCE_ONLINE = False

# ── 로컬 설정 (홈 폴더, exe 옆 아님 -> 권한 문제 회피) ────────────────────────
CONFIG_DIR = Path.home() / ".chzzk_zomboid"
CONFIG_PATH = CONFIG_DIR / "config.json"

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


# ── 채널 입력 정규화 ──────────────────────────────────────────────────────────
HEX32 = re.compile(r"[0-9a-fA-F]{32}")

def resource_path(rel):
    """exe(PyInstaller)로 묶였을 때든 그냥 실행이든 리소스 파일 경로를 찾는다."""
    base = getattr(sys, "_MEIPASS", os.path.dirname(os.path.abspath(__file__)))
    return os.path.join(base, rel)

ICON_FILE = "PuppetAPI_smart.ico"

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


class DonationSource:
    """치지직 수신 인터페이스. chzzkpy든 공식 API든 이 3개만 구현하면 코어는 안 바뀐다."""

    async def resolve_channel(self, text):
        """입력(URL/채널명/UUID) -> (uuid, 표시이름). 못 찾으면 (None, 사유)."""
        raise NotImplementedError

    async def connect(self, uuid, emit, nid_aut=None, nid_ses=None):
        """연결 후 도네마다 emit(Donation) 호출. 정상 종료 시 리턴, 문제 시 SourceError."""
        raise NotImplementedError

    async def close(self):
        raise NotImplementedError


class ChzzkpySource(DonationSource):
    """chzzkpy(비공식) 기반 구현. ← 이 파일에서 chzzkpy 를 import/호출하는 유일한 곳."""

    def __init__(self, grace_sec=3.0):
        self.grace = grace_sec
        self._client = None

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

    async def connect(self, uuid, emit, nid_aut=None, nid_ses=None):
        from chzzkpy.unofficial.chat import ChatClient
        self._client = ChatClient(uuid)
        started = time.monotonic()
        grace = self.grace

        @self._client.event
        async def on_donation(message):     # chzzkpy 가 함수명으로 이벤트 매칭 -> 이름 고정
            if time.monotonic() - started < grace:
                return                       # 접속 직후 리플레이된 과거 도네 무시
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

        try:
            await self._client.start(nid_aut, nid_ses)
        except Exception as e:
            low = f"{type(e).__name__}: {e}".lower()
            if "adult" in low or "verification" in low:
                raise AdultVerificationRequired() from e
            if any(k in low for k in ("chat_channel", "channel_is_null", "is_null", "not live", "offline")):
                raise ChannelOffline() from e
            raise

    async def close(self):
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
        "debuff_roulette":     "디버프 룰렛",
        "buff_roulette":       "버프 룰렛",
        "zombie_roulette":     "좀비 룰렛",
        "sprinter5":           "스프린터 5마리",
        "bandit_melee":        "적대 NPC (근접)",
        "vaccine":             "백신",
        "bandit_ranged":       "적대 NPC (원거리)",
        "exile":               "추방 텔레포트",
        "backroom":            "백룸",
        "missile":             "미사일 폭격",
        "random_weapon":       "랜덤 무기 (미구현)",
        "random_skill_potion": "랜덤 스킬 물약 (미구현)",
        "vehicle_kit":         "차량소환 키트 (미구현)",
        "revive_ticket":       "즉시부활 티켓 (미구현)",
        "cdda_spawn":          "CDDA 소환 (미구현)",
        "secret_passage_kit":  "비밀통로 키트 (미구현)",
        "horde_night":         "호드나이트 (미구현)",
        "rise_up_dead_man":    "라이즈 업 데드 맨 (미구현)",
    }

    # 금액(원) -> featureId. 유저가 GUI에서 자유롭게 재배정 가능(reward_tiers).
    # 이 값은 config.json에 reward_tiers가 없을 때(첫 실행/구버전 마이그레이션)의 기본값.
    DEFAULT_REWARD_TIERS = {
        1000:   "debuff_roulette",
        2000:   "buff_roulette",
        5000:   "zombie_roulette",
        10000:  "sprinter5",
        20000:  "bandit_melee",
        35000:  "vaccine",
        40000:  "bandit_ranged",
        150000: "missile",
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
    """원격 화이트리스트 fetch. 성공하면 config 에 캐시. 실패하면 캐시 폴백(없으면 빈 집합)."""
    import aiohttp
    try:
        timeout = aiohttp.ClientTimeout(total=6)
        async with aiohttp.ClientSession(headers=UA, timeout=timeout) as s:
            async with s.get(WHITELIST_URL) as r:
                raw = await r.text()
        wl = parse_whitelist(raw)
        if wl:
            cfg = load_config(); cfg["whitelist"] = sorted(wl); save_config(cfg)
            return wl
    except Exception:
        pass
    return set(load_config().get("whitelist", []))   # 네트워크 실패 → 마지막 캐시로 동작


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
#  코어: 수신 워커 (스레드 + asyncio -> Qt 시그널, 플랫폼/게임 중립)
# ═══════════════════════════════════════════════════════════════════════════════
class DonationWorker(QObject):
    donation = pyqtSignal(int, str, str)   # amount, sender, message
    status   = pyqtSignal(str, str)        # text, color(hex)
    resolved = pyqtSignal(str, str)        # uuid, display_name
    failed   = pyqtSignal(str)             # 멈춤
    note     = pyqtSignal(str)             # 로그용 (안 멈춤)
    adult_blocked = pyqtSignal()           # 19세 방송으로 전환 감지 (멈춤 + 게이트 복귀)

    def __init__(self, source, channel_text, nid_aut="", nid_ses="", reconnect_sec=5.0):
        super().__init__()
        self.source = source               # ← 어떤 수신 방식이든 DonationSource 만 받는다
        self.channel_text = channel_text
        self.nid_aut = nid_aut or None
        self.nid_ses = nid_ses or None
        self.reconnect = reconnect_sec
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

    def start(self):
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def stop(self):
        self._stop = True
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
            try:
                self.status.emit("연결됨", "#5dcaa5")
                self.loop.run_until_complete(
                    self.source.connect(uuid, self._emit, self.nid_aut, self.nid_ses))
                if self._stop:
                    break
                self._note_once("연결 끊김 (방송 종료?) — 재접속 대기 중")
                self.status.emit("재접속 대기…", "#ef9f27")
            except AdultVerificationRequired:
                self.adult_blocked.emit()
                return
            except ChannelOffline:
                if self._stop:
                    break
                self._note_once("방송이 꺼져 있어. 방송 시작하면 자동으로 연결됨.")
                self.status.emit("방송 대기 중", "#ef9f27")
            except Exception as e:
                if self._stop:
                    break
                self._note_once("연결 오류: " + f"{type(e).__name__}: {e}")
                self.status.emit("재접속 대기…", "#ef9f27")
            self._sleep(self.reconnect)

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


class MainWindow(QWidget):
    def __init__(self, preset=None):
        super().__init__()
        self.preset = preset or {}        # 런처에서 넘어온 {channel,uuid,name,autostart}
        self.setWindowTitle("치지직 API Launcher  "+VERSION)
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
        self.tier_row_widgets = []                    # [(row_widget, amt_edit, feat_combo), ...]
        # reward_tiers는 _build()가 편집 테이블을 그릴 때 이미 필요하므로 그 전에 로드.
        # 우선순위: 게이트에서 넘어온 preset["reward_tiers"](방금 import) > config.json > 기본값
        self.reward_preset_locked = "reward_tiers" in self.preset
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

        # 리워드 티어 (금액 <-> 기능 편집)
        root.addWidget(self._muted("리워드 티어  —  금액 ↔ 기능 편집 후 ‘저장’ (정확히 일치하는 금액만 발동)"))
        self.tiers_box = QVBoxLayout(); self.tiers_box.setSpacing(4)
        root.addLayout(self.tiers_box)
        for amt, fid in sorted(self.adapter.reward_tiers.items()):
            self._add_tier_row(amt, fid)

        tctl = QHBoxLayout()

        self.add_row_btn = QPushButton("+ 행 추가")
        self.add_row_btn.setObjectName("link")
        self.add_row_btn.clicked.connect(lambda: self._add_tier_row())
        tctl.addWidget(self.add_row_btn)

        tctl.addStretch(1)

        self.export_btn = QPushButton("내보내기")
        self.export_btn.setObjectName("link")
        self.export_btn.clicked.connect(self._export_reward_preset)
        tctl.addWidget(self.export_btn)

        self.save_tiers_btn = QPushButton("저장")
        self.save_tiers_btn.clicked.connect(self._save_tiers)
        tctl.addWidget(self.save_tiers_btn)

        root.addLayout(tctl)

        if self.reward_preset_locked:
            self.add_row_btn.setEnabled(False)
            self.export_btn.setEnabled(False)
            self.save_tiers_btn.setEnabled(False)

            lock = QLabel("프리셋이 적용되어 편집이 잠겨 있습니다.")
            lock.setObjectName("muted")
            root.addWidget(lock)

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

    # --- 리워드 티어 (금액 <-> featureId) ---
    def _add_tier_row(self, amount=None, feature_id=None):
        """편집 테이블에 한 행(금액 입력 + 기능 콤보 + 삭제버튼) 추가."""
        row = QWidget()
        h = QHBoxLayout(row); h.setContentsMargins(0, 0, 0, 0); h.setSpacing(6)
        amt_edit = QLineEdit("" if amount is None else str(amount))
        amt_edit.setPlaceholderText("금액")
        amt_edit.setFixedWidth(90)
        feat_combo = QComboBox()
        for fid, label in self.adapter.FEATURES.items():
            feat_combo.addItem(label, fid)
        if feature_id:
            idx = feat_combo.findData(feature_id)
            if idx >= 0:
                feat_combo.setCurrentIndex(idx)
        del_btn = QPushButton("✕"); del_btn.setObjectName("link"); del_btn.setFixedWidth(28)
        del_btn.clicked.connect(lambda: self._remove_tier_row(row))
        if self.reward_preset_locked:
            amt_edit.setEnabled(False)
            feat_combo.setEnabled(False)
            del_btn.setEnabled(False)
        h.addWidget(amt_edit); h.addWidget(feat_combo, 1); h.addWidget(del_btn)
        self.tiers_box.addWidget(row)
        self.tier_row_widgets.append((row, amt_edit, feat_combo))

    def _remove_tier_row(self, row):
        for i, (w, _amt, _feat) in enumerate(self.tier_row_widgets):
            if w is row:
                self.tier_row_widgets.pop(i)
                break
        self.tiers_box.removeWidget(row)
        row.deleteLater()

    def _save_tiers(self):
        """편집 테이블 내용 -> adapter.reward_tiers 반영 + config.json persist + 테스트 콤보 갱신."""
        new_tiers = {}
        for _row, amt_edit, feat_combo in self.tier_row_widgets:
            txt = amt_edit.text().strip().replace(",", "")
            if not txt:
                continue
            try:
                amt = int(txt)
            except ValueError:
                self._log(f"⚠ 잘못된 금액 무시: {txt!r}"); continue
            if amt <= 0:
                continue
            if amt in new_tiers:
                self._log(f"⚠ 금액 중복({amt:,}) — 나중 값으로 덮어씀")
            new_tiers[amt] = feat_combo.currentData()
        if not new_tiers:
            self._log("⚠ 저장할 티어가 없음 — 최소 1개는 있어야 함"); return
        self.adapter.reward_tiers = new_tiers
        self._persist()
        self._build_test_combo()
        self._log(f"리워드 티어 저장됨 ({len(new_tiers)}개)")

    def _build_test_combo(self):
        self.test_combo.clear()
        for amt, fid in sorted(self.adapter.reward_tiers.items()):
            label = self.adapter.FEATURES.get(fid, fid)
            self.test_combo.addItem(f"{amt:,} — {label}", amt)

    def _export_reward_preset(self):
        fn, _ = QFileDialog.getSaveFileName(
            self, "리워드 프리셋 내보내기",
            str(Path.home() / "reward_preset.json"), "JSON (*.json)")
        if not fn:
            return
        data = {str(amt): fid for amt, fid in sorted(self.adapter.reward_tiers.items())}
        try:
            Path(fn).write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
            self._log(f"리워드 프리셋 내보냄: {fn}")
        except OSError as e:
            self._log(f"⚠ 내보내기 실패: {e}")

    # --- 설정 복원/저장 ---
    def _load_reward_tiers(self):
        """preset(게이트 import) > config.json > 기본값 순으로 adapter.reward_tiers를 채운다.
           featureId가 FEATURES에 없는 항목/파싱 안 되는 키는 무시(방어적 마이그레이션)."""
        raw = self.preset.get("reward_tiers") or self.cfg.get("reward_tiers")
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
            "reward_tiers": {str(amt): fid for amt, fid in self.adapter.reward_tiers.items()},
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
        self._wl = None
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

    async def _ensure_wl(self):
        if not self._wl:                       # 비어있으면(실패 캐시 포함) 다음 시도 때 재요청
            self._wl = await fetch_whitelist()
        return self._wl or set()

    async def _verify(self, text):
        src = ChzzkpySource()                  # resolve 는 기존 어댑터 재사용
        try:
            uuid, name = await src.resolve_channel(text)
        except Exception:
            uuid, name = None, ""

        if FORCE_ONLINE:
            self.resolved.emit(uuid, name or "")
            return

        wl = await self._ensure_wl()
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
        self.setWindowTitle("치지직 API Launcher  "+VERSION)
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
        self.reward_preset = None      # 체크리스트 화면에서 import한 {amount: featureId, ...} (선택)
        self._build()
        self.setStyleSheet(DARK_QSS)
        # preset 있으면 UUID 확인 완료 상태(체크리스트)부터 시작
        if preset and preset.get("uuid"):
            self.input.setText(preset.get("channel", ""))
            self._on_resolved(preset["uuid"], preset.get("name", ""))

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
        import_btn = QPushButton("리워드 프리셋 불러오기"); import_btn.setObjectName("link")
        import_btn.clicked.connect(self._import_reward_preset)
        prow.addWidget(import_btn)
        self.preset_status = self._muted(""); self.preset_status.setAlignment(Qt.AlignCenter)
        prow.addWidget(self.preset_status)
        prow.addStretch(1)
        v.addLayout(prow)

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

    def _import_reward_preset(self):
        """리워드 프리셋(JSON, {amount: featureId}) 불러오기. 실제 반영은 MainWindow로 넘어간 뒤
           reward_tiers 로 로드됨 (_go_main 참고) — 여기선 파싱/검증만 하고 들고만 있는다."""
        fn, _ = QFileDialog.getOpenFileName(
            self, "리워드 프리셋 불러오기", str(Path.home()), "JSON (*.json)")
        if not fn:
            return
        try:
            raw = json.loads(Path(fn).read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as e:
            self.preset_status.setText(f"⚠ 불러오기 실패: {e}")
            return
        if not isinstance(raw, dict) or not raw:
            self.preset_status.setText("⚠ 형식이 올바르지 않음 ({amount: featureId} JSON)")
            return
        valid = {}
        for k, v in raw.items():
            try:
                amt = int(k)
            except (TypeError, ValueError):
                continue
            if amt > 0 and v in ZomboidAdapter.FEATURES:
                valid[k] = v
        if not valid:
            self.preset_status.setText("⚠ 유효한 티어가 없음 (featureId 불일치)")
            return
        self.reward_preset = valid
        self.preset_status.setText(f"프리셋 로드됨 ({len(valid)}개) — 연동 시작 시 적용")
        self.preset_status.setStyleSheet("color:#5dcaa5;")

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
        if self.reward_preset:
            preset["reward_tiers"] = self.reward_preset   # MainWindow._load_reward_tiers가 최우선으로 사용
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
