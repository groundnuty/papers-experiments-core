/* portshim.c: LD_PRELOAD shim for rootless Onedata services on an HPC node.
 *
 * There is no network namespace: the services share the node's interfaces with every other process on
 * the node. The rule is default-deny: every socket a process of the container binds is on loopback, except
 * the two public TCP ports. For every bind(), listen() and connect():
 *   bind()    TCP to 443, any address                 -> 8443, the address kept (public)
 *   bind()    TCP to 8443 or 6665, any address        -> untouched (public: REST and GUI; rtransfer)
 *   bind()    to a multicast group (IPv4 or IPv6)     -> fails with EADDRNOTAVAIL: a receiver on loopback would hear
 *                                                        nothing, and its group traffic would leave the node. (The
 *                                                        zone's nodefinder then never opens its two UDP sockets.)
 *   bind()    of port 0 on the wildcard address       -> deferred: the call succeeds and the socket stays unbound.
 *             (0.0.0.0, ::, ::ffff:0.0.0.0)              A client that then connects gets the kernel's own choice,
 *                                                        as if it had never called bind() (Erlang's resolver, inet_res,
 *                                                        binds 0.0.0.0:0 and connects to the site's DNS); a TCP
 *                                                        listen() lands it on loopback (below)
 *   bind()    anything else, TCP or UDP, any port     -> 127.0.0.1 (IPv6: ::1, or ::ffff:127.0.0.1 for a dual-stack
 *                                                        socket on ::, so IPv4 clients still reach it); 80 goes to
 *                                                        8080 and 4369 to this job's epmd port (ERL_EPMD_PORT in
 *                                                        HPCOP_ERLANG_ENV) on the way
 *   bind()    of IPv6 that lands on ::1, on a node
 *             with no usable ::1                      -> fails: EADDRINUSE for memcached and moxi, which skip the
 *                                                        address on it; EADDRNOTAVAIL (the kernel's own) for every
 *                                                        other program, epmd included, which exits on EADDRINUSE
 *                                                        (never moved to :: or 0.0.0.0; see v6_bind_errno)
 *   listen()  on an unbound socket                    -> bound to loopback, port 0, first (a deferred bind, or none:
 *                                                        the kernel would bind the wildcard address)
 *   listen()  on a socket bound off loopback, on a
 *             port that is not public                 -> fails with EADDRNOTAVAIL (a bind this shim did not see)
 *   connect() to 80/443 on an address of THIS host   -> 8080/8443
 *   connect() to 443 on a listed PEER address        -> 8443   (a provider reaching the zone, or
 *                                                                another provider, on its node)
 *   connect() to 4369 on an address of this host     -> this job's epmd port
 *   connect() to an address of this host, on a port
 *             another user may bind (not public, at
 *             least ip_unprivileged_port_start)       -> 127.0.0.1 (IPv6: ::1): every listener of ours is there
 *   anything else                                     -> untouched
 * The public ports are compiled in, so a missing file can never open a port. Ports below the node's
 * ip_unprivileged_port_start (1024) are left alone on connect: only root can listen there, and the node's
 * resolver may (53).
 * Peers are the addresses in HPCOP_PEERS_FILE: one IPv4 or IPv6 literal per line, '#' starts a comment.
 * Each file is read once per process, at first need; no name is ever resolved here.
 *
 * bind(), listen() and connect() go straight to the kernel (no dlsym). Build it inside the rootfs with the
 * image's own gcc (build-rootfs.sh), so it links against the image's glibc. It is installed through the
 * rootfs's /etc/ld.so.preload because `service` runs init scripts under env -i, which drops LD_PRELOAD.
 * On a node shared with other users loopback is shared too: portshim-owner.h adds an owner check, so a
 * loopback TCP connection between a socket of ours and one of another uid is refused, on every port but the
 * public ones: at accept() (every loopback listener of ours), at connect() (a listener of ours must hold the
 * port), and once connected (the socket at the other end must be ours; checked by connect() itself, or by
 * getsockopt(SO_ERROR) for a non-blocking one).
 * Statically linked programs and Go programs (Couchbase's goxdcr and projector) make their own system
 * calls and never pass through here: prep replaces projector with a stand-in that never listens, and
 * the live port check (check-defaults.sh --live) fails on any listener of ours off loopback, and on any
 * loopback listener held by a program other than the ones whose accept() passes through here. */
