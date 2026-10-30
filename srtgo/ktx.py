"""코레일 예매 어댑터.

srtgo가 쓰는 Korail 인터페이스를 pykorail 위에 맞춘다.
앱 무결성 토큰(DynaPath), NetFunnel 대기열, 로그인 비밀번호 암호화, 예약대기
처리는 pykorail이 담당한다.
"""

from datetime import datetime, timedelta, timezone

from pykorail import (
    AdultPassenger,
    Card,
    ChildPassenger,
    Disability1To3Passenger,
    Disability4To6Passenger,
    LoginFailedError,
    NeedToLoginError,
    NoResultsError,
    PykorailError,
    ReserveOption,
    SeniorPassenger,
    SoldOutError,
    TrainType,
    TransportError,
)
from pykorail import Korail as _KorailClient

# Constants
# 한국시간. 실행 환경 타임존과 무관하게 항상 UTC+9로 계산한다.
KST = timezone(timedelta(hours=9))

# srtgo.py가 쓰는 예외 이름을 pykorail 타입에 연결한다.
# KorailError는 pykorail 최상위 예외로 두어, srtgo의 `except KorailError`가 API 오류와
# 전송 오류(NetFunnel 포함)를 모두 잡도록 한다 (기존 korail-mobile-api와 같은 동작).
KorailError = PykorailError
KorailAuthError = NeedToLoginError  # 세션 만료(P058) 또는 로그인 전 호출
KorailSoldOutError = SoldOutError  # 잔여석 없음
KorailTransportError = TransportError  # 연결·전송 오류

__all__ = [
    "AdultPassenger",
    "ChildPassenger",
    "Disability1To3Passenger",
    "Disability4To6Passenger",
    "Korail",
    "KorailAuthError",
    "KorailError",
    "KorailSoldOutError",
    "KorailTransportError",
    "ReserveOption",
    "SeniorPassenger",
    "TrainType",
]


class Reservation:
    """결제 전 예약. srtgo가 쓰는 속성(tickets·is_ticket)을 pykorail 예약에 얹는다.

    pykorail의 Reservation은 불변(frozen)이라 srtgo가 속성을 덧붙일 수 없어서
    얇게 감싼다. 결제·취소에 쓸 원본 예약은 reservation으로 보관한다.
    """

    def __init__(self, reservation):
        self.reservation = reservation
        # srtgo는 좌석 목록을 tickets로 참조한다.
        self.tickets = list(reservation.seats)
        # 발권 완료가 아니라 예약임을 뜻한다. srtgo가 결제/취소 분기에서 갱신한다.
        self.is_ticket = False

    @property
    def is_waiting(self):
        return self.reservation.is_waiting

    def __repr__(self):
        return repr(self.reservation)

    __str__ = __repr__


class Ticket:
    """발권 완료된 승차권. srtgo가 쓰는 is_ticket 속성을 pykorail 승차권에 얹는다."""

    def __init__(self, ticket):
        self.ticket = ticket
        self.is_ticket = True

    def __repr__(self):
        return repr(self.ticket)

    __str__ = __repr__


class Korail:
    """srtgo가 쓰는 코레일 인터페이스 (pykorail 어댑터)."""

    def __init__(self, korail_id, korail_pw, auto_login=True, verbose=False):
        # validate_stations=False: 역 이름 검증을 서버에 맡겨 기존 동작과 맞춘다.
        self._client = _KorailClient(verbose=verbose, validate_stations=False)
        self.korail_id = korail_id
        self.korail_pw = korail_pw
        self.verbose = verbose
        if auto_login:
            self.login(korail_id, korail_pw)

    @property
    def is_login(self):
        """SRT 클래스와 이름을 맞춘 별칭."""
        return self._client.is_logged_in

    @property
    def membership_number(self):
        return self._client.membership_number

    @property
    def name(self):
        return self._client.name

    @property
    def email(self):
        return self._client.email

    @property
    def phone_number(self):
        return self._client.phone_number

    def login(self, korail_id=None, korail_pw=None):
        if korail_id:
            self.korail_id = korail_id
        if korail_pw:
            self.korail_pw = korail_pw

        try:
            # pykorail이 이메일·전화번호(하이픈 포함)·회원번호 형식을 알아서 구분한다.
            self._client.login(self.korail_id, self.korail_pw)
        except LoginFailedError as err:
            # 아이디·비밀번호 오류는 예외 대신 False로 알린다 (srtgo.py가 is_login으로 확인).
            print(f"로그인 실패: {err}")
            return False

        print(
            f"로그인 성공: {self.name} "
            f"(멤버십번호: {self.membership_number}, 전화번호: {self.phone_number})"
        )
        return True

    def logout(self):
        self._client.logout()

    def search_train(
        self,
        dep,
        arr,
        date=None,
        time=None,
        train_type=TrainType.ALL,
        passengers=None,
        include_no_seats=False,
        include_waiting_list=False,
    ):
        try:
            return self._client.trains.search(
                dep,
                arr,
                depart_after=self._depart_after(date, time),
                train_type=train_type,
                passengers=passengers,
                include_no_seats=include_no_seats,
                include_waiting_list=include_waiting_list,
            )
        except NoResultsError:
            return []

    @staticmethod
    def _depart_after(date, time):
        """date(YYYYMMDD)·time(HHMMSS)를 KST datetime으로. 지난 시각은 지금으로 올린다.

        오래 도는 예매 대기 루프에서 고정된 출발 시각이 현재를 지나면 pykorail이
        PastDepartureError를 내므로, 지난 시각은 현재로 올려 루프가 멈추지 않게 한다.
        """
        if not date and not time:
            return None
        now = datetime.now(KST)
        moment = datetime.strptime(
            (date or now.strftime("%Y%m%d")) + (time or now.strftime("%H%M%S")),
            "%Y%m%d%H%M%S",
        ).replace(tzinfo=KST)
        return max(moment, now)

    def reserve(self, train, passengers=None, option=ReserveOption.GENERAL_FIRST):
        # 좌석이 없고 예약대기가 열린 열차는 pykorail이 대기 예약으로 처리한다.
        reservation = self._client.reservations.create(
            train, passengers=passengers, option=option
        )
        return Reservation(reservation)

    def reservations(self):
        return [Reservation(rsv) for rsv in self._client.reservations.all()]

    def tickets(self):
        return [Ticket(ticket) for ticket in self._client.tickets.all()]

    def pay_with_card(
        self,
        rsv,
        card_number,
        card_password,
        birthday,
        card_expire,
        installment=0,
        card_type="J",
    ):
        if not isinstance(rsv, Reservation):
            raise TypeError("rsv must be a Reservation instance")

        card = Card(
            number=card_number,
            password=card_password,
            verify_number=birthday,
            expire=card_expire,
            installment=int(installment),
            is_corporate=card_type == "S",
        )
        self._client.reservations.pay(rsv.reservation, card)
        return True

    def cancel(self, rsv):
        if not isinstance(rsv, Reservation):
            raise TypeError("rsv must be a Reservation instance")
        self._client.reservations.cancel(rsv.reservation)
        return True

    def refund(self, ticket):
        if not isinstance(ticket, Ticket):
            raise TypeError("ticket must be a Ticket instance")
        self._client.tickets.refund(ticket.ticket)
        return True
