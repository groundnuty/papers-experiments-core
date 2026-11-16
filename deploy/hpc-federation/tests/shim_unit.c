/* shim_unit.c: unit tests of the port shim's decisions and of its file parsers.
 * Built with -DHPCOP_PEERS_FILE, -DHPCOP_ERLANG_ENV, -DHPCOP_PROC_NET_TCP, -DHPCOP_PROC_NET_TCP6 and
 * -DHPCOP_UNPRIV_START_FILE set to test files, and -DHPCOP_TEST_UID=1000; includes the shim source itself, so bind()/connect()/accept()
 * in this binary are the shim's own. No packet leaves the host: the decision functions are called, and a
 * few loopback sockets check bind() and listen() themselves. The host's own addresses are a fixed test table (10.20.30.40 and
 * 2001:db8::77), so the "an address of this host" cases do not depend on the machine. */
#define _GNU_SOURCE   /* before any system header, as in the shim itself (program_invocation_short_name) */
#include <stdio.h>
#include <stdlib.h>
#include "../bundle/portshim.c"

static int failures;

static void expect(const char *what, const char *got, const char *want) {
    int ok = strcmp(got, want) == 0;
    printf("%s %-62s got %s want %s\n", ok ? "ok:  " : "FAIL:", what, got, want);
    if (!ok) failures++;
}

static void expect_n(const char *what, unsigned got, unsigned want) {
    char g[16], w[16];
    snprintf(g, sizeof g, "%u", got); snprintf(w, sizeof w, "%u", want);
    expect(what, g, w);
}

static const char *plan_name(int plan) {
    static char e[32];
    if (plan == BIND_GO) return "";
    if (plan == BIND_DEFER) return " (deferred: left unbound)";
    snprintf(e, sizeof e, " (refused: %s)", plan == EADDRNOTAVAIL ? "EADDRNOTAVAIL" : plan == EADDRINUSE ? "EADDRINUSE" : "other");
    return e;
}

/* "addr:port" after the shim, for a bind (TCP when stream) or a connect; a deferred or refused bind says so */
static const char *v4s(const char *ip, unsigned short port, int is_connect, int stream) {
    static char out[96];
    struct in_addr a;
    char s[INET_ADDRSTRLEN];
    int plan = BIND_GO;
    if (inet_pton(AF_INET, ip, &a) != 1) { fprintf(stderr, "bad test address %s\n", ip); exit(2); }
    if (is_connect) rewrite_connect4(&a, &port);
    else plan = plan_bind4(&a, &port, stream);
    inet_ntop(AF_INET, &a, s, sizeof s);
    snprintf(out, sizeof out, "%s:%u%s", s, port, plan_name(plan));
    return out;
}

static const char *v4(const char *ip, unsigned short port, int is_connect) { return v4s(ip, port, is_connect, 1); }
static const char *udp4(const char *ip, unsigned short port) { return v4s(ip, port, 0, 0); }

static const char *v6s(const char *ip, unsigned short port, int is_connect, int only, int stream) {  /* only: IPV6_V6ONLY */
    static char out[112];
    struct in6_addr a;
    char s[INET6_ADDRSTRLEN];
    int plan = BIND_GO;
    int fd = socket(AF_INET6, stream ? SOCK_STREAM : SOCK_DGRAM, 0);
    if (fd < 0 || setsockopt(fd, IPPROTO_IPV6, IPV6_V6ONLY, &only, sizeof only) != 0) { perror("ipv6 socket"); exit(2); }
    if (inet_pton(AF_INET6, ip, &a) != 1) { fprintf(stderr, "bad test address %s\n", ip); exit(2); }
    if (is_connect) rewrite_connect6(&a, &port);
    else plan = plan_bind6(fd, &a, &port, stream);
    close(fd);
    inet_ntop(AF_INET6, &a, s, sizeof s);
    snprintf(out, sizeof out, "[%s]:%u%s", s, port, plan_name(plan));
    return out;
}

static const char *v6(const char *ip, unsigned short port, int is_connect, int only) { return v6s(ip, port, is_connect, only, 1); }

static struct endpoint ep(const char *ip, unsigned short port) {
    struct sockaddr_storage s;
    struct endpoint e;
    memset(&s, 0, sizeof s);
    if (strchr(ip, ':')) {
        struct sockaddr_in6 *v6 = (struct sockaddr_in6 *)&s;
        v6->sin6_family = AF_INET6; v6->sin6_port = htons(port);
        if (inet_pton(AF_INET6, ip, &v6->sin6_addr) != 1) { fprintf(stderr, "bad test address %s\n", ip); exit(2); }
    } else {
        struct sockaddr_in *v4 = (struct sockaddr_in *)&s;
        v4->sin_family = AF_INET; v4->sin_port = htons(port);
        if (inet_pton(AF_INET, ip, &v4->sin_addr) != 1) { fprintf(stderr, "bad test address %s\n", ip); exit(2); }
    }
    endpoint_of(&s, &e);
    return e;
}

static long uid_of(const char *peer, unsigned short pp, const char *local, unsigned short lp) {
    struct endpoint a = ep(peer, pp), b = ep(local, lp);
    return peer_uid(&a, &b);
}

static unsigned own_at(const char *ip, unsigned short port) {
    struct endpoint d = ep(ip, port);
    return (unsigned)own_listener(&d, NULL);
}

static void write_file(const char *path, const char *text) {
    FILE *f = fopen(path, "w");
    if (!f) { perror(path); exit(2); }
    fputs(text, f);
    fclose(f);
}

