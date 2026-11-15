/* portshim-owner.h: the owner check of the port shim; portshim.c includes it.
 *
 * On a node shared with other users, loopback is shared with them too. The shim therefore refuses a
 * loopback TCP connection between a socket of ours and a socket of another uid, on every port but the
 * public ones (default-deny: every listener of ours that is not public is on loopback), in both directions:
 *   accept()/accept4() on any loopback listener of ours (not a public port): the peer's socket must belong to this
 *       uid, or the connection is closed before the server reads a byte. One call closes at most
 *       ACCEPT_MAX_DROPS such connections, then returns EAGAIN (a non-blocking listener) or ECONNABORTED
 *       (a blocking one), which every server here treats as "try again": a flood cannot hold one call.
 *   connect() to loopback, on a port another user could bind (at least the node's ip_unprivileged_port_start,
 *       and not public), goes ahead only while a listener of this uid holds that exact
 *       address and port (or 0.0.0.0 for an IPv4 destination, [::] for an IPv6 one). The kernel then keeps
 *       every other uid off it: a conflicting bind fails, and SO_REUSEPORT needs the same uid. Otherwise
 *       the call fails with ECONNREFUSED, as when nothing listens, so a client that polls (the Couchbase
 *       initialiser, the launcher) waits for our listener. A connect to this job's epmd port waits up to
 *       EPMD_WAIT_MS for our epmd to bind.
 *   after the connect, the socket at the other end (found by the connection's 4-tuple, as at accept)
 *       must be this uid's, or not yet accepted from a listener that is this uid's; otherwise the
 *       connection is reset and the caller gets ECONNREFUSED before it sends a byte. A blocking connect is
 *       checked before it returns; a non-blocking one when the caller reads its result with
 *       getsockopt(SO_ERROR), as Python and the Erlang VM do. This closes the window in which our
 *       listener closes and another uid binds the port before the connect lands.
 * The owner comes from /proc/net/tcp and tcp6, which list every socket of the network namespace with its
 * uid (hidepid does not hide them). That uid is the owner of the socket's open file (sock_i_uid; kernels
 * before 6.16), so a socket with no open file shows "nobody" (uid 0, or the overflow uid inside a user
 * namespace): a connection still in its listener's accept queue, or one its owner has closed. At accept,
 * such a peer has closed and is dropped. After a connect, the other end in the accept queue (state
 * ESTABLISHED or SYN_RECV) counts as ours when a listener of ours holds the address, checked after the
 * connect: a listener that closes resets its queue, and only one uid can hold an address, so a queued
 * row that survives belongs to that listener; a socket another uid has accepted shows that uid. Any other
 * row, or none, is refused; so is everything when the tables cannot be read (fail closed).
 * Cost: the listener check reads only the listening part of the tables, which the kernel prints first;
 * the peer lookup stops at its row. Both still grow with the number of sockets on the node.
 * Only dynamically linked programs pass through here; Go programs (goxdcr, projector) do not. */

#ifndef HPCOP_PROC_NET_TCP
#define HPCOP_PROC_NET_TCP "/proc/net/tcp"
#endif
#ifndef HPCOP_PROC_NET_TCP6
#define HPCOP_PROC_NET_TCP6 "/proc/net/tcp6"
#endif

enum { TABLE_LINE = 256, TABLE_CHUNK = 4096, TCP_ESTABLISHED = 0x01, TCP_SYN_RECV = 0x03, TCP_LISTEN = 0x0A };
enum { ACCEPT_MAX_DROPS = 8, EPMD_WAIT_MS = 5000, WAIT_STEP_MS = 20 };

struct endpoint { unsigned char a[16]; unsigned short port; };   /* IPv4 as ::ffff:a.b.c.d */

struct query {
    int want_listener;                    /* 1: does a listener of ours hold .local? 0: .peer's uid */
    struct endpoint peer, local;          /* for the uid of the socket at peer, connected to local */
    unsigned long me;                     /* this uid */
    long uid;                             /* result: the peer's uid, -1 if not found */
    unsigned long state;                  /* result: the peer's TCP state */
    int own;                              /* result: a listener of this uid holds .local */
    int done;                             /* stop reading: answered, or past the listening part */
};

