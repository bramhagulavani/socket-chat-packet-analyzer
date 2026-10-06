import os
import re
import sys
import threading
import time

PROJECT_ROOT = os.path.dirname(os.path.abspath(__file__))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

# ---------------------------------------------------------------------------
# Generic client names
#
# A pool of neutral, non-identifying handles.  The name is derived from the
# client id, so a run is reproducible (the same id always gets the same name)
# and the report/screenshots stay consistent between demos.  A numeric suffix is
# appended in the unlikely event that the pool wraps around.
# ---------------------------------------------------------------------------
GENERIC_NAMES = [
    'Falcon', 'Otter', 'Kite',   'Lynx',  'Heron', 'Marten',
    'Osprey', 'Ibex',  'Puffin', 'Raven', 'Sable', 'Tapir',
    'Vireo',  'Wren',  'Yak',    'Zebu',  'Badger', 'Cobra',
    'Dingo',  'Egret', 'Finch',  'Gecko', 'Hare',  'Ibis',
]


def pick_name(client_id, used=()):
    """Deterministic generic name for a client id, unique within `used`."""
    base = GENERIC_NAMES[(client_id - 1) % len(GENERIC_NAMES)]
    name = base
    suffix = 2
    while name in used:
        name = f"{base}-{suffix}"
        suffix += 1
    return name


# ---------------------------------------------------------------------------
# Address / time formatting
# ---------------------------------------------------------------------------
def addr_str(addr):
    """(host, port) tuple -> 'host:port'."""
    return f"{addr[0]}:{addr[1]}"


def format_uptime(seconds):
    """Seconds -> '45s' / '3m 12s' / '1h 04m 09s'."""
    seconds = int(max(0, seconds))
    minutes, secs = divmod(seconds, 60)
    hours, minutes = divmod(minutes, 60)
    if hours:
        return f"{hours}h {minutes:02d}m {secs:02d}s"
    if minutes:
        return f"{minutes}m {secs:02d}s"
    return f"{secs}s"


def format_time(epoch):
    """Epoch seconds -> local 'HH:MM:SS'."""
    return time.strftime('%H:%M:%S', time.localtime(epoch))


# ---------------------------------------------------------------------------
# Client metadata rendering
# ---------------------------------------------------------------------------
# Two levels of detail, on purpose:
#   describe()          -> server console.  Everything the server knows.
#   describe_client()   -> what one client is told about another.  Name and id,
#                          which is all it needs in order to pick a `to=` target.
def describe(record):
    """Compact one-line identity, e.g. '#3 Kite (127.0.0.1:51240)'.  SERVER ONLY."""
    return f"#{record['id']} {record['name']} ({addr_str(record['addr'])})"


def describe_client(record):
    """What a client learns about another client: 'Kite (#3)'."""
    return f"{record['name']} (#{record['id']})"


def format_roster_table(records, viewer_id=None, now=None):
    """
    Aligned text table of every known client and its metadata.

    SERVER CONSOLE ONLY -- this includes remote addresses, join times and uptime,
    none of which are ever sent to a client.
    """
    if not records:
        return "  (nobody else is connected)"

    now = time.time() if now is None else now
    header = f"  {'ID':<5}{'NAME':<11}{'ADDRESS':<22}{'JOINED':<10}UPTIME"
    rows = [header, "  " + "-" * (len(header) - 2)]

    for record in sorted(records, key=lambda r: r['id']):
        joined = record.get('joined_at')
        uptime = format_uptime(now - joined) if joined else '-'
        when = format_time(joined) if joined else '-'
        marker = '  <- you' if viewer_id is not None and str(record['id']) == str(viewer_id) else ''
        rows.append(
            f"  #{record['id']:<4}{record['name']:<11}"
            f"{addr_str(record['addr']):<22}{when:<10}{uptime}{marker}"
        )
    return "\n".join(rows)