#define _GNU_SOURCE
#include <arpa/inet.h>
#include <errno.h>
#include <fcntl.h>
#include <ifaddrs.h>
#include <netinet/in.h>
#include <stdint.h>
#include <stdio.h>
#include <string.h>
#include <sys/socket.h>
#include <sys/stat.h>
#include <sys/syscall.h>
#include <time.h>
#include <unistd.h>

#ifndef HPCOP_PEERS_FILE
#define HPCOP_PEERS_FILE "/etc/hpcop-peers"
#endif
#ifndef HPCOP_ERLANG_ENV
#define HPCOP_ERLANG_ENV "/etc/hpcop-erlang-env"
#endif
#ifndef HPCOP_UNPRIV_START_FILE
#define HPCOP_UNPRIV_START_FILE "/proc/sys/net/ipv4/ip_unprivileged_port_start"
#endif
#define EPMD_WELL_KNOWN 4369
#define EPMD_JOB_MIN 21300   /* configure.sh draws this job's epmd port from [MIN, MAX] */
#define EPMD_JOB_MAX 21999
#define UNPRIV_DEFAULT 1024  /* the kernel's default ip_unprivileged_port_start */

/* hpcop-public-ports: begin (ports.sh's HPCOP_PUBLIC_PORTS must list the same; a test compares) */
static const unsigned short public_ports[] = {
    8443,                         /* from 443: REST, GUI, graph sync; provider-to-provider HTTPS */
    6665,                         /* rtransfer: provider-to-provider data */
};
/* hpcop-public-ports: end */

enum { MAX_LOCAL = 64, MAX_PEERS = 256, MAX_LINE = 64, WAIT_SPINS = 100000 };
enum { UNLOADED = 0, LOADING = 1, READY = 2 };
enum { BIND_GO = 0, BIND_DEFER = -1 };   /* plan_bind4/6: bind as planned, or leave unbound; else an errno */

static int is_public(unsigned short p) {
    for (size_t k = 0; k < sizeof public_ports / sizeof public_ports[0]; k++)
        if (p == public_ports[k]) return 1;
    return 0;
}

/* Tables filled once per process. A thread that finds another one LOADING waits a little; if the loader
 * never finishes (a fork() in the middle of a load), it carries on without the table. */
static int local_state, peer_state, epmd_state, unpriv_state;
static int nlocal4, nlocal6, npeer4, npeer6;
static unsigned short epmd_job_port;
static long unpriv_value = -1;
static in_addr_t local4[MAX_LOCAL];
static struct in6_addr local6[MAX_LOCAL];
static struct in_addr peer4[MAX_PEERS];
static struct in6_addr peer6[MAX_PEERS];

static int is_ready(int *state) { return __atomic_load_n(state, __ATOMIC_ACQUIRE) == READY; }

static int claim(int *state) {  /* 1: the caller loads the table and then calls publish() */
    int expected = UNLOADED;
    if (__atomic_compare_exchange_n(state, &expected, LOADING, 0, __ATOMIC_ACQ_REL, __ATOMIC_ACQUIRE))
        return 1;
    for (int i = 0; i < WAIT_SPINS && !is_ready(state); i++) syscall(SYS_sched_yield);
    return 0;
}

static void publish(int *state) { __atomic_store_n(state, READY, __ATOMIC_RELEASE); }

/* One line to the process's stderr, which is the service's own log: Couchbase's babysitter log for memcached and
 * moxi (ns_server reads their output), run_erl's log for an Erlang VM. Only to a pipe, a file or a terminal: a
 * daemon that closed fd 2 may have reused it for a socket, and a line must never go into a connection. */
