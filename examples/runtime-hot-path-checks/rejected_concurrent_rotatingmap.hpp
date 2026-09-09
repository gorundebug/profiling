#pragma once

#include <servicelib/runtime/detail/asio_dispatch.hpp>
#include <servicelib/runtime/store/storage.hpp>

#include <boost/asio/any_io_executor.hpp>
#include <boost/asio/steady_timer.hpp>
#include <boost/unordered/concurrent_flat_map.hpp>

#include <chrono>
#include <concepts>
#include <cstddef>
#include <functional>
#include <memory>
#include <mutex>
#include <optional>
#include <utility>

namespace servicelib::store {

inline constexpr std::size_t kRotatingMapShrinkFactor = 4;
inline constexpr std::size_t kRotatingMapShardCount = 64;
inline constexpr std::size_t kRotatingMapMinCapacity = 1'000;

template <typename K, typename V, typename Hash = std::hash<K>,
          typename Equal = std::equal_to<K>>
class RotatingMap final : public IStorage {
 public:
  using Duration = std::chrono::steady_clock::duration;

  explicit RotatingMap(Duration interval,
                       std::size_t minCapacity = kRotatingMapMinCapacity)
      : state_(std::make_shared<State>(
            detail::ParallelExecutorRegistry::Get(), interval, minCapacity)) {
    if (interval <= Duration::zero())
      throw std::invalid_argument("rotating map interval must be positive");
  }
  ~RotatingMap() override {
    if (state_->running) std::terminate();
  }

  void start([[maybe_unused]] Context context) override {
    std::lock_guard lock(state_->lifecycleMutex);
    if (state_->running) throw StoreAlreadyStartedError();
    if (state_->stopped) throw StoreStoppedError();
    state_->running = true;
    Arm(state_);
  }

  void stop([[maybe_unused]] Context context) override {
    std::lock_guard lock(state_->lifecycleMutex);
    if (state_->stopped) return;
    state_->running = false;
    state_->stopped = true;
    static_cast<void>(state_->timer.cancel());
  }

  void set(K key, V value) {
    if (!state_->values.try_emplace(std::move(key), std::move(value)))
      throw DuplicateKeyError();
  }

  template <typename Factory>
  [[nodiscard]] std::pair<V, bool> getOrCreate(const K& key, Factory&& factory) {
    std::optional<V> result;
    // Conversion happens only inside a successful emplacement. A losing
    // concurrent caller visits the existing value without calling its factory.
    struct LazyValue {
      Factory& factory;
      std::optional<V>& result;
      operator V() {
        result.emplace(std::forward<Factory>(factory)());
        return *result;
      }
    };
    const bool inserted = state_->values.try_emplace_or_cvisit(
        key, LazyValue{factory, result},
        [&](const auto& item) { result.emplace(item.second); });
    return {std::move(*result), !inserted};
  }

  [[nodiscard]] std::optional<V> get(const K& key) const
    requires std::copy_constructible<V>
  {
    std::optional<V> result;
    state_->values.cvisit(key, [&](const auto& item) { result.emplace(item.second); });
    return result;
  }

  [[nodiscard]] std::optional<V> pop(const K& key) {
    std::optional<V> result;
    state_->values.erase_if(key, [&](auto& item) {
      result.emplace(std::move(item.second));
      return true;
    });
    return result;
  }

  [[nodiscard]] std::size_t size() const { return state_->values.size(); }

 private:
  RotatingMap(const RotatingMap&) = delete;
  RotatingMap& operator=(const RotatingMap&) = delete;

  struct State final {
    State(boost::asio::any_io_executor executor, Duration intervalValue,
          std::size_t minCapacityValue)
        : timer(std::move(executor)),
          interval(intervalValue),
          minCapacity(minCapacityValue) {}
    std::mutex lifecycleMutex;
    boost::asio::steady_timer timer;
    Duration interval;
    std::size_t minCapacity;
    bool running{};
    bool stopped{};
    boost::unordered::concurrent_flat_map<K, V, Hash, Equal> values;
    std::size_t highWaterMark{};
  };

  static void Arm(const std::shared_ptr<State>& state) {
    state->timer.expires_after(state->interval);
    const std::weak_ptr<State> weak = state;
    state->timer.async_wait([weak](const boost::system::error_code& error) {
      if (error) return;
      const auto state = weak.lock();
      if (!state) return;
      {
        std::lock_guard lock(state->lifecycleMutex);
        if (!state->running) return;
      }
      Rotate(*state);
      std::lock_guard lock(state->lifecycleMutex);
      if (state->running) Arm(state);
    });
  }

  static void Rotate(State& state) {
    const auto total = state.values.size();
    const bool shouldShrink = state.highWaterMark == 0 ||
        total < (state.highWaterMark + kRotatingMapShrinkFactor - 1) /
                    kRotatingMapShrinkFactor;
    state.highWaterMark = std::max(state.highWaterMark, total);
    if (state.highWaterMark < state.minCapacity || !shouldShrink) return;
    state.highWaterMark = total;
    // Rotation in Go is capacity reclamation, not TTL eviction. Rehash is
    // synchronized by the container and preserves concurrent insertions.
    state.values.rehash(0);
  }

  std::shared_ptr<State> state_;
};
template <typename K, typename V, typename Hash = std::hash<K>,
          typename Equal = std::equal_to<K>>
std::unique_ptr<RotatingMap<K, V, Hash, Equal>> makeRotatingMap(
    typename RotatingMap<K, V, Hash, Equal>::Duration interval) {
  return std::make_unique<RotatingMap<K, V, Hash, Equal>>(interval);
}


}  // namespace servicelib::store
