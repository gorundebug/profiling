# Datasource / datasink: аудит производительности

Дата: 2026-09-09. Реализации: Go, C++ userver, C++ Boost, Rust, Python, TypeScript. Ниже сохранён исходный аудит; итог реализации и ограничения приведены в последнем разделе.

## Основание и границы

Проверен собственный код HTTP/gRPC/Kafka source и sink, дополнительно просмотрены local-коннекторы. Основная доказательная база — `.artifacts/run-manifest.json` и folded CPU-профили OrderService. Сценарий `function-call / process_order_out_of_stock`, 256 VU, 20 секунд, 2 CPU на сервис, logging/metrics/tracing отключены. Kafka отключена. Поэтому HTTP source и unary gRPC sink относятся к измеренному пути; HTTP sink, Kafka и local требуют своих нагрузочных сценариев. Это не allocation-профиль: наличие копии подтверждается кодом, её вклад в CPU/RSS ещё надо измерять.

Проценты ниже — доля веса стеков соответствующего профиля. Inclusive-значения пересекаются, их нельзя складывать или трактовать как ожидаемый прирост RPS. Профили сняты с ревизий из manifest; это не повторное измерение всех последних изменений рабочего дерева. Чужие библиотеки и их внутренние spans не предлагается менять.

## 1. TypeScript: копии HTTP и gRPC буферов

- [HTTP source JSON](/Users/sergeyalexeev/stream_app_go/tsservicelib/src/datasource/http/body.ts:95): `readRequestBody` уже собирает `Buffer.concat`, затем `Buffer.from(bytes)` полностью копирует его перед UTF-8 декодированием.
- [HTTP sink text](/Users/sergeyalexeev/stream_app_go/tsservicelib/src/datasink/http/node-http.ts:119): та же лишняя копия после `read()`.
- [gRPC sink](/Users/sergeyalexeev/stream_app_go/tsservicelib/src/datasink/grpc/grpc-js.ts:251) и [source](/Users/sergeyalexeev/stream_app_go/tsservicelib/src/datasource/grpc/grpc-js.ts:1247): `Buffer.from(serialize(...))` копирует готовый Uint8Array. Обёртка вызова сериализации принадлежит нам: можно адаптировать результат библиотеки к Buffer-view с правильными `byteOffset` и `byteLength`, если буфер не переиспользуется/не изменяется до окончания отправки.

Предложение: сохранить Buffer на HTTP-пути, отдельно обработать один chunk без concat; для freshly serialized protobuf использовать view. Кэшировать path и serializer/deserializer функции на уровне gRPC method, вместо создания строк и замыканий на каждый RPC.

Проверки: непустой byteOffset, Unicode, пустое тело, один/много chunks, лимит тела, отсутствие изменения данных при параллельной отправке. Копирование Kafka payload нельзя автоматически убрать тем же способом: его исходный массив может принадлежать вызывающему коду и изменяться после send.

## 2. Rust: HTTP Bytes превращаются в Vec и дополнительный Arc

- [Axum source](/Users/sergeyalexeev/stream_app_go/rustservicelib/src/datasource/http/axum.rs:660): `to_bytes(...)` → `to_vec()` → `Arc::new(body)`; исходный Bytes уже поддерживает разделяемое владение.
- [Reqwest sink](/Users/sergeyalexeev/stream_app_go/rustservicelib/src/datasink/http/reqwest.rs:136): `response.bytes().await?.to_vec()`.
- [HTTP response](/Users/sergeyalexeev/stream_app_go/rustservicelib/src/datasource/http/axum.rs:253): тело уже перемещается, но HeaderMap клонируется. Возможность перемещения headers зависит от допустимости их наблюдения через оставшиеся HandlerData.

Предложение: хранить `Bytes`, а `&[u8]` предоставлять без копирования; Vec выдавать только явно запрашивающему изменяемое владение коду. Это изменение публичных типов HandlerData/Response, поэтому нужно обновить примеры и проверить совместимость, а не просто заменить одну строку.

Дополнительно source материализует все HTTP headers в новый `HashMap<String,String>` для контекста. Можно исследовать разделяемый carrier или ленивое представление; нельзя просто выбросить пользовательские metadata.

## 3. Python: стоимость идентификаторов на каждом сетевом запросе

[Генератор](/Users/sergeyalexeev/stream_app_go/pyservicelib/src/pyservicelib_gorundebug/runtime/context/request.py:46) вызывает `str(uuid.uuid4())`. Он используется [HTTP source](/Users/sergeyalexeev/stream_app_go/pyservicelib/src/pyservicelib_gorundebug/datasource/http/aiohttpds.py:334) и [unary gRPC sink](/Users/sergeyalexeev/stream_app_go/pyservicelib/src/pyservicelib_gorundebug/datasink/grpc/grpcds.py:484).

