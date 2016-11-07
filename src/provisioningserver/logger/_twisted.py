# Copyright 2016 Canonical Ltd.  This software is licensed under the
# GNU Affero General Public License version 3 (see the file LICENSE).

"""Twisted-specific logging stuff."""

__all__ = [
    "configure_twisted_logging",
    "LegacyLogger",
    "VerbosityOptions",
]

import re
import sys
import warnings

import crochet
from provisioningserver.logger._common import (
    DEFAULT_LOG_FORMAT_DATE,
    DEFAULT_LOG_VERBOSITY,
    DEFAULT_LOG_VERBOSITY_LEVELS,
    get_module_for_file,
    LoggingMode,
    warn_unless,
)
from provisioningserver.utils import typed
from twisted import logger as twistedModern
from twisted.python import (
    log as twistedLegacy,
    usage,
)

# Map verbosity numbers to `twisted.logger` levels.
DEFAULT_TWISTED_VERBOSITY_LEVELS = {
    # verbosity: level
    0: twistedModern.LogLevel.error,
    1: twistedModern.LogLevel.warn,
    2: twistedModern.LogLevel.info,
    3: twistedModern.LogLevel.debug,
}

# Belt-n-braces.
assert (
    DEFAULT_TWISTED_VERBOSITY_LEVELS.keys() == DEFAULT_LOG_VERBOSITY_LEVELS), (
        "Twisted verbosity map does not match expectations.")


# Those levels for which we should emit log events.
_filterByLevels = frozenset()


def _filterByLevel(event):
    """Only log if event's level is in `_filterByLevels`."""
    if event.get("log_level") in _filterByLevels:
        return twistedModern.PredicateResult.maybe
    else:
        return twistedModern.PredicateResult.no


@typed
def set_twisted_verbosity(verbosity: int):
    """Reconfigure verbosity of the standard library's `logging` module."""
    # Convert `verbosity` into a Twisted `LogLevel`.
    level = get_twisted_logging_level(verbosity)
    # `LogLevel` is comparable, but this saves overall computation.
    global _filterByLevels
    _filterByLevels = {
        ll for ll in twistedModern.LogLevel.iterconstants()
        if ll >= level
    }


@typed
def configure_twisted_logging(verbosity: int, mode: LoggingMode):
    """Configure Twisted's legacy logging system.

    We do this because it's what `twistd` uses. When we switch to `twist` we
    can update this.

    :param verbosity: See `get_logging_level`.
    :param mode: The mode in which to configure logging. See `LoggingMode`.
    """
    set_twisted_verbosity(verbosity)

    # A list of markers for noise.
    noisy = (
        {"log_system": "-", "log_text": "Log opened."},
        {"log_system": "-", "log_text": "Main loop terminated."},
    )

    def filterByNoise(event, noisy=noisy):
        """Only log if event is not noisy."""
        for noise in noisy:
            if all(key in event and event[key] == noise[key] for key in noise):
                return twistedModern.PredicateResult.no
        else:
            return twistedModern.PredicateResult.maybe

    predicates = _filterByLevel, filterByNoise

    # When `twistd` starts the reactor it initialises the legacy logging
    # system. Intercept this to wrap the observer in a level filter. We can
    # use this same approach when not running under `twistd` too.
    def startLoggingWithObserver(observer, setStdout=1):
        observer = twistedModern.FilteringLogObserver(observer, predicates)
        reallyStartLoggingWithObserver(observer, setStdout)

    reallyStartLoggingWithObserver = twistedLegacy.startLoggingWithObserver
    twistedLegacy.startLoggingWithObserver = startLoggingWithObserver

    # Customise warnings behaviour. Ensure that nothing else — neither the
    # standard library's `logging` module nor Django — clobbers this later.
    warn_unless(warnings.showwarning.__module__ == warnings.__name__, (
        "The warnings module has already been modified; please investigate!"))
    if mode == LoggingMode.TWISTD:
        twistedModern.globalLogBeginner.showwarning = show_warning_via_twisted
        twistedLegacy.theLogPublisher.showwarning = show_warning_via_twisted
    else:
        twistedModern.globalLogBeginner.showwarning = warnings.showwarning
        twistedLegacy.theLogPublisher.showwarning = warnings.showwarning

    # Globally override Twisted's log date format. It's tricky to get to the
    # FileLogObserver that twistd installs so that we can modify its config
    # alone, but we actually do want to make a global change anyway.
    warn_unless(hasattr(twistedLegacy.FileLogObserver, "timeFormat"), (
        "No FileLogObserver.timeFormat attribute found; please investigate!"))
    twistedLegacy.FileLogObserver.timeFormat = DEFAULT_LOG_FORMAT_DATE

    # Install a wrapper so that log events from `t.logger` are logged with a
    # namespace and level by the legacy logger in `t.python.log`. This needs
    # to be injected into the `t.p.log` module in order to process events as
    # they move from the legacy to the modern systems.
    LegacyLogObserverWrapper.install()

    # Prevent `crochet` from initialising Twisted's logging.
    warn_unless(hasattr(crochet._main, "_startLoggingWithObserver"), (
        "No _startLoggingWithObserver function found; please investigate!"))
    crochet._main._startLoggingWithObserver = None

    # Turn off some inadvisable defaults in Twisted and elsewhere.
    from twisted.internet.protocol import AbstractDatagramProtocol, Factory
    warn_unless(hasattr(AbstractDatagramProtocol, "noisy"), (
        "No AbstractDatagramProtocol.noisy attribute; please investigate!"))
    AbstractDatagramProtocol.noisy = False
    warn_unless(hasattr(Factory, "noisy"), (
        "No Factory.noisy attribute; please investigate!"))
    Factory.noisy = False

    # Install filters for other noisy parts of Twisted itself.
    from twisted.internet import tcp, udp, unix
    LegacyLogger.install(tcp, observer=observe_twisted_internet_tcp)
    LegacyLogger.install(udp, observer=observe_twisted_internet_udp)
    LegacyLogger.install(unix, observer=observe_twisted_internet_unix)

    # Start Twisted logging if we're running a command. Use `sys.__stdout__`,
    # the original standard out stream when this process was started. This
    # bypasses any wrapping or redirection that may have been done elsewhere.
    if mode == LoggingMode.COMMAND:
        twistedLegacy.startLogging(sys.__stdout__, setStdout=False)


