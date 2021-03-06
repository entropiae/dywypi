import asyncio
from collections import defaultdict
from collections.abc import MutableMapping
import importlib
import logging
import pkgutil

from dywypi.event import Event, Message
from dywypi.event import PrivateMessage
from dywypi.event import PublicMessage

log = logging.getLogger(__name__)


class PluginDataWrapper(MutableMapping):
    """Event-aware methods for plugin data."""
    def __init__(self, plugin_data, event):
        self._plugin_data = plugin_data
        self._event = event

    def per_channel(self, cls):
        channel = self._event.channel
        d = self._plugin_data.per_channel
        if cls not in d[channel]:
            d[channel][cls] = cls(channel)
        return d[channel][cls]

    # Mapping interface
    def __getitem__(self, key):
        return self._plugin_data.general[key]

    def __setitem__(self, key, value):
        self._plugin_data.general[key] = value

    def __delitem__(self, key):
        del self._plugin_data.general

    def __iter__(self):
        return iter(self._plugin_data.general)

    def __len__(self):
        return len(self._plugin_data.general)


class EventWrapper:
    """Little wrapper around an event object that provides convenient plugin
    methods like `reply`.  All other attributes are delegated to the real
    event.
    """
    def __init__(self, event, plugin_data, plugin_manager):
        self.event = event
        self.type = type(event)
        self.data = PluginDataWrapper(plugin_data, event)
        self._plugin_manager = plugin_manager

    # TODO should these just be on Event?
    @asyncio.coroutine
    def reply(self, message):
        # TODO should address the speaker!
        return self.say(message)

    @asyncio.coroutine
    def say(self, message):
        if self.event.channel:
            reply_to = self.event.channel.name
        else:
            reply_to = self.event.source.name

        # TODO uhoh, where does this guy belong...
        # TODO and where does the formatting belong...  on a Dialect?  which is
        # not yet a thing?
        from dywypi.formatting import FormattedString
        if isinstance(message, FormattedString):
            # TODO this should probably be a method on the dialect actually...?
            message = message.render(self.event.client.format_transition)
        yield from self.event.client.say(reply_to, message)

    def __getattr__(self, attr):
        return getattr(self.event, attr)


class CommandMessage(Message):
    def __init__(self, source, target, message, command_name, argstr, **kwargs):
        super().__init__(source, target, message, **kwargs)
        self.command_name = command_name
        self.argstr = argstr
        self.args = argstr.strip().split()

    def __repr__(self):
        return "<{}: {} {!r}>".format(
            type(self).__qualname__, self.command_name, self.args)


class _DummyEvent:
    """Marker for an event class that doesn't actually have any interesting
    data and is triggered by the system itself, such as `Load`.
    """


# TODO should there be a shutdown event then?
class Load(_DummyEvent):
    """Fired when the plugin is first loaded."""


class PluginData:
    def __init__(self):
        self.general = dict()
        self.per_channel = defaultdict(dict)


