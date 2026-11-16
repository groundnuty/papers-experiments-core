/* gateport.c: LD_PRELOAD shim for the agent's cell on an HPC batch node: the connect-side mapper of port 443.
 *
 * The published REST client (onedatarestfsspec) dials port 443 and no other. The data gate cannot bind below 1024 on
 * these nodes (ip_unprivileged_port_start), so it listens on a port of its own, and the cell's /etc/hosts maps the
 * federation's names to 127.0.0.1: the client's connect to <name>:443 is a connect to 127.0.0.1:443. This shim sends that
 * one connect to the gate. Everything else the cell connects to is left exactly as it is.
 *
 *   connect()  of a TCP socket (SOCK_STREAM, protocol TCP: SCTP and MPTCP are not) to 127.0.0.1:443 or to [::1]:443
 *                                                  -> the same address, on the gate's port
 *   connect()  anything else (any other address or port, [::ffff:127.0.0.1]:443 included, UDP, a Unix socket) -> untouched
 *
 * The gate's port comes from two places, read once, when the shim loads (a constructor, before any thread of the
 * process exists), so the mapping never changes under a running process and no thread reads the environment while
 * another writes it:
 *   1. /etc/expcore/gate.json, the cell's read-only description of the gate, when the file exists:
 *        {"address": "127.0.0.1", "port": <1..65535>, "names": [...]}
 *      A JSON object, at most 4096 bytes, with "address" exactly "127.0.0.1" (the shim changes only the port, so a gate
 *      anywhere else cannot be reached by it) and "port" an integer from 1 to 65535; other keys are ignored. It is the
 *      source to rely on: the agent's code can clear its environment, and this shim is loaded through the cell's
 *      /etc/ld.so.preload into every process, which then still finds it. When the file exists it decides, whatever the
 *      environment says: a file that is not usable (not a regular file, unreadable, not that JSON, no usable port or
 *      address, a key given twice) makes the shim inert for the process and says so on stderr once, and the environment is
 *      not consulted. The file is a read-only bind of the launcher's: a cell in which the agent could write it could
 *      point its own connections elsewhere, and nobody else's.
 *   2. EXPCORE_GATE_PORT, when there is no such file: a decimal port, digits only, from 1 to 65535, no sign, space or
 *      suffix. Unset, the shim is inert and silent (a process that dropped its environment keeps its connections as
 *      they are); set to anything else, it is inert and says so on stderr once, by one line naming the variable.
 * A line on stderr is written only if stderr can take it at once, with SIGPIPE held back: a closed or full pipe loses
 * the line and never kills or blocks the process that loads the shim. The shim never guesses a port. A path other than /etc/expcore/gate.json is for the tests' build only
 * (-DEXPCORE_GATE_CONFIG_FILE="...").
 *
 * Only the port changes, never the address, and the result is the real connect()'s: the shim fabricates no success and
 * no error. A client that is refused on the gate's port sees the refusal.
 *
 * The address ::1 is rewritten like 127.0.0.1, as asked, and it keeps ::1: the gate must listen on [::1] for it to be
 * served. The core's gate listens on 127.0.0.1 only, so a connect to [::1]:443 then reaches whatever else holds
 * [::1]:<gate port> on this node, if anything does, and is refused if nothing does. The federation's names map to
 * 127.0.0.1 and no AAAA record, so the cell does not dial ::1 for them.
 *
 * Order in the preload list. The shim passes the rewritten call on to the next connect() in the lookup order
 * (dlsym(RTLD_NEXT)), not to the kernel, so it chains with the cell's egress shim: list it BEFORE egresslog.so
 *   /etc/ld.so.preload:  /expcore-preload/0.so  /hpcop-shim/egresslog.so        (or LD_PRELOAD=a.so:b.so)
 * and the egress log records the gate's port, the destination the connect really went to, which is what the egress
 * canary also records. Listed after egresslog.so it is never reached: that shim goes straight to the kernel, the
 * connect stays on 443 and is logged as 443. (tests/deploy/test_hpc_gate_port_shim.py checks both orders, and the
 * chaining against a stand-in for the next connect().)
 *
 * What passes it unchanged, as for the other shims: a statically linked program or a Go binary, raw system calls
 * (syscall(2)), io_uring, and a process that never loads it. The egress shim and the static scan of the agent's code
 * cells are what detect those.
 *
 * Build it inside the cell's image with the image's own gcc, as sandbox.sh builds egresslog.so (-ldl for a glibc before
 * 2.34, whose dlsym is in libdl; harmless after):
 *   gcc -shared -fPIC -O2 -fno-stack-protector -Wall -Wextra -Werror -o gateport.so gateport.c -ldl */