class LegacyLogObserverWrapper(twistedModern.LegacyLogObserverWrapper):
    """Ensure that `log_system` is set in the event.

    This mimics what `twisted.logger.formatEventAsClassicLogText` does when
    `log_system` is not set, and constructs it from `log_namespace` and
    `log_level`.

    This `log_system` value is then seen by `LegacyLogObserverWrapper` and
    copied into the `system` key and then printed out in the logs by Twisted's
    legacy logging (`t.python.log`) machinery. This still used by `twistd`, so
    the net effect is that the logger's namespace and level are printed to the
    `twistd` log.
    """

    @classmethod
    def install(cls):
        """Install this wrapper in place of `log.LegacyLogObserverWrapper`.

        Inject this wrapper into the `t.python.log` module then remove and
        re-add all the legacy observers so that they're re-wrapped.
        """
        twistedLegacy.LegacyLogObserverWrapper = cls
        for observer in twistedLegacy.theLogPublisher.observers:
            twistedLegacy.theLogPublisher.removeObserver(observer)
            twistedLegacy.theLogPublisher.addObserver(observer)

    def __call__(self, event):
        # Be defensive: `system` could be missing or could have a value of
        # None. Same goes for `log_system`, `log_namespace`, and `log_level`.
        if event.get("system") is None and event.get("log_system") is None:
            namespace = event.get("log_namespace")
            # Logs written directly to `t.p.log.logfile` and `.logerr` get a
            # namespace of "twisted.python.log". This is not interesting.
            if namespace == twistedLegacy.__name__:
                namespace = None
            level = event.get("log_level")
            event["log_system"] = "{namespace}#{level}".format(
                namespace=("-" if namespace is None else namespace),
                level=("-" if level is None else level.name))
        # Up-call, which will apply some more transformations.
        return super().__call__(event)


class LegacyLogger(twistedModern.Logger):
    """Looks like a stripped-down `t.p.log` module, logs to a `Logger`.

    Use this with code that cannot easily be changed to use `twisted.logger`
    but over which we want a greater degree of control.
    """

    @classmethod
    def install(cls, module, attribute="log", *, source=None, observer=None):
        """Install a `LegacyLogger` at `module.attribute`.

        Warns if `module.attribute` does not exist, but carries on anyway.

        :param module: A module (or any other object with assignable
            attributes and a `__name__`).
        :param attribute: The name of the attribute on `module` to replace.
        :param source: See `Logger.__init__`.
        :param observer: See `Logger.__init__`.
        :return: The newly created `LegacyLogger`.
        """
        replacing = getattr(module, attribute, "<not-found>")
        warn_unless(replacing is twistedLegacy, (
            "Legacy logger being installed to replace %r but expected a "
            "reference to twisted.python.log module; please investigate!"
            % (replacing,)))
        logger = cls(module.__name__, source=source, observer=observer)
        setattr(module, attribute, logger)
        return logger

    def msg(self, *message, **kwargs):
        """Write a message to the log.

        See `twisted.python.log.msg`. This allows multiple messages to be
        supplied but says that this "only works (sometimes) by accident". Here
        we make sure it works all the time on purpose.
        """
        fmt = " ".join("{_message_%d}" % i for i, _ in enumerate(message))
        kwargs.update({"_message_%d" % i: m for i, m in enumerate(message)})
        self.info(fmt, **kwargs)

    def err(self, _stuff=None, _why=None, **kwargs):
        """Write a failure to the log.

        See `twisted.python.log.err`.
        """
        self.failure("{_why}", _stuff, _why=_why, **kwargs)


