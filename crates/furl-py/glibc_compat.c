/* manylinux_2_28 shim for newer glibc symbols used by static dependencies. C23 strtol* delegates to pre-C23 libc; callers use decimal/hex only, and newer glibc strong symbols override these definitions. */

/* Declare pre-C23 strtol* directly; <stdlib.h> may redirect these calls to __isoc23_* and recurse. */
extern long strtol(const char *nptr, char **endptr, int base);
extern long long strtoll(const char *nptr, char **endptr, int base);
extern unsigned long strtoul(const char *nptr, char **endptr, int base);
extern unsigned long long strtoull(const char *nptr, char **endptr, int base);

long __isoc23_strtol(const char *nptr, char **endptr, int base) {
    return strtol(nptr, endptr, base);
}

long long __isoc23_strtoll(const char *nptr, char **endptr, int base) {
    return strtoll(nptr, endptr, base);
}

unsigned long __isoc23_strtoul(const char *nptr, char **endptr, int base) {
    return strtoul(nptr, endptr, base);
}

unsigned long long __isoc23_strtoull(const char *nptr, char **endptr, int base) {
    return strtoull(nptr, endptr, base);
}

/* Define __libc_single_threaded=0 on glibc <2.32; keeping locking enabled is safe for multithreaded callers. */
char __libc_single_threaded = 0;
