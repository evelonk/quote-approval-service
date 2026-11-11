# S09 — M1: SAST и поиск секретов

## Версии и область

- Проверен commit `e41e9b6774ba4adb66964460f56be155d664abcb`: исходный
  и повторный запуск выполнены на одной и той же версии кода.
- Запуск выполнен в отдельных чистых checkout: `git status --porcelain=v1`
  пуст, история не shallow.
- Semgrep CE **1.185.0**: все отслеживаемые Git Python-файлы переданы явными
  аргументами, включая тесты и скрипты: **13 файлов в каждом запуске**.
  Списки совпали с `paths.scanned`, ошибок разбора и пропущенных правил нет.
- Правила Semgrep: замороженный профиль `p/python`, **151 правило для Python**,
  [python-rules.yml](config/python-rules.yml). Метрики, проверка новой версии и
  онлайн-проверка секретов выключены; анализ выполнен локальным CE.
- Gitleaks **8.30.1**: стандартные встроенные правила через
  [gitleaks.toml](config/gitleaks.toml), без учебного `rbpo-training-token`.
  Проверены файлы e41e9b6 и его доступная история: **17 коммитов**, включая
  сам e41e9b6 и его предков.
- В Gitleaks включено полное маскирование `--redact=100`, комментарии
  `gitleaks:allow` игнорируются. Дополнительных исключений и baseline нет;
  штатные исключения встроенных правил сохранены.

Версии, источники правил и их SHA-256: [tools.json](tools.json).
Проверенный commit, дерево и границы истории: [sources.json](sources.json).
Все фактические команды, рабочие папки, переменные среды, время и exit codes:
[scan-runs.json](scan-runs.json).

## Первичные результаты и повторная проверка

| Проверка | e41e9b6 — исходный запуск | e41e9b6 — повтор после разбора |
| --- | --- | --- |
| Semgrep | 2 сигнала, exit 1 — [JSON](initial/semgrep.json), [лог](initial/semgrep.log) | 2 сигнала, exit 1 — [JSON](repeat/semgrep.json), [лог](repeat/semgrep.log) |
| Gitleaks, файлы | 0 сигналов, exit 0 — [JSON](initial/gitleaks-dir.json), [лог](initial/gitleaks-dir.log) | 0 сигналов, exit 0 — [JSON](repeat/gitleaks-dir.json), [лог](repeat/gitleaks-dir.log) |
| Gitleaks, история | 0 сигналов / 17 коммитов, exit 0 — [JSON](initial/gitleaks-git.json), [лог](initial/gitleaks-git.log) | 0 сигналов / 17 коммитов, exit 0 — [JSON](repeat/gitleaks-git.json), [лог](repeat/gitleaks-git.log) |


**Решения по всем сигналам:** [реестр findings.md](findings.md).
Два SQL-сигнала объединены в одну причину F-01 и признаны ложными сигналами
SQL-инъекции в проверенной реализации. Повторный запуск дал те же два сигнала.

Подтверждённых дефектов в этих сигналах нет. Код приложения, тесты, CI и
зависимости M1 не меняла; подавления сигналов не добавлялись. После разбора
Semgrep и оба режима Gitleaks повторены на e41e9b6 теми же версиями и правилами.
Сохранившиеся сигналы соответствуют решениям реестра.

## Воспроизведение

Нужны Git, Semgrep 1.185.0 и Gitleaks 8.30.1. Инструменты для этой проверки
установлены отдельно от окружения приложения.

Из корня репозитория подготовьте отдельную копию указанного коммита:

```bash
S09_REPO="$(git rev-parse --show-toplevel)"
S09_COMMIT=e41e9b6774ba4adb66964460f56be155d664abcb
S09_TMP="$(mktemp -d)"
git clone --no-hardlinks --no-checkout "$S09_REPO" "$S09_TMP/source"
git -C "$S09_TMP/source" checkout --detach "$S09_COMMIT"
cd "$S09_TMP/source"
test "$(git rev-parse HEAD)" = "$S09_COMMIT" || exit 1
git status --porcelain=v1
git rev-parse HEAD
semgrep --version
gitleaks version
```

Пути исполняемых файлов можно указать вместо `semgrep` и `gitleaks`.
Фактически использованные пути и окружение сохранены в `scan-runs.json`.


```bash
git ls-files -z -- '*.py' | xargs -0 env -u SEMGREP_APP_TOKEN semgrep scan \
  --oss-only --config "$S09_REPO/docs/evidence/s09/config/python-rules.yml" \
  --metrics off --disable-version-check --no-secrets-validation \
  --no-rewrite-rule-ids --strict --error --json --output "$S09_TMP/semgrep.json"
# Ожидается exit 1: сохранённые в реестре сигналы остаются.

gitleaks dir . --config "$S09_REPO/docs/evidence/s09/config/gitleaks.toml" \
  --redact=100 --ignore-gitleaks-allow --no-banner --no-color \
  --report-format json --report-path "$S09_TMP/gitleaks-dir.json"

gitleaks git . --log-opts="$S09_COMMIT" \
  --config "$S09_REPO/docs/evidence/s09/config/gitleaks.toml" \
  --redact=100 --ignore-gitleaks-allow --no-banner --no-color \
  --report-format json --report-path "$S09_TMP/gitleaks-git.json"
```



## Ограничения вывода

Результаты относятся только к e41e9b6 и выбранным правилам. Проверка не
доказывает отсутствие всех возможных уязвимостей. Semgrep CE не доказывает
правильность бизнес-переходов и контроля доступа; Gitleaks может не распознать
произвольный пароль или неизвестный формат секрета.
Новые коммиты требуют отдельного запуска.
