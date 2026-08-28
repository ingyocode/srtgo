"""
코레일 앱 무결성 토큰(`x-dynapath-m-token`) 생성기.

korail2(그리고 이를 이식한 srtgo)에는 이 토큰이 없다. 코레일이 이후에 도입한 것으로,
토큰 없이 보낸 요청은 아래 DYNAPATH_PATHS에 해당하는 경로(로그인/열차조회/예매)에서
서버가 거부한다. k-skill(NomaDamas)의 `scripts/ktx_booking.py`
(`DynaPathMasterEngine`, `PatchedKorail`)의 구현을 옮겼다.
"""

import base64
import secrets
import time
from Crypto.Cipher import AES
from Crypto.Util.Padding import pad


# 이 경로들이 URL에 포함된 요청에만 토큰과 Sid를 붙인다.
DYNAPATH_PATHS = (
    "/classes/com.korail.mobile.certification.TicketReservation",
    "/classes/com.korail.mobile.nonMember.NonMemTicket",
    "/classes/com.korail.mobile.research.TrainResearch",
    "/classes/com.korail.mobile.research.ResidualSeatsResearch.do",
    "/classes/com.korail.mobile.seatMovie.ScheduleView",
    "/classes/com.korail.mobile.seatMovie.ScheduleViewSpecial",
    "/classes/com.korail.mobile.trn.prcFare.do",
    "/classes/com.korail.mobile.login.Login",
)

# Sid 파라미터 생성용 AES-128 키 (IV도 동일 값)
SID_KEY = b"2485dd54d9deaa36"

# nonce 문자셋: 대문자 + 숫자
NONCE_ALPHABET = "ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789"


def needs_dynapath(url: str) -> bool:
    return any(path in url for path in DYNAPATH_PATHS)


def generate_sid(device: str, timestamp_ms: int) -> str:
    """`Sid` 파라미터: AES-128-CBC(iv=key)로 `{device}{timestamp(ms)}`를 암호화한 base64 + 개행.

    토큰과 **같은 timestamp**로 만들어야 한다 (서버가 둘을 대조한다).
    """
    cipher = AES.new(SID_KEY, AES.MODE_CBC, SID_KEY)
    encrypted = cipher.encrypt(pad(f"{device}{timestamp_ms}".encode("utf-8"), AES.block_size))
    return base64.b64encode(encrypted).decode("utf-8") + "\n"


def generate_nonce(length: int = 4) -> str:
    return "".join(secrets.choice(NONCE_ALPHABET) for _ in range(length))


class DynaPathMasterEngine:
    """앱 무결성 토큰 생성 엔진"""

    APP_ID = "com.korail.talk"
    AS_VALUE = "%5B38ff229cb34c7dda8e28220a2d750cce%5D"
    DEVICE_MODEL = "SM-S928N"
    OS_TYPE = "Android"
    SDK_VERSION = "v1"
    # 토큰 본문의 os= 값. ktx.py의 USER_AGENT와 반드시 일치해야 한다.
    OS_VERSION = "13"

    TABLE = "3FE9jgRD4KdCyuawklqGJYmvfMn15P7US8XbxeLQtWT6OicBAopINs2Vh0HZrz"

    def __init__(self, app_start_ts: int = None):
        self._i8 = 161
        self._i9 = 30
        self._i10 = 2
        # 앱 기동 시각. 엔진 인스턴스 생성 시점을 쓴다. (인자는 대조 테스트용)
        self._app_start_ts = str(
            app_start_ts if app_start_ts is not None else int(time.time() * 1000)
        )

    @staticmethod
    def _string2xa1s(data: str) -> list:
        """코드포인트를 7비트 단위 시퀀스로 분해"""
        result = []
        for char in data:
            codepoint = ord(char)
            if codepoint < 128:
                result.append(codepoint)
            elif codepoint < 2048:
                result.append(128 | ((codepoint >> 7) & 15))
                result.append(codepoint & 127)
            elif codepoint >= 262144:
                result.append(160)
                result.append((codepoint >> 14) & 127)
                result.append((codepoint >> 7) & 127)
                result.append(codepoint & 127)
            elif (63488 & codepoint) != 55296:
                result.append(((codepoint >> 14) & 15) | 144)
                result.append((codepoint >> 7) & 127)
                result.append(codepoint & 127)
        return result

    @staticmethod
    def _make_key(key: str) -> int:
        total = 0
        for char in key:
            codepoint = ord(char)
            bit = 32768
            for _ in range(16):
                if bit & codepoint:
                    break
                bit >>= 1
            total = total * (bit << 1) + codepoint
        return total

    @staticmethod
    def _internal_char(base_table: str, remainder: int, current: str) -> str:
        """base_table에서 current에 아직 없는 문자 중 remainder번째"""
        seen = 0
        for char in base_table:
            if char in current:
                continue
            if seen == remainder:
                return char
            seen += 1
        return " "

    def _make_encode_table(self, num: int, encode_size: int, base_table: str) -> str:
        """키에서 파생된 치환 테이블"""
        chars = ""
        temp = num
        for index in range(encode_size):
            divisor = encode_size - index
            chars += self._internal_char(base_table, temp % divisor, chars)
            temp //= divisor
        return chars

    def _encode_normal_be(self, data: str, table: str) -> str:
        """2바이트씩 161진 -> 30진 3자리로 변환"""
        values = self._string2xa1s(data)
        output = []
        digits = [0] * (self._i10 + 1)

        idx = 0
        tail = len(values) % self._i10
        body_size = len(values) - tail

        while idx < body_size:
            value = 0
            for _ in range(self._i10):
                value = value * self._i8 + values[idx]
                idx += 1
            for d in range(self._i10 + 1):
                digits[d] = value % self._i9
                value //= self._i9
            for d in range(self._i10, -1, -1):
                output.append(table[digits[d]])

        if tail > 0:
            value = 0
            for _ in range(tail):
                value = value * self._i8 + values[idx]
                idx += 1
            for d in range(tail + 1):
                digits[d] = value % self._i9
                value //= self._i9
            while tail >= 0:
                output.append(table[digits[tail]])
                tail -= 1

        return "".join(output)

    def generate_token(self, device_id: str, timestamp_ms: int, nonce: str) -> str:
        plaintext = (
            f"ai={self.APP_ID}"
            f"&di={device_id}"
            f"&as={self.AS_VALUE}"
            f"&su=false&dbg=false&emu=false&hk=false"
            f"&it={self._app_start_ts}"
            f"&ts={timestamp_ms}"
            f"&rt=0"
            f"&os={self.OS_VERSION}"
            f"&dm={self.DEVICE_MODEL}"
            f"&st={self.OS_TYPE}"
            f"&sv={self.SDK_VERSION}"
        )

        dyn_key = f"v1+{nonce}+{timestamp_ms}"
        key_encoded = self._encode_normal_be(dyn_key, self.TABLE)
        derived_table = self._make_encode_table(
            self._make_key(dyn_key), self._i9, self.TABLE
        )
        body_encoded = self._encode_normal_be(plaintext, derived_table)

        return f"bEeEP{self.TABLE[len(key_encoded)]}{key_encoded}{body_encoded}"
