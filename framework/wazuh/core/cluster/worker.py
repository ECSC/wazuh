# Copyright (C) 2015, Wazuh Inc.
# Created by Wazuh, Inc. <info@wazuh.com>.
# This program is free software; you can redistribute it and/or modify it under the terms of GPLv2
import asyncio
import contextlib
import errno
import json
import logging
import os
import shutil
from datetime import datetime, timezone
from time import perf_counter
from typing import Tuple, Dict, Callable, List
from typing import Union

from wazuh.core import cluster as metadata, common, exception, utils
from wazuh.core.cluster import client, cluster, common as c_common
from wazuh.core.cluster.dapi import dapi
from wazuh.core.utils import safe_move, get_utc_now
from wazuh.core.wdb import WazuhDBConnection


class ReceiveAgentGroupsTask(c_common.ReceiveStringTask):
    """
    Define the process and variables necessary to receive and process Agent groups (periodic) from the master.

    This task is created when the master finishes sending Agent groups chunks and its destroyed once the worker has
    updated all the received information.
    """

    def __init__(self, *args, **kwargs):
        """Class constructor.

        Parameters
        ----------
        args
            Positional arguments for parent constructor class.
        kwargs
            Keyword arguments for parent constructor class.
        """
        super().__init__(*args, **kwargs, info_type='agent-groups')

    def set_up_coro(self) -> Callable:
        """Set up the function to be called when the worker sends its Agent groups."""
        return self.wazuh_common.recv_agent_groups_periodic_information

    def done_callback(self, future=None):
        """Check whether the synchronization process was correct and free its lock.

        Parameters
        ----------
        future : asyncio.Future object
            Synchronization process result.
        """
        super().done_callback(future)
        self.wazuh_common.sync_agent_groups_free = True


class ReceiveEntireAgentGroupsTask(c_common.ReceiveStringTask):
    """
    Define the process and variables necessary to receive and process Agent groups (entire) from the master.

    This task is created when the master finishes sending Agent groups chunks and its destroyed once the worker has
    updated all the received information.
    """

    def __init__(self, *args, **kwargs):
        """Class constructor.

        Parameters
        ----------
        args
            Positional arguments for parent constructor class.
        kwargs
            Keyword arguments for parent constructor class.
        """
        super().__init__(*args, **kwargs, info_type='agent-groups')

    def set_up_coro(self) -> Callable:
        """Set up the function to be called when the worker sends its Agent groups."""
        return self.wazuh_common.recv_agent_groups_entire_information

    def done_callback(self, future=None):
        """Check whether the synchronization process was correct and free its lock.

        Parameters
        ----------
        future : asyncio.Future object
            Synchronization process result.
        """
        super().done_callback(future)
        self.wazuh_common.sync_agent_groups_free = True


class ReceiveIntegrityTask(c_common.ReceiveFileTask):
    """
    Create an asyncio.Task that waits until the master sends its integrity information and processes the
    received information.
    """

    def set_up_coro(self) -> Callable:
        """Set up the function to process the integrity files received from master."""
        return self.wazuh_common.process_files_from_master

    def done_callback(self, future=None):
        """Free the integrity sync lock and remove the task_id.

        Parameters
        ----------
        future : asyncio.Future object
            Synchronization process result.
        """
        self.wazuh_common.check_integrity_free = True
        super().done_callback(future)


