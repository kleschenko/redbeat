# Licensed under the Apache License, Version 2.0 (the 'License'); you may not
# use this file except in compliance with the License. You may obtain a copy
# of the License at http://www.apache.org/licenses/LICENSE-2.0
# Copyright 2015 Marc Sibson


import datetime
import time

try:
    import simplejson as json
except ImportError:
    import json

from celery.beat import Scheduler, ScheduleEntry
from celery.utils.log import get_logger
from celery.signals import beat_init
from celery.utils.timeutils import humanize_seconds
from celery.app import app_or_default

from redis.client import StrictRedis

from decoder import RedBeatJSONEncoder, RedBeatJSONDecoder


def redis(app=None):
    url = app_or_default(app).conf.REDBEAT_REDIS_URL
    return StrictRedis.from_url(url)


def add_defaults(app=None):
    app = app_or_default(app)

    app.add_defaults({
        'REDBEAT_REDIS_URL': app.conf['BROKER_URL'],
        'REDBEAT_KEY_PREFIX': 'redbeat:',
        'REDBEAT_SCHEDULE_KEY': app.conf.get('REDBEAT_KEY_PREFIX', 'redbeat:') + ':schedule',
        'REDBEAT_STATICS_KEY': app.conf.get('REDBEAT_KEY_PREFIX', 'redbeat:') + ':statics',
        'REDBEAT_LOCK_KEY': app.conf.get('REDBEAT_KEY_PREFIX', 'redbeat:') + ':lock',
        'REDBEAT_LOCK_TIMEOUT': app.conf.CELERYBEAT_MAX_LOOP_INTERVAL * 5,
    })


ADD_ENTRY_ERROR = """\

Couldn't add entry %r to redis schedule: %r. Contents: %r
"""

logger = get_logger(__name__)


def to_timestamp(dt):
    return time.mktime(dt.timetuple())


class RedBeatSchedulerEntry(ScheduleEntry):
    _meta = None

    def __init__(self, name=None, task=None, schedule=None, args=None, kwargs=None, enabled=True, **clsargs):
        super(RedBeatSchedulerEntry, self).__init__(name, task, schedule=schedule,
                                                    args=args, kwargs=kwargs, **clsargs)
        self.enabled = enabled

    @staticmethod
    def load_definition(key, app=None):
        definition = redis(app).hget(key, 'definition')
        if not definition:
            raise KeyError(key)

        return json.loads(definition, cls=RedBeatJSONDecoder)

    @staticmethod
    def load_meta(key, app=None):
        meta = redis(app).hget(key, 'meta')
        if not meta:
            return {'last_run_at': datetime.datetime.min}

        return json.loads(meta, cls=RedBeatJSONDecoder)

    @staticmethod
    def from_key(key, app=None):
        definition = RedBeatSchedulerEntry.load_definition(key, app)
        meta = RedBeatSchedulerEntry.load_meta(key, app)
        definition.update(meta)

        return RedBeatSchedulerEntry(**definition)

    @property
    def due_at(self):
        delta = self.schedule.remaining_estimate(self.last_run_at)
        return self.last_run_at + delta

    @property
    def key(self):
        return app_or_default(self.app).conf['REDBEAT_KEY_PREFIX'] + self.name

    @property
    def score(self):
        return to_timestamp(self.due_at)

    @property
    def redis(self):
        return redis(self.app)

    def save(self):
        definition = {
            'name': self.name,
            'task': self.task,
            'args': self.args,
            'kwargs': self.kwargs,
            'options': self.options,
            'schedule': self.schedule,
            'enabled': self.enabled,
        }
        self.redis.hset(self.key, 'definition', json.dumps(definition, cls=RedBeatJSONEncoder))
        self.redis.zadd(self.app.conf.REDBEAT_SCHEDULE_KEY, self.score, self.key)

    def delete(self):
        self.redis.zrem(self.app.conf.REDBEAT_SCHEDULE_KEY, self.key)
        self.redis.delete(self.key)

    def next(self, last_run_at=None):
        # TODO handle meta not loaded
        self.last_run_at = last_run_at or self._default_now()
        self.total_run_count += 1

        meta = {
            'last_run_at': self.last_run_at,
            'total_run_count': self.total_run_count,
        }
        self.redis.hset(self.key, 'meta', json.dumps(meta, cls=RedBeatJSONEncoder))
        self.redis.zadd(REDBEAT_SCHEDULE_KEY, self.score, self.key)

        return self
    __next__ = next

    def reschedule(self, last_run_at=None):
        self.last_run_at = last_run_at or self._default_now()
        meta = {
            'last_run_at': self.last_run_at,
        }
        self.redis.hset(self.key, 'meta', json.dumps(meta, cls=RedBeatJSONEncoder))
        self.redis.zadd(self.app.conf.REDBEAT_SCHEDULE_KEY, self.score, self.key)

    def is_due(self):
        if not self.enabled:
            return False, 5.0  # 5 second delay for re-enable.

        return super(RedBeatSchedulerEntry, self).is_due()


