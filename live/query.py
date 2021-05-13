import time
import signal
import inspect
import threading
import warnings
import os

from .object import LoggingObject
from .exceptions import LiveConnectionError

# TODO could probably refactor this import handling section a bit, to reduce the
# amount of hardcoded duplicates of the same strings... a loop over packages,
# in priority order?

# It might be nice to default to 'pythonosc', since then it would allow this
# package to work right out of the box if installed with the pythonosc option
# (like `pip install pylive[pythonosc]`), without requiring the user to set this
# environment variable. Defaulting to 'pythonosc' like this would only affect
# people using `pylive` with `liblo` if they also happen to have `pythonosc`
# installed. Since the pythonosc support is still a bit shaky, I don't think
# it makes sense as a default yet.
PYLIVE_BACKEND = os.environ.get('PYLIVE_BACKEND', 'liblo')

supported_backends = ['pythonosc', 'liblo']
if PYLIVE_BACKEND not in supported_backends:
    warnings.warn('PYLIVE_BACKEND="{}" not in supported backends: {}'.format(
        PYLIVE_BACKEND, supported_backends
    ))
    PYLIVE_BACKEND = 'pythonosc'

OSC_BACKEND = None
if PYLIVE_BACKEND == 'pythonosc':
    try:
        from pythonosc.dispatcher import Dispatcher
        from pythonosc.osc_server import ThreadingOSCUDPServer
        from pythonosc.udp_client import SimpleUDPClient
        OSC_BACKEND = 'pythonosc'

    # TODO test this is always the right error. is it ever ModuleNotFoundError,
    # and if so, does this match that?
    except ImportError:
        warnings.warn('trying PYLIVE_BACKEND=liblo because could not import '
            'pythonosc'
        )
        PYLIVE_BACKEND = 'liblo'

if PYLIVE_BACKEND == 'liblo':
    import liblo
    OSC_BACKEND = 'liblo'

assert OSC_BACKEND is not None


def singleton(cls):
    instances = {}
    def getinstance(*args):
        if cls not in instances:
            instances[cls] = cls(*args)
        return instances[cls]
    return getinstance

#------------------------------------------------------------------------
# Helper methods to save instantiating an object when making calls.
#------------------------------------------------------------------------

def query(*args, **kwargs):
    return Query().query(*args, **kwargs)

def cmd(*args, **kwargs):
    Query().cmd(*args, **kwargs)

