#   Copyright 2011 Openstack, LLC.
#
#   Licensed under the Apache License, Version 2.0 (the "License"); you may
#   not use this file except in compliance with the License. You may obtain
#   a copy of the License at
#
#       http://www.apache.org/licenses/LICENSE-2.0
#
#   Unless required by applicable law or agreed to in writing, software
#   distributed under the License is distributed on an "AS IS" BASIS, WITHOUT
#   WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied. See the
#   License for the specific language governing permissions and limitations
#   under the License.

"""Connect your vlan to the world."""

import json

import webob.dec
import webob.exc

from nova import compute
from nova import context
from nova import db
from nova import exception
from nova import flags
from nova import log as logging
from nova import wsgi


LOG = logging.getLogger("nova.api.metadata.contrib.cloudpipe")


FLAGS = flags.FLAGS
flags.DECLARE('use_forwarded_for', 'nova.api.auth')


class BastionKeyMetadata(wsgi.Application):
    """A wsgi app to serve public key lists for a project."""

    def __init__(self):
        self.compute_api = compute.API()

    def _get_users_in_project(self, project_id):
        # NOTE(todd): this only finds users with running instances
        adm = context.get_admin_context()
        search = {'project_id': project_id, 'deleted': False}
        instances = self.compute_api.get_all(adm, search_opts=search)
        user_ids = list(set([i['user_id'] for i in instances]))
        return user_ids

    @webob.dec.wsgify(RequestClass=wsgi.Request)
    def __call__(self, req):
        address = req.remote_addr
        if FLAGS.use_forwarded_for:
            address = req.headers.get('X-Forwarded-For', address)
        ctxt = context.get_admin_context()
        filters = {'fixed_ip': address, 'deleted': False}
        instance = None
        try:
            instance = self.compute_api.get_all(ctxt, search_opts=filters)[0]
        except exception.NotFound:
            return webob.exc.HTTPNotFound()
        if instance['image_ref'] != FLAGS.bastion_image_id:
            return webob.exc.HTTPNotFound()
        rv = {}
        users_in_project = self._get_users_in_project(instance['project_id'])
        for uid in users_in_project:
            rv[uid] = {}
            for key in db.key_pair_get_all_by_user(ctxt, uid):
                rv[uid][key['name']] = key['public_key']
        return json.dumps(rv)