static void note(const char *line) {
    struct stat st;
    size_t n = strlen(line);
    int saved = errno;
    if (syscall(SYS_fstat, 2, &st) == 0 && (S_ISFIFO(st.st_mode) || S_ISREG(st.st_mode) || S_ISCHR(st.st_mode)))
        while (n > 0) {
            long w = syscall(SYS_write, 2, line, n);
            if (w < 0 && errno == EINTR) continue;
            if (w <= 0) break;
            line += w; n -= (size_t)w;
        }
    errno = saved;
}

static void load_local(void) {
    struct ifaddrs *ifs, *i;
    int n4 = 0, n6 = 0;
    if (getifaddrs(&ifs) == 0) {
        for (i = ifs; i; i = i->ifa_next) {
            if (!i->ifa_addr) continue;
            if (i->ifa_addr->sa_family == AF_INET && n4 < MAX_LOCAL)
                local4[n4++] = ((struct sockaddr_in *)i->ifa_addr)->sin_addr.s_addr;
            else if (i->ifa_addr->sa_family == AF_INET6 && n6 < MAX_LOCAL)
                local6[n6++] = ((struct sockaddr_in6 *)i->ifa_addr)->sin6_addr;
        }
        freeifaddrs(ifs);
    }
    nlocal4 = n4; nlocal6 = n6;
}

static int locals_ready(void) {
    if (!is_ready(&local_state) && claim(&local_state)) { load_local(); publish(&local_state); }
    return is_ready(&local_state);
}

static int is_loopback4(in_addr_t a) { return (ntohl(a) >> 24) == 127; }

static int is_local4(in_addr_t a) {  /* loopback, or an address of one of this host's interfaces */
    if (is_loopback4(a)) return 1;
    if (!locals_ready()) return 0;
    for (int k = 0; k < nlocal4; k++)
        if (local4[k] == a) return 1;
    return 0;
}

static int is_local6(const struct in6_addr *a) {
    if (IN6_IS_ADDR_LOOPBACK(a)) return 1;
    if (!locals_ready()) return 0;
    for (int k = 0; k < nlocal6; k++)
        if (memcmp(&local6[k], a, sizeof *a) == 0) return 1;
    return 0;
}

/* Small streaming line reader: some callers run on small stacks, and no line here is long. */
static void read_lines(const char *path, void (*each)(char *)) {
    char chunk[512], line[MAX_LINE];
    size_t len = 0;
    int overlong = 0, comment = 0;
    int fd = open(path, O_RDONLY | O_CLOEXEC);
    if (fd < 0) return;
    for (;;) {
        ssize_t r = read(fd, chunk, sizeof chunk);
        if (r < 0 && errno == EINTR) continue;
        if (r <= 0) break;
        for (ssize_t k = 0; k < r; k++) {
            char c = chunk[k];
            if (c == '\n') {
                if (!overlong) { line[len] = '\0'; each(line); }
                len = 0; overlong = comment = 0;
                continue;
            }
            if (c == '#') comment = 1;
            if (comment || overlong) continue;
            if (len + 1 >= sizeof line) { overlong = 1; continue; }
            line[len++] = c;
        }
    }
    if (len && !overlong) { line[len] = '\0'; each(line); }
    close(fd);
}

static void add_peer(char *s) {
    char *e;
    while (*s == ' ' || *s == '\t' || *s == '\r') s++;
    e = s + strlen(s);
    while (e > s && (e[-1] == ' ' || e[-1] == '\t' || e[-1] == '\r')) *--e = '\0';
    if (!*s) return;
    if (npeer4 < MAX_PEERS && inet_pton(AF_INET, s, &peer4[npeer4]) == 1) npeer4++;
    else if (npeer6 < MAX_PEERS && inet_pton(AF_INET6, s, &peer6[npeer6]) == 1) npeer6++;
}