#define _GNU_SOURCE
#include <arpa/inet.h>
#include <dlfcn.h>
#include <errno.h>
#include <fcntl.h>
#include <netinet/in.h>
#include <poll.h>
#include <signal.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/socket.h>
#include <sys/stat.h>
#include <sys/syscall.h>
#include <time.h>
#include <unistd.h>

#ifndef EXPCORE_GATE_CONFIG_FILE
#define EXPCORE_GATE_CONFIG_FILE "/etc/expcore/gate.json"
#endif
#define ENV_GATE_PORT "EXPCORE_GATE_PORT"
#define GATE_ADDRESS "127.0.0.1"
#define FROM_PORT 443
#ifndef SIN6_LEN_RFC2133
#define SIN6_LEN_RFC2133 24   /* the shortest IPv6 address the kernel accepts (no sin6_scope_id) */
#endif

enum { CONFIG_MAX = 4096, JSON_DEPTH = 16, KEY_MAX = 16 };
enum { FILE_ABSENT = 0, FILE_OK = 1, FILE_BAD = 2 };

typedef int (*connect_fn)(int, const struct sockaddr *, socklen_t);

static unsigned short gate_port;   /* the port to send 443 to; 0: redirect nothing. Set once, by the constructor. */

/* One line to the process's stderr, only to a pipe, a file or a terminal: a daemon that closed fd 2 may have reused it
 * for a socket, and a line must never go into a connection. (As in portshim.c and egresslog.c.) */
static void note(const char *line) {
    struct stat st;
    size_t n = strlen(line);
    int saved = errno;
    if (syscall(SYS_fstat, 2, &st) == 0 && (S_ISFIFO(st.st_mode) || S_ISREG(st.st_mode) || S_ISCHR(st.st_mode))) {
        /* This runs in a constructor, before main() of every process that loads the shim, so it must neither kill nor
         * hold one: a pipe whose reader has gone raises SIGPIPE, and a full pipe blocks the write. The line is written
         * only if stderr can take it at once (poll), with SIGPIPE held back for the write and, if it came, taken away. */
        struct pollfd ready = {2, POLLOUT, 0};
        sigset_t pipe_signal, before, pending;
        int was_pending;
        sigemptyset(&pipe_signal);
        sigaddset(&pipe_signal, SIGPIPE);
        sigprocmask(SIG_BLOCK, &pipe_signal, &before);
        sigpending(&pending);
        was_pending = sigismember(&pending, SIGPIPE);
        if (poll(&ready, 1, 0) == 1 && (ready.revents & POLLOUT) && !(ready.revents & (POLLERR | POLLHUP | POLLNVAL)))
            while (n > 0) {
                long w = syscall(SYS_write, 2, line, n);
                if (w < 0 && errno == EINTR) continue;
                if (w <= 0) break;
                line += w; n -= (size_t)w;
            }
        sigpending(&pending);
        if (!was_pending && sigismember(&pending, SIGPIPE)) {
            struct timespec none = {0, 0};
            sigtimedwait(&pipe_signal, NULL, &none);
        }
        sigprocmask(SIG_SETMASK, &before, NULL);
    }
    errno = saved;
}

/* ---- the port, from the environment ---- */

/* 1..65535 from decimal digits alone (leading zeros allowed); 0 for anything else, the empty string included. */
static unsigned short parse_port(const char *s) {
    unsigned long v = 0;
    size_t digits = 0;
    for (; s[digits] >= '0' && s[digits] <= '9'; digits++) {
        v = v * 10 + (unsigned long)(s[digits] - '0');
        if (v > 65535) return 0;
    }
    return digits > 0 && s[digits] == '\0' && v >= 1 ? (unsigned short)v : 0;
}

/* ---- the port, from /etc/expcore/gate.json: a small strict JSON reader of the two keys it needs ---- */

struct json { const char *p, *end; };

static void skip_space(struct json *j) {
    while (j->p < j->end && (*j->p == ' ' || *j->p == '\t' || *j->p == '\n' || *j->p == '\r')) j->p++;
}

