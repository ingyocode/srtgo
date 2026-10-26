"""
코레일 예매 어댑터

srtgo가 쓰는 Korail 인터페이스를 korail-mobile-api(KorailClient) 위에 맞춘다.
앱 무결성 토큰(DynaPath), NetFunnel 대기열, 로그인 비밀번호 암호화는 라이브러리가 처리한다.
"""

from datetime import datetime, timedelta, timezone

from korail_mobile_api import (
    CardPayment,
    KorailApiError,
    KorailAppError,
    KorailAuthError,
    KorailClient,
    KorailConfig,
    KorailNoResultsError,
    KorailPassengerCounts,
    KorailReservationJobType,
    KorailReserveOption,
    KorailSoldOutError,
    KorailTransportError,
    OriginalTicketReference,
    PaidTicket,
    RefundCompanion,
    ReservationHoldResponse,
    ReservationJourney,
    ReservationResource,
    TicketListTrain,
    TicketReservationDetailRequest,
    TrainSearchQuery,
)


# Constants
# 한국시간. 실행 환경 타임존과 무관하게 항상 UTC+9로 계산한다.
KST = timezone(timedelta(hours=9))

# srtgo.py가 쓰는 이름을 라이브러리 타입에 연결한다
KorailError = KorailApiError
ReserveOption = KorailReserveOption

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


def _to_int(value, fallback=0):
    """응답 필드가 비었거나 숫자가 아니면 fallback"""
    try:
        return int(value)
    except (TypeError, ValueError):
        return fallback


def _hhmm(value):
    return f"{value[:2]}:{value[2:4]}" if value else "--:--"


# Schedule classes
class Schedule:
    """열차 종류·번호와 출발일·시각·구간 표시"""

    def __init__(
        self,
        train_type_name,
        train_no,
        dep_name,
        arr_name,
        dep_date,
        dep_time,
        arr_time,
    ):
        self.train_type_name = train_type_name
        self.train_no = train_no
        self.dep_name = dep_name
        self.arr_name = arr_name
        self.dep_date = dep_date
        self.dep_time = dep_time
        self.arr_time = arr_time

    def __repr__(self):
        dep_date = (
            f"{self.dep_date[4:6]}/{self.dep_date[6:8]}" if self.dep_date else "--/--"
        )
        train_line = f"[{(self.train_type_name or '')[:3]} {self.train_no}]"

        return (
            f"{train_line:<11s}"
            f"{dep_date} {_hhmm(self.dep_time)}~{_hhmm(self.arr_time)}  "
            f"{self.dep_name}~{self.arr_name}"
        )

    def _duration(self):
        if not (self.dep_time and self.arr_time):
            return 0
        duration = (int(self.arr_time[:2]) * 60 + int(self.arr_time[2:4])) - (
            int(self.dep_time[:2]) * 60 + int(self.dep_time[2:4])
        )
        return duration + 24 * 60 if duration < 0 else duration


class Train(Schedule):
    """조회한 열차. 예약할 때는 라이브러리의 TrainSummary(summary)를 그대로 넘긴다."""

    def __init__(self, summary):
        super().__init__(
            summary.train_class_name,
            summary.train_no,
            summary.departure_station_name,
            summary.arrival_station_name,
            summary.departure_date,
            summary.departure_time,
            summary.arrival_time,
        )
        self.summary = summary

    def __repr__(self):
        repr_str = super().__repr__()
        repr_str += f"  특실 {'가능' if self.has_special_seat() else '매진'}"
        repr_str += f", 일반실 {'가능' if self.has_general_seat() else '매진'}"
        # 예약대기 플래그가 음수이거나 없으면 예약대기를 받지 않는 열차다
        if _to_int(self.summary.wait_reservation_flag, -1) >= 0:
            repr_str += f", 예약대기 {'가능' if self.has_waiting_list() else '매진'}"
        repr_str += f" ({self._duration():>3d}분)"
        return repr_str

    def has_special_seat(self):
        return self.summary.has_special_seat()

    def has_general_seat(self):
        return self.summary.has_general_seat()

    def has_seat(self):
        return self.summary.has_seat()

    def has_waiting_list(self):
        return self.summary.has_waiting_list()