static void add_epmd(char *s) {  /* ERL_EPMD_PORT=<port in [EPMD_JOB_MIN, EPMD_JOB_MAX]>; anything else is ignored */
    static const char key[] = "ERL_EPMD_PORT=";
    unsigned long v = 0;
    if (strncmp(s, key, sizeof key - 1) != 0) return;
    s += sizeof key - 1;
    if (*s < '0' || *s > '9') return;
    for (; *s >= '0' && *s <= '9' && v <= 65535; s++) v = v * 10 + (unsigned long)(*s - '0');
    if (*s == '\0' && v >= EPMD_JOB_MIN && v <= EPMD_JOB_MAX) epmd_job_port = (unsigned short)v;
}

static void add_unpriv(char *s) {  /* the sysctl's value: a number in [0, 65535]; anything else is ignored */
    long v = 0;
    while (*s == ' ' || *s == '\t') s++;
    if (*s < '0' || *s > '9') return;
    for (; *s >= '0' && *s <= '9' && v <= 65535; s++) v = v * 10 + (*s - '0');
    while (*s == ' ' || *s == '\t' || *s == '\r') s++;
    if (*s == '\0' && v <= 65535) unpriv_value = v;
}

static int peers_ready(void) {
    if (!is_ready(&peer_state) && claim(&peer_state)) { read_lines(HPCOP_PEERS_FILE, add_peer); publish(&peer_state); }
    return is_ready(&peer_state);
}

static unsigned short epmd_port(void) {  /* this job's epmd port, or the well-known one if none is set */
    if (!is_ready(&epmd_state) && claim(&epmd_state)) { read_lines(HPCOP_ERLANG_ENV, add_epmd); publish(&epmd_state); }
    return is_ready(&epmd_state) && epmd_job_port ? epmd_job_port : EPMD_WELL_KNOWN;
}

/* The lowest port another unprivileged user can bind: the node's ip_unprivileged_port_start, read once
 * (1024 if it cannot be read). Below it only root listens, and root is outside the threat model. */
static long unpriv_start(void) {
    if (!is_ready(&unpriv_state) && claim(&unpriv_state)) { read_lines(HPCOP_UNPRIV_START_FILE, add_unpriv); publish(&unpriv_state); }
    return is_ready(&unpriv_state) && unpriv_value >= 0 ? unpriv_value : UNPRIV_DEFAULT;
}

/* 1 for a port the owner check covers on loopback, and that a connect to this host's own address sends to
 * loopback: one another user could listen on (at least the unprivileged start), and not public. */
static int owner_port(unsigned short p) { return !is_public(p) && p >= unpriv_start(); }

static int is_peer4(struct in_addr a) {
    if (!peers_ready()) return 0;
    for (int k = 0; k < npeer4; k++)
        if (peer4[k].s_addr == a.s_addr) return 1;
    return 0;
}

static int is_peer6(const struct in6_addr *a) {
    if (!peers_ready()) return 0;
    for (int k = 0; k < npeer6; k++)
        if (memcmp(&peer6[k], a, sizeof *a) == 0) return 1;
    return 0;
}

static int v6only(int fd) {   /* a system call: the shim's own getsockopt() is for SO_ERROR */
    int v = 0;
    socklen_t n = sizeof v;
    return syscall(SYS_getsockopt, fd, IPPROTO_IPV6, IPV6_V6ONLY, &v, &n) == 0 && v;
}

static int is_stream(int fd) {
    int t = 0;
    socklen_t n = sizeof t;
    return syscall(SYS_getsockopt, fd, SOL_SOCKET, SO_TYPE, &t, &n) == 0 && t == SOCK_STREAM;
}

/* ---- the program's name, for the errno of a refused IPv6 bind ---- */

/* A node may have IPv6 disabled (live run 4: disable_ipv6 = 1 on lo, no ::1). Checked once per process, at its first
 * IPv6 bind that lands on ::1, by binding [::1]:0 on a TCP socket that never listens (it is closed at once; a TCP
 * socket, so the live check's ban on UDP sockets of ours never sees it). */
#ifdef HPCOP_TEST_NO_V6_LOOPBACK   /* test builds only: behave as on a node with IPv6 disabled */
static int v6_loopback_usable(void) { return 0; }
#else
static int v6lo_state, v6lo_usable;

