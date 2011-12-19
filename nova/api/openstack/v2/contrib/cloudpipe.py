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
import os

import webob.dec
import webob.exc

from nova.api.openstack import wsgi
from nova.api.openstack import xmlutil
from nova.api.openstack.v2 import extensions
from nova.auth import manager
from nova.cloudpipe import pipelib
from nova import compute
from nova.compute import vm_states
from nova import context
from nova import db
from nova import exception
from nova import flags
from nova import log as logging
from nova import utils
from nova import wsgi as nova_wsgi


FLAGS = flags.FLAGS
LOG = logging.getLogger("nova.api.openstack.v2.contrib.cloudpipe")


class CloudpipeController(object):
    """Handle creating and listing cloudpipe instances."""

    def __init__(self):
        self.compute_api = compute.API()
        self.auth_manager = manager.AuthManager()
        self.cloudpipe = pipelib.CloudPipe()
        self.setup()

    def setup(self):
        """Ensure the keychains and folders exist."""
        # TODO(todd): this was copyed from api.ec2.cloud
        # FIXME(ja): this should be moved to a nova-manage command,
        # if not setup throw exceptions instead of running
        # Create keys folder, if it doesn't exist
        if not os.path.exists(FLAGS.keys_path):
            os.makedirs(FLAGS.keys_path)
        # Gen root CA, if we don't have one
        root_ca_path = os.path.join(FLAGS.ca_path, FLAGS.ca_file)
        if not os.path.exists(root_ca_path):
            genrootca_sh_path = os.path.join(os.path.dirname(__file__),
                                             os.path.pardir,
                                             os.path.pardir,
                                             'CA',
                                             'genrootca.sh')

            start = os.getcwd()
            if not os.path.exists(FLAGS.ca_path):
                os.makedirs(FLAGS.ca_path)
            os.chdir(FLAGS.ca_path)
            # TODO(vish): Do this with M2Crypto instead
            utils.runthis(_("Generating root CA: %s"), "sh", genrootca_sh_path)
            os.chdir(start)

    def _get_cloudpipe_for_project(self, ctxt, project_id, image_id):
        """Get the cloudpipe instance for a project ID."""
        # NOTE(todd): this should probably change to compute_api.get_all
        #             or db.instance_get_project_vpn
        for instance in db.instance_get_all_by_project(ctxt, project_id):
            if (instance['image_ref'] == str(image_id)
                and not instance['vm_state'] in [vm_states.DELETED]):
                return instance

    def _cloudpipe_dict(self, project, vpn_instance, bastion_instance):
        rv = {'project_id': project.id,
              'public_ip': project.vpn_ip,
              'public_port': project.vpn_port}
        if vpn_instance:
            rv['vpn_instance_id'] = vpn_instance['uuid']
            rv['vpn_created_at'] = utils.isotime(vpn_instance['created_at'])
            address = vpn_instance.get('fixed_ip', None)
            if address:
                rv['vpn_internal_ip'] = address['address']
            if project.vpn_ip and project.vpn_port:
                if utils.vpn_ping(project.vpn_ip, project.vpn_port):
                    rv['vpn_state'] = 'running'
                else:
                    rv['vpn_state'] = 'down'
            else:
                rv['vpn_state'] = 'invalid'
        else:
            rv['vpn_state'] = 'pending'
        if bastion_instance:
            rv['bastion_instance_id'] = bastion_instance['uuid']
            rv['bastion_created_at'] = utils.isotime(
                    bastion_instance['created_at'])
            address = bastion_instance.get('fixed_ip', None)
            rv['bastion_state'] = 'running'
        else:
            rv['bastion_state'] = 'pending'
        return rv

    def create(self, req, body={}):
        """Create a new cloudpipe instance, if none exists.
        
        To create a VPN, make a request as an administrator and pass in
        {cloudpipe: {project_id: XYZ}}

        To create a SSH Bastion, make a request to this endpoint as an
        unprivileged user.

        """

        ctxt = req.environ['nova.context']
        params = body.get('cloudpipe', {})
        if ctxt.is_admin and params.has_key('project_id'):
            return self._create_vpn_instance(ctxt, params['project_id'])
        else:
            return self._create_bastion_instance(ctxt)

    def _create_vpn_instance(self, ctxt, project_id):
        instance = self._get_cloudpipe_for_project(ctxt, project_id,
                                                   FLAGS.vpn_image_id)
        if not instance:
            proj = self.auth_manager.get_project(project_id)
            user_id = proj.project_manager_id
            try:
                self.cloudpipe.launch_vpn_instance(project_id, user_id)
            except db.NoMoreNetworks:
                msg = _("Unable to claim IP for VPN instances, ensure it "
                        "isn't running, and try again in a few minutes")
                raise exception.ApiError(msg)
            instance = self._get_cloudpipe_for_project(ctxt, project_id,
                                                       FLAGS.vpn_image_id)
        return {'instance_id': instance['uuid']}

    def _create_bastion_instance(self, ctxt):
        project_id = ctxt.project_id
        instance = self._get_cloudpipe_for_project(ctxt, project_id,
                                                   FLAGS.bastion_image_id)
        if not instance:
            proj = self.auth_manager.get_project(project_id)
            user_id = proj.project_manager_id
            self.cloudpipe.launch_bastion_instance(proj)
            instance = self._get_cloudpipe_for_project(ctxt, proj.id,
                                                       FLAGS.bastion_image_id)
        return {'instance_id': instance['uuid']}

    def index(self, req):
        """Show admins the list of running cloudpipe instances."""
        ctxt = req.environ['nova.context']
        vpns = []
        if ctxt.is_admin:
            # show all projects with vpn & bastion listed
            # TODO(todd): could use compute_api.get_all with admin context?
            for project in self.auth_manager.get_projects():
                vpn = self._get_cloudpipe_for_project(ctxt, project.id,
                                                      FLAGS.vpn_image_id)
                bastion = self._get_cloudpipe_for_project(ctxt, project.id,
                        FLAGS.bastion_image_id)
                vpns.append(self._cloudpipe_dict(project, vpn, bastion))
        else:
            # show just for the unprivileged user's project
            project = self.auth_manager.get_project(ctxt.project_id)
            vpn = self._get_cloudpipe_for_project(ctxt, project.id,
                                                  FLAGS.vpn_image_id)
            bastion = self._get_cloudpipe_for_project(ctxt, project.id,
                                                      FLAGS.bastion_image_id)
            vpns.append(self._cloudpipe_dict(project, vpn, bastion))
        return {'cloudpipes': vpns}


