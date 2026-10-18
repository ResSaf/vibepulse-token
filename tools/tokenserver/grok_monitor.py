"""Default-off Grok activity and explicitly sourced usage; no relay data."""
import json
import logging
import math
import os
import threading
import time
import unicodedata
from datetime import datetime
from urllib.parse import quote
from urllib.request import Request, urlopen

if __package__:
    from .state_files import state_dir
else:
    from state_files import state_dir

log = logging.getLogger('tokenserver.grok')
MAX_INT = 2147483647
STATES = ('waiting', 'error', 'working', 'done', 'idle')
LEASES = {'working': 600, 'waiting': 7200, 'error': 1800, 'done': 1800, 'idle': 1800}


def bounded(value):
    return max(0, min(MAX_INT, int(value)))


def text(value):
    if not isinstance(value, str):
        raise ValueError('invalid text')
    clean = ''.join(c for c in value if not unicodedata.category(c).startswith('C'))
    return clean


def display(value):
    # The panel's UI fonts carry printable ASCII only; fold accents and drop
    # anything else rather than shipping glyphs that render as boxes.
    folded = unicodedata.normalize('NFKD', text(value).upper())
    ascii_text = ''.join(c for c in folded if ' ' <= c <= '~').strip()
    return ascii_text[:16].strip()


def number(value):
    if type(value) not in (int, float) or (type(value) is float and not math.isfinite(value)):
        raise ValueError('invalid number')
    return value


class JsonFile:
    """Read only changed files; malformed input never leaks into logs."""
    def __init__(self, path):
        self.path = path
        self.signature = None
        self.data = None
        self.mtime = None
        self.warned = False

    def read(self, validate):
        try:
            stat = self.path.stat()
            signature = (stat.st_mtime_ns, stat.st_size)
            if signature == self.signature:
                return self.data
            self.signature = signature
            if stat.st_size > 65536:
                raise ValueError('oversized file')
            data = validate(json.loads(self.path.read_text(encoding='utf-8')))
            self.data, self.mtime = data, stat.st_mtime
        except FileNotFoundError:
            self.signature = self.data = self.mtime = None
        except (OSError, ValueError, TypeError, KeyError, RecursionError, OverflowError):
            self.data = None
            if not self.warned:
                log.warning('%s: invalid input ignored', self.path.name)
                self.warned = True
        return self.data


class GrokBotStore:
    def __init__(self, clock=time.time):
        self.clock = clock
        self._lock = threading.RLock()
        self._bots = {}
        self._day = None
        self._done = 0
        self._file = JsonFile(state_dir() / 'grok-bots.json')
        self._loaded = None

    def _validate(self, data, file=False):
        required = {'bot', 'state'} | ({'updatedAt'} if file else {'v'})
        if (not isinstance(data, dict) or not required <= data.keys() or
                data.keys() - required - {'activity'} or
                (not file and (type(data['v']) is not int or data['v'] != 1))):
            raise ValueError('invalid bot schema')
        name = text(data['bot'])
        if (not 1 <= len(name) <= 32 or not display(name) or
                data['state'] not in STATES):
            raise ValueError('invalid bot')
        activity = display(data['activity']) if 'activity' in data else ''
        stamp = number(data['updatedAt']) if file else self.clock()
        if stamp < 0 or stamp > self.clock():
            raise ValueError('invalid timestamp')
        return (name.upper(), {'name': display(name), 'state': data['state'],
                               'activity': activity, 'updatedAt': stamp})

    def _roll_day(self):
        day = datetime.fromtimestamp(self.clock()).date()
        if day != self._day:
            self._day, self._done = day, 0

    def _put(self, name, bot):
        self._roll_day()
        old = self._bots.get(name)
        if old and old['updatedAt'] >= bot['updatedAt']:
            # Live updates may share a clock tick; exact file replays cannot.
            if old == bot or old['updatedAt'] > bot['updatedAt']:
                return
        if bot['state'] == 'done' and (not old or old['state'] != 'done'):
            if datetime.fromtimestamp(bot['updatedAt']).date() == self._day:
                self._done = min(MAX_INT, self._done + 1)
        self._bots[name] = bot
        if len(self._bots) > 16:
            del self._bots[min(self._bots, key=lambda key: self._bots[key]['updatedAt'])]

    def accept(self, data):
        name, bot = self._validate(data)
        with self._lock:
            self._refresh()
            self._put(name, bot)

    def _refresh(self):
        def validate(data):
            if (not isinstance(data, dict) or set(data) != {'v', 'bots'} or
                    type(data['v']) is not int or data['v'] != 1 or
                    not isinstance(data['bots'], list) or len(data['bots']) > 256):
                raise ValueError('invalid bots file')
            return [self._validate(bot, file=True) for bot in data['bots']]
        records = self._file.read(validate)
        if records is not None and self._file.signature != self._loaded:
            self._loaded = self._file.signature
            for name, bot in records:
                self._put(name, bot)
        now = self.clock()
        self._bots = {k: b for k, b in self._bots.items()
                      if now - b['updatedAt'] < LEASES[b['state']]}
        self._roll_day()

    def snapshot(self):
        with self._lock:
            self._refresh()
            bots = sorted(self._bots.values(),
                          key=lambda b: (STATES.index(b['state']), -b['updatedAt']))
            return {'active': sum(b['state'] in ('working', 'waiting') for b in bots),
                    'doneToday': self._done, 'botCount': len(bots),
                    'bots': [{k: b[k] for k in ('name', 'state', 'activity')
                              if k != 'activity' or b[k]} for b in bots[:4]]}


