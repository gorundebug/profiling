# Result callback и общие примитивы Boost: результат проверки

2026-09-09. Публичные имена и сигнатуры сохранены. Пользователь отдельно разрешил переход HTTP callback на один зарегистрированный объект; это изменение поведения mutable captures, а не совместимая реализация прежнего copy-per-call контракта.

## Оставленные изменения

- C++ userver и Boost HTTP source сохраняют callback через shared_ptr и вызывают его без копирования std::function на каждый результат. Один объект остаётся живым до завершения использующих его вызовов, даже если регистрация удаляется. Защита от late/duplicate результатов и shutdown сохранена.
- setResultCallback не переименован и не получил дополнительных параметров. mutable captures теперь сохраняются между вызовами. Обработчик обязан синхронизировать изменяемое состояние при параллельных результатах; универсальная сериализация callback не добавлялась.
- Проверены два HTTP-обработчика OrderService в cppexample/cppboostexample: captures содержат resultContext и shared_ptr, изменяемое состояние заказа уже защищено mutex обработчика. Их вызывающий код менять не понадобилось. Первоначально изменение касалось только HTTP; последующим дополнением ниже охвачены local source, Kafka и gRPC.
- Boost SingleUseEvent: вместо стандартного mutex — атомарная публикация одноразового сигнала и списка ожидающих. Send / IsReady / регистрация AsyncWait не используют собственный std::mutex. Это не утверждение об отсутствии синхронизации внутри Boost.Asio и аллокатора.
- Wait / WaitUntil сохраняют синхронный контракт. Promise/future создаётся только для синхронного ожидающего. Отмена одного AsyncWait не устанавливает ready и не пробуждает остальные запросы. Отменённые регистрации содержат слабые ссылки; сами узлы освобождаются при Send или уничтожении события.
- Стандартный mutex оставшегося TaskStorage в том же sync.hpp не относится к SingleUseEvent и не менялся.

## Измерение SingleUseEvent

Локальный Docker ARM64/GCC13, -O3, три повтора; это микротест, не сервисный throughput. В цикле с настоящей регистрацией Asio waiter и последующим Send: до 304.7–306.5 нс/цикл, после 292.1–297.0 нс/цикл. Fast path Send+IsReady также дешевле, но его цифры сильно зависят от оптимизации короткого локального цикла; не переносить их на производительность сервиса.

Исходники и логи: runtime-hot-path-checks/event_bench.cpp, baseline_sync.hpp, event-bench.log.

## RotatingMap: быстрый кандидат отклонён

Исходная ротация не имеет TTL: она освобождает лишнюю ёмкость, сохраняя все записи. Был проверен вариант на boost::unordered::concurrent_flat_map из Boost 1.83, с lazy conversion для однократного getOrCreate и атомарным erase_if для pop. Внешние сигнатуры оставались прежними.