class Cloudpipe(extensions.ExtensionDescriptor):
    """Adds actions to create cloudpipe instances.

    When running with the Vlan network mode, you need a mechanism to route
    from the public Internet to your vlans.  This mechanism is known as a
    cloudpipe.

    At the time of creating this class, only OpenVPN is supported.  Support for
    a SSH Bastion host is forthcoming.
    """

    name = "Cloudpipe"
    alias = "os-cloudpipe"
    namespace = "http://docs.openstack.org/ext/cloudpipe/api/v1.1"
    updated = "2011-12-16T00:00:00+00:00"

    def get_resources(self):
        resources = []
        body_serializers = {
            'application/xml': CloudpipeSerializer(),
            }
        serializer = wsgi.ResponseSerializer(body_serializers)
        res = extensions.ResourceExtension('os-cloudpipe',
                                           CloudpipeController(),
                                           serializer=serializer)
        resources.append(res)
        return resources

class CloudpipeSerializer(xmlutil.XMLTemplateSerializer):
    def index(self):
        return CloudpipesTemplate()

    def default(self):
        return CloudpipeTemplate()

class CloudpipeTemplate(xmlutil.TemplateBuilder):
    def construct(self):
        return xmlutil.MasterTemplate(xmlutil.make_flat_dict('cloudpipe'), 1)


class CloudpipesTemplate(xmlutil.TemplateBuilder):
    def construct(self):
        root = xmlutil.TemplateElement('cloudpipes')
        elem = xmlutil.make_flat_dict('cloudpipe', selector='cloudpipes',
                                      subselector='cloudpipe')
        root.append(elem)
        return xmlutil.MasterTemplate(root, 1)

class KeyMetadata(nova_wsgi.Application):
    """A wsgi app to serve public key lists for a project."""

    def __init__(self):
        self.auth_manager = manager.AuthManager()
        self.compute_api = compute.API(network_api=network.API(),
                                       volume_api=volume.API())

    @webob.dec.wsgify(RequestClass=wsgi.Request)
    def __call__(self, req):
        address = req.remote_addr
        if FLAGS.use_forward_for:
            address = req.headers.get('X-Forwarded-For', address)
        ctxt = context.get_admin_context()
        filters = {'fixed_ip': address, 'deleted': False}
        instance = None
        try:
            instance = self.compute_api.get_all(ctxt, search_opts=filters)
        except exception.NotFound:
            return webob.exc.HTTPNotFound()
        if instance['image_ref'] != FLAGS.bastion_image_id:
            return webob.exc.HTTPNotFound()
        rv = {}
        project = self.auth_manager.get_project(instance['project_id'])
        for uid in project.member_ids:
            rv[uid] = {}
            for key in db.key_pair_get_all_by_user(ctxt, uid):
                rv[uid][key['name']] = key['public_key']
        return json.dumps(rv)
