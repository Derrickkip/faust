import asyncio
import re
from collections import OrderedDict
from typing import (
    Any, AsyncIterable, Awaitable, Callable, Dict, List, Mapping,
    MutableMapping, MutableSequence, Pattern, Sequence, Union, cast
)
from .transport.base import Consumer
from .types import AppT, K, V, Message, SerializerArg, Topic
from .utils.log import get_logger
from .utils.service import Service

logger = get_logger(__name__)


def topic(*topics: str,
          pattern: Union[str, Pattern] = None,
          type: type = None,
          key_serializer: SerializerArg = None) -> Topic:
    if isinstance(pattern, str):
        pattern = re.compile(pattern)
    return Topic(
        topics=topics,
        pattern=pattern,
        type=type,
        key_serializer=key_serializer,
    )


class stream:

    def __init__(self, topic: Topic,
                 *,
                 callbacks: Sequence[Callable] = None,
                 loop: asyncio.AbstractEventLoop = None,
                 **kwargs) -> None:
        self.topic = topic
        self.callbacks = callbacks
        self.loop = loop or asyncio.get_event_loop()

    def __call__(self, fun: Callable) -> 'Stream':
        return Stream(
            topics=[self.topic],
            callbacks={self.topic: self.callbacks},
            coros={self.topic: GeneratorCallback(fun, loop=self.loop)},
            loop=self.loop,
        )


class AsyncInputStream(AsyncIterable):

    queue: asyncio.Queue

    def __init__(self, *,
                 loop: asyncio.AbstractEventLoop = None) -> None:
        self.loop = loop or asyncio.get_event_loop()
        self.queue = asyncio.Queue(maxsize=1, loop=self.loop)

    async def next(self) -> Any:
        return await self.queue.get()

    async def put(self, value: V) -> None:
        await self.queue.put(value)

    async def __aiter__(self) -> 'AsyncIterable':
        return self

    async def __anext__(self) -> Awaitable:
        return await self.queue.get()

    async def join(self, timeout=None):
        await asyncio.wait_for(self._join(), timeout, loop=self.loop)

    async def _join(self, interval=0.1):
        while self.queue.qsize():
            await asyncio.sleep(interval)


class GeneratorCallback:
    inbox: AsyncInputStream

    def __init__(self, coro: Callable[[AsyncIterable], AsyncIterable],
                 *, loop: asyncio.AbstractEventLoop = None) -> None:
        self.loop = loop
        self.inbox = AsyncInputStream(loop=self.loop)
        self.coro = coro
        self.gen = self.coro(self.inbox)

    async def send(self, value: V, outbox: asyncio.Queue) -> None:
        await self.inbox.put(value)
        asyncio.ensure_future(self._drain_gen(outbox), loop=self.loop)

    async def _drain_gen(self, outbox: asyncio.Queue) -> None:
        new_value = await self.gen.__anext__()
        await outbox.put(new_value)

    async def join(self) -> None:
        # make sure everything in inqueue is processed.
        await self.inbox.join()