static int hex4(const char *p) {   /* the value of four hex digits, or -1 */
    int v = 0;
    for (int k = 0; k < 4; k++) {
        char c = p[k];
        int h = c >= '0' && c <= '9' ? c - '0' : c >= 'a' && c <= 'f' ? c - 'a' + 10 : c >= 'A' && c <= 'F' ? c - 'A' + 10 : -1;
        if (h < 0) return -1;
        v = v << 4 | h;
    }
    return v;
}

/* A string at j->p (the opening quote). Its bytes, escapes decoded, go to out (at most n - 1 of them; *len is the full
 * length); a character of 0x80 or more, or an escaped one, that this reader does not need to compare is stored as 0x7f.
 * 0 on a malformed string. */
static int read_string(struct json *j, char *out, size_t n, size_t *len) {
    size_t count = 0;
    if (j->p >= j->end || *j->p != '"') return 0;
    j->p++;
    for (;;) {
        unsigned char c;
        char put;
        if (j->p >= j->end) return 0;
        c = (unsigned char)*j->p++;
        if (c == '"') break;
        if (c < 0x20) return 0;
        put = c >= 0x80 ? 0x7f : (char)c;
        if (c == '\\') {
            if (j->p >= j->end) return 0;
            c = (unsigned char)*j->p++;
            switch (c) {
            case '"': case '\\': case '/': put = (char)c; break;
            case 'b': put = '\b'; break;
            case 'f': put = '\f'; break;
            case 'n': put = '\n'; break;
            case 'r': put = '\r'; break;
            case 't': put = '\t'; break;
            case 'u': {
                int v = j->end - j->p >= 4 ? hex4(j->p) : -1;
                if (v < 0) return 0;
                j->p += 4;
                put = v > 0 && v < 0x80 ? (char)v : 0x7f;
                break;
            }
            default: return 0;
            }
        }
        if (count + 1 < n) out[count] = put;
        count++;
    }
    out[count < n ? count : n - 1] = '\0';
    *len = count;
    return 1;
}

static int is_digit(const struct json *j) { return j->p < j->end && *j->p >= '0' && *j->p <= '9'; }

/* A number at j->p, as JSON writes one. *plain is set when it is only digits (no sign, fraction or exponent) and *value
 * to its value, capped at 65536. 0 on a malformed number. */
static int read_number(struct json *j, int *plain, unsigned long *value) {
    const char *start = j->p;
    *value = 0;
    if (j->p < j->end && *j->p == '-') j->p++;
    if (!is_digit(j)) return 0;
    if (*j->p == '0') j->p++;
    else while (is_digit(j)) j->p++;
    if (j->p < j->end && *j->p == '.') {
        j->p++;
        if (!is_digit(j)) return 0;
        while (is_digit(j)) j->p++;
    }
    if (j->p < j->end && (*j->p == 'e' || *j->p == 'E')) {
        j->p++;
        if (j->p < j->end && (*j->p == '+' || *j->p == '-')) j->p++;
        if (!is_digit(j)) return 0;
        while (is_digit(j)) j->p++;
    }
    *plain = 1;
    for (const char *c = start; c < j->p; c++) {
        if (*c < '0' || *c > '9') *plain = 0;
        else if (*value <= 65535) *value = *value * 10 + (unsigned long)(*c - '0');
    }
    if (*value > 65536) *value = 65536;
    return 1;
}

static int skip_value(struct json *j, int depth);

static int skip_literal(struct json *j, const char *word) {
    size_t n = strlen(word);
    if ((size_t)(j->end - j->p) < n || memcmp(j->p, word, n) != 0) return 0;
    j->p += n;
    return 1;
}

static int skip_container(struct json *j, int depth, char close, int keyed) {
    char key[KEY_MAX];
    size_t len;
    j->p++;   /* the opening bracket */
    skip_space(j);
    if (j->p < j->end && *j->p == close) { j->p++; return 1; }
    for (;;) {
        skip_space(j);
        if (keyed) {
            if (!read_string(j, key, sizeof key, &len)) return 0;
            skip_space(j);
            if (j->p >= j->end || *j->p++ != ':') return 0;
            skip_space(j);
        }
        if (!skip_value(j, depth + 1)) return 0;
        skip_space(j);
        if (j->p >= j->end) return 0;
        if (*j->p == ',') { j->p++; continue; }
        if (*j->p == close) { j->p++; return 1; }
        return 0;
    }
}