static int v6_loopback_usable(void) {
    if (!is_ready(&v6lo_state) && claim(&v6lo_state)) {
        struct sockaddr_in6 lo;
        long fd = syscall(SYS_socket, AF_INET6, SOCK_STREAM | SOCK_CLOEXEC, 0);
        memset(&lo, 0, sizeof lo);
        lo.sin6_family = AF_INET6;
        lo.sin6_addr = in6addr_loopback;
        v6lo_usable = fd >= 0 && syscall(SYS_bind, fd, &lo, sizeof lo) == 0;
        if (fd >= 0) syscall(SYS_close, fd);
        publish(&v6lo_state);
    }
    return is_ready(&v6lo_state) ? v6lo_usable : 1;   /* not known (a fork mid-check): the bind itself will tell */
}
#endif

/* Which program is this? A bind that would land on ::1, on a node without ::1, is answered with an errno, and the right
 * one depends on what the program does with it:
 *   memcached, moxi (Couchbase 4.5.1, both images): each binds every address getaddrinfo gives for its port, and of
 *     bind()'s errors only EADDRINUSE makes it skip that address and go on (memcached: cmp $0x62 at 0x413bc7, otherwise
 *     "Failed to bind to address" and exit 71; moxi: cmp $0x62 at 0x40f487, otherwise "could not listen on port").
 *     They get EADDRINUSE, so they skip IPv6 and keep their IPv4 listener on 127.0.0.1, owner-checked as before. moxi's
 *     second listener, ns_server's `-p 0`, is the same loop on port 0.
 *   epmd of erts-13.0.2 (both images' panels): binds 127.0.0.1 and then ::1, which is optional. EADDRINUSE there means
 *     "there is already a epmd running at port ... on ipaddr ::1", and it exits 0 (cmp $0x62 at 0x4ab9, then 0x4db1);
 *     any other errno it logs ("failed to bind on ipaddr ::1") and carries on with IPv4. So it gets the kernel's own
 *     error, EADDRNOTAVAIL (live run 5: with EADDRINUSE, no epmd ran and the panel's VM died on econnrefused).
 *   everything else gets EADDRNOTAVAIL as well: what the kernel would answer, so a program the shim does not know sees
 *     what it would see without the shim. (Erlang's distribution never gets here: inet_tcp_dist binds IPv4 on
 *     inet_dist_use_interface. Were it to, it walks its port range on EADDRINUSE only and returns any other error at
 *     once, so neither errno would help it; tests/beam_dist.py reads that from the images' beams.)
 * The program is told by the name of its executable, once per process: /proc/self/exe, which the process cannot choose,
 * else argv[0] (no /proc). An exact match on the two names, so an unidentified program is never given EADDRINUSE. The
 * shim never binds :: or 0.0.0.0 in place of ::1. Which of the two named the program goes to the service's log, once. */
static const char *base_of(const char *path) {
    const char *slash = strrchr(path, '/');
    return slash ? slash + 1 : path;
}

/* *by_exe: 1 if the name came from the link, 0 if from argv[0] */
static const char *program_name_at(const char *exe_link, char *buf, size_t n, int *by_exe) {
    ssize_t r = readlink(exe_link, buf, n - 1);
    if (r > 0 && (size_t)r < n - 1) {   /* a full buffer may be a cut path: not trusted */
        buf[r] = '\0';
        *by_exe = 1;
        return base_of(buf);
    }
    *by_exe = 0;
    return program_invocation_short_name;
}

static const char *program_name(char *buf, size_t n, int *by_exe) { return program_name_at("/proc/self/exe", buf, n, by_exe); }

static int skips_v6_on_eaddrinuse(const char *name) {
    return name && (strcmp(name, "memcached") == 0 || strcmp(name, "moxi") == 0);
}

static int prog_state, prog_eaddrinuse;