В Python OrderService `uuid4` занимает 3.47% self, 4.24% inclusive веса стеков. Это конкретный кандидат для замера, но не обещание такого же прироста throughput.

Предложение: генератор correlation ID с уникальным случайным префиксом процесса и счётчиком, либо другой более дешёвый генератор с сопоставимыми гарантиями. Свежий ID на каждый дочерний RPC сохранить: повторное использование родительского ID ломает корреляцию fan-out. Перед заменой проверить формат, уникальность между процессами/после fork и отсутствие требований к непредсказуемости. В Go используется xid, UUID-формат не является общим межъязыковым контрактом.

## 4. Go: fast path при выключенных метриках

[DataSourceEndpoint.OnRequestStart/End](/Users/sergeyalexeev/stream_app_go/servicelib/runtime/datasource.go:311) и [DataSinkEndpoint.OnRequestStart/End](/Users/sergeyalexeev/stream_app_go/servicelib/runtime/datasink.go:200) вызывают `time.Now`, `time.Since` и методы инструментов даже с noop metrics.

Предложение: определить состояние метрик при создании endpoint и не вычислять timestamps/duration на выключенном пути. Не затрагивать таймеры deadline, отмену и correlation state. В Python и обеих C++ реализациях подобная проверка уже есть — повторно рекомендовать её там не нужно.

В Go CPU-профиле `__kernel_clock_gettime` имеет 6.39% self, но этот символ включает часы всего процесса, а не только метрики. Экономию именно endpoint metrics нужно измерить отдельно.

## 5. Boost HTTP sink: опрос соединений и блокирующий shutdown

[Client::Acquire](/Users/sergeyalexeev/stream_app_go/cppboostservicelib/include/servicelib/datasink/http/client.hpp:354) ждёт свободный connection через таймер с [интервалом 1 мс](/Users/sergeyalexeev/stream_app_go/cppboostservicelib/include/servicelib/datasink/http/client.hpp:66). Каждый ожидающий просыпается, берёт mutex и проверяет pool. `ReleaseConnection` не передаёт соединение непосредственно ожидающему. Дополнительно idle connection выбирается без привязки к origin, после чего при другом host/port закрывается и переподключается.

[Client::Stop](/Users/sergeyalexeev/stream_app_go/cppboostservicelib/include/servicelib/datasink/http/client.hpp:183) блокирует поток на condition_variable до окончания активных операций. На executor, который должен завершить эти операции, это создаёт риск остановки прогресса.

Предложение: асинхронная очередь ожидания с передачей освободившегося соединения; отдельно учитывать origin; deadline/cancellation удаляют ожидающую запись. Асинхронный drain для shutdown. Сериализовать только изменение состояния connection pool, сами запросы продолжать независимо. Соединения уже переиспользуются — добавление keep-alive с нуля не требуется.

Статус: подтверждено кодом, текущий HTTP→gRPC профиль этот sink не измеряет.

## 6. C++ HTTP source: correlation callback и защита его жизни

[userver consumeResult](/Users/sergeyalexeev/stream_app_go/cppservicelib/include/servicelib/datasource/http/userver.hpp:416) и [Boost consumeResult](/Users/sergeyalexeev/stream_app_go/cppboostservicelib/include/servicelib/datasource/http/beast.hpp:770) дважды ищут pending result, материализуют stream ID и копируют `std::function` из callback map. Для callback с большими captures копия может дополнительно выделять память и увеличивать счётчики shared_ptr.

Предложение: держать callback в стабильной записи с разделяемым владением, чтобы получить безопасный handle без копирования всего замыкания; по возможности использовать lookup по string_view. Двойной lookup сейчас защищает от удаления pending request во время получения lifetime lock — без альтернативной схемы retire/in-flight учёта его убирать нельзя.

В Boost дополнительно используются [std::shared_mutex / std::mutex](/Users/sergeyalexeev/stream_app_go/cppboostservicelib/include/servicelib/datasource/http/beast.hpp:484) и общий activeMutex. В userver lifetimeMutex уже `userver::engine::SharedMutex`, это не тот же случай. Для Boost следует проектировать неблокирующее async ожидание retirement, сохранив параллельность callbacks. Нельзя решить это помещением всего бизнес-графа на один strand.

