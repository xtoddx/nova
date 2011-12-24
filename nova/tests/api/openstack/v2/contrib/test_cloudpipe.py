# Copyright 2011 OpenStack LLC.
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
import json

import webob
from lxml import etree

from nova.api.openstack.v2 import wsgi
from nova.api.openstack.v2.contrib import cloudpipe
from nova.auth import manager
from nova.cloudpipe import pipelib
from nova import context
from nova import crypto
from nova import db
from nova import flags
from nova import test
from nova.tests.api.openstack import fakes
from nova import utils


EMPTY_INSTANCE_LIST = True
FLAGS = flags.FLAGS


class FakeProject(object):
    def __init__(self, id, name, manager, desc, members, ip, port):
        self.id = id
        self.name = name
        self.project_manager_id = manager
        self.description = desc
        self.member_ids = members
        self.vpn_ip = ip
        self.vpn_port = port


def fake_vpn_instance():
    return {'id': 7, 'image_ref': FLAGS.vpn_image_id, 'vm_state': 'active',
            'created_at': utils.parse_strtime('1981-10-20T00:00:00.000000'),
            'uuid': 7777}


def fake_project():
    proj = FakeProject(1, 'fake', 'fakeuser', '', [1], '127.0.0.1', 22)
    return proj


def db_instance_get_all_by_project(self, project_id):
    if EMPTY_INSTANCE_LIST:
        return []
    else:
        return [fake_vpn_instance()]


def db_security_group_exists(context, project_id, group_name):
    # used in pipelib
    return True


def pipelib_launch_vpn_instance(self, project_id, user_id):
    global EMPTY_INSTANCE_LIST
    EMPTY_INSTANCE_LIST = False


def auth_manager_get_project(self, project_id):
    return fake_project()


def auth_manager_get_projects(self):
    return [fake_project()]


def utils_vpn_ping(addr, port, timoeout=0.05, session_id=None):
    return True


def better_not_call_this(*args, **kwargs):
    raise Exception("You should not have done that")


class CloudpipeVpnTest(test.TestCase):

    def setUp(self):
        super(CloudpipeVpnTest, self).setUp()
        self.flags(allow_admin_api=True, bastion_image_id='bastion',
                   vpn_image_id='vpn')
        self.controller = cloudpipe.CloudpipeController()
        fakes.stub_out_networking(self.stubs)
        fakes.stub_out_rate_limiting(self.stubs)
        self.stubs.Set(db, "instance_get_all_by_project",
                       db_instance_get_all_by_project)
        self.stubs.Set(db, "security_group_exists",
                       db_security_group_exists)
        self.stubs.SmartSet(self.controller.cloudpipe, "launch_vpn_instance",
                            pipelib_launch_vpn_instance)
        self.stubs.SmartSet(self.controller.auth_manager, "get_project",
                            auth_manager_get_project)
        self.stubs.SmartSet(self.controller.auth_manager, "get_projects",
                            auth_manager_get_projects)
        self.stubs.Set(utils, 'vpn_ping', utils_vpn_ping)
        self.admin_context = context.get_admin_context()
        self.app = fakes.wsgi_app(fake_auth_context=self.admin_context)
        global EMPTY_INSTANCE_LIST
        EMPTY_INSTANCE_LIST = True

    def test_cloudpipe_list_none_running(self):
        """Should still get an entry per-project, just less descriptive."""
        req = webob.Request.blank('/v2/123/os-cloudpipe')
        res = req.get_response(self.app)
        self.assertEqual(res.status_int, 200)
        res_dict = json.loads(res.body)
        response = {'cloudpipes': [{'project_id': 1, 'public_ip': '127.0.0.1',
                    'public_port': 22, 'vpn_state': 'pending',
                    'bastion_state': 'pending'}]}
        self.assertEqual(res_dict, response)

    def test_cloudpipe_list(self):
        global EMPTY_INSTANCE_LIST
        EMPTY_INSTANCE_LIST = False
        req = webob.Request.blank('/v2/123/os-cloudpipe')
        res = req.get_response(self.app)
        self.assertEqual(res.status_int, 200)
        res_dict = json.loads(res.body)
        response = {'cloudpipes': [{'project_id': 1, 'public_ip': '127.0.0.1',
                     'public_port': 22, 'vpn_state': 'running',
                     'bastion_state': 'pending', 'vpn_instance_id': 7777,
                     'vpn_created_at': '1981-10-20T00:00:00Z'}]}
        self.assertEqual(res_dict, response)

    def test_cloudpipe_create(self):
        body = {'cloudpipe': {'project_id': 1}}
        req = webob.Request.blank('/v2/123/os-cloudpipe')
        req.method = 'POST'
        req.body = json.dumps(body)
        req.headers['Content-Type'] = 'application/json'
        res = req.get_response(self.app)
        self.assertEqual(res.status_int, 200)
        res_dict = json.loads(res.body)
        response = {'instance_id': 7777}
        self.assertEqual(res_dict, response)

    def test_cloudpipe_create_already_running(self):
        global EMPTY_INSTANCE_LIST
        EMPTY_INSTANCE_LIST = False
        self.stubs.SmartSet(self.controller.cloudpipe, 'launch_vpn_instance',
                            better_not_call_this)
        body = {'cloudpipe': {'project_id': 1}}
        req = webob.Request.blank('/v2/123/os-cloudpipe')
        req.method = 'POST'
        req.body = json.dumps(body)
        req.headers['Content-Type'] = 'application/json'
        res = req.get_response(self.app)
        self.assertEqual(res.status_int, 200)
        res_dict = json.loads(res.body)
        response = {'instance_id': 7777}
        self.assertEqual(res_dict, response)


