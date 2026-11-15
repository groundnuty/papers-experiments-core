/* egresslog.c: LD_PRELOAD shim that logs the network destinations of the agent's processes on an HPC batch node.
 *
 * Detection, not a barrier. On a batch node without network namespaces the agent's network cannot be restricted, so
 * its outbound use is detected and reported instead (issue #179, the PI's ruling of 13 Nov 2026). The agent's cell
 * loads this shim, and only it: the services' port shim (portshim.c) is not loaded there, so the agent's network
 * behaves as without a shim, and every destination it asks for is logged.
 *
 * What it logs, one record per event, sent as one datagram to the run's collector (egress-collector.py, which runs
 * outside the cell and writes the log the agent cannot reach):
 *   connect    before connect() to an IPv4 or IPv6 address: the destination asked for
 *   result     after that connect(): what it returned, 0 or the errno (EINPROGRESS for a non-blocking connect)
 *   async      getsockopt(SO_ERROR) on an IPv4 or IPv6 socket: a non-blocking connect's outcome, with the peer when
 *              the socket has one ("-" and port 0 when it has none, a failed connect: the record's fd and pid link it
 *              to its connect)
 *   sendto, sendmsg, sendmmsg
 *              before the call, the first time this process sends to that address and port (UDP, or TCP Fast Open);
 *              a process remembers the last SEEN_MAX destinations
 * Never a payload: a record holds the operation, the pid, the descriptor, the time (CLOCK_REALTIME), the socket type,
 * the address family, the address, the port, the result and the count of records this process could not deliver
 * since its last one. Unix-domain and other non-IP sockets are not logged.
 *
 * The record, one line of text per datagram, space-separated:
 *   hpcop-egress/1 <op> <pid> <fd> <seconds>.<nanoseconds> <tcp|udp|raw|other> <inet|inet6> <address|-> <port>
 *     <result: - for an attempt, else 0 or the errno> <dropped>
 *
 * Delivery. The collector's socket path is compiled in (HPCOP_EGRESS_SOCKET), never read from the environment, so a
 * process cannot point its records elsewhere. The launcher binds the collector's directory read-only into the cell at
 * /hpcop-egress and the shim into /etc/ld.so.preload, so every dynamically linked process of the cell loads it, even
 * one started with a cleared environment, and each record travels on a fresh socket that is closed at once: nothing
 * is inherited, so a child that closes every descriptor (Python's subprocess does) still logs. A record waits for
 * room in a full collector's queue for at most DELIVER_WAIT_MS (2 s), never longer, so a collector that stops reading
 * fails the agent's calls instead of hanging them. **A connect, or a send to a new address, whose record cannot be
 * delivered is refused with EACCES**: through this shim a destination is logged, or it is not reached. A result or
 * async record that cannot be delivered is counted in `dropped`, which the process's next record carries, and the
 * collector's log is then not intact. A non-blocking call waits the same: refusing it at once would fail a burst of an
 * asynchronous client's connects whenever the collector's queue is momentarily full.
 *
 * What bypasses it (the static scan of the agent's code cells is the second line of detection; neither prevents
 * egress): a statically linked program or a Go binary, raw system calls (syscall(2), inline assembly), io_uring,
 * glibc's own resolver (getaddrinfo's DNS queries go to the kernel through glibc's internal aliases, which a preload
 * cannot interpose; the connect that follows a lookup is logged), and a process that unmaps or never loads the shim.
 * Under one uid the agent may also send forged records to the collector; the collector keeps whatever it receives,
 * so a forgery adds lines and never removes one.
 *
 * Build it inside the cell's image with the image's own gcc, as build-rootfs.sh does for the port shim:
 *   gcc -shared -fPIC -O2 -fno-stack-protector -Wall -Wextra -Werror -o egresslog.so egresslog.c */
#define _GNU_SOURCE
#include <arpa/inet.h>
#include <errno.h>
#include <netinet/in.h>
#include <stdio.h>
#include <string.h>
#include <sys/socket.h>
#include <sys/syscall.h>
#include <sys/un.h>
#include <time.h>
#include <unistd.h>

#ifndef HPCOP_EGRESS_SOCKET
#define HPCOP_EGRESS_SOCKET "/hpcop-egress/sock"
#endif
#define RECORD_FORMAT "hpcop-egress/1"

_Static_assert(sizeof HPCOP_EGRESS_SOCKET <= sizeof(((struct sockaddr_un *)0)->sun_path),
               "the collector's socket path does not fit a sockaddr_un");

enum { SEEN_MAX = 128, LOCK_SPINS = 1000, RECORD_MAX = 256, DELIVER_WAIT_MS = 2000, DELIVER_NAP_NS = 1000000 };
#ifndef SIN6_LEN_RFC2133
#define SIN6_LEN_RFC2133 24   /* the shortest IPv6 address the kernel accepts (net/ipv6: no sin6_scope_id) */
#endif