static void owner_tests(void) {
    /* /proc/net/tcp and tcp6 as the kernel prints them: every listener first, then the connections. This
     * process is uid 1000 (HPCOP_TEST_UID). The last tcp row is a listener printed after a connection,
     * which the kernel never does: it shows that the listener check reads only the listening part. */
    write_file(HPCOP_PROC_NET_TCP,
        "  sl  local_address rem_address   st tx_queue rx_queue tr tm->when retrnsmt   uid  timeout inode\n"
        "   0: 0100007F:24E3 00000000:0000 0A 00000000:00000000 00:00000000 00000000  1000        0 101 1 0000000000000000 100 0 0 10 0\n"
        "   1: 00000000:1F9B 00000000:0000 0A 00000000:00000000 00:00000000 00000000  4321        0 104 1 0000000000000000 100 0 0 10 0\n"
        "   2: 281E140A:2BCA 00000000:0000 0A 00000000:00000000 00:00000000 00000000  4321        0 105 1 0000000000000000 100 0 0 10 0\n"
        "   3: 0200007F:2BC9 00000000:0000 0A 00000000:00000000 00:00000000 00000000  4321        0 106 1 0000000000000000 100 0 0 10 0\n"
        "   4: 00000000:52D2 00000000:0000 0A 00000000:00000000 00:00000000 00000000  1000        0 107 1 0000000000000000 100 0 0 10 0\n"
        "   5: 0100007F:9C40 0100007F:24E3 01 00000000:00000000 00:00000000 00000000  1000        0 102 1 0000000000000000 20 4 30 10 -1\n"
        "   6: 0100007F:9C41 0100007F:24E3 01 00000000:00000000 00:00000000 00000000  4321        0 103 1 0000000000000000 20 4 30 10 -1\n"
        "   7: 0100007F:9C42 0100007F:24E3 06 00000000:00000000 03:00001770 00000000     0        0 0 3 0000000000000000\n"
        "   8: 0100007F:52D3 00000000:0000 0A 00000000:00000000 00:00000000 00000000  1000        0 108 1 0000000000000000 100 0 0 10 0\n");
    write_file(HPCOP_PROC_NET_TCP6,
        "  sl  local_address                         remote_address                        st tx_queue rx_queue tr tm->when retrnsmt   uid  timeout inode\n"
        "   0: 00000000000000000000000000000000:5361 00000000000000000000000000000000:0000 0A 00000000:00000000 00:00000000 00000000  4321        0 201 1 0000000000000000 100 0 0 10 0\n"
        "   1: 00000000000000000000000001000000:270E 00000000000000000000000000000000:0000 0A 00000000:00000000 00:00000000 00000000  1000        0 203 1 0000000000000000 100 0 0 10 0\n"
        "   2: 00000000000000000000000000000000:52D0 00000000000000000000000000000000:0000 0A 00000000:00000000 00:00000000 00000000  1000        0 204 1 0000000000000000 100 0 0 10 0\n"
        "   3: 0000000000000000FFFF00000100007F:52D1 00000000000000000000000000000000:0000 0A 00000000:00000000 00:00000000 00000000  1000        0 205 1 0000000000000000 100 0 0 10 0\n"
        "   4: 0000000000000000FFFF00000100007F:9C43 0000000000000000FFFF00000100007F:24E3 01 00000000:00000000 00:00000000 00000000  1000        0 202 1 0000000000000000 20 4 30 10 -1\n");
    puts("# owner check: who is at the other end of a loopback connection");
    expect_n("peer 127.0.0.1:40000 -> 127.0.0.1:9443 is ours (uid 1000)", (unsigned)uid_of("127.0.0.1", 40000, "127.0.0.1", 9443), 1000);
    expect_n("peer 127.0.0.1:40001 -> 127.0.0.1:9443 is uid 4321", (unsigned)uid_of("127.0.0.1", 40001, "127.0.0.1", 9443), 4321);
    expect_n("peer in TIME_WAIT shows uid 0", (unsigned)uid_of("127.0.0.1", 40002, "127.0.0.1", 9443), 0);
    expect_n("peer in neither table: not found (-1 + 1 = 0)", (unsigned)(uid_of("127.0.0.1", 40009, "127.0.0.1", 9443) + 1), 0);
    expect_n("v4-mapped peer found in tcp6 (uid 1000)", (unsigned)uid_of("::ffff:127.0.0.1", 40003, "::ffff:127.0.0.1", 9443), 1000);
    expect_n("v4 peer matched against a v4-mapped local (uid 1000)", (unsigned)uid_of("127.0.0.1", 40000, "::ffff:127.0.0.1", 9443), 1000);
    expect_n("uid 0 is 'no owner shown' (a queued or closed socket)", (unsigned)no_owner_shown(0), 1);
    expect_n("the overflow uid is 'no owner shown' (inside a user namespace)", (unsigned)no_owner_shown(65534), 1);
    expect_n("uid 1000 is an owner", (unsigned)no_owner_shown(1000), 0);
    puts("# owner check: a connect goes ahead only where a listener of ours holds the address (1) or not (0)");
    expect_n("127.0.0.1:9443: our listener on 127.0.0.1", own_at("127.0.0.1", 9443), 1);
    expect_n("127.0.0.1:8091: only uid 4321, on 0.0.0.0", own_at("127.0.0.1", 8091), 0);
    expect_n("127.0.0.1:11210: only uid 4321, on 10.20.30.40", own_at("127.0.0.1", 11210), 0);
    expect_n("127.0.0.1:21345: only uid 4321, on [::] (a fake epmd)", own_at("127.0.0.1", 21345), 0);
    expect_n("127.0.0.1:11209: nobody (uid 4321 is on 127.0.0.2)", own_at("127.0.0.1", 11209), 0);
    expect_n("127.0.0.1:12345: nobody at all (before our listener binds)", own_at("127.0.0.1", 12345), 0);
    expect_n("[::1]:9998: our listener on ::1", own_at("::1", 9998), 1);
    expect_n("127.0.0.1:9998: ours only on ::1, not here", own_at("127.0.0.1", 9998), 0);
    expect_n("127.0.0.1:21202: our listener on 0.0.0.0", own_at("127.0.0.1", 21202), 1);
    expect_n("127.0.0.2:21202: our listener on 0.0.0.0", own_at("127.0.0.2", 21202), 1);
    expect_n("127.0.0.1:21200: ours on [::] only (may be v6-only): no", own_at("127.0.0.1", 21200), 0);
    expect_n("[::1]:21200: ours on [::]", own_at("::1", 21200), 1);
    expect_n("127.0.0.1:21201: ours on [::ffff:127.0.0.1]", own_at("127.0.0.1", 21201), 1);
    expect_n("[::ffff:127.0.0.1]:21201: ours there", own_at("::ffff:127.0.0.1", 21201), 1);
    expect_n("127.0.0.1:21203: a listener row after the connections is not read", own_at("127.0.0.1", 21203), 0);
    {
        struct query q;
        memset(&q, 0, sizeof q);
        expect_n("an unreadable table is reported as such", (unsigned)scan_table("/nonexistent/hpcop/tcp", 0, &q), 0);
    }
}

