/* gateport_probe.c: the test program of the 443 mapper's handling of short socket addresses
 * (tests/deploy/test_hpc_gate_port_shim.py). The address it passes ends exactly at the end of a readable page, and the
 * next page cannot be read, so a shim that reads more of the address than its length says dies of SIGSEGV here.
 *   gateport_probe short     connect() with every length from 0 to 15 for an AF_INET address and from 0 to 23 for an
 *                            AF_INET6 one (the kernel's shortest is 24): one line each, "<family> <length> <result>
 *                            <errno>"; every one must fail with EINVAL, and none reaches a peer
 *   gateport_probe v6short   connect() to [::1]:443 with the 24-byte IPv6 address the kernel also accepts (no scope id),
 *                            which a shim that maps ::1:443 must read whole and no further: prints "<result> <peer port>" */
#define _GNU_SOURCE
#include <arpa/inet.h>
#include <errno.h>
#include <netinet/in.h>
#include <stdio.h>
#include <string.h>
#include <sys/mman.h>
#include <sys/socket.h>
#include <unistd.h>

#define SHORT_V6 24   /* the RFC 2133 length of a sockaddr_in6: no sin6_scope_id */

/* A buffer of `len` bytes that ends where the readable page ends. */
static char *at_page_end(size_t len) {
    long page = sysconf(_SC_PAGESIZE);
    char *m = mmap(NULL, 2 * (size_t)page, PROT_READ | PROT_WRITE, MAP_PRIVATE | MAP_ANONYMOUS, -1, 0);
    if (m == MAP_FAILED || mprotect(m + page, (size_t)page, PROT_NONE) != 0) return NULL;
    return m + page - len;
}

static void shorts(int family, size_t most) {
    for (size_t len = 0; len <= most; len++) {
        char *end = at_page_end(len);
        int fd = socket(family, SOCK_STREAM, 0), r;
        if (!end || fd < 0) { puts("setup failed"); return; }
        memset(end, 0, len);
        if (len >= sizeof(sa_family_t)) {
            sa_family_t f = (sa_family_t)family;
            memcpy(end, &f, sizeof f);
        }
        errno = 0;
        r = connect(fd, (struct sockaddr *)end, (socklen_t)len);
        printf("%s %zu %d %d\n", family == AF_INET ? "inet" : "inet6", len, r, errno);
        close(fd);
    }
}

int main(int argc, char **argv) {
    if (argc == 2 && strcmp(argv[1], "short") == 0) {
        shorts(AF_INET, 15);
        shorts(AF_INET6, SHORT_V6 - 1);
        return 0;
    }
    if (argc == 2 && strcmp(argv[1], "v6short") == 0) {
        char *end = at_page_end(SHORT_V6);
        int fd = socket(AF_INET6, SOCK_STREAM, 0), r;
        struct sockaddr_in6 peer;
        socklen_t n = sizeof peer;
        if (!end || fd < 0) { puts("setup failed"); return 1; }
        memset(end, 0, SHORT_V6);
        ((struct sockaddr_in6 *)end)->sin6_family = AF_INET6;
        ((struct sockaddr_in6 *)end)->sin6_port = htons(443);
        ((struct sockaddr_in6 *)end)->sin6_addr = in6addr_loopback;
        r = connect(fd, (struct sockaddr *)end, SHORT_V6);
        printf("%d %d\n", r, r == 0 && getpeername(fd, (struct sockaddr *)&peer, &n) == 0 ? ntohs(peer.sin6_port) : 0);
        return 0;
    }
    fputs("usage: gateport_probe short|v6short\n", stderr);
    return 2;
}