static int program_wants_eaddrinuse(void) {
    if (!is_ready(&prog_state) && claim(&prog_state)) {
        char buf[512], line[256];
        int by_exe = 0;
        const char *name = program_name(buf, sizeof buf, &by_exe);
        prog_eaddrinuse = skips_v6_on_eaddrinuse(name);
        snprintf(line, sizeof line, "hpcop portshim: pid %ld is '%.40s' by %s; an IPv6 bind on a node without ::1 gets %s\n",
                 (long)syscall(SYS_getpid), name ? name : "", by_exe ? "/proc/self/exe" : "argv[0] (/proc/self/exe unreadable)",
                 prog_eaddrinuse ? "EADDRINUSE" : "EADDRNOTAVAIL");
        note(line);
        publish(&prog_state);
    }
    return is_ready(&prog_state) && prog_eaddrinuse;   /* not known (a fork mid-load): the kernel's own answer */
}

/* 0: the bind goes ahead. Else the errno the shim answers instead of binding. */
static int v6_bind_errno(const struct in6_addr *a, unsigned short port) {
    if (is_public(port) || !IN6_IS_ADDR_LOOPBACK(a) || v6_loopback_usable()) return 0;
    return program_wants_eaddrinuse() ? EADDRINUSE : EADDRNOTAVAIL;
}

/* ---- bind() ---- */

static unsigned short bind_port(unsigned short p) {   /* 80, 443 and epmd's 4369, as the services must bind them */
    return p == 80 ? 8080 : p == 443 ? 8443 : p == EPMD_WELL_KNOWN ? epmd_port() : p;
}

/* IPv4: *a and *port as they must be bound (the caller's private copy). BIND_GO, BIND_DEFER, or an errno. */
static int plan_bind4(struct in_addr *a, unsigned short *port, int stream) {
    unsigned short p = bind_port(*port);
    *port = p;
    if (IN_MULTICAST(ntohl(a->s_addr))) return EADDRNOTAVAIL;
    if (stream && is_public(p)) return BIND_GO;
    if (is_loopback4(a->s_addr)) return BIND_GO;
    if (p == 0 && a->s_addr == htonl(INADDR_ANY)) return BIND_DEFER;
    a->s_addr = htonl(INADDR_LOOPBACK);
    return BIND_GO;
}

/* IPv6, with v4-mapped addresses handled as IPv4. */
static int plan_bind6(int fd, struct in6_addr *a, unsigned short *port, int stream) {
    static const unsigned char mapped_lo[16] = {0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0xff, 0xff, 127, 0, 0, 1};
    unsigned short p;
    if (IN6_IS_ADDR_V4MAPPED(a)) {
        struct in_addr v4;
        int plan;
        memcpy(&v4, &a->s6_addr[12], sizeof v4);
        plan = plan_bind4(&v4, port, stream);
        memcpy(&a->s6_addr[12], &v4, sizeof v4);
        return plan;
    }
    p = bind_port(*port);
    *port = p;
    if (IN6_IS_ADDR_MULTICAST(a)) return EADDRNOTAVAIL;
    if (stream && is_public(p)) return BIND_GO;
    if (!IN6_IS_ADDR_LOOPBACK(a)) {
        int any = IN6_IS_ADDR_UNSPECIFIED(a), refused;
        struct in6_addr to = in6addr_loopback;
        if (any && !v6only(fd)) memcpy(to.s6_addr, mapped_lo, sizeof mapped_lo);  /* dual-stack: IPv4 clients of 127.0.0.1 */
        if ((refused = v6_bind_errno(&to, p)) != 0) return refused;
        if (p == 0 && any) return BIND_DEFER;
        *a = to;
        return BIND_GO;
    }
    return v6_bind_errno(a, p);
}  /* (an IPv6 client of a dual-stack listener moved to ::ffff:127.0.0.1 must dial 127.0.0.1; none here does) */

static int unbound(int fd) {   /* a socket of ours with no local port yet */
    struct sockaddr_storage s;
    socklen_t n = sizeof s;
    memset(&s, 0, sizeof s);
    if (syscall(SYS_getsockname, fd, &s, &n) != 0) return 0;
    if (s.ss_family == AF_INET) return ((struct sockaddr_in *)&s)->sin_port == 0;
    return s.ss_family == AF_INET6 && ((struct sockaddr_in6 *)&s)->sin6_port == 0;
}