static int skip_value(struct json *j, int depth) {
    char text[KEY_MAX];
    size_t len;
    unsigned long v;
    int plain;
    if (depth > JSON_DEPTH || j->p >= j->end) return 0;
    switch (*j->p) {
    case '{': return skip_container(j, depth, '}', 1);
    case '[': return skip_container(j, depth, ']', 0);
    case '"': return read_string(j, text, sizeof text, &len);
    case 't': return skip_literal(j, "true");
    case 'f': return skip_literal(j, "false");
    case 'n': return skip_literal(j, "null");
    default: return read_number(j, &plain, &v);
    }
}

/* The top-level object: "address" and "port" are read, every other member is skipped (and must be JSON). *why says what
 * is wrong, when 0 is returned. */
static int read_gate_object(struct json *j, unsigned short *port, const char **why) {
    int have_address = 0, have_port = 0;
    unsigned long portv = 0;
    const char *bad = "is not a JSON object of at most 4096 bytes";
    skip_space(j);
    if (j->p >= j->end || *j->p != '{') goto fail;
    j->p++;
    skip_space(j);
    if (j->p < j->end && *j->p == '}') j->p++;
    else for (;;) {
        char key[KEY_MAX];
        size_t len;
        skip_space(j);
        bad = "is not valid JSON";
        if (!read_string(j, key, sizeof key, &len)) goto fail;
        skip_space(j);
        if (j->p >= j->end || *j->p++ != ':') goto fail;
        skip_space(j);
        if (len == 7 && strcmp(key, "address") == 0) {
            char value[16];
            size_t vlen;
            if (have_address++) { bad = "gives \"address\" twice"; goto fail; }
            if (j->p >= j->end || *j->p != '"' || !read_string(j, value, sizeof value, &vlen)) { bad = "has an \"address\" that is not a string"; goto fail; }
            if (vlen != strlen(GATE_ADDRESS) || strcmp(value, GATE_ADDRESS) != 0) { bad = "has an \"address\" other than " GATE_ADDRESS; goto fail; }
        } else if (len == 4 && strcmp(key, "port") == 0) {
            int plain;
            if (have_port++) { bad = "gives \"port\" twice"; goto fail; }
            if (!read_number(j, &plain, &portv) || !plain || portv < 1 || portv > 65535) { bad = "has a \"port\" that is not an integer from 1 to 65535"; goto fail; }
        } else if (!skip_value(j, 1)) {
            goto fail;
        }
        skip_space(j);
        if (j->p >= j->end) goto fail;
        if (*j->p == ',') { j->p++; continue; }
        if (*j->p++ == '}') break;
        goto fail;
    }
    skip_space(j);
    bad = "is not valid JSON";
    if (j->p != j->end) goto fail;
    if (!have_address) { bad = "has no \"address\""; goto fail; }
    if (!have_port) { bad = "has no \"port\""; goto fail; }
    *port = (unsigned short)portv;
    return 1;
fail:
    *why = bad;
    return 0;
}

/* FILE_ABSENT (no such file: the environment decides), FILE_OK with *port set, or FILE_BAD with *why. */
static int read_gate_file(unsigned short *port, const char **why) {
    char buf[CONFIG_MAX + 1];
    size_t have = 0;
    struct json j;
    struct stat st;
    /* O_NONBLOCK: opening a FIFO that nothing writes would otherwise hang every process that loads the shim */
    int fd = open(EXPCORE_GATE_CONFIG_FILE, O_RDONLY | O_CLOEXEC | O_NONBLOCK | O_NOCTTY);
    if (fd < 0) {
        if (errno == ENOENT || errno == ENOTDIR) return FILE_ABSENT;
        *why = "cannot be opened";
        return FILE_BAD;
    }
    if (fstat(fd, &st) != 0 || !S_ISREG(st.st_mode)) { close(fd); *why = "is not a regular file"; return FILE_BAD; }
    for (;;) {
        ssize_t r = read(fd, buf + have, sizeof buf - have);
        if (r < 0 && errno == EINTR) continue;
        if (r < 0) { close(fd); *why = "cannot be read"; return FILE_BAD; }
        if (r == 0) break;
        have += (size_t)r;
        if (have > CONFIG_MAX) { close(fd); *why = "is larger than 4096 bytes"; return FILE_BAD; }
    }
    close(fd);
    j.p = buf; j.end = buf + have;
    return read_gate_object(&j, port, why) ? FILE_OK : FILE_BAD;
}

