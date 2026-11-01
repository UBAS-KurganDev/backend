from enum import StrEnum
from typing import ClassVar

from pydantic import BaseModel, Field, field_validator


class CabinClass(StrEnum):
    ECONOMY = "E"
    PREMIUM_ECONOMY = "W"
    BUSINESS = "B"
    FIRST = "F"


class GeoInfo(BaseModel):
    lat: float | str | None = None
    lon: float | str | None = None
    country: str | None = None


class DirectionSegment(BaseModel):
    airCompany: str
    flightNumber: str
    flightTime: str
    journeyTime: str
    origin: str = Field(alias="from")
    destination: str = Field(alias="to")
    startTime: str
    startDate: str
    endTime: str
    endDate: str | None = ""
    fromGeo: GeoInfo | None = None
    toGeo: GeoInfo | None = None


class FlightOffer(BaseModel):
    currency: str
    price: float
    fareType: str
    departDate: str
    origin: str = Field(alias="from")
    destination: str = Field(alias="to")
    to_string: str
    directions: list[list[DirectionSegment]]


class SearchResultResponse(BaseModel):
    offers: list[FlightOffer] = Field(default_factory=list)


class SearchRequest(BaseModel):
    """Параметры поиска перелёта: откуда -> куда -> дата.

    Отвечает за два важных момента:
    1. Генерацию URL для поиска на agent.ru (по IATA-кодам аэропортов).
       Путь строится так же, как на самом сайте: дата без разделителей (0511),
       коды аэропортов в нижнем регистре, затем '|' и pax-код + класс.
    2. Формирование ключа кэша вида flights:SVX:AYT:2026-10-16:E:1:0:0.
       Направление зашито в ключ (SVX:AYT != AYT:SVX).
    """

    DATE_CODE_MIN_LEN: ClassVar[int] = 4  # длина кода даты в URL, например 0511
    MAX_TRAVELERS: ClassVar[int] = 9  # максимальное число пассажиров одного типа

    origin: str
    destination: str
    depart_date: str
    adults: int = 1
    children: int = 0
    infants: int = 0
    direct_flights_only: bool = False
    cabin_class: CabinClass = CabinClass.ECONOMY

    @field_validator("origin", "destination", "depart_date")
    @classmethod
    def strip_fields(cls, value: str) -> str:
        return value.strip()

    @field_validator("origin", "destination")
    @classmethod
    def normalize_iata(cls, value: str) -> str:
        return value.upper()

    @field_validator("adults")
    @classmethod
    def adults_not_zero(cls, value: int) -> int:
        if value < 1:
            raise ValueError("adults должен быть не меньше 1")
        return value

    @field_validator("adults", "children", "infants")
    @classmethod
    def travelers_in_range(cls, value: int) -> int:
        if value < 0 or value > cls.MAX_TRAVELERS:
            raise ValueError(f"значение должно быть в диапазоне 0..{cls.MAX_TRAVELERS}")
        return value

    @property
    def travelers_code(self) -> str:
        """Код пассажиров в том же формате, что формирует сайт agent.ru
        (см. CQgoXZr7.js): 'a<взрослые>' всегда, 'c<дети>' и 'i<младенцы>'
        — только если их больше нуля. Буква класса идёт последней.
        """
        code = f"a{self.adults}"
        if self.children:
            code += f"c{self.children}"
        if self.infants:
            code += f"i{self.infants}"
        if self.direct_flights_only:
            code += "d1"
        return f"{code}{self.cabin_class.value}"

    @property
    def date_code(self) -> str:
        """Дата в формате сайта: 'день' + 'месяц', всегда 4 символа
        (с ведущим нулём), например 0511 для 05.11.
        """
        day, month = self.depart_date[8:10], self.depart_date[5:7]
        return f"{int(day):02d}{int(month):02d}"

    @property
    def search_url(self) -> str:
        """Прямая ссылка поиска на agent.ru. Путь строится так же, как это
        делает фронтенд сайта (см. CHUNK CQgoXZr7.js):
        <дата><код_аэропорта_откуда><код_аэропорта_куда>|<pax-код><класс>.
        Коды аэропортов в нижнем регистре, параметры lang/curr — как у сайта.
        """
        code = f"{self.date_code}{self.origin.lower()}{self.destination.lower()}"
        return (
            f"https://www.agent.ru/air/avia-results/{code}%7C{self.travelers_code}"
            f"/?lang=ru&curr=RUB"
        )

    @property
    def cache_key(self) -> str:
        """Ключ кэша: направление + дата + класс + пассажиры + фильтр прямых."""
        direct = "d1" if self.direct_flights_only else "any"
        return (
            f"flights:{self.origin}:{self.destination}:{self.depart_date}:"
            f"{self.cabin_class.value}:{self.adults}:{self.children}:{self.infants}:{direct}"
        )

    def __repr__(self) -> str:
        return (
            f"SearchRequest({self.origin} -> {self.destination}, {self.depart_date}, "
            f"{self.travelers_code})"
        )
