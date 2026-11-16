/* egress_probe.c: the test program of the agent's egress shim (tests/deploy/test_hpc_egress_shim.py). It makes the
 * calls egresslog.c logs and prints what each returned, one "<name> <value>" line each:
 *   egress_probe all      a blocking TCP connect to a loopback listener of its own; a non-blocking TCP connect to
 *                         192.0.2.1:9 (TEST-NET-1, RFC 5737: never routed), its SO_ERROR read after up to 200 ms, then
 *                         closed; two UDP sendto()s to one loopback receiver and one sendmsg() to another, each
 *                         carrying PAYLOAD, which must never reach the log; a non-blocking IPv6 connect to [::1]:9
 *                         with the 24-byte address length the kernel also accepts
 *   egress_probe connect  the blocking loopback connect only (the test of a connect the shim cannot log) */
#include <arpa/inet.h>
#include <errno.h>
#include <fcntl.h>
#include <netinet/in.h>
#include <poll.h>
#include <stdio.h>
#include <string.h>
#include <sys/socket.h>
#include <sys/uio.h>
#include <unistd.h>

#define PAYLOAD "PAYLOAD-7f3a-NEVER-LOGGED"

static struct sockaddr_in loopback(unsigned short port) {
    struct sockaddr_in a;
    memset(&a, 0, sizeof a);
    a.sin_family = AF_INET;
    a.sin_addr.s_addr = htonl(INADDR_LOOPBACK);
    a.sin_port = htons(port);
    return a;
}

static unsigned short bound(int fd) {   /* bind fd to 127.0.0.1:0 and return the port the kernel chose */
    struct sockaddr_in a = loopback(0);
    socklen_t n = sizeof a;
    if (bind(fd, (struct sockaddr *)&a, sizeof a) != 0 || getsockname(fd, (struct sockaddr *)&a, &n) != 0) return 0;
    return ntohs(a.sin_port);
}

static void connect_loopback(void) {
    int l = socket(AF_INET, SOCK_STREAM, 0), c = socket(AF_INET, SOCK_STREAM, 0), r;
    unsigned short port = bound(l);
    struct sockaddr_in a = loopback(port);
    listen(l, 4);
    printf("listener %u\n", port);
    r = connect(c, (struct sockaddr *)&a, sizeof a);
    printf("connect-loopback %d %d\n", r, r == 0 ? 0 : errno);
    close(c);
    close(l);
}

static void connect_testnet(void) {
    int c = socket(AF_INET, SOCK_STREAM, 0), r, err = -1;
    struct sockaddr_in a;
    struct pollfd p;
    socklen_t n = sizeof err;
    memset(&a, 0, sizeof a);
    a.sin_family = AF_INET;
    a.sin_port = htons(9);
    inet_pton(AF_INET, "192.0.2.1", &a.sin_addr);
    fcntl(c, F_SETFL, fcntl(c, F_GETFL) | O_NONBLOCK);
    r = connect(c, (struct sockaddr *)&a, sizeof a);
    printf("connect-testnet %d %d\n", r, r == 0 ? 0 : errno);
    p.fd = c;
    p.events = POLLOUT;
    poll(&p, 1, 200);
    getsockopt(c, SOL_SOCKET, SO_ERROR, &err, &n);
    printf("so_error-testnet %d\n", err);
    close(c);
}

/* A non-blocking IPv6 connect to [::1]:9 whose address length is the RFC 2133 one (24 bytes, no scope id), which the
 * kernel accepts: the shim must log it as it logs a full-length one. */
static void connect_short_v6(void) {
    int c = socket(AF_INET6, SOCK_STREAM, 0), r;
    struct sockaddr_in6 a;
    if (c < 0) { printf("connect-short-v6 skipped %d\n", errno); return; }
    memset(&a, 0, sizeof a);
    a.sin6_family = AF_INET6;
    a.sin6_port = htons(9);
    a.sin6_addr = in6addr_loopback;
    fcntl(c, F_SETFL, fcntl(c, F_GETFL) | O_NONBLOCK);
    r = connect(c, (struct sockaddr *)&a, 24);
    printf("connect-short-v6 %d %d\n", r, r == 0 ? 0 : errno);
    close(c);
}

static void send_udp(void) {
    int r1 = socket(AF_INET, SOCK_DGRAM, 0), r2 = socket(AF_INET, SOCK_DGRAM, 0), s = socket(AF_INET, SOCK_DGRAM, 0);
    unsigned short first = bound(r1), second = bound(r2);
    struct sockaddr_in a = loopback(first), b = loopback(second);
    struct iovec io = {PAYLOAD, sizeof PAYLOAD - 1};
    struct msghdr m;
    int sent = 0;
    memset(&m, 0, sizeof m);
    m.msg_name = &b;
    m.msg_namelen = sizeof b;
    m.msg_iov = &io;
    m.msg_iovlen = 1;
    printf("udp %u %u\n", first, second);
    sent += sendto(s, PAYLOAD, sizeof PAYLOAD - 1, 0, (struct sockaddr *)&a, sizeof a) > 0;
    sent += sendto(s, PAYLOAD, sizeof PAYLOAD - 1, 0, (struct sockaddr *)&a, sizeof a) > 0;
    sent += sendmsg(s, &m, 0) > 0;
    printf("sent %d\n", sent);
    close(s);
    close(r1);
    close(r2);
}

int main(int argc, char **argv) {
    setvbuf(stdout, NULL, _IOLBF, 0);
    printf("pid %ld\n", (long)getpid());
    connect_loopback();
    if (argc > 1 && strcmp(argv[1], "all") == 0) {
        connect_testnet();
        send_udp();
        connect_short_v6();
    }
    return 0;
}
