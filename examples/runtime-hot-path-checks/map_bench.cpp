#include "rejected_concurrent_rotatingmap.hpp"
#include "baseline_rotatingmap.hpp"
#include <boost/unordered/concurrent_flat_map.hpp>
#include <boost/asio/io_context.hpp>
#include <barrier>
#include <iostream>
#include <thread>
using Value = std::shared_ptr<int>;
using Candidate=servicelib::store::RotatingMap<std::string,Value>;
template<class Map> void run(const char* name, Map& map, int threads) {
 std::barrier start{threads+1}; std::vector<std::jthread> workers;
 for(int t=0;t<threads;++t) workers.emplace_back([&,t]{
  std::vector<std::string> keys;
  for(int i=0;i<256;++i) keys.push_back(std::to_string(t)+"-request-"+std::to_string(i));
  auto value=std::make_shared<int>(42);
  start.arrive_and_wait();
  for(int n=0;n<1500;++n){
   for(auto& k:keys) map.set(k,value);
   for(auto& k:keys) if(!map.get(k)) std::terminate();
   for(auto& k:keys) if(!map.pop(k)) std::terminate();
  }
 });
 auto time=std::chrono::steady_clock::now(); start.arrive_and_wait(); workers.clear();
 auto ns=std::chrono::duration<double,std::nano>(std::chrono::steady_clock::now()-time).count();
 std::cout<<name<<" threads="<<threads<<" ns/op="<<ns/(threads*1500.*256*3)<<std::endl;
}
int main(){
 boost::asio::io_context io; servicelib::detail::ParallelExecutorRegistry::Set(io.get_executor());
 for(int repeat=0;repeat<3;++repeat) for(int t:{1,4}) {
  servicelib::store::BaselineRotatingMap<std::string,Value> old{std::chrono::hours{1}}; Candidate candidate{std::chrono::hours{1}};
  run("sharded",old,t);run("concurrent_flat_map",candidate,t);
 }
 servicelib::detail::ParallelExecutorRegistry::Clear();
}
