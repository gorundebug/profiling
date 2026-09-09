#include <servicelib/runtime/detail/sync.hpp>
#include "baseline_sync.hpp"
#include <iostream>
#include <boost/asio/co_spawn.hpp>
#include <boost/asio/io_context.hpp>
template<class Event> void run(const char* name) {
 auto start=std::chrono::steady_clock::now();
 std::size_t ready=0;
 for(int i=0;i<2000000;++i){ Event event;event.Send();ready+=event.IsReady(); }
 auto ns=std::chrono::duration<double,std::nano>(std::chrono::steady_clock::now()-start).count();
 std::cout<<name<<" send+ready ns="<<ns/2000000<<" count="<<ready<<std::endl;
}
template<class Event> void runAsync(const char* name) {
 boost::asio::io_context io;
 std::size_t ready=0;
 auto start=std::chrono::steady_clock::now();
 for(int i=0;i<100000;++i){
  io.restart(); Event event;
  boost::asio::co_spawn(io,event.AsyncWait(),[&](std::exception_ptr e){if(e)std::terminate();++ready;});
  io.poll(); event.Send(); io.run();
 }
 auto ns=std::chrono::duration<double,std::nano>(std::chrono::steady_clock::now()-start).count();
 std::cout<<name<<" async-register+send ns="<<ns/100000<<" count="<<ready<<std::endl;
}
int main(){for(int n=0;n<3;++n){run<servicelib::detail::BaselineSingleUseEvent>("before");run<servicelib::detail::SingleUseEvent>("after");runAsync<servicelib::detail::BaselineSingleUseEvent>("before");runAsync<servicelib::detail::SingleUseEvent>("after");}}