static unsigned short parse_epmd(const char *line) {
    char buf[MAX_LINE];
    snprintf(buf, sizeof buf, "%s", line);
    epmd_job_port = 0;
    add_epmd(buf);
    return epmd_job_port;
}

int main(void) {
    setvbuf(stdout, NULL, _IOLBF, 0);   /* the shim's own notes go to stderr: keep the lines whole */
    FILE *f = fopen(HPCOP_PEERS_FILE, "w");
    if (!f) { perror("peers file"); return 2; }
    fputs("# hpcop peers\n10.1.2.3\n   10.9.9.9   # zone, with a trailing comment\r\n\n2001:db8::5\n"
          "not-an-address\nzone.example.test\n"
          "1111111111111111111111111111111111111111111111111111111111111111111111111111.1.1.1\n"
          "192.0.2.44", f);  /* last line without a newline */
    fclose(f);
    f = fopen(HPCOP_ERLANG_ENV, "w");
    if (!f) { perror("erlang env file"); return 2; }
    fputs("# hpcop: Erlang settings for every VM\nERL_EPMD_ADDRESS=127.0.0.1\nERL_EPMD_PORT=21345\n", f);
    fclose(f);
    /* this host's interfaces, for the test: loopback, 10.20.30.40, ::1 and 2001:db8::77 */
    inet_pton(AF_INET, "127.0.0.1", &local4[0]); inet_pton(AF_INET, "10.20.30.40", &local4[1]); nlocal4 = 2;
    inet_pton(AF_INET6, "::1", &local6[0]); inet_pton(AF_INET6, "2001:db8::77", &local6[1]); nlocal6 = 2;
    publish(&local_state);

    write_file(HPCOP_UNPRIV_START_FILE, "1024\n");   /* the kernel's default ip_unprivileged_port_start */

    puts("# 80/443 and the peer list");
    expect("bind 0.0.0.0:443 -> 8443, address kept (public)", v4("0.0.0.0", 443, 0), "0.0.0.0:8443");
    expect("bind 0.0.0.0:80 -> 127.0.0.1:8080 (80 is private here)", v4("0.0.0.0", 80, 0), "127.0.0.1:8080");
    expect("bind [::]:443 -> 8443, address kept", v6("::", 443, 0, 0), "[::]:8443");
    expect("connect 127.0.0.1:443 (local) -> 8443", v4("127.0.0.1", 443, 1), "127.0.0.1:8443");
    expect("connect 127.0.0.1:80 (local) -> 8080", v4("127.0.0.1", 80, 1), "127.0.0.1:8080");
    expect("connect own 10.20.30.40:80 -> 127.0.0.1:8080", v4("10.20.30.40", 80, 1), "127.0.0.1:8080");
    expect("connect own 10.20.30.40:443 -> 8443, address kept", v4("10.20.30.40", 443, 1), "10.20.30.40:8443");
    expect("connect [::1]:443 (local) -> 8443", v6("::1", 443, 1, 0), "[::1]:8443");
    expect("connect peer 10.1.2.3:443 -> 8443", v4("10.1.2.3", 443, 1), "10.1.2.3:8443");
    expect("connect peer 10.9.9.9:443 (comment stripped) -> 8443", v4("10.9.9.9", 443, 1), "10.9.9.9:8443");
    expect("connect peer 192.0.2.44:443 (no final newline) -> 8443", v4("192.0.2.44", 443, 1), "192.0.2.44:8443");
    expect("connect peer [2001:db8::5]:443 -> 8443", v6("2001:db8::5", 443, 1, 0), "[2001:db8::5]:8443");
    expect("connect peer [::ffff:10.1.2.3]:443 (v4-mapped) -> 8443", v6("::ffff:10.1.2.3", 443, 1, 0), "[::ffff:10.1.2.3]:8443");
    expect("connect peer 10.1.2.3:80 untouched (peers: 443 only)", v4("10.1.2.3", 80, 1), "10.1.2.3:80");
    expect("connect peer 10.1.2.3:6665 (rtransfer) untouched", v4("10.1.2.3", 6665, 1), "10.1.2.3:6665");
    expect("connect peer 10.1.2.3:9443 untouched (not this host)", v4("10.1.2.3", 9443, 1), "10.1.2.3:9443");
    expect("connect non-peer 203.0.113.7:443 untouched", v4("203.0.113.7", 443, 1), "203.0.113.7:443");
    expect("connect non-peer [2001:db8::6]:443 untouched", v6("2001:db8::6", 443, 1, 0), "[2001:db8::6]:443");

    puts("# default-deny bind: the public TCP ports keep the node's address");
    expect("bind 0.0.0.0:8443 (public) untouched", v4("0.0.0.0", 8443, 0), "0.0.0.0:8443");
    expect("bind 0.0.0.0:6665 (rtransfer, public) untouched", v4("0.0.0.0", 6665, 0), "0.0.0.0:6665");
    expect("bind own 10.20.30.40:8443 (public) untouched", v4("10.20.30.40", 8443, 0), "10.20.30.40:8443");
    expect("bind own 10.20.30.40:6665 (public) untouched", v4("10.20.30.40", 6665, 0), "10.20.30.40:6665");
    expect("bind [::]:6665 (public) untouched", v6("::", 6665, 0, 0), "[::]:6665");
    expect("bind [::]:8443, IPV6_V6ONLY (public) untouched", v6("::", 8443, 0, 1), "[::]:8443");
    expect("UDP bind 0.0.0.0:8443: public is TCP only -> 127.0.0.1", udp4("0.0.0.0", 8443), "127.0.0.1:8443");
    expect("UDP bind 0.0.0.0:6665: public is TCP only -> 127.0.0.1", udp4("0.0.0.0", 6665), "127.0.0.1:6665");

    puts("# default-deny bind: every other port goes to loopback, listed once or not");
    expect("bind 0.0.0.0:9443 (onepanel) -> 127.0.0.1", v4("0.0.0.0", 9443, 0), "127.0.0.1:9443");
    expect("bind 0.0.0.0:8091 (Couchbase REST) -> 127.0.0.1", v4("0.0.0.0", 8091, 0), "127.0.0.1:8091");
    expect("bind 0.0.0.0:8092 (Couchbase views) -> 127.0.0.1", v4("0.0.0.0", 8092, 0), "127.0.0.1:8092");
    expect("bind 0.0.0.0:11209 (memcached dedicated) -> 127.0.0.1", v4("0.0.0.0", 11209, 0), "127.0.0.1:11209");
    expect("bind 0.0.0.0:11210 (memcached) -> 127.0.0.1", v4("0.0.0.0", 11210, 0), "127.0.0.1:11210");
    expect("bind 0.0.0.0:11211 (moxi) -> 127.0.0.1", v4("0.0.0.0", 11211, 0), "127.0.0.1:11211");
    expect("bind 0.0.0.0:21100 (Couchbase dist, first) -> 127.0.0.1", v4("0.0.0.0", 21100, 0), "127.0.0.1:21100");
    expect("bind 0.0.0.0:21299 (Couchbase dist, last) -> 127.0.0.1", v4("0.0.0.0", 21299, 0), "127.0.0.1:21299");
    expect("bind 0.0.0.0:9100 (Onedata dist, first) -> 127.0.0.1", v4("0.0.0.0", 9100, 0), "127.0.0.1:9100");
    expect("bind 0.0.0.0:9139 (Onedata dist, last) -> 127.0.0.1", v4("0.0.0.0", 9139, 0), "127.0.0.1:9139");
    expect("bind 0.0.0.0:9140 (unlisted before) -> 127.0.0.1", v4("0.0.0.0", 9140, 0), "127.0.0.1:9140");
    expect("bind 0.0.0.0:21099 (unlisted before) -> 127.0.0.1", v4("0.0.0.0", 21099, 0), "127.0.0.1:21099");
    expect("bind 0.0.0.0:40851 (ns_couchdb's distribution, live run 6) -> 127.0.0.1", v4("0.0.0.0", 40851, 0), "127.0.0.1:40851");
    expect("bind own 10.20.30.40:9443 -> 127.0.0.1", v4("10.20.30.40", 9443, 0), "127.0.0.1:9443");
    expect("bind own 10.20.30.40:45925 -> 127.0.0.1", v4("10.20.30.40", 45925, 0), "127.0.0.1:45925");
    expect("bind a non-local 203.0.113.7:9443 -> 127.0.0.1", v4("203.0.113.7", 9443, 0), "127.0.0.1:9443");
    expect("bind 127.0.0.1:8091 stays", v4("127.0.0.1", 8091, 0), "127.0.0.1:8091");
    expect("bind 127.0.0.2:40000 (loopback) stays", v4("127.0.0.2", 40000, 0), "127.0.0.2:40000");
    expect("bind [::]:8091, dual-stack -> [::ffff:127.0.0.1]", v6("::", 8091, 0, 0), "[::ffff:127.0.0.1]:8091");
    expect("bind [::]:8091, IPV6_V6ONLY -> [::1]", v6("::", 8091, 0, 1), "[::1]:8091");
    expect("bind [::]:40000, dual-stack (unlisted before) -> [::ffff:127.0.0.1]", v6("::", 40000, 0, 0), "[::ffff:127.0.0.1]:40000");
    expect("bind [::]:40000, IPV6_V6ONLY (unlisted before) -> [::1]", v6("::", 40000, 0, 1), "[::1]:40000");
    expect("bind [::ffff:0.0.0.0]:9443 -> [::ffff:127.0.0.1]", v6("::ffff:0.0.0.0", 9443, 0, 0), "[::ffff:127.0.0.1]:9443");
    expect("bind own [2001:db8::77]:9443 -> [::1]", v6("2001:db8::77", 9443, 0, 1), "[::1]:9443");
    expect("bind [fe80::1]:40000 (link-local) -> [::1]", v6("fe80::1", 40000, 0, 1), "[::1]:40000");

    puts("# default-deny bind: port 0 (ephemeral)");
    expect("TCP bind 0.0.0.0:0 (moxi's -p 0, or a client): deferred to listen() or connect()", v4("0.0.0.0", 0, 0), "0.0.0.0:0 (deferred: left unbound)");
    expect("TCP bind [::]:0, dual-stack: deferred", v6("::", 0, 0, 0), "[::]:0 (deferred: left unbound)");
    expect("TCP bind [::]:0, IPV6_V6ONLY (moxi's IPv6 -p 0): deferred, ::1 usable here", v6("::", 0, 0, 1), "[::]:0 (deferred: left unbound)");
    expect("TCP bind [::ffff:0.0.0.0]:0: deferred", v6("::ffff:0.0.0.0", 0, 0, 0), "[::ffff:0.0.0.0]:0 (deferred: left unbound)");
    expect("TCP bind own 10.20.30.40:0 -> 127.0.0.1:0", v4("10.20.30.40", 0, 0), "127.0.0.1:0");
    expect("TCP bind 127.0.0.1:0 stays", v4("127.0.0.1", 0, 0), "127.0.0.1:0");
    expect("UDP bind 0.0.0.0:0 (inet_res, a client that connects): deferred", udp4("0.0.0.0", 0), "0.0.0.0:0 (deferred: left unbound)");
    expect("UDP bind own 10.20.30.40:0 -> 127.0.0.1:0", udp4("10.20.30.40", 0), "127.0.0.1:0");

    puts("# default-deny bind: UDP servers, and multicast groups");
    expect("UDP bind 0.0.0.0:8053 (erldns) -> 127.0.0.1", udp4("0.0.0.0", 8053), "127.0.0.1:8053");
    expect("UDP bind 0.0.0.0:4475 -> 127.0.0.1", udp4("0.0.0.0", 4475), "127.0.0.1:4475");
    expect("UDP bind 0.0.0.0:59077 (an explicit port: a server) -> 127.0.0.1", udp4("0.0.0.0", 59077), "127.0.0.1:59077");
    expect("UDP bind [::]:8053, IPV6_V6ONLY -> [::1]", v6s("::", 8053, 0, 1, 0), "[::1]:8053");
    expect("UDP bind 224.0.0.1:4475 (nodefinder's group, live run 6) -> refused", udp4("224.0.0.1", 4475), "224.0.0.1:4475 (refused: EADDRNOTAVAIL)");
    expect("UDP bind 239.1.2.3:5000 (any IPv4 group) -> refused", udp4("239.1.2.3", 5000), "239.1.2.3:5000 (refused: EADDRNOTAVAIL)");
    expect("UDP bind [::ffff:224.0.0.1]:4475 -> refused", v6s("::ffff:224.0.0.1", 4475, 0, 0, 0), "[::ffff:224.0.0.1]:4475 (refused: EADDRNOTAVAIL)");
    expect("UDP bind [ff02::1]:4475 (an IPv6 group) -> refused", v6s("ff02::1", 4475, 0, 1, 0), "[ff02::1]:4475 (refused: EADDRNOTAVAIL)");
    expect("TCP bind 224.0.0.1:4475 -> refused", v4("224.0.0.1", 4475, 0), "224.0.0.1:4475 (refused: EADDRNOTAVAIL)");

    puts("# connect to this host: every port another user could bind goes to loopback");
    expect("connect own 10.20.30.40:8091 -> 127.0.0.1", v4("10.20.30.40", 8091, 1), "127.0.0.1:8091");
    expect("connect own 10.20.30.40:9443 -> 127.0.0.1", v4("10.20.30.40", 9443, 1), "127.0.0.1:9443");
    expect("connect own 10.20.30.40:9101 (dist) -> 127.0.0.1", v4("10.20.30.40", 9101, 1), "127.0.0.1:9101");
    expect("connect own 10.20.30.40:40851 (unlisted before) -> 127.0.0.1", v4("10.20.30.40", 40851, 1), "127.0.0.1:40851");
    expect("connect own 10.20.30.40:1024 (first unprivileged port) -> 127.0.0.1", v4("10.20.30.40", 1024, 1), "127.0.0.1:1024");
    expect("connect own 10.20.30.40:53 (the node's resolver, root's port) untouched", v4("10.20.30.40", 53, 1), "10.20.30.40:53");
    expect("connect own 10.20.30.40:1023 (root's port) untouched", v4("10.20.30.40", 1023, 1), "10.20.30.40:1023");
    expect("connect 0.0.0.0:9443 -> 127.0.0.1", v4("0.0.0.0", 9443, 1), "127.0.0.1:9443");
    expect("connect 0.0.0.0:53 untouched", v4("0.0.0.0", 53, 1), "0.0.0.0:53");
    expect("connect own 10.20.30.40:8443 (public) untouched", v4("10.20.30.40", 8443, 1), "10.20.30.40:8443");
    expect("connect own 10.20.30.40:6665 (public) untouched", v4("10.20.30.40", 6665, 1), "10.20.30.40:6665");
    expect("connect remote 203.0.113.7:8091 untouched", v4("203.0.113.7", 8091, 1), "203.0.113.7:8091");
    expect("connect remote 203.0.113.7:40000 untouched", v4("203.0.113.7", 40000, 1), "203.0.113.7:40000");
    expect("connect own [2001:db8::77]:9443 -> [::1]", v6("2001:db8::77", 9443, 1, 0), "[::1]:9443");
    expect("connect own [2001:db8::77]:40000 -> [::1]", v6("2001:db8::77", 40000, 1, 0), "[::1]:40000");
    expect("connect [::ffff:10.20.30.40]:9443 -> [::ffff:127.0.0.1]", v6("::ffff:10.20.30.40", 9443, 1, 0), "[::ffff:127.0.0.1]:9443");

    puts("# the owner check covers every loopback port but the public ones (1) or not (0)");
    {
        static const char *lo[] = {"127.0.0.1", "::1", "::ffff:127.0.0.1"};
        for (int k = 0; k < 3; k++) {
            char what[96];
            struct endpoint e;
            e = ep(lo[k], 40851); snprintf(what, sizeof what, "accept on %s:40851 (ns_couchdb, live run 6)", lo[k]);
            expect_n(what, (unsigned)accept_checked(&e), 1);
            e = ep(lo[k], 45925); snprintf(what, sizeof what, "accept on %s:45925 (moxi's -p 0, live run 6)", lo[k]);
            expect_n(what, (unsigned)accept_checked(&e), 1);
        }
        struct endpoint e = ep("127.0.0.1", 9443);
        expect_n("accept on 127.0.0.1:9443 (listed before)", (unsigned)accept_checked(&e), 1);
        e = ep("127.0.0.1", 8443); expect_n("accept on 127.0.0.1:8443 (public): not checked", (unsigned)accept_checked(&e), 0);
        e = ep("127.0.0.1", 6665); expect_n("accept on 127.0.0.1:6665 (public): not checked", (unsigned)accept_checked(&e), 0);
        e = ep("10.20.30.40", 40851); expect_n("accept on 10.20.30.40:40851 (off loopback): not checked", (unsigned)accept_checked(&e), 0);
        e = ep("127.0.0.1", 40851); expect_n("connect to 127.0.0.1:40851: checked", (unsigned)connect_checked(&e), 1);
        e = ep("127.0.0.1", 45925); expect_n("connect to 127.0.0.1:45925: checked", (unsigned)connect_checked(&e), 1);
        e = ep("::1", 33757); expect_n("connect to [::1]:33757: checked", (unsigned)connect_checked(&e), 1);
        e = ep("127.0.0.1", 1024); expect_n("connect to 127.0.0.1:1024: checked", (unsigned)connect_checked(&e), 1);
        e = ep("127.0.0.53", 53); expect_n("connect to 127.0.0.53:53 (the node's resolver): not checked", (unsigned)connect_checked(&e), 0);
        e = ep("127.0.0.1", 8443); expect_n("connect to 127.0.0.1:8443 (public): not checked", (unsigned)connect_checked(&e), 0);
        e = ep("10.20.30.40", 9443); expect_n("connect to 10.20.30.40:9443 (not loopback): not checked", (unsigned)connect_checked(&e), 0);
    }

    puts("# listen(): an unbound socket lands on loopback; one bound off loopback is refused");
    {
        int fd = socket(AF_INET, SOCK_STREAM, 0), one = 1;
        struct sockaddr_in a, got;
        socklen_t n = sizeof got;
        char s[INET_ADDRSTRLEN], what[96];
        memset(&a, 0, sizeof a);
        a.sin_family = AF_INET;   /* 0.0.0.0:0, as moxi's -p 0 asks */
        expect_n("bind(0.0.0.0:0) on a TCP socket returns 0", (unsigned)bind(fd, (struct sockaddr *)&a, sizeof a), 0);
        getsockname(fd, (struct sockaddr *)&got, &n);
        expect_n("  ... and leaves it unbound (port 0)", ntohs(got.sin_port), 0);
        expect_n("listen() on it returns 0", (unsigned)listen(fd, 1), 0);
        n = sizeof got; getsockname(fd, (struct sockaddr *)&got, &n);
        inet_ntop(AF_INET, &got.sin_addr, s, sizeof s);
        snprintf(what, sizeof what, "  ... and it listens on 127.0.0.1 (port %u)", ntohs(got.sin_port));
        expect(what, s, "127.0.0.1");
        close(fd);
        fd = socket(AF_INET, SOCK_STREAM, 0);
        setsockopt(fd, SOL_SOCKET, SO_REUSEADDR, &one, sizeof one);
        a.sin_port = 0;
        syscall(SYS_bind, fd, &a, sizeof a);   /* a bind the shim did not see (as a 24-byte IPv6 address would be) */
        errno = 0;
        expect_n("listen() on a socket bound to 0.0.0.0 behind the shim's back -> EADDRNOTAVAIL",
                 (unsigned)(listen(fd, 1) == -1 ? errno : 0), EADDRNOTAVAIL);
        close(fd);
        fd = socket(AF_INET, SOCK_STREAM, 0);
        expect_n("listen() on a socket never bound returns 0", (unsigned)listen(fd, 1), 0);
        n = sizeof got; getsockname(fd, (struct sockaddr *)&got, &n);
        inet_ntop(AF_INET, &got.sin_addr, s, sizeof s);
        expect("  ... on 127.0.0.1, not on 0.0.0.0 (the kernel's autobind)", s, "127.0.0.1");
        close(fd);
        fd = socket(AF_INET, SOCK_STREAM, 0);
        bind(fd, (struct sockaddr *)&a, sizeof a);   /* deferred */
        expect_n("a second bind(0.0.0.0:0) on that unbound socket is deferred too", (unsigned)bind(fd, (struct sockaddr *)&a, sizeof a), 0);
        close(fd);
        fd = socket(AF_INET, SOCK_DGRAM, 0);
        expect_n("UDP bind(0.0.0.0:0) returns 0, left unbound", (unsigned)bind(fd, (struct sockaddr *)&a, sizeof a), 0);
        {
            struct sockaddr_in to;
            memset(&to, 0, sizeof to);
            to.sin_family = AF_INET; to.sin_port = htons(9);
            inet_pton(AF_INET, "127.0.0.1", &to.sin_addr);
            expect_n("  ... then connect(127.0.0.1:9), as inet_res connects to its server: works", (unsigned)connect(fd, (struct sockaddr *)&to, sizeof to), 0);
        }
        close(fd);
    }

    puts("# ip_unprivileged_port_start, read once (1024 here); the parser");
    expect_n("the unprivileged start, from the file", (unsigned)unpriv_start(), 1024);
    {
        static const struct { const char *in; long want; } cases[] = {
            {"1024", 1024}, {"0", 0}, {" 443 ", 443}, {"65535", 65535}, {"65536", -1}, {"x", -1}, {"10x", -1}, {"", -1},
        };
        for (size_t k = 0; k < sizeof cases / sizeof cases[0]; k++) {
            char buf[32], what[64];
            snprintf(buf, sizeof buf, "%s", cases[k].in);
            unpriv_value = -1;
            add_unpriv(buf);
            snprintf(what, sizeof what, "parse '%s'", cases[k].in);
            expect_n(what, (unsigned)(unpriv_value + 1), (unsigned)(cases[k].want + 1));
        }
        unpriv_value = 1024;
    }

    puts("# epmd: 4369 -> this job's port");
    expect("bind 0.0.0.0:4369 -> 127.0.0.1:21345", v4("0.0.0.0", 4369, 0), "127.0.0.1:21345");
    expect("connect 127.0.0.1:4369 -> 127.0.0.1:21345", v4("127.0.0.1", 4369, 1), "127.0.0.1:21345");
    expect("connect own 10.20.30.40:4369 -> 127.0.0.1:21345", v4("10.20.30.40", 4369, 1), "127.0.0.1:21345");
    expect("bind 0.0.0.0:21345 (ERL_EPMD_PORT set directly) -> 127.0.0.1", v4("0.0.0.0", 21345, 0), "127.0.0.1:21345");
    expect("connect remote 203.0.113.7:4369 untouched", v4("203.0.113.7", 4369, 1), "203.0.113.7:4369");
    expect_n("env file: ERL_EPMD_PORT=21300 accepted", parse_epmd("ERL_EPMD_PORT=21300"), 21300);
    expect_n("env file: ERL_EPMD_PORT=4369 (outside the job range) ignored", parse_epmd("ERL_EPMD_PORT=4369"), 0);
    expect_n("env file: ERL_EPMD_PORT=21999 (end of the job range) accepted", parse_epmd("ERL_EPMD_PORT=21999"), 21999);
    expect_n("env file: ERL_EPMD_PORT=22000 (outside the job range) ignored", parse_epmd("ERL_EPMD_PORT=22000"), 0);
    expect_n("env file: ERL_EPMD_PORT=21345x ignored", parse_epmd("ERL_EPMD_PORT=21345x"), 0);
    expect_n("env file: ERL_EPMD_PORT=99999999999 ignored", parse_epmd("ERL_EPMD_PORT=99999999999"), 0);
    expect_n("env file: export ERL_EPMD_PORT=21345 (other syntax) ignored", parse_epmd("export ERL_EPMD_PORT=21345"), 0);
    epmd_job_port = 21345;

    puts("# IPv6 binds that land on ::1, with and without a usable ::1 (live runs 4 and 5: a node with IPv6 disabled)");
    {
        struct in6_addr lo6 = in6addr_loopback, mapped;
        char self[64];
        int by_exe = -1;
        inet_pton(AF_INET6, "::ffff:127.0.0.1", &mapped);
        expect_n("this host binds [::1]:0 (the suite runs where ::1 exists)", (unsigned)v6_loopback_usable(), 1);
        expect_n("with ::1: bind [::1]:11210 goes ahead", (unsigned)v6_bind_errno(&lo6, 11210), 0);
        v6lo_usable = 0;   /* as the check finds a node with disable_ipv6 = 1 on lo */
        /* The program is told by its executable's name. This test binary is not memcached or moxi: the kernel's own
         * answer, EADDRNOTAVAIL, on every port. epmd stops on EADDRINUSE (live run 5), so it must never get it. */
        expect("this program's name, from /proc/self/exe", program_name(self, sizeof self, &by_exe), "shim_unit");
        expect_n("  ... and it says it came from /proc/self/exe", (unsigned)by_exe, 1);
        expect_n("an unknown program skips IPv6 on EADDRINUSE: no", (unsigned)program_wants_eaddrinuse(), 0);
        expect_n("unknown program: bind [::1]:11210 (a memcached port) -> EADDRNOTAVAIL", (unsigned)v6_bind_errno(&lo6, 11210), EADDRNOTAVAIL);
        expect_n("unknown program: bind [::1]:21345 (the job's epmd port) -> EADDRNOTAVAIL", (unsigned)v6_bind_errno(&lo6, 21345), EADDRNOTAVAIL);
        expect_n("unknown program: bind [::1]:9101 (Onedata dist) -> EADDRNOTAVAIL", (unsigned)v6_bind_errno(&lo6, 9101), EADDRNOTAVAIL);
        expect_n("unknown program: bind [::1]:40000 (unlisted before) -> EADDRNOTAVAIL", (unsigned)v6_bind_errno(&lo6, 40000), EADDRNOTAVAIL);
        expect_n("unknown program: bind [::1]:8443 (public) left to the kernel", (unsigned)v6_bind_errno(&lo6, 8443), 0);
        expect_n("unknown program: bind [::ffff:127.0.0.1]:11210 (dual-stack target) goes ahead", (unsigned)v6_bind_errno(&mapped, 11210), 0);
        expect("unknown program: TCP bind [::]:0 v6-only -> EADDRNOTAVAIL", v6("::", 0, 0, 1), "[::]:0 (refused: EADDRNOTAVAIL)");
        expect("unknown program: TCP bind [::]:0 dual-stack: deferred (lands on ::ffff:127.0.0.1)", v6("::", 0, 0, 0), "[::]:0 (deferred: left unbound)");
        expect("unknown program: TCP bind [::]:6665 v6-only (public) untouched", v6("::", 6665, 0, 1), "[::]:6665");
        expect_n("name memcached skips IPv6 on EADDRINUSE", (unsigned)skips_v6_on_eaddrinuse("memcached"), 1);
        expect_n("name moxi skips IPv6 on EADDRINUSE", (unsigned)skips_v6_on_eaddrinuse("moxi"), 1);
        expect_n("name epmd does not", (unsigned)skips_v6_on_eaddrinuse("epmd"), 0);
        expect_n("name beam.smp does not", (unsigned)skips_v6_on_eaddrinuse("beam.smp"), 0);
        expect_n("name python3 does not", (unsigned)skips_v6_on_eaddrinuse("python3"), 0);
        expect_n("name memcached2 does not (exact names only)", (unsigned)skips_v6_on_eaddrinuse("memcached2"), 0);
        expect_n("name memcache does not (exact names only)", (unsigned)skips_v6_on_eaddrinuse("memcache"), 0);
        expect_n("name moxi.sh does not (exact names only)", (unsigned)skips_v6_on_eaddrinuse("moxi.sh"), 0);
        expect_n("an empty name does not", (unsigned)skips_v6_on_eaddrinuse(""), 0);
        expect("path /opt/couchbase/bin/memcached -> memcached", base_of("/opt/couchbase/bin/memcached"), "memcached");
        expect("path moxi (no directory) -> moxi", base_of("moxi"), "moxi");
        expect("path /a/memcached/ (a directory) -> empty", base_of("/a/memcached/"), "");
        {   /* the executable's link, as a test symlink (dangling is fine for readlink) */
            char lk[512], big[512];
            snprintf(lk, sizeof lk, "%s.exe", HPCOP_PEERS_FILE);
            unlink(lk);
            if (symlink("/opt/couchbase/bin/memcached", lk) != 0) { perror("test symlink"); return 2; }
            expect("exe link /opt/couchbase/bin/memcached -> memcached", program_name_at(lk, big, sizeof big, &by_exe), "memcached");
            expect_n("  ... named by the link (the log says /proc/self/exe)", (unsigned)by_exe, 1);
            expect("a buffer too small for the path: not trusted, argv[0]", program_name_at(lk, big, 16, &by_exe), "shim_unit");
            expect_n("  ... named by argv[0] (the log says so)", (unsigned)by_exe, 0);
            unlink(lk);
            if (symlink("/x/moxi/some/long/directory/name/that/does/not/fit/in/the/sixteen/byte/buffer", lk) != 0) { perror("test symlink"); return 2; }
            expect("a path cut right after a directory called moxi (/x/moxi): not trusted, argv[0]", program_name_at(lk, big, 8, &by_exe), "shim_unit");
            unlink(lk);
            expect("no /proc (a link that is not there): argv[0]", program_name_at(lk, big, sizeof big, &by_exe), "shim_unit");
            expect_n("  ... named by argv[0]", (unsigned)by_exe, 0);
        }
        /* as memcached and moxi: EADDRINUSE, on the same ports and nowhere else */
        prog_eaddrinuse = 1;
        expect_n("memcached/moxi: bind [::1]:11210 -> EADDRINUSE", (unsigned)v6_bind_errno(&lo6, 11210), EADDRINUSE);
        expect_n("memcached/moxi: bind [::1]:11209 (dedicated) -> EADDRINUSE", (unsigned)v6_bind_errno(&lo6, 11209), EADDRINUSE);
        expect_n("memcached/moxi: bind [::1]:11211 (moxi) -> EADDRINUSE", (unsigned)v6_bind_errno(&lo6, 11211), EADDRINUSE);
        expect("moxi: TCP bind [::]:0 v6-only (ns_server's -p 0, live run 6) -> EADDRINUSE: it skips IPv6", v6("::", 0, 0, 1), "[::]:0 (refused: EADDRINUSE)");
        expect_n("memcached/moxi: bind [::1]:8443 (public) left to the kernel", (unsigned)v6_bind_errno(&lo6, 8443), 0);
        expect_n("memcached/moxi: bind [::ffff:127.0.0.1]:11210 (dual-stack target) goes ahead", (unsigned)v6_bind_errno(&mapped, 11210), 0);
        prog_eaddrinuse = 0;
        v6lo_usable = 1;
    }

    owner_tests();

    puts("# parsers");
    expect_n("peers parsed: IPv4", (unsigned)npeer4, 3);
    expect_n("peers parsed: IPv6", (unsigned)npeer6, 1);
    f = fopen(HPCOP_PEERS_FILE, "a");  /* read once: a later addition is not seen by this process */
    if (!f) { perror("peers file"); return 2; }
    fputs("\n203.0.113.9\n", f);
    fclose(f);
    expect("peers file read once (later addition ignored)", v4("203.0.113.9", 443, 1), "203.0.113.9:443");

    printf("%s: %d failure(s)\n", failures ? "FAIL" : "PASS", failures);
    return failures ? 1 : 0;
}