class WorkerHandler(client.AbstractClient, c_common.WazuhCommon):
    """
    Handle connection with the master node.
    """

    def __init__(self, version, node_type, cluster_name, **kwargs):
        """Class constructor.

        Parameters
        ----------
        version : str
            Wazuh version. E.g., '4.0.0'.
        node_type : str
            Type of node (will always be worker but it's set as a variable in case more types are added in the future).
        cluster_name : str
            The cluster name.
        **kwargs
            Arguments for the parent class constructor.
        """
        super().__init__(**kwargs, tag="Worker")
        # The self.client_data will be sent to the master when doing a hello request.
        self.client_data = f"{self.name} {cluster_name} {node_type} {version}".encode()

        # Flag to prevent a new Integrity check if Integrity sync is in progress.
        self.check_integrity_free = True

        # Every task logger is configured to log using a tag describing the synchronization process. For example,
        # a log coming from the "Integrity" logger will look like this:
        # [Worker name] [Integrity] Log information
        # this way the same code can be shared among all sync tasks and logs will differentiate.
        self.task_loggers = {'Agent-info sync': self.setup_task_logger('Agent-info sync'),
                             'Agent-groups recv': self.setup_task_logger('Agent-groups recv'),
                             'Agent-groups recv full': self.setup_task_logger('Agent-groups recv full'),
                             'Agent-groups sync': self.setup_task_logger('Agent-groups sync'),
                             'Integrity check': self.setup_task_logger('Integrity check'),
                             'Integrity sync': self.setup_task_logger('Integrity sync')}
        default_date = datetime.utcfromtimestamp(0)
        self.sync_agent_groups_from_master = {'date_start_worker': default_date, 'date_end_worker': default_date,
                                              'n_synced_chunks': 0}
        self.agent_info_sync_status = {'date_start': 0.0}
        self.agent_groups_sync_status = {'date_start': 0.0}
        self.integrity_check_status = {'date_start': 0.0}
        self.integrity_sync_status = {'date_start': 0.0}
        self.agent_groups_checksum_mismatch_counter = 0
        self.agent_groups_checksum_mismatch_limit = 10

        # Maximum zip size allowed when syncing Integrity files.
        self.current_zip_limit = self.cluster_items['intervals']['communication']['max_zip_size']

    def connection_result(self, future_result):
        """Callback function called when the master sends a response to the hello command sent by the worker.

        Parameters
        ----------
        future_result : asyncio.Future object
            Result of the hello request.
        """
        super().connection_result(future_result)
        if self.connected:
            # create directory for temporary files
            worker_tmp_files = os.path.join(common.WAZUH_PATH, 'queue', 'cluster', self.name)
            if not os.path.exists(worker_tmp_files):
                utils.mkdir_with_mode(worker_tmp_files)

    def process_request(self, command: bytes, data: bytes) -> Union[bytes, Tuple[bytes, bytes]]:
        """Define all commands that a worker can receive from the master.

        Parameters
        ----------
        command : bytes
            Received command.
        data : bytes
            Received payload.

        Returns
        -------
        bytes
            Result.
        bytes
            Response message.
        """
        self.logger.debug(f"Command received: '{command}'")
        if command == b'syn_m_c_ok':
            return self.sync_integrity_ok_from_master()
        elif command == b'syn_m_c':
            return self.setup_receive_files_from_master()
        elif command == b'syn_m_c_e':
            return self.end_receiving_integrity(data.decode())
        elif command == b'syn_m_c_r':
            return self.error_receiving_integrity(data.decode())
        elif command == b'syn_g_m_w' or command == b'syn_g_m_w_c':
            return self.setup_sync_integrity(command, data)
        elif command == b'syn_m_a_e':
            logger = self.setup_task_logger('Agent-info sync')
            start_time = self.agent_info_sync_status['date_start']
            return c_common.end_sending_agent_information(logger, start_time, data.decode())
        elif command == b'syn_m_g_e':
            logger = self.setup_task_logger('Agent-groups sync')
            start_time = self.agent_groups_sync_status['date_start']
            return c_common.end_sending_agent_information(logger, start_time, data.decode())
        elif command == b'syn_m_a_err':
            logger = self.task_loggers['Agent-info sync']
            return c_common.error_receiving_agent_information(logger, data.decode(), info_type='agent-info')
        elif command == b'syn_m_g_err':
            logger = self.task_loggers['Agent-groups sync']
            return c_common.error_receiving_agent_information(logger, data.decode(), info_type='agent-groups')
        elif command == b'dapi_res':
            asyncio.create_task(self.forward_dapi_response(data))
            return b'ok', b'Response forwarded to worker'
        elif command == b'sendsyn_res':
            asyncio.create_task(self.forward_sendsync_response(data))
            return b'ok', b'Response forwarded to worker'
        elif command == b'dapi_err':
            dapi_client, error_msg = data.split(b' ', 1)
            asyncio.create_task(self.log_exceptions(
                self.server.local_server.clients[dapi_client.decode()].send_request(command, error_msg)))
            return b'ok', b'DAPI error forwarded to worker'
        elif command == b'sendsyn_err':
            sendsync_client, error_msg = data.split(b' ', 1)
            asyncio.create_task(self.log_exceptions(
                self.server.local_server.clients[sendsync_client.decode()].send_request(b'err', error_msg)))
            return b'ok', b'SendSync error forwarded to worker'
        elif command == b'dapi':
            self.server.dapi.add_request(b'master*' + data)
            return b'ok', b'Added request to API requests queue'
        else:
            return super().process_request(command, data)

    def get_manager(self):
        """Get the Worker object that created this WorkerHandler. Used in the class WazuhCommon.

        Returns
        -------
        AbstractClientManager
            Worker object.
        """
        return self.server

    def setup_sync_integrity(self, sync_type: bytes, data: bytes = None) -> Tuple[bytes, bytes]:
        """Start synchronization process.

        Parameters
        ----------
        sync_type : bytes
            Sync process to start.
        data : bytes
            Data to be sent.

        Returns
        -------
        bytes
            Result.
        bytes
            Response message.
        """
        if sync_type == b'syn_g_m_w':
            sync_function = ReceiveAgentGroupsTask
            logger_tag = 'Agent-groups recv'
        elif sync_type == b'syn_g_m_w_c':
            sync_function = ReceiveEntireAgentGroupsTask
            logger_tag = 'Agent-groups recv full'
        else:
            sync_function = None
            logger_tag = ''

        return super().setup_receive_file(receive_task_class=sync_function, data=data, logger_tag=logger_tag)

    def setup_receive_files_from_master(self):
        """Set up a task to wait until integrity information has been received from the master and process it.

        Returns
        -------
        bytes
            Result.
        bytes
            Response message.
        """
        integrity_logger = self.task_loggers['Integrity check']
        integrity_logger.info(
            f"Finished in {(get_utc_now().timestamp() - self.integrity_check_status['date_start']):.3f}s. "
            f"Sync required.")
        self.check_integrity_free = False
        return super().setup_receive_file(receive_task_class=ReceiveIntegrityTask, logger_tag='Integrity sync')

    def end_receiving_integrity(self, task_and_file_names: str) -> Tuple[bytes, bytes]:
        """Notify to the corresponding task that information has been received.

        The master notifies to this the worker that the integrity information has already been sent.
        Then, the worker notifies the previously created task that the information has been received.

        Parameters
        ----------
        task_and_file_names : str
            Task ID and a filename, separated by a space (' ').

        Returns
        -------
        bytes
            Result.
        bytes
            Response message.
        """
        return super().end_receiving_file(task_and_file_names=task_and_file_names, logger_tag='Integrity sync')

    def error_receiving_integrity(self, data: str) -> Tuple[bytes, bytes]:
        """Notify to the corresponding task that an error has occurred during the process.

        Parameters
        ----------
        data : str
            Task ID and error formatted as WazuhJSONEncoder.

        Returns
        -------
        bytes
            Result.
        bytes
            Response message.
        """
        return super().error_receiving_file(task_id_and_error_details=data, logger_tag='Integrity sync')

    def sync_integrity_ok_from_master(self) -> Tuple[bytes, bytes]:
        """Function called when the master sends the "syn_m_c_ok" command.

        Returns
        -------
        bytes
            Result.
        bytes
            Response message.
        """
        integrity_logger = self.task_loggers['Integrity check']
        integrity_logger.info(
            f"Finished in {(get_utc_now().timestamp() - self.integrity_check_status['date_start']):.3f}s. "
            f"Sync not required.")
        return b'ok', b'Thanks'

    async def compare_agent_groups_checksums(self, master_checksum, logger):
        """Compare the checksum of the local database with the checksum of the master node to check if these differ.

        If the checksum differs, a counter is incremented which at a certain limit
        will send a request to the master node asking for all the agent-groups information.

        Parameters
        ----------
        master_checksum : str
            Master node checksum.
        logger : Logger object
            Logger to use.

        Returns
        -------
        bool
            True if both checksums are equal, False if these differ or cannot be
            compared because there are records that need to be synchronized in the local DB.
        """
        wdb_conn = WazuhDBConnection()
        sync_object = c_common.SyncWazuhdb(manager=self, logger=logger, cmd=b'syn_g_m_w',
                                           data_retriever=wdb_conn.run_wdb_command,
                                           get_data_command='global sync-agent-groups-get ',
                                           get_payload={"condition": "sync_status", "get_global_hash": True})

        local_agent_groups = await sync_object.retrieve_information()
        if not local_agent_groups:
            return False

        local_agent_groups = json.loads(local_agent_groups[0])
        if not local_agent_groups[0]['data']:
            logger.debug2('There is no data requiring synchronization in the local database.')
            try:
                # There is no syncreq agent-groups so, the checksums should match
                local_checksum = local_agent_groups[-1]['hash']
                ck_equal = master_checksum == local_checksum
            except KeyError:
                local_checksum = 'UNABLE TO COLLECT FROM DB'
                ck_equal = False
            # If there are no records with syncreq and the checksums are different, it means that the worker database
            # is in an incorrect state. Therefore, all the information will be requested directly to the master node.
            if not ck_equal:
                logger.debug(f'The master\'s checksum and the worker\'s checksum are different. '
                             f'Local checksum: {local_checksum} | Master checksum: {master_checksum}.')
                self.agent_groups_checksum_mismatch_counter = self.agent_groups_checksum_mismatch_limit

            return ck_equal

        return False

    async def check_agent_groups_checksums(self, data, logger):
        """Checksum comparison limit controller function for agent-groups.

        This function is in charge of requesting to the master node the information of
        the database related to agent-groups if the limit of comparative checksums is exceeded.

        Parameters
        ----------
        data : dict
            Dictionary with the data obtained through the task_id.
        logger : Logger object
            Logger to use.
        """
        try:
            master_checksum = json.loads(data['chunks'][-1])[0]['hash']
        except KeyError:
            return

        same_checksum = await self.compare_agent_groups_checksums(master_checksum=master_checksum, logger=logger)
        if same_checksum:
            msg = 'The checksum of both databases match.'
            if self.agent_groups_checksum_mismatch_counter != 0:
                msg += ' Reset the attempt counter.'
            logger.debug(msg)
            self.agent_groups_checksum_mismatch_counter = 0
        else:
            self.agent_groups_checksum_mismatch_counter += 1
            if self.agent_groups_checksum_mismatch_counter <= self.agent_groups_checksum_mismatch_limit:
                logger.debug(
                    f'Checksum comparison failed. '
                    f'Attempt {self.agent_groups_checksum_mismatch_counter}/{self.agent_groups_checksum_mismatch_limit}.')

            if self.agent_groups_checksum_mismatch_counter >= self.agent_groups_checksum_mismatch_limit:
                await super().send_result_to_manager(b'syn_w_g_c', {})
                self.agent_groups_checksum_mismatch_counter = 0
                logger.info('Sent request to obtain all agent-groups information from the master node.')

    async def recv_agent_groups_periodic_information(self, task_id: bytes, info_type: str):
        """Create a process to receive the master periodic agent-groups information.

        Parameters
        ----------
        task_id : bytes
            ID of the string where the JSON chunks are stored.
        info_type : str
            Information type handled.

        Returns
        -------
        result : bytes
            Master's response after finishing the synchronization.
        """
        logger = self.task_loggers['Agent-groups recv']
        command = b'syn_w_g_e'
        error_command = b'syn_w_g_err'
        timeout = self.cluster_items['intervals']['worker']['timeout_agent_groups']

        return await self.recv_agent_groups_information(task_id, info_type, logger, command, error_command, timeout)

    async def recv_agent_groups_entire_information(self, task_id: bytes, info_type: str):
        """Create a process to receive the master entire agent-groups information.

        Parameters
        ----------
        task_id : bytes
            ID of the string where the JSON chunks are stored.
        info_type : str
            Information type handled.

        Returns
        -------
        result : bytes
            Master's response after finishing the synchronization.
        """
        logger = self.task_loggers['Agent-groups recv full']
        command = b'syn_wgc_e'
        error_command = b'syn_wgc_err'
        timeout = self.cluster_items['intervals']['worker']['timeout_agent_groups']

        return await self.recv_agent_groups_information(task_id, info_type, logger, command, error_command, timeout)

    async def recv_agent_groups_information(self, task_id: bytes, info_type: str, logger: logging.Logger,
                                            command: bytes, error_command: bytes, timeout: int):
        """Create a process to receive the master agent-groups information.

        Parameters
        ----------
        task_id : bytes
            ID of the string where the JSON chunks are stored.
        info_type : str
            Information type handled.
        logger : logging.Logger
            Logger used to print the function messages.
        command : bytes
            Command that will be sent to the master node to indicate the end of the task.
        error_command : bytes
            Command that will be sent to the master node in case of error.
        timeout : int
            Maximum time to send the information to the database.

        Returns
        -------
        result : bytes
            Master's response after finishing the synchronization.
        """
        logger.info('Starting.')
        start_time = datetime.utcnow().replace(tzinfo=timezone.utc)
        data = await super().get_chunks_in_task_id(task_id, error_command)
        result = await super().update_chunks_wdb(data, info_type, logger, error_command, timeout)
        response = await super().send_result_to_manager(command, result)
        await self.check_agent_groups_checksums(data, logger)

        end_time = datetime.utcnow().replace(tzinfo=timezone.utc)
        logger.info(f'Finished in {(end_time - start_time).total_seconds():.3f}s. '
                    f'Updated {result["updated_chunks"]} chunks.')

        return response

    async def sync_integrity(self):
        """Obtain files status and send it to the master.

        Asynchronous task that is started when the worker connects to the master. It starts an integrity synchronization
        process every self.cluster_items['intervals']['worker']['sync_integrity'] seconds.

        A dictionary like {'file_path': {<BLAKE2b, merged, merged_name, etc>}, ...} is created and sent to the master,
        containing the information of all the files inside the directories specified in cluster.json. The master
        compares it with its own information.
        """
        logger = self.task_loggers["Integrity check"]
        integrity_check = c_common.SyncFiles(cmd=b'syn_i_w_m', logger=logger, manager=self)

        while True:
            try:
                if self.connected:
                    start_time = get_utc_now().timestamp()
                    if self.check_integrity_free and await integrity_check.request_permission():
                        logger.info("Starting.")
                        self.integrity_check_status['date_start'] = start_time
                        self.server.integrity_control = await cluster.run_in_pool(self.loop, self.server.task_pool,
                                                                           cluster.get_files_status,
                                                                           self.server.integrity_control)
                        await integrity_check.sync(files={}, files_metadata=self.server.integrity_control,
                                                   metadata_len=len(self.server.integrity_control),
                                                   task_pool=self.server.task_pool)
            # If exception is raised during sync process, notify the master so it removes the file if received.
            except Exception as e:
                logger.error(f"Error synchronizing integrity: {e}")
                if isinstance(e, exception.WazuhException):
                    exc = json.dumps(e, cls=c_common.WazuhJSONEncoder)
                else:
                    exc = json.dumps(exception.WazuhClusterError(1000, extra_message=str(e)),
                                     cls=c_common.WazuhJSONEncoder)
                with contextlib.suppress(Exception):
                    await self.send_request(command=b'syn_i_w_m_r', data=f"None {exc}".encode())

            await asyncio.sleep(self.cluster_items['intervals']['worker']['sync_integrity'])

    async def setup_sync_agent_info(self):
        """Obtain information from agents reporting this worker and send it to the master.

        Asynchronous task that is started when the worker connects to the master. It starts an agent-info
        synchronization process every 'sync_agent_info' seconds.

        A list of JSON chunks with the information of all local agents is retrieved from local wazuh-db socket
        and sent to the master's wazuh-db.
        """
        logger = self.task_loggers["Agent-info sync"]
        wdb_conn = WazuhDBConnection()
        sync_object = c_common.SyncWazuhdb(manager=self, logger=logger, cmd=b'syn_a_w_m',
                                           data_retriever=wdb_conn.run_wdb_command,
                                           get_data_command='global sync-agent-info-get ',
                                           set_data_command='global sync-agent-info-set')

        await self.general_agent_sync_task(sync_object=sync_object, timer=self.agent_info_sync_status,
                                           sleep_interval=self.cluster_items['intervals']['worker']['sync_agent_info'])

    async def setup_sync_agent_groups(self):
        """Obtain information about groups from agents reporting this worker and send it to the master.

        Asynchronous task that is started when the worker connects to the master. It starts an agent-groups
        synchronization process every 'sync_agent_groups' seconds.

        A list of JSON chunks with the information of all local agents is retrieved from local wazuh-db socket
        and sent to the master's wazuh-db.
        """
        logger = self.task_loggers["Agent-groups sync"]
        wdb_conn = WazuhDBConnection()
        sync_object = c_common.SyncWazuhdb(manager=self, logger=logger, cmd=b'syn_g_w_m',
                                           data_retriever=wdb_conn.run_wdb_command,
                                           get_data_command='global sync-agent-groups-get ',
                                           get_payload={'condition': 'sync_status', 'last_id': 0}, pivot_key='last_id',
                                           set_data_command='global set-agent-groups',
                                           set_payload={'mode': 'empty_only', 'sync_status': 'syncreq'})

        await self.general_agent_sync_task(sync_object=sync_object, timer=self.agent_groups_sync_status,
                                           sleep_interval=self.cluster_items['intervals']['worker'][
                                               'sync_agent_groups'])

    async def general_agent_sync_task(self, sync_object, timer, sleep_interval):
        """General body of the database synchronization tasks. Constant loop that performs the task
        for which it has been configured every X seconds.

        Parameters
        ----------
        sync_object : c_common.SyncWazuhdb
            Object in charge of synchronization with the database.
        timer : dict
            Dictionary with initial task time.
        sleep_interval : int
            Waiting time set between iterations.
        """
        while True:
            try:
                if self.connected:
                    start_time = get_utc_now().timestamp()
                    if await sync_object.request_permission():
                        sync_object.logger.info("Starting.")
                        timer['date_start'] = start_time
                        chunks = await sync_object.retrieve_information()
                        await sync_object.sync(start_time=start_time, chunks=chunks)
            except Exception as e:
                sync_object.logger.error(f"Error synchronizing agent information: {e}")

            await asyncio.sleep(sleep_interval)

    async def sync_extra_valid(self, extra_valid: Dict):
        """Merge and send files of the worker node that are missing in the master node.

        Asynchronous task that is started when the master requests any extra valid files to be synchronized.
        That means, it is started in the sync_integrity process.

        Parameters
        ----------
        extra_valid : dict
            Keys are paths of files missing in the master node.
        """
        logger = self.task_loggers["Integrity sync"]

        try:
            start_time = perf_counter()
            logger.debug("Starting sending extra valid files to master.")
            extra_valid_sync = c_common.SyncFiles(cmd=b'syn_e_w_m', logger=logger, manager=self)

            # Merge all agent-groups files into one and create metadata dict with it (key->filepath, value->metadata).
            # The 'TYPE' and 'RELATIVE_PATH' strings are placeholders to specify the type of merge we want to perform.
            n_files, merged_file = cluster.merge_info(merge_type='TYPE', node_name=self.name,
                                                      files=extra_valid.keys())
            files_to_sync = {merged_file: {'merged': True, 'merge_type': 'TYPE', 'merge_name': merged_file,
                                           'cluster_item_key': 'RELATIVE_PATH'}} if n_files else {}

            # Permission is not requested since it was already granted in the 'Integrity check' task.
            await extra_valid_sync.sync(files=files_to_sync, files_metadata=files_to_sync,
                                        metadata_len=len(files_to_sync), task_pool=self.server.task_pool)
            logger.debug(f"Finished sending extra valid files in {(perf_counter() - start_time):.3f}s.")
            logger.info(f"Finished in {(get_utc_now().timestamp() - self.integrity_sync_status['date_start']):.3f}s.")

        # If exception is raised during sync process, notify the master, so it removes the file if received.
        except Exception as e:
            logger.error(f"Error synchronizing extra valid files: {e}")
            if isinstance(e, exception.WazuhException):
                exc = json.dumps(e, cls=c_common.WazuhJSONEncoder)
            else:
                exc = json.dumps(exception.WazuhClusterError(1000, extra_message=str(e)), cls=c_common.WazuhJSONEncoder)
            with contextlib.suppress(Exception):
                await self.send_request(command=b'syn_i_w_m_r', data=f"None {exc}".encode())

    async def process_files_from_master(self, name: str, file_received: asyncio.Event):
        """Perform relevant actions for each file according to its status.

        Process integrity files coming from the master. It updates necessary information and sends the master
        any required extra_valid files.

        Parameters
        ----------
        name : str
            Task ID that was waiting for the file to be received.
        file_received : asyncio.Event
            Asyncio event that is unlocked once the file has been received.
        """
        logger = self.task_loggers['Integrity sync']

        await self.wait_for_file(file=file_received, task_id=name)

        # Path of the zip containing a JSON with metadata and files to be updated in this worker node.
        received_filename = self.sync_tasks[name].filename
        if isinstance(received_filename, Exception):
            exc_info = json.dumps(exception.WazuhClusterError(
                1000, extra_message=str(self.sync_tasks[name].filename)), cls=c_common.WazuhJSONEncoder)
            with contextlib.suppress(Exception):
                await self.send_request(command=b'syn_i_w_m_r', data=b'None ' + exc_info.encode())
            raise received_filename

        zip_path = ""

        try:
            self.integrity_sync_status['date_start'] = get_utc_now().timestamp()
            logger.info("Starting.")

            """
            - zip_path contains the path of the unzipped directory
            - ko_files contains a Dict with this structure:
              {'missing': {'<file_path>': {<BLAKE2b, merged, merged_name, etc>}, ...},
               'shared': {...}, 'extra': {...}, 'extra_valid': {...}}
            """
            ko_files, zip_path = await cluster.run_in_pool(self.loop, self.server.task_pool, cluster.decompress_files,
                                                           received_filename)
            logger.info(f"Files to create: {len(ko_files['missing'])} | Files to update: {len(ko_files['shared'])} "
                        f"| Files to delete: {len(ko_files['extra'])}")

            if ko_files['shared'] or ko_files['missing'] or ko_files['extra']:
                # Update or remove files in this worker node according to their status (missing, extra or shared).
                logger.debug("Worker does not meet integrity checks. Actions required.")
                logger.debug("Updating local files: Start.")
                await cluster.run_in_pool(self.loop, self.server.task_pool, self.update_master_files_in_worker,
                                          ko_files, zip_path, self.cluster_items, self.task_loggers['Integrity sync'])
                logger.debug("Updating local files: End.")

            logger.info(f"Finished in {get_utc_now().timestamp() - self.integrity_sync_status['date_start']:.3f}s.")
        except Exception as e:
            logger.error(f"Error synchronizing files: {e}")
            if isinstance(e, exception.WazuhException):
                exc = json.dumps(e, cls=c_common.WazuhJSONEncoder)
            else:
                exc = json.dumps(exception.WazuhClusterError(1000, extra_message=str(e)), cls=c_common.WazuhJSONEncoder)
            with contextlib.suppress(Exception):
                await self.send_request(command=b'syn_i_w_m_r', data=f"None {exc}".encode())
        finally:
            zip_path and shutil.rmtree(zip_path)

    @staticmethod
    def update_master_files_in_worker(ko_files: Dict, zip_path: str, cluster_items: Dict, logger):
        """Iterate over received files and updates them locally.

        Parameters
        ----------
        ko_files : dict
            File metadata coming from the master.
        zip_path : str
            Pathname of the unzipped directory received from master and containing the files to update.
        cluster_items : dict
            Object containing cluster internal variables from the cluster.json file.
        logger : Logger object
            Logger to use.
        """

        def overwrite_or_create_files(filename_: str, data_: Dict):
            """Update a file coming from the master.

            Move a file which is inside the unzipped directory that comes from master to the path
            specified in 'filename'. If the file is 'merged' type, it is first split into files
            and then moved to their final directory.

            Parameters
            ----------
            filename_ : str
                Filename inside unzipped dir to update.
            data_ : dict
                File metadata such as modification time, whether it's a merged file or not, etc.
            """
            full_filename_path = os.path.join(common.WAZUH_PATH, filename)

            if data_['merged']:  # worker nodes can only receive agent-groups files
                # Split merged file into individual files inside zipdir (directory containing unzipped files),
                # and then move each one to the destination directory (<wazuh_path>/filename).
                # The TYPE string used in the 'unmerge_info' function is a placeholder. It corresponds to the
                # directory inside '{wazuh_path}/queue/' path.
                for name, content, _ in cluster.unmerge_info('TYPE', zip_path, filename_):
                    full_unmerged_name = os.path.join(common.WAZUH_PATH, name)
                    tmp_unmerged_path = full_unmerged_name + '.tmp'
                    with open(tmp_unmerged_path, 'wb') as f:
                        f.write(content)
                    safe_move(tmp_unmerged_path, full_unmerged_name,
                              permissions=cluster_items['files'][data_['cluster_item_key']]['permissions'],
                              ownership=(common.wazuh_uid(), common.wazuh_gid())
                              )
            else:
                # Create destination dir if it doesn't exist.
                if not os.path.exists(os.path.dirname(full_filename_path)):
                    utils.mkdir_with_mode(os.path.dirname(full_filename_path))
                # Move the file from zipdir (directory containing unzipped files) to <wazuh_path>/filename.
                safe_move(os.path.join(zip_path, filename_), full_filename_path,
                          permissions=cluster_items['files'][data_['cluster_item_key']]['permissions'],
                          ownership=(common.wazuh_uid(), common.wazuh_gid())
                          )

        errors = {'shared': 0, 'missing': 0, 'extra': 0}

        for filetype, files in ko_files.items():
            # Overwrite local files marked as shared or missing.
            if filetype == 'shared' or filetype == 'missing':
                logger.debug(f"Received {len(ko_files[filetype])} {filetype} files to update from master.")
                for filename, data in files.items():
                    try:
                        logger.debug2(f"Processing file {filename}")
                        overwrite_or_create_files(filename, data)
                    except Exception as e:
                        errors[filetype] += 1
                        logger.error(f"Error processing {filetype} file '{filename}': {e}")
                        continue
            # Remove local files marked as extra.
            elif filetype == 'extra':
                for file_to_remove in files:
                    try:
                        logger.debug2(f"Remove file: '{file_to_remove}'")
                        file_path = os.path.join(common.WAZUH_PATH, file_to_remove)
                        try:
                            os.remove(file_path)
                        except OSError as e:
                            if e.errno == errno.ENOENT:
                                logger.debug2(f"File {file_to_remove} doesn't exist.")
                                continue
                            else:
                                raise e
                    except Exception as e:
                        errors['extra'] += 1
                        logger.debug2(f"Error removing file '{file_to_remove}': {e}")
                        continue

        # Once files are deleted, check and remove subdirectories which are now empty, as specified in cluster.json.
        directories_to_check = (os.path.dirname(f) for f, data in ko_files['extra'].items()
                                if cluster_items['files'][data['cluster_item_key']]['remove_subdirs_if_empty'])
        for directory in directories_to_check:
            try:
                full_path = os.path.join(common.WAZUH_PATH, directory)
                dir_files = set(os.listdir(full_path))
                if not dir_files or dir_files.issubset(set(cluster_items['files']['excluded_files'])):
                    shutil.rmtree(full_path)
            except Exception as e:
                errors['extra'] += 1
                logger.debug2(f"Error removing directory '{directory}': {e}")
                continue

        if sum(errors.values()) > 0:
            logger.error(f"Found errors: {errors['shared']} overwriting, {errors['missing']} creating and "
                         f"{errors['extra']} removing", exc_info=False)

    def get_logger(self, logger_tag: str = ''):
        """Get current logger. In workers it will always return the main logger.

        Parameters
        ----------
        logger_tag : str
            Logger tag to return.

        Returns
        -------
        Logger object
            A logger object.
        """
        return self.logger