class Ticket(Schedule):
    """발권된 승차권 한 장"""

    def __init__(self, ticket):
        train = ticket.trains[0] if ticket.trains else TicketListTrain()
        super().__init__(
            train.train_class_name,
            train.train_no,
            train.departure_station_name,
            train.arrival_station_name,
            train.departure_date,
            train.departure_time,
            train.arrival_time,
        )
        self.ticket = ticket
        self.pnr_no = ticket.pnr_no
        self.car_no = train.car_no
        self.seat_no = train.seat_no
        self.seat_no_count = _to_int(train.seat_count, 1)
        self.price = _to_int(train.received_amount)

    def __repr__(self):
        repr_str = super().__repr__()
        repr_str += f" => {self.car_no}호 {self.seat_no}"
        if self.seat_no_count > 1:
            repr_str += f" 외 {self.seat_no_count - 1}석"
        repr_str += f", {self.price}원"
        return repr_str


class Reservation(Schedule):
    """결제 전 예약. 방금 예약한 건은 라이브러리 홀드를 hold에 둔다."""

    def __init__(
        self,
        rsv_id,
        price,
        seat_no_count,
        buy_limit_date,
        buy_limit_time,
        is_waiting,
        **schedule,
    ):
        super().__init__(**schedule)
        self.rsv_id = rsv_id
        self.price = price
        self.seat_no_count = seat_no_count
        self.buy_limit_date = buy_limit_date
        self.buy_limit_time = buy_limit_time
        self.is_waiting = is_waiting
        self.tickets = []
        self.hold = None

    @classmethod
    def from_history(cls, row):
        """예약 내역(list_reservations)의 열차 행"""
        return cls(
            row.pnr_no,
            _to_int(row.reserved_amount),
            row.seat_count or 0,
            row.payment_deadline_date,
            row.payment_deadline_time,
            row.payment_deadline_date == "00000000"
            or row.payment_deadline_time == "235959",
            train_type_name=row.train_class_name,
            train_no=row.train_no,
            dep_name=row.departure_station_name,
            arr_name=row.arrival_station_name,
            dep_date=row.run_date,
            dep_time=row.departure_time,
            arr_time=row.arrival_time,
        )

    @classmethod
    def from_hold(cls, hold, train, seat_no_count, is_waiting):
        """방금 만든 홀드. 역 이름 등은 조회한 열차에서 가져온다."""
        reservation = cls(
            hold.pnr_no,
            _to_int(hold.received_amount),
            seat_no_count,
            hold.payment_deadline_date,
            hold.payment_deadline_time,
            is_waiting,
            train_type_name=train.train_type_name,
            train_no=train.train_no,
            dep_name=train.dep_name,
            arr_name=train.arr_name,
            dep_date=train.dep_date,
            dep_time=train.dep_time,
            arr_time=train.arr_time,
        )
        reservation.hold = hold
        return reservation

    def __repr__(self):
        repr_str = super().__repr__()
        repr_str += f" ({self._duration():>3d}분)"
        repr_str += f", {self.price}원({self.seat_no_count}석)"
        if self.is_waiting:
            repr_str += ", 예약대기"
        elif self.buy_limit_date and self.buy_limit_time:
            buy_limit_time = _hhmm(self.buy_limit_time)
            buy_limit_date = (
                f"{int(self.buy_limit_date[4:6])}월 {int(self.buy_limit_date[6:])}일"
            )
            repr_str += f", 구입기한 {buy_limit_date} {buy_limit_time}"
        return repr_str


class Seat:
    """예약 좌석 한 개"""

    def __init__(self, seat):
        self.car = seat.car_no
        self.seat = seat.seat_no
        self.seat_type = seat.room_class_name
        self.passenger_type = seat.passenger_type_name
        self.price = _to_int(seat.received_amount)
        self.discount = _to_int(seat.total_discount_amount)
        self.is_waiting = not seat.seat_no

    def __repr__(self):
        if self.is_waiting:
            return (
                f"예약대기 ({self.seat_type}) {self.passenger_type}"
                f"[{self.price}원({self.discount}원 할인)]"
            )
        return (
            f"{self.car}호차 {self.seat} ({self.seat_type}) {self.passenger_type} "
            f"[{self.price}원({self.discount}원 할인)]"
        )


# Passenger classes
class Passenger:
    """승객 종류별 인원. field는 KorailPassengerCounts의 필드 이름이다."""

    field = None

    def __init__(self, count=1):
        self.count = count

    @staticmethod
    def to_counts(passengers):
        counts = {"adult": 0}
        for passenger in passengers:
            if not isinstance(passenger, Passenger):
                raise TypeError("Passengers must be based on Passenger")
            counts[passenger.field] = counts.get(passenger.field, 0) + passenger.count
        return KorailPassengerCounts(**counts)