class BotActivitySource:
    def __init__(self, store):
        self.store = store

    def snapshot(self, allow_empty=False):
        data = self.store.snapshot()
        if not allow_empty and not data['botCount'] and not data['doneToday']:
            return None  # no bot has reported: fall through to the next source
        return {'source': 'bots', 'stale': False,
                'active': data['active'], 'doneToday': data['doneToday']}


class ManualQuotaSource:
    def __init__(self, clock=time.time):
        self.clock = clock
        self._file = JsonFile(state_dir() / 'grok.json')
        self._lock = threading.Lock()

    @staticmethod
    def _validate(data):
        if (not isinstance(data, dict) or not {'v', 'usedPct'} <= data.keys() or
                data.keys() - {'v', 'usedPct', 'resetAt', 'label'} or
                type(data['v']) is not int or data['v'] != 1 or
                not 0 <= number(data['usedPct']) <= 100):
            raise ValueError('invalid quota')
        if 'resetAt' in data and number(data['resetAt']) < 0:
            raise ValueError('invalid reset')
        if 'label' in data and len(text(data['label'])) > 16:
            raise ValueError('invalid label')
        return data

    def snapshot(self):
        with self._lock:
            data = self._file.read(self._validate)
            if data is None:
                return None
            age = bounded(self.clock() - self._file.mtime)
            result = {'source': 'manual', 'stale': age > 600,
                      'ageSeconds': age, 'usedPct': int(data['usedPct'])}
            if 'resetAt' in data:
                result['resetSeconds'] = bounded(data['resetAt'] - self.clock())
            if 'label' in data:
                result['label'] = display(data['label'])
            return result


class XaiApiUsageSource:
    def __init__(self, fetch=None, clock=time.time):
        self.clock = clock
        self._key = os.environ.get('XAI_MANAGEMENT_KEY')
        self._team = os.environ.get('XAI_TEAM_ID')
        self.configured = bool(self._key and self._team)
        self._fetch = fetch or self._request
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread = None
        self._data = None
        self._success = None
        self._failed = False

    def _request(self):
        request = Request('https://management-api.x.ai/v1/billing/teams/' +
                          quote(self._team, safe='') + '/postpaid/invoice/preview',
                          headers={'Authorization': 'Bearer ' + self._key})
        with urlopen(request, timeout=15) as response:
            return json.load(response)

    def poll(self):
        if not self.configured:
            return
        try:
            reply = self._fetch()
            core = reply['coreInvoice']
            result = {}
            value = core.get('totalWithCorr', {}).get('val')
            if value is None:
                value = core.get('amountAfterVat')
            if value is not None:
                result['spentCents'] = bounded(int(value))
            value = core.get('prepaidCredits', {}).get('val')
            if value is not None:
                result['prepaidCents'] = bounded(-int(value))
            value = reply.get('effectiveSpendingLimit')
            if value is not None and int(value) > 0:
                result['limitCents'] = bounded(int(value))
            if not result:
                raise ValueError('missing billing values')
        except Exception:
            # Exception messages/HTTP bodies can contain credentials. Never log them.
            log.warning('xAI billing poll failed; retaining last good reading')
            with self._lock:
                self._failed = True
            return
        with self._lock:
            self._data, self._success, self._failed = result, self.clock(), False

    def _run(self):
        while not self._stop.is_set():
            self.poll()
            if self._stop.wait(300):
                break

    def start(self):
        if self.configured and self._thread is None:
            self._stop.clear()
            self._thread = threading.Thread(target=self._run, daemon=True)
            self._thread.start()

    def stop(self):
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=16)
            self._thread = None

    def snapshot(self):
        if not self.configured:
            return None
        with self._lock:
            result = {'source': 'xai', 'stale': True}
            if self._data is not None:
                age = bounded(self.clock() - self._success)
                result.update(self._data, ageSeconds=age,
                              stale=self._failed or age > 600)
            return result


def grok_snapshot(monitor=None):
    if monitor is None:
        return {'v': 1, 'enabled': False}
    return monitor.snapshot()


class GrokMonitor:
    def __init__(self, source='auto', clock=time.time, fetch=None):
        if source not in ('auto', 'xai', 'manual', 'bots'):
            raise ValueError('invalid Grok source')
        self.source = source
        self.store = GrokBotStore(clock)
        self.xai = XaiApiUsageSource(fetch, clock)
        self.manual = ManualQuotaSource(clock)
        self.bots = BotActivitySource(self.store)
        self._lock = threading.Lock()

    def start(self):
        if self.source in ('auto', 'xai'):
            self.xai.start()

    def stop(self):
        self.xai.stop()

    def snapshot(self):
        with self._lock:
            data = None
            # Auto: measured xAI API spend first, then live bot activity, then
            # the owner's manually copied quota. Nothing configured -> 'none'.
            for name in (('xai', 'bots', 'manual') if self.source == 'auto'
                         else (self.source,)):
                source = getattr(self, name)
                # An explicitly chosen bot source reports real zeros; in auto
                # mode an empty bot store defers to the manual quota.
                data = (source.snapshot(allow_empty=self.source == 'bots')
                        if name == 'bots' else source.snapshot())
                if data is not None:
                    break
            result = {'v': 1, 'enabled': True, **(data or {'source': 'none', 'stale': True})}
            bots = self.store.snapshot()
            result.update({k: bots[k] for k in ('active', 'doneToday', 'bots')})
            # JSON escaping can expand arbitrary Unicode/control-like text. Keep
            # the actual default json.dumps wire representation below 480 bytes.
            while len(json.dumps(result).encode('utf-8')) >= 480 and result['bots']:
                result['bots'].pop()
            return result

    def summary(self):
        data = self.snapshot()
        return {**{k: data[k] for k in ('enabled', 'source', 'stale')},
                'botCount': self.store.snapshot()['botCount']}
