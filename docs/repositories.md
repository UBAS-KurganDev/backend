# Замена источников без привязки к SQLite

Планировщик использует интерфейсы из routing/ports.py:

| Порт | Метод | Ответственность |
| --- | --- | --- |
| TopologyRepository | destinations(origins) | Исходящие города для нескольких городов |
| CityRepository | get_many(city_codes) | Профили привлекательности и справочные расходы |
| CityGeography | timezone(city_code) | Стабильная география для проверки местной даты |
| VisaCostResolver | expenses(segments, profiles) | Визовые расходы всего маршрута |
| FlightProvider | search(query) | Реальные предложения билетов |

В runtime.create_planner по умолчанию создаются SqliteTopologyRepository,
SqliteCityRepository и SqliteGeographyRepository. Для временного источника
замените конкретный объект непосредственно здесь:

```python
cities = TemporaryCityRepository(client)
return RoutePlanner(
    flights=provider,
    topology=topology,
    cities=cities,
    geography=geography,
    config=settings.planner_config,
)
```

Реализуйте async get_many и возвращайте CityProfile. Нет оценки — пропустите
город в ответе; для длительных остановок планировщик использует 0. Для сбоя
источника предназначен CitySourceError; ошибки программирования не маскируются
под нулевую оценку. Собственные клиенты регистрируйте в AsyncExitStack для закрытия.

CityProfile версии 2 содержит tourism_score в диапазоне 0–100. Географические поля,
старые сведения о визах, рекомендуемая длительность визита и цена отеля сохранены
для совместимости каталога; длительность визита и отель не влияют на MVP.
При чтении SQLite-профиля без profile_version старый score 0–1 умножается на 100,
файл не перезаписывается. Новые записи имеют profile_version=2. Для временного
источника используйте новую шкалу явно.

SqliteCatalog создаёт/наполняет каталог, но не используется планировщиком для
чтения. Репозитории открывают существующий файл read-only, каждый имеет свой
engine. Схема SQLite не менялась. SQLAlchemy строит все запросы без SQL-строк.

В исходном городе нужна география для точной проверки текущего местного дня.
API подключает её отдельно от туристического источника; сбой скрипта
привлекательности не мешает этой проверке. В штатном runtime каталог теперь
нужен и API, а не только worker. Старый демонстрационный каталог следует
явно пересоздать: новый демо включает MOW и TYO помимо ALA.

Замена стандартного визового адаптера:

```python
planner = RoutePlanner(
    flights=provider,
    topology=topology,
    cities=cities,
    geography=geography,
    visa_costs=MyVisaCostResolver(),
    config=config,
)
```

expenses — чистый синхронный расчёт по уже загруженным сведениям; блокирующих
сетевых вызовов внутри него быть не должно. Если будущему источнику виз нужна
сеть, загрузку следует добавить отдельным async-этапом до поиска кандидатов,
чтобы не делать одинаковые запросы для каждого маршрута.