Проверки: несколько результатов одновременно, callback возвращает false и остаётся зарегистрированным, duplicate/late result, done одновременно с отменой, EndRequest после завершения активных callback, отсутствие циклов владения.

## 7. Python Kafka source: лимит concurrency не ограничивает число ожидающих tasks

[_process_record](/Users/sergeyalexeev/stream_app_go/pyservicelib/src/pyservicelib_gorundebug/datasource/kafka/aiokafkads.py:405) создаёт `asyncio.Task` для каждого record. Уже внутри task берётся partition lock и затем разрешение concurrency. `_consume_loop` продолжает вычитывать записи, потому что `_process_record` лишь планирует обработку.

Следствие: лимит активных handler соблюдается, но очередь ожидающих coroutine и удерживаемых payload может вырасти. Кроме того, `setdefault(..., asyncio.Lock())` создаёт новый Lock даже для уже известной partition.

Предложение: очереди записей по partition, admission перед созданием callback task, ограничение read-ahead/pause-resume. Независимые partition продолжают работать параллельно, а порядок внутри partition и commit semantics сохраняются. `getmany` стоит измерить для уменьшения Python overhead чтения, но сам по себе он не исправит накопление tasks.

Статус: по коду; нужен Kafka-тест с медленной обработкой, контролем количества tasks/RSS, rebalance и остановки.

## 8. Rust / TypeScript Kafka sink: копии payload и слишком мелкие вызовы API

- [Rust SinkMessage::send/send_sync](/Users/sergeyalexeev/stream_app_go/rustservicelib/src/datasink/kafka/rdkafka.rs:498) клонируют key/value Vec. Возможны `Bytes` или дополнительный consuming API `send_owned(self, ...)`, оставляя повторную отправку через старый API.
- [TypeScript ConfluentProducer.send](/Users/sergeyalexeev/stream_app_go/tsservicelib/src/datasink/kafka/confluent.ts:743) копирует key/value и создаёт `messages: [message]` на каждую запись. Измерить небольшие batches по topic/partition с коротким окном и индивидуальным delivery completion. Kafka-клиент уже может батчить на сетевом уровне: это кандидат для уменьшения overhead нашего API, а не утверждение об отсутствии batching в librdkafka.
- В C++ userver source есть копирование сообщений при передаче между partition tasks. Оно обеспечивает владение после выхода из callback транспорта; удалять его без подтверждённого времени жизни буфера нельзя.

Для batching сохранить отмену, порядок, delivery callback, partition/offset, flush на stop; окно batching не должно ухудшить требуемую latency.

## 9. TS штатная отмена HTTP: отдельный кандидат после буферов

[requestCancellation.complete](/Users/sergeyalexeev/stream_app_go/tsservicelib/src/datasource/http/node-http.ts:724) создаёт Error и запускает abort-цепочку на каждом завершённом запросе. В профиле весь путь complete занимает 5.27% inclusive, собственное тело — 2.78% self.

Отмену сохраняем. Можно измерить повторно используемую внутреннюю причину штатного завершения и общий механизм подписок на signal. Это не означает, что вся стоимость complete исчезнет: dispatch отмены и cleanup нужны.

Отдельно проверены стеки `NodeError` (~4.80% inclusive): самые тяжёлые идут из grpc-js `destroyHttp2Stream → Writable.end`, а не из нашей строки `new Error("HTTP request completed")`. Приписывать эти 4.80% нашему Error и обещать убрать их было бы неверно; чужой код не меняем.

## Что уже сделано разумно

HTTP client/session и gRPC connections не создаются с нуля для каждого запроса в проверенных основных реализациях. В TS HTTP есть keep-alive agent, в Python — общая ClientSession, в Rust — общий reqwest Client, в Boost — connection pool. В C++ source тела HTTP в основном перемещаются, а не копируются целиком. В Python gRPC metadata есть короткий путь без tracing. Безусловного удаления spans или последовательного исполнения независимых запросов в предложениях нет.

Local/cron/Temporal не представлены данным нагрузочным сценарием. При просмотре local-коннекторов нового столь же убедительного транспортного bottleneck не найдено; это не доказательство отсутствия накладных расходов. Для Temporal нельзя переносить batching/очереди без проверки replay и durable semantics.

## Порядок реализации и проверки

1. Малые изменения: TS лишние копии HTTP/gRPC, Go noop metrics fast path. Сначала корректность буферов/метрик, затем тот же HTTP→gRPC профиль.
2. Rust Bytes и Python ID-generator: совместимость API/ID, обновление примеров, CPU + allocation/RSS профили.
3. Boost connection admission/shutdown и correlation callbacks: конкурентные тесты и отдельная HTTP-sink нагрузка. Ни одного user callback под глобальным strand/блокирующим mutex.
4. Kafka: сценарии нескольких partition с медленным downstream; task count, RSS, lag, latency, throughput, offset/commit/rebalance и drain.

