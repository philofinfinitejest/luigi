# Copyright (c) 2012 Spotify AB
#
# Licensed under the Apache License, Version 2.0 (the "License"); you may not
# use this file except in compliance with the License. You may obtain a copy of
# the License at
#
# http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS, WITHOUT
# WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied. See the
# License for the specific language governing permissions and limitations under
# the License.

# Simple REST server that takes commands in a JSON payload
import json
import os
import atexit
import mimetypes
import posixpath
import tornado.ioloop
import tornado.netutil
import tornado.web
import tornado.httpclient
import tornado.httpserver
import configuration
import scheduler
import pkg_resources
import signal
import task_history
import logging
logger = logging.getLogger("luigi.server")


def _create_scheduler():
    config = configuration.get_config()
    retry_delay = config.getfloat('scheduler', 'retry-delay', 900.0)
    remove_delay = config.getfloat('scheduler', 'remove-delay', 600.0)
    worker_disconnect_delay = config.getfloat('scheduler', 'worker-disconnect-delay', 60.0)
    state_path = config.get('scheduler', 'state-path', '/var/lib/luigi-server/state.pickle')

    # Jobs are disabled if we see more than disable_failures failures in disable_window seconds.
    # These disables last for disable_persist seconds.
    disable_window = config.getint('scheduler', 'disable-window-seconds', 3600)
    disable_failures = config.getint('scheduler', 'disable-num-failures', None)
    disable_persist = config.getint('scheduler', 'disable-persist-seconds', 86400)
    max_shown_tasks = config.getint('scheduler', 'max-shown-tasks', 100000)

    resources = config.getintdict('resources')
    if config.getboolean('scheduler', 'record_task_history', False):
        import db_task_history  # Needs sqlalchemy, thus imported here
        task_history_impl = db_task_history.DbTaskHistory()
    else:
        task_history_impl = task_history.NopHistory()
    return scheduler.CentralPlannerScheduler(
        retry_delay, remove_delay, worker_disconnect_delay, state_path, task_history_impl,
        resources, disable_persist, disable_window, disable_failures, max_shown_tasks,
    )


class RPCHandler(tornado.web.RequestHandler):

    """ Handle remote scheduling calls using rpc.RemoteSchedulerResponder"""

    def initialize(self, scheduler):
        self._scheduler = scheduler

    def get(self, method):
        payload = self.get_argument('data', default="{}")
        arguments = json.loads(payload)

        # TODO: we should probably denote all methods on the scheduler that are "API-level"
        # versus internal methods. Right now you can do a REST method call to any method
        # defined on the scheduler, which is pretty bad from a security point of view.

        if hasattr(self._scheduler, method):
            result = getattr(self._scheduler, method)(**arguments)
            self.write({"response": result})  # wrap all json response in a dictionary
        else:
            self.send_error(404)

    post = get


class BaseTaskHistoryHandler(tornado.web.RequestHandler):

    def initialize(self, scheduler):
        self._scheduler = scheduler

    def get_template_path(self):
        return pkg_resources.resource_filename(__name__, 'templates')


class RecentRunHandler(BaseTaskHistoryHandler):

    def get(self):
        tasks = self._scheduler.task_history.find_latest_runs()
        self.render("recent.html", tasks=tasks)


class ByNameHandler(BaseTaskHistoryHandler):

    def get(self, name):
        tasks = self._scheduler.task_history.find_all_by_name(name)
        self.render("recent.html", tasks=tasks)


class ByIdHandler(BaseTaskHistoryHandler):

    def get(self, id):
        task = self._scheduler.task_history.find_task_by_id(id)
        self.render("show.html", task=task)


class ByParamsHandler(BaseTaskHistoryHandler):

    def get(self, name):
        payload = self.get_argument('data', default="{}")
        arguments = json.loads(payload)
        tasks = self._scheduler.task_history.find_all_by_parameters(name, session=None, **arguments)
        self.render("recent.html", tasks=tasks)


class StaticFileHandler(tornado.web.RequestHandler):

    def get(self, path):
        # Path checking taken from Flask's safe_join function:
        # https://github.com/mitsuhiko/flask/blob/1d55b8983/flask/helpers.py#L563-L587
        path = posixpath.normpath(path)
        if os.path.isabs(path) or path.startswith(".."):
            return self.send_error(404)

        extension = os.path.splitext(path)[1]
        if extension in mimetypes.types_map:
            self.set_header("Content-Type", mimetypes.types_map[extension])
        data = pkg_resources.resource_string(__name__, os.path.join("static", path))
        self.write(data)


class RootPathHandler(tornado.web.RequestHandler):

    def get(self):
        self.redirect("/static/visualiser/index.html")


def app(scheduler):
    handlers = [
        (r'/api/(.*)', RPCHandler, {"scheduler": scheduler}),
        (r'/static/(.*)', StaticFileHandler),
        (r'/', RootPathHandler),
        (r'/history', RecentRunHandler, {'scheduler': scheduler}),
        (r'/history/by_name/(.*?)', ByNameHandler, {'scheduler': scheduler}),
        (r'/history/by_id/(.*?)', ByIdHandler, {'scheduler': scheduler}),
        (r'/history/by_params/(.*?)', ByParamsHandler, {'scheduler': scheduler})
    ]
    api_app = tornado.web.Application(handlers)
    return api_app


def _init_api(sched, responder=None, api_port=None, address=None):
    if responder:
        raise Exception('The "responder" argument is no longer supported')
    api_app = app(sched)
    api_sockets = tornado.netutil.bind_sockets(api_port, address=address)
    server = tornado.httpserver.HTTPServer(api_app)
    server.add_sockets(api_sockets)

    # Return the bound socket names.  Useful for connecting client in test scenarios.
    return [s.getsockname() for s in api_sockets]


def run(api_port=8082, address=None, scheduler=None, responder=None):
    """ Runs one instance of the API server """
    sched = scheduler or _create_scheduler()
    # load scheduler state
    sched.load()

    _init_api(sched, responder, api_port, address)

    # prune work DAG every 60 seconds
    pruner = tornado.ioloop.PeriodicCallback(sched.prune, 60000)
    pruner.start()

    def shutdown_handler(foo=None, bar=None):
        logger.info("Scheduler instance shutting down")
        sched.dump()
        os._exit(0)

    signal.signal(signal.SIGINT, shutdown_handler)
    signal.signal(signal.SIGTERM, shutdown_handler)
    if os.name == 'nt':
        signal.signal(signal.SIGBREAK, shutdown_handler)
    else:
        signal.signal(signal.SIGQUIT, shutdown_handler)
    atexit.register(shutdown_handler)

    logger.info("Scheduler starting up")

    tornado.ioloop.IOLoop.instance().start()


def run_api_threaded(api_port=8082, address=None):
    ''' For integration tests'''
    sock_names = _init_api(_create_scheduler(), None, api_port, address)

    import threading

    def scheduler_thread():
        # this is wrapped in a function so we get the instance
        # from the scheduler thread and not from the main thread
        tornado.ioloop.IOLoop.instance().start()

    threading.Thread(target=scheduler_thread).start()
    return sock_names


def stop():
    tornado.ioloop.IOLoop.instance().stop()

if __name__ == "__main__":
    run()