class PluginManager:
    def __init__(self):
        self.loaded_plugins = {}
        self.plugin_data = defaultdict(PluginData)

    @property
    def known_plugins(self):
        """Returns a dict mapping names to all known `Plugin` instances."""
        return BasePlugin._known_plugins

    def scan_package(self, package='dywypi.plugins'):
        """Scans a Python package for in-process Python plugins."""
        pkg = importlib.import_module(package)
        # TODO pkg.__path__ doesn't exist if pkg is /actually/ a module
        for finder, name, is_pkg in pkgutil.iter_modules(pkg.__path__, prefix=package + '.'):
            try:
                importlib.import_module(name)
            except ImportError as exc:
                log.error(
                    "Couldn't import plugin module {}: {}"
                    .format(name, exc))

    def loadall(self):
        for name, plugin in self.known_plugins.items():
            self.load(name)

    def load(self, plugin_name):
        if plugin_name in self.loaded_plugins:
            return
        # TODO keyerror
        plugin = self.known_plugins[plugin_name]
        #plugin.start()
        log.info("Loaded plugin {}".format(plugin.name))
        self.loaded_plugins[plugin.name] = plugin

    def loadmodule(self, modname):
        # This is a little chumptastic, but: figure out which plugins a module
        # adds by comparing the list of known plugins before and after.
        # TODO lol this doesn't necessarily work if the module was already
        # loaded.  this is dumb just allow scanning particular packages
        before_plugins = set(self.known_plugins)
        importlib.import_module(modname)
        after_plugins = set(self.known_plugins)

        for plugin_name in after_plugins - before_plugins:
            self.load(plugin_name)

    def _wrap_event(self, event, plugin):
        return EventWrapper(event, self.plugin_data[plugin], self)

    def _fire(self, event):
        futures = []
        for plugin in self.loaded_plugins.values():
            futures.extend(self._fire_on(event, plugin))
        return futures

    def _fire_on(self, event, plugin):
        wrapped = self._wrap_event(event, plugin)
        return plugin.fire(wrapped)

    def _fire_global_command(self, command_event):
        # TODO well this could be slightly more efficient
        # TODO should also mention when no command exists
        futures = []
        for plugin in self.loaded_plugins.values():
            wrapped = self._wrap_event(command_event, plugin)
            futures.extend(plugin.fire_command(wrapped, is_global=True))
        return futures

    def _fire_plugin_command(self, plugin_name, command_event):
        # TODO should DEFINITELY complain when plugin OR command doesn't exist
        try:
            plugin = self.loaded_plugins[plugin_name]
        except KeyError:
            raise
            # TODO
            #raise SomeExceptionThatGetsSentAsAReply(...)

        wrapped = self._wrap_event(command_event, plugin)
        return plugin.fire_command(wrapped, is_global=False)

    def fire(self, event):
        futures = self._fire(event)

        # Possibly also fire plugin-specific events.
        if isinstance(event, Message):
            # Messages get broken down a little further.
            is_public = (event.channel)
            is_command = (event.message.startswith(event.client.nick) and
                event.message[len(event.client.nick)] in ':, ')

            if is_command or not is_public:
                # Something addressed directly to us; this is a command and
                # needs special handling!
                if is_command:
                    message = event.message[len(event.client.nick) + 1:]
                else:
                    message = event.message
                try:
                    command_name, argstr = message.split(None, 1)
                except ValueError:
                    command_name, argstr = message.strip(), ''

                plugin_name, _, command_name = command_name.rpartition('.')
                command_event = CommandMessage(
                    event.source, event.target, event.message,
                    command_name, argstr,
                    client=event.client, raw=event.raw_message,
                )
                log.debug('Firing command %r', command_event)
                if plugin_name:
                    futures.extend(
                        self._fire_plugin_command(plugin_name, command_event))
                else:
                    futures.extend(
                        self._fire_global_command(command_event))
            else:
                # Regular public message.
                futures.extend(self._fire(event))

            # TODO: what about private messages that don't "look like"
            # commands?  what about "all" public messages?  etc?

        return futures


class PluginCommand:
    def __init__(self, coro, *, is_global):
        self.coro = coro
        self.is_global = is_global


class BasePlugin:
    _known_plugins = {}

    def __init__(self, name):
        if name in self._known_plugins:
            raise NameError(
                "Can't have two plugins named {}: {} versus {}"
                .format(
                    name,
                    self.__module__,
                    self._known_plugins[name].__module__))

        self.name = name
        self._known_plugins[name] = self


class Plugin(BasePlugin):
    def __init__(self, name):
        self.listeners = defaultdict(list)
        self.commands = {}

        super().__init__(name)

    def on(self, event_cls):
        if not issubclass(event_cls, (Event, _DummyEvent)):
            raise TypeError("Can only listen on an Event subclass, not {}".format(event_cls))

        def decorator(f):
            coro = asyncio.coroutine(f)
            for cls in event_cls.__mro__:
                if cls is Event:
                    # Ignore Event and its superclasses (presumably object)
                    break
                self.listeners[cls].append(coro)
            return coro

        return decorator

    def command(self, command_name, *, is_global=True):
        def decorator(f):
            coro = asyncio.coroutine(f)
            # TODO collisions etc
            self.commands[command_name] = PluginCommand(
                coro, is_global=is_global)
            return coro
        return decorator

    ### "Real" methods

    def fire(self, event):
        """Fire the given event, by dumping all the associated listeners on
        this plugin into the event loop.

        Returns a sequence of Futures, one for each listener (and possibly
        zero).  Event handlers aren't expected to have any particular result,
        but the caller might be interested in one for particular event types,
        or may wish to handle exceptions.
        """
        futures = []
        for listener in self.listeners[event.type]:
            # Fire them all off in parallel via async(); `yield from` would run
            # them all in serial and nonblock until they're all done!
            # TODO if there are exceptions here they're basically lost; whoever
            # asked for the event will never get an error message, and e.g.
            # py.test will never get a traceback
            futures.append(asyncio.async(listener(event), loop=event.loop))
        return futures

    def fire_command(self, event, *, is_global):
        """Fire a command event.  Return a sequence of Futures.  See `fire`.
        """
        futures = []
        if event.command_name in self.commands:
            command = self.commands[event.command_name]

            # Don't execute if the command is local-only and this wasn't
            # invoked with a prefix
            if command.is_global or not is_global:
                futures.append(
                    asyncio.async(command.coro(event), loop=event.loop))

        return futures


class PluginError(Exception): pass