/* One destination, as a record names it and as the new-address table compares it. */
struct dest {
    int family;                     /* AF_INET or AF_INET6 */
    unsigned short port;
    unsigned char a[16];            /* the address's bytes: 4 for IPv4 */
    char text[INET6_ADDRSTRLEN];    /* the address as text, or "-" when not known */
};

static unsigned long dropped;      /* records this process could not deliver since its last delivered one */

/* ---- the record ---- */

static const char *type_name(int fd) {
    int t = 0;
    socklen_t n = sizeof t;
    if (syscall(SYS_getsockopt, fd, SOL_SOCKET, SO_TYPE, &t, &n) != 0) return "other";
    return t == SOCK_STREAM ? "tcp" : t == SOCK_DGRAM ? "udp" : t == SOCK_RAW ? "raw" : "other";
}

/* 1 when addr is an IPv4 or IPv6 address the kernel would read, with *d filled; 0 for anything else. */
static int dest_of(const struct sockaddr *addr, socklen_t len, struct dest *d) {
    memset(d, 0, sizeof *d);
    if (!addr) return 0;
    if (addr->sa_family == AF_INET && len >= (socklen_t)sizeof(struct sockaddr_in)) {
        struct sockaddr_in s;
        memcpy(&s, addr, sizeof s);
        d->family = AF_INET;
        d->port = ntohs(s.sin_port);
        memcpy(d->a, &s.sin_addr, 4);
        return inet_ntop(AF_INET, &s.sin_addr, d->text, sizeof d->text) != NULL;
    }
    if (addr->sa_family == AF_INET6 && len >= (socklen_t)SIN6_LEN_RFC2133) {
        struct sockaddr_in6 s;   /* the kernel takes the RFC 2133 length too: an address without its scope id */
        memset(&s, 0, sizeof s);
        memcpy(&s, addr, len < (socklen_t)sizeof s ? (size_t)len : sizeof s);
        d->family = AF_INET6;
        d->port = ntohs(s.sin6_port);
        memcpy(d->a, &s.sin6_addr, 16);
        return inet_ntop(AF_INET6, &s.sin6_addr, d->text, sizeof d->text) != NULL;
    }
    return 0;
}

static long long monotonic_ms(void) {
    struct timespec t;
    if (clock_gettime(CLOCK_MONOTONIC, &t) != 0) return 0;
    return (long long)t.tv_sec * 1000 + t.tv_nsec / 1000000;
}

/* Send one record on a fresh socket; 0 when the collector's socket took it, -1 otherwise. A full collector's queue is
 * waited on, a millisecond at a time, for at most DELIVER_WAIT_MS: never an unbounded block. `result` is "-" for an
 * attempt. errno is the caller's to restore. */
static int deliver(const char *op, int fd, const struct dest *d, const char *type, const char *result) {
    static const struct sockaddr_un to = {AF_UNIX, HPCOP_EGRESS_SOCKET};
    char line[RECORD_MAX];
    struct timespec now;
    unsigned long owed = __atomic_exchange_n(&dropped, 0, __ATOMIC_ACQ_REL);
    long s, sent;
    int n;
    if (clock_gettime(CLOCK_REALTIME, &now) != 0) now.tv_sec = now.tv_nsec = 0;
    n = snprintf(line, sizeof line, "%s %s %ld %d %lld.%09ld %s %s %s %u %s %lu\n", RECORD_FORMAT, op,
                 (long)syscall(SYS_getpid), fd, (long long)now.tv_sec, (long)now.tv_nsec, type,
                 d->family == AF_INET6 ? "inet6" : "inet", d->text[0] ? d->text : "-", d->port, result, owed);
    if (n <= 0 || n >= (int)sizeof line || (s = syscall(SYS_socket, AF_UNIX, SOCK_DGRAM | SOCK_CLOEXEC, 0)) < 0) {
        __atomic_add_fetch(&dropped, owed, __ATOMIC_ACQ_REL);
        return -1;
    }
    for (long long deadline = monotonic_ms() + DELIVER_WAIT_MS;;) {
        sent = syscall(SYS_sendto, s, line, (size_t)n, MSG_NOSIGNAL | MSG_DONTWAIT, &to, sizeof to);
        if (sent >= 0 || (errno != EAGAIN && errno != EINTR) || monotonic_ms() >= deadline) break;
        if (errno == EAGAIN) {
            struct timespec nap = {0, DELIVER_NAP_NS};
            nanosleep(&nap, NULL);
        }
    }
    syscall(SYS_close, s);
    if (sent == n) return 0;
    __atomic_add_fetch(&dropped, owed, __ATOMIC_ACQ_REL);   /* still owed: the next record carries them */
    return -1;
}