static unsigned long my_uid(void) {
#ifdef HPCOP_TEST_UID
    return HPCOP_TEST_UID;
#else
    return (unsigned long)syscall(SYS_getuid);
#endif
}

/* "Nobody" as /proc/net/tcp prints a socket without an open file: 0, or the overflow uid when the reader
 * is in a user namespace that does not map root (read once; 65534 by default). */
static long overflow_uid_value;
static int no_owner_shown(long uid) {
    if (!overflow_uid_value) {
        char b[16];
        long v = 0;
        ssize_t n;
        int fd = open("/proc/sys/kernel/overflowuid", O_RDONLY | O_CLOEXEC);
        n = fd >= 0 ? read(fd, b, sizeof b - 1) : -1;
        if (fd >= 0) close(fd);
        for (ssize_t k = 0; k < n && b[k] >= '0' && b[k] <= '9'; k++) v = v * 10 + (b[k] - '0');
        __atomic_store_n(&overflow_uid_value, v > 0 ? v : 65534, __ATOMIC_RELAXED);
    }
    return uid == 0 || uid == __atomic_load_n(&overflow_uid_value, __ATOMIC_RELAXED);
}

static void endpoint_of(const struct sockaddr_storage *s, struct endpoint *e) {
    memset(e, 0, sizeof *e);
    if (s->ss_family == AF_INET) {
        const struct sockaddr_in *v4 = (const struct sockaddr_in *)s;
        e->a[10] = e->a[11] = 0xff;
        memcpy(&e->a[12], &v4->sin_addr, 4);
        e->port = ntohs(v4->sin_port);
    } else {
        const struct sockaddr_in6 *v6 = (const struct sockaddr_in6 *)s;
        memcpy(e->a, &v6->sin6_addr, 16);
        e->port = ntohs(v6->sin6_port);
    }
}

static int is_mapped(const unsigned char *a) {
    static const unsigned char mapped[12] = {0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0xff, 0xff};
    return memcmp(a, mapped, 12) == 0;
}

static int endpoint_is_loopback(const struct endpoint *e) {
    static const unsigned char lo6[16] = {0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 1};
    return memcmp(e->a, lo6, 16) == 0 || (is_mapped(e->a) && e->a[12] == 127);
}

/* A listener at l holds dest: an exact address, 0.0.0.0 for an IPv4 (or v4-mapped) destination, or [::]
 * for an IPv6 one. [::] does not count for IPv4: a v6-only socket there leaves 127.0.0.1 free for anyone,
 * and /proc does not show IPV6_V6ONLY. */
static int listener_holds(const struct endpoint *l, const struct endpoint *dest) {
    static const unsigned char any6[16] = {0};
    static const unsigned char mapped_any[16] = {0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0xff, 0xff, 0, 0, 0, 0};
    if (l->port != dest->port) return 0;
    if (memcmp(l->a, dest->a, 16) == 0) return 1;
    return memcmp(l->a, is_mapped(dest->a) ? mapped_any : any6, 16) == 0;
}

static int hexval(char c) {
    return c >= '0' && c <= '9' ? c - '0' : c >= 'A' && c <= 'F' ? c - 'A' + 10 : c >= 'a' && c <= 'f' ? c - 'a' + 10 : -1;
}

static const char *hex_u32(const char *p, unsigned digits, unsigned long *out) {
    unsigned long v = 0;
    for (unsigned k = 0; k < digits; k++, p++) {
        int h = hexval(*p);
        if (h < 0) return NULL;
        v = v << 4 | (unsigned long)h;
    }
    *out = v;
    return p;
}

/* "0100007F:24E3" (tcp) or 32 hex digits ":24E3" (tcp6): each 8-digit group is a 32-bit word of the
 * address as it sits in memory, printed as a native integer; the port is in host order. */
static const char *parse_endpoint(const char *p, int six, struct endpoint *e) {
    unsigned long w, port;
    uint32_t word;
    memset(e, 0, sizeof *e);
    while (*p == ' ') p++;
    for (int k = 0; k < (six ? 4 : 1); k++) {
        if (!(p = hex_u32(p, 8, &w))) return NULL;
        word = (uint32_t)w;
        memcpy(six ? &e->a[4 * k] : &e->a[12], &word, 4);
    }
    if (!six) e->a[10] = e->a[11] = 0xff;
    if (*p++ != ':' || !(p = hex_u32(p, 4, &port))) return NULL;
    e->port = (unsigned short)port;
    return p;
}