def format_roster_names(records, viewer_id=None):
    """
    What a client sees when it runs /clients: names and ids, nothing else.

    The id is kept because it is what a user types after `to=`; the address, join
    time and uptime are dropped on purpose.
    """
    if not records:
        return "  (nobody else is connected)"

    rows = []
    for record in sorted(records, key=lambda r: r['id']):
        marker = '  <- you' if viewer_id is not None and str(record['id']) == str(viewer_id) else ''
        rows.append(f"  {record['name']} (#{record['id']}){marker}")
    return "\n".join(rows)


# ---------------------------------------------------------------------------
# Control message protocol
# ---------------------------------------------------------------------------
CONTROL_MARKER = '/'
_PREFIX = CONTROL_MARKER.encode()


def ctrl(*fields):
    """Build a control message: ctrl('ACK', 'kind=file') -> b'/ACK kind=file'."""
    return _PREFIX + " ".join(fields).encode()


def parse_ctrl(raw):
    """
    Parse a control message.

    Returns (tag, fields) where `tag` is the uppercased keyword and `fields` is a
    dict of the `key=value` pairs.  Returns (None, {}) when `raw` is not a
    control message at all.
    """
    if not raw.startswith(_PREFIX):
        return None, {}

    tokens = raw.split()
    if not tokens:
        return None, {}

    tag = tokens[0][1:].decode('ascii', 'replace').upper()
    fields = {}
    for token in tokens[1:]:
        key, sep, value = token.partition(b'=')
        if sep:
            fields[key.decode('ascii', 'replace').lower()] = value.decode('utf-8', 'replace')
    return tag, fields


# Roster entries are packed into a single control line, so they must not contain
# spaces.  Format:  <id>=<Name>
#
# Only the id and the name travel.  The address and the join time stay on the
# server: a client is told who is in the room, never where they are connected
# from or when they turned up.
def _peer_entry(record):
    return f"{record['id']}={record['name']}"


def _peer_records(records):
    return sorted(records, key=lambda r: r['id'])


def render_roster(records, viewer_id=None, why='update'):
    """
    Build the single-line /ROSTER control message.

    `why` tells the client whether this is an answer to its own /clients request
    or just a background update after a join/leave.  Clients print the list only
    when they asked for it, so joining a busy room does not bury the terminal in
    rosters nobody requested.
    """
    entries = ",".join(_peer_entry(r) for r in _peer_records(records))
    return ctrl(
        "ROSTER",
        f"count={len(records)}",
        f"you={viewer_id if viewer_id is not None else '-'}",
        f"why={why}",
        "peers=" + (entries if entries else "-"),
    )


def parse_roster_peers(fields):
    """
    Inverse of _peer_entry.  Returns a list of {id, name} records.

    There is deliberately no 'addr' or 'joined_at' to fill in: that information
    is never put on the wire.
    """
    raw = fields.get('peers', '-')
    if not raw or raw == '-':
        return []

    records = []
    for entry in raw.split(','):
        ident, sep, name = entry.partition('=')
        if not sep:
            continue
        try:
            client_id = int(ident)
        except ValueError:
            continue
        records.append({'id': client_id, 'name': name})
    return records



def format_ack(fields):
    """Human-readable rendering of an /ACK control message."""
    name = fields.get('name', '(unnamed)')
    status = fields.get('status', 'ok')

    if status == 'ok':
        headline = f"File '{name}' was sent successfully"
    elif status == 'lossy':
        headline = (f"File '{name}' finished sending, but "
                    f"{fields.get('missing', '?')} chunk(s) never reached the wire")
    elif status == 'incomplete':
        headline = f"Transfer of '{name}' was cut short by the sender"
    else:
        headline = f"File '{name}' finished with status '{status}'"

    details = []
    if fields.get('bytes', '').isdigit():
        details.append(f"{fields['bytes']} bytes")
    if fields.get('chunks', '').isdigit():
        if fields.get('forwarded', '').isdigit():
            details.append(f"{fields['forwarded']}/{fields['chunks']} chunks delivered")
        else:
            details.append(f"{fields['chunks']} chunks")

    suffix = f" [{', '.join(details)}]" if details else ""
    audience = ""
    target = fields.get('target', 'all')
    if target not in ('all', '-', ''):
        audience = f" -> {render_target_label(target)} only"
    return f"{headline}{suffix} -> {fields.get('recipients', '0')} other client(s){audience}"