static long finish_bind(int fd, const struct sockaddr *addr, socklen_t len, int plan, int saved,
                        const struct sockaddr *orig, socklen_t orig_len) {
    if (plan == BIND_DEFER && unbound(fd)) { errno = saved; return 0; }
    if (plan == BIND_DEFER) { errno = saved; return syscall(SYS_bind, fd, orig, orig_len); }  /* the kernel's answer */
    if (plan > 0) { errno = plan; return -1; }
    errno = saved;
    return syscall(SYS_bind, fd, addr, len);
}

static long shim_bind(int fd, const struct sockaddr *addr, socklen_t len) {
    int saved = errno;  /* table loading must not leak an errno into a successful call */
    if (addr && len >= (socklen_t)sizeof(struct sockaddr_in) && addr->sa_family == AF_INET) {
        struct sockaddr_in s;
        unsigned short p;
        int plan;
        memcpy(&s, addr, sizeof s);
        p = ntohs(s.sin_port);
        plan = plan_bind4(&s.sin_addr, &p, is_stream(fd));
        s.sin_port = htons(p);
        return finish_bind(fd, (const struct sockaddr *)&s, sizeof s, plan, saved, addr, len);
    } else if (addr && len >= (socklen_t)sizeof(struct sockaddr_in6) && addr->sa_family == AF_INET6) {
        struct sockaddr_in6 s;
        unsigned short p;
        int plan;
        memcpy(&s, addr, sizeof s);
        p = ntohs(s.sin6_port);
        plan = plan_bind6(fd, &s.sin6_addr, &p, is_stream(fd));
        s.sin6_port = htons(p);
        if (memcmp(&s.sin6_addr, &((const struct sockaddr_in6 *)addr)->sin6_addr, sizeof s.sin6_addr) != 0)
            s.sin6_scope_id = 0;   /* moved to loopback: a link-local scope no longer applies */
        return finish_bind(fd, (const struct sockaddr *)&s, sizeof s, plan, saved, addr, len);
    }
    errno = saved;
    return syscall(SYS_bind, fd, addr, len);
}

/* ---- connect() ---- */

/* The port a connect() to `here` (the address is on this host) should really use. */
static unsigned short new_port(unsigned short p, int here, int peer443) {
    if (p == 80 || p == 443) return here ? (p == 80 ? 8080 : 8443) : (p == 443 && peer443 ? 8443 : p);
    if (p == EPMD_WELL_KNOWN && here) return epmd_port();
    return p;
}

/* IPv4: rewrite *a and *port in place (the caller's private copy); 1 if anything changed. */
static int rewrite_connect4(struct in_addr *a, unsigned short *port) {
    unsigned short p = *port, q;
    in_addr_t old = a->s_addr;
    /* is_local4() reads the interface table: ask only when the port is one this shim acts on */
    int here = old == htonl(INADDR_ANY)
               || ((p == 80 || p == 443 || p == EPMD_WELL_KNOWN || owner_port(p)) && is_local4(old));
    q = new_port(p, here, p == 443 && !here && is_peer4(*a));
    if (here && owner_port(q) && !is_loopback4(old)) a->s_addr = htonl(INADDR_LOOPBACK);
    *port = q;
    return q != p || a->s_addr != old;
}

static int rewrite_connect6(struct in6_addr *a, unsigned short *port) {
    unsigned short p = *port, q;
    int here;
    if (IN6_IS_ADDR_V4MAPPED(a)) {
        struct in_addr v4;
        int changed;
        memcpy(&v4, &a->s6_addr[12], sizeof v4);
        changed = rewrite_connect4(&v4, port);
        memcpy(&a->s6_addr[12], &v4, sizeof v4);
        return changed;
    }
    here = IN6_IS_ADDR_UNSPECIFIED(a) || ((p == 80 || p == 443 || p == EPMD_WELL_KNOWN || owner_port(p)) && is_local6(a));
    q = new_port(p, here, p == 443 && !here && is_peer6(a));
    *port = q;
    if (here && owner_port(q) && !IN6_IS_ADDR_LOOPBACK(a)) {
        *a = in6addr_loopback;
        return 1;
    }
    return q != p;
}