static void match_line(const char *line, int six, struct query *q) {
    struct endpoint loc, rem;
    unsigned long st, uid = 0;
    const char *p = strchr(line, ':');   /* after "sl:" */
    if (!p || !(p = parse_endpoint(p + 1, six, &loc)) || !(p = parse_endpoint(p, six, &rem))) return;
    while (*p == ' ') p++;
    if (!(p = hex_u32(p, 2, &st))) return;
    for (int field = 0; field < 3; field++) {   /* tx:rx, tr:tm, retrnsmt */
        while (*p == ' ') p++;
        while (*p && *p != ' ') p++;
    }
    while (*p == ' ') p++;
    if (*p < '0' || *p > '9') return;
    while (*p >= '0' && *p <= '9') uid = uid * 10 + (unsigned long)(*p++ - '0');
    if (q->want_listener) {
        /* The kernel prints every listener before any connection (older kernels interleave half-open
         * ones, SYN_RECV): the first other row ends the listening part. */
        if (st != TCP_LISTEN && st != TCP_SYN_RECV) q->done = 1;
        else if (st == TCP_LISTEN && uid == q->me && listener_holds(&loc, &q->local)) q->own = q->done = 1;
    } else if (loc.port == q->peer.port && rem.port == q->local.port
               && memcmp(loc.a, q->peer.a, 16) == 0 && memcmp(rem.a, q->local.a, 16) == 0) {
        q->uid = (long)uid;
        q->state = st;
        q->done = 1;
    }
}

/* Read the table until the query is answered. 0 if it cannot be read. Reads of a page at a time: the
 * kernel fills at most a page per read(), and each read() restarts its walk of the socket hash. */
static int scan_table(const char *path, int six, struct query *q) {
    char chunk[TABLE_CHUNK], line[TABLE_LINE];
    size_t len = 0;
    int fd = open(path, O_RDONLY | O_CLOEXEC);
    if (fd < 0) return 0;
    q->me = my_uid();
    while (!q->done) {
        ssize_t r = read(fd, chunk, sizeof chunk);
        if (r < 0 && errno == EINTR) continue;
        if (r <= 0) break;
        for (ssize_t k = 0; k < r && !q->done; k++) {
            if (chunk[k] == '\n' || len + 1 >= sizeof line) {
                line[len] = '\0'; match_line(line, six, q); len = 0;
                if (chunk[k] == '\n') continue;
            }
            line[len++] = chunk[k];
        }
    }
    if (len && !q->done) { line[len] = '\0'; match_line(line, six, q); }
    close(fd);
    return 1;
}

/* The uid of the socket at `peer` connected to `local`, and its state; -1 if it is in neither table.
 * `six_first`: read tcp6 first (where the listener that took the connection is). */
static long peer_row(const struct endpoint *peer, const struct endpoint *local, int six_first, unsigned long *state) {
    struct query q;
    for (int k = 0; k < 2; k++) {
        int six = k ? !six_first : six_first;
        memset(&q, 0, sizeof q);
        q.peer = *peer; q.local = *local; q.uid = -1;
        scan_table(six ? HPCOP_PROC_NET_TCP6 : HPCOP_PROC_NET_TCP, six, &q);
        if (q.uid >= 0) {
            if (state) *state = q.state;
            return q.uid;
        }
    }
    return -1;
}

static long peer_uid(const struct endpoint *peer, const struct endpoint *local) { return peer_row(peer, local, 0, NULL); }

/* 1 if a listener of this uid holds dest (listener_holds); *in6 (if given) says it is in tcp6. */
static int own_listener(const struct endpoint *dest, int *in6) {
    struct query q;
    for (int six = 0; six < 2; six++) {
        if (!six && !is_mapped(dest->a)) continue;   /* an IPv6 destination: only tcp6 can hold it */
        memset(&q, 0, sizeof q);
        q.want_listener = 1; q.local = *dest;
        scan_table(six ? HPCOP_PROC_NET_TCP6 : HPCOP_PROC_NET_TCP, six, &q);
        if (q.own) {
            if (in6) *in6 = six;
            return 1;
        }
    }
    return 0;
}