# ---------------------------------------------------------------------------
# File transfer: who is allowed to receive this file?
#
# The sender types the recipient into the command (``/sendfile notes.txt to=#2``)
# so the choice is made *before* the transfer starts, and the server resolves it
# against the live roster instead of trusting the client.  These helpers are the
# single implementation of that rule, shared by the TCP and UDP servers.
# ---------------------------------------------------------------------------

def normalize_target(text):
    """
    Turn whatever the user typed after ``to=`` into a (kind, value) pair.

        'all' / '*' / '' / None -> ('all', None)
        '3' / '#3'              -> ('id', 3)
        'Kite' / '@Kite'        -> ('name', 'Kite')

    Returns (None, None) when the text cannot be a target at all.
    """
    raw = (text or '').strip()
    if not raw or raw.lower() in ('all', '*', 'everyone'):
        return ('all', None)

    if raw.startswith('#'):
        raw = raw[1:]
    if raw.isdigit():
        return ('id', int(raw))

    if raw.startswith('@'):
        raw = raw[1:]
    if not raw or ' ' in raw:
        return (None, None)
    return ('name', raw)


def target_token(record):
    """A resolved record -> 'Kite#3'.  A broadcast (record=None) -> 'all'."""
    if record is None:
        return 'all'
    return f"{record['name']}#{record['id']}"


def parse_label(token):
    """Inverse of target_token: 'Kite#3' -> ('Kite', 3).  Never raises."""
    text = (token or '').strip()
    if not text or text in ('-', 'all'):
        return None, None
    name, sep, ident = text.partition('#')
    if not sep:
        # A bare id ('3') is also a legal label.
        return (None, int(text)) if text.isdigit() else (text, None)
    return (name or None), (int(ident) if ident.isdigit() else None)


def render_target_label(token):
    """'Kite#3' -> '@Kite (#3)' for display.  'all' -> 'everyone'."""
    if not token or token in ('-', 'all'):
        return 'everyone'
    name, ident = parse_label(token)
    if name and ident is not None:
        return f"@{name} (#{ident})"
    if ident is not None:
        return f"#{ident}"
    return f"@{token}"


def parse_file_opts(tokens):
    """['to=Kite#3', 'from=Otter#2'] -> {'to': 'Kite#3', 'from': 'Otter#2'}"""
    opts = {}
    for token in tokens:
        key, sep, value = token.partition('=')
        if sep:
            opts[key.strip().lower()] = value.strip()
    return opts


def format_file_header(name, amount, sender_record=None, target=None):
    """
    Build the canonical /FILE header line (without a trailing newline).

    `amount` is whatever the protocol counts in: bytes for TCP, total chunks for
    UDP.  Unknown/extra options are ignored by the reader, which is what lets the
    same header describe an attributed and a targeted transfer.
    """
    fields = [f"/FILE {name} {amount}"]
    if sender_record:
        fields.append(f"from={target_token(sender_record)}")
    if target and target != 'all':
        fields.append(f"to={target}")
    return " ".join(fields)


