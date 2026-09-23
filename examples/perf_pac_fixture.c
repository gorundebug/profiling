/* Compile on AArch64 with -g -O2 -mbranch-protection=pac-ret+leaf
 * -fomit-frame-pointer -fno-optimize-sibling-calls. No service changes needed.
 */
#include <stdint.h>

__attribute__((noinline)) uint64_t pac_leaf(uint64_t value)
{
    for (unsigned i = 0; i < 100000; ++i) {
        value = value * 6364136223846793005ULL + 1;
        __asm__ volatile("" : "+r"(value));
    }
    return value;
}

__attribute__((noinline)) uint64_t pac_middle(uint64_t value)
{
    return pac_leaf(value) + 1;
}

__attribute__((noinline)) uint64_t pac_worker(uint64_t value)
{
    return pac_middle(value) + 1;
}

int main(void)
{
    uint64_t value = 1;
    for (;;) {
        value = pac_worker(value);
        __asm__ volatile("" : "+r"(value));
    }
}
