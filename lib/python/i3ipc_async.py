import json
import asyncio
from Xlib import display
from collections import namedtuple

from i3ipc import (Connection, _PubSub, _PropsObject, MessageType,
                   CommandReply, Event, WorkspaceEvent, GenericEvent,
                   WindowEvent, BarconfigUpdateEvent, BindingEvent,
                   VersionReply, BarConfigReply, OutputReply,
                   WorkspaceReply, Con)

socketstream = namedtuple('socketstream', 'read write')


class AsyncConnection(Connection):
    #  MAGIC = 'i3-ipc'  # safety string for i3-ipc
    #  _chunk_size = 1024  # in bytes
    #  _timeout = 0.5  # in seconds
    #  _struct_header = '<%dsII' % len(MAGIC.encode('utf-8'))
    #  _struct_header_size = struct.calcsize(_struct_header)

    def __init__(self):
        d = display.Display()
        r = d.screen().root
        data = r.get_property(d.get_atom('I3_SOCKET_PATH'),
                              d.get_atom('UTF8_STRING'), 0, 9999)

        if not data.value:
            raise Exception('could not get i3 socket path')

        self._pubsub = _PubSub(self)
        self.props = _PropsObject(self)
        self.subscriptions = 0
        self.socket_path = data.value
        self.socketstream = None
        self.commandstream = None
        #  self.cmd_socket = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        #  self.cmd_socket.connect(self.socket_path)

    @asyncio.coroutine
    def _ipc_recv(self, stream):
        r = stream.read
        data = yield from r.read(14)

        if len(data) == 0:
            # EOF
            return '', 0

        msg_magic, msg_length, msg_type = self._unpack_header(data)
        msg_size = self._struct_header_size + msg_length
        while len(data) < msg_size:
            data += yield from r.read(msg_length)
        return (self._unpack(data), msg_type)

    @asyncio.coroutine
    def _ipc_send(self, stream, message_type, payload):
        w = stream.write
        w.write(self._pack(message_type, payload))
        yield from w.drain()
        data, msg_type = yield from self._ipc_recv(stream)
        return data

    def message(self, message_type, payload):
        return (yield from self._ipc_send(self.commandstream, message_type,
                                          payload))

    def command(self, payload):
        data = yield from self.message(MessageType.COMMAND, payload)
        return json.loads(data, object_hook=CommandReply)

    def get_version(self):
        data = yield from self.message(MessageType.GET_VERSION, '')
        return json.loads(data, object_hook=VersionReply)

    def get_bar_config(self, bar_id=None):
        # default to the first bar id
        if not bar_id:
            bar_config_list = yield from self.get_bar_config_list()
            if not bar_config_list:
                return None
            bar_id = bar_config_list[0]

        data = yield from self.message(MessageType.GET_BAR_CONFIG, bar_id)
        return json.loads(data, object_hook=BarConfigReply)

    def get_bar_config_list(self):
        data = yield from self.message(MessageType.GET_BAR_CONFIG, '')
        return json.loads(data)

    def get_outputs(self):
        data = yield from self.message(MessageType.GET_OUTPUTS, '')
        return json.loads(data, object_hook=OutputReply)

    def get_workspaces(self):
        data = yield from self.message(MessageType.GET_WORKSPACES, '')
        return json.loads(data, object_hook=WorkspaceReply)

    def get_tree(self):
        data = yield from self.message(MessageType.GET_TREE, '')
        return Con(json.loads(data), None, self)

    @asyncio.coroutine
    def subscribe(self, events):
        events_obj = []
        if events & Event.WORKSPACE:
            events_obj.append("workspace")
        if events & Event.OUTPUT:
            events_obj.append("output")
        if events & Event.MODE:
            events_obj.append("mode")
        if events & Event.WINDOW:
            events_obj.append("window")
        if events & Event.BARCONFIG_UPDATE:
            events_obj.append("barconfig_update")
        if events & Event.BINDING:
            events_obj.append("binding")

        data = yield from self._ipc_send(self.socketstream,
                                         MessageType.SUBSCRIBE,
                                         json.dumps(events_obj))
        result = json.loads(data, object_hook=CommandReply)
        self.subscriptions |= events
        return result

    @asyncio.coroutine
    def main(self):
        r, w = yield from asyncio.open_unix_connection(self.socket_path)
        self.commandstream = socketstream(r, w)

        r, w = yield from asyncio.open_unix_connection(self.socket_path)
        self.socketstream = socketstream(r, w)
        yield from self.subscribe(self.subscriptions)

        while True:
            stream = self.socketstream
            if stream is None:
                break
            data, msg_type = yield from self._ipc_recv(stream)

            if len(data) == 0:
                # EOF
                self._pubsub.emit('ipc-shutdown', None)
                break

            data = json.loads(data)
            msg_type = 1 << (msg_type & 0x7f)
            event_name = ''
            event = None

            if msg_type == Event.WORKSPACE:
                event_name = 'workspace'
                event = WorkspaceEvent(data, self)
            elif msg_type == Event.OUTPUT:
                event_name = 'output'
                event = GenericEvent(data)
            elif msg_type == Event.MODE:
                event_name = 'mode'
                event = GenericEvent(data)
            elif msg_type == Event.WINDOW:
                event_name = 'window'
                event = WindowEvent(data, self)
            elif msg_type == Event.BARCONFIG_UPDATE:
                event_name = 'barconfig_update'
                event = BarconfigUpdateEvent(data)
            elif msg_type == Event.BINDING:
                event_name = 'binding'
                event = BindingEvent(data)
            else:
                # we have not implemented this event
                continue

            self._pubsub.emit(event_name, event)

    def main_quit(self):
        self.commandstream.write.close()
        self.socketstream.write.close()
        self.socketstream = None
        self.commandstream = None