def resolve_target(records, sender_id, spec, other_count):
    """
    Decide which clients a /FILE header is allowed to reach.

    Args:
        records     : every connected client record (dicts with id / name / addr)
        sender_id   : id of the client asking to send -- it never gets its own file
        spec        : (kind, value) from normalize_target(), or None for a broadcast
        other_count : how many clients besides the sender are connected

    Returns:
        (record, None)          -- exactly one recipient (private transfer)
        (None, None)            -- everybody except the sender (broadcast)
        (None, (code, arg))     -- refused; `arg` is always a single whitespace-free
                                    token so it survives the control-message format
    """
    others = [r for r in records if str(r['id']) != str(sender_id)]

    if spec is None or spec[0] == 'all':
        if other_count == 0:
            return None, ('no_recipients', '')
        return None, None

    kind, value = spec

    if kind == 'id':
        for record in others:
            if str(record['id']) == str(value):
                return record, None
        if any(str(r['id']) == str(sender_id) for r in records) and \
                any(str(r['id']) == str(value) for r in records):
            return None, ('self_target', str(value))
        return None, ('unknown_target', str(value))

    wanted = str(value).lower()
    matches = [r for r in others if r['name'].lower() == wanted]
    if len(matches) == 1:
        return matches[0], None
    if len(matches) > 1:
        return None, ('ambiguous_target', str(value))
    if any(r['name'].lower() == wanted and str(r['id']) == str(sender_id)
           for r in records):
        return None, ('self_target', str(value))
    return None, ('unknown_target', str(value))


# ---------------------------------------------------------------------------
# Verdicts: what the client is told about a transfer it asked for
# ---------------------------------------------------------------------------
# Keyed by the /ERR code.  `{arg}` is filled with the selector the user typed.
SEND_ERROR_TEXT = {
    'no_recipients': 'nobody else is connected yet, so there is no recipient',
    'self_target': 'you cannot send a file to yourself -- pick a different client',
    'unknown_target': "no connected client matches '{arg}'",
    'ambiguous_target': "'{arg}' matches more than one client -- use its #id instead",
    'bad_target': "'{arg}' is not a usable selector -- use all, #3 or @Kite",
}


def format_send_error(fields):
    """Human-readable rendering of a /ERR refusal."""
    code = fields.get('code', 'rejected')
    arg = fields.get('arg', '')
    template = SEND_ERROR_TEXT.get(code, 'the server refused the transfer ({code})')
    reason = template.format(arg=arg or 'that', code=code)
    subject = f"File '{fields.get('name')}'" if fields.get('name') else 'The transfer'
    return (f"{subject} was NOT sent: {reason}. "
            f"Type /clients to see who is in the room.")


def format_send_verdict(fields):
    """Human-readable rendering of an accepted /SEND verdict."""
    target = fields.get('target', 'all')
    recipients = fields.get('recipients', '0')
    name = fields.get('name', '(unnamed)')
    if target in ('all', '-', ''):
        where = f"every other client ({recipients} recipient(s))"
    else:
        where = f"{render_target_label(target)} only"
    return f"File '{name}' will be delivered to {where}"


class FileVerdict:
    """
    A one-slot hand-off between a client's receive thread and its send loop.

    Only the receive thread ever reads the socket, so the /SEND or /ERR verdict
    for an outgoing file has to be parked here for the thread that is sitting in
    input() to pick up.  A small queue (rather than a single slot) keeps a late
    or duplicated verdict from being mistaken for the next transfer's answer --
    ``take()`` discards anything that is not about the file it was asked about.
    """

    def __init__(self):
        self._lock = threading.Lock()
        self._pending = []          # list of (tag, fields), oldest first

    def resolve(self, tag, fields):
        """Called by the receive thread when the server renders a verdict."""
        with self._lock:
            self._pending.append((tag, fields))

    def take(self, filename=None, timeout=5.0):
        """
        Wait for the verdict that belongs to `filename`.

        Returns (ok, tag, fields).  `ok` is False on timeout, or when the server
        refused the transfer outright (tag == 'ERR') -- in both cases the caller
        must not send any payload bytes.
        """
        deadline = time.monotonic() + timeout
        while True:
            with self._lock:
                while self._pending:
                    tag, fields = self._pending.pop(0)
                    name = fields.get('name')
                    if filename and name and name != filename:
                        continue        # a verdict about some other transfer
                    return True, tag, fields

            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return False, None, {}
            time.sleep(0.02)


# ---------------------------------------------------------------------------
# What the client prints, kept short
# ---------------------------------------------------------------------------
def render_connected(name, client_id, protocol):
    """The single line a client prints the moment it becomes able to chat."""
    return f"{name} (#{client_id}) connected - {protocol} chat"