/* A result or async record: one that cannot be delivered is counted, never refused (the call has been made). */
static void deliver_outcome(const char *op, int fd, const struct dest *d, const char *type, int err) {
    char result[16];
    snprintf(result, sizeof result, "%d", err);
    if (deliver(op, fd, d, type, result) != 0) __atomic_add_fetch(&dropped, 1, __ATOMIC_ACQ_REL);
}

/* ---- the new-address table of sendto, sendmsg and sendmmsg ---- */

static struct dest seen[SEEN_MAX];
static int seen_count, seen_next, seen_lock;

/* A bounded try: a fork() while another thread held the lock leaves the child's copy held, and a send must never
 * hang on it. Not taking it costs one record more, never one less. */
static int take_lock(void) {
    for (int k = 0; k < LOCK_SPINS; k++) {
        if (!__atomic_exchange_n(&seen_lock, 1, __ATOMIC_ACQUIRE)) return 1;
        syscall(SYS_sched_yield);
    }
    return 0;
}

static void drop_lock(void) { __atomic_store_n(&seen_lock, 0, __ATOMIC_RELEASE); }

static int same(const struct dest *x, const struct dest *y) {
    return x->family == y->family && x->port == y->port && memcmp(x->a, y->a, sizeof x->a) == 0;
}

static int seen_before(const struct dest *d) {
    int found = 0;
    if (!take_lock()) return 0;
    for (int k = 0; k < seen_count && !found; k++) found = same(&seen[k], d);
    drop_lock();
    return found;
}

static void remember(const struct dest *d) {
    if (!take_lock()) return;
    seen[seen_next] = *d;
    seen_next = (seen_next + 1) % SEEN_MAX;
    if (seen_count < SEEN_MAX) seen_count++;
    drop_lock();
}

/* 0 when the send may go ahead: no address, not IP, an address this process has logged, or one logged now. */
static int log_send(const char *op, int fd, const struct sockaddr *addr, socklen_t len) {
    struct dest d;
    if (!dest_of(addr, len, &d) || seen_before(&d)) return 0;
    if (deliver(op, fd, &d, type_name(fd), "-") != 0) return -1;
    remember(&d);
    return 0;
}

/* ---- the calls ---- */

int connect(int fd, const struct sockaddr *addr, socklen_t len) {
    int saved = errno, err;
    const char *type;
    struct dest d;
    long r;
    if (!dest_of(addr, len, &d)) { errno = saved; return (int)syscall(SYS_connect, fd, addr, len); }
    type = type_name(fd);
    if (deliver("connect", fd, &d, type, "-") != 0) { errno = EACCES; return -1; }
    errno = saved;
    r = syscall(SYS_connect, fd, addr, len);
    err = r == 0 ? 0 : errno;
    deliver_outcome("result", fd, &d, type, err);
    errno = r == 0 ? saved : err;
    return (int)r;
}

int getsockopt(int fd, int level, int name, void *val, socklen_t *len) {
    long r = syscall(SYS_getsockopt, fd, level, name, val, len);
    if (r == 0 && level == SOL_SOCKET && name == SO_ERROR && val && len && *len >= (socklen_t)sizeof(int)) {
        int saved = errno;
        struct sockaddr_storage me, peer;
        socklen_t n = sizeof me, m = sizeof peer;
        struct dest d;
        if (syscall(SYS_getsockname, fd, &me, &n) == 0 && (me.ss_family == AF_INET || me.ss_family == AF_INET6)) {
            if (syscall(SYS_getpeername, fd, &peer, &m) != 0 || !dest_of((struct sockaddr *)&peer, m, &d)) {
                memset(&d, 0, sizeof d);   /* no peer: a failed connect; its fd and pid link it to the attempt */
                d.family = me.ss_family;
            }
            deliver_outcome("async", fd, &d, type_name(fd), *(int *)val);
        }
        errno = saved;
    }
    return (int)r;
}

ssize_t sendto(int fd, const void *buf, size_t n, int flags, const struct sockaddr *addr, socklen_t len) {
    int saved = errno;
    if (log_send("sendto", fd, addr, len) != 0) { errno = EACCES; return -1; }
    errno = saved;
    return syscall(SYS_sendto, fd, buf, n, flags, addr, len);
}

ssize_t sendmsg(int fd, const struct msghdr *msg, int flags) {
    int saved = errno;
    if (msg && log_send("sendmsg", fd, msg->msg_name, msg->msg_namelen) != 0) { errno = EACCES; return -1; }
    errno = saved;
    return syscall(SYS_sendmsg, fd, msg, flags);
}

int sendmmsg(int fd, struct mmsghdr *vec, unsigned int vlen, int flags) {
    int saved = errno;
    for (unsigned int k = 0; vec && k < vlen; k++)
        if (log_send("sendmmsg", fd, vec[k].msg_hdr.msg_name, vec[k].msg_hdr.msg_namelen) != 0) {
            errno = EACCES;
            return -1;
        }
    errno = saved;
    return (int)syscall(SYS_sendmmsg, fd, vec, vlen, flags);
}