__attribute__((constructor)) static void gateport_init(void) {
    unsigned short port = 0;
    const char *why = "";
    const char *value;
    char line[320];
    int found = read_gate_file(&port, &why);
    if (found == FILE_OK) { gate_port = port; return; }
    if (found == FILE_BAD) {
        snprintf(line, sizeof line, "expcore gateport: pid %ld: %s %s; this process redirects no connection\n",
                 (long)syscall(SYS_getpid), EXPCORE_GATE_CONFIG_FILE, why);
        note(line);
        return;
    }
    value = getenv(ENV_GATE_PORT);
    if (!value) return;
    gate_port = parse_port(value);
    if (!gate_port) {
        snprintf(line, sizeof line, "expcore gateport: pid %ld: %s is not a decimal port from 1 to 65535; this process redirects no connection\n",
                 (long)syscall(SYS_getpid), ENV_GATE_PORT);
        note(line);
    }
}

/* ---- connect() ---- */

/* TCP and nothing else: a SOCK_STREAM socket may also be SCTP or MPTCP, whose connect to 127.0.0.1:443 is not the REST
 * client's. Two system calls: this shim must not call the connect it replaces. */
static int is_tcp(int fd) {
    int type = 0, protocol = 0;
    socklen_t n = sizeof type, m = sizeof protocol;
    return syscall(SYS_getsockopt, fd, SOL_SOCKET, SO_TYPE, &type, &n) == 0 && type == SOCK_STREAM
           && syscall(SYS_getsockopt, fd, SOL_SOCKET, SO_PROTOCOL, &protocol, &m) == 0 && protocol == IPPROTO_TCP;
}

/* 1 if a connect() of fd to addr is the one this shim redirects, with *to the destination to use instead: the same
 * address and the gate's port. Everything else, and anything that is not a well-formed address, is 0. */
static int redirect(int fd, const struct sockaddr *addr, socklen_t len, struct sockaddr_storage *to) {
    memset(to, 0, sizeof *to);
    if (!addr || len < (socklen_t)sizeof(sa_family_t)) return 0;   /* nothing of addr may be read before its length is */
    if (addr->sa_family == AF_INET && len >= (socklen_t)sizeof(struct sockaddr_in)) {
        struct sockaddr_in *s = (struct sockaddr_in *)to;
        memcpy(s, addr, sizeof *s);
        if (s->sin_addr.s_addr != htonl(INADDR_LOOPBACK) || s->sin_port != htons(FROM_PORT)) return 0;
        s->sin_port = htons(gate_port);
    } else if (addr->sa_family == AF_INET6 && len >= (socklen_t)SIN6_LEN_RFC2133) {
        struct sockaddr_in6 *s = (struct sockaddr_in6 *)to;
        memcpy(s, addr, len < (socklen_t)sizeof *s ? (size_t)len : sizeof *s);   /* the kernel takes the short length too */
        if (s->sin6_port != htons(FROM_PORT) || !IN6_IS_ADDR_LOOPBACK(&s->sin6_addr)) return 0;
        s->sin6_port = htons(gate_port);
    } else {
        return 0;
    }
    return is_tcp(fd);
}

/* The next connect() after this one: egresslog.so's when it is loaded after this shim, else libc's. Looked up on the
 * first call and kept; two threads that look it up together find the same address. Without one, the kernel. */
static long real_connect(int fd, const struct sockaddr *addr, socklen_t len, int saved) {
    static connect_fn next;
    connect_fn f = __atomic_load_n(&next, __ATOMIC_ACQUIRE);
    if (!f) {
        *(void **)(&f) = dlsym(RTLD_NEXT, "connect");
        if (f) __atomic_store_n(&next, f, __ATOMIC_RELEASE);
    }
    errno = saved;   /* the lookup and the socket-type probe must not leak an errno into a call that succeeds */
    return f ? f(fd, addr, len) : syscall(SYS_connect, fd, addr, len);
}

int connect(int fd, const struct sockaddr *addr, socklen_t len) {
    int saved = errno;
    struct sockaddr_storage to;
    if (gate_port && redirect(fd, addr, len, &to)) return (int)real_connect(fd, (const struct sockaddr *)&to, len, saved);
    return (int)real_connect(fd, addr, len, saved);
}