# Commands are never listed at startup -- they are one keystroke away, on demand.
HELP_LINES = [
    ("<text>", "send a message to everyone"),
    ("/clients", "list who is in the room"),
    ("/sendfile <path>", "send a file to everyone"),
    ("/sendfile <path> to=#2", "send a file to one client only"),
    ("/quit", "leave"),
]


def render_help():
    rows = "\n".join(f"  {command:<28} {text}" for command, text in HELP_LINES)
    return f"[HELP]\n{rows}"


def render_room_notice(record, action):
    """'Kite (#3) joined' / 'Lynx (#4) left' -- a name, nothing more."""
    return f"{describe_client(record)} {action}"


# ---------------------------------------------------------------------------
# Parsing the sender's command line
#
#   /sendfile <path>                     -> broadcast to the room
#   /sendfile <path> to=#2               -> to client 2, and nobody else
#   /sendfile <path> to=@Otter           -> same, by name
#   /sendto   #2 <path>                  -> alias, selector first
#
# The selector is deliberately part of the command rather than a separate
# "current target" setting: whatever the user typed is resolved and checked by
# the server at header time, so nothing is ever sent on a guess.
# ---------------------------------------------------------------------------
_TO_SUFFIX = re.compile(r'\s+to\s*=\s*(\S+)\s*$', re.IGNORECASE)
_DANGLING_TO = re.compile(r'\bto\s*=\s*$', re.IGNORECASE)

SEND_USAGE = 'Usage: /sendfile <path> [to=<#id|@Name>]   (or /sendto <#id|@Name> <path>)'


def parse_send_command(message):
    """
    Split a /sendfile (or /sendto) line into the file and the intended recipient.

    Returns (filepath, target_spec, error):
      * target_spec is None for a broadcast, otherwise the raw text after `to=`
      * error is a usage string when the line cannot be used at all

    The selector is returned unvalidated on purpose -- resolve_target() on the
    server is the authority on whether it names exactly one connected client.
    """
    text = (message or '').strip()
    lowered = text.lower()

    if lowered.startswith('/sendto '):
        spec, sep, path = text[len('/sendto '):].partition(' ')
        path = path.strip()
        if not sep or not spec.strip() or not path:
            return None, None, SEND_USAGE
        if len(spec.split()) > 1:
            return None, None, ('The selector must be a single token: '
                                '#id, @Name, a bare id or name, or "all".')
        return path, spec.strip(), None

    if lowered.startswith('/sendfile'):
        rest = text[len('/sendfile'):].strip()
        if not rest:
            return None, None, SEND_USAGE
        if _DANGLING_TO.search(rest):
            return None, None, ('to= needs a selector after it -- ' + SEND_USAGE)
        match = _TO_SUFFIX.search(rest)
        if match:
            return rest[:match.start()].strip(), match.group(1), None
        return rest, None, None

    return None, None, None


# ---------------------------------------------------------------------------
# Terminal presentation
# ---------------------------------------------------------------------------
def is_probably_text(data):
    """
    True when a payload is safe to echo to the terminal.

    File chunks are raw binary; decoding them with errors='replace' would spray
    mojibake all over the console.  Anything that is not clean, fully printable
    UTF-8 is treated as file data and suppressed instead of displayed.
    """
    if not data:
        return True
    if b'\x00' in data:
        return False
    try:
        text = data.decode('utf-8')
    except UnicodeDecodeError:
        return False
    return all(ch.isprintable() or ch in '\r\n\t' for ch in text)


def format_banner(title, rows):
    """ASCII box used for optional headers (e.g. a scripted demo header)."""
    body = [f"{key:<10} {value}" for key, value in rows]
    inner = max([len(title)] + [len(line) for line in body]) + 2
    border = "+" + "-" * inner + "+"
    lines = [
        border,
        "|" + title.center(inner) + "|",
        "+" + "-" * inner + "+",
    ]
    lines += ["|" + line.ljust(inner) + "|" for line in body]
    lines.append(border)
    return "\n".join(lines)
