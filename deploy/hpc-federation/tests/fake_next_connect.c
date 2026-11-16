/* fake_next_connect.c: the test's stand-in for the next connect() after gateport.so in the preload list
 * (tests/deploy/test_hpc_gate_port_shim.py). It is loaded after the shim, so the shim's dlsym(RTLD_NEXT, "connect")
 * finds it, and it sees exactly the destination the shim passes on. It never connects: it appends one line per call to
 * the file named by FAKE_CONNECT_LOG, "<tcp|udp|other> <inet|inet6|unix|other> <address|-> <port|0>", and fails with
 * ENETDOWN, so a test reads the destinations from the file and the shim's result from the caller's errno. */
#define _GNU_SOURCE
#include <arpa/inet.h>
#include <errno.h>
#include <fcntl.h>
#include <netinet/in.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/socket.h>
#include <unistd.h>

int connect(int fd, const struct sockaddr *addr, socklen_t len) {
    char line[160], host[INET6_ADDRSTRLEN] = "-";
    const char *kind = "other", *family = "other";
    const char *path = getenv("FAKE_CONNECT_LOG");
    unsigned port = 0;
    int type = 0, out;
    socklen_t n = sizeof type;
    (void)len;
    if (getsockopt(fd, SOL_SOCKET, SO_TYPE, &type, &n) == 0) kind = type == SOCK_STREAM ? "tcp" : type == SOCK_DGRAM ? "udp" : "other";
    if (addr && addr->sa_family == AF_INET) {
        const struct sockaddr_in *s = (const struct sockaddr_in *)addr;
        family = "inet";
        port = ntohs(s->sin_port);
        inet_ntop(AF_INET, &s->sin_addr, host, sizeof host);
    } else if (addr && addr->sa_family == AF_INET6) {
        const struct sockaddr_in6 *s = (const struct sockaddr_in6 *)addr;
        family = "inet6";
        port = ntohs(s->sin6_port);
        inet_ntop(AF_INET6, &s->sin6_addr, host, sizeof host);
    } else if (addr && addr->sa_family == AF_UNIX) {
        family = "unix";
    }
    snprintf(line, sizeof line, "%s %s %s %u\n", kind, family, host, port);
    if (path && (out = open(path, O_WRONLY | O_APPEND | O_CREAT | O_CLOEXEC, 0600)) >= 0) {
        if (write(out, line, strlen(line)) < 0) { /* the test then finds the line missing */ }
        close(out);
    }
    errno = ENETDOWN;
    return -1;
}