class Worker(client.AbstractClientManager):
    """
    Initialize worker variables, connect to the master and run the DAPI request queue.
    """

    def __init__(self, **kwargs):
        """Class constructor.

        Parameters
        ----------
        kwargs
            Arbitrary keyword arguments to be sent as parameter to data_retriever callable.
        """
        self.task_pool = kwargs.pop('task_pool')
        super().__init__(**kwargs, tag="Worker")
        self.cluster_name = self.configuration['name']
        self.version = metadata.__version__
        self.node_type = self.configuration['node_type']
        self.handler_class = WorkerHandler
        self.extra_args = {'cluster_name': self.cluster_name, 'version': self.version, 'node_type': self.node_type}
        self.dapi = dapi.APIRequestQueue(server=self)
        self.integrity_control = {}

    def add_tasks(self) -> List[Tuple[asyncio.coroutine, Tuple]]:
        """Define the tasks that the worker will always run in an infinite loop.

        Returns
        -------
        List of tuples
            The first item is the coroutine to run and the second is the arguments it needs. In this case,
            all coroutines don't need arguments.
        """
        return super().add_tasks() + [(self.client.sync_integrity, tuple()),
                                      (self.client.setup_sync_agent_info, tuple()),
                                      (self.client.setup_sync_agent_groups, tuple()), (self.dapi.run, tuple())]

    def get_node(self) -> Dict:
        """Get basic information about the worker node. Used in the GET/cluster/node API call.

        Returns
        -------
        dict
            Basic node information.
        """
        return {'type': self.configuration['node_type'], 'cluster': self.configuration['name'],
                'node': self.configuration['node_name']}
