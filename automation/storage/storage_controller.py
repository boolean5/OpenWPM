import asyncio
import base64
import logging
import queue
import random
import threading
import time
from collections import defaultdict
from typing import Any, DefaultDict, Dict, List, Optional, Tuple

from multiprocess import Queue

from automation.utilities.multiprocess_utils import Process

from ..SocketInterface import AsyncServerSocket
from ..types import BrowserId, VisitId
from .storage_providers import (
    StructuredStorageProvider,
    TableName,
    UnstructuredStorageProvider,
)

RECORD_TYPE_CONTENT = "page_content"
RECORD_TYPE_META = "meta_information"
ACTION_TYPE_FINALIZE = "Finalize"
ACTION_TYPE_INITIALIZE = "Initialize"
RECORD_TYPE_CREATE = "create_table"
STATUS_TIMEOUT = 120  # seconds
SHUTDOWN_SIGNAL = "SHUTDOWN"
BATCH_COMMIT_TIMEOUT = 30  # commit a batch if no new records for N seconds


STATUS_UPDATE_INTERVAL = 5  # seconds


class StorageController:
    def __init__(
        self,
        structured_storage: StructuredStorageProvider,
        unstructured_storage: Optional[UnstructuredStorageProvider],
        status_queue: Queue,
        completion_queue: Queue,
        shutdown_queue: Queue,
    ) -> None:
        """
        Creates a BaseListener instance

        Parameters
        ----------
        status_queue
            queue that the current amount of records to be processed will
            be sent to
            also used for initialization
        completion_queue
            queue containing the visitIDs of saved records
        shutdown_queue
            queue that the main process can use to shut down the listener
        """
        self.status_queue = status_queue
        self.completion_queue = completion_queue
        self.shutdown_queue = shutdown_queue
        self._shutdown_flag = False
        self._relaxed = False
        self._last_update = time.time()  # last status update time
        self.record_queue: Queue = None  # Initialized on `startup`
        self.logger = logging.getLogger("openwpm")
        self.current_tasks: DefaultDict[VisitId, List[asyncio.Task]] = defaultdict(list)
        self.sock: Optional[AsyncServerSocket] = None
        self.structured_storage = structured_storage
        self.unstructured_storage = unstructured_storage
        self._last_record_received: Optional[float] = None

    async def startup(self) -> None:
        """Puts the DataAggregator into a runable state
        by starting up the ServerSocket"""
        self.record_queue = asyncio.Queue()
        self.sock = AsyncServerSocket(
            self.record_queue, asyncio.get_event_loop(), name=type(self).__name__
        )
        self.status_queue.put(self.sock.sock.getsockname())
        self.sock.start_accepting()

    async def poll_queue(self) -> None:
        """Tries to get one record from the queue and processes it, if there is one"""
        assert self.record_queue is not None
        if self.record_queue.empty():
            return

        record: Tuple[str, Any] = await self.record_queue.get()
        if len(record) != 2:
            self.logger.error("Query is not the correct length %s", repr(record))
            return

        self._last_record_received = time.time()
        record_type, data = record

        self.logger.info("Received record for record_type %s", record_type)

        if record_type == RECORD_TYPE_CREATE:
            raise RuntimeError(
                f"""{RECORD_TYPE_CREATE} is no longer supported.
                since the user now has access to the DB before it
                goes into use, they should set up all schemas before
                launching the DataAggregator
                """
            )

        if record_type == RECORD_TYPE_CONTENT:
            assert isinstance(data, tuple)
            assert len(data) == 2
            if self.unstructured_storage is None:
                self.logger.error(
                    """Tried to save content while not having
                                  provided any unstructured storage provider."""
                )
                return
            content, content_hash = data
            content = base64.b64decode(content)
            await self.unstructured_storage.store_blob(
                filename=content_hash, blob=content
            )

            return
        if record_type == RECORD_TYPE_META:
            await self._handle_meta(data)
            return
        visit_id = VisitId(data["visit_id"])
        table_name = TableName(record_type)

        self.current_tasks[visit_id].append(
            asyncio.create_task(
                self.structured_storage.store_record(
                    table=table_name, visit_id=visit_id, record=data
                )
            )
        )

    async def _handle_meta(self, data: Dict[str, Any]) -> None:
        """
        Messages for the table RECORD_TYPE_SPECIAL are metainformation
        communicated to the aggregator
        Supported message types:
        - finalize: A message sent by the extension to
                    signal that a visit_id is complete.
        """
        visit_id = VisitId(data["visit_id"])
        action = data["action"]

        self.logger.info(
            "Received meta message to %s for visit_id %d", action, visit_id
        )
        if action == ACTION_TYPE_INITIALIZE:
            return
        elif action == ACTION_TYPE_FINALIZE:
            success = data["success"]
            for task in self.current_tasks[visit_id]:
                await task
            self.logger.debug(
                "Awaited all tasks for visit_id %d while finalizing", visit_id
            )

            await self.structured_storage.finalize_visit_id(
                visit_id, interrupted=not success
            )
            self.completion_queue.put((visit_id, success))
            del self.current_tasks[visit_id]
        else:
            raise ValueError("Unexpected action: %s", action)

    def update_status_queue(self) -> None:
        """Send manager process a status update."""
        if (time.time() - self._last_update) < STATUS_UPDATE_INTERVAL:
            return
        qsize = self.record_queue.qsize()
        self.status_queue.put(qsize)
        self.logger.debug(
            "Status update; current record queue size: %d. "
            "current number of threads: %d." % (qsize, threading.active_count())
        )
        self._last_update = time.time()

    async def drain_queue(self) -> None:
        """ Ensures queue is empty before closing """
        while not self.record_queue.empty():
            await self.poll_queue()
        self.logger.info("Queue was flushed completely")

    async def shutdown(self) -> None:
        await self.structured_storage.flush_cache()
        await self.structured_storage.shutdown()
        if self.unstructured_storage is not None:
            await self.unstructured_storage.flush_cache()
            await self.unstructured_storage.shutdown()

    def should_shutdown(self) -> bool:
        """Return `True` if the listener has received a shutdown signal
        Sets `self._relaxed` and `self.shutdown_flag`
        `self._relaxed means this shutdown is
        happening after all visits have completed and
        all data can be seen as complete
        """
        if not self.shutdown_queue.empty():
            _, relaxed = self.shutdown_queue.get()
            self._relaxed = relaxed
            self._shutdown_flag = True
            self.logger.info("Received shutdown signal!")
            return True
        return False

    async def save_batch_if_past_timeout(self) -> None:
        """Save the current batch of records if no new data has been received.

        If we aren't receiving new data for this batch we commit early
        regardless of the current batch size."""
        if self._last_record_received is None:
            return
        if time.time() - self._last_record_received < BATCH_COMMIT_TIMEOUT:
            return
        self.logger.debug(
            "Saving current records since no new data has "
            "been written for %d seconds." % (time.time() - self._last_record_received)
        )
        await self.drain_queue()
        self._last_record_received = None

    async def finish_tasks(self) -> None:
        for visit_id, tasks in self.current_tasks.items():
            for task in tasks:
                await task

    async def _run(self) -> None:
        await self.startup()
        while not self.should_shutdown():
            self.update_status_queue()
            await self.save_batch_if_past_timeout()
            await self.poll_queue()
        await self.drain_queue()
        await self.finish_tasks()
        await self.shutdown()

    def run(self) -> None:
        asyncio.run(self._run(), debug=True)