По [документации Boost 1.83](https://www.boost.org/doc/libs/1_83_0/libs/unordered/doc/html/unordered.html), concurrent_flat_map сам использует внутреннюю синхронизацию и блокирует операции при rehash; это не lock-free map.

Микротест 256 активных ключей на поток, set/get/pop, три повтора: исходная версия 31.2–32.1 нс/операцию на одном потоке и 59.2–64.6 на четырёх; кандидат 17.1–17.4 и 42.1–45.4 соответственно. Несмотря на это, ASan/UBSan stress-test с move-only значениями и ротацией каждые 1 мс зависал: полный прогон остановлен по timeout 120 секунд, изолированный — по timeout 20 секунд. Исходная RotatingMap проходит тот же тест (около 1.8 секунды под sanitizer). Первопричина зависания не локализована; приписывать её конкретно Boost без дополнительной диагностики нельзя.

Кандидат из библиотеки убран. Стандартные mutex в RotatingMap остались. Замена на spinlock или отключение shrink ради прохождения теста не выполнялись. Отклонённый эксперимент сохранён в runtime-hot-path-checks/rejected_concurrent_rotatingmap.hpp и не включается библиотекой.

## Проверки

- userver: 12 HTTP-тестов + 2 теста настоящего OrderService с сгенерированными OpenAPI types.
- Boost: 20 HTTP-тестов, 15 store/event-тестов, 7 local/custom-тестов, 2 теста OrderService под ASan/UBSan; ещё 3 gRPC endpoint-теста всех четырёх видов вызова также под ASan/UBSan — всего 47 тестов Boost.
- Добавлены: сохранение mutable callback state между результатами, точный порядок trace events в Boost, гонки регистрации с Send (200 итераций по 4 потока), несколько async waiters и независимая отмена, однократная фабрика при конкуренции, отказ фабрики без вставки и move-only map values при ротации.
- Чужой код и spans не редактировались. Сервисного профилирования после изменения не было.

Для повторения микротестов из runtime-hot-path-checks в cppboostservicelib-build:local:

```sh
g++ -O3 -DNDEBUG -std=c++20 -pthread -I/source/cppboostservicelib/include event_bench.cpp -o /tmp/event_bench -lboost_json -lcrypto -lyaml-cpp
/tmp/event_bench
g++ -O3 -DNDEBUG -std=c++20 -pthread -I/source/cppboostservicelib/include map_bench.cpp -o /tmp/map_bench -lboost_json -lcrypto -lyaml-cpp
/tmp/map_bench
```

## Дополнительная проверка всех мест использования (2026-09-09)

`setResultCallback`: в C++ обнаружены три реализации на библиотеку — HTTP, local source, общий gRPC source. В local source и gRPC устранена копия std::function при получении результата: берётся shared_ptr на зарегистрированный callable. Имена методов и сигнатуры не изменены. Пустой callback по-прежнему обрабатывается как неизвестный message ID. Удаление callback, защита от позднего результата и точки событий трассировки остались прежними. Это не удаление оставшихся mutex из local/gRPC.

Kafka и local source — разные публичные источники. Однако уже в HEAD обеих C++ библиотек Kafka использует localsource::Endpoint целиком через HandlerAdapter, включая ResultContext и lifecycle. В Go, Rust, Python и TypeScript у Kafka отдельные реализации. Текущая оптимизация эту архитектуру не меняла.

Новые проверки: mutable capture считает два последовательных результата как 1, 2 в local source и gRPC. Boost: 7 local/custom и 4 gRPC теста прошли под ASan/UBSan. Userver: 16 local/Kafka и 4 gRPC теста прошли в Debug. Это проверки дополнения; результаты предыдущего HTTP/SingleUseEvent прогона приведены выше.

### Полный охват RotatingMap в Boost

| Пользователь | Значение | Операции |
|---|---|---|
| HTTP source | shared_ptr<Result> | set/get/pop |
| local source, также C++ Kafka через адаптер | shared_ptr<Result> | set/get/pop |
| общий gRPC source | shared_ptr<Request> | set/get/pop |
| client-streaming gRPC sink | shared_ptr<SessionCell> | getOrCreate/get/pop |
| bidirectional-streaming gRPC sink | shared_ptr<SessionCell> | getOrCreate/get/pop |

Все пять также используют start/stop. В streaming sink фабрика создаёт только SessionCell; beginRequest, tracing и запуск RPC выполняет победитель вставки (`loaded == false`) после неё. Публикацию единственного победителя и ожидание готовности ячейки необходимо сохранить независимо от устройства map. Сама SessionCell также содержит mutex/condition_variable: замена одной map не сделает весь streaming путь неблокирующим.

Общую RotatingMap нельзя подменять copy-on-write контейнером с повторным вызовом фабрики: публичный getOrCreate допускает произвольную фабрику, и проверка требует её однократного вызова. Atomic shared_ptr сам по себе также не доказывает отсутствие внутренних блокировок. Удаление узла должно гарантировать безопасное освобождение памяти при конкурентных get/pop и ротации. Проверенный ранее кандидат не прошёл stress-test; новая безопасная неблокирующая замена в библиотеку не внесена. Это остаётся нерешённой частью оптимизации, а не утверждением, что такая реализация невозможна.

## Kafka отделена от local source

По последующему запросу пользователя обе C++ Kafka реализации теперь используют собственный `datasource/kafka/detail/endpoint.hpp`. Он владеет lifecycle, concurrency admission и отдельной pending RotatingMap; зависимости от `localsource::Endpoint` и `DataProducer` нет. Неиспользуемый для Kafka режим запуска отдельной задачи на каждое входящее сообщение в новую реализацию не перенесён.

Общие PendingResult/ResultContext вынесены в `datasource/detail/result_context.hpp`. Старые имена и идентичность классов в namespace localsource сохранены для исходной совместимости явно типизированных обработчиков, а kafka::ResultContext доступен как alias. Это общие данные/примитив callback, а не связь жизненных циклов двух источников. Имеющийся тест явно принимает прежний localsource::ResultContext при работе через Kafka Endpoint::make.

Порядок соответствует текущему Go sarama.go: ConsumeClaim непосредственно вызывает EndpointRequest для каждого сообщения. При наличии result stream EndpointRequest ждёт Done/отмену перед следующим сообщением той же partition. Работа разных partitions остаётся параллельной; задачи бизнес-графа этим не сериализуются. Транспортное чтение, commit и rebalance в этом отделении не переписывались; утверждать полное совпадение всех деталей транспорта с Go нельзя.

Добавлена отдельная Kafka regression-проверка: retained callback получает два результата, commit выполняется при втором, поздний результат не вызывает callback после завершения. Проверяется точный порядок событий kafka.input: begin_request, result_consumed, done_called, result_consumed, consume_message, done_received.

RotatingMap теперь имеет отдельный шестой владеющий endpoint в Kafka (раньше это было косвенное использование через local source). В ней по-прежнему остаётся стандартный mutex; отделение Kafka не выдаётся за решение вопроса неблокирующей map.

Финальная проверка отделения: 19 Kafka-тестов Boost под ASan/UBSan и 17 local/Kafka-тестов userver прошли, включая новый тест точного порядка trace events. После отделения также прошли 7 local/custom-тестов Boost под ASan/UBSan. Логи сохранены в runtime-hot-path-checks/separate-kafka-{boost-asan,userver}.log.

## Streaming gRPC Boost: первые три этапа

ClientStreamingEndpoint и BidirectionalStreamingEndpoint больше не используют собственные std::mutex/std::condition_variable для ожидания SessionCell, mutex списка сессий или std::shared_mutex для lifetime.

- SessionCell публикует результат создания через SingleUseEvent. Конкурентное сообщение возвращает управление и ожидает готовности в отдельной Asio coroutine. Отмена контекста прекращает ожидание; обработчики не исполняются на общем strand. Готовые сессии сохраняют прямой путь вызова. Для legacy синхронного RPC продолжение после ожидания отправляется в BlockingExecutorRegistry.
- Регистрация публикует слабую ссылку на ячейку до beginRequest/start RPC. Atomic exchange при stop отделяет список; публикация, столкнувшаяся с закрытым списком, сама помечает ячейку для отмены. Если RPC ещё не создан, cancel выполняется после публикации готовности. Узлы слабых регистраций освобождаются при stop, как прежний vector; это не оптимизация удержания памяти между остановками.
- StreamingActivity объединяет флаг закрытия и число активных операций в одном atomic. Закрытие запрещает новые операции, а завершение асинхронно ждёт принятые handler/response операции и инициализацию. Только затем удаляется pending-запись, вызывается endRequest и закрывается span. Sender/done, вызванные из уже принятого handler, могут завершиться даже после закрытия входа; сохранённый callback после окончания допуска не пишет в закрытый span.
- Endpoint admission охватывает создание сессий и отложенные сообщения, поэтому stop не пропускает RPC, который создаётся одновременно с остановкой. Синхронный stop остаётся блокирующим по своему контракту. Блокирующие Finish/Read старого синхронного клиента продолжают выполняться через существующие задачи для синхронных операций.

Публичные методы endpoint, Sender и ResultContext не переименованы. Универсальная RotatingMap не изменялась. Общие примитивы других транспортов (включая их AsyncOperations) не переделывались; полностью неблокирующим весь стек библиотек/аллокатора не объявляется.

Проверки: 18 gRPC endpoint/lifecycle тестов прошли под ASan/UBSan. Среди них 14 lifecycle сценариев: конкурентный создатель и ожидающие сообщения, независимое продолжение сообщений, отмена ожидающего, stop во время создания, завершение из start/done, удержание активного response/handler до endRequest, повторное/позднее завершение, ошибка start и legacy клиенты. Точный порядок grpc.output событий проверен при немедленном completion. 12 основных lifecycle сценариев прошли ещё 10 повторов под ASan/UBSan. Логи: runtime-hot-path-checks/streaming-lifecycle-{asan,stress-asan}.log. ThreadSanitizer и сервисное профилирование в этом прогоне не выполнялись.