class AdultPassenger(Passenger):
    field = "adult"


class ChildPassenger(Passenger):
    field = "child"


class SeniorPassenger(Passenger):
    field = "senior"


class Disability1To3Passenger(Passenger):
    field = "severe_disability"


class Disability4To6Passenger(Passenger):
    field = "mild_disability"


# Options
class TrainType:
    KTX = "100"
    SAEMAEUL = "101"
    MUGUNGHWA = "102"
    TONGGUEN = "103"
    NURIRO = "102"
    ALL = "109"
    AIRPORT = "105"
    KTX_SANCHEON = "100"
    ITX_SAEMAEUL = "101"
    ITX_CHEONGCHUN = "104"


class Korail:
    """srtgo가 쓰는 코레일 인터페이스"""

    def __init__(self, korail_id, korail_pw, auto_login=True, verbose=False):
        # 기본 설정에서 DynaPath 토큰과 NetFunnel 대기열이 켜져 있다
        self._client = KorailClient(KorailConfig())
        self._reservations = ReservationResource(self._client)
        self.korail_id = korail_id
        self.korail_pw = korail_pw
        self.verbose = verbose
        self.logined = False
        self.membership_number = None
        self.name = None
        self.email = None
        self.phone_number = None
        if auto_login:
            self.login(korail_id, korail_pw)

    @property
    def is_login(self):
        """SRT 클래스와 이름을 맞춘 별칭"""
        return self.logined

    def _log(self, msg) -> None:
        if self.verbose:
            print(f"[*] {msg}")

    def login(self, korail_id=None, korail_pw=None):
        if korail_id:
            self.korail_id = korail_id
        if korail_pw:
            self.korail_pw = korail_pw

        # 라이브러리는 하이픈 없는 전화번호만 받는다
        member_no = (
            self.korail_id if "@" in self.korail_id else self.korail_id.replace("-", "")
        )
        try:
            session = self._client.login(member_no, self.korail_pw)
        except KorailAuthError as err:
            # 아이디·비밀번호 오류는 예외 대신 False로 알린다 (srtgo.py가 is_login으로 확인)
            print(f"로그인 실패: {err}")
            self.logined = False
            return False

        self.membership_number = session.member_card_no
        self.name = session.raw.get("strCustNm")
        self.email = session.raw.get("strEmailAdr")
        self.phone_number = session.raw.get("strCpNo")
        print(
            f"로그인 성공: {self.name} (멤버십번호: {self.membership_number}, 전화번호: {self.phone_number})"
        )
        self.logined = True
        return True

    def logout(self):
        self._client.logout()
        self.logined = False

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
        kst_now = datetime.now(KST)
        counts = Passenger.to_counts(passengers or [AdultPassenger()])
        query = TrainSearchQuery(
            departure_station=dep,
            arrival_station=arr,
            departure_date=date or kst_now.strftime("%Y%m%d"),
            departure_time=time or kst_now.strftime("%H%M%S"),
            passengers=counts.adult,
            child_passengers=counts.child,
            senior_passengers=counts.senior,
            high_disability_passengers=counts.severe_disability,
            low_disability_passengers=counts.mild_disability,
            train_group_code=train_type,
        )

        try:
            result = self._client.search_trains(query)
        except KorailNoResultsError:
            return []
        self._log(result.raw)

        trains = [Train(summary) for summary in result.trains]
        return [
            t
            for t in trains
            if t.has_seat()
            or include_no_seats
            or (include_waiting_list and t.has_waiting_list())
        ]

    def reserve(self, train, passengers=None, option=ReserveOption.GENERAL_FIRST):
        counts = Passenger.to_counts(passengers or [AdultPassenger()])
        is_waiting = not train.has_seat()

        if is_waiting:
            # 예약대기는 일반실만 신청할 수 있다
            hold = self._client.reserve(
                train.summary,
                passengers=counts,
                job_type=KorailReservationJobType.STANDBY,
            )
            self._log(hold.raw)
            self._confirm_standby(hold)
        else:
            # option(일반실/특실 우선·전용)에 맞춰 남은 좌석이 있는 객실을 고른다
            hold = self._reservations.create(
                train.summary, passengers=counts, option=option
            )
            self._log(hold.raw)

        reservation = Reservation.from_hold(hold, train, counts.total, is_waiting)
        try:
            reservation.tickets = self.ticket_info(hold.pnr_no)
        except KorailApiError as err:
            # 예약은 이미 잡혔으므로 좌석 표시만 건너뛴다
            self._log(f"좌석 조회 실패: {err}")
        return reservation

    def _confirm_standby(self, hold):
        """앱처럼 예약대기 옵션(SMS 알림·좌석 등급 변경 없음)을 저장한다."""
        try:
            self._client.set_standby_options(hold)
        except KorailApiError as err:
            # 대기 예약은 이미 잡혔으므로 다시 예약하지 않도록 예외를 올리지 않는다
            print(f"예약대기 옵션 저장 실패: {err}")

    def _reservation_detail(self, rsv_id):
        return self._client.get_ticket_reservation_detail(
            TicketReservationDetailRequest(pnr_no=rsv_id)
        )

    def tickets(self):
        try:
            response = self._client.list_tickets()
        except KorailNoResultsError:
            return []
        self._log(response.raw)
        return [
            Ticket(ticket)
            for reservation in response.reservations
            for ticket in reservation.tickets
        ]

    def reservations(self):
        history = self._client.list_reservations()
        self._log(history.raw)
        reserves = []
        for row in history.items:
            reservation = Reservation.from_history(row)
            reservation.tickets = self.ticket_info(row.pnr_no)
            reserves.append(reservation)
        return reserves

    def ticket_info(self, rsv_id):
        try:
            detail = self._reservation_detail(rsv_id)
        except KorailNoResultsError:
            return []
        if not detail.journeys:
            return []
        return [Seat(seat) for seat in detail.journeys[0].seats]

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

        # 방금 만든 예약은 그 홀드로, 예약 내역에서 고른 건은 PNR로 홀드를 다시 받아 결제한다
        hold = rsv.hold or self._client.get_reservation_hold(rsv.rsv_id)
        card = CardPayment(
            card_number=card_number,
            card_password=card_password,
            card_expire=card_expire,
            birthday=birthday,
            installment=str(installment),
            card_type=card_type,
        )
        result = self._client.pay_with_card(hold, card)
        self._log(result.raw)
        if result.str_result != "SUCC":
            # 카드 거절은 예외가 아니라 FAIL 응답으로 돌아온다
            raise KorailAppError(result.h_msg_cd, result.h_msg_txt, raw=result.raw)
        return True

    def cancel(self, rsv):
        if not isinstance(rsv, Reservation):
            raise TypeError("rsv must be a Reservation instance")
        hold = rsv.hold or self._cancel_hold(rsv.rsv_id)
        self._client.cancel_unpaid_hold(hold)
        return True

    def _cancel_hold(self, rsv_id):
        """예약 상세의 PNR·여정으로 취소용 홀드를 만든다.

        결제용 홀드가 아니므로 pay_with_card에는 쓰지 않는다.
        """
        detail = self._reservation_detail(rsv_id)
        return ReservationHoldResponse(
            str_result=detail.str_result,
            pnr_no=detail.pnr_no,
            journey_count=detail.journey_count,
            journeys=tuple(
                ReservationJourney(
                    journey_sequence=journey.journey_sequence,
                    reservation_change_no=journey.reservation_change_no,
                )
                for journey in detail.journeys
            ),
            payable=False,
        )

    def refund(self, ticket):
        if not isinstance(ticket, Ticket):
            raise TypeError("ticket must be a Ticket instance")

        # 승차권 상세 → PaidTicket → 수수료 조회 → 환불 (한 장씩)
        reference = OriginalTicketReference(
            sale_window_no=ticket.ticket.sale_window_no,
            sale_date=ticket.ticket.return_sale_date,
            sale_sequence=ticket.ticket.sale_sequence,
            return_password=ticket.ticket.return_password,
        )
        paid = PaidTicket.from_refund_detail(
            self._client.get_refund_ticket_detail(reference)
        )
        commission = self._client.get_refund_fee(
            reference,
            RefundCompanion(
                name=paid.companion_name,
                certificate_no=paid.companion_certificate_no,
            ),
        )
        result = self._client.refund(
            paid,
            commission=commission,
            pbp_acceptance_target_flag=ticket.ticket.pbp_acceptance_target_flag,
        )
        self._log(result.raw)
        return True