Измерять одинаковые payload, concurrency, CPU quota, tracing/metrics mode; отдельно с включёнными и выключенными инструментами. Профили разных языков собраны разными инструментами, прямое сравнение их raw weights некорректно.


## Реализовано с сохранением внешнего API (2026-09-09)

- TypeScript: удалены повторные копии HTTP-body и результатов gRPC-сериализации; Buffer views учитывают byteOffset/byteLength. Кодеки и method path кэшируются на descriptor через WeakMap. Single-chunk HTTP не проходит через concat. Публичные сигнатуры сохранены.
- Rust: публичные `Vec<u8>` / `Arc<Vec<u8>>` сохранены. Вместо обязательного `Bytes::to_vec()` используется consuming conversion `Vec::from(Bytes)` / `into()`. Согласно [реализации bytes](https://docs.rs/bytes/1.12.1/src/bytes/bytes.rs.html), возможность переиспользования зависит от владения буфером; гарантировать отсутствие копий для shared/sliced transport storage нельзя. Новый send_take удалён, пример снова использует прежний send.
- Python: ID остаётся криптографически случайным UUIDv4, но без создания объекта UUID. Микротест на этом хосте (лучший из 5, 100000 вызовов) — 1.460 мкс вместо 1.919 мкс; это не измерение RPS сервиса.
- Python Kafka: pause partition при admission, resume после завершения запроса; отдельная задача и ContextVars на запрос, concurrency между partition сохранена. Нет task-per-record backlog, нет batching. Проверены медленные partition через consumer loop, порядок, stop, отзыв partition, контекст и managed offsets. Эти тесты не заменяют интеграционный rebalance с настоящим Kafka broker.
- Go: короткий путь request metrics для конкретного встроенного noop backend. Внешний metrics interface не расширяется; неизвестные/custom backend продолжают получать измерения. Добавлен тест noop и включённых метрик для source/sink.
- Boost HTTP sink: polling заменён передачей соединения через channel и короткие операции учёта на strand. Бизнес-код на общий strand не переносится. Отмена ожидания удаляет waiter; владение полученным соединением защищено lease, в том числе при отмене co_spawn. Публичный синхронный Stop сохранён и по-прежнему дожидается завершения принятой работы.
- Boost HTTP source: стандартные callbacksMutex/lifetimeMutex и activeMutex удалены; неизменяемые снимки таблицы callback, атомарный допуск/retirement и асинхронное ожидание завершения активных вызовов. Проверка корреляции после допуска сохранена из-за ротации pending map. Shutdown использует общий stop token поколения вместе с отдельной отменой запроса.

### Что намеренно не менялось

`setResultCallback` и копирование mutable captures на каждый вызов сохранены. `setSharedResultCallback` удалён. В userver этот кандидат оптимизации не оставлен: отказ от копирования произвольного std::function менял бы семантику. Там используются engine::Mutex/SharedMutex, а не std::mutex.

Полностью убрать блокировки всего Boost transport path этим изменением не удалось: shared `RotatingMap`, `SingleUseEvent`, registry HTTP-сессий и другие transport adapters содержат стандартные mutex. Router после Freeze уже читает таблицу без mutex. Синхронные Stop/consumeResult нельзя просто заменить на coroutine API при текущем требовании совместимости. Не заменяем mutex на spinlock и не переносим бизнес-граф целиком на strand; оставшиеся общие примитивы требуют отдельного проектирования и нагрузки.

Общая Error-причина отмены, Kafka batching, изменение наблюдаемых headers/metadata и публичных типов не включены. Чужие библиотеки и их spans не редактировались.

### Проверка

TypeScript: build, lint изменённых файлов, 253 passed / 1 skipped; добавлен sliced-buffer / split-UTF-8 regression. Go: runtime, datasource и datasink suite прошли. Rust: 115 тестов прошли. Python: 306 тестов полного suite прошли; mypy для изменённых модулей чистый. Boost: 20 HTTP-тестов и 2 OrderService-теста, включая точный порядок trace events, mutable callback, отмену waiter, pool reuse, deadlines и shutdown; ASan/UBSan проверены. Логи прогонов находятся в `/tmp/*transport*` и `/tmp/transport-cpp-review/`.

Повторного профилирования сервисов после этих изменений ещё не было; прирост throughput/latency не заявляется.