class RedBeatScheduler(Scheduler):
    # how often should we sync in schedule information
    # from the backend redis database
    Entry = RedBeatSchedulerEntry

    lock = None

    def __init__(self, app, **kwargs):
        add_defaults(app)
        self.lock_key = kwargs.pop('lock_key', app.conf.REDBEAT_LOCK_KEY)
        self.lock_timeout = kwargs.pop('lock_timeout', app.conf.REDBEAT_LOCK_TIMEOUT)
        super(RedBeatScheduler, self).__init__(app, **kwargs)

    @property
    def redis(self):
        return redis(self.app)

    def setup_schedule(self):
        # cleanup old static entries
        previous = self.redis.smembers(self.app.conf.REDBEAT_STATICS_KEY)
        current = set(self.app.conf.CELERYBEAT_SCHEDULE.keys())
        removed = previous - current
        for name in removed:
            RedBeatSchedulerEntry(name).delete()

        # setup statics
        self.install_default_entries(self.app.conf.CELERYBEAT_SCHEDULE)
        if not self.app.conf.CELERYBEAT_SCHEDULE:
            return

        self.update_from_dict(self.app.conf.CELERYBEAT_SCHEDULE)

        # track static entries
        self.redis.sadd(self.app.conf.REDBEAT_STATICS_KEY, *self.app.conf.CELERYBEAT_SCHEDULE.keys())

    def update_from_dict(self, dict_):
        for name, entry in dict_.items():
            try:
                entry = self._maybe_entry(name, entry)
            except Exception as exc:
                logger.error(ADD_ENTRY_ERROR, name, exc, entry)
                continue

            entry.save()  # store into redis
            logger.debug(unicode(entry))

    def reserve(self, entry):
        new_entry = next(entry)
        return new_entry

    @property
    def schedule(self):
        # need to peek into the next tick to accurate calculate our sleep time
        logger.debug('Selecting tasks')
        max_due_at = to_timestamp(self.app.now() + datetime.timedelta(seconds=self.max_interval))
        due_tasks = self.redis.zrangebyscore(self.app.conf.REDBEAT_SCHEDULE_KEY, 0, max_due_at)

        logger.info('Loading %d tasks', len(due_tasks))
        d = {}
        for key in due_tasks:
            try:
                entry = self.Entry.from_key(key, app=self.app)
            except KeyError:
                logger.warning('failed to load %s, removing', key)
                self.redis.zrem(self.app.conf.REDBEAT_SCHEDULE_KEY, key)
                continue

            d[entry.name] = entry

        logger.debug('Processing tasks')

        return d

    def tick(self, **kwargs):
        if self.lock:
            logger.debug('beat: Extending lock...')
            self.redis.pexpire(self.lock_key, int(self.lock_timeout * 1000))
        return super(RedBeatScheduler, self).tick(**kwargs)

    def close(self):
        if self.lock:
            logger.debug('beat: Releasing Lock')
            self.lock.release()
            self.lock = None
        super(RedBeatScheduler, self).close()

    @property
    def info(self):
        info = ['       . redis -> {}'.format(self.app.conf.REDBEAT_REDIS_URL)]
        if self.lock_key:
            info.append('       . lock -> `{}` {} ({}s)'.format(self.lock_key, humanize_seconds(self.lock_timeout), self.lock_timeout))
        return '\n'.join(info)


@beat_init.connect
def acquire_distributed_beat_lock(sender=None, **kwargs):
    scheduler = sender.scheduler
    if not scheduler.lock_key:
        return

    lock = redis(scheduler.app).lock(scheduler.lock_key, timeout=scheduler.lock_timeout, sleep=scheduler.max_interval)
    logger.debug('bett: Acquiring lock...')
    lock.acquire()
    scheduler.lock = lock