/* ---- accept() ---- */

/* 1 if a connection accepted on `local` is one the owner check covers: every loopback listener of ours but a
 * public port's (8443 on 0.0.0.0 also takes loopback connections, as any other node may connect to it). */
static int accept_checked(const struct endpoint *local) { return endpoint_is_loopback(local) && !is_public(local->port); }

/* An accepted connection we keep: anything accept_checked() leaves out, or a peer of our uid. */
static int accepted_ok(int c) {
    struct sockaddr_storage me, peer;
    struct endpoint em, ep;
    socklen_t n = sizeof me, m = sizeof peer;
    if (syscall(SYS_getsockname, c, &me, &n) != 0 || (me.ss_family != AF_INET && me.ss_family != AF_INET6)) return 1;
    endpoint_of(&me, &em);
    if (!accept_checked(&em)) return 1;
    if (syscall(SYS_getpeername, c, &peer, &m) != 0) return 0;
    endpoint_of(&peer, &ep);
    return peer_uid(&ep, &em) == (long)my_uid();
}

static int filtered_accept(int fd, struct sockaddr *addr, socklen_t *len, int flags, int four) {
    int saved = errno;
    socklen_t room = len ? *len : 0;
    long fl;
    for (int dropped = 0; dropped < ACCEPT_MAX_DROPS; dropped++) {
        long c;
        if (len) *len = room;
        c = four ? syscall(SYS_accept4, fd, addr, len, flags) : syscall(SYS_accept, fd, addr, len);
        if (c < 0) return (int)c;
        if (accepted_ok((int)c)) { errno = saved; return (int)c; }
        syscall(SYS_close, (int)c);   /* another user's connection: drop it, take the next one */
    }
    /* A flood: give the caller its loop back. epmd (both ERTS) goes back to select() on EAGAIN and on
     * ECONNABORTED; the Erlang VM ignores EAGAIN on its non-blocking listeners; memcached and moxi log
     * and wait for the next event. */
    fl = syscall(SYS_fcntl, fd, F_GETFL);
    errno = fl >= 0 && (fl & O_NONBLOCK) ? EAGAIN : ECONNABORTED;
    return -1;
}

int accept(int fd, struct sockaddr *addr, socklen_t *len) { return filtered_accept(fd, addr, len, 0, 0); }
int accept4(int fd, struct sockaddr *addr, socklen_t *len, int flags) { return filtered_accept(fd, addr, len, flags, 1); }

/* ---- connect() ---- */

/* 1 if a connect() to dest is one the owner check covers: to loopback, on a port another user could bind. */
static int connect_checked(const struct endpoint *dest) { return endpoint_is_loopback(dest) && owner_port(dest->port); }

/* 1 if a connect() of fd to addr is one the owner check covers: TCP, and connect_checked(). */
static int owner_checked(int fd, const struct sockaddr *addr, struct endpoint *dest) {
    struct sockaddr_storage s;
    int type = 0;
    socklen_t n = sizeof type;
    memset(&s, 0, sizeof s);
    memcpy(&s, addr, addr->sa_family == AF_INET ? sizeof(struct sockaddr_in) : sizeof(struct sockaddr_in6));
    endpoint_of(&s, dest);
    if (!connect_checked(dest)) return 0;
    return syscall(SYS_getsockopt, fd, SOL_SOCKET, SO_TYPE, &type, &n) == 0 && type == SOCK_STREAM;
}

static void sleep_ms(long ms) {
    struct timespec t = {ms / 1000, (ms % 1000) * 1000000L};
    syscall(SYS_nanosleep, &t, NULL);
}