class StorageControllerHandle:
    """This class contains all methods relevant for the TaskManager
    to interact with the DataAggregator
    """

    def __init__(
        self,
        structured_storage: StructuredStorageProvider,
        unstructured_storage: UnstructuredStorageProvider,
    ) -> None:

        self.listener_address = None
        self.listener_process: Optional[Process] = None
        self.status_queue = Queue()
        self.completion_queue = Queue()
        self.shutdown_queue = Queue()
        self._last_status = None
        self._last_status_received: Optional[float] = None
        self.logger = logging.getLogger("openwpm")
        self.aggregator = StorageController(
            structured_storage,
            unstructured_storage,
            status_queue=self.status_queue,
            completion_queue=self.completion_queue,
            shutdown_queue=self.shutdown_queue,
        )

    def get_next_visit_id(self) -> VisitId:
        """Generate visit id as randomly generated positive integer less than 2^53.

        Parquet can support integers up to 64 bits, but Javascript can only
        represent integers up to 53 bits:
        https://developer.mozilla.org/en-US/docs/Web/JavaScript/Reference/Global_Objects/Number/MAX_SAFE_INTEGER
        Thus, we cap these values at 53 bits.
        """
        return VisitId(random.getrandbits(53))

    def get_next_browser_id(self) -> BrowserId:
        """Generate crawl id as randomly generated positive 32bit integer

        Note: Parquet's partitioned dataset reader only supports integer
        partition columns up to 32 bits.
        """
        return BrowserId(random.getrandbits(32))

    def save_configuration(self, openwpm_version: str, browser_version: str) -> None:
        # FIXME I need to find a solution for this
        self.logger.error(
            "Can't log config as of yet, because it's still not implemented"
        )

    def launch(self) -> None:
        """Starts the data aggregator"""
        self.listener_process = Process(
            name="StorageController",
            target=StorageController.run,
            args=(self.aggregator,),
        )
        self.listener_process.daemon = True
        self.listener_process.start()

        self.listener_address = self.status_queue.get()

    def get_new_completed_visits(self) -> List[Tuple[int, bool]]:
        """
        Returns a list of all visit ids that have been processed since
        the last time the method was called and whether or not they
        have been interrupted.

        This method will return an empty list in case no visit ids have
        been processed since the last time this method was called
        """
        finished_visit_ids = list()
        while not self.completion_queue.empty():
            finished_visit_ids.append(self.completion_queue.get())
        return finished_visit_ids

    def shutdown(self, relaxed: bool = True) -> None:
        """ Terminate the aggregator listener process"""
        assert isinstance(self.listener_process, Process)
        self.logger.debug(
            "Sending the shutdown signal to the %s listener process..."
            % type(self).__name__
        )
        self.shutdown_queue.put((SHUTDOWN_SIGNAL, relaxed))
        start_time = time.time()
        self.listener_process.join(300)
        self.logger.debug(
            "%s took %s seconds to close."
            % (type(self).__name__, str(time.time() - start_time))
        )
        self.listener_address = None
        self.listener_process = None

    def get_most_recent_status(self) -> int:
        """Return the most recent queue size sent from the listener process"""

        # Block until we receive the first status update
        if self._last_status is None:
            return self.get_status()

        # Drain status queue until we receive most recent update
        while not self.status_queue.empty():
            self._last_status = self.status_queue.get()
            self._last_status_received = time.time()

        # Check last status signal
        if (time.time() - self._last_status_received) > STATUS_TIMEOUT:
            raise RuntimeError(
                "No status update from DataAggregator listener process "
                "for %d seconds." % (time.time() - self._last_status_received)
            )

        return self._last_status

    def get_status(self) -> int:
        """Get listener process status. If the status queue is empty, block."""
        try:
            self._last_status = self.status_queue.get(
                block=True, timeout=STATUS_TIMEOUT
            )
            self._last_status_received = time.time()
        except queue.Empty:
            assert self._last_status_received is not None
            raise RuntimeError(
                "No status update from DataAggregator listener process "
                "for %d seconds." % (time.time() - self._last_status_received)
            )
        assert isinstance(self._last_status, int)
        return self._last_status