class Stream(Service, AsyncIterable):

    app: AppT = None
    topics: MutableSequence[Topic] = None
    name: str = None
    loop: asyncio.AbstractEventLoop = None
    outbox: asyncio.Queue
    _consumers: MutableMapping[Topic, Consumer] = None
    _callbacks: MutableMapping[Topic, Sequence[Callable]] = None
    _coros: MutableMapping[Topic, GeneratorCallback] = None

    def __init__(self, name: str = None,
                 topics: Sequence[Topic] = None,
                 callbacks: MutableMapping[Topic, Sequence[Callable]] = None,
                 coros: MutableMapping[Topic, GeneratorCallback] = None,
                 app: AppT = None,
                 loop: asyncio.AbstractEventLoop = None) -> None:
        self.app = app
        self.name = name
        if not isinstance(topics, MutableSequence):
            topics = list(topics)
        self.topics = cast(MutableSequence, topics)
        self._callbacks = callbacks
        self._consumers = OrderedDict()
        self._coros = coros
        self.outbox = asyncio.Queue(maxsize=1, loop=self.loop)
        super().__init__(loop=loop)

    def bind(self, app: AppT) -> 'Stream':
        stream = self.clone(name=app.new_stream_name(), app=app)
        app.add_source(stream)
        return stream

    def info(self) -> Mapping[str, Any]:
        return {
            'name': self.name,
            'topics': self.topics,
            'callbacks': self._callbacks,
            'coros': self._coros,
            'loop': self.loop,
        }

    def clone(self, **kwargs) -> 'Stream':
        return self.__class__(**{**self.info(), **kwargs})

    def combine(self, *children: 'Stream', **kwargs):
        nodes = (self,) + children
        topics: List[Topic] = []
        callbacks: Dict[Topic, Sequence[Callable]] = {}
        coros: Dict[Topic, GeneratorCallback] = {}
        for node in nodes:
            topics.extend(node.topics)
            callbacks.update(node._callbacks)
            coros.update(node._coros)
        return self.clone(topics=topics, callbacks=callbacks, coros=coros)

    async def on_message(self, topic: Topic, key: K, value: V) -> None:
        callbacks = self._callbacks[topic]
        if callbacks is not None:
            for callback in callbacks:
                res = callback(value)
                if isinstance(res, Awaitable):
                    value = await res
                else:
                    value = res
        coro = self._coros[topic]
        if coro is not None:
            await coro.send(value, self.outbox)
        await self.process(key, value)

    async def process(self, key: K, value: V) -> V:
        print('Received K/V: %r %r' % (key, value))
        return value

    async def subscribe(self, topic: Topic,
                        *,
                        callbacks: Sequence[Callable] = None,
                        coro: Callable = None) -> None:
        if topic not in self.topics:
            self.topics.append(topic)
        self._callbacks[topic] = callbacks
        self._coros[topic] = GeneratorCallback(coro, loop=self.loop)
        await self._subscribe(topic)

    async def _subscribe(self, topic: Topic) -> None:
        if topic not in self._consumers:
            c = self._consumers[topic] = self.create_consumer_for_topic(topic)
            await c.start()

    async def unsubscribe(self, topic: Topic) -> None:
        try:
            self.topics.remove(topic)
        except ValueError:
            pass
        self._callbacks.pop(topic, None)
        self._coros.pop(topic, None)
        await self._unsubscribe(topic)

    async def _unsubscribe(self, topic: Topic) -> None:
        try:
            consumer = self._consumers.pop(topic)
        except KeyError:
            pass
        else:
            await consumer.stop()

    async def on_start(self) -> None:
        if self.app is None:
            raise RuntimeError('Cannot start stream not bound to app.')
        for topic in self.topics:
            await self._subscribe(topic)

    async def on_stop(self) -> None:
        for consumer in reversed(list(self._consumers.values())):
            await consumer.stop()
        self._consumers.clear()
        for coro in self._coros.values():
            await coro.join()

    def on_key_decode_error(self, exc: Exception, message: Message) -> None:
        logger.error('Cannot decode key: %r: %r', message.key, exc)

    def on_value_decode_error(self, exc: Exception, message: Message) -> None:
        logger.error('Cannot decode value for key=%r (%r): %r',
                     message.key, message.value, exc)

    def create_consumer_for_topic(self, topic: Topic) -> Consumer:
        return self.app.transport.create_consumer(
            topic=topic,
            callback=self.on_message,
            on_key_decode_error=self.on_key_decode_error,
            on_value_decode_error=self.on_value_decode_error,
        )

    def __and__(self, other: 'Stream') -> 'Stream':
        return self.combine(self, other)

    def __copy__(self) -> 'Stream':
        return self.clone()

    async def __aiter__(self) -> 'Stream':
        await self.maybe_start()
        return self

    async def __anext__(self) -> Awaitable:
        return await self.outbox.get()


class Table(Stream):

    #: This maintains the state of the stream (the K/V store).
    _state: MutableMapping

    def on_init(self) -> None:
        self._state = {}

    async def on_message(self, topic: Topic, key: K, value: V) -> None:
        self._state[key] = await self.process(key, value)

    def __getitem__(self, key: Any) -> Any:
        return self._state[key]

    def __setitem__(self, key: Any, value: Any) -> None:
        self._state[key] = value

    def __delitem__(self, key: Any) -> None:
        del self._state[key]