class CloudpipeBastionTest(test.TestCase):

    def setUp(self):
        super(CloudpipeBastionTest, self).setUp()
        self.flags(bastion_image_id='bastion', vpn_image_id='vpn')
        self.controller = cloudpipe.CloudpipeController()
        fakes.stub_out_networking(self.stubs)
        fakes.stub_out_rate_limiting(self.stubs)
        self.stubs.Set(db, "instance_get_all_by_project",
                       self._stub_instance_get_all)
        self.stubs.Set(db, "security_group_exists",
                       db_security_group_exists)
        self.stubs.SmartSet(self.controller.cloudpipe,
                            "launch_bastion_instance",
                            self._stub_launch)
        self.stubs.SmartSet(self.controller.auth_manager, "get_project",
                            auth_manager_get_project)
        self.stubs.SmartSet(self.controller.auth_manager, "get_projects",
                            auth_manager_get_projects)
        self.app = fakes.wsgi_app()
        self.running_instances = []

    def _fake_instance(self, id=7, image_uuid='bastion'):
        return {'id': id, 'image_ref': image_uuid, 'vm_state': 'active',
                'created_at': utils.parse_strtime('1981-10-20T00:00:00.000000'),
                'uuid': id}

    def _stub_instance_get_all(self, *args, **kwargs):
        return self.running_instances

    def _stub_launch(self, *args, **kwargs):
        i = self._fake_instance()
        self.running_instances.append(i)
        return i['id']

    def test_cloudpipe_list(self):
        # fake a running instance
        self.running_instances.append(self._fake_instance(29))
        req = webob.Request.blank('/v2/fake/os-cloudpipe')
        res = req.get_response(self.app)
        self.assertEqual(res.status_int, 200)
        res_dict = json.loads(res.body)
        response = {'cloudpipes': [{'project_id': 1, 'public_ip': '127.0.0.1',
                     'public_port': 22, 'vpn_state': 'pending',
                     'bastion_state': 'running', 'bastion_instance_id': 29,
                     'bastion_created_at': '1981-10-20T00:00:00Z'}]}
        self.assertEqual(res_dict, response)

    def test_create_bastion(self):
        req = webob.Request.blank('/v2/fake/os-cloudpipe')
        req.method = 'POST'
        res = req.get_response(self.app)
        self.assertEqual(res.status_int, 200)
        res_dict = json.loads(res.body)
        response = {'instance_id': 7}
        self.assertEqual(res_dict, response)

    def test_create_bastion_already_running(self):
        # fake an instance with a unique id
        self.running_instances.append(self._fake_instance(999))
        self.stubs.SmartSet(self.controller.cloudpipe, 'launch_vpn_instance',
                            better_not_call_this)
        req = webob.Request.blank('/v2/fake/os-cloudpipe')
        req.method = 'POST'
        res = req.get_response(self.app)
        self.assertEqual(res.status_int, 200)
        res_dict = json.loads(res.body)
        response = {'instance_id': 999}
        self.assertEqual(res_dict, response)


class CloudpipesXMLSerializerTest(test.TestCase):
    def setUp(self):
        super(CloudpipesXMLSerializerTest, self).setUp()
        self.serializer = cloudpipe.CloudpipeSerializer()
        self.deserializer = wsgi.XMLDeserializer()

    def test_default_serializer(self):
        exemplar = dict(cloudpipe=dict(instance_id='1234-1234-1234-1234'))
        text = self.serializer.serialize(exemplar)
        tree = etree.fromstring(text)
        self.assertEqual('cloudpipe', tree.tag)
        for child in tree:
            self.assertTrue(child.tag in exemplar['cloudpipe'])
            self.assertEqual(child.text, exemplar['cloudpipe'][child.tag])

    def test_index_serializer(self):
        exemplar = dict(cloudpipes=[
                dict(cloudpipe=dict(
                        project_id='1234',
                        public_ip='1.2.3.4',
                        public_port='321',
                        instance_id='1234-1234-1234-1234',
                        created_at=utils.isotime(datetime.datetime.utcnow()),
                        state='running')),
                dict(cloudpipe=dict(
                        project_id='4321',
                        public_ip='4.3.2.1',
                        public_port='123',
                        state='pending'))])
        text = self.serializer.serialize(exemplar, 'index')
        tree = etree.fromstring(text)
        self.assertEqual('cloudpipes', tree.tag)
        self.assertEqual(len(exemplar['cloudpipes']), len(tree))
        for idx, cloudpipe in enumerate(tree):
            self.assertEqual('cloudpipe', cloudpipe.tag)
            kp_data = exemplar['cloudpipes'][idx]['cloudpipe']
            for child in cloudpipe:
                self.assertTrue(child.tag in kp_data)
                self.assertEqual(child.text, kp_data[child.tag])

    def test_deserializer(self):
        exemplar = dict(cloudpipe=dict(project_id='4321'))
        intext = ("<?xml version='1.0' encoding='UTF-8'?>\n"
                  '<cloudpipe><project_id>4321</project_id></cloudpipe>')
        result = self.deserializer.deserialize(intext)['body']
        self.assertEqual(result, exemplar)