class VerbosityOptions(usage.Options):
    """Command-line logging verbosity options."""

    _verbosity_max = max(DEFAULT_TWISTED_VERBOSITY_LEVELS)
    _verbosity_min = min(DEFAULT_TWISTED_VERBOSITY_LEVELS)

    def __init__(self):
        super(VerbosityOptions, self).__init__()
        self["verbosity"] = DEFAULT_LOG_VERBOSITY
        self.longOpt.sort()  # https://twistedmatrix.com/trac/ticket/8866

    def opt_verbose(self):
        """Increase logging verbosity."""
        self["verbosity"] = min(
            self._verbosity_max, self["verbosity"] + 1)

    opt_v = opt_verbose

    def opt_quiet(self):
        """Decrease logging verbosity."""
        self["verbosity"] = max(
            self._verbosity_min, self["verbosity"] - 1)

    opt_q = opt_quiet


@typed
def get_twisted_logging_level(verbosity: int):  # -> LogLevel
    """Return the Twisted logging level corresponding to `verbosity`.

    The level returned should be treated as *inclusive*. For example
    `LogLevel.info` means that informational messages ought to be logged as
    well as messages of a higher level.

    :param verbosity: 0, 1, 2, or 3, meaning very quiet logging, quiet
        logging, normal logging, and verbose/debug logging.
    """
    levels = DEFAULT_TWISTED_VERBOSITY_LEVELS
    v_min, v_max = min(levels), max(levels)
    if verbosity > v_max:
        return levels[v_max]
    elif verbosity < v_min:
        return levels[v_min]
    else:
        return levels[verbosity]


def show_warning_via_twisted(
        message, category, filename, lineno, file=None, line=None):
    """Replacement for `warnings.showwarning` that logs via Twisted."""
    if file is None:
        # Try to find a module name with which to log this warning.
        module = get_module_for_file(filename)
        logger = twistedModern.Logger(
            "global" if module is None else module.__name__)
        # `message` is/can be an instance of `category`, so stringify.
        logger.warn(
            "{category}: {message}", message=str(message),
            category=category.__qualname__, filename=filename,
            lineno=lineno, line=line)
    else:
        # It's not clear why and when `file` will be specified, but try to
        # honour the intention.
        warning = warnings.formatwarning(
            message, category, filename, lineno, line)
        try:
            file.write(warning)
            file.flush()
        except OSError:
            pass  # We tried.


_observe_twisted_internet_tcp_noise = re.compile(
    r"^(?:[(].+ Port \d+ Closed[)]|.+ starting on \d+)")


def observe_twisted_internet_tcp(event):
    """Observe events from `twisted.internet.tcp` and filter out noise."""
    message = twistedModern.formatEvent(event)
    if _observe_twisted_internet_tcp_noise.match(message) is None:
        twistedModern.globalLogPublisher(event)


_observe_twisted_internet_udp_noise = re.compile(
    r"^(?:[(].+ Port \d+ Closed[)]|.+ starting on \d+)")


def observe_twisted_internet_udp(event):
    """Observe events from `twisted.internet.udp` and filter out noise."""
    message = twistedModern.formatEvent(event)
    if _observe_twisted_internet_udp_noise.match(message) is None:
        twistedModern.globalLogPublisher(event)


_observe_twisted_internet_unix_noise = re.compile(
    r"^(?:[(]Port \d+ Closed[)]|.+ starting on .+)")


def observe_twisted_internet_unix(event):
    """Observe events from `twisted.internet.unix` and filter out noise."""
    message = twistedModern.formatEvent(event)
    if _observe_twisted_internet_unix_noise.match(message) is None:
        twistedModern.globalLogPublisher(event)
