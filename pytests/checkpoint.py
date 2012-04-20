import time
import uuid
import logger
import unittest

from threading import Thread
from tasks.future import TimeoutError
from TestInput import TestInputSingleton
from tasks.taskmanager import TaskManager
from tasks.taskscheduler import TaskScheduler
from couchbase.stats_tools import StatsCommon
from membase.api.rest_client import RestConnection
from membase.helper.bucket_helper import BucketOperationHelper
from membase.helper.cluster_helper import ClusterOperationHelper
from memcached.helper.data_helper import MemcachedClientHelper

log = logger.Logger.get_logger()

ACTIVE="active"
REPLICA1="replica1"
REPLICA2="replica2"
Replica3="replica3"

class CheckpointTests(unittest.TestCase):

    def setUp(self):
        self.task_manager = TaskManager()
        self.task_manager.start()

        self.input = TestInputSingleton.input
        self.servers = self.input.servers
        self.num_servers = self.input.param("servers", 1)

        master = self.servers[0]
        num_replicas = self.input.param("replicas", 1)
        self.bucket = 'default'

        # Start: Should be in a before class function
        BucketOperationHelper.delete_all_buckets_or_assert(self.servers, self)
        for server in self.servers:
            ClusterOperationHelper.cleanup_cluster([server])
        ClusterOperationHelper.wait_for_ns_servers_or_assert([master], self)
        # End: Should be in a before class function

        self.quota = TaskScheduler.init_node(self.task_manager, master)
        self.old_vbuckets = self._get_vbuckets(master)
        ClusterOperationHelper.set_vbuckets(master, 1)
        TaskScheduler.bucket_create(self.task_manager, master, self.bucket,
                                    num_replicas, 12001, self.quota)
        TaskScheduler.rebalance(self.task_manager, self.servers[:self.num_servers],
                                self.servers[1:self.num_servers], [])

    def tearDown(self):
        master = self.servers[0]
        ClusterOperationHelper.set_vbuckets(master, self.old_vbuckets)
        rest = RestConnection(master)
        rest.stop_rebalance()
        TaskScheduler.rebalance(self.task_manager, self.servers[:self.num_servers], [],
                                self.servers[1:self.num_servers])
        TaskScheduler.bucket_delete(self.task_manager, master, self.bucket)
        self.task_manager.stop()

    def checkpoint_create_items(self):
        param = 'checkpoint'
        stat_key = 'vb_0:open_checkpoint_id'
        num_items = 6000

        master = self._get_server_by_state(self.servers[:self.num_servers], self.bucket, ACTIVE)
        self._set_checkpoint_size(self.servers[:self.num_servers], self.bucket, '5000')
        chk_stats = StatsCommon.get_stats(self.servers[:self.num_servers], self.bucket,
                                           param, stat_key)
        load_thread = self.generate_load(master, self.bucket, num_items)
        load_thread.join()
        stats = []
        for server, value in chk_stats.items():
            StatsCommon.build_stat_check(server, param, stat_key, '>', value, stats)
        task = TaskScheduler.async_wait_for_stats(self.task_manager, stats, self.bucket)
        try:
            timeout = 30 if (num_items * .001) < 30 else num_items * .001
            task.result(timeout)
        except TimeoutError:
            self.fail("New checkpoint not created")

    def checkpoint_create_time(self):
        param = 'checkpoint'
        timeout = 60
        stat_key = 'vb_0:open_checkpoint_id'

        master = self._get_server_by_state(self.servers[:self.num_servers], self.bucket, ACTIVE)
        self._set_checkpoint_timeout(self.servers[:self.num_servers], self.bucket, str(timeout))
        chk_stats = StatsCommon.get_stats(self.servers[:self.num_servers], self.bucket,
                                          param, stat_key)
        stats = []
        for server, value in chk_stats.items():
            StatsCommon.build_stat_check(server, param, stat_key, '>', value, stats)
        load_thread = self.generate_load(master, self.bucket, 1)
        load_thread.join()
        log.info("Sleeping for {0} seconds)".format(timeout))
        time.sleep(timeout)
        task = TaskScheduler.async_wait_for_stats(self.task_manager, stats, self.bucket)
        try:
            task.result(60)
        except TimeoutError:
            self.fail("New checkpoint not created")
        self._set_checkpoint_timeout(self.servers[:self.num_servers], self.bucket, str(600))

    def checkpoint_deduplication(self):
        param = 'checkpoint'
        stat_key = 'vb_0:num_checkpoint_items'

        master = self._get_server_by_state(self.servers[:self.num_servers], self.bucket, ACTIVE)
        slave1 = self._get_server_by_state(self.servers[:self.num_servers], self.bucket, REPLICA1)
        self._set_checkpoint_size(self.servers[:self.num_servers], self.bucket, '5000')
        self._stop_replication(slave1, self.bucket)
        load_thread = self.generate_load(master, self.bucket, 4500)
        load_thread.join()
        load_thread = self.generate_load(master, self.bucket, 1000)
        load_thread.join()
        self._start_replication(slave1, self.bucket)

        stats = []
        StatsCommon.build_stat_check(master, param, stat_key, '==', 4501, stats)
        StatsCommon.build_stat_check(slave1, param, stat_key, '==', 4501, stats)
        task = TaskScheduler.async_wait_for_stats(self.task_manager, stats, self.bucket)
        try:
            task.result(60)
        except TimeoutError:
            self.fail("Items weren't deduplicated")

    def _set_checkpoint_size(self, servers, bucket, size):
        for server in servers:
            client = MemcachedClientHelper.direct_client(server, bucket)
            client.set_flush_param('chk_max_items', size)

    def _set_checkpoint_timeout(self, servers, bucket, time):
        for server in servers:
            client = MemcachedClientHelper.direct_client(server, bucket)
            client.set_flush_param('chk_period', time)

    def _stop_replication(self, server, bucket):
        client = MemcachedClientHelper.direct_client(server, bucket)
        client.set_flush_param('tap_throttle_queue_cap', '0')

    def _start_replication(self, server, bucket):
        client = MemcachedClientHelper.direct_client(server, bucket)
        client.set_flush_param('tap_throttle_queue_cap', '1000000')

    def _get_vbuckets(self, server):
        rest = RestConnection(server)
        command = "ns_config:search(couchbase_num_vbuckets_default)"
        status, content = rest.diag_eval(command)

        try:
            vbuckets = int(re.sub('[^\d]', '', content))
        except:
            vbuckets = 1024
        return vbuckets

    def _get_server_by_state(self, servers, bucket, vb_state):
        rest = RestConnection(servers[0])
        vbuckets = rest.get_vbuckets(self.bucket)[0]
        addr = None
        if vb_state == ACTIVE:
            addr = vbuckets.master
        elif vb_state == REPLICA1:
            addr = vbuckets.replica[0].encode("ascii", "ignore")
        elif vb_state == REPLICA2:
            addr = vbuckets.replica[1].encode("ascii", "ignore")
        elif vb_state == REPLICA3:
            addr = vbuckets.replica[2].encode("ascii", "ignore")
        else:
            return None

        addr = addr.split(':', 1)[0]
        for server in servers:
            if addr == server.ip:
                return server
        return None

    def generate_load(self, server, bucket, num_items):
        class LoadGen(Thread):
            def __init__(self, server, bucket, num_items):
                Thread.__init__(self)
                self.server = server
                self.bucket = bucket
                self.num_items = num_items

            def run(self):
                client = MemcachedClientHelper.direct_client(server, bucket)
                for i in range(num_items):
                    key = "key-{0}".format(i)
                    value = "value-{0}".format(str(uuid.uuid4())[:7])
                    client.set(key, 0, 0, value, 0)
                log.info("Loaded {0} key".format(num_items))

        load_thread = LoadGen(server, bucket, num_items)
        load_thread.start()
        return load_thread