#include "portshim-owner.h"

/* ---- listen() ---- */

/* 0: the listen goes ahead (the socket now on loopback, or public). Else the errno to fail it with. */
static int place_listener(int fd) {
    static const unsigned char mapped_lo[16] = {0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0xff, 0xff, 127, 0, 0, 1};
    struct sockaddr_storage s;
    struct endpoint e;
    socklen_t n = sizeof s;
    char line[256];
    memset(&s, 0, sizeof s);
    if (syscall(SYS_getsockname, fd, &s, &n) != 0 || (s.ss_family != AF_INET && s.ss_family != AF_INET6)) return 0;
    endpoint_of(&s, &e);
    if (e.port == 0) {   /* unbound (a deferred bind, or none): loopback, as every other bind of ours */
        if (s.ss_family == AF_INET) {
            struct sockaddr_in lo;
            memset(&lo, 0, sizeof lo);
            lo.sin_family = AF_INET;
            lo.sin_addr.s_addr = htonl(INADDR_LOOPBACK);
            return syscall(SYS_bind, fd, &lo, sizeof lo) == 0 ? 0 : errno;
        } else {
            struct sockaddr_in6 lo;
            memset(&lo, 0, sizeof lo);
            lo.sin6_family = AF_INET6;
            if (!v6only(fd)) memcpy(lo.sin6_addr.s6_addr, mapped_lo, sizeof mapped_lo);
            else if (v6_loopback_usable()) lo.sin6_addr = in6addr_loopback;
            else return EADDRNOTAVAIL;   /* what the kernel answers for ::1 on such a node */
            return syscall(SYS_bind, fd, &lo, sizeof lo) == 0 ? 0 : errno;
        }
    }
    if (endpoint_is_loopback(&e) || is_public(e.port)) return 0;
    snprintf(line, sizeof line, "hpcop portshim: pid %ld: listen() refused on port %u, bound off loopback (a bind this shim did not see)\n",
             (long)syscall(SYS_getpid), e.port);
    note(line);
    return EADDRNOTAVAIL;
}

static long shim_connect(int fd, const struct sockaddr *addr, socklen_t len) {
    int saved = errno;
    if (addr && len >= (socklen_t)sizeof(struct sockaddr_in) && addr->sa_family == AF_INET) {
        struct sockaddr_in s;
        unsigned short p;
        memcpy(&s, addr, sizeof s);
        p = ntohs(s.sin_port);
        if (rewrite_connect4(&s.sin_addr, &p)) s.sin_port = htons(p);
        return checked_connect(fd, (const struct sockaddr *)&s, sizeof s, saved);
    } else if (addr && len >= (socklen_t)sizeof(struct sockaddr_in6) && addr->sa_family == AF_INET6) {
        struct sockaddr_in6 s;
        unsigned short p;
        memcpy(&s, addr, sizeof s);
        p = ntohs(s.sin6_port);
        if (rewrite_connect6(&s.sin6_addr, &p)) s.sin6_port = htons(p);
        return checked_connect(fd, (const struct sockaddr *)&s, sizeof s, saved);
    }
    errno = saved;
    return syscall(SYS_connect, fd, addr, len);
}

int bind(int fd, const struct sockaddr *addr, socklen_t len) { return (int)shim_bind(fd, addr, len); }
int connect(int fd, const struct sockaddr *addr, socklen_t len) { return (int)shim_connect(fd, addr, len); }
int listen(int fd, int backlog) {
    int saved = errno, refused = place_listener(fd);
    if (refused) { errno = refused; return -1; }
    errno = saved;
    return (int)syscall(SYS_listen, fd, backlog);
}
