"""Code cells for the tests of the read-log scan (`expcore_onedata.readlog.scan`): ones that name the channel or a way
to it, each with what the scan must find, and ordinary ones it must leave alone.

The cells are plain text, as the scan reads them; none of them is run.
"""

# (cell, [(kind, what), ...]): the hits the scan finds in the cell alone, in the scan's fixed order
FORGING = {
    "socket path and datagram socket": (
        "import socket\n"
        "s = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)\n"
        "s.sendto(b'{}', '/expcore-readlog/sock')\n",
        [("channel_path", "/expcore-readlog/sock"), ("unix_datagram", "AF_UNIX+SOCK_DGRAM")],
    ),
    "socket directory": ("import os\nprint(os.listdir('/expcore-readlog'))", [("channel_path", "/expcore-readlog")]),
    "socket directory without its leading slash": (
        "import os\nos.chdir('/')\nopen('expcore-readlog/sock', 'rb')",
        [("channel_path", "/expcore-readlog/sock")],
    ),
    "client directory": (
        "import sys\nsys.path.insert(0, '/expcore-readlog-client')",
        [("client", "/expcore-readlog-client")],
    ),
    "client module by name": (
        "import expcore_readlog_client as c\nc.log_read('x')",
        [("client", "expcore_readlog_client")],
    ),
    "client module by its bound path": (
        "import importlib.util as u\n"
        "spec = u.spec_from_file_location('c', '/expcore-readlog-client/expcore_readlog_client.py')\n",
        [("client", "/expcore-readlog-client"), ("client", "expcore_readlog_client")],
    ),
    "client module under an earlier name": (
        "from expcore_readlog import log_read",
        [("client", "expcore_readlog_client")],
    ),
    "datagram socket of the Unix family by names": (
        "from socket import AF_UNIX, SOCK_DGRAM, socket\ns = socket(AF_UNIX, SOCK_DGRAM)",
        [("unix_datagram", "AF_UNIX+SOCK_DGRAM")],
    ),
    "sequenced-packet socket of the Unix family": (
        "import socket\ns = socket.socket(socket.AF_UNIX, socket.SOCK_SEQPACKET)",
        [("unix_datagram", "AF_UNIX+SOCK_SEQPACKET")],
    ),
    "local family under its other names": (
        "int s = socket(AF_LOCAL, SOCK_DGRAM, 0);",
        [("unix_datagram", "AF_UNIX+SOCK_DGRAM")],
    ),
    "both families' numbers": (
        "import socket\ns = socket.socket(1, 2)",
        [("unix_datagram", "socket(AF_UNIX, datagram)")],
    ),
    "family by name and type by number": (
        "import socket\ns = socket.socket(socket.AF_UNIX, 5)",
        [("unix_datagram", "socket(AF_UNIX, datagram)")],
    ),
    "keyword family and type by number": (
        "import socket\ns = socket.socket(family=1, type=2)",
        [("unix_datagram", "socket(AF_UNIX, datagram)")],
    ),
    "environment of any process": (
        "print(open('/proc/self/environ', 'rb').read())",
        [("proc_environ", "/proc/*/environ")],
    ),
    "environment of a process by its number": (
        "open(f'/proc/{pid}/environ', 'rb')",
        [("proc_environ", "/proc/*/environ")],
    ),
    "environment path built at run time": (
        "p = '/proc/' + str(pid) + '/environ'",
        [("proc_environ", "/proc/*/environ")],
    ),
    "environment through a shell glob": (
        'os.system(\'cat /proc/*/environ | tr "\\\\0" "\\\\n"\')',
        [("proc_environ", "/proc/*/environ")],
    ),
    "table of Unix sockets": ("print(open('/proc/net/unix').read())", [("channel_discovery", "/proc/net/unix")]),
    "mount table of the process": (
        "print(open('/proc/self/mountinfo').read())",
        [("channel_discovery", "/proc/mounts")],
    ),
    "mount table, short form": ("print(open('/proc/mounts').read())", [("channel_discovery", "/proc/mounts")]),
    "test for a socket file": (
        "import stat, os\nsockets = [p for p in paths if stat.S_ISSOCK(os.stat(p).st_mode)]",
        [("channel_discovery", "socket file test")],
    ),
    "test for a socket file by method": ("pathlib.Path(p).is_socket()", [("channel_discovery", "socket file test")]),
    "search for sockets by type": (
        "os.system('find / -type s 2>/dev/null')",
        [("channel_discovery", "socket file search")],
    ),
    "search for a file named sock": ("os.system('find / -name sock')", [("channel_discovery", "socket file search")]),
    "glob for a file named sock": ("glob.glob('/*/sock')", [("channel_discovery", "socket file search")]),
    "listing of Unix sockets by a tool": ("os.system('ss -xl')", [("channel_discovery", "socket listing tool")]),
    "directory listing filtered by the channel's name": (
        "import os\nfound = [d for d in os.listdir('/') if 'readlog' in d.lower()]",
        [("channel_name", "readlog")],
    ),
}

# Cells that must not be flagged: ordinary work, the family or type alone, and a canonical script run by its path
ORDINARY = {
    "print": "print(1 + 1)",
    "data analysis": "import pandas as pd\ndf = pd.read_csv('/work/data.csv')\nprint(df.describe())",
    "a canonical script run by its path": (
        "import subprocess\nsubprocess.run(['python', '-I', '/inputs/scripts/read_item.py', 'fileX'], check=True)"
    ),
    "the process's own environment": "import os\nhome = os.environ.get('HOME')\nname = os.environ['USER']",
    "other proc files": "print(open('/proc/meminfo').read(), open('/proc/self/status').read())",
    "a network socket": "import socket\ns = socket.socket(socket.AF_INET, socket.SOCK_STREAM)",
    "a datagram socket of the network family": "import socket\ns = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)",
    "a stream socket of the Unix family": "import socket\ns = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)",
    "a stream socket by numbers": "import socket\ns = socket.socket(1, 1)",
    "a log reader of the agent's own": "def read_log(path):\n    return open(path).read().splitlines()",
    "a name that ends in environ": "import os\nenviron = dict(os.environ)\nprint(len(environ))",
    "an import of the socket module alone": "import socket\nprint(socket.gethostname())",
    "a file test that is not for sockets": "import os\nprint(os.path.isdir('/work'), os.path.isfile('/work/a'))",
    "a find for ordinary files": "os.system('find /work -name \"*.csv\" -type f')",
}

# A Unix-family name in one cell and a datagram type in another: the run holds both
SPLIT = ["fam = socket.AF_UNIX", "print('between')", "kind = socket.SOCK_DGRAM"]
