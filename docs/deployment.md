# Production deployment в Yandex Cloud

Backend разворачивается на одной минимальной VM из приватного Yandex Cloud
Registry. Workflow [deploy.yml](../.github/workflows/deploy.yml) запускается после
push в `master` или вручную на `master`, собирает образ с неизменяемым тегом полного
Git SHA, публикует его и обновляет API и worker через Docker Compose.

## Ресурсы

- folder: `prod` (`b1g74ku9ruilbkb9ie56`);
- registry: `ubas` (`cn12qrbnsnck5l6a3en0`);
- lifecycle policy: `ubas-keep-latest-five` (`cn16d5s5dj12dnvgqtpe`),
  выключена до подтверждения политики хранения;
- VM: `ubas-backend` (`fv4k2j5mqppooderha86`), `153.51.167.77`;
- CI service account: `ubas-ci` (`aje5uv722diotvqdgqb1`), только push образов;
- VM service account: `ubas-vm` (`ajevumq38a3sgbu6rf8l`), только pull образов.

CI получает IAM-токен через GitHub OIDC и workload identity federation
`ubas-github` (`ajenncsq1buh0g2fghsf`). Постоянный ключ сервисного аккаунта не
создаётся. Федерация допускает только subject production environment. Организация
GitHub добавляет в него стабильные числовые ID владельца и репозитория, поэтому
фактическая привязка равна
`repo:UBAS-KurganDev@334516411/backend@1390760795:environment:production`.
Job дополнительно проверяет, что ref равен `refs/heads/master`. GitHub secrets для
deployment не нужны.

VM раз в минуту проверяет тег `master` системным timer. Pull выполняется от имени
прикреплённого `ubas-vm` через metadata service. После pull используется digest,
поэтому во время запуска mutable-тег уже не участвует.
Unit-файлы и конфигурация Nginx находятся в [deployment](../deployment).

## Состояние VM

Постоянные файлы находятся в `/opt/ubas`:

- `.env` — runtime-настройки и пароль PostgreSQL; workflow его не перезаписывает;
- `.deploy.env` — только `UBAS_IMAGE` текущего релиза;
- `current-release` — SHA успешно запущенного релиза, доступный как `/__version`;
- `var/topology-catalog.sqlite3` и topology snapshot — данные планировщика;
- `config/planner.json` — версия из `master`;
- volume `ubas-backend_postgres_data` — очередь и результаты.

Каталог и PostgreSQL volume не входят в образ и не удаляются при деплое. Compose
публикует API только на `127.0.0.1:8000`; внешний порт 80 обслуживает Nginx.

## Порядок обновления

1. GitHub обменивает OIDC JWT на короткоживущий IAM-токен `ubas-ci`.
2. Собираются и публикуются immutable-тег `<git-sha>` и отслеживаемый тег `master`.
3. VM получает собственный короткоживущий IAM-токен через metadata service и
   скачивает `master`.
4. Из скачанного образа извлекаются manifest, planner config и deployment scripts;
   для Compose фиксируется immutable digest.
5. Старые API и worker останавливаются, миграции выполняются entrypoint API,
   Compose ждёт healthcheck, затем проверяется `/openapi.json` через Nginx.
6. После успеха обновляется `/__version`, а GitHub job сверяет его с ожидаемым SHA.
7. При ошибке manifest и предыдущий image digest восстанавливаются. Если новая
   миграция БД несовместима со старым кодом, автоматический rollback приложения
   может быть невозможен; такие изменения требуют отдельного плана миграции.

Одновременные деплои запрещены GitHub concurrency и двумя `flock` на VM. Образы, которые
не используются больше семи дней, очищаются с локального диска VM. Для Registry
подготовлена выключенная lifecycle policy, которая хранит последние пять версий.
Перед включением выполнен dry run без кандидатов на удаление. Включение
`HARD_DELETE` требует отдельного решения о глубине rollback-истории.

Environment `production` используется для формирования ограниченного OIDC subject,
но secrets и variables в нём не нужны. Облачные идентификаторы и публичный адрес
не являются секретами и закреплены в workflow. При замене VM нужно обновить
`VM_HOST`, переустановить systemd timer и перенести постоянный каталог.
