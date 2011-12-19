# Copyright 2010 United States Government as represented by the
# Administrator of the National Aeronautics and Space Administration.
# All Rights Reserved.
#
#    Licensed under the Apache License, Version 2.0 (the "License"); you may
#    not use this file except in compliance with the License. You may obtain
#    a copy of the License at
#
#         http://www.apache.org/licenses/LICENSE-2.0
#
#    Unless required by applicable law or agreed to in writing, software
#    distributed under the License is distributed on an "AS IS" BASIS, WITHOUT
#    WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied. See the
#    License for the specific language governing permissions and limitations
#    under the License.

import datetime

from nova.auth import manager
from nova.cloudpipe import pipelib
from nova import compute
from nova import context
from nova import db
from nova import flags
import nova.image
from nova import rpc
from nova.scheduler import driver
from nova import test
from nova import utils


FLAGS = flags.FLAGS


class FakeComputeAPI(object):
    def create(self, context, **kwargs):
        self.context = context
        self.kwargs = kwargs
        return {}


class FakeSchedulerDriver(driver.Scheduler):
    def schedule_run_instance(self, context, request_spec, *_args, **kwargs):
        elevated = context.elevated()
        instance = self.create_instance_db_entry(elevated, request_spec)
        host = "test_host"
        driver.cast_to_compute_host(context, host, 'run_instance',
                                    instance_uuid=instance['uuid'], **kwargs)
        return [instance]


def get_secgroup(context, project_id, name):
    return True


class CloudpipeTestCase(test.TestCase):
    def setUp(self):
        super(CloudpipeTestCase, self).setUp()
        self.flags(bastion_image_id='bastion', bastion_secgroup_suffix='_foo')
        self.cloudpipe = pipelib.CloudPipe()

    def test_launch_bastion_instance(self):
        # we want to check it calls compute API .create with right params
        fake_api = FakeComputeAPI()
        project_manager_id = 17
        project = manager.Project(1, 'fake', project_manager_id,
                                  'fake project', [project_manager_id, 14])
        self.cloudpipe.launch_bastion_instance(project, fake_api)
        self.assertEqual(1, fake_api.context.project_id)
        self.assertEqual(17, fake_api.context.user_id)
        expected_kwargs = {'min_count': 1, 'max_count': 1,
                           'security_group': ['1_foo'],
                           'image_href': 'bastion',
                           'instance_type': {
                               'local_gb': 0,
                               'name': 'm1.tiny',
                               'deleted': False,
                               'created_at': None,
                               'updated_at': None,
                               'memory_mb': 512,
                               'vcpus': 1,
                               'flavorid': '1',
                               'swap': 0,
                               'rxtx_factor': 0.0,
                               'extra_specs': {},
                               'deleted_at': None,
                               'vcpu_weight': None,
                               'id': 2}}

        self.assertEqual(expected_kwargs, fake_api.kwargs)

    def test_bastion_network_association(self):
        admin = context.get_admin_context()
        ctxt = None
        (image_service, _h) = nova.image.get_image_service(ctxt,
                                                           image_href='bastion')
        timestamp = datetime.datetime.utcnow()
        bastion_image_metadata = {'id': 'bastion',
                                  'name': 'bastion',
                                  'created_at': timestamp,
                                  'updated_at': timestamp,
                                  'deleted_at': None,
                                  'deleted': False,
                                  'status': 'active',
                                  'is_public': True,
                                  'container_format': 'raw',
                                  'disk_format': 'raw',
                                  'properties': {
                                      'kernel_id': FLAGS.null_kernel,
                                      'ramdisk_id': FLAGS.null_kernel,
                                      'architecture': 'x86_64'}}
        image_service.create(ctxt, bastion_image_metadata)

        # set up RPC for scheduler
        scheduler = utils.import_class(FLAGS.scheduler_manager)
        scheduler_driver = 'nova.tests.test_cloudpipe.FakeSchedulerDriver' #()
        scheduler = scheduler(scheduler_driver)
        rpc_conn = rpc.create_connection(new=True)
        rpc_conn.create_consumer(FLAGS.scheduler_topic, scheduler, fanout=False)

        # set up RPC for compute
        compute_mgr = utils.import_object(FLAGS.compute_manager)
        rpc_conn.create_consumer(FLAGS.compute_topic, compute_mgr, fanout=False)
        rpc_conn.create_consumer(FLAGS.compute_topic + '.test_host',
                                 compute_mgr, fanout=False)

        # We want to check args to compute_mgr.network_api.allocate_for_instance
        # But we want to call the original still, so keep it around
        compute_mgr.network_api._old_afi = \
                compute_mgr.network_api.allocate_for_instance
        api_kwargs = {}
        def _fake_allocate_for_instance(self, context, instance, **kwargs):
            api_kwargs.update(kwargs)
            self._old_afi(context, instance, **kwargs)
        self.stubs.SmartSet(compute_mgr.network_api, 'allocate_for_instance',
                            _fake_allocate_for_instance)

        # set up the network manager
        net_mgr = utils.import_object(FLAGS.network_manager)
        rpc_conn.create_consumer(FLAGS.network_topic, net_mgr, fanout=False)

        # create a floating address to be allocated
        db.floating_ip_create(admin, {'address': '1.2.3.4'})

        # Now call to create bastion, so we can trap what comes along
        compute_api = compute.API(image_service=image_service)
        project_manager_id = '17'
        project = manager.Project('1', 'fake', project_manager_id,
                                  'fake project', [project_manager_id, '14'])
        rv = self.cloudpipe.launch_bastion_instance(project, compute_api)

        # make sure the cloudpipe bits got passed around correctly
        self.assertTrue(api_kwargs['bastion'])
        self.assertFalse(api_kwargs['vpn'])

        # check floating ip association
        instances, reservation = rv
        instance = compute_api.get(admin, instances[0]['uuid'])
        fixed = dict(instance['fixed_ips'][0])
        floating = compute_mgr.network_api.get_floating_ips_by_fixed_address(\
                    admin, fixed['address'])
        self.assertEqual('1.2.3.4', floating[0])
