# VM скрапер-ноды в Yandex Cloud

Корневой модуль Terraform для одной или нескольких независимых VM с
`ubas-scraper-node`. Он создаёт отдельные VPC, subnet, группу безопасности,
сервисный аккаунт и VM, устанавливает Docker через cloud-init, скачивает образ
backend из Yandex Container Registry и запускает контейнер ноды. Worker
подключается к этим нодам через `UBAS_AGENT_NODE_URLS`.

Это прототип. Модуль не связан с продуктовой VM API/worker и не изменяет
`ubas-backend`, его сеть, диск или PostgreSQL.

## Одна команда

Что нужно заранее:

- Terraform >= 1.9;
- авторизованный `yc` CLI или `YC_TOKEN` с правами создавать compute, VPC, IAM
  и сервисные аккаунты в выбранном каталоге;
- образ backend уже опубликован в Yandex Container Registry workflow
  [deploy.yml](../../../.github/workflows/deploy.yml).

Если `TF_CLI_CONFIG_FILE` не задан, `apply.sh` использует `terraformrc.yandex` и
зеркало `registry.terraform.io` в Yandex Cloud. Для другого зеркала провайдеров
задайте `TF_CLI_CONFIG_FILE` явно.

Из корня репозитория:

```shell
./deployment/terraform/scraper-node/apply.sh
```

Скрипт по возможности получает `YC_TOKEN` и ID каталога из `yc`, выполняет
`terraform init`/`apply`, затем до 15 минут ждёт `/healthz` каждой ноды. В конце
он печатает готовый фрагмент окружения для worker.

Если `yc` не установлен, получите IAM-токен отдельно и экспортируйте его вместе
с каталогом:

```shell
export YC_TOKEN="<iam-token>"
export TF_VAR_folder_id="<folder-id>"
./deployment/terraform/scraper-node/apply.sh
```

`-auto-approve` указан намеренно: модуль создаёт только новые ресурсы. Перед
первым запуском в production-подобном окружении просмотрите plan. Отключить
ожидание после apply можно через `WAIT_FOR_NODE=0`.

## Настройка

Скопируйте `terraform.tfvars.example` в `terraform.tfvars` и отредактируйте его
или используйте переменные `TF_VAR_*`. `terraform.tfvars` исключён из Git.

| Переменная | Значение по умолчанию | Смысл |
| --- | --- | --- |
| `folder_id` | обязательна | Каталог для VM, сети и сервисного аккаунта |
| `cloud_id` | `null` | Берётся из `YC_CLOUD_ID`, если не задан |
| `zone` | `ru-central1-a` | Зона VM и subnet |
| `name` | `ubas-scraper-node` | Префикс имён ресурсов |
| `node_count` | `1` | Количество VM-нод |
| `image` | `.../ubas-backend:master` | Образ с консольной командой ноды |
| `node_port` | `8101` | HTTP-порт ноды |
| `node_concurrency` | `2` | Браузерные слоты на ноду |
| `node_allowed_cidrs` | `["0.0.0.0/0"]` | Кто может вызывать API ноды |
| `ssh_allowed_cidrs` | `[]` | Кто может подключаться по SSH |
| `ssh_public_key` | `""` | Необязательный ключ для пользователя `ubuntu` |
| `cores` / `memory_gb` | `2` / `4` | Ресурсы под Chromium |
| `preemptible` | `false` | Дешевле, но поиск может быть прерван |

Две ноды одной командой:

```shell
TF_VAR_node_count=2 ./deployment/terraform/scraper-node/apply.sh
```

или в `terraform.tfvars`:

```hcl
folder_id  = "<folder-id>"
node_count = 2
```

## Выходные значения

- `node_urls` — список для `UBAS_AGENT_NODE_URLS`;
- `healthcheck_urls` — адреса `/healthz`;
- `node_public_ips` — публичные адреса VM;
- `node_internal_ips` / `node_internal_urls` — приватные адреса внутри VPC;
- `node_token` — токен (sensitive) для заголовка `X-UBAS-Node-Token`;
- `worker_env` — закрытый фрагмент окружения worker.

Добавьте фрагмент в окружение worker. `UBAS_AGENT_NODE_CONCURRENCY` должен
совпадать со значением модуля. В `node_urls` используются публичные IP; worker
внутри той же VPC должен брать адреса из `node_internal_urls`.

## Безопасность и жизненный цикл

- По умолчанию `node_allowed_cidrs` открыт в интернет. Сгенерированный токен
  защищает точку входа, но CIDR лучше сузить до сети worker. Если вы это
  сделаете, добавьте туда и машину, с которой запускается `apply.sh`, иначе
  проверка `/healthz` после apply не сможет достучаться до ноды. В этом случае
  можно использовать `WAIT_FOR_NODE=0`.
- Токен хранится в Terraform state и в metadata/user-data VM. Для общего state
  используйте зашифрованный remote backend и не коммитьте файлы состояния.
- Сервисный аккаунт VM получает только `container-registry.images.puller`;
  постоянный IAM-токен на диск не записывается.
- Cloud-init выполняется один раз. Изменение `image` не перезапускает его
  автоматически. Чтобы обновить существующую ноду, замените VM:

  ```shell
  terraform apply -replace='yandex_compute_instance.node[0]'
  ```

- Удалить всё созданное модулем:

  ```shell
  ./deployment/terraform/scraper-node/destroy.sh
  ```

  или `terraform destroy` с теми же авторизацией и переменными.

- Модуль не настраивает TLS или mTLS. За пределами прототипа держите ноду в
  приватной сети или за группой безопасности с ограниченным доступом.

## Диагностика

Статус cloud-init и журналы ноды:

```shell
ssh ubuntu@<public-ip> sudo cloud-init status --long
ssh ubuntu@<public-ip> sudo journalctl -u cloud-init --no-pager
ssh ubuntu@<public-ip> sudo docker logs ubas-scraper-node-01
```

Проверка с машины, где запускался Terraform:

```shell
curl -H "X-UBAS-Node-Token: <token>" http://<public-ip>:8101/healthz
```

Если `apply.sh` сообщает, что нода не готова, проверьте, что образ скачался и VM
может достучаться до `agent.ru` и Yandex Container Registry.