@singleton
class Query(LoggingObject):
    """ Object responsible for passing OSC queries to the LiveOSC server,
    parsing and proxying responses.

    This object is a singleton, under the assumption that only one Live instance
    can be running, so only one global Live Query object should be needed.

    Following this assumption, static helper functions also exist:

        live.query(path, *args)
        live.cmd(path, *args)
    """

    def __init__(self, address=("127.0.0.1", 9900), listen_port=9002):
        self.beat_callback = None
        self.startup_callback = None
        self.listen_port = listen_port

        #------------------------------------------------------------------------
        # Handler callbacks for particular messages from Live.
        # Used so that other processes can register callbacks when states change.
        #------------------------------------------------------------------------
        self.handlers = {}

        self.osc_address = address
        if OSC_BACKEND == 'liblo':
            self.osc_target = liblo.Address(address[0], address[1])
            self.osc_server = liblo.Server(listen_port)
            self.osc_server.add_method(None, None, self.handler)
            self.osc_server.add_bundle_handlers(
                self.start_bundle_handler, self.end_bundle_handler
            )

        elif OSC_BACKEND == 'pythonosc':
            # TODO how to deal w/ bundles? even necessary?
            # (the handlers seem to be just logging...)
            # (i think only some of the clip code refers to bundles at all)

            ip = address[0]
            self.osc_client = SimpleUDPClient(ip, address[1])

            self.dispatcher = Dispatcher()
            self.dispatcher.set_default_handler(self.pythonosc_handler_wrapper)

            # TODO TODO may need to take more care that this, or the other
            # pythonosc objects, actually close all of their connections before
            # exit / atexit
            # for some reason, maybe most likely something else, there seem to
            # be less frequent apparent "connection" issues with liblo than with
            # pythonosc...
            self.osc_server = ThreadingOSCUDPServer((ip, listen_port),
                self.dispatcher
            )

        self.osc_server_thread = None

        self.osc_read_event = None
        self.osc_timeout = 3.0

        self.osc_server_events = {}

        self.query_address = None
        self.query_rv = []

        self.listen()

    def osc_server_read(self):
        assert OSC_BACKEND == 'liblo'
        while True:
            self.osc_server.recv(10)

    def listen(self):
        if OSC_BACKEND == 'liblo':
            target = self.osc_server_read
        elif OSC_BACKEND == 'pythonosc':
            target = self.osc_server.serve_forever

        self.osc_server_thread = threading.Thread(target=target)
        self.osc_server_thread.setDaemon(True)
        self.osc_server_thread.start()

    def stop(self):
        """ Terminate this query object and unbind from OSC listening. """
        pass

    def cmd(self, msg, *args):
        """ Send a Live command without expecting a response back:

            live.cmd("/live/tempo", 110.0) """
        
        self.log_debug("OSC output: %s %s", msg, args)
        try:
            if OSC_BACKEND == 'liblo':
                liblo.send(self.osc_target, msg, *args)

            elif OSC_BACKEND == 'pythonosc':
                # not clear on whether this unpacking in len(1) case in
                # necessary, just trying to make it look like examples in docs
                if len(args) == 1:
                    args = args[0]

                self.osc_client.send_message(msg, args)
    
        # TODO TODO need to modify pythonosc client call / handling so it will
        # also raise an error in this case? (probably)
        except Exception as e:
            self.log_debug(f"During cmd({msg}, {args})")
            raise LiveConnectionError("Couldn't send message to Live (is LiveOSC present and activated?)")


    # TODO maybe compute something like the average latency for a response to
    # arrive for a query (maybe weighted by recency) for debugging whether the
    # timeout is reasonable?
    # TODO + number of commands already processed / sent maybe + maybe log
    # whether particular commands always fail?

    def query(self, msg, *args, **kwargs):
        """ Send a Live command and synchronously wait for its response:

            return live.query("/live/tempo")

        Returns a list of values. """

        #------------------------------------------------------------------------
        # Use **kwargs because we want to be able to specify an optional kw
        # arg after variable-length args -- 
        # eg live.query("/set/freq", 440, 1.0, response_address = "/verify/freq")
        #
        # http://stackoverflow.com/questions/5940180/python-default-keyword-arguments-after-variable-length-positional-arguments
        #------------------------------------------------------------------------

        #------------------------------------------------------------------------
        # Some calls produce responses at different addresses
        # (eg /live/device -> /live/deviceall). Specify a response_address to
        # take account of this.
        #------------------------------------------------------------------------
        response_address = kwargs.get("response_address", None)
        if response_address:
            response_address = response_address
        else:
            response_address = msg

        #------------------------------------------------------------------------
        # Create an Event to block the thread until this response has been
        # triggered.
        #------------------------------------------------------------------------
        self.osc_server_events[response_address] = threading.Event()

        #------------------------------------------------------------------------
        # query_rv will be populated by the callback, storing the return value
        # of the OSC query.
        #------------------------------------------------------------------------
        self.query_address = response_address
        self.query_rv = []
        self.cmd(msg, *args)

        #------------------------------------------------------------------------
        # Wait for a response. 
        #------------------------------------------------------------------------
        timeout = kwargs.get("timeout", self.osc_timeout)
        rv = self.osc_server_events[response_address].wait(timeout)

        if not rv:
            self.log_debug(f"Timeout during query({msg}, {args}, {kwargs})")
            # TODO could change error message to not question whether LiveOSC
            # is setup correctly if there has been any successful communication
            # so far...
            raise LiveConnectionError("Timed out waiting for response from LiveOSC. Is Live running and LiveOSC installed?")

        return self.query_rv

    # TODO maybe pythonosc.osc_bundle_builder / osc_message_builder could
    # replace some of what these are doing (in OSC_BACKEND == 'pythonosc' case)?
    # (though not clear these are critical...)
    def start_bundle_handler(self, *args):
        assert OSC_BACKEND == 'liblo'
        self.log_debug("OSC: start bundle")

    def end_bundle_handler(self, *args):
        assert OSC_BACKEND == 'liblo'
        self.log_debug("OSC: end bundle")

    def pythonosc_handler_wrapper(self, address, *args):
        assert OSC_BACKEND == 'pythonosc'
        # TODO may need to unwrap len(args) == 0 case or something like that
        self.handler(address, args, None)

    def handler(self, address, data, types):
        self.log_debug("OSC input: %s %s" % (address, data))

        #------------------------------------------------------------------------
        # Execute any callbacks that have been registered for this message
        #------------------------------------------------------------------------
        if address in self.handlers:
            for handler in self.handlers[address]:
                handler(*data)

        #------------------------------------------------------------------------
        # If this message is awaiting a synchronous return, trigger the
        # thread event and update our return value. 
        #------------------------------------------------------------------------
        if address == self.query_address:
            self.query_rv += data
            self.osc_server_events[address].set()
            return

        if address == "/live/beat":
            if self.beat_callback is not None:
                #------------------------------------------------------------------------
                # Beat callbacks are used if we want to trigger an event on each beat,
                # to synchronise with the timing of the Live set.
                #
                # Callbacks may take one argument: the current beat count.
                # If not specified, call with 0 arguments.
                #------------------------------------------------------------------------
                has_arg = False
                try:
                    signature = inspect.signature(self.beat_callback)
                    has_arg = len(signature.parameters) > 0
                except:
                    # Python 2
                    argspec = inspect.getargspec(self.beat_callback)
                    has_arg = len(argspec.args) > 0 and argspec.args[-1] != "self"

                if has_arg:
                    self.beat_callback(data[0])
                else:
                    self.beat_callback()

        elif address == "/remix/oscserver/startup":
            if self.startup_callback is not None:
                self.startup_callback()

    def add_handler(self, address, handler):
        if not address in self.handlers:
            self.handlers[address] = []
        self.handlers[address].append(handler)