/* Before the connect: a listener of ours holds dest. Our epmd may still be starting: wait for it. */
static int own_listener_wait(const struct endpoint *dest, int *in6) {
    int waits = dest->port == epmd_port() ? EPMD_WAIT_MS / WAIT_STEP_MS : 0;
#ifdef HPCOP_TEST_ASSUME_OWN_LISTENER   /* test builds only: no check before the connect, to test the one after */
    (void)waits;
    if (in6) *in6 = 0;
    return 1;
#endif
    for (;;) {
        if (own_listener(dest, in6)) return 1;
        if (waits-- <= 0) return 0;
        sleep_ms(WAIT_STEP_MS);
    }
}

/* After the connect: the socket at the other end of fd (at dest) is this uid's, or it waits in the accept
 * queue of a listener of this uid. The listener check comes first, then the row: a listener of ours that
 * holds dest now means no other uid's listener did since that row was queued (see the top). A row that
 * is not found is looked up once more: a table read a page at a time can miss a row that moves. */
static int peer_is_ours(int fd, const struct endpoint *dest) {
    struct sockaddr_storage me;
    struct endpoint em;
    socklen_t n = sizeof me;
    unsigned long state = 0;
    int in6 = 0, held;
    long uid;
    if (syscall(SYS_getsockname, fd, &me, &n) != 0 || (me.ss_family != AF_INET && me.ss_family != AF_INET6)) return 0;
    endpoint_of(&me, &em);
    held = own_listener(dest, &in6);
    uid = peer_row(dest, &em, in6, &state);
    if (uid < 0) uid = peer_row(dest, &em, in6, &state);
    if (uid == (long)my_uid()) return 1;
    return uid >= 0 && no_owner_shown(uid) && (state == TCP_ESTABLISHED || state == TCP_SYN_RECV) && held;
}

static void abort_connection(int fd) {   /* AF_UNSPEC dissolves the connection (a reset); fd stays the caller's */
    struct sockaddr sa;
    memset(&sa, 0, sizeof sa);
    sa.sa_family = AF_UNSPEC;
    syscall(SYS_connect, fd, &sa, sizeof sa);
}

/* connect() of fd to addr (already rewritten by the shim); saved: the caller's errno. */
static long checked_connect(int fd, const struct sockaddr *addr, socklen_t len, int saved) {
    struct endpoint dest;
    int in6 = 0;
    long r;
    if (!owner_checked(fd, addr, &dest)) { errno = saved; return syscall(SYS_connect, fd, addr, len); }
    if (!own_listener_wait(&dest, &in6)) { errno = ECONNREFUSED; return -1; }
    errno = saved;
    r = syscall(SYS_connect, fd, addr, len);
    if (r == 0 || errno == EISCONN) {   /* connected: check the other end before the caller uses it */
        int e = errno;
        if (!peer_is_ours(fd, &dest)) { abort_connection(fd); errno = ECONNREFUSED; return -1; }
        errno = r == 0 ? saved : e;
    }
    return r;   /* EINPROGRESS: checked when the caller reads the result, below */
}

/* getsockopt(SO_ERROR) is how a non-blocking connect learns its result. When it says "connected" and
 * the socket is ours on a loopback port the owner check covers, check the other end now; if it is not this uid's, reset
 * the connection and report ECONNREFUSED instead. */
int getsockopt(int fd, int level, int name, void *val, socklen_t *len) {
    long r = syscall(SYS_getsockopt, fd, level, name, val, len);
    if (r == 0 && level == SOL_SOCKET && name == SO_ERROR && val && len && *len >= (socklen_t)sizeof(int)
        && *(int *)val == 0) {
        int saved = errno, e = 0;
        struct sockaddr_storage peer;
        struct endpoint dest;
        socklen_t n = sizeof peer, m = sizeof e;
        if (syscall(SYS_getpeername, fd, &peer, &n) == 0) {
            if ((peer.ss_family == AF_INET || peer.ss_family == AF_INET6)
                && owner_checked(fd, (const struct sockaddr *)&peer, &dest)) {
                if (!peer_is_ours(fd, &dest)) {
                    abort_connection(fd);
                    *(int *)val = ECONNREFUSED;
                }
            }
        } else if (errno == ENOTCONN && syscall(SYS_getsockopt, fd, SOL_SOCKET, SO_ERROR, &e, &m) == 0 && e) {
            *(int *)val = e;   /* it was reset between the two reads: report that, not "connected" */
        }
        errno = saved;
    }
    return (int)r;
}
